import asyncio
import json
import os

import pytest

from iac_code.tools.base import ToolContext, ToolResult
from iac_code.types.permissions import PermissionMode, ToolPermissionContext
from iac_code.types.stream_events import StackProgressEvent


def _permission_ctx(*, allow=None, deny=None, mode=PermissionMode.DEFAULT):
    return ToolPermissionContext(
        cwd="/tmp",
        allow_rules=allow or {},
        deny_rules=deny or {},
        mode=mode,
    )


class FakeRosStack:
    def __init__(self, results, progress_events=None):
        self.results = list(results)
        self.progress_events = list(progress_events or [])
        self.calls = []
        self.wait_calls = []

    async def _emit_next_progress(self, context):
        if context.event_queue is not None and self.progress_events:
            await context.event_queue.put(self.progress_events.pop(0))

    async def execute(self, *, tool_input, context):
        self.calls.append((tool_input, context))
        await self._emit_next_progress(context)
        if not self.results:
            raise AssertionError("unexpected ros stack call")
        return self.results.pop(0)

    async def wait_for_stack_operation(self, action, params, region, stack_id, context):
        self.wait_calls.append((action, params, region, stack_id, context))
        await self._emit_next_progress(context)
        if not self.results:
            raise AssertionError("unexpected ros stack wait call")
        return self.results.pop(0)


def _deploy_tool(monkeypatch, *, guard_state=None, results=None, progress_events=None):
    from iac_code.pipeline.selling.tools.ros_deploy_tool import RosDeployTool

    fake_stack = FakeRosStack(results or [], progress_events=progress_events)
    tool = RosDeployTool(completion_guard_state=guard_state if guard_state is not None else {})
    monkeypatch.setattr(tool, "_new_stack_tool", lambda: fake_stack)
    return tool, fake_stack


@pytest.mark.asyncio
async def test_create_records_failed_stack_as_owned_for_recovery(monkeypatch):
    guard_state = {}
    tool, fake_stack = _deploy_tool(
        monkeypatch,
        guard_state=guard_state,
        results=[
            ToolResult.error(
                json.dumps(
                    {
                        "stack_id": "stack-failed",
                        "stack_name": "demo",
                        "status": "CREATE_FAILED",
                        "is_success": False,
                    }
                )
            )
        ],
    )

    result = await tool.execute(
        tool_input={
            "action": "create",
            "stack_name": "demo",
            "template_url": "templates/demo.yml",
            "parameters": {"ZoneId": "cn-hangzhou-k"},
            "region_id": "cn-hangzhou",
        },
        context=ToolContext(cwd="/workspace", pipeline_mode=True),
    )

    assert result.is_error is True
    assert guard_state["ros_deploy_owned_stack_ids"]["stack-failed"]["action"] == "create"
    assert fake_stack.calls[0][0] == {
        "action": "CreateStack",
        "params": {
            "StackName": "demo",
            "DisableRollback": True,
            "TemplateURL": os.path.realpath("/workspace/templates/demo.yml"),
            "Parameters": {"ZoneId": "cn-hangzhou-k"},
        },
        "region_id": "cn-hangzhou",
    }


@pytest.mark.asyncio
async def test_create_preserves_pipeline_mode_for_internal_ros_stack(monkeypatch):
    tool, fake_stack = _deploy_tool(
        monkeypatch,
        results=[ToolResult.success(json.dumps({"stack_id": "stack-new", "is_success": True}))],
    )

    result = await tool.execute(
        tool_input={
            "action": "create",
            "stack_name": "demo",
            "template_url": "templates/demo.yml",
            "region_id": "cn-hangzhou",
        },
        context=ToolContext(cwd="/workspace", pipeline_mode=True),
    )

    assert result.is_error is False
    assert fake_stack.calls[0][1].pipeline_mode is True


def test_internal_ros_stack_allows_deployment_guard_without_clearing_pipeline_mode():
    from iac_code.pipeline.selling.tools.ros_deploy_tool import RosDeployTool

    stack = RosDeployTool()._new_stack_tool()
    kwargs = stack._call_action_kwargs(ToolContext(cwd="/workspace", pipeline_mode=True))

    assert kwargs["pipeline_mode"] is True
    assert kwargs["allow_pipeline_deployment_actions"] is True


def test_ros_deploy_uses_long_running_stack_timeout():
    from iac_code.pipeline.selling.tools.ros_deploy_tool import RosDeployTool

    assert RosDeployTool().timeout == 3600.0


@pytest.mark.parametrize(
    ("tool_input", "expected_fields"),
    [
        (
            {
                "action": "wait",
                "stack_id": "stack-slow",
                "stack_name": "demo",
                "template_url": "templates/demo.yml",
                "parameters": {"ZoneId": "cn-hangzhou-k"},
            },
            ("parameters", "stack_name", "template_url"),
        ),
        (
            {
                "action": "create",
                "stack_id": "stack-existing",
                "stack_name": "demo",
                "template_url": "templates/demo.yml",
            },
            ("stack_id",),
        ),
        (
            {
                "action": "continue_create",
                "stack_id": "stack-failed",
                "stack_name": "demo",
                "template_url": "templates/demo.yml",
            },
            ("stack_name",),
        ),
    ],
)
def test_ros_deploy_validate_input_rejects_fields_not_used_by_action(tool_input, expected_fields):
    from iac_code.pipeline.selling.tools.ros_deploy_tool import RosDeployTool

    valid, error = RosDeployTool().validate_input(tool_input)

    assert valid is False
    assert "not supported for action" in error
    for field in expected_fields:
        assert field in error


@pytest.mark.parametrize(
    ("tool_input", "expected_fields"),
    [
        ({"action": "create", "template_url": "templates/demo.yml"}, ("stack_name",)),
        ({"action": "create", "stack_name": "demo"}, ("template_url",)),
        ({"action": "continue_create", "template_url": "templates/demo.yml"}, ("stack_id",)),
        ({"action": "continue_create", "stack_id": "stack-failed"}, ("template_url",)),
        ({"action": "delete_and_create", "stack_name": "demo", "template_url": "templates/demo.yml"}, ("stack_id",)),
        ({"action": "wait"}, ("stack_id",)),
    ],
)
def test_ros_deploy_validate_input_requires_action_fields(tool_input, expected_fields):
    from iac_code.pipeline.selling.tools.ros_deploy_tool import RosDeployTool

    valid, error = RosDeployTool().validate_input(tool_input)

    assert valid is False
    assert "Missing required field" in error
    for field in expected_fields:
        assert field in error


@pytest.mark.asyncio
async def test_create_records_started_stack_as_owned_when_polling_fails(monkeypatch):
    guard_state = {}
    tool, _fake_stack = _deploy_tool(
        monkeypatch,
        guard_state=guard_state,
        results=[
            ToolResult(
                content="[GetStackStatus] status service unavailable",
                is_error=True,
                metadata={
                    "provider": "ros",
                    "action": "CreateStack",
                    "stack_id": "stack-started",
                    "stack_name": "demo",
                    "region_id": "cn-hangzhou",
                    "error_stage": "status",
                },
            )
        ],
    )

    result = await tool.execute(
        tool_input={
            "action": "create",
            "stack_name": "demo",
            "template_url": "templates/demo.yml",
            "region_id": "cn-hangzhou",
        },
        context=ToolContext(cwd="/workspace", pipeline_mode=True),
    )

    assert result.is_error is True
    assert guard_state["ros_deploy_owned_stack_ids"]["stack-started"]["action"] == "create"
    permission = await tool.check_permissions(
        {"action": "continue_create", "stack_id": "stack-started"},
        _permission_ctx(allow={"session": ["ros_deploy(continue_create:stack-started)"]}),
    )
    assert permission.behavior == "allow"


@pytest.mark.asyncio
async def test_continue_create_defaults_to_recreate_with_auto_recreating_resources(monkeypatch):
    guard_state = {"ros_deploy_owned_stack_ids": {"stack-failed": {"action": "create"}}}
    tool, fake_stack = _deploy_tool(
        monkeypatch,
        guard_state=guard_state,
        results=[
            ToolResult.success(
                json.dumps(
                    {
                        "stack_id": "stack-failed",
                        "stack_name": "demo",
                        "status": "CREATE_COMPLETE",
                        "is_success": True,
                    }
                )
            )
        ],
    )

    result = await tool.execute(
        tool_input={
            "action": "continue_create",
            "stack_id": "stack-failed",
            "template_url": "templates/demo.yml",
            "parameters": {"ZoneId": "cn-hangzhou-k"},
            "region_id": "cn-hangzhou",
        },
        context=ToolContext(cwd="/workspace", pipeline_mode=True),
    )

    assert result.is_error is False
    assert fake_stack.calls[0][0] == {
        "action": "ContinueCreateStack",
        "params": {
            "StackId": "stack-failed",
            "Mode": "Recreate",
            "RecreatingOptions": ["AutoRecreatingResources"],
            "TemplateURL": os.path.realpath("/workspace/templates/demo.yml"),
            "Parameters": {"ZoneId": "cn-hangzhou-k"},
        },
        "region_id": "cn-hangzhou",
    }


@pytest.mark.asyncio
async def test_wait_action_polls_existing_stack_without_starting_lifecycle_action(monkeypatch):
    tool, fake_stack = _deploy_tool(
        monkeypatch,
        results=[
            ToolResult.success(
                json.dumps(
                    {
                        "stack_id": "stack-slow",
                        "stack_name": "demo",
                        "status": "CREATE_COMPLETE",
                        "is_success": True,
                    }
                )
            )
        ],
    )

    result = await tool.execute(
        tool_input={
            "action": "wait",
            "stack_id": "stack-slow",
            "region_id": "cn-hangzhou",
        },
        context=ToolContext(cwd="/workspace", pipeline_mode=True),
    )

    assert result.is_error is False
    assert fake_stack.calls == []
    assert fake_stack.wait_calls[0][:4] == (
        "CreateStack",
        {"StackId": "stack-slow"},
        "cn-hangzhou",
        "stack-slow",
    )


@pytest.mark.asyncio
async def test_wait_action_rejects_create_only_fields_without_polling(monkeypatch):
    tool, fake_stack = _deploy_tool(monkeypatch, results=[])

    result = await tool.execute(
        tool_input={
            "action": "wait",
            "stack_id": "stack-slow",
            "stack_name": "demo",
            "template_url": "templates/demo.yml",
            "parameters": {"ZoneId": "cn-hangzhou-k"},
        },
        context=ToolContext(cwd="/workspace", pipeline_mode=True),
    )

    assert result.is_error is True
    assert "not supported for action" in result.content
    assert "parameters" in result.content
    assert "stack_name" in result.content
    assert "template_url" in result.content
    assert fake_stack.calls == []
    assert fake_stack.wait_calls == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("tool_input", "guard_state", "expected_progress_stack_ids", "uses_wait"),
    [
        (
            {"action": "create", "stack_name": "demo", "template_url": "templates/demo.yml"},
            {},
            ["stack-created"],
            False,
        ),
        (
            {"action": "continue_create", "stack_id": "stack-failed", "template_url": "templates/demo.yml"},
            {"ros_deploy_owned_stack_ids": {"stack-failed": {"action": "create"}}},
            ["stack-failed"],
            False,
        ),
        (
            {"action": "wait", "stack_id": "stack-slow", "region_id": "cn-hangzhou"},
            {},
            ["stack-slow"],
            True,
        ),
        (
            {
                "action": "delete_and_create",
                "stack_id": "stack-old",
                "stack_name": "demo",
                "template_url": "templates/demo.yml",
            },
            {"ros_deploy_owned_stack_ids": {"stack-old": {"action": "create"}}},
            ["stack-old", "stack-new"],
            False,
        ),
    ],
)
async def test_ros_deploy_preserves_event_queue_for_all_progress_actions(
    monkeypatch,
    tmp_path,
    tool_input,
    guard_state,
    expected_progress_stack_ids,
    uses_wait,
):
    templates_dir = tmp_path / "templates"
    templates_dir.mkdir()
    (templates_dir / "demo.yml").write_text("ROSTemplateFormatVersion: '2015-09-01'\n", encoding="utf-8")
    progress_events = [
        StackProgressEvent(
            stack_id=stack_id,
            stack_name="demo",
            status="DELETE_COMPLETE" if stack_id == "stack-old" else "CREATE_COMPLETE",
            progress_percentage=100.0,
            resources=[],
            elapsed_seconds=index + 1,
        )
        for index, stack_id in enumerate(expected_progress_stack_ids)
    ]
    results = [
        ToolResult.success(
            json.dumps(
                {
                    "stack_id": stack_id,
                    "stack_name": "demo",
                    "status": "DELETE_COMPLETE" if stack_id == "stack-old" else "CREATE_COMPLETE",
                    "is_success": True,
                }
            )
        )
        for stack_id in expected_progress_stack_ids
    ]
    tool, fake_stack = _deploy_tool(
        monkeypatch,
        guard_state=guard_state,
        results=results,
        progress_events=progress_events,
    )
    event_queue: asyncio.Queue = asyncio.Queue()
    context = ToolContext(cwd=str(tmp_path), pipeline_mode=True, event_queue=event_queue)

    result = await tool.execute(tool_input=tool_input, context=context)

    assert result.is_error is False
    contexts = [call[-1] for call in (fake_stack.wait_calls if uses_wait else fake_stack.calls)]
    assert len(contexts) == len(expected_progress_stack_ids)
    assert all(call_context.event_queue is event_queue for call_context in contexts)
    emitted_events = []
    while not event_queue.empty():
        emitted_events.append(event_queue.get_nowait())
    assert [event.stack_id for event in emitted_events] == expected_progress_stack_ids


@pytest.mark.asyncio
async def test_continue_validation_failure_recommends_delete_and_create_without_running_it(monkeypatch):
    guard_state = {"ros_deploy_owned_stack_ids": {"stack-failed": {"action": "create"}}}
    tool, fake_stack = _deploy_tool(
        monkeypatch,
        guard_state=guard_state,
        results=[ToolResult.error("[ContinueCreateStack] ContinueCreateStackValidationFailed: cannot continue")],
    )

    result = await tool.execute(
        tool_input={
            "action": "continue_create",
            "stack_id": "stack-failed",
            "template_url": "templates/demo.yml",
            "parameters": {"ZoneId": "cn-hangzhou-k"},
        },
        context=ToolContext(cwd="/workspace", pipeline_mode=True),
    )

    assert result.is_error is True
    data = json.loads(result.content)
    assert data["error_code"] == "ContinueCreateStackValidationFailed"
    assert data["recommended_action"] == "delete_and_create"
    assert len(fake_stack.calls) == 1


@pytest.mark.asyncio
async def test_delete_and_create_rejects_stack_not_created_by_current_pipeline(monkeypatch):
    tool, fake_stack = _deploy_tool(monkeypatch, guard_state={}, results=[])

    result = await tool.execute(
        tool_input={
            "action": "delete_and_create",
            "stack_id": "stack-from-list-stacks",
            "stack_name": "demo",
            "template_url": "templates/demo.yml",
        },
        context=ToolContext(cwd="/workspace", pipeline_mode=True),
    )

    assert result.is_error is True
    assert "not created by the current selling deployment step" in result.content
    assert fake_stack.calls == []


@pytest.mark.asyncio
async def test_delete_and_create_deletes_owned_stack_then_creates_replacement(monkeypatch, tmp_path):
    guard_state = {"ros_deploy_owned_stack_ids": {"stack-old": {"action": "create"}}}
    templates_dir = tmp_path / "templates"
    templates_dir.mkdir()
    (templates_dir / "demo.yml").write_text("ROSTemplateFormatVersion: '2015-09-01'\n", encoding="utf-8")
    tool, fake_stack = _deploy_tool(
        monkeypatch,
        guard_state=guard_state,
        results=[
            ToolResult.success(
                json.dumps(
                    {
                        "stack_id": "stack-old",
                        "stack_name": "demo",
                        "status": "DELETE_COMPLETE",
                        "is_success": True,
                    }
                )
            ),
            ToolResult.success(
                json.dumps(
                    {
                        "stack_id": "stack-new",
                        "stack_name": "demo",
                        "status": "CREATE_COMPLETE",
                        "is_success": True,
                    }
                )
            ),
        ],
    )

    result = await tool.execute(
        tool_input={
            "action": "delete_and_create",
            "stack_id": "stack-old",
            "stack_name": "demo",
            "template_url": "templates/demo.yml",
            "parameters": {"ZoneId": "cn-hangzhou-k"},
            "region_id": "cn-hangzhou",
        },
        context=ToolContext(cwd=str(tmp_path), pipeline_mode=True),
    )

    assert result.is_error is False
    assert [call[0]["action"] for call in fake_stack.calls] == ["DeleteStack", "CreateStack"]
    assert guard_state["ros_deploy_owned_stack_ids"]["stack-new"]["action"] == "delete_and_create"
    assert json.loads(result.content)["stack_id"] == "stack-new"


@pytest.mark.asyncio
async def test_delete_and_create_validates_replacement_before_delete(monkeypatch):
    guard_state = {"ros_deploy_owned_stack_ids": {"stack-old": {"action": "create"}}}
    tool, fake_stack = _deploy_tool(monkeypatch, guard_state=guard_state, results=[])

    result = await tool.execute(
        tool_input={
            "action": "delete_and_create",
            "stack_id": "stack-old",
            "stack_name": "demo",
        },
        context=ToolContext(cwd="/workspace", pipeline_mode=True),
    )

    assert result.is_error is True
    assert "template_url" in result.content
    assert fake_stack.calls == []


@pytest.mark.parametrize(
    "parameters",
    [
        {"Password": "***"},
        {"Nested": [{"Password": " [REDACTED] "}]},
        {"Password": "<ReDaCtEd>"},
    ],
)
def test_deployment_actions_reject_recursive_redaction_placeholders(parameters):
    from iac_code.pipeline.selling.tools.ros_deploy_tool import RosDeployTool

    valid, error = RosDeployTool().validate_input(
        {
            "action": "create",
            "stack_name": "demo",
            "template_url": "templates/demo.yml",
            "parameters": parameters,
        }
    )

    assert valid is False
    assert "redaction placeholder" in error
    assert "Password" not in error


@pytest.mark.parametrize("value", ["business***suffix", "prefix [REDACTED] suffix", "<redacted>-label"])
def test_deployment_actions_allow_strings_that_only_contain_placeholder_text(value):
    from iac_code.pipeline.selling.tools.ros_deploy_tool import RosDeployTool

    valid, error = RosDeployTool().validate_input(
        {
            "action": "create",
            "stack_name": "demo",
            "template_url": "templates/demo.yml",
            "parameters": {"Banner": value},
        }
    )

    assert valid is True
    assert error == ""


def test_deployment_actions_do_not_treat_parameter_names_as_values():
    from iac_code.pipeline.selling.tools.ros_deploy_tool import RosDeployTool

    valid, error = RosDeployTool().validate_input(
        {
            "action": "create",
            "stack_name": "demo",
            "template_url": "templates/demo.yml",
            "parameters": {"<ReDaCtEd>": "real-value"},
        }
    )

    assert valid is True
    assert error == ""


@pytest.mark.asyncio
async def test_delete_and_create_rejects_placeholder_before_deleting_owned_stack(monkeypatch, tmp_path):
    guard_state = {"ros_deploy_owned_stack_ids": {"stack-old": {"action": "create"}}}
    templates_dir = tmp_path / "templates"
    templates_dir.mkdir()
    (templates_dir / "demo.yml").write_text("ROSTemplateFormatVersion: '2015-09-01'\n", encoding="utf-8")
    tool, fake_stack = _deploy_tool(monkeypatch, guard_state=guard_state, results=[])

    result = await tool.execute(
        tool_input={
            "action": "delete_and_create",
            "stack_id": "stack-old",
            "stack_name": "demo",
            "template_url": "templates/demo.yml",
            "parameters": {"Database": {"Password": "[REDACTED]"}},
        },
        context=ToolContext(cwd=str(tmp_path), pipeline_mode=True),
    )

    assert result.is_error is True
    assert "redaction placeholder" in result.content
    assert fake_stack.calls == []


@pytest.mark.asyncio
async def test_delete_and_create_rejects_missing_local_template_before_delete(monkeypatch, tmp_path):
    guard_state = {"ros_deploy_owned_stack_ids": {"stack-old": {"action": "create"}}}
    tool, fake_stack = _deploy_tool(monkeypatch, guard_state=guard_state, results=[])

    result = await tool.execute(
        tool_input={
            "action": "delete_and_create",
            "stack_id": "stack-old",
            "stack_name": "demo",
            "template_url": "templates/missing.yml",
        },
        context=ToolContext(cwd=str(tmp_path), pipeline_mode=True),
    )

    assert result.is_error is True
    assert "templates/missing.yml" in result.content
    assert fake_stack.calls == []


@pytest.mark.asyncio
async def test_permission_asks_for_continue_create_stack_scoped_rule(monkeypatch):
    guard_state = {"ros_deploy_owned_stack_ids": {"stack-failed": {"action": "create"}}}
    tool, _fake_stack = _deploy_tool(monkeypatch, guard_state=guard_state)

    result = await tool.check_permissions(
        {"action": "continue_create", "stack_id": "stack-failed"},
        _permission_ctx(),
    )

    assert result.behavior == "ask"
    assert result.suggestions is not None
    assert result.suggestions[0].tool_name == "ros_deploy"
    assert result.suggestions[0].rule_content == "continue_create:stack-failed"
    assert result.suggestions[0].display_text == "Continue ROS stack creation: stack-failed"


@pytest.mark.asyncio
async def test_permission_rule_display_text_uses_friendly_deploy_action_names(monkeypatch):
    guard_state = {"ros_deploy_owned_stack_ids": {"stack-old": {"action": "create"}}}
    tool, _fake_stack = _deploy_tool(monkeypatch, guard_state=guard_state)

    create = await tool.check_permissions(
        {"action": "create", "stack_name": "demo-stack"},
        _permission_ctx(),
    )
    delete_and_create = await tool.check_permissions(
        {"action": "delete_and_create", "stack_id": "stack-old"},
        _permission_ctx(),
    )

    assert create.suggestions is not None
    assert create.suggestions[0].rule_content == "create:demo-stack"
    assert create.suggestions[0].display_text == "Create ROS stack: demo-stack"
    assert delete_and_create.suggestions is not None
    assert delete_and_create.suggestions[0].rule_content == "delete_and_create:stack-old"
    assert delete_and_create.suggestions[0].display_text == "Delete failed ROS stack and create replacement: stack-old"


@pytest.mark.asyncio
async def test_permission_allows_matching_continue_create_stack_rule(monkeypatch):
    guard_state = {"ros_deploy_owned_stack_ids": {"stack-failed": {"action": "create"}}}
    tool, _fake_stack = _deploy_tool(monkeypatch, guard_state=guard_state)

    result = await tool.check_permissions(
        {"action": "continue_create", "stack_id": "stack-failed"},
        _permission_ctx(allow={"session": ["ros_deploy(continue_create:stack-failed)"]}),
    )

    assert result.behavior == "allow"


@pytest.mark.asyncio
async def test_permission_allows_wait_without_stack_ownership(monkeypatch):
    tool, _fake_stack = _deploy_tool(monkeypatch, guard_state={})

    result = await tool.check_permissions(
        {"action": "wait", "stack_id": "stack-from-progress"},
        _permission_ctx(),
    )

    assert result.behavior == "allow"
    assert result.audit is not None
    assert result.audit.is_read_only is True


@pytest.mark.asyncio
async def test_permission_deny_rule_still_blocks_wait(monkeypatch):
    tool, _fake_stack = _deploy_tool(monkeypatch, guard_state={})

    result = await tool.check_permissions(
        {"action": "wait", "stack_id": "stack-from-progress"},
        _permission_ctx(deny={"session": ["ros_deploy(wait:stack-from-progress)"]}),
    )

    assert result.behavior == "deny"


@pytest.mark.asyncio
async def test_permission_asks_before_reading_local_template_url_outside_allowed_roots_even_with_allow_rule(
    monkeypatch,
    tmp_path,
):
    project = tmp_path / "project"
    project.mkdir()
    outside_template = tmp_path / "outside.yml"
    outside_template.write_text("ROSTemplateFormatVersion: '2015-09-01'\n", encoding="utf-8")
    tool, _fake_stack = _deploy_tool(monkeypatch)

    result = await tool.check_permissions(
        {
            "action": "create",
            "stack_name": "demo",
            "template_url": str(outside_template),
        },
        ToolPermissionContext(
            cwd=str(project),
            allow_rules={"session": ["ros_deploy(create:demo)"]},
        ),
    )

    assert result.behavior == "ask"
    assert result.reason is not None
    assert result.reason.type == "path_constraint"
    assert result.suggestions is not None
    assert result.suggestions[0].rule_content == "create:demo"


@pytest.mark.asyncio
async def test_permission_allows_and_execute_resolves_local_template_url_from_relative_read_root(monkeypatch, tmp_path):
    project = tmp_path / "project"
    skill_root = tmp_path / "skill"
    project.mkdir()
    (skill_root / "templates").mkdir(parents=True)
    (skill_root / "templates" / "demo.yml").write_text("ROSTemplateFormatVersion: '2015-09-01'\n", encoding="utf-8")
    tool, fake_stack = _deploy_tool(
        monkeypatch,
        results=[ToolResult.success(json.dumps({"stack_id": "stack-new", "is_success": True}))],
    )

    permission = await tool.check_permissions(
        {
            "action": "create",
            "stack_name": "demo",
            "template_url": "templates/demo.yml",
        },
        ToolPermissionContext(
            cwd=str(project),
            allow_rules={"session": ["ros_deploy(create:demo)"]},
            trusted_read_directories=[str(skill_root)],
            relative_read_directories=[str(skill_root)],
        ),
    )
    assert permission.behavior == "allow"

    result = await tool.execute(
        tool_input={
            "action": "create",
            "stack_name": "demo",
            "template_url": "templates/demo.yml",
        },
        context=ToolContext(
            cwd=str(project),
            trusted_read_directories=[str(skill_root)],
            relative_read_directories=[str(skill_root)],
            pipeline_mode=True,
        ),
    )

    assert result.is_error is False
    assert fake_stack.calls[0][0]["params"]["TemplateURL"] == os.path.realpath(skill_root / "templates" / "demo.yml")


@pytest.mark.asyncio
async def test_permission_denies_delete_and_create_for_unowned_stack_even_with_allow_rule(monkeypatch):
    tool, _fake_stack = _deploy_tool(monkeypatch, guard_state={})

    result = await tool.check_permissions(
        {"action": "delete_and_create", "stack_id": "stack-from-list-stacks"},
        _permission_ctx(allow={"session": ["ros_deploy(delete_and_create:stack-from-list-stacks)"]}),
    )

    assert result.behavior == "deny"
    assert "not created by the current selling deployment step" in result.message


@pytest.mark.asyncio
async def test_permission_denies_continue_create_for_unowned_stack_even_with_allow_rule(monkeypatch):
    tool, _fake_stack = _deploy_tool(monkeypatch, guard_state={})

    result = await tool.check_permissions(
        {"action": "continue_create", "stack_id": "stack-from-list-stacks"},
        _permission_ctx(allow={"session": ["ros_deploy(continue_create:stack-from-list-stacks)"]}),
    )

    assert result.behavior == "deny"
    assert "not created by the current selling deployment step" in result.message


def test_continue_validation_failure_result_rendering_mentions_recommended_action():
    from iac_code.pipeline.selling.tools.ros_deploy_tool import RosDeployTool

    content = json.dumps(
        {
            "stack_id": "stack-failed",
            "error_code": "ContinueCreateStackValidationFailed",
            "recommended_action": "delete_and_create",
            "message": "cannot continue",
        }
    )

    message = RosDeployTool().render_tool_result_message(content, is_error=True)

    assert message is not None
    assert "ContinueCreateStackValidationFailed" in message
    assert "delete_and_create" in message


def test_render_tool_result_message_shows_short_success_summary():
    from iac_code.pipeline.selling.tools.ros_deploy_tool import RosDeployTool

    content = json.dumps(
        {
            "stack_id": "a463b158-5429-4a2d-9173-825271c28dcb",
            "stack_name": "single-vswitch-20260706-k7m3x9",
            "status": "CREATE_COMPLETE",
            "status_reason": "Stack CREATE completed successfully",
            "progress_percentage": 100.0,
            "elapsed_seconds": 5,
            "is_success": True,
        },
        indent=2,
    )

    message = RosDeployTool().render_tool_result_message(content)

    assert message == "single-vswitch-20260706-k7m3x9 creation succeeded (a463b158)"


def test_render_tool_result_message_shows_short_failure_summary():
    from iac_code.pipeline.selling.tools.ros_deploy_tool import RosDeployTool

    content = json.dumps(
        {
            "stack_id": "a463b158-5429-4a2d-9173-825271c28dcb",
            "stack_name": "single-vswitch-20260706-k7m3x9",
            "status": "CREATE_FAILED",
            "status_reason": (
                "Resource CREATE failed: VPCResourceException: resources.VSwitch: "
                "code: InvalidCidrBlock.Overlapped, message: The CIDR block 192.168.200.0/24 "
                "Overlapped exists CIDR block."
            ),
            "progress_percentage": 0.0,
            "elapsed_seconds": 5,
            "is_success": False,
        },
        indent=2,
    )

    message = RosDeployTool().render_tool_result_message(content, is_error=True)

    assert message == "single-vswitch-20260706-k7m3x9 creation failed: CIDR block overlapped (a463b158)"


def test_render_tool_result_message_keeps_verbose_json():
    from iac_code.pipeline.selling.tools.ros_deploy_tool import RosDeployTool

    content = json.dumps(
        {
            "stack_id": "a463b158-5429-4a2d-9173-825271c28dcb",
            "stack_name": "single-vswitch-20260706-k7m3x9",
            "status": "CREATE_COMPLETE",
            "is_success": True,
        },
        indent=2,
    )

    message = RosDeployTool().render_tool_result_message(content, verbose=True)

    assert message == content
