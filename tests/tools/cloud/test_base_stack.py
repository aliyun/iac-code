"""Tests for BaseCloudStack abstract base class."""

from __future__ import annotations

import asyncio
import json

import pytest

from iac_code.tools.base import ToolContext
from iac_code.tools.cloud.base_stack import BaseCloudStack
from iac_code.tools.cloud.types import ResourceStatus, StackStatus
from iac_code.types.stream_events import StackProgressEvent


class MockCloudStack(BaseCloudStack):
    poll_interval = 0  # No sleep in tests

    def __init__(self) -> None:
        self._poll_count = 0
        self._target_polls = 2

    @property
    def provider_name(self) -> str:
        return "mock"

    @property
    def supported_actions(self) -> list[str]:
        return ["CreateStack", "DeleteStack"]

    @property
    def description(self) -> str:
        return "Mock cloud stack for testing"

    async def call_action(self, action: str, params: dict, region: str) -> str:
        return "stack-id-123"

    async def get_stack_status(self, stack_id: str, region: str) -> StackStatus:
        self._poll_count += 1
        if self._poll_count >= self._target_polls:
            return StackStatus(
                stack_id=stack_id,
                stack_name="test-stack",
                status="CREATE_COMPLETE",
                status_reason="Completed",
                progress_percentage=100,
            )
        return StackStatus(
            stack_id=stack_id,
            stack_name="test-stack",
            status="CREATE_IN_PROGRESS",
            status_reason="In progress",
            progress_percentage=50,
        )

    async def get_stack_resources(self, stack_id: str, region: str) -> list[ResourceStatus]:
        return [
            ResourceStatus(
                name="MyResource",
                resource_type="ALIYUN::ECS::Instance",
                status="CREATE_IN_PROGRESS",
                status_reason="",
            )
        ]


class MockDeleteCloudStack(MockCloudStack):
    @property
    def supported_actions(self) -> list[str]:
        return ["DeleteStack"]

    async def get_stack_status(self, stack_id: str, region: str) -> StackStatus:
        return StackStatus(
            stack_id=stack_id,
            stack_name="test-stack",
            status="DELETE_COMPLETE",
            status_reason="Deleted",
            progress_percentage=100,
        )

    def is_action_success(self, action: str, status: StackStatus) -> bool:
        return action == "DeleteStack" and status.status == "DELETE_COMPLETE"


class HookCapturingCloudStack(MockCloudStack):
    def __init__(self) -> None:
        super().__init__()
        self.terminal_hook_call: dict | None = None
        self.polling_cancelled_hook_call: dict | None = None

    def on_terminal_status(
        self,
        action: str,
        params: dict,
        region: str,
        status: StackStatus,
        resources: list[ResourceStatus],
        elapsed_seconds: int,
    ) -> None:
        self.terminal_hook_call = {
            "action": action,
            "params": params,
            "region": region,
            "status": status,
            "resources": resources,
            "elapsed_seconds": elapsed_seconds,
        }

    def on_polling_cancelled(
        self,
        action: str,
        params: dict,
        region: str,
        stack_id: str,
        elapsed_seconds: int,
    ) -> None:
        self.polling_cancelled_hook_call = {
            "action": action,
            "params": params,
            "region": region,
            "stack_id": stack_id,
            "elapsed_seconds": elapsed_seconds,
        }


class RaisingTerminalHookCloudStack(MockCloudStack):
    def on_terminal_status(
        self,
        action: str,
        params: dict,
        region: str,
        status: StackStatus,
        resources: list[ResourceStatus],
        elapsed_seconds: int,
    ) -> None:
        raise RuntimeError("hook failed")


@pytest.fixture
def stack() -> MockCloudStack:
    return MockCloudStack()


@pytest.fixture
def context() -> ToolContext:
    return ToolContext()


class TestBaseCloudStackProperties:
    def test_name(self, stack: MockCloudStack) -> None:
        assert stack.name == "mock_stack"

    def test_timeout_input_schema_and_messages(self, stack: MockCloudStack) -> None:
        assert stack.timeout == 3600.0
        schema = stack.input_schema
        assert schema["required"] == ["action"]
        assert schema["properties"]["action"]["enum"] == ["CreateStack", "DeleteStack"]
        assert stack.user_facing_name({}) == "CloudStack"
        assert stack.render_tool_use_message({"action": "CreateStack", "region_id": "cn-hz"}) == "CreateStack cn-hz"
        assert stack.render_tool_use_message({}) is None
        assert stack.get_activity_description(None) is None
        assert "CreateStack cn-hz" in stack.get_activity_description({"action": "CreateStack", "region_id": "cn-hz"})

    def test_is_read_only_false(self, stack: MockCloudStack) -> None:
        assert stack.is_read_only({"action": "CreateStack"}) is False

    def test_is_concurrency_safe_false(self, stack: MockCloudStack) -> None:
        assert stack.is_concurrency_safe({"action": "CreateStack"}) is False

    def test_is_destructive_true(self, stack: MockCloudStack) -> None:
        assert stack.is_destructive({"action": "DeleteStack"}) is True


class TestBaseCloudStackExecute:
    @pytest.mark.asyncio
    async def test_execute_polls_until_terminal(self, stack: MockCloudStack, context: ToolContext) -> None:
        result = await stack.execute(
            tool_input={"action": "CreateStack", "params": {"StackName": "test"}},
            context=context,
        )
        assert result.is_error is False
        # Confirm we polled until terminal status
        assert stack._poll_count >= stack._target_polls

    @pytest.mark.asyncio
    async def test_execute_success_result_contains_stack_info(
        self, stack: MockCloudStack, context: ToolContext
    ) -> None:
        result = await stack.execute(
            tool_input={"action": "CreateStack", "params": {"StackName": "test"}},
            context=context,
        )
        assert result.is_error is False
        data = json.loads(result.content)
        assert data["stack_id"] == "stack-id-123"
        assert data["status"] == "CREATE_COMPLETE"

    @pytest.mark.asyncio
    async def test_execute_emits_progress_events_to_queue(self, stack: MockCloudStack) -> None:
        queue: asyncio.Queue = asyncio.Queue()
        context = ToolContext(event_queue=queue)

        await stack.execute(
            tool_input={"action": "CreateStack", "params": {"StackName": "test"}},
            context=context,
        )

        events = []
        while not queue.empty():
            events.append(await queue.get())

        progress_events = [e for e in events if isinstance(e, StackProgressEvent)]
        assert len(progress_events) >= 1
        first = progress_events[0]
        assert first.stack_id == "stack-id-123"
        assert first.stack_name == "test-stack"

    @pytest.mark.asyncio
    async def test_execute_invalid_action_returns_error(self, stack: MockCloudStack, context: ToolContext) -> None:
        result = await stack.execute(
            tool_input={"action": "ListStacks"},
            context=context,
        )
        assert result.is_error is True
        assert "ListStacks" in result.content

    @pytest.mark.asyncio
    async def test_execute_no_queue_does_not_raise(self, stack: MockCloudStack, context: ToolContext) -> None:
        # context has no event_queue (None) — should complete without error
        result = await stack.execute(
            tool_input={"action": "CreateStack", "params": {}},
            context=context,
        )
        assert result.is_error is False

    @pytest.mark.asyncio
    async def test_execute_call_action_error(self, stack: MockCloudStack) -> None:
        async def fail_call(action: str, params: dict, region: str) -> str:
            raise RuntimeError("boom")

        stack.call_action = fail_call  # type: ignore[method-assign]
        result = await stack.execute(tool_input={"action": "CreateStack", "params": {}}, context=ToolContext())
        assert result.is_error is True
        assert "[CreateStack] boom" in result.content

    @pytest.mark.asyncio
    async def test_execute_status_error(self, stack: MockCloudStack) -> None:
        async def fail_status(stack_id: str, region: str) -> StackStatus:
            raise RuntimeError("status failed")

        stack.get_stack_status = fail_status  # type: ignore[method-assign]
        result = await stack.execute(tool_input={"action": "CreateStack", "params": {}}, context=ToolContext())
        assert result.is_error is True
        assert "[GetStackStatus] status failed" in result.content

    @pytest.mark.asyncio
    async def test_execute_resources_error(self, stack: MockCloudStack) -> None:
        async def fail_resources(stack_id: str, region: str) -> list[ResourceStatus]:
            raise RuntimeError("resources failed")

        stack.get_stack_resources = fail_resources  # type: ignore[method-assign]
        result = await stack.execute(tool_input={"action": "CreateStack", "params": {}}, context=ToolContext())
        assert result.is_error is True
        assert "[GetStackResources] resources failed" in result.content

    @pytest.mark.asyncio
    async def test_execute_terminal_status_still_runs_hook_when_resources_error(self) -> None:
        stack = HookCapturingCloudStack()
        stack._target_polls = 1

        async def fail_resources(stack_id: str, region: str) -> list[ResourceStatus]:
            raise RuntimeError("resources failed")

        stack.get_stack_resources = fail_resources  # type: ignore[method-assign]
        result = await stack.execute(tool_input={"action": "CreateStack", "params": {}}, context=ToolContext())
        assert result.is_error is False
        assert stack.terminal_hook_call is not None
        assert stack.terminal_hook_call["status"].status == "CREATE_COMPLETE"
        assert stack.terminal_hook_call["resources"] == []

    @pytest.mark.asyncio
    async def test_execute_respects_instance_poll_interval_override(self) -> None:
        stack = MockCloudStack()
        stack.poll_interval = 0.25

        async def stop_after_sleep(stack_id: str, region: str) -> StackStatus:
            raise RuntimeError("stop")

        stack.get_stack_status = stop_after_sleep  # type: ignore[method-assign]

        sleep_calls: list[float] = []

        async def record_sleep(delay: float) -> None:
            sleep_calls.append(delay)

        with pytest.MonkeyPatch.context() as monkeypatch:
            monkeypatch.setattr("iac_code.tools.cloud.base_stack.asyncio.sleep", record_sleep)
            result = await stack.execute(tool_input={"action": "CreateStack", "params": {}}, context=ToolContext())

        assert result.is_error is True
        assert sleep_calls == [0.25]

    @pytest.mark.asyncio
    async def test_execute_terminal_failure_returns_error(self, stack: MockCloudStack) -> None:
        async def failed_status(stack_id: str, region: str) -> StackStatus:
            return StackStatus(
                stack_id=stack_id,
                stack_name="test-stack",
                status="CREATE_FAILED",
                status_reason="Failed",
                progress_percentage=100,
            )

        stack.get_stack_status = failed_status  # type: ignore[method-assign]
        result = await stack.execute(tool_input={"action": "CreateStack", "params": {}}, context=ToolContext())
        assert result.is_error is True
        data = json.loads(result.content)
        assert data["status"] == "CREATE_FAILED"
        assert data["is_success"] is False

    @pytest.mark.asyncio
    async def test_execute_terminal_rollback_returns_error(self, stack: MockCloudStack) -> None:
        async def rollback_status(stack_id: str, region: str) -> StackStatus:
            return StackStatus(
                stack_id=stack_id,
                stack_name="test-stack",
                status="CREATE_ROLLBACK_COMPLETE",
                status_reason="Rolled back",
                progress_percentage=100,
            )

        stack.get_stack_status = rollback_status  # type: ignore[method-assign]
        result = await stack.execute(tool_input={"action": "CreateStack", "params": {}}, context=ToolContext())
        assert result.is_error is True
        data = json.loads(result.content)
        assert data["status"] == "CREATE_ROLLBACK_COMPLETE"
        assert data["is_success"] is False

    @pytest.mark.asyncio
    async def test_execute_delete_complete_can_be_action_success(self) -> None:
        stack = MockDeleteCloudStack()
        result = await stack.execute(tool_input={"action": "DeleteStack", "params": {}}, context=ToolContext())
        assert result.is_error is False
        data = json.loads(result.content)
        assert data["status"] == "DELETE_COMPLETE"
        assert data["is_success"] is True

    @pytest.mark.asyncio
    async def test_execute_calls_terminal_status_hook_with_terminal_context(self) -> None:
        stack = HookCapturingCloudStack()
        params = {"StackName": "test"}
        result = await stack.execute(
            tool_input={"action": "CreateStack", "params": params, "region_id": "cn-hangzhou"},
            context=ToolContext(),
        )
        assert result.is_error is False
        assert stack.terminal_hook_call is not None
        assert stack.terminal_hook_call["action"] == "CreateStack"
        assert stack.terminal_hook_call["params"] is params
        assert stack.terminal_hook_call["region"] == "cn-hangzhou"
        assert stack.terminal_hook_call["status"].status == "CREATE_COMPLETE"
        assert stack.terminal_hook_call["resources"][0].name == "MyResource"
        assert stack.terminal_hook_call["elapsed_seconds"] == json.loads(result.content)["elapsed_seconds"]

    @pytest.mark.asyncio
    async def test_execute_terminal_status_hook_failure_does_not_flip_result(self) -> None:
        stack = RaisingTerminalHookCloudStack()
        result = await stack.execute(tool_input={"action": "CreateStack", "params": {}}, context=ToolContext())
        assert result.is_error is False
        data = json.loads(result.content)
        assert data["status"] == "CREATE_COMPLETE"
        assert data["is_success"] is True

    @pytest.mark.asyncio
    async def test_execute_calls_polling_cancelled_hook_and_reraises(self) -> None:
        stack = HookCapturingCloudStack()
        params = {"StackName": "test"}

        async def cancelled_status(stack_id: str, region: str) -> StackStatus:
            raise asyncio.CancelledError

        stack.get_stack_status = cancelled_status  # type: ignore[method-assign]

        with pytest.raises(asyncio.CancelledError):
            await stack.execute(
                tool_input={"action": "CreateStack", "params": params, "region_id": "cn-hangzhou"},
                context=ToolContext(),
            )

        assert stack.polling_cancelled_hook_call == {
            "action": "CreateStack",
            "params": params,
            "region": "cn-hangzhou",
            "stack_id": "stack-id-123",
            "elapsed_seconds": 0,
        }


class MockCloudStackWithDefaultRegion(MockCloudStack):
    def _get_default_region(self) -> str:
        return "cn-shanghai"

    async def call_action(self, action: str, params: dict, region: str) -> str:
        self.last_region = region
        return await super().call_action(action, params, region)


class TestBaseCloudStackDefaultRegion:
    @pytest.mark.asyncio
    async def test_execute_uses_default_region_when_not_provided(self) -> None:
        stack = MockCloudStackWithDefaultRegion()
        context = ToolContext()
        await stack.execute(
            tool_input={"action": "CreateStack", "params": {"StackName": "test"}},
            context=context,
        )
        assert stack.last_region == "cn-shanghai"

    @pytest.mark.asyncio
    async def test_execute_uses_explicit_region_over_default(self) -> None:
        stack = MockCloudStackWithDefaultRegion()
        context = ToolContext()
        await stack.execute(
            tool_input={"action": "CreateStack", "params": {"StackName": "test"}, "region_id": "cn-beijing"},
            context=context,
        )
        assert stack.last_region == "cn-beijing"

    def test_input_schema_mentions_default_region(self) -> None:
        stack = MockCloudStackWithDefaultRegion()
        assert "Defaults to 'cn-shanghai'." in stack.input_schema["properties"]["region_id"]["description"]


class TestBaseCloudStackRenderToolResultMessage:
    def test_compact_mode_shows_summary(self) -> None:
        stack = MockCloudStack()
        output = json.dumps(
            {"stack_name": "my-stack", "stack_id": "stack-001", "status": "CREATE_COMPLETE", "elapsed_seconds": 42}
        )
        result = stack.render_tool_result_message(output)
        assert result == "my-stack(stack-001) CREATE_COMPLETE (42s)"

    def test_verbose_mode_shows_full_output(self) -> None:
        stack = MockCloudStack()
        output = json.dumps({"stack_name": "my-stack", "status": "CREATE_COMPLETE", "elapsed_seconds": 42})
        result = stack.render_tool_result_message(output, verbose=True)
        assert result == output.strip()

    def test_invalid_json_truncates(self) -> None:
        stack = MockCloudStack()
        result = stack.render_tool_result_message("not json")
        assert result == "not json"

    def test_error_strips_raw_response(self) -> None:
        stack = MockCloudStack()
        raw = (
            "Error: MissingRegionId code: 400, RegionId is mandatory for this action. "
            "request id: FCA05FD7-47E1-5FFC-B2F0-AADED6EF132E "
            "Response: {'RequestId': 'FCA05FD7-47E1-5FFC-B2F0-AADED6EF132E', 'Message': 'RegionId is mandatory'}"
        )
        result = stack.render_tool_result_message(raw, is_error=True)
        assert "Response:" not in result
        assert "MissingRegionId" in result
        assert "FCA05FD7" in result

    def test_error_json_shows_summary(self) -> None:
        stack = MockCloudStack()
        output = json.dumps(
            {
                "stack_name": "my-stack",
                "stack_id": "stack-001",
                "status": "CREATE_FAILED",
                "elapsed_seconds": 30,
            }
        )
        result = stack.render_tool_result_message(output, is_error=True)
        assert result == "my-stack(stack-001) CREATE_FAILED (30s)"

    def test_clean_error_message_strips_response_suffix(self) -> None:
        assert BaseCloudStack._clean_error_message("msg Response: {raw}") == "msg"
