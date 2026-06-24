"""Assemble raw text + already-pasted images into a list[ContentBlock].

Pasted images are expected to already carry resized/encoded data and a
populated ``media_type`` (the REPL paste path runs them through
``maybe_resize_and_downsample`` before storing). We therefore pass the
base64 payload through verbatim instead of decoding and re-encoding it.
"""

from __future__ import annotations

from iac_code.agent.message import ContentBlock, ImageBlock, TextBlock
from iac_code.utils.image.pasted_content import PastedContent, parse_image_refs


def process_user_input(
    text: str,
    *,
    pasted_contents: dict[int, PastedContent],
) -> list[ContentBlock]:
    refs = [r for r in parse_image_refs(text) if r.id in pasted_contents and pasted_contents[r.id].is_valid_image()]
    if not refs:
        return [TextBlock(text=text)] if text else []

    blocks: list[ContentBlock] = []
    cursor = 0
    for ref in refs:
        if ref.start > cursor:
            blocks.append(TextBlock(text=text[cursor : ref.start]))
        pc = pasted_contents[ref.id]
        blocks.append(
            ImageBlock(
                media_type=pc.media_type or "image/png",
                data=pc.content,
                ref_id=pc.id,
            )
        )
        cursor = ref.end
    if cursor < len(text):
        blocks.append(TextBlock(text=text[cursor:]))
    return blocks
