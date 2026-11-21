#!/usr/bin/env python3
"""Проверка таблицы Excel: ошибки в формулах, итоги с разными диапазонами, числа и даты, записанные текстом.

  python3 tools/proverka_formul.py ФАЙЛ.xlsx

Печатает список «лист!ячейка: что не так» и итог по количеству. Файл только читается, не меняется.
Нужны библиотеки: bash tools/ustanovka.sh
"""
import collections
import datetime as dt
import pathlib
import re
import sys

import _venv  # перезапуск под .venv проекта, если он есть

ERRORS = {
    "#REF!": "ссылка на ячейку или лист, которых уже нет",
    "#DIV/0!": "деление на ноль",
    "#N/A": "не нашлось значение для поиска",
    "#VALUE!": "в формулу попал текст вместо числа",
    "#NAME?": "в формуле неизвестное имя или функция",
}
SUM = re.compile(r"^=\s*SUM\(\$?([A-Z]{1,3})\$?(\d+):\$?([A-Z]{1,3})\$?(\d+)\)$", re.I)
NUM_TEXT = re.compile(r"^[-+]?\d+(\.\d+)?$")
DATE_TEXT = re.compile(r"^\d{1,2}[./-]\d{1,2}[./-]\d{2,4}\s*(г\.?)?$")
LIMIT = 40  # строк одного вида в списке; остальное считаем числом


def show(title, lines):
    if not lines:
        return
    print(f"\n{title}")
    for s in lines[:LIMIT]:
        print("  " + s)
    if len(lines) > LIMIT:
        print(f"  ... и ещё {len(lines) - LIMIT}")


def main():
    if len(sys.argv) != 2 or sys.argv[1].startswith("-"):
        sys.exit(__doc__)
    path = pathlib.Path(sys.argv[1]).expanduser().resolve()
    if not path.exists():
        sys.exit(f"Файл не найден: {sys.argv[1]}")
    if path.suffix.lower() not in (".xlsx", ".xlsm"):
        sys.exit("Проверка работает с файлами .xlsx. Старую таблицу .xls сначала переведите: положите её во «входящие» и запустите python3 tools/obezlichit.py inbox")
    try:
        import openpyxl
    except ImportError:
        sys.exit("Нужны библиотеки для таблиц. Запустите установку: bash tools/ustanovka.sh")
    wb = openpyxl.load_workbook(path)
    wv = openpyxl.load_workbook(path, data_only=True)

    errors, totals, texts = [], [], []
    never_calculated = False
    for ws in wb.worksheets:
        wsv = wv[ws.title]
        sums = collections.defaultdict(list)             # строка -> [(колонка, начало, конец)]
        cols = collections.defaultdict(lambda: collections.Counter())
        text_cells = collections.defaultdict(list)       # (колонка, вид) -> [ячейки]
        for row in ws.iter_rows():
            for c in row:
                f = c.value
                if f is None:
                    continue
                cached = wsv[c.coordinate].value
                is_formula = isinstance(f, str) and f.startswith("=")
                where = f"{ws.title}!{c.coordinate}"
                if is_formula:
                    if cached is None:
                        never_calculated = True
                    own = next((e for e in ERRORS if e in f.upper()), None)
                    got = cached if isinstance(cached, str) and cached in ERRORS else None
                    if own:
                        errors.append(f"{where}: в формуле {own}: {ERRORS[own]}. Формула: {f}")
                    elif got:
                        errors.append(f"{where}: результат {got}: {ERRORS[got]}. Вероятно, ошибка пришла из ячеек, на которые ссылается формула")
                    m = SUM.match(f)
                    if m and m.group(1).upper() == m.group(3).upper() == c.column_letter:
                        sums[c.row].append((c.column_letter, int(m.group(2)), int(m.group(4))))
                    if isinstance(cached, (int, float)) and not isinstance(cached, bool):
                        cols[c.column]["num"] += 1
                    continue
                if c.data_type == "e":
                    errors.append(f"{where}: в ячейке записана ошибка {f}: {ERRORS.get(str(f), 'ошибка расчёта')}")
                elif isinstance(f, bool):
                    continue
                elif isinstance(f, (int, float)):
                    cols[c.column]["num"] += 1
                elif isinstance(f, (dt.datetime, dt.date)):
                    cols[c.column]["date"] += 1
                elif isinstance(f, str):
                    s = f.replace("\xa0", "").replace(" ", "").replace(",", ".")
                    if NUM_TEXT.match(s):
                        cols[c.column]["tnum"] += 1
                        text_cells[(c.column, "число")].append(c.coordinate)
                    elif DATE_TEXT.match(f.strip()):
                        cols[c.column]["tdate"] += 1
                        text_cells[(c.column, "дата")].append(c.coordinate)
        for r, items in sorted(sums.items()):
            groups = collections.defaultdict(list)
            for col, a, b in items:
                groups[(a, b)].append(col)
            if len(groups) > 1:
                first, last = items[0][0], items[-1][0]
                parts = "; ".join(f"строки {a}–{b} в колонках {', '.join(cs)}" for (a, b), cs in sorted(groups.items()))
                totals.append(f"{ws.title}!{first}{r}:{last}{r}: итоги суммируют разные диапазоны: {parts}")
        for (col, kind), cells in sorted(text_cells.items()):
            have = cols[col]["num" if kind == "число" else "date"]
            # колонка «числовая», если настоящих чисел или дат не меньше трёх и вдвое больше, чем записанных текстом
            if have >= 3 and have >= 2 * len(cells):
                shown = ", ".join(cells[:8]) + (f" и ещё {len(cells) - 8}" if len(cells) > 8 else "")
                what = "число записано текстом" if kind == "число" else "дата записана текстом"
                texts.append(f"{ws.title}!{cells[0]}: {what}, остальные значения колонки настоящие. Ячеек: {len(cells)} ({shown})")

    print(f"Проверил файл «{path.name}», листов {len(wb.worksheets)}.")
    if never_calculated:
        print("Внимание: в файле нет рассчитанных значений формул (его сохранила программа без пересчёта). "
              "Ошибки, пришедшие из других ячеек, могут быть не видны.")
    show("Ошибки в ячейках:", errors)
    show("Итоги с разными диапазонами:", totals)
    show("Числа и даты текстом:", texts)
    if not (errors or totals or texts):
        print("Проблем не нашёл.")
    else:
        print(f"\nИтого: ячеек с ошибками {len(errors)}, строк итогов с разными диапазонами {len(totals)}, "
              f"колонок с числами или датами текстом {len(texts)}.")


if __name__ == "__main__":
    main()
