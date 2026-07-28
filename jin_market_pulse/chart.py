from __future__ import annotations

from io import BytesIO

from PIL import Image, ImageDraw, ImageFont

from .config import KST
from .models import PriceSeries


WIDTH = 1200
HEIGHT = 600
BACKGROUND = "#090b0f"
GRID = "#252a33"
TEXT = "#f4f6f8"
MUTED = "#9da5b2"
UP = "#31c48d"
DOWN = "#ff5a52"


def _font(size: int, *, bold: bool = False) -> ImageFont.ImageFont:
    names = (
        ("DejaVuSans-Bold.ttf", "arialbd.ttf")
        if bold
        else ("DejaVuSans.ttf", "arial.ttf")
    )
    for name in names:
        try:
            return ImageFont.truetype(name, size)
        except OSError:
            continue
    return ImageFont.load_default()


def render_btc_chart(series: PriceSeries) -> bytes:
    if len(series.points) < 20:
        raise ValueError("BTC chart requires at least 20 valid price points")

    values = [point.value for point in series.points]
    first = values[0]
    current = values[-1]
    low = min(values)
    high = max(values)
    change = (current - first) / first * 100 if first else 0.0
    color = UP if change >= 0 else DOWN

    image = Image.new("RGB", (WIDTH, HEIGHT), BACKGROUND)
    draw = ImageDraw.Draw(image)
    title_font = _font(30, bold=True)
    price_font = _font(54, bold=True)
    body_font = _font(24)
    small_font = _font(19)

    draw.text((70, 42), "BTC / USD · 24H", fill=TEXT, font=title_font)
    draw.text((70, 85), f"${current:,.0f}", fill=TEXT, font=price_font)
    draw.text(
        (355, 108),
        f"{change:+.2f}%",
        fill=color,
        font=_font(30, bold=True),
    )
    draw.text(
        (790, 56),
        f"HIGH  ${high:,.0f}",
        fill=MUTED,
        font=body_font,
    )
    draw.text(
        (790, 94),
        f"LOW   ${low:,.0f}",
        fill=MUTED,
        font=body_font,
    )

    left, top, right, bottom = 85, 180, 1130, 510
    span = max(high - low, max(abs(high), 1.0) * 0.002)
    padded_low = low - span * 0.08
    padded_high = high + span * 0.08

    for index in range(5):
        y = top + (bottom - top) * index / 4
        draw.line((left, y, right, y), fill=GRID, width=1)
        value = padded_high - (padded_high - padded_low) * index / 4
        draw.text((right + 12, y - 10), f"{value:,.0f}", fill=MUTED, font=small_font)

    coordinates: list[tuple[float, float]] = []
    for index, value in enumerate(values):
        x = left + (right - left) * index / max(len(values) - 1, 1)
        y = bottom - (value - padded_low) / (padded_high - padded_low) * (bottom - top)
        coordinates.append((x, y))

    if len(coordinates) >= 2:
        fill_points = [(left, bottom), *coordinates, (right, bottom)]
        overlay = Image.new("RGBA", image.size, (0, 0, 0, 0))
        overlay_draw = ImageDraw.Draw(overlay)
        rgb = tuple(int(color[index : index + 2], 16) for index in (1, 3, 5))
        overlay_draw.polygon(fill_points, fill=(*rgb, 35))
        image = Image.alpha_composite(image.convert("RGBA"), overlay).convert("RGB")
        draw = ImageDraw.Draw(image)
        draw.line(coordinates, fill=color, width=4, joint="curve")

    tick_indexes = [0, len(series.points) // 4, len(series.points) // 2, len(series.points) * 3 // 4, len(series.points) - 1]
    for index in tick_indexes:
        point = series.points[index]
        x = left + (right - left) * index / max(len(series.points) - 1, 1)
        label = point.timestamp.astimezone(KST).strftime("%m/%d %H:%M")
        box = draw.textbbox((0, 0), label, font=small_font)
        label_width = box[2] - box[0]
        draw.text((x - label_width / 2, bottom + 22), label, fill=MUTED, font=small_font)

    draw.text((70, 560), "Source: Yahoo Finance | Times in KST", fill=MUTED, font=small_font)
    output = BytesIO()
    image.save(output, format="PNG", optimize=True)
    return output.getvalue()
