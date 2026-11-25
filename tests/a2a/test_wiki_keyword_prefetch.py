"""Overlap tool-free keyword inference while retaining scoped Wiki semantics."""

from __future__ import annotations

import asyncio

import pytest

from iac_code.agent.message import Message
from iac_code.providers.base import NonStreamingResponse
from iac_code.services import wiki_auto_search as wiki
from iac_code.tools.base import ToolContext
from iac_code.types.stream_events import Usage


class Manager:
    def __init__(self):
        self.started = asyncio.Event()
        self.release = asyncio.Event()
        self.closed = asyncio.Event()
        self.calls = 0

    def for_side_call(self, policy):
        assert policy.thinking_enabled is False
        return self

    def get_model_name(self):
        return "fixture"

    async def complete(self, **kwargs):
        self.calls += 1
        self.started.set()
        try:
            await self.release.wait()
            return NonStreamingResponse(
                message_id="fixture",
                text='{"keywords":["ECS"]}',
                tool_uses=[],
                stop_reason="end_turn",
                usage=Usage(input_tokens=7),
            )
        finally:
            self.closed.set()


def start_work(manager, tmp_path, usage, *, query="ECS", recent=None, permission=None):
    return wiki.WikiAutoSearch(manager, root=tmp_path).start(
        Message(role="user", content=query),
        scope_id="fixture-step",
        serial=True,
        context=ToolContext(cwd=str(tmp_path)),
        permission=permission,
        recent=recent or [],
        persist=lambda snapshot: None,
        record_usage=lambda *args: usage.append(args),
    )


@pytest.mark.asyncio
async def test_prefetch_overlaps_preparation_is_adopted_once_and_uses_step_permissions(tmp_path, monkeypatch):
    manager = Manager()
    root_usage, step_usage, searches = [], [], []
    permission = object()

    async def search(root, keywords, context, permission, **kwargs):
        searches.append((root, keywords, permission))
        return []

    monkeypatch.setattr(wiki, "search", search)
    async with wiki.prefetch_wiki_keywords(manager, "ECS", lambda *args: root_usage.append(args)):
        await manager.started.wait()
        # Preparation can complete while inference is still in flight.
        work = start_work(manager, tmp_path, step_usage, permission=permission)
        await asyncio.sleep(0)
        assert not work.task.done()
        manager.release.set()
        await work.task
        assert work.snapshot["keywords"] == ["ECS"]
        assert work.snapshot["status"] == "no_matches"
    assert manager.calls == 1
    assert len(step_usage) == 1 and root_usage == []
    assert searches == [(tmp_path, ["ECS"], permission)]
    assert wiki._KEYWORD_PREFETCH.get() is None


@pytest.mark.asyncio
@pytest.mark.parametrize("mismatch", ["query", "provider", "history"])
async def test_prefetch_matches_exact_input_and_provider_only(tmp_path, monkeypatch, mismatch):
    manager = Manager()
    manager.release.set()
    other = Manager()
    other.release.set()
    root_usage, step_usage = [], []

    async def search(*args, **kwargs):
        return []

    monkeypatch.setattr(wiki, "search", search)
    async with wiki.prefetch_wiki_keywords(manager, "ECS", lambda *args: root_usage.append(args)):
        work = start_work(
            other if mismatch == "provider" else manager,
            tmp_path,
            step_usage,
            query="VPC" if mismatch == "query" else "ECS",
            recent=["previous question"] if mismatch == "history" else [],
        )
        await work.task
    assert manager.calls + other.calls == 2
    assert len(root_usage) == len(step_usage) == 1


@pytest.mark.asyncio
async def test_unused_prefetch_cancellation_drains_inference():
    manager = Manager()
    async with wiki.prefetch_wiki_keywords(manager, "ECS", lambda *args: None):
        await manager.started.wait()
    assert manager.closed.is_set()
    assert wiki._KEYWORD_PREFETCH.get() is None


@pytest.mark.asyncio
async def test_owner_cancel_drains_adopted_inference(tmp_path):
    manager = Manager()
    created = asyncio.Event()

    async def owner():
        async with wiki.prefetch_wiki_keywords(manager, "ECS", lambda *args: None):
            work = start_work(manager, tmp_path, [])
            while not wiki._KEYWORD_PREFETCH.get().used:
                await asyncio.sleep(0)
            created.set()
            await work.task

    task = asyncio.create_task(owner())
    await created.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert manager.closed.is_set()


@pytest.mark.asyncio
async def test_prefetch_failure_keeps_existing_keyword_fallback(tmp_path, monkeypatch):
    manager = Manager()

    async def fail(**kwargs):
        raise RuntimeError("fixture inference failed")

    async def search(*args, **kwargs):
        return []

    monkeypatch.setattr(manager, "complete", fail)
    monkeypatch.setattr(wiki, "search", search)
    async with wiki.prefetch_wiki_keywords(manager, "ECS", lambda *args: None):
        work = start_work(manager, tmp_path, [])
        await work.task
    assert work.snapshot["keywords"] == ["ECS"]
    assert work.snapshot["status"] == "no_matches"


@pytest.mark.asyncio
async def test_executor_dispatches_keyword_request_while_pipeline_prepares(tmp_path, monkeypatch):
    import threading
    from types import SimpleNamespace

    from iac_code.a2a.executor import IacCodeA2AExecutor
    from iac_code.a2a.metrics import NoOpA2AMetrics
    from iac_code.a2a.task_store import A2ATaskStore
    from iac_code.providers.manager import ProviderManager
    from iac_code.types.stream_events import TextDeltaEvent
    from tests.a2a.fakes import FakeEventQueue, FakeRequestContext
    from tests.a2a.test_pipeline_executor import FakePipeline, _fake_runtime

    monkeypatch.setenv("IAC_CODE_MODE", "pipeline")
    monkeypatch.setenv("IAC_CODE_PIPELINE_NAME", "selling_solution_first")
    monkeypatch.setenv("IAC_CODE_WIKI_AUTO_SEARCH", "1")
    preparing = threading.Event()
    inference = threading.Event()
    step_usage = []
    main_thread = threading.get_ident()

    class Provider:
        _PROVIDER_KEY = "openai"

        async def complete(self, *args, **kwargs):
            inference.set()
            assert await asyncio.to_thread(preparing.wait, 3)
            return NonStreamingResponse(
                message_id="fixture",
                text='{"keywords":["ECS"]}',
                tool_uses=[],
                stop_reason="end_turn",
                usage=Usage(),
            )

    monkeypatch.setattr("iac_code.providers.manager.create_provider", lambda *args, **kwargs: Provider())
    manager = ProviderManager(model="fixture", credentials={}, ignore_llm_source=True)
    runtime = _fake_runtime()
    runtime.provider_manager = manager
    runtime.agent_loop = SimpleNamespace(_record_session_usage=lambda *args: pytest.fail("inference was not adopted"))

    class Pipeline(FakePipeline):
        async def run(self, prompt):
            work = start_work(manager, tmp_path, step_usage, query="hello")
            await work.task
            yield TextDeltaEvent(text="answer after Wiki")

    pipeline = Pipeline([], session_dir=tmp_path / "pipeline")

    def create(*args, **kwargs):
        assert threading.get_ident() != main_thread
        preparing.set()
        assert inference.wait(3), "inference did not overlap local preparation"
        return pipeline

    async def search(*args, **kwargs):
        return []

    monkeypatch.setattr(wiki, "search", search)
    monkeypatch.setattr("iac_code.a2a.pipeline_executor.create_pipeline", create)
    monkeypatch.setattr("iac_code.a2a.pipeline_executor.create_agent_runtime", lambda options: runtime)
    store = A2ATaskStore(metrics=NoOpA2AMetrics())
    executor = IacCodeA2AExecutor(task_store=store, model="fixture")
    queue = FakeEventQueue()
    await executor.execute(FakeRequestContext(metadata={"iac_code": {"cwd": str(tmp_path)}}), queue)
    assert preparing.is_set() and inference.is_set()
    assert len(step_usage) == 1
    assert "".join(store._tasks["task-1"].output_text) == "answer after Wiki"


@pytest.mark.asyncio
async def test_new_cancellation_waits_for_owned_prefetch_cleanup():
    cleanup_started = asyncio.Event()
    release_cleanup = asyncio.Event()
    manager = Manager()

    async def complete(**kwargs):
        manager.started.set()
        try:
            await asyncio.Event().wait()
        finally:
            cleanup_started.set()
            await release_cleanup.wait()
            manager.closed.set()

    manager.complete = complete

    async def owner():
        async with wiki.prefetch_wiki_keywords(manager, "ECS", lambda *args: None):
            await manager.started.wait()

    task = asyncio.create_task(owner())
    await cleanup_started.wait()
    task.cancel()
    await asyncio.sleep(0)
    assert not task.done()
    release_cleanup.set()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert manager.closed.is_set()


@pytest.mark.parametrize(
    "entry,expected",
    [
        ({"config": {"wiki_search_on_enter": True}}, True),
        ({"config": {"wiki_search_on_enter": False}}, False),
        ({"enabled_when": "flag", "config": {"wiki_search_on_enter": True}}, False),
        ({}, False),
    ],
)
def test_only_unconditional_wiki_entry_is_prefetched(tmp_path, monkeypatch, entry, expected):
    import json

    from iac_code.a2a.pipeline_executor import _pipeline_starts_with_wiki

    (tmp_path / "pipeline.yaml").write_text(json.dumps({"steps": [entry]}), encoding="utf-8")
    monkeypatch.setattr("iac_code.pipeline.discover_pipelines", lambda: {"fixture": tmp_path})
    assert _pipeline_starts_with_wiki("fixture") is expected
    assert _pipeline_starts_with_wiki("unknown") is False


@pytest.mark.asyncio
async def test_adopted_wiki_duration_includes_prefetch_time(tmp_path, monkeypatch):
    import time

    manager = Manager()
    manager.release.set()

    async def search(*args, **kwargs):
        return []

    monkeypatch.setattr(wiki, "search", search)
    async with wiki.prefetch_wiki_keywords(manager, "ECS", lambda *args: None):
        wiki._KEYWORD_PREFETCH.get().started = time.monotonic() - 2
        work = start_work(manager, tmp_path, [])
        await work.task
    assert work.snapshot["elapsed_ms"] >= 2000
