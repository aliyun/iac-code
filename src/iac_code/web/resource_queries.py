"""Bounded, on-demand resource reads. No URLs, account search or model calls.

Plans are built from current server-side facts, not request-supplied API names
or parent attributes. The reader uses the public tool permission/contract path.
"""

from __future__ import annotations

import asyncio
import copy
import inspect
import json
import time
import uuid
from collections import OrderedDict
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any

from iac_code.tools.cloud.resource_references import ResourceSpec, locator_text, resource_spec


class ResourceQueryError(ValueError):
    """A finite, non-secret result code suitable for the resource UI."""


async def _source_current(callback: Callable[[], bool | Awaitable[bool]]) -> bool:
    value = callback()
    return (await value if inspect.isawaitable(value) else value) is True


class ResourceQueryAttempt:
    """Bound active work separately from a user's explicit approval wait."""

    def __init__(
        self, timeout: float, is_current: Callable[[], bool | Awaitable[bool]], *, permission_timeout: float = 300
    ) -> None:
        self.remaining = timeout
        self.is_current = is_current
        self.permission_timeout = permission_timeout
        self.timed_out = False
        self._timer: asyncio.TimerHandle | None = None
        self._started = 0.0

    async def check_current(self) -> None:
        if not await _source_current(self.is_current):
            raise ResourceQueryError("resource_query_source_changed")

    def start(self) -> None:
        loop = asyncio.get_running_loop()
        task = asyncio.current_task()
        assert task is not None
        self._started = loop.time()

        def expire() -> None:
            self.timed_out = True
            task.cancel()

        self._timer = loop.call_later(max(0, self.remaining), expire)

    def stop(self) -> None:
        if self._timer is not None:
            self._timer.cancel()
            self._timer = None
            self.remaining -= asyncio.get_running_loop().time() - self._started

    async def request_permission(self, resolver: Callable[..., Awaitable[bool]], event: Any) -> bool:
        await self.check_current()
        self.stop()
        try:
            allowed = await asyncio.wait_for(resolver(event), timeout=self.permission_timeout)
            await self.check_current()
            return allowed is True
        except asyncio.TimeoutError:
            raise ResourceQueryError("resource_query_permission_timeout") from None
        finally:
            self.start()


async def request_resource_query_permission(manager: Any, session: Any, event: Any) -> bool:
    """Use the existing Web approval UI without an Agent/pipeline continuation.

    The grant belongs only to the live invocation contract. A server restart
    invalidates it; an explicit retry obtains a fresh contract and approval.
    """
    from iac_code.web.permissions import DEFAULT_PERMISSION_CHOICES
    from iac_code.web.runtime import _permission_request_payload

    payload = _permission_request_payload(event, turn_id="", allow_always=False)
    # The shared permission panel displays `command` as plain text. Include
    # the exact fixed API request so the user can inspect this read's target.
    payload["command"] = json.dumps(event.tool_input, ensure_ascii=False, indent=2)
    # Never let suggestions turn this one read into an entire-tool session rule.
    payload.update(choices=[dict(choice) for choice in DEFAULT_PERMISSION_CHOICES], suggestions=[])
    future = asyncio.get_running_loop().create_future()
    request_id = manager.add_permission_request(session, payload, future=future, audit_event=event)
    try:
        return await asyncio.shield(future) is True
    finally:
        manager.cancel_permission_request(request_id, session_id=session.session_id)


def _stable_id(fact: dict[str, Any], spec: ResourceSpec) -> str:
    if not spec.stable_field:
        return ""
    return next(
        (
            item["identifier"]
            for item in [fact, *fact.get("aliases", [])]
            if item["identifier_field"] == spec.stable_field
        ),
        fact.get("attributes", {}).get(spec.stable_field, ""),
    )


@dataclass(frozen=True)
class ResourceQueryPlan:
    product: str
    action: str
    version: str
    account_scope: str
    console_partition: str
    resource_type: str
    identifier: str
    identifier_kind: str
    identifier_field: str
    reference_id: str
    source_version: str = ""
    target_region_id: str = ""
    region_id: str = ""
    stack_id: str = ""
    logical_resource_id: str = ""
    stable_id: str = ""
    attributes: tuple[tuple[str, str], ...] = ()
    requested_attributes: tuple[str, ...] = ()

    def tool_input(self) -> dict[str, Any]:
        if self.product == "ros" and self.action == "GetStackResource" and self.version == "2019-09-10":
            params: dict[str, Any] = {
                "StackId": self.stack_id,
                "LogicalResourceId": self.logical_resource_id,
                "RegionId": self.region_id,
                "ShowResourceAttributes": True,
                "ResourceAttributes": list(self.requested_attributes),
            }
        elif self.product == "ram" and self.action == "GetRole" and self.version == "2015-05-01":
            params = {"RoleName": self.identifier}
        else:
            raise ResourceQueryError("resource_query_unsupported")
        return {
            "product": self.product,
            "version": self.version,
            "action": self.action,
            "params": params,
            **({"region_id": self.region_id} if self.product == "ros" else {}),
        }


def plan_resource_query(reference: dict[str, Any], facts: list[dict[str, Any]]) -> ResourceQueryPlan:
    """Require an independent current observation of the selected identity."""
    spec = resource_spec(reference.get("resource_type"))
    if spec is None or not locator_text(reference.get("account_scope")):
        raise ResourceQueryError("resource_query_needs_context")
    if reference.get("console_partition") not in {"aliyun", "alibabacloud"}:
        raise ResourceQueryError("resource_query_needs_context")
    if reference.get("status", "").startswith("DELETE_"):
        raise ResourceQueryError("resource_query_lifecycle_changed")
    identity = (reference["identifier_kind"], reference["identifier"])
    independent = []
    for fact in facts:
        if resource_spec(fact.get("resource_type")) != spec or fact.get("evidence") != "api_response":
            continue
        if fact.get("status", "").startswith("DELETE_"):
            continue
        if identity not in {(item["identifier_kind"], item["identifier"]) for item in [fact, *fact.get("aliases", [])]}:
            continue
        fields = ("account_scope", "console_partition", *(() if spec.scope_kind == "account" else ("region_id",)))
        if any(fact.get(key) != reference.get(key) for key in fields):
            continue
        stable = _stable_id(reference, spec)
        if stable and _stable_id(fact, spec) not in {"", stable}:
            continue
        expected_attributes = reference.get("attributes", {})
        if any(
            fact.get("attributes", {}).get(key) not in {None, value}
            for key in (*spec.parent_fields, spec.stable_field)
            if key and (value := expected_attributes.get(key))
        ):
            continue
        independent.append(fact)
    if not independent:
        raise ResourceQueryError("resource_query_source_unverified")
    base: dict[str, Any] = {
        "account_scope": reference["account_scope"],
        "console_partition": reference["console_partition"],
        "resource_type": spec.resource_type,
        "identifier": reference["identifier"],
        "identifier_kind": reference["identifier_kind"],
        "identifier_field": reference["identifier_field"],
        "reference_id": reference["reference_id"],
        "target_region_id": reference.get("region_id", ""),
        "stable_id": _stable_id(reference, spec),
        "attributes": tuple(sorted(reference.get("attributes", {}).items())),
    }
    # A RoleId is never passed as RoleName, even when the API field is called
    # PhysicalResourceId. The UI's name route does not require this extra read.
    if spec.resource_type == "ALIYUN::RAM::Role" and reference["identifier_field"] == "RoleName":
        return ResourceQueryPlan(product="ram", action="GetRole", version="2015-05-01", **base)
    targets = {
        (source["request_region_id"], source["stack_id"], source["logical_resource_id"])
        for fact in independent
        for source in fact["sources"]
        if source.get("product") == "ros"
        and source.get("evidence", fact["evidence"]) == "api_response"
        and all(locator_text(source.get(key)) for key in ("request_region_id", "stack_id", "logical_resource_id"))
    }
    if len(targets) != 1:
        raise ResourceQueryError("resource_query_ambiguous" if targets else "resource_query_needs_context")
    region, stack, logical = next(iter(targets))
    return ResourceQueryPlan(
        product="ros",
        action="GetStackResource",
        version="2019-09-10",
        region_id=region,
        stack_id=stack,
        logical_resource_id=logical,
        requested_attributes=tuple(
            sorted(
                {
                    *(() if spec.identifier_field == "PhysicalResourceId" else (spec.identifier_field,)),
                    *spec.parent_fields,
                    *spec.attribute_fields,
                }
            )
        ),
        **base,
    )


def project_query_result(plan: ResourceQueryPlan, facts: list[dict[str, Any]]) -> dict[str, Any]:
    """Accept one same-object response; never attach a replacement's parents."""
    spec = resource_spec(plan.resource_type)
    assert spec is not None
    matches = []
    for fact in facts:
        if resource_spec(fact.get("resource_type")) != spec or fact.get("evidence") != "api_response":
            continue
        if fact.get("account_scope") != plan.account_scope:
            continue
        if fact.get("console_partition") not in {None, plan.console_partition}:
            continue
        if (plan.identifier_kind, plan.identifier) not in {
            (item["identifier_kind"], item["identifier"]) for item in [fact, *fact.get("aliases", [])]
        }:
            continue
        if plan.product == "ros" and (
            fact.get("stack_id") != plan.stack_id or fact.get("logical_resource_id") != plan.logical_resource_id
        ):
            continue
        if not any(
            source.get("product") == plan.product and source.get("action") == plan.action for source in fact["sources"]
        ):
            continue
        attrs = fact.get("attributes", {})
        if plan.stable_id and _stable_id(fact, spec) != plan.stable_id:
            raise ResourceQueryError("resource_query_lifecycle_changed")
        if any(attrs.get(key) != value for key, value in plan.attributes if key in spec.parent_fields):
            raise ResourceQueryError("resource_query_lifecycle_changed")
        if fact.get("status", "").startswith("DELETE_"):
            raise ResourceQueryError("resource_query_lifecycle_changed")
        if spec.scope_kind == "regional" and fact.get("region_id") != plan.target_region_id:
            if spec.resource_type not in {
                "ALIYUN::ECS::Image",
                "ALIYUN::ECS::ImageCopy",
                "ALIYUN::ECS::ImageDetail",
            } or fact.get("region_id") != attrs.get("DestinationRegionId"):
                continue
        matches.append(fact)
    if len(matches) != 1:
        raise ResourceQueryError("resource_query_target_changed")
    result = copy.deepcopy(matches[0])
    result["console_partition"] = plan.console_partition
    return result


class ResourceQueryCoordinator:
    """One per session; bounded in-flight work and short-lived in-memory cache."""

    def __init__(self, *, timeout: float = 10, clock: Callable[[], float] = time.monotonic) -> None:
        self.timeout, self.clock = timeout, clock
        self._inflight: dict[ResourceQueryPlan, asyncio.Task[dict[str, Any]]] = {}
        self._cache: OrderedDict[ResourceQueryPlan, tuple[float, dict[str, Any] | str]] = OrderedDict()
        self._closed = False

    async def query(
        self,
        plan: ResourceQueryPlan,
        reader: Callable[[ResourceQueryPlan, ResourceQueryAttempt], Awaitable[list[dict[str, Any]]]],
        *,
        is_current: Callable[[], bool | Awaitable[bool]],
    ) -> dict[str, Any]:
        if self._closed or not await _source_current(is_current) or self._closed:
            raise ResourceQueryError("resource_query_source_changed")
        cached = self._cache.get(plan)
        if cached and cached[0] > self.clock():
            self._cache.move_to_end(plan)
            if isinstance(cached[1], str):
                raise ResourceQueryError(cached[1])
            return copy.deepcopy(cached[1])
        self._cache.pop(plan, None)
        task = self._inflight.get(plan)
        if task is None:
            if len(self._inflight) >= 4:
                raise ResourceQueryError("resource_query_busy")
            task = asyncio.create_task(self._read(plan, reader, is_current))
            self._inflight[plan] = task
            task.add_done_callback(lambda finished: self._finished(plan, finished))
        result = await asyncio.shield(task)
        if self._closed or not await _source_current(is_current) or self._closed:
            raise ResourceQueryError("resource_query_source_changed")
        return copy.deepcopy(result)

    def _finished(self, plan: ResourceQueryPlan, task: asyncio.Task[dict[str, Any]]) -> None:
        if self._inflight.get(plan) is task:
            self._inflight.pop(plan, None)
        if not task.cancelled():
            task.exception()  # Consume errors even if the requesting tab left.

    async def _read(self, plan, reader, is_current) -> dict[str, Any]:
        async def current():
            return not self._closed and await _source_current(is_current) and not self._closed

        attempt = ResourceQueryAttempt(self.timeout, current)
        attempt.start()
        try:
            facts = await reader(plan, attempt)
            await attempt.check_current()
            result = project_query_result(plan, facts)
        except asyncio.CancelledError:
            if not attempt.timed_out:
                raise
            code = "resource_query_timeout"
        except asyncio.TimeoutError:
            code = "resource_query_timeout"
        except ResourceQueryError as error:
            code = str(error)
        except Exception:
            code = "resource_query_failed"
        else:
            self._remember(plan, result, 300)
            return result
        finally:
            attempt.stop()
        self._remember(plan, code, 3)
        raise ResourceQueryError(code)

    def _remember(self, plan, value, ttl) -> None:
        if not self._closed:
            self._cache[plan] = (self.clock() + ttl, value)
            self._cache.move_to_end(plan)
            while len(self._cache) > 128:
                self._cache.popitem(last=False)

    def close(self) -> None:
        self._closed = True
        self._cache.clear()
        for task in self._inflight.values():
            task.cancel()


async def read_resource_attributes(
    plan: ResourceQueryPlan,
    *,
    cwd: str,
    session_id: str,
    permission_context: Any,
    cache_dir: Path,
    attempt: ResourceQueryAttempt | None = None,
    permission_resolver: Callable[..., Awaitable[bool]] | None = None,
    load_permission_context: Callable[[], Any] | None = None,
) -> list[dict[str, Any]]:
    """Production reader. Loading credentials is delayed until an explicit read.

    A stale/different opaque scope fails closed; no profile or default account
    is substituted. OAuth refresh must not silently change the selected scope.
    """
    from iac_code.services.cloud_credentials import CloudCredentials
    from iac_code.services.permissions.pipeline import check_tool_permission
    from iac_code.services.providers.aliyun import DEFAULT_REGION, AliyunCredentials
    from iac_code.tools.base import ToolContext
    from iac_code.tools.cloud.aliyun.aliyun_api import AliyunApi
    from iac_code.tools.cloud.aliyun.contract_store import canonical_input_sha256
    from iac_code.tools.cloud.aliyun.resource_reference_extractors import credential_reference_context
    from iac_code.tools.cloud.aliyun.runtime import create_aliyun_runtime_services
    from iac_code.tools.cloud.resource_references import resource_references_from_metadata
    from iac_code.types.permissions import InvocationBinding
    from iac_code.types.stream_events import PermissionRequestEvent

    def selected_credential():
        credential = CloudCredentials().get_provider("aliyun")
        if credential is None:
            raise ResourceQueryError("resource_query_credentials_required")
        context = credential_reference_context(credential)
        if context.get("account_scope") != plan.account_scope or context.get("console_partition") not in {
            None,
            plan.console_partition,
        }:
            raise ResourceQueryError("resource_query_account_changed")
        return credential

    credential = await asyncio.to_thread(selected_credential)
    if credential.mode == "OAuth":
        credential = await asyncio.to_thread(AliyunCredentials.refresh_oauth_if_needed, credential)
        if credential_reference_context(credential).get("account_scope") != plan.account_scope:
            raise ResourceQueryError("resource_query_account_changed")
    runtime = create_aliyun_runtime_services(cache_dir=cache_dir)
    snapshot_id = None
    try:
        # Freeze the checked credential for this attempt. Recheck configuration
        # before target execution, without switching to a new credential.
        runtime.credential_provider = lambda: credential
        # The common API validator requires a nonempty request region even for
        # global RAM. This execution hint is NOT a role identity, required UI
        # field, persisted target region or cache dimension. ROS stays pinned
        # to the actual source stack region; it never uses this fallback.
        runtime.default_region_provider = lambda: (
            plan.region_id if plan.product == "ros" else credential.region_id or DEFAULT_REGION
        )
        tool = AliyunApi(services=runtime)
        tool_input = tool.prepare_invocation_input(plan.tool_input())
        binding = InvocationBinding(
            runtime_nonce=uuid.uuid4().hex,
            session_id=session_id,
            tool_use_id="resource-query:" + uuid.uuid4().hex,
            tool_name=tool.name,
            canonical_input_sha256=canonical_input_sha256(tool_input),
        )
        permissions = replace(copy.deepcopy(permission_context), invocation_binding=binding)
        permission = await check_tool_permission(tool, tool_input, permissions)
        snapshot_id = permission.snapshot_id
        if permission.behavior == "ask" and permission_resolver is not None:
            # Authorization cannot turn an unverified/write contract into a read.
            if permission.audit is None or permission.audit.is_read_only is not True:
                raise ResourceQueryError("resource_query_readonly_unverified")
            event = PermissionRequestEvent(
                tool_name=tool.name,
                tool_input=copy.deepcopy(tool_input),
                tool_use_id=binding.tool_use_id,
                permission_result=permission,
                audit_context={
                    "metadata": permission.audit,
                    "settings": permissions.audit_settings,
                    "session_id": session_id,
                    "cwd": cwd,
                },
            )
            if attempt is not None:
                allowed = await attempt.request_permission(permission_resolver, event)
            else:
                allowed = await asyncio.wait_for(permission_resolver(event), timeout=300)
            if allowed is not True:
                raise ResourceQueryError("resource_query_permission_denied")
            # A grant is tied to the exact policy snapshot too. Do not execute
            # if another tab changes permissions while the prompt is open.
            current_permissions = load_permission_context() if load_permission_context else permission_context
            if replace(copy.deepcopy(current_permissions), invocation_binding=binding) != permissions:
                raise ResourceQueryError("resource_query_permission_changed")
        elif permission.behavior != "allow":
            raise ResourceQueryError(
                "resource_query_permission_required"
                if permission.behavior == "ask"
                else "resource_query_permission_denied"
            )
        # A blanket/bypass mode must not turn a mislabeled API into a write.
        if permission.audit is None or permission.audit.is_read_only is not True:
            raise ResourceQueryError("resource_query_readonly_unverified")
        await asyncio.to_thread(selected_credential)
        if attempt is not None:
            await attempt.check_current()
        result = await tool.execute(
            tool_input=tool_input,
            context=ToolContext(
                cwd=cwd,
                tool_use_id=binding.tool_use_id,
                permission_context=permissions,
                invocation_binding=binding,
                snapshot_id=permission.snapshot_id,
                security_digest=permission.security_digest,
                execution_class=permission.execution_class,
            ),
        )
        if result.is_error:
            raise ResourceQueryError("resource_query_failed")
        return resource_references_from_metadata(result.metadata)
    finally:
        if snapshot_id is not None and runtime.contract_store.is_pending(snapshot_id):
            runtime.contract_store.reject(snapshot_id)
        await runtime.aclose()
