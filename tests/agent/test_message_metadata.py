from iac_code.agent.message import (
    RECALLED_MEMORY_MARKER,
    Message,
    ToolResultBlock,
    create_recalled_memory_message,
    get_recalled_memory_files,
    is_recalled_memory_message,
)


def test_aliyun_http_metadata_roundtrips_in_session_but_not_provider_wire():
    message = Message(
        role="user",
        content=[
            ToolResultBlock(
                tool_use_id="tool-1",
                content='{"Instances": []}',
                metadata={
                    "aliyun_http": {
                        "contract_version": "aliyun_body_v1",
                        "status": 200,
                        "status_class": "2xx",
                        "content_state": "inline_final",
                    }
                },
            )
        ],
    )

    restored = Message.from_dict(message.to_dict())

    assert restored == message
    assert restored.content[0].metadata["aliyun_http"]["content_state"] == "inline_final"
    assert restored.to_api_format() == {
        "role": "user",
        "content": [
            {
                "type": "tool_result",
                "tool_use_id": "tool-1",
                "content": '{"Instances": []}',
                "is_error": False,
            }
        ],
    }


def test_recalled_memory_message_serializes_metadata():
    msg = create_recalled_memory_message(
        "# Recalled Memory\nUse YAML for ROS templates",
        ["ros-yaml.md"],
    )

    data = msg.to_dict()
    loaded = Message.from_dict(data)

    assert loaded.role == "user"
    assert RECALLED_MEMORY_MARKER in loaded.get_text()
    assert is_recalled_memory_message(loaded) is True
    assert get_recalled_memory_files(loaded) == ["ros-yaml.md"]
    assert loaded.to_api_format() == {"role": "user", "content": loaded.content}


def test_non_memory_message_has_no_recalled_files():
    msg = Message(role="user", content="hello")

    assert is_recalled_memory_message(msg) is False
    assert get_recalled_memory_files(msg) == []
