"""DeepSeek vision accounting and request-image preparation.

Ported from the official harness (``dsh-llm-deepseek`` image-tokens module,
itself the provider's published calculator): scale an image below 544x544 total
pixels up, project it onto a 14px patch grid downsampled 3:1 per axis into token
cells, and cap one request image at 1024 tokens by solving the largest
aspect-preserving grid that fits.

Requests should carry the dimensions the provider prices, not the raw chart:
the harness re-encodes opaque images as JPEG (85/75/60 quality ladder) and
alpha-bearing images as WebP, capped at 2 MiB, then uploads those exact bytes.
"""

from __future__ import annotations

import io
import math

from PIL import Image

PATCH_SIZE = 14
DOWNSAMPLE_RATIO = 3
MAX_IMAGE_TOKENS = 1024
MIN_PIXELS = 544 * 544
CELL_SIZE = PATCH_SIZE * DOWNSAMPLE_RATIO
DEFAULT_MAX_IMAGE_BYTES = 2 * 1024 * 1024
JPEG_QUALITIES = (85, 75, 60)


def _int_div(value: int, divisor: int) -> int:
    return value // divisor


def _ceil_div(value: int, divisor: int) -> int:
    return (value + divisor - 1) // divisor


def _grid_tokens(grid_height: int, grid_width: int) -> int:
    return grid_height * (grid_width + 1) + 2


def _grid_cells(padded_length: int) -> int:
    return _ceil_div(_int_div(padded_length, PATCH_SIZE), DOWNSAMPLE_RATIO)


def _long_edge_dimensions(width: int, height: int, target_long: int) -> tuple[int, int]:
    if width >= height:
        return target_long, max(1, round(height * target_long / width))
    return max(1, round(width * target_long / height)), target_long


def _solve_resize_ratio(height: int, width: int, budget: int) -> dict:
    aspect = height / width
    ideal_grid_width = math.sqrt((budget - 2) / aspect + 0.25) - 0.5
    ideal_grid_height = ideal_grid_width * aspect
    if ideal_grid_width < 1:
        solved_grid_width = 1
        solved_grid_height = _int_div(budget - 2, 2)
        best_width = solved_grid_width * CELL_SIZE
        best_height = solved_grid_height * CELL_SIZE
    elif ideal_grid_height < 1:
        solved_grid_height = 1
        best_width = (_int_div(budget - 2, solved_grid_height) - 1) * CELL_SIZE
        best_height = solved_grid_height * CELL_SIZE
    else:
        solved_grid_width = int(ideal_grid_width)
        solved_grid_height = int(ideal_grid_height)
        scale = min(
            solved_grid_width * CELL_SIZE / width,
            solved_grid_height * CELL_SIZE / height,
        )
        best_width = int(width * scale / PATCH_SIZE) * PATCH_SIZE
        best_height = int(height * scale / PATCH_SIZE) * PATCH_SIZE
    grid_height = _grid_cells(best_height)
    grid_width = _grid_cells(best_width)
    return {
        "grid_height": grid_height,
        "grid_width": grid_width,
        "best_height": best_height,
        "best_width": best_width,
        "num_tokens": _grid_tokens(grid_height, grid_width),
    }


def _safe_resize(height: int, width: int, padded_height: int, padded_width: int) -> dict:
    grid_height = _grid_cells(padded_height)
    grid_width = _grid_cells(padded_width)
    direct = {
        "grid_height": grid_height,
        "grid_width": grid_width,
        "best_height": padded_height,
        "best_width": padded_width,
        "num_tokens": _grid_tokens(grid_height, grid_width),
    }
    if direct["num_tokens"] <= MAX_IMAGE_TOKENS:
        return direct
    solved = _solve_resize_ratio(height, width, MAX_IMAGE_TOKENS)
    return solved


def _resize_once(width: int, height: int) -> dict:
    scaled_width, scaled_height = width, height
    pixels = scaled_width * scaled_height
    if 0 < pixels < MIN_PIXELS:
        scale = math.sqrt(MIN_PIXELS / pixels)
        scaled_width = int(scaled_width * scale)
        scaled_height = int(scaled_height * scale)
    padded_width = _ceil_div(scaled_width, PATCH_SIZE) * PATCH_SIZE
    padded_height = _ceil_div(scaled_height, PATCH_SIZE) * PATCH_SIZE
    return _safe_resize(scaled_height, scaled_width, padded_height, padded_width)


def _same_resize(a: dict, b: dict) -> bool:
    keys = ("grid_height", "grid_width", "best_height", "best_width", "num_tokens")
    return all(a[key] == b[key] for key in keys)


def image_tokens(width: int, height: int) -> int:
    """Provider vision-token price for one image of these dimensions."""
    result = _resize_once(width, height)
    for _ in range(1, 10):
        nxt = _resize_once(result["best_width"], result["best_height"])
        if _same_resize(nxt, result):
            return result["num_tokens"]
        result = nxt
    return result["num_tokens"]


def request_dimensions(width: int, height: int) -> tuple[int, int]:
    """Dimensions the harness sends so the provider keeps the whole image."""
    padded_width = _ceil_div(width, PATCH_SIZE) * PATCH_SIZE
    padded_height = _ceil_div(height, PATCH_SIZE) * PATCH_SIZE
    if _grid_tokens(_grid_cells(padded_height), _grid_cells(padded_width)) <= MAX_IMAGE_TOKENS:
        return width, height
    solved = _solve_resize_ratio(height, width, MAX_IMAGE_TOKENS)
    long_edge = solved["best_width"] if width >= height else solved["best_height"]
    return _long_edge_dimensions(width, height, long_edge)


def _has_alpha(image: Image.Image) -> bool:
    return image.mode in ("RGBA", "LA", "P") and "transparency" in image.info or image.mode in ("RGBA", "LA")


def _encode(image: Image.Image) -> tuple[bytes, str]:
    if _has_alpha(image):
        buffer = io.BytesIO()
        image.save(buffer, format="WEBP", quality=85, method=0)
        return buffer.getvalue(), "image/webp"
    best: bytes | None = None
    for quality in JPEG_QUALITIES:
        buffer = io.BytesIO()
        image.convert("RGB").save(buffer, format="JPEG", quality=quality)
        data = buffer.getvalue()
        if best is None or len(data) < len(best):
            best = data
        if len(data) <= DEFAULT_MAX_IMAGE_BYTES:
            return data, "image/jpeg"
    assert best is not None
    return best, "image/jpeg"


def prepare_image(data: bytes) -> tuple[bytes, str]:
    """Resize and re-encode one image for a DeepSeek request.

    Returns ``(bytes, media_type)``. Small images are never enlarged; the
    dimensions are the provider-priced request dimensions.
    """
    with Image.open(io.BytesIO(data)) as image:
        image.load()
        target = request_dimensions(image.width, image.height)
        if target != (image.width, image.height):
            image = image.resize(target, Image.LANCZOS)
        return _encode(image)
