"""Tests for AliyunApi tool."""

from __future__ import annotations

import asyncio
import json
import re
import time
from contextlib import nullcontext
from pathlib import Path
from types import MappingProxyType, SimpleNamespace
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from iac_code.services.providers.aliyun import AliyunCredential
from iac_code.services.providers.aliyun_oauth import AliyunOAuthError, AliyunOAuthReloginRequired
from iac_code.services.telemetry import set_client
from iac_code.services.telemetry.client import TelemetryClient
from iac_code.services.telemetry.events import EventEmitter
from iac_code.services.telemetry.metrics import MetricsRegistry
from iac_code.services.telemetry.names import AliyunApiAttr, Events, GenAiAttr, Metrics, Spans
from iac_code.services.telemetry.sink import AnalyticsSink
from iac_code.tools.base import ToolContext, ToolRegistry, ToolResult
from iac_code.tools.cloud.aliyun import aliyun_api as aliyun_api_module
from iac_code.tools.cloud.aliyun.acs3_transport import (
    NormalizedApiResponse,
    TransportRouter,
)
from iac_code.tools.cloud.aliyun.aliyun_api import AliyunApi
from iac_code.tools.cloud.aliyun.aliyun_api_doc import AliyunApiDoc
from iac_code.tools.cloud.aliyun.api_contract import ApiContractError, ApiContractResolver, RequestBuilder
from iac_code.tools.cloud.aliyun.contract_store import ResolvedContractStore, canonical_input_sha256
from iac_code.tools.cloud.aliyun.endpoint_resolver import EndpointResolution, HostBindingResolver
from iac_code.tools.cloud.aliyun.openmeta import MetadataFetch, ProductMetadata, normalize_api_metadata
from iac_code.tools.cloud.aliyun.oss_v4_adapter import OssOperationCatalog
from iac_code.tools.cloud.aliyun.product_resolver import ProductResolver
from iac_code.tools.cloud.aliyun.public_errors import public_aliyun_error
from iac_code.tools.cloud.aliyun.result_contract import ALIYUN_BODY_CONTRACT_VERSION, ALIYUN_HTTP_METADATA_KEY
from iac_code.tools.cloud.aliyun.retry_policy import RetryBudget, RetryExhausted, RetryReason, TransportFailure
from iac_code.tools.tool_executor import ToolCallRequest, ToolExecutor
from iac_code.types.permissions import InvocationBinding, ToolPermissionContext
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
    return AliyunApi.isolated_for_tests()


@pytest.fixture
def context() -> ToolContext:
    return ToolContext()


class TestAliyunApiProperties:
    def test_runtime_instance_does_not_load_legacy_endpoint_table(self, monkeypatch: pytest.MonkeyPatch) -> None:
        def fail() -> dict:
            raise AssertionError("legacy endpoint table loaded")

        monkeypatch.setattr(aliyun_api_module, "_load_legacy_endpoints", fail)

        instance = AliyunApi(services=object())

        assert instance._legacy_endpoints == {}

    def test_name(self, api: AliyunApi) -> None:
        assert api.name == "aliyun_api"

    def test_provider_name(self, api: AliyunApi) -> None:
        assert api.provider_name == "aliyun"

    def test_target_error_detail_redacts_space_separated_request_id(self) -> None:
        detail = aliyun_api_module._redact_target_error_detail(
            "Specified parameter Version is not valid. request id: 01234567-89AB-CDEF-0123-456789ABCDEF"
        )

        assert "01234567" not in detail
        assert "<redacted>" in detail

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
        pattern = re.compile(schema["properties"]["version"]["pattern"])
        assert pattern.fullmatch("2014-05-26")
        assert pattern.fullmatch("20240611")
        assert pattern.fullmatch("iap_1.0")
        assert pattern.fullmatch("2014/05/26") is None

    def test_input_schema_has_params(self, api: AliyunApi) -> None:
        schema = api.input_schema
        assert "params" in schema["properties"]
        assert schema["properties"]["params"]["type"] == "object"

    def test_input_schema_has_region_id(self, api: AliyunApi) -> None:
        schema = api.input_schema
        assert "region_id" in schema["properties"]
        assert schema["properties"]["region_id"]["type"] == "string"

    def test_input_schema_has_optional_automatically_resolved_endpoint(self, api: AliyunApi) -> None:
        schema = api.input_schema
        endpoint = schema["properties"]["endpoint"]
        assert endpoint["type"] == "string"
        assert "默认会自动获取，通常不需要传" in endpoint["description"]
        assert "endpoint" not in schema["required"]

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

    @pytest.mark.parametrize("method", ["DELETE", "PUT", "POST"])
    def test_roa_write_methods_are_not_read_only_even_with_read_action(self, api: AliyunApi, method: str) -> None:
        assert (
            api.is_read_only(
                {
                    "product": "cs",
                    "action": "DescribeClusters",
                    "style": "ROA",
                    "method": method,
                    "pathname": "/clusters/c-123",
                }
            )
            is False
        )

    def test_roa_get_without_body_can_be_read_only(self, api: AliyunApi) -> None:
        assert (
            api.is_read_only(
                {
                    "product": "cs",
                    "action": "DescribeClusters",
                    "style": "ROA",
                    "method": "GET",
                    "pathname": "/clusters",
                }
            )
            is True
        )

    def test_roa_get_with_body_is_not_read_only(self, api: AliyunApi) -> None:
        assert (
            api.is_read_only(
                {
                    "product": "cs",
                    "action": "DescribeClusters",
                    "style": "ROA",
                    "method": "GET",
                    "pathname": "/clusters",
                    "body": {"force": True},
                }
            )
            is False
        )


class TestAliyunApiPipelineRosTemplateActions:
    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        ("action", "expected_tool"),
        [
            ("ValidateTemplate", "ros_validate_template"),
            ("GetTemplateParameterConstraints", "ros_get_template_parameter_constraints"),
            ("PreviewStack", "ros_preview_template"),
            ("GetTemplateEstimateCost", "ros_estimate_template_cost"),
        ],
    )
    async def test_pipeline_rejects_raw_ros_template_api_actions(
        self,
        api: AliyunApi,
        action: str,
        expected_tool: str,
    ) -> None:
        result = await api.execute(
            tool_input={
                "product": "ros",
                "action": action,
                "params": {"TemplateURL": "templates/app.yml"},
                "region_id": "cn-hangzhou",
            },
            context=ToolContext(pipeline_mode=True),
        )

        assert result.is_error
        assert expected_tool in result.content
        assert "aliyun_api" in result.content

    @pytest.mark.asyncio
    @pytest.mark.parametrize("action", ["CreateStack", "ContinueCreateStack", "DeleteStack", "UpdateStack"])
    async def test_pipeline_rejects_raw_ros_deployment_api_actions(self, api: AliyunApi, action: str) -> None:
        result = await api.execute(
            tool_input={
                "product": "ros",
                "action": action,
                "params": {"TemplateURL": "templates/app.yml", "StackId": "stack-123"},
                "region_id": "cn-hangzhou",
            },
            context=ToolContext(pipeline_mode=True),
        )

        assert result.is_error
        assert "ros_deploy" in result.content
        assert "raw ROS deployment API" in result.content


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

    @pytest.mark.parametrize("fails", [False, True])
    @pytest.mark.asyncio
    async def test_legacy_execution_telemetry_uses_finite_event_without_identifiers(
        self, api: AliyunApi, context: ToolContext, mock_credentials, fails: bool
    ) -> None:
        product = "Ro*Secret"
        action = "Create:Stack SECRET"
        region = "CN-Hangzhou/Secret"
        version = "2023-01-01/Secret"
        mock_client = MagicMock()
        if fails:
            mock_client.call_api.side_effect = Exception(
                "boom for {} {} {} {}".format(product.lower(), action.lower(), region.lower(), version.lower())
            )
        else:
            mock_client.call_api.return_value = {"body": {"Result": "ok"}}

        with (
            patch.object(api, "_discover_endpoint", return_value=None),
            patch("iac_code.tools.cloud.aliyun.aliyun_api.OpenApiClient", return_value=mock_client),
            patch("iac_code.tools.cloud.aliyun.aliyun_api.log_event") as log_event,
            patch("iac_code.tools.cloud.aliyun.aliyun_api.add_metric") as add_metric,
        ):
            result = await api.execute(
                tool_input={
                    "product": product,
                    "action": action,
                    "version": version,
                    "region_id": region,
                },
                context=context,
            )

        assert result.is_error is fails
        event_name, event_payload = log_event.call_args.args
        assert event_name == Events.ALIYUN_API_LEGACY_CALLED
        assert event_payload == {"outcome": "failure" if fails else "success"}
        metric_attrs = add_metric.call_args_list[0].args[2]
        assert metric_attrs["api_service"] == "unsafe"
        telemetry_dump = json.dumps(
            {
                "events": [call.args for call in log_event.call_args_list],
                "metrics": [call.args for call in add_metric.call_args_list],
            },
            default=str,
        )
        for raw_value in (
            product,
            product.upper(),
            product.lower(),
            action,
            action.lower(),
            region,
            region.lower(),
            version,
            version.lower(),
        ):
            assert raw_value not in telemetry_dump

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
                    "action": "CreateChangeSet",
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
    async def test_ros_template_url_is_required_for_pipeline_template_action(
        self, api: AliyunApi, context: ToolContext, mock_credentials
    ) -> None:
        with patch("iac_code.tools.cloud.aliyun.aliyun_api.OpenApiClient") as mock_open_api_client:
            result = await api.execute(
                tool_input={
                    "product": "ros",
                    "action": "CreateChangeSet",
                    "params": {"Parameters": {"ZoneId": "cn-hangzhou-k"}},
                    "region_id": "cn-hangzhou",
                },
                context=ToolContext(pipeline_mode=True),
            )

        assert result.is_error is True
        assert "TemplateURL" in result.content
        assert "CreateChangeSet" in result.content
        mock_open_api_client.assert_not_called()

    @pytest.mark.asyncio
    @pytest.mark.parametrize("template_source", [{"TemplateId": "tpl-123"}, {"TemplateScratchId": "ts-123"}])
    async def test_ros_pipeline_template_action_accepts_only_template_url(
        self, api: AliyunApi, context: ToolContext, mock_credentials, template_source: dict[str, str]
    ) -> None:
        with patch("iac_code.tools.cloud.aliyun.aliyun_api.OpenApiClient") as mock_open_api_client:
            result = await api.execute(
                tool_input={
                    "product": "ros",
                    "action": "CreateChangeSet",
                    "params": template_source,
                    "region_id": "cn-hangzhou",
                },
                context=ToolContext(pipeline_mode=True),
            )

        assert result.is_error is True
        assert "TemplateURL" in result.content
        mock_open_api_client.assert_not_called()

    @pytest.mark.asyncio
    async def test_ros_pipeline_non_template_action_does_not_require_template_url(
        self, api: AliyunApi, context: ToolContext, mock_credentials
    ) -> None:
        mock_client = MagicMock()
        mock_client.call_api.return_value = {"body": {"Stacks": []}}

        with patch("iac_code.tools.cloud.aliyun.aliyun_api.OpenApiClient", return_value=mock_client):
            result = await api.execute(
                tool_input={
                    "product": "ros",
                    "action": "ListStacks",
                    "params": {},
                    "region_id": "cn-hangzhou",
                },
                context=ToolContext(pipeline_mode=True),
            )

        assert result.is_error is False
        mock_client.call_api.assert_called_once()

    @pytest.mark.asyncio
    async def test_ros_template_action_requires_source_outside_pipeline(
        self, api: AliyunApi, context: ToolContext, mock_credentials
    ) -> None:
        mock_client = MagicMock()
        mock_client.call_api.return_value = {"body": {"RequestId": "req-1"}}

        with patch("iac_code.tools.cloud.aliyun.aliyun_api.OpenApiClient", return_value=mock_client):
            result = await api.execute(
                tool_input={
                    "product": "ros",
                    "action": "GetTemplateEstimateCost",
                    "params": {"Parameters": {"ZoneId": "cn-hangzhou-k"}},
                    "region_id": "cn-hangzhou",
                },
                context=context,
            )

        assert result.is_error is True
        assert "ROS1201" in result.content
        mock_client.call_api.assert_not_called()

    @pytest.mark.asyncio
    async def test_ros_remote_template_url_scheme_is_case_insensitive(
        self, api: AliyunApi, context: ToolContext, mock_credentials
    ) -> None:
        mock_client = MagicMock()
        mock_client.call_api.return_value = {"body": {"Description": "Valid"}}

        with (
            patch("iac_code.tools.cloud.aliyun.aliyun_api.OpenApiClient", return_value=mock_client),
            patch("iac_code.tools.cloud.aliyun.template_source.Path.read_text") as read_text,
        ):
            read_text.side_effect = AssertionError("remote TemplateURL should not be read as a local file")
            result = await api.execute(
                tool_input={
                    "product": "ros",
                    "action": "ValidateTemplate",
                    "params": {"TemplateURL": "HTTPS://example.com/template.yml"},
                    "region_id": "cn-hangzhou",
                },
                context=context,
            )

        assert result.is_error is False
        mock_client.call_api.assert_called_once()
        request = mock_client.call_api.call_args[0][1]
        assert request.query["TemplateURL"] == "HTTPS://example.com/template.yml"
        assert "TemplateBody" not in request.query

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


def _production_raw_api(
    product: str,
    version: str,
    action: str,
    *,
    style: str = "RPC",
    method: str = "POST",
    path: str = "/",
    operation_type: str = "read",
    parameters: list[dict[str, Any]] | None = None,
    consumes: list[str] | None = None,
    produces: list[str] | None = None,
) -> dict[str, Any]:
    return {
        "product": product,
        "version": version,
        "action": action,
        "style": style,
        "methods": [method],
        "path": path,
        "schemes": ["HTTPS"],
        "consumes": consumes or [],
        "produces": produces or ["application/json"],
        "operationType": operation_type,
        "security": [{"AK": []}],
        "parameters": parameters or [],
        "responses": {},
        "components": {"schemas": {}},
    }


class _ProductionOpenMeta:
    def __init__(self, documents: list[dict[str, Any]]) -> None:
        self.documents = {
            (
                str(document["product"]).casefold(),
                str(document["version"]),
                str(document["action"]).casefold(),
            ): document
            for document in documents
        }
        self.products: dict[str, ProductMetadata] = {}
        for document in documents:
            product = str(document["product"])
            version = str(document["version"])
            self.products.setdefault(
                product.casefold(),
                ProductMetadata(product=product, default_version=version, versions=(version,), documentation_url=None),
            )
        catalog_only_products = (
            ("Rds", "2014-08-15"),
            ("Vpc", "2016-04-28"),
            ("Dysmsapi", "2017-05-25"),
            ("Dyvmsapi", "2017-05-25"),
        )
        for product, version in catalog_only_products:
            self.products.setdefault(
                product.casefold(),
                ProductMetadata(product=product, default_version=version, versions=(version,), documentation_url=None),
            )
        self.product_catalog = tuple(self.products.values())
        self.temporarily_unavailable: set[tuple[str, str]] = set()
        self.calls: list[tuple[str, ...]] = []

    async def get_product(self, product: str) -> MetadataFetch[Any]:
        self.calls.append(("product", product))
        value = self.products.get(product.casefold())
        return MetadataFetch(
            value=value,
            source="fresh" if value else None,
            error=None if value else "not_found",
            cache_status="memory_fresh" if value else "miss",
        )

    async def list_products(self) -> MetadataFetch[Any]:
        self.calls.append(("products",))
        return MetadataFetch(
            value=self.product_catalog,
            source="fresh",
            error=None,
            cache_status="memory_fresh",
        )

    async def get_api(self, product: str, version: str, action: str) -> MetadataFetch[Any]:
        self.calls.append(("api", product, version, action))
        if (product.casefold(), action.casefold()) in self.temporarily_unavailable:
            return MetadataFetch(
                value=None,
                source=None,
                error="temporarily_unavailable",
                cache_status="miss",
            )
        raw = self.documents.get((product.casefold(), version, action.casefold()))
        value = normalize_api_metadata(raw) if raw is not None else None
        return MetadataFetch(
            value=value,
            source="fresh" if value else None,
            error=None if value else "not_found",
            cache_status="memory_fresh" if value else "negative_hit",
        )


class _ProductionEndpointResolver:
    def __init__(self) -> None:
        self.calls: list[tuple[Any, ...]] = []
        self.failure: Exception | None = None
        self.omit_host_template = False

    async def resolve(self, contract, region_id, credential, *, host_values, explicit_endpoint=None):
        self.calls.append((contract, region_id, credential, dict(host_values), explicit_endpoint))
        if self.failure is not None:
            raise self.failure
        if explicit_endpoint is not None:
            return EndpointResolution(endpoint=explicit_endpoint, source="explicit", host_template=None)
        if contract.product == "Oss":
            return EndpointResolution(
                endpoint="oss-cn-hangzhou.aliyuncs.com",
                source="catalog_region",
                host_template=None if self.omit_host_template else "{bucket}.{endpoint}",
            )
        if contract.product == "FC":
            endpoint = "fcv3.cn-hangzhou.aliyuncs.com"
        else:
            endpoint = "{}.cn-hangzhou.aliyuncs.com".format(contract.product.casefold())
        return EndpointResolution(endpoint=endpoint, source="catalog_region", host_template=None)


class _ProductionTransport:
    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []
        self.responses: dict[str, NormalizedApiResponse] = {}
        self.errors: dict[str, Exception] = {}

    async def execute(self, **kwargs: Any) -> NormalizedApiResponse:
        self.calls.append(kwargs)
        action = kwargs["contract"].action
        if action in self.errors:
            raise self.errors[action]
        if action == "GetBinary":
            return NormalizedApiResponse(
                status=200,
                headers=MappingProxyType({"content-type": "application/octet-stream"}),
                body={"encoding": "base64", "data": "ZGF0YQ=="},
                content_type="application/octet-stream",
                content_encoding=None,
                size=4,
            )
        return self.responses.get(
            action,
            NormalizedApiResponse(
                status=200,
                headers=MappingProxyType({"x-acs-request-id": "request-1"}),
                body={"RequestId": "request-1", "Action": action},
                content_type="application/json",
                content_encoding=None,
                size=32,
            ),
        )

    async def aclose(self) -> None:
        return None


class _CaptureMetricInstrument:
    def __init__(self) -> None:
        self.calls: list[tuple[int | float, dict[str, Any]]] = []

    def add(self, value: int | float, attributes: dict[str, Any]) -> None:
        self.calls.append((value, attributes))

    def record(self, value: int | float, attributes: dict[str, Any]) -> None:
        self.calls.append((value, attributes))


def _production_telemetry_client() -> tuple[
    TelemetryClient,
    MagicMock,
    _CaptureMetricInstrument,
    _CaptureMetricInstrument,
]:
    emitter = MagicMock(spec=EventEmitter)
    sink = AnalyticsSink(emitter)
    sink.activate()
    count = _CaptureMetricInstrument()
    duration = _CaptureMetricInstrument()
    metrics = MetricsRegistry(
        {
            Metrics.ALIYUN_API_CALLED_COUNT: count,
            Metrics.ALIYUN_API_CALLED_DURATION: duration,
        }
    )
    client = TelemetryClient(
        identity=MagicMock(),
        attributes=MagicMock(),
        metrics=metrics,
        events=emitter,
        tracer=MagicMock(),
        sink=sink,
        fallback=MagicMock(),
    )
    return client, emitter, count, duration


def _production_documents() -> list[dict[str, Any]]:
    string_query = {"name": "RegionId", "in": "query", "required": True, "schema": {"type": "string"}}
    oss_common = [
        {"name": "bucket", "in": "host", "required": True, "schema": {"type": "string"}},
        {
            "name": "key",
            "in": "path",
            "required": True,
            "pathEncoding": "preserve_slashes",
            "schema": {"type": "string"},
        },
    ]
    documents = [
        _production_raw_api(
            "Ecs",
            "2014-05-26",
            "DescribeInstances",
            parameters=[
                string_query,
                {"name": "InstanceIds", "in": "query", "schema": {"type": "string"}},
            ],
        ),
        _production_raw_api(
            "FC",
            "2023-03-30",
            "GetFunction",
            style="ROA",
            method="GET",
            path="/2023-03-30/functions/{functionName}",
            parameters=[
                {"name": "functionName", "in": "path", "required": True, "schema": {"type": "string"}},
                {"name": "qualifier", "in": "query", "schema": {"type": "string"}},
            ],
        ),
        _production_raw_api("Chatbot", "2022-04-08", "ListInstance", method="GET"),
        _production_raw_api(
            "ROS",
            "2019-09-10",
            "ValidateTemplate",
            parameters=[
                {"name": "TemplateBody", "in": "formData", "required": True, "schema": {"type": "string"}},
                string_query,
            ],
            consumes=["application/x-www-form-urlencoded"],
        ),
        _production_raw_api(
            "ROS",
            "2019-09-10",
            "CreateStack",
            operation_type="write",
            parameters=[
                {"name": "StackName", "in": "query", "required": True, "schema": {"type": "string"}},
                string_query,
            ],
        ),
        _production_raw_api(
            "Ecs",
            "2014-05-26",
            "EchoBody",
            operation_type="write",
            parameters=[{"name": "body", "in": "body", "required": True, "schema": {}}],
            consumes=["application/json"],
        ),
        _production_raw_api(
            "Ecs",
            "2014-05-26",
            "PutBytes",
            operation_type="write",
            parameters=[
                {"name": "body", "in": "body", "required": True, "schema": {"type": "string", "format": "binary"}}
            ],
            consumes=["application/octet-stream"],
        ),
        _production_raw_api("Ecs", "2014-05-26", "GetXml", produces=["application/xml"]),
        _production_raw_api("Ecs", "2014-05-26", "GetText", produces=["text/plain"]),
        _production_raw_api("Ecs", "2014-05-26", "GetBinary", produces=["application/octet-stream"]),
        _production_raw_api(
            "Oss",
            "2019-05-17",
            "GetObject",
            style="ROA",
            method="GET",
            path="/{key}",
            parameters=oss_common,
            produces=["application/octet-stream"],
        ),
        _production_raw_api(
            "Oss",
            "2019-05-17",
            "PutObject",
            style="ROA",
            method="PUT",
            path="/{key}",
            operation_type="write",
            parameters=[
                *oss_common,
                {"name": "body", "in": "body", "schema": {"type": "string", "format": "binary"}},
                {"name": "x-oss-meta-*", "in": "header", "schema": {"type": "object"}},
            ],
            consumes=["application/octet-stream"],
        ),
        _production_raw_api(
            "Oss",
            "2019-05-17",
            "GetObjectMeta",
            style="ROA",
            method="HEAD",
            path="/{key}",
            parameters=oss_common,
            produces=[],
        ),
        _production_raw_api(
            "Oss",
            "2019-05-17",
            "HeadObject",
            style="ROA",
            method="HEAD",
            path="/{key}",
            parameters=oss_common,
            produces=[],
        ),
        _production_raw_api(
            "Oss",
            "2019-05-17",
            "CompleteMultipartUpload",
            style="ROA",
            method="POST",
            path="/{key}",
            operation_type="write",
            parameters=oss_common,
            consumes=["application/xml"],
        ),
    ]
    return documents


def _production_services() -> tuple[Any, _ProductionOpenMeta, _ProductionEndpointResolver, _ProductionTransport]:
    openmeta = _ProductionOpenMeta(_production_documents())
    catalog = OssOperationCatalog.load()
    endpoint_resolver = _ProductionEndpointResolver()
    transport = _ProductionTransport()
    budgets: list[RetryBudget] = []

    def retry_budget_factory() -> RetryBudget:
        budget = RetryBudget(deadline=time.monotonic() + 60)
        budgets.append(budget)
        return budget

    services = SimpleNamespace(
        openmeta=openmeta,
        contract_resolver=ApiContractResolver(openmeta, oss_catalog=catalog),
        request_builder=RequestBuilder(),
        endpoint_resolver=endpoint_resolver,
        host_binding_resolver=HostBindingResolver(("aliyuncs.com",)),
        transport_router=TransportRouter({"tea": transport, "acs3_streaming": transport, "oss_v4_sdk": transport}),
        oss_operation_catalog=catalog,
        contract_store=ResolvedContractStore(),
        credential_provider=lambda: AliyunCredential(
            access_key_id="fake-id",
            access_key_secret="fake-secret",
            region_id="cn-hangzhou",
        ),
        retry_budget_factory=retry_budget_factory,
        random=lambda: 0.0,
        permission_stage_observer=None,
        execution_stage_observer=None,
        budgets=budgets,
    )
    return services, openmeta, endpoint_resolver, transport


async def _production_execute(
    tool: AliyunApi,
    tool_input: dict[str, Any],
    *,
    cwd: str = "",
    event_queue: asyncio.Queue | None = None,
    tool_use_id: str | None = "production-call",
) -> Any:
    tool_input = tool.prepare_invocation_input(tool_input)
    binding = InvocationBinding(
        runtime_nonce="runtime",
        session_id="session",
        tool_use_id="production-call",
        tool_name="aliyun_api",
        canonical_input_sha256=canonical_input_sha256(tool_input),
    )
    permission_context = ToolPermissionContext(
        cwd=cwd,
        trusted_read_directories=[cwd] if cwd else [],
        invocation_binding=binding,
    )
    permission = await tool.check_permissions(tool_input, permission_context)
    assert permission.behavior in {"allow", "ask"}
    assert permission.snapshot_id is not None
    assert permission.security_digest is not None
    return await tool.execute(
        tool_input=tool_input,
        context=ToolContext(
            cwd=cwd,
            event_queue=event_queue,
            tool_use_id=tool_use_id,
            trusted_read_directories=[cwd] if cwd else [],
            invocation_binding=binding,
            snapshot_id=permission.snapshot_id,
            security_digest=permission.security_digest,
            execution_class=permission.execution_class,
        ),
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("version", [None, "2022-04-08"], ids=["default-version", "explicit-version"])
async def test_product_single_edit_is_shared_by_execute_and_doc_tools(version: str | None) -> None:
    services, openmeta, _, transport = _production_services()
    execute_tool = AliyunApi(services=services)
    doc_tool = AliyunApiDoc(services)
    tool_input: dict[str, Any] = {
        "product": "Chatbo",
        "action": "ListInstance",
        "region_id": "cn-hangzhou",
    }
    if version is not None:
        tool_input["version"] = version

    execute_result = await _production_execute(execute_tool, tool_input)
    doc_result = await doc_tool.execute(
        tool_input={key: value for key, value in tool_input.items() if key != "region_id"},
        context=ToolContext(tool_use_id="matching-doc"),
    )

    assert execute_result.is_error is False
    assert doc_result.is_error is False
    assert json.loads(doc_result.content)["product"] == "Chatbot"
    assert json.loads(doc_result.content)["version"] == "2022-04-08"
    assert transport.calls[-1]["contract"].product == "Chatbot"
    assert transport.calls[-1]["contract"].requested_product == "Chatbo"
    assert transport.calls[-1]["contract"].product_match_strategy == "single_edit"
    assert transport.calls[-1]["contract"].product_match_confidence == "medium"
    assert all(call[1] == "Chatbot" for call in openmeta.calls if call[0] == "api")


@pytest.mark.asyncio
async def test_unlisted_product_with_explicit_version_is_shared_by_execute_and_doc_tools() -> None:
    services, openmeta, _, transport = _production_services()
    raw = _production_raw_api("NewService", "2026-07-19", "DescribeThings")
    openmeta.documents[("newservice", "2026-07-19", "describethings")] = raw
    tool_input = {
        "product": "NewService",
        "version": "2026-07-19",
        "action": "DescribeThings",
        "region_id": "cn-hangzhou",
    }

    execute_result = await _production_execute(AliyunApi(services=services), tool_input)
    doc_result = await AliyunApiDoc(services).execute(
        tool_input={key: value for key, value in tool_input.items() if key != "region_id"},
        context=ToolContext(tool_use_id="unlisted-product-doc"),
    )

    assert execute_result.is_error is False
    assert doc_result.is_error is False
    contract = transport.calls[-1]["contract"]
    assert contract.product == "NewService"
    assert contract.product_match_strategy == "unverified"
    assert contract.product_match_confidence == "none"
    assert openmeta.calls
    assert all(call == ("api", "NewService", "2026-07-19", "DescribeThings") for call in openmeta.calls)


@pytest.mark.asyncio
async def test_ascii_whitespace_product_reaches_the_shared_resolver_through_both_tool_schemas() -> None:
    services, _, _, transport = _production_services()
    execute_tool = AliyunApi(services=services)
    doc_tool = AliyunApiDoc(services)
    requested = "\t Chatbot \n"

    execute_result = await _production_execute(
        execute_tool,
        {"product": requested, "action": "ListInstance", "region_id": "cn-hangzhou"},
    )
    doc_result = await doc_tool.execute(
        tool_input={"product": requested, "action": "ListInstance"},
        context=ToolContext(tool_use_id="trimmed-doc"),
    )

    assert execute_result.is_error is False
    assert doc_result.is_error is False
    assert json.loads(doc_result.content)["product"] == "Chatbot"
    contract = transport.calls[-1]["contract"]
    assert contract.product == "Chatbot"
    assert contract.requested_product == requested
    assert contract.product_match_strategy == "trimmed_exact"
    assert contract.product_match_confidence == "high"


@pytest.mark.asyncio
async def test_ros_builtin_alias_preserves_local_template_materialization(tmp_path: Path) -> None:
    services, _, _, transport = _production_services()
    tool = AliyunApi(services=services)
    template = tmp_path / "template.json"
    template.write_text(
        json.dumps({"ROSTemplateFormatVersion": "2015-09-01", "Resources": {}}),
        encoding="utf-8",
    )

    result = await _production_execute(
        tool,
        {
            "product": "ResourceOrchestrationService",
            "action": "ValidateTemplate",
            "params": {"TemplateURL": str(template)},
            "region_id": "cn-hangzhou",
        },
        cwd=str(tmp_path),
    )

    assert result.is_error is False
    call = transport.calls[-1]
    assert call["contract"].product == "ROS"
    assert call["contract"].product_match_strategy == "builtin_alias"
    assert b"TemplateBody=" in call["request"].body
    assert str(template).encode() not in call["request"].body


@pytest.mark.asyncio
async def test_ambiguous_product_stops_before_api_credentials_endpoint_and_target() -> None:
    services, openmeta, endpoint_resolver, transport = _production_services()
    credential_calls: list[None] = []
    services.credential_provider = lambda: credential_calls.append(None)
    tool = AliyunApi(services=services)
    tool_input = tool.prepare_invocation_input(
        {"product": "dy0msapi", "action": "DescribeSomething", "region_id": "cn-hangzhou"}
    )
    binding = InvocationBinding(
        "runtime",
        "session",
        "ambiguous-product",
        "aliyun_api",
        canonical_input_sha256(tool_input),
    )

    permission = await tool.check_permissions(
        tool_input,
        ToolPermissionContext(invocation_binding=binding),
    )

    assert permission.behavior == "deny"
    assert "Dysmsapi" in (permission.message or "")
    assert "Dyvmsapi" in (permission.message or "")
    assert openmeta.calls == []
    assert credential_calls == []
    assert endpoint_resolver.calls == []
    assert transport.calls == []


@pytest.mark.asyncio
async def test_exact_product_action_miss_never_switches_to_another_product() -> None:
    services, openmeta, endpoint_resolver, transport = _production_services()
    credential_calls: list[None] = []
    services.credential_provider = lambda: credential_calls.append(None)
    tool = AliyunApi(services=services)
    tool_input = tool.prepare_invocation_input(
        {"product": "Chatbot", "action": "DescribeInstances", "region_id": "cn-hangzhou"}
    )
    binding = InvocationBinding(
        "runtime",
        "session",
        "missing-action",
        "aliyun_api",
        canonical_input_sha256(tool_input),
    )

    permission = await tool.check_permissions(tool_input, ToolPermissionContext(invocation_binding=binding))

    assert permission.behavior == "deny"
    assert ("api", "Chatbot", "2022-04-08", "DescribeInstances") in openmeta.calls
    assert not any(call[0] == "api" and call[1] == "Ecs" for call in openmeta.calls)
    assert credential_calls == []
    assert endpoint_resolver.calls == []
    assert transport.calls == []


@pytest.mark.asyncio
async def test_canonical_product_rechecks_deny_rules_after_alias_resolution() -> None:
    services, openmeta, endpoint_resolver, transport = _production_services()
    tool = AliyunApi(services=services)
    tool_input = tool.prepare_invocation_input(
        {
            "product": "ElasticComputeService",
            "action": "DescribeInstances",
            "region_id": "cn-hangzhou",
        }
    )
    binding = InvocationBinding(
        "runtime",
        "session",
        "canonical-deny",
        "aliyun_api",
        canonical_input_sha256(tool_input),
    )

    permission = await tool.check_permissions(
        tool_input,
        ToolPermissionContext(invocation_binding=binding, deny_rules={"session": ["aliyun_api(Ecs:*)"]}),
    )

    assert permission.behavior == "deny"
    assert permission.audit is not None and permission.audit.rule == "Ecs:*"
    assert ("api", "Ecs", "2014-05-26", "DescribeInstances") in openmeta.calls
    assert endpoint_resolver.calls == []
    assert transport.calls == []


@pytest.mark.asyncio
async def test_canonical_ros_product_rechecks_pipeline_guard_after_alias_resolution() -> None:
    services, openmeta, endpoint_resolver, transport = _production_services()
    tool = AliyunApi(services=services)
    tool_input = tool.prepare_invocation_input(
        {
            "product": "ResourceOrchestrationService",
            "action": "CreateStack",
            "region_id": "cn-hangzhou",
        }
    )
    binding = InvocationBinding(
        "runtime",
        "session",
        "canonical-ros",
        "aliyun_api",
        canonical_input_sha256(tool_input),
    )

    permission = await tool.check_permissions(
        tool_input,
        ToolPermissionContext(invocation_binding=binding, pipeline_mode=True),
    )

    assert permission.behavior == "deny"
    assert "raw ROS deployment API" in (permission.message or "")
    assert ("api", "ROS", "2019-09-10", "CreateStack") in openmeta.calls
    assert endpoint_resolver.calls == []
    assert transport.calls == []


@pytest.mark.asyncio
async def test_omitted_region_uses_pure_local_default_before_binding_permission_and_execution() -> None:
    services, openmeta, endpoint_resolver, transport = _production_services()
    stages: list[str] = []
    services.default_region_provider = lambda: stages.append("default_region") or "cn-shanghai"
    services.permission_stage_observer = stages.append
    tool = AliyunApi(services=services)
    raw_input = {
        "product": "Ecs",
        "action": "DescribeInstances",
        "params": {"InstanceIds": '["i-private"]'},
    }

    effective_input = tool.prepare_invocation_input(raw_input)
    result = await _production_execute(tool, raw_input)

    assert result.is_error is False
    assert "region_id" not in raw_input
    assert effective_input["region_id"] == "cn-shanghai"
    assert canonical_input_sha256(effective_input) != canonical_input_sha256(raw_input)
    assert stages.index("default_region") < stages.index("openmeta")
    assert openmeta.calls[0] == ("api", "Ecs", "2014-05-26", "DescribeInstances")
    assert endpoint_resolver.calls[-1][1] == "cn-shanghai"
    assert dict(transport.calls[-1]["request"].canonical_query) == {
        "InstanceIds": '["i-private"]',
        "RegionId": "cn-shanghai",
    }
    assert (
        transport.calls[-1]["contract"].security_digest(
            aliyun_api_module._runtime_call_shape(
                effective_input,
                contract=transport.calls[-1]["contract"],
            )
        )
        == transport.calls[-1]["context"].security_digest
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("action", ["DescribeInstances", "EchoBody"])
async def test_protocol_metadata_error_fails_before_credential_endpoint_and_target(action: str) -> None:
    services, openmeta, endpoint_resolver, transport = _production_services()
    credential_calls: list[None] = []
    services.credential_provider = lambda: credential_calls.append(None)

    async def protocol_error(product: str, version: str, requested_action: str) -> MetadataFetch[Any]:
        openmeta.calls.append(("api", product, version, requested_action))
        return MetadataFetch(value=None, source=None, error="protocol_error", cache_status="miss")

    openmeta.get_api = protocol_error  # type: ignore[method-assign]
    tool = AliyunApi(services=services)
    tool_input: dict[str, Any] = {
        "product": "Ecs",
        "action": action,
        "region_id": "cn-hangzhou",
    }
    if action == "EchoBody":
        tool_input["body"] = {"name": "business-value"}
    binding = InvocationBinding(
        runtime_nonce="runtime",
        session_id="session",
        tool_use_id="protocol-error",
        tool_name="aliyun_api",
        canonical_input_sha256=canonical_input_sha256(tool_input),
    )

    permission = await tool.check_permissions(
        tool_input,
        ToolPermissionContext(invocation_binding=binding),
    )

    assert permission.behavior == "deny"
    assert credential_calls == []
    assert endpoint_resolver.calls == []
    assert transport.calls == []


@pytest.mark.asyncio
async def test_explicit_version_never_reads_remote_product_catalog_when_api_metadata_is_unavailable() -> None:
    services, openmeta, endpoint_resolver, transport = _production_services()
    credential_calls: list[None] = []
    original_credential_provider = services.credential_provider

    def credential_provider() -> AliyunCredential:
        credential_calls.append(None)
        return original_credential_provider()

    async def remote_product_access_forbidden(product: str) -> MetadataFetch[Any]:
        raise AssertionError("remote product metadata must not be read")

    async def api_temporarily_unavailable(
        product: str,
        version: str,
        action: str,
    ) -> MetadataFetch[Any]:
        openmeta.calls.append(("api", product, version, action))
        return MetadataFetch(
            value=None,
            source=None,
            error="temporarily_unavailable",
            cache_status="miss",
        )

    services.credential_provider = credential_provider
    openmeta.get_product = remote_product_access_forbidden  # type: ignore[method-assign]
    openmeta.get_api = api_temporarily_unavailable  # type: ignore[method-assign]
    tool = AliyunApi(services=services)
    tool_input = {
        "product": "Ecs",
        "version": "2014-05-26",
        "action": "DescribeInstances",
        "region_id": "cn-hangzhou",
    }
    binding = InvocationBinding(
        runtime_nonce="runtime",
        session_id="session",
        tool_use_id="product-error",
        tool_name="aliyun_api",
        canonical_input_sha256=canonical_input_sha256(tool_input),
    )

    permission = await tool.check_permissions(
        tool_input,
        ToolPermissionContext(invocation_binding=binding),
    )
    execution = await tool._execute_internal_trusted(tool_input, ToolContext())

    assert permission.behavior == "allow"
    assert execution.is_error is False
    assert openmeta.calls == [
        ("api", "Ecs", "2014-05-26", "DescribeInstances"),
        ("api", "Ecs", "2014-05-26", "DescribeInstances"),
    ]
    assert credential_calls == [None]
    assert len(endpoint_resolver.calls) == 1
    assert len(transport.calls) == 1


@pytest.mark.asyncio
async def test_ros_hook_syntax_error_preserves_original_detail(tmp_path: Path) -> None:
    services, _, _, transport = _production_services()
    credential_calls: list[None] = []
    services.credential_provider = lambda: credential_calls.append(None)
    tool = AliyunApi(services=services)
    secret = "CUSTOMER_TOKEN_123"
    template = tmp_path / "invalid-template.yml"
    template.write_text(
        f"ROSTemplateFormatVersion: '2015-09-01'\nDescription: {secret}\nResources: [\n",
        encoding="utf-8",
    )

    result = await _production_execute(
        tool,
        {
            "product": "ros",
            "action": "ValidateTemplate",
            "params": {"TemplateURL": str(template)},
            "region_id": "cn-hangzhou",
        },
        cwd=str(tmp_path),
    )

    assert result.is_error is True
    assert "ROS1001" in result.content
    assert "line 4:1" in result.content
    assert "Context:" not in result.content
    assert secret not in result.content
    assert str(template) not in result.content
    assert credential_calls == []
    assert transport.calls == []


@pytest.mark.asyncio
async def test_ros_hook_structure_error_preserves_actionable_detail(tmp_path: Path) -> None:
    services, _, _, transport = _production_services()
    tool = AliyunApi(services=services)
    template = tmp_path / "existing-vpc-vswitch.yml"
    template.write_text(
        "ROSTemplateFormatVersion: '2015-09-01'\n"
        "Parameters:\n"
        "  VpcId:\n"
        "    Type: String\n"
        "    AssociationProperty: ALIYUN::ECS::VPC::VPCId\n"
        "Resources:\n"
        "  VSwitch:\n"
        "    Type: ALIYUN::ECS::VSwitch\n"
        "    Properties:\n"
        "      VpcId: !Ref VpcId\n"
        "      ZoneId: cn-hangzhou-k\n"
        "      CidrBlock: 192.168.0.0/24\n",
        encoding="utf-8",
    )

    result = await _production_execute(
        tool,
        {
            "product": "ros",
            "action": "ValidateTemplate",
            "params": {"TemplateURL": str(template)},
            "region_id": "cn-hangzhou",
        },
        cwd=str(tmp_path),
    )

    assert result.is_error is True
    assert "ROS5102" in result.content
    assert "existing VPC" in result.content
    assert "CidrBlock" in result.content
    assert str(template) not in result.content
    assert transport.calls == []


@pytest.mark.asyncio
async def test_openmeta_preserves_hook_error_result(monkeypatch) -> None:
    services, _, _, transport = _production_services()
    hook_detail = "actionable hook detail"
    monkeypatch.setattr(
        "iac_code.tools.cloud.aliyun.api_hooks.run_hooks",
        lambda *_args, **_kwargs: ToolResult.error(hook_detail),
    )

    result = await _production_execute(
        AliyunApi(services=services),
        _target_test_input("DescribeInstances"),
    )

    assert result == ToolResult.error(hook_detail)
    assert transport.calls == []


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["success", "service-error", "internal-error"])
async def test_ros_adapter_preflights_once_and_attaches_outcome(mode: str) -> None:
    from iac_code.tools.cloud.aliyun.api_hooks import run_hooks

    services, _, endpoint_resolver, transport = _production_services()
    if mode == "service-error":
        transport.responses["ValidateTemplate"] = NormalizedApiResponse(
            status=500,
            headers=MappingProxyType({}),
            body={"Code": "InvalidRequest", "Message": "service failed"},
            content_type="application/json",
            content_encoding=None,
            size=64,
        )
    elif mode == "internal-error":
        endpoint_resolver.failure = RuntimeError("endpoint failed after preflight")
    body = (
        "ROSTemplateFormatVersion: '2015-09-01'\n"
        "Resources:\n"
        "  Wait:\n"
        "    Type: ALIYUN::ROS::Sleep\n"
        "    Properties:\n"
        "      Triggers: {Zones: {Fn::GetAZs: ''}}\n"
    )

    with patch("iac_code.tools.cloud.aliyun.api_hooks.run_hooks", wraps=run_hooks) as hook:
        result = await _production_execute(
            AliyunApi(services=services),
            {
                "product": "ros",
                "action": "ValidateTemplate",
                "params": {"TemplateBody": body},
                "region_id": "cn-hangzhou",
            },
        )

    hook.assert_called_once()
    assert hook.call_args.kwargs["read_only"] is True
    assert result.is_error is (mode != "success")
    assert result.metadata["ros_validation"]["warning_count"] >= 1
    assert "ROS local preflight diagnostics" in result.content


@pytest.mark.asyncio
async def test_ros_adapter_preflights_remote_template_url_once() -> None:
    from iac_code.tools.cloud.aliyun.api_hooks import run_hooks

    services, _, _, _ = _production_services()

    with patch("iac_code.tools.cloud.aliyun.api_hooks.run_hooks", wraps=run_hooks) as hook:
        await _production_execute(
            AliyunApi(services=services),
            {
                "product": "ros",
                "action": "ValidateTemplate",
                "params": {"TemplateURL": "https://example.com/template.yml"},
                "region_id": "cn-hangzhou",
            },
        )

    hook.assert_called_once()
    assert hook.call_args.kwargs["read_only"] is True


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "content_type",
    [
        "application /json",
        "application/(comment)json",
        'application/json; charset="utf-8";',
        "application/json; charset=utf-8; CHARSET=us-ascii",
    ],
)
async def test_invalid_content_type_stops_before_openmeta_credential_and_target(content_type: str) -> None:
    services, openmeta, endpoint_resolver, transport = _production_services()
    credential_calls: list[None] = []
    services.credential_provider = lambda: credential_calls.append(None)
    tool = AliyunApi(services=services)
    tool_input = {
        "product": "Ecs",
        "version": "2014-05-26",
        "action": "EchoBody",
        "region_id": "cn-hangzhou",
        "body": {"name": "business-value"},
        "content_type": content_type,
    }
    binding = InvocationBinding(
        runtime_nonce="runtime",
        session_id="session",
        tool_use_id="invalid-content-type",
        tool_name="aliyun_api",
        canonical_input_sha256=canonical_input_sha256(tool_input),
    )

    permission = await tool.check_permissions(
        tool_input,
        ToolPermissionContext(invocation_binding=binding),
    )
    execution = await tool._execute_internal_trusted(tool_input, ToolContext())

    assert permission.behavior == "deny"
    expected = public_aliyun_error(
        ApiContractError("invalid_content_type"),
        product="Ecs",
        version="2014-05-26",
        action="EchoBody",
        region_id="cn-hangzhou",
    )
    assert permission.message == expected
    assert execution.is_error is True
    assert execution.content == expected
    assert openmeta.calls == []
    assert credential_calls == []
    assert endpoint_resolver.calls == []
    assert transport.calls == []


@pytest.mark.asyncio
async def test_recursive_parameter_contract_is_authorized_without_target_call() -> None:
    services, openmeta, endpoint_resolver, transport = _production_services()
    credential_calls: list[None] = []
    services.credential_provider = lambda: credential_calls.append(None)
    recursive = _production_raw_api(
        "Ecs",
        "2014-05-26",
        "RecursiveAction",
        parameters=[
            {
                "name": "Node",
                "in": "query",
                "schema": {"$ref": "#/components/schemas/Node"},
            }
        ],
    )
    recursive["components"] = {
        "schemas": {
            "Node": {
                "type": "object",
                "properties": {"child": {"$ref": "#/components/schemas/Node"}},
            }
        }
    }
    openmeta.documents[("ecs", "2014-05-26", "recursiveaction")] = recursive
    tool_input = {
        "product": "Ecs",
        "action": "RecursiveAction",
        "region_id": "cn-hangzhou",
        "params": {"Node": {"child": None}},
    }
    binding = InvocationBinding(
        runtime_nonce="runtime",
        session_id="session",
        tool_use_id="recursive",
        tool_name="aliyun_api",
        canonical_input_sha256=canonical_input_sha256(tool_input),
    )

    permission = await AliyunApi(services=services).check_permissions(
        tool_input,
        ToolPermissionContext(invocation_binding=binding),
    )

    assert permission.behavior == "allow"
    assert permission.snapshot_id is not None
    assert permission.security_digest is not None
    assert credential_calls == []
    assert endpoint_resolver.calls == []
    assert transport.calls == []


def _target_test_input(action: str) -> dict[str, Any]:
    tool_input: dict[str, Any] = {
        "product": "Oss" if action == "HeadObject" else "Ecs",
        "action": action,
        "region_id": "cn-hangzhou",
    }
    if action == "HeadObject":
        tool_input["params"] = {"bucket": "private-bucket", "key": "private/path"}
    return tool_input


def _assert_detailed_production_telemetry(
    emitter: MagicMock,
    count: _CaptureMetricInstrument,
    duration: _CaptureMetricInstrument,
    *,
    target_outcome: str,
    legacy_outcome: str,
) -> None:
    event_calls = [call.args for call in emitter.emit.call_args_list]
    detailed = [metadata for name, metadata in event_calls if name == Events.ALIYUN_API_CALLED]
    legacy = [metadata for name, metadata in event_calls if name == Events.ALIYUN_API_LEGACY_CALLED]
    expected_attributes = {
        "api_service": "ECS",
        "outcome": legacy_outcome,
        "target_outcome": target_outcome,
    }
    assert len(detailed) == 1
    assert detailed[0]["outcome"] == target_outcome
    assert legacy == [{"outcome": legacy_outcome}]
    assert count.calls == [(1, expected_attributes)]
    assert len(duration.calls) == 1
    assert duration.calls[0][0] >= 0
    assert duration.calls[0][1] == expected_attributes
    serialized = json.dumps({"events": event_calls, "count": count.calls, "duration": duration.calls})
    for secret in (
        "business-value",
        "private-bucket",
        "private/path",
        "artifact_path",
        "secret",
        "InvalidRequest",
    ):
        assert secret not in serialized


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("mode", "target_outcome", "legacy_outcome"),
    [
        ("success", "success", "success"),
        ("http", "http_error", "failure"),
        ("pre_connect", "pre_connect_failure", "failure"),
        ("unknown", "unknown_after_transport_error", "failure"),
    ],
)
async def test_target_outcome_reaches_real_production_event_and_metric_sinks_once(
    mode: str,
    target_outcome: str,
    legacy_outcome: str,
) -> None:
    services, _, _, transport = _production_services()
    if mode == "http":
        transport.responses["EchoBody"] = NormalizedApiResponse(
            status=500,
            headers=MappingProxyType({"authorization": "secret"}),
            body={"Code": "InvalidRequest", "Message": "business-value"},
            content_type="application/json",
            content_encoding=None,
            size=128,
        )
    elif mode == "pre_connect":
        transport.errors["EchoBody"] = TransportFailure(outcome="pre_connect_failure", reason=None)
    elif mode == "unknown":
        transport.errors["EchoBody"] = TransportFailure(
            outcome="unknown_after_transport_error",
            reason=None,
        )
    client, emitter, count, duration = _production_telemetry_client()
    set_client(client)
    try:
        await _production_execute(
            AliyunApi(services=services),
            {
                "product": "Ecs",
                "action": "EchoBody",
                "region_id": "cn-hangzhou",
                "body": {"name": "business-value"},
            },
        )
    finally:
        set_client(None)

    _assert_detailed_production_telemetry(
        emitter,
        count,
        duration,
        target_outcome=target_outcome,
        legacy_outcome=legacy_outcome,
    )


def _recorded_span_attributes(span: MagicMock) -> dict[str, Any]:
    return {call.args[0]: call.args[1] for call in span.set_attribute.call_args_list}


@pytest.mark.parametrize("key", ["RequestId", "requestId", "request_id", "REQUEST-ID"])
def test_response_request_id_accepts_body_key_variants(key: str) -> None:
    response = SimpleNamespace(body={key: "request-body-1"}, headers={})

    assert aliyun_api_module._response_request_id(response) == "request-body-1"


@pytest.mark.parametrize(
    "key",
    ["Request-Id", "X-Acs-Request-Id", "X-Log-RequestId", "x_oss_request_id", "x-request-id"],
)
def test_response_request_id_accepts_header_key_variants(key: str) -> None:
    response = SimpleNamespace(body={}, headers={key: "request-header-1"})

    assert aliyun_api_module._response_request_id(response) == "request-header-1"


@pytest.mark.parametrize("key", ["ErrorCode", "errorCode", "error_code", "ERROR-CODE", "Code", "error"])
def test_response_error_code_accepts_body_key_variants(key: str) -> None:
    response = SimpleNamespace(body={key: "Throttling.Api"}, headers={})

    assert aliyun_api_module._response_error_code(response) == "Throttling.Api"


@pytest.mark.parametrize(
    "key",
    ["Error-Code", "X-Acs-Error-Code", "X-Log-Error-Code", "x_oss_error_code", "x-error-code"],
)
def test_response_error_code_accepts_header_key_variants(key: str) -> None:
    response = SimpleNamespace(body={}, headers={key: "Throttling.Api"})

    assert aliyun_api_module._response_error_code(response) == "Throttling.Api"


def test_exception_telemetry_accepts_properties_data_and_header_variants() -> None:
    class ApiError(RuntimeError):
        @property
        def status_code(self) -> str:
            return "429"

        @property
        def request_id(self) -> None:
            return None

    error = ApiError("failed")
    error.data = {"error_code": "Throttling.Api"}
    error.response_headers = {"X-Log-Request-Id": "request-exception-1"}

    assert aliyun_api_module._exception_telemetry_attrs(error) == {
        AliyunApiAttr.HTTP_STATUS_CODE: 429,
        AliyunApiAttr.REQUEST_ID: "request-exception-1",
        AliyunApiAttr.ERROR_CODE: "Throttling.Api",
    }


@pytest.mark.asyncio
async def test_target_success_emits_queryable_api_call_span() -> None:
    services, _, _, transport = _production_services()
    transport.responses["DescribeInstances"] = NormalizedApiResponse(
        status=200,
        headers=MappingProxyType({}),
        body={"RequestId": "request-success-1", "Instances": []},
        content_type="application/json",
        content_encoding=None,
        size=64,
    )
    span = MagicMock()

    with (
        patch.object(aliyun_api_module, "start_span", return_value=nullcontext(span)) as start_span,
        patch.object(aliyun_api_module, "get_session_id", return_value="iac_sess_1"),
        patch.object(aliyun_api_module, "get_user_id", return_value="iac_user_1"),
    ):
        result = await _production_execute(
            AliyunApi(services=services),
            {"product": "Ecs", "action": "DescribeInstances", "region_id": "cn-hangzhou"},
        )

    assert result.is_error is False
    start_span.assert_called_once()
    assert start_span.call_args.args[0] == Spans.ALIYUN_API_CALL
    initial_attrs = start_span.call_args.args[1]
    assert initial_attrs[AliyunApiAttr.SERVICE] == "ECS"
    assert initial_attrs[AliyunApiAttr.PRODUCT] == "Ecs"
    assert initial_attrs[AliyunApiAttr.ACTION] == "DescribeInstances"
    assert initial_attrs[AliyunApiAttr.VERSION] == "2014-05-26"
    assert initial_attrs[AliyunApiAttr.REGION] == "cn-hangzhou"
    assert initial_attrs[AliyunApiAttr.HTTP_METHOD] == "POST"
    assert initial_attrs[GenAiAttr.TOOL_CALL_ID] == "production-call"
    assert initial_attrs[GenAiAttr.SESSION_ID] == "iac_sess_1"
    assert initial_attrs[GenAiAttr.USER_ID] == "iac_user_1"
    result_attrs = _recorded_span_attributes(span)
    assert result_attrs == {
        AliyunApiAttr.OUTCOME: "success",
        AliyunApiAttr.TARGET_OUTCOME: "success",
        AliyunApiAttr.HTTP_STATUS_CODE: 200,
        AliyunApiAttr.REQUEST_ID: "request-success-1",
    }


@pytest.mark.asyncio
async def test_target_http_error_span_records_status_error_code_and_header_request_id() -> None:
    services, _, _, transport = _production_services()
    transport.responses["DescribeInstances"] = NormalizedApiResponse(
        status=429,
        headers=MappingProxyType({"x-acs-request-id": "request-http-1"}),
        body={"ErrorCode": 30010, "Message": "retry later"},
        content_type="application/json",
        content_encoding=None,
        size=64,
    )
    span = MagicMock()

    with patch.object(aliyun_api_module, "start_span", return_value=nullcontext(span)):
        result = await _production_execute(
            AliyunApi(services=services),
            {"product": "Ecs", "action": "DescribeInstances", "region_id": "cn-hangzhou"},
        )

    assert result.is_error is True
    result_attrs = _recorded_span_attributes(span)
    assert result_attrs == {
        AliyunApiAttr.OUTCOME: "failure",
        AliyunApiAttr.TARGET_OUTCOME: "http_error",
        AliyunApiAttr.HTTP_STATUS_CODE: 429,
        AliyunApiAttr.REQUEST_ID: "request-http-1",
        AliyunApiAttr.ERROR_CODE: "30010",
    }


@pytest.mark.asyncio
async def test_target_transport_failure_span_records_outcome_without_response_fields() -> None:
    services, _, _, transport = _production_services()
    error = TransportFailure(outcome="connect_timeout", reason=None)
    error.request_id = "request-transport-1"
    error.code = "NetworkTimeout"
    transport.errors["DescribeInstances"] = error
    span = MagicMock()

    with patch.object(aliyun_api_module, "start_span", return_value=nullcontext(span)):
        result = await _production_execute(
            AliyunApi(services=services),
            {"product": "Ecs", "action": "DescribeInstances", "region_id": "cn-hangzhou"},
        )

    assert result.is_error is True
    result_attrs = _recorded_span_attributes(span)
    assert result_attrs == {
        AliyunApiAttr.OUTCOME: "failure",
        AliyunApiAttr.TARGET_OUTCOME: "connect_timeout",
        AliyunApiAttr.REQUEST_ID: "request-transport-1",
        AliyunApiAttr.ERROR_CODE: "NetworkTimeout",
    }


@pytest.mark.asyncio
async def test_target_cancel_reaches_real_production_event_and_metric_sinks_once() -> None:
    services, _, _, transport = _production_services()
    started = asyncio.Event()
    blocked = asyncio.Event()

    async def cancelling_transport(**kwargs: Any) -> NormalizedApiResponse:
        transport.calls.append(kwargs)
        started.set()
        await blocked.wait()
        raise AssertionError("unreachable")

    transport.execute = cancelling_transport  # type: ignore[method-assign]
    client, emitter, count, duration = _production_telemetry_client()
    set_client(client)
    task = asyncio.create_task(
        _production_execute(
            AliyunApi(services=services),
            {
                "product": "Ecs",
                "action": "EchoBody",
                "region_id": "cn-hangzhou",
                "body": {"name": "business-value"},
            },
        )
    )
    try:
        await asyncio.wait_for(started.wait(), timeout=1)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    finally:
        set_client(None)

    _assert_detailed_production_telemetry(
        emitter,
        count,
        duration,
        target_outcome="unknown_after_cancel",
        legacy_outcome="failure",
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("action", "expected_transport"),
    [
        ("DescribeInstances", "tea"),
        ("GetText", "acs3_streaming"),
        ("HeadObject", "oss_v4_sdk"),
    ],
)
@pytest.mark.parametrize("status", [302, 400, 500])
async def test_shared_target_boundary_converts_transport_http_errors_to_sanitized_semantic_tool_errors(
    monkeypatch,
    action: str,
    expected_transport: str,
    status: int,
) -> None:
    services, _, _, transport = _production_services()
    target_audit: list[dict[str, Any]] = []
    legacy_events: list[tuple[str, dict[str, Any]]] = []
    metrics: list[tuple[Any, ...]] = []
    contract_events: list[dict[str, Any]] = []
    services.target_outcome_observer = target_audit.append
    monkeypatch.setattr(aliyun_api_module, "log_event", lambda name, fields: legacy_events.append((name, fields)))
    monkeypatch.setattr(aliyun_api_module, "add_metric", lambda *args: metrics.append(args))
    monkeypatch.setattr(aliyun_api_module, "emit_aliyun_api_called", lambda **fields: contract_events.append(fields))
    transport.responses[action] = NormalizedApiResponse(
        status=status,
        headers=MappingProxyType(
            {
                "x-acs-request-id": "request-private",
                "authorization": "secret-authorization",
            }
        ),
        body={
            "Code": "InvalidRequest",
            "Message": "该用户没有授权；InstanceId=i-1234567890abcdef0；AccessKeySecret=secret-value",
            "AccessKeySecret": "secret-value",
            "Description": "Use a valid business authorization before retrying.",
        },
        content_type="application/json",
        content_encoding=None,
        size=10_100,
    )
    tool = AliyunApi(services=services)

    result = await _production_execute(tool, _target_test_input(action))

    assert transport.calls[-1]["contract"].transport == expected_transport
    assert result.is_error is True
    product = "Oss" if action == "HeadObject" else "Ecs"
    assert result.content.startswith(
        f"Alibaba Cloud API {product}/{action} returned HTTP {status} with error code InvalidRequest. "
        "Check the request and cloud permissions before retrying."
    )
    assert "Message: 该用户没有授权" in result.content
    assert "Description: Use a valid business authorization before retrying." in result.content
    assert len(result.content) < 768
    assert "i-1234567890abcdef0" not in result.content
    assert "secret" not in result.content.casefold()
    assert target_audit == [{"outcome": "http_error", "duration_ms": target_audit[0]["duration_ms"]}]
    assert legacy_events == [(Events.ALIYUN_API_LEGACY_CALLED, {"outcome": "failure"})]
    assert len(contract_events) == 1
    assert [entry[0] for entry in metrics] == [
        Metrics.ALIYUN_API_CALLED_COUNT,
        Metrics.ALIYUN_API_CALLED_DURATION,
    ]


@pytest.mark.asyncio
async def test_target_boundary_preserves_oauth_style_business_error_semantics(monkeypatch) -> None:
    services, _, _, transport = _production_services()
    monkeypatch.setattr(aliyun_api_module, "log_event", lambda *_args: None)
    monkeypatch.setattr(aliyun_api_module, "add_metric", lambda *_args: None)
    monkeypatch.setattr(aliyun_api_module, "emit_aliyun_api_called", lambda **_fields: None)
    transport.responses["DescribeInstances"] = NormalizedApiResponse(
        status=404,
        headers=MappingProxyType({"x-acs-request-id": "request-private"}),
        body={
            "error": "instance_not_found",
            "error_description": "Instance not found by request.",
            "request_id": "request-private",
        },
        content_type="application/json",
        content_encoding=None,
        size=160,
    )

    result = await _production_execute(AliyunApi(services=services), _target_test_input("DescribeInstances"))

    assert result.is_error is True
    assert "HTTP 404 with error code instance_not_found" in result.content
    assert "Description: Instance not found by request." in result.content
    assert "request-private" not in result.content


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "outcome",
    ["pre_connect_failure", "unknown_after_transport_error"],
)
async def test_shared_target_boundary_records_transport_phase_outcome_and_bounded_error(
    monkeypatch,
    outcome: str,
) -> None:
    services, _, _, transport = _production_services()
    target_audit: list[dict[str, Any]] = []
    legacy_events: list[tuple[str, dict[str, Any]]] = []
    metrics: list[tuple[Any, ...]] = []
    services.target_outcome_observer = target_audit.append
    transport.errors["EchoBody"] = TransportFailure(outcome=outcome, reason=None)
    monkeypatch.setattr(aliyun_api_module, "log_event", lambda name, fields: legacy_events.append((name, fields)))
    monkeypatch.setattr(aliyun_api_module, "add_metric", lambda *args: metrics.append(args))
    tool = AliyunApi(services=services)

    result = await _production_execute(
        tool,
        {
            "product": "Ecs",
            "action": "EchoBody",
            "body": {"name": "business-value"},
            "region_id": "cn-hangzhou",
        },
    )

    assert result.is_error is True
    expected = {
        "pre_connect_failure": (
            "Alibaba Cloud API Ecs/EchoBody could not connect before the request was sent. "
            "Check network and endpoint access, then retry."
        ),
        "unknown_after_transport_error": (
            "Alibaba Cloud API Ecs/EchoBody may have been sent before the connection failed. "
            "Check cloud state before retrying to avoid duplicate changes."
        ),
    }
    assert result.content == expected[outcome]
    assert target_audit[0]["outcome"] == outcome
    assert legacy_events == [(Events.ALIYUN_API_LEGACY_CALLED, {"outcome": "failure"})]
    assert [entry[0] for entry in metrics] == [
        Metrics.ALIYUN_API_CALLED_COUNT,
        Metrics.ALIYUN_API_CALLED_DURATION,
    ]


@pytest.mark.asyncio
async def test_shared_target_boundary_records_unknown_cancel_but_not_pretarget_failures(monkeypatch) -> None:
    services, _, _, transport = _production_services()
    target_audit: list[dict[str, Any]] = []
    legacy_events: list[tuple[str, dict[str, Any]]] = []
    metrics: list[tuple[Any, ...]] = []
    started = asyncio.Event()
    blocked = asyncio.Event()

    async def cancelling_transport(**kwargs: Any) -> NormalizedApiResponse:
        transport.calls.append(kwargs)
        started.set()
        await blocked.wait()
        raise AssertionError("unreachable")

    transport.execute = cancelling_transport  # type: ignore[method-assign]
    services.target_outcome_observer = target_audit.append
    monkeypatch.setattr(aliyun_api_module, "log_event", lambda name, fields: legacy_events.append((name, fields)))
    monkeypatch.setattr(aliyun_api_module, "add_metric", lambda *args: metrics.append(args))
    tool = AliyunApi(services=services)
    task = asyncio.create_task(
        _production_execute(
            tool,
            {
                "product": "Ecs",
                "action": "EchoBody",
                "body": {"name": "business-value"},
                "region_id": "cn-hangzhou",
            },
        )
    )
    await asyncio.wait_for(started.wait(), timeout=1)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert target_audit[0]["outcome"] == "unknown_after_cancel"
    assert legacy_events == [(Events.ALIYUN_API_LEGACY_CALLED, {"outcome": "failure"})]
    assert [entry[0] for entry in metrics] == [
        Metrics.ALIYUN_API_CALLED_COUNT,
        Metrics.ALIYUN_API_CALLED_DURATION,
    ]

    pretarget_services, _, _, _ = _production_services()
    pretarget_audit: list[dict[str, Any]] = []
    pretarget_services.target_outcome_observer = pretarget_audit.append
    pretarget_services.credential_provider = lambda: (_ for _ in ()).throw(RuntimeError("credential_failure"))
    pretarget_result = await _production_execute(
        AliyunApi(services=pretarget_services),
        _target_test_input("DescribeInstances"),
    )
    assert pretarget_result.is_error is True
    assert pretarget_audit == []


@pytest.mark.asyncio
async def test_production_runtime_ecs_rpc_fc_roa_and_doc_execute_contract_identity() -> None:
    services, _, _, transport = _production_services()
    tool = AliyunApi(services=services)
    doc = AliyunApiDoc(services)

    ecs = await _production_execute(
        tool,
        {
            "product": "ecs",
            "action": "DescribeInstances",
            "params": {"InstanceIds": '["i-a","i-b"]'},
            "region_id": "cn-hangzhou",
        },
    )
    ecs_call = transport.calls[-1]
    assert ecs.is_error is False
    assert ecs_call["contract"].product == "Ecs"
    assert ecs_call["contract"].version == "2014-05-26"
    assert ecs_call["contract"].style == "RPC"
    assert ecs_call["request"].canonical_query == (
        ("InstanceIds", '["i-a","i-b"]'),
        ("RegionId", "cn-hangzhou"),
    )

    fc = await _production_execute(
        tool,
        {
            "product": "FC",
            "action": "GetFunction",
            "params": {"functionName": "demo", "qualifier": "LATEST"},
            "region_id": "cn-hangzhou",
        },
    )
    fc_call = transport.calls[-1]
    doc_result = await doc.execute(
        tool_input={"product": "FC", "action": "GetFunction"},
        context=ToolContext(tool_use_id="doc-call"),
    )
    doc_payload = json.loads(doc_result.content)
    assert fc.is_error is False
    assert fc_call["request"].raw_path == b"/2023-03-30/functions/demo"
    assert fc_call["request"].canonical_query == (("qualifier", "LATEST"),)
    assert doc_payload["product"] == fc_call["contract"].product
    assert doc_payload["version"] == fc_call["contract"].version
    assert doc_payload["style"] == fc_call["contract"].style
    assert doc_payload["method"] == fc_call["contract"].method
    assert doc_payload["path"] == fc_call["contract"].pathname
    assert doc_payload["executable"] == fc_call["contract"].executable
    assert doc_payload["unsupported_reasons"] == list(fc_call["contract"].unsupported_reasons)


@pytest.mark.asyncio
async def test_production_runtime_forwards_explicit_endpoint_as_bound_call_target() -> None:
    services, _, endpoint_resolver, transport = _production_services()
    endpoint = "ecs.cn-shenzhen.aliyuncs.com"

    result = await _production_execute(
        AliyunApi(services=services),
        {
            "product": "Ecs",
            "action": "DescribeInstances",
            "region_id": "cn-shenzhen",
            "endpoint": endpoint,
        },
    )

    assert result.is_error is False
    assert endpoint_resolver.calls[-1][4] == endpoint
    assert transport.calls[-1]["endpoint"].source == "explicit"
    assert transport.calls[-1]["endpoint"].wire_endpoint == endpoint


@pytest.mark.asyncio
async def test_production_runtime_anonymous_contract_does_not_load_credentials() -> None:
    services, openmeta, endpoint_resolver, transport = _production_services()
    raw = _production_raw_api("PublicSvc", "2026-01-01", "GetPublicInfo")
    raw["security"] = [{"Anonymous": []}]
    openmeta.documents[("publicsvc", "2026-01-01", "getpublicinfo")] = raw
    openmeta.products["publicsvc"] = ProductMetadata(
        product="PublicSvc",
        default_version="2026-01-01",
        versions=("2026-01-01",),
        documentation_url=None,
    )
    product_resolver = ProductResolver(
        openmeta,
        aliases_path=None,
        catalog=tuple(openmeta.products.values()),
    )
    services.contract_resolver = ApiContractResolver(
        openmeta,
        oss_catalog=OssOperationCatalog.load(),
        product_resolver=product_resolver,
    )
    services.credential_provider = lambda: (_ for _ in ()).throw(RuntimeError("credential_loaded"))

    result = await _production_execute(
        AliyunApi(services=services),
        {
            "product": "PublicSvc",
            "version": "2026-01-01",
            "action": "GetPublicInfo",
            "region_id": "cn-hangzhou",
        },
    )

    assert result.is_error is False
    assert json.loads(result.content) == {"RequestId": "request-1", "Action": "GetPublicInfo"}
    assert result.metadata is not None
    assert result.metadata[ALIYUN_HTTP_METADATA_KEY]["contract_version"] == ALIYUN_BODY_CONTRACT_VERSION
    assert result.metadata[ALIYUN_HTTP_METADATA_KEY]["status"] == 200
    assert endpoint_resolver.calls[0][2] is None
    assert transport.calls[0]["credential"] is None


@pytest.mark.asyncio
async def test_production_runtime_ros_template_formdata_hooks_and_events(tmp_path: Path) -> None:
    services, _, _, transport = _production_services()
    transport.responses["CreateStack"] = NormalizedApiResponse(
        200,
        MappingProxyType({}),
        {"StackId": "stack-1", "RequestId": "request-1"},
        "application/json",
        None,
        48,
    )
    tool = AliyunApi(services=services)
    template = tmp_path / "template.json"
    template.write_text(
        json.dumps(
            {
                "ROSTemplateFormatVersion": "2015-09-01",
                "Resources": {"Vpc": {"Type": "ALIYUN::ECS::VPC", "Properties": {}}},
            }
        ),
        encoding="utf-8",
    )

    with patch("iac_code.tools.cloud.aliyun.aliyun_api.log_event") as log_event_mock:
        validate = await _production_execute(
            tool,
            {
                "product": "ros",
                "action": "ValidateTemplate",
                "params": {"TemplateURL": str(template)},
                "region_id": "cn-hangzhou",
            },
            cwd=str(tmp_path),
        )
    validate_call = transport.calls[-1]
    assert validate.is_error is False
    assert validate_call["contract"].request_body_type == "formData"
    assert b"TemplateBody=" in validate_call["request"].body
    assert log_event_mock.call_args_list[-1].args[0] == Events.TEMPLATE_VALIDATED

    queue: asyncio.Queue = asyncio.Queue()
    created = await _production_execute(
        tool,
        {
            "product": "ros",
            "action": "CreateStack",
            "params": {"StackName": "demo", "TemplateURL": str(template)},
            "region_id": "cn-hangzhou",
        },
        event_queue=queue,
    )
    event = queue.get_nowait()
    assert created.is_error is False
    assert isinstance(event, ResourceObservedEvent)
    assert event.resource_id == "stack-1"


@pytest.mark.asyncio
@pytest.mark.parametrize("source_kind", ("inline", "local"))
@pytest.mark.parametrize("product", ("ros", "ResourceOrchestrationService"))
async def test_ros_parameters_hook_preserves_bound_security_shape(
    tmp_path: Path,
    source_kind: str,
    product: str,
) -> None:
    services, _, _, transport = _production_services()
    transport.responses["CreateStack"] = NormalizedApiResponse(
        200,
        MappingProxyType({}),
        {"StackId": "stack-parameters", "RequestId": "request-parameters"},
        "application/json",
        None,
        48,
    )
    template_body = "ROSTemplateFormatVersion: 2015-09-01\nResources: {}\n"
    params: dict[str, Any] = {
        "StackName": "demo",
        "Parameters": {"Environment": "test"},
    }
    cwd = ""
    if source_kind == "inline":
        params["TemplateBody"] = template_body
    else:
        template = tmp_path / "parameters-template.yaml"
        template.write_text(template_body, encoding="utf-8")
        params["TemplateURL"] = str(template)
        cwd = str(tmp_path)

    tool = AliyunApi(services=services)
    raw_input = {
        "product": product,
        "action": "CreateStack",
        "params": params,
        "region_id": "cn-hangzhou",
    }
    assert tool.prepare_invocation_input(raw_input)["params"] == params

    result = await _production_execute(tool, raw_input, cwd=cwd)

    assert result.is_error is False
    assert len(transport.calls) == 1
    assert params["Parameters"] == {"Environment": "test"}
    assert not any(key.startswith("Parameters.") for key in params)


@pytest.mark.parametrize("body", [[1, {"nested": True}], "scalar", 7, True, None])
@pytest.mark.asyncio
async def test_production_runtime_accepts_arbitrary_json_body(body: Any) -> None:
    services, _, _, transport = _production_services()
    tool = AliyunApi(services=services)

    result = await _production_execute(
        tool,
        {
            "product": "Ecs",
            "action": "EchoBody",
            "body": body,
            "region_id": "cn-hangzhou",
        },
    )

    assert result.is_error is False
    assert json.loads(transport.calls[-1]["request"].body) == body


@pytest.mark.asyncio
async def test_production_runtime_binary_body_file_and_json_xml_text_binary_responses(tmp_path: Path) -> None:
    services, _, _, transport = _production_services()
    transport.responses["GetXml"] = NormalizedApiResponse(
        200, MappingProxyType({}), "<Result>ok</Result>", "application/xml", None, 19
    )
    transport.responses["GetText"] = NormalizedApiResponse(
        200, MappingProxyType({}), "plain text", "text/plain", None, 10
    )
    tool = AliyunApi(services=services)
    body_file = tmp_path / "body.bin"
    body_file.write_bytes(b"binary-body")
    uploaded = await _production_execute(
        tool,
        {
            "product": "Ecs",
            "action": "PutBytes",
            "body_file": str(body_file),
            "region_id": "cn-hangzhou",
        },
        cwd=str(tmp_path),
    )
    assert uploaded.is_error is False
    assert transport.calls[-1]["request"].body == b"binary-body"

    for action, expected in (("GetXml", "<Result>ok</Result>"), ("GetText", "plain text")):
        result = await _production_execute(
            tool,
            {"product": "Ecs", "action": action, "region_id": "cn-hangzhou"},
        )
        assert result.content == expected

    binary = await _production_execute(
        tool,
        {"product": "Ecs", "action": "GetBinary", "region_id": "cn-hangzhou"},
    )
    binary_payload = json.loads(binary.content)
    assert binary_payload == {"encoding": "base64", "data": "ZGF0YQ=="}


@pytest.mark.asyncio
async def test_runtime_preserves_retry_exhaustion_reason_at_public_and_telemetry_boundaries() -> None:
    services, _, _, transport = _production_services()
    target_audit: list[dict[str, Any]] = []
    services.target_outcome_observer = target_audit.append
    transport.errors["DescribeInstances"] = RetryExhausted(
        outcome="retryable_status",
        reason=RetryReason.RETRYABLE_STATUS,
    )

    result = await _production_execute(
        AliyunApi(services=services),
        {"product": "Ecs", "action": "DescribeInstances", "region_id": "cn-hangzhou"},
    )

    assert result.is_error is True
    assert result.content == (
        "Alibaba Cloud API Ecs/DescribeInstances received a retryable service response, but the retry deadline "
        "expired. Retry the read-only request later."
    )
    assert target_audit[0]["outcome"] == "retryable_status"
    assert "RetryExhausted" not in result.content


@pytest.mark.asyncio
async def test_tool_executor_timeout_returns_unknown_cloud_state_after_target_cancel() -> None:
    services, _, _, transport = _production_services()
    target_audit: list[dict[str, Any]] = []
    services.target_outcome_observer = target_audit.append

    async def blocked_target(**kwargs: Any) -> NormalizedApiResponse:
        del kwargs
        await asyncio.Event().wait()
        raise AssertionError("unreachable")

    transport.execute = blocked_target  # type: ignore[method-assign]
    tool = AliyunApi(services=services)
    tool_input = tool.prepare_invocation_input(
        {
            "product": "Ecs",
            "action": "EchoBody",
            "region_id": "cn-hangzhou",
            "body": {"name": "business-value"},
        }
    )
    binding = InvocationBinding(
        runtime_nonce="runtime",
        session_id="session",
        tool_use_id="timeout-call",
        tool_name="aliyun_api",
        canonical_input_sha256=canonical_input_sha256(tool_input),
    )
    permission = await tool.check_permissions(
        tool_input,
        ToolPermissionContext(cwd="/tmp", invocation_binding=binding),
    )
    assert permission.snapshot_id is not None
    assert permission.security_digest is not None
    registry = ToolRegistry()
    registry.register(tool)

    result = (
        await ToolExecutor(registry, tool_timeout=0.01).execute_batch(
            [
                ToolCallRequest(
                    id="timeout-call",
                    name="aliyun_api",
                    input=tool_input,
                    invocation_binding=binding,
                    snapshot_id=permission.snapshot_id,
                    security_digest=permission.security_digest,
                    execution_class=permission.execution_class,
                )
            ],
            ToolContext(),
        )
    )[0]

    assert result == ToolResult.error(
        "Alibaba Cloud API Ecs/EchoBody timed out after the request may have been sent. "
        "Check cloud state before retrying to avoid duplicate changes."
    )
    assert len(target_audit) == 1
    assert target_audit[0]["outcome"] == "unknown_after_cancel"
    assert isinstance(target_audit[0]["duration_ms"], int)


@pytest.mark.asyncio
async def test_tool_executor_timeout_before_target_confirms_request_was_not_sent() -> None:
    services, _, _, transport = _production_services()
    tool = AliyunApi(services=services)
    tool_input = tool.prepare_invocation_input(
        {
            "product": "Ecs",
            "action": "EchoBody",
            "region_id": "cn-hangzhou",
            "body": {"name": "business-value"},
        }
    )
    binding = InvocationBinding(
        runtime_nonce="runtime",
        session_id="session",
        tool_use_id="pretarget-timeout-call",
        tool_name="aliyun_api",
        canonical_input_sha256=canonical_input_sha256(tool_input),
    )
    permission = await tool.check_permissions(
        tool_input,
        ToolPermissionContext(cwd="/tmp", invocation_binding=binding),
    )
    assert permission.snapshot_id is not None
    assert permission.security_digest is not None

    async def blocked_credential() -> AliyunCredential:
        await asyncio.Event().wait()
        raise AssertionError("unreachable")

    services.credential_provider = blocked_credential
    registry = ToolRegistry()
    registry.register(tool)
    result = (
        await ToolExecutor(registry, tool_timeout=0.01).execute_batch(
            [
                ToolCallRequest(
                    id="pretarget-timeout-call",
                    name="aliyun_api",
                    input=tool_input,
                    invocation_binding=binding,
                    snapshot_id=permission.snapshot_id,
                    security_digest=permission.security_digest,
                    execution_class=permission.execution_class,
                )
            ],
            ToolContext(),
        )
    )[0]

    assert result == ToolResult.error(
        "Alibaba Cloud API Ecs/EchoBody timed out before the request was sent. Retry the operation."
    )
    assert transport.calls == []


@pytest.mark.asyncio
async def test_runtime_maps_invalid_target_response_to_safe_public_and_telemetry_boundaries() -> None:
    services, _, _, transport = _production_services()
    target_audit: list[dict[str, Any]] = []
    services.target_outcome_observer = target_audit.append
    transport.errors["DescribeInstances"] = RuntimeError("invalid_response")

    result = await _production_execute(
        AliyunApi(services=services),
        {"product": "Ecs", "action": "DescribeInstances", "region_id": "cn-hangzhou"},
    )

    assert result == ToolResult.error(
        "Alibaba Cloud API Ecs/DescribeInstances returned an invalid response after the request was sent. "
        "Check cloud state before retrying to avoid duplicate changes."
    )
    assert target_audit[0]["outcome"] == "invalid_response"


def test_public_validation_keeps_safe_params_container_type_context() -> None:
    result = AliyunApi().validation_error_result(
        {
            "product": "Ecs",
            "action": "DescribeInstances",
            "region_id": "cn-hangzhou",
            "params": ["CUSTOMER_SECRET"],
        }
    )

    assert result is not None
    assert result.is_error is True
    assert result.content == (
        "Alibaba Cloud API Ecs/DescribeInstances parameter params expects object but received array."
    )
    assert "CUSTOMER_SECRET" not in result.content


@pytest.mark.parametrize(
    ("action", "force_catalog_stream"),
    [("GetBinary", False), ("GetObject", True)],
)
@pytest.mark.asyncio
async def test_binary_response_contract_executes_without_artifact_context(
    action: str,
    force_catalog_stream: bool,
) -> None:
    services, openmeta, endpoint_resolver, transport = _production_services()
    if force_catalog_stream:
        openmeta.documents[("oss", "2019-05-17", "getobject")]["produces"] = ["application/json"]
    stages: list[str] = []
    services.execution_stage_observer = stages.append
    tool_input: dict[str, Any] = {
        "product": "Oss" if action == "GetObject" else "Ecs",
        "action": action,
        "region_id": "cn-hangzhou",
    }
    if action == "GetObject":
        tool_input["params"] = {"bucket": "demo-bucket", "key": "object.bin"}

    result = await _production_execute(
        AliyunApi(services=services),
        tool_input,
        tool_use_id=None,
    )

    assert result.is_error is False
    assert stages.index("contract") < stages.index("credential") < stages.index("endpoint") < stages.index("target")
    assert endpoint_resolver.calls
    assert transport.calls


@pytest.mark.parametrize(
    ("action", "body", "content_type"),
    [
        ("GetText", "inline text", "text/plain"),
        ("GetXml", "<Result>inline</Result>", "application/xml"),
    ],
)
@pytest.mark.asyncio
async def test_inline_text_contract_executes_without_tool_use_id(
    action: str,
    body: str,
    content_type: str,
) -> None:
    services, _, endpoint_resolver, transport = _production_services()
    stages: list[str] = []
    services.execution_stage_observer = stages.append
    transport.responses[action] = NormalizedApiResponse(
        200,
        MappingProxyType({}),
        body,
        content_type,
        None,
        len(body.encode()),
    )

    result = await _production_execute(
        AliyunApi(services=services),
        {"product": "Ecs", "action": action, "region_id": "cn-hangzhou"},
        tool_use_id=None,
    )

    assert result.is_error is False
    assert result.content == body
    assert stages.index("contract") < stages.index("credential") < stages.index("endpoint") < stages.index("target")
    assert endpoint_resolver.calls
    assert transport.calls


@pytest.mark.asyncio
async def test_production_runtime_oss_get_put_metadata_head_and_unsupported_catalog(tmp_path: Path) -> None:
    services, _, _, transport = _production_services()
    transport.responses["GetObject"] = NormalizedApiResponse(
        200,
        MappingProxyType({"content-type": "application/octet-stream"}),
        {"encoding": "base64", "data": "b2JqZWN0"},
        "application/octet-stream",
        None,
        6,
    )
    tool = AliyunApi(services=services)
    common = {"bucket": "demo-bucket", "key": "dir/object.txt"}
    body_file = tmp_path / "object.bin"
    body_file.write_bytes(b"object")
    for action in ("GetObject", "GetObjectMeta", "HeadObject"):
        result = await _production_execute(
            tool,
            {
                "product": "Oss",
                "action": action,
                "params": common,
                "region_id": "cn-hangzhou",
            },
        )
        assert result.is_error is False
        call = transport.calls[-1]
        assert call["contract"].transport == "oss_v4_sdk"
        assert call["endpoint"].endpoint == "oss-cn-hangzhou.aliyuncs.com"
        assert call["endpoint"].expected_host == "demo-bucket.oss-cn-hangzhou.aliyuncs.com"

    put = await _production_execute(
        tool,
        {
            "product": "Oss",
            "action": "PutObject",
            "params": {**common, "x-oss-meta-*": {"owner": "iac"}},
            "body_file": str(body_file),
            "content_type": "application/custom",
            "region_id": "cn-hangzhou",
        },
        cwd=str(tmp_path),
    )
    put_request = transport.calls[-1]["request"]
    assert put.is_error is False
    assert put_request.body == b"object"
    assert put_request.headers["content-type"] == "application/custom"
    assert put_request.headers["x-oss-meta-owner"] == "iac"
    before = len(transport.calls)
    unsupported_input = {
        "product": "Oss",
        "action": "CompleteMultipartUpload",
        "params": common,
        "region_id": "cn-hangzhou",
    }
    unsupported_binding = InvocationBinding(
        runtime_nonce="runtime",
        session_id="session",
        tool_use_id="unsupported",
        tool_name="aliyun_api",
        canonical_input_sha256=canonical_input_sha256(unsupported_input),
    )
    unsupported = await tool.check_permissions(
        unsupported_input,
        ToolPermissionContext(invocation_binding=unsupported_binding),
    )
    assert unsupported.behavior == "deny"
    assert unsupported.message == (
        "Alibaba Cloud OSS API Oss/CompleteMultipartUpload is not supported by this runtime. "
        "Choose a supported OSS action or use another client."
    )
    assert "field_mapping_missing" not in unsupported.message
    assert len(transport.calls) == before


@pytest.mark.asyncio
async def test_production_runtime_headers_only_limit_returns_stable_public_error() -> None:
    services, _, _, transport = _production_services()
    secret_value = "must-not-reach-public-result"
    transport.responses["HeadObject"] = NormalizedApiResponse(
        200,
        MappingProxyType({f"x-response-{index}": secret_value for index in range(65)}),
        None,
        None,
        None,
        0,
    )

    result = await _production_execute(
        AliyunApi(services=services),
        {
            "product": "Oss",
            "action": "HeadObject",
            "params": {"bucket": "demo-bucket", "key": "dir/object.txt"},
            "region_id": "cn-hangzhou",
        },
    )

    assert result.is_error is True
    assert result.content == public_aliyun_error(
        "aliyun_response_headers_too_large",
        product="Oss",
        action="HeadObject",
        region_id="cn-hangzhou",
    )
    assert "Verify the cloud resource state before retrying." in result.content
    assert "aliyun_response_headers_too_large" not in result.content
    assert secret_value not in result.content


@pytest.mark.asyncio
async def test_production_runtime_explicit_fallback_endpoint_host_failure_and_single_retry_budget() -> None:
    services, openmeta, endpoint_resolver, transport = _production_services()
    tool = AliyunApi(services=services)
    openmeta.temporarily_unavailable.add(("ecs", "fallbackaction"))

    fallback = await _production_execute(
        tool,
        {
            "product": "Ecs",
            "version": "2014-05-26",
            "action": "FallbackAction",
            "region_id": "cn-hangzhou",
        },
    )
    assert fallback.is_error is False
    assert transport.calls[-1]["contract"].metadata_source == "explicit_fallback"
    assert transport.calls[-1]["budget"] is services.budgets[-1]
    assert len(services.budgets) == 1

    endpoint_resolver.failure = RuntimeError("endpoint_unavailable")
    before = len(transport.calls)
    failed_endpoint = await _production_execute(
        tool,
        {"product": "Ecs", "action": "DescribeInstances", "params": {}, "region_id": "cn-hangzhou"},
    )
    assert failed_endpoint.is_error is True
    assert failed_endpoint.content == (
        "No trusted Alibaba Cloud endpoint is available for Ecs/DescribeInstances in cn-hangzhou. "
        "Check the region or endpoint configuration."
    )
    assert len(transport.calls) == before

    endpoint_resolver.failure = None
    endpoint_resolver.omit_host_template = True
    failed_host = await _production_execute(
        tool,
        {
            "product": "Oss",
            "action": "GetObjectMeta",
            "params": {"bucket": "demo-bucket", "key": "object"},
            "region_id": "cn-hangzhou",
        },
    )
    assert failed_host.is_error is True
    assert failed_host.content == "Alibaba Cloud host parameters are invalid for Oss/GetObjectMeta in cn-hangzhou."
    assert len(transport.calls) == before


@pytest.mark.asyncio
async def test_production_runtime_transport_unknown_outcome_keeps_approved_contract_snapshot() -> None:
    services, _, _, transport = _production_services()
    transport.errors["EchoBody"] = RuntimeError("unknown_after_transport_error")
    tool = AliyunApi(services=services)
    tool_input = {
        "product": "Ecs",
        "action": "EchoBody",
        "body": {"name": "business-value"},
        "region_id": "cn-hangzhou",
    }

    result = await _production_execute(tool, tool_input)

    assert result.is_error is True
    assert result.content == (
        "Alibaba Cloud API Ecs/EchoBody may have been sent before the connection failed. "
        "Check cloud state before retrying to avoid duplicate changes."
    )
    assert transport.calls[-1]["contract"].action == "EchoBody"
    assert services.contract_store.size == 0
    assert transport.calls[-1]["budget"] is services.budgets[-1]
    assert len(services.budgets) == 1


class TestAliyunApiDoesNotBlockEventLoop:
    """The blocking OpenAPI network call must run off the event loop (asyncio.to_thread)
    so it never starves web agent turns, SSE streams, and HTTP handlers.
    """

    @pytest.mark.asyncio
    async def test_call_api_does_not_starve_loop(self, api: AliyunApi, context: ToolContext, mock_credentials) -> None:
        import threading

        entered = threading.Event()
        release = threading.Event()

        def blocking_call_api(*args, **kwargs):
            entered.set()
            release.wait(5)
            return {"body": {"Instances": []}}

        mock_client = MagicMock()
        mock_client.call_api.side_effect = blocking_call_api

        with patch("iac_code.tools.cloud.aliyun.aliyun_api.OpenApiClient", return_value=mock_client):
            task = asyncio.create_task(
                api.execute(
                    tool_input={
                        "product": "ecs",
                        "action": "DescribeInstances",
                        "region_id": "cn-hangzhou",
                    },
                    context=context,
                )
            )

            # Worker thread entered the blocking call while the loop stayed free.
            await asyncio.wait_for(asyncio.to_thread(entered.wait, 1), timeout=2)
            assert not task.done()
            for _ in range(5):
                await asyncio.sleep(0)
            assert not task.done()

            release.set()
            result = await asyncio.wait_for(task, timeout=2)

        assert result.is_error is False
        data = json.loads(result.content)
        assert data == {"Instances": []}
