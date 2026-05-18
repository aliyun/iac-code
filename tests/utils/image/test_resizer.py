import io

import pytest
from PIL import Image

from iac_code.utils.image.resizer import (
    ImageResizeError,
    maybe_resize_and_downsample,
)


def _make_png(w: int, h: int, color=(255, 0, 0)) -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", (w, h), color=color).save(buf, format="PNG")
    return buf.getvalue()


def test_small_png_passes_through():
    raw = _make_png(100, 100)
    out = maybe_resize_and_downsample(raw)
    assert out.media_type == "image/png"
    assert out.dimensions.display_width == 100
    assert out.data == raw  # untouched


def test_oversized_dimension_is_scaled_down():
    raw = _make_png(3000, 1500)
    out = maybe_resize_and_downsample(raw)
    assert out.dimensions.display_width <= 2000
    assert out.dimensions.display_height <= 2000


def test_empty_buffer_raises():
    with pytest.raises(ImageResizeError):
        maybe_resize_and_downsample(b"")


def test_max_base64_size_respected():
    raw = _make_png(2400, 2400, color=(255, 255, 255))
    out = maybe_resize_and_downsample(raw)
    import base64

    assert len(base64.b64encode(out.data)) <= 5 * 1024 * 1024


def test_bmp_input_is_converted_to_png():
    buf = io.BytesIO()
    Image.new("RGB", (50, 50), color=(0, 255, 0)).save(buf, format="BMP")
    raw = buf.getvalue()
    assert raw[:2] == b"BM"  # sanity: confirm we made BMP

    out = maybe_resize_and_downsample(raw)
    assert out.media_type == "image/png"
    # Output bytes must be PNG-shaped (not BMP)
    assert out.data[:8] == b"\x89PNG\r\n\x1a\n"
