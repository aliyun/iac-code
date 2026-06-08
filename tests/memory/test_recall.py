from __future__ import annotations

import asyncio
import json
import time
from types import SimpleNamespace

import pytest

from iac_code.memory.memory_manager import MemoryManager
from iac_code.types.stream_events import Usage


class FakeRecallProvider:
    def __init__(
        self,
        text: str,
        *,
        delay: float = 0.0,
        error: Exception | None = None,
        usage: Usage | None = None,
    ):
        self.text = text
        self.delay = delay
        self.error = error
        self.usage = usage
        self.calls: list[dict[str, object]] = []

    async def complete(
        self,
        messages,
        system,
        tools=None,
        max_tokens=8192,
        cache_policy="default",
    ):
        self.calls.append(
            {
                "messages": messages,
                "system": system,
                "tools": tools,
                "max_tokens": max_tokens,
                "cache_policy": cache_policy,
            }
        )
        if self.delay:
            await asyncio.sleep(self.delay)
        if self.error is not None:
            raise self.error
        return SimpleNamespace(text=self.text, usage=self.usage)


class FailingMemoryManager:
    def list_memories(self):
        raise OSError("memory dir unavailable")


@pytest.fixture
def memory_manager(tmp_path):
    manager = MemoryManager(memory_dir=str(tmp_path))
    manager.save(
        "project-deadline",
        content="Freeze on 2026-06-15",
        memory_type="project",
        description="Project delivery schedule",
    )
    manager.save(
        "feedback-testing",
        content="Prefer integration tests",
        memory_type="feedback",
        description="User testing preference",
    )
    return manager


@pytest.mark.asyncio
async def test_recall_default_timeout_is_ten_seconds(memory_manager, monkeypatch):
    from iac_code.memory import recall as recall_mod

    captured: dict[str, float] = {}

    async def capture_wait_for(awaitable, timeout):
        captured["timeout"] = timeout
        return await awaitable

    monkeypatch.setattr(recall_mod.asyncio, "wait_for", capture_wait_for)
    provider = FakeRecallProvider(json.dumps({"files": []}))
    service = recall_mod.MemoryRecallService(memory_manager=memory_manager, provider_manager=provider)

    await service.recall("deadline")

    assert captured["timeout"] == 10.0


@pytest.mark.asyncio
async def test_recall_selects_valid_topic_files_and_reads_content(memory_manager):
    from iac_code.memory.recall import MemoryRecallService

    provider = FakeRecallProvider(json.dumps({"files": ["project-deadline.md", "../escape.md", "missing.md"]}))
    service = MemoryRecallService(memory_manager=memory_manager, provider_manager=provider)

    result = await service.recall("what is the project deadline?")

    assert result.selected_files == ["project-deadline.md"]
    assert "Freeze on 2026-06-15" in result.content
    assert "Prefer integration tests" not in result.content
    stats = service.get_stats_snapshot()
    assert stats["total_side_queries"] == 1
    assert stats["successful_side_queries"] == 1
    assert stats["failed_side_queries"] == 0
    assert stats["total_selected_files"] == 1
    assert stats["last_status"] == "success"
    assert "User query:" in stats["last_prompt_preview"]
    assert "project-deadline.md" in stats["last_prompt_preview"]
    assert stats["last_response_preview"] == '{"files": ["project-deadline.md", "../escape.md", "missing.md"]}'


@pytest.mark.asyncio
async def test_recall_manifest_contains_frontmatter_not_topic_body(memory_manager):
    from iac_code.memory.recall import MemoryRecallService

    provider = FakeRecallProvider(json.dumps({"files": []}))
    service = MemoryRecallService(memory_manager=memory_manager, provider_manager=provider)

    await service.recall("deadline")

    call = provider.calls[0]
    manifest_prompt = call["messages"][0].content
    assert "Project delivery schedule" in manifest_prompt
    assert "Freeze on 2026-06-15" not in manifest_prompt


@pytest.mark.asyncio
async def test_recall_manifest_uses_metadata_without_loading_topic_bodies(memory_manager, monkeypatch):
    from iac_code.memory.recall import MemoryRecallService

    def fail_body_load(path):
        raise AssertionError(f"body should not be loaded while building recall manifest: {path}")

    monkeypatch.setattr(memory_manager, "_load_memory_file", fail_body_load)
    provider = FakeRecallProvider(json.dumps({"files": []}))
    service = MemoryRecallService(memory_manager=memory_manager, provider_manager=provider)

    result = await service.recall("deadline")

    assert result.status == "success"
    assert provider.calls


@pytest.mark.asyncio
async def test_recall_handles_malformed_json_as_failed(memory_manager):
    from iac_code.memory.recall import MemoryRecallService

    service = MemoryRecallService(
        memory_manager=memory_manager,
        provider_manager=FakeRecallProvider("not json", usage=Usage(input_tokens=3, output_tokens=1)),
    )

    result = await service.recall("deadline")

    assert result.content == ""
    assert result.selected_files == []
    assert result.usage == Usage(input_tokens=3, output_tokens=1)
    stats = service.get_stats_snapshot()
    assert stats["total_side_queries"] == 1
    assert stats["failed_side_queries"] == 1
    assert stats["last_status"] == "failed"


@pytest.mark.asyncio
async def test_recall_stats_record_side_call_token_usage(memory_manager):
    from iac_code.memory.recall import MemoryRecallService

    service = MemoryRecallService(
        memory_manager=memory_manager,
        provider_manager=FakeRecallProvider(
            json.dumps({"files": ["project-deadline.md"]}),
            usage=Usage(
                input_tokens=123,
                output_tokens=12,
                cache_read_input_tokens=4,
                cache_creation_input_tokens=7,
            ),
        ),
    )

    await service.recall("deadline")

    stats = service.get_stats_snapshot()
    assert stats["total_usage"] == {
        "input_tokens": 123,
        "output_tokens": 12,
        "cache_read_input_tokens": 4,
        "cache_creation_input_tokens": 7,
        "total_tokens": 135,
        "recorded_events": 1,
        "has_recorded_usage": True,
    }
    assert stats["last_usage"] == {
        "input_tokens": 123,
        "output_tokens": 12,
        "cache_read_input_tokens": 4,
        "cache_creation_input_tokens": 7,
        "total_tokens": 135,
        "recorded_events": 1,
        "has_recorded_usage": True,
    }


@pytest.mark.asyncio
async def test_recall_side_call_disables_explicit_cache(memory_manager):
    from iac_code.memory.recall import MemoryRecallService

    provider = FakeRecallProvider(json.dumps({"files": ["project-deadline.md"]}))
    service = MemoryRecallService(memory_manager=memory_manager, provider_manager=provider)

    await service.recall("deadline")

    assert provider.calls[0]["cache_policy"] == "no_explicit_cache"


@pytest.mark.asyncio
async def test_recall_prompt_uses_configured_max_files(memory_manager):
    from iac_code.memory.recall import MemoryRecallService

    provider = FakeRecallProvider(json.dumps({"files": []}))
    service = MemoryRecallService(memory_manager=memory_manager, provider_manager=provider, max_files=2)

    await service.recall("deadline")

    assert "Select at most 2 files." in provider.calls[0]["system"]


@pytest.mark.asyncio
async def test_recall_handles_provider_error(memory_manager):
    from iac_code.memory.recall import MemoryRecallService

    service = MemoryRecallService(
        memory_manager=memory_manager,
        provider_manager=FakeRecallProvider("", error=RuntimeError("provider down")),
    )

    result = await service.recall("deadline")

    assert result.content == ""
    assert service.get_stats_snapshot()["last_status"] == "failed"


@pytest.mark.asyncio
async def test_recall_handles_timeout(memory_manager):
    from iac_code.memory.recall import MemoryRecallService

    service = MemoryRecallService(
        memory_manager=memory_manager,
        provider_manager=FakeRecallProvider(json.dumps({"files": ["project-deadline.md"]}), delay=0.05),
        timeout_seconds=0.001,
    )

    result = await service.recall("deadline")

    assert result.content == ""
    stats = service.get_stats_snapshot()
    assert stats["failed_side_queries"] == 1
    assert stats["last_status"] == "timeout"


@pytest.mark.asyncio
async def test_start_prefetch_returns_without_waiting_for_provider(memory_manager):
    from iac_code.memory.recall import MemoryRecallService

    service = MemoryRecallService(
        memory_manager=memory_manager,
        provider_manager=FakeRecallProvider(json.dumps({"files": ["project-deadline.md"]}), delay=0.05),
        timeout_seconds=1.0,
    )

    started = time.monotonic()
    prefetch = service.start_prefetch("deadline")
    elapsed = time.monotonic() - started

    assert prefetch is not None
    assert elapsed < 0.02
    assert prefetch.done() is False

    result = await prefetch.wait()

    assert result.selected_files == ["project-deadline.md"]
    assert service.get_stats_snapshot()["last_status"] == "success"


@pytest.mark.asyncio
async def test_inflight_prefetch_is_reported_as_pending(memory_manager):
    from iac_code.memory.recall import MemoryRecallService

    service = MemoryRecallService(
        memory_manager=memory_manager,
        provider_manager=FakeRecallProvider(json.dumps({"files": ["project-deadline.md"]}), delay=0.05),
    )

    prefetch = service.start_prefetch("deadline")
    assert prefetch is not None

    for _ in range(20):
        stats = service.get_stats_snapshot()
        if stats["in_flight_side_queries"] == 1:
            break
        await asyncio.sleep(0)

    stats = service.get_stats_snapshot()
    assert stats["total_side_queries"] == 1
    assert stats["in_flight_side_queries"] == 1
    assert stats["successful_side_queries"] == 0
    assert stats["failed_side_queries"] == 0
    assert stats["cancelled_side_queries"] == 0
    assert stats["last_status"] == "pending"
    assert stats["last_side_query_status"] == "pending"

    result = await prefetch.wait()

    stats = service.get_stats_snapshot()
    assert result.selected_files == ["project-deadline.md"]
    assert stats["in_flight_side_queries"] == 0
    assert stats["successful_side_queries"] == 1
    assert stats["last_side_query_status"] == "success"


@pytest.mark.asyncio
async def test_prefetch_applies_configured_timeout(memory_manager):
    from iac_code.memory.recall import MemoryRecallService

    service = MemoryRecallService(
        memory_manager=memory_manager,
        provider_manager=FakeRecallProvider(json.dumps({"files": ["project-deadline.md"]}), delay=0.05),
        timeout_seconds=0.001,
    )

    prefetch = service.start_prefetch("deadline")
    assert prefetch is not None

    await asyncio.sleep(0.01)

    assert prefetch.done() is True
    result = prefetch.result()
    assert result.status == "timeout"
    stats = service.get_stats_snapshot()
    assert stats["total_side_queries"] == 1
    assert stats["in_flight_side_queries"] == 0
    assert stats["failed_side_queries"] == 1
    assert stats["cancelled_side_queries"] == 0
    assert stats["last_status"] == "timeout"
    assert stats["last_side_query_status"] == "timeout"


@pytest.mark.asyncio
async def test_cancelled_prefetch_records_cancelled(memory_manager):
    from iac_code.memory.recall import MemoryRecallService

    service = MemoryRecallService(
        memory_manager=memory_manager,
        provider_manager=FakeRecallProvider(json.dumps({"files": ["project-deadline.md"]}), delay=1.0),
        timeout_seconds=5.0,
    )

    prefetch = service.start_prefetch("deadline")
    assert prefetch is not None

    prefetch.cancel()
    await asyncio.sleep(0)

    stats = service.get_stats_snapshot()
    assert stats["cancelled_side_queries"] == 1
    assert stats["total_side_queries"] >= stats["cancelled_side_queries"]
    assert stats["last_status"] == "cancelled"
    assert stats["last_selected_files"] == []
    assert stats["last_side_query_status"] == "cancelled"
    assert stats["last_side_query_selected_files"] == []
    assert stats["last_side_query_duration_ms"] == 0


@pytest.mark.asyncio
async def test_inflight_prefetch_filters_files_read_after_manifest_build(memory_manager):
    from iac_code.memory.recall import MemoryRecallService

    provider = FakeRecallProvider(json.dumps({"files": ["project-deadline.md"]}), delay=0.01)
    service = MemoryRecallService(
        memory_manager=memory_manager,
        provider_manager=provider,
    )

    prefetch = service.start_prefetch("deadline")
    assert prefetch is not None

    for _ in range(20):
        if provider.calls:
            break
        await asyncio.sleep(0)
    assert provider.calls

    service.mark_files_read(["project-deadline.md"])

    result = await prefetch.wait()

    assert result.content == ""
    assert result.selected_files == []
    stats = service.get_stats_snapshot()
    assert stats["last_status"] == "success"
    assert stats["last_selected_files"] == []


@pytest.mark.asyncio
async def test_skipped_recall_preserves_last_side_query_stats(memory_manager):
    from iac_code.memory.recall import MemoryRecallService

    provider = FakeRecallProvider(
        json.dumps({"files": ["project-deadline.md"]}),
        usage=Usage(input_tokens=10, output_tokens=2, cache_read_input_tokens=3),
    )
    service = MemoryRecallService(memory_manager=memory_manager, provider_manager=provider)

    first = await service.recall("deadline")
    assert first.selected_files == ["project-deadline.md"]

    service.mark_files_surfaced(first.selected_files)
    service.mark_files_surfaced(["feedback-testing.md"])
    second = await service.recall("deadline")

    assert second.status == "skipped"
    stats = service.get_stats_snapshot()
    assert stats["total_side_queries"] == 1
    assert stats["last_status"] == "skipped"
    assert stats["last_selected_files"] == []
    assert stats["last_side_query_status"] == "success"
    assert stats["last_side_query_selected_files"] == ["project-deadline.md"]
    assert stats["last_usage"]["cache_read_input_tokens"] == 3


@pytest.mark.asyncio
async def test_recall_result_does_not_suppress_until_marked_surfaced(memory_manager):
    from iac_code.memory.recall import MemoryRecallService

    first_provider = FakeRecallProvider(json.dumps({"files": ["project-deadline.md"]}))
    service = MemoryRecallService(memory_manager=memory_manager, provider_manager=first_provider)

    first = await service.recall("deadline")
    assert first.selected_files == ["project-deadline.md"]

    second_provider = FakeRecallProvider(json.dumps({"files": ["project-deadline.md", "feedback-testing.md"]}))
    service._provider_manager = second_provider

    second = await service.recall("testing preference")

    assert second.selected_files == ["project-deadline.md", "feedback-testing.md"]
    second_manifest = second_provider.calls[0]["messages"][0].content
    assert "project-deadline.md" in second_manifest
    assert "feedback-testing.md" in second_manifest


@pytest.mark.asyncio
async def test_recall_does_not_repeat_previously_surfaced_files(memory_manager):
    from iac_code.memory.recall import MemoryRecallService

    first_provider = FakeRecallProvider(json.dumps({"files": ["project-deadline.md"]}))
    service = MemoryRecallService(memory_manager=memory_manager, provider_manager=first_provider)

    first = await service.recall("deadline")
    assert first.selected_files == ["project-deadline.md"]
    service.mark_files_surfaced(first.selected_files)

    second_provider = FakeRecallProvider(json.dumps({"files": ["project-deadline.md", "feedback-testing.md"]}))
    service._provider_manager = second_provider

    second = await service.recall("testing preference")

    assert second.selected_files == ["feedback-testing.md"]
    second_manifest = second_provider.calls[0]["messages"][0].content
    assert "feedback-testing.md" in second_manifest
    assert "project-deadline.md" not in second_manifest


@pytest.mark.asyncio
async def test_recall_excludes_memories_read_by_tool(memory_manager):
    from iac_code.memory.recall import MemoryRecallService

    provider = FakeRecallProvider(json.dumps({"files": ["project-deadline.md", "feedback-testing.md"]}))
    service = MemoryRecallService(memory_manager=memory_manager, provider_manager=provider)
    service.mark_files_read(["project-deadline.md"])

    result = await service.recall("deadline and testing")

    assert result.selected_files == ["feedback-testing.md"]
    manifest = provider.calls[0]["messages"][0].content
    assert "feedback-testing.md" in manifest
    assert "project-deadline.md" not in manifest


@pytest.mark.asyncio
async def test_recall_skips_when_no_topic_files(tmp_path):
    from iac_code.memory.recall import MemoryRecallService

    provider = FakeRecallProvider(json.dumps({"files": ["missing.md"]}))
    service = MemoryRecallService(memory_manager=MemoryManager(memory_dir=str(tmp_path)), provider_manager=provider)

    result = await service.recall("anything")

    assert result.content == ""
    assert provider.calls == []
    assert service.get_stats_snapshot()["last_status"] == "skipped"


@pytest.mark.asyncio
async def test_recall_skips_when_auto_memory_is_disabled(memory_manager):
    from iac_code.memory.recall import MemoryRecallService

    provider = FakeRecallProvider(json.dumps({"files": ["project-deadline.md"]}))
    service = MemoryRecallService(memory_manager=memory_manager, provider_manager=provider, is_enabled=lambda: False)

    result = await service.recall("deadline")

    assert result.content == ""
    assert result.status == "disabled"
    assert provider.calls == []
    stats = service.get_stats_snapshot()
    assert stats["total_side_queries"] == 0
    assert stats["last_status"] == "disabled"


@pytest.mark.asyncio
async def test_recall_manifest_failure_degrades_to_failed_without_provider_call():
    from iac_code.memory.recall import MemoryRecallService

    provider = FakeRecallProvider(json.dumps({"files": ["project-deadline.md"]}))
    service = MemoryRecallService(memory_manager=FailingMemoryManager(), provider_manager=provider)

    result = await service.recall("deadline")

    assert result.content == ""
    assert result.selected_files == []
    assert provider.calls == []
    stats = service.get_stats_snapshot()
    assert stats["total_side_queries"] == 1
    assert stats["failed_side_queries"] == 1
    assert stats["last_status"] == "failed"


def test_recall_stats_can_be_reset(memory_manager):
    from iac_code.memory.recall import MemoryRecallService

    service = MemoryRecallService(
        memory_manager=memory_manager,
        provider_manager=FakeRecallProvider(json.dumps({"files": ["project-deadline.md"]})),
    )
    service._stats.total_side_queries = 1
    service._stats.successful_side_queries = 1
    service._stats.total_selected_files = 1
    service._stats.last_status = "success"
    service._stats.last_selected_files = ["project-deadline.md"]

    service.reset_stats()

    assert service.get_stats_snapshot() == {
        "total_side_queries": 0,
        "in_flight_side_queries": 0,
        "successful_side_queries": 0,
        "failed_side_queries": 0,
        "cancelled_side_queries": 0,
        "total_selected_files": 0,
        "last_duration_ms": 0,
        "last_status": "skipped",
        "last_selected_files": [],
        "last_side_query_duration_ms": 0,
        "last_side_query_status": "skipped",
        "last_side_query_selected_files": [],
        "last_prompt_preview": "",
        "last_response_preview": "",
        "last_prompt_chars": 0,
        "last_response_chars": 0,
        "total_usage": {
            "input_tokens": 0,
            "output_tokens": 0,
            "cache_read_input_tokens": 0,
            "cache_creation_input_tokens": 0,
            "total_tokens": 0,
            "recorded_events": 0,
            "has_recorded_usage": False,
        },
        "last_usage": {
            "input_tokens": 0,
            "output_tokens": 0,
            "cache_read_input_tokens": 0,
            "cache_creation_input_tokens": 0,
            "total_tokens": 0,
            "recorded_events": 0,
            "has_recorded_usage": False,
        },
    }


def test_replace_surfaced_files_overwrites_process_local_state(memory_manager):
    from iac_code.memory.recall import MemoryRecallService

    service = MemoryRecallService(
        memory_manager=memory_manager,
        provider_manager=FakeRecallProvider(json.dumps({"files": []})),
    )

    service.mark_files_surfaced(["old.md"])
    service.replace_surfaced_files(["project-deadline.md"])

    assert service.get_suppressed_files() == {"project-deadline.md"}
