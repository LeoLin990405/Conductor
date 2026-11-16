"""DeepSeek vision accounting and request-image preparation."""

import io

import pytest
from PIL import Image

from src.llms.deepseek_image_tokens import (
    DEFAULT_MAX_IMAGE_BYTES,
    image_tokens,
    prepare_image,
    request_dimensions,
)


def _png(width, height, mode="RGB", color=(200, 30, 30)):
    image = Image.new(mode, (width, height), color)
    buffer = io.BytesIO()
    image.save(buffer, format="PNG")
    return buffer.getvalue()


def test_small_images_are_not_enlarged():
    assert request_dimensions(100, 80) == (100, 80)


def test_a_large_square_is_capped_by_the_token_budget():
    width, height = request_dimensions(2048, 2048)
    assert width <= 1302 and height <= 1302
    assert image_tokens(width, height) <= 1024


def test_a_wide_image_keeps_its_aspect_ratio():
    width, height = request_dimensions(4000, 1000)
    assert width > height
    assert abs(width / height - 4.0) < 0.15
    assert image_tokens(width, height) <= 1024


def test_token_accounting_matches_the_provider_calculator():
    # 1024px / 14px patches / 3:1 downsample = 25 cells per axis; every row
    # carries a separator plus two framing tokens: 25 * 26 + 2.
    assert image_tokens(1024, 1024) == 652
    assert image_tokens(544, 544) <= 1024
    assert image_tokens(64, 64) <= 1024


def test_opaque_images_are_jpegs_within_the_byte_budget():
    data, media_type = prepare_image(_png(2048, 2048))
    assert media_type == "image/jpeg"
    assert len(data) <= DEFAULT_MAX_IMAGE_BYTES
    with Image.open(io.BytesIO(data)) as image:
        assert image.size == request_dimensions(2048, 2048)


def test_alpha_images_are_webps():
    data, media_type = prepare_image(_png(800, 600, mode="RGBA", color=(0, 0, 0, 0)))
    assert media_type == "image/webp"
    with Image.open(io.BytesIO(data)) as image:
        assert image.size == (800, 600)


def test_invalid_bytes_raise():
    with pytest.raises(Exception):
        prepare_image(b"not an image")
