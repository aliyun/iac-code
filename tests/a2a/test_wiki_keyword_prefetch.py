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


@pytest.mark.asyncio
async def test_worker_ready_prefetch_runs_during_tools_setup_and_is_adopted_once(tmp_path, monkeypatch):
    import threading

    manager = Manager()
    root_usage, step_usage = [], []
    tools_finished = threading.Event()
    monkeypatch.setattr(wiki, "search", _no_search)

    @wiki.keyword_preparation_scope
    async def execute():
        early = wiki.current_keyword_preparation()

        def prepare_tools():
            early.start_from_thread(manager, "ECS", lambda *args: root_usage.append(args))
            assert not tools_finished.is_set()

        await asyncio.to_thread(prepare_tools)
        await asyncio.wait_for(manager.started.wait(), 2)
        assert not tools_finished.is_set()
        tools_finished.set()
        work = start_work(manager, tmp_path, step_usage)
        manager.release.set()
        await work.task

    await execute()
    assert manager.calls == 1 and len(step_usage) == 1 and not root_usage
    assert wiki._KEYWORD_PREFETCH.get() is None


async def _no_search(*args, **kwargs):
    return []


@pytest.mark.asyncio
async def test_worker_ready_prefetch_is_drained_on_request_cancellation():
    manager = Manager()

    @wiki.keyword_preparation_scope
    async def execute():
        early = wiki.current_keyword_preparation()
        await asyncio.to_thread(early.start_from_thread, manager, "ECS", lambda *args: None)
        await manager.started.wait()
        await asyncio.Event().wait()

    request = asyncio.create_task(execute())
    await manager.started.wait()
    request.cancel()
    with pytest.raises(asyncio.CancelledError):
        await request
    assert manager.closed.is_set()


@pytest.mark.asyncio
async def test_late_worker_callback_after_owner_close_does_not_launch_inference():
    manager = Manager()
    holders = []

    @wiki.keyword_preparation_scope
    async def execute():
        holders.append(wiki.current_keyword_preparation())

    await execute()
    await asyncio.to_thread(holders[0].start_from_thread, manager, "ECS", lambda *args: None)
    await asyncio.sleep(0)
    assert manager.calls == 0


@pytest.mark.asyncio
async def test_completed_unused_worker_prefetch_records_usage_exactly_once():
    manager = Manager()
    usage = []

    @wiki.keyword_preparation_scope
    async def execute():
        early = wiki.current_keyword_preparation()
        await asyncio.to_thread(early.start_from_thread, manager, "ECS", lambda *args: usage.append(args))
        await manager.started.wait()
        manager.release.set()
        await manager.closed.wait()
        await early.aclose()
        await early.aclose()

    await execute()
    assert manager.calls == 1 and len(usage) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("initialization_fails", [False, True])
async def test_executor_starts_after_provider_config_before_tools_and_drains_failed_setup(
    tmp_path,
    monkeypatch,
    initialization_fails,
):
    import threading
    from types import SimpleNamespace

    from iac_code.a2a.executor import IacCodeA2AExecutor
    from iac_code.a2a.task_store import A2ATaskStore
    from iac_code.providers.manager import ProviderManager
    from iac_code.types.stream_events import TextDeltaEvent
    from tests.a2a.fakes import FakeEventQueue, FakeRequestContext
    from tests.a2a.test_pipeline_executor import FakePipeline, _fake_runtime

    monkeypatch.setenv("IAC_CODE_MODE", "pipeline")
    monkeypatch.setenv("IAC_CODE_PIPELINE_NAME", "selling_solution_first")
    monkeypatch.setenv("IAC_CODE_WIKI_AUTO_SEARCH", "true")
    inference_started, tools_started, inference_closed = threading.Event(), threading.Event(), threading.Event()
    root_usage, step_usage, configurations = [], [], []

    class Provider:
        _PROVIDER_KEY = "openai"

        async def complete(self, *args, **kwargs):
            from iac_code.providers.request_headers import get_provider_request_headers

            assert get_provider_request_headers() == {"X-Fixture-Request": "request-scoped"}
            inference_started.set()
            try:
                assert await asyncio.to_thread(tools_started.wait, 3)
                if initialization_fails:
                    await asyncio.Event().wait()
                return NonStreamingResponse(
                    message_id="fixture",
                    text='{"keywords":["ECS"]}',
                    tool_uses=[],
                    stop_reason="end_turn",
                    usage=Usage(input_tokens=7),
                )
            finally:
                inference_closed.set()

    monkeypatch.setattr("iac_code.providers.manager.create_provider", lambda *args, **kwargs: Provider())
    manager = ProviderManager(model="fixture", credentials={}, ignore_llm_source=True)
    reconfigure = manager.reconfigure

    def configured(*args, **kwargs):
        configurations.append(args[0])
        return reconfigure(*args, **kwargs)

    monkeypatch.setattr(manager, "reconfigure", configured)
    runtime = _fake_runtime()
    runtime.provider_manager = manager
    runtime.agent_loop = SimpleNamespace(_record_session_usage=lambda *args: root_usage.append(args))

    class Pipeline(FakePipeline):
        async def run(self, prompt):
            work = start_work(manager, tmp_path, step_usage, query="hello")
            await work.task
            yield TextDeltaEvent(text="answer")

    pipeline = Pipeline([], session_dir=tmp_path / "pipeline")

    def create_runtime(options):
        assert options.provider_ready is not None
        options.provider_ready(manager)
        tools_started.set()
        assert inference_started.wait(3), "inference did not begin during initialization"
        assert configurations == ["fixture"]
        if initialization_fails:
            raise RuntimeError("fixture initialization failure")
        return runtime

    monkeypatch.setattr("iac_code.a2a.pipeline_executor.create_agent_runtime", create_runtime)
    monkeypatch.setattr("iac_code.a2a.pipeline_executor.create_pipeline", lambda *args, **kwargs: pipeline)
    monkeypatch.setattr(wiki, "search", _no_search)
    store = A2ATaskStore()
    executor = IacCodeA2AExecutor(task_store=store, model="fixture")
    queue = FakeEventQueue()
    await executor.execute(
        FakeRequestContext(
            metadata={
                "iac_code": {
                    "cwd": str(tmp_path),
                    "llm_headers": {"X-Fixture-Request": "request-scoped"},
                }
            }
        ),
        queue,
    )
    assert inference_closed.is_set()
    assert configurations == ["fixture"]  # No second reconfigure while the side request is in flight.
    if initialization_fails:
        assert not step_usage and not root_usage
    else:
        assert len(step_usage) == 1 and not root_usage
        assert "".join(store._tasks["task-1"].output_text) == "answer"


@pytest.mark.asyncio
async def test_concurrent_early_prefetch_headers_are_isolated_and_do_not_leak_to_owner():
    from iac_code.providers.request_headers import get_provider_request_headers, use_provider_request_headers

    observed = []

    class HeaderManager(Manager):
        async def complete(self, **kwargs):
            observed.append(get_provider_request_headers())
            return NonStreamingResponse(
                message_id="fixture", text='{"keywords":[]}', tool_uses=[], stop_reason="end_turn", usage=Usage()
            )

    @wiki.keyword_preparation_scope
    async def execute(value):
        early = wiki.current_keyword_preparation()
        manager = HeaderManager()
        await asyncio.to_thread(
            early.start_from_thread, manager, "ECS", lambda *args: None, headers={"X-Fixture": value}
        )
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        if early.prefetch is not None:
            await early.prefetch.task
        assert get_provider_request_headers() == {"X-Owner": "unchanged"}

    with use_provider_request_headers({"X-Owner": "unchanged"}):
        await asyncio.gather(execute("one"), execute("two"))
        assert get_provider_request_headers() == {"X-Owner": "unchanged"}
    assert sorted(row["X-Fixture"] for row in observed) == ["one", "two"]
