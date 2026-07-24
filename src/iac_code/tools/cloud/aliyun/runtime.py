"""Shared Alibaba Cloud runtime services foundation."""

from __future__ import annotations

import asyncio
import random as random_module
import re
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, field, replace
from datetime import datetime
from pathlib import Path
from typing import Any

from iac_code.services.providers.aliyun import DEFAULT_REGION
from iac_code.services.telemetry import add_metric, log_event
from iac_code.services.telemetry.names import ALIYUN_API_TARGET_OUTCOMES, Events, Metrics
from iac_code.tools.base import ToolContext, ToolResult
from iac_code.tools.cloud.aliyun.acs3_transport import (
    Acs1Transport,
    Acs3StreamingTransport,
    TeaTransportAdapter,
    TransportRouter,
)
from iac_code.tools.cloud.aliyun.api_contract import ApiContractResolver, RequestBuilder
from iac_code.tools.cloud.aliyun.contract_store import (
    PROCESS_RESOLVED_CONTRACT_STORE,
    ResolvedContractStore,
    canonical_input_sha256,
)
from iac_code.tools.cloud.aliyun.endpoint_resolver import EndpointResolver, HostBindingResolver
from iac_code.tools.cloud.aliyun.openmeta import MetadataFetch, OpenMetaClient, utc_now
from iac_code.tools.cloud.aliyun.oss_v4_adapter import (
    OssOperationCatalog,
    OssStreamingHttpClient,
    OssV4Adapter,
)
from iac_code.tools.cloud.aliyun.product_resolver import ProductResolution, ProductResolver
from iac_code.tools.cloud.aliyun.public_errors import public_aliyun_error
from iac_code.types.permissions import InvocationBinding, PermissionResult, ToolPermissionContext
from iac_code.utils.async_lifecycle import await_task_to_completion

_USE_BOUND_CAPABILITY = object()
_DELEGATED_ACTIONS = {
    "ros_validate_template": "ValidateTemplate",
    "ros_get_template_parameter_constraints": "GetTemplateParameterConstraints",
    "ros_preview_template": "PreviewStack",
    "ros_estimate_template_cost": "GetTemplateEstimateCost",
}

_METADATA_SOURCES = frozenset({"fresh", "cache", "stale_cache", "explicit_fallback"})
_API_STYLES = frozenset({"RPC", "ROA"})
_HTTP_METHODS = frozenset({"GET", "POST", "PUT", "DELETE", "PATCH", "HEAD", "OPTIONS"})
_TRANSPORTS = frozenset({"tea", "acs1", "acs3_streaming", "oss_v4_sdk"})
_SIGNATURE_SCHEMES = frozenset({"acs1", "acs3", "oss_v4"})
_ENDPOINT_SOURCES = frozenset(
    {"explicit", "override", "location", "catalog_region", "catalog_global", "override_pattern", "error"}
)
_OPENMETA_CACHE_STATUSES = frozenset({"memory_fresh", "disk_fresh", "remote", "disk_stale", "negative_hit", "miss"})
_OPENMETA_OUTCOMES = frozenset({"success", "not_found", "temporarily_unavailable", "protocol_error"})
_DOC_DETAILS = frozenset({"summary", "full"})
_DOC_OUTCOMES = frozenset(
    {
        "success",
        "not_found",
        "temporarily_unavailable",
        "protocol_error",
        "invalid_input",
        "contract_error",
    }
)
_CONTRACT_ERROR_STAGES = frozenset(
    {
        "product",
        "version",
        "api",
        "security",
        "parameter",
        "media_type",
        "endpoint",
        "host",
        "signature",
        "transport",
        "oss_catalog",
    }
)
_SAFE_TELEMETRY_PRODUCT = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,127}$")
_ASCII_WHITESPACE = " \t\n\r\f\v"


def _require_finite_label(value: str, allowed: frozenset[str]) -> str:
    if not isinstance(value, str) or value not in allowed:
        raise ValueError("invalid_aliyun_telemetry_label")
    return value


def emit_aliyun_openmeta_request(outcome: str) -> None:
    add_metric(
        Metrics.ALIYUN_OPENMETA_REQUEST_COUNT,
        1,
        {"outcome": _require_finite_label(outcome, _OPENMETA_OUTCOMES)},
    )


def emit_aliyun_openmeta_cache(status: str) -> None:
    add_metric(
        Metrics.ALIYUN_OPENMETA_CACHE_COUNT,
        1,
        {"status": _require_finite_label(status, _OPENMETA_CACHE_STATUSES)},
    )


def emit_aliyun_api_doc(detail: str, outcome: str) -> None:
    add_metric(
        Metrics.ALIYUN_API_DOC_COUNT,
        1,
        {
            "detail": _require_finite_label(detail, _DOC_DETAILS),
            "outcome": _require_finite_label(outcome, _DOC_OUTCOMES),
        },
    )


def emit_aliyun_endpoint_resolution(source: str) -> None:
    add_metric(
        Metrics.ALIYUN_ENDPOINT_RESOLUTION_COUNT,
        1,
        {"source": _require_finite_label(source, _ENDPOINT_SOURCES)},
    )


def emit_aliyun_api_contract_error(stage: str) -> None:
    add_metric(
        Metrics.ALIYUN_API_CONTRACT_ERROR_COUNT,
        1,
        {"stage": _require_finite_label(stage, _CONTRACT_ERROR_STAGES)},
    )


def emit_aliyun_product_resolution(resolution: ProductResolution) -> None:
    requested_product = resolution.requested_product
    normalized_requested = requested_product.strip(_ASCII_WHITESPACE)
    if len(requested_product) > 140 or _SAFE_TELEMETRY_PRODUCT.fullmatch(normalized_requested) is None:
        requested_product = (
            resolution.normalized_product
            if _SAFE_TELEMETRY_PRODUCT.fullmatch(resolution.normalized_product) is not None
            else "invalid"
        )
    log_event(
        Events.ALIYUN_PRODUCT_RESOLVED,
        {
            "requested_product": requested_product,
            "canonical_product": resolution.canonical_product or "",
            "match_strategy": resolution.strategy,
            "confidence": resolution.confidence,
            "outcome": resolution.outcome,
        },
    )


def emit_aliyun_api_called(
    *,
    metadata_source: str,
    api_style: str,
    http_method: str,
    transport: str,
    signature_scheme: str,
    endpoint_source: str,
    host_template_applied: bool,
    contract_override_used: bool,
    openmeta_cache_status: str,
    outcome: str,
) -> None:
    if not isinstance(host_template_applied, bool) or not isinstance(contract_override_used, bool):
        raise ValueError("invalid_aliyun_telemetry_label")
    log_event(
        Events.ALIYUN_API_CALLED,
        {
            "metadata_source": _require_finite_label(metadata_source, _METADATA_SOURCES),
            "api_style": _require_finite_label(api_style, _API_STYLES),
            "http_method": _require_finite_label(http_method, _HTTP_METHODS),
            "transport": _require_finite_label(transport, _TRANSPORTS),
            "signature_scheme": _require_finite_label(signature_scheme, _SIGNATURE_SCHEMES),
            "endpoint_source": _require_finite_label(endpoint_source, _ENDPOINT_SOURCES),
            "host_template_applied": host_template_applied,
            "contract_override_used": contract_override_used,
            "openmeta_cache_status": _require_finite_label(openmeta_cache_status, _OPENMETA_CACHE_STATUSES),
            "outcome": _require_finite_label(outcome, ALIYUN_API_TARGET_OUTCOMES),
        },
    )


class AliyunDelegatedExecutor:
    """Model-side ROS adapter that reuses the outer permission handoff."""

    def __init__(self, public_tool: Any, *, action: str) -> None:
        self._public_tool = public_tool
        self._action = action

    def _public_error(self, code: str, tool_input: Mapping[str, Any]) -> str:
        return public_aliyun_error(
            code,
            product="ros",
            action=self._action,
            region_id=tool_input.get("region_id"),
        )

    async def check_permissions(
        self,
        tool_input: Mapping[str, Any],
        context: ToolPermissionContext,
    ) -> PermissionResult:
        if not _valid_delegated_binding(tool_input, context.invocation_binding, action=self._action):
            return PermissionResult(
                behavior="deny",
                message=self._public_error("aliyun_delegated_outer_binding_required", tool_input),
            )
        from iac_code.tools.cloud.aliyun.ros_template_tools import (
            build_delegated_call_shape,
            validate_delegated_tool_input,
        )

        if not validate_delegated_tool_input(tool_input, action=self._action):
            return PermissionResult(behavior="deny", message=self._public_error("invalid_tool_input", tool_input))
        shape = build_delegated_call_shape(tool_input, action=self._action)
        return await self._public_tool.check_shape_permissions(shape, replace(context, pipeline_mode=False))

    async def execute(self, tool_input: Mapping[str, Any], context: ToolContext) -> ToolResult:
        try:
            if not _valid_delegated_binding(tool_input, context.invocation_binding, action=self._action):
                return ToolResult.error(self._public_error("aliyun_delegated_outer_binding_required", tool_input))
            from iac_code.tools.cloud.aliyun.ros_template_tools import (
                build_delegated_call_shape,
                validate_delegated_tool_input,
            )

            if not validate_delegated_tool_input(tool_input, action=self._action):
                return ToolResult.error(self._public_error("invalid_tool_input", tool_input))
            shape = build_delegated_call_shape(tool_input, action=self._action)
            delegated_context = _delegated_tool_context(context)
            try:
                return await self._public_tool.execute_delegated(
                    shape,
                    tool_input,
                    delegated_context,
                )
            finally:
                context.ros_preflight_outcome = delegated_context.ros_preflight_outcome
        finally:
            self._public_tool._invalidate_runtime_handoff(context)


class AliyunInternalCaller:
    """Possession-based internal caller whose capability remains closure-held."""

    def __init__(self, invoke: Callable[..., Awaitable[ToolResult]]) -> None:
        self._invoke = invoke

    async def call(
        self,
        *,
        tool_input: Mapping[str, Any],
        context: ToolContext,
        capability: object = _USE_BOUND_CAPABILITY,
    ) -> ToolResult:
        return await self._invoke(capability=capability, tool_input=tool_input, context=context)


def bind_aliyun_internal_caller(public_tool: Any) -> AliyunInternalCaller:
    """Return a caller whose unforgeable token is held only by this closure."""

    expected_capability = object()

    async def invoke(
        *,
        capability: object,
        tool_input: Mapping[str, Any],
        context: ToolContext,
    ) -> ToolResult:
        presented = expected_capability if capability is _USE_BOUND_CAPABILITY else capability
        if presented is not expected_capability:
            return ToolResult.error("aliyun_internal_capability_required")
        if any(
            value is not None
            for value in (
                context.invocation_binding,
                context.snapshot_id,
                context.security_digest,
                context.execution_class,
            )
        ):
            return ToolResult.error("aliyun_internal_handoff_forbidden")
        return await public_tool._execute_internal_trusted(tool_input, context)

    return AliyunInternalCaller(invoke)


def _valid_delegated_binding(tool_input: Mapping[str, Any], binding: Any, *, action: str) -> bool:
    return (
        isinstance(binding, InvocationBinding)
        and _DELEGATED_ACTIONS.get(binding.tool_name) == action
        and binding.canonical_input_sha256 == canonical_input_sha256(tool_input)
    )


def _delegated_tool_context(context: ToolContext) -> ToolContext:
    return ToolContext(
        cwd=context.cwd,
        event_queue=context.event_queue,
        tool_use_id=context.tool_use_id,
        additional_directories=list(context.additional_directories),
        trusted_read_directories=list(context.trusted_read_directories),
        relative_read_directories=list(context.relative_read_directories),
        strict_read_directories=list(context.strict_read_directories),
        read_path_violation_behavior=context.read_path_violation_behavior,
        pipeline_mode=False,
        env_overrides=dict(context.env_overrides),
        telemetry_attributes=dict(context.telemetry_attributes),
        permission_context=context.permission_context,
        invocation_binding=context.invocation_binding,
        snapshot_id=context.snapshot_id,
        security_digest=context.security_digest,
        execution_class=context.execution_class,
        trusted_ros_account_context=context.trusted_ros_account_context,
    )


@dataclass
class AliyunRuntimeServices:
    openmeta: OpenMetaClient
    contract_resolver: ApiContractResolver
    request_builder: RequestBuilder
    endpoint_resolver: EndpointResolver
    host_binding_resolver: HostBindingResolver
    transport_router: TransportRouter
    oss_operation_catalog: OssOperationCatalog
    oss_http_client: OssStreamingHttpClient
    clock: Callable[[], datetime]
    random: Callable[[], float]
    contract_store: ResolvedContractStore = PROCESS_RESOLVED_CONTRACT_STORE
    credential_provider: Callable[[], Any] | None = None
    default_region_provider: Callable[[], str] = lambda: DEFAULT_REGION
    retry_budget_factory: Callable[[], Any] | None = None
    permission_stage_observer: Callable[[str], None] | None = None
    execution_stage_observer: Callable[[str], None] | None = None
    target_outcome_observer: Callable[[Mapping[str, Any]], None] | None = None
    delegated_executor_factory: Callable[[str], AliyunDelegatedExecutor] = field(init=False, repr=False)
    internal_caller: AliyunInternalCaller = field(init=False, repr=False)
    _closed: bool = field(default=False, init=False, repr=False)
    _close_task: asyncio.Task[None] | None = field(default=None, init=False, repr=False)

    def __post_init__(self) -> None:
        from iac_code.tools.cloud.aliyun.aliyun_api import AliyunApi

        public_tool = AliyunApi(services=self)
        self.delegated_executor_factory = lambda action: AliyunDelegatedExecutor(public_tool, action=action)
        self.internal_caller = bind_aliyun_internal_caller(public_tool)

    async def aclose(self) -> None:
        if self._closed:
            return
        task = self._close_task
        if task is None:
            task = asyncio.create_task(self._close_owned_resources())
            self._close_task = task
        try:
            await await_task_to_completion(task)
        finally:
            if task.done() and not self._closed and self._close_task is task:
                self._close_task = None

    async def _close_owned_resources(self) -> None:
        try:
            await self.transport_router.aclose()
        finally:
            await self.openmeta.aclose()
        self._closed = True

    async def run_api_doc_operation(
        self,
        *,
        detail: str,
        operation: Callable[[], Awaitable[Any]],
    ) -> Any:
        if detail not in _DOC_DETAILS:
            emit_aliyun_api_doc("summary", "invalid_input")
            raise ValueError("invalid_api_doc_detail")
        try:
            result = await operation()
        except asyncio.CancelledError:
            raise
        except OSError:
            emit_aliyun_api_doc(detail, "protocol_error")
            raise
        except Exception:
            emit_aliyun_api_doc(detail, "contract_error")
            raise
        outcome = "success"
        if isinstance(result, MetadataFetch) and result.value is None:
            outcome = result.error or "contract_error"
        emit_aliyun_api_doc(detail, outcome)
        return result


def create_aliyun_runtime_services(
    *,
    cache_dir: Path,
    clock: Callable[[], datetime] = utc_now,
    random_fn: Callable[[], float] = random_module.random,
    openmeta_transport: Any | None = None,
) -> AliyunRuntimeServices:
    openmeta = OpenMetaClient(
        cache_dir=cache_dir,
        clock=clock,
        transport=openmeta_transport,
        request_outcome_observer=emit_aliyun_openmeta_request,
        cache_status_observer=emit_aliyun_openmeta_cache,
    )
    product_resolver = ProductResolver(openmeta, observer=emit_aliyun_product_resolution)
    endpoint_resolver = EndpointResolver(cache_dir=cache_dir, clock=clock)
    oss_operation_catalog = OssOperationCatalog.load()
    oss_http_client = OssStreamingHttpClient()
    oss_adapter = OssV4Adapter(
        catalog=oss_operation_catalog,
        http_client=oss_http_client,
        host_binding_resolver=endpoint_resolver.host_binding_resolver,
    )
    transport_router = TransportRouter(
        {
            "tea": TeaTransportAdapter(),
            "acs1": Acs1Transport(),
            "acs3_streaming": Acs3StreamingTransport(),
            "oss_v4_sdk": oss_adapter,
        }
    )
    return AliyunRuntimeServices(
        openmeta=openmeta,
        contract_resolver=ApiContractResolver(
            openmeta,
            oss_catalog=oss_operation_catalog,
            product_resolver=product_resolver,
        ),
        request_builder=RequestBuilder(),
        endpoint_resolver=endpoint_resolver,
        host_binding_resolver=endpoint_resolver.host_binding_resolver,
        transport_router=transport_router,
        oss_operation_catalog=oss_operation_catalog,
        oss_http_client=oss_http_client,
        clock=clock,
        random=random_fn,
    )
