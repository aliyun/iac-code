"""Search, source attribution and request-only formatting without cloud/LLM access."""

import copy
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from iac_code.agent.message import Message
from iac_code.providers.request_policy import ProviderRequestPolicy
from iac_code.services.token_counter import TokenCounter
from iac_code.services.wiki_auto_search import (
    METADATA_KEY,
    WikiAutoSearch,
    extraction_input,
    input_text,
    local_ui_auto_search,
    parse_keywords,
    reference,
    search,
)
from iac_code.tools.base import ToolContext
from iac_code.types.permissions import ToolPermissionContext
from iac_code.types.stream_events import Usage


@pytest.mark.parametrize(
    "text, expected",
    [('{"keywords":[]}', []), ('{"keywords":["B70"]}', ["B70"]), ('```json\n{"keywords":["B70","B70"]}\n```', ["B70"])],
)
def test_keywords_do_not_require_padding(text, expected):
    assert parse_keywords(text) == expected


@pytest.mark.parametrize(
    "text",
    [
        "{}",
        '{"keywords":"B70"}',
        '{"keywords":[""]}',
        '{"keywords":[1]}',
        json.dumps({"keywords": [str(i) for i in range(9)]}),
    ],
)
def test_invalid_keywords_are_not_silently_accepted(text):
    with pytest.raises(ValueError):
        parse_keywords(text)


@pytest.mark.asyncio
async def test_literal_search_boundaries_and_no_body_in_hits(tmp_path):
    root = tmp_path / "knowledge"
    root.mkdir()
    (root / "gpu.md").write_text('# GPU facts\n\n中文B70显卡\nSECRET BODY\n', encoding="utf-8")
    (root / "other.md").write_text('# Other\n\nB700, not that model\n', encoding="utf-8")
    (root / "api.md").write_text('# Parameters\n\nSystemDisk.Size\n', encoding="utf-8")
    context = ToolContext(cwd=str(tmp_path))
    hits = await search(root, ["B70"], context, None)
    assert [hit["path"] for hit in hits] == ["gpu.md"]
    assert hits[0]["title"] == "GPU facts" and hits[0]["hit_lines"] == [3]
    assert "SECRET BODY" not in str(hits)
    assert [hit["path"] for hit in await search(root, ["SystemDisk.Size"], context, None)] == ["api.md"]
    assert not await search(root, ["SystemDiskXSize"], context, None)


@pytest.mark.asyncio
async def test_search_respects_strict_read_roots(tmp_path):
    root = Path(__file__).resolve().parents[2] / "src/iac_code/knowledge"
    permission = ToolPermissionContext(
        cwd=str(tmp_path), strict_read_directories=[str(tmp_path)], read_path_violation_behavior="deny"
    )
    assert not await search(root, ["B70"], ToolContext(cwd=str(tmp_path)), permission)


class Extractor:
    def __init__(self, text):
        self.text = text
        self.calls = 0
        self.policies = []

    def get_model_name(self):
        return "fixture"

    def for_side_call(self, policy):
        self.policies.append(policy)
        return self

    async def complete(
        self, messages, system, tools=None, cache_policy="default", telemetry_messages=None, *, lease=None,
    ):
        self.calls += 1
        return SimpleNamespace(text=self.text, usage=Usage(input_tokens=3), usage_attribution=None)


@pytest.mark.asyncio
async def test_local_ui_wiki_scope_is_nested_task_local_and_restored_after_failure(monkeypatch):
    import asyncio

    from iac_code.services.wiki_auto_search import async_local_ui_auto_search, enabled

    monkeypatch.setenv("IAC_CODE_WIKI_AUTO_SEARCH", "false")
    entered, release = asyncio.Event(), asyncio.Event()

    @async_local_ui_auto_search()
    async def web_request():
        assert enabled() is True
        with local_ui_auto_search():
            assert enabled() is True
        assert enabled() is True
        entered.set()
        await release.wait()
        raise RuntimeError("test failure")

    task = asyncio.create_task(web_request())
    try:
        await asyncio.wait_for(entered.wait(), 5)
        assert enabled() is False  # A simultaneous remote request retains its own policy.
    finally:
        release.set()
        with pytest.raises(RuntimeError, match="test failure"):
            await task
    assert enabled() is False


@pytest.mark.asyncio
async def test_chinese_keyword_extraction_uses_current_provider_complete_signature(tmp_path):
    root = tmp_path / "knowledge"
    root.mkdir()
    (root / "ecs.md").write_text("经济型实例适用场景\n", encoding="utf-8")
    extractor = Extractor('{"keywords":["经济型"]}')
    service = WikiAutoSearch(extractor, root=root)
    work = service.start(
        Message(role="user", content="经济型实例适合什么场景？"),
        scope_id="current-provider-signature", serial=True, context=ToolContext(cwd=str(tmp_path)),
        permission=None, recent=[], persist=lambda _: None, record_usage=lambda *args: None,
    )
    await work.task
    assert extractor.calls == 1
    assert work.snapshot["status"] == "ready"
    assert work.snapshot["keywords"] == ["经济型"]
    assert [hit["path"] for hit in work.snapshot["hits"]] == ["ecs.md"]


@pytest.mark.asyncio
async def test_empty_and_restored_skipped_never_search_or_retry(tmp_path, monkeypatch):
    root = tmp_path / "knowledge"
    root.mkdir()
    extractor = Extractor('{"keywords":[]}')
    service = WikiAutoSearch(extractor, root=root)
    calls = []

    async def unexpected(*args, **kwargs):
        pytest.fail("empty keywords must not search")

    monkeypatch.setattr("iac_code.services.wiki_auto_search.search", unexpected)
    message = Message(role="user", content="Thanks")
    kwargs = dict(
        scope_id="session-normal",
        serial=True,
        context=ToolContext(cwd=str(tmp_path)),
        permission=None,
        recent=[],
        persist=calls.append,
        record_usage=lambda *args: None,
    )
    work = service.start(message, **kwargs)
    await work.task
    assert work.snapshot["status"] == "skipped" and extractor.calls == 1
    restored = service.start(message, **kwargs)
    assert restored.snapshot["status"] == "skipped" and restored.task is None
    assert extractor.calls == 1 and len(calls) == 1
    assert extractor.policies == [ProviderRequestPolicy(thinking_enabled=False, effort="low")]


@pytest.mark.asyncio
async def test_old_query_record_cannot_be_reused_for_new_input(tmp_path):
    root = tmp_path / "knowledge"
    root.mkdir()
    extractor = Extractor('{"keywords":[]}')
    service = WikiAutoSearch(extractor, root=root)
    message = Message(role="user", content="Thanks")
    kwargs = dict(
        scope_id="session-normal",
        serial=True,
        context=ToolContext(cwd=str(tmp_path)),
        permission=None,
        recent=[],
        persist=lambda value: None,
        record_usage=lambda *args: None,
    )
    first = service.start(message, **kwargs)
    await first.task
    new = Message(role="user", content="A different question", metadata=message.metadata.copy())
    second = service.start(new, **kwargs)
    await second.task
    assert first.snapshot["query_id"] != second.snapshot["query_id"] and extractor.calls == 2


@pytest.mark.asyncio
async def test_lost_auxiliary_usage_does_not_discard_valid_keywords(tmp_path):
    root = tmp_path / "knowledge"
    root.mkdir()
    (root / "gpu.md").write_text("B70\n", encoding="utf-8")
    service = WikiAutoSearch(Extractor('{"keywords":["B70"]}'), root=root)

    def unavailable(*args):
        raise OSError("auxiliary accounting unavailable")

    work = service.start(
        Message(role="user", content="Explain B70"),
        scope_id="usage",
        serial=True,
        context=ToolContext(cwd=str(tmp_path)),
        permission=None,
        recent=[],
        persist=lambda value: None,
        record_usage=unavailable,
    )
    await work.task
    assert work.snapshot["status"] == "ready" and work.snapshot["keywords"] == ["B70"]


def test_reference_contains_source_excerpts_and_obeys_budget(tmp_path):
    (tmp_path / "gpu.md").touch()
    snapshot = {
        "status": "ready",
        "keywords": ["B70"],
        "hits": [
            {
                "path": "gpu.md",
                "title": "GPU facts",
                "matched_terms": ["B70"],
                "hit_lines": [4],
                "lines": [{"number": 4, "score": 0.9, "text": "**B70** source facts"}],
            }
        ],
    }
    counter = TokenCounter()
    block = reference(snapshot, tmp_path, counter=counter, max_tokens=1200)
    assert str(tmp_path / "gpu.md") in block and "- 4: **B70** source facts" in block
    assert len(block.encode()) <= 6144 and counter.count_text(block) <= 1200
    assert not reference(snapshot, tmp_path, counter=counter, max_tokens=10)
    assert not reference({"status": "skipped"}, tmp_path, counter=counter, max_tokens=1200)
    assert METADATA_KEY not in block


def test_extractor_input_is_bounded_without_losing_identifiers_and_new_question():
    query = "背景说明" * 2000 + " DescribePrice SystemDisk.Size " + "历史资料" * 2000 + "请核实 B70"
    counter = TokenCounter("glm-5.3-prime")
    payload = extraction_input(query, ["过期背景" * 1000, "近期说明" * 1000], counter)
    assert counter.count_text(payload["input"]) <= 1000
    assert sum(counter.count_text(text) for text in payload["recent_user_inputs"]) <= 200
    assert all(name in payload["input"] for name in ("DescribePrice", "SystemDisk.Size", "B70"))
    assert payload["input"].endswith("请核实 B70")


@pytest.mark.asyncio
@pytest.mark.parametrize("change", ["corpus", "extractor", "skipped-extractor", "installation-root"])
async def test_restored_navigation_is_frozen_and_resolves_current_installation(tmp_path, monkeypatch, change):
    import shutil

    from iac_code.services.wiki_auto_search import EXTRACTOR_VERSION

    root = tmp_path / "knowledge"
    root.mkdir()
    page = root / "gpu.md"
    page.write_text("# GPU facts\n\nB70\n", encoding="utf-8")
    extractor = Extractor('{"keywords":[]}' if change == "skipped-extractor" else '{"keywords":["B70"]}')
    service = WikiAutoSearch(extractor, root=root)
    message = Message(role="user", content="Explain B70")
    persisted = []
    kwargs = dict(
        scope_id="normal",
        serial=False,
        context=ToolContext(cwd=str(tmp_path)),
        permission=None,
        recent=[],
        persist=lambda value: persisted.append(copy.deepcopy(value)),
        record_usage=lambda *args: None,
    )
    work = service.start(message, **kwargs)
    await work.task
    query = work.snapshot["query_id"]
    if change == "corpus":
        page.write_text("# Updated GPU facts\n\nB70\n", encoding="utf-8")
    elif "extractor" in change:
        message.metadata[METADATA_KEY]["extractor_version"] = EXTRACTOR_VERSION - 1
    else:
        relocated = tmp_path / "new-installation" / "knowledge"
        relocated.mkdir(parents=True)
        shutil.copy2(page, relocated / page.name)
        service = WikiAutoSearch(extractor, root=relocated)
    historical = copy.deepcopy(message.metadata)
    persisted.clear()
    monkeypatch.setattr(
        "iac_code.services.wiki_auto_search.corpus_fingerprint",
        lambda *args: pytest.fail("restoration must not inspect the current corpus"),
    )
    restored = service.start(message, **kwargs, restoring=True)
    assert restored.task is None
    assert restored.snapshot["query_id"] == query and restored.snapshot["dispatch_mode"] == "async"
    assert extractor.calls == 1
    assert message.metadata == historical
    assert not persisted
    if change == "corpus":
        assert restored.snapshot["hits"][0]["title"] == "GPU facts"
    if change == "installation-root":
        block = reference(restored.snapshot, service.root, counter=TokenCounter(), max_tokens=1200)
        assert str(service.root / "gpu.md") in block
        assert str(page) not in block


@pytest.mark.asyncio
@pytest.mark.parametrize("cached", [False, True])
async def test_restoration_discards_unfinished_work_without_rewriting_history(tmp_path, monkeypatch, cached):
    import asyncio

    extractor = Extractor('{"keywords":[]}')
    service = WikiAutoSearch(extractor, root=tmp_path)
    message = Message(role="user", content="Explain B70", metadata={"messageId": "old-input"})
    kwargs = dict(
        scope_id="normal", serial=True, context=ToolContext(cwd=str(tmp_path)), permission=None, recent=[],
        persist=lambda value: pytest.fail("restoration must not persist Wiki metadata"),
        record_usage=lambda *args: None,
    )
    if cached:
        # Simulate a snapshot persisted before extraction completed, without starting a live task.
        blocker = asyncio.Event()
        original_complete = extractor.complete

        async def blocked_complete(**kw):
            await blocker.wait()
            return await original_complete(**kw)

        monkeypatch.setattr(extractor, "complete", blocked_complete)
        old = service.start(message, **kwargs)
        old.task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await old.task
    historical = copy.deepcopy(message.metadata)
    monkeypatch.setattr(
        "iac_code.services.wiki_auto_search.corpus_fingerprint",
        lambda *args: pytest.fail("restoration must not search"),
    )
    restored = service.start(message, **kwargs, restoring=True)
    assert restored.task is None and restored.snapshot["status"] == "canceled"
    assert not reference(restored.snapshot, tmp_path, counter=TokenCounter(), max_tokens=1200)
    assert message.metadata == historical and extractor.calls == 0


def test_cached_sources_still_obey_current_permissions_and_reject_corruption(tmp_path):
    root = tmp_path / "knowledge"
    root.mkdir()
    (root / "gpu.md").touch()
    snapshot = {
        "status": "ready",
        "keywords": ["B70"],
        "hits": [
            {"path": "gpu.md", "title": "GPU facts", "matched_terms": ["B70"],
             "lines": [{"number": 4, "score": 0.9, "text": "B70 facts"}]},
            {"path": "../outside.md", "title": "Outside", "matched_terms": ["B70"]},
        ],
    }
    denied = tmp_path / "allowed-only"
    denied.mkdir()
    permission = ToolPermissionContext(
        cwd=str(tmp_path), strict_read_directories=[str(denied)], read_path_violation_behavior="deny"
    )
    assert not reference(snapshot, root, counter=TokenCounter(), max_tokens=1200, permission=permission)
    block = reference(snapshot, root, counter=TokenCounter(), max_tokens=1200)
    assert "gpu.md" in block and "outside.md" not in block
    assert not reference({**snapshot, "keywords": [None]}, root, counter=TokenCounter(), max_tokens=1200)
    assert not reference({**snapshot, "hits": None}, root, counter=TokenCounter(), max_tokens=1200)
    damaged = {
        **snapshot,
        "hits": [
            {"path": "invalid\x00.md", "title": "Invalid", "matched_terms": ["B70"]},
            {"path": "gpu.md", "title": "GPU facts", "matched_terms": ["B70"], "hit_lines": "NOT NAVIGATION",
             "lines": [{"number": 4, "score": 0.9, "text": "B70 facts"}]},
        ],
    }
    block = reference(damaged, root, counter=TokenCounter(), max_tokens=1200)
    assert "gpu.md" in block and "NOT NAVIGATION" not in block and "invalid" not in block


def test_compaction_preserves_only_navigation_metadata_without_summary_prose():
    from iac_code.agent.message import is_compaction_summary_message
    from iac_code.services.context_manager import ContextManager

    manager = ContextManager(system_prompt="test", model="fixture")
    for index in range(30):
        manager.add_user_message("Earlier question " + str(index))
        manager.add_assistant_message("Earlier answer")
    original = manager.add_user_message("What is B70?")
    original.metadata[METADATA_KEY] = {"query_id": "original", "dispatch_mode": "async", "status": "pending"}
    manager.apply_compaction("Prior conversation summary")
    marker = next(m for m in manager.get_context_messages() if is_compaction_summary_message(m))
    assert marker.metadata[METADATA_KEY] == original.metadata[METADATA_KEY]
    assert "wiki_auto_search" not in str(marker.content) and "original" not in str(marker.content)
    original.metadata[METADATA_KEY] = {**original.metadata[METADATA_KEY], "status": "ready"}
    assert marker.metadata[METADATA_KEY]["status"] == "pending", "no late-state merge is required"


class LoopProvider(Extractor):
    def __init__(self):
        import asyncio

        super().__init__('{"keywords":[]}')
        self.started = asyncio.Event()
        self.release = asyncio.Event()
        self.main_calls = 0

    async def complete(self, **kwargs):
        self.started.set()
        await self.release.wait()
        return await super().complete(**kwargs)

    async def stream(self, messages, system, tools=None, max_tokens=8192):
        from iac_code.types.stream_events import MessageEndEvent, TextDeltaEvent

        self.main_calls += 1
        yield TextDeltaEvent(text="main answer")
        yield MessageEndEvent(stop_reason="end_turn", usage=Usage())


@pytest.mark.asyncio
@pytest.mark.parametrize("pipeline", [False, True])
@pytest.mark.parametrize("continuation", [False, True])
async def test_web_wiki_search_streams_progress_and_enriches_context_without_environment_flag(
    tmp_path, monkeypatch, pipeline, continuation
):
    from iac_code.agent.agent_loop import AgentLoop
    from iac_code.services.session_storage import SessionStorage
    from iac_code.tools.base import ToolRegistry
    from iac_code.types.stream_events import MessageEndEvent, TextDeltaEvent, WikiSearchEvent
    from iac_code.web.events import WebEventTranslator

    monkeypatch.setenv("IAC_CODE_CONFIG_DIR", str(tmp_path / "config"))
    monkeypatch.delenv("IAC_CODE_WIKI_AUTO_SEARCH", raising=False)
    root = tmp_path / "knowledge"
    root.mkdir()
    (root / "gpu.md").write_text("# GPU specifications\nB70 has 32 GB GDDR6 memory.\n", encoding="utf-8")
    requests = []

    class Provider(Extractor):
        async def stream(self, messages, **kwargs):
            # A continued turn searches in the background. Let it finish while the
            # model is streaming, so its completion event is also exercised.
            if loop._wiki_work.task is not None:
                await loop._wiki_work.task
            requests.append(messages)
            yield TextDeltaEvent(text="answer")
            yield MessageEndEvent(stop_reason="end_turn", usage=Usage())

    provider = Provider('{"keywords":["B70"]}')
    storage = SessionStorage()
    with local_ui_auto_search():
        loop = AgentLoop(
            provider_manager=provider,
            system_prompt="test",
            tool_registry=ToolRegistry(),
            cwd=str(tmp_path),
            session_id="web-wiki",
            session_storage=storage,
            resume_messages=[Message(role="user", content="Earlier question")] if continuation else None,
            pipeline_mode=pipeline,
            wiki_search_context={"enabled": True, "scope_id": "step1"} if pipeline else None,
        )
        loop._wiki_search.root = root
        events = [event async for event in loop.run_streaming("Explain B70")]
    wiki = [event for event in events if isinstance(event, WikiSearchEvent)]
    assert [event.phase for event in wiki] == ["extracting", "searching", "ready"]
    assert provider.calls == 1
    assert loop._wiki_work.snapshot["dispatch_mode"] == ("async" if continuation else "serial")
    if not continuation:
        assert "32 GB GDDR6" in str(requests[0])
    else:
        assert "32 GB GDDR6" in str(loop._wiki_messages())
    projected = WebEventTranslator("web-wiki").translate_stream_event(wiki[-1], turn_id="turn1")
    assert projected["payload"]["files"] == [
        {
            "path": "gpu.md",
            "title": "GPU specifications",
            "lines": [{"number": 2, "text": "**B70** has 32 GB GDDR6 memory."}],
        }
    ]
    assert projected["payload"]["elapsedMs"] >= 0
    persisted = storage.load(str(tmp_path), "web-wiki")
    owner = next(message for message in persisted if message.metadata.get(METADATA_KEY))
    assert owner.metadata[METADATA_KEY]["status"] == "ready"
    assert [input_text(message.content) for message in persisted if message.role == "assistant"] == ["answer"]


@pytest.mark.asyncio
async def test_replaying_old_clarification_does_not_extract_but_new_input_does(tmp_path, monkeypatch):
    from iac_code.agent.agent_loop import AgentLoop
    from iac_code.agent.message import ToolResultBlock, ToolUseBlock
    from iac_code.tools.base import ToolRegistry

    monkeypatch.setenv("IAC_CODE_CONFIG_DIR", str(tmp_path / "config"))
    monkeypatch.setenv("IAC_CODE_WIKI_AUTO_SEARCH", "true")
    manager = LoopProvider()
    manager.release.set()
    loop = AgentLoop(
        provider_manager=manager, system_prompt="test", tool_registry=ToolRegistry(),
        session_id="old-clarification", cwd=str(tmp_path), pipeline_mode=True,
        wiki_search_context={"enabled": True, "scope_id": "step1"},
        resume_messages=[
            Message(role="user", content="Explain this model"),
            Message(role="assistant", content=[ToolUseBlock(id="question", name="ask_user_question")]),
            Message(role="user", content=[ToolResultBlock(tool_use_id="question", content='{"free_text":"B70"}')]),
        ],
    )
    historical = [copy.deepcopy(message.metadata) for message in loop.context_manager.get_messages()]
    _events = [event async for event in loop.continue_streaming()]
    assert manager.calls == 0
    assert [message.metadata for message in loop.context_manager.get_messages()[:len(historical)]] == historical
    _events = [event async for event in loop.run_streaming("Explain ECS billing")]
    await loop._wiki_work.task
    assert manager.calls == 1 and manager.main_calls == 2
    assert loop._wiki_work.snapshot["dispatch_mode"] == "async"


@pytest.mark.asyncio
@pytest.mark.parametrize("replay_seed", [False, True])
@pytest.mark.parametrize("answer, expected", [
    ({"free_text": "B70是一款intel显卡", "selected_label": ""}, "B70是一款intel显卡"),
    ({"free_text": "", "selected_label": "Intel B70 显卡"}, "Intel B70 显卡"),
    ({"free_text": "32GB 显存", "selected_label": "Intel B70 显卡"}, "Intel B70 显卡\n32GB 显存"),
])
async def test_resumed_clarification_extracts_the_latest_answer_without_duplicating_input(
    tmp_path, monkeypatch, answer, expected, replay_seed,
):
    import asyncio

    from iac_code.agent.agent_loop import AgentLoop
    from iac_code.agent.message import ToolResultBlock, ToolUseBlock
    from iac_code.tools.base import ToolRegistry

    monkeypatch.setenv("IAC_CODE_CONFIG_DIR", str(tmp_path / "config"))
    monkeypatch.setenv("IAC_CODE_WIKI_AUTO_SEARCH", "true")

    class RecordingProvider(LoopProvider):
        async def complete(self, **kwargs):
            self.extraction = json.loads(kwargs["messages"][0].content)
            return await super().complete(**kwargs)

    original = "我要购买按量付费的tdx，可信计算服务器"
    result = ToolResultBlock(tool_use_id="question", content=json.dumps(answer, ensure_ascii=False))
    manager = RecordingProvider()
    manager.text = '{"keywords":["B70"]}'
    loop = AgentLoop(
        provider_manager=manager, system_prompt="test", tool_registry=ToolRegistry(),
        session_id="clarification", cwd=str(tmp_path), pipeline_mode=True,
        wiki_search_context={
            "enabled": True, "scope_id": "step1", "seed": original, "resuming_question": True,
        },
        resume_messages=[
            Message(role="user", content=original),
            Message(role="assistant", content=[ToolUseBlock(id="question", name="ask_user_question")]),
            Message(role="user", content=[result]),
        ],
    )

    async def turn():
        return [event async for event in loop.continue_streaming()]

    async def first_turn():
        stream = loop.run_streaming(original) if replay_seed else loop.continue_streaming()
        return [event async for event in stream]

    task = asyncio.create_task(first_turn())
    try:
        await asyncio.wait_for(manager.started.wait(), 5)
        assert manager.extraction["input"] == expected
        assert loop._wiki_work.snapshot["dispatch_mode"] == "async"
        manager.release.set()
        await asyncio.wait_for(task, 5)
        await loop._wiki_work.task
        assert loop._wiki_work.snapshot["status"] == "ready"
        assert "intel-arc-pro-b70.md" in str([m.content for m in loop._wiki_messages()])
        assert result.content == json.dumps(answer, ensure_ascii=False)
        assert sum(m.content == original for m in loop.context_manager.get_messages()) == 1 + replay_seed
        assert not any(m.content == expected for m in loop.context_manager.get_messages())
        await turn()
        assert manager.calls == 1, "continuing the same answer must reuse its Wiki query"
        _events = [event async for event in loop.run_streaming("Explain ECS billing")]
        await loop._wiki_work.task
        assert manager.extraction["input"] == "Explain ECS billing"
        assert manager.calls == 2, "a later user input must not reuse the earlier clarification"
    finally:
        manager.release.set()
        await asyncio.gather(task, return_exceptions=True)
        if loop._wiki_work is not None and loop._wiki_work.task is not None:
            await loop._wiki_work.task


@pytest.mark.asyncio
async def test_live_question_result_starts_one_async_query_on_the_existing_tool_message(tmp_path, monkeypatch):
    from iac_code.agent.agent_loop import AgentLoop
    from iac_code.agent.message import ToolResultBlock
    from iac_code.pipeline.engine.ask_user_question_tool import AskUserQuestionTool
    from iac_code.tools.base import ToolRegistry
    from iac_code.types.stream_events import (
        AskUserQuestionEvent,
        MessageEndEvent,
        ToolUseEndEvent,
        ToolUseStartEvent,
    )

    monkeypatch.setenv("IAC_CODE_CONFIG_DIR", str(tmp_path / "config"))
    monkeypatch.setenv("IAC_CODE_WIKI_AUTO_SEARCH", "true")

    class QuestionProvider(LoopProvider):
        def __init__(self):
            super().__init__()
            self.inputs = []
            self.release.set()

        async def complete(self, **kwargs):
            self.inputs.append(json.loads(kwargs["messages"][0].content)["input"])
            return await super().complete(**kwargs)

        async def stream(self, messages, system, tools=None, max_tokens=8192):
            if self.main_calls == 0:
                self.main_calls += 1
                yield ToolUseStartEvent(tool_use_id="question", name="ask_user_question")
                yield ToolUseEndEvent(tool_use_id="question", name="ask_user_question", input={
                    "question": "Which hardware?", "options": [{"id": "gpu", "label": "Intel B70 显卡"}],
                })
                yield MessageEndEvent(stop_reason="tool_use", usage=Usage())
            else:
                async for event in super().stream(messages, system, tools=tools, max_tokens=max_tokens):
                    yield event

    manager = QuestionProvider()
    registry = ToolRegistry()
    registry.register(AskUserQuestionTool())
    loop = AgentLoop(
        provider_manager=manager, system_prompt="test", tool_registry=registry,
        session_id="live-question", cwd=str(tmp_path), pipeline_mode=True,
        wiki_search_context={"enabled": True, "scope_id": "step1"},
    )
    original = "我要购买按量付费的tdx，可信计算服务器"
    reply = "B70是一款intel显卡"
    async for event in loop.run_streaming(original):
        if isinstance(event, AskUserQuestionEvent):
            event.response_future.set_result({"free_text": reply})
    await loop._wiki_work.task
    assert manager.inputs == [original, reply]
    assert loop._wiki_work.snapshot["dispatch_mode"] == "async"
    owners = [m for m in loop.context_manager.get_messages()
              if m.metadata.get(METADATA_KEY, {}).get("query_id") == loop._wiki_work.snapshot["query_id"]]
    assert len(owners) == 1
    assert isinstance(owners[0].content[0], ToolResultBlock)
    assert json.loads(owners[0].content[0].content)["free_text"] == reply
    assert not any(m.content == reply for m in loop.context_manager.get_messages())


@pytest.mark.parametrize("tool, result_id, is_error, payload", [
    ("aliyun_api", "question", False, '{"free_text":"B70"}'),
    ("ask_user_question", "different", False, '{"free_text":"B70"}'),
    ("ask_user_question", "question", True, '{"free_text":"B70"}'),
    ("ask_user_question", "question", False, 'not JSON'),
    ("ask_user_question", "question", False, '["B70"]'),
    ("ask_user_question", "question", False, '{"free_text":123,"selected_label":false}'),
])
def test_only_successful_matching_question_answers_become_wiki_input(tool, result_id, is_error, payload):
    from iac_code.agent.message import ToolResultBlock, ToolUseBlock
    from iac_code.services.wiki_auto_search import question_answer_text

    history = [Message(role="assistant", content=[ToolUseBlock(id="question", name=tool)])]
    reply = Message(role="user", content=[
        ToolResultBlock(tool_use_id=result_id, content=payload, is_error=is_error),
    ])
    assert question_answer_text(reply, [*history, reply]) == ""


@pytest.mark.asyncio
@pytest.mark.parametrize("prior_normal", [False, True])
@pytest.mark.parametrize("surface", ["a2a", "web"])
async def test_handoff_preserves_normal_first_input_scope(tmp_path, monkeypatch, prior_normal, surface):
    import asyncio

    from iac_code.agent.agent_loop import AgentLoop
    from iac_code.services.session_storage import PIPELINE_USER_HISTORY_KEY
    from iac_code.tools.base import ToolRegistry

    monkeypatch.setenv("IAC_CODE_CONFIG_DIR", str(tmp_path / "config"))
    monkeypatch.setenv("IAC_CODE_WIKI_AUTO_SEARCH", "true")
    history = [
        Message(role="user", content="pipeline purchase", metadata={PIPELINE_USER_HISTORY_KEY: {"message_id": "p1"}}),
        Message(role="user", content="injected handoff summary", metadata={"type": "pipeline_handoff_context"}),
    ]
    if prior_normal:
        history.insert(0, Message(role="user", content="earlier normal question"))
    if surface == "web":
        from iac_code.web.session_manager import WebSessionManager

        web = WebSessionManager(cwd=tmp_path)
        session = web.create_session(mode="pipeline", session_id="handoff")
        web.persist_pipeline_user_prompt(session, "pipeline purchase")
        web.persist_pipeline_handoff_context(session, "injected handoff summary")
        if prior_normal:
            web.persist_pipeline_user_prompt(session, "earlier normal question", normal_chat=True)
        history = web.load_resume_messages(session.session_id)
    manager = LoopProvider()
    loop = AgentLoop(
        provider_manager=manager,
        system_prompt="test",
        tool_registry=ToolRegistry(),
        session_id="handoff",
        cwd=str(tmp_path),
        resume_messages=history,
    )

    async def turn():
        return [event async for event in loop.run_streaming("What is B70?")]

    task = asyncio.create_task(turn())
    try:
        await asyncio.wait_for(manager.started.wait(), 5)
        assert loop._wiki_work.snapshot["dispatch_mode"] == ("async" if prior_normal else "serial")
        if not prior_normal:
            assert manager.main_calls == 0
        manager.release.set()
        await asyncio.wait_for(task, 5)
        assert manager.main_calls == 1
    finally:
        manager.release.set()
        await asyncio.gather(task, return_exceptions=True)


@pytest.mark.asyncio
async def test_hot_session_replacement_cancels_all_pending_old_scope_queries(tmp_path, monkeypatch):
    import asyncio

    from iac_code.agent.agent_loop import AgentLoop
    from iac_code.tools.base import ToolRegistry

    monkeypatch.setenv("IAC_CODE_CONFIG_DIR", str(tmp_path / "config"))
    monkeypatch.setenv("IAC_CODE_WIKI_AUTO_SEARCH", "true")
    manager = LoopProvider()
    loop = AgentLoop(
        provider_manager=manager,
        system_prompt="test",
        tool_registry=ToolRegistry(),
        session_id="old",
        cwd=str(tmp_path),
        resume_messages=[Message(role="user", content="earlier normal question")],
    )
    works = []
    for prompt in ("What is B70?", "Explain DescribePrice"):
        _events = [event async for event in loop.run_streaming(prompt)]
        works.append(loop._wiki_work)
    await asyncio.wait_for(manager.started.wait(), 5)
    loop.replace_session("new", [])
    await asyncio.gather(*(work.task for work in works), return_exceptions=True)
    assert all(work.snapshot["status"] == "canceled" for work in works)
    assert loop._wiki_work is None and loop.session_id == "new"
    manager.release.set()
    _events = [event async for event in loop.run_streaming("A new B70 question")]
    assert loop._wiki_work.snapshot["dispatch_mode"] == "serial"
    assert ":new:normal" in loop._wiki_work.snapshot["scope_id"]


@pytest.mark.parametrize("same_storage", [False, True])
@pytest.mark.parametrize("pipeline", [False, True])
@pytest.mark.asyncio
async def test_wiki_cleanup_is_scoped_to_the_session_storage(tmp_path, monkeypatch, same_storage, pipeline):
    import asyncio

    from iac_code.agent.agent_loop import AgentLoop
    from iac_code.pipeline.engine.transcript_storage import PipelineTranscriptStorage
    from iac_code.services.session_storage import SessionStorage
    from iac_code.tools.base import ToolRegistry

    monkeypatch.setenv("IAC_CODE_CONFIG_DIR", str(tmp_path / "config"))
    monkeypatch.setenv("IAC_CODE_WIKI_AUTO_SEARCH", "true")
    managers = [LoopProvider(), LoopProvider()]
    loops = []
    works = []
    storage_class = PipelineTranscriptStorage if pipeline else SessionStorage
    try:
        for index, manager in enumerate(managers):
            loop = AgentLoop(
                provider_manager=manager, system_prompt="test", tool_registry=ToolRegistry(),
                session_id="same-session", cwd=str(tmp_path),
                session_storage=storage_class(tmp_path / ("a" if same_storage or index == 0 else "b")),
                resume_messages=[Message(role="user", content="Earlier question")],
                pipeline_mode=pipeline, wiki_search_context={"enabled": True, "scope_id": "shared-step"},
            )
            loop._start_wiki_work(loop.context_manager.add_user_message(f"Explain API {index}"))
            loops.append(loop)
            works.append(loop._wiki_work)
        await asyncio.wait_for(asyncio.gather(*(manager.started.wait() for manager in managers)), 5)
        assert works[0].snapshot["scope_id"] == works[1].snapshot["scope_id"]
        loops[0].reset()
        with pytest.raises(asyncio.CancelledError):
            await works[0].task
        assert works[0].snapshot["status"] == "canceled"
        if same_storage:
            with pytest.raises(asyncio.CancelledError):
                await works[1].task
            assert works[1].snapshot["status"] == "canceled"
        else:
            assert not works[1].task.done()
            managers[1].release.set()
            await works[1].task
            assert works[1].snapshot["status"] == "skipped"
    finally:
        for manager in managers:
            manager.release.set()
        await asyncio.gather(*(work.task for work in works), return_exceptions=True)
