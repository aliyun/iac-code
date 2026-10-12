"""Run the bundled Terraform schema examples through the offline Aliyun runtime.

The API fixture is the public OpenMeta document retrieved on 2026-10-08 from
https://api.aliyun.com/meta/v1/products/IaCService/versions/2021-08-06/apis/GetResourceType/api.json?language=ZH_CN.
Only metadata HTTP is mocked; contract resolution, permission checks and wire encoding are real.
"""

from __future__ import annotations

import ast
import json
import re
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import httpx
import pytest

from iac_code.services.permissions.pipeline import check_tool_permission
from iac_code.tools.base import ToolContext
from iac_code.tools.cloud.aliyun.acs3_transport import NormalizedApiResponse
from iac_code.tools.cloud.aliyun.aliyun_api import AliyunApi, _runtime_call_shape, _runtime_is_read_only
from iac_code.tools.cloud.aliyun.api_contract import ApiContractResolver, RequestBuilder
from iac_code.tools.cloud.aliyun.contract_store import ResolvedContractStore, canonical_input_sha256
from iac_code.tools.cloud.aliyun.endpoint_resolver import EndpointResolution
from iac_code.tools.cloud.aliyun.openmeta import OpenMetaClient
from iac_code.types.permissions import InvocationBinding, PermissionResult, ToolPermissionContext

SKILL_ROOT = Path(__file__).resolve().parents[4] / "src/iac_code/skills/bundled/iac_aliyun"
SKILL_EXAMPLES = ("SKILL.md", "references/cloud-products/ga.md")
API_PATH = "/meta/v1/products/IaCService/versions/2021-08-06/apis/GetResourceType/api.json"
DOCS_PATH = "/meta/v1/products/IaCService/versions/2021-08-06/api-docs.json"


class OfflineApiTransport:
    def __init__(self) -> None:
        self.requests = []

    def prepare(self, **kwargs: Any) -> OfflineApiTransport:
        self.requests.append(kwargs["request"])
        return self

    async def execute(self, *, budget: Any) -> NormalizedApiResponse:
        return NormalizedApiResponse(200, {}, {"resourceType": "alicloud_vpc"}, "application/json", None, 30)


class OfflineEndpointResolver:
    async def resolve(self, *args: Any, **kwargs: Any) -> EndpointResolution:
        return EndpointResolution("iac.aliyuncs.com", "catalog_global", None)

    def bind(self, contract: Any, endpoint: str, host_template: Any, host_values: Any) -> str:
        return endpoint


class IacServiceSkillRuntime:
    def __init__(self, cache_dir: Path) -> None:
        self.cwd = str(cache_dir.parent)
        fixture = Path(__file__).parent / "fixtures/openmeta/iacservice_get_resource_type.json"
        self.metadata = json.loads(fixture.read_text(encoding="utf-8"))
        self.apis = {"GetResourceType": self.metadata}
        for action, name in (
            ("ListProducts", "iacservice_list_products.json"),
            ("ListResourceTypes", "iacservice_list_resource_types.json"),
            ("GetModuleVersion", "iacservice_get_module_version.json"),
        ):
            self.apis[action] = json.loads((fixture.parent / name).read_text(encoding="utf-8"))
        self.openmeta = OpenMetaClient(cache_dir=cache_dir, transport=httpx.MockTransport(self.metadata_response))
        self.resolver = ApiContractResolver(self.openmeta)
        self.builder = RequestBuilder()
        self.clock_value = 100.0
        self.services = SimpleNamespace(
            contract_resolver=self.resolver,
            contract_store=ResolvedContractStore(ttl_seconds=1.0, clock=self.clock),
            request_builder=self.builder,
            endpoint_resolver=OfflineEndpointResolver(),
            host_binding_resolver=OfflineEndpointResolver(),
            transport_router=OfflineApiTransport(),
        )
        self.tool = AliyunApi(services=self.services)

    def clock(self) -> float:
        return self.clock_value

    def metadata_response(self, request: httpx.Request) -> httpx.Response:
        if request.url.path == "/meta/v1/products.json":
            return httpx.Response(
                200,
                json={
                    "products": [
                        {
                            "product": "IaCService",
                            "defaultVersion": "2021-08-06",
                            "versions": ["2021-08-06"],
                            "style": "RPC",
                        }
                    ]
                },
            )
        for action, metadata in self.apis.items():
            if request.url.path == API_PATH.replace("GetResourceType", action):
                return httpx.Response(200, json=metadata)
        if request.url.path == DOCS_PATH:
            return httpx.Response(
                200,
                json={
                    "info": {"style": "ROA", "product": "IaCService", "version": "2021-08-06"},
                    "apis": self.apis,
                },
            )
        raise AssertionError("Unexpected metadata request: {}".format(request.url.path))

    @staticmethod
    def skill_input(relative_path: str, resource_type: str) -> dict[str, Any]:
        content = (SKILL_ROOT / relative_path).read_text(encoding="utf-8")
        examples = re.findall(r'aliyun_api\(product="IaCService"[^\n)]*\)', content)
        assert len(examples) == 1
        expression = ast.parse(examples[0], mode="eval").body
        assert isinstance(expression, ast.Call)
        for node in ast.walk(expression):
            if isinstance(node, ast.Constant) and isinstance(node.value, str):
                node.value = node.value.replace("<类型>", resource_type)
        tool_input = {keyword.arg: ast.literal_eval(keyword.value) for keyword in expression.keywords}
        return {**tool_input, "region_id": "cn-hangzhou"}

    def permission_context(self, tool_input: dict[str, Any]) -> ToolPermissionContext:
        return ToolPermissionContext(
            cwd=self.cwd,
            invocation_binding=InvocationBinding(
                "runtime", "session", "call", "aliyun_api", canonical_input_sha256(tool_input)
            ),
        )

    def execution_context(self, permission: PermissionResult) -> ToolContext:
        return ToolContext(
            cwd=self.cwd,
            tool_use_id="call",
            invocation_binding=permission.invocation_binding,
            snapshot_id=permission.snapshot_id,
            security_digest=permission.security_digest,
            execution_class=permission.execution_class,
        )

    @staticmethod
    def information_input(**changes: Any) -> dict[str, Any]:
        return {
            "product": "IaCService",
            "version": "2021-08-06",
            "action": "GetResourceType",
            "style": "ROA",
            "method": "GET",
            "pathname": "/resourceType/alicloud_vpc",
            "region_id": "cn-hangzhou",
            **changes,
        }


@pytest.mark.parametrize("relative_path", SKILL_EXAMPLES)
@pytest.mark.parametrize(
    ("resource_type", "expected_path"),
    [
        ("alicloud_vpc", b"/resourceType/alicloud_vpc"),
        ("alicloud_vpc/测试 %", b"/resourceType/alicloud_vpc%2F%E6%B5%8B%E8%AF%95%20%25"),
    ],
)
@pytest.mark.asyncio
async def test_bundled_terraform_schema_example_is_automatic_read(
    tmp_path: Path, relative_path: str, resource_type: str, expected_path: bytes
) -> None:
    runtime = IacServiceSkillRuntime(tmp_path / "metadata")
    try:
        tool_input = runtime.skill_input(relative_path, resource_type)
        result = await check_tool_permission(runtime.tool, tool_input, runtime.permission_context(tool_input))

        assert result.behavior == "allow"
        assert result.execution_class == "concurrent"
        assert result.audit is not None
        assert result.audit.is_read_only is True
        assert result.audit.scope == "read_only"
        assert tool_input["version"] == "2021-08-06"
        contract = await runtime.resolver.resolve(_runtime_call_shape(tool_input), allow_fallback=False)
        request = await runtime.builder.build(contract, tool_input)
        assert contract.style == "ROA"
        assert contract.operation_type == "read"
        assert contract.auth_type == "Anonymous"
        assert request.raw_path == expected_path
        assert request.method == "GET"
        assert request.body is None
    finally:
        await runtime.openmeta.aclose()


@pytest.mark.parametrize(
    ("pathname", "expected"),
    [("/resourceType/alicloud_vpc", "allow"), ("/other/alicloud_vpc", "ask")],
)
@pytest.mark.asyncio
async def test_explicit_schema_path_uses_the_official_route(tmp_path: Path, pathname: str, expected: str) -> None:
    runtime = IacServiceSkillRuntime(tmp_path / "metadata")
    try:
        tool_input = {
            "product": "IaCService",
            "action": "GetResourceType",
            "style": "ROA",
            "method": "GET",
            "pathname": pathname,
            "region_id": "cn-hangzhou",
        }
        result = await check_tool_permission(runtime.tool, tool_input, runtime.permission_context(tool_input))

        assert result.behavior == expected
        assert result.execution_class == ("concurrent" if expected == "allow" else "serial")
        assert result.reason is not None
        assert result.reason.type == ("read_only" if expected == "allow" else "untrusted_write")
        assert result.audit is not None
        assert result.audit.is_read_only is (expected == "allow")
        contract = await runtime.resolver.resolve(_runtime_call_shape(tool_input), allow_fallback=False)
        request = await runtime.builder.build(contract, tool_input)
        assert contract.operation_type == "read"
        assert request.raw_path == pathname.encode("ascii")
    finally:
        await runtime.openmeta.aclose()


@pytest.mark.parametrize("recover", [False, True], ids=["snapshot", "expired-recovery"])
@pytest.mark.parametrize(
    ("action", "pathname"),
    [
        ("GetResourceType", "/resourceType/alicloud_vpc"),
        ("GetResourceType", "/resourceType/alicloud_vpc%2F%E6%B5%8B%E8%AF%95%20%25"),
        ("ListResourceTypes", "/resourceTypes"),
        ("ListProducts", "/products"),
    ],
)
@pytest.mark.asyncio
async def test_information_get_executes_once_with_the_same_readonly_class(
    tmp_path: Path, recover: bool, action: str, pathname: str
) -> None:
    runtime = IacServiceSkillRuntime(tmp_path / "metadata")
    try:
        tool_input = {
            "product": "IaCService",
            "version": "2021-08-06",
            "action": action,
            "style": "ROA",
            "method": "GET",
            "pathname": pathname,
            "region_id": "cn-hangzhou",
        }
        permission = await check_tool_permission(runtime.tool, tool_input, runtime.permission_context(tool_input))
        assert permission.behavior == "allow"
        assert permission.execution_class == "concurrent"
        assert permission.audit is not None
        assert permission.audit.is_read_only is True
        context = runtime.execution_context(permission)
        if recover:
            runtime.clock_value += 2.0
        result = await runtime.tool.execute(tool_input=tool_input, context=context)
        assert result.is_error is False, result.content
        assert json.loads(result.content) == {"resourceType": "alicloud_vpc"}
        assert runtime.services.transport_router.requests[0].raw_path == pathname.encode("ascii")
        replay = await runtime.tool.execute(tool_input=tool_input, context=context)
        assert replay.is_error is True
        assert len(runtime.services.transport_router.requests) == 1
    finally:
        await runtime.openmeta.aclose()


@pytest.mark.parametrize(
    ("changes", "expected"),
    [
        ({"params": {"resourceType": "alicloud_vpc"}}, "allow"),
        (
            {
                "pathname": "/resourceType/alicloud_vpc%2F%E6%B5%8B%E8%AF%95%20%25",
                "params": {"resourceType": "alicloud_vpc/测试 %"},
            },
            "allow",
        ),
        ({"params": {"resourceType": "alicloud_ecs_instance"}}, "ask"),
        ({"params": {"resourceType": True}}, "ask"),
        ({"pathname": "/products"}, "ask"),
        ({"action": "ListProducts", "pathname": "/resourceTypes"}, "ask"),
        ({"pathname": "/resourceType/a/extra"}, "ask"),
        ({"pathname": "/resourceType/"}, "ask"),
        ({"pathname": "/resourceType/.."}, "ask"),
        ({"pathname": "/resourceType/%2E%2E"}, "ask"),
        ({"pathname": "/resourceType/a%2F..%2Fb"}, "ask"),
        ({"pathname": "/resourceType/%00"}, "ask"),
        ({"pathname": "/resourceType/a%5Cb"}, "ask"),
        ({"pathname": "/resourceType/%FF"}, "ask"),
        ({"pathname": "/resourceType/%zz"}, "ask"),
        ({"pathname": "/resourceType/alicloud_vpc?other=route"}, "ask"),
        ({"pathname": "/resourceType/alicloud_vpc#other"}, "ask"),
        ({"method": "POST"}, "ask"),
        ({"method": "DELETE"}, "ask"),
        ({"style": "RPC"}, "ask"),
        ({"body": {}}, "ask"),
    ],
)
@pytest.mark.asyncio
async def test_information_route_compatibility_preserves_request_boundaries(
    tmp_path: Path, changes: dict[str, Any], expected: str
) -> None:
    runtime = IacServiceSkillRuntime(tmp_path / "metadata")
    try:
        tool_input = runtime.information_input(**changes)
        permission = await check_tool_permission(runtime.tool, tool_input, runtime.permission_context(tool_input))
        assert permission.behavior == expected
        assert permission.execution_class == ("concurrent" if expected == "allow" else "serial")
        assert permission.audit is not None
        assert permission.audit.is_read_only is (expected == "allow")
    finally:
        await runtime.openmeta.aclose()


@pytest.mark.parametrize("recover", [False, True])
@pytest.mark.asyncio
async def test_information_read_rejects_execution_class_mutation(tmp_path: Path, recover: bool) -> None:
    runtime = IacServiceSkillRuntime(tmp_path / "metadata")
    try:
        tool_input = runtime.information_input()
        permission = await check_tool_permission(runtime.tool, tool_input, runtime.permission_context(tool_input))
        assert permission.behavior == "allow"
        context = runtime.execution_context(permission)
        context.execution_class = "serial"
        if recover:
            runtime.clock_value += 2.0
        result = await runtime.tool.execute(tool_input=tool_input, context=context)
        assert result.is_error is True
        assert runtime.services.transport_router.requests == []
    finally:
        await runtime.openmeta.aclose()


@pytest.mark.parametrize("operation_type", ["write", "readAndWrite", None])
@pytest.mark.asyncio
async def test_information_name_and_get_do_not_override_metadata_operation_type(
    tmp_path: Path, operation_type: str | None
) -> None:
    runtime = IacServiceSkillRuntime(tmp_path / "metadata")
    runtime.metadata["operationType"] = operation_type
    try:
        tool_input = runtime.information_input()
        permission = await check_tool_permission(runtime.tool, tool_input, runtime.permission_context(tool_input))
        assert permission.behavior == "ask"
        assert permission.execution_class == "serial"
        assert permission.audit is not None
        assert permission.audit.is_read_only is False
    finally:
        await runtime.openmeta.aclose()


@pytest.mark.parametrize("metadata_source", ["fresh", "cache", "stale_cache", "explicit_fallback"])
@pytest.mark.parametrize(
    ("product", "version"),
    [("IaCService", "2021-08-06"), ("IaCService", "2021-07-22"), ("FC", "2023-03-30")],
)
@pytest.mark.asyncio
async def test_concrete_route_compatibility_uses_metadata_across_products_and_versions(
    tmp_path: Path, metadata_source: str, product: str, version: str
) -> None:
    runtime = IacServiceSkillRuntime(tmp_path / "metadata")
    try:
        tool_input = runtime.information_input()
        metadata = await runtime.resolver.resolve(
            _runtime_call_shape({name: value for name, value in tool_input.items() if name != "pathname"}),
            allow_fallback=False,
        )
        metadata = replace(metadata, metadata_source=metadata_source, product=product, version=version)
        tool_input.update(product=product, version=version)
        contract = replace(metadata, pathname=tool_input["pathname"])
        assert _runtime_is_read_only(
            contract, _runtime_call_shape(tool_input, contract=contract), metadata, tool_input=tool_input
        ) is (metadata_source != "explicit_fallback")
    finally:
        await runtime.openmeta.aclose()


@pytest.mark.parametrize("params", [{}, {"moduleId": "m-123", "moduleVersion": "v-1"}])
@pytest.mark.asyncio
async def test_other_information_actions_match_all_official_path_parameters(tmp_path: Path, params: dict) -> None:
    runtime = IacServiceSkillRuntime(tmp_path / "metadata")
    try:
        tool_input = runtime.information_input(
            action="GetModuleVersion", pathname="/modules/m-123/versions/v-1", params=params
        )
        permission = await check_tool_permission(runtime.tool, tool_input, runtime.permission_context(tool_input))
        assert permission.behavior == "allow"
        assert permission.execution_class == "concurrent"
        contract = await runtime.resolver.resolve(_runtime_call_shape(tool_input), allow_fallback=False)
        request = await runtime.builder.build(contract, tool_input)
        assert request.raw_path == b"/modules/m-123/versions/v-1"
    finally:
        await runtime.openmeta.aclose()


@pytest.mark.parametrize("rule", ["aliyun_api", "aliyun_api(IaCService:GetResourceType)"])
@pytest.mark.parametrize(("behavior", "expected"), [("deny", "deny"), ("ask", "allow")])
@pytest.mark.asyncio
async def test_information_read_honors_deny_and_ignores_ask_rules(
    tmp_path: Path, rule: str, behavior: str, expected: str
) -> None:
    runtime = IacServiceSkillRuntime(tmp_path / "metadata")
    try:
        tool_input = runtime.information_input()
        context = runtime.permission_context(tool_input)
        setattr(context, behavior + "_rules", {"user_settings": [rule]})
        permission = await check_tool_permission(runtime.tool, tool_input, context)
        assert permission.behavior == expected
        assert runtime.services.transport_router.requests == []
    finally:
        await runtime.openmeta.aclose()


@pytest.mark.asyncio
async def test_information_read_does_not_bypass_body_file_or_invocation_binding(tmp_path: Path) -> None:
    project = tmp_path / "project"
    project.mkdir()
    body_file = tmp_path / "outside.json"
    body_file.write_text("{}", encoding="utf-8")
    runtime = IacServiceSkillRuntime(project / "metadata")
    try:
        tool_input = runtime.information_input(body_file=str(body_file))
        context = runtime.permission_context(tool_input)
        context.strict_read_directories = [str(project)]
        context.read_path_violation_behavior = "deny"
        permission = await check_tool_permission(runtime.tool, tool_input, context)
        assert permission.behavior == "deny"
        assert permission.reason is not None
        assert permission.reason.type == "path_constraint"

        tool_input = runtime.information_input()
        context = runtime.permission_context(tool_input)
        context.invocation_binding = replace(context.invocation_binding, canonical_input_sha256="0" * 64)
        permission = await check_tool_permission(runtime.tool, tool_input, context)
        assert permission.behavior == "deny"
        assert runtime.services.contract_store.size == 0
    finally:
        await runtime.openmeta.aclose()
