from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from iac_code.agent.agent_loop import AgentLoop
from iac_code.tools.base import ToolResult
from iac_code.tools.tool_executor import ToolExecutor
from iac_code.types.stream_events import (
    CompactionEvent,
    MessageEndEvent,
    MessageStartEvent,
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

    async def test_auto_compact_returns_none_without_prompt(self, mock_provider, mock_registry):
        loop = AgentLoop(provider_manager=mock_provider, system_prompt="test", tool_registry=mock_registry)
        loop.context_manager = MagicMock()
        loop.context_manager.build_compaction_prompt.return_value = ""

        assert await loop._auto_compact() is None

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

    async def test_compact_returns_empty_when_no_messages(self, mock_provider, mock_registry):
        loop = AgentLoop(provider_manager=mock_provider, system_prompt="test", tool_registry=mock_registry)
        loop.context_manager = MagicMock()
        loop.context_manager.get_messages.return_value = []

        result = await loop.compact()

        assert result.status == "empty"

    async def test_compact_returns_too_short_when_all_in_preserve_window(self, mock_provider, mock_registry):
        loop = AgentLoop(provider_manager=mock_provider, system_prompt="test", tool_registry=mock_registry)
        loop.context_manager = MagicMock()
        loop.context_manager.get_messages.return_value = [object()]
        loop.context_manager.build_compaction_prompt.return_value = ""
        loop.context_manager.preserve_recent_turns = 3

        result = await loop.compact()

        assert result.status == "too_short"
        assert result.preserve_recent_turns == 3

    async def test_compact_returns_failed_on_provider_error(self, mock_provider, mock_registry):
        mock_provider.complete = AsyncMock(side_effect=RuntimeError("boom"))
        loop = AgentLoop(provider_manager=mock_provider, system_prompt="test", tool_registry=mock_registry)
        loop.context_manager = MagicMock()
        loop.context_manager.get_messages.return_value = [object()]
        loop.context_manager.build_compaction_prompt.return_value = "compact me"

        result = await loop.compact()

        assert result.status == "failed"


class TestAgentLoopHelpers:
    def test_reset_and_get_context_usage_delegate(self, mock_provider, mock_registry):
        loop = AgentLoop(provider_manager=mock_provider, system_prompt="test", tool_registry=mock_registry)
        loop.context_manager = MagicMock()
        loop.context_manager.get_usage.return_value = {"total_tokens": 10}
        loop._auto_loaded_skills.add("iac-aliyun")

        loop.reset()
        usage = loop.get_context_usage()

        loop.context_manager.reset.assert_called_once()
        assert loop._auto_loaded_skills == set()
        assert usage == {"total_tokens": 10}

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
