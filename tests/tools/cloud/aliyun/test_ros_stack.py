"""Tests for RosStack tool."""

from __future__ import annotations

import asyncio
from unittest.mock import MagicMock, patch

import pytest

from iac_code.tools.base import ToolContext
from iac_code.tools.cloud.aliyun.ros_stack import RosStack
from iac_code.types.stream_events import StackProgressEvent


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

        with patch("iac_code.tools.cloud.aliyun.ros_stack.RosClientFactory") as mock_factory:
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


class TestRosStackExtra:
    @pytest.fixture
    def stack(self, monkeypatch):
        from iac_code.tools.cloud.aliyun.ros_stack import RosStack

        s = RosStack()
        # Short-circuit client construction
        monkeypatch.setattr(s, "_get_client", lambda region: _FakeRosClient())
        return s

    @pytest.mark.asyncio
    async def test_continue_create_stack(self, stack):
        result = await stack.call_action(
            "ContinueCreateStack", {"StackId": "sx", "RegionId": "cn-hangzhou"}, "cn-hangzhou"
        )
        assert result == "stack-fake"

    @pytest.mark.asyncio
    async def test_delete_stack_returns_stack_id(self, stack):
        result = await stack.call_action("DeleteStack", {"StackId": "sx"}, "cn-hangzhou")
        assert result == "sx"

    @pytest.mark.asyncio
    async def test_template_url_local_file_read(self, stack, tmp_path):
        tpl = tmp_path / "tpl.json"
        tpl.write_text('{"ROSTemplateFormatVersion": "2015-09-01"}')
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


class _FakeRosClient:
    def create_stack(self, req):
        return _FakeResp("stack-fake")

    def update_stack(self, req):
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
