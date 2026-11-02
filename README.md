# Universal Agent Ecosystem

Модульный репозиторий регламентов, практических навыков, плагинов и профильных MCP-серверов для автономных и парных AI-ассистентов (Antigravity, Cursor, VS Code, Windsurf, Claude Code, Neovim).

---

## 📁 Архитектура каталогов

```text
agent-ecosystem/
├── .gitignore               # Изоляция локальных БД, кэшей и ключей
├── symlink.sh               # Универсальный установщик симлинков под целевые IDE
├── README.md                # Спецификация экосистемы
├── rules/                   # Глобальные поведенческие регламенты (00-04)
├── skills/                  # Общесистемные базовые навыки
│   ├── web-data-miner/      # Парсинг и сбор данных
│   └── graphify/            # Построение графов знаний
├── plugins/                 # Специализированные плагины (правила + навыки)
│   ├── osint-suite/         # Разведка OSINT (19 навыков + регламент AGENTS.md)
│   ├── academic-craft/      # Научные публикации (PEER, Inside-Out, Beeblio-матрица, ARIS-достоверность, ГОСТ)
│   └── code-craft/          # Инженерия кода (Physical Oracle для CFD, ADR, VibeWise, Worktree, Drift Guard)
└── mcp-servers/             # Высокопроизводительные поисковые шлюзы
    ├── google-search/       # Gemini Search Grounding (отчеты FTS5 BM25, Dual-Stage Fallback)
    └── exa-search/          # Exa.ai SDK Grounding (0.5-2с поиск, балансировщик ключей, FTS5 BM25)
```

---

## 🚀 Установка и проброс симлинков (`symlink.sh`)

Скрипт `symlink.sh` создает атомарные символические ссылки на правила, навыки и MCP-серверы в целевую среду.

### Под Antigravity IDE (по умолчанию `~/.gemini`):
```bash
./symlink.sh --ide antigravity
```

### Под другие среды:
```bash
./symlink.sh --ide cursor
./symlink.sh --ide vscode
./symlink.sh --ide claude
```

### Ручное указание каталога:
```bash
./symlink.sh --target /path/to/custom/dir
```

### Тестовый запуск (без изменения файлов):
```bash
./symlink.sh --dry-run
```

---

## ⚙️ Стандарт конфигурации MCP-серверов

Каждый MCP-сервер в каталоге `mcp-servers/` содержит:
1. `config.json` — параметры путей к библиотеке (`storage_dir`), срока ротации (`retention_days`), параметров сети и моделей. Поддерживает как относительные пути (от папки сервера), так и абсолютные пути ОС.
2. `instructions.md` — исчерпывающий регламент инструментов и параметров для вызывающих агентов (агентам запрещено читать `server.py`).
3. `.gitignore` — изоляция локальных SQLite баз данных (`*.db`, `*.db-wal`), кэшей и реальных API-ключей.
4. `keys.txt.example` (для сервисов с балансировкой) — шаблон для указания собственных ключей.
