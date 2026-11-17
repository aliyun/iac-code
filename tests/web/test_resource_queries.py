from __future__ import annotations

import asyncio
import copy
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from iac_code.services.providers.aliyun import AliyunCredential
from iac_code.tools.base import ToolResult
from iac_code.tools.cloud.aliyun.resource_reference_extractors import credential_reference_context
from iac_code.tools.cloud.resource_references import (
    resource_reference_metadata,
    sanitize_resource_reference,
    stack_resource_reference,
)
from iac_code.types.permissions import PermissionAuditMetadata, PermissionResult, ToolPermissionContext
from iac_code.types.stream_events import PermissionRequestEvent
from iac_code.web.resource_queries import (
    ResourceQueryAttempt,
    ResourceQueryCoordinator,
    ResourceQueryError,
    plan_resource_query,
    project_query_result,
    read_resource_attributes,
    request_resource_query_permission,
)
from iac_code.web.resource_reference_controller import ResourceReferenceController
from iac_code.web.resource_references import ResourceRevisionConflictError, resource_fact_to_web
from iac_code.web.resource_sources import read_visible_source


@pytest.mark.asyncio
async def test_async_source_validation_is_awaited_again_after_permission():
    current = True
    checks = []

    async def valid():
        await asyncio.sleep(0)
        checks.append(current)
        return current

    async def allow(_event):
        nonlocal current
        current = False
        return True

    attempt = ResourceQueryAttempt(10, valid)
    attempt.start()
    try:
        with pytest.raises(ResourceQueryError, match="source_changed"):
            await attempt.request_permission(allow, object())
        assert checks == [True, False]
    finally:
        attempt.stop()


def role(**values):
    fact = sanitize_resource_reference(
        {
            "resource_type": "ALIYUN::RAM::Role",
            "identifier": "DeployRole",
            "identifier_kind": "name",
            "identifier_field": "RoleName",
            "account_scope": "fake-account",
            "console_partition": "aliyun",
            "evidence": "api_response",
            "attributes": {},
            "sources": [
                {
                    "tool_use_id": "tool-1",
                    "source_path": "/result/Role/RoleName",
                    "product": "ram",
                    "action": "GetRole",
                    "request_region_id": "cn-beijing",
                    "evidence": "api_response",
                    "observed_at": 100,
                }
            ],
            **values,
        }
    )
    assert fact is not None
    return fact


def reference(fact):
    return {**fact, "reference_id": "ref-test"}


def function():
    return role(
        resource_type="ALIYUN::FC::Function",
        identifier="Handler",
        identifier_field="FunctionName",
        region_id="cn-beijing",
        stack_id="fake-stack",
        logical_resource_id="LogicalFunction",
        sources=[
            {
                "tool_use_id": "tool-1",
                "source_path": "/resources/0",
                "product": "ros",
                "action": "ListStackResources",
                "request_region_id": "cn-beijing",
                "stack_id": "fake-stack",
                "logical_resource_id": "LogicalFunction",
                "evidence": "api_response",
                "observed_at": 100,
            }
        ],
    )


def test_plan_role_is_global_and_never_substitutes_numeric_id():
    fact = role()
    plan = plan_resource_query(reference(fact), [fact])
    assert plan.product == "ram" and plan.action == "GetRole"
    assert plan.region_id == "" and plan.tool_input()["params"] == {"RoleName": "DeployRole"}
    changed_region = copy.deepcopy(fact)
    changed_region["sources"][0]["request_region_id"] = "eu-central-1"
    assert plan_resource_query(reference(fact), [changed_region]) == plan
    opaque = role(identifier="123456789", identifier_kind="id", identifier_field="PhysicalResourceId")
    with pytest.raises(ResourceQueryError, match="needs_context"):
        plan_resource_query(reference(opaque), [opaque])


def test_plan_requires_observed_same_account_and_explicit_ros_source():
    fact = function()
    plan = plan_resource_query(reference(fact), [fact])
    assert plan.tool_input()["params"] == {
        "StackId": "fake-stack",
        "RegionId": "cn-beijing",
        "LogicalResourceId": "LogicalFunction",
        "ShowResourceAttributes": True,
        "ResourceAttributes": ["FunctionName", "ServiceName"],
    }
    for field, value in (
        ("account_scope", "other-account"),
        ("evidence", "user_context"),
        ("region_id", "eu-central-1"),
    ):
        changed = {**fact, field: value}
        with pytest.raises(ResourceQueryError, match="source_unverified"):
            plan_resource_query(reference(fact), [changed])
    missing = copy.deepcopy(fact)
    del missing["sources"][0]["request_region_id"]
    with pytest.raises(ResourceQueryError, match="needs_context"):
        plan_resource_query(reference(fact), [missing])
    other = copy.deepcopy(fact)
    other["sources"][0]["stack_id"] = "another-stack"
    with pytest.raises(ResourceQueryError, match="ambiguous"):
        plan_resource_query(reference(fact), [fact, other])


def test_catalog_resource_query_requests_only_template_attributes():
    fact = stack_resource_reference(
        {
            "ResourceType": "ALIYUN::FNF::Flow",
            "PhysicalResourceId": "flow-physical",
            "LogicalResourceId": "Flow",
        },
        stack_id="fake-stack",
        region_id="cn-hangzhou",
        tool_use_id="tool-1",
        source_path="/Resources/0",
        account_scope="fake-account",
        console_partition="aliyun",
    )
    assert fact is not None
    plan = plan_resource_query(reference(fact), [fact])
    assert plan.requested_attributes == ("FlowName",)
    assert plan.tool_input()["params"]["ResourceAttributes"] == ["FlowName"]
    loaded = stack_resource_reference(
        {
            "ResourceType": "ALIYUN::FNF::Flow",
            "PhysicalResourceId": "flow-physical",
            "LogicalResourceId": "Flow",
            "ResourceAttributes": {"FlowName": "OrderFlow"},
        },
        stack_id="fake-stack",
        region_id="cn-hangzhou",
        tool_use_id="resource-query",
        source_path="/result",
        account_scope="fake-account",
        console_partition="aliyun",
        action="GetStackResource",
    )
    assert loaded is not None
    assert project_query_result(plan, [loaded])["attributes"] == {"FlowName": "OrderFlow"}


@pytest.mark.parametrize(
    "change",
    [
        {"identifier": "OtherRole"},
        {"account_scope": "other-account"},
        {"attributes": {"RoleId": "222222222"}},
        {"status": "DELETE_COMPLETE"},
    ],
)
def test_result_never_attaches_a_replacement_or_wrong_account(change):
    fact = role(attributes={"RoleId": "111111111"})
    plan = plan_resource_query(reference(fact), [fact])
    with pytest.raises(ResourceQueryError):
        project_query_result(plan, [role(**{"attributes": {"RoleId": "111111111"}, **change})])


def test_stable_identity_from_alias_is_not_lost_in_query_plan():
    fact = role(aliases=[{"identifier": "111111111", "identifier_kind": "id", "identifier_field": "RoleId"}])
    plan = plan_resource_query(reference(fact), [fact])
    assert plan.stable_id == "111111111"
    with pytest.raises(ResourceQueryError, match="lifecycle_changed"):
        project_query_result(plan, [role(attributes={"RoleId": "222222222"})])


@pytest.mark.asyncio
async def test_coalescing_cancellation_cache_and_source_isolation():
    fact = role()
    plan = plan_resource_query(reference(fact), [fact])
    gate, entered = asyncio.Event(), asyncio.Event()
    coordinator = ResourceQueryCoordinator()
    calls = 0

    async def reader(plan, attempt):
        nonlocal calls
        calls += 1
        entered.set()
        await gate.wait()
        return [fact]

    first = asyncio.create_task(coordinator.query(plan, reader, is_current=lambda: True))
    await entered.wait()
    second = asyncio.create_task(coordinator.query(plan, reader, is_current=lambda: True))
    first.cancel()
    with pytest.raises(asyncio.CancelledError):
        await first
    gate.set()
    result = await second
    result["attributes"]["RoleId"] = "mutated"
    cached = await coordinator.query(plan, reader, is_current=lambda: True)
    assert "RoleId" not in cached["attributes"] and calls == 1
    with pytest.raises(ResourceQueryError, match="source_changed"):
        await coordinator.query(plan, reader, is_current=lambda: False)
    await coordinator.query(replace(plan, source_version="different-source"), reader, is_current=lambda: True)
    assert calls == 2
    coordinator.close()


@pytest.mark.asyncio
async def test_concurrency_timeout_negative_cache_and_close():
    fact = role()
    plan = plan_resource_query(reference(fact), [fact])
    clock = [0.0]
    coordinator = ResourceQueryCoordinator(timeout=0.05, clock=lambda: clock[0])
    count = 0
    entered = asyncio.Event()

    async def reader(plan, attempt):
        nonlocal count
        count += 1
        if count == 4:
            entered.set()
        await asyncio.Event().wait()
        return []

    jobs = [
        asyncio.create_task(coordinator.query(replace(plan, reference_id=str(i)), reader, is_current=lambda: True))
        for i in range(4)
    ]
    await entered.wait()
    with pytest.raises(ResourceQueryError, match="busy"):
        await coordinator.query(plan, reader, is_current=lambda: True)
    results = await asyncio.gather(*jobs, return_exceptions=True)
    assert all(isinstance(result, ResourceQueryError) and str(result) == "resource_query_timeout" for result in results)
    with pytest.raises(ResourceQueryError, match="timeout"):
        await coordinator.query(replace(plan, reference_id="0"), reader, is_current=lambda: True)
    assert count == 4
    clock[0] = 4.0
    with pytest.raises(ResourceQueryError, match="timeout"):
        await coordinator.query(replace(plan, reference_id="0"), reader, is_current=lambda: True)
    assert count == 5
    coordinator.close()
    with pytest.raises(ResourceQueryError, match="source_changed"):
        await coordinator.query(plan, reader, is_current=lambda: True)


def query_request(controller, fact):
    data = {
        "messages": [{"messageId": "m1", "text": '{"' + fact["identifier_field"] + '":"' + fact["identifier"] + '"}'}],
        "tools": {
            "tool-1": {
                "toolName": "aliyun_api",
                "input": {},
                "results": [
                    {
                        "content": "visible result",
                        "resourceReferences": [resource_fact_to_web(fact)],
                    }
                ],
            }
        },
    }
    snapshot = controller.hydrate(data)
    return data, {
        "expectedRevision": snapshot["revision"],
        "referenceId": snapshot["items"][0]["referenceId"],
        "identifier": fact["identifier"],
        "identifierKind": fact["identifier_kind"],
        "source": {"messageId": "m1", "sourcePath": "/text"},
        "sourceDigest": read_visible_source(data, {"message_id": "m1", "source_path": "/text"}).digest,
        "group": [
            fact["identifier_kind"],
            fact["identifier"],
            fact["identifier_field"],
            fact["resource_type"],
            None,
            None,
            None,
            [],
            {"kind": "field"},
            None,
        ],
        "queryMissingAttributes": True,
    }


@pytest.mark.asyncio
async def test_query_parent_persists_and_revokes_with_source_version(tmp_path):
    controller = ResourceReferenceController(tmp_path / "references.json", lambda *args: None)
    fact = function()
    data, request = query_request(controller, fact)
    response = copy.deepcopy(fact)
    response["attributes"] = {"ServiceName": "ServiceA"}
    response["sources"][0].update(action="GetStackResource", observed_at=200)
    reader = AsyncMock(return_value=[response])
    result = await controller.resolve_attributes(request, load_current=lambda: data, reader=reader)
    assert result["status"] == "attributes_loaded"
    assert len(result["snapshot"]["items"]) == 1
    assert result["snapshot"]["items"][0]["referenceId"] == request["referenceId"]
    assert result["snapshot"]["items"][0]["attributes"] == {"ServiceName": "ServiceA"}
    await controller.close()
    restored = ResourceReferenceController(controller.path, lambda *args: None)
    snapshot = restored.hydrate(data)
    assert snapshot["items"][0]["attributes"] == {"ServiceName": "ServiceA"}
    data["tools"]["tool-1"]["results"][0]["content"] = "changed source"
    snapshot = restored.hydrate(data)
    assert snapshot["items"][0]["attributes"] == {}
    assert snapshot["items"][0]["referenceId"] == request["referenceId"]
    await restored.close()


@pytest.mark.asyncio
async def test_query_target_after_first_snapshot_page_still_acknowledges(tmp_path):
    controller = ResourceReferenceController(tmp_path / "references.json", lambda *args: None)
    data, request = query_request(controller, role(identifier="SeedRole000"))
    facts = [role(identifier=f"SeedRole{index:03d}") for index in range(500)] + [role()]
    data["tools"]["tool-1"]["results"][0]["resourceReferences"] = resource_fact_to_web(facts)
    data["messages"][0]["text"] = '{"RoleName":"DeployRole"}'
    snapshot = controller.hydrate(data)
    assert snapshot["nextCursor"] and all(item["identifier"] != "DeployRole" for item in snapshot["items"])
    target = next(item for item in controller.store.references.values() if item["identifier"] == "DeployRole")
    request.update(expectedRevision=snapshot["revision"], referenceId=target["reference_id"], identifier="DeployRole")
    request["group"][1] = "DeployRole"
    request["sourceDigest"] = read_visible_source(data, {"message_id": "m1", "source_path": "/text"}).digest
    reader = AsyncMock(return_value=[role(attributes={"RoleId": "111111111"})])
    result = await controller.resolve_attributes(request, load_current=lambda: data, reader=reader)
    assert result["status"] == "attributes_loaded"
    assert result["referenceId"] == request["referenceId"]
    assert result["snapshot"]["nextCursor"]
    assert controller.store.references[target["reference_id"]]["attributes"]["RoleId"] == "111111111"
    await controller.close()


@pytest.mark.asyncio
async def test_changed_source_during_query_drops_late_result(tmp_path):
    controller = ResourceReferenceController(tmp_path / "references.json", lambda *args: None)
    data, request = query_request(controller, role())

    async def reader(plan, attempt):
        data["tools"]["tool-1"]["results"][0]["content"] = "replaced"
        return [role(attributes={"RoleId": "111111111"})]

    with pytest.raises(ResourceQueryError, match="source_changed"):
        await controller.resolve_attributes(request, load_current=lambda: data, reader=reader)
    assert controller.snapshot()["items"][0]["attributes"] == {}
    await controller.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("tamper", ["context", "action", "endpoint", "sourceDigest", "identifier"])
async def test_query_rejects_client_targets_and_stale_source_before_read(tmp_path, tamper):
    controller = ResourceReferenceController(tmp_path / "references.json", lambda *args: None)
    data, request = query_request(controller, role())
    request[tamper] = "fake-tamper"
    reader = AsyncMock()
    with pytest.raises(ValueError):
        await controller.resolve_attributes(request, load_current=lambda: data, reader=reader)
    reader.assert_not_called()
    await controller.close()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "behavior,readonly,expected",
    [
        ("allow", True, None),
        ("deny", True, "permission_denied"),
        ("ask", True, "permission_required"),
        ("allow", False, "readonly_unverified"),
    ],
)
async def test_production_reader_uses_public_permission_handoff(tmp_path, monkeypatch, behavior, readonly, expected):
    credential = AliyunCredential(mode="AK", access_key_id="fake-key", access_key_secret="fake-secret")
    fact = role(**credential_reference_context(credential))
    plan = plan_resource_query(reference(fact), [fact])
    monkeypatch.setattr("iac_code.services.cloud_credentials.CloudCredentials.get_provider", lambda *args: credential)
    permission = PermissionResult(
        behavior=behavior,
        audit=PermissionAuditMetadata(scope="once", source="test", is_read_only=readonly),
        snapshot_id="snapshot-test",
        security_digest="digest-test",
        execution_class="concurrent",
    )
    gate = AsyncMock(return_value=permission)
    monkeypatch.setattr("iac_code.services.permissions.pipeline.check_tool_permission", gate)
    store = SimpleNamespace(is_pending=Mock(return_value=True), reject=Mock())
    runtime = SimpleNamespace(aclose=AsyncMock(), contract_store=store)
    monkeypatch.setattr("iac_code.tools.cloud.aliyun.runtime.create_aliyun_runtime_services", lambda **kwargs: runtime)
    execute = AsyncMock(
        return_value=ToolResult(
            content="not parsed", metadata={"resource_references": resource_reference_metadata([fact])}
        )
    )
    monkeypatch.setattr("iac_code.tools.cloud.aliyun.aliyun_api.AliyunApi.execute", execute)
    if expected:
        with pytest.raises(ResourceQueryError, match=expected):
            await read_resource_attributes(
                plan,
                cwd=str(tmp_path),
                session_id="session-test",
                permission_context=ToolPermissionContext(),
                cache_dir=tmp_path,
            )
        execute.assert_not_called()
    else:
        result = await read_resource_attributes(
            plan,
            cwd=str(tmp_path),
            session_id="session-test",
            permission_context=ToolPermissionContext(),
            cache_dir=tmp_path,
        )
        assert result == [fact]
        checked_tool, checked_input, checked_context = gate.call_args.args
        context = execute.call_args.kwargs["context"]
        assert context.invocation_binding == checked_context.invocation_binding
        assert context.snapshot_id == permission.snapshot_id
        assert context.security_digest == permission.security_digest
        assert checked_input["region_id"] and checked_input["params"] == {"RoleName": "DeployRole"}
        assert plan.region_id == "" and "region_id" not in plan.tool_input()
        assert checked_tool._runtime_services.credential_provider() is credential
    runtime.aclose.assert_awaited_once()
    store.reject.assert_called_once_with("snapshot-test")


@pytest.mark.asyncio
async def test_production_reader_rejects_changed_credential_before_runtime(tmp_path, monkeypatch):
    credential = AliyunCredential(mode="AK", access_key_id="fake-other-key", access_key_secret="fake-other-secret")
    monkeypatch.setattr("iac_code.services.cloud_credentials.CloudCredentials.get_provider", lambda *args: credential)
    factory = Mock(side_effect=AssertionError("must not create network runtime"))
    monkeypatch.setattr("iac_code.tools.cloud.aliyun.runtime.create_aliyun_runtime_services", factory)
    with pytest.raises(ResourceQueryError, match="account_changed"):
        await read_resource_attributes(
            plan_resource_query(reference(role()), [role()]),
            cwd=str(tmp_path),
            session_id="s",
            permission_context=ToolPermissionContext(),
            cache_dir=tmp_path,
        )
    factory.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize("outcome", ["allow", "deny", "account", "policy", "source", "write"])
async def test_reader_approval_revalidates_exact_source_credential_and_policy(tmp_path, monkeypatch, outcome):
    credential = AliyunCredential(mode="AK", access_key_id="fake-key", access_key_secret="fake-secret")
    selected = [credential]
    monkeypatch.setattr("iac_code.services.cloud_credentials.CloudCredentials.get_provider", lambda *args: selected[0])
    fact = role(**credential_reference_context(credential))
    plan = plan_resource_query(reference(fact), [fact])
    policy = [ToolPermissionContext(cwd=str(tmp_path))]
    original_policy = policy[0]
    current = [True]
    runtime = SimpleNamespace(
        aclose=AsyncMock(), contract_store=SimpleNamespace(is_pending=Mock(return_value=True), reject=Mock())
    )
    monkeypatch.setattr("iac_code.tools.cloud.aliyun.runtime.create_aliyun_runtime_services", lambda **kwargs: runtime)

    async def gate(tool, tool_input, permissions):
        return PermissionResult(
            behavior="ask",
            audit=PermissionAuditMetadata(scope="once", source="test", is_read_only=outcome != "write"),
            invocation_binding=permissions.invocation_binding,
            snapshot_id="query-contract",
            security_digest="query-digest",
            execution_class="concurrent",
        )

    monkeypatch.setattr("iac_code.services.permissions.pipeline.check_tool_permission", gate)
    execute = AsyncMock(
        return_value=ToolResult(content="", metadata={"resource_references": resource_reference_metadata([fact])})
    )
    monkeypatch.setattr("iac_code.tools.cloud.aliyun.aliyun_api.AliyunApi.execute", execute)
    offered = []

    async def approve(event):
        offered.append(event)
        assert event.permission_result.snapshot_id == "query-contract"
        assert event.permission_result.invocation_binding.tool_use_id == event.tool_use_id
        assert event.tool_input["params"] == {"RoleName": "DeployRole"}
        # A renderer receives a copy, not mutable execution input.
        event.tool_input["params"]["RoleName"] = "NotTheApprovedTarget"
        if outcome == "account":
            selected[0] = AliyunCredential(mode="AK", access_key_id="fake-other", access_key_secret="fake-other")
        if outcome == "policy":
            policy[0] = replace(policy[0], deny_rules={"session": ["aliyun_api"]})
        if outcome == "source":
            current[0] = False
        return outcome != "deny"

    async def reader(plan, attempt):
        return await read_resource_attributes(
            plan,
            cwd=str(tmp_path),
            session_id="query-test",
            permission_context=original_policy,
            cache_dir=tmp_path,
            attempt=attempt,
            permission_resolver=approve,
            load_permission_context=lambda: policy[0],
        )

    coordinator = ResourceQueryCoordinator()
    errors = {
        "deny": "permission_denied",
        "account": "account_changed",
        "policy": "permission_changed",
        "source": "source_changed",
        "write": "readonly_unverified",
    }
    try:
        if outcome in errors:
            with pytest.raises(ResourceQueryError, match=errors[outcome]):
                await coordinator.query(plan, reader, is_current=lambda: current[0])
            execute.assert_not_called()
        else:
            assert await coordinator.query(plan, reader, is_current=lambda: True) == fact
            execute.assert_awaited_once()
            assert execute.call_args.kwargs["tool_input"]["params"] == {"RoleName": "DeployRole"}
            context = execute.call_args.kwargs["context"]
            assert context.snapshot_id == "query-contract" and context.security_digest == "query-digest"
            assert context.invocation_binding == offered[0].permission_result.invocation_binding
        assert len(offered) == (outcome != "write")
        assert not original_policy.allow_rules and not original_policy.deny_rules
        runtime.aclose.assert_awaited_once()
        runtime.contract_store.reject.assert_called_once_with("query-contract")
    finally:
        coordinator.close()


@pytest.mark.asyncio
async def test_controller_hydration_never_queries_and_sidecar_cannot_authorize_read(tmp_path):
    controller = ResourceReferenceController(tmp_path / "references.json", lambda *args: None)
    data, request = query_request(controller, role())
    reader = AsyncMock()
    controller.hydrate(data)
    controller.snapshot()
    reader.assert_not_called()
    # Keeping the tool ID alone cannot authorize a read with stale metadata.
    data["tools"]["tool-1"]["results"] = [{"content": "other result"}]
    request["expectedRevision"] = controller.hydrate(data)["revision"]
    assert controller.snapshot()["items"] == []
    with pytest.raises(ValueError, match="resource_reference_unavailable"):
        await controller.resolve_attributes(request, load_current=lambda: data, reader=reader)
    reader.assert_not_called()
    await controller.close()


@pytest.mark.asyncio
async def test_external_tool_cannot_authorize_query_with_self_labeled_metadata(tmp_path):
    controller = ResourceReferenceController(tmp_path / "references.json", lambda *args: None)
    data, request = query_request(controller, role())
    data["tools"]["tool-1"]["toolName"] = "mcp__fake__resource"
    reader = AsyncMock()
    with pytest.raises(ResourceRevisionConflictError, match="resource_revision_conflict"):
        await controller.resolve_attributes(request, load_current=lambda: data, reader=reader)
    request["expectedRevision"] = controller.hydrate(data)["revision"]
    with pytest.raises(ResourceQueryError, match="source_unverified"):
        await controller.resolve_attributes(request, load_current=lambda: data, reader=reader)
    reader.assert_not_called()
    await controller.close()


@pytest.mark.asyncio
async def test_query_failure_redacts_backend_error_and_close_cancels_work():
    fact = role()
    plan = plan_resource_query(reference(fact), [fact])
    coordinator = ResourceQueryCoordinator()

    async def failed(plan, attempt):
        raise RuntimeError("secret response fake-sensitive-value")

    with pytest.raises(ResourceQueryError, match="^resource_query_failed$"):
        await coordinator.query(plan, failed, is_current=lambda: True)
    entered, cancelled = asyncio.Event(), asyncio.Event()

    async def slow(plan, attempt):
        entered.set()
        try:
            await asyncio.Event().wait()
        finally:
            cancelled.set()

    job = asyncio.create_task(coordinator.query(replace(plan, source_version="new"), slow, is_current=lambda: True))
    await entered.wait()
    coordinator.close()
    with pytest.raises(asyncio.CancelledError):
        await job
    assert cancelled.is_set()


@pytest.mark.asyncio
@pytest.mark.parametrize("outcome", ["allow", "timeout", "close", "source_changed"])
async def test_approval_wait_is_separate_from_active_read_budget(outcome):
    coordinator = ResourceQueryCoordinator(timeout=0.02)
    fact = role()
    plan = plan_resource_query(reference(fact), [fact])
    waiting = asyncio.Event()
    released = asyncio.Event()
    cleaned = asyncio.Event()
    current = [True]
    reads = []

    async def approve(event):
        waiting.set()
        try:
            await released.wait()
            return True
        finally:
            cleaned.set()

    async def reader(plan, attempt):
        attempt.permission_timeout = 0.06 if outcome == "timeout" else 1
        assert await attempt.request_permission(approve, object())
        reads.append(plan)
        return [fact]

    job = asyncio.create_task(coordinator.query(plan, reader, is_current=lambda: current[0]))
    await asyncio.wait_for(waiting.wait(), 1)
    await asyncio.sleep(0.04)  # Longer than the read deadline, but not the approval deadline.
    assert not job.done() and not reads
    if outcome == "close":
        coordinator.close()
        with pytest.raises(asyncio.CancelledError):
            await job
    elif outcome == "timeout":
        with pytest.raises(ResourceQueryError, match="permission_timeout"):
            await job
    else:
        current[0] = outcome != "source_changed"
        released.set()
        if outcome == "source_changed":
            with pytest.raises(ResourceQueryError, match="source_changed"):
                await job
        else:
            assert await job == fact
    assert cleaned.is_set()
    assert len(reads) == (outcome == "allow")
    coordinator.close()


@pytest.mark.asyncio
async def test_read_budget_resumes_after_approval_and_still_times_out():
    coordinator = ResourceQueryCoordinator(timeout=0.02)
    fact = role()

    async def reader(plan, attempt):
        assert await attempt.request_permission(AsyncMock(return_value=True), object())
        await asyncio.Event().wait()

    with pytest.raises(ResourceQueryError, match="resource_query_timeout"):
        await coordinator.query(plan_resource_query(reference(fact), [fact]), reader, is_current=lambda: True)
    coordinator.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("choice,audit_ok", [("allow_once", True), ("reject_once", True), ("allow_once", False)])
async def test_http_query_approval_is_once_only_audited_and_does_not_resume_agent(
    tmp_path, monkeypatch, choice, audit_ok
):
    import httpx

    from iac_code.web.app import create_app
    from iac_code.web.session_manager import WebSessionManager

    manager = WebSessionManager(projects_dir=tmp_path / "projects", cwd=tmp_path / "project")
    session = manager.create_session(session_id="query-approval-session")
    controller = session.resource_references
    assert controller is not None
    data, request = query_request(controller, role())
    monkeypatch.setattr(manager, "load_visible_transcript", lambda *args, **kwargs: copy.deepcopy(data))
    permissions = ToolPermissionContext(cwd=str(tmp_path))
    monkeypatch.setattr(manager, "ensure_permission_context", lambda *args: permissions)
    audit = Mock(return_value=audit_ok)
    monkeypatch.setattr("iac_code.services.permissions.audit.emit_permission_boundary_audit", audit)
    waiting = asyncio.Event()
    original_add = manager.add_permission_request

    def add(*args, **kwargs):
        request_id = original_add(*args, **kwargs)
        waiting.set()
        return request_id

    monkeypatch.setattr(manager, "add_permission_request", add)
    reads = []

    async def reader(plan, *, attempt, permission_resolver, **kwargs):
        event = PermissionRequestEvent(
            tool_name="aliyun_api",
            tool_input=plan.tool_input(),
            tool_use_id="resource-query:fake",
            permission_result=PermissionResult(
                behavior="ask", audit=PermissionAuditMetadata(scope="once", source="test", is_read_only=True)
            ),
        )
        if not await attempt.request_permission(permission_resolver, event):
            raise ResourceQueryError("resource_query_permission_denied")
        reads.append(plan)
        return [role(attributes={"RoleId": "111111111"})]

    monkeypatch.setattr("iac_code.web.resource_queries.read_resource_attributes", reader)
    app = create_app(session_manager=manager)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://testserver") as client:
        job = asyncio.create_task(
            client.post(f"/api/sessions/{session.web_session_id}/resource-references/resolve", json=request)
        )
        try:
            await asyncio.wait_for(waiting.wait(), 2)
            pending = next(iter(session.pending_permissions.values()))
            assert pending.boundary_id is None
            assert pending.payload["turnId"] == ""
            assert pending.payload["suggestions"] == []
            assert {item["id"] for item in pending.payload["choices"]} == {"allow_once", "reject_once"}
            assert '"RoleName": "DeployRole"' in pending.payload["command"]
            assert session.active_turn_task is None and not reads and not job.done()
            endpoint = f"/api/permissions/{pending.request_id}/answer"
            invalid = await client.post(endpoint, json={"sessionId": session.session_id, "choice": "always_allow"})
            assert invalid.status_code == 400 and not reads and not pending.future.done()
            wrong = await client.post(endpoint, json={"sessionId": "another-session", "choice": "allow_once"})
            assert wrong.status_code >= 400 and not pending.future.done()
            answer = await client.post(endpoint, json={"sessionId": session.session_id, "choice": choice})
            assert answer.status_code == 200
            result = await asyncio.wait_for(job, 2)
            allowed = choice == "allow_once" and audit_ok
            assert result.json()["status"] == ("attributes_loaded" if allowed else "needs_context")
            assert len(reads) == allowed
            audit.assert_called_once()
            assert audit.call_args.kwargs["scope"] == "once"
            assert not permissions.allow_rules and not permissions.deny_rules
            assert not session.pending_permissions and session.active_turn_task is None
            duplicate = await client.post(endpoint, json={"sessionId": session.session_id, "choice": choice})
            assert duplicate.status_code >= 400 and len(reads) == allowed
        finally:
            await controller.close()
            if not job.done():
                job.cancel()
            await asyncio.gather(job, return_exceptions=True)


@pytest.mark.asyncio
async def test_pending_query_approval_is_removed_on_cancel(tmp_path):
    from iac_code.web.session_manager import WebSessionManager

    manager = WebSessionManager(projects_dir=tmp_path / "projects", cwd=tmp_path / "project")
    session = manager.create_session(session_id="canceled-query")
    event = PermissionRequestEvent(tool_name="aliyun_api", tool_input={}, tool_use_id="resource-query:fake")
    task = asyncio.create_task(request_resource_query_permission(manager, session, event))
    await asyncio.sleep(0)
    assert len(session.pending_permissions) == 1
    pending = next(iter(session.pending_permissions.values()))
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert not session.pending_permissions and pending.future.result() is False


@pytest.mark.parametrize("failure", [None, "resource_query_permission_denied"])
def test_real_resolve_route_loads_attributes_without_binding_or_raw_response(tmp_path, monkeypatch, failure):
    from starlette.testclient import TestClient

    from iac_code.web.app import create_app
    from iac_code.web.session_manager import WebSessionManager

    manager = WebSessionManager(projects_dir=tmp_path / "projects", cwd=tmp_path / "project")
    session = manager.create_session(session_id="query-session")
    controller = session.resource_references
    assert controller is not None
    data, request = query_request(controller, role())
    monkeypatch.setattr(manager, "load_visible_transcript", lambda *args, **kwargs: copy.deepcopy(data))
    monkeypatch.setattr(manager, "ensure_permission_context", lambda *args: ToolPermissionContext(cwd=str(tmp_path)))
    response_fact = role(attributes={"RoleId": "111111111"})
    reader = AsyncMock(return_value=[response_fact], side_effect=ResourceQueryError(failure) if failure else None)
    monkeypatch.setattr("iac_code.web.resource_queries.read_resource_attributes", reader)
    with TestClient(create_app(session_manager=manager)) as client:
        response = client.post(f"/api/sessions/{session.web_session_id}/resource-references/resolve", json=request)
        assert response.status_code == 200
        payload = response.json()
        if failure:
            assert payload == {"status": "needs_context", "reason": failure}
        else:
            assert payload["status"] == "attributes_loaded"
            assert payload["snapshot"]["bindings"] == []
            assert payload["snapshot"]["items"][0]["attributes"]["RoleId"] == "111111111"
            snapshot = client.get(f"/api/sessions/{session.web_session_id}/resource-references").json()
            assert snapshot["items"][0]["attributes"]["RoleId"] == "111111111"
        reader.assert_awaited_once()


@pytest.mark.asyncio
async def test_production_reader_rechecks_account_after_permission_wait(tmp_path, monkeypatch):
    first = AliyunCredential(mode="AK", access_key_id="fake-key-1", access_key_secret="fake-secret-1")
    second = AliyunCredential(mode="AK", access_key_id="fake-key-2", access_key_secret="fake-secret-2")
    credentials = [first]
    monkeypatch.setattr(
        "iac_code.services.cloud_credentials.CloudCredentials.get_provider", lambda *args: credentials[0]
    )
    fact = role(**credential_reference_context(first))
    plan = plan_resource_query(reference(fact), [fact])
    runtime = SimpleNamespace(
        aclose=AsyncMock(), contract_store=SimpleNamespace(is_pending=Mock(return_value=True), reject=Mock())
    )
    monkeypatch.setattr("iac_code.tools.cloud.aliyun.runtime.create_aliyun_runtime_services", lambda **kwargs: runtime)

    async def gate(*args):
        credentials[0] = second
        return PermissionResult(
            behavior="allow",
            snapshot_id="snapshot",
            audit=PermissionAuditMetadata(scope="once", source="test", is_read_only=True),
        )

    monkeypatch.setattr("iac_code.services.permissions.pipeline.check_tool_permission", gate)
    execute = AsyncMock()
    monkeypatch.setattr("iac_code.tools.cloud.aliyun.aliyun_api.AliyunApi.execute", execute)
    with pytest.raises(ResourceQueryError, match="account_changed"):
        await read_resource_attributes(
            plan, cwd=str(tmp_path), session_id="s", permission_context=ToolPermissionContext(), cache_dir=tmp_path
        )
    execute.assert_not_called()
    runtime.aclose.assert_awaited_once()


@pytest.mark.asyncio
@pytest.mark.parametrize("denied", [False, True])
async def test_real_permission_contract_and_tool_execution_with_offline_transport(tmp_path, monkeypatch, denied):
    """Exercise the actual gate, handoff, builder and metadata extraction."""
    from types import MappingProxyType

    from iac_code.services.permissions.pipeline import check_tool_permission
    from iac_code.tools.cloud.aliyun.acs3_transport import NormalizedApiResponse
    from iac_code.tools.cloud.aliyun.api_contract import CanonicalWireContract, RequestBuilder
    from iac_code.tools.cloud.aliyun.contract_store import ResolvedContractStore
    from iac_code.tools.cloud.aliyun.endpoint_resolver import EndpointResolution
    from iac_code.tools.cloud.aliyun.openmeta import ParameterMetadata

    credential = AliyunCredential(mode="AK", access_key_id="fake-offline-key", access_key_secret="fake-offline-secret")
    fact = role(**credential_reference_context(credential))
    plan = plan_resource_query(reference(fact), [fact])
    monkeypatch.setattr("iac_code.services.cloud_credentials.CloudCredentials.get_provider", lambda *args: credential)
    contract = CanonicalWireContract(
        metadata_source="fresh",
        product="Ram",
        version="2015-05-01",
        action="GetRole",
        style="RPC",
        method="POST",
        pathname="/",
        operation_type="read",
        auth_type="AK",
        signature_scheme="acs3",
        transport="tea",
        executable=True,
        unsupported_reasons=(),
        parameters=(ParameterMetadata("RoleName", "query", True, None, None, {"type": "string"}, None, None),),
        consumes=("application/json",),
        produces=("application/json",),
        policy_digest="fake-policy",
    )
    execute = AsyncMock(
        return_value=NormalizedApiResponse(
            status=200,
            headers=MappingProxyType({}),
            body={
                "Role": {
                    "RoleName": "DeployRole",
                    "RoleId": "111111111",
                    "AssumeRolePolicyDocument": "not-to-persist",
                }
            },
            content_type="application/json",
            content_encoding=None,
            size=100,
        )
    )
    prepare = Mock(return_value=SimpleNamespace(execute=execute))
    runtime = SimpleNamespace(
        contract_resolver=SimpleNamespace(resolve=AsyncMock(return_value=contract)),
        contract_store=ResolvedContractStore(),
        request_builder=RequestBuilder(),
        endpoint_resolver=SimpleNamespace(
            resolve=AsyncMock(return_value=EndpointResolution("ram.aliyuncs.com", "catalog_global", None))
        ),
        host_binding_resolver=SimpleNamespace(bind=lambda contract, endpoint, *args: endpoint),
        transport_router=SimpleNamespace(prepare=prepare),
        aclose=AsyncMock(),
    )
    monkeypatch.setattr("iac_code.tools.cloud.aliyun.runtime.create_aliyun_runtime_services", lambda **kwargs: runtime)

    async def inspect_gate(*args):
        decision = await check_tool_permission(*args)
        if not denied:
            assert decision.behavior == "allow", decision
        return decision

    monkeypatch.setattr("iac_code.services.permissions.pipeline.check_tool_permission", inspect_gate)
    context = ToolPermissionContext(
        cwd=str(tmp_path), deny_rules={"session": ["aliyun_api(ram:GetRole)"]} if denied else {}
    )
    if denied:
        with pytest.raises(ResourceQueryError, match="permission_denied"):
            await read_resource_attributes(
                plan, cwd=str(tmp_path), session_id="s", permission_context=context, cache_dir=tmp_path
            )
        prepare.assert_not_called()
    else:
        result = await read_resource_attributes(
            plan, cwd=str(tmp_path), session_id="s", permission_context=context, cache_dir=tmp_path
        )
        projected = project_query_result(plan, result)
        assert projected["attributes"] == {"RoleName": "DeployRole", "RoleId": "111111111"}
        assert "not-to-persist" not in str(projected)
        assert prepare.call_args.kwargs["credential"] is credential
        execute.assert_awaited_once()
    runtime.aclose.assert_awaited_once()
