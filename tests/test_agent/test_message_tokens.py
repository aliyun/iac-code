"""Tests for Message token_count field and Conversation helpers."""

from iac_code.agent.message import Conversation, Message


def test_message_has_token_count():
    msg = Message(role="user", content="Hello")
    assert msg.token_count == 0  # Default
    msg.token_count = 42
    assert msg.token_count == 42


def test_conversation_get_total_tokens():
    conv = Conversation()
    m1 = conv.add_user_message("Hello")
    m1.token_count = 10
    m2 = conv.add_assistant_message("Hi")
    m2.token_count = 5
    assert conv.get_total_tokens() == 15


def test_conversation_replace_messages():
    conv = Conversation()
    conv.add_user_message("old")
    new_msg = Message(role="user", content="new", token_count=20)
    conv.replace_messages([new_msg])
    assert len(conv.messages) == 1
    assert conv.messages[0].content == "new"
