from __future__ import annotations

from io import StringIO
from unittest.mock import MagicMock, patch

import pytest
from rich.console import Console

from iac_code.agent.message import Message, create_recalled_memory_message
from iac_code.pipeline.engine.cleanup import CLEANUP_PROMPT_METADATA_TYPE
from iac_code.tools.base import Tool, ToolContext, ToolRegistry, ToolResult
from iac_code.tools.read_file import ReadFileTool
from iac_code.types.stream_events import StackInstancesProgressEvent, StackProgressEvent
from iac_code.ui.core.key_event import KeyEvent
from iac_code.ui.renderer import (
    RenderedTurn,
    Renderer,
    StreamingInputBuffer,
    _CropTop,
    _DashMarkdown,
    _Segment,
    _SubAgentChild,
    _ToolCallRecord,
)


def make_console(width: int = 80, height: int = 12) -> Console:
    return Console(
        file=StringIO(),
        width=width,
        height=height,
        force_terminal=True,
        color_system=None,
        legacy_windows=False,
        _environ={},
    )


class DemoTool(Tool):
    @property
    def name(self) -> str:
        return "demo"

    @property
    def description(self) -> str:
        return "demo"

    @property
    def input_schema(self) -> dict:
        return {"type": "object"}

    async def execute(self, *, tool_input: dict, context: ToolContext) -> ToolResult:
        return ToolResult.success("ok")

    def render_tool_use_message(self, input: dict, *, verbose: bool = False) -> str | None:
        return "detail verbose" if verbose else "detail"

    def render_tool_result_message(self, output: str, *, is_error: bool = False, verbose: bool = False) -> str | None:
        return f"{output} verbose" if verbose else output

    def user_facing_name(self, input: dict | None = None) -> str:
        return "Demo"


def make_renderer() -> Renderer:
    console = make_console()
    registry = ToolRegistry()
    registry.register(DemoTool())
    return Renderer(console, registry, status_callback=lambda: "ready")


def make_renderer_with_read_tool() -> Renderer:
    registry = ToolRegistry()
    registry.register(ReadFileTool())
    return Renderer(make_console(), registry, status_callback=lambda: "ready")


class TestThinkingSegment:
    def test_segment_supports_thinking_summary_kind(self):
        seg = _Segment(kind="thinking_summary", elapsed_seconds=12.3)
        assert seg.kind == "thinking_summary"
        assert seg.elapsed_seconds == 12.3
        assert seg.text == ""
        assert seg.tool is None

    def test_segment_default_elapsed_zero(self):
        seg = _Segment(kind="text", text="hi")
        assert seg.elapsed_seconds == 0.0


class TestRendererHelpers:
    def test_dash_markdown_renders_dash_bullets(self):
        console = make_console()
        console.print(_DashMarkdown("* first\n* second"))
        output = console.file.getvalue()
        assert " - first" in output
        assert " - second" in output

    def test_crop_top_keeps_last_lines(self):
        console = make_console()
        console.print(_CropTop("one\ntwo\nthree\nfour", max_height=2))
        output = console.file.getvalue()
        assert "one" not in output
        assert "two" not in output
        assert "three" in output
        assert "four" in output

    def test_find_safe_split_pos_skips_fenced_blocks(self):
        renderer = make_renderer()
        text = "intro\n\n```py\nx = 1\n\nx = 2\n```\n\noutro"
        pos, in_fence = renderer._find_safe_split_pos(text)
        assert in_fence is False
        assert text[pos : pos + 2] == "\n\n"
        assert text[pos + 2 :].startswith("outro")

    def test_build_footer_and_record_user_turn(self):
        renderer = make_renderer()
        footer = renderer._build_footer()
        renderer.console.print(footer)
        output = renderer.console.file.getvalue()
        assert "ready" in output
        assert "❯" in output

        renderer.record_user_turn("hello")
        assert renderer.message_history == [
            RenderedTurn(role="user", text="hello", timestamp=renderer.message_history[0].timestamp)
        ]

    def test_build_footer_shows_full_queued_message_section(self):
        renderer = make_renderer()
        buffer = StreamingInputBuffer()
        for char in "你好":
            buffer.handle_key(KeyEvent(key=char, char=char))
        buffer.handle_key(KeyEvent(key="enter", char="\n"))
        renderer._streaming_input = buffer

        renderer.console.print(renderer._build_footer())
        output = renderer.console.file.getvalue()

        assert "Messages to be submitted after next tool call" in output
        assert "press esc to interrupt" in output
        assert "send" in output
        assert "immediately" in output
        assert "↳ 你好" in output
        assert "↵ 1" not in output

    def test_build_footer_uses_i18n_for_queued_message_section(self, monkeypatch):
        import iac_code.ui.renderer as renderer_mod

        translations = {
            "Messages to be submitted after next tool call": "下次工具调用后要提交的消息",
            "press esc to interrupt and send immediately": "按 esc 中断并立即发送",
        }
        monkeypatch.setattr(renderer_mod, "_", lambda message: translations.get(message, message))
        renderer = make_renderer()
        buffer = StreamingInputBuffer()
        for char in "你好":
            buffer.handle_key(KeyEvent(key=char, char=char))
        buffer.handle_key(KeyEvent(key="enter", char="\n"))
        renderer._streaming_input = buffer

        renderer.console.print(renderer._build_footer())
        output = renderer.console.file.getvalue()

        assert "下次工具调用后要提交的消息" in output
        assert "按 esc 中断并立即发送" in output

    def test_replay_history_hides_recalled_memory_messages(self):
        renderer = make_renderer()

        renderer.replay_history(
            [
                Message(role="user", content="visible question"),
                create_recalled_memory_message("# Recalled Memory\nPrefer ROS YAML.", ["ros-yaml.md"]),
                Message(role="assistant", content="visible answer"),
            ]
        )

        output = renderer.console.file.getvalue()
        assert "visible question" in output
        assert "visible answer" in output
        assert "Prefer ROS YAML" not in output
        assert "Relevant persistent memories" not in output

    def test_replay_history_hides_pipeline_cleanup_prompt(self):
        renderer = make_renderer()

        renderer.replay_history(
            [
                Message(role="user", content="visible question"),
                Message(
                    role="user",
                    content="hidden cleanup prompt",
                    metadata={"type": CLEANUP_PROMPT_METADATA_TYPE},
                ),
                Message(role="assistant", content="visible answer"),
            ]
        )

        output = renderer.console.file.getvalue()
        assert "visible question" in output
        assert "visible answer" in output
        assert "hidden cleanup prompt" not in output

    def test_any_segment_has_verbose_content(self):
        renderer = make_renderer()
        segments = [
            _Segment(kind="tool", tool=_ToolCallRecord(tool_name="demo", tool_input={}, done=True, result="done"))
        ]
        assert renderer._any_segment_has_verbose(segments) is True

    def test_render_tool_result_uses_tool_summary(self):
        renderer = make_renderer()
        line = renderer._render_tool_result(_ToolCallRecord(tool_name="demo", tool_input={}, done=True, result="done"))
        assert line is not None
        assert "done" in str(line)

    def test_render_progress_groups_include_resource_rows(self):
        renderer = make_renderer()

        stack = renderer._render_stack_progress(
            StackProgressEvent(
                stack_id="stack-1",
                stack_name="demo-stack",
                status="CREATE_IN_PROGRESS",
                progress_percentage=50,
                resources=[
                    {
                        "name": "vpc",
                        "resource_type": "ALIYUN::ECS::VPC",
                        "status": "CREATE_COMPLETE",
                        "status_icon": "✓",
                    }
                ],
                elapsed_seconds=10,
            )
        )
        instances = renderer._render_instances_progress(
            StackInstancesProgressEvent(
                stack_group_name="demo-group",
                operation_id="op-1",
                status="RUNNING",
                progress_percentage=75,
                instances=[{"account_id": "123", "region_id": "cn-hz", "status": "SUCCEEDED", "status_icon": "✓"}],
                elapsed_seconds=12,
            )
        )

        renderer.console.print(stack)
        renderer.console.print(instances)
        output = renderer.console.file.getvalue()
        assert "demo-stack" in output
        assert "vpc" in output
        assert "demo-group" in output
        assert "cn-hz" in output

    def test_render_tool_header_shows_child_summary_and_result_hides_in_compact_mode(self):
        renderer = make_renderer()
        record = _ToolCallRecord(
            tool_name="demo",
            tool_input={"path": "a.txt"},
            done=True,
            result="used 1200 tokens",
            children=[_SubAgentChild(tool_name="demo", tool_input={})],
            start_time=10.0,
        )

        with patch("iac_code.ui.renderer.time.monotonic", return_value=12.5):
            header = renderer._render_tool_header(record)

        assert "Done (1 tool uses" in str(header)
        assert "1.2k tokens" in str(header)
        assert renderer._render_tool_result(record) is None

    def test_render_tool_header_localizes_pipeline_tool_name(self):
        renderer = make_renderer()
        record = _ToolCallRecord(tool_name="complete_step", tool_input={}, done=True)

        header = renderer._render_tool_header(record)

        assert "Complete step" in header.plain
        assert "complete_step" not in header.plain

    def test_print_segments_to_scrollback_archives_and_merges_assistant_turns(self):
        renderer = make_renderer()

        renderer._print_segments_to_scrollback([_Segment(kind="text", text="first")], "")
        renderer._print_segments_to_scrollback([], "second")

        assert len(renderer.message_history) == 1
        assert renderer.message_history[0].role == "assistant"
        assert [segment.text for segment in renderer.message_history[0].segments] == ["first", "second"]
        output = renderer.console.file.getvalue()
        assert "first" in output
        assert "second" in output

    def test_replay_history_hides_internal_skill_context_messages(self):
        from iac_code.agent.message import Message

        renderer = make_renderer()

        renderer.replay_history(
            [
                Message(role="user", content="继续"),
                Message(
                    role="user",
                    content=(
                        "<skill-name>iac-aliyun</skill-name>\n\nBase directory for this skill: /tmp/skill\n\n# Body"
                    ),
                ),
                Message(role="assistant", content="ok"),
            ]
        )

        output = renderer.console.file.getvalue()
        assert "继续" in output
        assert "ok" in output
        assert "<skill-name>iac-aliyun</skill-name>" not in output
        assert "Base directory for this skill" not in output

    def test_show_transcript_constructs_view_with_current_segments(self):
        renderer = make_renderer()
        fake_view = MagicMock()

        with patch("iac_code.ui.transcript_view.TranscriptView", return_value=fake_view) as transcript_view:
            renderer.show_transcript(current_segments=[_Segment(kind="text", text="live")])

        transcript_view.assert_called_once()
        fake_view.run.assert_called_once_with()

    @pytest.mark.asyncio
    async def test_prompt_permission_allow_once(self):
        renderer = make_renderer()
        event = MagicMock(tool_name="demo", tool_input={"path": "a.txt"})

        with patch("iac_code.ui.components.select.Select.run", return_value="allow_once"):
            allowed = await renderer.prompt_permission(event)

        assert allowed is True
        output = renderer.console.file.getvalue()
        assert "Allow this action?" in output
        assert "detail" in output


class TestStreamingInputBuffer:
    def test_enter_queues_current_buffer_and_clears_prompt(self):
        buffer = StreamingInputBuffer()

        for char in "next turn":
            buffer.handle_key(KeyEvent(key=char, char=char))
        outcome = buffer.handle_key(KeyEvent(key="enter", char="\n"))

        assert outcome == "queued"
        assert buffer.queued_inputs == ["next turn"]
        assert buffer.text == ""

    def test_escape_interrupts_and_queues_unsubmitted_buffer(self):
        buffer = StreamingInputBuffer()

        for char in "redirect":
            buffer.handle_key(KeyEvent(key=char, char=char))
        outcome = buffer.handle_key(KeyEvent(key="escape", char="\x1b"))

        assert outcome == "interrupt"
        assert buffer.interrupted is True
        assert buffer.queued_inputs == ["redirect"]

    def test_escape_interrupts_without_duplicating_existing_queue(self):
        buffer = StreamingInputBuffer()

        for char in "queued":
            buffer.handle_key(KeyEvent(key=char, char=char))
        buffer.handle_key(KeyEvent(key="enter", char="\n"))
        outcome = buffer.handle_key(KeyEvent(key="escape", char="\x1b"))

        assert outcome == "interrupt"
        assert buffer.interrupted is True
        assert buffer.queued_inputs == ["queued"]

    def test_drain_queued_inputs_keeps_non_matching_items(self):
        buffer = StreamingInputBuffer()
        for text in ("prompt", "/help", "second"):
            for char in text:
                buffer.handle_key(KeyEvent(key=char, char=char))
            buffer.handle_key(KeyEvent(key="enter", char="\n"))

        drained = buffer.drain_queued_inputs(lambda value: not value.startswith("/"))

        assert drained == ["prompt", "second"]
        assert buffer.queued_inputs == ["/help"]


class TestStreamingHeaderPreview:
    def test_header_uses_partial_input_when_tool_input_is_empty(self):
        renderer = make_renderer_with_read_tool()
        rec = _ToolCallRecord(
            tool_name="read_file",
            tool_input={},
            partial_input='{"path": "src/foo.py"',  # path closed, JSON object not closed
        )

        header = renderer._render_tool_header(rec)

        assert "foo.py" in header.plain

    def test_header_ignores_partial_input_when_tool_input_is_present(self):
        renderer = make_renderer_with_read_tool()
        rec = _ToolCallRecord(
            tool_name="read_file",
            tool_input={"path": "src/real.py"},
            partial_input='{"path": "src/stale.py"',  # should be ignored
        )

        header = renderer._render_tool_header(rec)

        assert "real.py" in header.plain
        assert "stale.py" not in header.plain

    def test_header_no_detail_when_partial_input_field_not_yet_closed(self):
        renderer = make_renderer_with_read_tool()
        rec = _ToolCallRecord(
            tool_name="read_file",
            tool_input={},
            partial_input='{"path": "src/foo',  # value not closed
        )

        header = renderer._render_tool_header(rec)

        # No parens means no detail rendered
        assert "(" not in header.plain
