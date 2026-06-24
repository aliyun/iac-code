from __future__ import annotations

import json
import logging

from iac_code.agent.message import Message, TextBlock, ToolResultBlock, ToolUseBlock
from iac_code.pipeline.engine import completion_guard_state
from iac_code.pipeline.engine.completion_guard_state import record_completion_guard_tool_result
from iac_code.pipeline.engine.recovery import (
    last_successful_tool_input,
    reconstruct_completion_guard_state,
    reconstruct_step_result,
)
from iac_code.pipeline.engine.types import StepStatus


def test_reconstruct_step_result_from_successful_complete_step():
    messages = [
        Message(role="user", content="start"),
        Message(
            role="assistant",
            content=[
                TextBlock(text="done"),
                ToolUseBlock(
                    id="tu_complete",
                    name="complete_step",
                    input={
                        "conclusion": {"is_infra_intent": True, "confidence": "high"},
                        "rollback_request": {"target_step": "intent_parsing", "reason": "needs revision"},
                    },
                ),
            ],
        ),
        Message(role="user", content=[ToolResultBlock(tool_use_id="tu_complete", content="ok", is_error=False)]),
    ]

    result = reconstruct_step_result(messages, "architecture_design")

    assert result is not None
    assert result.step_id == "architecture_design"
    assert result.status == StepStatus.COMPLETED
    assert result.conclusion == {"is_infra_intent": True, "confidence": "high"}
    assert result.rollback_request == ("intent_parsing", "needs revision")


def test_reconstruct_step_result_ignores_error_tool_result():
    messages = [
        Message(
            role="assistant",
            content=[
                ToolUseBlock(
                    id="tu_complete",
                    name="complete_step",
                    input={"conclusion": {"ok": True}},
                )
            ],
        ),
        Message(role="user", content=[ToolResultBlock(tool_use_id="tu_complete", content="bad", is_error=True)]),
    ]

    assert reconstruct_step_result(messages, "intent_parsing") is None


def test_reconstruct_step_result_uses_last_successful_complete_step():
    messages = [
        Message(
            role="assistant",
            content=[
                ToolUseBlock(id="tu_old", name="complete_step", input={"conclusion": {"value": "old"}}),
                ToolUseBlock(id="tu_new", name="complete_step", input={"conclusion": {"value": "new"}}),
            ],
        ),
        Message(
            role="user",
            content=[
                ToolResultBlock(tool_use_id="tu_old", content="ok", is_error=False),
                ToolResultBlock(tool_use_id="tu_new", content="ok", is_error=False),
            ],
        ),
    ]

    result = reconstruct_step_result(messages, "intent_parsing")

    assert result is not None
    assert result.conclusion == {"value": "new"}


def test_last_successful_tool_input_uses_successful_tool_result_order():
    messages = [
        Message(
            role="assistant",
            content=[
                ToolUseBlock(id="tu_old", name="complete_step", input={"conclusion": {"value": "old"}}),
                ToolUseBlock(id="tu_other", name="ask_user_question", input={"question": "q"}),
                ToolUseBlock(id="tu_new", name="complete_step", input={"conclusion": {"value": "new"}}),
                ToolUseBlock(id="tu_error", name="complete_step", input={"conclusion": {"value": "error"}}),
            ],
        ),
        Message(
            role="user",
            content=[
                ToolResultBlock(tool_use_id="tu_new", content="ok", is_error=False),
                ToolResultBlock(tool_use_id="tu_other", content="ok", is_error=False),
                ToolResultBlock(tool_use_id="tu_error", content="bad", is_error=True),
                ToolResultBlock(tool_use_id="tu_old", content="ok", is_error=False),
            ],
        ),
    ]

    assert last_successful_tool_input(messages, "complete_step") == {"conclusion": {"value": "old"}}


def test_reconstruct_completion_guard_state_from_ask_user_question():
    messages = [
        Message(
            role="assistant",
            content=[ToolUseBlock(id="tu_question", name="ask_user_question", input={"question": "q", "options": []})],
        ),
        Message(
            role="user",
            content=[
                ToolResultBlock(
                    tool_use_id="tu_question",
                    content='{"selected_id": "deploy", "selected_label": "Deploy", "free_text": "cn-hangzhou"}',
                    is_error=False,
                )
            ],
        ),
    ]

    state = reconstruct_completion_guard_state(messages)

    assert state["successful_tools"] == {"ask_user_question"}
    assert state["tool_results"]["ask_user_question"] == {
        "selected_id": "deploy",
        "selected_label": "Deploy",
        "free_text": "cn-hangzhou",
    }


def test_reconstruct_completion_guard_state_ignores_failed_tools():
    messages = [
        Message(
            role="assistant",
            content=[ToolUseBlock(id="tu_question", name="ask_user_question", input={"question": "q", "options": []})],
        ),
        Message(
            role="user",
            content=[ToolResultBlock(tool_use_id="tu_question", content="cancelled", is_error=True)],
        ),
    ]

    state = reconstruct_completion_guard_state(messages)

    assert state["successful_tools"] == set()
    assert state["tool_results"] == {}


def test_reconstruct_completion_guard_state_ignores_successful_non_guard_tools():
    messages = [
        Message(
            role="assistant",
            content=[ToolUseBlock(id="tu_complete", name="complete_step", input={"conclusion": {"ok": True}})],
        ),
        Message(role="user", content=[ToolResultBlock(tool_use_id="tu_complete", content="ok", is_error=False)]),
    ]

    state = reconstruct_completion_guard_state(messages)

    assert state["successful_tools"] == set()
    assert state["tool_results"] == {}


def test_reconstruct_completion_guard_state_records_ros_stack_results_for_completion_guards():
    messages = [
        Message(
            role="assistant",
            content=[
                ToolUseBlock(
                    id="tu_stack",
                    name="ros_stack",
                    input={"action": "CreateStack", "params": {"StackName": "demo"}},
                )
            ],
        ),
        Message(
            role="user",
            content=[
                ToolResultBlock(
                    tool_use_id="tu_stack",
                    content=json.dumps(
                        {
                            "stack_id": "stack-123",
                            "stack_name": "demo",
                            "status": "CREATE_COMPLETE",
                            "is_success": True,
                        }
                    ),
                    is_error=False,
                )
            ],
        ),
    ]

    state = reconstruct_completion_guard_state(messages)

    assert state["successful_tools"] == {"ros_stack"}
    assert state["tool_results"]["ros_stack"]["stack_id"] == "stack-123"
    assert state["tool_result_records"] == [
        {
            "tool_name": "ros_stack",
            "input": {"action": "CreateStack", "params": {"StackName": "demo"}},
            "result": {
                "stack_id": "stack-123",
                "stack_name": "demo",
                "status": "CREATE_COMPLETE",
                "is_success": True,
            },
            "is_error": False,
        }
    ]


def test_completion_guard_state_logs_json_parse_failures(caplog):
    caplog.set_level(logging.WARNING, logger="iac_code.pipeline.engine.completion_guard_state")
    state = {}

    record_completion_guard_tool_result(
        state,
        tool_name="ros_stack",
        tool_input={"action": "CreateStack"},
        content="{not-json",
        is_error=False,
    )

    assert "Failed to parse completion guard state" in caplog.text


def test_completion_guard_state_logs_rebuild_failures(monkeypatch, caplog):
    caplog.set_level(logging.WARNING, logger="iac_code.pipeline.engine.completion_guard_state")

    def fail_record(*_args, **_kwargs):
        raise RuntimeError("boom")

    monkeypatch.setattr(completion_guard_state, "_record_ask_user_question", fail_record)

    record_completion_guard_tool_result(
        {},
        tool_name="ask_user_question",
        tool_input={},
        content='{"free_text": "ok"}',
        is_error=False,
    )

    assert "Failed to rebuild completion guard state" in caplog.text
