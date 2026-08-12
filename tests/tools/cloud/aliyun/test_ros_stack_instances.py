"""Tests for RosStackInstances tool."""

from __future__ import annotations

import asyncio
import threading
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from iac_code.services.providers.aliyun_credentials_runtime import (
    ECS_METADATA_UNREACHABLE,
    ECS_RAM_ROLE_NOT_FOUND,
)
from iac_code.tools.base import ToolContext
from iac_code.tools.cloud.aliyun.ros_client import RosClientFactory
from iac_code.tools.cloud.aliyun.ros_stack_instances import RosStackInstances
from iac_code.tools.cloud.types import InstanceStatus
from iac_code.types.stream_events import StackInstancesProgressEvent
from tests.tools.cloud.aliyun._ecs_ram_role_fakes import FakeEcsRuntime


def _sdk_envelope(inner: BaseException):
    """Wrap `inner` the way the ROS SDK does when signing fails inside a call."""
    from darabonba.exceptions import UnretryableException
    from darabonba.policy.retry import RetryPolicyContext
    from Tea.request import TeaRequest

    return UnretryableException(RetryPolicyContext(http_request=TeaRequest(), exception=inner))


class _ThreadRecordingClient:
    """Fake ROS client that records which thread each blocking SDK call ran on."""

    def __init__(self) -> None:
        self.threads: dict[str, int] = {}

    def _record(self, name: str) -> Any:
        self.threads[name] = threading.get_ident()
        response = MagicMock()
        response.body.operation_id = "op-1"
        response.body.to_map.return_value = (
            {"StackGroupOperation": {"Status": "SUCCEEDED"}}
            if name == "get_stack_group_operation"
            else {"StackInstances": []}
        )
        return response

    def create_stack_instances(self, request: Any) -> Any:
        return self._record("create_stack_instances")

    def update_stack_instances(self, request: Any) -> Any:
        return self._record("update_stack_instances")

    def delete_stack_instances(self, request: Any) -> Any:
        return self._record("delete_stack_instances")

    def get_stack_group_operation(self, request: Any) -> Any:
        return self._record("get_stack_group_operation")

    def list_stack_instances(self, request: Any) -> Any:
        return self._record("list_stack_instances")


@pytest.fixture
def mock_credentials():
    with patch("iac_code.tools.cloud.aliyun.ros_stack_instances.CloudCredentials") as mock:
        cred = MagicMock()
        cred.access_key_id = "test-ak"
        cred.access_key_secret = "test-secret"
        cred.region_id = "cn-hangzhou"
        instance = mock.return_value
        instance.get_provider.return_value = cred
        yield instance


@pytest.fixture
def tool(monkeypatch) -> RosStackInstances:
    monkeypatch.setattr(RosStackInstances, "poll_interval", 0)
    return RosStackInstances()


@pytest.fixture
def context() -> ToolContext:
    return ToolContext()


class TestRosStackInstancesProperties:
    def test_name(self, tool: RosStackInstances) -> None:
        assert tool.name == "ros_stack_instances"

    def test_supported_actions(self, tool: RosStackInstances) -> None:
        assert tool.supported_actions == [
            "CreateStackInstances",
            "UpdateStackInstances",
            "DeleteStackInstances",
        ]

    def test_is_destructive(self, tool: RosStackInstances) -> None:
        assert tool.is_destructive({"action": "CreateStackInstances"}) is True
        assert tool.is_destructive({"action": "DeleteStackInstances"}) is True

    def test_misc_properties_and_region_resolution(self, tool: RosStackInstances, mock_credentials) -> None:
        assert tool.timeout == 3600.0
        assert tool.is_read_only({}) is False
        assert tool.is_concurrency_safe({}) is False
        assert tool.user_facing_name({}) == "CloudStackInstances"
        assert tool._resolve_region({"region_id": "cn-shanghai"}) == "cn-shanghai"
        assert tool._resolve_region({}) == "cn-hangzhou"

    def test_input_schema_and_messages_include_default_region(self, tool: RosStackInstances, mock_credentials) -> None:
        schema = tool.input_schema
        assert "Defaults to 'cn-hangzhou'." in schema["properties"]["region_id"]["description"]
        assert tool.render_tool_use_message({"action": "CreateStackInstances", "region_id": "cn-shanghai"}) == (
            "CreateStackInstances cn-shanghai"
        )
        assert tool.render_tool_use_message({}) == "cn-hangzhou"
        assert tool.get_activity_description(None) is None
        assert "CreateStackInstances cn-shanghai" in tool.get_activity_description(
            {"action": "CreateStackInstances", "region_id": "cn-shanghai"}
        )

    @pytest.mark.asyncio
    async def test_initiate_update_delete_and_status_helpers(self, tool: RosStackInstances) -> None:
        client = MagicMock()

        update_response = MagicMock()
        update_response.body.operation_id = "op-update"
        delete_response = MagicMock()
        delete_response.body.operation_id = "op-delete"
        client.update_stack_instances.return_value = update_response
        client.delete_stack_instances.return_value = delete_response

        op_response = MagicMock()
        op_response.body.to_map.return_value = {"StackGroupOperation": {"Status": "SUCCEEDED"}}
        client.get_stack_group_operation.return_value = op_response

        list_response = MagicMock()
        list_response.body.to_map.return_value = {
            "StackInstances": [
                {
                    "AccountId": "123",
                    "RegionId": "cn-hz",
                    "Status": "CURRENT",
                    "StatusReason": "ok",
                    "ElapsedSeconds": 8,
                }
            ]
        }
        client.list_stack_instances.return_value = list_response

        assert await tool._initiate(client, "UpdateStackInstances", {"StackGroupName": "demo"}) == "op-update"
        assert await tool._initiate(client, "DeleteStackInstances", {"StackGroupName": "demo"}) == "op-delete"
        assert await tool._get_operation_status(client, "op-1", "cn-hz") == "SUCCEEDED"
        instances = await tool._get_instances(client, "demo", "cn-hz")
        assert instances == [
            InstanceStatus(
                account_id="123",
                region_id="cn-hz",
                status="CURRENT",
                status_reason="ok",
                elapsed_seconds=8,
            )
        ]

    @pytest.mark.asyncio
    async def test_initiate_unsupported_action_raises(self, tool: RosStackInstances) -> None:
        with pytest.raises(ValueError):
            await tool._initiate(MagicMock(), "UnknownAction", {})


class TestRosStackInstancesExecute:
    @pytest.mark.asyncio
    async def test_execute_unsupported_action(self, tool: RosStackInstances, context: ToolContext) -> None:
        result = await tool.execute(
            tool_input={"action": "ListStackInstances"},
            context=context,
        )
        assert result.is_error is True
        assert "ListStackInstances" in result.content

    @pytest.mark.asyncio
    async def test_execute_create_instances(self, tool: RosStackInstances, mock_credentials) -> None:
        mock_client = MagicMock()

        # create_stack_instances returns operation_id
        create_response = MagicMock()
        create_response.body.operation_id = "op-123"
        mock_client.create_stack_instances.return_value = create_response

        # get_stack_group_operation returns SUCCEEDED status
        get_operation_response = MagicMock()
        get_operation_response.body.to_map.return_value = {"StackGroupOperation": {"Status": "SUCCEEDED"}}
        mock_client.get_stack_group_operation.return_value = get_operation_response

        # list_stack_instances returns one instance
        list_instances_response = MagicMock()
        list_instances_response.body.to_map.return_value = {
            "StackInstances": [
                {
                    "AccountId": "123456789",
                    "RegionId": "cn-hangzhou",
                    "Status": "SUCCEEDED",
                    "StatusReason": "",
                    "DriftDetectionTime": None,
                }
            ]
        }
        mock_client.list_stack_instances.return_value = list_instances_response

        queue: asyncio.Queue = asyncio.Queue()
        ctx = ToolContext(event_queue=queue, tool_use_id="toolu-instances")

        with patch("iac_code.tools.cloud.aliyun.ros_stack_instances.RosClientFactory") as mock_factory:
            mock_factory.create.return_value = mock_client
            result = await tool.execute(
                tool_input={
                    "action": "CreateStackInstances",
                    "params": {
                        "StackGroupName": "test-group",
                        "AccountIds": ["123456789"],
                        "RegionIds": ["cn-hangzhou"],
                    },
                    "region_id": "cn-hangzhou",
                },
                context=ctx,
            )

        assert result.is_error is False
        mock_client.create_stack_instances.assert_called_once()
        create_request = mock_client.create_stack_instances.call_args.args[0]
        assert create_request.region_id == "cn-hangzhou"
        mock_client.get_stack_group_operation.assert_called()
        mock_client.list_stack_instances.assert_called()

        # Verify StackInstancesProgressEvent was emitted
        events = []
        while not queue.empty():
            events.append(await queue.get())

        progress_events = [e for e in events if isinstance(e, StackInstancesProgressEvent)]
        assert len(progress_events) >= 1
        first = progress_events[0]
        assert first.operation_id == "op-123"
        assert first.tool_use_id == "toolu-instances"
        assert first.status == "SUCCEEDED"
        assert len(first.instances) == 1
        assert first.instances[0]["account_id"] == "123456789"
        assert first.instances[0]["region_id"] == "cn-hangzhou"
        assert first.instances[0]["status"] == "SUCCEEDED"

    @pytest.mark.asyncio
    async def test_execute_reacquires_clients_while_polling(self, tool: RosStackInstances) -> None:
        initiate_client = MagicMock(name="initiate-client")
        first_poll_client = MagicMock(name="first-poll-client")
        second_poll_client = MagicMock(name="second-poll-client")

        clients = [initiate_client, first_poll_client, second_poll_client]
        statuses = ["RUNNING", "SUCCEEDED"]
        status_clients = []
        instance_clients = []

        async def fake_get_operation_status(client, operation_id, region):
            status_clients.append(client)
            return statuses.pop(0)

        async def fake_get_instances(client, stack_group_name, region):
            instance_clients.append(client)
            return []

        with (
            patch.object(tool, "_get_client", side_effect=clients),
            patch.object(tool, "_initiate", return_value="op-1"),
            patch.object(tool, "_get_operation_status", side_effect=fake_get_operation_status),
            patch.object(tool, "_get_instances", side_effect=fake_get_instances),
        ):
            result = await tool.execute(
                tool_input={"action": "CreateStackInstances", "params": {"StackGroupName": "demo"}},
                context=ToolContext(),
            )

        assert result.is_error is False
        assert status_clients == [first_poll_client, second_poll_client]
        assert instance_clients == [first_poll_client, second_poll_client]

    @pytest.mark.asyncio
    async def test_execute_returns_initiate_error(self, tool: RosStackInstances) -> None:
        with (
            patch.object(tool, "_get_client", return_value=MagicMock()),
            patch.object(tool, "_initiate", side_effect=RuntimeError("boom")),
        ):
            result = await tool.execute(
                tool_input={"action": "CreateStackInstances", "params": {"StackGroupName": "demo"}},
                context=ToolContext(),
            )

        assert result.is_error is True
        assert "[CreateStackInstances] boom" in result.content

    @pytest.mark.asyncio
    async def test_execute_returns_status_error(self, tool: RosStackInstances) -> None:
        with (
            patch.object(tool, "_get_client", return_value=MagicMock()),
            patch.object(tool, "_initiate", return_value="op-1"),
            patch.object(tool, "_get_operation_status", side_effect=RuntimeError("status failed")),
        ):
            result = await tool.execute(
                tool_input={"action": "CreateStackInstances", "params": {"StackGroupName": "demo"}},
                context=ToolContext(),
            )

        assert result.is_error is True
        assert "[GetStackGroupOperation] status failed" in result.content

    @pytest.mark.asyncio
    async def test_execute_returns_instances_error(self, tool: RosStackInstances) -> None:
        with (
            patch.object(tool, "_get_client", return_value=MagicMock()),
            patch.object(tool, "_initiate", return_value="op-1"),
            patch.object(tool, "_get_operation_status", return_value="RUNNING"),
            patch.object(tool, "_get_instances", side_effect=RuntimeError("instances failed")),
        ):
            result = await tool.execute(
                tool_input={"action": "CreateStackInstances", "params": {"StackGroupName": "demo"}},
                context=ToolContext(),
            )

        assert result.is_error is True
        assert "[ListStackInstances] instances failed" in result.content

    @pytest.mark.asyncio
    async def test_execute_returns_error_result_for_failed_terminal_status(self, tool: RosStackInstances) -> None:
        with (
            patch.object(tool, "_get_client", return_value=MagicMock()),
            patch.object(tool, "_initiate", return_value="op-1"),
            patch.object(tool, "_get_operation_status", return_value="FAILED"),
            patch.object(
                tool,
                "_get_instances",
                return_value=[
                    InstanceStatus(
                        account_id="123", region_id="cn-hz", status="FAILED", status_reason="", elapsed_seconds=1
                    )
                ],
            ),
        ):
            result = await tool.execute(
                tool_input={"action": "CreateStackInstances", "params": {"StackGroupName": "demo"}},
                context=ToolContext(),
            )

        assert result.is_error is True
        assert '"status": "FAILED"' in result.content

    @pytest.mark.asyncio
    async def test_execute_handles_empty_instances_without_queue(self, tool: RosStackInstances) -> None:
        with (
            patch.object(tool, "_get_client", return_value=MagicMock()),
            patch.object(tool, "_initiate", return_value="op-1"),
            patch.object(tool, "_get_operation_status", return_value="SUCCEEDED"),
            patch.object(tool, "_get_instances", return_value=[]),
        ):
            result = await tool.execute(
                tool_input={"action": "CreateStackInstances", "params": {"StackGroupName": "demo"}},
                context=ToolContext(),
            )

        assert result.is_error is False
        assert '"progress_percentage": 0' in result.content


class TestRosStackInstancesRenderToolResultMessage:
    def test_compact_mode_shows_summary(self, tool: RosStackInstances) -> None:
        import json

        output = json.dumps({"stack_group_name": "my-group", "status": "SUCCEEDED", "elapsed_seconds": 30})
        result = tool.render_tool_result_message(output)
        assert result == "my-group SUCCEEDED (30s)"

    def test_verbose_mode_shows_full_output(self, tool: RosStackInstances) -> None:
        import json

        output = json.dumps({"stack_group_name": "my-group", "status": "SUCCEEDED", "elapsed_seconds": 30})
        result = tool.render_tool_result_message(output, verbose=True)
        assert result == output.strip()

    def test_invalid_json_and_trimmed_output(self, tool: RosStackInstances) -> None:
        text = "x" * 300
        assert tool.render_tool_result_message("not-json") == "not-json"
        assert tool.render_tool_result_message(text) == text[:200]


class TestRosStackInstancesOffloadsBlockingSdkCalls:
    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        ("action", "method"),
        [
            ("CreateStackInstances", "create_stack_instances"),
            ("UpdateStackInstances", "update_stack_instances"),
            ("DeleteStackInstances", "delete_stack_instances"),
        ],
    )
    async def test_initiate_calls_the_sdk_off_the_event_loop(
        self, tool: RosStackInstances, action: str, method: str
    ) -> None:
        client = _ThreadRecordingClient()

        assert await tool._initiate(client, action, {"StackGroupName": "demo"}) == "op-1"

        assert client.threads[method] != threading.get_ident()

    @pytest.mark.asyncio
    async def test_polling_calls_the_sdk_off_the_event_loop(self, tool: RosStackInstances) -> None:
        client = _ThreadRecordingClient()
        loop_thread = threading.get_ident()

        assert await tool._get_operation_status(client, "op-1", "cn-shanghai") == "SUCCEEDED"
        assert await tool._get_instances(client, "demo", "cn-shanghai") == []

        assert client.threads["get_stack_group_operation"] != loop_thread
        assert client.threads["list_stack_instances"] != loop_thread

    @pytest.mark.asyncio
    async def test_every_sdk_call_goes_through_to_thread(self, tool: RosStackInstances, monkeypatch) -> None:
        client = _ThreadRecordingClient()
        offloaded: list[str] = []
        real_to_thread = asyncio.to_thread

        async def recording_to_thread(func, /, *args, **kwargs):
            offloaded.append(func.__name__)
            return await real_to_thread(func, *args, **kwargs)

        monkeypatch.setattr(asyncio, "to_thread", recording_to_thread)

        await tool._initiate(client, "CreateStackInstances", {"StackGroupName": "demo"})
        await tool._initiate(client, "UpdateStackInstances", {"StackGroupName": "demo"})
        await tool._initiate(client, "DeleteStackInstances", {"StackGroupName": "demo"})
        await tool._get_operation_status(client, "op-1", "cn-shanghai")
        await tool._get_instances(client, "demo", "cn-shanghai")

        assert offloaded == [
            "create_stack_instances",
            "update_stack_instances",
            "delete_stack_instances",
            "get_stack_group_operation",
            "list_stack_instances",
        ]


class TestRosStackInstancesEcsCredentialErrors:
    @pytest.mark.asyncio
    async def test_initiate_credential_failure_becomes_public_text(self, tool: RosStackInstances) -> None:
        with (
            patch.object(tool, "_get_client", return_value=MagicMock()),
            patch.object(tool, "_initiate", side_effect=ValueError(ECS_RAM_ROLE_NOT_FOUND)),
        ):
            result = await tool.execute(
                tool_input={"action": "CreateStackInstances", "params": {"StackGroupName": "demo"}},
                context=ToolContext(),
            )

        assert result.is_error is True
        assert "ECS instance RAM role was found" in result.content
        assert ECS_RAM_ROLE_NOT_FOUND not in result.content
        # The public message replaces the raw "[action] error" form entirely.
        assert "[CreateStackInstances]" not in result.content

    @pytest.mark.asyncio
    async def test_polling_credential_failure_becomes_public_text(self, tool: RosStackInstances) -> None:
        with (
            patch.object(tool, "_get_client", return_value=MagicMock()),
            patch.object(tool, "_initiate", return_value="op-1"),
            patch.object(tool, "_get_operation_status", side_effect=ValueError(ECS_METADATA_UNREACHABLE)),
        ):
            result = await tool.execute(
                tool_input={"action": "CreateStackInstances", "params": {"StackGroupName": "demo"}},
                context=ToolContext(),
            )

        assert result.is_error is True
        assert "instance metadata service is unreachable" in result.content
        assert "GetStackGroupOperation" in result.content
        assert ECS_METADATA_UNREACHABLE not in result.content

    @pytest.mark.asyncio
    async def test_unrelated_value_error_keeps_the_existing_shape(self, tool: RosStackInstances) -> None:
        with (
            patch.object(tool, "_get_client", return_value=MagicMock()),
            patch.object(tool, "_initiate", side_effect=ValueError("bad params")),
        ):
            result = await tool.execute(
                tool_input={"action": "CreateStackInstances", "params": {"StackGroupName": "demo"}},
                context=ToolContext(),
            )

        assert result.is_error is True
        assert "[CreateStackInstances] bad params" in result.content

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        ("method", "action"),
        [
            ("create_stack_instances", "CreateStackInstances"),
            ("get_stack_group_operation", "GetStackGroupOperation"),
            ("list_stack_instances", "ListStackInstances"),
        ],
    )
    async def test_wrapped_signing_failures_become_public_text(
        self, tool: RosStackInstances, method: str, action: str
    ) -> None:
        client = MagicMock()
        create_response = MagicMock()
        create_response.body.operation_id = "op-1"
        client.create_stack_instances.return_value = create_response
        status_response = MagicMock()
        status_response.body.to_map.return_value = {"StackGroupOperation": {"Status": "RUNNING"}}
        client.get_stack_group_operation.return_value = status_response
        list_response = MagicMock()
        list_response.body.to_map.return_value = {"StackInstances": []}
        client.list_stack_instances.return_value = list_response
        # Signing happens inside the SDK call, so the stable code arrives wrapped.
        getattr(client, method).side_effect = _sdk_envelope(ValueError(ECS_METADATA_UNREACHABLE))

        with patch.object(tool, "_get_client", return_value=client):
            result = await tool.execute(
                tool_input={
                    "action": "CreateStackInstances",
                    "params": {"StackGroupName": "demo"},
                    "region_id": "cn-shanghai",
                },
                context=ToolContext(),
            )

        assert result.is_error is True
        assert "instance metadata service is unreachable" in result.content
        assert action in result.content
        assert ECS_METADATA_UNREACHABLE not in result.content

    @pytest.mark.asyncio
    async def test_unrelated_wrapped_failures_keep_the_existing_shape(self, tool: RosStackInstances) -> None:
        client = MagicMock()
        client.create_stack_instances.side_effect = _sdk_envelope(RuntimeError("Throttling.User"))

        with patch.object(tool, "_get_client", return_value=client):
            result = await tool.execute(
                tool_input={"action": "CreateStackInstances", "params": {"StackGroupName": "demo"}},
                context=ToolContext(),
            )

        assert result.is_error is True
        assert "[CreateStackInstances] Throttling.User" in result.content

    @pytest.mark.asyncio
    async def test_instance_metadata_is_never_read_on_the_event_loop(
        self, tool: RosStackInstances, fake_ecs_runtime: FakeEcsRuntime, monkeypatch
    ) -> None:
        """Drive the real ROS SDK with a fake metadata provider: signing fails, nothing is sent."""
        fake_ecs_runtime.fail_with(ValueError(ECS_METADATA_UNREACHABLE))
        credential = fake_ecs_runtime.credential(region_id="cn-shanghai")
        monkeypatch.setattr(tool, "_get_client", lambda region: RosClientFactory.create(credential, region_id=region))
        loop_thread = threading.get_ident()

        result = await tool.execute(
            tool_input={
                "action": "CreateStackInstances",
                "params": {"StackGroupName": "demo"},
                "region_id": "cn-shanghai",
            },
            context=ToolContext(),
        )

        assert result.is_error is True
        assert "instance metadata service is unreachable" in result.content
        assert ECS_METADATA_UNREACHABLE not in result.content
        read_threads = [ident for provider in fake_ecs_runtime.providers for ident in provider.call_threads]
        assert read_threads
        assert loop_thread not in read_threads
