"""Contact sheets: tile sampled frames (with timestamp labels) into one image per 9 frames."""

from __future__ import annotations

import base64
import io
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

from .util import fmt_ts

COLS, ROWS = 3, 3
PER_SHEET = COLS * ROWS
TILE_LONG = 480  # 3x3 tiles of 480x270 -> 1440x810, just over 1.1 MP (~1,550 image tokens)


def _font(size: int):
    try:
        return ImageFont.load_default(size=size)
    except TypeError:  # Pillow < 10.1
        return ImageFont.load_default()


def make_sheet(frames: list[tuple[float, Path]]) -> Image.Image:
    with Image.open(frames[0][1]) as first:
        vertical = first.height > first.width
    tw, th = (TILE_LONG * 9 // 16, TILE_LONG) if vertical else (TILE_LONG, TILE_LONG * 9 // 16)
    cols = min(COLS, len(frames))
    rows = (len(frames) + cols - 1) // cols
    sheet = Image.new("RGB", (cols * tw, rows * th), "black")
    draw = ImageDraw.Draw(sheet)
    font = _font(max(14, th // 12))
    for i, (t, path) in enumerate(frames):
        with Image.open(path) as im:
            im = im.convert("RGB")
            im.thumbnail((tw, th))
            x, y = (i % cols) * tw, (i // cols) * th
            sheet.paste(im, (x + (tw - im.width) // 2, y + (th - im.height) // 2))
        label = fmt_ts(t)
        box = draw.textbbox((x + 6, y + 6), label, font=font)
        draw.rectangle((box[0] - 4, box[1] - 3, box[2] + 4, box[3] + 3), fill="black")
        draw.text((x + 6, y + 6), label, fill="yellow", font=font)
    return sheet


def to_b64_jpeg(im: Image.Image, quality: int = 80) -> str:
    buf = io.BytesIO()
    im.save(buf, "JPEG", quality=quality)
    return base64.standard_b64encode(buf.getvalue()).decode()


def sheets_for(frames: list[tuple[float, Path]]) -> list[Image.Image]:
    return [make_sheet(frames[i:i + PER_SHEET]) for i in range(0, len(frames), PER_SHEET)]
