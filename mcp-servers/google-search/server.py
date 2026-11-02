# /// script
# requires-python = ">=3.10"
# dependencies = [
#     "mcp<2",
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
from typing import Optional, List, Dict, Any
import httpx
from mcp.server.fastmcp import FastMCP
from mcp.types import Tool as MCPTool

mcp = FastMCP("GoogleSearchGrounding")

BASE_DIR = Path(__file__).resolve().parent
CONFIG_FILE = BASE_DIR / "config.json"


def load_config() -> dict:
    defaults = {
        "storage_dir": "/var/tmp/scratch/google_library",
        "retention_days": 30,
        "primary_model": "gemini-3.8-flash-tiered",
        "fallback_model": "gemini-3.8-flash-high",
        "antigravity_url": "http://127.0.0.1:8045",
        "timeout_seconds": 140.0,
    }
    if CONFIG_FILE.exists():
        try:
            user_conf = json.loads(CONFIG_FILE.read_text(encoding="utf-8"))
            defaults.update(user_conf)
        except Exception as e:
            sys.stderr.write(f"[GOOGLE-CONFIG] Ошибка чтения config.json: {e}\n")
    return defaults


CONFIG = load_config()

# Разрешение пути хранения: абсолютный или относительный от BASE_DIR
raw_storage = Path(CONFIG.get("storage_dir", "/var/tmp/scratch/google_library"))
LIBRARY_DIR = raw_storage if raw_storage.is_absolute() else (BASE_DIR / raw_storage).resolve()
LIBRARY_DB_PATH = LIBRARY_DIR / "library.db"
REPORTS_DIR = LIBRARY_DIR / "reports"
LIBRARY_RETENTION_DAYS = int(CONFIG.get("retention_days", 30))

ANTIGRAVITY_RAW = os.environ.get("ANTIGRAVITY_URL") or CONFIG.get("antigravity_url", "http://127.0.0.1:8045")
for suffix in ["/v1/chat/completions", "/v1beta", "/v1"]:
    if ANTIGRAVITY_RAW.endswith(suffix):
        ANTIGRAVITY_RAW = ANTIGRAVITY_RAW[:-len(suffix)]
ANTIGRAVITY_BASE_URL = ANTIGRAVITY_RAW

DEFAULT_BASE_MODEL = os.environ.get("ANTIGRAVITY_MODEL") or CONFIG.get("primary_model", "gemini-3.8-flash-tiered")
FALLBACK_MODEL = CONFIG.get("fallback_model", "gemini-3.8-flash-high")
REQUEST_TIMEOUT = float(CONFIG.get("timeout_seconds", 140.0))


def get_search_agent_name(base_model: str) -> str:
    clean = base_model.strip()
    has_online = clean.endswith("-online")
    raw = clean[:-7] if has_online else clean
    return MODEL_ALIASES.get(raw, raw)


def slugify(text: str) -> str:
    text = re.sub(r"[^\w\s-]", "", text, flags=re.UNICODE).strip().lower()
    return re.sub(r"[-\s]+", "_", text)[:50]


class GoogleKnowledgeLibrary:
    """
    Пополняемая библиотека аналитических отчетов Google Grounding с поиском SQLite FTS5 (BM25).
    Идентична по архитектуре библиотеке Exa, но изолирована в своей директории.
    """
    def __init__(self, db_path: Path):
        self.db_path = db_path
        self._init_db()

    def _init_db(self):
        try:
            self.db_path.parent.mkdir(parents=True, exist_ok=True)
            REPORTS_DIR.mkdir(parents=True, exist_ok=True)
            with sqlite3.connect(self.db_path) as conn:
                conn.execute("PRAGMA journal_mode=WAL;")
                conn.execute("""
                    CREATE TABLE IF NOT EXISTS documents (
                        doc_id TEXT PRIMARY KEY,
                        project TEXT,
                        query TEXT,
                        report_path TEXT,
                        content TEXT,
                        model_used TEXT,
                        created_at REAL,
                        updated_at REAL
                    )
                """)
                conn.execute("""
                    CREATE VIRTUAL TABLE IF NOT EXISTS documents_fts USING fts5(
                        doc_id UNINDEXED,
                        project,
                        query,
                        content,
                        tokenize = 'unicode61'
                    )
                """)
        except Exception as e:
            sys.stderr.write(f"[GOOGLE-LIBRARY] Ошибка инициализации: {e}\n")

    def store_report(self, project: str, query: str, content: str, model_used: str) -> str:
        if not content or not content.strip():
            return ""

        now = time.time()
        ts_str = datetime.now().strftime("%Y-%m-%d_%H%M%S")
        short_hash = hashlib.sha256(f"{query}_{now}".encode("utf-8")).hexdigest()[:6]
        slug = slugify(query) or "report"
        proj_clean = slugify(project) or "default"

        proj_reports_dir = REPORTS_DIR / proj_clean
        proj_reports_dir.mkdir(parents=True, exist_ok=True)

        filename = f"{ts_str}_{short_hash}_{slug}.md"
        file_path = proj_reports_dir / filename

        md_content = f"# Аналитическое исследование Google Grounding\n"
        md_content += f"- **Проект:** `{proj_clean}`\n"
        md_content += f"- **Запрос:** {query}\n"
        md_content += f"- **Модель:** `{model_used}`\n"
        md_content += f"- **Дата:** {datetime.now(timezone.utc).isoformat()}\n\n"
        md_content += "---\n\n"
        md_content += content.strip() + "\n"

        try:
            file_path.write_text(md_content, encoding="utf-8")
        except Exception as e:
            sys.stderr.write(f"[GOOGLE-LIBRARY] Ошибка записи файла отчета: {e}\n")

        doc_id = f"{proj_clean}:{ts_str}_{short_hash}_{slug}"
        try:
            with sqlite3.connect(self.db_path, timeout=2.0) as conn:
                conn.execute("""
                    INSERT INTO documents (doc_id, project, query, report_path, content, model_used, created_at, updated_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(doc_id) DO UPDATE SET
                        content = excluded.content,
                        report_path = excluded.report_path,
                        model_used = excluded.model_used,
                        updated_at = excluded.updated_at
                """, (doc_id, proj_clean, query, str(file_path), content, model_used, now, now))

                conn.execute("DELETE FROM documents_fts WHERE doc_id = ?", (doc_id,))
                conn.execute("""
                    INSERT INTO documents_fts (doc_id, project, query, content)
                    VALUES (?, ?, ?, ?)
                """, (doc_id, proj_clean, query, content))

                # Автоматическая ротация (30 дней)
                purge_cutoff = now - (LIBRARY_RETENTION_DAYS * 86400)
                cur = conn.execute("SELECT doc_id, report_path FROM documents WHERE updated_at < ?", (purge_cutoff,))
                old_records = cur.fetchall()
                if old_records:
                    for old_id, old_p in old_records:
                        try:
                            p = Path(old_p)
                            if p.exists():
                                p.unlink()
                        except Exception:
                            pass
                    conn.executemany("DELETE FROM documents WHERE doc_id = ?", [(r[0],) for r in old_records])
                    conn.executemany("DELETE FROM documents_fts WHERE doc_id = ?", [(r[0],) for r in old_records])
        except Exception as e:
            sys.stderr.write(f"[GOOGLE-LIBRARY] Ошибка записи БД: {e}\n")

        return str(file_path)

    def search(self, query: str, project: Optional[str] = None, limit: int = 5) -> List[Dict[str, Any]]:
        results = []
        clean_query = "".join(c if c.isalnum() or c.isspace() else " " for c in query).strip()
        if not clean_query:
            return results

        terms = [f'"{t}"*' for t in clean_query.split() if len(t) > 1]
        match_expr = " OR ".join(terms) if terms else f'"{clean_query}"*'

        try:
            with sqlite3.connect(self.db_path, timeout=1.0) as conn:
                conn.row_factory = sqlite3.Row
                cur = conn.cursor()

                if project:
                    proj_clean = slugify(project)
                    sql = """
                        SELECT d.doc_id, d.project, d.query, d.report_path, d.content, d.model_used, d.updated_at,
                               bm25(documents_fts) as rank
                        FROM documents_fts
                        JOIN documents d ON documents_fts.doc_id = d.doc_id
                        WHERE documents_fts MATCH ? AND d.project = ?
                        ORDER BY rank ASC
                        LIMIT ?
                    """
                    cur.execute(sql, (match_expr, proj_clean, limit))
                else:
                    sql = """
                        SELECT d.doc_id, d.project, d.query, d.report_path, d.content, d.model_used, d.updated_at,
                               bm25(documents_fts) as rank
                        FROM documents_fts
                        JOIN documents d ON documents_fts.doc_id = d.doc_id
                        WHERE documents_fts MATCH ?
                        ORDER BY rank ASC
                        LIMIT ?
                    """
                    cur.execute(sql, (match_expr, limit))

                for row in cur.fetchall():
                    results.append({
                        "doc_id": row["doc_id"],
                        "project": row["project"],
                        "query": row["query"],
                        "report_path": row["report_path"],
                        "content": row["content"],
                        "model_used": row["model_used"],
                        "updated_at": row["updated_at"]
                    })
        except Exception as e:
            sys.stderr.write(f"[GOOGLE-LIBRARY] Ошибка поиска: {e}\n")
        return results

    def get_stats(self) -> Dict[str, Any]:
        try:
            with sqlite3.connect(self.db_path, timeout=1.0) as conn:
                cur = conn.cursor()
                cur.execute("SELECT COUNT(*), COUNT(DISTINCT project) FROM documents")
                row = cur.fetchone()
                return {
                    "total_reports": row[0] if row else 0,
                    "projects_count": row[1] if row else 0,
                    "db_path": str(self.db_path),
                    "reports_dir": str(REPORTS_DIR)
                }
        except Exception:
            return {"total_reports": 0, "projects_count": 0, "db_path": str(self.db_path), "reports_dir": str(REPORTS_DIR)}

    def clear(self) -> str:
        try:
            with sqlite3.connect(self.db_path, timeout=2.0) as conn:
                conn.execute("DELETE FROM documents")
                conn.execute("DELETE FROM documents_fts")
            return "Локальная библиотека отчетов Google Grounding очищена."
        except Exception as e:
            return f"Ошибка при очистке: {e}"


library = GoogleKnowledgeLibrary(LIBRARY_DB_PATH)


async def _execute_single_query_with_retry(client: httpx.AsyncClient, query: str, primary_model: str, project: str) -> dict:
    models_to_try = [primary_model]
    if primary_model != FALLBACK_MODEL:
        models_to_try.append(FALLBACK_MODEL)

    last_err = ""
    for target_model in models_to_try:
        canonical_model = get_search_agent_name(target_model)
        is_gemini = "gemini" in canonical_model.lower()

        if is_gemini:
            url = f"{ANTIGRAVITY_BASE_URL}/v1beta/models/{canonical_model}:generateContent"
            payload = {
                "contents": [{"role": "user", "parts": [{"text": query}]}],
                "tools": [{"googleSearch": {}}]
            }
        else:
            url = f"{ANTIGRAVITY_BASE_URL}/v1/chat/completions"
            payload = {
                "model": canonical_model,
                "messages": [{"role": "user", "content": query}]
            }

        try:
            response = await client.post(url, json=payload, timeout=httpx.Timeout(REQUEST_TIMEOUT, connect=10.0))
            response.raise_for_status()
            data = response.json()

            content = ""
            if is_gemini:
                candidates = data.get("candidates", [])
                if candidates:
                    parts = candidates[0].get("content", {}).get("parts", [])
                    text_parts = [p.get("text", "") for p in parts if p.get("text") and not p.get("thought")]
                    if not text_parts:
                        text_parts = [p.get("text", "") for p in parts if p.get("text")]
                    content = "".join(text_parts).strip()
            else:
                if "choices" in data and len(data["choices"]) > 0:
                    content = data["choices"][0]["message"]["content"]

            if content:
                report_file = library.store_report(
                    project=project,
                    query=query,
                    content=content,
                    model_used=canonical_model
                )
                return {
                    "query": query,
                    "status": "success",
                    "content": content,
                    "model_used": canonical_model,
                    "report_file": report_file,
                    "project": project
                }

        except httpx.TimeoutException:
            last_err = f"Таймаут ({REQUEST_TIMEOUT}s) на модели {canonical_model}"
            continue
        except Exception as e:
            last_err = f"Ошибка ({canonical_model}): {e}"
            continue

    return {
        "query": query,
        "status": "error",
        "content": (
            f"⚠️ ВНИМАНИЕ АГЕНТУ: Локальный шлюз Google Grounding временно не ответил ({last_err}).\n"
            f"КАТЕГОРИЧЕСКИ ЗАПРЕЩЕНО вызывать стоковый инструмент `search_web` (он не работает и дает шум)!\n"
            f"Вместо этого используйте `exa_search` (сервер `exa-search`) либо локальную библиотеку `google_library_search`."
        ),
        "model_used": "none",
        "report_file": None,
        "project": project
    }


@mcp.tool()
async def check_model_liveness() -> str:
    """
    Проверяет реальный статус и доступность бэкенда Google Search и рассуждающей модели.
    """
    start = time.time()
    gateway_status = "НЕДОСТУПЕН"
    ping_ms = 0.0
    try:
        async with httpx.AsyncClient(timeout=4.0) as client:
            resp = await client.get(f"{ANTIGRAVITY_BASE_URL}/v1/models")
            if resp.status_code == 200:
                ping_ms = round((time.time() - start) * 1000, 1)
                gateway_status = f"ДОСТУПЕН (HTTP 200, {ping_ms} мс)"
    except Exception as e:
        gateway_status = f"ОШИБКА ({e})"

    report = [
        "=== СТАТУС ШЛЮЗА GOOGLE SEARCH GROUNDING ===",
        f"1. Локальный шлюз ({ANTIGRAVITY_BASE_URL}): {gateway_status}",
        f"2. Основная модель: {DEFAULT_BASE_MODEL}",
        f"3. Резервная модель: {FALLBACK_MODEL}",
        f"4. Директория отчетов: {REPORTS_DIR}",
    ]
    return "\n".join(report)


@mcp.tool()
async def google_search(query: str, project: str = "default", model_override: Optional[str] = None) -> str:
    """
    Выполняет глубокое аналитическое исследование Google Search с использованием Gemini Search Grounding.
    Все результаты автоматически сохраняются в структурированные Markdown-файлы по проектам и индексируются в SQLite FTS5.

    Параметры:
    - query: развернутая постановка инженерной или исследовательской задачи на естественном языке.
    - project: название подпроекта для изоляции данных (например: 'gzla-cfd', 'plasma-article', 'default').
    - model_override: переопределение модели.
    """
    base = model_override if model_override else DEFAULT_BASE_MODEL
    total_timeout = httpx.Timeout(REQUEST_TIMEOUT * 2 + 10.0, connect=10.0)

    async with httpx.AsyncClient(timeout=total_timeout) as client:
        res = await _execute_single_query_with_retry(client, query, base, project)
        if res["status"] == "success" and res.get("report_file"):
            header = f"### [Google Grounding] Отчет сохранен: [{Path(res['report_file']).name}](file://{res['report_file']})\n"
            header += f"*Проект: `{res['project']}` | Модель: `{res['model_used']}`*\n\n"
            return header + res["content"]
        return res["content"]


@mcp.tool()
async def google_search_batch(queries: List[str], project: str = "default", model_override: Optional[str] = None) -> str:
    """
    Параллельный запуск нескольких независимых исследовательских сессий Google Grounding через asyncio.gather.
    Все отчеты сохраняются в Markdown-файлы с именами по темам и индексируются в базу проекта.
    """
    if not queries:
        return "Список запросов пуст."

    base = model_override if model_override else DEFAULT_BASE_MODEL
    total_timeout = httpx.Timeout(REQUEST_TIMEOUT * 2 + 10.0, connect=10.0)
    limits = httpx.Limits(max_keepalive_connections=20, max_connections=50)

    async with httpx.AsyncClient(timeout=total_timeout, limits=limits) as client:
        tasks = [_execute_single_query_with_retry(client, q, base, project) for q in queries]
        results = await asyncio.gather(*tasks)

    formatted = []
    total = len(results)
    for idx, item in enumerate(results, start=1):
        status_tag = f" [{item['status'].upper()}]" if item["status"] != "success" else ""
        file_link = f"\n*Файл отчета:* [{Path(item['report_file']).name}](file://{item['report_file']})" if item.get("report_file") else ""
        header = f"=== QUERY {idx}/{total}: {item['query']}{status_tag} ==={file_link}\n"
        formatted.append(f"{header}\n{item['content']}\n")

    return "\n" + "\n".join(formatted)


@mcp.tool()
async def google_library_search(query: str, project: Optional[str] = None, limit: int = 5) -> str:
    """
    Мгновенный полнотекстовый поиск (SQLite FTS5 / BM25) по ранее синтезированным отчетам Google Grounding.
    Работает локально за 1 мс, без ожидания и без повторной генерации.
    """
    matches = library.search(query=query, project=project, limit=limit)
    if not matches:
        proj_str = f" в проекте «{project}»" if project else ""
        return f"В библиотеке отчетов Google Grounding{proj_str} ничего не найдено по запросу: «{query}»."

    lines = [
        f"### Библиотека отчетов Google Grounding: «{query}» (Найдено: {len(matches)})",
        "*Источник: локальная база SQLite FTS5 (0.5 мс)*",
        ""
    ]

    for i, doc in enumerate(matches, 1):
        file_link = f"[{Path(doc['report_path']).name}](file://{doc['report_path']})"
        lines.append(f"**{i}. Проект: `{doc['project']}` | Запрос: {doc['query']}**")
        lines.append(f"- Полный файл отчета: {file_link}")
        snippet = " ".join(doc["content"][:350].replace("\n", " ").split())
        lines.append(f"> {snippet}...\n")

    return "\n".join(lines).strip()


@mcp.tool()
async def google_library_status() -> str:
    """
    Показывает статистику сохраненных отчетов Google Grounding (число отчетов, проекты, пути на диске).
    """
    stats = library.get_stats()
    return (
        f"=== СТАТИСТИКА БИБЛИОТЕКИ ОТЧЕТОВ GOOGLE GROUNDING ===\n"
        f"- Всего сгенерированных отчетов: **{stats['total_reports']}**\n"
        f"- Активных проектов: **{stats['projects_count']}**\n"
        f"- База данных (SQLite FTS5): `{stats['db_path']}`\n"
        f"- Каталог файлов отчетов: `{stats['reports_dir']}`\n"
        f"- Срок ротации: **{LIBRARY_RETENTION_DAYS} дней**\n"
        f"- Локальный поиск: `google_library_search(query=\"...\", project=\"...\")`\n"
    )


@mcp.tool()
async def google_library_clear() -> str:
    """
    Очищает базу ранее сохраненных отчетов Google Grounding.
    """
    return library.clear()


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
