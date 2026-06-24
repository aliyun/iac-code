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
    assert payload == {"type": "image", "media_type": "image/png", "data": "aGVsbG8=", "ref_id": None}


def test_message_with_image_blocks_deserializes_round_trip():
    msg = Message(
        role="user",
        content=[TextBlock(text="see"), ImageBlock(media_type="image/png", data="aGVsbG8=")],
    )

    loaded = Message.from_dict(msg.to_dict())

    assert loaded == msg
    assert isinstance(loaded.content, list)
    assert isinstance(loaded.content[1], ImageBlock)


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
    assert "ref_id" not in api["content"][1]


def test_conversation_add_user_message_accepts_blocks():
    conv = Conversation()
    conv.add_user_message([TextBlock(text="hi"), ImageBlock(media_type="image/png", data="x")])
    assert conv.messages[-1].role == "user"
    assert isinstance(conv.messages[-1].content, list)
