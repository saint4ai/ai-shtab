#!/usr/bin/env python3
"""Наши цены против конкурентов по таблице сравнения.

  python3 tools/ceny.py ФАЙЛ --my "Название" [--list ЛИСТ] [--porog 3] [--vyhod ПУТЬ]

Таблица: в одной строке над парами колонок названия компаний, под ними метки «Цена» и «Коэфф»
(цена за кг и вес единицы продажи в кг: метра, листа, бухты). Строки, где заполнена только первая колонка
и нет цифр, считаются разделами («Круг», «Лист г/к»). Какая компания наша, скрипт не угадывает: --my.
Без --my покажет список компаний. Результат: Excel «имя.отчёт.xlsx» (листы Сводка, Мы дороже,
Мы дешевле, Коэффициенты, Вся таблица) и короткая сводка в консоли. Нужны библиотеки: bash tools/ustanovka.sh
"""
import argparse
import pathlib
import re
import statistics
import sys

import _venv  # перезапуск под .venv проекта, если он есть

from vagony import norm, put, read_sheets, to_num

ROOT = pathlib.Path(__file__).resolve().parent.parent
BIG_COEF = 50      # коэффициент больше этого: вес бухты или тяжёлого листа, с метровыми не смешиваем
COEF_SPREAD = 0.15  # расхождение коэффициентов, с которого ставим флаг


def fmt(v):
    if v is None:
        return ""
    if isinstance(v, float) and v == int(v):
        return str(int(v))
    return re.sub(r"\s+", " ", str(v)).strip()


def find_blocks(rows):
    """Строка с метками «Цена» и пары колонок компаний. Возвращает (номер строки с 0, [компании]) или None."""
    for i, row in enumerate(rows[:15]):
        cols = [c for c, v in enumerate(row) if norm(v) == "цена" and c + 1 < len(row) and norm(row[c + 1]).startswith("коэф")]
        if not cols:
            continue
        above = rows[i - 1] if i else []
        blocks, used = [], set()
        for n, c in enumerate(cols, 1):
            name = next((fmt(above[x]) for x in (c, c + 1) if x < len(above) and fmt(above[x])), f"Компания {n}")
            while name in used:
                name += " (2)"
            used.add(name)
            blocks.append({"name": name, "price": c, "coef": c + 1})
        return i, blocks
    return None


def read_positions(rows, hdr, blocks):
    """Позиции с ценами и коэффициентами компаний, номер строки исходника, раздел."""
    out, section = [], fmt(rows[hdr][0]) if rows[hdr] else ""
    for i in range(hdr + 1, len(rows)):
        row = rows[i] + [None] * 2
        a, b = fmt(row[0]), fmt(row[1])
        cells = {}
        for bl in blocks:
            p, k = to_num(row[bl["price"]]), to_num(row[bl["coef"]])
            if p is not None and p > 0:
                cells[bl["name"]] = (p, k if k and k > 0 else None)
        has_numbers = any(to_num(row[bl[x]]) is not None for bl in blocks for x in ("price", "coef"))
        if a and not b and not has_numbers and not re.search(r"\d", a):
            section = a
            continue
        if cells:
            out.append({"row": i + 1, "section": section, "name": f"{a}, {b}" if a and b else (a or b), "cells": cells})
    return out


def coef_class(k):
    return "big" if k > BIG_COEF else "small"


def analyse_spread(pos):
    """Расходятся ли коэффициенты компаний больше чем на 15%. Малые (вес метра) и большие (вес листа, балки)
    сравниваем отдельно. У бухты коэффициент это вес бухты: разный вес бухты не наценка, такие позиции не берём."""
    if "бухт" in pos["name"].lower():
        return False
    for cls in ("small", "big"):
        ks = [v[1] for v in pos["cells"].values() if v[1] and coef_class(v[1]) == cls]
        if len(ks) >= 2 and (max(ks) - min(ks)) / min(ks) > COEF_SPREAD:
            return True
    return False


def analyse(pos, my):
    """Наша цена против медианы конкурентов по одной позиции. None, если сравнивать не с чем."""
    cells = pos["cells"]
    if my not in cells:
        return None
    comp = {n: v for n, v in cells.items() if n != my}
    if not comp:
        return None
    mine = cells[my][0]
    prices = [v[0] for v in comp.values()]
    med = statistics.median(prices)
    low = min(prices)
    r = dict(pos, mine=mine, med=med, dev=(mine - med) / med * 100,
             cheapest=", ".join(n for n, v in comp.items() if v[0] == low), cheapest_price=low,
             place=1 + sum(1 for p in prices if p < mine), total=len(cells))
    r["spread"] = analyse_spread(pos)
    # цена за единицу: только у компаний с тем же классом коэффициента, что у нас
    mk = cells[my][1]
    r["unit"] = None
    if mk:
        same = {n: v for n, v in comp.items() if v[1] and coef_class(v[1]) == coef_class(mk)}
        if same:
            r["unit"] = {"mine": mine * mk, "med": statistics.median(v[0] * v[1] for v in same.values()),
                         "med_kg": statistics.median(v[0] for v in same.values()), "coef": mk,
                         "kmin": min(v[1] for v in same.values()), "kmax": max(v[1] for v in same.values())}
    return r


def main():
    ap = argparse.ArgumentParser(description="Наши цены против конкурентов")
    ap.add_argument("file")
    ap.add_argument("--my", help="название нашей компании как в шапке таблицы")
    ap.add_argument("--list", help="имя листа; по умолчанию первый, где есть метки «Цена» и «Коэфф»")
    ap.add_argument("--porog", type=float, default=3, help="порог отклонения от медианы в процентах, по умолчанию 3")
    ap.add_argument("--vyhod", help="куда положить отчёт .xlsx")
    a = ap.parse_args()

    path = pathlib.Path(a.file).expanduser().resolve()
    if not path.exists():
        sys.exit(f"Файл не найден: {a.file}")
    sheets = read_sheets(path)
    if a.list and a.list not in sheets:
        sys.exit(f"Листа «{a.list}» нет. Есть: " + ", ".join(sheets))
    found = None
    for n in ([a.list] if a.list else sheets):
        res = find_blocks(sheets[n])
        if res:
            found = (n, *res)
            break
    if not found:
        sys.exit("Не нашёл таблицу сравнения: нужна строка с метками «Цена» и «Коэфф» под названиями компаний. "
                 "Если лист другой, укажите его через --list.")
    name, hdr, blocks = found
    names = [b["name"] for b in blocks]
    if not a.my:
        sys.exit(f"В листе «{name}» найдены компании: {', '.join(names)}.\n"
                 "Укажите, какая из них наша: --my \"Название\"")
    key = norm(a.my)
    my = next((n for n in names if norm(n) == key), None) or next((n for n in names if key in norm(n)), None)
    if not my:
        sys.exit(f"Компании «{a.my}» нет. Есть: {', '.join(names)}")

    positions = read_positions(sheets[name], hdr, blocks)
    rows = [r for r in (analyse(p, my) for p in positions) if r]
    without_us = sum(1 for p in positions if my not in p["cells"])
    only_us = sum(1 for p in positions if my in p["cells"] and len(p["cells"]) == 1)
    pr = a.porog
    pricey = sorted((r for r in rows if r["dev"] > pr), key=lambda r: -r["dev"])
    cheap = sorted((r for r in rows if r["dev"] < -pr), key=lambda r: r["dev"])
    spread_n = sum(1 for r in rows if r["spread"])
    spread_all = sum(1 for p in positions if analyse_spread(p))
    first = sum(1 for r in rows if r["place"] == 1)
    last = sum(1 for r in rows if r["place"] == r["total"])
    k_cheap_kg, k_dear_kg = [], []
    for r in rows:
        u = r["unit"]
        if u and r["mine"] < u["med_kg"] and u["mine"] > u["med"]:
            k_cheap_kg.append(r)
        elif u and r["mine"] > u["med_kg"] and u["mine"] < u["med"]:
            k_dear_kg.append(r)

    from openpyxl import Workbook
    from openpyxl.styles import PatternFill
    wb = Workbook()
    yes = lambda r: "да" if r["spread"] else ""

    def base(r):
        return [r["row"], r["section"], r["name"]]

    ws = wb.active
    ws.title = "Сводка"
    put(ws, ["Показатель", "Значение"], [
        ("Файл", path.name), ("Лист", name), ("Наша компания", my), ("Конкуренты", ", ".join(n for n in names if n != my)),
        ("Порог отклонения от медианы, %", pr),
        ("Позиций с нашей ценой и ценой хотя бы одного конкурента", len(rows)),
        (f"Мы дороже медианы больше чем на {pr:g}%", len(pricey)), (f"Мы дешевле медианы больше чем на {pr:g}%", len(cheap)),
        ("Остальные, в пределах порога", len(rows) - len(pricey) - len(cheap)),
        ("Мы самые дешёвые в позициях", first), ("Мы самые дорогие в позициях", last),
        ("Расходятся коэффициенты больше чем на 15% (среди этих позиций)", spread_n),
        ("Расходятся коэффициенты больше чем на 15% (во всей таблице, по всем компаниям)", spread_all),
        ("За кг дешевле медианы, за единицу дороже", len(k_cheap_kg)), ("За кг дороже медианы, за единицу дешевле", len(k_dear_kg)),
        ("Позиций без нашей цены", without_us), ("Позиций только с нашей ценой", only_us),
    ])
    head = ["Строка в файле", "Раздел", "Позиция", "Наша цена, ₸/кг", "Медиана конкурентов, ₸/кг", "Отклонение, %",
            "Самый дешёвый конкурент", "Его цена, ₸/кг", "Наше место", "Коэфф. расходятся"]
    ws = wb.create_sheet("Мы дороже")
    put(ws, head, [base(r) + [r["mine"], r["med"], round(r["dev"], 1), r["cheapest"], r["cheapest_price"],
                              f"{r['place']} из {r['total']}", yes(r)] for r in pricey])
    ws = wb.create_sheet("Мы дешевле")
    put(ws, head[:6] + ["Запас до медианы, ₸/кг"] + head[6:],
        [base(r) + [r["mine"], r["med"], round(r["dev"], 1), round(r["med"] - r["mine"], 1), r["cheapest"],
                    r["cheapest_price"], f"{r['place']} из {r['total']}", yes(r)] for r in cheap])
    ws = wb.create_sheet("Коэффициенты")
    kr = [base(r) + ["за кг дешевле, за единицу дороже", r["mine"], r["unit"]["med_kg"], round(r["mine"] * r["unit"]["coef"], 1),
                     round(r["unit"]["med"], 1), r["unit"]["coef"], f"{r['unit']['kmin']:g} – {r['unit']['kmax']:g}"] for r in k_cheap_kg]
    kr += [base(r) + ["за кг дороже, за единицу дешевле", r["mine"], r["unit"]["med_kg"], round(r["mine"] * r["unit"]["coef"], 1),
                      round(r["unit"]["med"], 1), r["unit"]["coef"], f"{r['unit']['kmin']:g} – {r['unit']['kmax']:g}"] for r in k_dear_kg]
    put(ws, ["Строка в файле", "Раздел", "Позиция", "Что получилось", "Наша цена, ₸/кг", "Медиана конкурентов, ₸/кг",
             "Наша цена за единицу, ₸", "Медиана конкурентов за единицу, ₸", "Наш коэфф., кг", "Коэфф. конкурентов, кг"], kr)
    ws = wb.create_sheet("Вся таблица")
    comp_names = [n for n in names if n != my]
    all_rows = [base(r) + [r["cells"][my][0]] + [r["cells"][n][0] if n in r["cells"] else None for n in comp_names]
                + [r["med"], round(r["dev"], 1), f"{r['place']} из {r['total']}", yes(r)] for r in rows]
    put(ws, ["Строка в файле", "Раздел", "Позиция", f"{my} (наша), ₸/кг"] + [f"{n}, ₸/кг" for n in comp_names]
        + ["Медиана конкурентов, ₸/кг", "Отклонение, %", "Наше место", "Коэфф. расходятся"], all_rows)
    red, green = PatternFill("solid", fgColor="FFC7CE"), PatternFill("solid", fgColor="C6EFCE")
    dev_col = 4 + len(comp_names) + 2
    for rr, r in zip(ws.iter_rows(min_row=2), rows):
        if r["dev"] > 0:
            rr[3].fill = rr[dev_col - 1].fill = red
        elif r["dev"] < 0:
            rr[3].fill = rr[dev_col - 1].fill = green

    if a.vyhod:
        out = pathlib.Path(a.vyhod).expanduser().resolve()
    else:
        # отчёт не кладём во «входящие»: сторож данных считает любой новый файл там необработанным
        folder = ROOT / "сводки" if path.parent == ROOT / "входящие" else path.parent
        out = folder / f"{path.stem}.отчёт.xlsx"
    out.parent.mkdir(parents=True, exist_ok=True)
    wb.save(out)

    def top(lst, sign):
        r = lst[0]
        return f" Сильнее всего: «{r['name']}», {sign}{abs(r['dev']):.0f}% к медиане (строка {r['row']})."

    lines = [f"Лист «{name}», наша компания «{my}», конкурентов {len(comp_names)}.",
             f"Позиций, где есть наша цена и цена хотя бы одного конкурента: {len(rows)} (лист «Вся таблица»).",
             f"Мы дороже медианы больше чем на {pr:g}%: {len(pricey)} позиций (лист «Мы дороже»)." + (top(pricey, "+") if pricey else ""),
             f"Мы дешевле медианы больше чем на {pr:g}%: {len(cheap)} позиций, тут можно поднять цену (лист «Мы дешевле»)." + (top(cheap, "−") if cheap else ""),
             f"В пределах ±{pr:g}% от медианы: {len(rows) - len(pricey) - len(cheap)} позиций.",
             f"Мы самые дешёвые в {first} позициях, самые дорогие в {last}.",
             f"Коэффициенты у компаний расходятся больше чем на 15%: {spread_all} позиций во всей таблице, из них {spread_n} среди сравниваемых (колонка «Коэфф. расходятся» в листе «Вся таблица»).",
             f"За кг мы дешевле медианы, а за единицу дороже: {len(k_cheap_kg)}. Наоборот: {len(k_dear_kg)} (лист «Коэффициенты»).",
             f"Без нашей цены: {without_us} позиций, их не сравнивал.",
             f"Отчёт: {out}"]
    print("\n".join(lines))


if __name__ == "__main__":
    main()
