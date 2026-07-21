from __future__ import annotations

import asyncio
import importlib
import threading
from dataclasses import fields, replace
from types import MappingProxyType
from typing import Any

import pytest

from iac_code.agent.agent_loop import AgentLoop
from iac_code.tools.base import Tool, ToolContext, ToolRegistry, ToolResult
from iac_code.tools.cloud.aliyun.api_contract import ApiCallShape, CanonicalWireContract
from iac_code.tools.cloud.aliyun.contract_store import PROCESS_RESOLVED_CONTRACT_STORE
from iac_code.types.permissions import (
    PermissionAuditMetadata,
    PermissionResult,
    ToolPermissionContext,
)
from iac_code.types.stream_events import (
    MessageEndEvent,
    MessageStartEvent,
    PermissionRequestEvent,
    ToolUseEndEvent,
    ToolUseStartEvent,
    Usage,
)


def _contract(**changes: Any) -> CanonicalWireContract:
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
        "consumes": (),
        "produces": ("application/json",),
        "policy_digest": "fixture-policy",
    }
    values.update(changes)
    return CanonicalWireContract(**values)


def _shape(**changes: Any) -> ApiCallShape:
    values: dict[str, Any] = {
        "product": "Ecs",
        "version": "2014-05-26",
        "action": "DescribeInstances",
        "region_id": "cn-hangzhou",
        "explicit_overrides": (),
        "parameter_names_by_location": MappingProxyType({"query": ("RegionId",)}),
        "body_source": "none",
    }
    values.update(changes)
    return ApiCallShape(**values)


def _contract_api() -> tuple[Any, Any, Any, Any]:
    permissions = importlib.import_module("iac_code.types.permissions")
    store_module = importlib.import_module("iac_code.tools.cloud.aliyun.contract_store")
    return (
        getattr(permissions, "InvocationBinding"),
        getattr(store_module, "ResolvedContractError"),
        getattr(store_module, "ResolvedContractStore"),
        getattr(store_module, "canonical_input_sha256"),
    )


def _binding(**changes: Any) -> Any:
    invocation_binding_type, _, _, canonical_input_sha256 = _contract_api()
    values = {
        "runtime_nonce": "runtime-a",
        "session_id": "session-a",
        "tool_use_id": "tool-use-a",
        "tool_name": "aliyun_api",
        "canonical_input_sha256": canonical_input_sha256(
            {
                "product": "ecs",
                "action": "DescribeInstances",
                "params": {"InstanceName": "alpha"},
            }
        ),
    }
    values.update(changes)
    return invocation_binding_type(**values)


def test_invocation_binding_has_only_the_five_canonical_fields() -> None:
    invocation_binding_type, _, _, _ = _contract_api()

    assert [field.name for field in fields(invocation_binding_type)] == [
        "runtime_nonce",
        "session_id",
        "tool_use_id",
        "tool_name",
        "canonical_input_sha256",
    ]
    assert invocation_binding_type.__dataclass_params__.frozen is True


def test_input_hash_covers_complete_values_and_path_string_but_never_reads_file(tmp_path: Any) -> None:
    _, _, _, canonical_input_sha256 = _contract_api()
    body_file = tmp_path / "payload.bin"
    body_file.write_bytes(b"first")
    base = {
        "product": "ecs",
        "action": "DescribeInstances",
        "params": {"Name": "alpha", "Nested": {"enabled": True}},
        "body_file": str(body_file),
    }

    base_hash = canonical_input_sha256(base)
    assert canonical_input_sha256({**base, "params": {**base["params"], "Name": "beta"}}) != base_hash
    assert canonical_input_sha256({**base, "body_file": str(tmp_path / "other.bin")}) != base_hash

    body_file.write_bytes(b"second-content-does-not-enter-the-hash")
    assert canonical_input_sha256(base) == base_hash
    assert canonical_input_sha256(dict(reversed(list(base.items())))) == base_hash


def test_store_is_one_shot_and_consumes_binding_or_digest_mismatch() -> None:
    _, resolved_contract_error, resolved_contract_store_type, _ = _contract_api()
    store = resolved_contract_store_type()
    binding = _binding()
    contract = _contract()
    digest = contract.security_digest(_shape())

    snapshot_id = store.create(
        binding=binding,
        contract=contract,
        security_digest=digest,
        execution_class="concurrent",
    )
    snapshot = store.consume(snapshot_id=snapshot_id, binding=binding, security_digest=digest)
    assert snapshot.binding == binding
    assert snapshot.contract == contract
    assert snapshot.security_digest == digest
    assert snapshot.execution_class == "concurrent"
    with pytest.raises(resolved_contract_error, match="snapshot_not_found"):
        store.consume(snapshot_id=snapshot_id, binding=binding, security_digest=digest)

    for mismatch in ("binding", "digest"):
        snapshot_id = store.create(
            binding=binding,
            contract=contract,
            security_digest=digest,
            execution_class="serial",
        )
        kwargs = {
            "snapshot_id": snapshot_id,
            "binding": replace(binding, tool_use_id="other") if mismatch == "binding" else binding,
            "security_digest": "0" * 64 if mismatch == "digest" else digest,
        }
        with pytest.raises(resolved_contract_error, match=f"snapshot_{mismatch}_mismatch"):
            store.consume(**kwargs)
        with pytest.raises(resolved_contract_error, match="snapshot_not_found"):
            store.consume(snapshot_id=snapshot_id, binding=binding, security_digest=digest)


@pytest.mark.parametrize(
    ("binding_changes", "digest_override", "expected_error"),
    [
        ({"runtime_nonce": "runtime-b"}, None, "snapshot_binding_mismatch"),
        ({"session_id": "session-b"}, None, "snapshot_binding_mismatch"),
        ({"tool_use_id": "tool-use-b"}, None, "snapshot_binding_mismatch"),
        ({"tool_name": "ros_validate_template"}, None, "snapshot_binding_mismatch"),
        ({"canonical_input_sha256": "0" * 64}, None, "snapshot_binding_mismatch"),
        ({}, "0" * 64, "snapshot_digest_mismatch"),
    ],
)
def test_expired_store_validates_every_binding_field_and_digest_before_expiration(
    binding_changes: dict[str, str],
    digest_override: str | None,
    expected_error: str,
) -> None:
    _, resolved_contract_error, resolved_contract_store_type, _ = _contract_api()
    now = [100.0]
    store = resolved_contract_store_type(ttl_seconds=1.0, clock=lambda: now[0])
    binding = _binding()
    contract = _contract()
    digest = contract.security_digest(_shape())
    snapshot_id = store.create(
        binding=binding,
        contract=contract,
        security_digest=digest,
        execution_class="concurrent",
    )
    now[0] += 2.0

    with pytest.raises(resolved_contract_error, match=expected_error):
        store.consume(
            snapshot_id=snapshot_id,
            binding=replace(binding, **binding_changes),
            security_digest=digest_override or digest,
        )
    with pytest.raises(resolved_contract_error, match="snapshot_not_found"):
        store.consume(snapshot_id=snapshot_id, binding=binding, security_digest=digest)


def test_store_capacity_ttl_and_cancel_are_bounded_and_idempotent() -> None:
    _, resolved_contract_error, resolved_contract_store_type, _ = _contract_api()
    now = [100.0]
    store = resolved_contract_store_type(max_entries=2, ttl_seconds=900.0, clock=lambda: now[0])
    binding = _binding()
    contract = _contract()
    digest = contract.security_digest(_shape())

    first = store.create(
        binding=binding,
        contract=contract,
        security_digest=digest,
        execution_class="concurrent",
    )
    second = store.create(
        binding=replace(binding, tool_use_id="second"),
        contract=contract,
        security_digest=digest,
        execution_class="concurrent",
    )
    third_binding = replace(binding, tool_use_id="third")
    third = store.create(
        binding=third_binding,
        contract=contract,
        security_digest=digest,
        execution_class="serial",
    )
    assert store.size == 2
    first_recovery = store.consume(snapshot_id=first, binding=binding, security_digest=digest)
    assert first_recovery.execution_class == "concurrent"
    store.reject_recovery(first, first_recovery.claim_id)

    store.cancel(second)
    store.cancel(second)
    with pytest.raises(resolved_contract_error, match="snapshot_not_found"):
        store.consume(snapshot_id=second, binding=replace(binding, tool_use_id="second"), security_digest=digest)

    now[0] += 901.0
    third_recovery = store.consume(snapshot_id=third, binding=third_binding, security_digest=digest)
    assert third_recovery.execution_class == "serial"
    store.cancel_recovery(third, third_recovery.claim_id)
    assert store.size == 0


def test_capacity_eviction_grants_one_recovery_claim_then_terminalizes_consumed() -> None:
    _, resolved_contract_error, resolved_contract_store_type, _ = _contract_api()
    store = resolved_contract_store_type(max_entries=1)
    binding = _binding()
    contract = _contract()
    digest = contract.security_digest(_shape())
    snapshot_id = store.create(
        binding=binding,
        contract=contract,
        security_digest=digest,
        execution_class="concurrent",
    )
    store.create(
        binding=replace(binding, tool_use_id="evicting-call"),
        contract=contract,
        security_digest=digest,
        execution_class="serial",
    )

    recovery = store.consume(snapshot_id=snapshot_id, binding=binding, security_digest=digest)

    assert recovery.binding == binding
    assert recovery.security_digest == digest
    assert recovery.execution_class == "concurrent"
    assert isinstance(recovery.claim_id, str) and len(recovery.claim_id) >= 32
    assert not hasattr(recovery, "contract")
    store.complete_recovery(
        snapshot_id=snapshot_id,
        claim_id=recovery.claim_id,
        binding=binding,
        security_digest=digest,
        execution_class="concurrent",
    )
    with pytest.raises(resolved_contract_error, match="snapshot_not_found") as replay:
        store.consume(snapshot_id=snapshot_id, binding=binding, security_digest=digest)
    assert replay.value.lifecycle == "consumed"


@pytest.mark.parametrize("terminal_state", ["consumed", "cancelled", "rejected"])
def test_terminal_snapshot_states_never_recover_and_replays_remain_terminal(terminal_state: str) -> None:
    _, resolved_contract_error, resolved_contract_store_type, _ = _contract_api()
    store = resolved_contract_store_type()
    binding = _binding()
    contract = _contract()
    digest = contract.security_digest(_shape())
    snapshot_id = store.create(
        binding=binding,
        contract=contract,
        security_digest=digest,
        execution_class="concurrent",
    )
    if terminal_state == "consumed":
        store.consume(snapshot_id=snapshot_id, binding=binding, security_digest=digest)
    elif terminal_state == "cancelled":
        store.cancel(snapshot_id)
    else:
        store.reject(snapshot_id)

    with pytest.raises(resolved_contract_error, match="snapshot_not_found") as first_replay:
        store.consume(snapshot_id=snapshot_id, binding=binding, security_digest=digest)
    assert first_replay.value.lifecycle == terminal_state
    with pytest.raises(resolved_contract_error, match="snapshot_not_found") as repeated_replay:
        store.consume(snapshot_id=snapshot_id, binding=binding, security_digest=digest)
    assert repeated_replay.value.lifecycle == "replayed"


def test_snapshot_and_tombstone_accounting_is_bounded_with_random_default_tokens() -> None:
    _, _, resolved_contract_store_type, _ = _contract_api()
    store = resolved_contract_store_type(max_entries=2)
    binding = _binding()
    contract = _contract()
    digest = contract.security_digest(_shape())
    identifiers: set[str] = set()

    for index in range(12):
        snapshot_id = store.create(
            binding=replace(binding, tool_use_id=f"call-{index}"),
            contract=contract,
            security_digest=digest,
            execution_class="concurrent",
        )
        identifiers.add(snapshot_id)
        if index % 3 == 0:
            store.cancel(snapshot_id)

    assert len(identifiers) == 12
    assert all(len(snapshot_id) >= 32 for snapshot_id in identifiers)
    assert len(store._entries) <= 2
    assert len(store._recoverable) <= 2
    assert len(store._terminal) <= 2


def test_store_is_thread_lock_only_and_works_from_distinct_event_loops() -> None:
    _, _, resolved_contract_store_type, _ = _contract_api()
    store = resolved_contract_store_type()
    assert isinstance(store._lock, type(threading.Lock()))
    assert not any(isinstance(value, (asyncio.Lock, asyncio.Event, asyncio.Future)) for value in vars(store).values())

    binding = _binding()
    contract = _contract()
    digest = contract.security_digest(_shape())

    async def create_in_loop() -> str:
        return store.create(
            binding=binding,
            contract=contract,
            security_digest=digest,
            execution_class="concurrent",
        )

    async def consume_in_loop(snapshot_id: str) -> Any:
        await asyncio.sleep(0)
        return store.consume(snapshot_id=snapshot_id, binding=binding, security_digest=digest)

    snapshot_id = asyncio.run(create_in_loop())
    consumed = asyncio.run(consume_in_loop(snapshot_id))
    assert consumed.contract is contract


def test_recovery_claim_is_one_shot_across_concurrent_event_loops() -> None:
    _, resolved_contract_error, resolved_contract_store_type, _ = _contract_api()
    store = resolved_contract_store_type(max_entries=1)
    binding = _binding()
    contract = _contract()
    digest = contract.security_digest(_shape())
    snapshot_id = store.create(
        binding=binding,
        contract=contract,
        security_digest=digest,
        execution_class="concurrent",
    )
    evicting_id = store.create(
        binding=replace(binding, tool_use_id="other-call"),
        contract=contract,
        security_digest=digest,
        execution_class="serial",
    )
    barrier = threading.Barrier(2)
    outcomes: list[Any] = []
    outcomes_lock = threading.Lock()

    def consume_from_thread() -> None:
        async def consume() -> Any:
            barrier.wait()
            await asyncio.sleep(0)
            return store.consume(snapshot_id=snapshot_id, binding=binding, security_digest=digest)

        try:
            outcome: Any = asyncio.run(consume())
        except BaseException as error:
            outcome = error
        with outcomes_lock:
            outcomes.append(outcome)

    threads = [threading.Thread(target=consume_from_thread) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=2)

    assert all(not thread.is_alive() for thread in threads)
    recoveries = [outcome for outcome in outcomes if not isinstance(outcome, BaseException)]
    failures = [outcome for outcome in outcomes if isinstance(outcome, BaseException)]
    assert len(recoveries) == 1
    assert len(failures) == 1
    assert isinstance(failures[0], resolved_contract_error)
    assert failures[0].lifecycle == "recovering"
    recovery = recoveries[0]
    store.complete_recovery(
        snapshot_id=snapshot_id,
        claim_id=recovery.claim_id,
        binding=binding,
        security_digest=digest,
        execution_class="concurrent",
    )
    store.cancel(evicting_id)


def test_inflight_recovery_capacity_never_discards_another_approved_snapshot() -> None:
    _, resolved_contract_error, resolved_contract_store_type, _ = _contract_api()
    store = resolved_contract_store_type(max_entries=1)
    binding = _binding()
    contract = _contract()
    digest = contract.security_digest(_shape())
    first = store.create(
        binding=binding,
        contract=contract,
        security_digest=digest,
        execution_class="concurrent",
    )
    second_binding = replace(binding, tool_use_id="second")
    second = store.create(
        binding=second_binding,
        contract=contract,
        security_digest=digest,
        execution_class="serial",
    )
    recovery = store.consume(snapshot_id=first, binding=binding, security_digest=digest)

    with pytest.raises(resolved_contract_error, match="snapshot_capacity_exhausted"):
        store.create(
            binding=replace(binding, tool_use_id="third"),
            contract=contract,
            security_digest=digest,
            execution_class="serial",
        )

    preserved = store.consume(snapshot_id=second, binding=second_binding, security_digest=digest)
    assert preserved.contract is contract
    assert preserved.binding == second_binding
    store.cancel_recovery(first, recovery.claim_id)


class _OneCallProvider:
    def __init__(self, tool_input: dict[str, Any]) -> None:
        self.tool_input = tool_input

    def get_model_name(self) -> str:
        return "fake"

    async def stream(self, messages, system, tools=None, max_tokens=8192):
        yield MessageStartEvent(message_id="message")
        yield ToolUseStartEvent(tool_use_id="tool-use", name="snapshot_tool")
        yield ToolUseEndEvent(tool_use_id="tool-use", name="snapshot_tool", input=self.tool_input)
        yield MessageEndEvent(stop_reason="tool_use", usage=Usage())


class _SnapshotTool(Tool):
    def __init__(
        self,
        *,
        behavior: str,
        consume: bool = False,
        block: bool = False,
    ) -> None:
        self.behavior = behavior
        self.consume = consume
        self.block = block
        self.snapshot_ids: list[str] = []
        self.permission_contexts: list[ToolPermissionContext] = []
        self.execute_started = asyncio.Event()
        self.release = asyncio.Event()

    @property
    def name(self) -> str:
        return "snapshot_tool"

    @property
    def description(self) -> str:
        return "Snapshot lifecycle test tool"

    @property
    def input_schema(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {"value": {"type": "string"}},
            "required": ["value"],
        }

    @property
    def requires_runtime_execution_class(self) -> bool:
        return True

    async def check_permissions(self, input: dict, context=None) -> PermissionResult:
        assert isinstance(context, ToolPermissionContext)
        assert context.invocation_binding is not None
        assert context.invocation_binding.tool_use_id == "tool-use"
        assert context.invocation_binding.tool_name == self.name
        assert context.invocation_binding.canonical_input_sha256 == _contract_api()[3](input)
        self.permission_contexts.append(context)
        contract = _contract()
        digest = contract.security_digest(_shape())
        snapshot_id = PROCESS_RESOLVED_CONTRACT_STORE.create(
            binding=context.invocation_binding,
            contract=contract,
            security_digest=digest,
            execution_class="serial",
        )
        self.snapshot_ids.append(snapshot_id)
        return PermissionResult(
            behavior=self.behavior,  # type: ignore[arg-type]
            message="Allow snapshot?",
            audit=PermissionAuditMetadata(
                scope="once",
                source="snapshot_tool",
                is_read_only=False,
            ),
            invocation_binding=context.invocation_binding,
            snapshot_id=snapshot_id,
            security_digest=digest,
            execution_class="serial",
        )

    async def execute(self, *, tool_input: dict[str, Any], context: ToolContext) -> ToolResult:
        self.execute_started.set()
        if self.consume:
            assert context.invocation_binding is not None
            assert context.snapshot_id is not None
            assert context.security_digest is not None
            PROCESS_RESOLVED_CONTRACT_STORE.consume(
                snapshot_id=context.snapshot_id,
                binding=context.invocation_binding,
                security_digest=context.security_digest,
            )
        if self.block:
            await self.release.wait()
        return ToolResult.success("ok")


class _PreparedSnapshotTool(_SnapshotTool):
    def __init__(self) -> None:
        super().__init__(behavior="allow")
        self.inputs: list[dict[str, Any]] = []

    def prepare_invocation_input(self, tool_input: dict[str, Any]) -> dict[str, Any]:
        return {**tool_input, "region_id": "cn-shanghai"}

    async def check_permissions(self, input: dict, context=None) -> PermissionResult:
        assert input["region_id"] == "cn-shanghai"
        return await super().check_permissions(input, context)

    async def execute(self, *, tool_input: dict[str, Any], context: ToolContext) -> ToolResult:
        self.inputs.append(tool_input)
        return await super().execute(tool_input=tool_input, context=context)


def _snapshot_loop(
    tool: _SnapshotTool,
    tool_input: dict[str, Any],
    permission_context: ToolPermissionContext,
) -> AgentLoop:
    registry = ToolRegistry()
    registry.register(tool)
    return AgentLoop(
        provider_manager=_OneCallProvider(tool_input),
        system_prompt="system",
        tool_registry=registry,
        max_turns=1,
        session_id="session",
        permission_context=permission_context,
    )


@pytest.mark.asyncio
async def test_agent_loop_prepares_effective_input_before_permission_binding_and_execution() -> None:
    raw_input = {"value": "business"}
    tool = _PreparedSnapshotTool()
    loop = _snapshot_loop(tool, raw_input, ToolPermissionContext(cwd="/tmp"))

    async for _event in loop.run_streaming("run"):
        pass

    assert raw_input == {"value": "business"}
    assert tool.inputs == [{"value": "business", "region_id": "cn-shanghai"}]
    binding = tool.permission_contexts[0].invocation_binding
    assert binding is not None
    assert binding.canonical_input_sha256 == _contract_api()[3](tool.inputs[0])


@pytest.mark.asyncio
@pytest.mark.parametrize("behavior", ["deny", "ask"])
async def test_agent_loop_rejects_snapshot_on_deny_or_prompt_rejection(behavior: str) -> None:
    assert PROCESS_RESOLVED_CONTRACT_STORE.size == 0
    shared = ToolPermissionContext(cwd="/tmp")
    tool = _SnapshotTool(behavior=behavior)
    loop = _snapshot_loop(tool, {"value": "business"}, shared)

    async for event in loop.run_streaming("run"):
        if isinstance(event, PermissionRequestEvent):
            event.response_future.set_result(False)

    assert PROCESS_RESOLVED_CONTRACT_STORE.size == 0
    assert shared.invocation_binding is None
    assert tool.permission_contexts[0] is not shared
    _, resolved_contract_error, _, _ = _contract_api()
    with pytest.raises(resolved_contract_error, match="snapshot_not_found") as terminal:
        PROCESS_RESOLVED_CONTRACT_STORE.consume(
            snapshot_id=tool.snapshot_ids[0],
            binding=tool.permission_contexts[0].invocation_binding,
            security_digest=_contract().security_digest(_shape()),
        )
    assert terminal.value.lifecycle == "rejected"


@pytest.mark.asyncio
@pytest.mark.parametrize("entrypoint", ["run_streaming", "continue_streaming"])
async def test_agent_loop_cancels_snapshot_when_permission_prompt_is_cancelled(entrypoint: str) -> None:
    assert PROCESS_RESOLVED_CONTRACT_STORE.size == 0
    tool = _SnapshotTool(behavior="ask")
    loop = _snapshot_loop(tool, {"value": "business"}, ToolPermissionContext(cwd="/tmp"))
    prompt_ready = asyncio.Event()

    async def consume() -> None:
        stream = loop.run_streaming("run") if entrypoint == "run_streaming" else loop.continue_streaming()
        async for event in stream:
            if isinstance(event, PermissionRequestEvent):
                prompt_ready.set()

    task = asyncio.create_task(consume())
    await asyncio.wait_for(prompt_ready.wait(), timeout=1)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert PROCESS_RESOLVED_CONTRACT_STORE.size == 0
    _, resolved_contract_error, _, _ = _contract_api()
    with pytest.raises(resolved_contract_error, match="snapshot_not_found") as terminal:
        PROCESS_RESOLVED_CONTRACT_STORE.consume(
            snapshot_id=tool.snapshot_ids[0],
            binding=tool.permission_contexts[0].invocation_binding,
            security_digest=_contract().security_digest(_shape()),
        )
    assert terminal.value.lifecycle == "cancelled"


@pytest.mark.asyncio
async def test_agent_loop_cancels_snapshot_on_audit_or_pre_execution_failure(monkeypatch) -> None:
    async def run_case(*, fail_audit: bool, tool_input: dict[str, Any]) -> None:
        tool = _SnapshotTool(behavior="allow")
        loop = _snapshot_loop(tool, tool_input, ToolPermissionContext(cwd="/tmp"))
        monkeypatch.setattr(
            "iac_code.agent.agent_loop._emit_no_prompt_permission_audit",
            lambda **kwargs: not fail_audit,
        )
        async for _event in loop.run_streaming("run"):
            pass
        assert PROCESS_RESOLVED_CONTRACT_STORE.size == 0
        assert tool.execute_started.is_set() is (not fail_audit and "value" in tool_input)

    await run_case(fail_audit=True, tool_input={"value": "business"})
    await run_case(fail_audit=False, tool_input={})


@pytest.mark.asyncio
async def test_agent_loop_cancels_unconsumed_snapshot_on_batch_cancellation() -> None:
    tool = _SnapshotTool(behavior="allow", block=True)
    loop = _snapshot_loop(tool, {"value": "business"}, ToolPermissionContext(cwd="/tmp"))

    async def consume() -> None:
        async for _event in loop.run_streaming("run"):
            pass

    task = asyncio.create_task(consume())
    await asyncio.wait_for(tool.execute_started.wait(), timeout=1)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert PROCESS_RESOLVED_CONTRACT_STORE.size == 0


@pytest.mark.asyncio
async def test_agent_loop_releases_consumed_snapshot_on_batch_cancellation(monkeypatch) -> None:
    cleanup_cancels = []
    original_cancel = PROCESS_RESOLVED_CONTRACT_STORE.cancel

    def record_cancel(snapshot_id: str) -> None:
        cleanup_cancels.append(snapshot_id)
        original_cancel(snapshot_id)

    monkeypatch.setattr(PROCESS_RESOLVED_CONTRACT_STORE, "cancel", record_cancel)
    tool = _SnapshotTool(behavior="allow", consume=True, block=True)
    loop = _snapshot_loop(tool, {"value": "business"}, ToolPermissionContext(cwd="/tmp"))

    async def consume() -> None:
        async for _event in loop.run_streaming("run"):
            pass

    task = asyncio.create_task(consume())
    await asyncio.wait_for(tool.execute_started.wait(), timeout=1)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert PROCESS_RESOLVED_CONTRACT_STORE.size == 0
    assert cleanup_cancels == []


@pytest.mark.asyncio
async def test_agent_loop_success_hands_off_and_consumes_snapshot_once(monkeypatch) -> None:
    cleanup_cancels = []
    original_cancel = PROCESS_RESOLVED_CONTRACT_STORE.cancel

    def record_cancel(snapshot_id: str) -> None:
        cleanup_cancels.append(snapshot_id)
        original_cancel(snapshot_id)

    monkeypatch.setattr(PROCESS_RESOLVED_CONTRACT_STORE, "cancel", record_cancel)
    tool = _SnapshotTool(behavior="allow", consume=True)
    loop = _snapshot_loop(tool, {"value": "business"}, ToolPermissionContext(cwd="/tmp"))

    async for _event in loop.run_streaming("run"):
        pass

    assert tool.execute_started.is_set()
    assert PROCESS_RESOLVED_CONTRACT_STORE.size == 0
    assert cleanup_cancels == []
    _, resolved_contract_error, _, _ = _contract_api()
    with pytest.raises(resolved_contract_error, match="snapshot_not_found"):
        PROCESS_RESOLVED_CONTRACT_STORE.consume(
            snapshot_id=tool.snapshot_ids[0],
            binding=tool.permission_contexts[0].invocation_binding,
            security_digest=_contract().security_digest(_shape()),
        )
