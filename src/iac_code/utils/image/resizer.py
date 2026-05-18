"""Pillow-based image resize / downsample, mirroring the CC imageResizer.

Limits:
  - API_IMAGE_MAX_BASE64_SIZE = 5 MB (after base64 encoding)
  - IMAGE_TARGET_RAW_SIZE     = 3.75 MB (raw bytes)
  - IMAGE_MAX_WIDTH/HEIGHT    = 2000 px
"""

from __future__ import annotations

import io
from dataclasses import dataclass

from PIL import Image, ImageFile

ImageFile.LOAD_TRUNCATED_IMAGES = False  # strict mode

_PILLOW_FORMAT_TO_MEDIA_TYPE: dict[str, str] = {
    "PNG": "image/png",
    "JPEG": "image/jpeg",
    "GIF": "image/gif",
    "WEBP": "image/webp",
}

API_IMAGE_MAX_BASE64_SIZE = 5 * 1024 * 1024
IMAGE_TARGET_RAW_SIZE = (API_IMAGE_MAX_BASE64_SIZE * 3) // 4
IMAGE_MAX_WIDTH = 2000
IMAGE_MAX_HEIGHT = 2000
JPEG_QUALITY_LADDER: tuple[int, ...] = (80, 60, 40, 20)

_LANCZOS = Image.Resampling.LANCZOS


class ImageResizeError(Exception):
    pass


@dataclass
class ImageDimensions:
    original_width: int
    original_height: int
    display_width: int
    display_height: int


@dataclass
class ResizeResult:
    data: bytes
    media_type: str
    dimensions: ImageDimensions


def _save_jpeg(img: Image.Image, quality: int) -> bytes:
    buf = io.BytesIO()
    if img.mode != "RGB":
        img = img.convert("RGB")
    img.save(buf, format="JPEG", quality=quality, optimize=True)
    return buf.getvalue()


def _save_png(img: Image.Image) -> bytes:
    buf = io.BytesIO()
    img.save(buf, format="PNG", optimize=True)
    return buf.getvalue()


def maybe_resize_and_downsample(raw: bytes) -> ResizeResult:
    if not raw:
        raise ImageResizeError("Image file is empty (0 bytes)")
    try:
        img = Image.open(io.BytesIO(raw))
        img.load()
    except Exception as exc:
        raise ImageResizeError(f"Could not decode image: {exc}") from exc

    ow, oh = img.size

    # Trust Pillow's decoded format (detect_image_format's magic-byte fallback
    # would label BMP as PNG).
    pillow_format = (img.format or "").upper()
    media_type = _PILLOW_FORMAT_TO_MEDIA_TYPE.get(pillow_format)
    # Formats that can't be sent as-is (e.g. BMP) get converted to PNG.
    if media_type is None:
        raw = _save_png(img)
        media_type = "image/png"

    # Fast path: dimensions and byte size already within limits.
    if len(raw) <= IMAGE_TARGET_RAW_SIZE and ow <= IMAGE_MAX_WIDTH and oh <= IMAGE_MAX_HEIGHT:
        return ResizeResult(
            data=raw,
            media_type=media_type,
            dimensions=ImageDimensions(ow, oh, ow, oh),
        )

    # Proportional scale to fit the bounding box.
    img_scaled = img.copy()
    img_scaled.thumbnail((IMAGE_MAX_WIDTH, IMAGE_MAX_HEIGHT), _LANCZOS)
    sw, sh = img_scaled.size

    if media_type == "image/png":
        attempt = _save_png(img_scaled)
        if len(attempt) <= IMAGE_TARGET_RAW_SIZE:
            return ResizeResult(attempt, "image/png", ImageDimensions(ow, oh, sw, sh))
        # Fall through to the JPEG quality ladder.

    for quality in JPEG_QUALITY_LADDER:
        attempt = _save_jpeg(img_scaled, quality)
        if len(attempt) <= IMAGE_TARGET_RAW_SIZE:
            return ResizeResult(attempt, "image/jpeg", ImageDimensions(ow, oh, sw, sh))

    # Last-resort: shrink to 1000 px and JPEG q=20.
    img_final = img.copy()
    img_final.thumbnail((1000, 1000), _LANCZOS)
    fw, fh = img_final.size
    attempt = _save_jpeg(img_final, 20)
    if len(attempt) > IMAGE_TARGET_RAW_SIZE:
        raise ImageResizeError(f"Image cannot be reduced under {IMAGE_TARGET_RAW_SIZE} bytes")
    return ResizeResult(attempt, "image/jpeg", ImageDimensions(ow, oh, fw, fh))
