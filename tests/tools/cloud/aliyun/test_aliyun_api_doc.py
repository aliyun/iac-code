"""Exact anonymous document views for metadata-driven Alibaba Cloud APIs."""

from __future__ import annotations

import json
from dataclasses import fields
from types import SimpleNamespace
from typing import Any, Literal

import pytest

from iac_code.tools.base import ToolContext, ToolRegistry, ToolResult
from iac_code.tools.cloud.aliyun.aliyun_api import AliyunApi
from iac_code.tools.cloud.aliyun.aliyun_api_doc import AliyunApiDoc
from iac_code.tools.cloud.aliyun.api_contract import (
    ApiCallShape,
    ApiContractError,
    ApiContractResolver,
    CanonicalWireContract,
    RequestBuilder,
)
from iac_code.tools.cloud.aliyun.contract_store import ResolvedContractStore, canonical_input_sha256
from iac_code.tools.cloud.aliyun.openmeta import (
    ApiMetadata,
    MetadataFetch,
    ProductMetadata,
    normalize_api_metadata,
)
from iac_code.tools.tool_executor import ToolCallRequest, ToolExecutor
from iac_code.types.permissions import InvocationBinding, ToolPermissionContext


class FakeOpenMeta:
    def __init__(
        self,
        metadata: Any | None,
        *,
        product: ProductMetadata | None = None,
        product_error: str | None = None,
        api_error: str | None = None,
        api_source: Literal["fresh", "cache", "stale_cache"] = "fresh",
    ) -> None:
        self.metadata = metadata
        self.product = product or ProductMetadata(
            product=str(getattr(metadata, "product", "Ecs")),
            default_version=str(getattr(metadata, "version", "2014-05-26")),
            versions=(str(getattr(metadata, "version", "2014-05-26")),),
            documentation_url=None,
        )
        self.product_error = product_error
        self.api_error = api_error
        self.api_source = api_source
        self.calls: list[tuple[str, ...]] = []

    async def get_product(self, product: str) -> MetadataFetch[Any]:
        self.calls.append(("product", product))
        value = None if self.product_error else self.product
        return MetadataFetch(
            value=value,
            source="fresh" if value is not None else None,
            error=self.product_error,  # type: ignore[arg-type]
            cache_status="memory_fresh" if value is not None else "miss",
        )

    async def get_api(self, product: str, version: str, action: str) -> MetadataFetch[Any]:
        self.calls.append(("api", product, version, action))
        value = None if self.api_error else self.metadata
        return MetadataFetch(
            value=value,
            source=self.api_source if value is not None else None,
            error=self.api_error,  # type: ignore[arg-type]
            cache_status="memory_fresh" if value is not None else "miss",
        )


class FakeContractResolver:
    def __init__(self, contract: CanonicalWireContract) -> None:
        self.contract = contract
        self.calls: list[tuple[Any, bool]] = []

    async def resolve(self, call: Any, *, allow_fallback: bool) -> CanonicalWireContract:
        self.calls.append((call, allow_fallback))
        return self.contract


class RaisingContractResolver:
    def __init__(self, code: str, *, product: str | None = None) -> None:
        self.code = code
        self.product = product
        self.calls: list[tuple[Any, bool]] = []

    async def resolve(self, call: Any, *, allow_fallback: bool) -> CanonicalWireContract:
        self.calls.append((call, allow_fallback))
        raise ApiContractError(self.code, product=self.product)


def raw_api(**changes: Any) -> dict[str, Any]:
    raw: dict[str, Any] = {
        "product": "Ecs",
        "version": "2014-05-26",
        "action": "DescribeInstances",
        "style": "RPC",
        "methods": ["POST"],
        "path": "/",
        "schemes": ["HTTPS"],
        "consumes": ["application/json"],
        "produces": ["application/json"],
        "operationType": "read",
        "security": [{"AK": []}],
        "parameters": [
            {
                "name": "RegionId",
                "in": "query",
                "required": True,
                "description": "Region",
                "example": "cn-hangzhou",
                "schema": {"type": "string"},
            },
            {
                "name": "InstanceIds",
                "in": "query",
                "schema": {"type": "string"},
            },
        ],
        "responses": {},
        "components": {"schemas": {}},
    }
    raw.update(changes)
    return raw


def document_metadata(raw: dict[str, Any], **extras: Any) -> SimpleNamespace:
    metadata = normalize_api_metadata(raw)
    values = {field.name: getattr(metadata, field.name) for field in fields(ApiMetadata)}
    values.update(
        {
            "summary": extras.pop("summary", None),
            "deprecated": extras.pop("deprecated", False),
            "error_codes": extras.pop("error_codes", {}),
            "change_set": extras.pop("change_set", ()),
            "static_info": extras.pop("static_info", {}),
        }
    )
    values.update(extras)
    return SimpleNamespace(**values)


def contract_for(metadata: Any, **changes: Any) -> CanonicalWireContract:
    values: dict[str, Any] = {
        "metadata_source": "fresh",
        "product": metadata.product,
        "version": metadata.version,
        "action": metadata.action,
        "style": metadata.style,
        "method": metadata.method,
        "pathname": metadata.pathname,
        "operation_type": metadata.operation_type,
        "auth_type": "AK",
        "signature_scheme": "acs3",
        "transport": "tea",
        "executable": True,
        "unsupported_reasons": (),
        "parameters": metadata.parameters,
        "consumes": metadata.consumes,
        "produces": metadata.produces,
        "policy_digest": "fixture-policy",
        "request_body_type": metadata.request_body_type,
        "response_body_type": metadata.response_body_type,
        "security_declared": metadata.security_declared,
        "security_requirements": metadata.security_requirements,
        "openmeta_cache_status": "memory_fresh",
    }
    values.update(changes)
    return CanonicalWireContract(**values)


def tool_for(metadata: Any, **contract_changes: Any) -> tuple[AliyunApiDoc, FakeOpenMeta, FakeContractResolver]:
    openmeta = FakeOpenMeta(metadata)
    resolver = FakeContractResolver(contract_for(metadata, **contract_changes))
    services = SimpleNamespace(openmeta=openmeta, contract_resolver=resolver)
    return AliyunApiDoc(services), openmeta, resolver


def compact(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def summary_snapshot(
    *,
    product: str,
    version: str,
    action: str,
    summary: str | None,
    style: str,
    method: str,
    path: str,
    operation_type: str | None,
    executable: bool,
    unsupported_reasons: list[str],
    required_parameters: list[dict[str, Any]],
    optional_parameters: list[str],
) -> dict[str, Any]:
    return {
        "product": product,
        "version": version,
        "action": action,
        "summary": summary,
        "style": style,
        "method": method,
        "path": path,
        "operation_type": operation_type,
        "executable": executable,
        "unsupported_reasons": unsupported_reasons,
        "documentation_url": "https://api.aliyun.com/api/{}/{}/{}".format(product, version, action),
        "required_parameters": required_parameters,
        "optional_parameters": optional_parameters,
    }


def rpc_case() -> tuple[Any, dict[str, Any], dict[str, Any]]:
    metadata = document_metadata(raw_api(), summary="List ECS instances")
    expected = summary_snapshot(
        product="Ecs",
        version="2014-05-26",
        action="DescribeInstances",
        summary="List ECS instances",
        style="RPC",
        method="POST",
        path="/",
        operation_type="read",
        executable=True,
        unsupported_reasons=[],
        required_parameters=[{"name": "RegionId", "in": "query", "type": "string", "example": "cn-hangzhou"}],
        optional_parameters=["InstanceIds"],
    )
    return metadata, {}, expected


def roa_case() -> tuple[Any, dict[str, Any], dict[str, Any]]:
    raw = raw_api(
        product="FC",
        version="2023-03-30",
        action="GetFunction",
        style="ROA",
        methods=["GET"],
        path="/2023-03-30/functions/{functionName}",
        parameters=[
            {
                "name": "functionName",
                "in": "path",
                "required": True,
                "pathEncoding": "segment",
                "example": "demo",
                "schema": {"type": "string", "format": "name"},
            },
            {"name": "qualifier", "in": "query", "schema": {"type": "string"}},
        ],
    )
    metadata = document_metadata(raw, summary="Get one function")
    expected = summary_snapshot(
        product="FC",
        version="2023-03-30",
        action="GetFunction",
        summary="Get one function",
        style="ROA",
        method="GET",
        path="/2023-03-30/functions/{functionName}",
        operation_type="read",
        executable=True,
        unsupported_reasons=[],
        required_parameters=[
            {
                "name": "functionName",
                "in": "path",
                "type": "string",
                "path_encoding": "segment",
                "format": "name",
                "example": "demo",
            }
        ],
        optional_parameters=["qualifier"],
    )
    return metadata, {}, expected


def formdata_case() -> tuple[Any, dict[str, Any], dict[str, Any]]:
    raw = raw_api(
        product="ROS",
        version="2019-09-10",
        action="ValidateTemplate",
        consumes=["application/x-www-form-urlencoded"],
        parameters=[
            {
                "name": "TemplateBody",
                "in": "formData",
                "required": True,
                "schema": {"type": "string"},
            },
            {"name": "RegionId", "in": "query", "schema": {"type": "string"}},
        ],
    )
    metadata = document_metadata(raw, summary="Validate a ROS template")
    expected = summary_snapshot(
        product="ROS",
        version="2019-09-10",
        action="ValidateTemplate",
        summary="Validate a ROS template",
        style="RPC",
        method="POST",
        path="/",
        operation_type="read",
        executable=True,
        unsupported_reasons=[],
        required_parameters=[{"name": "TemplateBody", "in": "formData", "type": "string"}],
        optional_parameters=["RegionId"],
    )
    return metadata, {}, expected


def oss_supported_case() -> tuple[Any, dict[str, Any], dict[str, Any]]:
    raw = raw_api(
        product="Oss",
        version="2019-05-17",
        action="GetObject",
        style="ROA",
        methods=["GET"],
        path="/{key}",
        consumes=[],
        produces=["application/octet-stream"],
        parameters=[
            {"name": "bucket", "in": "host", "required": True, "schema": {"type": "string"}},
            {
                "name": "key",
                "in": "path",
                "required": True,
                "pathEncoding": "preserve_slashes",
                "schema": {"type": "string"},
            },
        ],
    )
    metadata = document_metadata(raw, summary="Download an object")
    changes = {"signature_scheme": "oss_v4", "transport": "oss_v4_sdk"}
    expected = summary_snapshot(
        product="Oss",
        version="2019-05-17",
        action="GetObject",
        summary="Download an object",
        style="ROA",
        method="GET",
        path="/{key}",
        operation_type="read",
        executable=True,
        unsupported_reasons=[],
        required_parameters=[
            {"name": "bucket", "in": "host", "type": "string"},
            {
                "name": "key",
                "in": "path",
                "type": "string",
                "path_encoding": "preserve_slashes",
            },
        ],
        optional_parameters=[],
    )
    return metadata, changes, expected


def oss_unsupported_case() -> tuple[Any, dict[str, Any], dict[str, Any]]:
    raw = raw_api(
        product="Oss",
        version="2019-05-17",
        action="SelectObject",
        style="ROA",
        methods=["POST"],
        path="/{key}",
        operationType="write",
        parameters=[
            {"name": "bucket", "in": "host", "required": True, "schema": {"type": "string"}},
            {"name": "key", "in": "path", "required": True, "schema": {"type": "string"}},
        ],
    )
    metadata = document_metadata(raw, summary="Query object content")
    reasons = ("oss_response_mode_unsupported",)
    changes = {
        "signature_scheme": "oss_v4",
        "transport": "oss_v4_sdk",
        "executable": False,
        "unsupported_reasons": reasons,
    }
    expected = summary_snapshot(
        product="Oss",
        version="2019-05-17",
        action="SelectObject",
        summary="Query object content",
        style="ROA",
        method="POST",
        path="/{key}",
        operation_type="write",
        executable=False,
        unsupported_reasons=[
            "Alibaba Cloud OSS API Oss/SelectObject is not supported by this runtime. "
            "Choose a supported OSS action or use another client."
        ],
        required_parameters=[
            {"name": "bucket", "in": "host", "type": "string"},
            {"name": "key", "in": "path", "type": "string"},
        ],
        optional_parameters=[],
    )
    return metadata, changes, expected


@pytest.mark.parametrize(
    "case_factory",
    [rpc_case, roa_case, formdata_case, oss_supported_case, oss_unsupported_case],
    ids=["rpc", "roa", "formData", "oss-supported", "oss-unsupported"],
)
@pytest.mark.asyncio
async def test_summary_snapshots_are_exact_and_use_the_canonical_contract(case_factory: Any) -> None:
    metadata, contract_changes, expected = case_factory()
    tool, openmeta, resolver = tool_for(metadata, **contract_changes)

    result = await tool.execute(
        tool_input={"product": metadata.product, "action": metadata.action},
        context=ToolContext(tool_use_id="doc-call"),
    )

    assert result.is_error is False
    assert result.content == compact(expected)
    assert list(json.loads(result.content)) == list(expected)
    assert resolver.calls[0][1] is False
    assert resolver.calls[0][0].version is None
    assert openmeta.calls == [("api", metadata.product, metadata.version, metadata.action)]


@pytest.mark.asyncio
async def test_doc_fetches_the_exact_version_selected_by_the_contract_resolver() -> None:
    metadata = document_metadata(raw_api(version="2024-01-01"))
    product = ProductMetadata(
        product="Ecs",
        default_version="2025-01-01",
        versions=("2025-01-01",),
        documentation_url=None,
        first_class_excluded_versions=("2024-01-01",),
    )

    class SelectionOpenMeta(FakeOpenMeta):
        async def get_api_for_version_selection(
            self,
            product: str,
            version: str,
            action: str,
        ) -> MetadataFetch[Any]:
            self.calls.append(("selection_api", product, version, action))
            return MetadataFetch(value=self.metadata, source="fresh", error=None, cache_status="memory_fresh")

    openmeta = SelectionOpenMeta(metadata, product=product)
    resolver = FakeContractResolver(contract_for(metadata))
    tool = AliyunApiDoc(SimpleNamespace(openmeta=openmeta, contract_resolver=resolver))

    result = await tool.execute(
        tool_input={"product": "Ecs", "action": metadata.action},
        context=ToolContext(tool_use_id="doc-selected-version"),
    )

    assert result.is_error is False
    assert json.loads(result.content)["version"] == "2024-01-01"
    assert openmeta.calls == [("selection_api", "Ecs", "2024-01-01", metadata.action)]


@pytest.mark.asyncio
async def test_summary_uses_required_document_fields_nested_in_official_parameter_schema() -> None:
    raw = raw_api()
    region_id = raw["parameters"][0]
    for field in ("required", "description", "example"):
        region_id["schema"][field] = region_id.pop(field)
    metadata = document_metadata(raw, summary="List ECS instances")
    tool, _, _ = tool_for(metadata)

    result = await tool.execute(
        tool_input={"product": "Ecs", "action": "DescribeInstances"},
        context=ToolContext(tool_use_id="doc-official-shape"),
    )

    document = json.loads(result.content)
    assert document["required_parameters"] == [
        {"name": "RegionId", "in": "query", "type": "string", "example": "cn-hangzhou"}
    ]
    assert document["optional_parameters"] == ["InstanceIds"]


def test_schema_defaults_detail_to_summary_and_is_anonymous_read_only() -> None:
    metadata, _, _ = rpc_case()
    tool, _, _ = tool_for(metadata)

    assert tool.input_schema == {
        "type": "object",
        "properties": {
            "product": {"type": "string"},
            "action": {"type": "string"},
            "version": {"type": "string"},
            "detail": {"type": "string", "enum": ["summary", "full"], "default": "summary"},
        },
        "required": ["product", "action"],
        "additionalProperties": False,
    }
    assert tool.is_read_only({"product": "Ecs", "action": "DescribeInstances"}) is True
    assert tool.is_concurrency_safe({"product": "Ecs", "action": "DescribeInstances"}) is True


def test_repl_result_is_compact_until_verbose_transcript_is_requested(monkeypatch) -> None:
    metadata, _, document = rpc_case()
    tool, _, _ = tool_for(metadata)
    document["components"] = {"schemas": {"Large": {"description": "x" * 2_000}}}
    output = compact(document)
    monkeypatch.setattr("iac_code.tools.cloud.aliyun.aliyun_api_doc._", lambda message: "i18n:" + message)

    assert tool.render_verbose_result_in_transcript is True
    assert tool.render_tool_result_message(output) == (
        "i18n:Ecs/2014-05-26 DescribeInstances | RPC POST / | required=1 | optional=1 | executable=true"
    )
    assert tool.render_tool_result_message(output, verbose=True) == output


def test_repl_error_result_remains_visible_without_expansion() -> None:
    metadata, _, _ = rpc_case()
    tool, _, _ = tool_for(metadata)
    error = "Alibaba Cloud API metadata is temporarily unavailable."

    assert tool.render_tool_result_message(error, is_error=True) == error
    assert tool.render_tool_result_message(error, is_error=True, verbose=True) == error


def test_openmeta_normalization_retains_the_complete_document_view() -> None:
    raw = raw_api(
        title="Describe instances",
        summary="List ECS instances",
        deprecated=True,
        parameters=[
            {
                "name": "Filter",
                "in": "query",
                "required": True,
                "schema": {"$ref": "#/components/schemas/Filter"},
            }
        ],
        components={"schemas": {"Filter": {"type": "object"}}},
        errorCodes={"400": [{"Code": "InvalidParameter"}]},
        changeSet=[{"changeType": "ADD"}],
        staticInfo={"returnType": "async"},
    )

    metadata = normalize_api_metadata(raw)

    assert metadata.title == "Describe instances"
    assert metadata.summary == "List ECS instances"
    assert metadata.deprecated is True
    assert metadata.document_parameters[0].schema == {"$ref": "#/components/schemas/Filter"}
    assert metadata.parameters[0].schema == {"type": "object"}
    assert metadata.error_codes == {"400": ({"Code": "InvalidParameter"},)}
    assert metadata.change_set == ({"changeType": "ADD"},)
    assert metadata.static_info == {"returnType": "async"}


@pytest.mark.asyncio
async def test_invalid_detail_fails_before_openmeta() -> None:
    metadata, _, _ = rpc_case()
    tool, openmeta, resolver = tool_for(metadata)

    result = await tool.execute(
        tool_input={"product": "Ecs", "action": "DescribeInstances", "detail": "raw"},
        context=ToolContext(tool_use_id="doc-call"),
    )

    assert result.is_error is True
    assert result.content == "Alibaba Cloud API Ecs/DescribeInstances detail must be one of: summary, full."
    assert openmeta.calls == []
    assert resolver.calls == []


@pytest.mark.parametrize(
    ("field", "value", "expected"),
    [
        (
            "product",
            "ecs/../../secret",
            "Alibaba Cloud product must contain only letters, numbers, underscores, or hyphens.",
        ),
        (
            "action",
            "Describe/Instances",
            "Alibaba Cloud API action must contain only letters, numbers, underscores, or hyphens.",
        ),
        ("version", "2014/05/26", "Alibaba Cloud API version contains unsupported characters."),
    ],
)
@pytest.mark.asyncio
async def test_doc_and_execute_share_public_identity_validation_before_openmeta(
    field: str,
    value: str,
    expected: str,
) -> None:
    metadata, _, _ = rpc_case()
    openmeta = FakeOpenMeta(metadata)
    resolver = FakeContractResolver(contract_for(metadata))
    services = SimpleNamespace(
        openmeta=openmeta,
        contract_resolver=resolver,
        contract_store=ResolvedContractStore(),
        permission_stage_observer=None,
        default_region_provider=lambda: "cn-hangzhou",
    )
    doc_tool = AliyunApiDoc(services)
    execute_tool = AliyunApi(services=services)
    tool_input = {
        "product": "ecs",
        "action": "DescribeInstances",
        "version": "2014-05-26",
        field: value,
    }

    doc_result = await doc_tool.execute(tool_input=tool_input, context=ToolContext(tool_use_id="doc-call"))
    prepared = execute_tool.prepare_invocation_input(tool_input)
    binding = InvocationBinding(
        runtime_nonce="runtime",
        session_id="session",
        tool_use_id="call",
        tool_name="aliyun_api",
        canonical_input_sha256=canonical_input_sha256(prepared),
    )
    permission = await execute_tool.check_permissions(
        tool_input,
        ToolPermissionContext(cwd="/tmp", invocation_binding=binding),
    )

    assert doc_result == ToolResult.error(expected)
    assert permission.behavior == "deny"
    assert permission.message == expected
    assert openmeta.calls == []
    assert resolver.calls == []


@pytest.mark.parametrize(
    ("api_error", "runtime_code", "expected"),
    [
        (
            "not_found",
            "metadata_not_found",
            "Alibaba Cloud API Ecs/2014-05-26/DescribeInstances was not found. Check the product, version, and action.",
        ),
        (
            "temporarily_unavailable",
            "metadata_unavailable",
            "Alibaba Cloud API metadata for Ecs/DescribeInstances is temporarily unavailable; try again later.",
        ),
        (
            "protocol_error",
            "metadata_protocol_error",
            "Alibaba Cloud API metadata for Ecs/DescribeInstances returned an incompatible response. "
            "Check the API identifiers or try again later.",
        ),
    ],
)
@pytest.mark.asyncio
async def test_doc_and_execute_share_metadata_failure_public_errors(
    api_error: str,
    runtime_code: str,
    expected: str,
) -> None:
    metadata, _, _ = rpc_case()
    openmeta = FakeOpenMeta(metadata, api_error=api_error)
    resolver = RaisingContractResolver(runtime_code, product="Ecs")
    services = SimpleNamespace(
        openmeta=openmeta,
        contract_resolver=resolver,
        contract_store=ResolvedContractStore(),
        permission_stage_observer=None,
        default_region_provider=lambda: "cn-hangzhou",
    )
    doc_tool = AliyunApiDoc(services)
    execute_tool = AliyunApi(services=services)
    tool_input = {"product": "ecs", "action": "DescribeInstances", "version": "2014-05-26"}

    doc_result = await doc_tool.execute(tool_input=tool_input, context=ToolContext(tool_use_id="doc-call"))
    prepared = execute_tool.prepare_invocation_input(tool_input)
    binding = InvocationBinding(
        runtime_nonce="runtime",
        session_id="session",
        tool_use_id="call",
        tool_name="aliyun_api",
        canonical_input_sha256=canonical_input_sha256(prepared),
    )
    permission = await execute_tool.check_permissions(
        tool_input,
        ToolPermissionContext(cwd="/tmp", invocation_binding=binding),
    )

    assert doc_result == ToolResult.error(expected)
    assert permission.behavior == "deny"
    assert permission.message == expected


@pytest.mark.asyncio
async def test_full_snapshot_contains_all_fields_and_only_reachable_recursive_components() -> None:
    root_schema = {
        "type": "object",
        "properties": {"child": {"$ref": "#/components/schemas/Child"}},
    }
    child_schema = {
        "type": "object",
        "properties": {"parent": {"$ref": "#/components/schemas/Root"}},
    }
    raw = raw_api(
        parameters=[
            {
                "name": "Filter",
                "in": "query",
                "required": True,
                "style": "json",
                "description": "Structured filter",
                "example": {"name": "demo"},
                "schema": {"$ref": "#/components/schemas/Root"},
            },
            {
                "name": "PageNumber",
                "in": "query",
                "description": "Page number",
                "schema": {"type": "integer", "format": "int32", "enum": [1, 2]},
            },
        ],
        responses={"200": {"schema": {"$ref": "#/components/schemas/Root"}}},
        components={
            "schemas": {
                "Root": root_schema,
                "Child": child_schema,
                "Unused": {"type": "string"},
            }
        },
    )
    metadata = document_metadata(
        raw,
        summary="List ECS instances",
        deprecated=True,
        error_codes={"400": ({"Code": "InvalidParameter", "Message": "long text"},)},
        change_set=({"changeType": "ADD", "effectiveTime": "2026-07-11"},),
        static_info={"returnType": "async"},
        unknown_extension="must-not-leak",
    )
    tool, _, _ = tool_for(metadata)

    result = await tool.execute(
        tool_input={"product": "Ecs", "action": "DescribeInstances", "detail": "full"},
        context=ToolContext(tool_use_id="doc-call"),
    )

    assert result.is_error is False
    payload = json.loads(result.content)
    assert list(payload) == [
        "product",
        "version",
        "action",
        "summary",
        "style",
        "method",
        "path",
        "operation_type",
        "executable",
        "unsupported_reasons",
        "documentation_url",
        "required_parameters",
        "optional_parameters",
        "parameters",
        "consumes",
        "produces",
        "schemes",
        "security",
        "deprecated",
        "responses",
        "components",
        "error_codes",
        "change_set",
        "static_info",
    ]
    assert payload["parameters"] == [
        {
            "name": "Filter",
            "in": "query",
            "required": True,
            "schema": {"$ref": "#/components/schemas/Root"},
            "description": "Structured filter",
            "example": {"name": "demo"},
        },
        {
            "name": "PageNumber",
            "in": "query",
            "required": False,
            "schema": {"type": "integer", "format": "int32", "enum": [1, 2]},
            "description": "Page number",
            "example": None,
        },
    ]
    assert payload["security"] == [{"AK": []}]
    assert payload["components"] == {"schemas": {"Root": root_schema, "Child": child_schema}}
    assert payload["error_codes"] == {"400": ["InvalidParameter"]}
    assert payload["change_set"] == [{"changeType": "ADD", "effectiveTime": "2026-07-11"}]
    assert payload["static_info"] == {"returnType": "async"}
    assert "unknown_extension" not in result.content


@pytest.mark.asyncio
async def test_invalid_parameter_ref_uses_real_resolver_for_doc_execute_identity() -> None:
    raw = raw_api(
        parameters=[
            {
                "name": "Broken",
                "in": "query",
                "required": True,
                "schema": {
                    "type": "object",
                    "properties": {"child": {"$ref": "#/components/schemas/Missing"}},
                },
            }
        ],
    )
    metadata = normalize_api_metadata(raw)
    openmeta = FakeOpenMeta(metadata)
    resolver = ApiContractResolver(openmeta)
    tool = AliyunApiDoc(SimpleNamespace(openmeta=openmeta, contract_resolver=resolver))

    call = ApiCallShape(
        product="Ecs",
        version=None,
        action="DescribeInstances",
        region_id="cn-hangzhou",
        explicit_overrides=(),
        parameter_names_by_location={},
        body_source="none",
    )

    result = await tool.execute(
        tool_input={"product": "Ecs", "action": "DescribeInstances", "detail": "full"},
        context=ToolContext(tool_use_id="doc-call"),
    )

    payload = json.loads(result.content)
    contract = await resolver.resolve(call, allow_fallback=False)
    assert contract.executable is False
    assert contract.unsupported_reasons == ("parameter_schema_reference_unsupported",)
    assert payload["executable"] is contract.executable
    assert payload["unsupported_reasons"] == [
        "Alibaba Cloud API Ecs/DescribeInstances metadata uses a schema this runtime cannot execute. "
        "Choose another API version or action."
    ]
    assert payload["parameters"][0]["schema"]["properties"]["child"] == {"$ref": "#/components/schemas/Missing"}
    assert payload["responses"] == {}
    assert payload["components"] == {"schemas": {}}
    with pytest.raises(ApiContractError, match="contract_not_executable"):
        await RequestBuilder().build(contract, {"params": {"Broken": "value"}})


@pytest.mark.asyncio
async def test_unknown_parameter_style_uses_canonical_contract_reason() -> None:
    raw = raw_api(
        parameters=[
            {
                "name": "Future",
                "in": "query",
                "style": "futureStyle",
                "schema": {"type": "string"},
            }
        ]
    )
    metadata = normalize_api_metadata(raw)
    openmeta = FakeOpenMeta(metadata)
    resolver = ApiContractResolver(openmeta)
    tool = AliyunApiDoc(SimpleNamespace(openmeta=openmeta, contract_resolver=resolver))

    result = await tool.execute(
        tool_input={"product": "Ecs", "action": "DescribeInstances"},
        context=ToolContext(tool_use_id="doc-call"),
    )

    payload = json.loads(result.content)
    assert payload["executable"] is False
    expected = (
        "Alibaba Cloud API Ecs/DescribeInstances uses a protocol shape this runtime cannot execute. "
        "Choose another API version or action."
    )
    assert payload["unsupported_reasons"] == [expected]

    execute_tool = AliyunApi(
        services=SimpleNamespace(
            openmeta=openmeta,
            contract_resolver=resolver,
            contract_store=ResolvedContractStore(),
            permission_stage_observer=None,
            default_region_provider=lambda: "cn-hangzhou",
        )
    )
    tool_input = {"product": "Ecs", "action": "DescribeInstances"}
    prepared = execute_tool.prepare_invocation_input(tool_input)
    binding = InvocationBinding(
        "runtime",
        "session",
        "execute",
        "aliyun_api",
        canonical_input_sha256(prepared),
    )
    permission = await execute_tool.check_permissions(
        tool_input,
        ToolPermissionContext(cwd="/tmp", invocation_binding=binding),
    )

    assert permission.behavior == "deny"
    assert permission.message == expected
    assert "parameter_style_unsupported" not in result.content


@pytest.mark.parametrize(
    ("product_error", "api_error", "expected"),
    [
        (
            "not_found",
            None,
            "Alibaba Cloud product Nope was not found. Check the product code and try again.",
        ),
        (
            None,
            "not_found",
            "Alibaba Cloud API Ecs/2014-05-26/Missing was not found. Check the product, version, and action.",
        ),
        (
            None,
            "temporarily_unavailable",
            "Alibaba Cloud API metadata for Ecs/Missing is temporarily unavailable; try again later.",
        ),
        (
            None,
            "protocol_error",
            "Alibaba Cloud API metadata for Ecs/Missing returned an incompatible response. "
            "Check the API identifiers or try again later.",
        ),
    ],
)
@pytest.mark.asyncio
async def test_not_found_and_temporary_unavailable_are_distinct_and_never_fallback(
    product_error: str | None,
    api_error: str | None,
    expected: str,
) -> None:
    metadata, _, _ = rpc_case()
    openmeta = FakeOpenMeta(metadata, product_error=product_error, api_error=api_error)
    resolver: FakeContractResolver | RaisingContractResolver
    if product_error:
        resolver = RaisingContractResolver("product_not_found", product="Nope")
    else:
        resolver = FakeContractResolver(contract_for(metadata))
    tool = AliyunApiDoc(SimpleNamespace(openmeta=openmeta, contract_resolver=resolver))
    product = "Nope" if product_error else "Ecs"
    action = "Missing" if api_error else "DescribeInstances"

    result = await tool.execute(
        tool_input={"product": product, "action": action},
        context=ToolContext(tool_use_id="doc-call"),
    )

    assert result.is_error is True
    assert result.content == expected
    assert len(resolver.calls) == 1
    assert resolver.calls[0][0].version is None
    assert resolver.calls[0][1] is False


@pytest.mark.parametrize("product_error", ["temporarily_unavailable", "protocol_error", "not_found"])
@pytest.mark.parametrize("api_source", ["fresh", "cache", "stale_cache"])
@pytest.mark.asyncio
async def test_explicit_version_uses_api_metadata_when_product_metadata_is_unavailable(
    product_error: str,
    api_source: Literal["fresh", "cache", "stale_cache"],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    metadata, _, expected = rpc_case()
    openmeta = FakeOpenMeta(metadata, product_error=product_error, api_source=api_source)
    resolver = FakeContractResolver(contract_for(metadata, metadata_source=api_source))
    tool = AliyunApiDoc(SimpleNamespace(openmeta=openmeta, contract_resolver=resolver))
    telemetry: list[tuple[str, str]] = []
    monkeypatch.setattr(
        "iac_code.tools.cloud.aliyun.aliyun_api_doc.emit_aliyun_api_doc",
        lambda detail, outcome: telemetry.append((detail, outcome)),
    )

    result = await tool.execute(
        tool_input={"product": "ecs", "version": "2014-05-26", "action": "DescribeInstances"},
        context=ToolContext(tool_use_id="doc-call"),
    )

    assert result == ToolResult.success(compact(expected))
    assert openmeta.calls == [("api", "Ecs", "2014-05-26", "DescribeInstances")]
    assert resolver.calls[0][0].product == "ecs"
    assert resolver.calls[0][0].version == "2014-05-26"
    assert resolver.calls[0][1] is False
    assert telemetry == [("summary", "success")]


@pytest.mark.parametrize(
    ("api_error", "expected"),
    [
        (
            "not_found",
            "Alibaba Cloud API Ecs/2014-05-26/DescribeInstances was not found. Check the product, version, and action.",
        ),
        (
            "protocol_error",
            "Alibaba Cloud API metadata for Ecs/DescribeInstances returned an incompatible response. "
            "Check the API identifiers or try again later.",
        ),
    ],
)
@pytest.mark.asyncio
async def test_explicit_version_reports_api_metadata_error_when_product_metadata_is_unavailable(
    api_error: str,
    expected: str,
) -> None:
    metadata, _, _ = rpc_case()
    openmeta = FakeOpenMeta(metadata, product_error="protocol_error", api_error=api_error)
    resolver = FakeContractResolver(contract_for(metadata))
    tool = AliyunApiDoc(SimpleNamespace(openmeta=openmeta, contract_resolver=resolver))

    result = await tool.execute(
        tool_input={"product": "ecs", "version": "2014-05-26", "action": "DescribeInstances"},
        context=ToolContext(tool_use_id="doc-call"),
    )

    assert result == ToolResult.error(expected)
    assert openmeta.calls == [("api", "Ecs", "2014-05-26", "DescribeInstances")]
    assert len(resolver.calls) == 1
    assert resolver.calls[0][0].version == "2014-05-26"
    assert resolver.calls[0][1] is False


async def _render_at_character_count(metadata: Any, target: int) -> Any:
    tool, _, _ = tool_for(metadata)
    metadata.summary = ""
    probe = await tool.execute(
        tool_input={"product": metadata.product, "action": metadata.action, "detail": "full"},
        context=ToolContext(tool_use_id="probe"),
    )
    assert probe.is_error is False
    metadata.summary = "x" * (target - len(probe.content))
    return await tool.execute(
        tool_input={"product": metadata.product, "action": metadata.action, "detail": "full"},
        context=ToolContext(tool_use_id="doc-call"),
    )


@pytest.mark.asyncio
async def test_full_document_48000_is_inline_and_low_priority_overflow_is_compacted() -> None:
    metadata, _, _ = rpc_case()

    inline = await _render_at_character_count(metadata, 48_000)
    assert len(inline.content) == 48_000
    assert inline.metadata is None

    overflow = await _render_at_character_count(metadata, 48_001)
    payload = json.loads(overflow.content)
    assert len(overflow.content) <= 48_000
    assert payload["parameters"][0]["schema"] == {"type": "string"}
    assert "summary" in payload["truncated_sections"]
    assert overflow.metadata is None


@pytest.mark.asyncio
async def test_full_document_returns_complete_schema_when_it_cannot_fit_inline_budget() -> None:
    raw = raw_api()
    large_enum = "x" * 49_000
    raw["parameters"][0]["schema"] = {"type": "string", "enum": [large_enum]}
    metadata = document_metadata(raw, summary="discardable summary")
    tool, _, _ = tool_for(metadata)

    result = await tool.execute(
        tool_input={"product": "Ecs", "action": "DescribeInstances", "detail": "full"},
        context=ToolContext(),
    )

    assert result.is_error is False
    assert result.metadata is None
    assert len(result.content) > 48_000
    assert json.loads(result.content)["parameters"][0]["schema"] == raw["parameters"][0]["schema"]


@pytest.mark.asyncio
async def test_full_document_compaction_preserves_executable_parameter_schema() -> None:
    enum_value = "e" * 24_000
    raw = raw_api()
    raw["parameters"][0]["schema"] = {"type": "string", "enum": [enum_value]}
    metadata = document_metadata(raw, summary=None)
    tool, _, _ = tool_for(metadata)

    result = await tool.execute(
        tool_input={"product": "Ecs", "action": "DescribeInstances", "detail": "full"},
        context=ToolContext(),
    )

    payload = json.loads(result.content)
    assert result.is_error is False
    assert payload["parameters"][0]["schema"] == {"type": "string", "enum": [enum_value]}
    assert "enum" not in payload["required_parameters"][0]


@pytest.mark.asyncio
async def test_doc_and_execute_share_unknown_top_level_field_error_context_before_openmeta() -> None:
    metadata, _, _ = rpc_case()
    tool, openmeta, _ = tool_for(metadata)
    services = SimpleNamespace(
        openmeta=openmeta,
        contract_resolver=FakeContractResolver(contract_for(metadata)),
        contract_store=ResolvedContractStore(),
        permission_stage_observer=None,
        default_region_provider=lambda: "cn-hangzhou",
    )
    execute_tool = AliyunApi(services=services)
    tool_input = {"product": "Ecs", "action": "DescribeInstances", "extra": "business-value"}

    doc_result = await tool.execute(tool_input=tool_input, context=ToolContext(tool_use_id="doc-call"))
    prepared = execute_tool.prepare_invocation_input(tool_input)
    binding = InvocationBinding(
        runtime_nonce="runtime",
        session_id="session",
        tool_use_id="call",
        tool_name="aliyun_api",
        canonical_input_sha256=canonical_input_sha256(prepared),
    )
    permission = await execute_tool.check_permissions(
        tool_input,
        ToolPermissionContext(cwd="/tmp", invocation_binding=binding),
    )

    assert doc_result.is_error is True
    assert permission.behavior == "deny"
    assert doc_result.content == permission.message
    assert "Ecs/DescribeInstances" in doc_result.content
    assert "business-value" not in doc_result.content
    assert openmeta.calls == []


@pytest.mark.parametrize(
    "tool_input",
    [
        {"product": "Ecs", "action": "DescribeInstances", "detail": "CUSTOMER_SECRET"},
        {"product": "Ecs", "action": "DescribeInstances", "extra": "CUSTOMER_SECRET"},
    ],
)
@pytest.mark.asyncio
async def test_tool_executor_uses_doc_public_validation_boundary_without_echoing_invalid_values(
    tool_input: dict[str, Any],
) -> None:
    metadata, _, _ = rpc_case()
    tool, openmeta, _ = tool_for(metadata)
    registry = ToolRegistry()
    registry.register(tool)

    result = (
        await ToolExecutor(registry).execute_batch(
            [ToolCallRequest(id="doc-call", name="aliyun_api_doc", input=tool_input)],
            ToolContext(),
        )
    )[0]

    assert result.is_error is True
    assert "CUSTOMER_SECRET" not in result.content
    assert "Ecs/DescribeInstances" in result.content
    assert openmeta.calls == []


@pytest.mark.parametrize("detail", [["CUSTOMER_SECRET"], {"value": "CUSTOMER_SECRET"}, "raw"])
@pytest.mark.asyncio
async def test_doc_validation_hook_is_total_and_emits_invalid_input_telemetry(
    detail: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    metadata, _, _ = rpc_case()
    tool, openmeta, _ = tool_for(metadata)
    registry = ToolRegistry()
    registry.register(tool)
    telemetry: list[tuple[str, str]] = []
    monkeypatch.setattr(
        "iac_code.tools.cloud.aliyun.aliyun_api_doc.emit_aliyun_api_doc",
        lambda safe_detail, outcome: telemetry.append((safe_detail, outcome)),
    )

    result = (
        await ToolExecutor(registry).execute_batch(
            [
                ToolCallRequest(
                    id="doc-invalid",
                    name="aliyun_api_doc",
                    input={"product": "Ecs", "action": "DescribeInstances", "detail": detail},
                )
            ],
            ToolContext(),
        )
    )[0]

    assert result.is_error is True
    assert "CUSTOMER_SECRET" not in result.content
    assert telemetry == [("summary", "invalid_input")]
    assert openmeta.calls == []


@pytest.mark.asyncio
async def test_tool_executor_uses_same_safe_identity_boundary_for_doc_and_execute_tools() -> None:
    metadata, _, _ = rpc_case()
    doc_tool, openmeta, resolver = tool_for(metadata)
    services = SimpleNamespace(
        openmeta=openmeta,
        contract_resolver=resolver,
        contract_store=ResolvedContractStore(),
        permission_stage_observer=None,
        default_region_provider=lambda: "cn-hangzhou",
    )
    registry = ToolRegistry()
    registry.register(doc_tool)
    registry.register(AliyunApi(services=services))
    unsafe_input = {
        "product": "ecs/../../CUSTOMER_SECRET",
        "action": "DescribeInstances",
    }

    results = await ToolExecutor(registry).execute_batch(
        [
            ToolCallRequest(id="doc", name="aliyun_api_doc", input=dict(unsafe_input)),
            ToolCallRequest(id="execute", name="aliyun_api", input=dict(unsafe_input)),
        ],
        ToolContext(),
    )

    assert results[0] == results[1]
    assert results[0].is_error is True
    assert "CUSTOMER_SECRET" not in results[0].content
    assert openmeta.calls == []


@pytest.mark.asyncio
async def test_doc_and_execute_use_openmeta_canonical_product_in_metadata_errors() -> None:
    product = ProductMetadata("FC", "2023-03-30", ("2023-03-30",), None)
    openmeta = FakeOpenMeta(None, product=product, api_error="protocol_error")
    resolver = ApiContractResolver(openmeta)
    services = SimpleNamespace(
        openmeta=openmeta,
        contract_resolver=resolver,
        contract_store=ResolvedContractStore(),
        permission_stage_observer=None,
        default_region_provider=lambda: "cn-hangzhou",
    )
    doc_tool = AliyunApiDoc(services)
    execute_tool = AliyunApi(services=services)
    tool_input = {"product": "fc", "action": "GetFunction", "version": "2023-03-30"}

    doc_result = await doc_tool.execute(tool_input=tool_input, context=ToolContext(tool_use_id="doc"))
    prepared = execute_tool.prepare_invocation_input(tool_input)
    binding = InvocationBinding(
        "runtime",
        "session",
        "execute",
        "aliyun_api",
        canonical_input_sha256(prepared),
    )
    permission = await execute_tool.check_permissions(
        tool_input,
        ToolPermissionContext(cwd="/tmp", invocation_binding=binding),
    )

    expected = (
        "Alibaba Cloud API metadata for FC/GetFunction returned an incompatible response. "
        "Check the API identifiers or try again later."
    )
    assert doc_result == ToolResult.error(expected)
    assert permission.behavior == "deny"
    assert permission.message == expected


@pytest.mark.asyncio
async def test_doc_and_execute_share_known_product_canonicalization_when_product_metadata_fails() -> None:
    openmeta = FakeOpenMeta(None, product_error="protocol_error")
    resolver = ApiContractResolver(openmeta)
    services = SimpleNamespace(
        openmeta=openmeta,
        contract_resolver=resolver,
        contract_store=ResolvedContractStore(),
        permission_stage_observer=None,
        default_region_provider=lambda: "cn-hangzhou",
    )
    doc_tool = AliyunApiDoc(services)
    execute_tool = AliyunApi(services=services)
    tool_input = {"product": "ecs", "action": "DescribeInstances"}

    doc_result = await doc_tool.execute(tool_input=tool_input, context=ToolContext(tool_use_id="doc"))
    prepared = execute_tool.prepare_invocation_input(tool_input)
    binding = InvocationBinding(
        "runtime",
        "session",
        "execute",
        "aliyun_api",
        canonical_input_sha256(prepared),
    )
    permission = await execute_tool.check_permissions(
        tool_input,
        ToolPermissionContext(cwd="/tmp", invocation_binding=binding),
    )

    expected = (
        "Alibaba Cloud API metadata for Ecs/DescribeInstances returned an incompatible response. "
        "Check the API identifiers or try again later."
    )
    assert doc_result == ToolResult.error(expected)
    assert permission.behavior == "deny"
    assert permission.message == expected


@pytest.mark.asyncio
async def test_large_full_document_does_not_require_session_or_tool_use_id() -> None:
    raw = raw_api()
    raw["parameters"][0]["schema"] = {"type": "string", "enum": ["x" * 49_000]}
    metadata = document_metadata(raw)
    tool, _, _ = tool_for(metadata)

    result = await tool.execute(
        tool_input={"product": "Ecs", "action": "DescribeInstances", "detail": "full"},
        context=ToolContext(),
    )

    assert result.is_error is False
    assert result.metadata is None
    assert len(result.content) > 48_000
    assert json.loads(result.content)["parameters"][0]["schema"] == raw["parameters"][0]["schema"]
