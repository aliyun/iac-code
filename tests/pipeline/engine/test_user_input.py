from iac_code.agent.message import ImageBlock, TextBlock, ToolResultBlock
from iac_code.pipeline.engine.user_input import (
    PipelineUserInput,
    content_display_text,
    content_has_images,
    normalize_pipeline_user_input,
)


def test_normalize_string_input() -> None:
    value = normalize_pipeline_user_input("create an ecs")

    assert value == PipelineUserInput(
        content="create an ecs",
        display_text="create an ecs",
        has_images=False,
    )
    assert value.is_empty is False


def test_normalize_image_only_input_is_not_empty() -> None:
    image = ImageBlock(media_type="image/png", data="aGVsbG8=")

    value = normalize_pipeline_user_input([image])

    assert value.content == [image]
    assert value.display_text == "[Image input]"
    assert value.has_images is True
    assert value.is_empty is False


def test_content_display_text_extracts_text_and_tool_result_without_image_bytes() -> None:
    blocks = [
        TextBlock(text="text part"),
        ImageBlock(media_type="image/png", data="aGVsbG8="),
        ToolResultBlock(tool_use_id="toolu_1", content='{"answer":"ok"}'),
    ]

    assert content_has_images(blocks) is True
    assert content_display_text(blocks) == 'text part\n{"answer":"ok"}'


def test_with_prepended_text_preserves_original_image_block() -> None:
    image = ImageBlock(media_type="image/png", data="aGVsbG8=")
    value = normalize_pipeline_user_input([TextBlock(text="original"), image])

    updated = value.with_prepended_text("rollback context")

    assert updated.display_text == "rollback context\n\noriginal"
    assert updated.has_images is True
    assert updated.content == [
        TextBlock(text="rollback context"),
        TextBlock(text="original"),
        image,
    ]
