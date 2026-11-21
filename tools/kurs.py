#!/usr/bin/env python3
"""Официальный курс Нацбанка Казахстана на дату.

  python3 tools/kurs.py [ДД.ММ.ГГГГ] [--val RUB,USD,EUR]

Без даты берётся сегодняшняя. Курс пересчитывается на 1 единицу валюты (у некоторых Нацбанк даёт на 10 или 100).
Только стандартная библиотека. В запрос уходит одна дата, ничего больше.
"""
import argparse
import datetime as dt
import re
import sys
import urllib.error
import urllib.request
import xml.etree.ElementTree as ET

URL = "https://nationalbank.kz/rss/get_rates.cfm?fdate={}"


def fetch(date):
    req = urllib.request.Request(URL.format(date), headers={"User-Agent": "ai-shtab"})
    try:
        with urllib.request.urlopen(req, timeout=15) as r:
            return r.read()
    except (urllib.error.URLError, OSError):
        sys.exit("Нет ответа от сайта Нацбанка. Проверьте интернет и повторите через минуту.")


def rates(xml):
    try:
        root = ET.fromstring(xml)
    except ET.ParseError:
        sys.exit("Нацбанк ответил не курсами. Повторите позже.")
    out = {}
    for it in root.iter("item"):
        code, price, quant = (it.findtext(k, "").strip() for k in ("title", "description", "quant"))
        try:
            out[code.upper()] = float(price.replace(",", ".")) / float(quant or 1)
        except ValueError:
            continue
    return out


def main():
    ap = argparse.ArgumentParser(description="Курс Нацбанка РК")
    ap.add_argument("date", nargs="?", default=dt.date.today().strftime("%d.%m.%Y"), help="ДД.ММ.ГГГГ, по умолчанию сегодня")
    ap.add_argument("--val", default="RUB,USD,EUR", help="валюты через запятую, по умолчанию RUB,USD,EUR")
    a = ap.parse_args()
    if not re.fullmatch(r"\d{2}\.\d{2}\.\d{4}", a.date):
        sys.exit("Дата должна быть в виде ДД.ММ.ГГГГ, например 21.11.2026")
    try:
        dt.datetime.strptime(a.date, "%d.%m.%Y")
    except ValueError:
        sys.exit(f"Такой даты нет: {a.date}")
    got = rates(fetch(a.date))
    if not got:
        sys.exit(f"На {a.date} Нацбанк курсов не опубликовал. Попробуйте другую дату.")
    for code in (v.strip().upper() for v in a.val.split(",") if v.strip()):
        if code in got:
            print(f"{a.date}, Нацбанк РК: 1 {code} = {got[code]:.4f}".rstrip("0").rstrip(".") + " ₸")
        else:
            print(f"{a.date}, Нацбанк РК: валюты {code} в списке нет")


if __name__ == "__main__":
    main()
