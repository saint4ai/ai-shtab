#!/bin/bash
# Офис агентов: Paperclip и плагин Paperclip Office на этом компьютере.
# Адрес только локальный: http://127.0.0.1:3100
#
#   bash tools/ofis.sh ustanovit   поставить и открыть (первый раз 2-5 минут)
#   bash tools/ofis.sh otkryt      открыть, если закрыт
#   bash tools/ofis.sh zakryt      закрыть
#   bash tools/ofis.sh status      открыт ли офис и какой плагин стоит
#   bash tools/ofis.sh udalit      закрыть и убрать данные офиса в Корзину

set -uo pipefail

PAPERCLIP="paperclipai@2026.1117.0"
OFFICE_VERSION="0.1.2"
URL="http://127.0.0.1:3100"
HOME_DIR="$HOME/.paperclip"
CONFIG="$HOME_DIR/instances/default/config.json"
LOG="$HOME_DIR/ofis.log"

export PAPERCLIP_TELEMETRY_DISABLED=1 DO_NOT_TRACK=1 npm_config_loglevel=error

pc() { npx -y -p node@24 -p "$PAPERCLIP" paperclipai "$@"; }
ok() { curl -s --max-time 3 "$URL/api/health" | grep -q '"status":"ok"'; }

need_node() {
  command -v npx >/dev/null && return 0
  echo "Нет Node.js. Установите версию LTS с https://nodejs.org (установщик для macOS), потом повторите."
  exit 1
}

otkryt() {
  if ok; then echo "Офис открыт: $URL"; return 0; fi
  need_node
  mkdir -p "$HOME_DIR"
  if [ -f "$CONFIG" ]; then args=(run); else args=(onboard --yes --no-install-service); fi
  nohup npx -y -p node@24 -p "$PAPERCLIP" paperclipai "${args[@]}" >>"$LOG" 2>&1 &
  for _ in $(seq 1 300); do ok && break; sleep 1; done
  if ok; then echo "Офис открыт: $URL"; else
    echo "Офис не открылся за 5 минут. Последние строки журнала $LOG:"; tail -20 "$LOG"; exit 1
  fi
}

telemetry_off() {
  python3 - "$CONFIG" <<'PY'
import json, sys
p = sys.argv[1]
d = json.load(open(p))
d.setdefault("telemetry", {})["enabled"] = False
json.dump(d, open(p, "w"), ensure_ascii=False, indent=2)
PY
}

case "${1:-}" in
  ustanovit)
    otkryt
    telemetry_off
    pc plugin install paperclip-office --version "$OFFICE_VERSION" || exit 1
    echo "Готово. Откройте $URL: мастер попросит название компании и первого агента. Офис в меню слева, пункт Office."
    ;;
  otkryt) otkryt ;;
  zakryt)
    pkill -f "paperclipai (run|onboard)" || true
    for _ in $(seq 1 20); do ok || break; sleep 1; done
    pkill -f "$HOME_DIR/instances/default/db" || true
    ok && { echo "Офис не закрылся, проверьте журнал $LOG"; exit 1; }
    echo "Офис закрыт."
    ;;
  status)
    if ok; then echo "Офис открыт: $URL"; pc plugin list; else echo "Офис закрыт."; fi
    ;;
  udalit)
    bash "$0" zakryt
    stamp=$(date +%Y%m%d-%H%M%S)
    [ -d "$HOME_DIR" ] && mv "$HOME_DIR" "$HOME/.Trash/paperclip-$stamp"
    for d in "$HOME"/.npm/_npx/*/; do
      grep -q '"paperclipai"' "$d/package.json" 2>/dev/null && mv "$d" "$HOME/.Trash/paperclip-npx-$stamp-$(basename "$d")"
    done
    echo "Офис удалён. Данные и скачанный Paperclip (около 3 ГБ) в Корзине: место освободится, когда очистите её."
    ;;
  *) sed -n '2,9p' "$0" | sed 's/^# \{0,1\}//'; exit 1 ;;
esac
