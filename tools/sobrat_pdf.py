"""Собирает PDF-презентацию из HTML. Каждая <section> страницы = один лист PDF.

Запуск:
    python3 tools/sobrat_pdf.py презентации/файл.html            # альбомный A4
    python3 tools/sobrat_pdf.py презентации/файл.html --knizhnaya # книжный A4
PDF кладётся рядом с HTML, с тем же именем. Пустые листы удаляются.

Нужны: python3 -m pip install --user playwright pypdf
       python3 -m playwright install chromium   (если нет Google Chrome)
"""
import pathlib, sys

import pypdf
from playwright.sync_api import sync_playwright


def launch(p):
    try:
        return p.chromium.launch()
    except Exception:
        try:
            return p.chromium.launch(channel="chrome")
        except Exception:
            sys.exit("Нет браузера для сборки. Поставьте: python3 -m playwright install chromium")


def is_blank(page):
    if page.extract_text().strip() or page.images:
        return False
    contents = page.get_contents()
    return contents is None or len(contents.get_data()) < 200


def main(src, portrait=False):
    src = pathlib.Path(src).resolve()
    out = src.with_suffix(".pdf")
    raw = src.with_name(src.stem + "._raw.pdf")
    w, h = ("210mm", "297mm") if portrait else ("297mm", "210mm")
    with sync_playwright() as p:
        browser = launch(p)
        page = browser.new_page()
        page.goto(src.as_uri(), wait_until="networkidle")
        page.wait_for_timeout(2000)  # шрифты успевают примениться
        page.pdf(path=str(raw), width=w, height=h, print_background=True,
                 margin={"top": "0", "right": "0", "bottom": "0", "left": "0"}, prefer_css_page_size=False)
        browser.close()
    reader = pypdf.PdfReader(str(raw))
    writer = pypdf.PdfWriter()
    dropped = 0
    for pg in reader.pages:
        if is_blank(pg):
            dropped += 1
        else:
            writer.add_page(pg)
    with open(out, "wb") as f:
        writer.write(f)
    raw.unlink()
    print(f"Готово: {out} — листов {len(writer.pages)}, убрано пустых {dropped}, {out.stat().st_size // 1024} КБ")


if __name__ == "__main__":
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    if len(args) != 1:
        sys.exit(__doc__)
    main(args[0], portrait="--knizhnaya" in sys.argv)
