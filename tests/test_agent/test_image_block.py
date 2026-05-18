from iac_code.agent.message import (
    Conversation,
    ImageBlock,
    Message,
    TextBlock,
)


def test_image_block_serializes_round_trip():
    block = ImageBlock(media_type="image/png", data="aGVsbG8=")
    assert block.type == "image"
    payload = block.model_dump()
    assert payload == {"type": "image", "media_type": "image/png", "data": "aGVsbG8="}


def test_message_with_blocks_to_api_format_keeps_image():
    msg = Message(
        role="user",
        content=[
            TextBlock(text="see"),
            ImageBlock(media_type="image/png", data="x"),
        ],
    )
    api = msg.to_api_format()
    assert api["content"][1]["type"] == "image"
    assert api["content"][1]["data"] == "x"


def test_conversation_add_user_message_accepts_blocks():
    conv = Conversation()
    conv.add_user_message([TextBlock(text="hi"), ImageBlock(media_type="image/png", data="x")])
    assert conv.messages[-1].role == "user"
    assert isinstance(conv.messages[-1].content, list)
