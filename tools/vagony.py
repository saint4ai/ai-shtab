#!/usr/bin/env python3
"""Контроль вагонов: сколько едут, как быстро их сажают в 1С, где нарушены сроки.

  python3 tools/vagony.py ФАЙЛ [--list ЛИСТ] [--v-puti 8] [--posadka 2] [--segodnya ГГГГ-ММ-ДД] [--vyhod ПУТЬ]

ФАЙЛ: .xlsx, .xls или .csv (выгрузка Google Таблицы). Колонки находятся по заголовкам:
дата прихода, дата выхода, завод, тн, номер вагона, дата выгрузки, дата оформления, станция выгрузки.
Результат: Excel «имя.отчёт.xlsx» (листы Сводка, По заводам, По станциям, Нарушения, Ошибки данных)
и короткая сводка в консоли. Исходный файл не меняется. Нужны библиотеки: bash tools/ustanovka.sh
"""
import argparse
import collections
import csv
import datetime as dt
import io
import pathlib
import re
import statistics
import sys

import _venv  # перезапуск под .venv проекта, если он есть

ROOT = pathlib.Path(__file__).resolve().parent.parent

# поле -> шаблон заголовка (после приведения к нижнему регистру, ё -> е)
FIELDS = {
    "arrival": ("дата прихода", r"^(дата\s+(прихода|прибытия)|приход|прибытие)$"),
    "depart": ("дата выхода", r"^(дата\s+(выхода|отгрузки)|выход|отгрузка)$"),
    "plant": ("завод", r"завод"),
    "tons": ("тн", r"^(тн|тонн\w*|вес.*)$"),
    "wagon": ("номер вагона", r"^(номер\s+вагона|№\s*вагона|вагон)$"),
    "unload": ("дата выгрузки", r"^(дата\s+(выгрузки|разгрузки)|выгрузка|разгрузка)$"),
    "register": ("дата оформления", r"оф\w*рмлени|посадк"),
    "station": ("станция выгрузки", r"станци"),
}
OPTIONAL = {"station"}


def need_libs():
    try:
        import openpyxl
        return openpyxl
    except ImportError:
        sys.exit("Нужны библиотеки для таблиц. Запустите установку: bash tools/ustanovka.sh")


def read_sheets(path):
    """Возвращает {имя листа: список строк}. Пустые ячейки None, даты datetime."""
    suf = path.suffix.lower()
    if suf == ".csv":
        raw = path.read_bytes()
        for enc in ("utf-8-sig", "cp1251"):
            try:
                text = raw.decode(enc)
                break
            except UnicodeDecodeError:
                continue
        else:
            sys.exit(f"Не удалось прочитать {path.name}: неизвестная кодировка")
        try:
            dialect = csv.Sniffer().sniff(text[:4096], delimiters=";,\t")
        except csv.Error:
            dialect = csv.excel
        rows = [[(v.strip() or None) for v in r] for r in csv.reader(io.StringIO(text), dialect)]
        return {"csv": rows}
    openpyxl = need_libs()
    if suf == ".xls":
        try:
            import xlrd
        except ImportError:
            sys.exit("Для старых таблиц .xls нужны библиотеки. Запустите установку: bash tools/ustanovka.sh")
        book = xlrd.open_workbook(str(path))
        out = {}
        for sh in book.sheets():
            rows = []
            for r in range(sh.nrows):
                row = []
                for c in range(sh.ncols):
                    t, v = sh.cell_type(r, c), sh.cell_value(r, c)
                    if t in (xlrd.XL_CELL_EMPTY, xlrd.XL_CELL_BLANK) or v == "":
                        v = None
                    elif t == xlrd.XL_CELL_DATE:
                        v = xlrd.xldate_as_datetime(v, book.datemode)
                    row.append(v)
                rows.append(row)
            out[sh.name] = rows
        return out
    if suf not in (".xlsx", ".xlsm"):
        sys.exit(f"Формат {path.suffix} не поддерживается: .xlsx, .xls, .csv")
    wb = openpyxl.load_workbook(path, data_only=True)
    return {ws.title: [list(r) for r in ws.iter_rows(values_only=True)] for ws in wb.worksheets}


def norm(s):
    return re.sub(r"\s+", " ", str(s).replace("ё", "е").replace("Ё", "Е")).strip().lower() if s is not None else ""


def find_columns(rows):
    """Ищет строку заголовков среди первых 10 строк. Возвращает (номер строки с 0, {поле: колонка})."""
    best = (0, {})
    for i, row in enumerate(rows[:10]):
        found = {}
        for c, v in enumerate(row):
            h = norm(v)
            for key, (_, rx) in FIELDS.items():
                if key not in found and h and re.search(rx, h):
                    found[key] = c
        if len(found) > len(best[1]):
            best = (i, found)
    return best


def to_date(v):
    """Дата из ячейки: настоящая дата или текст вида 05.03.2024г. Возвращает (дата или None, текст не разобран)."""
    if v is None or (isinstance(v, str) and not v.strip()):
        return None, False
    if isinstance(v, dt.datetime):
        return v.date(), False
    if isinstance(v, dt.date):
        return v, False
    s = str(v).strip()
    m = re.search(r"(\d{1,2})[.\-/](\d{1,2})[.\-/](\d{4}|\d{2})", s)
    try:
        if m:
            d, mo, y = int(m.group(1)), int(m.group(2)), int(m.group(3))
            return dt.date(y + 2000 if y < 100 else y, mo, d), False
        m = re.search(r"(\d{4})-(\d{2})-(\d{2})", s)
        if m:
            return dt.date(int(m.group(1)), int(m.group(2)), int(m.group(3))), False
    except ValueError:
        pass
    return None, True


def to_num(v):
    if v is None:
        return None
    if isinstance(v, (int, float)) and not isinstance(v, bool):
        return float(v)
    try:
        return float(str(v).replace("\xa0", "").replace(" ", "").replace(",", "."))
    except ValueError:
        return None


def levenshtein(a, b):
    if abs(len(a) - len(b)) > 1:
        return 2
    prev = list(range(len(b) + 1))
    for i, ca in enumerate(a, 1):
        cur = [i]
        for j, cb in enumerate(b, 1):
            cur.append(min(prev[j] + 1, cur[j - 1] + 1, prev[j - 1] + (ca != cb)))
        prev = cur
    return prev[-1]


def plant_groups(names):
    """Названия с разницей в одну букву склеиваются. Возвращает {название как есть: общее название группы}.
    Короткие названия (до 4 букв) не склеиваем: у аббревиатур разница в букву чаще означает другой завод."""
    keys = {}
    for n in names:
        keys.setdefault(re.sub(r"\s+", " ", n).strip().lower(), []).append(n)
    ks = sorted(keys)
    parent = {k: k for k in ks}

    def root(k):
        while parent[k] != k:
            k = parent[k]
        return k

    for i, a in enumerate(ks):
        for b in ks[i + 1:]:
            if min(len(a), len(b)) >= 4 and levenshtein(a, b) <= 1:
                parent[root(b)] = root(a)
    return {n: root(k) for k, ns in keys.items() for n in ns}


def parse(rows, cols, hdr_row):
    """Строки-вагоны с вычисленными датами и сроками."""
    cnt_plant = collections.Counter()
    items = []
    for i in range(hdr_row + 1, len(rows)):
        row = rows[i]
        get = lambda k: row[cols[k]] if k in cols and cols[k] < len(row) else None
        if not any(get(k) not in (None, "") for k in ("wagon", "plant", "arrival", "depart")):
            continue
        it = {"row": i + 1, "errors": [], "viol": []}
        wg = get("wagon")
        it["wagon"] = str(int(wg)) if isinstance(wg, float) and wg == int(wg) else (str(wg).strip() if wg is not None else "")
        plant = re.sub(r"\s+", " ", str(get("plant") or "")).strip()
        it["plant"] = plant
        if plant:
            cnt_plant[plant] += 1
        it["station"] = re.sub(r"\s+", " ", str(get("station") or "")).strip()
        for k in ("arrival", "depart", "unload", "register"):
            d, bad = to_date(get(k))
            it[k] = d
            if bad:
                it["errors"].append(f"дата «{FIELDS[k][0]}» не разобрана: «{get(k)}»")
        t = to_num(get("tons"))
        if t is not None and t > 200:
            it["errors"].append(f"тоннаж {t:g} похож на килограммы, пересчитан в {t / 1000:g} т")
            t = t / 1000
        it["tons"] = t
        it["transit"] = (it["arrival"] - it["depart"]).days if it["arrival"] and it["depart"] else None
        it["to_unload"] = (it["unload"] - it["arrival"]).days if it["unload"] and it["arrival"] else None
        it["posadka"] = (it["register"] - it["arrival"]).days if it["register"] and it["arrival"] else None
        if it["transit"] is not None and it["transit"] < 0:
            it["errors"].append("дата выхода позже даты прихода")
        items.append(it)
    return items, cnt_plant


def find_violations(items, v_puti, posadka, today):
    for it in items:
        v = it["viol"]
        if it["transit"] is not None and it["transit"] > v_puti:
            v.append((f"в пути дольше {v_puti} дн.", it["transit"] - v_puti))
        if it["posadka"] is not None and it["posadka"] > posadka:
            v.append((f"посадка дольше {posadka} дн.", it["posadka"] - posadka))
        if it["unload"] and it["register"] and it["register"] < it["unload"]:
            v.append(("оформление раньше выгрузки", (it["unload"] - it["register"]).days))
        stuck = it["depart"] and not it["arrival"] and (today - it["depart"]).days > v_puti
        unreg = it["arrival"] and not it["register"] and (today - it["arrival"]).days > posadka
        if stuck:
            v.append((f"задерживается в пути: вышел {it['depart']:%d.%m.%Y}, прихода нет", (today - it["depart"]).days - v_puti))
        if unreg:
            v.append((f"не посажен: пришёл {it['arrival']:%d.%m.%Y}, оформления нет", (today - it["arrival"]).days - posadka))
        for k, name in (("arrival", "прихода"), ("depart", "выхода"), ("unload", "выгрузки"), ("register", "оформления")):
            covered = (k == "arrival" and stuck) or (k == "register" and unreg)
            if it[k] is None and not covered and not any(name in e for e in it["errors"]):
                v.append((f"нет даты {name}", None))


def style_sheet(ws):
    from openpyxl.styles import Font
    for c in ws[1]:
        c.font = Font(bold=True)
    ws.freeze_panes = "A2"
    for col in ws.columns:
        width = max((len(str(c.value)) for c in col if c.value is not None), default=8)
        ws.column_dimensions[col[0].column_letter].width = min(max(width + 2, 10), 70)


def put(ws, header, rows):
    ws.append(header)
    for r in rows:
        ws.append(list(r))
    style_sheet(ws)


def vagonov(n):
    return f"{n} " + ("вагонов" if 11 <= n % 100 <= 14 or n % 10 in (0, 5, 6, 7, 8, 9) else "вагон" if n % 10 == 1 else "вагона")


def med(xs):
    return statistics.median(xs) if xs else None


def main():
    ap = argparse.ArgumentParser(description="Контроль вагонов: сроки в пути и посадка")
    ap.add_argument("file")
    ap.add_argument("--list", help="имя листа; по умолчанию первый, где нашлись нужные колонки")
    ap.add_argument("--v-puti", type=int, default=8, help="норма дней в пути, по умолчанию 8")
    ap.add_argument("--posadka", type=int, default=2, help="норма дней на посадку после прихода, по умолчанию 2")
    ap.add_argument("--segodnya", help="дата отсчёта ГГГГ-ММ-ДД, по умолчанию сегодня")
    ap.add_argument("--vyhod", help="куда положить отчёт .xlsx")
    a = ap.parse_args()

    path = pathlib.Path(a.file).expanduser().resolve()
    if not path.exists():
        sys.exit(f"Файл не найден: {a.file}")
    try:
        today = dt.date.fromisoformat(a.segodnya) if a.segodnya else dt.date.today()
    except ValueError:
        sys.exit("Дата в --segodnya должна быть в виде ГГГГ-ММ-ДД, например 2026-11-21")

    sheets = read_sheets(path)
    if a.list:
        if a.list not in sheets:
            sys.exit(f"Листа «{a.list}» нет. Есть: " + ", ".join(sheets))
        names = [a.list]
    else:
        names = list(sheets)
    chosen = None
    for n in names:
        hdr, cols = find_columns(sheets[n])
        if all(k in cols for k in FIELDS if k not in OPTIONAL):
            chosen = (n, hdr, cols)
            break
    if not chosen:
        n = names[0]
        hdr, cols = find_columns(sheets[n])
        miss = [FIELDS[k][0] for k in FIELDS if k not in cols and k not in OPTIONAL]
        heads = [str(v) for v in (sheets[n][hdr] if sheets[n] else []) if v not in (None, "")]
        sys.exit(f"В листе «{n}» не хватает колонок: {', '.join(miss)}. Заголовки в файле: {', '.join(heads) or 'не найдены'}. "
                 "Укажите нужный лист через --list.")
    name, hdr, cols = chosen
    items, cnt_plant = parse(sheets[name], cols, hdr)
    if not items:
        sys.exit(f"В листе «{name}» нет строк с вагонами")
    find_violations(items, a.v_puti, a.posadka, today)

    grp = plant_groups(list(cnt_plant))
    group_names = collections.defaultdict(list)
    for n_, g in grp.items():
        group_names[g].append(n_)
    merged = {g: sorted(ns, key=lambda x: -cnt_plant[x]) for g, ns in group_names.items() if len(ns) > 1}
    for it in items:
        it["group"] = merged[grp[it["plant"]]][0] if it["plant"] in grp and grp[it["plant"]] in merged else it["plant"]

    from openpyxl import Workbook
    wb = Workbook()

    # По заводам
    by_plant = collections.defaultdict(list)
    for it in items:
        by_plant[it["group"] or "(завод не указан)"].append(it)
    plant_rows = []
    for g, its in sorted(by_plant.items(), key=lambda kv: -len(kv[1])):
        tr = [x["transit"] for x in its if x["transit"] is not None]
        ok = sum(1 for t in tr if t <= a.v_puti)
        alias = ""
        for ns in merged.values():
            if ns[0] == g:
                alias = "объединены написания: " + " / ".join(f"{x} ({cnt_plant[x]})" for x in ns)
        plant_rows.append([g, len(its), round(sum(x["tons"] or 0 for x in its), 3), med(tr), max(tr) if tr else None,
                           ok / len(tr) if tr else None, alias])
    # По станциям
    by_st = collections.defaultdict(list)
    for it in items:
        by_st[it["station"] or "(станция не указана)"].append(it)
    st_rows = []
    for s, its in sorted(by_st.items(), key=lambda kv: -len(kv[1])):
        ps = [x["posadka"] for x in its if x["posadka"] is not None]
        uv = [x["to_unload"] for x in its if x["to_unload"] is not None]
        st_rows.append([s, len(its), round(sum(x["tons"] or 0 for x in its), 3), med(uv), med(ps), max(ps) if ps else None])
    # Нарушения и ошибки
    viol_rows, err_rows = [], []
    for it in items:
        for what, days in it["viol"]:
            viol_rows.append([it["row"], it["wagon"], it["plant"], what, days])
        for e in it["errors"]:
            err_rows.append([it["row"], it["wagon"], it["plant"], e])
    for ns in merged.values():
        total = sum(cnt_plant[x] for x in ns)
        err_rows.append(["", "", "", f"возможно, один завод: {' / '.join(ns)} ({' + '.join(str(cnt_plant[x]) for x in ns)} = {vagonov(total)})"])

    tr_all = [x["transit"] for x in items if x["transit"] is not None]
    ps_all = [x["posadka"] for x in items if x["posadka"] is not None]
    long_transit = [x for x in items if x["transit"] is not None and x["transit"] > a.v_puti]
    long_posadka = [x for x in items if x["posadka"] is not None and x["posadka"] > a.posadka]
    early = [x for x in items if x["unload"] and x["register"] and x["register"] < x["unload"]]
    kg_rows = [x["row"] for x in items if any("килограмм" in e for e in x["errors"])]
    stuck = [x for x in items if any(w.startswith("задерживается") for w, _ in x["viol"])]
    unreg = [x for x in items if any(w.startswith("не посажен") for w, _ in x["viol"])]
    no_depart = [x["row"] for x in items if x["depart"] is None and not any("выхода" in e for e in x["errors"])]

    summary = [
        ("Файл", path.name), ("Лист", name), ("Вагонов", len(items)),
        ("Тонн всего", round(sum(x["tons"] or 0 for x in items), 3)),
        ("Норма: дней в пути", a.v_puti), ("Норма: дней на посадку", a.posadka),
        ("Дней в пути, медиана", med(tr_all)), ("Дней в пути, максимум", max(tr_all) if tr_all else None),
        (f"В пути дольше нормы: вагонов из {len(tr_all)}", len(long_transit)),
        ("Посадка, медиана дней", med(ps_all)), ("Посадка, максимум дней", max(ps_all) if ps_all else None),
        (f"Посадка дольше нормы: вагонов из {len(ps_all)}", len(long_posadka)),
        ("Оформление раньше выгрузки, вагонов", len(early)),
        ("Тоннаж похож на кг, строк", len(kg_rows)),
        (f"Задерживаются в пути на {today:%d.%m.%Y}", len(stuck)),
        (f"Не посажены на {today:%d.%m.%Y}", len(unreg)),
        ("Нарушений в листе «Нарушения»", len(viol_rows)), ("Ошибок данных", len(err_rows)),
    ]
    ws = wb.active
    ws.title = "Сводка"
    put(ws, ["Показатель", "Значение"], summary)
    ws = wb.create_sheet("По заводам")
    put(ws, ["Завод", "Вагонов", "Тонн", "Дней в пути, медиана", "Дней в пути, максимум", "Доля вовремя", "Примечание"], plant_rows)
    for r in ws.iter_rows(min_row=2):
        r[1].number_format, r[2].number_format, r[5].number_format = "#,##0", "#,##0.0", "0%"
    ws = wb.create_sheet("По станциям")
    put(ws, ["Станция", "Вагонов", "Тонн", "Выгрузка после прихода, медиана дней", "Посадка, медиана дней", "Посадка, максимум дней"], st_rows)
    for r in ws.iter_rows(min_row=2):
        r[1].number_format, r[2].number_format = "#,##0", "#,##0.0"
    ws = wb.create_sheet("Нарушения")
    put(ws, ["Строка в файле", "Вагон", "Завод", "Что не так", "На сколько дней"], viol_rows)
    ws = wb.create_sheet("Ошибки данных")
    put(ws, ["Строка в файле", "Вагон", "Завод", "Что не так"], err_rows)

    if a.vyhod:
        out = pathlib.Path(a.vyhod).expanduser().resolve()
    else:
        # отчёт не кладём во «входящие»: сторож данных считает любой новый файл там необработанным
        folder = ROOT / "сводки" if path.parent == ROOT / "входящие" else path.parent
        out = folder / f"{path.stem}.отчёт.xlsx"
    out.parent.mkdir(parents=True, exist_ok=True)
    wb.save(out)

    def rows_txt(rs):
        return ", ".join(map(str, rs[:6])) + (" и др." if len(rs) > 6 else "")

    tons_txt = f"{sum(x['tons'] or 0 for x in items):,.0f}".replace(",", " ")
    lines = [f"Файл «{path.name}», лист «{name}»: {vagonov(len(items))}, {tons_txt} т.",
             f"В пути дольше {a.v_puti} дней: {len(long_transit)} из {len(tr_all)} вагонов, медиана {med(tr_all):g} дн. (лист «Нарушения»)"
             if tr_all else "Даты прихода и выхода не заполнены, сроки в пути посчитать нельзя.",
             f"Посадка дольше {a.posadka} дней после прихода: {len(long_posadka)} из {len(ps_all)} вагонов, медиана {med(ps_all):g} дн. (лист «Нарушения»)"
             if ps_all else "Даты оформления не заполнены, посадку посчитать нельзя."]
    if early:
        lines.append(f"Оформление раньше выгрузки: {vagonov(len(early))}, строки {rows_txt([x['row'] for x in early])} (лист «Нарушения»)")
    if kg_rows:
        lines.append(f"Тоннаж похож на килограммы в строках {rows_txt(kg_rows)} (всего {len(kg_rows)}), пересчитал в тонны (лист «Ошибки данных»)")
    if no_depart:
        lines.append(f"Нет даты выхода в строках {rows_txt(no_depart)} (всего {len(no_depart)}), в расчёт пути они не вошли")
    for ns in merged.values():
        lines.append(f"Возможно, один завод: {' / '.join(ns)} ({' + '.join(str(cnt_plant[x]) for x in ns)} вагонов); в сводке по заводам считал вместе")
    if stuck or unreg:
        lines.append(f"На {today:%d.%m.%Y}: задерживается в пути {len(stuck)}, не посажено {len(unreg)} (лист «Нарушения»)")
    lines.append(f"Отчёт: {out}")
    print("\n".join(lines[:15]))


if __name__ == "__main__":
    main()
