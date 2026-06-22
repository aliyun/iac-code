from iac_code.agent.message import ImageBlock, Message, TextBlock, ToolResultBlock
from iac_code.pipeline.engine.resume_recovery import reconcile_resume_messages, user_message_already_in_resume


def test_reconcile_resume_messages_filters_duplicate_tool_result_blocks_only():
    existing = Message(
        role="user",
        content=[ToolResultBlock(tool_use_id="toolu_existing", content="done")],
    )
    sidecar = Message(
        role="user",
        content=[
            ToolResultBlock(tool_use_id="toolu_existing", content="done"),
            ToolResultBlock(tool_use_id="toolu_new", content="new"),
        ],
    )

    merged = reconcile_resume_messages([existing], [sidecar])

    assert merged is not None
    assert len(merged) == 2
    assert merged[0] == existing
    assert merged[1] == Message(
        role="user",
        content=[ToolResultBlock(tool_use_id="toolu_new", content="new")],
    )


def test_user_message_already_in_resume_matches_image_message():
    image_message = [
        TextBlock(text="参考这张图"),
        ImageBlock(media_type="image/png", data="aW1hZ2U="),
    ]

    assert user_message_already_in_resume(image_message, [Message(role="user", content=image_message)]) is True
