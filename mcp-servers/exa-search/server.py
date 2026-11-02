# /// script
# requires-python = ">=3.10"
# dependencies = [
#     "mcp<2",
#     "exa-py>=1.8.0",
#     "httpx",
# ]
# ///

import os
import re
import sys
import json
import time
import sqlite3
import hashlib
import asyncio
from pathlib import Path
from datetime import datetime, timezone
from typing import Optional, List, Dict, Any, Tuple
from exa_py import Exa
from mcp.server.fastmcp import FastMCP
from mcp.types import Tool as MCPTool

mcp = FastMCP("ExaSearchGrounding")

MAX_PROMPT_CHARS = 200_000  # Защита от переполнения контекста (~50,000 токенов)

BASE_DIR = Path(__file__).resolve().parent
CONFIG_FILE = BASE_DIR / "config.json"


def load_config() -> dict:
    defaults = {
        "storage_dir": "/var/tmp/scratch/exa_library",
        "retention_days": 30,
        "proxy_url": "",
        "default_num_results": 5,
        "default_max_characters": 2500,
        "highlights": True,
    }
    if CONFIG_FILE.exists():
        try:
            user_conf = json.loads(CONFIG_FILE.read_text(encoding="utf-8"))
            defaults.update(user_conf)
        except Exception as e:
            sys.stderr.write(f"[EXA-CONFIG] Ошибка чтения config.json: {e}\n")
    return defaults


CONFIG = load_config()

# Разрешение пути хранения: абсолютный или относительный от BASE_DIR
raw_storage = Path(CONFIG.get("storage_dir", "/var/tmp/scratch/exa_library"))
LIBRARY_DIR = raw_storage if raw_storage.is_absolute() else (BASE_DIR / raw_storage).resolve()
LIBRARY_DB_PATH = LIBRARY_DIR / "library.db"
REPORTS_DIR = LIBRARY_DIR / "reports"
LIBRARY_RETENTION_DAYS = int(CONFIG.get("retention_days", 30))

# Проксирование трафика: по умолчанию прямое соединение (без прокси)
DEFAULT_PROXY = (os.environ.get("EXA_PROXY") or CONFIG.get("proxy_url", "")).strip()
if DEFAULT_PROXY:
    os.environ["HTTP_PROXY"] = DEFAULT_PROXY
    os.environ["HTTPS_PROXY"] = DEFAULT_PROXY
    os.environ["all_proxy"] = DEFAULT_PROXY

KEYS_FILE = BASE_DIR / "keys.txt"
STATUS_FILE = BASE_DIR / "key_status.json"


def slugify(text: str) -> str:
    text = re.sub(r"[^\w\s-]", "", text, flags=re.UNICODE).strip().lower()
    return re.sub(r"[-\s]+", "_", text)[:50]


class KeyBalancer:
    """
    Модуль балансировщика API-ключей Exa:
    - Читает keys.txt (по одному ключу на строку)
    - Ведет key_status.json с учетом расходов и ошибок
    - Round-Robin распределение между активными счетами
    - Автоматическая изоляция исчерпанных ключей (HTTP 402/403)
    - Потокобезопасен (asyncio.Lock)
    """
    def __init__(self, keys_path: Path, status_path: Path):
        self.keys_path = keys_path
        self.status_path = status_path
        self._index = 0
        self._lock = asyncio.Lock()
        self._ensure_files()

    def _ensure_files(self):
        if not self.keys_path.exists():
            self.keys_path.write_text("# Список API ключей Exa.ai (по одному на строку)\n", encoding="utf-8")
        if not self.status_path.exists():
            self.status_path.write_text("{}", encoding="utf-8")

    def _load_status(self) -> dict:
        try:
            if self.status_path.exists():
                return json.loads(self.status_path.read_text(encoding="utf-8"))
        except Exception:
            pass
        return {}

    def _save_status(self, status: dict):
        try:
            self.status_path.write_text(json.dumps(status, indent=2, ensure_ascii=False), encoding="utf-8")
        except Exception as e:
            sys.stderr.write(f"[EXA-BALANCER] Ошибка сохранения статуса: {e}\n")

    def load_keys(self) -> list[str]:
        keys = []
        if self.keys_path.exists():
            for line in self.keys_path.read_text(encoding="utf-8").splitlines():
                line = line.strip()
                if line and not line.startswith("#"):
                    keys.append(line)
        env_keys = os.environ.get("EXA_API_KEYS", "") or os.environ.get("EXA_API_KEY", "")
        if env_keys:
            for k in env_keys.replace(";", ",").split(","):
                k = k.strip()
                if k and k not in keys:
                    keys.append(k)
        return keys

    async def get_active_key(self) -> tuple[str, list[str]]:
        async with self._lock:
            all_keys = self.load_keys()
            if not all_keys:
                return "", []

            status = self._load_status()
            dirty = False
            for k in all_keys:
                if k not in status:
                    status[k] = {
                        "status": "active",
                        "total_spent_usd": 0.0,
                        "total_requests": 0,
                        "last_used_at": None,
                        "error_message": None
                    }
                    dirty = True
            if dirty:
                self._save_status(status)

            active_keys = [k for k in all_keys if status.get(k, {}).get("status") == "active"]
            if not active_keys:
                return "", all_keys

            self._index = (self._index + 1) % len(active_keys)
            return active_keys[self._index], active_keys

    async def mark_exhausted(self, key: str, reason: str):
        async with self._lock:
            status = self._load_status()
            if key not in status:
                status[key] = {"total_spent_usd": 0.0, "total_requests": 0}
            status[key]["status"] = "exhausted"
            status[key]["last_used_at"] = datetime.now(timezone.utc).isoformat()
            status[key]["error_message"] = reason
            self._save_status(status)
            sys.stderr.write(f"[EXA-BALANCER] Ключ {key[:6]}... помечен как ИСЧЕРПАН: {reason}\n")

    async def record_success(self, key: str, cost: float):
        async with self._lock:
            status = self._load_status()
            if key not in status:
                status[key] = {"status": "active", "total_spent_usd": 0.0, "total_requests": 0}
            status[key]["status"] = "active"
            status[key]["total_spent_usd"] = round(status[key].get("total_spent_usd", 0.0) + cost, 5)
            status[key]["total_requests"] = status[key].get("total_requests", 0) + 1
            status[key]["last_used_at"] = datetime.now(timezone.utc).isoformat()
            status[key]["error_message"] = None
            self._save_status(status)

    async def add_new_key(self, new_key: str) -> str:
        async with self._lock:
            clean = new_key.strip()
            if not clean:
                return "Ошибка: передан пустой ключ."
            keys = self.load_keys()
            if clean in keys:
                return f"Ключ {clean[:6]}... уже есть в пуле."

            with open(self.keys_path, "a", encoding="utf-8") as f:
                f.write(f"\n{clean}\n")

            status = self._load_status()
            status[clean] = {
                "status": "active",
                "total_spent_usd": 0.0,
                "total_requests": 0,
                "last_used_at": None,
                "error_message": None
            }
            self._save_status(status)
            return f"Ключ {clean[:6]}... успешно добавлен в keys.txt и активирован."


class KnowledgeLibrary:
    """
    Пополняемая база знаний найденных материалов с полнотекстовым поиском FTS5 (SQLite).
    - Сохраняет статьи, выжимки, заголовки, даты и URL
    - Предоставляет мгновенный поиск (BM25) без выхода в сеть и без затрат баланса
    - Выполняет ротацию средствами БД (удаление материалов старше 30 дней)
    - Позволяет агенту обращаться к ранее найденным материалам по теме
    """
    def __init__(self, db_path: Path):
        self.db_path = db_path
        self._init_db()

    def _init_db(self):
        try:
            self.db_path.parent.mkdir(parents=True, exist_ok=True)
            with sqlite3.connect(self.db_path) as conn:
                conn.execute("PRAGMA journal_mode=WAL;")
                conn.execute("""
                    CREATE TABLE IF NOT EXISTS documents (
                        url TEXT PRIMARY KEY,
                        project TEXT DEFAULT 'default',
                        title TEXT,
                        author TEXT,
                        published_date TEXT,
                        content TEXT,
                        highlights_json TEXT,
                        query_source TEXT,
                        created_at REAL,
                        updated_at REAL
                    )
                """)
                # Миграция: проверяем наличие колонки project в существующей таблице documents
                cols = [row[1] for row in conn.execute("PRAGMA table_info(documents)").fetchall()]
                if "project" not in cols:
                    conn.execute("ALTER TABLE documents ADD COLUMN project TEXT DEFAULT 'default'")

                # Проверяем колонки FTS5
                conn.execute("""
                    CREATE VIRTUAL TABLE IF NOT EXISTS documents_fts USING fts5(
                        url UNINDEXED,
                        project,
                        title,
                        content,
                        highlights_json,
                        query_source,
                        tokenize = 'unicode61'
                    )
                """)
                try:
                    conn.execute("SELECT project FROM documents_fts LIMIT 0")
                except sqlite3.OperationalError:
                    conn.execute("DROP TABLE IF EXISTS documents_fts")
                    conn.execute("""
                        CREATE VIRTUAL TABLE documents_fts USING fts5(
                            url UNINDEXED,
                            project,
                            title,
                            content,
                            highlights_json,
                            query_source,
                            tokenize = 'unicode61'
                        )
                    """)
                    conn.execute("""
                        INSERT INTO documents_fts (url, project, title, content, highlights_json, query_source)
                        SELECT url, COALESCE(project, 'default'), title, content, highlights_json, query_source FROM documents
                    """)
        except Exception as e:
            sys.stderr.write(f"[EXA-LIBRARY] Ошибка инициализации базы: {e}\n")

    def store_document(
        self,
        url: str,
        title: str,
        author: Optional[str],
        published_date: Optional[str],
        content: str,
        highlights: List[str],
        query_source: str,
        project: str = "default"
    ):
        """Сохраняет или обновляет документ в библиотеке и индексе FTS5."""
        if not url:
            return
        now = time.time()
        proj_clean = slugify(project) or "default"
        hl_json = json.dumps(highlights or [], ensure_ascii=False)
        clean_title = title or "Без заголовка"
        clean_content = content or ""

        try:
            with sqlite3.connect(self.db_path, timeout=2.0) as conn:
                # Вставка/обновление таблицы documents
                conn.execute("""
                    INSERT INTO documents (url, project, title, author, published_date, content, highlights_json, query_source, created_at, updated_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(url) DO UPDATE SET
                        project = excluded.project,
                        title = excluded.title,
                        author = COALESCE(excluded.author, documents.author),
                        published_date = COALESCE(excluded.published_date, documents.published_date),
                        content = CASE WHEN length(excluded.content) > length(documents.content) THEN excluded.content ELSE documents.content END,
                        highlights_json = excluded.highlights_json,
                        query_source = excluded.query_source,
                        updated_at = excluded.updated_at
                """, (url, proj_clean, clean_title, author, published_date, clean_content, hl_json, query_source, now, now))

                # Обновление FTS5 индекса (удаление старого вхождения и вставка нового)
                conn.execute("DELETE FROM documents_fts WHERE url = ?", (url,))
                conn.execute("""
                    INSERT INTO documents_fts (url, project, title, content, highlights_json, query_source)
                    VALUES (?, ?, ?, ?, ?, ?)
                """, (url, proj_clean, clean_title, clean_content, hl_json, query_source))

                # Автоматическая ротация: удаление документов старше LIBRARY_RETENTION_DAYS
                purge_cutoff = now - (LIBRARY_RETENTION_DAYS * 86400)
                cur = conn.execute("SELECT url FROM documents WHERE updated_at < ?", (purge_cutoff,))
                old_urls = [r[0] for r in cur.fetchall()]
                if old_urls:
                    conn.executemany("DELETE FROM documents WHERE url = ?", [(u,) for u in old_urls])
                    conn.executemany("DELETE FROM documents_fts WHERE url = ?", [(u,) for u in old_urls])
        except Exception as e:
            sys.stderr.write(f"[EXA-LIBRARY] Ошибка записи документа: {e}\n")

    def search(self, query: str, project: Optional[str] = None, limit: int = 5) -> List[Dict[str, Any]]:
        """Выполняет полнотекстовый поиск FTS5 по локальной библиотеке."""
        results = []
        if not query or not query.strip():
            return results

        # Экранирование спецсимволов FTS5
        clean_query = "".join(c if c.isalnum() or c.isspace() else " " for c in query).strip()
        if not clean_query:
            return results

        # Формирование связки префиксов для точного совпадения корней
        terms = [f'"{t}"*' for t in clean_query.split() if len(t) > 1]
        if not terms:
            match_expr = f'"{clean_query}"*'
        else:
            match_expr = " OR ".join(terms)

        try:
            with sqlite3.connect(self.db_path, timeout=1.0) as conn:
                conn.row_factory = sqlite3.Row
                cur = conn.cursor()
                if project:
                    proj_clean = slugify(project)
                    sql = """
                        SELECT d.url, d.project, d.title, d.author, d.published_date, d.content, d.highlights_json, d.query_source, d.updated_at,
                               bm25(documents_fts) as rank
                        FROM documents_fts
                        JOIN documents d ON documents_fts.url = d.url
                        WHERE documents_fts MATCH ? AND d.project = ?
                        ORDER BY rank ASC
                        LIMIT ?
                    """
                    cur.execute(sql, (match_expr, proj_clean, limit))
                else:
                    sql = """
                        SELECT d.url, d.project, d.title, d.author, d.published_date, d.content, d.highlights_json, d.query_source, d.updated_at,
                               bm25(documents_fts) as rank
                        FROM documents_fts
                        JOIN documents d ON documents_fts.url = d.url
                        WHERE documents_fts MATCH ?
                        ORDER BY rank ASC
                        LIMIT ?
                    """
                    cur.execute(sql, (match_expr, limit))
                for row in cur.fetchall():
                    hl = []
                    try:
                        hl = json.loads(row["highlights_json"])
                    except Exception:
                        pass
                    results.append({
                        "url": row["url"],
                        "project": row["project"],
                        "title": row["title"],
                        "author": row["author"],
                        "published_date": row["published_date"],
                        "content": row["content"],
                        "highlights": hl,
                        "query_source": row["query_source"],
                        "updated_at": row["updated_at"]
                    })
        except Exception as e:
            sys.stderr.write(f"[EXA-LIBRARY] Ошибка FTS5 поиска: {e}\n")
        return results

    def get_stats(self) -> Dict[str, Any]:
        """Возвращает статистику объема библиотеки."""
        try:
            with sqlite3.connect(self.db_path, timeout=1.0) as conn:
                cur = conn.cursor()
                cur.execute("SELECT COUNT(*), COUNT(DISTINCT project), MIN(created_at), MAX(updated_at) FROM documents")
                row = cur.fetchone()
                total = row[0] if row else 0
                proj_count = row[1] if row else 0
                return {"total_docs": total, "projects_count": proj_count, "db_path": str(self.db_path)}
        except Exception:
            return {"total_docs": 0, "projects_count": 0, "db_path": str(self.db_path)}

    def clear(self) -> str:
        """Полная очистка библиотеки."""
        try:
            with sqlite3.connect(self.db_path, timeout=2.0) as conn:
                conn.execute("DELETE FROM documents")
                conn.execute("DELETE FROM documents_fts")
            return "Локальная библиотека успешно очищена."
        except Exception as e:
            return f"Ошибка при очистке: {e}"


balancer = KeyBalancer(KEYS_FILE, STATUS_FILE)
library = KnowledgeLibrary(LIBRARY_DB_PATH)


async def _call_with_failover(func, *args, **kwargs) -> tuple[Any, str, float]:
    """
    Выполняет вызов официального SDK exa-py с отказоустойчивостью:
    - Пробует текущий активный ключ
    - При ошибке 402/403/исчерпании квоты переключается на следующий ключ
    - Возвращает (результат, key_used, cost)
    """
    max_retries = 8
    attempt = 0
    tested_keys = set()

    while attempt < max_retries:
        key, active_keys = await balancer.get_active_key()
        if not key:
            raise RuntimeError("❌ ВСЕ API-КЛЮЧИ EXA ИСЧЕРПАНЫ: в пуле нет активных ключей с положительным балансом. Добавьте рабочий ключ в keys.txt или вызовите exa_add_key.")

        tested_keys.add(key)
        exa = Exa(api_key=key, user_agent="Antigravity-Exa-MCP/2.0")

        try:
            res = await asyncio.to_thread(func, exa, *args, **kwargs)
            cost = 0.0
            if hasattr(res, "cost_dollars") and res.cost_dollars:
                cost = getattr(res.cost_dollars, "total", 0.0) or 0.0

            await balancer.record_success(key, cost)
            return res, key, cost

        except Exception as e:
            err_str = str(e).lower()
            if any(term in err_str for term in ["402", "403", "payment", "credit", "balance", "quota", "forbidden"]):
                await balancer.mark_exhausted(key, f"Ошибка баланса: {str(e)[:150]}")
                attempt += 1
                continue
            elif "429" in err_str or "rate limit" in err_str:
                sys.stderr.write(f"[EXA-BALANCER] Rate limit на ключе {key[:6]}..., пробуем следующий...\n")
                attempt += 1
                await asyncio.sleep(0.5)
                continue
            else:
                attempt += 1
                if attempt >= len(active_keys):
                    raise e

    raise RuntimeError(f"❌ ВСЕ ПРОВЕРЕННЫЕ КЛЮЧИ ({len(tested_keys)}) ИСЧЕРПАНЫ ИЛИ НЕДОСТУПНЫ. Пополните баланс.")


def _format_search_results(data_dict: dict, query_title: str) -> str:
    key_info = f" (Ключ: {data_dict.get('_key_used', 'N/A')})"
    cost_info = data_dict.get("_cost", 0.0)

    lines = [
        f"### Результаты Exa: {query_title}",
        f"*Стоимость: ${cost_info:.4f}{key_info}*",
        ""
    ]

    results = data_dict.get("results", [])
    if not results:
        lines.append("Ничего не найдено по данному запросу.")
        return "\n".join(lines)

    for i, r in enumerate(results, 1):
        title = r.get("title") or "Без заголовка"
        url = r.get("url") or ""
        author = f" | Автор: {r['author']}" if r.get("author") else ""
        date = f" | Дата: {r['published_date'][:10]}" if r.get("published_date") else ""
        lines.append(f"**{i}. [{title}]({url})**{author}{date}")

        highlights = r.get("highlights", [])
        if highlights:
            for h in highlights[:3]:
                clean_h = " ".join(h.replace("\n", " ").split())
                lines.append(f"> «{clean_h}»")

        text = r.get("text", "")
        if text and not highlights:
            snippet = " ".join(text[:400].replace("\n", " ").split())
            lines.append(f"> {snippet}...")

        lines.append("")

    return "\n".join(lines).strip()


@mcp.tool()
async def exa_search(
    query: str,
    project: str = "default",
    num_results: int = 5,
    search_type: str = "auto",
    category: Optional[str] = None,
    include_domains: Optional[List[str]] = None,
    exclude_domains: Optional[List[str]] = None,
    highlights: bool = True,
    text_max_characters: int = 2500
) -> str:
    """
    Основной семантический веб-поиск через официальный SDK exa-py.
    Все найденные материалы автоматически индексируются в локальную библиотеку (SQLite FTS5).

    Параметры:
    - query: текст запроса или описание предмета поиска (до 200,000 символов).
    - project: имя проекта для каталогизации в библиотеке (по умолчанию 'default').
    - num_results: количество возвращаемых результатов (1-10).
    - search_type: 'auto', 'neural' или 'keyword' (официальные типы поиска Exa).
    - category: фильтрация по категории Exa ('company', 'research paper', 'news', 'pdf', 'github').
    - highlights: извлекать ли релевантные цитаты (highlights).
    - text_max_characters: максимальный объем чистого текста страницы (по умолчанию 2500).
    """
    if not query or not query.strip():
        return "Ошибка: пустой поисковый запрос Exa."

    if len(query) > MAX_PROMPT_CHARS:
        return f"Ошибка: длина запроса ({len(query)} симв.) превышает лимит {MAX_PROMPT_CHARS} символов."

    contents_dict = {
        "text": {"max_characters": text_max_characters}
    }
    if highlights:
        contents_dict["highlights"] = {"max_characters": 500}

    def _do_search(exa_client: Exa):
        return exa_client.search(
            query=query,
            num_results=num_results,
            type=search_type,
            category=category,
            include_domains=include_domains,
            exclude_domains=exclude_domains,
            contents=contents_dict
        )

    try:
        res, key_used, cost = await _call_with_failover(_do_search)
        results_list = []
        for r in res.results:
            results_list.append({
                "title": r.title,
                "url": r.url,
                "author": r.author,
                "published_date": r.published_date,
                "highlights": r.highlights or [],
                "text": r.text or ""
            })
            # Пополнение локальной библиотеки знаний в фоне
            library.store_document(
                url=r.url,
                title=r.title,
                author=r.author,
                published_date=r.published_date,
                content=r.text or "",
                highlights=r.highlights or [],
                query_source=query,
                project=project
            )

        data_dict = {
            "results": results_list,
            "_key_used": f"{key_used[:6]}...{key_used[-4:]}",
            "_cost": cost
        }
        return _format_search_results(data_dict, query)

    except Exception as e:
        return f"### Ошибка поиска Exa: {query}\n{str(e)}"


@mcp.tool()
async def exa_answer(query: str) -> str:
    """
    Мгновенный прямой синтез ответа на вопрос с проверяемыми веб-источниками и цитатами (Exa Answer API).
    Работает за 1-2 секунды, идеально для быстрой верификации фактов или формул.
    """
    def _do_answer(exa_client: Exa):
        return exa_client.answer(query=query)

    try:
        res, key_used, cost = await _call_with_failover(_do_answer)
        citations = []
        if hasattr(res, "citations") and res.citations:
            for c in res.citations:
                url = getattr(c, "url", str(c))
                citations.append(url)

        answer_text = res.answer if hasattr(res, "answer") else str(res)
        cit_lines = "\n".join(f"- {c}" for c in citations) if citations else "*Прямые цитаты отсутствуют*"
        return (
            f"### Ответ Exa: {query}\n"
            f"*Стоимость: ${cost:.4f} (Ключ: {key_used[:6]}...)*\n\n"
            f"{answer_text}\n\n"
            f"**Первоисточники:**\n{cit_lines}"
        )
    except Exception as e:
        return f"### Ошибка генерации ответа Exa: {query}\n{str(e)}"


@mcp.tool()
async def exa_find_similar(url: str, project: str = "default", num_results: int = 5, highlights: bool = True) -> str:
    """
    Поиск веб-страниц и статей, семантически похожих на переданный URL (поиск прямых аналогов, сходных методик, конкурентов или библиотек).
    """
    contents_dict = {"text": {"max_characters": 2000}}
    if highlights:
        contents_dict["highlights"] = {"max_characters": 400}

    def _do_find(exa_client: Exa):
        return exa_client.find_similar_and_contents(url=url, num_results=num_results, **contents_dict)

    try:
        res, key_used, cost = await _call_with_failover(_do_find)
        results_list = []
        for r in res.results:
            results_list.append({
                "title": r.title,
                "url": r.url,
                "author": r.author,
                "published_date": r.published_date,
                "highlights": r.highlights or [],
                "text": r.text or ""
            })
            library.store_document(
                url=r.url,
                title=r.title,
                author=r.author,
                published_date=r.published_date,
                content=r.text or "",
                highlights=r.highlights or [],
                query_source=f"Similar to {url}",
                project=project
            )

        data_dict = {
            "results": results_list,
            "_key_used": f"{key_used[:6]}...{key_used[-4:]}",
            "_cost": cost
        }
        return _format_search_results(data_dict, f"Аналоги для {url}")
    except Exception as e:
        return f"### Ошибка поиска аналогов: {url}\n{str(e)}"


@mcp.tool()
async def exa_get_contents(urls: List[str], project: str = "default", text_max_characters: int = 4000) -> str:
    """
    Пакетно извлекает очищенный машиночитаемый текст/Markdown с указанных веб-страниц.
    Сохраняет выгруженные тексты в локальную библиотеку для постоянного доступа без сети.
    """
    if not urls:
        return "Список ссылок пуст."

    def _do_contents(exa_client: Exa):
        return exa_client.get_contents(urls=urls, text={"max_characters": text_max_characters})

    try:
        res, key_used, cost = await _call_with_failover(_do_contents)
        stored_results = []
        for r in res.results:
            text_val = r.text or ""
            title_val = r.title or "Документ"
            stored_results.append({"title": title_val, "url": r.url or "", "text": text_val})
            library.store_document(
                url=r.url,
                title=title_val,
                author=r.author,
                published_date=r.published_date,
                content=text_val,
                highlights=[],
                query_source="Direct fetch",
                project=project
            )

        lines = [f"### Выгрузка контента Exa ({len(stored_results)} страниц) | Стоимость: ${cost:.4f}", ""]
        for i, r in enumerate(stored_results, 1):
            lines.append(f"#### {i}. [{r['title']}]({r['url']})")
            lines.append(r["text"] if r["text"] else "*Содержимое пусто или не удалось извлечь.*")
            lines.append("\n---\n")

        return "\n".join(lines).strip()
    except Exception as e:
        return f"Ошибка при получении контента: {str(e)}"


@mcp.tool()
async def exa_search_batch(
    queries: List[str],
    project: str = "default",
    num_results: int = 3,
    highlights: bool = True,
    text_max_characters: int = 2500
) -> str:
    """
    Пакетный параллельный запуск нескольких семантических поисков через asyncio.gather за 1-3 секунды.
    Идеально для исследования многогранных инженерных тем без задержек.

    Параметры:
    - queries: список поисковых запросов.
    - project: имя проекта для каталогизации.
    - num_results: количество результатов на каждый запрос.
    - highlights: извлекать ли релевантные цитаты.
    - text_max_characters: максимальный объем текста (по умолчанию 2500).
    """
    if not queries:
        return "Список запросов пуст."

    tasks = [
        exa_search(
            query=q,
            project=project,
            num_results=num_results,
            highlights=highlights,
            text_max_characters=text_max_characters
        )
        for q in queries
    ]
    raw_results = await asyncio.gather(*tasks, return_exceptions=True)

    formatted = []
    total = len(queries)
    for idx, (q, res) in enumerate(zip(queries, raw_results), start=1):
        if isinstance(res, Exception):
            formatted.append(f"=== [ПАКЕТ {idx}/{total}] Запрос: {q} ===\n\nОшибка выполнения: {type(res).__name__}: {str(res)}\n")
        else:
            formatted.append(f"=== [ПАКЕТ {idx}/{total}] Запрос: {q} ===\n\n{res}\n")

    return "\n" + "\n".join(formatted)


@mcp.tool()
async def exa_library_search(query: str, project: Optional[str] = None, limit: int = 5) -> str:
    """
    Мгновенный локальный полнотекстовый поиск (SQLite FTS5 / BM25) по всей базе ранее найденных документов, статей и выжимок Exa.
    Работает за 1 мс, БЕЗ обращения к сети и с нулевой стоимостью ($0.00).
    Используйте для поиска по накопленной литературе, формулам и отчетам.
    """
    matches = library.search(query=query, project=project, limit=limit)
    if not matches:
        proj_str = f" в проекте «{project}»" if project else ""
        return f"В локальной библиотеке знаний{proj_str} ничего не найдено по запросу: «{query}». Попробуйте выполнить свежий поиск через `exa_search`."

    lines = [
        f"### Локальная библиотека Exa: «{query}» (Найдено: {len(matches)})",
        "*Источник: локальная база знаний SQLite FTS5 (0.5 мс, $0.00)*",
        ""
    ]

    for i, doc in enumerate(matches, 1):
        title = doc["title"]
        url = doc["url"]
        author = f" | Автор: {doc['author']}" if doc["author"] else ""
        date = f" | Дата: {doc['published_date'][:10]}" if doc["published_date"] else ""
        src = f" *(Из поиска: {doc['query_source']})*" if doc["query_source"] else ""

        proj_tag = f" | Проект: `{doc['project']}`" if doc.get('project') else ""
        lines.append(f"**{i}. [{title}]({url})**{proj_tag}{author}{date}{src}")

        hl = doc.get("highlights", [])
        if hl:
            for h in hl[:2]:
                clean_h = " ".join(h.replace("\n", " ").split())
                lines.append(f"> «{clean_h}»")
        elif doc.get("content"):
            snippet = " ".join(doc["content"][:350].replace("\n", " ").split())
            lines.append(f"> {snippet}...")

        lines.append("")

    return "\n".join(lines).strip()


@mcp.tool()
async def exa_library_status() -> str:
    """
    Показывает статистику локальной библиотеки знаний (количество накопленных документов, путь к файлу БД и срок ротации).
    """
    stats = library.get_stats()
    return (
        f"=== СТАТИСТИКА ЛОКАЛЬНОЙ БИБЛИОТЕКИ EXA ===\n"
        f"- Всего сохраненных документов/статей: **{stats['total_docs']}**\n"
        f"- Активных проектов: **{stats.get('projects_count', 0)}**\n"
        f"- Файл базы данных (SQLite FTS5): `{stats['db_path']}`\n"
        f"- Срок автоматической ротации: **{LIBRARY_RETENTION_DAYS} дней**\n"
        f"- Быстрый локальный поиск: `exa_library_search(query=\"...\", project=\"...\")`\n"
    )


@mcp.tool()
async def exa_library_clear() -> str:
    """
    Полностью очищает локальную библиотеку сохраненных документов (при необходимости начать исследование с чистого листа).
    """
    return library.clear()


@mcp.tool()
async def exa_deep_research(instructions: str, project: str = "default") -> str:
    """
    Автономный многошаговый исследовательский агент Exa (Deep Research Engine).
    Планирует дерево поиска, автономно читает десятки источников, сохраняет Markdown-отчет и возвращает готовое резюме.
    """
    def _do_research(exa_client: Exa):
        res = exa_client.research.create(
            instructions=instructions,
            model="exa-research-fast"
        )
        finished = exa_client.research.poll_until_finished(res.research_id, poll_interval=2.0, timeout=120.0)
        return finished

    try:
        finished_res, key_used, cost = await _call_with_failover(_do_research)
        report = getattr(finished_res, "report", None) or getattr(finished_res, "content", None) or str(finished_res)

        now = time.time()
        ts_str = datetime.now().strftime("%Y-%m-%d_%H%M%S")
        short_hash = hashlib.sha256(f"{instructions}_{now}".encode("utf-8")).hexdigest()[:6]
        slug = slugify(instructions) or "research"
        proj_clean = slugify(project) or "default"

        proj_reports_dir = REPORTS_DIR / proj_clean
        proj_reports_dir.mkdir(parents=True, exist_ok=True)
        filename = f"{ts_str}_{short_hash}_{slug}.md"
        report_file = proj_reports_dir / filename

        md_content = f"# Exa Deep Research Report\n"
        md_content += f"- **Проект:** `{proj_clean}`\n"
        md_content += f"- **Задача:** {instructions}\n"
        md_content += f"- **Ключ:** `{key_used[:6]}...` | **Стоимость:** ${cost:.4f}\n"
        md_content += f"- **Дата:** {datetime.now(timezone.utc).isoformat()}\n\n"
        md_content += "---\n\n"
        md_content += report + "\n"

        try:
            report_file.write_text(md_content, encoding="utf-8")
        except Exception as e:
            sys.stderr.write(f"[EXA-LIBRARY] Ошибка записи отчета deep research: {e}\n")

        library.store_document(
            url=f"report://{filename}",
            title=f"Exa Deep Research: {instructions[:60]}",
            author="Exa Deep Research",
            published_date=datetime.now(timezone.utc).isoformat(),
            content=report,
            highlights=[],
            query_source=instructions,
            project=project
        )

        return (
            f"### Результат глубокого исследования Exa (Deep Research)\n"
            f"*Файл отчета:* [{filename}](file://{report_file})\n"
            f"*Ключ: {key_used[:6]}... | Стоимость: ${cost:.4f} | ID: {getattr(finished_res, 'research_id', 'N/A')}*\n\n"
            f"{report}"
        )
    except Exception as e:
        return f"Ошибка при выполнении глубокого исследования Exa: {str(e)}"


@mcp.tool()
async def exa_pool_status() -> str:
    """
    Аудит пула ключей: количество активных/исчерпанных счетов, суммарный расход в долларах и число выполненных запросов.
    """
    keys = balancer.load_keys()
    status = balancer._load_status()

    active_count = 0
    exhausted_count = 0
    total_spent = 0.0
    total_reqs = 0

    lines = [
        "=== СТАТУС ПУЛА И БАЛАНСИРОВЩИКА EXA API ===",
        f"Файл ключей: {KEYS_FILE}",
        f"Файл статусов: {STATUS_FILE}",
        f"Прокси: {DEFAULT_PROXY or 'Прямое соединение'}",
        ""
    ]

    for k in keys:
        st = status.get(k, {})
        s = st.get("status", "active")
        spent = st.get("total_spent_usd", 0.0)
        reqs = st.get("total_requests", 0)
        last = st.get("last_used_at") or "Никогда"
        err = st.get("error_message")

        masked = f"{k[:6]}...{k[-4:]}"
        if s == "active":
            active_count += 1
            icon = "🟢 АКТИВЕН"
        else:
            exhausted_count += 1
            icon = "🔴 ИСЧЕРПАН (Баланс 0$)"

        total_spent += spent
        total_reqs += reqs

        lines.append(f"- **{masked}**: {icon} | Запросов: {reqs} | Потрачено: ${spent:.4f} | Посл. вызов: {last}")
        if err:
            lines.append(f"  *Причина:* `{err[:120]}`")

    lines.append("")
    lines.append(f"**ИТОГО**: Активных счетов: {active_count}/{len(keys)} | Исчерпано: {exhausted_count} | Всего запросов: {total_reqs} | Суммарно потрачено: ${total_spent:.4f}")
    if active_count == 0:
        lines.append("\n⚠️ **ВНИМАНИЕ:** В пуле нет ни одного активного ключа! Пожалуйста, добавьте рабочий ключ в keys.txt.")

    return "\n".join(lines)


@mcp.tool()
async def exa_add_key(api_key: str) -> str:
    """
    Добавляет новый API-ключ в keys.txt и сразу регистрирует его в активном пуле.
    """
    return await balancer.add_new_key(api_key)


async def clean_list_tools() -> list[MCPTool]:
    tools = mcp._tool_manager.list_tools()
    return [
        MCPTool(
            name=info.name,
            title=info.title,
            description=info.description,
            inputSchema=info.parameters,
            outputSchema=None,
        )
        for info in tools
    ]

mcp.list_tools = clean_list_tools
mcp._mcp_server.list_tools()(clean_list_tools)

if __name__ == "__main__":
    mcp.run(transport="stdio")
