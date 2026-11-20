"""Watermark for uploaded waifu pictures (photos only). Needs: pip install pillow

IMPORTANT: keep watermark.ttf (DejaVu Sans Bold, it has every character of TEXT) in the
same folder as this file. Without a font the watermark cannot be drawn, so stamp() fails
loudly (see the log) instead of drawing tiny invisible text.
"""
import asyncio
import io
import logging
import os
from functools import lru_cache

from PIL import Image, ImageDraw, ImageFont

log = logging.getLogger("watermark")
LAST_ERROR = ""          # reason of the last failure, shown in the /upload message

TEXT = "Oᴛᴀᴋυ ຖᴇxυʂ"
HERE = os.path.dirname(os.path.abspath(__file__))
FONTS = [
    os.path.join(HERE, "watermark.ttf"),
    os.path.join(os.getcwd(), "watermark.ttf"),
    os.path.join(HERE, "fonts", "watermark.ttf"),
    "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
    "/usr/share/fonts/dejavu/DejaVuSans-Bold.ttf",
    "/usr/share/fonts/truetype/noto/NotoSans-Bold.ttf",
]


@lru_cache(maxsize=32)
def _font(size: int):
    for p in FONTS:
        if os.path.exists(p):
            try:
                return ImageFont.truetype(p, size)
            except OSError as e:
                log.warning("cannot load font %s: %s", p, e)
    raise RuntimeError("watermark font not found: put watermark.ttf next to watermark.py")


def _stamp(raw: bytes) -> bytes:
    im = Image.open(io.BytesIO(raw)).convert("RGB")
    w, h = im.size
    font = _font(max(12, w // 28))
    x0, y0, x1, y1 = ImageDraw.Draw(im).textbbox((0, 0), TEXT, font=font, stroke_width=1)
    tw, th = x1 - x0, y1 - y0
    pad = max(8, h // 60)
    strip_h = min(h, th + pad + 8)                      # only the bottom strip is processed (faster)
    top = h - strip_h
    strip = im.crop((0, top, w, h)).convert("RGBA")
    layer = Image.new("RGBA", strip.size, (0, 0, 0, 0))
    ImageDraw.Draw(layer).text(((w - tw) // 2 - x0, 8 - y0), TEXT, font=font,
                               fill=(255, 255, 255, 110), stroke_width=1, stroke_fill=(0, 0, 0, 90))
    im.paste(Image.alpha_composite(strip, layer).convert("RGB"), (0, top))
    out = io.BytesIO()
    im.save(out, "JPEG", quality=90)
    return out.getvalue()


async def _download(bot, file_id: str, tries: int = 3) -> bytes:
    """getFile + download with short timeouts and retries. Telegram sometimes answers 'Gateway Timeout';
    waiting for it twice (the default) took 2 minutes, so give up on each try quickly and try again."""
    last: Exception | None = None
    for i in range(tries):
        try:
            f = await bot.get_file(file_id, request_timeout=15)
            buf = io.BytesIO()
            await bot.download_file(f.file_path, destination=buf, timeout=30)
            return buf.getvalue()
        except Exception as e:
            last = e
            log.warning("watermark download try %s/%s failed: %s", i + 1, tries, e)
            if i < tries - 1:
                await asyncio.sleep(1.5 * (i + 1))
    raise last


async def stamp(bot, file_id: str) -> bytes | None:
    """Download a Telegram photo and return it watermarked (JPEG bytes), or None on any failure."""
    global LAST_ERROR
    LAST_ERROR = ""
    try:
        return await asyncio.to_thread(_stamp, await _download(bot, file_id))
    except Exception as e:
        LAST_ERROR = f"{type(e).__name__}: {e}"[:120]
        log.exception("watermark failed")
        return None
