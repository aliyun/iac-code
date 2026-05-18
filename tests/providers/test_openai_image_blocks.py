from iac_code.providers.base import ContentBlock, Message
from iac_code.providers.openai_provider import OpenAIProvider


def test_user_image_converts_to_image_url():
    p = OpenAIProvider(model="gpt-5.4", api_key="x")
    msg = Message(
        role="user",
        content=[
            ContentBlock(type="text", text="look"),
            ContentBlock(type="image", media_type="image/png", data="aGVsbG8="),
        ],
    )
    api = p._convert_messages([msg])
    assert api == [
        {
            "role": "user",
            "content": [
                {"type": "text", "text": "look"},
                {
                    "type": "image_url",
                    "image_url": {"url": "data:image/png;base64,aGVsbG8="},
                },
            ],
        }
    ]


def test_text_only_user_message_stays_string():
    p = OpenAIProvider(model="gpt-5.4", api_key="x")
    msg = Message(role="user", content="plain")
    api = p._convert_messages([msg])
    assert api == [{"role": "user", "content": "plain"}]


def test_user_image_only_emits_content_list():
    p = OpenAIProvider(model="gpt-5.4", api_key="x")
    msg = Message(
        role="user",
        content=[
            ContentBlock(type="image", media_type="image/jpeg", data="ZmFrZQ=="),
        ],
    )
    api = p._convert_messages([msg])
    assert api == [
        {
            "role": "user",
            "content": [
                {
                    "type": "image_url",
                    "image_url": {"url": "data:image/jpeg;base64,ZmFrZQ=="},
                },
            ],
        }
    ]
