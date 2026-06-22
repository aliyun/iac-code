"""User input wrapper for pipeline entry points.

Pipeline execution needs the original content blocks for model calls, while
UI, A2A status, telemetry, and sidecar metadata need text-only display data.
"""

from __future__ import annotations

from dataclasses import dataclass

from iac_code.agent.message import ContentBlock, ImageBlock, TextBlock, ToolResultBlock

PipelineInputContent = str | list[ContentBlock]
IMAGE_INPUT_PLACEHOLDER = "[Image input]"


def content_has_images(content: PipelineInputContent | None) -> bool:
    if not isinstance(content, list):
        return False
    return any(isinstance(block, ImageBlock) or getattr(block, "type", None) == "image" for block in content)


def content_display_text(content: PipelineInputContent | None) -> str:
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    parts: list[str] = []
    has_images = False
    for block in content:
        if isinstance(block, TextBlock):
            parts.append(block.text)
            continue
        if isinstance(block, ToolResultBlock):
            parts.append(block.content)
            continue
        if isinstance(block, ImageBlock) or getattr(block, "type", None) == "image":
            has_images = True
    text = "\n".join(part for part in parts if part)
    if text.strip():
        return text
    return IMAGE_INPUT_PLACEHOLDER if has_images else ""


@dataclass(frozen=True)
class PipelineUserInput:
    content: PipelineInputContent
    display_text: str
    has_images: bool

    @property
    def is_empty(self) -> bool:
        return not self.display_text.strip() and not self.has_images

    def with_prepended_text(self, text: str) -> "PipelineUserInput":
        prefix = text.strip()
        if not prefix:
            return self
        if isinstance(self.content, str):
            content: PipelineInputContent = f"{prefix}\n\n{self.content}" if self.content else prefix
        else:
            content = [TextBlock(text=prefix), *self.content]
        display_text = f"{prefix}\n\n{self.display_text}" if self.display_text.strip() else prefix
        return PipelineUserInput(
            content=content,
            display_text=display_text,
            has_images=content_has_images(content),
        )


def normalize_pipeline_user_input(
    user_input: str | list[ContentBlock] | PipelineUserInput | None,
    *,
    display_text: str | None = None,
) -> PipelineUserInput:
    if isinstance(user_input, PipelineUserInput):
        if display_text is None:
            return user_input
        return PipelineUserInput(
            content=user_input.content,
            display_text=display_text or content_display_text(user_input.content),
            has_images=user_input.has_images,
        )
    content: PipelineInputContent = "" if user_input is None else user_input
    resolved_display_text = display_text if display_text is not None else content_display_text(content)
    return PipelineUserInput(
        content=content,
        display_text=resolved_display_text,
        has_images=content_has_images(content),
    )
