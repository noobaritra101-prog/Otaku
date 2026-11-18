"""Watermark for uploaded waifu pictures (photos only). Needs: pip install pillow"""
import asyncio
import io
import logging
import os

from PIL import Image, ImageDraw, ImageFont

log = logging.getLogger("watermark")

TEXT = "Oᴛᴀᴋυ ຖᴇxυʂ"
# Put a font that has these characters next to the bot as watermark.ttf (Noto Sans Bold works),
# or edit this list. First one that exists is used.
FONTS = [
    os.path.join(os.path.dirname(__file__), "watermark.ttf"),
    "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
    "/usr/share/fonts/truetype/noto/NotoSans-Bold.ttf",
]


def _font(size: int):
    for p in FONTS:
        if os.path.exists(p):
            return ImageFont.truetype(p, size)
    return ImageFont.load_default()


def _stamp(raw: bytes) -> bytes:
    im = Image.open(io.BytesIO(raw)).convert("RGBA")
    w, h = im.size
    font = _font(max(16, w // 14))
    layer = Image.new("RGBA", im.size, (0, 0, 0, 0))
    d = ImageDraw.Draw(layer)
    x0, y0, x1, y1 = d.textbbox((0, 0), TEXT, font=font, stroke_width=2)
    x = (w - (x1 - x0)) // 2 - x0
    y = h - (y1 - y0) - max(12, h // 40) - y0          # bottom centre
    d.text((x, y), TEXT, font=font, fill=(255, 255, 255, 215),
           stroke_width=2, stroke_fill=(0, 0, 0, 190))
    out = io.BytesIO()
    Image.alpha_composite(im, layer).convert("RGB").save(out, "JPEG", quality=95)
    return out.getvalue()


async def stamp(bot, file_id: str) -> bytes | None:
    """Download a Telegram photo and return it watermarked (JPEG bytes), or None on any failure."""
    try:
        buf = io.BytesIO()
        await bot.download(file_id, destination=buf)
        return await asyncio.to_thread(_stamp, buf.getvalue())
    except Exception as e:
        log.warning("watermark failed: %s", e)
        return None
