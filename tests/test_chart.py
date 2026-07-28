from __future__ import annotations

from datetime import datetime, timedelta, timezone
from io import BytesIO

import pytest
from PIL import Image

from jin_market_pulse.chart import DOWN, HEIGHT, WIDTH, render_btc_chart
from jin_market_pulse.models import PricePoint, PriceSeries


def make_series(count: int = 30) -> PriceSeries:
    start = datetime.now(timezone.utc) - timedelta(hours=24)
    return PriceSeries(
        key="btc",
        name="BTC",
        source="fixture",
        points=[
            PricePoint(
                timestamp=start + timedelta(minutes=5 * index),
                value=100000 - index * 25,
            )
            for index in range(count)
        ],
    )


def test_btc_chart_is_nonblank_png_with_expected_size():
    content = render_btc_chart(make_series())
    image = Image.open(BytesIO(content)).convert("RGB")
    pixels = image.get_flattened_data()

    assert image.size == (WIDTH, HEIGHT)
    assert len(set(pixels)) > 20
    down_rgb = tuple(int(DOWN[index : index + 2], 16) for index in (1, 3, 5))
    assert any(
        abs(pixel[0] - down_rgb[0]) < 8
        and abs(pixel[1] - down_rgb[1]) < 8
        and abs(pixel[2] - down_rgb[2]) < 8
        for pixel in pixels
    )


def test_btc_chart_rejects_insufficient_points():
    with pytest.raises(ValueError, match="at least 20"):
        render_btc_chart(make_series(19))
