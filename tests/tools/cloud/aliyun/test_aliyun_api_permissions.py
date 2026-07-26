import asyncio
import copy
import json
import time
from dataclasses import replace
from itertools import product
from pathlib import Path
from types import MappingProxyType, SimpleNamespace
from typing import Any

import pytest

from iac_code.services.permissions.audit import fingerprint_text
from iac_code.services.permissions.pipeline import check_tool_permission
from iac_code.tools.base import ToolContext, ToolResult
from iac_code.tools.cloud.aliyun import aliyun_api as aliyun_api_module
from iac_code.tools.cloud.aliyun import api_contract as api_contract_module
from iac_code.tools.cloud.aliyun.acs3_transport import NormalizedApiResponse, TransportRouter
from iac_code.tools.cloud.aliyun.aliyun_api import AliyunApi
from iac_code.tools.cloud.aliyun.api_contract import (
    ApiContractError,
    BuiltApiRequest,
    CanonicalWireContract,
    RequestBuilder,
    ResponseBodyPolicy,
)
from iac_code.tools.cloud.aliyun.contract_store import (
    ResolvedContractStore,
    canonical_input_sha256,
)
from iac_code.tools.cloud.aliyun.endpoint_resolver import EndpointResolution
from iac_code.tools.cloud.aliyun.openmeta import ParameterMetadata
from iac_code.tools.cloud.aliyun.public_errors import public_aliyun_error
from iac_code.tools.cloud.aliyun.result_contract import (
    ALIYUN_BODY_CONTRACT_VERSION,
    ALIYUN_HTTP_METADATA_KEY,
)
from iac_code.tools.cloud.aliyun.retry_policy import RetryBudget
from iac_code.tools.path_safety import get_iac_code_application_root
from iac_code.types.permissions import InvocationBinding, PermissionMode, ToolPermissionContext


def _ctx(*, allow=None, deny=None, ask=None, mode=PermissionMode.DEFAULT):
    return ToolPermissionContext(
        cwd="/tmp",
        allow_rules=allow or {},
        deny_rules=deny or {},
        ask_rules=ask or {},
        mode=mode,
    )


def _strict_ctx(project, session_dir) -> ToolPermissionContext:
    return ToolPermissionContext(
        cwd=str(project),
        strict_read_directories=[str(project), str(session_dir), str(get_iac_code_application_root())],
        read_path_violation_behavior="deny",
    )


@pytest.mark.asyncio
async def test_read_api_allows() -> None:
    result = await AliyunApi().check_permissions({"product": "ecs", "action": "DescribeInstances"}, _ctx())
    assert result.behavior == "allow"
    assert result.audit is not None
    assert result.audit.scope == "read_only"
    assert result.audit.is_read_only is True


@pytest.mark.asyncio
async def test_ros_preview_stack_is_readonly_case_insensitive() -> None:
    result = await AliyunApi().check_permissions({"product": "ROS", "action": "PreviewStack"}, _ctx())
    assert result.behavior == "allow"


@pytest.mark.asyncio
async def test_readonly_ros_local_template_url_outside_strict_roots_denied(tmp_path) -> None:
    project = tmp_path / "project"
    session_dir = tmp_path / "session"
    outside = tmp_path / "outside"
    project.mkdir()
    session_dir.mkdir()
    outside.mkdir()
    template = outside / "template.yml"
    template.write_text("ROSTemplateFormatVersion: '2015-09-01'\n", encoding="utf-8")

    result = await AliyunApi().check_permissions(
        {
            "product": "ros",
            "action": "ValidateTemplate",
            "params": {"TemplateURL": str(template)},
        },
        _strict_ctx(project, session_dir),
    )

    assert result.behavior == "deny"
    assert result.reason is not None
    assert result.reason.type == "path_constraint"


@pytest.mark.asyncio
async def test_write_ros_local_template_url_outside_strict_roots_denied_before_ask(tmp_path) -> None:
    project = tmp_path / "project"
    session_dir = tmp_path / "session"
    outside = tmp_path / "outside"
    project.mkdir()
    session_dir.mkdir()
    outside.mkdir()
    template = outside / "template.yml"
    template.write_text("ROSTemplateFormatVersion: '2015-09-01'\n", encoding="utf-8")

    result = await AliyunApi().check_permissions(
        {
            "product": "ros",
            "action": "CreateStack",
            "params": {"StackName": "demo", "TemplateURL": str(template)},
        },
        _strict_ctx(project, session_dir),
    )

    assert result.behavior == "deny"
    assert result.reason is not None
    assert result.reason.type == "path_constraint"


@pytest.mark.asyncio
async def test_write_api_asks_with_action_suggestion() -> None:
    result = await AliyunApi().check_permissions({"product": "ros", "action": "CreateStack"}, _ctx())
    assert result.behavior == "ask"
    assert result.audit is not None
    assert result.audit.scope == "once"
    assert result.suggestions is not None
    assert result.suggestions[0].tool_name == "aliyun_api"
    assert result.suggestions[0].rule_content == "ros:CreateStack"


@pytest.mark.asyncio
async def test_roa_write_method_asks_even_with_read_prefixed_action() -> None:
    result = await AliyunApi().check_permissions(
        {
            "product": "cs",
            "action": "DescribeClusters",
            "style": "ROA",
            "method": "DELETE",
            "pathname": "/clusters/c-123",
        },
        _ctx(),
    )

    assert result.behavior == "ask"
    assert result.audit is not None
    assert result.audit.scope == "once"
    assert result.audit.is_read_only is False
    assert result.suggestions is not None
    assert result.suggestions[0].tool_name == "aliyun_api"
    assert result.suggestions[0].rule_content == "cs:DescribeClusters"


@pytest.mark.asyncio
async def test_roa_write_method_honors_exact_product_action_allow_rule() -> None:
    result = await AliyunApi().check_permissions(
        {
            "product": "cs",
            "action": "DescribeClusters",
            "style": "ROA",
            "method": "DELETE",
            "pathname": "/clusters/c-123",
        },
        _ctx(allow={"session": ["aliyun_api(cs:DescribeClusters)"]}),
    )

    assert result.behavior == "allow"
    assert result.audit is not None
    assert result.audit.scope == "session_rule"
    assert result.audit.rule_source == "session"
    assert result.audit.rule == "cs:DescribeClusters"
    assert result.audit.is_read_only is False


@pytest.mark.asyncio
async def test_roa_write_method_requires_exact_allow_rule() -> None:
    result = await AliyunApi().check_permissions(
        {
            "product": "cs",
            "action": "DescribeClusters",
            "style": "ROA",
            "method": "DELETE",
            "pathname": "/clusters/c-123",
        },
        _ctx(allow={"session": ["aliyun_api(cs:*)"]}),
    )

    assert result.behavior == "ask"
    assert result.audit is not None
    assert result.audit.scope == "once"
    assert result.audit.is_read_only is False


@pytest.mark.asyncio
async def test_roa_write_method_still_honors_deny_rule() -> None:
    result = await AliyunApi().check_permissions(
        {
            "product": "cs",
            "action": "DescribeClusters",
            "style": "ROA",
            "method": "DELETE",
            "pathname": "/clusters/c-123",
        },
        _ctx(deny={"local_settings": ["aliyun_api(cs:DescribeClusters)"]}),
    )

    assert result.behavior == "deny"
    assert result.audit is not None
    assert result.audit.scope == "settings_rule"
    assert result.audit.rule == "cs:DescribeClusters"
    assert result.audit.is_read_only is False


@pytest.mark.parametrize(
    "input",
    [
        {"product": "ros", "action": None},
        {"product": "ros", "action": 123},
        {"product": 123, "action": "CreateStack"},
    ],
)
@pytest.mark.asyncio
async def test_malformed_product_or_action_fails_closed_without_suggestion(input: dict) -> None:
    result = await AliyunApi().check_permissions(input, _ctx())
    assert result.behavior == "ask"
    assert result.suggestions in (None, [])


@pytest.mark.parametrize(
    "product, action",
    [
        ("ro*", "CreateStack"),
        ("ros", "Create:Stack"),
        ("ros", "Create(Stack)"),
        ("ros", "x" * 129),
        ("ro*", "DescribeInstances"),
        ("ros", "Describe:Stack"),
        ("ros", "DescribeInstances token=secret"),
        ("ros", "Get/../../CreateStack"),
    ],
)
@pytest.mark.asyncio
async def test_unsafe_values_do_not_get_persistent_suggestions(product: str, action: str) -> None:
    result = await AliyunApi().check_permissions({"product": product, "action": action}, _ctx())
    assert result.behavior == "ask"
    assert result.audit is not None
    assert result.audit.is_read_only is False
    assert result.suggestions in (None, [])


@pytest.mark.asyncio
async def test_unsafe_values_are_fingerprinted_in_operation_metadata() -> None:
    result = await AliyunApi().check_permissions(
        {
            "product": "ro*secret",
            "action": "Create:Stack secret",
            "region_id": "cn-hangzhou/secret",
        },
        _ctx(),
    )

    assert result.audit is not None
    assert result.audit.operation == {
        "product_fingerprint": fingerprint_text("ro*secret"),
        "action_fingerprint": fingerprint_text("Create:Stack secret"),
        "region_fingerprint": fingerprint_text("cn-hangzhou/secret"),
    }


@pytest.mark.asyncio
async def test_wildcard_rule_does_not_allow_unsafe_runtime_action() -> None:
    context = _ctx(allow={"project_settings": ["aliyun_api(ros:*)"]})
    result = await AliyunApi().check_permissions({"product": "ros", "action": "Create:Stack"}, context)
    assert result.behavior == "ask"
    assert result.suggestions in (None, [])


@pytest.mark.asyncio
async def test_wildcard_rule_does_not_allow_unsafe_runtime_product() -> None:
    context = _ctx(allow={"project_settings": ["aliyun_api(ro*:CreateStack)"]})
    result = await AliyunApi().check_permissions({"product": "ro*", "action": "CreateStack"}, context)
    assert result.behavior == "ask"
    assert result.suggestions in (None, [])


@pytest.mark.parametrize("input", [{"product": "ros"}, {"action": "CreateStack"}])
@pytest.mark.asyncio
async def test_missing_product_or_action_asks_without_suggestion(input: dict) -> None:
    result = await AliyunApi().check_permissions(input, _ctx(allow={"project_settings": ["aliyun_api(ros:*)"]}))
    assert result.behavior == "ask"
    assert result.suggestions in (None, [])


@pytest.mark.asyncio
async def test_exact_rule_allows_only_matching_action() -> None:
    context = _ctx(allow={"user_settings": ["aliyun_api(ros:CreateStack)"]})
    allowed = await AliyunApi().check_permissions({"product": "ros", "action": "CreateStack"}, context)
    asked = await AliyunApi().check_permissions({"product": "ros", "action": "DeleteStack"}, context)
    assert allowed.behavior == "allow"
    assert allowed.audit is not None
    assert allowed.audit.scope == "settings_rule"
    assert allowed.audit.rule == "ros:CreateStack"
    assert allowed.audit.rule_source == "user_settings"
    assert asked.behavior == "ask"


@pytest.mark.asyncio
async def test_wildcard_allow_rule_does_not_allow_write_api() -> None:
    context = _ctx(allow={"project_settings": ["aliyun_api(ros:*)"]})
    result = await AliyunApi().check_permissions({"product": "ros", "action": "UpdateStack"}, context)
    assert result.behavior == "ask"


@pytest.mark.asyncio
async def test_wildcard_allow_rule_can_match_read_api() -> None:
    context = _ctx(allow={"project_settings": ["aliyun_api(ros:*)"]})
    result = await AliyunApi().check_permissions({"product": "ros", "action": "GetStack"}, context)
    assert result.behavior == "allow"
    assert result.audit is not None
    assert result.audit.scope == "settings_rule"
    assert result.audit.rule == "ros:*"
    assert result.audit.rule_source == "project_settings"


@pytest.mark.asyncio
async def test_deny_and_ask_precedence() -> None:
    context = _ctx(
        allow={"user_settings": ["aliyun_api(ros:CreateStack)"]},
        ask={"project_settings": ["aliyun_api(ros:Create*)"]},
        deny={"local_settings": ["aliyun_api(ros:*)"]},
    )
    result = await AliyunApi().check_permissions({"product": "ros", "action": "CreateStack"}, context)
    assert result.behavior == "deny"
    assert result.audit is not None
    assert result.audit.rule == "ros:*"
    assert result.audit.rule_source == "local_settings"


@pytest.mark.asyncio
async def test_specificity_is_product_first() -> None:
    context = _ctx(ask={"session": ["aliyun_api(ro*:CreateStack)", "aliyun_api(ros:*)"]})
    result = await AliyunApi().check_permissions({"product": "ros", "action": "CreateStack"}, context)
    assert result.behavior == "ask"
    assert result.audit is not None
    assert result.audit.scope == "session_rule"
    assert result.audit.rule == "ros:*"


@pytest.mark.asyncio
async def test_same_source_equal_specificity_prefers_later_rule() -> None:
    context = _ctx(
        ask={
            "user_settings": [
                "aliyun_api(ro*:CreateStack)",
                "aliyun_api(r*s:CreateStack)",
            ]
        }
    )

    result = await AliyunApi().check_permissions({"product": "ros", "action": "CreateStack"}, context)

    assert result.behavior == "ask"
    assert result.audit is not None
    assert result.audit.rule == "r*s:CreateStack"


@pytest.mark.parametrize(
    ("behavior", "rules_key"),
    [("allow", "allow"), ("deny", "deny"), ("ask", "ask")],
)
@pytest.mark.asyncio
async def test_cli_action_rules_use_cli_scope(behavior: str, rules_key: str) -> None:
    rules = {"cli_arg": ["aliyun_api(ros:CreateStack)"]}
    context = _ctx(**{rules_key: rules})

    result = await AliyunApi().check_permissions({"product": "ros", "action": "CreateStack"}, context)

    assert result.behavior == behavior
    assert result.audit is not None
    assert result.audit.scope == "cli_rule"
    assert result.audit.rule_source == "cli_arg"
    assert result.audit.rule == "ros:CreateStack"


def test_aliyun_api_disables_blanket_allow() -> None:
    assert AliyunApi().supports_blanket_allow is False


def test_aliyun_api_schema_exposes_all_runtime_methods_and_body_file() -> None:
    properties = AliyunApi().input_schema["properties"]

    assert properties["method"]["enum"] == ["GET", "POST", "PUT", "DELETE", "PATCH", "HEAD", "OPTIONS"]
    assert properties["body_file"]["type"] == "string"


def _canonical_contract(**changes: Any) -> CanonicalWireContract:
    values: dict[str, Any] = {
        "metadata_source": "fresh",
        "product": "Ecs",
        "version": "2014-05-26",
        "action": "DescribeInstances",
        "style": "RPC",
        "method": "POST",
        "pathname": "/",
        "operation_type": "read",
        "auth_type": "AK",
        "signature_scheme": "acs3",
        "transport": "tea",
        "executable": True,
        "unsupported_reasons": (),
        "parameters": (),
        "consumes": ("application/json",),
        "produces": ("application/json",),
        "policy_digest": "fixture-policy",
    }
    values.update(changes)
    return CanonicalWireContract(**values)


class _FakeContractResolver:
    def __init__(self, contract: CanonicalWireContract, *, metadata_contract: CanonicalWireContract | None = None):
        self.contract = contract
        self.metadata_contract = metadata_contract or contract
        self.calls = []

    async def resolve(self, call, *, allow_fallback):
        self.calls.append((call, allow_fallback))
        if call.explicit_overrides:
            return self.contract
        return self.metadata_contract


def _runtime_tool(
    contract: CanonicalWireContract,
    *,
    metadata_contract: CanonicalWireContract | None = None,
    stages: list[str] | None = None,
) -> tuple[AliyunApi, SimpleNamespace]:
    runtime = SimpleNamespace(
        contract_resolver=_FakeContractResolver(contract, metadata_contract=metadata_contract),
        contract_store=ResolvedContractStore(),
        permission_stage_observer=(stages.append if stages is not None else None),
    )
    return AliyunApi(services=runtime), runtime


def _bound_context(
    tool_input: dict[str, Any],
    *,
    tool_name: str = "aliyun_api",
    **changes: Any,
) -> ToolPermissionContext:
    values: dict[str, Any] = {
        "cwd": "/tmp",
        "invocation_binding": InvocationBinding(
            "runtime",
            "session",
            "call",
            tool_name,
            canonical_input_sha256(tool_input),
        ),
    }
    values.update(changes)
    return ToolPermissionContext(**values)


def _expected_public_error(
    code: str,
    tool_input: dict[str, Any],
    *,
    contract: CanonicalWireContract | None = None,
) -> str:
    return public_aliyun_error(
        ApiContractError(code),
        product=contract.product if contract is not None else tool_input.get("product"),
        version=contract.version if contract is not None else tool_input.get("version"),
        action=contract.action if contract is not None else tool_input.get("action"),
        region_id=tool_input.get("region_id"),
    )


@pytest.mark.parametrize(
    (
        "operation_type",
        "style",
        "method",
        "body_source",
        "metadata_source",
        "override_equality",
        "action",
        "expected_behavior",
        "expected_class",
    ),
    [
        ("read", "RPC", "POST", "none", "fresh", None, "DescribeInstances", "allow", "concurrent"),
        ("write", "RPC", "POST", "none", "fresh", None, "CreateInstance", "ask", "serial"),
        ("read", "ROA", "GET", "none", "cache", None, "GetFunction", "allow", "concurrent"),
        ("read", "ROA", "OPTIONS", "none", "stale_cache", None, "GetFunction", "allow", "concurrent"),
        ("read", "ROA", "POST", "none", "fresh", None, "GetFunction", "ask", "serial"),
        ("read", "ROA", "GET", "body", "fresh", None, "GetFunction", "ask", "serial"),
        ("read", "RPC", "POST", "body", "fresh", None, "DescribeInstances", "allow", "concurrent"),
        (None, "RPC", "POST", "none", "explicit_fallback", None, "DescribeInstances", "allow", "concurrent"),
        (None, "RPC", "POST", "none", "explicit_fallback", None, "CreateInstance", "ask", "serial"),
        (None, "ROA", "GET", "none", "explicit_fallback", True, "GetFunction", "allow", "concurrent"),
        (None, "ROA", "OPTIONS", "none", "explicit_fallback", True, "GetFunction", "ask", "serial"),
        ("read", "RPC", "POST", "none", "fresh", True, "DescribeInstances", "allow", "concurrent"),
        ("read", "RPC", "GET", "none", "fresh", False, "DescribeInstances", "ask", "serial"),
    ],
)
@pytest.mark.asyncio
async def test_runtime_permission_classification_matrix(
    operation_type,
    style,
    method,
    body_source,
    metadata_source,
    override_equality,
    action,
    expected_behavior,
    expected_class,
) -> None:
    request_body_type = "json" if body_source == "body" else "none"
    contract = _canonical_contract(
        metadata_source=metadata_source,
        action=action,
        operation_type=operation_type,
        style=style,
        method=method,
        pathname="/functions" if style == "ROA" else "/",
        request_body_type=request_body_type,
    )
    metadata_contract = contract
    tool_input: dict[str, Any] = {
        "product": "ecs" if style == "RPC" else "fc",
        "version": contract.version,
        "action": action,
        "region_id": "cn-hangzhou",
    }
    if body_source == "body":
        tool_input["body"] = {"Name": "business-value"}
    if override_equality is not None:
        tool_input.update(style=style, method=method, pathname=contract.pathname)
        if override_equality is False:
            metadata_contract = _canonical_contract(
                metadata_source=metadata_source,
                action=action,
                operation_type=operation_type,
                style=style,
                method="POST",
                pathname=contract.pathname,
                request_body_type=request_body_type,
            )
    tool, runtime = _runtime_tool(contract, metadata_contract=metadata_contract)

    result = await tool.check_permissions(tool_input, _bound_context(tool_input))

    assert result.behavior == expected_behavior
    assert result.execution_class == expected_class
    assert result.snapshot_id is not None
    assert result.security_digest is not None
    assert result.invocation_binding == _bound_context(tool_input).invocation_binding
    assert result.audit is not None
    assert result.audit.is_read_only is (expected_behavior == "allow")
    assert result.audit.operation["api_version"] == contract.version
    assert result.audit.operation["api_style"] == style
    assert result.audit.operation["http_method"] == method
    assert result.audit.operation["operation_type"] == operation_type
    assert result.audit.operation["metadata_source"] == metadata_source
    if expected_behavior == "ask":
        assert result.reason is not None
        assert result.reason.type == "untrusted_write"
    assert runtime.contract_store.size == 1


_TRUSTED_CLASSIFICATION_CASES = [
    pytest.param(
        operation_type,
        style,
        method,
        body_source,
        metadata_source,
        override_equality,
        id="-".join(
            (
                operation_type,
                style,
                method,
                body_source,
                metadata_source,
                "implicit" if override_equality is None else f"override-{override_equality}",
            )
        ),
    )
    for operation_type, (style, method), body_source, metadata_source, override_equality in product(
        ("read", "readAndWrite"),
        (("RPC", "POST"), ("ROA", "GET"), ("ROA", "OPTIONS"), ("ROA", "POST")),
        ("none", "body", "body_file", "params_body", "formdata"),
        ("fresh", "cache", "stale_cache"),
        (None, True, False),
    )
]


def _classification_body_source(
    body_source: str,
) -> tuple[dict[str, Any], tuple[ParameterMetadata, ...], str]:
    if body_source == "body":
        return {"body": {"Name": "business-value"}}, (), "json"
    if body_source == "body_file":
        return {"body_file": "payload.bin"}, (), "byte"
    if body_source == "params_body":
        parameter = ParameterMetadata("Payload", "body", False, None, None, {"type": "object"}, None, None)
        return {"params": {"Payload": {"Name": "business-value"}}}, (parameter,), "json"
    if body_source == "formdata":
        parameter = ParameterMetadata("TemplateBody", "formData", False, None, None, {"type": "string"}, None, None)
        return {"params": {"TemplateBody": "{}"}}, (parameter,), "formData"
    return {}, (), "none"


@pytest.mark.parametrize(
    ("operation_type", "style", "method", "body_source", "metadata_source", "override_equality"),
    _TRUSTED_CLASSIFICATION_CASES,
)
@pytest.mark.asyncio
async def test_runtime_permission_trusted_classification_cartesian_product(
    operation_type, style, method, body_source, metadata_source, override_equality
) -> None:
    body_input, parameters, request_body_type = _classification_body_source(body_source)
    pathname = "/" if style == "RPC" else "/instances"
    contract = _canonical_contract(
        metadata_source=metadata_source,
        operation_type=operation_type,
        style=style,
        method=method,
        pathname=pathname,
        parameters=parameters,
        request_body_type=request_body_type,
    )
    metadata_contract = contract
    tool_input: dict[str, Any] = {
        "product": "ecs",
        "version": contract.version,
        "action": contract.action,
        "region_id": "cn-hangzhou",
        **body_input,
    }
    if override_equality is not None:
        tool_input.update(style=style, method=method, pathname=pathname)
        if override_equality is False:
            metadata_contract = replace(contract, method="GET" if method != "GET" else "POST")
    tool, _ = _runtime_tool(contract, metadata_contract=metadata_contract)

    result = await tool.check_permissions(tool_input, _bound_context(tool_input))

    expected_read = (
        operation_type == "read"
        and override_equality is not False
        and (style == "RPC" or (method in {"GET", "HEAD", "OPTIONS"} and body_source == "none"))
    )
    assert result.behavior == ("allow" if expected_read else "ask")
    assert result.execution_class == ("concurrent" if expected_read else "serial")
    assert result.audit is not None
    assert result.audit.is_read_only is expected_read
    if not expected_read:
        assert result.reason is not None
        assert result.reason.type == "untrusted_write"


@pytest.mark.parametrize("body_source", ("none", "body", "body_file", "params_body", "formdata"))
@pytest.mark.asyncio
async def test_explicit_fallback_read_and_write_is_serial_for_every_body_source(body_source: str) -> None:
    body_input, parameters, request_body_type = _classification_body_source(body_source)
    contract = _canonical_contract(
        metadata_source="explicit_fallback",
        operation_type="readAndWrite",
        parameters=parameters,
        request_body_type=request_body_type,
    )
    tool_input = {
        "product": "ecs",
        "version": contract.version,
        "action": contract.action,
        "region_id": "cn-hangzhou",
        **body_input,
    }
    tool, _ = _runtime_tool(contract)

    result = await tool.check_permissions(tool_input, _bound_context(tool_input))

    assert result.behavior == "ask"
    assert result.execution_class == "serial"
    assert result.reason is not None and result.reason.type == "untrusted_write"


@pytest.mark.asyncio
async def test_preview_stack_exception_never_bypasses_explicit_fallback_write_classification() -> None:
    contract = _canonical_contract(
        metadata_source="explicit_fallback",
        product="ROS",
        version="2019-09-10",
        action="PreviewStack",
        style="ROA",
        method="DELETE",
        pathname="/stacks/production",
        operation_type=None,
        request_body_type="json",
    )
    tool_input = {
        "product": "ros",
        "version": contract.version,
        "action": contract.action,
        "region_id": "cn-hangzhou",
        "style": "ROA",
        "method": "DELETE",
        "pathname": "/stacks/production",
        "body": {"StackName": "production"},
    }
    tool, _ = _runtime_tool(contract)

    result = await tool.check_permissions(tool_input, _bound_context(tool_input))

    assert result.behavior == "ask"
    assert result.execution_class == "serial"
    assert result.reason is not None and result.reason.type == "untrusted_write"


@pytest.mark.asyncio
async def test_standard_rpc_preview_stack_remains_read_only_during_explicit_fallback() -> None:
    contract = _canonical_contract(
        metadata_source="explicit_fallback",
        product="ROS",
        version="2019-09-10",
        action="PreviewStack",
        style="RPC",
        method="POST",
        pathname="/",
        operation_type=None,
        request_body_type="json",
    )
    tool_input = {
        "product": "ros",
        "version": contract.version,
        "action": contract.action,
        "region_id": "cn-hangzhou",
        "params": {"StackName": "preview"},
    }
    tool, _ = _runtime_tool(contract)

    result = await tool.check_permissions(tool_input, _bound_context(tool_input))

    assert result.behavior == "allow"
    assert result.execution_class == "concurrent"
    assert result.audit is not None and result.audit.is_read_only is True


@pytest.mark.asyncio
async def test_snapshot_capacity_exhaustion_denies_with_stable_public_error() -> None:
    contract = _canonical_contract()
    store = ResolvedContractStore(max_entries=1)
    tool_input = {
        "product": contract.product,
        "version": contract.version,
        "action": contract.action,
        "region_id": "cn-hangzhou",
    }
    digest = contract.security_digest(aliyun_api_module._runtime_call_shape(tool_input, contract=contract))
    first_binding = InvocationBinding("runtime", "session", "first", "aliyun_api", canonical_input_sha256(tool_input))
    first = store.create(
        binding=first_binding,
        contract=contract,
        security_digest=digest,
        execution_class="concurrent",
    )
    second_binding = replace(first_binding, tool_use_id="second")
    second = store.create(
        binding=second_binding,
        contract=contract,
        security_digest=digest,
        execution_class="concurrent",
    )
    recovery = store.consume(snapshot_id=first, binding=first_binding, security_digest=digest)
    tool, runtime = _runtime_tool(contract)
    runtime.contract_store = store

    result = await tool.check_permissions(tool_input, _bound_context(tool_input))

    assert result.behavior == "deny"
    assert result.message == _expected_public_error("snapshot_capacity_exhausted", tool_input)
    assert "snapshot_capacity_exhausted" not in result.message
    store.cancel_recovery(first, recovery.claim_id)
    store.cancel(second)


@pytest.mark.asyncio
async def test_preview_stack_read_exception_allows_public_and_delegated_canonical_paths() -> None:
    from iac_code.tools.cloud.aliyun.runtime import AliyunDelegatedExecutor

    template_parameter = ParameterMetadata("TemplateURL", "formData", False, None, None, {"type": "string"}, None, None)
    contract = _canonical_contract(
        product="ROS",
        version="2019-09-10",
        action="PreviewStack",
        operation_type="readAndWrite",
        parameters=(template_parameter,),
        request_body_type="formData",
    )
    tool, _ = _runtime_tool(contract)
    public_input = {
        "product": "ros",
        "version": contract.version,
        "action": contract.action,
        "region_id": "cn-hangzhou",
        "params": {"TemplateURL": "https://example.com/template.yml"},
    }

    public = await tool.check_permissions(public_input, _bound_context(public_input))

    assert public.behavior == "allow"
    assert public.execution_class == "concurrent"

    outer_input = {
        "template_url": "https://example.com/template.yml",
        "stack_name": "preview",
        "parameters": {},
    }
    delegated = AliyunDelegatedExecutor(tool, action="PreviewStack")
    delegated_result = await delegated.check_permissions(
        outer_input,
        _bound_context(outer_input, tool_name="ros_preview_template"),
    )
    assert delegated_result.behavior == "allow"
    assert delegated_result.execution_class == "concurrent"
    assert tool._runtime_services.contract_resolver.calls[-1][0].region_id == "cn-hangzhou"


@pytest.mark.asyncio
async def test_preview_stack_read_exception_does_not_bypass_public_pipeline_guard() -> None:
    contract = _canonical_contract(
        product="ROS",
        version="2019-09-10",
        action="PreviewStack",
        operation_type="readAndWrite",
        parameters=(ParameterMetadata("TemplateURL", "formData", False, None, None, {"type": "string"}, None, None),),
        request_body_type="formData",
    )
    tool, runtime = _runtime_tool(contract)
    tool_input = {
        "product": "ros",
        "version": contract.version,
        "action": contract.action,
        "region_id": "cn-hangzhou",
        "params": {"TemplateURL": "https://example.com/template.yml"},
    }

    result = await tool.check_permissions(tool_input, _bound_context(tool_input, pipeline_mode=True))

    assert result.behavior == "deny"
    assert "must use the dedicated ros_preview_template tool" in result.message
    assert runtime.contract_resolver.calls == []


@pytest.mark.asyncio
async def test_preview_stack_read_exception_does_not_bypass_file_permission_before_openmeta(tmp_path) -> None:
    contract = _canonical_contract(
        product="ROS",
        version="2019-09-10",
        action="PreviewStack",
        operation_type="readAndWrite",
    )
    stages: list[str] = []
    tool, runtime = _runtime_tool(contract, stages=stages)
    outside = tmp_path / "outside" / "template.yml"
    outside.parent.mkdir()
    outside.write_text("ROSTemplateFormatVersion: '2015-09-01'", encoding="utf-8")
    project = tmp_path / "project"
    project.mkdir()
    tool_input = {
        "product": "ros",
        "version": contract.version,
        "action": contract.action,
        "region_id": "cn-hangzhou",
        "params": {"TemplateURL": str(outside)},
    }
    context = _bound_context(
        tool_input,
        cwd=str(project),
        strict_read_directories=[str(project)],
        read_path_violation_behavior="deny",
    )

    result = await tool.check_permissions(tool_input, context)

    assert result.behavior == "deny"
    assert stages == ["local_input", "pipeline_guard", "file_permission"]
    assert runtime.contract_resolver.calls == []


@pytest.mark.parametrize("failure", ["body", "override"])
@pytest.mark.asyncio
async def test_preview_stack_read_exception_requires_valid_body_and_matching_overrides(failure: str) -> None:
    parameters = (ParameterMetadata("TemplateURL", "formData", False, None, None, {"type": "string"}, None, None),)
    metadata_contract = _canonical_contract(
        product="ROS",
        version="2019-09-10",
        action="PreviewStack",
        operation_type="readAndWrite",
        parameters=parameters,
        request_body_type="formData" if failure == "override" else "none",
    )
    contract = metadata_contract
    tool_input = {
        "product": "ros",
        "version": contract.version,
        "action": contract.action,
        "region_id": "cn-hangzhou",
        "params": {"TemplateURL": "https://example.com/template.yml"},
    }
    if failure == "override":
        tool_input["method"] = "GET"
        contract = replace(metadata_contract, method="GET")
    tool, _ = _runtime_tool(contract, metadata_contract=metadata_contract)

    result = await tool.check_permissions(tool_input, _bound_context(tool_input))

    assert result.behavior == "ask"
    assert result.execution_class == "serial"
    assert result.reason is not None and result.reason.type == "untrusted_write"


@pytest.mark.asyncio
async def test_preview_stack_read_exception_requires_public_and_delegated_bindings_before_openmeta() -> None:
    from iac_code.tools.cloud.aliyun.runtime import AliyunDelegatedExecutor

    contract = _canonical_contract(
        product="ROS",
        version="2019-09-10",
        action="PreviewStack",
        operation_type="readAndWrite",
    )
    tool, runtime = _runtime_tool(contract)
    public_input = {
        "product": "ros",
        "version": contract.version,
        "action": contract.action,
        "region_id": "cn-hangzhou",
    }
    forged_public = _bound_context(public_input)
    assert forged_public.invocation_binding is not None
    forged_public = replace(
        forged_public,
        invocation_binding=replace(forged_public.invocation_binding, canonical_input_sha256="0" * 64),
    )

    public = await tool.check_permissions(public_input, forged_public)
    delegated_input = {
        "template_url": "https://example.com/template.yml",
        "stack_name": "preview",
        "parameters": {},
    }
    delegated = await AliyunDelegatedExecutor(tool, action="PreviewStack").check_permissions(
        delegated_input,
        _bound_context(delegated_input, tool_name="aliyun_api"),
    )

    assert public.behavior == "deny"
    assert public.message == _expected_public_error("aliyun_public_binding_required", public_input)
    assert delegated.behavior == "deny"
    assert delegated.message == _expected_public_error(
        "aliyun_delegated_outer_binding_required",
        {"product": "ros", "action": "PreviewStack"},
    )
    assert runtime.contract_resolver.calls == []


@pytest.mark.asyncio
async def test_preview_stack_local_template_sentinel_does_not_override_canonical_action(tmp_path) -> None:
    from iac_code.tools.cloud.aliyun.runtime import AliyunDelegatedExecutor

    template = tmp_path / "template.yml"
    template.write_text("ROSTemplateFormatVersion: '2015-09-01'", encoding="utf-8")
    contract = _canonical_contract(
        product="ROS",
        version="2019-09-10",
        action="CreateStack",
        operation_type="write",
        parameters=(ParameterMetadata("TemplateBody", "formData", False, None, None, {"type": "string"}, None, None),),
        request_body_type="formData",
    )
    tool, runtime = _runtime_tool(contract)
    delegated_input = {
        "template_url": str(template),
        "stack_name": "preview",
        "parameters": {},
    }

    result = await AliyunDelegatedExecutor(tool, action="PreviewStack").check_permissions(
        delegated_input,
        _bound_context(delegated_input, tool_name="ros_preview_template", cwd=str(tmp_path)),
    )

    assert result.behavior == "ask"
    assert result.execution_class == "serial"
    assert len(runtime.contract_resolver.calls) == 1


@pytest.mark.asyncio
async def test_runtime_permission_local_stages_precede_openmeta_and_deny_short_circuits(tmp_path) -> None:
    stages: list[str] = []
    tool, runtime = _runtime_tool(_canonical_contract(), stages=stages)
    outside = tmp_path / "outside.bin"
    outside.write_bytes(b"payload")
    tool_input = {
        "product": "ecs",
        "version": "2014-05-26",
        "action": "DescribeInstances",
        "region_id": "cn-hangzhou",
        "body_file": str(outside),
    }
    context = _bound_context(
        tool_input,
        cwd=str(tmp_path / "project"),
        strict_read_directories=[str(tmp_path / "project")],
        read_path_violation_behavior="deny",
        deny_rules={"local_settings": ["aliyun_api(ecs:DescribeInstances)"]},
    )

    result = await tool.check_permissions(tool_input, context)

    assert result.behavior == "deny"
    assert stages == ["local_input", "pipeline_guard", "file_permission"]
    assert runtime.contract_resolver.calls == []
    assert runtime.contract_store.size == 0


@pytest.mark.parametrize(
    ("field", "value", "error"),
    [
        ("action", "Describe/Instances", "invalid_action"),
        ("version", "2014/05/26", "invalid_version"),
        ("region_id", "https://region.invalid", "invalid_region_id"),
        ("endpoint", "https://ecs.cn-hangzhou.aliyuncs.com", "invalid_endpoint"),
        ("style", "GraphQL", "invalid_style"),
        ("method", "TRACE", "invalid_method"),
        ("pathname", "https://host.invalid/path", "invalid_pathname"),
        ("content_type", "not-a-media-type", "invalid_content_type"),
        ("body_file", "", "invalid_body_file"),
        ("params", None, "invalid_tool_input"),
        ("params_body", {"Payload": "value"}, "invalid_tool_input"),
        ("formdata", {"TemplateBody": "{}"}, "invalid_tool_input"),
    ],
)
@pytest.mark.asyncio
async def test_runtime_permission_rejects_malformed_wire_fields_before_openmeta(field, value, error) -> None:
    stages: list[str] = []
    tool, runtime = _runtime_tool(_canonical_contract(), stages=stages)
    tool_input = {
        "product": "ecs",
        "version": "2014-05-26",
        "action": "DescribeInstances",
        "region_id": "cn-hangzhou",
        field: value,
    }

    result = await tool.check_permissions(tool_input, _bound_context(tool_input))

    assert result.behavior == "deny"
    assert result.message == _expected_public_error(error, tool_input)
    assert stages == ["local_input"]
    assert runtime.contract_resolver.calls == []


@pytest.mark.asyncio
async def test_runtime_permission_validates_model_schema_before_normalization(monkeypatch) -> None:
    order: list[str] = []
    tool, _runtime = _runtime_tool(_canonical_contract())
    original_validate = tool.validate_input
    original_normalize = aliyun_api_module._normalize_runtime_input

    def observe_validate(tool_input):
        order.append("schema")
        return original_validate(tool_input)

    def observe_normalize(tool_input, *, allow_internal_shape=False, allow_arbitrary_json_body=False):
        order.append("normalize")
        return original_normalize(
            tool_input,
            allow_internal_shape=allow_internal_shape,
            allow_arbitrary_json_body=allow_arbitrary_json_body,
        )

    monkeypatch.setattr(tool, "validate_input", observe_validate)
    monkeypatch.setattr(aliyun_api_module, "_normalize_runtime_input", observe_normalize)
    tool_input = {
        "product": "ecs",
        "version": "2014-05-26",
        "action": "DescribeInstances",
        "region_id": "cn-hangzhou",
    }

    await tool.check_permissions(tool_input, _bound_context(tool_input))

    assert order[:2] == ["schema", "normalize"]


@pytest.mark.parametrize(
    ("source", "field", "value", "error"),
    [
        ("body", "body", {"nested": {1: "value"}}, "invalid_body"),
        ("params_body", "params", {1: {"Payload": "value"}}, "invalid_params"),
        ("formdata", "params", {"TemplateBody": {1: "value"}}, "invalid_params"),
    ],
)
@pytest.mark.asyncio
async def test_runtime_permission_rejects_non_json_body_source_shapes_before_openmeta(
    source, field, value, error
) -> None:
    del source
    stages: list[str] = []
    tool, runtime = _runtime_tool(_canonical_contract(), stages=stages)
    tool_input = {
        "product": "ecs",
        "version": "2014-05-26",
        "action": "DescribeInstances",
        "region_id": "cn-hangzhou",
        field: value,
    }

    result = await tool.check_permissions(tool_input, _bound_context(tool_input))

    assert result.behavior == "deny"
    assert result.message == _expected_public_error(error, tool_input)
    assert stages == ["local_input"]
    assert runtime.contract_resolver.calls == []


@pytest.mark.asyncio
async def test_runtime_permission_merges_file_rule_and_cloud_asks_once_with_snapshot(tmp_path) -> None:
    stages: list[str] = []
    body_file = tmp_path / "outside" / "payload.bin"
    body_file.parent.mkdir()
    body_file.write_bytes(b"payload")
    contract = _canonical_contract(
        action="CreateInstance",
        operation_type="write",
        request_body_type="byte",
    )
    tool, runtime = _runtime_tool(contract, stages=stages)
    tool_input = {
        "product": "ecs",
        "version": contract.version,
        "action": contract.action,
        "region_id": "cn-hangzhou",
        "body_file": str(body_file),
    }
    project = tmp_path / "project"
    project.mkdir()
    context = _bound_context(
        tool_input,
        cwd=str(project),
        ask_rules={"project_settings": ["aliyun_api(ecs:CreateInstance)"]},
    )

    result = await tool.check_permissions(tool_input, context)

    assert result.behavior == "ask"
    assert result.snapshot_id is not None
    assert result.reasons is not None
    assert [reason.type for reason in result.reasons] == ["path_constraint", "rule", "untrusted_write"]
    assert result.message.count("Allow") == 1
    assert stages == ["local_input", "pipeline_guard", "file_permission", "local_rules", "openmeta"]
    assert len(runtime.contract_resolver.calls) == 1
    assert runtime.contract_store.size == 1


@pytest.mark.asyncio
async def test_runtime_permission_pipeline_preserves_sanitized_audit_for_each_merged_subcheck(tmp_path) -> None:
    body_file = tmp_path / "outside" / "private-payload.bin"
    body_file.parent.mkdir()
    body_file.write_bytes(b"private-body-value")
    contract = _canonical_contract(
        action="CreateInstance",
        operation_type="write",
        request_body_type="byte",
    )
    tool, runtime = _runtime_tool(contract)
    tool_input = {
        "product": "ecs",
        "version": contract.version,
        "action": contract.action,
        "region_id": "cn-hangzhou",
        "params": {"Name": "business-parameter-value"},
        "body_file": str(body_file),
    }
    project = tmp_path / "project"
    project.mkdir()
    context = _bound_context(
        tool_input,
        cwd=str(project),
        ask_rules={
            "project_settings": [
                "aliyun_api(ecs:CreateInstance)",
                "aliyun_api",
            ]
        },
    )

    result = await check_tool_permission(tool, tool_input, context)

    assert result.behavior == "ask"
    assert result.message.count("Allow") == 1
    assert result.reasons is not None
    assert [reason.type for reason in result.reasons] == ["path_constraint", "rule", "untrusted_write"]
    assert [item.reason_type for item in result.audit_items] == ["path_constraint", "rule", "untrusted_write"]
    assert 0 < len(result.audit_items) <= 8
    assert result.audit is not None
    assert result.audit.rule == "aliyun_api"
    assert result.snapshot_id is not None
    assert result.security_digest is not None
    assert result.execution_class == "serial"
    assert len(result.suggestions or []) == 1
    assert runtime.contract_store.size == 1

    serialized_audit = json.dumps([vars(item) for item in result.audit_items], sort_keys=True)
    assert str(body_file) not in serialized_audit
    assert "private-body-value" not in serialized_audit
    assert "business-parameter-value" not in serialized_audit
    for item in result.audit_items:
        assert not ({"path", "pathname", "query", "header", "body", "body_file"} & item.operation.keys())


@pytest.mark.asyncio
async def test_global_bypass_cannot_auto_allow_sensitive_body_file_cloud_write(tmp_path) -> None:
    project = tmp_path / "project"
    project.mkdir()
    body_file = project / ".env"
    body_file.write_bytes(b"SECRET=fake")
    contract = _canonical_contract(
        action="CreateInstance",
        operation_type="write",
        request_body_type="byte",
    )
    tool, runtime = _runtime_tool(contract)
    tool_input = {
        "product": "ecs",
        "version": contract.version,
        "action": contract.action,
        "region_id": "cn-hangzhou",
        "body_file": str(body_file),
    }
    context = _bound_context(
        tool_input,
        cwd=str(project),
        mode=PermissionMode.BYPASS_PERMISSIONS,
    )

    result = await check_tool_permission(tool, tool_input, context)

    assert result.behavior == "ask"
    assert result.reasons is not None
    assert [reason.type for reason in result.reasons] == ["safety_check", "untrusted_write"]
    assert result.snapshot_id is not None
    assert len(runtime.contract_resolver.calls) == 1
    assert runtime.contract_store.size == 1


@pytest.mark.asyncio
async def test_global_bypass_cannot_auto_allow_out_of_project_body_file_cloud_write(tmp_path) -> None:
    project = tmp_path / "project"
    outside = tmp_path / "outside"
    project.mkdir()
    outside.mkdir()
    body_file = outside / "payload.bin"
    body_file.write_bytes(b"payload")
    contract = _canonical_contract(
        action="CreateInstance",
        operation_type="write",
        request_body_type="byte",
    )
    tool, runtime = _runtime_tool(contract)
    tool_input = {
        "product": "ecs",
        "version": contract.version,
        "action": contract.action,
        "region_id": "cn-hangzhou",
        "body_file": str(body_file),
    }
    context = _bound_context(
        tool_input,
        cwd=str(project),
        mode=PermissionMode.BYPASS_PERMISSIONS,
    )

    result = await check_tool_permission(tool, tool_input, context)

    assert result.behavior == "ask"
    assert result.reasons is not None
    assert [reason.type for reason in result.reasons] == ["path_constraint", "untrusted_write"]
    assert result.snapshot_id is not None
    assert len(runtime.contract_resolver.calls) == 1
    assert runtime.contract_store.size == 1


@pytest.mark.parametrize(
    ("action", "tool_name", "tool_input"),
    [
        ("ValidateTemplate", "ros_validate_template", {"template_url": 123}),
        ("ValidateTemplate", "ros_validate_template", {"template_url": None}),
        (
            "ValidateTemplate",
            "ros_validate_template",
            {"template_url": "https://example.com/template.yml", "region_id": None},
        ),
        (
            "GetTemplateParameterConstraints",
            "ros_get_template_parameter_constraints",
            {"template_url": "https://example.com/template.yml", "parameters": None},
        ),
        (
            "PreviewStack",
            "ros_preview_template",
            {
                "template_url": "https://example.com/template.yml",
                "stack_name": None,
                "parameters": {},
            },
        ),
        (
            "PreviewStack",
            "ros_preview_template",
            {
                "template_url": "https://example.com/template.yml",
                "stack_name": "preview",
                "parameters": None,
            },
        ),
        (
            "GetTemplateEstimateCost",
            "ros_estimate_template_cost",
            {"template_url": "https://example.com/template.yml", "parameters": None},
        ),
    ],
)
@pytest.mark.asyncio
async def test_delegated_model_schema_rejects_explicit_nulls_and_wrong_types_before_openmeta(
    action: str,
    tool_name: str,
    tool_input: dict[str, Any],
) -> None:
    from iac_code.tools.cloud.aliyun.runtime import AliyunDelegatedExecutor

    contract = _canonical_contract(
        product="ROS",
        version="2019-09-10",
        action=action,
        operation_type="read",
    )
    tool, runtime = _runtime_tool(contract)
    delegated = AliyunDelegatedExecutor(tool, action=action)

    result = await delegated.check_permissions(
        tool_input,
        _bound_context(tool_input, tool_name=tool_name, pipeline_mode=True),
    )

    assert result.behavior == "deny"
    assert result.message == _expected_public_error(
        "invalid_tool_input",
        {"product": "ros", "action": action, "region_id": tool_input.get("region_id")},
    )
    assert runtime.contract_resolver.calls == []
    assert runtime.contract_store.size == 0


@pytest.mark.asyncio
async def test_runtime_permission_requires_outer_binding_before_openmeta() -> None:
    tool, runtime = _runtime_tool(_canonical_contract())
    tool_input = {
        "product": "ecs",
        "version": "2014-05-26",
        "action": "DescribeInstances",
        "region_id": "cn-hangzhou",
    }

    result = await tool.check_permissions(tool_input, ToolPermissionContext(cwd="/tmp"))

    assert result.behavior == "deny"
    assert result.message == _expected_public_error("aliyun_invocation_binding_required", tool_input)
    assert runtime.contract_resolver.calls == []


@pytest.mark.parametrize("forgery", ["tool_name", "input_hash"])
@pytest.mark.asyncio
async def test_runtime_permission_rejects_forged_public_binding_before_openmeta(forgery: str) -> None:
    tool, runtime = _runtime_tool(_canonical_contract())
    tool_input = {
        "product": "ecs",
        "version": "2014-05-26",
        "action": "DescribeInstances",
        "region_id": "cn-hangzhou",
    }
    context = _bound_context(tool_input)
    if forgery == "tool_name":
        context.invocation_binding = replace(context.invocation_binding, tool_name="ros_validate_template")
    else:
        context.invocation_binding = replace(context.invocation_binding, canonical_input_sha256="0" * 64)

    result = await tool.check_permissions(tool_input, context)

    assert result.behavior == "deny"
    assert result.message == _expected_public_error("aliyun_public_binding_required", tool_input)
    assert runtime.contract_resolver.calls == []
    assert runtime.contract_store.size == 0


class _FakeRequestBuilder:
    def __init__(self, calls: list[str]) -> None:
        self.calls = calls
        self.inputs = []

    async def build(self, contract, tool_input):
        self.calls.append("request_builder_call")
        self.inputs.append(tool_input)
        body_file = tool_input.get("body_file")
        return BuiltApiRequest(
            method=contract.method,
            raw_path=contract.pathname.encode("ascii"),
            canonical_query=(),
            headers=MappingProxyType({}),
            body=body_file if isinstance(body_file, bytes) else None,
            response_policy=ResponseBodyPolicy("json", 1024, ()),
            host_values=MappingProxyType({}),
        )


class _FakeEndpointResolver:
    def __init__(self, calls: list[str]) -> None:
        self.calls = calls

    async def resolve(self, contract, region_id, credential, *, host_values=None, explicit_endpoint=None):
        del explicit_endpoint
        self.calls.append("location_network")
        return EndpointResolution("ecs.cn-hangzhou.aliyuncs.com", "location", None)


class _FakeHostBindingResolver:
    def bind(self, contract, endpoint, host_template, host_values):
        return endpoint


class _FakeTransportRouter:
    def __init__(self, calls: list[str]) -> None:
        self.calls = calls
        self.budgets: list[RetryBudget] = []
        self.requests = []

    def prepare(self, **kwargs):
        async def execute(*, budget):
            self.calls.append("target_product_network")
            self.budgets.append(budget)
            self.requests.append(kwargs["request"])
            return NormalizedApiResponse(
                status=200,
                headers=MappingProxyType({"x-acs-request-id": "request"}),
                body={"RequestId": "request", "Business": "value"},
                content_type="application/json",
                content_encoding=None,
                size=43,
            )

        return SimpleNamespace(execute=execute)


def _execution_runtime(
    contract: CanonicalWireContract,
    *,
    stages: list[str] | None = None,
    calls: list[str] | None = None,
    contract_store: ResolvedContractStore | None = None,
) -> tuple[AliyunApi, SimpleNamespace]:
    calls = calls if calls is not None else []
    budget = RetryBudget(deadline=time.monotonic() + 60)

    async def credential_provider():
        calls.append("credential_network")
        return SimpleNamespace(mode="AK", access_key_id="fake-id", access_key_secret="fake-secret")

    runtime = SimpleNamespace(
        contract_resolver=_FakeContractResolver(contract),
        contract_store=contract_store or ResolvedContractStore(),
        permission_stage_observer=None,
        execution_stage_observer=(stages.append if stages is not None else None),
        request_builder=_FakeRequestBuilder(calls),
        credential_provider=credential_provider,
        endpoint_resolver=_FakeEndpointResolver(calls),
        host_binding_resolver=_FakeHostBindingResolver(),
        transport_router=_FakeTransportRouter(calls),
        retry_budget_factory=lambda: budget,
    )
    return AliyunApi(services=runtime), runtime


def _execution_context(permission_context: ToolPermissionContext, permission) -> ToolContext:
    return ToolContext(
        cwd=permission_context.cwd,
        tool_use_id="call",
        pipeline_mode=permission_context.pipeline_mode,
        additional_directories=list(permission_context.additional_directories),
        trusted_read_directories=list(permission_context.trusted_read_directories),
        relative_read_directories=list(permission_context.relative_read_directories),
        strict_read_directories=list(permission_context.strict_read_directories),
        read_path_violation_behavior=permission_context.read_path_violation_behavior,
        invocation_binding=permission.invocation_binding,
        snapshot_id=permission.snapshot_id,
        security_digest=permission.security_digest,
        execution_class=permission.execution_class,
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("tool_name", "action", "outer_input"),
    [
        (
            "ros_validate_template",
            "ValidateTemplate",
            {"template_url": "https://example.com/template.yml", "region_id": "cn-hangzhou"},
        ),
        (
            "ros_get_template_parameter_constraints",
            "GetTemplateParameterConstraints",
            {
                "template_url": "https://example.com/template.yml",
                "region_id": "cn-hangzhou",
                "parameters": {},
            },
        ),
        (
            "ros_preview_template",
            "PreviewStack",
            {
                "template_url": "https://example.com/template.yml",
                "region_id": "cn-hangzhou",
                "stack_name": "preview",
                "parameters": {},
            },
        ),
        (
            "ros_estimate_template_cost",
            "GetTemplateEstimateCost",
            {
                "template_url": "https://example.com/template.yml",
                "region_id": "cn-hangzhou",
                "parameters": {},
            },
        ),
    ],
)
async def test_delegated_ros_tools_propagate_business_body_and_internal_http_metadata(
    tool_name: str,
    action: str,
    outer_input: dict[str, Any],
) -> None:
    from iac_code.tools.cloud.aliyun.runtime import AliyunDelegatedExecutor

    contract = _canonical_contract(
        product="ROS",
        version="2019-09-10",
        action=action,
        operation_type="read",
    )
    public_tool, _runtime = _execution_runtime(contract)
    delegated = AliyunDelegatedExecutor(public_tool, action=action)
    permission_context = _bound_context(outer_input, tool_name=tool_name, pipeline_mode=True)

    permission = await delegated.check_permissions(outer_input, permission_context)
    assert permission.behavior == "allow"
    result = await delegated.execute(outer_input, _execution_context(permission_context, permission))

    assert result.is_error is False
    assert json.loads(result.content) == {"RequestId": "request", "Business": "value"}
    assert result.metadata is not None
    http_metadata = result.metadata[ALIYUN_HTTP_METADATA_KEY]
    assert http_metadata["contract_version"] == ALIYUN_BODY_CONTRACT_VERSION
    assert http_metadata["product"] == "ROS"
    assert http_metadata["action"] == action
    assert http_metadata["status"] == 200
    assert http_metadata["body_format"] == "json"


async def _approved_execution_context(
    tool: AliyunApi,
    tool_input: dict[str, Any],
    *,
    tool_name: str = "aliyun_api",
    pipeline_mode: bool = False,
) -> tuple[ToolPermissionContext, ToolContext]:
    permission_context = _bound_context(tool_input, tool_name=tool_name, pipeline_mode=pipeline_mode)
    permission = await tool.check_permissions(tool_input, permission_context)
    assert permission.behavior == "allow"
    return permission_context, _execution_context(permission_context, permission)


class _RaisingRequestBuilder:
    def __init__(self, error: ApiContractError) -> None:
        self.error = error

    async def build(self, contract, tool_input):
        del contract, tool_input
        raise self.error


@pytest.mark.parametrize(
    ("code", "expected"),
    [
        (
            "metadata_not_found",
            "Alibaba Cloud API Ecs/2014-05-26/DescribeInstances was not found. Check the product, version, and action.",
        ),
        (
            "metadata_unavailable",
            "Alibaba Cloud API metadata for Ecs/DescribeInstances is temporarily unavailable; try again later.",
        ),
        (
            "invalid_or_missing_version",
            "No valid Alibaba Cloud API version is available for Ecs; provide an explicit version.",
        ),
        (
            "missing_required_parameters:InstanceId",
            "Alibaba Cloud API Ecs/DescribeInstances requires parameter InstanceId.",
        ),
        (
            "unresolved_path_parameter",
            "Alibaba Cloud API path parameters are invalid or incomplete for Ecs/DescribeInstances.",
        ),
        (
            "endpoint_unavailable",
            "No trusted Alibaba Cloud endpoint is available for Ecs/DescribeInstances in cn-hangzhou. "
            "Check the region or endpoint configuration.",
        ),
        (
            "invalid_host_label",
            "Alibaba Cloud host parameters are invalid for Ecs/DescribeInstances in cn-hangzhou.",
        ),
        (
            "aliyun_credentials_required",
            "Alibaba Cloud credentials are unavailable. Configure credentials and retry Ecs/DescribeInstances.",
        ),
        (
            "security_requires_unsupported_scheme",
            "Alibaba Cloud authentication is unsupported for Ecs/DescribeInstances. "
            "Check the API contract or choose an API that supports AccessKey authentication.",
        ),
        (
            "unsupported_signature_scheme",
            "Alibaba Cloud signing is unsupported for Ecs/DescribeInstances. Check the API contract.",
        ),
        (
            "content_type_mismatch",
            "Alibaba Cloud API Ecs/DescribeInstances content_type does not match the request body. "
            "Use a compatible media type.",
        ),
        (
            "snapshot_digest_mismatch",
            "Alibaba Cloud API authorization expired or changed. "
            "Run Ecs/DescribeInstances again to approve the current contract.",
        ),
    ],
)
@pytest.mark.asyncio
async def test_runtime_maps_internal_failures_to_actionable_public_errors(code: str, expected: str) -> None:
    contract = _canonical_contract()
    tool, runtime = _execution_runtime(contract)
    tool_input = {
        "product": "ecs",
        "version": contract.version,
        "action": contract.action,
        "region_id": "cn-hangzhou",
    }
    _permission_context, context = await _approved_execution_context(tool, tool_input)
    runtime.request_builder = _RaisingRequestBuilder(ApiContractError(code))

    result = await tool.execute(tool_input=tool_input, context=context)

    assert result == ToolResult.error(expected)
    assert code not in result.content


@pytest.mark.asyncio
async def test_parameter_type_error_names_safe_context_and_types_without_business_value() -> None:
    contract = _canonical_contract(
        parameters=(
            ParameterMetadata(
                name="Name",
                location="query",
                required=False,
                style=None,
                path_encoding=None,
                schema={"type": "string"},
                description=None,
                example=None,
            ),
        )
    )
    tool, runtime = _execution_runtime(contract)
    runtime.request_builder = RequestBuilder()
    tool_input = {
        "product": "ecs",
        "version": contract.version,
        "action": contract.action,
        "region_id": "cn-hangzhou",
        "params": {"Name": 424242},
    }
    _permission_context, context = await _approved_execution_context(tool, tool_input)

    result = await tool.execute(tool_input=tool_input, context=context)

    assert result == ToolResult.error(
        "Alibaba Cloud API Ecs/DescribeInstances parameter Name expects string but received integer."
    )
    assert "424242" not in result.content


@pytest.mark.asyncio
async def test_runtime_contract_error_uses_gettext_public_boundary(monkeypatch) -> None:
    contract = _canonical_contract()
    tool, runtime = _execution_runtime(contract)
    tool_input = {
        "product": "ecs",
        "version": contract.version,
        "action": contract.action,
        "region_id": "cn-hangzhou",
    }
    _permission_context, context = await _approved_execution_context(tool, tool_input)
    runtime.request_builder = _RaisingRequestBuilder(ApiContractError("content_type_mismatch"))
    monkeypatch.setattr(
        "iac_code.tools.cloud.aliyun.public_errors._",
        lambda message: "translated::" + message,
    )

    result = await tool.execute(tool_input=tool_input, context=context)

    assert result.is_error is True
    assert result.content.startswith("translated::")
    assert "content_type_mismatch" not in result.content


@pytest.mark.asyncio
async def test_delegated_local_template_is_materialized_only_after_contract_consumption(tmp_path) -> None:
    from iac_code.tools.cloud.aliyun.runtime import AliyunDelegatedExecutor

    template = tmp_path / "template.yml"
    template_body = (
        "ROSTemplateFormatVersion: '2015-09-01'\n"
        "Resources:\n"
        "  Wait:\n"
        "    Type: ALIYUN::ROS::Sleep\n"
        "    Properties:\n"
        "      Triggers: {Zones: {Fn::GetAZs: ''}}\n"
    )
    template.write_bytes(template_body.encode("utf-8"))
    contract = _canonical_contract(
        product="ROS",
        version="2019-09-10",
        action="ValidateTemplate",
    )
    stages: list[str] = []
    tool, runtime = _execution_runtime(contract, stages=stages)
    delegated = AliyunDelegatedExecutor(tool, action="ValidateTemplate")
    outer_input = {"template_url": str(template), "region_id": "cn-hangzhou"}
    permission_context = _bound_context(
        outer_input,
        tool_name="ros_validate_template",
        cwd=str(tmp_path),
        pipeline_mode=True,
    )

    permission = await delegated.check_permissions(outer_input, permission_context)
    assert permission.behavior == "allow"
    context = _execution_context(permission_context, permission)
    result = await delegated.execute(outer_input, context)

    assert result.is_error is False
    assert context.ros_preflight_outcome is not None
    assert context.ros_preflight_outcome.report.warning_count == 1
    builder_input = runtime.request_builder.inputs[0]
    assert builder_input["params"]["TemplateBody"] == template_body
    assert "TemplateURL" not in builder_input["params"]
    assert stages.index("contract") < stages.index("materialize") < stages.index("request_builder")


@pytest.mark.asyncio
async def test_invalid_inline_ros_template_rejects_approved_handoff_before_credentials() -> None:
    contract = _canonical_contract(
        product="ROS",
        version="2019-09-10",
        action="ValidateTemplate",
        operation_type="read",
    )
    tool, runtime = _execution_runtime(contract)
    calls: list[str] = []
    runtime.default_region_provider = lambda: calls.append("default-region") or "cn-hangzhou"
    runtime.credential_provider = lambda: calls.append("credential")

    tool_input = {
        "product": "ros",
        "version": "2019-09-10",
        "action": "ValidateTemplate",
        "params": {"TemplateBody": "ROSTemplateFormatVersion: 2015-09-01\nResources: ["},
        "region_id": "cn-hangzhou",
    }
    _, context = await _approved_execution_context(tool, tool_input)
    calls.clear()

    result = await tool.execute(tool_input=tool_input, context=context)

    assert result.is_error
    assert "ROS1001" in result.content
    assert calls == []
    assert runtime.contract_store.size == 0


@pytest.mark.asyncio
async def test_non_string_inline_ros_template_rejects_approved_handoff_before_credentials() -> None:
    contract = _canonical_contract(
        product="ROS",
        version="2019-09-10",
        action="ValidateTemplate",
        operation_type="read",
    )
    tool, runtime = _execution_runtime(contract)
    calls: list[str] = []
    runtime.default_region_provider = lambda: calls.append("default-region") or "cn-hangzhou"
    runtime.credential_provider = lambda: calls.append("credential")

    tool_input = {
        "product": "ros",
        "version": "2019-09-10",
        "action": "ValidateTemplate",
        "params": {"TemplateBody": 123},
        "region_id": "cn-hangzhou",
    }
    _, context = await _approved_execution_context(tool, tool_input)
    calls.clear()

    result = await tool.execute(tool_input=tool_input, context=context)

    assert result.is_error
    assert result.content.count("ROS local validation failed") == 1
    assert "ROS local preflight diagnostics" not in result.content
    assert calls == []
    assert runtime.contract_store.size == 0


@pytest.mark.asyncio
async def test_missing_ros_template_source_rejects_approved_handoff_before_credentials() -> None:
    contract = _canonical_contract(
        product="ROS",
        version="2019-09-10",
        action="ValidateTemplate",
        operation_type="read",
    )
    tool, runtime = _execution_runtime(contract)
    calls: list[str] = []
    runtime.default_region_provider = lambda: calls.append("default-region") or "cn-hangzhou"
    runtime.credential_provider = lambda: calls.append("credential")

    tool_input = {
        "product": "ros",
        "version": "2019-09-10",
        "action": "ValidateTemplate",
        "params": {},
        "region_id": "cn-hangzhou",
    }
    _, context = await _approved_execution_context(tool, tool_input)
    calls.clear()

    result = await tool.execute(tool_input=tool_input, context=context)

    assert result.is_error
    assert result.content.count("ROS local validation failed") == 1
    assert "ROS1201" in result.content
    assert "ROS local preflight diagnostics" not in result.content
    assert calls == []
    assert runtime.contract_store.size == 0


@pytest.mark.asyncio
async def test_invalid_inline_ros_template_cannot_bypass_public_binding_validation() -> None:
    valid_input = {
        "product": "ros",
        "version": "2019-09-10",
        "action": "ValidateTemplate",
        "params": {"TemplateBody": "ROSTemplateFormatVersion: '2015-09-01'\nResources: {}\n"},
        "region_id": "cn-hangzhou",
    }
    invalid_input = {
        **valid_input,
        "params": {"TemplateBody": "ROSTemplateFormatVersion: 2015-09-01\nResources: ["},
    }
    tool, runtime = _execution_runtime(
        _canonical_contract(
            product="ROS",
            version="2019-09-10",
            action="ValidateTemplate",
            operation_type="read",
        )
    )
    _, context = await _approved_execution_context(tool, valid_input)

    result = await tool.execute(tool_input=invalid_input, context=context)

    assert result == ToolResult.error(_expected_public_error("aliyun_invocation_binding_mismatch", invalid_input))
    assert "ROS1001" not in result.content
    assert runtime.contract_store.size == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("template_url_kind", ("relative", "absolute"))
async def test_delegated_local_template_materializes_from_symlinked_logical_cwd(
    tmp_path,
    template_url_kind: str,
) -> None:
    from iac_code.tools.cloud.aliyun.runtime import AliyunDelegatedExecutor

    physical_root = tmp_path / "mount-root" / "oss" / "bucket"
    physical_cwd = physical_root / "ctx-1"
    physical_templates = physical_cwd / "templates"
    physical_templates.mkdir(parents=True)
    logical_root = tmp_path / "workspace"
    try:
        logical_root.symlink_to(physical_root, target_is_directory=True)
    except OSError as error:
        pytest.skip(f"directory symlinks are unavailable: {error}")
    logical_cwd = logical_root / "ctx-1"

    template_body = "ROSTemplateFormatVersion: '2015-09-01'\nResources: {}\n"
    (physical_templates / "template.yml").write_bytes(template_body.encode("utf-8"))
    template_url = (
        "templates/template.yml" if template_url_kind == "relative" else str(logical_cwd / "templates" / "template.yml")
    )
    contract = _canonical_contract(
        product="ROS",
        version="2019-09-10",
        action="ValidateTemplate",
    )
    stages: list[str] = []
    tool, runtime = _execution_runtime(contract, stages=stages)
    delegated = AliyunDelegatedExecutor(tool, action="ValidateTemplate")
    outer_input = {"template_url": template_url, "region_id": "cn-hangzhou"}
    permission_context = _bound_context(
        outer_input,
        tool_name="ros_validate_template",
        cwd=str(logical_cwd),
        pipeline_mode=True,
    )

    permission = await delegated.check_permissions(outer_input, permission_context)
    assert permission.behavior == "allow"
    result = await delegated.execute(outer_input, _execution_context(permission_context, permission))

    assert result.is_error is False
    builder_input = runtime.request_builder.inputs[0]
    assert builder_input["params"]["TemplateBody"] == template_body
    assert "TemplateURL" not in builder_input["params"]
    assert stages.index("contract") < stages.index("materialize") < stages.index("request_builder")


@pytest.mark.asyncio
async def test_delegated_local_template_materializes_from_authorized_path_alias(tmp_path: Path) -> None:
    from iac_code.tools.cloud.aliyun.runtime import AliyunDelegatedExecutor

    project = tmp_path / "project"
    project.mkdir()
    physical_temp = tmp_path / "private" / "tmp"
    physical_temp.mkdir(parents=True)
    logical_temp = tmp_path / "tmp"
    try:
        logical_temp.symlink_to(physical_temp, target_is_directory=True)
    except OSError as error:
        pytest.skip(f"directory symlinks are unavailable: {error}")
    template_body = "ROSTemplateFormatVersion: '2015-09-01'\nResources: {}\n"
    physical_template = physical_temp / "template.yml"
    physical_template.write_bytes(template_body.encode("utf-8"))
    logical_template = logical_temp / physical_template.name
    contract = _canonical_contract(
        product="ROS",
        version="2019-09-10",
        action="ValidateTemplate",
    )
    tool, runtime = _execution_runtime(contract)
    delegated = AliyunDelegatedExecutor(tool, action="ValidateTemplate")
    outer_input = {"template_url": str(logical_template), "region_id": "cn-hangzhou"}
    permission_context = _bound_context(
        outer_input,
        tool_name="ros_validate_template",
        cwd=str(project),
        pipeline_mode=True,
    )

    permission = await delegated.check_permissions(outer_input, permission_context)
    assert permission.behavior == "ask"
    result = await delegated.execute(outer_input, _execution_context(permission_context, permission))

    assert result.is_error is False
    assert runtime.request_builder.inputs[0]["params"]["TemplateBody"] == template_body


@pytest.mark.asyncio
async def test_delegated_local_template_uses_symlink_target_approved_in_snapshot(tmp_path) -> None:
    from iac_code.tools.cloud.aliyun.runtime import AliyunDelegatedExecutor

    approved_body = "ROSTemplateFormatVersion: '2015-09-01'\nResources: {}\n"
    template = tmp_path / "template.yml"
    template.write_bytes(approved_body.encode("utf-8"))
    replacement = tmp_path / "replacement.yml"
    replacement.write_bytes(b"ROSTemplateFormatVersion: '2015-09-01'\nDescription: replacement\n")
    symlink = tmp_path / "template-link.yml"
    symlink.symlink_to(template)
    contract = _canonical_contract(
        product="ROS",
        version="2019-09-10",
        action="ValidateTemplate",
    )
    stages: list[str] = []
    tool, runtime = _execution_runtime(contract, stages=stages)
    delegated = AliyunDelegatedExecutor(tool, action="ValidateTemplate")
    outer_input = {"template_url": str(symlink), "region_id": "cn-hangzhou"}
    permission_context = _bound_context(
        outer_input,
        tool_name="ros_validate_template",
        cwd=str(tmp_path),
        pipeline_mode=True,
    )
    permission = await delegated.check_permissions(outer_input, permission_context)
    assert permission.behavior == "allow"
    symlink.unlink()
    symlink.symlink_to(replacement)

    result = await delegated.execute(outer_input, _execution_context(permission_context, permission))

    assert result.is_error is False
    assert runtime.request_builder.inputs[0]["params"]["TemplateBody"] == approved_body
    assert stages.index("contract") < stages.index("materialize") < stages.index("request_builder")


@pytest.mark.asyncio
async def test_delegated_local_template_rejects_parent_symlink_swap_at_materialization(tmp_path) -> None:
    from iac_code.tools.cloud.aliyun.runtime import AliyunDelegatedExecutor

    approved_dir = tmp_path / "approved"
    approved_dir.mkdir()
    template = approved_dir / "template.yml"
    template.write_text("Resources: {}\n", encoding="utf-8")
    outside_dir = tmp_path / "outside"
    outside_dir.mkdir()
    (outside_dir / template.name).write_text("Secret: outside\n", encoding="utf-8")
    contract = _canonical_contract(
        product="ROS",
        version="2019-09-10",
        action="ValidateTemplate",
    )
    tool, runtime = _execution_runtime(contract)
    delegated = AliyunDelegatedExecutor(tool, action="ValidateTemplate")
    outer_input = {"template_url": str(template), "region_id": "cn-hangzhou"}
    permission_context = _bound_context(
        outer_input,
        tool_name="ros_validate_template",
        cwd=str(tmp_path),
        pipeline_mode=True,
    )
    permission = await delegated.check_permissions(outer_input, permission_context)
    assert permission.behavior == "allow"

    def swap_parent(stage: str) -> None:
        if stage != "materialize":
            return
        approved_dir.rename(tmp_path / "approved-original")
        try:
            approved_dir.symlink_to(outside_dir, target_is_directory=True)
        except OSError as error:
            pytest.skip(f"directory symlinks are unavailable: {error}")

    runtime.execution_stage_observer = swap_parent
    result = await delegated.execute(outer_input, _execution_context(permission_context, permission))

    assert result == ToolResult.error(
        _expected_public_error("invalid_body_file", {"region_id": "cn-hangzhou"}, contract=contract)
    )
    assert runtime.request_builder.inputs == []


@pytest.mark.asyncio
async def test_body_file_symlink_is_materialized_from_approved_physical_path(tmp_path) -> None:
    target = tmp_path / "payload.bin"
    target.write_bytes(b"approved-payload")
    symlink = tmp_path / "payload-link.bin"
    symlink.symlink_to(target)
    contract = _canonical_contract(request_body_type="byte")
    calls: list[str] = []
    tool, runtime = _execution_runtime(contract, calls=calls)
    tool_input = {
        "product": "ecs",
        "version": contract.version,
        "action": contract.action,
        "region_id": "cn-hangzhou",
        "body_file": str(symlink),
    }
    permission_context = _bound_context(tool_input, cwd=str(tmp_path))
    permission = await tool.check_permissions(tool_input, permission_context)
    assert permission.behavior == "allow"

    result = await tool.execute(tool_input=tool_input, context=_execution_context(permission_context, permission))

    assert result.is_error is False
    assert "request_builder_call" in calls
    assert runtime.transport_router.requests[0].body == b"approved-payload"


@pytest.mark.asyncio
async def test_body_file_rejects_parent_symlink_swap_at_materialization(tmp_path) -> None:
    approved_dir = tmp_path / "approved"
    approved_dir.mkdir()
    body_file = approved_dir / "payload.bin"
    body_file.write_bytes(b"approved-payload")
    outside_dir = tmp_path / "outside"
    outside_dir.mkdir()
    (outside_dir / body_file.name).write_bytes(b"outside-payload")
    contract = _canonical_contract(request_body_type="byte")
    tool, runtime = _execution_runtime(contract)
    tool_input = {
        "product": "ecs",
        "version": contract.version,
        "action": contract.action,
        "region_id": "cn-hangzhou",
        "body_file": str(body_file),
    }
    permission_context = _bound_context(tool_input, cwd=str(tmp_path))
    permission = await tool.check_permissions(tool_input, permission_context)
    assert permission.behavior == "allow"

    def swap_parent(stage: str) -> None:
        if stage != "materialize":
            return
        approved_dir.rename(tmp_path / "approved-original")
        try:
            approved_dir.symlink_to(outside_dir, target_is_directory=True)
        except OSError as error:
            pytest.skip(f"directory symlinks are unavailable: {error}")

    runtime.execution_stage_observer = swap_parent
    result = await tool.execute(tool_input=tool_input, context=_execution_context(permission_context, permission))

    assert result == ToolResult.error(_expected_public_error("invalid_body_file", tool_input, contract=contract))
    assert runtime.request_builder.inputs == []
    assert runtime.transport_router.requests == []


@pytest.mark.asyncio
async def test_body_file_materialized_bytes_are_the_target_request_body(tmp_path) -> None:
    body_file = tmp_path / "payload.bin"
    body_file.write_bytes(b"approved-payload")
    contract = _canonical_contract(request_body_type="byte")
    tool, runtime = _execution_runtime(contract)
    tool_input = {
        "product": "ecs",
        "version": contract.version,
        "action": contract.action,
        "region_id": "cn-hangzhou",
        "body_file": str(body_file),
    }
    permission_context = _bound_context(tool_input, cwd=str(tmp_path))
    permission = await tool.check_permissions(tool_input, permission_context)
    assert permission.behavior == "allow"

    result = await tool.execute(tool_input=tool_input, context=_execution_context(permission_context, permission))

    assert result.is_error is False
    assert runtime.transport_router.requests[0].body == b"approved-payload"


@pytest.mark.asyncio
async def test_body_file_is_read_once_and_real_request_builder_receives_only_materialized_bytes(
    tmp_path, monkeypatch
) -> None:
    body_file = tmp_path / "payload.bin"
    body_file.write_bytes(b"approved-payload")
    contract = _canonical_contract(
        request_body_type="byte",
        consumes=("application/octet-stream",),
    )
    tool, runtime = _execution_runtime(contract)
    runtime.request_builder = RequestBuilder()
    materialization_reads: list[Path] = []
    real_read = aliyun_api_module._read_body_file

    def materialize_once(path: Path) -> bytes:
        materialization_reads.append(path)
        return real_read(path)

    def reject_builder_reread(path: Path) -> bytes:
        raise AssertionError(f"request builder reread body_file: {path}")

    monkeypatch.setattr(aliyun_api_module, "_read_body_file", materialize_once)
    monkeypatch.setattr(api_contract_module, "_read_body_file", reject_builder_reread)
    tool_input = {
        "product": "ecs",
        "version": contract.version,
        "action": contract.action,
        "region_id": "cn-hangzhou",
        "body_file": str(body_file),
    }
    permission_context = _bound_context(tool_input, cwd=str(tmp_path))
    permission = await tool.check_permissions(tool_input, permission_context)
    assert permission.behavior == "allow"

    result = await tool.execute(tool_input=tool_input, context=_execution_context(permission_context, permission))

    assert result.is_error is False
    assert materialization_reads == [body_file]
    assert runtime.transport_router.requests[0].body == b"approved-payload"


@pytest.mark.asyncio
async def test_approved_file_ask_passes_repeated_execution_authorization(tmp_path) -> None:
    project = tmp_path / "project"
    project.mkdir()
    body_file = tmp_path / "payload.bin"
    body_file.write_bytes(b"approved-payload")
    contract = _canonical_contract(operation_type="write", request_body_type="byte")
    tool, runtime = _execution_runtime(contract)
    tool_input = {
        "product": "ecs",
        "version": contract.version,
        "action": contract.action,
        "region_id": "cn-hangzhou",
        "body_file": str(body_file),
    }
    permission_context = _bound_context(tool_input, cwd=str(project))
    permission = await tool.check_permissions(tool_input, permission_context)
    assert permission.behavior == "ask"

    result = await tool.execute(tool_input=tool_input, context=_execution_context(permission_context, permission))

    assert result.is_error is False
    assert runtime.transport_router.requests[0].body == b"approved-payload"


@pytest.mark.asyncio
async def test_capacity_evicted_snapshot_re_resolves_identical_contract_and_executes_once() -> None:
    store = ResolvedContractStore(max_entries=1)
    contract = _canonical_contract()
    tool_input = {
        "product": "ecs",
        "version": contract.version,
        "action": contract.action,
        "region_id": "cn-hangzhou",
    }
    tool, runtime = _execution_runtime(contract, contract_store=store)
    permission_context, context = await _approved_execution_context(tool, tool_input)
    assert permission_context.invocation_binding is not None
    assert context.security_digest is not None
    evicting_id = store.create(
        binding=replace(permission_context.invocation_binding, tool_use_id="other-call"),
        contract=contract,
        security_digest=context.security_digest,
        execution_class="serial",
    )

    result = await tool.execute(tool_input=tool_input, context=context)

    assert result.is_error is False
    assert len(runtime.contract_resolver.calls) == 2
    assert len(runtime.transport_router.requests) == 1
    store.cancel(evicting_id)


@pytest.mark.asyncio
async def test_expired_snapshot_re_resolves_and_requires_the_same_digest() -> None:
    now = [100.0]
    store = ResolvedContractStore(ttl_seconds=1.0, clock=lambda: now[0])
    contract = _canonical_contract()
    tool_input = {
        "product": "ecs",
        "version": contract.version,
        "action": contract.action,
        "region_id": "cn-hangzhou",
    }
    tool, runtime = _execution_runtime(contract, contract_store=store)
    _, context = await _approved_execution_context(tool, tool_input)
    now[0] += 2.0

    result = await tool.execute(tool_input=tool_input, context=context)

    assert result.is_error is False
    assert len(runtime.contract_resolver.calls) == 2


@pytest.mark.asyncio
async def test_expired_snapshot_preserves_the_authorized_body_file_target(tmp_path) -> None:
    now = [100.0]
    store = ResolvedContractStore(ttl_seconds=1.0, clock=lambda: now[0])
    approved = tmp_path / "approved.bin"
    approved.write_bytes(b"approved-payload")
    replacement = tmp_path / "replacement.bin"
    replacement.write_bytes(b"replacement-payload")
    body_file = tmp_path / "payload-link.bin"
    body_file.symlink_to(approved)
    contract = _canonical_contract(request_body_type="byte")
    tool, runtime = _execution_runtime(contract, contract_store=store)
    tool_input = {
        "product": "ecs",
        "version": contract.version,
        "action": contract.action,
        "region_id": "cn-hangzhou",
        "body_file": str(body_file),
    }
    permission_context = _bound_context(tool_input, cwd=str(tmp_path))
    permission = await tool.check_permissions(tool_input, permission_context)
    assert permission.behavior == "allow"
    now[0] += 2.0
    body_file.unlink()
    body_file.symlink_to(replacement)

    result = await tool.execute(tool_input=tool_input, context=_execution_context(permission_context, permission))

    assert result.is_error is False
    assert runtime.transport_router.requests[0].body == b"approved-payload"
    assert len(runtime.contract_resolver.calls) == 2


@pytest.mark.parametrize(
    ("loss", "drift", "expected_error"),
    [
        ("capacity", "digest", "snapshot_digest_mismatch"),
        ("expiry", "digest", "snapshot_digest_mismatch"),
        ("capacity", "execution_class", "snapshot_execution_class_mismatch"),
        ("expiry", "execution_class", "snapshot_execution_class_mismatch"),
    ],
)
@pytest.mark.asyncio
async def test_snapshot_recovery_denies_digest_or_execution_class_drift_once(
    monkeypatch,
    loss: str,
    drift: str,
    expected_error: str,
) -> None:
    now = [100.0]
    store = ResolvedContractStore(max_entries=1, ttl_seconds=1.0, clock=lambda: now[0])
    contract = _canonical_contract()
    tool_input = {
        "product": "ecs",
        "version": contract.version,
        "action": contract.action,
        "region_id": "cn-hangzhou",
    }
    tool, runtime = _execution_runtime(contract, contract_store=store)
    permission_context, context = await _approved_execution_context(tool, tool_input)
    evicting_id: str | None = None
    if loss == "capacity":
        assert permission_context.invocation_binding is not None
        assert context.security_digest is not None
        evicting_id = store.create(
            binding=replace(permission_context.invocation_binding, tool_use_id="other-call"),
            contract=contract,
            security_digest=context.security_digest,
            execution_class="serial",
        )
    else:
        now[0] += 2.0

    if drift == "digest":
        drifted_contract = replace(contract, method="GET")
        runtime.contract_resolver.contract = drifted_contract
        runtime.contract_resolver.metadata_contract = drifted_contract
    else:
        monkeypatch.setattr(aliyun_api_module, "_runtime_is_read_only", lambda *_args, **_kwargs: False)

    result = await tool.execute(tool_input=tool_input, context=context)

    assert result == ToolResult.error(
        _expected_public_error(expected_error, tool_input, contract=runtime.contract_resolver.contract)
    )
    assert len(runtime.contract_resolver.calls) == 2
    assert runtime.transport_router.requests == []
    replay = await tool.execute(tool_input=tool_input, context=context)
    assert replay == ToolResult.error(_expected_public_error("snapshot_not_found", tool_input))
    assert len(runtime.contract_resolver.calls) == 2
    assert runtime.transport_router.requests == []
    if evicting_id is not None:
        store.cancel(evicting_id)


@pytest.mark.parametrize("terminalizer", ["cancel", "reject"])
@pytest.mark.asyncio
async def test_cancelled_or_rejected_snapshot_cannot_re_resolve_or_execute(terminalizer: str) -> None:
    store = ResolvedContractStore()
    contract = _canonical_contract()
    tool_input = {
        "product": "ecs",
        "version": contract.version,
        "action": contract.action,
        "region_id": "cn-hangzhou",
    }
    tool, runtime = _execution_runtime(contract, contract_store=store)
    _, context = await _approved_execution_context(tool, tool_input)
    assert context.snapshot_id is not None
    getattr(store, terminalizer)(context.snapshot_id)

    result = await tool.execute(tool_input=tool_input, context=context)
    replay = await tool.execute(tool_input=tool_input, context=context)

    assert result == ToolResult.error(_expected_public_error("snapshot_not_found", tool_input))
    assert replay == ToolResult.error(_expected_public_error("snapshot_not_found", tool_input))
    assert len(runtime.contract_resolver.calls) == 1
    assert runtime.transport_router.requests == []


@pytest.mark.parametrize("contender_binding", ["matching", "forged"])
@pytest.mark.asyncio
async def test_concurrent_snapshot_recovery_claim_allows_exactly_one_execution(contender_binding: str) -> None:
    store = ResolvedContractStore(max_entries=1)
    contract = _canonical_contract()
    tool_input = {
        "product": "ecs",
        "version": contract.version,
        "action": contract.action,
        "region_id": "cn-hangzhou",
    }
    tool, runtime = _execution_runtime(contract, contract_store=store)
    permission_context, context = await _approved_execution_context(tool, tool_input)
    assert permission_context.invocation_binding is not None
    assert context.security_digest is not None
    evicting_id = store.create(
        binding=replace(permission_context.invocation_binding, tool_use_id="other-call"),
        contract=contract,
        security_digest=context.security_digest,
        execution_class="serial",
    )
    entered = asyncio.Event()
    release = asyncio.Event()
    original_resolve = runtime.contract_resolver.resolve

    async def blocking_resolve(call, *, allow_fallback):
        entered.set()
        await release.wait()
        return await original_resolve(call, allow_fallback=allow_fallback)

    runtime.contract_resolver.resolve = blocking_resolve
    first = asyncio.create_task(tool.execute(tool_input=tool_input, context=context))
    second_tool = AliyunApi(services=runtime)
    second_context = context
    if contender_binding == "forged":
        second_context = copy.copy(context)
        assert second_context.invocation_binding is not None
        second_context.invocation_binding = replace(second_context.invocation_binding, runtime_nonce="forged-runtime")
    second: asyncio.Task[ToolResult] | None = None
    try:
        await asyncio.wait_for(entered.wait(), timeout=1)
        second = asyncio.create_task(second_tool.execute(tool_input=tool_input, context=second_context))
        second_result = await asyncio.wait_for(second, timeout=1)
        release.set()
        first_result = await asyncio.wait_for(first, timeout=1)
    finally:
        release.set()
        pending = [task for task in (first, second) if task is not None and not task.done()]
        if pending:
            await asyncio.gather(*pending, return_exceptions=True)
        store.cancel(evicting_id)

    assert sum(not result.is_error for result in (first_result, second_result)) == 1
    expected = ToolResult.error(_expected_public_error("snapshot_not_found", tool_input))
    assert sum(result == expected for result in (first_result, second_result)) == 1
    assert len(runtime.contract_resolver.calls) == 2
    assert len(runtime.transport_router.requests) == 1


@pytest.mark.parametrize(
    "mutation",
    [
        "runtime_nonce",
        "session_id",
        "tool_use_id",
        "tool_name",
        "canonical_input_sha256",
        "security_digest",
    ],
)
@pytest.mark.asyncio
async def test_expired_public_snapshot_rejects_every_stored_handoff_mutation_without_reresolving(
    mutation: str,
) -> None:
    now = [100.0]
    store = ResolvedContractStore(ttl_seconds=1.0, clock=lambda: now[0])
    contract = _canonical_contract()
    tool_input = {
        "product": "ecs",
        "version": contract.version,
        "action": contract.action,
        "region_id": "cn-hangzhou",
    }
    tool, runtime = _execution_runtime(contract, contract_store=store)
    _, context = await _approved_execution_context(tool, tool_input)
    now[0] += 2.0
    if mutation == "security_digest":
        context.security_digest = "0" * 64
    else:
        assert context.invocation_binding is not None
        replacement = {
            "runtime_nonce": "other-runtime",
            "session_id": "other-session",
            "tool_use_id": "other-call",
            "tool_name": "ros_validate_template",
            "canonical_input_sha256": "0" * 64,
        }[mutation]
        context.invocation_binding = replace(context.invocation_binding, **{mutation: replacement})

    result = await tool.execute(tool_input=tool_input, context=context)

    assert result.is_error is True
    assert len(runtime.contract_resolver.calls) == 1
    assert runtime.transport_router.requests == []


@pytest.mark.asyncio
async def test_expired_serial_snapshot_rejects_forged_concurrent_execution_class() -> None:
    now = [100.0]
    store = ResolvedContractStore(ttl_seconds=1.0, clock=lambda: now[0])
    contract = _canonical_contract(operation_type="write")
    tool_input = {
        "product": "ecs",
        "version": contract.version,
        "action": contract.action,
        "region_id": "cn-hangzhou",
    }
    tool, runtime = _execution_runtime(contract, contract_store=store)
    permission_context = _bound_context(
        tool_input,
        allow_rules={"session": ["aliyun_api(ecs:DescribeInstances)"]},
    )
    permission = await tool.check_permissions(tool_input, permission_context)
    assert permission.behavior == "allow"
    assert permission.execution_class == "serial"
    context = _execution_context(permission_context, permission)
    context.execution_class = "concurrent"
    now[0] += 2.0

    result = await tool.execute(tool_input=tool_input, context=context)

    assert result == ToolResult.error(
        _expected_public_error("snapshot_execution_class_mismatch", tool_input, contract=contract)
    )
    assert runtime.transport_router.requests == []


@pytest.mark.asyncio
async def test_public_runtime_requires_and_consumes_binding_snapshot_digest_once() -> None:
    tool_input = {
        "product": "ecs",
        "version": "2014-05-26",
        "action": "DescribeInstances",
        "region_id": "cn-hangzhou",
    }
    tool, runtime = _execution_runtime(_canonical_contract())
    _, context = await _approved_execution_context(tool, tool_input)

    result = await tool.execute(tool_input=tool_input, context=context)

    assert result.is_error is False
    assert json.loads(result.content)["Business"] == "value"
    assert result.metadata["aliyun_http"] == {
        "contract_version": "aliyun_body_v1",
        "product": "Ecs",
        "version": "2014-05-26",
        "action": "DescribeInstances",
        "status": 200,
        "status_class": "2xx",
        "response_mode": "json",
        "body_format": "json",
        "headers_present": True,
        "body_present": True,
        "content_type_present": True,
        "size_present": True,
        "content_encoding_present": False,
        "headers_nonempty": True,
        "header_count": 1,
    }
    assert runtime.contract_store.size == 0
    assert len(runtime.transport_router.budgets) == 1

    replay = await tool.execute(tool_input=tool_input, context=context)
    assert replay.is_error is True
    assert replay.content == _expected_public_error("snapshot_not_found", tool_input)
    assert len(runtime.contract_resolver.calls) == 1
    assert len(runtime.transport_router.budgets) == 1


@pytest.mark.parametrize("missing", ["invocation_binding", "snapshot_id", "security_digest"])
@pytest.mark.asyncio
async def test_public_runtime_rejects_each_missing_handoff_field(missing: str) -> None:
    tool_input = {
        "product": "ecs",
        "version": "2014-05-26",
        "action": "DescribeInstances",
        "region_id": "cn-hangzhou",
    }
    tool, runtime = _execution_runtime(_canonical_contract())
    _, context = await _approved_execution_context(tool, tool_input)
    setattr(context, missing, None)

    result = await tool.execute(tool_input=tool_input, context=context)

    assert result.is_error is True
    assert result.content == _expected_public_error("aliyun_runtime_handoff_required", tool_input)
    assert runtime.transport_router.budgets == []


@pytest.mark.parametrize("mutation", ["input", "binding", "digest"])
@pytest.mark.asyncio
async def test_public_runtime_rejects_forged_or_changed_handoff(mutation: str) -> None:
    tool_input = {
        "product": "ecs",
        "version": "2014-05-26",
        "action": "DescribeInstances",
        "region_id": "cn-hangzhou",
        "params": {"Name": "approved"},
    }
    tool, runtime = _execution_runtime(_canonical_contract())
    _, context = await _approved_execution_context(tool, tool_input)
    execution_input = tool_input
    if mutation == "input":
        execution_input = {**tool_input, "params": {"Name": "changed"}}
    elif mutation == "binding":
        context.invocation_binding = replace(context.invocation_binding, tool_use_id="forged")
    else:
        context.security_digest = "0" * 64

    result = await tool.execute(tool_input=execution_input, context=context)

    assert result.is_error is True
    assert runtime.transport_router.budgets == []


@pytest.mark.parametrize(
    ("mutation", "expected_error"),
    [
        ("input_hash", "aliyun_invocation_binding_mismatch"),
        ("tool_use_id", "aliyun_invocation_binding_mismatch"),
        ("tool_name", "aliyun_public_binding_required"),
    ],
)
@pytest.mark.asyncio
async def test_public_preconsume_handoff_rejection_invalidates_snapshot(
    mutation: str,
    expected_error: str,
) -> None:
    tool_input = {
        "product": "ecs",
        "version": "2014-05-26",
        "action": "DescribeInstances",
        "region_id": "cn-hangzhou",
    }
    tool, runtime = _execution_runtime(_canonical_contract())
    _, context = await _approved_execution_context(tool, tool_input)
    execution_input = tool_input
    if mutation == "input_hash":
        execution_input = {**tool_input, "params": {"Name": "changed"}}
    elif mutation == "tool_use_id":
        context.tool_use_id = "other-call"
    else:
        assert context.invocation_binding is not None
        context.invocation_binding = replace(context.invocation_binding, tool_name="ros_validate_template")

    result = await tool.execute(tool_input=execution_input, context=context)

    assert result == ToolResult.error(_expected_public_error(expected_error, execution_input))
    assert runtime.contract_store.size == 0


@pytest.mark.asyncio
async def test_public_schema_rejection_invalidates_snapshot() -> None:
    tool_input = {
        "product": "ecs",
        "version": "2014-05-26",
        "action": "DescribeInstances",
        "region_id": "cn-hangzhou",
        "params": None,
    }
    contract = _canonical_contract()
    tool, runtime = _execution_runtime(contract)
    binding = InvocationBinding("runtime", "session", "call", "aliyun_api", canonical_input_sha256(tool_input))
    snapshot_id = runtime.contract_store.create(
        binding=binding,
        contract=contract,
        security_digest="digest",
        execution_class="concurrent",
    )
    context = ToolContext(
        tool_use_id="call",
        invocation_binding=binding,
        snapshot_id=snapshot_id,
        security_digest="digest",
        execution_class="concurrent",
    )

    result = await tool.execute(tool_input=tool_input, context=context)

    assert result == ToolResult.error(_expected_public_error("invalid_tool_input", tool_input))
    assert runtime.contract_store.size == 0


@pytest.mark.asyncio
async def test_public_runtime_observes_authoritative_ten_stages_and_one_budget() -> None:
    stages: list[str] = []
    calls: list[str] = []
    tool_input = {
        "product": "ecs",
        "version": "2014-05-26",
        "action": "DescribeInstances",
        "region_id": "cn-hangzhou",
    }
    tool, runtime = _execution_runtime(_canonical_contract(), stages=stages, calls=calls)
    _, context = await _approved_execution_context(tool, tool_input)

    result = await tool.execute(tool_input=tool_input, context=context)

    assert result.is_error is False
    assert stages == [
        "normalize_trust",
        "local_authorization",
        "contract",
        "materialize",
        "hooks",
        "request_builder",
        "credential",
        "endpoint",
        "transport",
        "target",
    ]
    assert calls == [
        "request_builder_call",
        "credential_network",
        "location_network",
        "target_product_network",
    ]
    assert len(runtime.transport_router.budgets) == 1


@pytest.mark.asyncio
async def test_real_transport_router_prepares_in_stage_nine_and_stage_ten_only_executes_target() -> None:
    stages: list[str] = []
    budgets: list[RetryBudget] = []

    class RecordingTransport:
        async def execute(self, **kwargs):
            assert stages[-1] == "target"
            budgets.append(kwargs["budget"])
            return NormalizedApiResponse(
                status=200,
                headers=MappingProxyType({}),
                body={"RequestId": "request"},
                content_type="application/json",
                content_encoding=None,
                size=24,
            )

    contract = _canonical_contract()
    router = TransportRouter({"tea": RecordingTransport()})
    original_prepare = router.prepare

    def prepare_in_stage_nine(**kwargs):
        assert stages[-1] == "transport"
        return original_prepare(**kwargs)

    router.prepare = prepare_in_stage_nine  # type: ignore[method-assign]
    tool_input = {
        "product": "ecs",
        "version": contract.version,
        "action": contract.action,
        "region_id": "cn-hangzhou",
    }
    tool, runtime = _execution_runtime(contract, stages=stages)
    runtime.transport_router = router
    _, context = await _approved_execution_context(tool, tool_input)

    result = await tool.execute(tool_input=tool_input, context=context)

    assert result.is_error is False
    assert len(budgets) == 1
    assert budgets[0] is runtime.retry_budget_factory()


@pytest.mark.asyncio
async def test_runtime_emits_private_api_event_and_endpoint_metric_after_success(monkeypatch) -> None:
    events = []
    endpoint_sources = []
    monkeypatch.setattr(
        "iac_code.tools.cloud.aliyun.aliyun_api.emit_aliyun_api_called",
        lambda **metadata: events.append(metadata),
    )
    monkeypatch.setattr(
        "iac_code.tools.cloud.aliyun.aliyun_api.emit_aliyun_endpoint_resolution",
        endpoint_sources.append,
    )
    contract = _canonical_contract(metadata_source="fresh", openmeta_cache_status="memory_fresh")
    tool_input = {
        "product": "ecs",
        "version": contract.version,
        "action": contract.action,
        "region_id": "cn-hangzhou",
    }
    tool, _ = _execution_runtime(contract)
    _, context = await _approved_execution_context(tool, tool_input)

    result = await tool.execute(tool_input=tool_input, context=context)

    assert result.is_error is False
    assert endpoint_sources == ["location"]
    assert events == [
        {
            "metadata_source": "fresh",
            "api_style": "RPC",
            "http_method": "POST",
            "transport": "tea",
            "signature_scheme": "acs3",
            "endpoint_source": "location",
            "host_template_applied": False,
            "contract_override_used": False,
            "openmeta_cache_status": "memory_fresh",
            "outcome": "success",
        }
    ]


@pytest.mark.asyncio
async def test_runtime_contract_error_metric_uses_finite_stage_and_never_reaches_target(monkeypatch) -> None:
    stages = []
    monkeypatch.setattr(
        "iac_code.tools.cloud.aliyun.aliyun_api.emit_aliyun_api_contract_error",
        stages.append,
    )
    tool_input = {
        "product": "",
        "version": "2014-05-26",
        "action": "DescribeInstances",
        "region_id": "cn-hangzhou",
    }
    tool, runtime = _execution_runtime(_canonical_contract())
    context = ToolContext(
        tool_use_id="call",
        invocation_binding=InvocationBinding(
            "runtime",
            "session",
            "call",
            "aliyun_api",
            canonical_input_sha256(tool_input),
        ),
        snapshot_id="unused",
        security_digest="0" * 64,
    )

    result = await tool.execute(tool_input=tool_input, context=context)

    assert result == ToolResult.error(_expected_public_error("invalid_product", tool_input))
    assert stages == ["product"]
    assert runtime.transport_router.requests == []


@pytest.mark.parametrize(
    "failed_stage",
    [
        "normalize_trust",
        "local_authorization",
        "contract",
        "materialize",
        "hooks",
        "request_builder",
        "credential",
        "endpoint",
        "transport",
    ],
)
@pytest.mark.asyncio
async def test_stages_one_through_nine_never_reach_target_transport(failed_stage: str) -> None:
    stages: list[str] = []
    calls: list[str] = []
    tool_input = {
        "product": "ecs",
        "version": "2014-05-26",
        "action": "DescribeInstances",
        "region_id": "cn-hangzhou",
    }
    tool, runtime = _execution_runtime(_canonical_contract(), calls=calls)

    def fail_at(stage: str) -> None:
        stages.append(stage)
        if stage == failed_stage:
            raise RuntimeError("stage_failure")

    runtime.execution_stage_observer = fail_at
    _, context = await _approved_execution_context(tool, tool_input)

    result = await tool.execute(tool_input=tool_input, context=context)

    assert result.is_error is True
    assert stages[-1] == failed_stage
    assert "target_product_network" not in calls


def _runtime_trust_api() -> tuple[Any, Any]:
    import iac_code.tools.cloud.aliyun.runtime as runtime_module

    return (
        getattr(runtime_module, "AliyunDelegatedExecutor"),
        getattr(runtime_module, "bind_aliyun_internal_caller"),
    )


@pytest.mark.asyncio
async def test_delegated_runtime_reuses_outer_triplet_and_rejects_self_created_binding() -> None:
    delegated_executor_type, _ = _runtime_trust_api()
    tool, runtime = _execution_runtime(
        _canonical_contract(
            product="ROS",
            version="2019-09-10",
            action="ValidateTemplate",
            operation_type="read",
        )
    )
    delegated = delegated_executor_type(tool, action="ValidateTemplate")
    outer_input = {
        "template_url": "https://example.com/template.yml",
        "region_id": "cn-hangzhou",
    }
    permission_context = _bound_context(
        outer_input,
        tool_name="ros_validate_template",
        pipeline_mode=True,
    )

    permission = await delegated.check_permissions(outer_input, permission_context)
    assert permission.behavior == "allow"
    assert permission.invocation_binding is permission_context.invocation_binding
    context = ToolContext(
        tool_use_id="call",
        pipeline_mode=True,
        invocation_binding=permission.invocation_binding,
        snapshot_id=permission.snapshot_id,
        security_digest=permission.security_digest,
        execution_class=permission.execution_class,
    )
    result = await delegated.execute(outer_input, context)
    assert result.is_error is False
    assert runtime.contract_store.size == 0

    forged = _bound_context(
        {"product": "ros", "action": "ValidateTemplate"},
        tool_name="aliyun_api",
    )
    denied = await delegated.check_permissions(outer_input, forged)
    assert denied.behavior == "deny"
    expected_delegated_error = _expected_public_error(
        "aliyun_delegated_outer_binding_required",
        {"product": "ros", "action": "ValidateTemplate", "region_id": "cn-hangzhou"},
    )
    assert denied.message == expected_delegated_error
    malformed = await delegated.check_permissions(
        outer_input,
        replace(permission_context, invocation_binding=object()),  # type: ignore[arg-type]
    )
    assert malformed.behavior == "deny"
    assert malformed.message == expected_delegated_error

    direct = await tool.execute_delegated(
        {"product": "ros", "version": "2019-09-10", "action": "ValidateTemplate", "region_id": "cn-hangzhou"},
        outer_input,
        ToolContext(
            tool_use_id="call",
            invocation_binding=replace(
                forged.invocation_binding,
                canonical_input_sha256=canonical_input_sha256(outer_input),
            ),
            snapshot_id="forged",
            security_digest="0" * 64,
        ),
    )
    assert direct == ToolResult.error(expected_delegated_error)


@pytest.mark.parametrize("mutation", ["input_hash", "tool_name", "schema"])
@pytest.mark.asyncio
async def test_delegated_preconsume_rejection_invalidates_snapshot(mutation: str) -> None:
    from iac_code.tools.cloud.aliyun.runtime import AliyunDelegatedExecutor

    contract = _canonical_contract(
        product="ROS",
        version="2019-09-10",
        action="ValidateTemplate",
        operation_type="read",
    )
    tool, runtime = _execution_runtime(contract)
    delegated = AliyunDelegatedExecutor(tool, action="ValidateTemplate")
    approved_input = {"template_url": "https://example.com/template.yml", "region_id": "cn-hangzhou"}
    permission_context = _bound_context(approved_input, tool_name="ros_validate_template", pipeline_mode=True)
    permission = await delegated.check_permissions(approved_input, permission_context)
    assert permission.behavior == "allow"
    context = _execution_context(permission_context, permission)
    execution_input: dict[str, Any] = approved_input
    if mutation == "input_hash":
        execution_input = {**approved_input, "region_id": "cn-shanghai"}
    elif mutation == "tool_name":
        assert context.invocation_binding is not None
        context.invocation_binding = replace(context.invocation_binding, tool_name="ros_create_stack")
    else:
        execution_input = {**approved_input, "template_url": 123}
        assert context.invocation_binding is not None
        context.invocation_binding = replace(
            context.invocation_binding,
            canonical_input_sha256=canonical_input_sha256(execution_input),
        )

    result = await delegated.execute(execution_input, context)

    expected_code = "invalid_tool_input" if mutation == "schema" else "aliyun_delegated_outer_binding_required"
    assert result == ToolResult.error(
        _expected_public_error(
            expected_code,
            {"product": "ros", "action": "ValidateTemplate", "region_id": execution_input.get("region_id")},
        )
    )
    assert runtime.contract_store.size == 0


@pytest.mark.asyncio
async def test_delegated_tool_use_rejection_invalidates_snapshot() -> None:
    from iac_code.tools.cloud.aliyun.runtime import AliyunDelegatedExecutor

    contract = _canonical_contract(
        product="ROS",
        version="2019-09-10",
        action="ValidateTemplate",
        operation_type="read",
    )
    tool, runtime = _execution_runtime(contract)
    delegated = AliyunDelegatedExecutor(tool, action="ValidateTemplate")
    outer_input = {"template_url": "https://example.com/template.yml", "region_id": "cn-hangzhou"}
    permission_context = _bound_context(outer_input, tool_name="ros_validate_template", pipeline_mode=True)
    permission = await delegated.check_permissions(outer_input, permission_context)
    assert permission.behavior == "allow"
    context = _execution_context(permission_context, permission)
    context.tool_use_id = "other-call"

    result = await delegated.execute(outer_input, context)

    assert result == ToolResult.error(
        _expected_public_error(
            "aliyun_invocation_binding_mismatch",
            {"product": "ros", "action": "ValidateTemplate", "region_id": "cn-hangzhou"},
        )
    )
    assert runtime.contract_store.size == 0


@pytest.mark.parametrize("failure_stage", ["outer_input_hash", "call_shape", "tool_context"])
@pytest.mark.parametrize("failure_type", ["exception", "cancellation"])
@pytest.mark.asyncio
async def test_delegated_preconsume_failure_always_invalidates_supplied_snapshot(
    monkeypatch,
    failure_stage: str,
    failure_type: str,
) -> None:
    import iac_code.tools.cloud.aliyun.ros_template_tools as ros_template_tools
    import iac_code.tools.cloud.aliyun.runtime as runtime_module

    contract = _canonical_contract(
        product="ROS",
        version="2019-09-10",
        action="ValidateTemplate",
        operation_type="read",
    )
    tool, runtime = _execution_runtime(contract)
    delegated = runtime_module.AliyunDelegatedExecutor(tool, action="ValidateTemplate")
    outer_input = {"template_url": "https://example.com/template.yml", "region_id": "cn-hangzhou"}
    permission_context = _bound_context(outer_input, tool_name="ros_validate_template", pipeline_mode=True)
    permission = await delegated.check_permissions(outer_input, permission_context)
    assert permission.behavior == "allow"
    context = _execution_context(permission_context, permission)
    assert runtime.contract_store.size == 1

    failure = asyncio.CancelledError() if failure_type == "cancellation" else RuntimeError("preconsume_failure")

    def fail(*args, **kwargs):
        del args, kwargs
        raise failure

    if failure_stage == "outer_input_hash":
        monkeypatch.setattr(runtime_module, "canonical_input_sha256", fail)
    elif failure_stage == "call_shape":
        monkeypatch.setattr(ros_template_tools, "build_delegated_call_shape", fail)
    else:
        monkeypatch.setattr(runtime_module, "_delegated_tool_context", fail)

    expected_error = asyncio.CancelledError if failure_type == "cancellation" else RuntimeError
    with pytest.raises(expected_error):
        await delegated.execute(outer_input, context)

    assert runtime.contract_store.size == 0


@pytest.mark.asyncio
async def test_delegated_success_idempotently_cancels_consumed_snapshot(monkeypatch) -> None:
    from iac_code.tools.cloud.aliyun.runtime import AliyunDelegatedExecutor

    contract = _canonical_contract(
        product="ROS",
        version="2019-09-10",
        action="ValidateTemplate",
        operation_type="read",
    )
    tool, runtime = _execution_runtime(contract)
    delegated = AliyunDelegatedExecutor(tool, action="ValidateTemplate")
    outer_input = {"template_url": "https://example.com/template.yml", "region_id": "cn-hangzhou"}
    permission_context = _bound_context(outer_input, tool_name="ros_validate_template", pipeline_mode=True)
    permission = await delegated.check_permissions(outer_input, permission_context)
    assert permission.behavior == "allow"
    context = _execution_context(permission_context, permission)
    snapshot_id = permission.snapshot_id
    assert isinstance(snapshot_id, str)
    cancel_calls: list[str] = []
    original_cancel = runtime.contract_store.cancel

    def record_cancel(candidate: str) -> None:
        cancel_calls.append(candidate)
        original_cancel(candidate)

    monkeypatch.setattr(runtime.contract_store, "cancel", record_cancel)

    result = await delegated.execute(outer_input, context)

    assert result.is_error is False
    assert cancel_calls == [snapshot_id]
    assert runtime.contract_store.size == 0


@pytest.mark.parametrize(
    "mutation",
    [
        "runtime_nonce",
        "session_id",
        "tool_use_id",
        "tool_name",
        "canonical_input_sha256",
        "security_digest",
    ],
)
@pytest.mark.asyncio
async def test_expired_delegated_snapshot_rejects_every_stored_handoff_mutation_without_reresolving(
    mutation: str,
) -> None:
    from iac_code.tools.cloud.aliyun.runtime import AliyunDelegatedExecutor

    now = [100.0]
    store = ResolvedContractStore(ttl_seconds=1.0, clock=lambda: now[0])
    contract = _canonical_contract(
        product="ROS",
        version="2019-09-10",
        action="ValidateTemplate",
        operation_type="read",
    )
    tool, runtime = _execution_runtime(contract, contract_store=store)
    delegated = AliyunDelegatedExecutor(tool, action="ValidateTemplate")
    outer_input = {"template_url": "https://example.com/template.yml", "region_id": "cn-hangzhou"}
    permission_context = _bound_context(
        outer_input,
        tool_name="ros_validate_template",
        pipeline_mode=True,
    )
    permission = await delegated.check_permissions(outer_input, permission_context)
    assert permission.behavior == "allow"
    context = _execution_context(permission_context, permission)
    now[0] += 2.0
    if mutation == "security_digest":
        context.security_digest = "0" * 64
    else:
        assert context.invocation_binding is not None
        replacement = {
            "runtime_nonce": "other-runtime",
            "session_id": "other-session",
            "tool_use_id": "other-call",
            "tool_name": "aliyun_api",
            "canonical_input_sha256": "0" * 64,
        }[mutation]
        context.invocation_binding = replace(context.invocation_binding, **{mutation: replacement})

    result = await delegated.execute(outer_input, context)

    assert result.is_error is True
    assert len(runtime.contract_resolver.calls) == 1
    assert runtime.transport_router.requests == []


@pytest.mark.asyncio
async def test_internal_caller_is_bound_capability_only_and_rejects_all_cross_path_inputs() -> None:
    _, bind_internal = _runtime_trust_api()
    tool_input = {
        "product": "ecs",
        "version": "2014-05-26",
        "action": "DescribeInstances",
        "region_id": "cn-hangzhou",
    }
    tool, runtime = _execution_runtime(_canonical_contract())
    caller = bind_internal(tool)

    legal = await caller.call(tool_input=tool_input, context=ToolContext())
    assert legal.is_error is False
    assert len(runtime.transport_router.budgets) == 1

    for forged in (False, True, "capability", object(), ToolContext()):
        denied = await caller.call(capability=forged, tool_input=tool_input, context=ToolContext())
        assert denied == ToolResult.error("aliyun_internal_capability_required")
    cross_path = await caller.call(
        tool_input=tool_input,
        context=ToolContext(snapshot_id="snapshot", security_digest="digest"),
    )
    assert cross_path == ToolResult.error("aliyun_internal_handoff_forbidden")
    assert len(runtime.transport_router.budgets) == 1

    direct = await tool.execute_internal(tool_input, ToolContext())
    assert direct == ToolResult.error("aliyun_internal_capability_required")
    assert len(runtime.transport_router.budgets) == 1


@pytest.mark.asyncio
async def test_runtime_services_own_one_delegated_factory_and_bound_internal_caller(tmp_path: Path) -> None:
    from iac_code.tools.cloud.aliyun.runtime import (
        AliyunDelegatedExecutor,
        AliyunInternalCaller,
        create_aliyun_runtime_services,
    )

    services = create_aliyun_runtime_services(cache_dir=tmp_path)
    try:
        first = services.delegated_executor_factory("ValidateTemplate")
        second = services.delegated_executor_factory("CreateStack")

        assert isinstance(first, AliyunDelegatedExecutor)
        assert isinstance(second, AliyunDelegatedExecutor)
        assert first._public_tool is second._public_tool
        assert isinstance(services.internal_caller, AliyunInternalCaller)
    finally:
        await services.aclose()
