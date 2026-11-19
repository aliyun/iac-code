import pytest

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


def test_reconcile_resume_messages_replaces_synthetic_error_with_durable_success():
    interrupted = Message(
        role="user",
        content=[
            ToolResultBlock(
                tool_use_id="ask-1",
                content="Session interrupted before tool execution completed.",
                is_error=True,
            )
        ],
    )
    answered = Message(
        role="user",
        content=[ToolResultBlock(tool_use_id="ask-1", content='{"free_text":"Node.js API"}')],
    )

    merged = reconcile_resume_messages([interrupted], [answered])

    assert merged == [answered]


def test_user_message_already_in_resume_matches_image_message():
    image_message = [
        TextBlock(text="参考这张图"),
        ImageBlock(media_type="image/png", data="aW1hZ2U="),
    ]

    assert user_message_already_in_resume(image_message, [Message(role="user", content=image_message)]) is True


@pytest.mark.parametrize("structured", [False, True])
def test_user_message_already_in_resume_ignores_runtime_accounting(structured):
    content = [TextBlock(text="购买服务器")] if structured else "购买服务器"
    message = Message(role="user", content=content, token_count=20, elapsed_seconds=1.5)
    original = message.to_dict()

    assert user_message_already_in_resume(content, [message]) is True
    assert user_message_already_in_resume("取消购买", [message]) is False
    assert message.to_dict() == original


def test_reconcile_resume_messages_deduplicates_runtime_accounting_without_losing_original():
    transcript = Message(role="user", content="购买服务器", token_count=20, elapsed_seconds=1.5)
    sidecar = Message(role="user", content="购买服务器")

    assert reconcile_resume_messages([transcript], [sidecar]) == [transcript]
    assert transcript.token_count == 20 and transcript.elapsed_seconds == 1.5


@pytest.mark.parametrize("status", ["pending", "ready", "skipped"])
@pytest.mark.parametrize("structured", [False, True])
def test_user_message_already_in_resume_ignores_wiki_search_state(status, structured):
    content = [TextBlock(text="B70 availability")] if structured else "B70 availability"
    message = Message(
        role="user",
        content=content,
        metadata={"wiki_auto_search": {"query_id": "q1", "status": status}},
    )
    original = message.to_dict()

    assert user_message_already_in_resume(content, [message]) is True
    assert message.to_dict() == original


@pytest.mark.parametrize("states", [("ready", "pending"), ("pending", "ready"), ("ready", None)])
def test_reconcile_resume_messages_deduplicates_wiki_state_without_merging_it(states):
    transcript_status, sidecar_status = states
    transcript = Message(
        role="user",
        content="B70 availability",
        metadata={"wiki_auto_search": {"query_id": "q1", "status": transcript_status}},
    )
    sidecar = Message(
        role="user",
        content="B70 availability",
        metadata={"wiki_auto_search": {"query_id": "q1", "status": sidecar_status}} if sidecar_status else {},
    )
    original_transcript = transcript.to_dict()
    original_sidecar = sidecar.to_dict()

    merged = reconcile_resume_messages([transcript], [sidecar])

    assert merged == [transcript]
    assert transcript.to_dict() == original_transcript
    assert sidecar.to_dict() == original_sidecar


def test_reconcile_resume_messages_preserves_other_input_identity_metadata():
    transcript = Message(
        role="user",
        content="B70 availability",
        metadata={"messageId": "input-1", "wiki_auto_search": {"query_id": "q1", "status": "ready"}},
    )
    sidecar = Message(
        role="user",
        content="B70 availability",
        metadata={"messageId": "input-2", "wiki_auto_search": {"query_id": "q2", "status": "pending"}},
    )

    assert reconcile_resume_messages([transcript], [sidecar]) == [transcript, sidecar]


def test_user_message_already_in_resume_still_compares_other_metadata():
    message = Message(
        role="user",
        content="B70 availability",
        metadata={"messageId": "input-1", "wiki_auto_search": {"query_id": "q1", "status": "ready"}},
    )

    assert user_message_already_in_resume("B70 availability", [message]) is False


@pytest.mark.parametrize("structured", [False, True])
def test_restored_input_deduplication_excludes_the_retained_history(structured):
    content = [TextBlock(text="Explain this again")] if structured else "Explain this again"
    previous = Message(role="user", content=content, token_count=10)
    history = [previous, Message(role="assistant", content="Earlier answer")]
    assert not user_message_already_in_resume(content, history, start_index=len(history))
    current = Message(role="user", content=content, metadata={"wiki_auto_search": {"status": "pending"}})
    assert user_message_already_in_resume(content, [*history, current], start_index=len(history))


@pytest.mark.parametrize("boundary_retained", [False, True])
def test_restored_input_deduplication_survives_compacted_history(boundary_retained):
    current = Message(role="user", content="Explain this again", token_count=10)
    messages = [Message(role="user", content="Compacted history")]
    if boundary_retained:
        messages += [current, Message(role="user", content=[ToolResultBlock(tool_use_id="prior", content="done")])]
        assert not user_message_already_in_resume(
            current.content, messages, start_index=50, start_after_tool_id="prior",
        )
    assert user_message_already_in_resume(
        current.content, [*messages, current], start_index=50, start_after_tool_id="prior",
    )
