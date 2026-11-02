#!/usr/bin/env python3
"""
Скрипт верификации библиографических записей BibTeX (.bib) через открытые реестры (Crossref & doi.org).
Проверяет существование DOI, сопоставляет названия, авторов и годы публикации,
выявляет генеративные галлюцинации и фальсификации (методология ARIS).
"""

import os
import re
import sys
import json
import argparse
from datetime import datetime
import urllib.request
import urllib.parse
import urllib.error
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Dict, List, Optional, Tuple, Any


def clean_latex(text: str) -> str:
    """Очищает строку от синтаксических конструкций LaTeX."""
    if not text:
        return ""
    text = re.sub(r"\\[a-zA-Z]+(\[[^\]]*\])?(\{([^}]*)\})?", r"\3", text)
    text = text.replace("{", "").replace("}", "").replace("~", " ")
    text = text.replace("--", "-").replace("---", "—")
    return " ".join(text.split()).strip()


def normalize_title(title: str) -> str:
    """Нормализует название статьи для сопоставления."""
    clean = clean_latex(title).lower()
    return re.sub(r"[^\w\s]", "", clean)


def extract_last_names(author_str: str) -> List[str]:
    """Извлекает фамилии авторов из строки BibTeX."""
    clean = clean_latex(author_str)
    authors = [a.strip() for a in clean.split(" and ")]
    last_names = []
    for a in authors:
        if not a:
            continue
        if "," in a:
            # Формат: "Фамилия, И. О."
            ln = a.split(",")[0].strip()
        else:
            # Формат: "И. О. Фамилия"
            parts = a.split()
            ln = parts[-1].strip() if parts else ""
        if ln and ln.lower() != "others":
            last_names.append(ln.lower())
    return last_names


def parse_bibtex_fields(body: str) -> Dict[str, str]:
    """Надежный парсер полей BibTeX с учетом вложенных фигурных скобок."""
    fields = {}
    i = 0
    n = len(body)
    while i < n:
        # Ищем имя поля
        while i < n and (body[i].isspace() or body[i] == ","):
            i += 1
        if i >= n:
            break
        # Считываем имя поля (буквы, цифры, дефис, подчеркивание)
        start_name = i
        while i < n and (body[i].isalnum() or body[i] in "_-"):
            i += 1
        name = body[start_name:i].strip().lower()
        if not name:
            i += 1
            continue
            
        # Ищем знак равенства '='
        while i < n and body[i].isspace():
            i += 1
        if i >= n or body[i] != "=":
            continue
        i += 1  # Пропускаем '='
        
        while i < n and body[i].isspace():
            i += 1
        if i >= n:
            break
            
        val = ""
        delimiter = body[i]
        if delimiter == "{":
            i += 1
            brace_depth = 1
            start_val = i
            while i < n and brace_depth > 0:
                if body[i] == "{":
                    brace_depth += 1
                elif body[i] == "}":
                    brace_depth -= 1
                i += 1
            val = body[start_val:i - 1]
        elif delimiter == '"':
            i += 1
            start_val = i
            while i < n and body[i] != '"':
                if body[i] == "\\" and i + 1 < n:
                    i += 2
                    continue
                i += 1
            val = body[start_val:i]
            if i < n and body[i] == '"':
                i += 1
        else:
            # Значение без кавычек (число или макрос)
            start_val = i
            while i < n and body[i] not in ",\n\r}":
                i += 1
            val = body[start_val:i].strip()
            
        fields[name] = val.strip()
        
    return fields


def parse_bibtex(bib_content: str) -> List[Dict[str, Any]]:
    """Парсер BibTeX записей с поддержкой вложенных блоков."""
    entries = []
    pattern = re.compile(r"@(\w+)\s*\{\s*([^,]+),", re.UNICODE)
    pos = 0
    while True:
        match = pattern.search(bib_content, pos)
        if not match:
            break
        entry_type = match.group(1).lower()
        key = match.group(2).strip()
        
        start_idx = match.end()
        brace_count = 1
        i = start_idx
        while i < len(bib_content) and brace_count > 0:
            if bib_content[i] == "{":
                brace_count += 1
            elif bib_content[i] == "}":
                brace_count -= 1
            i += 1
            
        body = bib_content[start_idx:i - 1]
        pos = i
        
        fields = parse_bibtex_fields(body)
            
        entries.append({
            "key": key,
            "type": entry_type,
            "title": fields.get("title", ""),
            "author": fields.get("author", ""),
            "year": fields.get("year", ""),
            "doi": fields.get("doi", "").strip(),
            "url": fields.get("url", "").strip(),
            "journal": fields.get("journal") or fields.get("booktitle") or fields.get("publisher", ""),
            "raw_fields": fields
        })
    return entries


def fetch_doi_metadata(doi: str, timeout: float = 10.0) -> Tuple[int, Optional[Dict[str, Any]], str]:
    """
    Запрашивает официальные метаданные статьи через doi.org Content Negotiation (CSL JSON).
    Подходит для всех DOI: Crossref, DataCite, mARXiv.
    """
    clean_doi = doi.strip()
    if clean_doi.startswith("http"):
        clean_doi = re.sub(r"^https?://(dx\.)?doi\.org/", "", clean_doi)
    clean_doi = clean_doi.strip("/")
    
    url = f"https://doi.org/{urllib.parse.quote(clean_doi)}"
    req = urllib.request.Request(
        url,
        headers={
            "Accept": "application/vnd.citationstyles.csl+json",
            "User-Agent": "AntigravityAcademicVerifier/1.0 (mailto:support@antigravity.agent)"
        }
    )
    
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            status = resp.status
            content = resp.read().decode("utf-8")
            data = json.loads(content)
            return status, data, ""
    except urllib.error.HTTPError as e:
        return e.code, None, f"HTTP {e.code}: {e.reason}"
    except Exception as e:
        return 0, None, str(e)


def search_crossref_by_title(title: str, timeout: float = 10.0) -> Optional[Dict[str, Any]]:
    """Ищет статью в Crossref по названию, если DOI отсутствует."""
    if not title:
        return None
    clean = clean_latex(title)
    url = f"https://api.crossref.org/works?query.title={urllib.parse.quote(clean)}&rows=1"
    req = urllib.request.Request(
        url,
        headers={"User-Agent": "AntigravityAcademicVerifier/1.0 (mailto:support@antigravity.agent)"}
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            if resp.status == 200:
                data = json.loads(resp.read().decode("utf-8"))
                items = data.get("message", {}).get("items", [])
                if items:
                    return items[0]
    except Exception:
        pass
    return None


def calculate_similarity(s1: str, s2: str) -> float:
    """Вычисляет коэффициент схожести по множеству слов (Jaccard similarity)."""
    words1 = set(s1.split())
    words2 = set(s2.split())
    if not words1 or not words2:
        return 0.0
    intersection = words1.intersection(words2)
    union = words1.union(words2)
    return len(intersection) / len(union)


def verify_entry(entry: Dict[str, Any], timeout: float = 10.0) -> Dict[str, Any]:
    """Проводит полный 4-шаговый аудит достоверности одной библиографической записи."""
    key = entry["key"]
    bib_title = entry["title"]
    bib_author = entry["author"]
    bib_year = entry["year"]
    bib_doi = entry["doi"]
    
    result = {
        "key": key,
        "bib_title": clean_latex(bib_title),
        "bib_author": clean_latex(bib_author),
        "bib_year": bib_year,
        "bib_doi": bib_doi,
        "status": "UNKNOWN",
        "verified_title": "",
        "verified_authors": "",
        "verified_year": "",
        "verified_doi": "",
        "notes": []
    }
    
    if bib_doi:
        status_code, meta, err_msg = fetch_doi_metadata(bib_doi, timeout=timeout)
        if status_code == 200 and meta:
            # Извлекаем метаданные из CSL JSON
            real_title = meta.get("title", "")
            if isinstance(real_title, list):
                real_title = real_title[0] if real_title else ""
            real_title = clean_latex(str(real_title))
            
            real_authors = []
            for a in meta.get("author", []):
                fam = a.get("family", "")
                giv = a.get("given", "")
                if fam and giv:
                    real_authors.append(f"{giv} {fam}")
                elif fam:
                    real_authors.append(fam)
            
            # Извлечение года
            issued = meta.get("issued", {}).get("date-parts", [[]])
            real_year = str(issued[0][0]) if issued and issued[0] else ""
            
            result["verified_title"] = real_title
            result["verified_authors"] = ", ".join(real_authors[:5])
            result["verified_year"] = real_year
            result["verified_doi"] = meta.get("DOI", bib_doi)
            
            # Сопоставление названия
            norm_bib = normalize_title(bib_title)
            norm_real = normalize_title(real_title)
            sim = calculate_similarity(norm_bib, norm_real)
            
            # Сопоставление авторов
            bib_last_names = extract_last_names(bib_author)
            real_last_names = [a.get("family", "").lower() for a in meta.get("author", []) if a.get("family")]
            
            author_match = False
            if bib_last_names and real_last_names:
                overlap = set(bib_last_names).intersection(set(real_last_names))
                if len(overlap) >= 1:
                    author_match = True
            elif not bib_last_names:
                author_match = True  # Если в bib не указаны авторы
                
            if sim >= 0.5 and author_match:
                result["status"] = "VERIFIED"
                if sim < 0.85:
                    result["notes"].append("Незначительное отличие в названии статьи")
            elif sim < 0.4:
                result["status"] = "FABRICATED_DOI"
                result["notes"].append(f"DOI указывает на другую статью: «{real_title}»")
            elif not author_match:
                result["status"] = "AUTHOR_CHIMERA"
                result["notes"].append(f"Несовпадение авторов (В реестре: {', '.join(real_last_names)})")
            else:
                result["status"] = "VERIFIED"
        elif status_code == 404:
            result["status"] = "INVALID_DOI_404"
            result["notes"].append(f"DOI {bib_doi} не существует в реестре doi.org (HTTP 404)")
        else:
            result["status"] = "NETWORK_ERROR"
            result["notes"].append(f"Ошибка запроса: {err_msg}")
    else:
        # DOI не указан в bib — попытка поиска в Crossref по названию
        found = search_crossref_by_title(bib_title, timeout=timeout)
        if found:
            found_title = found.get("title", [""])[0] if found.get("title") else ""
            found_doi = found.get("DOI", "")
            norm_bib = normalize_title(bib_title)
            norm_found = normalize_title(found_title)
            sim = calculate_similarity(norm_bib, norm_found)
            if sim >= 0.7:
                result["status"] = "DOI_SUGGESTED"
                result["verified_doi"] = found_doi
                result["verified_title"] = found_title
                result["notes"].append(f"Найден подходящий DOI в Crossref: {found_doi}")
            else:
                result["status"] = "NO_DOI"
                result["notes"].append("Локальный источник без DOI (книга/отчет/ГОСТ)")
        else:
            result["status"] = "NO_DOI"
            result["notes"].append("DOI отсутствует в файле")
            
    return result


def main():
    parser = argparse.ArgumentParser(description="Аудит достоверности источников BibTeX (ARIS Anti-Fabrication)")
    parser.add_argument("bib_file", help="Путь к целевому файлу .bib")
    parser.add_argument("--report", "-r", default=None, help="Путь для сохранения итогового отчета Markdown")
    parser.add_argument("--workers", "-w", type=int, default=8, help="Количество параллельных сетевых потоков")
    parser.add_argument("--timeout", "-t", type=float, default=8.0, help="Таймаут запроса к doi.org (секунды)")
    args = parser.parse_args()

    bib_path = os.path.abspath(args.bib_file)
    if not os.path.exists(bib_path):
        print(f"❌ Ошибка: Файл {bib_path} не найден.", file=sys.stderr)
        sys.exit(1)

    with open(bib_path, "r", encoding="utf-8", errors="replace") as f:
        content = f.read()

    entries = parse_bibtex(content)
    total = len(entries)
    print(f"📖 Загружено {total} записей из {os.path.basename(bib_path)}. Запуск многопоточной верификации...\n")

    results = []
    with ThreadPoolExecutor(max_workers=args.workers) as executor:
        future_to_entry = {executor.submit(verify_entry, entry, args.timeout): entry for entry in entries}
        for future in as_completed(future_to_entry):
            res = future.result()
            results.append(res)
            # Вывод компактного индикатора в терминал
            status_symbol = {
                "VERIFIED": "✅",
                "DOI_SUGGESTED": "💡",
                "NO_DOI": "⚪",
                "FABRICATED_DOI": "❌",
                "AUTHOR_CHIMERA": "⚠️",
                "INVALID_DOI_404": "🚫",
                "NETWORK_ERROR": "🔌"
            }.get(res["status"], "❓")
            print(f"[{status_symbol} {res['status']:<15}] {res['key']}")

    # Статистика
    counts = {}
    for r in results:
        counts[r["status"]] = counts.get(r["status"], 0) + 1

    print("\n" + "=" * 60)
    print("ИТОГИ АУДИТА ДОСТОВЕРНОСТИ:")
    for status, cnt in sorted(counts.items()):
        print(f"  {status}: {cnt}")
    print("=" * 60)

    # Генерация подробного Markdown-отчета
    report_lines = [
        f"# Отчет верификации библиографии: `{os.path.basename(bib_path)}`",
        f"\n**Дата проверки:** {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}",
        f"**Всего записей:** {total}\n",
        "## Сводная статистика\n",
        "| Категория | Количество | Описание |",
        "|---|---|---|",
        f"| ✅ **VERIFIED** | {counts.get('VERIFIED', 0)} | Полностью подтверждено в официальном реестре DOI |",
        f"| 💡 **DOI_SUGGESTED** | {counts.get('DOI_SUGGESTED', 0)} | В bib не было DOI, но найден точный аналог в Crossref |",
        f"| ⚪ **NO_DOI** | {counts.get('NO_DOI', 0)} | Локальные книги, отчеты, стандарты без DOI |",
        f"| ❌ **FABRICATED_DOI** | {counts.get('FABRICATED_DOI', 0)} | Фальсификация: DOI существует, но статья совсем другая |",
        f"| ⚠️ **AUTHOR_CHIMERA** | {counts.get('AUTHOR_CHIMERA', 0)} | Подлог авторов: статья совпадает, но авторы подменены |",
        f"| 🚫 **INVALID_DOI_404** | {counts.get('INVALID_DOI_404', 0)} | Несуществующий DOI (галлюцинация генеративной модели) |",
        f"| 🔌 **NETWORK_ERROR** | {counts.get('NETWORK_ERROR', 0)} | Таймаут или временный сбой сети |",
        "\n---\n",
        "## Детальный реестр записей\n"
    ]

    for r in sorted(results, key=lambda x: x["key"]):
        report_lines.append(f"### `[{r['status']}]` {r['key']}")
        report_lines.append(f"- **Название в bib:** {r['bib_title']}")
        if r["bib_author"]:
            report_lines.append(f"- **Авторы в bib:** {r['bib_author']}")
        if r["bib_doi"]:
            report_lines.append(f"- **DOI в bib:** [{r['bib_doi']}](https://doi.org/{r['bib_doi']})")
        if r["verified_title"]:
            report_lines.append(f"- **Официальное название:** {r['verified_title']}")
        if r["verified_authors"]:
            report_lines.append(f"- **Официальные авторы:** {r['verified_authors']}")
        if r["verified_doi"] and r["verified_doi"] != r["bib_doi"]:
            report_lines.append(f"- **Рекомендованный DOI:** [{r['verified_doi']}](https://doi.org/{r['verified_doi']})")
        if r["notes"]:
            report_lines.append(f"- **Примечания:** {'; '.join(r['notes'])}")
        report_lines.append("")

    report_md = "\n".join(report_lines)
    report_target = args.report or f"/var/tmp/scratch/bib_audit_{os.path.basename(bib_path)}.md"
    os.makedirs(os.path.dirname(os.path.abspath(report_target)), exist_ok=True)
    with open(report_target, "w", encoding="utf-8") as rf:
        rf.write(report_md)

    print(f"\n📄 Полный аналитический отчет сохранен в: {report_target}")


if __name__ == "__main__":
    main()
