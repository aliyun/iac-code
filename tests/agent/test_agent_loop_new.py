import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, Mock, patch

import pytest

from iac_code.agent.agent_loop import AgentLoop
from iac_code.tools.base import ToolResult
from iac_code.tools.tool_executor import ToolExecutor
from iac_code.types.stream_events import (
    CompactionEvent,
    MessageEndEvent,
    MessageStartEvent,
    QueuedInputSubmittedEvent,
    TextDeltaEvent,
    ToolResultEvent,
    ToolUseEndEvent,
    ToolUseStartEvent,
    Usage,
)


@pytest.fixture
def mock_provider():
    m = MagicMock()
    m.get_model_name.return_value = "test-model"
    return m


@pytest.fixture
def mock_registry():
    r = MagicMock()
    r.list_tools.return_value = []
    r.get.return_value = None
    return r


def test_init_syncs_recalled_memory_from_resume_messages(mock_provider, mock_registry):
    from iac_code.agent.message import create_recalled_memory_message

    class FakeRecallService:
        def __init__(self):
            self.surfaced: set[str] = set()

        def mark_files_surfaced(self, filenames):
            self.surfaced.update(filenames)

        def get_stats_snapshot(self):
            return {"last_status": "skipped"}

    recall_service = FakeRecallService()
    AgentLoop(
        provider_manager=mock_provider,
        system_prompt="test",
        tool_registry=mock_registry,
        resume_messages=[create_recalled_memory_message("# Recalled Memory\nUse YAML", ["ros-yaml.md"])],
        memory_recall_service=recall_service,
    )

    assert recall_service.surfaced == {"ros-yaml.md"}


def test_replace_session_syncs_recalled_memory_from_loaded_messages(mock_provider, mock_registry):
    from iac_code.agent.message import create_recalled_memory_message

    class FakeRecallService:
        def __init__(self):
            self.reset_called = False
            self.surfaced: set[str] = set()

        def reset_stats(self):
            self.reset_called = True

        def mark_files_surfaced(self, filenames):
            self.surfaced.update(filenames)

        def get_stats_snapshot(self):
            return {"last_status": "skipped"}

    recall_service = FakeRecallService()
    loop = AgentLoop(
        provider_manager=mock_provider,
        system_prompt="test",
        tool_registry=mock_registry,
        session_id="old-session",
        memory_recall_service=recall_service,
    )

    loop.replace_session(
        "new-session",
        resume_messages=[create_recalled_memory_message("# Recalled Memory\nUse YAML", ["ros-yaml.md"])],
    )

    assert recall_service.reset_called is True
    assert recall_service.surfaced == {"ros-yaml.md"}


class TestAgentLoopInit:
    def test_init(self, mock_provider, mock_registry):
        loop = AgentLoop(provider_manager=mock_provider, system_prompt="test", tool_registry=mock_registry)
        assert loop._provider_manager is mock_provider
        assert isinstance(loop._tool_executor, ToolExecutor)

    def test_max_turns(self, mock_provider, mock_registry):
        loop = AgentLoop(
            provider_manager=mock_provider,
            system_prompt="test",
            tool_registry=mock_registry,
            max_turns=30,
        )
        assert loop._max_turns == 30

    def test_get_tool_definitions(self, mock_provider):
        tool = SimpleNamespace(name="read_file", description="Read file", input_schema={"type": "object"})
        registry = MagicMock()
        registry.list_tools.return_value = [tool]

        loop = AgentLoop(provider_manager=mock_provider, system_prompt="test", tool_registry=registry)
        defs = loop._get_tool_definitions()

        assert len(defs) == 1
        assert defs[0].name == "read_file"
        assert defs[0].description == "Read file"

    def test_init_syncs_tool_definitions_to_context_usage(self, mock_provider):
        tool = SimpleNamespace(name="read_file", description="Read file", input_schema={"type": "object"})
        registry = MagicMock()
        registry.list_tools.return_value = [tool]

        loop = AgentLoop(provider_manager=mock_provider, system_prompt="test", tool_registry=registry)

        assert loop.context_manager.get_usage()["tool_definition_tokens"] > 0

    def test_set_auto_trigger_skills_refreshes_candidates(self, mock_provider, mock_registry):
        old_command = SimpleNamespace(name="old-skill")
        new_command = SimpleNamespace(name="new-skill")
        loop = AgentLoop(
            provider_manager=mock_provider,
            system_prompt="test",
            tool_registry=mock_registry,
            auto_trigger_skills=[old_command],
        )
        loop._auto_loaded_skills.add("old-skill")

        loop.set_auto_trigger_skills([new_command])

        assert loop._auto_trigger_skills == [new_command]
        assert loop._auto_loaded_skills == {"old-skill"}

    def test_get_provider_messages_converts_strings_and_blocks(self, mock_provider, mock_registry):
        loop = AgentLoop(provider_manager=mock_provider, system_prompt="test", tool_registry=mock_registry)
        loop.context_manager = MagicMock()
        loop.context_manager.get_api_messages.return_value = [
            {"role": "user", "content": "hello"},
            {
                "role": "assistant",
                "content": [
                    {"type": "text", "text": "hi"},
                    {"type": "tool_use", "id": "toolu_1", "name": "read_file", "input": {"path": "a.txt"}},
                    "ignored",
                ],
            },
        ]

        messages = loop._get_provider_messages()

        assert len(messages) == 2
        assert messages[0].role == "user"
        assert messages[0].content == "hello"
        assert len(messages[1].content) == 2

    def test_apply_context_modifier(self, mock_provider, mock_registry):
        loop = AgentLoop(provider_manager=mock_provider, system_prompt="test", tool_registry=mock_registry)

        loop._apply_context_modifier(
            lambda ctx: {
                "allowed_tool_rules": ["read:*"],
                "model_override": "o3",
                "effort_override": "high",
            }
        )

        assert loop._allowed_tool_rules == ["read:*"]
        assert loop._model_override == "o3"
        assert loop._effort_override == "high"


@pytest.mark.asyncio
class TestAgentLoopStreaming:
    async def test_text_only(self, mock_provider, mock_registry):
        async def fake_stream(messages, system, tools=None, max_tokens=8192):
            yield MessageStartEvent(message_id="m1")
            yield TextDeltaEvent(text="Hello!")
            yield MessageEndEvent(stop_reason="end_turn", usage=Usage())

        mock_provider.stream = fake_stream

        loop = AgentLoop(provider_manager=mock_provider, system_prompt="test", tool_registry=mock_registry)
        events = [e async for e in loop.run_streaming("Hi")]
        types = [e.type for e in events]
        assert "text_delta" in types

    async def test_run_returns_text(self, mock_provider, mock_registry):
        async def fake_stream(messages, system, tools=None, max_tokens=8192):
            yield MessageStartEvent(message_id="m1")
            yield TextDeltaEvent(text="Hello!")
            yield MessageEndEvent(stop_reason="end_turn", usage=Usage())

        mock_provider.stream = fake_stream

        loop = AgentLoop(provider_manager=mock_provider, system_prompt="test", tool_registry=mock_registry)
        result = await loop.run("Hi")
        assert result == "Hello!"

    async def test_memory_recall_is_hidden_context_message(self, mock_provider, mock_registry):
        captured_messages = []

        async def fake_stream(messages, system, tools=None, max_tokens=8192):
            captured_messages.append(messages)
            yield MessageStartEvent(message_id="m1")
            yield TextDeltaEvent(text="ok")
            yield MessageEndEvent(stop_reason="end_turn", usage=Usage())

        class FakeRecallService:
            def __init__(self):
                self.queries: list[str] = []
                self.surfaced: set[str] = set()
                self.replaced: list[set[str]] = []

            def start_prefetch(self, user_input):
                from iac_code.memory.recall import MemoryRecallPrefetch, MemoryRecallResult

                self.queries.append(user_input)

                async def recall():
                    return MemoryRecallResult(
                        content="# Recalled Memory\nFreeze on 2026-06-15",
                        selected_files=["topic.md"],
                    )

                return MemoryRecallPrefetch(asyncio.create_task(recall()))

            def get_stats_snapshot(self):
                return {"last_status": "success"}

            def mark_files_surfaced(self, filenames):
                self.surfaced.update(filenames)

            def replace_surfaced_files(self, filenames):
                self.replaced.append(set(filenames))

        class FakeSessionStorage:
            def __init__(self):
                self.messages = []

            def append(self, cwd, session_id, message, git_branch=None):
                self.messages.append(message)

        mock_provider.stream = fake_stream
        recall_service = FakeRecallService()
        storage = FakeSessionStorage()
        loop = AgentLoop(
            provider_manager=mock_provider,
            system_prompt="base system",
            tool_registry=mock_registry,
            session_storage=storage,
            memory_recall_service=recall_service,
        )

        events = [event async for event in loop.run_streaming("what is the deadline?")]

        assert any(isinstance(event, MessageEndEvent) for event in events)
        assert recall_service.queries == ["what is the deadline?"]
        assert captured_messages[0][-1].role == "user"
        assert "Relevant persistent memories recalled for this conversation" in captured_messages[0][-1].content
        assert "Freeze on 2026-06-15" in captured_messages[0][-1].content
        assert recall_service.surfaced == {"topic.md"}
        assert any(message.metadata.get("type") == "recalled_memory" for message in storage.messages)
        assert any("Freeze on 2026-06-15" in str(message.content) for message in storage.messages)
        assert not hasattr(loop, "_current_recalled_memory_content") or loop._current_recalled_memory_content == ""

    async def test_recalled_memory_persists_and_suppresses_duplicate_in_future_turn(self, mock_provider, mock_registry):
        captured_messages = []

        async def fake_stream(messages, system, tools=None, max_tokens=8192):
            captured_messages.append(messages)
            yield MessageStartEvent(message_id=f"m{len(captured_messages)}")
            yield TextDeltaEvent(text="ok")
            yield MessageEndEvent(stop_reason="end_turn", usage=Usage())

        class FakeRecallService:
            def __init__(self):
                self.queries: list[str] = []
                self.surfaced: set[str] = set()

            def start_prefetch(self, user_input):
                from iac_code.memory.recall import MemoryRecallPrefetch, MemoryRecallResult

                if "ros-yaml.md" in self.surfaced:
                    return None
                self.queries.append(user_input)

                async def recall():
                    return MemoryRecallResult(
                        content="# Recalled Memory\nUse YAML for ROS templates",
                        selected_files=["ros-yaml.md"],
                    )

                return MemoryRecallPrefetch(asyncio.create_task(recall()))

            def get_stats_snapshot(self):
                return {"last_status": "success"}

            def mark_files_surfaced(self, filenames):
                self.surfaced.update(filenames)

        mock_provider.stream = fake_stream
        recall_service = FakeRecallService()
        loop = AgentLoop(
            provider_manager=mock_provider,
            system_prompt="base system",
            tool_registry=mock_registry,
            memory_recall_service=recall_service,
        )

        await loop.run("what format should I use?")
        await loop.run("what format should I use?")

        assert recall_service.queries == ["what format should I use?"]
        assert "Use YAML for ROS templates" in str(captured_messages[0])
        assert "Use YAML for ROS templates" in str(captured_messages[1])

    async def test_manual_compaction_resyncs_recalled_memory_suppression(self, mock_provider, mock_registry):
        class FakeRecallService:
            def __init__(self):
                self.surfaced: set[str] = set()

            def mark_files_surfaced(self, filenames):
                self.surfaced.update(filenames)

            def get_stats_snapshot(self):
                return {"last_status": "skipped"}

        async def fake_complete(messages, system):
            return SimpleNamespace(text="summary", usage=Usage(input_tokens=1, output_tokens=1))

        recall_service = FakeRecallService()
        mock_provider.complete = fake_complete
        loop = AgentLoop(
            provider_manager=mock_provider,
            system_prompt="test",
            tool_registry=mock_registry,
            memory_recall_service=recall_service,
        )
        loop.context_manager = MagicMock()
        loop.context_manager.get_messages.return_value = [SimpleNamespace(role="user")]
        loop.context_manager.build_compaction_prompt.return_value = "compact me"
        loop.context_manager.apply_compaction.return_value = (1200, 400)
        loop.context_manager.get_surfaced_memory_files.return_value = {"recent.md"}

        result = await loop.compact()

        assert result.status == "success"
        assert recall_service.surfaced == {"recent.md"}

    async def test_memory_recall_usage_is_recorded_in_session_usage(self, mock_provider, mock_registry, tmp_path):
        async def fake_stream(messages, system, tools=None, max_tokens=8192):
            yield MessageStartEvent(message_id="m1")
            yield TextDeltaEvent(text="ok")
            yield MessageEndEvent(stop_reason="end_turn", usage=Usage(input_tokens=10, output_tokens=5))

        class FakeRecallService:
            def start_prefetch(self, user_input):
                from iac_code.memory.recall import MemoryRecallPrefetch, MemoryRecallResult

                async def recall():
                    return MemoryRecallResult(
                        content="",
                        selected_files=[],
                        usage=Usage(input_tokens=4, output_tokens=1, cache_read_input_tokens=2),
                    )

                return MemoryRecallPrefetch(asyncio.create_task(recall()))

            def get_stats_snapshot(self):
                return {"last_status": "success"}

        from iac_code.services.session_usage import SessionUsageStore

        mock_provider.stream = fake_stream
        store = SessionUsageStore(projects_dir=tmp_path)
        loop = AgentLoop(
            provider_manager=mock_provider,
            system_prompt="test",
            tool_registry=mock_registry,
            session_id="recall-usage-session",
            cwd="/tmp/status-project",
            session_usage_store=store,
            memory_recall_service=FakeRecallService(),
        )

        await loop.run("Hi")

        totals = loop.get_session_usage()
        assert totals.input_tokens == 14
        assert totals.output_tokens == 6
        assert totals.cache_read_input_tokens == 2
        assert totals.recorded_events == 2
        assert store.load("/tmp/status-project", "recall-usage-session").total_tokens == 20

    async def test_slow_memory_prefetch_does_not_block_provider_call_and_is_cancelled_after_turn(
        self, mock_provider, mock_registry
    ):
        recall_can_finish = asyncio.Event()

        class FakeRecallService:
            def __init__(self):
                self.cancelled = False
                self.surfaced: list[str] = []

            def start_prefetch(self, user_input):
                from iac_code.memory.recall import MemoryRecallPrefetch, MemoryRecallResult

                async def recall():
                    await recall_can_finish.wait()
                    return MemoryRecallResult(content="# Recalled Memory\nlate", selected_files=["late.md"])

                return MemoryRecallPrefetch(
                    asyncio.create_task(recall()),
                    on_cancel=lambda: setattr(self, "cancelled", True),
                )

            def get_stats_snapshot(self):
                return {"last_status": "cancelled"}

            def mark_files_surfaced(self, filenames):
                self.surfaced.extend(filenames)

        captured_messages = []

        async def fake_stream(messages, system, tools=None, max_tokens=8192):
            captured_messages.append(messages)
            yield MessageStartEvent(message_id="m1")
            yield TextDeltaEvent(text="ok")
            yield MessageEndEvent(stop_reason="end_turn", usage=Usage())

        mock_provider.stream = fake_stream
        recall_service = FakeRecallService()
        loop = AgentLoop(
            provider_manager=mock_provider,
            system_prompt="base system",
            tool_registry=mock_registry,
            memory_recall_service=recall_service,
        )

        events = [event async for event in loop.run_streaming("fast answer")]

        assert any(isinstance(event, MessageEndEvent) for event in events)
        assert all("# Recalled Memory" not in str(message.content) for message in captured_messages[0])
        assert recall_service.cancelled is True

        recall_can_finish.set()
        for _ in range(5):
            await asyncio.sleep(0)

        assert not any(
            "# Recalled Memory\nlate" in str(message.content) for message in loop.context_manager.get_messages()
        )
        assert recall_service.surfaced == []

    async def test_finished_turn_cancels_only_its_own_memory_prefetch(self, mock_provider, mock_registry):
        slow_stream_started = asyncio.Event()
        slow_stream_can_finish = asyncio.Event()

        class FakeRecallService:
            def __init__(self):
                self.cancelled: list[str] = []

            def start_prefetch(self, user_input):
                from iac_code.memory.recall import MemoryRecallPrefetch, MemoryRecallResult

                async def recall():
                    await asyncio.Event().wait()
                    return MemoryRecallResult(content="# Recalled Memory\nunused", selected_files=["unused.md"])

                return MemoryRecallPrefetch(
                    asyncio.create_task(recall()),
                    on_cancel=lambda: self.cancelled.append(user_input),
                )

            def get_stats_snapshot(self):
                return {"last_status": "cancelled"}

        call_count = 0

        async def fake_stream(messages, system, tools=None, max_tokens=8192):
            nonlocal call_count
            call_count += 1
            yield MessageStartEvent(message_id=f"m{call_count}")
            if call_count == 1:
                slow_stream_started.set()
                await slow_stream_can_finish.wait()
            yield TextDeltaEvent(text="ok")
            yield MessageEndEvent(stop_reason="end_turn", usage=Usage())

        mock_provider.stream = fake_stream
        recall_service = FakeRecallService()
        loop = AgentLoop(
            provider_manager=mock_provider,
            system_prompt="base system",
            tool_registry=mock_registry,
            memory_recall_service=recall_service,
        )

        slow_task = asyncio.create_task(loop.run("slow turn"))
        await slow_stream_started.wait()
        await loop.run("fast turn")

        assert recall_service.cancelled == ["fast turn"]

        slow_stream_can_finish.set()
        await slow_task

        assert recall_service.cancelled == ["fast turn", "slow turn"]

    async def test_discarded_completed_memory_prefetch_records_usage_without_injection(
        self, mock_provider, mock_registry, tmp_path
    ):
        from iac_code.memory.recall import MemoryRecallResult
        from iac_code.services.session_usage import SessionUsageStore

        class FakeMemoryPrefetch:
            def __init__(self):
                self.finished = False
                self.cancelled = False

            def done(self):
                return self.finished

            def result(self):
                return MemoryRecallResult(
                    content="# Recalled Memory\nlate",
                    selected_files=["late.md"],
                    usage=Usage(input_tokens=4, output_tokens=1, cache_read_input_tokens=2),
                )

            def cancel(self):
                self.cancelled = True

        class FakeRecallService:
            def __init__(self):
                self.prefetch = FakeMemoryPrefetch()
                self.surfaced: list[str] = []

            def start_prefetch(self, user_input):
                return self.prefetch

            def get_stats_snapshot(self):
                return {"last_status": "success"}

            def mark_files_surfaced(self, filenames):
                self.surfaced.extend(filenames)

        recall_service = FakeRecallService()

        async def fake_stream(messages, system, tools=None, max_tokens=8192):
            yield MessageStartEvent(message_id="m1")
            recall_service.prefetch.finished = True
            yield TextDeltaEvent(text="ok")
            yield MessageEndEvent(stop_reason="end_turn", usage=Usage(input_tokens=10, output_tokens=5))

        mock_provider.stream = fake_stream
        store = SessionUsageStore(projects_dir=tmp_path)
        loop = AgentLoop(
            provider_manager=mock_provider,
            system_prompt="base system",
            tool_registry=mock_registry,
            session_id="discarded-recall-usage",
            cwd="/tmp/status-project",
            session_usage_store=store,
            memory_recall_service=recall_service,
        )

        await loop.run("fast answer")

        totals = loop.get_session_usage()
        assert totals.input_tokens == 14
        assert totals.output_tokens == 6
        assert totals.cache_read_input_tokens == 2
        assert totals.recorded_events == 2
        assert recall_service.prefetch.cancelled is False
        assert recall_service.surfaced == []
        assert not any(
            "# Recalled Memory\nlate" in str(message.content) for message in loop.context_manager.get_messages()
        )

    async def test_ready_memory_prefetch_injects_at_next_provider_round(self, mock_provider, mock_registry):
        recall_can_finish = asyncio.Event()
        captured_messages = []

        class FakeRecallService:
            def __init__(self):
                self.surfaced: set[str] = set()

            def start_prefetch(self, user_input):
                from iac_code.memory.recall import MemoryRecallPrefetch, MemoryRecallResult

                async def recall():
                    await recall_can_finish.wait()
                    return MemoryRecallResult(
                        content="# Recalled Memory\nUse YAML for ROS templates",
                        selected_files=["ros-yaml.md"],
                    )

                return MemoryRecallPrefetch(asyncio.create_task(recall()))

            def get_stats_snapshot(self):
                return {"last_status": "success"}

            def mark_files_surfaced(self, filenames):
                self.surfaced.update(filenames)

        call_count = 0

        async def fake_stream(messages, system, tools=None, max_tokens=8192):
            nonlocal call_count
            call_count += 1
            captured_messages.append(messages)
            if call_count == 1:
                yield MessageStartEvent(message_id="m1")
                yield ToolUseStartEvent(tool_use_id="toolu_1", name="read_file")
                yield ToolUseEndEvent(tool_use_id="toolu_1", name="read_file", input={"path": "a.txt"})
                yield MessageEndEvent(stop_reason="tool_use", usage=Usage())
                return

            yield MessageStartEvent(message_id="m2")
            yield TextDeltaEvent(text="ok")
            yield MessageEndEvent(stop_reason="end_turn", usage=Usage())

        async def execute_batch(requests, context):
            recall_can_finish.set()
            return [ToolResult(content="file content", is_error=False)]

        mock_provider.stream = fake_stream
        mock_registry.get.return_value = None
        recall_service = FakeRecallService()
        loop = AgentLoop(
            provider_manager=mock_provider,
            system_prompt="base system",
            tool_registry=mock_registry,
            memory_recall_service=recall_service,
        )
        loop._tool_executor.execute_batch = execute_batch

        await loop.run("what format should I use?")

        assert "# Recalled Memory" not in str(captured_messages[0])
        assert "Use YAML for ROS templates" in str(captured_messages[1])
        assert recall_service.surfaced == {"ros-yaml.md"}

    async def test_late_memory_recall_is_not_persisted_for_matching_next_turn(self, mock_provider, mock_registry):
        captured_messages = []

        async def fake_stream(messages, system, tools=None, max_tokens=8192):
            captured_messages.append(messages)
            yield MessageStartEvent(message_id="m1")
            await asyncio.sleep(0.02)
            yield TextDeltaEvent(text="ok")
            yield MessageEndEvent(stop_reason="end_turn", usage=Usage())

        class FakeRecallService:
            def __init__(self):
                self.queries: list[str] = []
                self.surfaced: set[str] = set()

            def start_prefetch(self, user_input):
                from iac_code.memory.recall import MemoryRecallPrefetch, MemoryRecallResult

                if "ros-yaml.md" in self.surfaced:
                    return None
                self.queries.append(user_input)

                async def recall():
                    await asyncio.sleep(0.01)
                    return MemoryRecallResult(
                        content="# Recalled Memory\nUse YAML for ROS templates",
                        selected_files=["ros-yaml.md"],
                    )

                return MemoryRecallPrefetch(asyncio.create_task(recall()))

            def get_stats_snapshot(self):
                return {"last_status": "success"}

            def mark_files_surfaced(self, filenames):
                self.surfaced.update(filenames)

        mock_provider.stream = fake_stream
        recall_service = FakeRecallService()
        loop = AgentLoop(
            provider_manager=mock_provider,
            system_prompt="base system",
            tool_registry=mock_registry,
            memory_recall_service=recall_service,
        )

        await loop.run("what format should I use?")
        await loop.run("what format should I use?")
        await loop.run("what format should I use?")

        assert recall_service.queries == [
            "what format should I use?",
            "what format should I use?",
            "what format should I use?",
        ]
        assert "# Recalled Memory" not in str(captured_messages[0])
        assert "Use YAML for ROS templates" not in str(captured_messages[1])
        assert "Use YAML for ROS templates" not in str(captured_messages[2])
        assert recall_service.surfaced == set()

    async def test_late_memory_recall_is_not_persisted_after_different_next_turn(self, mock_provider, mock_registry):
        captured_messages = []

        async def fake_stream(messages, system, tools=None, max_tokens=8192):
            captured_messages.append(messages)
            yield MessageStartEvent(message_id="m1")
            await asyncio.sleep(0.02)
            yield TextDeltaEvent(text="ok")
            yield MessageEndEvent(stop_reason="end_turn", usage=Usage())

        class FakeRecallService:
            def __init__(self):
                self.queries: list[str] = []
                self.surfaced: set[str] = set()

            def start_prefetch(self, user_input):
                from iac_code.memory.recall import MemoryRecallPrefetch, MemoryRecallResult

                if user_input == "different question" or "ros-yaml.md" in self.surfaced:
                    return None
                self.queries.append(user_input)

                async def recall():
                    await asyncio.sleep(0.01)
                    return MemoryRecallResult(
                        content="# Recalled Memory\nUse YAML for ROS templates",
                        selected_files=["ros-yaml.md"],
                    )

                return MemoryRecallPrefetch(asyncio.create_task(recall()))

            def get_stats_snapshot(self):
                return {"last_status": "success"}

            def mark_files_surfaced(self, filenames):
                self.surfaced.update(filenames)

        mock_provider.stream = fake_stream
        recall_service = FakeRecallService()
        loop = AgentLoop(
            provider_manager=mock_provider,
            system_prompt="base system",
            tool_registry=mock_registry,
            memory_recall_service=recall_service,
        )

        await loop.run("what format should I use?")
        await loop.run("different question")
        await loop.run("what format should I use?")

        assert recall_service.queries == ["what format should I use?", "what format should I use?"]
        assert "Use YAML for ROS templates" not in str(captured_messages[1])
        assert "Use YAML for ROS templates" not in str(captured_messages[2])
        assert recall_service.surfaced == set()

    async def test_previous_turn_recall_completing_during_next_turn_is_not_persisted(
        self, mock_provider, mock_registry
    ):
        first_recall_can_finish = asyncio.Event()
        second_stream_started = asyncio.Event()
        second_stream_can_finish = asyncio.Event()
        captured_messages = []

        async def fake_stream(messages, system, tools=None, max_tokens=8192):
            captured_messages.append(messages)
            yield MessageStartEvent(message_id="m1")
            if len(captured_messages) == 2:
                second_stream_started.set()
                await second_stream_can_finish.wait()
            yield TextDeltaEvent(text="ok")
            yield MessageEndEvent(stop_reason="end_turn", usage=Usage())

        class FakeRecallService:
            def __init__(self):
                self.queries: list[str] = []
                self.surfaced: set[str] = set()

            def start_prefetch(self, user_input):
                from iac_code.memory.recall import MemoryRecallPrefetch, MemoryRecallResult

                self.queries.append(user_input)

                async def recall():
                    if user_input == "first":
                        await first_recall_can_finish.wait()
                        return MemoryRecallResult(
                            content="# Recalled Memory\nUse YAML for ROS templates",
                            selected_files=["ros-yaml.md"],
                        )
                    return MemoryRecallResult(content="", selected_files=[])

                return MemoryRecallPrefetch(asyncio.create_task(recall()))

            def get_stats_snapshot(self):
                return {"last_status": "success"}

            def mark_files_surfaced(self, filenames):
                self.surfaced.update(filenames)

            def replace_surfaced_files(self, filenames):
                self.surfaced = set(filenames)

        mock_provider.stream = fake_stream
        recall_service = FakeRecallService()
        loop = AgentLoop(
            provider_manager=mock_provider,
            system_prompt="base system",
            tool_registry=mock_registry,
            memory_recall_service=recall_service,
        )

        await loop.run("first")
        second_run = asyncio.create_task(loop.run("second"))
        await second_stream_started.wait()
        first_recall_can_finish.set()
        for _ in range(5):
            await asyncio.sleep(0)
            if recall_service.surfaced:
                break
        assert recall_service.surfaced == set()

        second_stream_can_finish.set()
        await second_run
        await loop.run("third")

        assert recall_service.surfaced == set()
        assert "Use YAML for ROS templates" not in str(captured_messages[1])
        assert "Use YAML for ROS templates" not in str(captured_messages[2])

    async def test_cancelled_turn_discards_pending_memory_prefetch(self, mock_provider, mock_registry):
        recall_can_finish = asyncio.Event()
        stream_started = asyncio.Event()
        stream_can_finish = asyncio.Event()

        async def fake_stream(messages, system, tools=None, max_tokens=8192):
            yield MessageStartEvent(message_id="m1")
            stream_started.set()
            await stream_can_finish.wait()
            yield TextDeltaEvent(text="ok")
            yield MessageEndEvent(stop_reason="end_turn", usage=Usage())

        class FakeRecallService:
            def __init__(self):
                self.cancelled = False
                self.surfaced: list[str] = []

            def start_prefetch(self, user_input):
                from iac_code.memory.recall import MemoryRecallPrefetch, MemoryRecallResult

                async def recall():
                    await recall_can_finish.wait()
                    return MemoryRecallResult(
                        content="# Recalled Memory\nDiscard cancelled turn context",
                        selected_files=["cancelled.md"],
                    )

                return MemoryRecallPrefetch(
                    asyncio.create_task(recall()),
                    on_cancel=lambda: setattr(self, "cancelled", True),
                )

            def get_stats_snapshot(self):
                return {"last_status": "success"}

            def mark_files_surfaced(self, filenames):
                self.surfaced.extend(filenames)

        mock_provider.stream = fake_stream
        recall_service = FakeRecallService()
        loop = AgentLoop(
            provider_manager=mock_provider,
            system_prompt="base system",
            tool_registry=mock_registry,
            memory_recall_service=recall_service,
        )

        async def consume():
            async for _event in loop.run_streaming("cancel this turn"):
                pass

        task = asyncio.create_task(consume())
        await stream_started.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

        recall_can_finish.set()
        stream_can_finish.set()
        for _ in range(5):
            await asyncio.sleep(0)

        assert recall_service.cancelled is True
        assert recall_service.surfaced == []
        assert not any(
            getattr(message, "metadata", {}).get("type") == "recalled_memory"
            for message in loop.context_manager.get_messages()
        )

    async def test_cancelled_tool_execution_cancels_execute_batch(self, mock_provider, mock_registry):
        execute_started = asyncio.Event()
        execute_cancelled = asyncio.Event()
        execute_released = asyncio.Event()

        async def fake_stream(messages, system, tools=None, max_tokens=8192):
            yield MessageStartEvent(message_id="m1")
            yield ToolUseStartEvent(tool_use_id="toolu_1", name="read_file")
            yield ToolUseEndEvent(tool_use_id="toolu_1", name="read_file", input={"path": "a.txt"})
            yield MessageEndEvent(stop_reason="tool_use", usage=Usage())

        async def execute_batch(requests, context):
            execute_started.set()
            try:
                await execute_released.wait()
            except asyncio.CancelledError:
                execute_cancelled.set()
                raise
            return [ToolResult(content="late result", is_error=False)]

        mock_provider.stream = fake_stream
        mock_registry.list_tools.return_value = [SimpleNamespace(name="read_file", description="Read", input_schema={})]
        mock_registry.get.return_value = None
        loop = AgentLoop(provider_manager=mock_provider, system_prompt="test", tool_registry=mock_registry)
        loop._tool_executor.execute_batch = execute_batch

        async def consume():
            async for _event in loop.run_streaming("Hi"):
                pass

        task = asyncio.create_task(consume())
        await asyncio.wait_for(execute_started.wait(), timeout=1)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

        try:
            await asyncio.wait_for(execute_cancelled.wait(), timeout=0.1)
        finally:
            execute_released.set()

    async def test_system_prompt_refresher_updates_each_provider_round_and_tools(self, mock_provider, mock_registry):
        captured_systems: list[str] = []

        class PromptAwareTool:
            name = "read_file"
            description = "Read"
            input_schema = {}

            def __init__(self):
                self.prompts: list[str] = []

            def set_system_prompt(self, system_prompt: str) -> None:
                self.prompts.append(system_prompt)

        tool = PromptAwareTool()

        def refresh_prompt():
            return "fresh-1" if not captured_systems else "fresh-2"

        async def fake_stream(messages, system, tools=None, max_tokens=8192):
            captured_systems.append(system)
            if len(captured_systems) == 1:
                yield MessageStartEvent(message_id="m1")
                yield ToolUseStartEvent(tool_use_id="toolu_1", name="read_file")
                yield ToolUseEndEvent(tool_use_id="toolu_1", name="read_file", input={"path": "a.txt"})
                yield MessageEndEvent(stop_reason="tool_use", usage=Usage())
                return

            yield MessageStartEvent(message_id="m2")
            yield TextDeltaEvent(text="ok")
            yield MessageEndEvent(stop_reason="end_turn", usage=Usage())

        mock_provider.stream = fake_stream
        mock_registry.list_tools.return_value = [tool]
        loop = AgentLoop(
            provider_manager=mock_provider,
            system_prompt="stale",
            tool_registry=mock_registry,
            system_prompt_refresher=refresh_prompt,
        )
        loop._result_storage = MagicMock()
        loop._result_storage.process.return_value = SimpleNamespace(content="processed result")
        loop._tool_executor.execute_batch = AsyncMock(return_value=[ToolResult(content="raw result", is_error=False)])

        await loop.run("Hi")

        assert captured_systems == ["fresh-1", "fresh-2"]
        assert tool.prompts[-1] == "fresh-2"
        assert loop.context_manager.system_prompt == "fresh-2"

    async def test_records_non_zero_message_end_usage(self, mock_provider, mock_registry, tmp_path):
        async def fake_stream(messages, system, tools=None, max_tokens=8192):
            yield MessageStartEvent(message_id="m1")
            yield TextDeltaEvent(text="Hello!")
            yield MessageEndEvent(
                stop_reason="end_turn",
                usage=Usage(
                    input_tokens=10,
                    output_tokens=5,
                    cache_read_input_tokens=3,
                    cache_creation_input_tokens=2,
                ),
            )

        from iac_code.services.session_usage import SessionUsageStore

        mock_provider.stream = fake_stream
        store = SessionUsageStore(projects_dir=tmp_path)
        loop = AgentLoop(
            provider_manager=mock_provider,
            system_prompt="test",
            tool_registry=mock_registry,
            session_id="usage-session",
            cwd="/tmp/status-project",
            session_usage_store=store,
        )

        events = [e async for e in loop.run_streaming("Hi")]

        assert any(isinstance(e, MessageEndEvent) for e in events)
        totals = loop.get_session_usage()
        assert totals.input_tokens == 10
        assert totals.output_tokens == 5
        assert totals.cache_read_input_tokens == 3
        assert totals.cache_creation_input_tokens == 2
        assert totals.recorded_events == 1
        assert store.load("/tmp/status-project", "usage-session").total_tokens == 15

    async def test_records_usage_with_runtime_provider_key(self, mock_provider, mock_registry, tmp_path, monkeypatch):
        async def fake_stream(messages, system, tools=None, max_tokens=8192):
            yield MessageStartEvent(message_id="m1")
            yield TextDeltaEvent(text="Hello!")
            yield MessageEndEvent(stop_reason="end_turn", usage=Usage(input_tokens=10, output_tokens=5))

        from iac_code.services.session_usage import SessionUsageStore

        monkeypatch.setattr("iac_code.config.get_active_provider_key", lambda: "openai")
        mock_provider.stream = fake_stream
        mock_provider.get_provider_key.return_value = "dashscope_token_plan"
        mock_provider.get_model_name.return_value = "runtime-model"
        store = SessionUsageStore(projects_dir=tmp_path)
        loop = AgentLoop(
            provider_manager=mock_provider,
            system_prompt="test",
            tool_registry=mock_registry,
            session_id="runtime-session",
            cwd="/tmp/status-project",
            session_usage_store=store,
        )

        await loop.run("Hi")

        row = store.path_for("/tmp/status-project", "runtime-session").read_text(encoding="utf-8")
        assert '"provider": "dashscope_token_plan"' in row
        assert '"model": "runtime-model"' in row

    async def test_records_usage_from_multiple_model_calls_in_one_prompt(self, mock_provider, mock_registry, tmp_path):
        call_count = 0

        async def fake_stream(messages, system, tools=None, max_tokens=8192):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                yield MessageStartEvent(message_id="m1")
                yield ToolUseStartEvent(tool_use_id="toolu_1", name="read_file")
                yield ToolUseEndEvent(tool_use_id="toolu_1", name="read_file", input={"path": "a.txt"})
                yield MessageEndEvent(stop_reason="tool_use", usage=Usage(input_tokens=10, output_tokens=5))
                return

            yield MessageStartEvent(message_id="m2")
            yield TextDeltaEvent(text="After tool")
            yield MessageEndEvent(stop_reason="end_turn", usage=Usage(input_tokens=7, output_tokens=3))

        from iac_code.services.session_usage import SessionUsageStore

        mock_provider.stream = fake_stream
        mock_registry.list_tools.return_value = [SimpleNamespace(name="read_file", description="Read", input_schema={})]
        store = SessionUsageStore(projects_dir=tmp_path)
        loop = AgentLoop(
            provider_manager=mock_provider,
            system_prompt="test",
            tool_registry=mock_registry,
            session_id="multi-call-session",
            cwd="/tmp/status-project",
            session_usage_store=store,
        )
        loop._result_storage = MagicMock()
        loop._result_storage.process.return_value = SimpleNamespace(content="processed result")
        loop._tool_executor.execute_batch = AsyncMock(return_value=[ToolResult(content="raw result", is_error=False)])

        events = [e async for e in loop.run_streaming("Hi")]

        assert call_count == 2
        assert any(isinstance(e, ToolResultEvent) for e in events)
        totals = loop.get_session_usage()
        assert totals.input_tokens == 17
        assert totals.output_tokens == 8
        assert totals.recorded_events == 2
        assert store.load("/tmp/status-project", "multi-call-session").total_tokens == 25

    async def test_queued_input_is_submitted_after_tool_results_before_next_model_call(
        self, mock_provider, mock_registry
    ):
        call_messages = []
        call_count = 0

        async def fake_stream(messages, system, tools=None, max_tokens=8192):
            nonlocal call_count
            call_count += 1
            call_messages.append(messages)
            if call_count == 1:
                yield MessageStartEvent(message_id="m1")
                yield ToolUseStartEvent(tool_use_id="toolu_1", name="read_file")
                yield ToolUseEndEvent(tool_use_id="toolu_1", name="read_file", input={"path": "a.txt"})
                yield MessageEndEvent(stop_reason="tool_use", usage=Usage())
                return

            yield MessageStartEvent(message_id="m2")
            yield TextDeltaEvent(text="handled queued input")
            yield MessageEndEvent(stop_reason="end_turn", usage=Usage())

        drained = False

        def queued_input_provider():
            nonlocal drained
            if drained:
                return []
            drained = True
            return ["你好"]

        mock_provider.stream = fake_stream
        loop = AgentLoop(provider_manager=mock_provider, system_prompt="test", tool_registry=mock_registry)
        loop._result_storage = MagicMock()
        loop._result_storage.process.return_value = SimpleNamespace(content="file contents")
        loop._tool_executor.execute_batch = AsyncMock(return_value=[ToolResult(content="raw result", is_error=False)])

        events = [e async for e in loop.run_streaming("Hi", queued_input_provider=queued_input_provider)]

        assert call_count == 2
        assert any(isinstance(e, QueuedInputSubmittedEvent) and e.text == "你好" for e in events)
        second_call = call_messages[1]
        assert [message.role for message in second_call] == ["user", "assistant", "user", "user"]
        assert second_call[0].content == "Hi"
        assert second_call[2].content[0].type == "tool_result"
        assert second_call[3].content == "你好"

    async def test_queued_input_provider_is_not_drained_without_tool_call(self, mock_provider, mock_registry):
        queued_input_provider = Mock(return_value=["should wait"])

        async def fake_stream(messages, system, tools=None, max_tokens=8192):
            yield MessageStartEvent(message_id="m1")
            yield TextDeltaEvent(text="done")
            yield MessageEndEvent(stop_reason="end_turn", usage=Usage())

        mock_provider.stream = fake_stream
        loop = AgentLoop(provider_manager=mock_provider, system_prompt="test", tool_registry=mock_registry)

        events = [e async for e in loop.run_streaming("Hi", queued_input_provider=queued_input_provider)]

        assert not any(isinstance(e, QueuedInputSubmittedEvent) for e in events)
        queued_input_provider.assert_not_called()
        user_texts = [message.get_text() for message in loop.context_manager.get_messages() if message.role == "user"]
        assert user_texts == ["Hi"]

    async def test_react_step_span_records_round_output_when_debug_content_enabled(self, mock_provider, mock_registry):
        async def fake_stream(messages, system, tools=None, max_tokens=8192):
            yield MessageStartEvent(message_id="m1")
            yield TextDeltaEvent(text="final")
            yield TextDeltaEvent(text=" answer")
            yield MessageEndEvent(stop_reason="end_turn", usage=Usage())

        class CapturedSpan:
            def __init__(self, name, attrs):
                self.name = name
                self.attrs = dict(attrs or {})

            def set_attribute(self, key, value):
                self.attrs[key] = value

        class CapturedContext:
            def __init__(self, span):
                self.span = span

            def __enter__(self):
                return self.span

            def __exit__(self, exc_type, exc, tb):
                return False

        captured = []

        def capture_span(name, attrs=None):
            span = CapturedSpan(name, attrs)
            captured.append(span)
            return CapturedContext(span)

        mock_provider.stream = fake_stream
        loop = AgentLoop(provider_manager=mock_provider, system_prompt="test", tool_registry=mock_registry)

        with (
            patch("iac_code.services.telemetry.start_span", side_effect=capture_span),
            patch("iac_code.services.telemetry.get_session_id", return_value="sess"),
            patch("iac_code.services.telemetry.get_user_id", return_value="user"),
            patch("iac_code.services.telemetry.log_event"),
            patch("iac_code.services.telemetry.add_metric"),
            patch("iac_code.services.telemetry.config.should_capture_content_on_span", return_value=True),
        ):
            events = [event async for event in loop.run_streaming("Hi")]

        assert any(isinstance(event, MessageEndEvent) for event in events)
        react_span = next(span for span in captured if span.name == "react step")
        output = json.loads(react_span.attrs["gen_ai.output.messages"])
        assert output[0]["parts"][0]["content"] == "final answer"
        assert output[0]["finish_reason"] == "end_turn"

    async def test_does_not_record_zero_message_end_usage(self, mock_provider, mock_registry, tmp_path):
        async def fake_stream(messages, system, tools=None, max_tokens=8192):
            yield MessageStartEvent(message_id="m1")
            yield TextDeltaEvent(text="Hello!")
            yield MessageEndEvent(stop_reason="end_turn", usage=Usage())

        from iac_code.services.session_usage import SessionUsageStore

        mock_provider.stream = fake_stream
        store = SessionUsageStore(projects_dir=tmp_path)
        loop = AgentLoop(
            provider_manager=mock_provider,
            system_prompt="test",
            tool_registry=mock_registry,
            session_id="zero-session",
            cwd="/tmp/status-project",
            session_usage_store=store,
        )

        await loop.run("Hi")

        assert loop.get_session_usage().has_recorded_usage is False
        assert not store.path_for("/tmp/status-project", "zero-session").exists()

    async def test_abnormal_provider_stream_emits_stream_error_end(self, mock_provider, mock_registry):
        async def fake_stream(messages, system, tools=None, max_tokens=8192):
            yield MessageStartEvent(message_id="m1")
            yield TextDeltaEvent(text="partial")

        mock_provider.stream = fake_stream

        loop = AgentLoop(provider_manager=mock_provider, system_prompt="test", tool_registry=mock_registry)

        events = [e async for e in loop.run_streaming("Hi")]

        assert any(isinstance(e, TextDeltaEvent) and e.text == "partial" for e in events)
        assert isinstance(events[-1], MessageEndEvent)
        assert events[-1].stop_reason == "stream_error"
        assert events[-1].usage == Usage()

    async def test_replace_session_reloads_usage_totals(self, mock_provider, mock_registry, tmp_path):
        from iac_code.services.session_usage import SessionUsageStore

        store = SessionUsageStore(projects_dir=tmp_path)
        store.append("/tmp/status-project", "old-session", Usage(input_tokens=1, output_tokens=2))
        store.append("/tmp/status-project", "new-session", Usage(input_tokens=7, output_tokens=8))

        loop = AgentLoop(
            provider_manager=mock_provider,
            system_prompt="test",
            tool_registry=mock_registry,
            session_id="old-session",
            cwd="/tmp/status-project",
            session_usage_store=store,
        )

        assert loop.get_session_usage().total_tokens == 3

        loop.replace_session("new-session", resume_messages=None)

        assert loop.session_id == "new-session"
        assert loop.get_session_usage().input_tokens == 7
        assert loop.get_session_usage().output_tokens == 8
        assert loop.get_session_usage().total_tokens == 15

    async def test_refresh_session_usage_reloads_external_usage_totals(self, mock_provider, mock_registry, tmp_path):
        from iac_code.services.session_usage import SessionUsageStore

        store = SessionUsageStore(projects_dir=tmp_path)
        store.append("/tmp/status-project", "usage-session", Usage(input_tokens=1, output_tokens=2))

        loop = AgentLoop(
            provider_manager=mock_provider,
            system_prompt="test",
            tool_registry=mock_registry,
            session_id="usage-session",
            cwd="/tmp/status-project",
            session_usage_store=store,
        )

        assert loop.get_session_usage().total_tokens == 3

        store.append(
            "/tmp/status-project",
            "usage-session",
            Usage(input_tokens=7, output_tokens=8, cache_read_input_tokens=4),
        )

        assert loop.get_session_usage().total_tokens == 3
        loop.refresh_session_usage()

        totals = loop.get_session_usage()
        assert totals.input_tokens == 8
        assert totals.output_tokens == 10
        assert totals.cache_read_input_tokens == 4
        assert totals.total_tokens == 18

    async def test_replace_session_resets_memory_recall_stats(self, mock_provider, mock_registry):
        class FakeRecallService:
            def __init__(self):
                self.reset_called = False

            def reset_stats(self):
                self.reset_called = True

            def get_stats_snapshot(self):
                return {"last_status": "success", "total_side_queries": 1}

        recall_service = FakeRecallService()
        loop = AgentLoop(
            provider_manager=mock_provider,
            system_prompt="test",
            tool_registry=mock_registry,
            session_id="old-session",
            memory_recall_service=recall_service,
        )

        loop.replace_session("new-session", resume_messages=None)

        assert recall_service.reset_called is True

    async def test_replace_session_clears_last_provider_request_snapshot(self, mock_provider, mock_registry):
        async def fake_stream(messages, system, tools=None, max_tokens=8192):
            yield MessageStartEvent(message_id="m1")
            yield TextDeltaEvent(text="ok")
            yield MessageEndEvent(stop_reason="end_turn", usage=Usage())

        mock_provider.stream = fake_stream
        loop = AgentLoop(provider_manager=mock_provider, system_prompt="test", tool_registry=mock_registry)

        await loop.run("old prompt")
        assert loop.get_last_provider_request_snapshot()["provider_messages"]

        loop.replace_session("new-session", resume_messages=None)

        assert loop.get_last_provider_request_snapshot() == {}

    async def test_reset_clears_last_provider_request_snapshot(self, mock_provider, mock_registry):
        async def fake_stream(messages, system, tools=None, max_tokens=8192):
            yield MessageStartEvent(message_id="m1")
            yield TextDeltaEvent(text="ok")
            yield MessageEndEvent(stop_reason="end_turn", usage=Usage())

        mock_provider.stream = fake_stream
        loop = AgentLoop(provider_manager=mock_provider, system_prompt="test", tool_registry=mock_registry)

        await loop.run("old prompt")
        assert loop.get_last_provider_request_snapshot()["provider_messages"]

        loop.reset()

        assert loop.get_last_provider_request_snapshot() == {}

    async def test_read_memory_tool_marks_file_as_read_for_recall_dedupe(self, mock_provider, mock_registry):
        class FakeRecallService:
            def __init__(self):
                self.marked: list[str] = []

            def get_stats_snapshot(self):
                return {"last_status": "skipped"}

            def mark_files_read(self, files):
                self.marked.extend(files)

        call_count = 0

        async def fake_stream(messages, system, tools=None, max_tokens=8192):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                yield MessageStartEvent(message_id="m1")
                yield ToolUseStartEvent(tool_use_id="toolu_1", name="read_memory")
                yield ToolUseEndEvent(tool_use_id="toolu_1", name="read_memory", input={"name": "project-deadline"})
                yield MessageEndEvent(stop_reason="tool_use", usage=Usage())
                return

            yield MessageStartEvent(message_id="m2")
            yield TextDeltaEvent(text="ok")
            yield MessageEndEvent(stop_reason="end_turn", usage=Usage())

        from iac_code.memory.memory_tools import ReadMemoryTool

        recall_service = FakeRecallService()
        read_tool = ReadMemoryTool(MagicMock())
        mock_provider.stream = fake_stream
        mock_registry.list_tools.return_value = [read_tool]
        mock_registry.get.return_value = read_tool
        loop = AgentLoop(
            provider_manager=mock_provider,
            system_prompt="test",
            tool_registry=mock_registry,
            memory_recall_service=recall_service,
        )
        loop._result_storage = MagicMock()
        loop._result_storage.process.return_value = SimpleNamespace(content="processed result")
        loop._tool_executor.execute_batch = AsyncMock(
            return_value=[ToolResult(content="memory content", is_error=False)]
        )

        await loop.run("read remembered deadline")

        assert recall_service.marked == ["project-deadline.md"]

    async def test_read_memory_suppresses_completed_prefetch_injection(self, mock_provider, mock_registry):
        recall_can_finish = asyncio.Event()
        captured_messages = []

        class FakeRecallService:
            def __init__(self):
                self.read_files: set[str] = set()

            def start_prefetch(self, user_input):
                from iac_code.memory.recall import MemoryRecallPrefetch, MemoryRecallResult

                async def recall():
                    await recall_can_finish.wait()
                    return MemoryRecallResult(
                        content="# Recalled Memory\nFreeze",
                        selected_files=["project-deadline.md"],
                    )

                return MemoryRecallPrefetch(asyncio.create_task(recall()))

            def get_stats_snapshot(self):
                return {"last_status": "success"}

            def mark_files_read(self, files):
                self.read_files.update(files)

            def get_suppressed_files(self):
                return set(self.read_files)

        class FakeSessionStorage:
            def __init__(self):
                self.messages = []

            def append(self, cwd, session_id, message, git_branch=None):
                self.messages.append(message)

        call_count = 0

        async def fake_stream(messages, system, tools=None, max_tokens=8192):
            nonlocal call_count
            call_count += 1
            captured_messages.append(messages)
            if call_count == 1:
                yield MessageStartEvent(message_id="m1")
                yield ToolUseStartEvent(tool_use_id="toolu_1", name="read_memory")
                yield ToolUseEndEvent(tool_use_id="toolu_1", name="read_memory", input={"name": "project-deadline"})
                yield MessageEndEvent(stop_reason="tool_use", usage=Usage())
                recall_can_finish.set()
                return

            yield MessageStartEvent(message_id="m2")
            yield TextDeltaEvent(text="ok")
            yield MessageEndEvent(stop_reason="end_turn", usage=Usage())

        from iac_code.memory.memory_tools import ReadMemoryTool

        recall_service = FakeRecallService()
        storage = FakeSessionStorage()
        read_tool = ReadMemoryTool(MagicMock())
        mock_provider.stream = fake_stream
        mock_registry.list_tools.return_value = [read_tool]
        mock_registry.get.return_value = read_tool
        loop = AgentLoop(
            provider_manager=mock_provider,
            system_prompt="test",
            tool_registry=mock_registry,
            session_storage=storage,
            memory_recall_service=recall_service,
        )
        loop._result_storage = MagicMock()
        loop._result_storage.process.return_value = SimpleNamespace(content="processed result")
        loop._tool_executor.execute_batch = AsyncMock(
            return_value=[ToolResult(content="memory content", is_error=False)]
        )

        await loop.run("read remembered deadline")

        assert call_count == 2
        assert recall_service.read_files == {"project-deadline.md"}
        assert "# Recalled Memory" not in str(captured_messages[1])
        assert "Freeze" not in str(captured_messages[1])
        assert not any(message.metadata.get("type") == "recalled_memory" for message in storage.messages)

    async def test_recalled_memory_injection_keeps_unsuppressed_files(self, mock_provider, mock_registry):
        from iac_code.agent.message import get_recalled_memory_files

        class FakeRecallService:
            def __init__(self):
                self.surfaced: list[str] = []

            def get_stats_snapshot(self):
                return {"last_status": "success"}

            def get_suppressed_files(self):
                return {"old.md"}

            def mark_files_surfaced(self, filenames):
                self.surfaced.extend(filenames)

        class FakeSessionStorage:
            def __init__(self):
                self.messages = []

            def append(self, cwd, session_id, message, git_branch=None):
                self.messages.append(message)

        storage = FakeSessionStorage()
        recall_service = FakeRecallService()
        loop = AgentLoop(
            provider_manager=mock_provider,
            system_prompt="test",
            tool_registry=mock_registry,
            session_storage=storage,
            memory_recall_service=recall_service,
        )
        result = SimpleNamespace(
            content=(
                "# Recalled Memory\n\n"
                "## old.md\n[project] old topic\n\nold body\n\n"
                "## new.md\n[project] new topic\n\nnew body"
            ),
            selected_files=["old.md", "new.md"],
        )

        assert loop._inject_recalled_memory_result(result) is True

        [message] = storage.messages
        assert get_recalled_memory_files(message) == ["new.md"]
        assert "new body" in str(message.content)
        assert "old body" not in str(message.content)
        assert recall_service.surfaced == ["new.md"]

    async def test_compacted_out_recalled_memory_remains_suppressed(self, mock_provider, mock_registry):
        from iac_code.agent.message import get_recalled_memory_files

        class FakeRecallService:
            def __init__(self):
                self.surfaced: set[str] = set()

            def get_stats_snapshot(self):
                return {"last_status": "success"}

            def replace_surfaced_files(self, filenames):
                self.surfaced = set(filenames)

            def get_suppressed_files(self):
                return set(self.surfaced)

            def mark_files_surfaced(self, filenames):
                self.surfaced.update(filenames)

        recall_service = FakeRecallService()
        loop = AgentLoop(
            provider_manager=mock_provider,
            system_prompt="test",
            tool_registry=mock_registry,
            memory_recall_service=recall_service,
        )
        first_result = SimpleNamespace(
            content="# Recalled Memory\n\n## old.md\n[project] old topic\n\nold body",
            selected_files=["old.md"],
        )
        second_result = SimpleNamespace(
            content="# Recalled Memory\n\n## old.md\n[project] old topic\n\nold body again",
            selected_files=["old.md"],
        )

        assert loop._inject_recalled_memory_result(first_result) is True
        for i in range(6):
            loop.context_manager.add_user_message(f"User message {i}")
            loop.context_manager.add_assistant_message(f"Assistant response {i}")

        loop.context_manager.apply_compaction("Summary after old memory")
        loop._sync_recall_suppression_from_context()

        assert recall_service.surfaced == {"old.md"}
        assert loop._inject_recalled_memory_result(second_result) is False
        recalled_messages = [
            message
            for message in loop.context_manager.get_messages()
            if get_recalled_memory_files(message) == ["old.md"]
        ]
        assert recalled_messages == []

    async def test_run_streaming_executes_tools_and_applies_extensions(self, mock_provider, mock_registry):
        call_count = 0

        async def fake_stream(messages, system, tools=None, max_tokens=8192):
            nonlocal call_count
            call_count += 1

            if call_count == 1:
                yield MessageStartEvent(message_id="m1")
                yield TextDeltaEvent(text="Before tool")
                yield ToolUseStartEvent(tool_use_id="toolu_1", name="read_file")
                yield ToolUseEndEvent(tool_use_id="toolu_1", name="read_file", input={"path": "a.txt"})
                yield MessageEndEvent(stop_reason="tool_use", usage=Usage())
                return

            yield MessageStartEvent(message_id="m2")
            yield TextDeltaEvent(text="After tool")
            yield MessageEndEvent(stop_reason="end_turn", usage=Usage())

        mock_provider.stream = fake_stream
        mock_registry.list_tools.return_value = [SimpleNamespace(name="read_file", description="Read", input_schema={})]

        loop = AgentLoop(provider_manager=mock_provider, system_prompt="test", tool_registry=mock_registry)
        loop._result_storage = MagicMock()
        loop._result_storage.process.return_value = SimpleNamespace(content="processed result")
        loop.context_manager = MagicMock()
        loop.context_manager.get_api_messages.return_value = []
        loop.context_manager.needs_compaction.return_value = False

        modifier_called = []
        result = ToolResult(
            content="raw result",
            is_error=False,
            new_messages=[{"role": "system", "content": "injected"}],
            context_modifier=lambda ctx: modifier_called.append(ctx) or {"allowed_tool_rules": ["read:*"]},
        )
        loop._tool_executor.execute_batch = AsyncMock(return_value=[result])

        events = [e async for e in loop.run_streaming("Hi")]

        tool_results = [e for e in events if isinstance(e, ToolResultEvent)]
        assert len(tool_results) == 1
        assert tool_results[0].result == "processed result"
        loop.context_manager.add_user_message.assert_called_once_with("Hi")
        assert loop.context_manager.add_assistant_message.call_count == 2
        loop.context_manager.add_tool_results.assert_called_once()
        loop.context_manager.add_raw_message.assert_called_once_with({"role": "system", "content": "injected"})
        assert modifier_called
        assert loop._allowed_tool_rules == ["read:*"]

    async def test_terminal_error_step_result_metadata_stops_streaming(self, mock_provider, mock_registry):
        from iac_code.pipeline.engine.types import StepResult, StepStatus

        calls = 0

        async def fake_stream(messages, system, tools=None, max_tokens=8192):
            nonlocal calls
            calls += 1
            yield MessageStartEvent(message_id=f"m{calls}")
            if calls == 1:
                yield ToolUseStartEvent(tool_use_id="done_1", name="complete_step")
                yield ToolUseEndEvent(
                    tool_use_id="done_1",
                    name="complete_step",
                    input={"conclusion": {"bad": "data"}},
                )
                yield MessageEndEvent(stop_reason="tool_use", usage=Usage())
                return
            yield TextDeltaEvent(text="should not continue")
            yield MessageEndEvent(stop_reason="end_turn", usage=Usage())

        failed_step = StepResult(
            step_id="intent_parsing",
            status=StepStatus.FAILED,
            error="Schema validation failed after 2 attempts",
        )
        mock_provider.stream = fake_stream
        mock_registry.list_tools.return_value = [
            SimpleNamespace(name="complete_step", description="Complete", input_schema={})
        ]
        mock_registry.get.return_value = None

        loop = AgentLoop(provider_manager=mock_provider, system_prompt="test", tool_registry=mock_registry)
        loop._tool_executor.execute_batch = AsyncMock(
            return_value=[
                ToolResult(
                    content="conclusion 校验失败（已超过最大重试次数 1）",
                    is_error=True,
                    metadata={"step_result": failed_step},
                )
            ]
        )

        events = [event async for event in loop.run_streaming("Hi")]

        tool_results = [event for event in events if isinstance(event, ToolResultEvent)]
        assert len(tool_results) == 1
        assert tool_results[0].is_error is True
        assert tool_results[0].metadata == {"step_result": failed_step}
        assert calls == 1

    async def test_run_streaming_tombstone_discards_partial_turn(self, mock_provider, mock_registry):
        from iac_code.types.stream_events import TombstoneEvent

        async def fake_stream(messages, system, tools=None, max_tokens=8192):
            yield MessageStartEvent(message_id="m1")
            yield TextDeltaEvent(text="partial")
            yield TombstoneEvent(message_id="m1")
            yield MessageStartEvent(message_id="m2")
            yield TextDeltaEvent(text="final")
            yield MessageEndEvent(stop_reason="end_turn", usage=Usage())

        mock_provider.stream = fake_stream

        loop = AgentLoop(provider_manager=mock_provider, system_prompt="test", tool_registry=mock_registry)
        loop.context_manager = MagicMock()
        loop.context_manager.get_api_messages.return_value = []
        loop.context_manager.needs_compaction.return_value = False

        await loop.run("Hi")

        assistant_blocks = loop.context_manager.add_assistant_message.call_args.args[0]
        assert len(assistant_blocks) == 1
        assert assistant_blocks[0].text == "final"

    async def test_max_turns_zero_emits_max_turns_without_provider_call(self, mock_provider, mock_registry):
        mock_provider.stream = AsyncMock()

        loop = AgentLoop(
            provider_manager=mock_provider,
            system_prompt="test",
            tool_registry=mock_registry,
            max_turns=0,
        )
        loop.context_manager = MagicMock()
        loop.context_manager.needs_compaction.return_value = False

        events = [e async for e in loop.run_streaming("Hi")]

        assert any(isinstance(e, MessageEndEvent) and e.stop_reason == "max_turns" for e in events)
        mock_provider.stream.assert_not_called()

    async def test_tool_use_exhaustion_emits_max_turns_after_tool_result(self, mock_provider, mock_registry):
        async def fake_stream(messages, system, tools=None, max_tokens=8192):
            yield MessageStartEvent(message_id="m1")
            yield ToolUseStartEvent(tool_use_id="toolu_1", name="read_file")
            yield ToolUseEndEvent(tool_use_id="toolu_1", name="read_file", input={"path": "a.txt"})
            yield MessageEndEvent(stop_reason="tool_use", usage=Usage())

        mock_provider.stream = fake_stream
        mock_registry.list_tools.return_value = [SimpleNamespace(name="read_file", description="Read", input_schema={})]

        loop = AgentLoop(
            provider_manager=mock_provider,
            system_prompt="test",
            tool_registry=mock_registry,
            max_turns=1,
        )
        loop._result_storage = MagicMock()
        loop._result_storage.process.return_value = SimpleNamespace(content="processed result")
        loop.context_manager = MagicMock()
        loop.context_manager.get_api_messages.return_value = []
        loop.context_manager.needs_compaction.return_value = False
        loop._tool_executor.execute_batch = AsyncMock(return_value=[ToolResult(content="raw result", is_error=False)])

        events = [e async for e in loop.run_streaming("Hi")]

        event_types = [e.type for e in events]
        assert "tool_result" in event_types
        assert isinstance(events[-1], MessageEndEvent)
        assert events[-1].stop_reason == "max_turns"

    async def test_normal_completion_does_not_emit_synthetic_max_turns(self, mock_provider, mock_registry):
        async def fake_stream(messages, system, tools=None, max_tokens=8192):
            yield MessageStartEvent(message_id="m1")
            yield TextDeltaEvent(text="Hello!")
            yield MessageEndEvent(stop_reason="end_turn", usage=Usage())

        mock_provider.stream = fake_stream

        loop = AgentLoop(
            provider_manager=mock_provider,
            system_prompt="test",
            tool_registry=mock_registry,
            max_turns=1,
        )

        events = [e async for e in loop.run_streaming("Hi")]

        assert not any(isinstance(e, MessageEndEvent) and e.stop_reason == "max_turns" for e in events)

    async def test_streaming_refreshes_tool_definitions_before_compaction_and_provider(self, mock_provider):
        runtime_tool = SimpleNamespace(
            name="read_file",
            description="Read file",
            input_schema={"type": "object", "properties": {"path": {"type": "string"}}},
        )
        registry = MagicMock()
        registry.list_tools.side_effect = [[], [runtime_tool]]

        async def fake_stream(messages, system, tools=None, max_tokens=8192):
            assert tools is not None
            assert [tool.name for tool in tools] == ["read_file"]
            yield MessageStartEvent(message_id="m1")
            yield TextDeltaEvent(text="Hello!")
            yield MessageEndEvent(stop_reason="end_turn", usage=Usage())

        mock_provider.stream = fake_stream
        loop = AgentLoop(provider_manager=mock_provider, system_prompt="test", tool_registry=registry)
        seen_tool_tokens = []
        original_needs_compaction = loop.context_manager.needs_compaction

        def spy_needs_compaction():
            seen_tool_tokens.append(loop.context_manager.get_usage()["tool_definition_tokens"])
            return original_needs_compaction()

        loop.context_manager.needs_compaction = spy_needs_compaction

        await loop.run("Hi")

        assert seen_tool_tokens and seen_tool_tokens[0] > 0
        assert loop.context_manager.get_usage()["tool_definition_tokens"] == seen_tool_tokens[0]
        assert registry.list_tools.call_count == 2

    async def test_auto_trigger_injects_skill_before_provider_call(self, mock_provider, mock_registry):
        from iac_code.commands.registry import PromptCommand
        from iac_code.skills.frontmatter import SkillFrontmatter
        from iac_code.skills.skill_definition import SkillDefinition
        from iac_code.types.skill_source import SkillSource

        fm = SkillFrontmatter(description="demo", auto_trigger={"script": "auto_trigger.py"})
        skill = SkillDefinition(
            name="demo",
            description="demo",
            frontmatter=fm,
            content="Demo skill prompt",
            source=SkillSource.BUNDLED,
            skill_root="/tmp",
        )
        command = PromptCommand(name="demo", description="demo", skill=skill, source=SkillSource.BUNDLED)

        async def fake_process(prompt, skills, *, loaded_skill_names, context_messages=None, session_id=""):
            loaded_skill_names.add("demo")
            return [
                SimpleNamespace(
                    skill_name="demo",
                    new_messages=[{"role": "user", "content": "<skill-name>demo</skill-name>\n\nDemo skill prompt"}],
                    context_modifier=None,
                )
            ]

        async def fake_stream(messages, system, tools=None, max_tokens=8192):
            assert messages[0].content.startswith("<skill-name>demo</skill-name>")
            assert messages[1].content == "please match"
            yield MessageStartEvent(message_id="m1")
            yield TextDeltaEvent(text="ok")
            yield MessageEndEvent(stop_reason="end_turn", usage=Usage())

        mock_provider.stream = fake_stream
        loop = AgentLoop(
            provider_manager=mock_provider,
            system_prompt="test",
            tool_registry=mock_registry,
            auto_trigger_skills=[command],
        )

        with patch("iac_code.skills.auto_trigger.process_auto_triggered_skills", fake_process):
            events = [e async for e in loop.run_streaming("please match")]

        assert any(isinstance(e, TextDeltaEvent) for e in events)
        assert loop._auto_loaded_skills == {"demo"}

    async def test_auto_trigger_persists_injected_message_before_user_message(self, mock_provider, mock_registry):
        from iac_code.commands.registry import PromptCommand
        from iac_code.skills.frontmatter import SkillFrontmatter
        from iac_code.skills.skill_definition import SkillDefinition
        from iac_code.types.skill_source import SkillSource

        fm = SkillFrontmatter(description="demo", auto_trigger={"script": "auto_trigger.py"})
        skill = SkillDefinition(
            name="demo",
            description="demo",
            frontmatter=fm,
            content="Demo skill prompt",
            source=SkillSource.BUNDLED,
            skill_root="/tmp",
        )
        command = PromptCommand(name="demo", description="demo", skill=skill, source=SkillSource.BUNDLED)
        session_storage = MagicMock()

        async def fake_process(prompt, skills, *, loaded_skill_names, context_messages=None, session_id=""):
            loaded_skill_names.add("demo")
            return [
                SimpleNamespace(
                    skill_name="demo",
                    new_messages=[{"role": "user", "content": "<skill-name>demo</skill-name>\n\nDemo skill prompt"}],
                    context_modifier=None,
                )
            ]

        async def fake_stream(messages, system, tools=None, max_tokens=8192):
            assert messages[0].content.startswith("<skill-name>demo</skill-name>")
            assert messages[1].content == "please match"
            yield MessageStartEvent(message_id="m1")
            yield MessageEndEvent(stop_reason="end_turn", usage=Usage())

        mock_provider.stream = fake_stream
        loop = AgentLoop(
            provider_manager=mock_provider,
            system_prompt="test",
            tool_registry=mock_registry,
            session_storage=session_storage,
            session_id="session-a",
            auto_trigger_skills=[command],
        )

        with patch("iac_code.skills.auto_trigger.process_auto_triggered_skills", fake_process):
            await loop.run("please match")

        persisted_messages = [call.args[2] for call in session_storage.append.call_args_list]
        assert persisted_messages[0].content.startswith("<skill-name>demo</skill-name>")
        assert persisted_messages[1].content == "please match"

    async def test_auto_trigger_resume_keeps_persisted_skill_idempotent(self, tmp_path, mock_provider, mock_registry):
        from iac_code.commands.registry import PromptCommand
        from iac_code.services.session_storage import SessionStorage
        from iac_code.skills.frontmatter import SkillFrontmatter
        from iac_code.skills.skill_definition import SkillDefinition
        from iac_code.types.skill_source import SkillSource

        skill_root = tmp_path / "skill"
        skill_root.mkdir()
        (skill_root / "auto_trigger.py").write_text(
            "ENABLE_AUTO_TRIGGER = True\ndef should_trigger(prompt):\n    return 'match me' in prompt\n",
            encoding="utf-8",
        )
        fm = SkillFrontmatter(description="demo", auto_trigger={"script": "auto_trigger.py"})
        skill = SkillDefinition(
            name="demo",
            description="demo",
            frontmatter=fm,
            content="Demo skill prompt",
            source=SkillSource.BUNDLED,
            skill_root=str(skill_root),
        )
        command = PromptCommand(name="demo", description="demo", skill=skill, source=SkillSource.BUNDLED)
        storage = SessionStorage(projects_dir=tmp_path / "projects")
        cwd = str(tmp_path / "cwd")
        session_id = "session-a"

        async def first_stream(messages, system, tools=None, max_tokens=8192):
            assert messages[0].content.startswith("<skill-name>demo</skill-name>")
            assert messages[1].content == "please match me"
            yield MessageStartEvent(message_id="m1")
            yield MessageEndEvent(stop_reason="end_turn", usage=Usage())

        mock_provider.stream = first_stream
        first_loop = AgentLoop(
            provider_manager=mock_provider,
            system_prompt="test",
            tool_registry=mock_registry,
            session_storage=storage,
            session_id=session_id,
            cwd=cwd,
            auto_trigger_skills=[command],
        )

        await first_loop.run("please match me")
        loaded = storage.load(cwd, session_id)
        assert sum("<skill-name>demo</skill-name>" in message.get_text() for message in loaded) == 1

        async def resumed_stream(messages, system, tools=None, max_tokens=8192):
            assert sum("<skill-name>demo</skill-name>" in message.content for message in messages) == 1
            assert messages[-1].content == "please match me again"
            yield MessageStartEvent(message_id="m2")
            yield MessageEndEvent(stop_reason="end_turn", usage=Usage())

        mock_provider.stream = resumed_stream
        resumed_loop = AgentLoop(
            provider_manager=mock_provider,
            system_prompt="test",
            tool_registry=mock_registry,
            session_storage=storage,
            session_id=session_id,
            resume_messages=loaded,
            cwd=cwd,
            auto_trigger_skills=[command],
        )

        await resumed_loop.run("please match me again")
        reloaded = storage.load(cwd, session_id)

        assert sum("<skill-name>demo</skill-name>" in message.get_text() for message in reloaded) == 1
        assert resumed_loop._auto_loaded_skills == {"demo"}

    async def test_auto_trigger_does_not_repeat_loaded_skill(self, mock_provider, mock_registry):
        from iac_code.commands.registry import PromptCommand
        from iac_code.skills.frontmatter import SkillFrontmatter
        from iac_code.skills.skill_definition import SkillDefinition
        from iac_code.types.skill_source import SkillSource

        fm = SkillFrontmatter(description="demo", auto_trigger={"script": "auto_trigger.py"})
        skill = SkillDefinition(
            name="demo",
            description="demo",
            frontmatter=fm,
            content="Demo skill prompt",
            source=SkillSource.BUNDLED,
            skill_root="/tmp",
        )
        command = PromptCommand(name="demo", description="demo", skill=skill, source=SkillSource.BUNDLED)
        calls = 0

        async def fake_process(prompt, skills, *, loaded_skill_names, context_messages=None, session_id=""):
            nonlocal calls
            calls += 1
            loaded_skill_names.add("demo")
            return [
                SimpleNamespace(
                    skill_name="demo",
                    new_messages=[{"role": "user", "content": "<skill-name>demo</skill-name>\n\nDemo skill prompt"}],
                    context_modifier=None,
                )
            ]

        async def fake_stream(messages, system, tools=None, max_tokens=8192):
            yield MessageStartEvent(message_id="m1")
            yield MessageEndEvent(stop_reason="end_turn", usage=Usage())

        mock_provider.stream = fake_stream
        loop = AgentLoop(
            provider_manager=mock_provider,
            system_prompt="test",
            tool_registry=mock_registry,
            auto_trigger_skills=[command],
        )

        with patch("iac_code.skills.auto_trigger.process_auto_triggered_skills", fake_process):
            await loop.run("first")
            await loop.run("second")

        injected = [
            message
            for message in loop.context_manager.get_messages()
            if isinstance(message.content, str) and "<skill-name>demo</skill-name>" in message.content
        ]
        assert calls == 1
        assert len(injected) == 1


@pytest.mark.asyncio
class TestAgentLoopCompaction:
    async def test_auto_compact_success(self, mock_provider, mock_registry):
        mock_provider.complete = AsyncMock(return_value=SimpleNamespace(text="summary"))
        loop = AgentLoop(provider_manager=mock_provider, system_prompt="test", tool_registry=mock_registry)
        loop.context_manager = MagicMock()
        loop.context_manager.build_compaction_prompt.return_value = "compact me"
        loop.context_manager.apply_compaction.return_value = (1200, 400)

        event = await loop._auto_compact()

        assert isinstance(event, CompactionEvent)
        assert event.original_tokens == 1200
        assert event.compacted_tokens == 400

    async def test_auto_compact_persists_compacted_session(self, mock_provider, mock_registry):
        from iac_code.agent.message import Message

        mock_provider.complete = AsyncMock(return_value=SimpleNamespace(text="summary"))
        session_storage = MagicMock()
        compacted_messages = [Message(role="user", content="[Conversation Summary]\nsummary")]
        loop = AgentLoop(
            provider_manager=mock_provider,
            system_prompt="test",
            tool_registry=mock_registry,
            session_id="auto-compact-session",
            cwd="/tmp/status-project",
            session_storage=session_storage,
        )
        loop._current_git_branch = "main"
        loop.context_manager = MagicMock()
        loop.context_manager.build_compaction_prompt.return_value = "compact me"
        loop.context_manager.apply_compaction.return_value = (1200, 400)
        loop.context_manager.get_messages.return_value = compacted_messages

        await loop._auto_compact()

        session_storage.save.assert_called_once_with(
            "/tmp/status-project",
            "auto-compact-session",
            compacted_messages,
            git_branch="main",
            preserve_cleanup_prompts=True,
        )

    async def test_auto_compact_records_response_usage(self, mock_provider, mock_registry, tmp_path):
        from iac_code.services.session_usage import SessionUsageStore

        mock_provider.complete = AsyncMock(
            return_value=SimpleNamespace(
                text="summary",
                usage=Usage(input_tokens=11, output_tokens=4, cache_read_input_tokens=2),
            )
        )
        store = SessionUsageStore(projects_dir=tmp_path)
        loop = AgentLoop(
            provider_manager=mock_provider,
            system_prompt="test",
            tool_registry=mock_registry,
            session_id="auto-compact-usage",
            cwd="/tmp/status-project",
            session_usage_store=store,
        )
        loop.context_manager = MagicMock()
        loop.context_manager.build_compaction_prompt.return_value = "compact me"
        loop.context_manager.apply_compaction.return_value = (1200, 400)

        event = await loop._auto_compact()

        assert isinstance(event, CompactionEvent)
        totals = loop.get_session_usage()
        assert totals.input_tokens == 11
        assert totals.output_tokens == 4
        assert totals.cache_read_input_tokens == 2
        assert totals.recorded_events == 1
        assert store.load("/tmp/status-project", "auto-compact-usage").total_tokens == 15

    async def test_auto_compact_returns_none_without_prompt(self, mock_provider, mock_registry):
        session_storage = MagicMock()
        loop = AgentLoop(
            provider_manager=mock_provider,
            system_prompt="test",
            tool_registry=mock_registry,
            session_storage=session_storage,
        )
        loop.context_manager = MagicMock()
        loop.context_manager.build_compaction_prompt.return_value = ""

        assert await loop._auto_compact() is None
        session_storage.save.assert_not_called()

    async def test_auto_compact_does_not_persist_on_provider_error(self, mock_provider, mock_registry):
        mock_provider.complete = AsyncMock(side_effect=RuntimeError("boom"))
        session_storage = MagicMock()
        loop = AgentLoop(
            provider_manager=mock_provider,
            system_prompt="test",
            tool_registry=mock_registry,
            session_storage=session_storage,
        )
        loop.context_manager = MagicMock()
        loop.context_manager.build_compaction_prompt.return_value = "compact me"

        assert await loop._auto_compact() is None
        session_storage.save.assert_not_called()

    async def test_compact_returns_success_with_tokens(self, mock_provider, mock_registry):
        mock_provider.complete = AsyncMock(return_value=SimpleNamespace(text="summary"))
        loop = AgentLoop(provider_manager=mock_provider, system_prompt="test", tool_registry=mock_registry)
        loop.context_manager = MagicMock()
        loop.context_manager.get_messages.return_value = [object()]
        loop.context_manager.build_compaction_prompt.return_value = "compact me"
        loop.context_manager.apply_compaction.return_value = (900, 300)

        result = await loop.compact()

        assert result.status == "success"
        assert (result.original_tokens, result.compacted_tokens) == (900, 300)

    async def test_compact_persists_compacted_session(self, mock_provider, mock_registry):
        from iac_code.agent.message import Message

        mock_provider.complete = AsyncMock(return_value=SimpleNamespace(text="summary"))
        session_storage = MagicMock()
        compacted_messages = [Message(role="user", content="[Conversation Summary]\nsummary")]
        loop = AgentLoop(
            provider_manager=mock_provider,
            system_prompt="test",
            tool_registry=mock_registry,
            session_id="manual-compact-session",
            cwd="/tmp/status-project",
            session_storage=session_storage,
        )
        loop._current_git_branch = "dev"
        loop.context_manager = MagicMock()
        loop.context_manager.get_messages.return_value = compacted_messages
        loop.context_manager.build_compaction_prompt.return_value = "compact me"
        loop.context_manager.apply_compaction.return_value = (900, 300)

        result = await loop.compact()

        assert result.status == "success"
        session_storage.save.assert_called_once_with(
            "/tmp/status-project",
            "manual-compact-session",
            compacted_messages,
            git_branch="dev",
            preserve_cleanup_prompts=True,
        )

    async def test_compact_records_response_usage(self, mock_provider, mock_registry, tmp_path):
        from iac_code.services.session_usage import SessionUsageStore

        mock_provider.complete = AsyncMock(
            return_value=SimpleNamespace(
                text="summary",
                usage=Usage(input_tokens=13, output_tokens=6, cache_creation_input_tokens=3),
            )
        )
        store = SessionUsageStore(projects_dir=tmp_path)
        loop = AgentLoop(
            provider_manager=mock_provider,
            system_prompt="test",
            tool_registry=mock_registry,
            session_id="manual-compact-usage",
            cwd="/tmp/status-project",
            session_usage_store=store,
        )
        loop.context_manager = MagicMock()
        loop.context_manager.get_messages.return_value = [object()]
        loop.context_manager.build_compaction_prompt.return_value = "compact me"
        loop.context_manager.apply_compaction.return_value = (900, 300)

        result = await loop.compact()

        assert result.status == "success"
        totals = loop.get_session_usage()
        assert totals.input_tokens == 13
        assert totals.output_tokens == 6
        assert totals.cache_creation_input_tokens == 3
        assert totals.recorded_events == 1
        assert store.load("/tmp/status-project", "manual-compact-usage").total_tokens == 19

    async def test_compact_returns_empty_when_no_messages(self, mock_provider, mock_registry):
        session_storage = MagicMock()
        loop = AgentLoop(
            provider_manager=mock_provider,
            system_prompt="test",
            tool_registry=mock_registry,
            session_storage=session_storage,
        )
        loop.context_manager = MagicMock()
        loop.context_manager.get_messages.return_value = []

        result = await loop.compact()

        assert result.status == "empty"
        session_storage.save.assert_not_called()

    async def test_compact_returns_too_short_when_all_in_preserve_window(self, mock_provider, mock_registry):
        session_storage = MagicMock()
        loop = AgentLoop(
            provider_manager=mock_provider,
            system_prompt="test",
            tool_registry=mock_registry,
            session_storage=session_storage,
        )
        loop.context_manager = MagicMock()
        loop.context_manager.get_messages.return_value = [object()]
        loop.context_manager.build_compaction_prompt.return_value = ""
        loop.context_manager.preserve_recent_turns = 3

        result = await loop.compact()

        assert result.status == "too_short"
        assert result.preserve_recent_turns == 3
        session_storage.save.assert_not_called()

    async def test_compact_returns_failed_on_provider_error(self, mock_provider, mock_registry):
        mock_provider.complete = AsyncMock(side_effect=RuntimeError("boom"))
        session_storage = MagicMock()
        loop = AgentLoop(
            provider_manager=mock_provider,
            system_prompt="test",
            tool_registry=mock_registry,
            session_storage=session_storage,
        )
        loop.context_manager = MagicMock()
        loop.context_manager.get_messages.return_value = [object()]
        loop.context_manager.build_compaction_prompt.return_value = "compact me"

        result = await loop.compact()

        assert result.status == "failed"
        session_storage.save.assert_not_called()


class TestAgentLoopHelpers:
    def test_reset_and_get_context_usage_delegate(self, mock_provider, mock_registry):
        class FakeRecallService:
            def __init__(self):
                self.reset_called = False

            def reset_stats(self):
                self.reset_called = True

            def get_stats_snapshot(self):
                return {"last_status": "success"}

        recall_service = FakeRecallService()
        loop = AgentLoop(
            provider_manager=mock_provider,
            system_prompt="test",
            tool_registry=mock_registry,
            memory_recall_service=recall_service,
        )
        loop.context_manager = MagicMock()
        loop.context_manager.get_usage.return_value = {"total_tokens": 10}
        loop._auto_loaded_skills.add("iac-aliyun")

        loop.reset()
        usage = loop.get_context_usage()

        loop.context_manager.reset.assert_called_once()
        assert loop._auto_loaded_skills == set()
        assert usage == {"total_tokens": 10}
        assert recall_service.reset_called is True

    def test_replace_session_clears_auto_loaded_skills(self, mock_provider, mock_registry):
        from iac_code.agent.message import Message

        loop = AgentLoop(provider_manager=mock_provider, system_prompt="test", tool_registry=mock_registry)
        loop._auto_loaded_skills.add("demo")

        loop.replace_session("session-b", [Message(role="user", content="new session")])

        assert loop._auto_loaded_skills == set()


class TestAgentLoopSetProvider:
    def test_set_provider_preserves_messages(self, mock_provider, mock_registry):
        loop = AgentLoop(provider_manager=mock_provider, system_prompt="test", tool_registry=mock_registry)
        loop.context_manager.add_user_message("Hello")
        loop.context_manager.add_user_message("World")

        new_provider = MagicMock()
        new_provider.get_model_name.return_value = "claude-opus-4-7"
        loop.set_provider(new_provider)

        assert loop._provider_manager is new_provider
        messages = loop.context_manager.get_messages()
        assert len(messages) == 2
        assert messages[0].get_text() == "Hello"
        assert messages[1].get_text() == "World"

    def test_set_provider_updates_context_window_for_new_model(self, mock_provider, mock_registry):
        loop = AgentLoop(provider_manager=mock_provider, system_prompt="test", tool_registry=mock_registry)
        # mock_provider returns "test-model" → falls back to default config (128_000)
        assert loop.context_manager._config.context_window == 128_000

        new_provider = MagicMock()
        new_provider.get_model_name.return_value = "claude-opus-4-7"
        loop.set_provider(new_provider)

        assert loop.context_manager._config.context_window == 200_000

    def test_set_provider_optionally_refreshes_system_prompt(self, mock_provider, mock_registry):
        loop = AgentLoop(provider_manager=mock_provider, system_prompt="old prompt", tool_registry=mock_registry)

        new_provider = MagicMock()
        new_provider.get_model_name.return_value = "test-model"
        loop.set_provider(new_provider, system_prompt="new prompt")

        assert loop.system_prompt == "new prompt"
        assert loop.context_manager.system_prompt == "new prompt"

    def test_set_provider_keeps_system_prompt_when_none(self, mock_provider, mock_registry):
        loop = AgentLoop(provider_manager=mock_provider, system_prompt="kept", tool_registry=mock_registry)

        new_provider = MagicMock()
        new_provider.get_model_name.return_value = "test-model"
        loop.set_provider(new_provider)

        assert loop.system_prompt == "kept"
        assert loop.context_manager.system_prompt == "kept"

    def test_set_provider_resyncs_tool_definitions(self, mock_provider, mock_registry):
        tool = SimpleNamespace(name="read_file", description="Read file", input_schema={"type": "object"})
        mock_registry.list_tools.return_value = [tool]
        loop = AgentLoop(provider_manager=mock_provider, system_prompt="test", tool_registry=mock_registry)
        loop.context_manager = MagicMock()

        new_provider = MagicMock()
        new_provider.get_model_name.return_value = "claude-opus-4-7"
        loop.set_provider(new_provider)

        loop.context_manager.set_model.assert_called_once_with("claude-opus-4-7")
        loop.context_manager.set_tool_definitions.assert_called_once()
