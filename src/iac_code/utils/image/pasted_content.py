from __future__ import annotations

import re
from dataclasses import dataclass

_IMAGE_REF_RE = re.compile(r"\[Image #(\d+)\]")


@dataclass
class PastedContent:
    id: int
    type: str  # 'text' | 'image'
    content: str  # base64-encoded string when type == 'image'
    media_type: str | None = None
    filename: str | None = None
    source_path: str | None = None

    def is_valid_image(self) -> bool:
        return self.type == "image" and bool(self.content)


@dataclass
class ImageRef:
    id: int
    start: int
    end: int


def format_image_ref(image_id: int) -> str:
    return f"[Image #{image_id}]"


def parse_image_refs(text: str) -> list[ImageRef]:
    return [ImageRef(int(m.group(1)), m.start(), m.end()) for m in _IMAGE_REF_RE.finditer(text)]
