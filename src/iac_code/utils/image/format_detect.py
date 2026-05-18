"""Detect image format from magic bytes."""

from __future__ import annotations

import re

IMAGE_EXTENSION_REGEX = re.compile(r"\.(png|jpe?g|gif|webp)$", re.IGNORECASE)


def detect_image_format(buf: bytes) -> str:
    """Return the MIME type. Falls back to image/png when unknown."""
    if len(buf) < 4:
        return "image/png"
    if buf[:8] == b"\x89PNG\r\n\x1a\n":
        return "image/png"
    if buf[:3] == b"\xff\xd8\xff":
        return "image/jpeg"
    if buf[:3] == b"GIF":
        return "image/gif"
    if len(buf) >= 12 and buf[:4] == b"RIFF" and buf[8:12] == b"WEBP":
        return "image/webp"
    return "image/png"
