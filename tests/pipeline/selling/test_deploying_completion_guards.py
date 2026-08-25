from __future__ import annotations

from pathlib import Path

import pytest

from iac_code.pipeline.engine.complete_step_tool import CompleteStepTool
from iac_code.pipeline.engine.loader import load_pipeline_dir
from iac_code.pipeline.engine.types import StepConfig
from iac_code.tools.base import ToolContext


def _selling_dir() -> Path:
    return Path(__file__).resolve().parents[3] / "src" / "iac_code" / "pipeline" / "selling"


def _deploying_step():
    loaded = load_pipeline_dir(_selling_dir())
    return next(step for step in loaded.steps if step.step_id == "deploying")


def _deploying_tool(records: list[dict], *, user_message: str = "") -> CompleteStepTool:
    step = _deploying_step()
    return CompleteStepTool(
        StepConfig(
            step_id=step.step_id,
            conclusion_field=step.conclusion_field,
            forward=step.forward,
            conclusion_schema=step.conclusion_schema,
        ),
        completion_guards=step.completion_guards,
        completion_guard_state={
            "successful_tools": set(),
            "tool_results": {},
            "tool_result_records": list(records),
        },
        user_message=user_message,
    )


def _edit_file_record(file_path: str = "templates/sae.yml") -> dict:
    return {
        "tool_name": "edit_file",
        "input": {"file_path": file_path},
        "result": {"file_path": file_path},
        "is_error": False,
    }


def _validate_template_record(file_path: str = "templates/sae.yml") -> dict:
    return {
        "tool_name": "ros_validate_template",
        "input": {"template_url": file_path},
        "result": {"Description": "Valid"},
        "is_error": False,
    }


def _create_failed_record(action: str = "create", stack_id: str = "stack-sae-1") -> dict:
    return {
        "tool_name": "ros_deploy",
        "input": {"action": action, "stack_id": stack_id},
        "result": {
            "stack_id": stack_id,
            "status": "CREATE_FAILED",
            "status_reason": "ALIYUN::SAE::Application CREATE_FAILED",
            "is_success": False,
        },
        "is_error": True,
    }


def test_deploying_guards_cover_success_failed_and_cancelled() -> None:
    guards = _deploying_step().completion_guards

    assert [guard["when_conclusion_field_equals"]["status"] for guard in guards] == [
        "success",
        "failed",
        "cancelled",
    ]
    assert [guard["message_key"] for guard in guards] == [
        "deploy_wait_create_complete",
        "deploy_retry_after_template_fix",
        "deploy_cancel_requires_user_request",
    ]
    for guard in guards[1:]:
        requirement = guard["require_tool_result"]
        assert requirement["tool"] == "ros_deploy"
        assert requirement["allow_error_result"] is True
        assert requirement["latest_match"] is True
        assert guard["when_tool_result_exists"]["tools"] == ["edit_file", "ros_validate_template"]


@pytest.mark.asyncio
async def test_failed_conclusion_after_edit_and_revalidate_requires_redeploy() -> None:
    tool = _deploying_tool(
        [
            _create_failed_record(),
            _edit_file_record(),
            _validate_template_record(),
        ]
    )

    result = await tool.execute(
        tool_input={"conclusion": {"status": "failed", "error": "CREATE_FAILED"}},
        context=ToolContext(),
    )

    assert result.is_error
    assert "ros_deploy" in result.content


@pytest.mark.asyncio
async def test_failed_conclusion_accepted_after_redeploy_attempt() -> None:
    tool = _deploying_tool(
        [
            _create_failed_record(),
            _edit_file_record(),
            _validate_template_record(),
            _create_failed_record(action="continue_create"),
        ]
    )

    result = await tool.execute(
        tool_input={"conclusion": {"status": "failed", "error": "SAE quota exhausted after retry"}},
        context=ToolContext(),
    )

    assert not result.is_error


@pytest.mark.asyncio
async def test_failed_conclusion_without_template_repair_is_not_blocked() -> None:
    tool = _deploying_tool([])

    result = await tool.execute(
        tool_input={"conclusion": {"status": "failed", "error": "no permission to call SAE"}},
        context=ToolContext(),
    )

    assert not result.is_error


@pytest.mark.asyncio
async def test_cancelled_conclusion_after_template_repair_is_rejected() -> None:
    tool = _deploying_tool([_create_failed_record(), _edit_file_record(), _validate_template_record()])

    result = await tool.execute(
        tool_input={"conclusion": {"status": "cancelled"}},
        context=ToolContext(),
    )

    assert result.is_error


@pytest.mark.asyncio
async def test_cancelled_conclusion_allowed_when_user_cancelled() -> None:
    tool = _deploying_tool(
        [_create_failed_record(), _edit_file_record(), _validate_template_record()],
        user_message="先取消部署，我再想想",
    )

    result = await tool.execute(
        tool_input={"conclusion": {"status": "cancelled"}},
        context=ToolContext(),
    )

    assert not result.is_error


@pytest.mark.asyncio
async def test_success_conclusion_still_requires_create_complete() -> None:
    tool = _deploying_tool([_create_failed_record(), _edit_file_record(), _validate_template_record()])

    result = await tool.execute(
        tool_input={"conclusion": {"status": "success", "stack_id": "stack-sae-1"}},
        context=ToolContext(),
    )

    assert result.is_error
    assert "CREATE_COMPLETE" in result.content
