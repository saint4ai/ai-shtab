"""Готовит HTML-презентацию к отправке файлом в WhatsApp или Telegram.

Просмотрщик WhatsApp на iPhone не выполняет скрипты, поэтому:
- каждое окно-сцену (iframe со srcdoc и размером в style) записываем в GIF;
- видео (<video src="файл.mp4">) переводим в GIF;
- картинки с диска встраиваем в файл;
- все <script> удаляем.

Итог: один файл, который открывается на телефоне с картинками и анимациями.

Запуск:
    python3 tools/html_v_whatsapp.py презентации/вход.html презентации/выход_whatsapp.html
Нужны: python3 -m pip install --user playwright imageio-ffmpeg
       python3 -m playwright install chromium
"""
import asyncio, base64, html, mimetypes, pathlib, re, shutil, subprocess, sys, tempfile

FPS = 10          # кадров в секунду в GIF
HOLD = 2.5        # секунд держим последний кадр
DSF = 1.25        # чёткость на экране телефона
LIVE_SECONDS = 14 # сколько снимать сцену без __seek


def ffmpeg():
    exe = shutil.which("ffmpeg")
    if exe:
        return exe
    try:
        import imageio_ffmpeg
        return imageio_ffmpeg.get_ffmpeg_exe()
    except ImportError:
        sys.exit("Нет ffmpeg. Поставьте: python3 -m pip install --user imageio-ffmpeg")


def to_gif(args, dst, width, fps=FPS, colors=128):
    vf = (f"fps={fps},scale={width}:-2:flags=lanczos,split[a][b];"
          f"[a]palettegen=stats_mode=diff:max_colors={colors}[p];[b][p]paletteuse=dither=none:diff_mode=rectangle")
    subprocess.run([ffmpeg(), "-loglevel", "error", "-y", *args, "-vf", vf, "-loop", "0", "-an", str(dst)], check=True)


async def launch(p):
    """Браузер для съёмки: свой Chromium от Playwright или установленный Google Chrome."""
    try:
        return await p.chromium.launch(headless=True)
    except Exception:
        try:
            return await p.chromium.launch(channel="chrome", headless=True)
        except Exception:
            sys.exit("Нет браузера для съёмки. Поставьте: python3 -m playwright install chromium")


async def record_scene(browser, scene_html, w, h, work):
    """Снимает сцену скриншотами. Если в сцене есть window.__seek, кадры берутся по времени, без дрожания."""
    src = work / f"scene_{len(list(work.glob('scene_*.html')))}.html"
    src.write_text(scene_html, encoding="utf-8")
    ctx = await browser.new_context(viewport={"width": w, "height": h}, device_scale_factor=DSF)
    page = await ctx.new_page()
    await page.goto(src.as_uri())
    await page.wait_for_timeout(300)
    frames = []
    loop_ms = await page.evaluate("typeof window.__seek==='function' && window.__loop ? window.__loop : 0")
    if loop_ms:
        end = loop_ms - 4000 if loop_ms > 5000 else loop_ms
        for t in range(0, end + 400, 1000 // FPS):
            await page.evaluate(f"window.__seek({t})")
            frames.append((t / 1000, await page.screenshot(type="png")))
    else:
        clock = asyncio.get_running_loop()
        t0 = clock.time()
        while clock.time() - t0 < LIVE_SECONDS:
            ts = clock.time()
            frames.append((ts, await page.screenshot(type="png")))
    await ctx.close()
    fdir = pathlib.Path(tempfile.mkdtemp(dir=work))
    lines = []
    for i, (ts, data) in enumerate(frames):
        fn = fdir / f"{i:05d}.png"
        fn.write_bytes(data)
        dur = frames[i + 1][0] - ts if i + 1 < len(frames) else HOLD
        lines.append(f"file '{fn}'\nduration {max(dur, 0.001):.4f}")
    lines.append(f"file '{fdir / f'{len(frames) - 1:05d}.png'}'")
    (fdir / "list.txt").write_text("\n".join(lines))
    gif = fdir / "scene.gif"
    to_gif(["-f", "concat", "-safe", "0", "-i", str(fdir / "list.txt")], gif, int(w * DSF))
    return gif


def data_uri(path, mime=None):
    mime = mime or mimetypes.guess_type(str(path))[0] or "application/octet-stream"
    return f"data:{mime};base64," + base64.b64encode(pathlib.Path(path).read_bytes()).decode()


async def main(src_path, dst_path):
    from playwright.async_api import async_playwright
    src = pathlib.Path(src_path).resolve()
    base = src.parent
    page = src.read_text(encoding="utf-8")
    work = pathlib.Path(tempfile.mkdtemp(prefix="html_v_whatsapp_"))

    iframe_re = re.compile(r'<iframe\b([^>]*?)\bsrcdoc="([^"]*)"([^>]*)></iframe>', re.S)
    scenes = list(iframe_re.finditer(page))
    gifs = []
    if scenes:
        async with async_playwright() as p:
            browser = await launch(p)
            for m in scenes:
                attrs = m.group(1) + m.group(3)
                size = re.search(r"width:\s*(\d+)px;\s*height:\s*(\d+)px", attrs)
                if not size:
                    sys.exit("У iframe нет размера в style (width:..px;height:..px), сцену не записать")
                w, h = int(size.group(1)), int(size.group(2))
                gif = await record_scene(browser, html.unescape(m.group(2)), w, h, work)
                gifs.append(gif)
                print(f"сцена {len(gifs)}: {w}×{h}, {gif.stat().st_size // 1024} КБ")
            await browser.close()
    it = iter(gifs)

    def repl_iframe(m):
        title = re.search(r'title="([^"]*)"', m.group(1) + m.group(3))
        alt = title.group(1) if title else ""
        return (f'<img src="{data_uri(next(it), "image/gif")}" alt="{alt}" '
                'style="position:absolute;inset:0;width:100%;height:100%;display:block">')

    page = iframe_re.sub(repl_iframe, page)

    def repl_video(m):
        vsrc = re.search(r'src="([^"]+)"', m.group(0))
        if not vsrc or vsrc.group(1).startswith(("http", "data:")):
            return m.group(0)
        gif = work / f"video_{abs(hash(vsrc.group(1)))}.gif"
        to_gif(["-ss", "0", "-t", "11", "-i", str(base / vsrc.group(1))], gif, 640, fps=6)
        print(f"видео {vsrc.group(1)}: {gif.stat().st_size // 1024} КБ")
        return (f'<img src="{data_uri(gif, "image/gif")}" alt="" '
                'style="display:block;width:100%;height:auto;border-radius:inherit">')

    page = re.sub(r"<video\b[^>]*>.*?</video>", repl_video, page, flags=re.S)

    def repl_img(m):
        s = m.group(2)
        if s.startswith(("data:", "http")) or not (base / s).exists():
            return m.group(0)
        return f'{m.group(1)}"{data_uri(base / s)}"'

    page = re.sub(r'(<img\b[^>]*?\bsrc=)"([^"]+)"', repl_img, page)
    page = re.sub(r"<script\b.*?</script>", "", page, flags=re.S)

    dst = pathlib.Path(dst_path)
    dst.write_text(page, encoding="utf-8")
    shutil.rmtree(work, ignore_errors=True)
    print(f"Готово: {dst} ({dst.stat().st_size // 1024} КБ). Сцен: {len(gifs)}. Скриптов не осталось.")


if __name__ == "__main__":
    if len(sys.argv) != 3:
        sys.exit(__doc__)
    asyncio.run(main(sys.argv[1], sys.argv[2]))
