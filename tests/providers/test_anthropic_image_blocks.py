from iac_code.providers.anthropic_provider import AnthropicProvider
from iac_code.providers.base import ContentBlock, Message


def test_image_block_converts_to_anthropic_source():
    p = AnthropicProvider(model="claude-opus-4-7", api_key="x")
    msg = Message(
        role="user",
        content=[
            ContentBlock(type="text", text="look"),
            ContentBlock(type="image", media_type="image/png", data="aGVsbG8="),
        ],
    )
    api = p._convert_messages([msg])
    assert api[0]["content"][1] == {
        "type": "image",
        "source": {"type": "base64", "media_type": "image/png", "data": "aGVsbG8="},
    }


def test_text_only_message_unchanged():
    p = AnthropicProvider(model="claude-opus-4-7", api_key="x")
    msg = Message(role="user", content="plain")
    api = p._convert_messages([msg])
    assert api == [{"role": "user", "content": "plain"}]
