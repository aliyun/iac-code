"""Tests for AliyunApi tool."""

from __future__ import annotations

import asyncio
import json
from unittest.mock import MagicMock, patch

import pytest

from iac_code.services.providers.aliyun import AliyunCredential
from iac_code.services.providers.aliyun_oauth import AliyunOAuthError, AliyunOAuthReloginRequired
from iac_code.tools.base import ToolContext
from iac_code.tools.cloud.aliyun import aliyun_api as aliyun_api_module
from iac_code.tools.cloud.aliyun.aliyun_api import AliyunApi
from iac_code.types.stream_events import ResourceObservedEvent


@pytest.fixture
def mock_credentials():
    with patch("iac_code.tools.cloud.aliyun.aliyun_api.CloudCredentials") as mock:
        cred = MagicMock()
        cred.access_key_id = "test-ak"
        cred.access_key_secret = "test-secret"
        cred.region_id = "cn-hangzhou"
        cred.mode = "AK"
        instance = mock.return_value
        instance.get_provider.return_value = cred
        yield instance


@pytest.fixture
def api() -> AliyunApi:
    return AliyunApi()


@pytest.fixture
def context() -> ToolContext:
    return ToolContext()


class TestAliyunApiProperties:
    def test_name(self, api: AliyunApi) -> None:
        assert api.name == "aliyun_api"

    def test_provider_name(self, api: AliyunApi) -> None:
        assert api.provider_name == "aliyun"

    def test_input_schema_has_product(self, api: AliyunApi) -> None:
        schema = api.input_schema
        assert "product" in schema["properties"]
        assert schema["properties"]["product"]["type"] == "string"

    def test_input_schema_has_action_without_enum(self, api: AliyunApi) -> None:
        schema = api.input_schema
        assert "action" in schema["properties"]
        assert schema["properties"]["action"]["type"] == "string"
        assert "enum" not in schema["properties"]["action"]

    def test_input_schema_has_version(self, api: AliyunApi) -> None:
        schema = api.input_schema
        assert "version" in schema["properties"]
        assert schema["properties"]["version"]["type"] == "string"

    def test_input_schema_has_params(self, api: AliyunApi) -> None:
        schema = api.input_schema
        assert "params" in schema["properties"]
        assert schema["properties"]["params"]["type"] == "object"

    def test_input_schema_has_region_id(self, api: AliyunApi) -> None:
        schema = api.input_schema
        assert "region_id" in schema["properties"]
        assert schema["properties"]["region_id"]["type"] == "string"

    def test_input_schema_requires_product_and_action(self, api: AliyunApi) -> None:
        schema = api.input_schema
        assert "product" in schema["required"]
        assert "action" in schema["required"]

    def test_is_read_only_for_describe_actions(self, api: AliyunApi) -> None:
        assert api.is_read_only({"action": "DescribeInstances"}) is True
        assert api.is_read_only({"action": "DescribeRegions"}) is True

    def test_is_read_only_for_list_actions(self, api: AliyunApi) -> None:
        assert api.is_read_only({"action": "ListStacks"}) is True

    def test_is_read_only_for_get_actions(self, api: AliyunApi) -> None:
        assert api.is_read_only({"action": "GetStack"}) is True

    def test_is_read_only_for_validate_actions(self, api: AliyunApi) -> None:
        # ROS ValidateTemplate only validates template syntax server-side; no mutation.
        assert api.is_read_only({"action": "ValidateTemplate"}) is True

    def test_is_read_only_for_ros_preview_stack(self, api: AliyunApi) -> None:
        assert api.is_read_only({"product": "ros", "action": "PreviewStack"}) is True

    def test_is_concurrency_safe_for_ros_preview_stack(self, api: AliyunApi) -> None:
        assert api.is_concurrency_safe({"product": "ros", "action": "PreviewStack"}) is True

    def test_preview_stack_is_not_generically_read_only_for_other_products(self, api: AliyunApi) -> None:
        assert api.is_read_only({"product": "ecs", "action": "PreviewStack"}) is False

    def test_is_read_only_false_for_create(self, api: AliyunApi) -> None:
        assert api.is_read_only({"action": "CreateInstance"}) is False

    def test_is_read_only_false_for_delete(self, api: AliyunApi) -> None:
        assert api.is_read_only({"action": "DeleteInstance"}) is False


class TestAliyunApiVersionResolution:
    def test_known_product_resolves_version(self, api: AliyunApi) -> None:
        version = api._resolve_version({"product": "ecs"})
        assert version == "2014-05-26"

    def test_known_product_ros(self, api: AliyunApi) -> None:
        version = api._resolve_version({"product": "ros"})
        assert version == "2019-09-10"

    def test_explicit_version_overrides_map(self, api: AliyunApi) -> None:
        version = api._resolve_version({"product": "ecs", "version": "2020-01-01"})
        assert version == "2020-01-01"

    def test_unknown_product_without_version_raises(self, api: AliyunApi) -> None:
        with pytest.raises(ValueError, match="unknown-product"):
            api._resolve_version({"product": "unknown-product"})

    def test_case_insensitive_ros(self, api: AliyunApi) -> None:
        assert api._resolve_version({"product": "ROS"}) == "2019-09-10"
        assert api._resolve_version({"product": "Ros"}) == "2019-09-10"

    def test_case_insensitive_ecs(self, api: AliyunApi) -> None:
        assert api._resolve_version({"product": "ECS"}) == "2014-05-26"

    def test_case_insensitive_preserves_mixed_case(self, api: AliyunApi) -> None:
        assert api._resolve_version({"product": "IaCService"}) == "2021-08-06"
        assert api._resolve_version({"product": "iacservice"}) == "2021-08-06"
        assert api._resolve_version({"product": "IACSERVICE"}) == "2021-08-06"


class TestAliyunApiEndpoint:
    def test_central_only(self, api: AliyunApi) -> None:
        assert api._get_endpoint("ros") == "ros.aliyuncs.com"
        assert api._get_endpoint("ros", "cn-hangzhou") == "ros.aliyuncs.com"
        assert api._get_endpoint("IaCService") == "iac.aliyuncs.com"

    def test_central_region(self, api: AliyunApi) -> None:
        assert api._get_endpoint("ecs", "cn-hangzhou-finance") == "ecs.aliyuncs.com"
        assert api._get_endpoint("rds", "cn-hangzhou") == "rds.aliyuncs.com"
        assert api._get_endpoint("slb", "cn-hangzhou") == "slb.aliyuncs.com"

    def test_regional_mapping(self, api: AliyunApi) -> None:
        assert api._get_endpoint("alb", "cn-hangzhou-finance") == "alb.cn-hangzhou.aliyuncs.com"

    def test_regional(self, api: AliyunApi) -> None:
        assert api._get_endpoint("ecs", "cn-beijing") == "ecs.cn-beijing.aliyuncs.com"
        assert api._get_endpoint("rds", "ap-southeast-1") == "rds.ap-southeast-1.aliyuncs.com"
        assert api._get_endpoint("r-kvstore", "cn-beijing") == "r-kvstore.cn-beijing.aliyuncs.com"
        assert api._get_endpoint("slb", "cn-beijing") == "slb.cn-beijing.aliyuncs.com"
        assert api._get_endpoint("vpc", "cn-hangzhou") == "vpc.cn-hangzhou.aliyuncs.com"
        assert api._get_endpoint("alb", "cn-beijing") == "alb.cn-beijing.aliyuncs.com"
        assert api._get_endpoint("nlb", "us-east-1") == "nlb.us-east-1.aliyuncs.com"

    def test_oss_special_pattern(self, api: AliyunApi) -> None:
        assert api._get_endpoint("oss", "cn-hangzhou") == "oss-cn-hangzhou.aliyuncs.com"
        assert api._get_endpoint("oss", "rg-china-mainland") == "oss-rg-china-mainland.aliyuncs.com"

    def test_no_region_returns_none_for_regional_products(self, api: AliyunApi) -> None:
        assert api._get_endpoint("ecs") is None
        assert api._get_endpoint("vpc") is None
        assert api._get_endpoint("oss") is None

    def test_unknown_region_returns_none(self, api: AliyunApi) -> None:
        assert api._get_endpoint("ecs", "unknown-region") is None

    def test_unknown_product_returns_none(self, api: AliyunApi) -> None:
        assert api._get_endpoint("unknown", "cn-hangzhou") is None
        assert api._get_endpoint("unknown") is None

    def test_case_insensitive_endpoint(self, api: AliyunApi) -> None:
        assert api._get_endpoint("ROS") == "ros.aliyuncs.com"
        assert api._get_endpoint("Ros", "cn-hangzhou") == "ros.aliyuncs.com"
        assert api._get_endpoint("ECS", "cn-beijing") == "ecs.cn-beijing.aliyuncs.com"

    def test_fallback(self, api: AliyunApi) -> None:
        assert api._get_endpoint_fallback("unknown", "cn-hangzhou") == "unknown.cn-hangzhou.aliyuncs.com"
        assert api._get_endpoint_fallback("unknown") == "unknown.aliyuncs.com"


class TestAliyunApiDiscoverEndpoint:
    @pytest.fixture(autouse=True)
    def clear_cache(self) -> None:
        aliyun_api_module._endpoint_cache.clear()

    def test_discover_success(self, api: AliyunApi) -> None:
        credential = AliyunCredential(
            mode="AK",
            access_key_id="ak",
            access_key_secret="sk",
            region_id="cn-beijing",
        )
        mock_client = MagicMock()
        mock_client.call_api.return_value = {
            "body": {
                "Endpoints": {
                    "Endpoint": [
                        {"Type": "openAPI", "Endpoint": "newprod.cn-beijing.aliyuncs.com"},
                        {"Type": "innerAPI", "Endpoint": "newprod-inner.aliyuncs.com"},
                    ]
                }
            }
        }
        with patch("iac_code.tools.cloud.aliyun.aliyun_api.OpenApiClient", return_value=mock_client):
            result = api._discover_endpoint("newprod", "cn-beijing", credential)
        assert result == "newprod.cn-beijing.aliyuncs.com"
        # Verify cached
        assert aliyun_api_module._endpoint_cache[("newprod", "cn-beijing")] == "newprod.cn-beijing.aliyuncs.com"

    def test_discover_api_error(self, api: AliyunApi) -> None:
        credential = AliyunCredential(
            mode="AK",
            access_key_id="ak",
            access_key_secret="sk",
            region_id="cn-beijing",
        )
        mock_client = MagicMock()
        mock_client.call_api.side_effect = Exception("InvalidRegionId")
        with patch("iac_code.tools.cloud.aliyun.aliyun_api.OpenApiClient", return_value=mock_client):
            result = api._discover_endpoint("badprod", "bad-region", credential)
        assert result is None
        # Negative result also cached
        assert aliyun_api_module._endpoint_cache[("badprod", "bad-region")] is None

    def test_discover_no_region(self, api: AliyunApi) -> None:
        credential = AliyunCredential(
            mode="AK",
            access_key_id="ak",
            access_key_secret="sk",
            region_id="",
        )
        assert api._discover_endpoint("ecs", "", credential) is None

    def test_discover_uses_cache(self, api: AliyunApi) -> None:
        aliyun_api_module._endpoint_cache[("cached", "cn-hangzhou")] = "cached.cn-hangzhou.aliyuncs.com"
        credential = AliyunCredential(
            mode="AK",
            access_key_id="ak",
            access_key_secret="sk",
            region_id="cn-hangzhou",
        )
        # Should return cached value without calling API
        result = api._discover_endpoint("cached", "cn-hangzhou", credential)
        assert result == "cached.cn-hangzhou.aliyuncs.com"


class TestAliyunApiDisplayMethods:
    def test_user_facing_name(self, api: AliyunApi) -> None:
        result = api.user_facing_name()
        assert "Aliyun API" in result

    def test_render_tool_use_message(self, api: AliyunApi) -> None:
        result = api.render_tool_use_message(
            {"action": "DescribeInstances", "product": "ecs", "region_id": "cn-shanghai"}
        )
        assert result is not None
        assert "DescribeInstances" in result
        assert "ecs" in result

    def test_get_activity_description(self, api: AliyunApi) -> None:
        desc = api.get_activity_description(
            {"action": "DescribeInstances", "product": "ecs", "region_id": "cn-shanghai"}
        )
        assert desc is not None
        assert "DescribeInstances" in desc

    def test_get_action_display_detail_with_product_and_region(self, api: AliyunApi) -> None:
        detail = api._get_action_display_detail(
            {"product": "ecs", "action": "DescribeInstances", "region_id": "cn-hangzhou"}
        )
        assert "ecs" in detail
        assert "cn-hangzhou" in detail

    def test_get_action_display_detail_product_only(self, api: AliyunApi) -> None:
        with patch.object(api, "_get_default_region", return_value=""):
            detail = api._get_action_display_detail({"product": "ecs", "action": "DescribeInstances"})
        assert detail == "ecs"

    def test_summarize_success_result_includes_request_id(self, api: AliyunApi) -> None:
        result = api._summarize_success_result("DescribeInstances", {"RequestId": "ABC-123-XYZ", "Instances": []})
        assert "ABC-123-XYZ" in result

    def test_summarize_success_result_without_request_id(self, api: AliyunApi) -> None:
        result = api._summarize_success_result("DescribeInstances", {"Instances": []})
        assert "RequestId" not in result

    def test_render_tool_result_message_uses_request_id(self, api: AliyunApi) -> None:
        api._last_action = "DescribeInstances"
        api._last_result = {"RequestId": "REQ-42", "Instances": []}
        message = api.render_tool_result_message('{"RequestId": "REQ-42", "Instances": []}')
        assert message is not None
        assert "REQ-42" in message


class TestAliyunApiSerializeParams:
    def test_string_unchanged(self) -> None:
        result = AliyunApi._serialize_params({"key": "value"})
        assert result == {"key": "value"}

    def test_int_converted(self) -> None:
        result = AliyunApi._serialize_params({"PageSize": 10})
        assert result == {"PageSize": "10"}

    def test_bool_lowercase(self) -> None:
        result = AliyunApi._serialize_params({"DryRun": True, "Force": False})
        assert result == {"DryRun": "true", "Force": "false"}

    def test_dict_json_dumped(self) -> None:
        result = AliyunApi._serialize_params({"Tags": {"env": "prod"}})
        assert result == {"Tags": json.dumps({"env": "prod"}, ensure_ascii=False)}

    def test_mixed_params(self) -> None:
        result = AliyunApi._serialize_params({"Name": "test", "Count": 5, "DryRun": True, "Meta": {"k": "v"}})
        assert result["Name"] == "test"
        assert result["Count"] == "5"
        assert result["DryRun"] == "true"
        assert result["Meta"] == json.dumps({"k": "v"}, ensure_ascii=False)


class TestAliyunApiExecute:
    @pytest.mark.asyncio
    async def test_unknown_product_without_version_returns_error(self, api: AliyunApi, context: ToolContext) -> None:
        result = await api.execute(
            tool_input={"product": "unknown-svc", "action": "DoSomething"},
            context=context,
        )
        assert result.is_error is True
        assert "unknown-svc" in result.content

    @pytest.mark.asyncio
    async def test_no_credentials_returns_error(self, api: AliyunApi, context: ToolContext) -> None:
        with patch("iac_code.tools.cloud.aliyun.aliyun_api.CloudCredentials") as mock_creds:
            mock_creds.return_value.get_provider.return_value = None
            result = await api.execute(
                tool_input={"product": "ecs", "action": "DescribeInstances"},
                context=context,
            )
        assert result.is_error is True

    @pytest.mark.asyncio
    async def test_successful_call(self, api: AliyunApi, context: ToolContext, mock_credentials) -> None:
        mock_client = MagicMock()
        mock_client.call_api.return_value = {"body": {"Instances": []}}

        with patch("iac_code.tools.cloud.aliyun.aliyun_api.OpenApiClient", return_value=mock_client):
            result = await api.execute(
                tool_input={
                    "product": "ecs",
                    "action": "DescribeInstances",
                    "region_id": "cn-hangzhou",
                },
                context=context,
            )

        assert result.is_error is False
        data = json.loads(result.content)
        assert data == {"Instances": []}
        mock_client.call_api.assert_called_once()

    @pytest.mark.asyncio
    async def test_ros_create_stack_emits_resource_observed_event(self, api: AliyunApi, mock_credentials) -> None:
        queue: asyncio.Queue = asyncio.Queue()
        context = ToolContext(event_queue=queue, tool_use_id="toolu-create")
        mock_client = MagicMock()
        mock_client.call_api.return_value = {
            "body": {
                "RequestId": "req-1",
                "StackId": "stack-id-123",
            }
        }

        with patch("iac_code.tools.cloud.aliyun.aliyun_api.OpenApiClient", return_value=mock_client):
            result = await api.execute(
                tool_input={
                    "product": "ros",
                    "action": "CreateStack",
                    "params": {
                        "StackName": "iac-e2e-stack",
                        "TemplateBody": "ROSTemplateFormatVersion: '2015-09-01'\nResources: {}\n",
                    },
                    "region_id": "cn-hangzhou",
                },
                context=context,
            )

        assert result.is_error is False
        events = []
        while not queue.empty():
            events.append(await queue.get())

        assert len(events) == 1
        event = events[0]
        assert isinstance(event, ResourceObservedEvent)
        assert event.provider == "ros"
        assert event.resource_type == "stack"
        assert event.resource_id == "stack-id-123"
        assert event.resource_name == "iac-e2e-stack"
        assert event.region_id == "cn-hangzhou"
        assert event.action == "CreateStack"
        assert event.tool_name == "aliyun_api"
        assert event.tool_use_id == "toolu-create"
        assert event.metadata == {}

    @pytest.mark.asyncio
    async def test_explicit_version(self, api: AliyunApi, context: ToolContext, mock_credentials) -> None:
        mock_client = MagicMock()
        mock_client.call_api.return_value = {"body": {"Result": "ok"}}

        with patch("iac_code.tools.cloud.aliyun.aliyun_api.OpenApiClient", return_value=mock_client):
            result = await api.execute(
                tool_input={
                    "product": "custom-svc",
                    "action": "CustomAction",
                    "version": "2023-01-01",
                    "region_id": "cn-beijing",
                },
                context=context,
            )

        assert result.is_error is False
        data = json.loads(result.content)
        assert data == {"Result": "ok"}

    @pytest.mark.asyncio
    async def test_api_error_cleans_response_body(self, api: AliyunApi, context: ToolContext, mock_credentials) -> None:
        mock_client = MagicMock()
        mock_client.call_api.side_effect = Exception('InvalidAction.NotFound Response: {"RequestId": "xxx"}')

        with patch("iac_code.tools.cloud.aliyun.aliyun_api.OpenApiClient", return_value=mock_client):
            result = await api.execute(
                tool_input={
                    "product": "ecs",
                    "action": "BadAction",
                    "region_id": "cn-hangzhou",
                },
                context=context,
            )

        assert result.is_error is True
        assert "InvalidAction.NotFound" in result.content
        assert "Response:" not in result.content

    @pytest.mark.asyncio
    async def test_params_serialized_in_request(self, api: AliyunApi, context: ToolContext, mock_credentials) -> None:
        mock_client = MagicMock()
        mock_client.call_api.return_value = {"body": {"Instances": []}}

        with patch("iac_code.tools.cloud.aliyun.aliyun_api.OpenApiClient", return_value=mock_client):
            result = await api.execute(
                tool_input={
                    "product": "ecs",
                    "action": "DescribeInstances",
                    "params": {"PageSize": 10, "DryRun": True},
                    "region_id": "cn-hangzhou",
                },
                context=context,
            )

        assert result.is_error is False
        # Verify call_api was called and params were serialized
        call_args = mock_client.call_api.call_args
        request = call_args[0][1]  # second positional arg is the OpenApiRequest
        assert request.query["PageSize"] == "10"
        assert request.query["DryRun"] == "true"

    @pytest.mark.asyncio
    async def test_execute_refreshes_oauth_before_endpoint_discovery(
        self, api: AliyunApi, context: ToolContext
    ) -> None:
        oauth_cred = AliyunCredential(
            mode="OAuth",
            access_key_id="tmp-ak",
            access_key_secret="tmp-sk",
            sts_token="tmp-sts",
            region_id="cn-hangzhou",
            oauth_access_token="access-token",
            oauth_refresh_token="refresh-token",
        )
        refreshed = AliyunCredential(
            mode="OAuth",
            access_key_id="new-ak",
            access_key_secret="new-sk",
            sts_token="new-sts",
            region_id="cn-hangzhou",
            oauth_access_token="access-token",
            oauth_refresh_token="refresh-token",
        )
        mock_client = MagicMock()
        mock_client.call_api.side_effect = [
            {"body": {"Endpoints": {"Endpoint": [{"Type": "openAPI", "Endpoint": "custom.aliyuncs.com"}]}}},
            {"body": {"Instances": []}},
        ]

        with (
            patch("iac_code.tools.cloud.aliyun.aliyun_api.CloudCredentials") as cloud_credentials,
            patch.object(
                aliyun_api_module.AliyunCredentials, "refresh_oauth_if_needed", return_value=refreshed
            ) as refresh,
            patch("iac_code.tools.cloud.aliyun.aliyun_api.OpenApiClient", return_value=mock_client) as client_cls,
        ):
            cloud_credentials.return_value.get_provider.return_value = oauth_cred
            result = await api.execute(
                tool_input={
                    "product": "custom-svc",
                    "action": "DescribeInstances",
                    "version": "2023-01-01",
                    "region_id": "cn-hangzhou",
                },
                context=context,
            )

        assert result.is_error is False
        refresh.assert_called_once_with(oauth_cred)
        discovery_config = client_cls.call_args_list[0].args[0]
        call_config = client_cls.call_args_list[1].args[0]
        assert discovery_config.access_key_id == "new-ak"
        assert discovery_config.security_token == "new-sts"
        assert call_config.access_key_id == "new-ak"
        assert call_config.security_token == "new-sts"

    @pytest.mark.asyncio
    async def test_execute_returns_relogin_error_when_oauth_refresh_requires_login(
        self, api: AliyunApi, context: ToolContext
    ) -> None:
        oauth_cred = AliyunCredential(
            mode="OAuth",
            access_key_id="tmp-ak",
            access_key_secret="tmp-sk",
            sts_token="tmp-sts",
            region_id="cn-hangzhou",
            oauth_access_token="access-token",
            oauth_refresh_token="refresh-token",
        )

        with (
            patch("iac_code.tools.cloud.aliyun.aliyun_api.CloudCredentials") as cloud_credentials,
            patch.object(
                aliyun_api_module.AliyunCredentials,
                "refresh_oauth_if_needed",
                side_effect=AliyunOAuthReloginRequired("Run /auth and choose OAuth Login (Browser)."),
            ),
        ):
            cloud_credentials.return_value.get_provider.return_value = oauth_cred
            result = await api.execute(
                tool_input={"product": "ecs", "action": "DescribeInstances", "region_id": "cn-hangzhou"},
                context=context,
            )

        assert result.is_error is True
        assert "/auth" in result.content
        assert "OAuth Login (Browser)" in result.content

    @pytest.mark.asyncio
    async def test_execute_returns_oauth_error_when_refresh_fails(self, api: AliyunApi, context: ToolContext) -> None:
        oauth_cred = AliyunCredential(
            mode="OAuth",
            access_key_id="tmp-ak",
            access_key_secret="tmp-sk",
            sts_token="tmp-sts",
            region_id="cn-hangzhou",
            oauth_access_token="access-token",
            oauth_refresh_token="refresh-token",
        )

        with (
            patch("iac_code.tools.cloud.aliyun.aliyun_api.CloudCredentials") as cloud_credentials,
            patch.object(
                aliyun_api_module.AliyunCredentials,
                "refresh_oauth_if_needed",
                side_effect=AliyunOAuthError("temporary oauth refresh failure"),
            ),
        ):
            cloud_credentials.return_value.get_provider.return_value = oauth_cred
            result = await api.execute(
                tool_input={"product": "ecs", "action": "DescribeInstances", "region_id": "cn-hangzhou"},
                context=context,
            )

        assert result.is_error is True
        assert "temporary oauth refresh failure" in result.content


class TestAliyunApiProductNormalization:
    @pytest.mark.asyncio
    async def test_uppercase_product_works(self, api: AliyunApi, context: ToolContext, mock_credentials) -> None:
        mock_client = MagicMock()
        mock_client.call_api.return_value = {"body": {"Instances": []}}

        with patch("iac_code.tools.cloud.aliyun.aliyun_api.OpenApiClient", return_value=mock_client):
            result = await api.execute(
                tool_input={"product": "ROS", "action": "ListStacks", "region_id": "cn-hangzhou"},
                context=context,
            )
        assert result.is_error is False


class TestAliyunApiHooks:
    @pytest.mark.asyncio
    async def test_ros_template_body_is_rejected_before_cloud_call(
        self, api: AliyunApi, context: ToolContext, mock_credentials
    ) -> None:
        with patch("iac_code.tools.cloud.aliyun.aliyun_api.OpenApiClient") as mock_open_api_client:
            result = await api.execute(
                tool_input={
                    "product": "ros",
                    "action": "ValidateTemplate",
                    "params": {"TemplateBody": "{}"},
                    "region_id": "cn-hangzhou",
                },
                context=ToolContext(pipeline_mode=True),
            )

        assert result.is_error is True
        assert "TemplateBody" in result.content
        assert "TemplateURL" in result.content
        mock_open_api_client.assert_not_called()

    @pytest.mark.asyncio
    async def test_ros_template_body_is_allowed_outside_pipeline(
        self, api: AliyunApi, context: ToolContext, mock_credentials
    ) -> None:
        template = json.dumps(
            {
                "ROSTemplateFormatVersion": "2015-09-01",
                "Resources": {
                    "Vpc": {"Type": "ALIYUN::ECS::VPC", "Properties": {}},
                },
            }
        )
        mock_client = MagicMock()
        mock_client.call_api.return_value = {"body": {"Description": "Valid"}}

        with patch("iac_code.tools.cloud.aliyun.aliyun_api.OpenApiClient", return_value=mock_client):
            result = await api.execute(
                tool_input={
                    "product": "ros",
                    "action": "ValidateTemplate",
                    "params": {"TemplateBody": template},
                    "region_id": "cn-hangzhou",
                },
                context=context,
            )

        assert result.is_error is False
        mock_client.call_api.assert_called_once()

    @pytest.mark.asyncio
    async def test_hook_blocks_validate_with_wrong_resource_types(
        self, api: AliyunApi, context: ToolContext, mock_credentials, tmp_path
    ) -> None:
        template = json.dumps(
            {
                "ROSTemplateFormatVersion": "2015-09-01",
                "Resources": {
                    "Vpc": {"Type": "ALIYUN::VPC::VPC", "Properties": {}},
                    "VSwitch": {"Type": "ALIYUN::VPC::VSwitch", "Properties": {}},
                },
            }
        )
        template_file = tmp_path / "wrong-resource-types.json"
        template_file.write_text(template, encoding="utf-8")
        result = await api.execute(
            tool_input={
                "product": "ros",
                "action": "ValidateTemplate",
                "params": {"TemplateURL": str(template_file)},
                "region_id": "cn-hangzhou",
            },
            context=context,
        )
        assert result.is_error is True
        assert "ALIYUN::ECS::VPC" in result.content
        assert "ALIYUN::ECS::VSwitch" in result.content

    @pytest.mark.asyncio
    async def test_hook_passes_correct_resource_types(
        self, api: AliyunApi, context: ToolContext, mock_credentials, tmp_path
    ) -> None:
        template = json.dumps(
            {
                "ROSTemplateFormatVersion": "2015-09-01",
                "Resources": {
                    "Vpc": {"Type": "ALIYUN::ECS::VPC", "Properties": {}},
                },
            }
        )
        template_file = tmp_path / "correct-resource-types.json"
        template_file.write_text(template, encoding="utf-8")
        mock_client = MagicMock()
        mock_client.call_api.return_value = {"body": {"Description": "Valid"}}

        with patch("iac_code.tools.cloud.aliyun.aliyun_api.OpenApiClient", return_value=mock_client):
            result = await api.execute(
                tool_input={
                    "product": "ros",
                    "action": "ValidateTemplate",
                    "params": {"TemplateURL": str(template_file)},
                    "region_id": "cn-hangzhou",
                },
                context=context,
            )
        assert result.is_error is False


class TestAliyunApiBuildConfig:
    def test_ak_mode(self) -> None:
        credential = AliyunCredential(
            mode="AK",
            access_key_id="ak-id",
            access_key_secret="ak-secret",
            region_id="cn-hangzhou",
        )
        config = AliyunApi._build_config(credential, "ecs.aliyuncs.com", "cn-hangzhou")
        assert config.access_key_id == "ak-id"
        assert config.access_key_secret == "ak-secret"
        assert config.endpoint == "ecs.aliyuncs.com"
        assert config.region_id == "cn-hangzhou"
        assert config.security_token is None
        assert config.credential is None
        assert config.user_agent and config.user_agent.startswith("iac-code/")

    def test_sts_token_mode(self) -> None:
        credential = AliyunCredential(
            mode="StsToken",
            access_key_id="ak-id",
            access_key_secret="ak-secret",
            region_id="cn-beijing",
            sts_token="my-sts-token",
        )
        config = AliyunApi._build_config(credential, "ecs.aliyuncs.com", "cn-beijing")
        assert config.access_key_id == "ak-id"
        assert config.access_key_secret == "ak-secret"
        assert config.security_token == "my-sts-token"
        assert config.endpoint == "ecs.aliyuncs.com"
        assert config.region_id == "cn-beijing"
        assert config.user_agent and config.user_agent.startswith("iac-code/")

    def test_oauth_mode_builds_sts_config(self) -> None:
        credential = AliyunCredential(
            mode="OAuth",
            access_key_id="tmp-ak",
            access_key_secret="tmp-sk",
            sts_token="tmp-sts",
            region_id="cn-hangzhou",
        )
        config = AliyunApi._build_config(credential, "ecs.aliyuncs.com", "cn-hangzhou")
        assert config.access_key_id == "tmp-ak"
        assert config.access_key_secret == "tmp-sk"
        assert config.security_token == "tmp-sts"
        assert config.endpoint == "ecs.aliyuncs.com"
        assert config.region_id == "cn-hangzhou"
        assert config.user_agent and config.user_agent.startswith("iac-code/")

    def test_ram_role_arn_mode(self) -> None:
        credential = AliyunCredential(
            mode="RamRoleArn",
            access_key_id="ak-id",
            access_key_secret="ak-secret",
            region_id="cn-shanghai",
            ram_role_arn="acs:ram::123456:role/test-role",
            ram_session_name="test-session",
        )
        config = AliyunApi._build_config(credential, "ecs.aliyuncs.com", "cn-shanghai")
        assert config.credential is not None
        assert config.endpoint == "ecs.aliyuncs.com"
        assert config.region_id == "cn-shanghai"
        # AK fields should not be set when using credential client
        assert config.access_key_id is None
        assert config.access_key_secret is None
        assert config.user_agent and config.user_agent.startswith("iac-code/")
