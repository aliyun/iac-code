from __future__ import annotations

import json
import logging

import pytest

from iac_code.agent.message import Message, TextBlock, ToolResultBlock, ToolUseBlock
from iac_code.pipeline.engine import completion_guard_state
from iac_code.pipeline.engine.complete_step_tool import CompleteStepTool
from iac_code.pipeline.engine.completion_guard_state import record_completion_guard_tool_result
from iac_code.pipeline.engine.recovery import (
    last_successful_tool_input,
    reconstruct_completion_guard_state,
    reconstruct_step_result,
)
from iac_code.pipeline.engine.step_executor import _completion_guard_tool_result_content
from iac_code.pipeline.engine.types import StepConfig, StepStatus
from iac_code.tools.base import ToolContext
from iac_code.tools.result_storage import EXTERNALIZED_RESULT_PATH_METADATA_KEY, ResultStorage
from iac_code.types.stream_events import ToolResultEvent


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


def test_reconstruct_completion_guard_state_records_ros_deploy_results_for_completion_guards():
    messages = [
        Message(
            role="assistant",
            content=[
                ToolUseBlock(
                    id="tu_deploy",
                    name="ros_deploy",
                    input={"action": "delete_and_create", "stack_id": "stack-old"},
                )
            ],
        ),
        Message(
            role="user",
            content=[
                ToolResultBlock(
                    tool_use_id="tu_deploy",
                    content=json.dumps(
                        {
                            "stack_id": "stack-new",
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

    assert state["successful_tools"] == {"ros_deploy"}
    assert state["tool_results"]["ros_deploy"]["stack_id"] == "stack-new"
    assert state["tool_result_records"] == [
        {
            "tool_name": "ros_deploy",
            "input": {"action": "delete_and_create", "stack_id": "stack-old"},
            "result": {
                "stack_id": "stack-new",
                "stack_name": "demo",
                "status": "CREATE_COMPLETE",
                "is_success": True,
            },
            "is_error": False,
        }
    ]


def test_reconstruct_completion_guard_state_records_structured_tool_results_for_completion_guards():
    messages = [
        Message(
            role="assistant",
            content=[
                ToolUseBlock(
                    id="tu_scan",
                    name="infraguard_scan",
                    input={"file_path": "template.yaml", "blocking_severities": ["high"]},
                )
            ],
        ),
        Message(
            role="user",
            content=[
                ToolResultBlock(
                    tool_use_id="tu_scan",
                    content=json.dumps(
                        {
                            "file_path": "template.yaml",
                            "passed": True,
                            "blocking_findings": 0,
                            "summary": {},
                        }
                    ),
                    is_error=False,
                )
            ],
        ),
    ]

    state = reconstruct_completion_guard_state(messages)

    assert state["successful_tools"] == {"infraguard_scan"}
    assert state["tool_results"]["infraguard_scan"]["passed"] is True
    assert state["tool_result_records"] == [
        {
            "tool_name": "infraguard_scan",
            "input": {"file_path": "template.yaml", "blocking_severities": ["high"]},
            "result": {
                "file_path": "template.yaml",
                "passed": True,
                "blocking_findings": 0,
                "summary": {},
            },
            "is_error": False,
        }
    ]


def test_reconstruct_completion_guard_state_reads_externalized_tool_result_metadata(tmp_path):
    stored_result_path = tmp_path / "scan.json"
    payload = {
        "file_path": "template.yaml",
        "passed": True,
        "blocking_findings": 0,
        "file_content": "x" * 60_000,
        "file_sha256": "abc123",
    }
    stored_result_path.write_text(json.dumps(payload), encoding="utf-8")
    messages = [
        Message(
            role="assistant",
            content=[
                ToolUseBlock(
                    id="tu_scan",
                    name="infraguard_scan",
                    input={"file_path": "template.yaml", "include_file_content": True},
                )
            ],
        ),
        Message(
            role="user",
            content=[
                ToolResultBlock(
                    tool_use_id="tu_scan",
                    content='{"file_path": "template.yaml"',
                    metadata={EXTERNALIZED_RESULT_PATH_METADATA_KEY: str(stored_result_path)},
                    is_error=False,
                )
            ],
        ),
    ]

    state = reconstruct_completion_guard_state(messages)

    assert state["successful_tools"] == {"infraguard_scan"}
    assert state["tool_results"]["infraguard_scan"] == payload


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "tool_name",
    [
        "aliyun_api",
        "ros_validate_template",
        "ros_get_template_parameter_constraints",
        "ros_preview_template",
        "ros_estimate_template_cost",
    ],
)
@pytest.mark.parametrize("state_source", ["live", "resume"])
@pytest.mark.parametrize("externalized", [False, True])
@pytest.mark.parametrize(
    ("contract_case", "required_fields", "expected_success"),
    [
        ("future_body", {"ready": True, "resource_id": "resource-1"}, True),
        ("old_nested_body", {"ready": True, "resource_id": "resource-1"}, False),
        ("old_top_level", {"status": 200}, True),
    ],
)
async def test_aliyun_completion_guard_contract_for_live_and_resume(
    tmp_path,
    tool_name,
    state_source,
    externalized,
    contract_case,
    required_fields,
    expected_success,
):
    business = {"ready": True, "resource_id": "resource-1"}
    if externalized:
        business["padding"] = "X" * 50_000
    if contract_case == "future_body":
        content = json.dumps(business)
        metadata = {"aliyun_http": {"contract_version": "aliyun_body_v1"}}
    else:
        content = json.dumps(
            {
                "status": 200,
                "headers": {},
                "body": business,
                "content_type": "application/json",
                "content_encoding": None,
                "size": len(json.dumps(business)),
            }
        )
        metadata = {}

    storage = ResultStorage(
        storage_dir=str(tmp_path / "tool-results"),
        max_inline_chars=50_000,
        preview_chars=2_000,
    )
    processed = storage.process("tool-1", content)
    if processed.is_externalized:
        metadata[EXTERNALIZED_RESULT_PATH_METADATA_KEY] = processed.file_path
    if state_source == "live":
        event = ToolResultEvent(
            tool_use_id="tool-1",
            tool_name=tool_name,
            result=processed.content,
            metadata=metadata,
        )
        state = {}
        record_completion_guard_tool_result(
            state,
            tool_name=tool_name,
            tool_input={},
            content=_completion_guard_tool_result_content(event),
            is_error=False,
        )
    else:
        state = reconstruct_completion_guard_state(
            [
                Message(
                    role="assistant",
                    content=[ToolUseBlock(id="tool-1", name=tool_name, input={})],
                ),
                Message(
                    role="user",
                    content=[
                        ToolResultBlock(
                            tool_use_id="tool-1",
                            content=processed.content,
                            metadata=metadata,
                        )
                    ],
                ),
            ]
        )

    tool = CompleteStepTool(
        StepConfig(step_id="reviewing", conclusion_field="result", forward=None),
        completion_guards=[
            {
                "when_conclusion_field_equals": {"done": True},
                "require_tool_result": {
                    "tool": tool_name,
                    "result_field_equals": required_fields,
                },
            }
        ],
        completion_guard_state=state,
    )

    result = await tool.execute(tool_input={"conclusion": {"done": True}}, context=ToolContext())

    assert processed.is_externalized is externalized
    assert (not result.is_error) is expected_success


def test_reconstruct_completion_guard_state_falls_back_when_externalized_tool_result_is_missing(tmp_path, caplog):
    caplog.set_level(logging.WARNING, logger="iac_code.pipeline.engine.recovery")
    missing_result_path = tmp_path / "missing-scan.json"
    messages = [
        Message(
            role="assistant",
            content=[
                ToolUseBlock(
                    id="tu_scan",
                    name="infraguard_scan",
                    input={"file_path": "template.yaml", "include_file_content": True},
                )
            ],
        ),
        Message(
            role="user",
            content=[
                ToolResultBlock(
                    tool_use_id="tu_scan",
                    content='{"file_path": "template.yaml"',
                    metadata={EXTERNALIZED_RESULT_PATH_METADATA_KEY: str(missing_result_path)},
                    is_error=False,
                )
            ],
        ),
    ]

    state = reconstruct_completion_guard_state(messages)

    assert "Failed to read externalized tool result while rebuilding completion guard state" in caplog.text
    assert state["successful_tools"] == set()
    assert state["tool_results"] == {}


def test_reconstruct_completion_guard_state_records_ros_deploy_owned_failed_create_stack():
    messages = [
        Message(
            role="assistant",
            content=[
                ToolUseBlock(
                    id="tu_deploy",
                    name="ros_deploy",
                    input={"action": "create", "stack_name": "demo"},
                )
            ],
        ),
        Message(
            role="user",
            content=[
                ToolResultBlock(
                    tool_use_id="tu_deploy",
                    content=json.dumps(
                        {
                            "stack_id": "stack-failed",
                            "stack_name": "demo",
                            "status": "CREATE_FAILED",
                            "is_success": False,
                        }
                    ),
                    is_error=True,
                )
            ],
        ),
    ]

    state = reconstruct_completion_guard_state(messages)

    assert state["ros_deploy_owned_stack_ids"]["stack-failed"]["action"] == "create"


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


def test_completion_guard_state_does_not_warn_for_plain_text_unstructured_tool_results(caplog):
    caplog.set_level(logging.WARNING, logger="iac_code.pipeline.engine.completion_guard_state")
    state = {}

    record_completion_guard_tool_result(
        state,
        tool_name="read_file",
        tool_input={"path": "/tmp/template.yaml"},
        content="plain template content",
        is_error=False,
    )

    assert "Failed to parse completion guard state" not in caplog.text
    assert state["successful_tools"] == set()
    assert state["tool_results"] == {}


def test_completion_guard_state_records_file_mutations_with_plain_text_results():
    state = {}

    record_completion_guard_tool_result(
        state,
        tool_name="edit_file",
        tool_input={"path": "/tmp/template.yaml"},
        content="Successfully edited /tmp/template.yaml",
        is_error=False,
    )

    assert state["successful_tools"] == {"edit_file"}
    assert state["tool_results"]["edit_file"]["file_path"] == "/tmp/template.yaml"
    assert state["tool_result_records"] == [
        {
            "tool_name": "edit_file",
            "input": {"path": "/tmp/template.yaml"},
            "result": {"file_path": "/tmp/template.yaml"},
            "is_error": False,
        }
    ]


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
