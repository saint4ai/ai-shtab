#!/usr/bin/env python3
"""Собирает коммерческое предложение (КП) в один HTML-файл из данных в JSON.

Запуск:
    python3 tools/kp.py ДАННЫЕ.json [--vyhod ПУТЬ.html]
Дальше PDF:
    python3 tools/sobrat_pdf.py ПУТЬ.html --knizhnaya

По умолчанию HTML кладётся в презентации/ с именем КП_<клиент>_<дата>.html. Если такой файл уже есть,
к имени добавляется _v2, _v3: прежний вариант не затирается. Явный --vyhod перезаписывает файл.
Шаблоны, стили, шрифты и иконки лежат в шаблоны/кп/, формат данных описан в шаблоны/кп/README.md.

Только стандартная библиотека Python 3.9+. Суммы считает этот скрипт (Decimal, до тенге), агент их не считает.
В HTML всё встроено (шрифты, иконки, логотип), он открывается где угодно без интернета.
"""
import argparse
import base64
import colorsys
import datetime
import html
import json
import pathlib
import re
import sys
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP

ROOT = pathlib.Path(__file__).resolve().parent.parent
TPL = ROOT / "шаблоны" / "кп"
NBSP = "\u00a0"
MONTHS = ["января", "февраля", "марта", "апреля", "мая", "июня", "июля", "августа", "сентября", "октября", "ноября", "декабря"]
LEGAL = {"тоо", "ип", "ао", "оао", "ооо", "зао", "пао", "жшс", "кх", "тд"}
DEFAULT_ACCENT, DEFAULT_ACCENT2 = "#F08A24", "#4F6D8A"
LOGO_TYPES = {".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".svg": "image/svg+xml", ".webp": "image/webp"}
LOGO_MAX = 2 * 1024 * 1024
ICON_BY_WORD = [  # слово в названии группы -> иконка сечения; порядок важен
    ("профтруб", "profil_truba"), ("профильн", "profil_truba"), ("арматур", "armatura"), ("труб", "truba"),
    ("лист", "list"), ("уголок", "ugolok"), ("угол", "ugolok"), ("швеллер", "shveller"), ("двутавр", "dvutavr"),
    ("проволок", "provoloka"), ("бухт", "provoloka"), ("сетк", "setka"), ("круг", "krug"), ("катанк", "krug"),
]
GLYPH_BY_WORD = [("завод", "zavod"), ("станц", "stanciya"), ("вагон", "stanciya"), ("склад", "sklad"),
                 ("объект", "obekt"), ("стройк", "obekt"), ("авто", "gruz"), ("достав", "gruz"), ("машин", "gruz")]

# Геометрия листа в мм для раскладки таблицы по листам. Высоты рядов считаются с запасом: лучше пустое место внизу листа,
# чем обрезанный текст. Значения сверены с отрисовкой в Chromium (см. шаблоны/кп/README.md, раздел «Если правите вид»).
PAGE_H = 296.8
USABLE = PAGE_H - 12 - 16
GAP = 4.2
NO_PT = {"": 24, "m": 19, "s": 15}  # кегль номера КП в шапке по его длине
G = {"slim": 15.5, "ph": 22.0, "tbl_over": 13.5, "note": 5.5, "row": 3.45, "line": 4.0, "min_tail": 3,
     "row_roomy": 5.7, "line_roomy": 4.5, "roomy_over": 3.0, "roomy_max": 8}


class DataError(Exception):
    pass


class H(str):
    """Готовая вёрстка: вставляется в шаблон как есть. Всё остальное экранируется."""


def esc(v):
    return html.escape(str(v), quote=True)


# ---------- шаблоны ----------
_BLOCKS = {}


def blocks(name):
    if name not in _BLOCKS:
        text = (TPL / name).read_text(encoding="utf-8")
        parts = re.split(r"<!--@([\w-]+)-->\n?", text)
        _BLOCKS.update({parts[i]: parts[i + 1].rstrip("\n") for i in range(1, len(parts), 2)})
    return _BLOCKS


def fill(_block, **ctx):
    block = _BLOCKS[_block]

    def sub(m):
        v = ctx[m.group(1)]
        return v if isinstance(v, H) else esc(v)

    return H(re.sub(r"\{\{(\w+)\}\}", sub, block))


# ---------- числа и даты ----------
def to_dec(v, label, errs, what="число", example="342000 или 12,5"):
    if v is None or isinstance(v, bool) or (isinstance(v, str) and not v.strip()):
        errs.append(f"{label}: не указано. Впишите {what}.")
        return None
    try:
        d = Decimal(str(v).replace(NBSP, "").replace(" ", "").replace(",", "."))
    except InvalidOperation:
        errs.append(f"{label}: «{v}» не похоже на число. Впишите {what}, например {example}.")
        return None
    if not d.is_finite() or abs(d) >= Decimal(10) ** 12:
        errs.append(f"{label}: число «{v}» слишком большое или некорректное. Проверьте его.")
        return None
    return d


def money(d):
    n = int(d.quantize(Decimal(1), ROUND_HALF_UP))
    return ("−" if n < 0 else "") + f"{abs(n):,}".replace(",", NBSP)


def num(d, places):
    q = d.quantize(Decimal(1).scaleb(-places), ROUND_HALF_UP)
    ip, _, fp = format(q, f",.{places}f").partition(".")
    fp = fp.rstrip("0")
    return ip.replace(",", NBSP) + ("," + fp if fp else "")


def ru_date(d):
    return f"{d.day} {MONTHS[d.month - 1]} {d.year} г."


def parse_date(v, errs):
    if v in (None, ""):
        return datetime.date.today()
    for fmt in ("%Y-%m-%d", "%d.%m.%Y"):
        try:
            return datetime.datetime.strptime(str(v).strip(), fmt).date()
        except ValueError:
            pass
    errs.append(f"date: «{v}» не распознана. Напишите дату как 2026-11-21 или 21.11.2026.")
    return None


# ---------- цвета бренда ----------
def hex_rgb(h):
    h = h.lstrip("#")
    if len(h) == 3:
        h = "".join(c * 2 for c in h)
    return tuple(int(h[i:i + 2], 16) for i in (0, 2, 4))


def rgb_hex(c):
    return "#%02X%02X%02X" % tuple(max(0, min(255, round(x))) for x in c)


def lum(c):
    def f(x):
        x /= 255
        return x / 12.92 if x <= 0.04045 else ((x + 0.055) / 1.055) ** 2.4
    return 0.2126 * f(c[0]) + 0.7152 * f(c[1]) + 0.0722 * f(c[2])


def contrast(a, b):
    la, lb = sorted((lum(a), lum(b)), reverse=True)
    return (la + 0.05) / (lb + 0.05)


def mix(a, b, t):
    return tuple(a[i] + (b[i] - a[i]) * t for i in range(3))


def lift(c, bg, ratio):
    """Осветляет цвет, пока его контраст с фоном bg не станет не ниже ratio."""
    for k in range(51):
        m = mix(c, (255, 255, 255), k / 50)
        if contrast(m, bg) >= ratio:
            return m
    return (255, 255, 255)


def cap_lum(c, top):
    """Затемняет цвет к графиту, пока яркость не опустится до top."""
    for k in range(51):
        m = mix(c, (20, 28, 38), k / 50)
        if lum(m) <= top:
            return m
    return (20, 28, 38)


def calm(c, top_lum, top_sat):
    """Второй цвет для свечения на тёмном фоне: не ярче top_lum и не насыщеннее top_sat, чтобы фон не грязнился."""
    h, l, s = colorsys.rgb_to_hls(*(x / 255 for x in cap_lum(c, top_lum)))
    return tuple(x * 255 for x in colorsys.hls_to_rgb(h, l, min(s, top_sat)))


def theme_css(accent, accent2, decor_names):
    a, s = hex_rgb(accent), hex_rgb(accent2)
    glass = (58, 76, 98)  # самый светлый фон под текстом: стальной угол листа и стекло над свечением
    glow = lift(cap_lum(a, 0.5), glass, 2.8)
    second = calm(s, 0.2, 0.6)
    on_acc = (16, 12, 6) if contrast(a, (16, 12, 6)) >= contrast(a, (255, 255, 255)) else (255, 255, 255)
    v = {
        "--accent": rgb_hex(a), "--accent-rgb": "%d, %d, %d" % tuple(round(x) for x in glow),
        "--accent-hi": rgb_hex(mix(a, (255, 255, 255), 0.3)), "--accent-text": rgb_hex(lift(a, glass, 5.0)),
        "--accent-big": rgb_hex(lift(a, glass, 3.6)),
        "--accent-glow": rgb_hex(glow), "--on-accent": rgb_hex(on_acc),
        "--accent2-rgb": "%d, %d, %d" % tuple(round(x) for x in second),
    }
    for n in decor_names:
        svg = (TPL / "фон" / f"{n}.svg").read_text(encoding="utf-8").replace("{{accent}}", rgb_hex(glow))
        v[f"--decor-{n}"] = 'url("data:image/svg+xml;base64,%s")' % base64.b64encode(svg.encode("utf-8")).decode()
    return ":root{" + ";".join(f"{k}:{x}" for k, x in v.items()) + "}"


def fonts_css():
    base = TPL / "шрифты"
    css = (base / "fonts.css").read_text(encoding="utf-8")
    return re.sub(r"url\(([\w.-]+\.woff2)\)",
                  lambda m: "url(data:font/woff2;base64,%s)" % base64.b64encode((base / m.group(1)).read_bytes()).decode(), css)


# ---------- проверка данных ----------
def text(v, label, errs, need=False, limit=None):
    if v is None or (isinstance(v, str) and not v.strip()):
        if need:
            errs.append(f"{label}: пусто. Заполните.")
        return ""
    if not isinstance(v, (str, int, float)) or isinstance(v, bool):
        errs.append(f"{label}: ожидается текст.")
        return ""
    s = str(v).strip()
    if limit and len(s) > limit:
        errs.append(f"{label}: {len(s)} знаков, допустимо до {limit}. Сократите.")
    return s


def obj(v, label, errs):
    """Раздел данных должен быть объектом { ... }. Пустое значение считаем пустым объектом."""
    if v in (None, "", [], {}):
        return {}
    if not isinstance(v, dict):
        errs.append(f"{label}: ожидается объект {{ ... }}, а не «{str(v)[:30]}».")
        return {}
    return v


def unknown(v, known, where, warns):
    for key in v:
        if key not in known:
            warns.append(f"поле «{key}» в «{where}» не используется. Нет ли опечатки?")


def logo_data(path, base_dir, errs):
    p = pathlib.Path(str(path)).expanduser()
    for cand in ([p] if p.is_absolute() else [base_dir / p, ROOT / p]):
        if cand.is_file():
            p = cand
            break
    else:
        errs.append(f"brand.logo: файл «{path}» не найден. Положите логотип в знания/бренд/ и укажите путь к нему.")
        return None
    if p.suffix.lower() not in LOGO_TYPES:
        errs.append(f"brand.logo: формат «{p.suffix}» не подходит. Нужен PNG, SVG, JPG или WebP.")
        return None
    raw = p.read_bytes()
    if len(raw) > LOGO_MAX:
        errs.append(f"brand.logo: файл {len(raw) // 1024} КБ, допустимо до {LOGO_MAX // 1024}. Уменьшите логотип.")
        return None
    return "data:%s;base64,%s" % (LOGO_TYPES[p.suffix.lower()], base64.b64encode(raw).decode())


def parse(data, base_dir):
    """Проверяет данные и считает суммы. Все найденные проблемы собирает в одну ошибку."""
    errs, warns = [], []
    if not isinstance(data, dict):
        raise DataError(["Данные должны быть объектом JSON: { ... }."])
    unknown(data, ("template", "brand", "company", "client", "number", "date", "subject", "intro", "items", "vat", "terms", "manager",
                   "about", "assortment", "logistics", "why"), "корень файла", warns)
    d = {"template": data.get("template"), "warnings": warns}
    if d["template"] not in ("postavka", "prezentaciya"):
        errs.append('template: укажите "postavka" (КП на поставку) или "prezentaciya" (КП-презентация).')

    brand = obj(data.get("brand"), "brand", errs)
    unknown(brand, ("accent", "accent2", "logo"), "brand", warns)
    d["accent"], d["accent2"] = DEFAULT_ACCENT, DEFAULT_ACCENT2
    for key, field in (("accent", "accent"), ("accent2", "accent2")):
        if brand.get(key):
            if not re.fullmatch(r"#(?:[0-9a-fA-F]{3}|[0-9a-fA-F]{6})", str(brand[key])):
                errs.append(f"brand.{key}: «{brand[key]}» не цвет. Нужен вид #F08A24.")
            else:
                d[field] = str(brand[key])
    d["logo"] = logo_data(brand["logo"], base_dir, errs) if brand.get("logo") else None

    c = obj(data.get("company"), "company", errs)
    unknown(c, ("name", "tagline", "address", "phone", "email", "site", "branches"), "company", warns)
    d["company"] = {k: text(c.get(k), f"company.{k}", errs, need=(k == "name"), limit=lim)
                    for k, lim in (("name", 80), ("tagline", 90), ("address", 110), ("phone", 40), ("email", 60), ("site", 60))}
    br = c.get("branches") or []
    if not isinstance(br, list) or len(", ".join(str(b) for b in br)) > 260:
        errs.append("company.branches: нужен список городов, вместе не больше 260 знаков.")
        br = []
    d["company"]["branches"] = [text(b, "company.branches", errs) for b in br]

    cl = data.get("client")
    cl = {"name": cl} if isinstance(cl, str) else obj(cl, "client", errs)
    unknown(cl, ("name", "contact"), "client", warns)
    d["client"] = {"name": text(cl.get("name"), "client.name", errs, need=True, limit=90),
                   "contact": text(cl.get("contact"), "client.contact", errs, limit=80)}
    d["number"] = text(data.get("number"), "number", errs, limit=24)
    d["date"] = parse_date(data.get("date"), errs)
    d["intro"] = text(data.get("intro"), "intro", errs, limit=190)
    d["subject"] = text(data.get("subject"), "subject", errs, limit=140)

    vat = obj(data.get("vat"), "vat", errs)
    unknown(vat, ("rate", "included", "note"), "vat", warns)
    d["vat"] = None
    if vat:
        rate = None  # ставки нет: считать НДС не из чего, остаётся только пояснение под таблицей
        if vat.get("rate") not in (None, ""):
            rate = to_dec(vat.get("rate"), "vat.rate", errs, "ставку НДС в процентах", "12 или 16")
        if rate is not None and not (0 <= rate <= 100):
            errs.append("vat.rate: ставка должна быть от 0 до 100.")
        inc = vat.get("included")
        if rate is not None and not isinstance(inc, bool):
            errs.append("vat.included: при заданной ставке впишите true (цены уже с НДС) или false (НДС добавляется к сумме). Без слов в кавычках.")
        d["vat"] = {"rate": rate, "included": inc is True, "note": text(vat.get("note"), "vat.note", errs, limit=120)}

    items = data.get("items")
    d["items"] = []
    if not isinstance(items, list) or not items:
        errs.append('items: нет позиций. Добавьте хотя бы одну в "items".')
        items = []
    for i, it in enumerate(items, 1):
        lab = f"Позиция {i}"
        if not isinstance(it, dict):
            errs.append(f"{lab}: ожидается объект с полями name, unit, qty, price.")
            continue
        unknown(it, ("name", "size", "unit", "qty", "price", "price_kg"), lab, warns)
        name = text(it.get("name"), f"{lab}, наименование", errs, need=True, limit=200)
        unit = text(it.get("unit"), f"{lab}, единица (т, м, лист, шт)", errs, need=True, limit=5)
        size = text(it.get("size"), f"{lab}, размер или марка", errs, limit=60)
        qty = to_dec(it.get("qty"), f"{lab}, количество", errs, "количество", "20 или 12,5")
        if it.get("price_kg") not in (None, "") and it.get("price") not in (None, ""):
            errs.append(f"{lab}: заданы и price, и price_kg. Оставьте одно поле.")
            price, label_price = None, lab
        elif it.get("price_kg") not in (None, ""):
            kg = to_dec(it.get("price_kg"), f"{lab}, цена за кг", errs, "цену за кг")
            if unit != "т":
                errs.append(f'{lab}: цена за кг подходит, только если единица "т". Впишите цену за единицу в поле price.')
            price = kg * 1000 if kg is not None else None
            label_price = f"{lab}, цена за кг"
        else:
            price = to_dec(it.get("price"), f"{lab}, цена", errs, "цену за единицу")
            label_price = f"{lab}, цена"
        if qty is not None and qty <= 0:
            errs.append(f"{lab}: количество {qty} не подходит. Оно должно быть больше нуля.")
        if price is not None and price <= 0:
            errs.append(f"{label_price}: {price} не подходит. Цена должна быть больше нуля, без цены позицию не добавляют: уточните её.")
        if qty is not None and price is not None and qty > 0 and price > 0:
            qty, price = qty.quantize(Decimal("0.001"), ROUND_HALF_UP), price.quantize(Decimal("0.01"), ROUND_HALF_UP)  # как на листе
            total_row = (qty * price).quantize(Decimal(1), ROUND_HALF_UP)
            if qty == 0 or price == 0 or total_row == 0:
                errs.append(f"{lab}: после округления количество, цена или сумма равны нулю. Проверьте цифры.")
            elif not (digits_fit(num(qty, 3), 20.6) and digits_fit(num(price, 2), 21.4) and digits_fit(money(total_row), 28.6, 0.7)):
                errs.append(f"{lab}: число не помещается в колонку таблицы (слишком много знаков). Проверьте цифры или разбейте позицию.")
            else:
                d["items"].append({"name": name, "size": size, "unit": unit, "qty": qty, "price": price, "sum": total_row})

    ts = data.get("terms") or []
    d["terms"] = []
    if not isinstance(ts, list) or len(ts) > 8:
        errs.append("terms: нужен список условий, не больше 8.")
        ts = []
    for i, t in enumerate(ts, 1):
        t = t if isinstance(t, dict) else {}
        d["terms"].append({"label": text(t.get("label"), f"Условие {i}, название", errs, need=True, limit=28),
                           "text": text(t.get("text"), f"Условие {i}, текст", errs, need=True, limit=150)})

    m = obj(data.get("manager"), "manager", errs)
    unknown(m, ("name", "position", "phone", "email"), "manager", warns)
    d["manager"] = None
    if m:
        d["manager"] = {k: text(m.get(k), f"manager.{k}", errs, need=(k == "name"), limit=60) for k in ("name", "position", "phone", "email")}

    if d["template"] == "prezentaciya":
        parse_presentation(data, d, errs)
    if errs:
        raise DataError(errs)
    # итоги считает скрипт
    total = sum((i["sum"] for i in d["items"]), Decimal(0))
    if total >= Decimal(10) ** 11:
        raise DataError(["Итог больше 100 млрд ₸: так не бывает в одном КП. Проверьте цены и количества."])
    d["total"] = total
    d["vat_sum"], d["payable"] = None, total
    if d["vat"] and d["vat"]["rate"] is not None:
        r = d["vat"]["rate"]
        if d["vat"]["included"]:
            d["vat_sum"] = (total * r / (100 + r)).quantize(Decimal(1), ROUND_HALF_UP)
        else:
            d["vat_sum"] = (total * r / 100).quantize(Decimal(1), ROUND_HALF_UP)
            d["payable"] = total + d["vat_sum"]
    return d


def parse_presentation(data, d, errs):
    warns = d["warnings"]
    a = obj(data.get("about"), "about", errs)
    unknown(a, ("text", "facts"), "about", warns)
    facts = a.get("facts") or []
    if not isinstance(facts, list) or len(facts) > 4:
        errs.append("about.facts: фактов-цифр может быть не больше 4. Оставьте самые сильные.")
        facts = []
    d["about"] = {"text": text(a.get("text"), "about.text", errs, limit=520),
                  "facts": [{"value": text(f.get("value"), f"Факт {i}, цифра", errs, need=True, limit=14),
                             "label": text(f.get("label"), f"Факт {i}, подпись", errs, need=True, limit=60)}
                            for i, f in enumerate(facts, 1) if isinstance(f, dict)]}
    groups = data.get("assortment") or []
    if not isinstance(groups, list) or len(groups) > 8:
        errs.append("assortment: групп ассортимента может быть не больше 8. Объедините близкие.")
        groups = []
    d["assortment"] = []
    for i, g in enumerate(groups, 1):
        g = g if isinstance(g, dict) else {}
        title = text(g.get("title"), f"Группа {i}, название", errs, need=True, limit=24)
        its = g.get("items") or []
        top = 3 if len(groups) > 6 else 6
        if not isinstance(its, list) or len(its) > top:
            errs.append(f"Группа {i}: в списке может быть не больше {top} строк" + (" (групп больше шести, плитки тесные)." if top == 3 else "."))
            its = []
        icon = g.get("icon") or next((v for k, v in ICON_BY_WORD if k in title.lower()), "krug")
        d["assortment"].append({"title": title, "icon": check_icon(icon, f"Группа {i}", errs),
                                "items": [text(x, f"Группа {i}, строка списка", errs, limit=36) for x in its]})
    lg = obj(data.get("logistics"), "logistics", errs)
    unknown(lg, ("steps", "note"), "logistics", warns)
    steps = lg.get("steps") or []
    if not isinstance(steps, list) or len(steps) > 6:
        errs.append("logistics.steps: шагов может быть не больше 6.")
        steps = []
    d["logistics"] = {"note": text(lg.get("note"), "logistics.note", errs, limit=220), "steps": []}
    for i, s in enumerate(steps, 1):
        s = s if isinstance(s, dict) else {}
        title = text(s.get("title"), f"Шаг {i}, название", errs, need=True, limit=24)
        glyph = s.get("icon") or next((v for k, v in GLYPH_BY_WORD if k in title.lower()), None)
        d["logistics"]["steps"].append({"title": title, "text": text(s.get("text"), f"Шаг {i}, пояснение", errs, limit=90),
                                        "term": text(s.get("term"), f"Шаг {i}, срок", errs, need=True, limit=16),
                                        "icon": check_icon(glyph, f"Шаг {i}", errs) if glyph else None})
    why = data.get("why") or []
    if not isinstance(why, list) or len(why) > 4:
        errs.append('why: пунктов "почему мы" может быть не больше 4.')
        why = []
    d["why"] = [{"title": text(w.get("title"), f"Пункт {i}, заголовок", errs, need=True, limit=34),
                 "text": text(w.get("text"), f"Пункт {i}, пояснение", errs, limit=80)}
                for i, w in enumerate(why, 1) if isinstance(w, dict)]


def check_icon(name, label, errs):
    if not re.fullmatch(r"[a-z_]+", str(name)) or not (TPL / "иконки" / f"{name}.svg").is_file():
        have = ", ".join(sorted(p.stem for p in (TPL / "иконки").glob("*.svg")))
        errs.append(f"{label}: иконки «{name}» нет. Доступны: {have}.")
        return None
    return name


def icon_svg(name):
    return H((TPL / "иконки" / f"{name}.svg").read_text(encoding="utf-8").strip())


# ---------- оценка высоты текста для раскладки по листам ----------
def _cw(c):
    if c == " ":
        return 0.27
    if c.isdigit():
        return 0.6
    if c.isupper():
        return 0.74
    if c in ".,;:!|'\"()":
        return 0.3
    if c in "-–−/":
        return 0.4
    return 0.6


def lines_for(s, width_mm, pt):
    """Сколько строк займёт текст в колонке. Считает с запасом, переносы только по пробелам."""
    em = pt * 0.3528
    cap = width_mm / em
    n, cur = 1, 0.0
    for w in s.split():
        ww = sum(_cw(c) for c in w)
        if ww > cap:
            n += int((cur + ww) // cap) if cur else int(ww // cap)
            cur = (cur + ww) % cap
        elif cur and cur + 0.27 + ww > cap:
            n, cur = n + 1, ww
        else:
            cur += (0.27 if cur else 0) + ww
    return n


def digits_fit(s, inner_mm, digit=0.68):
    """Число из таблицы помещается в колонку в одну строку. Меряем по самому крупному шрифту таблицы (9,8 pt)."""
    return sum(digit if c.isdigit() else 0.3 for c in s) * 9.8 * 0.3528 <= inner_mm


def row_height(it, roomy=False):
    n = max(lines_for(it["name"], 49.0, 9.8 if roomy else 8.8), lines_for(it["size"], 24.0, 9.0 if roomy else 8.3))
    return (G["row_roomy"] + n * G["line_roomy"]) if roomy else (G["row"] + n * G["line"])


def paginate(heights, first_cap, cont_cap, tail):
    """Раскладывает строки по листам. Итоги (tail) всегда на последнем листе: если не помещаются рядом со строками,
    последний лист берёт до min_tail строк с конца, а если и так тесно, итоги идут на отдельный лист без строк."""
    pages, used = [[]], [0.0]
    for i, h in enumerate(heights):
        cap = first_cap if len(pages) == 1 else cont_cap
        if pages[-1] and used[-1] + h > cap:
            pages.append([])
            used.append(0.0)
        pages[-1].append(i)
        used[-1] += h
    cap = first_cap if len(pages) == 1 else cont_cap
    if used[-1] + tail <= cap:
        return pages
    rows, k, h = pages[-1], 0, 0.0
    while k < min(G["min_tail"], len(rows)) and h + heights[rows[-1 - k]] + tail <= cont_cap:
        h += heights[rows[-1 - k]]
        k += 1
    if len(rows) - k < (2 if len(pages) == 1 else 1):  # на листе с шапкой после переноса остаются минимум 2 строки
        k = 0
    pages[-1] = rows[:len(rows) - k]
    pages.append(rows[len(rows) - k:] if k else [])
    return pages


# ---------- сборка листов ----------
def initials(name, caps_inside=False):
    words = [w for w in re.findall(r"[^\W_]+", name) if w.lower() not in LEGAL]
    if not words:
        return "?"
    if len(words) == 1 and caps_inside:
        caps = re.findall(r"[A-ZА-ЯЁӘҒҚҢӨҰҮҺІ]", words[0])
        if len(caps) >= 2:
            return "".join(caps[:2])
    return "".join(w[0].upper() for w in words[:2])


def link_tel(p):
    return H(f'<a href="tel:{esc(re.sub(r"[^0-9+]", "", p))}">{esc(p)}</a>')


def link_mail(e):
    return H(f'<a href="mailto:{esc(e)}">{esc(e)}</a>')


def link_web(u):
    return H(f'<a href="{esc(u if re.match(r"https?://", u) else "https://" + u)}">{esc(u)}</a>')


def lines_html(*parts):
    return H("".join(f"<div>{p if isinstance(p, H) else esc(p)}</div>" for p in parts if p))


class Doc:
    def __init__(self, d):
        self.d = d
        self.co = d["company"]
        self.pages = []  # (css-класс, тело)
        self.date_ru = ru_date(d["date"])
        self.number = f"№{NBSP}{d['number']}" if d["number"] else ""

    def no_cls(self):
        n = len(self.number)
        return "" if n <= 11 else ("m" if n <= 16 else "s")

    def mark(self, size=""):
        if self.d["logo"]:
            return fill("logo", src=self.d["logo"], alt=self.co["name"], size=size)
        return fill("mark", ini=initials(self.co["name"], True), size=size)

    def brand(self, size=""):
        return fill("brand", mark=self.mark(size), name=self.co["name"], tagline=self.co["tagline"] if not size else "")

    def hero_height(self):
        """Высота шапки листа 1: переносы имён, подписи и вступления меняют её на 10-20 мм."""
        co, cl, d = self.co, self.d["client"], self.d
        doc_w = max(44.7, len(self.number) * 0.62 * NO_PT[self.no_cls()] * 0.3528 + 2)
        plate = 44.0 if d["logo"] else 14.0
        name_w = 168 - doc_w - 6 - plate - 4.2
        brand = max(14.0, lines_for(co["name"], name_w, 14.5) * 5.9 + (1.2 + lines_for(co["tagline"], name_w, 8.4) * 4.2 if co["tagline"] else 0))
        top = max(brand, 18.8)
        left = 2.8 + 1.4 + lines_for(cl["name"], 82.0, 11) * 4.7 + (1 + lines_for(cl["contact"], 82.0, 8.6) * 4.3 if cl["contact"] else 0)
        right = 3.4 + lines_for(d["intro"], 78.0, 9.4) * 4.8 if d["intro"] else 0
        return 13.5 + top + 9.9 + max(left, right) + 0.6

    def contacts_line(self):
        return " · ".join(x for x in (self.co["address"], self.co["phone"], self.co["site"]) if x)

    def add(self, cls, body):
        self.pages.append((cls, "\n".join(body)))

    def table(self, rows, start, note="", roomy=False):
        out = []
        for k, it in enumerate(rows, start):
            out.append(fill("row", n=k, name=it["name"], size=it["size"], unit=it["unit"], qty=num(it["qty"], 3),
                            price=num(it["price"], 2), sum=money(it["sum"])))
        return fill("table", rows=H("\n".join(out)), note=fill("note", text=note) if note else H(""), cls="roomy" if roomy else "")

    def totals(self):
        d = self.d
        lines = []
        if d["vat_sum"] is not None:
            rate = num(d["vat"]["rate"], 2)
            lines.append(fill("totline", label="Сумма", value=money(d["total"]) + NBSP + "₸"))
            lab = f"{'В том числе НДС' if d['vat']['included'] else 'НДС'} {rate}%"
            lines.append(fill("totline", label=lab, value=money(d["vat_sum"]) + NBSP + "₸"))
        pay = money(d["payable"]) + NBSP + "₸"
        return fill("totals", lines=H("".join(lines)), total=pay, scls="s" if len(pay) > 13 else "")

    def terms(self):
        return fill("terms", items=H("\n".join(fill("term", label=t["label"], text=t["text"]) for t in self.d["terms"])))

    def manager(self):
        m = self.d["manager"]
        if not m:
            return H("")
        contacts = lines_html(link_tel(m["phone"]) if m["phone"] else "", link_mail(m["email"]) if m["email"] else "")
        return fill("manager", ini=initials(m["name"]), name=m["name"], position=m["position"], contacts=contacts)

    def terms_height(self, width=63.0):
        return 5 + 4.6 + 3 + 3 + sum(max(lines_for(t["text"], width, 8.9) * 4.4, 4.4) + 2.5 for t in self.d["terms"])

    def why_height(self):
        """Плитки «почему мы» по двое в ряд, высота ряда по самой высокой плитке."""
        w, n = self.d["why"], len(self.d["why"])
        per = 1 if n <= 2 else 2
        tile = [10.8 + 8.5 + 3 + lines_for(x["title"], 64.0, 12) * 5.2 + 1.4 + (lines_for(x["text"], 64.0, 9.4) * 4.8 if x["text"] else 0) for x in w]
        rows = [max(tile[i:i + per]) for i in range(0, n, per)]
        return 5 + sum(rows) + GAP * len(rows)

    def contacts_height(self):
        co, m = self.co, self.d["manager"] or {}
        left = 2.8 + 1.4 + 5.2 + 4.4 + 3 + 5.3 * ((1 if m.get("phone") else 0) + (1 if m.get("email") else 0))
        right = 2.8 + 1.4 + 5.2 + 3 + 5.3 * (lines_for(co["address"], 80.0, 9.6) + sum(1 for x in (co["phone"], co["email"], co["site"]) if x))
        br = 3.6 + 2.8 + 1.4 + lines_for(", ".join(co["branches"]), 160.0, 8.4) * 4.3 if co["branches"] else 0
        return 12 + max(left, right) + br

    def totals_height(self):
        return 10 + 2.4 + 3 + 2.8 + 1.6 + 8 + (2 * 7.2 if self.d["vat_sum"] is not None else 0)

    def tail_height(self, terms=True, mgr=True, note=False):
        d = self.d
        t_h = self.terms_height() if terms and d["terms"] else 0.0
        bottom = max(t_h, self.totals_height())
        return GAP + bottom + (GAP + 21.0 if mgr and d["manager"] else 0) + (G["note"] if note else 0)

    def bottom_block(self, with_terms=True):
        if with_terms and self.d["terms"]:
            cls = " start" if self.terms_height() - self.totals_height() > 18 else ""
            return H(f'<div class="bottom{cls}">{self.terms()}{self.totals()}</div>')
        return H(f'<div class="bottom one">{self.totals()}</div>')

    def roomy(self, limit):
        """Строк мало: набираем таблицу просторнее, чтобы лист не выглядел пустым."""
        return len(self.d["items"]) <= limit

    def paginate_items(self, first_cap, cont_cap, tail, roomy=False):
        hs = [row_height(i, roomy) for i in self.d["items"]]
        over = G["tbl_over"] + (G["roomy_over"] if roomy else 0)
        return paginate(hs, first_cap - over, cont_cap - over, tail)

    def note(self):
        v = self.d["vat"]
        return v["note"] if v and v["note"] else ""


def build_postavka(doc):
    d = doc.d
    note = doc.note()
    tail = doc.tail_height(note=bool(note))
    roomy = doc.roomy(G["roomy_max"])
    pages = doc.paginate_items(USABLE - doc.hero_height() - GAP, USABLE - G["slim"] - GAP, tail, roomy)
    n = 0
    for pi, rows in enumerate(pages):
        last = pi == len(pages) - 1
        items = [d["items"][i] for i in rows]
        body = []
        if pi == 0:
            body.append(fill("hero", brand=doc.brand(), number=doc.number, ncls=doc.no_cls(), date=doc.date_ru, client=d["client"]["name"],
                             contact=d["client"]["contact"], intro=d["intro"]))
        else:
            body.append(fill("slim", brand=doc.brand("sm"), number=doc.number or "КП", date=doc.date_ru, client=d["client"]["name"]))
        if items or pi == 0:
            body.append(doc.table(items, n + 1, note if last else "", roomy))
        elif note:
            body.append(fill("note", text=note))
        n += len(items)
        if last:
            body.append(doc.bottom_block())
            body.append(H(f'<div class="pin">{doc.manager()}</div>') if d["manager"] else H(""))
        doc.add("dc-" + "abc"[pi % 3], body)


def fact_size(value, first):
    n = len(value)
    return "xl" if first and n <= 3 else ("m" if n > 7 else "")


def page_head(no, title, side=""):
    return fill("ph", no=f"{no:02d}", title=title, side=side)


def build_prezentaciya(doc):
    d = doc.d
    d_icons = [g["icon"] for g in d["assortment"]] or ["armatura", "truba", "dvutavr", "shveller", "ugolok", "setka"]
    strip = H("".join(icon_svg(n) for n in d_icons[:6]))
    doc.add("cov dc-cover", [fill("cover", mark=doc.mark(), name=doc.co["name"], client=d["client"]["name"], subject=d["subject"],
                                  number=doc.number or "", date=doc.date_ru, icons=strip)])
    no = 1

    def nxt():
        nonlocal no
        no += 1
        return no

    a = d["about"]
    if a["text"] or a["facts"]:
        body = [page_head(nxt(), "О компании")]
        if a["text"]:
            body.append(fill("about", text=a["text"]))
        if a["facts"]:
            facts = "\n".join(fill("fact", i=i, value=f["value"], label=f["label"], size=fact_size(f["value"], i == 1),
                                    icon=H(f'<div class="fact-ic">{icon_svg(d_icons[0])}</div>') if i == 1 else H(""))
                              for i, f in enumerate(a["facts"], 1))
            body.append(H(f'<div class="facts n{len(a["facts"])}">{facts}</div>'))
        doc.add("dc-b", body)
    if d["assortment"]:
        gs = "\n".join(fill("group", icon=icon_svg(g["icon"]), title=g["title"],
                            li=H("".join(f"<li>{esc(x)}</li>" for x in g["items"]))) for g in d["assortment"])
        doc.add("dc-c", [page_head(nxt(), "Ассортимент"), fill("groups", items=H(gs))])
    # цены: таблица как в КП на поставку, листов может быть несколько
    note = doc.note()
    tail = doc.tail_height(terms=False, mgr=False, note=bool(note))
    roomy = doc.roomy(10)
    pages = doc.paginate_items(USABLE - G["ph"] - GAP, USABLE - G["ph"] - GAP, tail, roomy)
    n = 0
    for pi, rows in enumerate(pages):
        last = pi == len(pages) - 1
        items = [d["items"][i] for i in rows]
        body = [page_head(nxt(), "Цены" if pi == 0 else "Цены, продолжение", f"на {doc.date_ru}" if pi == 0 else "")]
        if items:
            body.append(doc.table(items, n + 1, note if last else "", roomy))
        elif note:
            body.append(fill("note", text=note))
        n += len(items)
        if last:
            body.append(doc.bottom_block(with_terms=False))
        doc.add("dc-" + "bc"[pi % 2], body)
    st = d["logistics"]["steps"]
    if st:
        parts = []
        for i, s in enumerate(st, 1):
            glyph = icon_svg(s["icon"]) if s["icon"] else fill("stepnum", n=i)
            parts.append(fill("step", glyph=glyph, title=s["title"], text=s["text"], term=s["term"]))
        flow = ('<div class="link"></div>').join(parts)
        body = [page_head(nxt(), "Логистика"), H(f'<div class="steps">{flow}</div>')]
        if d["logistics"]["note"]:
            body.append(fill("lnote", text=d["logistics"]["note"]))
        doc.add("dc-c", body)
    # условия, почему мы, контакты
    title = {(True, True): "Условия и почему мы", (True, False): "Условия", (False, True): "Почему мы"}.get(
        (bool(d["terms"]), bool(d["why"])), "Контакты")
    body = [page_head(nxt(), title)]
    if d["terms"]:
        body.append(doc.terms())
    if d["why"]:
        items = "\n".join(fill("whyitem", n=f"{i:02d}", title=w["title"], text=w["text"]) for i, w in enumerate(d["why"], 1))
        body.append(fill("why", count=len(d["why"]), items=H(items)))
    body_h = G["ph"] + (GAP + doc.terms_height(133.0) if d["terms"] else 0) + (GAP + doc.why_height() if d["why"] else 0)
    if body_h > USABLE:
        raise DataError(["Условия и пункты «почему мы» не помещаются на один лист. Сократите тексты или уберите лишние пункты."])
    if body_h + GAP + doc.contacts_height() > USABLE:  # контакты уходят на свой лист
        doc.add("dc-b", body)
        body = [page_head(nxt(), "Контакты")]
    m = d["manager"] or {"name": "", "position": "", "phone": "", "email": ""}
    co = doc.co
    mlines = lines_html(link_tel(m["phone"]) if m["phone"] else "", link_mail(m["email"]) if m["email"] else "")
    clines = lines_html(co["address"], link_tel(co["phone"]) if co["phone"] else "", link_mail(co["email"]) if co["email"] else "",
                        link_web(co["site"]) if co["site"] else "")
    cities = H(", ".join(f'<span style="white-space:nowrap">{esc(x)}</span>' for x in co["branches"]))  # город не рвём по дефису
    br = fill("branches", text=cities) if co["branches"] else H("")
    body.append(H(f'<div class="pin">{fill("contacts", name=m["name"], position=m["position"], lines=mlines, company=co["name"], clines=clines, branches=br)}</div>'))
    doc.add("dc-b", body)


# ---------- вывод ----------
def slug(name):
    words = [w for w in re.split(r"\s+", re.sub(r"[«»\"']", "", name)) if w.lower() not in LEGAL]
    s = re.sub(r"[^\w-]+", "-", "-".join(words), flags=re.U).strip("-")
    return (s[:40].strip("-")) or "клиент"


def build(d):
    blocks("общее.html")
    blocks(d["template"] + ".html")
    doc = Doc(d)
    (build_postavka if d["template"] == "postavka" else build_prezentaciya)(doc)
    total = len(doc.pages)
    foot_left = doc.contacts_line() or doc.co["name"]
    out = []
    for i, (cls, body) in enumerate(doc.pages, 1):
        foot = fill("foot", left=foot_left, right=f"Лист {i} из {total}" if total > 1 else "")
        out.append(fill("page", cls=cls, body=H(body), foot=foot))
    used = sorted({c.split("-")[1] for cls, _ in doc.pages for c in cls.split() if c.startswith("dc-")})
    decor = {"a": "list_a", "b": "list_b", "c": "list_c", "cover": "oblozhka"}
    brand = theme_css(d["accent"], d["accent2"], [decor[u] for u in used])
    title = f"КП {d['number']} для {d['client']['name']}" if d["number"] else f"КП для {d['client']['name']}"
    page = fill("doc", title=title, fonts=H(fonts_css()), css=H((TPL / "стили.css").read_text(encoding="utf-8")),
                brand=H(brand), pages=H("\n".join(out)))
    return page, total


def main():
    ap = argparse.ArgumentParser(description="Собирает КП в один HTML-файл из данных JSON.")
    ap.add_argument("dannye", help="файл с данными КП (JSON)")
    ap.add_argument("--vyhod", help="куда положить HTML (по умолчанию презентации/КП_<клиент>_<дата>.html)")
    args = ap.parse_args()
    src = pathlib.Path(args.dannye).expanduser().resolve()
    try:
        data = json.loads(src.read_text(encoding="utf-8-sig"))
    except FileNotFoundError:
        sys.exit(f"Ошибка: файл данных не найден: {src}")
    except json.JSONDecodeError as e:
        sys.exit(f"Ошибка: в файле данных нарушен JSON, строка {e.lineno}, позиция {e.colno}: {e.msg}. Проверьте запятые и кавычки.")
    try:
        d = parse(data, src.parent)
        page, total = build(d)
    except DataError as e:
        sys.exit("Не собрано. Что поправить в данных:\n" + "\n".join(f" - {m}" for m in e.args[0]))
    for w in d["warnings"]:
        print("Внимание: " + w, file=sys.stderr)
    if args.vyhod:
        out = pathlib.Path(args.vyhod).expanduser().resolve()
    else:
        out = ROOT / "презентации" / f"КП_{slug(d['client']['name'])}_{d['date'].isoformat()}.html"
        k = 2
        while out.exists():
            out = out.with_name(f"КП_{slug(d['client']['name'])}_{d['date'].isoformat()}_v{k}.html")
            k += 1
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(page, encoding="utf-8")
    print(f"Готово: {out}")
    print(f"Листов: {total}, позиций: {len(d['items'])}")
    print(f"Сумма: {money(d['total'])} ₸" + (f", НДС: {money(d['vat_sum'])} ₸" if d["vat_sum"] is not None else ""))
    print(f"Итого к оплате: {money(d['payable'])} ₸")
    print(f"Дальше PDF: python3 tools/sobrat_pdf.py \"{out}\" --knizhnaya")


if __name__ == "__main__":
    main()
