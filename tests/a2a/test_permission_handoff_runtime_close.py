"""Permission ownership requires confirmed runtime teardown, including retries."""

import asyncio

import pytest

from iac_code.a2a.task_store import A2ATaskStore


class _Runtime:
    def __init__(self, *, blocked=False, fail=False):
        self.release = asyncio.Event()
        if not blocked:
            self.release.set()
        self.started = asyncio.Event()
        self.finished = asyncio.Event()
        self.calls = 0
        self.cancellations = 0
        self.fail = fail

    async def aclose(self):
        self.calls += 1
        self.started.set()
        try:
            await self.release.wait()
        except asyncio.CancelledError:
            self.cancellations += 1
            await self.release.wait()
        if self.fail:
            raise RuntimeError("runtime teardown failed")
        self.finished.set()


@pytest.fixture
def store(tmp_path, monkeypatch):
    monkeypatch.setenv("IAC_CODE_CONFIG_DIR", str(tmp_path / "config"))
    monkeypatch.setattr("iac_code.a2a.task_store._RUNTIME_CLOSE_TIMEOUT_SECONDS", 0.01)
    return A2ATaskStore()


async def create_context(store, runtime):
    return await store.get_or_create_context(context_id="ctx", cwd="/repo", runtime_factory=lambda _sid: runtime)


@pytest.mark.asyncio
async def test_strict_runtime_close_clears_cached_runtime_only_after_success(store):
    runtime = _Runtime()
    record = await create_context(store, runtime)
    await store.discard_context_runtime_for_input_handoff("ctx")
    assert runtime.finished.is_set()
    assert record.runtime is None
    await store.discard_context_runtime_for_input_handoff("ctx")
    assert runtime.calls == 1


@pytest.mark.asyncio
async def test_strict_runtime_close_timeout_retains_runtime_and_retries_same_task(store):
    runtime = _Runtime(blocked=True)
    record = await create_context(store, runtime)
    try:
        with pytest.raises(asyncio.TimeoutError):
            await store.discard_context_runtime_for_input_handoff("ctx")
        assert record.runtime is runtime
        assert not runtime.finished.is_set()
        with pytest.raises(RuntimeError, match="closing"):
            await create_context(store, _Runtime())
        with pytest.raises(asyncio.TimeoutError):
            await store.discard_context_runtime_for_input_handoff("ctx")
        assert runtime.calls == 1
        assert runtime.cancellations == 0
        runtime.release.set()
        await store.discard_context_runtime_for_input_handoff("ctx")
        assert runtime.finished.is_set()
        assert record.runtime is None
        assert runtime.calls == 1
    finally:
        runtime.release.set()
        await asyncio.sleep(0)


@pytest.mark.asyncio
async def test_strict_runtime_close_failure_is_not_a_drain_receipt(store):
    runtime = _Runtime(fail=True)
    record = await create_context(store, runtime)
    for _ in range(2):
        with pytest.raises(RuntimeError, match="teardown failed"):
            await store.discard_context_runtime_for_input_handoff("ctx")
    assert record.runtime is runtime
    assert runtime.calls == 1
    with pytest.raises(RuntimeError, match="closing"):
        await create_context(store, _Runtime())


@pytest.mark.asyncio
async def test_request_cancellation_does_not_cancel_or_duplicate_strict_runtime_close(store):
    runtime = _Runtime(blocked=True)
    record = await create_context(store, runtime)
    closing = asyncio.create_task(store.discard_context_runtime_for_input_handoff("ctx"))
    await runtime.started.wait()
    closing.cancel()
    with pytest.raises(asyncio.CancelledError):
        await closing
    assert record.runtime is runtime
    runtime.release.set()
    await store.discard_context_runtime_for_input_handoff("ctx")
    assert runtime.finished.is_set()
    assert runtime.calls == 1
    assert runtime.cancellations == 0


@pytest.mark.asyncio
async def test_concurrent_strict_runtime_close_calls_share_one_teardown(store):
    runtime = _Runtime()
    await create_context(store, runtime)
    await asyncio.gather(
        store.discard_context_runtime_for_input_handoff("ctx"),
        store.discard_context_runtime_for_input_handoff("ctx"),
    )
    assert runtime.calls == 1


@pytest.mark.asyncio
async def test_ordinary_discard_reuses_a_pending_strict_close(store):
    runtime = _Runtime(blocked=True)
    record = await create_context(store, runtime)
    with pytest.raises(asyncio.TimeoutError):
        await store.discard_context_runtime_for_input_handoff("ctx")
    runtime.release.set()
    await store.discard_context_runtime("ctx")
    assert runtime.calls == 1
    assert record.runtime is None


@pytest.mark.asyncio
async def test_shutdown_does_not_start_second_close_for_failed_strict_teardown(store):
    runtime = _Runtime(fail=True)
    await create_context(store, runtime)
    with pytest.raises(RuntimeError, match="teardown failed"):
        await store.discard_context_runtime_for_input_handoff("ctx")
    await store.stop_cleanup_loop()
    assert runtime.calls == 1


@pytest.mark.asyncio
async def test_strict_close_blocks_reconciliation_until_retry_confirms_success(store):
    runtime = _Runtime(blocked=True)
    await create_context(store, runtime)
    with pytest.raises(asyncio.TimeoutError):
        await store.discard_context_runtime_for_input_handoff("ctx")
    assert await store.context_reconciliation_is_blocked("ctx")
    with pytest.raises(RuntimeError, match="closing"):
        await store.ensure_context_reconciliation_safe("ctx")
    runtime.release.set()
    await store.discard_context_runtime_for_input_handoff("ctx")
    assert not await store.context_reconciliation_is_blocked("ctx")


@pytest.mark.asyncio
async def test_runtime_creation_inflight_cannot_be_declared_drained(store):
    gate = asyncio.Event()
    creating = asyncio.create_task(gate.wait())
    store._context_runtime_tasks["ctx"] = creating
    try:
        with pytest.raises(RuntimeError, match="initializing"):
            await store.discard_context_runtime_for_input_handoff("ctx")
    finally:
        gate.set()
        await creating
        store._context_runtime_tasks.pop("ctx")
