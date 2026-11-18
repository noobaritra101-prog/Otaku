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

TEXT = "Oᴛᴀᴋυ ຖᴇxυʂ"
HERE = os.path.dirname(os.path.abspath(__file__))
FONTS = [
    os.path.join(HERE, "watermark.ttf"),
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
    font = _font(max(16, w // 14))
    x0, y0, x1, y1 = ImageDraw.Draw(im).textbbox((0, 0), TEXT, font=font, stroke_width=2)
    tw, th = x1 - x0, y1 - y0
    pad = max(12, h // 40)
    strip_h = min(h, th + pad + 8)                      # only the bottom strip is processed (faster)
    top = h - strip_h
    strip = im.crop((0, top, w, h)).convert("RGBA")
    layer = Image.new("RGBA", strip.size, (0, 0, 0, 0))
    ImageDraw.Draw(layer).text(((w - tw) // 2 - x0, 8 - y0), TEXT, font=font,
                               fill=(255, 255, 255, 230), stroke_width=2, stroke_fill=(0, 0, 0, 200))
    im.paste(Image.alpha_composite(strip, layer).convert("RGB"), (0, top))
    out = io.BytesIO()
    im.save(out, "JPEG", quality=90)
    return out.getvalue()


async def stamp(bot, file_id: str) -> bytes | None:
    """Download a Telegram photo and return it watermarked (JPEG bytes), or None on any failure."""
    try:
        buf = io.BytesIO()
        await bot.download(file_id, destination=buf)
        return await asyncio.to_thread(_stamp, buf.getvalue())
    except Exception:
        log.exception("watermark failed")
        return None
