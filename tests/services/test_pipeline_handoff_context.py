import json

import pytest

from iac_code.agent.message import Message
from iac_code.pipeline.engine.handoff import build_handoff_summary
from iac_code.services.context_manager import ContextManager
from iac_code.services.pipeline_handoff_context import (
    fold_pipeline_user_history,
    include_original_user_request,
    pipeline_handoff_message,
)
from iac_code.services.session_storage import PIPELINE_USER_HISTORY_KEY, PipelineUserHistoryRecord


def _input(text, message_id="input-1"):
    return PipelineUserHistoryRecord(message_id, "context-1", "task-1", text).message()


@pytest.mark.parametrize("tool_confirmation", ["true", "false"])
def test_original_request_is_complete_and_handoff_format_and_safety_are_unchanged(monkeypatch, tool_confirmation):
    monkeypatch.setenv("IAC_CODE_HANDOFF_USE_TOOL_CONFIRMATION", tool_confirmation)
    original = '创建资源。\n保留全部文字、引号 " 和换行。'
    inputs = [_input(original), _input("后续补充条件", "input-2")]
    summary = build_handoff_summary(
        "selling", "completed", {"selection": {"status": "selected"}}, ["selection", "missing"]
    )
    enriched = include_original_user_request(summary, inputs)
    marker = "Included context:\n"
    old_prefix, old_tail = summary.split(marker)
    new_prefix, new_tail = enriched.split(marker)
    old_data, old_end = json.JSONDecoder().raw_decode(old_tail)
    new_data, new_end = json.JSONDecoder().raw_decode(new_tail)
    assert new_prefix == old_prefix
    assert new_tail[new_end:] == old_tail[old_end:]
    assert new_data == {**old_data, "original_user_request": original + "\n\n后续补充条件"}
    assert include_original_user_request(enriched, inputs) == enriched


def test_only_covered_pipeline_inputs_leave_model_context_and_full_history_is_retained():
    previous = Message(role="user", content="earlier Normal input")
    original = _input("old demand")
    summary = build_handoff_summary("selling", "completed", {}, [])
    handoff = pipeline_handoff_message(summary, [previous, original])
    cleanup = Message(role="user", content="cleanup policy", metadata={"type": "pipeline_cleanup_prompt"})
    new = Message(role="user", content="old demand")  # Same text, a new untagged request.
    later = _input("later Pipeline input", "input-2")
    history = [previous, original, handoff, cleanup, new, later]
    manager = ContextManager("test", "test")
    manager.load_messages(history)
    assert manager.get_messages() == history
    assert manager.get_context_messages() == [previous, handoff, cleanup, new, later]
    assert manager.get_api_messages() == [message.to_api_format() for message in manager.get_context_messages()]
    assert manager.get_messages() == history
    assert manager.get_total_tokens() == manager._system_prompt_tokens + sum(
        message.token_count for message in manager.get_context_messages()
    )


@pytest.mark.parametrize(
    "summary", ["handoff", '[Pipeline Handoff Context]\nIncluded context:\n{"original_user_request":']
)
def test_unstructured_handoff_cannot_hide_user_history(summary):
    original = _input("old demand")
    handoff = pipeline_handoff_message(summary, [original])
    assert not handoff.metadata
    assert fold_pipeline_user_history([original, handoff]) == [original, handoff]


def test_user_text_resembling_handoff_does_not_cover_history():
    original = _input("old demand")
    summary = include_original_user_request(build_handoff_summary("selling", "completed", {}, []), [original])
    fake = Message(role="user", content=summary)
    assert fold_pipeline_user_history([original, fake]) == [original, fake]
    broken = Message(role="user", content="unmatched", metadata={PIPELINE_USER_HISTORY_KEY: {"message_id": []}})
    handoff = pipeline_handoff_message(summary, [original])
    assert fold_pipeline_user_history([original, broken, handoff]) == [broken, handoff]
