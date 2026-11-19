#!/usr/bin/env python3
"""Обезличивание данных на этом компьютере.

ИИН и БИН, телефоны, почты и названия клиентов заменяются метками вида «Клиент-001».
Таблица соответствий и исходные файлы лежат в .private/ и никуда не отправляются.

  python3 tools/obezlichit.py inbox                 обработать все новые файлы во «входящие/»
  python3 tools/obezlichit.py mask ФАЙЛ [--columns "Покупатель"] [--kind Клиент]
  python3 tools/obezlichit.py restore ФАЙЛ           вернуть настоящие названия в готовый отчёт

inbox: для каждого нового файла ищет персональные данные. Нашёл — пишет копию «имя.обезл.ext»,
а исходник переносит в .private/исходники/. Чистые файлы оставляет как есть. Печатает только сводку,
содержимое файлов не печатает. Поддерживаются .xlsx (нужен openpyxl), .csv, .txt, .md.
"""
import argparse
import csv
import io
import json
import pathlib
import re
import shutil
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent
INBOX = ROOT / "входящие"
PRIV = ROOT / ".private"
MAP = PRIV / "соответствия.json"
DONE = PRIV / "обработано.txt"
ORIG = PRIV / "исходники"

PATTERNS = [
    ("ИИН", re.compile(r"(?<!\d)\d{12}(?!\d)")),
    ("Телефон", re.compile(r"(?<![\d+])(?:\+7|8)[\s\-()]*\d{3}[\s\-()]*\d{3}[\s\-]*\d{2}[\s\-]*\d{2}(?!\d)")),
    ("Почта", re.compile(r"[\w.+-]+@[\w-]+(?:\.[\w-]+)+")),
]
# Колонки с названиями клиентов: метка «Клиент» во всех таблицах, чтобы клиентов можно было сопоставлять.
CLIENT_HEADERS = re.compile(r"клиент|покупател|контрагент|заказчик|получател|плательщик|фио|контактное лицо", re.I)
LABEL = re.compile(r"\b([A-Za-zА-Яа-яЁё]+)-(\d{3})\b")
SKIP = {"README.md", ".DS_Store"}


def load_map():
    if MAP.exists():
        return json.loads(MAP.read_text(encoding="utf-8"))
    return {"forward": {}, "back": {}, "counters": {}}


def save_map(m):
    PRIV.mkdir(exist_ok=True)
    MAP.write_text(json.dumps(m, ensure_ascii=False, indent=1), encoding="utf-8")


class Masker:
    def __init__(self, m, columns=None, kind=None, auto=False):
        self.m, self.columns, self.kind, self.auto, self.count = m, columns or set(), kind, auto, {}

    def label(self, kind, value):
        value = re.sub(r"\s+", " ", str(value)).strip()
        key = f"{kind}:{value.casefold()}"
        lab = self.m["forward"].get(key)
        if lab is None:
            n = self.m["counters"].get(kind, 0) + 1
            self.m["counters"][kind] = n
            lab = f"{kind}-{n:03d}"
            self.m["forward"][key], self.m["back"][lab] = lab, value
        self.count[kind] = self.count.get(kind, 0) + 1
        return lab

    def is_name_column(self, header):
        if not header:
            return False
        return header in self.columns or (self.auto and bool(CLIENT_HEADERS.search(header)))

    def text(self, s):
        for kind, rx in PATTERNS:
            s = rx.sub(lambda mt, k=kind: self.label(k, mt.group(0)), s)
        return s

    def cell(self, v, header=None):
        if v is None or v == "":
            return v
        if self.is_name_column(header):
            return self.label(self.kind or "Клиент", v)
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
    sys.exit(f"Не удалось прочитать {p.name}: неизвестная кодировка")


def need_openpyxl():
    try:
        import openpyxl
        return openpyxl
    except ImportError:
        sys.exit("Для .xlsx нужен openpyxl. Установите: python3 -m pip install --user openpyxl")


def masked_name(path):
    return path.with_name(f"{path.stem}.обезл{path.suffix}")


def mask_file(path, mk):
    """Пишет обезличенную копию рядом с исходником. Возвращает путь копии."""
    out = masked_name(path)
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
        return None
    return out


def stat(count):
    return ", ".join(f"{k} {v}" for k, v in sorted(count.items())) or "персональных данных нет"


def inbox():
    PRIV.mkdir(exist_ok=True)
    done = set(DONE.read_text(encoding="utf-8").splitlines()) if DONE.exists() else set()
    m = load_map()
    new = [p for p in sorted(INBOX.iterdir()) if p.is_file() and p.name not in SKIP
           and ".обезл." not in p.name and p.name not in done]
    if not new:
        print("Новых файлов во «входящих» нет.")
        return
    for p in new:
        mk = Masker(m, auto=True)
        out = mask_file(p, mk)
        if out is None:
            print(f"{p.name}: формат не поддерживается, файл оставлен как есть. Открывай, только если в нём нет данных клиентов.")
        elif mk.count:
            ORIG.mkdir(parents=True, exist_ok=True)
            shutil.move(str(p), str(ORIG / p.name))
            print(f"{p.name}: заменено {stat(mk.count)}. Работай с «{out.name}», исходник убран в .private/исходники/.")
        else:
            out.unlink()
            print(f"{p.name}: персональных данных нет, файл можно открывать.")
        done.add(p.name)
    DONE.write_text("\n".join(sorted(done)), encoding="utf-8")
    save_map(m)


def mask(path, columns, kind):
    m = load_map()
    mk = Masker(m, columns=columns, kind=kind)
    out = mask_file(path, mk)
    if out is None:
        sys.exit(f"Формат {path.suffix} не поддерживается: .xlsx, .csv, .txt, .md")
    save_map(m)
    print(f"Готово: {out.name}. Заменено: {stat(mk.count)}")


def restore(path):
    back = load_map()["back"]
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
    print(f"Файл с настоящими данными: {out.relative_to(ROOT)}. Откройте его сами, агенту он закрыт.")


def main():
    ap = argparse.ArgumentParser(description="Обезличивание данных на этом компьютере")
    ap.add_argument("action", choices=["inbox", "mask", "restore"])
    ap.add_argument("file", nargs="?")
    ap.add_argument("--columns", default="", help="колонки с названиями через запятую")
    ap.add_argument("--kind", default="Клиент", help="метка для этих колонок, по умолчанию «Клиент»")
    a = ap.parse_args()
    if a.action == "inbox":
        return inbox()
    if not a.file:
        sys.exit("Укажите файл")
    p = pathlib.Path(a.file).expanduser().resolve()
    if not p.exists():
        sys.exit(f"Файл не найден: {a.file}")
    if a.action == "mask":
        mask(p, {c.strip() for c in a.columns.split(",") if c.strip()}, a.kind)
    else:
        restore(p)


if __name__ == "__main__":
    main()
