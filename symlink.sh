#!/usr/bin/env bash
# ==============================================================================
#  Agent Ecosystem — Универсальный менеджер симлинков и профилей IDE
#  Пробрасывает правила, навыки, плагины и MCP-серверы в целевые среды:
#  - Antigravity (~/.gemini/)
#  - Cursor (~/.cursor/ или workspace .cursor/)
#  - VS Code / Windsurf
#  - Claude Code (~/.claude/)
# ==============================================================================

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
MODULE_DIR="$SCRIPT_DIR"

# Цветовая палитра
BOLD='\033[1m'
CYAN='\033[0;36m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
RED='\033[0;31m'
NC='\033[0m'

IDE_MODE="antigravity"
TARGET_DIR=""
DO_BACKUP=true
DRY_RUN=false

print_usage() {
    echo -e "${BOLD}Использование:${NC} $0 [опции]"
    echo ""
    echo "Опции:"
    echo "  --ide, -i <name>        Профиль IDE: antigravity (по умолчанию), cursor, vscode, claude"
    echo "  --target, -t <path>     Пользовательский целевой каталог (переопределяет стандартный путь IDE)"
    echo "  --no-backup             Не создавать резервные копии заменяемых каталогов"
    echo "  --dry-run, -n           Тестовый запуск без изменения файлов"
    echo "  --help, -h              Показать эту справку"
}

while [[ $# -gt 0 ]]; do
    case "$1" in
        --ide|-i)
            IDE_MODE="$2"
            shift 2
            ;;
        --target|-t)
            TARGET_DIR="$2"
            shift 2
            ;;
        --no-backup)
            DO_BACKUP=false
            shift
            ;;
        --dry-run|-n)
            DRY_RUN=true
            shift
            ;;
        --help|-h)
            print_usage
            exit 0
            ;;
        *)
            echo -e "${RED}Неизвестный параметр: $1${NC}"
            print_usage
            exit 1
            ;;
    esac
done

link_item() {
    local src="$1"
    local dst="$2"

    if [ "$DRY_RUN" = true ]; then
        echo -e "  ${CYAN}[DRY-RUN]${NC} $dst -> $src"
        return 0
    fi

    local parent_dir
    parent_dir="$(dirname "$dst")"
    mkdir -p "$parent_dir"

    # Если путь существует и это не симлинк
    if [ -e "$dst" ] && [ ! -L "$dst" ]; then
        if [ "$DO_BACKUP" = true ]; then
            local bak="${dst}.bak.$(date +%Y%m%d_%H%M%S)"
            mv "$dst" "$bak"
            echo -e "  ${YELLOW}📦 Создана резервная копия:${NC} $bak"
        else
            rm -rf "$dst"
        fi
    elif [ -L "$dst" ]; then
        rm -f "$dst"
    fi

    ln -sfn "$src" "$dst"
    echo -e "  ${GREEN}✓ Симлинк:${NC} $dst -> $src"
}

echo -e "${CYAN}${BOLD}"
echo "========================================================================"
echo "          🌐 AGENT ECOSYSTEM — UNIVERSAL SYMLINK INSTALLER              "
echo "========================================================================"
echo -e "${NC}"
echo -e "Профиль: ${BOLD}${IDE_MODE}${NC} | Модуль: ${CYAN}${MODULE_DIR}${NC}"

case "$IDE_MODE" in
    antigravity)
        TARGET="${TARGET_DIR:-$HOME/.gemini}"
        echo -e "Целевой каталог: ${BOLD}${TARGET}${NC}\n"

        echo -e "${BOLD}[1/5] Проброс глобальных регламентов (rules)...${NC}"
        link_item "$MODULE_DIR/rules" "$TARGET/config/rules"

        echo -e "\n${BOLD}[2/5] Проброс навыков (skills)...${NC}"
        link_item "$MODULE_DIR/skills" "$TARGET/config/skills"

        echo -e "\n${BOLD}[3/5] Проброс плагинов (plugins)...${NC}"
        mkdir -p "$TARGET/config/plugins"
        for p in "$MODULE_DIR/plugins"/*; do
            if [ -d "$p" ]; then
                link_item "$p" "$TARGET/config/plugins/$(basename "$p")"
            fi
        done

        echo -e "\n${BOLD}[4/5] Проброс MCP-серверов (mcp-servers)...${NC}"
        mkdir -p "$TARGET/mcp-servers"
        for s in "$MODULE_DIR/mcp-servers"/*; do
            if [ -d "$s" ]; then
                sname="$(basename "$s")"
                link_item "$s" "$TARGET/mcp-servers/$sname"
                # Проброс инструкции в нативную директорию Antigravity mcp/<name>/instructions.md
                if [ -f "$s/instructions.md" ]; then
                    link_item "$s/instructions.md" "$TARGET/antigravity/mcp/$sname/instructions.md"
                fi
            fi
        done

        echo -e "\n${BOLD}[5/5] Проверка секретов и окружения...${NC}"
        EXA_KEYS_LIVE="$TARGET/mcp-servers/exa-search/keys.txt"
        EXA_KEYS_SRC="$MODULE_DIR/mcp-servers/exa-search/keys.txt.example"
        if [ ! -f "$EXA_KEYS_LIVE" ] && [ -f "$EXA_KEYS_SRC" ]; then
            if [ "$DRY_RUN" = false ]; then
                cp "$EXA_KEYS_SRC" "$EXA_KEYS_LIVE"
                echo -e "  ${YELLOW}ℹ️ Создан шаблон ключей:${NC} $EXA_KEYS_LIVE"
            fi
        fi
        ;;

    cursor)
        TARGET="${TARGET_DIR:-$HOME/.cursor}"
        echo -e "Целевой каталог Cursor: ${BOLD}${TARGET}${NC}\n"
        mkdir -p "$TARGET/rules"
        link_item "$MODULE_DIR/rules" "$TARGET/rules/agent-rules"
        link_item "$MODULE_DIR/skills" "$TARGET/skills"
        ;;

    vscode|windsurf)
        TARGET="${TARGET_DIR:-$HOME/.config/Code/User}"
        echo -e "Целевой каталог VS Code: ${BOLD}${TARGET}${NC}\n"
        link_item "$MODULE_DIR/rules" "$TARGET/agent-rules"
        link_item "$MODULE_DIR/skills" "$TARGET/skills"
        ;;

    claude)
        TARGET="${TARGET_DIR:-$HOME/.claude}"
        echo -e "Целевой каталог Claude: ${BOLD}${TARGET}${NC}\n"
        link_item "$MODULE_DIR/rules" "$TARGET/rules"
        link_item "$MODULE_DIR/skills" "$TARGET/skills"
        ;;

    *)
        echo -e "${RED}❌ Неизвестный профиль IDE: $IDE_MODE${NC}"
        exit 1
        ;;
esac

echo -e "\n${GREEN}${BOLD}========================================================================${NC}"
echo -e "${GREEN}${BOLD}           🎉 СИМЛИНКИ ЭКОСИСТЕМЫ УСПЕШНО АКТУАЛИЗИРОВАНЫ!              ${NC}"
echo -e "${GREEN}${BOLD}========================================================================${NC}\n"
