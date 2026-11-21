#!/bin/bash
# Готовит рабочую папку: библиотеки для таблиц Excel, PDF и документов в папке проекта (.venv).
# Запуск из корня проекта. Повторный запуск безопасен: что уже стоит, пропускается.
#
#   bash tools/ustanovka.sh                 поставить всё, что можно без Homebrew
#   bash tools/ustanovka.sh --libreoffice   ещё поставить LibreOffice (около 1 ГБ), если есть Homebrew
#   bash tools/ustanovka.sh --proverka      ничего не ставить, только показать, что готово
#
# Скрипт не ставит Homebrew и Docker, не меняет настройки терминала, не просит пароль.
# Homebrew и автозапуск ставит `python3 tools/whatsapp.py install`.

set -uo pipefail
export LC_ALL=en_US.UTF-8   # чтобы таблица итога выравнивалась по буквам, а не по байтам

cd "$(dirname "$0")/.." || exit 1
ROOT="$PWD"
ENV="$ROOT/.venv"
PY="$ENV/bin/python"
REQ="$ROOT/tools/requirements.txt"
LOG="$(mktemp -t ustanovka)"
trap 'rm -f "$LOG"' EXIT

WITH_LO=0
ONLY_CHECK=0
for a in "$@"; do
  case "$a" in
    --libreoffice) WITH_LO=1 ;;
    --proverka) ONLY_CHECK=1 ;;
    *) echo "Неизвестный параметр: $a. Доступны: --proverka, --libreoffice"; exit 1 ;;
  esac
done

BREW=""
for b in /opt/homebrew/bin/brew /usr/local/bin/brew; do
  [ -x "$b" ] && BREW="$b" && break
done

fail() { echo; echo "$1"; [ -s "$LOG" ] && { echo "Подробности:"; tail -n 8 "$LOG"; }; exit 1; }
need_internet() { echo "Если нет интернета, подключитесь и запустите установку снова."; }

install_all() {
  echo "1/5 Проверяю Python"
  if ! python3 -c "import sys" >/dev/null 2>&1; then
    echo "Нужны инструменты разработчика Apple. Откроется окно, нажмите «Установить», подождите 5–10 минут и запустите установку снова."
    xcode-select --install >/dev/null 2>&1
    exit 1
  fi

  echo "2/5 Создаю окружение проекта (.venv)"
  if [ ! -x "$PY" ]; then
    python3 -m venv "$ENV" >"$LOG" 2>&1 || fail "Не удалось создать окружение."
  fi
  "$PY" -m pip install -q --disable-pip-version-check --upgrade pip >"$LOG" 2>&1 \
    || { need_internet; fail "Не удалось обновить установщик библиотек."; }

  echo "3/5 Ставлю библиотеки для Excel, PDF и картинок (1–3 минуты)"
  "$PY" -m pip install -q --disable-pip-version-check -r "$REQ" >"$LOG" 2>&1 \
    || { need_internet; fail "Не удалось поставить библиотеки."; }
  # Чтение PowerPoint и Word агентом: нужен Python 3.10 и новее. На старом pip подставил бы пустую заготовку, поэтому проверяем версию
  if "$PY" -c "import sys; sys.exit(0 if sys.version_info >= (3, 10) else 1)"; then
    "$PY" -m pip install -q --disable-pip-version-check "markitdown[pptx,docx]" >"$LOG" 2>&1 || true
  fi

  echo "4/5 Проверяю браузер для PDF"
  if [ ! -d "/Applications/Google Chrome.app" ]; then
    "$PY" -m playwright install chromium >"$LOG" 2>&1 \
      || { need_internet; fail "Не удалось поставить браузер для PDF. Можно установить Google Chrome и запустить снова."; }
  fi

  echo "5/5 Проверяю Word, PowerPoint и пересчёт формул"
  if [ -n "$BREW" ]; then
    eval "$("$BREW" shellenv)"   # только для этого запуска, настройки терминала не меняются
    command -v node >/dev/null 2>&1 || "$BREW" install node >"$LOG" 2>&1 || echo "Node.js не поставился, продолжаю."
    command -v pandoc >/dev/null 2>&1 || "$BREW" install pandoc >"$LOG" 2>&1 || echo "Pandoc не поставился, продолжаю."
    if command -v npm >/dev/null 2>&1; then
      npm ls -g --depth=0 2>/dev/null | grep -q pptxgenjs && npm ls -g --depth=0 2>/dev/null | grep -q " docx@" \
        || npm install -g pptxgenjs docx >"$LOG" 2>&1 || echo "Пакеты для Word и PowerPoint не поставились, продолжаю."
    fi
    if [ "$WITH_LO" = 1 ] && [ ! -d "/Applications/LibreOffice.app" ] && ! command -v soffice >/dev/null 2>&1; then
      echo "Ставлю LibreOffice, это около 1 ГБ"
      "$BREW" install --cask libreoffice >"$LOG" 2>&1 || echo "LibreOffice не поставился, продолжаю."
    fi
  fi
}

row() {   # название | состояние
  local name="$1" pad=""
  while [ $(( ${#name} + ${#pad} )) -lt 38 ]; do pad="$pad "; done
  echo "$name$pad$2"
}

summary() {
  echo; echo "Итог"
  CORE=1
  local PIP_OK=0 b
  [ -x "$PY" ] && PIP_OK=1

  if [ "$PIP_OK" = 1 ] && "$PY" -c "import openpyxl, xlrd, pandas" >/dev/null 2>&1; then
    row "Таблицы Excel (.xlsx и старые .xls)" "готово"
  else
    CORE=0; row "Таблицы Excel (.xlsx и старые .xls)" "нет: запустите bash tools/ustanovka.sh"
  fi

  if [ "$PIP_OK" = 1 ] && "$PY" -c "import pypdf, pdfplumber, reportlab, PIL" >/dev/null 2>&1; then
    row "Чтение и сборка PDF" "готово"
  else
    CORE=0; row "Чтение и сборка PDF" "нет: запустите bash tools/ustanovka.sh"
  fi

  b=""
  [ "$PIP_OK" = 1 ] && b="$("$PY" - 2>/dev/null <<'PYEOF'
from playwright.sync_api import sync_playwright
with sync_playwright() as p:
    for kw, name in (({}, "Chromium"), ({"channel": "chrome"}, "Google Chrome")):
        try:
            p.chromium.launch(**kw).close()
            print(name)
            break
        except Exception:
            pass
PYEOF
)"
  if [ -n "$b" ]; then row "PDF из HTML" "готово ($b)"
  else CORE=0; row "PDF из HTML" "нет: установите Google Chrome или запустите bash tools/ustanovka.sh"; fi

  if [ "$PIP_OK" = 1 ] && "$PY" -c "import imageio_ffmpeg" >/dev/null 2>&1; then
    row "Презентация в WhatsApp (GIF)" "готово"
  else
    row "Презентация в WhatsApp (GIF)" "нет: запустите bash tools/ustanovka.sh"
  fi

  if [ "$PIP_OK" = 1 ] && "$PY" -c "from markitdown import MarkItDown" >/dev/null 2>&1; then
    row "Чтение PowerPoint и Word" "готово"
  else
    row "Чтение PowerPoint и Word" "нет: нужен Python новее, необязательно"
  fi

  [ -n "$BREW" ] && eval "$("$BREW" shellenv)"
  if command -v node >/dev/null 2>&1 && command -v pandoc >/dev/null 2>&1 \
     && npm ls -g --depth=0 2>/dev/null | grep -q pptxgenjs && npm ls -g --depth=0 2>/dev/null | grep -q " docx@"; then
    row "Word и PowerPoint с нуля" "готово (Node $(node -v))"
  elif [ -z "$BREW" ]; then
    row "Word и PowerPoint с нуля" "нет: появятся после подключения WhatsApp (там ставится Homebrew), затем запустите установку ещё раз"
  else
    row "Word и PowerPoint с нуля" "нет: запустите bash tools/ustanovka.sh"
  fi

  if [ -d "/Applications/LibreOffice.app" ] || command -v soffice >/dev/null 2>&1; then
    row "Пересчёт формул" "готово"
  else
    row "Пересчёт формул" "по желанию: bash tools/ustanovka.sh --libreoffice"
  fi
}

[ "$ONLY_CHECK" = 1 ] || install_all
summary
[ "$CORE" = 1 ] || exit 1
