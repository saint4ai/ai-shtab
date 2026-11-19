#!/usr/bin/env python3
"""Сторож данных. Подключён в .claude/settings.json перед каждым обращением агента к файлам и командам.

1. Закрытая папка .private (исходники с персональными данными, таблица соответствий) агенту недоступна.
2. Пока во «входящих» есть необработанные файлы, агент не может их открыть: сначала шлюз
   `python3 tools/obezlichit.py inbox`, который делает обезличенные копии.
Выход с кодом 2 останавливает действие агента, текст ошибки агент видит и понимает, что делать.
"""
import json
import os
import pathlib
import sys

ROOT = pathlib.Path(os.environ.get("CLAUDE_PROJECT_DIR") or pathlib.Path(__file__).resolve().parents[2])
INBOX = ROOT / "входящие"
DONE = ROOT / ".private" / "обработано.txt"
SKIP = {"README.md", ".DS_Store"}


def block(msg):
    print(msg, file=sys.stderr)
    sys.exit(2)


def unprocessed():
    if not INBOX.is_dir():
        return []
    done = set(DONE.read_text(encoding="utf-8").splitlines()) if DONE.exists() else set()
    return [p.name for p in INBOX.iterdir() if p.is_file() and p.name not in SKIP
            and ".обезл." not in p.name and p.name not in done]


try:
    data = json.load(sys.stdin)
except Exception:
    sys.exit(0)

tool = data.get("tool_name", "")
inp = data.get("tool_input") or {}
text = " ".join(str(v) for v in inp.values() if isinstance(v, (str, int, float)))
cmd = str(inp.get("command", "")).strip()

if ".private" in text:
    block("Папка .private закрыта: там исходники с персональными данными и таблица соответствий. "
          "Работай с обезличенными копиями. Настоящие названия в готовый отчёт возвращает "
          "`python3 tools/obezlichit.py restore ФАЙЛ`.")

if "входящие" in text and tool not in ("Glob",):
    names_only = cmd.startswith("ls")
    gateway = "tools/obezlichit.py" in cmd
    waiting = unprocessed()
    if waiting and not (names_only or gateway):
        block("Во «входящих» есть необработанные файлы: " + ", ".join(sorted(waiting)[:10]) + ". "
              "Сначала запусти `python3 tools/obezlichit.py inbox`: он сделает обезличенные копии. "
              "Потом работай с файлами «*.обезл.*» и с теми, где персональных данных нет.")

sys.exit(0)
