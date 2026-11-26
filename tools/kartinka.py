"""Картинка из HTML: сводку или отчёт можно прислать в группу фото, его видно прямо в чате.

Запуск:
    python3 tools/kartinka.py сводки/2026-11-27.html сводки/2026-11-27.png [--width 1080]
Ширина 1080 px подходит для телефона, высота берётся по содержимому.
Нужны библиотеки и браузер: bash tools/ustanovka.sh
"""
import argparse, pathlib, sys

import _venv  # перезапуск под .venv проекта, если он есть

try:
    from playwright.sync_api import sync_playwright
except ImportError:
    sys.exit("Нужен браузер для картинок. Запустите установку: bash tools/ustanovka.sh")


def main():
    ap = argparse.ArgumentParser(description="Картинка PNG из HTML")
    ap.add_argument("html")
    ap.add_argument("png")
    ap.add_argument("--width", type=int, default=1080)
    a = ap.parse_args()
    src = pathlib.Path(a.html).resolve()
    if not src.is_file():
        sys.exit(f"Нет файла {src}")
    with sync_playwright() as p:
        try:
            b = p.chromium.launch()
        except Exception:
            b = p.chromium.launch(channel="chrome")
        page = b.new_page(viewport={"width": a.width, "height": 800}, device_scale_factor=2)
        page.goto(src.as_uri())
        page.wait_for_load_state("networkidle")
        page.screenshot(path=a.png, full_page=True)
        b.close()
    print(f"Готово: {a.png}")


if __name__ == "__main__":
    main()
