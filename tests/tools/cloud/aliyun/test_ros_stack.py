"""Tests for RosStack tool."""

from __future__ import annotations

import asyncio
import json
from unittest.mock import MagicMock, patch

import pytest

from iac_code.services.telemetry.names import Events, Metrics
from iac_code.tools.base import ToolContext
from iac_code.tools.cloud.aliyun.ros_stack import RosStack
from iac_code.types.stream_events import StackProgressEvent

_MINIMAL_TEMPLATE_BODY = (
    '{"ROSTemplateFormatVersion": "2015-09-01", '
    '"Resources": {"Vpc": {"Type": "ALIYUN::ECS::VPC", "Properties": {"CidrBlock": "192.168.0.0/16"}}}}'
)


@pytest.fixture
def mock_credentials():
    with patch("iac_code.tools.cloud.aliyun.ros_stack.CloudCredentials") as mock:
        cred = MagicMock()
        cred.access_key_id = "test-ak"
        cred.access_key_secret = "test-secret"
        cred.region_id = "cn-hangzhou"
        instance = mock.return_value
        instance.get_provider.return_value = cred
        yield instance


@pytest.fixture
def tool() -> RosStack:
    t = RosStack()
    t.poll_interval = 0
    return t


@pytest.fixture
def context() -> ToolContext:
    return ToolContext()


class TestRosStackProperties:
    def test_name(self, tool: RosStack) -> None:
        assert tool.name == "ros_stack"

    def test_supported_actions(self, tool: RosStack) -> None:
        assert tool.supported_actions == [
            "CreateStack",
            "UpdateStack",
            "ContinueCreateStack",
            "DeleteStack",
        ]

    def test_is_read_only_false(self, tool: RosStack) -> None:
        assert tool.is_read_only({"action": "CreateStack"}) is False
        assert tool.is_read_only({"action": "DeleteStack"}) is False

    def test_is_destructive(self, tool: RosStack) -> None:
        assert tool.is_destructive({"action": "DeleteStack"}) is True
        assert tool.is_destructive({"action": "CreateStack"}) is True


class TestRosStackExecute:
    @pytest.mark.asyncio
    async def test_execute_unsupported_action(self, tool: RosStack, context: ToolContext) -> None:
        result = await tool.execute(
            tool_input={"action": "ListStacks"},
            context=context,
        )
        assert result.is_error is True
        assert "ListStacks" in result.content

    @pytest.mark.asyncio
    async def test_execute_create_stack(self, tool: RosStack, mock_credentials) -> None:
        mock_client = MagicMock()

        # create_stack returns stack_id
        create_response = MagicMock()
        create_response.body.stack_id = "stack-123"
        mock_client.create_stack.return_value = create_response

        # get_stack returns COMPLETE status immediately
        get_stack_response = MagicMock()
        get_stack_response.body.to_map.return_value = {
            "StackId": "stack-123",
            "StackName": "test",
            "Status": "CREATE_COMPLETE",
            "StatusReason": "",
        }
        mock_client.get_stack.return_value = get_stack_response

        # list_stack_resources returns one resource
        list_resources_response = MagicMock()
        list_resources_response.body.to_map.return_value = {
            "Resources": [
                {
                    "LogicalResourceId": "Vpc",
                    "ResourceType": "ALIYUN::ECS::VPC",
                    "Status": "CREATE_COMPLETE",
                    "StatusReason": "",
                }
            ]
        }
        mock_client.list_stack_resources.return_value = list_resources_response

        queue: asyncio.Queue = asyncio.Queue()
        ctx = ToolContext(event_queue=queue)

        with (
            patch("iac_code.tools.cloud.aliyun.ros_stack.RosClientFactory") as mock_factory,
            patch("iac_code.tools.cloud.aliyun.api_hooks.run_hooks", return_value=None),
        ):
            mock_factory.create.return_value = mock_client
            result = await tool.execute(
                tool_input={
                    "action": "CreateStack",
                    "params": {"StackName": "test", "TemplateBody": "{}"},
                    "region_id": "cn-hangzhou",
                },
                context=ctx,
            )

        assert result.is_error is False
        mock_client.create_stack.assert_called_once()
        mock_client.get_stack.assert_called()
        mock_client.list_stack_resources.assert_called()

        # Verify StackProgressEvent was emitted
        events = []
        while not queue.empty():
            events.append(await queue.get())

        progress_events = [e for e in events if isinstance(e, StackProgressEvent)]
        assert len(progress_events) >= 1
        first = progress_events[0]
        assert first.stack_id == "stack-123"
        assert first.stack_name == "test"
        assert first.status == "CREATE_COMPLETE"
        assert len(first.resources) == 1
        assert first.resources[0]["name"] == "Vpc"
        assert first.resources[0]["resource_type"] == "ALIYUN::ECS::VPC"

    @pytest.mark.asyncio
    async def test_create_stack_emits_success_telemetry_only_after_terminal_success(
        self, tool: RosStack, mock_credentials
    ) -> None:
        mock_client = MagicMock()

        create_response = MagicMock()
        create_response.body.stack_id = "stack-123"
        mock_client.create_stack.return_value = create_response

        get_stack_response = MagicMock()
        get_stack_response.body.to_map.return_value = {
            "StackId": "stack-123",
            "StackName": "test",
            "Status": "CREATE_COMPLETE",
            "StatusReason": "",
        }
        mock_client.get_stack.return_value = get_stack_response

        list_resources_response = MagicMock()
        list_resources_response.body.to_map.return_value = {"Resources": []}
        mock_client.list_stack_resources.return_value = list_resources_response

        events: list[tuple[str, dict]] = []
        metrics: list[tuple[str, int, dict]] = []

        def record_event(event_name: str, metadata: dict | None = None) -> None:
            if event_name == Events.DEPLOYMENT_SUCCEEDED:
                assert mock_client.get_stack.called
            events.append((event_name, metadata or {}))

        with (
            patch("iac_code.tools.cloud.aliyun.ros_stack.RosClientFactory") as mock_factory,
            patch("iac_code.tools.cloud.aliyun.api_hooks.run_hooks", return_value=None),
            patch("iac_code.tools.cloud.aliyun.ros_stack.log_event", side_effect=record_event),
            patch(
                "iac_code.tools.cloud.aliyun.ros_stack.add_metric",
                side_effect=lambda name, value, attrs=None: metrics.append((name, value, attrs or {})),
            ),
        ):
            mock_factory.create.return_value = mock_client
            result = await tool.execute(
                tool_input={
                    "action": "CreateStack",
                    "params": {"StackName": "test", "TemplateBody": "{}"},
                    "region_id": "cn-hangzhou",
                },
                context=ToolContext(),
            )

        assert result.is_error is False
        assert [event_name for event_name, _ in events].count(Events.DEPLOYMENT_STARTED) == 1
        succeeded_events = [metadata for event_name, metadata in events if event_name == Events.DEPLOYMENT_SUCCEEDED]
        assert len(succeeded_events) == 1
        assert succeeded_events[0]["stack_status"] == "CREATE_COMPLETE"
        assert any(
            name == Metrics.DEPLOYMENT_COUNT and attrs == {"kind": "ros", "outcome": "success"}
            for name, _, attrs in metrics
        )

    @pytest.mark.asyncio
    async def test_create_stack_started_telemetry_failure_does_not_prevent_api_call(
        self, tool: RosStack, mock_credentials
    ) -> None:
        mock_client = MagicMock()

        create_response = MagicMock()
        create_response.body.stack_id = "stack-123"
        mock_client.create_stack.return_value = create_response

        get_stack_response = MagicMock()
        get_stack_response.body.to_map.return_value = {
            "StackId": "stack-123",
            "StackName": "test",
            "Status": "CREATE_COMPLETE",
            "StatusReason": "",
        }
        mock_client.get_stack.return_value = get_stack_response

        list_resources_response = MagicMock()
        list_resources_response.body.to_map.return_value = {"Resources": []}
        mock_client.list_stack_resources.return_value = list_resources_response

        def flaky_log_event(event_name: str, metadata: dict | None = None) -> None:
            if event_name == Events.DEPLOYMENT_STARTED:
                raise RuntimeError("telemetry unavailable")

        with (
            patch("iac_code.tools.cloud.aliyun.ros_stack.RosClientFactory") as mock_factory,
            patch("iac_code.tools.cloud.aliyun.api_hooks.run_hooks", return_value=None),
            patch("iac_code.tools.cloud.aliyun.ros_stack.log_event", side_effect=flaky_log_event),
        ):
            mock_factory.create.return_value = mock_client
            result = await tool.execute(
                tool_input={
                    "action": "CreateStack",
                    "params": {"StackName": "test", "TemplateBody": "{}"},
                    "region_id": "cn-hangzhou",
                },
                context=ToolContext(),
            )

        assert result.is_error is False
        mock_client.create_stack.assert_called_once()

    @pytest.mark.asyncio
    async def test_create_stack_polling_cancellation_cleans_context_and_emits_cancel_telemetry(
        self, tool: RosStack, mock_credentials
    ) -> None:
        mock_client = MagicMock()

        create_response = MagicMock()
        create_response.body.stack_id = "stack-123"
        mock_client.create_stack.return_value = create_response
        mock_client.get_stack.side_effect = asyncio.CancelledError

        events: list[tuple[str, dict]] = []
        metrics: list[tuple[str, int, dict]] = []

        with (
            patch("iac_code.tools.cloud.aliyun.ros_stack.RosClientFactory") as mock_factory,
            patch("iac_code.tools.cloud.aliyun.api_hooks.run_hooks", return_value=None),
            patch(
                "iac_code.tools.cloud.aliyun.ros_stack.log_event",
                side_effect=lambda event_name, metadata=None: events.append((event_name, metadata or {})),
            ),
            patch(
                "iac_code.tools.cloud.aliyun.ros_stack.add_metric",
                side_effect=lambda name, value, attrs=None: metrics.append((name, value, attrs or {})),
            ),
        ):
            mock_factory.create.return_value = mock_client

            with pytest.raises(asyncio.CancelledError):
                await tool.execute(
                    tool_input={
                        "action": "CreateStack",
                        "params": {"StackName": "test", "TemplateBody": "{}"},
                        "region_id": "cn-hangzhou",
                    },
                    context=ToolContext(),
                )

        assert ("stack-123", "CreateStack") not in tool._deployment_telemetry_contexts()

        cancelled_events = [metadata for event_name, metadata in events if event_name == Events.DEPLOYMENT_CANCELLED]
        assert len(cancelled_events) == 1
        assert cancelled_events[0]["iac_kind"] == "ros"
        assert cancelled_events[0]["region"] == "cn-hangzhou"
        assert cancelled_events[0]["reason"] == "user_cancel"
        assert isinstance(cancelled_events[0]["duration_ms"], int)

        assert any(
            name == Metrics.DEPLOYMENT_COUNT and value == 1 and attrs == {"kind": "ros", "outcome": "cancel"}
            for name, value, attrs in metrics
        )

    @pytest.mark.asyncio
    async def test_create_stack_template_generated_telemetry_failure_does_not_prevent_api_call(
        self, tool: RosStack, mock_credentials
    ) -> None:
        mock_client = MagicMock()

        create_response = MagicMock()
        create_response.body.stack_id = "stack-123"
        mock_client.create_stack.return_value = create_response

        get_stack_response = MagicMock()
        get_stack_response.body.to_map.return_value = {
            "StackId": "stack-123",
            "StackName": "test",
            "Status": "CREATE_COMPLETE",
            "StatusReason": "",
        }
        mock_client.get_stack.return_value = get_stack_response

        list_resources_response = MagicMock()
        list_resources_response.body.to_map.return_value = {"Resources": []}
        mock_client.list_stack_resources.return_value = list_resources_response

        def flaky_log_event(event_name: str, metadata: dict | None = None) -> None:
            if event_name == Events.TEMPLATE_GENERATED:
                raise RuntimeError("telemetry unavailable")

        with (
            patch("iac_code.tools.cloud.aliyun.ros_stack.RosClientFactory") as mock_factory,
            patch("iac_code.tools.cloud.aliyun.api_hooks.run_hooks", return_value=None),
            patch("iac_code.tools.cloud.aliyun.ros_stack.log_event", side_effect=flaky_log_event),
        ):
            mock_factory.create.return_value = mock_client
            result = await tool.execute(
                tool_input={
                    "action": "CreateStack",
                    "params": {"StackName": "test", "TemplateBody": "{}"},
                    "region_id": "cn-hangzhou",
                },
                context=ToolContext(),
            )

        assert result.is_error is False
        mock_client.create_stack.assert_called_once()

    @pytest.mark.asyncio
    async def test_create_stack_pre_request_metric_failure_does_not_prevent_api_call(
        self, tool: RosStack, mock_credentials
    ) -> None:
        mock_client = MagicMock()

        create_response = MagicMock()
        create_response.body.stack_id = "stack-123"
        mock_client.create_stack.return_value = create_response

        get_stack_response = MagicMock()
        get_stack_response.body.to_map.return_value = {
            "StackId": "stack-123",
            "StackName": "test",
            "Status": "CREATE_COMPLETE",
            "StatusReason": "",
        }
        mock_client.get_stack.return_value = get_stack_response

        list_resources_response = MagicMock()
        list_resources_response.body.to_map.return_value = {"Resources": []}
        mock_client.list_stack_resources.return_value = list_resources_response

        def flaky_add_metric(name: str, value: int, attrs: dict | None = None) -> None:
            if name == Metrics.TEMPLATE_GENERATED_COUNT:
                raise RuntimeError("telemetry unavailable")

        with (
            patch("iac_code.tools.cloud.aliyun.ros_stack.RosClientFactory") as mock_factory,
            patch("iac_code.tools.cloud.aliyun.api_hooks.run_hooks", return_value=None),
            patch("iac_code.tools.cloud.aliyun.ros_stack.add_metric", side_effect=flaky_add_metric),
        ):
            mock_factory.create.return_value = mock_client
            result = await tool.execute(
                tool_input={
                    "action": "CreateStack",
                    "params": {"StackName": "test", "TemplateBody": "{}"},
                    "region_id": "cn-hangzhou",
                },
                context=ToolContext(),
            )

        assert result.is_error is False
        mock_client.create_stack.assert_called_once()

    @pytest.mark.asyncio
    async def test_create_stack_non_string_resource_type_does_not_prevent_api_call(
        self, tool: RosStack, mock_credentials
    ) -> None:
        mock_client = MagicMock()

        create_response = MagicMock()
        create_response.body.stack_id = "stack-123"
        mock_client.create_stack.return_value = create_response

        get_stack_response = MagicMock()
        get_stack_response.body.to_map.return_value = {
            "StackId": "stack-123",
            "StackName": "test",
            "Status": "CREATE_COMPLETE",
            "StatusReason": "",
        }
        mock_client.get_stack.return_value = get_stack_response

        list_resources_response = MagicMock()
        list_resources_response.body.to_map.return_value = {"Resources": []}
        mock_client.list_stack_resources.return_value = list_resources_response

        template_body = json.dumps({"Resources": {"R": {"Type": 123}}})

        with (
            patch("iac_code.tools.cloud.aliyun.ros_stack.RosClientFactory") as mock_factory,
            patch("iac_code.tools.cloud.aliyun.api_hooks.run_hooks", return_value=None),
        ):
            mock_factory.create.return_value = mock_client
            result = await tool.execute(
                tool_input={
                    "action": "CreateStack",
                    "params": {"StackName": "test", "TemplateBody": template_body},
                    "region_id": "cn-hangzhou",
                },
                context=ToolContext(),
            )

        assert result.is_error is False
        mock_client.create_stack.assert_called_once()
        data = json.loads(result.content)
        assert data["status"] == "CREATE_COMPLETE"

    @pytest.mark.asyncio
    async def test_create_stack_none_template_body_does_not_prevent_api_call(
        self, tool: RosStack, mock_credentials
    ) -> None:
        mock_client = MagicMock()

        create_response = MagicMock()
        create_response.body.stack_id = "stack-123"
        mock_client.create_stack.return_value = create_response

        get_stack_response = MagicMock()
        get_stack_response.body.to_map.return_value = {
            "StackId": "stack-123",
            "StackName": "test",
            "Status": "CREATE_COMPLETE",
            "StatusReason": "",
        }
        mock_client.get_stack.return_value = get_stack_response

        list_resources_response = MagicMock()
        list_resources_response.body.to_map.return_value = {"Resources": []}
        mock_client.list_stack_resources.return_value = list_resources_response

        with (
            patch("iac_code.tools.cloud.aliyun.ros_stack.RosClientFactory") as mock_factory,
            patch("iac_code.tools.cloud.aliyun.api_hooks.run_hooks", return_value=None),
        ):
            mock_factory.create.return_value = mock_client
            result = await tool.execute(
                tool_input={
                    "action": "CreateStack",
                    "params": {"StackName": "test", "TemplateBody": None},
                    "region_id": "cn-hangzhou",
                },
                context=ToolContext(),
            )

        assert result.is_error is False
        mock_client.create_stack.assert_called_once()
        request = mock_client.create_stack.call_args.args[0]
        assert request.template_body is None
        data = json.loads(result.content)
        assert data["status"] == "CREATE_COMPLETE"

    @pytest.mark.asyncio
    async def test_create_stack_rollback_emits_failure_telemetry(self, tool: RosStack, mock_credentials) -> None:
        mock_client = MagicMock()

        create_response = MagicMock()
        create_response.body.stack_id = "stack-123"
        mock_client.create_stack.return_value = create_response

        get_stack_response = MagicMock()
        get_stack_response.body.to_map.return_value = {
            "StackId": "stack-123",
            "StackName": "test",
            "Status": "CREATE_ROLLBACK_COMPLETE",
            "StatusReason": "Resource failed",
        }
        mock_client.get_stack.return_value = get_stack_response

        list_resources_response = MagicMock()
        list_resources_response.body.to_map.return_value = {"Resources": []}
        mock_client.list_stack_resources.return_value = list_resources_response

        events: list[tuple[str, dict]] = []

        with (
            patch("iac_code.tools.cloud.aliyun.ros_stack.RosClientFactory") as mock_factory,
            patch("iac_code.tools.cloud.aliyun.api_hooks.run_hooks", return_value=None),
            patch(
                "iac_code.tools.cloud.aliyun.ros_stack.log_event",
                side_effect=lambda event_name, metadata=None: events.append((event_name, metadata or {})),
            ),
        ):
            mock_factory.create.return_value = mock_client
            result = await tool.execute(
                tool_input={
                    "action": "CreateStack",
                    "params": {"StackName": "test", "TemplateBody": "{}"},
                    "region_id": "cn-hangzhou",
                },
                context=ToolContext(),
            )

        assert result.is_error is True
        assert not [metadata for event_name, metadata in events if event_name == Events.DEPLOYMENT_SUCCEEDED]
        failed_events = [metadata for event_name, metadata in events if event_name == Events.DEPLOYMENT_FAILED]
        assert len(failed_events) == 1
        assert failed_events[0]["stack_status"] == "CREATE_ROLLBACK_COMPLETE"

    @pytest.mark.asyncio
    async def test_create_stack_import_create_complete_is_terminal_success(
        self, tool: RosStack, mock_credentials
    ) -> None:
        mock_client = MagicMock()

        create_response = MagicMock()
        create_response.body.stack_id = "stack-123"
        mock_client.create_stack.return_value = create_response

        get_stack_response = MagicMock()
        get_stack_response.body.to_map.return_value = {
            "StackId": "stack-123",
            "StackName": "test",
            "Status": "IMPORT_CREATE_COMPLETE",
            "StatusReason": "",
        }
        mock_client.get_stack.side_effect = [get_stack_response, RuntimeError("unexpected second poll")]

        list_resources_response = MagicMock()
        list_resources_response.body.to_map.return_value = {"Resources": []}
        mock_client.list_stack_resources.return_value = list_resources_response

        with (
            patch("iac_code.tools.cloud.aliyun.ros_stack.RosClientFactory") as mock_factory,
            patch("iac_code.tools.cloud.aliyun.api_hooks.run_hooks", return_value=None),
        ):
            mock_factory.create.return_value = mock_client
            result = await tool.execute(
                tool_input={
                    "action": "CreateStack",
                    "params": {"StackName": "test", "TemplateBody": "{}"},
                    "region_id": "cn-hangzhou",
                },
                context=ToolContext(),
            )

        assert result.is_error is False
        assert mock_client.get_stack.call_count == 1
        data = json.loads(result.content)
        assert data["status"] == "IMPORT_CREATE_COMPLETE"
        assert data["is_success"] is True

    @pytest.mark.asyncio
    async def test_create_stack_import_create_rollback_complete_is_terminal_error(
        self, tool: RosStack, mock_credentials
    ) -> None:
        mock_client = MagicMock()

        create_response = MagicMock()
        create_response.body.stack_id = "stack-123"
        mock_client.create_stack.return_value = create_response

        get_stack_response = MagicMock()
        get_stack_response.body.to_map.return_value = {
            "StackId": "stack-123",
            "StackName": "test",
            "Status": "IMPORT_CREATE_ROLLBACK_COMPLETE",
            "StatusReason": "Import rollback completed",
        }
        mock_client.get_stack.side_effect = [get_stack_response, RuntimeError("unexpected second poll")]

        list_resources_response = MagicMock()
        list_resources_response.body.to_map.return_value = {"Resources": []}
        mock_client.list_stack_resources.return_value = list_resources_response

        with (
            patch("iac_code.tools.cloud.aliyun.ros_stack.RosClientFactory") as mock_factory,
            patch("iac_code.tools.cloud.aliyun.api_hooks.run_hooks", return_value=None),
        ):
            mock_factory.create.return_value = mock_client
            result = await tool.execute(
                tool_input={
                    "action": "CreateStack",
                    "params": {"StackName": "test", "TemplateBody": "{}"},
                    "region_id": "cn-hangzhou",
                },
                context=ToolContext(),
            )

        assert result.is_error is True
        assert mock_client.get_stack.call_count == 1
        data = json.loads(result.content)
        assert data["status"] == "IMPORT_CREATE_ROLLBACK_COMPLETE"
        assert data["is_success"] is False

    @pytest.mark.asyncio
    async def test_update_stack_emits_success_telemetry_only_after_terminal_success(
        self, tool: RosStack, mock_credentials
    ) -> None:
        mock_client = MagicMock()

        update_response = MagicMock()
        update_response.body.stack_id = "stack-123"
        mock_client.update_stack.return_value = update_response

        get_stack_response = MagicMock()
        get_stack_response.body.to_map.return_value = {
            "StackId": "stack-123",
            "StackName": "test",
            "Status": "UPDATE_COMPLETE",
            "StatusReason": "",
        }
        mock_client.get_stack.return_value = get_stack_response

        list_resources_response = MagicMock()
        list_resources_response.body.to_map.return_value = {"Resources": []}
        mock_client.list_stack_resources.return_value = list_resources_response

        events: list[tuple[str, dict]] = []

        def record_event(event_name: str, metadata: dict | None = None) -> None:
            if event_name == Events.DEPLOYMENT_SUCCEEDED:
                assert mock_client.get_stack.called
            events.append((event_name, metadata or {}))

        with (
            patch("iac_code.tools.cloud.aliyun.ros_stack.RosClientFactory") as mock_factory,
            patch("iac_code.tools.cloud.aliyun.api_hooks.run_hooks", return_value=None),
            patch("iac_code.tools.cloud.aliyun.ros_stack.log_event", side_effect=record_event),
        ):
            mock_factory.create.return_value = mock_client
            result = await tool.execute(
                tool_input={
                    "action": "UpdateStack",
                    "params": {"StackId": "stack-123", "TemplateBody": "{}"},
                    "region_id": "cn-hangzhou",
                },
                context=ToolContext(),
            )

        assert result.is_error is False
        assert [event_name for event_name, _ in events].count(Events.DEPLOYMENT_STARTED) == 1
        succeeded_events = [metadata for event_name, metadata in events if event_name == Events.DEPLOYMENT_SUCCEEDED]
        assert len(succeeded_events) == 1
        assert succeeded_events[0]["stack_status"] == "UPDATE_COMPLETE"

    @pytest.mark.asyncio
    async def test_update_stack_ignores_create_complete_from_previous_operation(
        self, tool: RosStack, mock_credentials
    ) -> None:
        mock_client = MagicMock()

        update_response = MagicMock()
        update_response.body.stack_id = "stack-123"
        mock_client.update_stack.return_value = update_response

        create_complete_response = MagicMock()
        create_complete_response.body.to_map.return_value = {
            "StackId": "stack-123",
            "StackName": "test",
            "Status": "CREATE_COMPLETE",
            "StatusReason": "",
        }
        update_complete_response = MagicMock()
        update_complete_response.body.to_map.return_value = {
            "StackId": "stack-123",
            "StackName": "test",
            "Status": "UPDATE_COMPLETE",
            "StatusReason": "",
        }
        mock_client.get_stack.side_effect = [create_complete_response, update_complete_response]

        list_resources_response = MagicMock()
        list_resources_response.body.to_map.return_value = {"Resources": []}
        mock_client.list_stack_resources.return_value = list_resources_response

        events: list[tuple[str, dict]] = []

        with (
            patch("iac_code.tools.cloud.aliyun.ros_stack.RosClientFactory") as mock_factory,
            patch("iac_code.tools.cloud.aliyun.api_hooks.run_hooks", return_value=None),
            patch(
                "iac_code.tools.cloud.aliyun.ros_stack.log_event",
                side_effect=lambda event_name, metadata=None: events.append((event_name, metadata or {})),
            ),
        ):
            mock_factory.create.return_value = mock_client
            result = await tool.execute(
                tool_input={
                    "action": "UpdateStack",
                    "params": {"StackId": "stack-123", "TemplateBody": "{}"},
                    "region_id": "cn-hangzhou",
                },
                context=ToolContext(),
            )

        assert result.is_error is False
        assert mock_client.get_stack.call_count == 2
        succeeded_events = [metadata for event_name, metadata in events if event_name == Events.DEPLOYMENT_SUCCEEDED]
        assert len(succeeded_events) == 1
        assert succeeded_events[0]["stack_status"] == "UPDATE_COMPLETE"

    @pytest.mark.asyncio
    async def test_update_stack_resource_error_with_stale_create_complete_is_not_terminal_fallback(
        self, tool: RosStack, mock_credentials
    ) -> None:
        mock_client = MagicMock()

        update_response = MagicMock()
        update_response.body.stack_id = "stack-123"
        mock_client.update_stack.return_value = update_response

        create_complete_response = MagicMock()
        create_complete_response.body.to_map.return_value = {
            "StackId": "stack-123",
            "StackName": "test",
            "Status": "CREATE_COMPLETE",
            "StatusReason": "",
        }
        mock_client.get_stack.side_effect = [create_complete_response, RuntimeError("unexpected second poll")]
        mock_client.list_stack_resources.side_effect = RuntimeError("resources unavailable")

        with (
            patch("iac_code.tools.cloud.aliyun.ros_stack.RosClientFactory") as mock_factory,
            patch("iac_code.tools.cloud.aliyun.api_hooks.run_hooks", return_value=None),
        ):
            mock_factory.create.return_value = mock_client
            result = await tool.execute(
                tool_input={
                    "action": "UpdateStack",
                    "params": {"StackId": "stack-123", "TemplateBody": "{}"},
                    "region_id": "cn-hangzhou",
                },
                context=ToolContext(),
            )

        assert result.is_error is True
        assert "[GetStackResources] resources unavailable" in result.content
        assert ("stack-123", "UpdateStack") not in tool._deployment_telemetry_contexts()

    @pytest.mark.asyncio
    async def test_update_stack_started_telemetry_failure_does_not_prevent_api_call(
        self, tool: RosStack, mock_credentials
    ) -> None:
        mock_client = MagicMock()

        update_response = MagicMock()
        update_response.body.stack_id = "stack-123"
        mock_client.update_stack.return_value = update_response

        get_stack_response = MagicMock()
        get_stack_response.body.to_map.return_value = {
            "StackId": "stack-123",
            "StackName": "test",
            "Status": "UPDATE_COMPLETE",
            "StatusReason": "",
        }
        mock_client.get_stack.return_value = get_stack_response

        list_resources_response = MagicMock()
        list_resources_response.body.to_map.return_value = {"Resources": []}
        mock_client.list_stack_resources.return_value = list_resources_response

        def flaky_log_event(event_name: str, metadata: dict | None = None) -> None:
            if event_name == Events.DEPLOYMENT_STARTED:
                raise RuntimeError("telemetry unavailable")

        with (
            patch("iac_code.tools.cloud.aliyun.ros_stack.RosClientFactory") as mock_factory,
            patch("iac_code.tools.cloud.aliyun.api_hooks.run_hooks", return_value=None),
            patch("iac_code.tools.cloud.aliyun.ros_stack.log_event", side_effect=flaky_log_event),
        ):
            mock_factory.create.return_value = mock_client
            result = await tool.execute(
                tool_input={
                    "action": "UpdateStack",
                    "params": {"StackId": "stack-123", "TemplateBody": "{}"},
                    "region_id": "cn-hangzhou",
                },
                context=ToolContext(),
            )

        assert result.is_error is False
        mock_client.update_stack.assert_called_once()

    @pytest.mark.asyncio
    async def test_update_stack_non_string_resource_type_does_not_prevent_api_call(
        self, tool: RosStack, mock_credentials
    ) -> None:
        mock_client = MagicMock()

        update_response = MagicMock()
        update_response.body.stack_id = "stack-123"
        mock_client.update_stack.return_value = update_response

        get_stack_response = MagicMock()
        get_stack_response.body.to_map.return_value = {
            "StackId": "stack-123",
            "StackName": "test",
            "Status": "UPDATE_COMPLETE",
            "StatusReason": "",
        }
        mock_client.get_stack.return_value = get_stack_response

        list_resources_response = MagicMock()
        list_resources_response.body.to_map.return_value = {"Resources": []}
        mock_client.list_stack_resources.return_value = list_resources_response

        template_body = json.dumps({"Resources": {"R": {"Type": 123}}})

        with (
            patch("iac_code.tools.cloud.aliyun.ros_stack.RosClientFactory") as mock_factory,
            patch("iac_code.tools.cloud.aliyun.api_hooks.run_hooks", return_value=None),
        ):
            mock_factory.create.return_value = mock_client
            result = await tool.execute(
                tool_input={
                    "action": "UpdateStack",
                    "params": {"StackId": "stack-123", "TemplateBody": template_body},
                    "region_id": "cn-hangzhou",
                },
                context=ToolContext(),
            )

        assert result.is_error is False
        mock_client.update_stack.assert_called_once()
        data = json.loads(result.content)
        assert data["status"] == "UPDATE_COMPLETE"

    @pytest.mark.asyncio
    async def test_update_stack_none_template_body_does_not_prevent_api_call(
        self, tool: RosStack, mock_credentials
    ) -> None:
        mock_client = MagicMock()

        update_response = MagicMock()
        update_response.body.stack_id = "stack-123"
        mock_client.update_stack.return_value = update_response

        get_stack_response = MagicMock()
        get_stack_response.body.to_map.return_value = {
            "StackId": "stack-123",
            "StackName": "test",
            "Status": "UPDATE_COMPLETE",
            "StatusReason": "",
        }
        mock_client.get_stack.return_value = get_stack_response

        list_resources_response = MagicMock()
        list_resources_response.body.to_map.return_value = {"Resources": []}
        mock_client.list_stack_resources.return_value = list_resources_response

        with (
            patch("iac_code.tools.cloud.aliyun.ros_stack.RosClientFactory") as mock_factory,
            patch("iac_code.tools.cloud.aliyun.api_hooks.run_hooks", return_value=None),
        ):
            mock_factory.create.return_value = mock_client
            result = await tool.execute(
                tool_input={
                    "action": "UpdateStack",
                    "params": {"StackId": "stack-123", "TemplateBody": None},
                    "region_id": "cn-hangzhou",
                },
                context=ToolContext(),
            )

        assert result.is_error is False
        mock_client.update_stack.assert_called_once()
        request = mock_client.update_stack.call_args.args[0]
        assert request.template_body is None
        data = json.loads(result.content)
        assert data["status"] == "UPDATE_COMPLETE"

    @pytest.mark.asyncio
    async def test_update_stack_rollback_emits_failure_telemetry(self, tool: RosStack, mock_credentials) -> None:
        mock_client = MagicMock()

        update_response = MagicMock()
        update_response.body.stack_id = "stack-123"
        mock_client.update_stack.return_value = update_response

        get_stack_response = MagicMock()
        get_stack_response.body.to_map.return_value = {
            "StackId": "stack-123",
            "StackName": "test",
            "Status": "ROLLBACK_COMPLETE",
            "StatusReason": "Update rolled back",
        }
        mock_client.get_stack.return_value = get_stack_response

        list_resources_response = MagicMock()
        list_resources_response.body.to_map.return_value = {"Resources": []}
        mock_client.list_stack_resources.return_value = list_resources_response

        events: list[tuple[str, dict]] = []

        with (
            patch("iac_code.tools.cloud.aliyun.ros_stack.RosClientFactory") as mock_factory,
            patch("iac_code.tools.cloud.aliyun.api_hooks.run_hooks", return_value=None),
            patch(
                "iac_code.tools.cloud.aliyun.ros_stack.log_event",
                side_effect=lambda event_name, metadata=None: events.append((event_name, metadata or {})),
            ),
        ):
            mock_factory.create.return_value = mock_client
            result = await tool.execute(
                tool_input={
                    "action": "UpdateStack",
                    "params": {"StackId": "stack-123", "TemplateBody": "{}"},
                    "region_id": "cn-hangzhou",
                },
                context=ToolContext(),
            )

        assert result.is_error is True
        assert not [metadata for event_name, metadata in events if event_name == Events.DEPLOYMENT_SUCCEEDED]
        failed_events = [metadata for event_name, metadata in events if event_name == Events.DEPLOYMENT_FAILED]
        assert len(failed_events) == 1
        assert failed_events[0]["stack_status"] == "ROLLBACK_COMPLETE"

    @pytest.mark.asyncio
    async def test_delete_stack_complete_is_tool_success_without_deployment_success_telemetry(
        self, tool: RosStack, mock_credentials
    ) -> None:
        mock_client = MagicMock()
        mock_client.delete_stack.return_value = None

        get_stack_response = MagicMock()
        get_stack_response.body.to_map.return_value = {
            "StackId": "stack-123",
            "StackName": "test",
            "Status": "DELETE_COMPLETE",
            "StatusReason": "",
        }
        mock_client.get_stack.return_value = get_stack_response

        list_resources_response = MagicMock()
        list_resources_response.body.to_map.return_value = {"Resources": []}
        mock_client.list_stack_resources.return_value = list_resources_response
        events: list[tuple[str, dict]] = []

        with (
            patch("iac_code.tools.cloud.aliyun.ros_stack.RosClientFactory") as mock_factory,
            patch("iac_code.tools.cloud.aliyun.api_hooks.run_hooks", return_value=None),
            patch(
                "iac_code.tools.cloud.aliyun.ros_stack.log_event",
                side_effect=lambda event_name, metadata=None: events.append((event_name, metadata or {})),
            ),
        ):
            mock_factory.create.return_value = mock_client
            result = await tool.execute(
                tool_input={
                    "action": "DeleteStack",
                    "params": {"StackId": "stack-123"},
                    "region_id": "cn-hangzhou",
                },
                context=ToolContext(),
            )

        assert result.is_error is False
        data = json.loads(result.content)
        assert data["status"] == "DELETE_COMPLETE"
        assert data["is_success"] is True
        assert not [metadata for event_name, metadata in events if event_name == Events.DEPLOYMENT_SUCCEEDED]

    @pytest.mark.asyncio
    async def test_delete_stack_ignores_create_complete_from_previous_operation(
        self, tool: RosStack, mock_credentials
    ) -> None:
        mock_client = MagicMock()
        mock_client.delete_stack.return_value = None

        create_complete_response = MagicMock()
        create_complete_response.body.to_map.return_value = {
            "StackId": "stack-123",
            "StackName": "test",
            "Status": "CREATE_COMPLETE",
            "StatusReason": "",
        }
        delete_complete_response = MagicMock()
        delete_complete_response.body.to_map.return_value = {
            "StackId": "stack-123",
            "StackName": "test",
            "Status": "DELETE_COMPLETE",
            "StatusReason": "",
        }
        mock_client.get_stack.side_effect = [create_complete_response, delete_complete_response]

        list_resources_response = MagicMock()
        list_resources_response.body.to_map.return_value = {"Resources": []}
        mock_client.list_stack_resources.return_value = list_resources_response
        events: list[tuple[str, dict]] = []

        with (
            patch("iac_code.tools.cloud.aliyun.ros_stack.RosClientFactory") as mock_factory,
            patch("iac_code.tools.cloud.aliyun.api_hooks.run_hooks", return_value=None),
            patch(
                "iac_code.tools.cloud.aliyun.ros_stack.log_event",
                side_effect=lambda event_name, metadata=None: events.append((event_name, metadata or {})),
            ),
        ):
            mock_factory.create.return_value = mock_client
            result = await tool.execute(
                tool_input={
                    "action": "DeleteStack",
                    "params": {"StackId": "stack-123"},
                    "region_id": "cn-hangzhou",
                },
                context=ToolContext(),
            )

        assert result.is_error is False
        assert mock_client.get_stack.call_count == 2
        data = json.loads(result.content)
        assert data["status"] == "DELETE_COMPLETE"
        assert data["is_success"] is True
        assert not [metadata for event_name, metadata in events if event_name == Events.DEPLOYMENT_SUCCEEDED]

    @pytest.mark.asyncio
    async def test_delete_stack_resource_error_with_stale_create_complete_is_not_terminal_fallback(
        self, tool: RosStack, mock_credentials
    ) -> None:
        mock_client = MagicMock()
        mock_client.delete_stack.return_value = None

        create_complete_response = MagicMock()
        create_complete_response.body.to_map.return_value = {
            "StackId": "stack-123",
            "StackName": "test",
            "Status": "CREATE_COMPLETE",
            "StatusReason": "",
        }
        mock_client.get_stack.side_effect = [create_complete_response, RuntimeError("unexpected second poll")]
        mock_client.list_stack_resources.side_effect = RuntimeError("resources unavailable")

        with (
            patch("iac_code.tools.cloud.aliyun.ros_stack.RosClientFactory") as mock_factory,
            patch("iac_code.tools.cloud.aliyun.api_hooks.run_hooks", return_value=None),
        ):
            mock_factory.create.return_value = mock_client
            result = await tool.execute(
                tool_input={
                    "action": "DeleteStack",
                    "params": {"StackId": "stack-123"},
                    "region_id": "cn-hangzhou",
                },
                context=ToolContext(),
            )

        assert result.is_error is True
        assert "[GetStackResources] resources unavailable" in result.content

    @pytest.mark.asyncio
    async def test_delete_stack_failed_is_tool_error(self, tool: RosStack, mock_credentials) -> None:
        mock_client = MagicMock()
        mock_client.delete_stack.return_value = None

        get_stack_response = MagicMock()
        get_stack_response.body.to_map.return_value = {
            "StackId": "stack-123",
            "StackName": "test",
            "Status": "DELETE_FAILED",
            "StatusReason": "Delete failed",
        }
        mock_client.get_stack.return_value = get_stack_response

        list_resources_response = MagicMock()
        list_resources_response.body.to_map.return_value = {"Resources": []}
        mock_client.list_stack_resources.return_value = list_resources_response

        with (
            patch("iac_code.tools.cloud.aliyun.ros_stack.RosClientFactory") as mock_factory,
            patch("iac_code.tools.cloud.aliyun.api_hooks.run_hooks", return_value=None),
        ):
            mock_factory.create.return_value = mock_client
            result = await tool.execute(
                tool_input={
                    "action": "DeleteStack",
                    "params": {"StackId": "stack-123"},
                    "region_id": "cn-hangzhou",
                },
                context=ToolContext(),
            )

        assert result.is_error is True
        data = json.loads(result.content)
        assert data["status"] == "DELETE_FAILED"
        assert data["is_success"] is False

    @pytest.mark.asyncio
    async def test_delete_stack_api_failure_is_not_masked_by_telemetry_failure(
        self, tool: RosStack, mock_credentials
    ) -> None:
        mock_client = MagicMock()
        mock_client.delete_stack.side_effect = RuntimeError("api exploded")

        with (
            patch("iac_code.tools.cloud.aliyun.ros_stack.RosClientFactory") as mock_factory,
            patch("iac_code.tools.cloud.aliyun.api_hooks.run_hooks", return_value=None),
            patch("iac_code.tools.cloud.aliyun.ros_stack.log_event", side_effect=RuntimeError("telemetry exploded")),
            patch("iac_code.tools.cloud.aliyun.ros_stack.add_metric", side_effect=RuntimeError("telemetry exploded")),
        ):
            mock_factory.create.return_value = mock_client
            result = await tool.execute(
                tool_input={
                    "action": "DeleteStack",
                    "params": {"StackId": "stack-123"},
                    "region_id": "cn-hangzhou",
                },
                context=ToolContext(),
            )

        assert result.is_error is True
        assert "[DeleteStack] api exploded" in result.content
        assert "telemetry exploded" not in result.content
        mock_client.delete_stack.assert_called_once()

    @pytest.mark.asyncio
    @pytest.mark.parametrize("stale_action", ["CreateStack", "UpdateStack"])
    async def test_stale_create_or_update_context_is_not_consumed_by_delete_stack_terminal_status(
        self, tool: RosStack, mock_credentials, stale_action: str
    ) -> None:
        mock_client = MagicMock()

        action_response = MagicMock()
        action_response.body.stack_id = "stack-123"
        if stale_action == "CreateStack":
            mock_client.create_stack.return_value = action_response
            stale_params = {"StackName": "test", "TemplateBody": "{}"}
        else:
            mock_client.update_stack.return_value = action_response
            stale_params = {"StackId": "stack-123", "TemplateBody": "{}"}

        delete_get_stack_response = MagicMock()
        delete_get_stack_response.body.to_map.return_value = {
            "StackId": "stack-123",
            "StackName": "test",
            "Status": "DELETE_COMPLETE",
            "StatusReason": "",
        }
        mock_client.list_stack_resources.return_value.body.to_map.return_value = {"Resources": []}

        events: list[tuple[str, dict]] = []

        with (
            patch("iac_code.tools.cloud.aliyun.ros_stack.RosClientFactory") as mock_factory,
            patch("iac_code.tools.cloud.aliyun.api_hooks.run_hooks", return_value=None),
            patch(
                "iac_code.tools.cloud.aliyun.ros_stack.log_event",
                side_effect=lambda event_name, metadata=None: events.append((event_name, metadata or {})),
            ),
        ):
            mock_factory.create.return_value = mock_client
            mock_client.get_stack.side_effect = RuntimeError("status unavailable")
            stale_result = await tool.execute(
                tool_input={
                    "action": stale_action,
                    "params": stale_params,
                    "region_id": "cn-hangzhou",
                },
                context=ToolContext(),
            )

            events.clear()
            mock_client.get_stack.side_effect = None
            mock_client.get_stack.return_value = delete_get_stack_response
            delete_result = await tool.execute(
                tool_input={
                    "action": "DeleteStack",
                    "params": {"StackId": "stack-123"},
                    "region_id": "cn-hangzhou",
                },
                context=ToolContext(),
            )

        assert stale_result.is_error is True
        assert delete_result.is_error is False
        assert not [metadata for event_name, metadata in events if event_name == Events.DEPLOYMENT_SUCCEEDED]
        assert not [metadata for event_name, metadata in events if event_name == Events.DEPLOYMENT_FAILED]

    @pytest.mark.asyncio
    async def test_delete_stack_does_not_pop_stale_create_stack_context(self, tool: RosStack, mock_credentials) -> None:
        mock_client = MagicMock()
        mock_client.delete_stack.return_value = None

        get_stack_response = MagicMock()
        get_stack_response.body.to_map.return_value = {
            "StackId": "stack-123",
            "StackName": "test",
            "Status": "DELETE_COMPLETE",
            "StatusReason": "",
        }
        mock_client.get_stack.return_value = get_stack_response

        list_resources_response = MagicMock()
        list_resources_response.body.to_map.return_value = {"Resources": []}
        mock_client.list_stack_resources.return_value = list_resources_response

        tool._store_deployment_telemetry_context(
            "stack-123",
            action="CreateStack",
            iac_kind="ros",
            region="cn-hangzhou",
            started_at=0,
            resource_count_total=0,
            resource_types=[],
            resource_type_counts=[],
            terraform_providers=[],
        )

        with (
            patch("iac_code.tools.cloud.aliyun.ros_stack.RosClientFactory") as mock_factory,
            patch("iac_code.tools.cloud.aliyun.api_hooks.run_hooks", return_value=None),
        ):
            mock_factory.create.return_value = mock_client
            result = await tool.execute(
                tool_input={
                    "action": "DeleteStack",
                    "params": {"StackId": "stack-123"},
                    "region_id": "cn-hangzhou",
                },
                context=ToolContext(),
            )

        assert result.is_error is False
        assert any(context["action"] == "CreateStack" for context in tool._deployment_telemetry_contexts().values())

    @pytest.mark.asyncio
    async def test_terminal_status_emits_deployment_telemetry_when_resource_polling_fails(
        self, tool: RosStack, mock_credentials
    ) -> None:
        mock_client = MagicMock()

        create_response = MagicMock()
        create_response.body.stack_id = "stack-123"
        mock_client.create_stack.return_value = create_response

        get_stack_response = MagicMock()
        get_stack_response.body.to_map.return_value = {
            "StackId": "stack-123",
            "StackName": "test",
            "Status": "CREATE_COMPLETE",
            "StatusReason": "",
        }
        mock_client.get_stack.return_value = get_stack_response
        mock_client.list_stack_resources.side_effect = RuntimeError("resources unavailable")

        events: list[tuple[str, dict]] = []

        with (
            patch("iac_code.tools.cloud.aliyun.ros_stack.RosClientFactory") as mock_factory,
            patch("iac_code.tools.cloud.aliyun.api_hooks.run_hooks", return_value=None),
            patch(
                "iac_code.tools.cloud.aliyun.ros_stack.log_event",
                side_effect=lambda event_name, metadata=None: events.append((event_name, metadata or {})),
            ),
        ):
            mock_factory.create.return_value = mock_client
            result = await tool.execute(
                tool_input={
                    "action": "CreateStack",
                    "params": {"StackName": "test", "TemplateBody": "{}"},
                    "region_id": "cn-hangzhou",
                },
                context=ToolContext(),
            )

        assert result.is_error is False
        succeeded_events = [metadata for event_name, metadata in events if event_name == Events.DEPLOYMENT_SUCCEEDED]
        assert len(succeeded_events) == 1
        assert succeeded_events[0]["stack_status"] == "CREATE_COMPLETE"

    @pytest.mark.asyncio
    async def test_terminal_telemetry_hook_failure_does_not_flip_successful_result(
        self, tool: RosStack, mock_credentials
    ) -> None:
        mock_client = MagicMock()

        create_response = MagicMock()
        create_response.body.stack_id = "stack-123"
        mock_client.create_stack.return_value = create_response

        get_stack_response = MagicMock()
        get_stack_response.body.to_map.return_value = {
            "StackId": "stack-123",
            "StackName": "test",
            "Status": "CREATE_COMPLETE",
            "StatusReason": "",
        }
        mock_client.get_stack.return_value = get_stack_response

        list_resources_response = MagicMock()
        list_resources_response.body.to_map.return_value = {"Resources": []}
        mock_client.list_stack_resources.return_value = list_resources_response

        def flaky_log_event(event_name: str, metadata: dict | None = None) -> None:
            if event_name == Events.DEPLOYMENT_SUCCEEDED:
                raise RuntimeError("telemetry unavailable")

        with (
            patch("iac_code.tools.cloud.aliyun.ros_stack.RosClientFactory") as mock_factory,
            patch("iac_code.tools.cloud.aliyun.api_hooks.run_hooks", return_value=None),
            patch("iac_code.tools.cloud.aliyun.ros_stack.log_event", side_effect=flaky_log_event),
        ):
            mock_factory.create.return_value = mock_client
            result = await tool.execute(
                tool_input={
                    "action": "CreateStack",
                    "params": {"StackName": "test", "TemplateBody": "{}"},
                    "region_id": "cn-hangzhou",
                },
                context=ToolContext(),
            )

        assert result.is_error is False

    @pytest.mark.asyncio
    async def test_terminal_telemetry_event_failure_does_not_skip_remaining_metrics(
        self, tool: RosStack, mock_credentials
    ) -> None:
        mock_client = MagicMock()

        create_response = MagicMock()
        create_response.body.stack_id = "stack-123"
        mock_client.create_stack.return_value = create_response

        get_stack_response = MagicMock()
        get_stack_response.body.to_map.return_value = {
            "StackId": "stack-123",
            "StackName": "test",
            "Status": "CREATE_COMPLETE",
            "StatusReason": "",
        }
        mock_client.get_stack.return_value = get_stack_response

        list_resources_response = MagicMock()
        list_resources_response.body.to_map.return_value = {"Resources": []}
        mock_client.list_stack_resources.return_value = list_resources_response

        metrics: list[tuple[str, int, dict]] = []

        def flaky_log_event(event_name: str, metadata: dict | None = None) -> None:
            if event_name == Events.DEPLOYMENT_SUCCEEDED:
                raise RuntimeError("telemetry unavailable")

        with (
            patch("iac_code.tools.cloud.aliyun.ros_stack.RosClientFactory") as mock_factory,
            patch("iac_code.tools.cloud.aliyun.api_hooks.run_hooks", return_value=None),
            patch("iac_code.tools.cloud.aliyun.ros_stack.log_event", side_effect=flaky_log_event),
            patch(
                "iac_code.tools.cloud.aliyun.ros_stack.add_metric",
                side_effect=lambda name, value, attrs=None: metrics.append((name, value, attrs or {})),
            ),
        ):
            mock_factory.create.return_value = mock_client
            result = await tool.execute(
                tool_input={
                    "action": "CreateStack",
                    "params": {"StackName": "test", "TemplateBody": "{}"},
                    "region_id": "cn-hangzhou",
                },
                context=ToolContext(),
            )

        assert result.is_error is False
        assert ("stack-123", "CreateStack") not in tool._deployment_telemetry_contexts()
        assert any(
            name == Metrics.DEPLOYMENT_COUNT and attrs == {"kind": "ros", "outcome": "success"}
            for name, _, attrs in metrics
        )


class TestRosStackExtra:
    @pytest.fixture
    def stack(self, monkeypatch):
        from iac_code.tools.cloud.aliyun.ros_stack import RosStack

        s = RosStack()
        # Short-circuit client construction
        monkeypatch.setattr(s, "_get_client", lambda region: _FakeRosClient())
        # Bypass pre-call hooks in unit tests
        monkeypatch.setattr("iac_code.tools.cloud.aliyun.api_hooks.run_hooks", lambda *a, **kw: None)
        return s

    @pytest.mark.asyncio
    async def test_continue_create_stack(self, stack):
        result = await stack.call_action(
            "ContinueCreateStack", {"StackId": "sx", "RegionId": "cn-hangzhou"}, "cn-hangzhou"
        )
        assert result == "stack-fake"

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        ("action", "params", "sdk_method"),
        [
            ("CreateStack", {"StackName": "n", "TemplateBody": "{}"}, "create_stack"),
            ("UpdateStack", {"StackId": "sx", "TemplateBody": "{}"}, "update_stack"),
            ("ContinueCreateStack", {"StackId": "sx"}, "continue_create_stack"),
            ("DeleteStack", {"StackId": "sx"}, "delete_stack"),
        ],
    )
    async def test_call_action_offloads_blocking_sdk_calls_to_thread(
        self, stack, monkeypatch, action, params, sdk_method
    ):
        to_thread_calls: list[str] = []

        async def record_to_thread(func, *args, **kwargs):
            to_thread_calls.append(func.__name__)
            return func(*args, **kwargs)

        monkeypatch.setattr("iac_code.tools.cloud.aliyun.ros_stack.asyncio.to_thread", record_to_thread)

        result = await stack.call_action(action, params, "cn-hangzhou")

        expected = "sx" if action == "DeleteStack" else "stack-fake"
        assert result == expected
        assert sdk_method in to_thread_calls

    @pytest.mark.asyncio
    async def test_delete_stack_returns_stack_id(self, stack):
        result = await stack.call_action("DeleteStack", {"StackId": "sx"}, "cn-hangzhou")
        assert result == "sx"

    @pytest.mark.asyncio
    async def test_template_url_local_file_read(self, stack, tmp_path):
        tpl = tmp_path / "tpl.json"
        tpl.write_text('{"ROSTemplateFormatVersion": "2015-09-01"}', encoding="utf-8")
        result = await stack.call_action(
            "CreateStack",
            {"StackName": "n", "TemplateURL": str(tpl)},
            "cn-hangzhou",
        )
        assert result == "stack-fake"

    @pytest.mark.asyncio
    async def test_template_body_dict_to_json(self, stack):
        result = await stack.call_action(
            "CreateStack",
            {"StackName": "n", "TemplateBody": {"ROSTemplateFormatVersion": "2015-09-01"}},
            "cn-hangzhou",
        )
        assert result == "stack-fake"

    @pytest.mark.asyncio
    async def test_unsupported_action_raises(self, stack):
        with pytest.raises(ValueError, match="Unsupported"):
            await stack.call_action("MakeCoffee", {}, "cn-hangzhou")

    def test_user_facing_name(self):
        from iac_code.tools.cloud.aliyun.ros_stack import RosStack

        assert RosStack().user_facing_name() == "ROS Stack"

    def test_supported_actions(self):
        from iac_code.tools.cloud.aliyun.ros_stack import RosStack

        s = RosStack()
        assert "CreateStack" in s.supported_actions
        assert "DeleteStack" in s.supported_actions

    def test_provider_name(self):
        from iac_code.tools.cloud.aliyun.ros_stack import RosStack

        assert RosStack().provider_name == "ros"

    def test_get_default_region_no_cred(self, monkeypatch):
        from iac_code.tools.cloud.aliyun.ros_stack import RosStack

        class FakeCreds:
            def get_provider(self, name):
                return None

        monkeypatch.setattr("iac_code.tools.cloud.aliyun.ros_stack.CloudCredentials", lambda: FakeCreds())
        assert RosStack()._get_default_region() == ""

    @pytest.mark.asyncio
    async def test_get_stack_status_offloads_blocking_sdk_call_to_thread(self, stack, monkeypatch):
        to_thread_calls: list[str] = []

        async def record_to_thread(func, *args, **kwargs):
            to_thread_calls.append(func.__name__)
            return func(*args, **kwargs)

        monkeypatch.setattr("iac_code.tools.cloud.aliyun.ros_stack.asyncio.to_thread", record_to_thread)

        status = await stack.get_stack_status("sx", "cn-hangzhou")

        assert status.stack_id == "sx"
        assert "get_stack" in to_thread_calls

    @pytest.mark.asyncio
    async def test_get_stack_resources_offloads_blocking_sdk_call_to_thread(self, stack, monkeypatch):
        to_thread_calls: list[str] = []

        async def record_to_thread(func, *args, **kwargs):
            to_thread_calls.append(func.__name__)
            return func(*args, **kwargs)

        monkeypatch.setattr("iac_code.tools.cloud.aliyun.ros_stack.asyncio.to_thread", record_to_thread)

        resources = await stack.get_stack_resources("sx", "cn-hangzhou")

        assert resources == []
        assert "list_stack_resources" in to_thread_calls

    @pytest.mark.asyncio
    async def test_create_stack_parameters_survive_hooks_for_typed_sdk(self, monkeypatch):
        from iac_code.tools.cloud.aliyun.ros_stack import RosStack

        client = _FakeRosClient()
        s = RosStack()
        monkeypatch.setattr(s, "_get_client", lambda region: client)

        result = await s.call_action(
            "CreateStack",
            {
                "StackName": "n",
                "TemplateBody": _MINIMAL_TEMPLATE_BODY,
                "Parameters": [
                    {"ParameterKey": "VpcId", "ParameterValue": "vpc-123"},
                    {"ParameterKey": "ZoneId", "ParameterValue": "cn-hangzhou-h"},
                ],
            },
            "cn-hangzhou",
        )

        assert result == "stack-fake"
        assert client.create_request is not None
        assert client.create_request.to_map()["Parameters"] == [
            {"ParameterKey": "VpcId", "ParameterValue": "vpc-123"},
            {"ParameterKey": "ZoneId", "ParameterValue": "cn-hangzhou-h"},
        ]

    @pytest.mark.asyncio
    async def test_create_stack_flat_parameters_are_restored_for_typed_sdk(self, monkeypatch):
        from iac_code.tools.cloud.aliyun.ros_stack import RosStack

        client = _FakeRosClient()
        s = RosStack()
        monkeypatch.setattr(s, "_get_client", lambda region: client)

        result = await s.call_action(
            "CreateStack",
            {
                "StackName": "n",
                "TemplateBody": _MINIMAL_TEMPLATE_BODY,
                "Parameters.1.ParameterKey": "VpcId",
                "Parameters.1.ParameterValue": "vpc-123",
            },
            "cn-hangzhou",
        )

        assert result == "stack-fake"
        assert client.create_request is not None
        assert client.create_request.to_map()["Parameters"] == [
            {"ParameterKey": "VpcId", "ParameterValue": "vpc-123"},
        ]

    @pytest.mark.asyncio
    async def test_update_stack_flat_parameters_are_restored_for_typed_sdk(self, monkeypatch):
        from iac_code.tools.cloud.aliyun.ros_stack import RosStack

        client = _FakeRosClient()
        s = RosStack()
        monkeypatch.setattr(s, "_get_client", lambda region: client)

        result = await s.call_action(
            "UpdateStack",
            {
                "StackId": "stack-123",
                "TemplateBody": _MINIMAL_TEMPLATE_BODY,
                "Parameters.1.ParameterKey": "VpcId",
                "Parameters.1.ParameterValue": "vpc-123",
            },
            "cn-hangzhou",
        )

        assert result == "stack-fake"
        assert client.update_request is not None
        assert client.update_request.to_map()["Parameters"] == [
            {"ParameterKey": "VpcId", "ParameterValue": "vpc-123"},
        ]


class _FakeRosClient:
    def __init__(self):
        self.create_request = None
        self.update_request = None

    def create_stack(self, req):
        self.create_request = req
        return _FakeResp("stack-fake")

    def update_stack(self, req):
        self.update_request = req
        return _FakeResp("stack-fake")

    def continue_create_stack(self, req):
        return _FakeResp("stack-fake")

    def delete_stack(self, req):
        return None

    def get_stack(self, req):
        return _FakeResp("stack-fake")

    def list_stack_resources(self, req):
        return _FakeResp("stack-fake")


class _FakeResp:
    def __init__(self, stack_id: str):
        self.body = _FakeBody(stack_id)


class _FakeBody:
    def __init__(self, stack_id: str):
        self.stack_id = stack_id

    def to_map(self):
        return {}
