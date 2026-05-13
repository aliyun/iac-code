"""Tests for BaseCloudApi abstract base class."""

from __future__ import annotations

import json

import pytest

from iac_code.tools.base import ToolContext
from iac_code.tools.cloud.base_api import BaseCloudApi


class MockCloudApi(BaseCloudApi):
    @property
    def provider_name(self) -> str:
        return "mock"

    @property
    def supported_actions(self) -> list[str]:
        return ["ListThings", "GetThing"]

    @property
    def description(self) -> str:
        return "Mock cloud API for testing"

    async def call_action(self, action: str, params: dict, region: str) -> dict:
        if action == "ListThings":
            return {"Things": [{"Id": "1"}]}
        if action == "GetThing":
            return {"Thing": {"Id": params.get("ThingId")}}
        raise ValueError(f"Unknown: {action}")


class MockCloudApiWithDefaultRegion(MockCloudApi):
    def _get_default_region(self) -> str:
        return "cn-shanghai"


class MockCloudApiWithSummary(MockCloudApi):
    def _summarize_success_result(self, action: str, result: dict) -> str:
        return f"{action} -> {len(result)} fields"


@pytest.fixture
def api() -> MockCloudApi:
    return MockCloudApi()


@pytest.fixture
def context() -> ToolContext:
    return ToolContext()


class TestBaseCloudApiProperties:
    def test_name_includes_provider(self, api: MockCloudApi) -> None:
        assert api.name == "mock_api"

    def test_input_schema_has_action_enum(self, api: MockCloudApi) -> None:
        schema = api.input_schema
        assert "action" in schema["properties"]
        assert schema["properties"]["action"]["enum"] == ["ListThings", "GetThing"]

    def test_input_schema_has_params(self, api: MockCloudApi) -> None:
        schema = api.input_schema
        assert "params" in schema["properties"]
        assert schema["properties"]["params"]["type"] == "object"

    def test_input_schema_has_region_id(self, api: MockCloudApi) -> None:
        schema = api.input_schema
        assert "region_id" in schema["properties"]
        assert schema["properties"]["region_id"]["type"] == "string"

    def test_input_schema_requires_action(self, api: MockCloudApi) -> None:
        schema = api.input_schema
        assert "action" in schema["required"]

    def test_is_read_only_for_list(self, api: MockCloudApi) -> None:
        assert api.is_read_only({"action": "ListThings"}) is True

    def test_is_read_only_for_get(self, api: MockCloudApi) -> None:
        assert api.is_read_only({"action": "GetThing"}) is True

    def test_is_read_only_for_validate(self, api: MockCloudApi) -> None:
        # Validate* actions perform server-side checks without mutating resources.
        assert api.is_read_only({"action": "ValidateTemplate"}) is True

    def test_is_read_only_false_for_create(self, api: MockCloudApi) -> None:
        assert api.is_read_only({"action": "CreateStack"}) is False

    def test_is_read_only_false_for_delete(self, api: MockCloudApi) -> None:
        assert api.is_read_only({"action": "DeleteStack"}) is False

    def test_is_read_only_false_for_update(self, api: MockCloudApi) -> None:
        assert api.is_read_only({"action": "UpdateStack"}) is False

    def test_is_concurrency_safe_delegates_to_is_read_only(self, api: MockCloudApi) -> None:
        assert api.is_concurrency_safe({"action": "ListThings"}) is True
        assert api.is_concurrency_safe({"action": "CreateStack"}) is False

    def test_user_facing_name_and_messages(self, api: MockCloudApi) -> None:
        assert api.user_facing_name() == "CloudAPI"
        assert api.render_tool_use_message({"action": "ListThings", "region_id": "cn-hz"}) == "ListThings cn-hz"
        assert api.render_tool_use_message({}) is None
        assert api.get_activity_description(None) is None

    def test_default_region_is_used_in_schema_and_messages(self) -> None:
        api = MockCloudApiWithDefaultRegion()
        assert "Defaults to 'cn-shanghai'." in api.input_schema["properties"]["region_id"]["description"]
        assert api._resolve_region({}) == "cn-shanghai"
        assert api.render_tool_use_message({"action": "ListThings"}) == "ListThings cn-shanghai"


class TestBaseCloudApiExecute:
    @pytest.mark.asyncio
    async def test_execute_valid_list_action_returns_success(self, api: MockCloudApi, context: ToolContext) -> None:
        result = await api.execute(
            tool_input={"action": "ListThings"},
            context=context,
        )
        assert result.is_error is False
        data = json.loads(result.content)
        assert data == {"Things": [{"Id": "1"}]}

    @pytest.mark.asyncio
    async def test_execute_valid_get_action_with_params(self, api: MockCloudApi, context: ToolContext) -> None:
        result = await api.execute(
            tool_input={"action": "GetThing", "params": {"ThingId": "42"}},
            context=context,
        )
        assert result.is_error is False
        data = json.loads(result.content)
        assert data == {"Thing": {"Id": "42"}}

    @pytest.mark.asyncio
    async def test_execute_invalid_action_returns_error(self, api: MockCloudApi, context: ToolContext) -> None:
        result = await api.execute(
            tool_input={"action": "UnknownAction"},
            context=context,
        )
        assert result.is_error is True
        assert "UnknownAction" in result.content

    @pytest.mark.asyncio
    async def test_execute_exception_resets_last_result(self, context: ToolContext) -> None:
        class FailingApi(MockCloudApi):
            async def call_action(self, action: str, params: dict, region: str) -> dict:
                raise RuntimeError("bad call")

        api = FailingApi()
        result = await api.execute(tool_input={"action": "ListThings"}, context=context)
        assert result.is_error is True
        assert result.content == "bad call"
        assert getattr(api, "_last_action", None) == ""
        assert getattr(api, "_last_result", None) is None

    def test_get_activity_description(self, api: MockCloudApi) -> None:
        desc = api.get_activity_description({"action": "ListThings"})
        assert desc is not None
        assert "ListThings" in desc


class TestBaseCloudApiRenderToolResultMessage:
    def test_compact_mode_shows_line_count(self, api: MockCloudApi) -> None:
        output = '{\n  "Things": [\n    {"Id": "1"}\n  ]\n}'
        result = api.render_tool_result_message(output)
        assert result == "Received response (5 lines)"

    def test_verbose_mode_shows_full_output(self, api: MockCloudApi) -> None:
        output = '{\n  "Things": [\n    {"Id": "1"}\n  ]\n}'
        result = api.render_tool_result_message(output, verbose=True)
        assert result == output.strip()

    def test_error_long_message_preserved(self, api: MockCloudApi) -> None:
        output = "x" * 300
        result = api.render_tool_result_message(output, is_error=True)
        assert result == output

    def test_error_short_message_preserved(self, api: MockCloudApi) -> None:
        output = "Something went wrong"
        result = api.render_tool_result_message(output, is_error=True)
        assert result == "Something went wrong"

    def test_error_message_is_cleaned(self, api: MockCloudApi) -> None:
        output = "Boom Response: {'raw': true}"
        result = api.render_tool_result_message(output, is_error=True)
        assert result == "Boom"

    def test_success_summary_uses_cached_action_and_result(self) -> None:
        api = MockCloudApiWithSummary()
        api._last_action = "ListThings"
        api._last_result = {"Things": [{"Id": "1"}]}
        result = api.render_tool_result_message('{\n  "Things": [{"Id": "1"}]\n}')
        assert result == "ListThings -> 1 fields"
