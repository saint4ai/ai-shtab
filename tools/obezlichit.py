#!/usr/bin/env python3
"""Обезличивание таблиц и текстов на этом компьютере.

ИИН и БИН, телефоны, почты и значения из выбранных колонок заменяются метками вида «ИИН-001».
Таблица соответствий хранится в .private/соответствия.json и никуда не отправляется.

  python3 tools/obezlichit.py mask ФАЙЛ [--columns "Клиент,ФИО"]
  python3 tools/obezlichit.py restore ФАЙЛ

Поддерживаются .xlsx (нужен openpyxl), .csv, .txt, .md.
"""
import argparse
import csv
import io
import json
import pathlib
import re
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent
PRIV = ROOT / ".private"
MAP = PRIV / "соответствия.json"

PATTERNS = [
    ("ИИН", re.compile(r"(?<!\d)\d{12}(?!\d)")),
    ("Телефон", re.compile(r"(?<![\d+])(?:\+7|8)[\s\-()]*\d{3}[\s\-()]*\d{3}[\s\-]*\d{2}[\s\-]*\d{2}(?!\d)")),
    ("Почта", re.compile(r"[\w.+-]+@[\w-]+(?:\.[\w-]+)+")),
]
LABEL = re.compile(r"\b([A-Za-zА-Яа-яЁё]+)-(\d{3})\b")


def load_map():
    if MAP.exists():
        return json.loads(MAP.read_text(encoding="utf-8"))
    return {"forward": {}, "back": {}, "counters": {}}


def save_map(m):
    PRIV.mkdir(exist_ok=True)
    MAP.write_text(json.dumps(m, ensure_ascii=False, indent=1), encoding="utf-8")


class Masker:
    def __init__(self, m, columns):
        self.m, self.columns, self.count = m, columns, {}

    def label(self, kind, value):
        key = f"{kind}:{value}"
        lab = self.m["forward"].get(key)
        if lab is None:
            n = self.m["counters"].get(kind, 0) + 1
            self.m["counters"][kind] = n
            lab = f"{kind}-{n:03d}"
            self.m["forward"][key], self.m["back"][lab] = lab, value
        self.count[kind] = self.count.get(kind, 0) + 1
        return lab

    def text(self, s):
        for kind, rx in PATTERNS:
            s = rx.sub(lambda mt, k=kind: self.label(k, mt.group(0)), s)
        return s

    def cell(self, v, header=None):
        if v is None or v == "":
            return v
        if header and header in self.columns:
            return self.label(re.sub(r"[^A-Za-zА-Яа-яЁё]", "", header) or "Значение", str(v).strip())
        if isinstance(v, str):
            return v if v.startswith("=") else self.text(v)
        if isinstance(v, int) and not isinstance(v, bool):
            s = str(v)
            for kind, rx in PATTERNS[:2]:
                if rx.fullmatch(s):
                    return self.label(kind, s)
        return v


def read_text(p):
    raw = p.read_bytes()
    for enc in ("utf-8-sig", "cp1251"):
        try:
            return raw.decode(enc)
        except UnicodeDecodeError:
            continue
    sys.exit(f"Не удалось прочитать {p}: неизвестная кодировка")


def need_openpyxl():
    try:
        import openpyxl  # noqa: F401
        return openpyxl
    except ImportError:
        sys.exit("Для .xlsx нужен openpyxl. Установите: python3 -m pip install --user openpyxl")


def mask(path, columns):
    m = load_map()
    mk = Masker(m, columns)
    out = path.with_name(f"{path.stem}.обезл{path.suffix}")
    suf = path.suffix.lower()
    if suf == ".xlsx":
        openpyxl = need_openpyxl()
        wb = openpyxl.load_workbook(path)
        for ws in wb.worksheets:
            headers = {}
            for r, row in enumerate(ws.iter_rows()):
                for c in row:
                    if r == 0:
                        headers[c.column] = str(c.value).strip() if c.value is not None else None
                        continue
                    c.value = mk.cell(c.value, headers.get(c.column))
        wb.save(out)
    elif suf == ".csv":
        text = read_text(path)
        try:
            dialect = csv.Sniffer().sniff(text[:4096], delimiters=";,\t")
        except csv.Error:
            dialect = csv.excel
            dialect.delimiter = ";"
        rows = list(csv.reader(io.StringIO(text), dialect))
        head = [h.strip() for h in rows[0]] if rows else []
        body = [[mk.cell(v, head[i] if i < len(head) else None) for i, v in enumerate(r)] for r in rows[1:]]
        with out.open("w", encoding="utf-8-sig", newline="") as f:
            csv.writer(f, dialect).writerows([rows[0]] + body if rows else [])
    elif suf in (".txt", ".md"):
        out.write_text(mk.text(read_text(path)), encoding="utf-8")
    else:
        sys.exit(f"Формат {suf} не поддерживается: .xlsx, .csv, .txt, .md")
    save_map(m)
    stat = ", ".join(f"{k} {v}" for k, v in sorted(mk.count.items())) or "персональных данных не найдено"
    print(f"Готово: {out}\nЗаменено: {stat}")


def restore(path):
    m = load_map()
    back = m["back"]
    swap = lambda s: LABEL.sub(lambda mt: back.get(mt.group(0), mt.group(0)), s)
    dest = PRIV / "восстановлено"
    dest.mkdir(parents=True, exist_ok=True)
    out = dest / path.name
    if path.suffix.lower() == ".xlsx":
        openpyxl = need_openpyxl()
        wb = openpyxl.load_workbook(path)
        for ws in wb.worksheets:
            for row in ws.iter_rows():
                for c in row:
                    if isinstance(c.value, str):
                        c.value = swap(c.value)
        wb.save(out)
    else:
        out.write_text(swap(read_text(path)), encoding="utf-8")
    print(f"Файл с настоящими данными: {out}")


def main():
    ap = argparse.ArgumentParser(description="Обезличивание данных на этом компьютере")
    ap.add_argument("action", choices=["mask", "restore"])
    ap.add_argument("file")
    ap.add_argument("--columns", default="", help="колонки с именами через запятую")
    a = ap.parse_args()
    p = pathlib.Path(a.file).expanduser().resolve()
    if not p.exists():
        sys.exit(f"Файл не найден: {p}")
    if a.action == "mask":
        mask(p, {c.strip() for c in a.columns.split(",") if c.strip()})
    else:
        restore(p)


if __name__ == "__main__":
    main()
