"""Перезапускает скрипт под окружением проекта (.venv), если оно есть.

Подключение: первой строкой скрипта `import _venv`. Нет .venv или скрипт уже идёт под ним: ничего не делает.
Окружение создаёт `bash tools/ustanovka.sh`.
"""
import os
import pathlib
import sys

_ENV = pathlib.Path(__file__).resolve().parent.parent / ".venv"
_PY = _ENV / "bin" / "python"

# resolve у папки, а не у бинарника: бинарник в окружении является ссылкой на системный Python
if _PY.exists() and pathlib.Path(sys.prefix).resolve() != _ENV.resolve():
    os.execv(str(_PY), [str(_PY), *sys.argv])
