from __future__ import annotations

import asyncio
import json
from io import StringIO
from unittest.mock import MagicMock, patch

import pytest
from rich.console import Console

from iac_code.agent.agent_loop import AgentLoop
from iac_code.agent.message import (
    ImageBlock,
    Message,
    TextBlock,
    ToolResultBlock,
    ToolUseBlock,
    create_recalled_memory_message,
)
from iac_code.pipeline.engine.cleanup import CLEANUP_PROMPT_METADATA_TYPE
from iac_code.tools.base import Tool, ToolContext, ToolRegistry, ToolResult
from iac_code.tools.read_file import ReadFileTool
from iac_code.types.stream_events import (
    TOOL_RENDER_METADATA_KEY,
    TOOL_RENDER_RESULT_COMPACT_KEY,
    TOOL_RENDER_RESULT_VERBOSE_KEY,
    MCPProgressEvent,
    PermissionRequestEvent,
    StackInstancesProgressEvent,
    StackProgressEvent,
)
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


def make_link_console(width: int = 80, height: int = 12) -> Console:
    return Console(
        file=StringIO(),
        width=width,
        height=height,
        force_terminal=True,
        color_system="standard",
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


class AliyunInstanceRendererTool(DemoTool):
    @property
    def name(self) -> str:
        return "aliyun_api"

    def render_tool_result_message(self, output: str, *, is_error: bool = False, verbose: bool = False) -> str | None:
        return "mutable instance renderer"


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

    def test_replay_history_does_not_link_plain_image_refs_without_image_blocks(self):
        console = make_link_console()
        registry = ToolRegistry()
        renderer = Renderer(
            console,
            registry,
            status_callback=lambda: "ready",
            image_path_resolver=lambda image_id: f"/tmp/session-image-{image_id}.png",
        )

        renderer.replay_history([Message(role="user", content="see [Image #1]")])

        output = console.file.getvalue()
        assert "[Image #1]" in output
        assert "\x1b]8;" not in output
        assert "file:///tmp/session-image-1.png" not in output

    def test_replay_history_renders_structured_image_blocks_as_image_refs(self):
        console = make_link_console()
        registry = ToolRegistry()
        renderer = Renderer(
            console,
            registry,
            status_callback=lambda: "ready",
            image_block_path_resolver=lambda block: f"/tmp/session-image-{block.ref_id}.png",
        )

        renderer.replay_history(
            [
                Message(
                    role="user",
                    content=[
                        TextBlock(text="see "),
                        ImageBlock(media_type="image/png", data="aGVsbG8=", ref_id=8),
                    ],
                )
            ]
        )

        output = console.file.getvalue()
        assert "see " in output
        assert "[Image #8]" in output
        assert renderer._file_url("/tmp/session-image-8.png") in output

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

    def test_render_tool_result_uses_record_render_metadata_when_registry_missing(self):
        renderer = Renderer(make_console(), ToolRegistry(), status_callback=lambda: "ready")
        rec = _ToolCallRecord(
            tool_name="infraguard_scan",
            tool_input={},
            done=True,
            result='{"command": ["infraguard", "scan"]}',
            metadata={
                TOOL_RENDER_METADATA_KEY: {
                    TOOL_RENDER_RESULT_COMPACT_KEY: "passed · 0 findings",
                    TOOL_RENDER_RESULT_VERBOSE_KEY: "Command: infraguard scan\nStatus: passed",
                }
            },
        )

        compact = renderer._render_tool_result(rec)
        assert compact is not None
        assert "passed · 0 findings" in compact.plain
        assert '{"command"' not in compact.plain

        renderer._verbose = True
        verbose = renderer._render_tool_result(rec)
        assert verbose is not None
        assert "Command: infraguard scan" in verbose.plain
        assert '{"command"' not in verbose.plain

    def test_replay_history_uses_tool_result_block_render_metadata_when_registry_missing(self):
        renderer = Renderer(make_console(), ToolRegistry(), status_callback=lambda: "ready")
        long_error = (
            "Invalid input for tool 'complete_step': 'conclusion' is a required property\n"
            "Current step: intent_parsing\n"
            "conclusion must match this schema summary:\n"
            '{"type": "object", "required": ["is_infra_intent", "confidence"]}'
        )
        messages = [
            Message(
                role="assistant",
                content=[ToolUseBlock(id="tu_bad", name="complete_step", input={})],
            ),
            Message(
                role="user",
                content=[
                    ToolResultBlock(
                        tool_use_id="tu_bad",
                        content=long_error,
                        is_error=True,
                        metadata={
                            TOOL_RENDER_METADATA_KEY: {
                                TOOL_RENDER_RESULT_COMPACT_KEY: "complete_step validation failed."
                            }
                        },
                    )
                ],
            ),
        ]

        renderer.replay_history(messages)

        output = renderer.console.file.getvalue()
        assert "complete_step validation failed." in output
        assert "'conclusion' is a required property" not in output
        assert "schema summary" not in output

    def test_marked_aliyun_result_prefers_atomic_metadata_over_registered_instance_live_and_replay(self):
        registry = ToolRegistry()
        registry.register(AliyunInstanceRendererTool())
        renderer = Renderer(make_console(), registry, status_callback=lambda: "ready")
        metadata = {
            "aliyun_http": {
                "contract_version": "aliyun_body_v1",
                "status": 200,
                "status_class": "2xx",
                "response_mode": "json",
                "body_format": "json",
                "content_state": "inline_final",
            },
            TOOL_RENDER_METADATA_KEY: {
                TOOL_RENDER_RESULT_COMPACT_KEY: "atomic compact summary",
                TOOL_RENDER_RESULT_VERBOSE_KEY: "atomic verbose summary",
            },
        }
        record = _ToolCallRecord(
            tool_name="aliyun_api",
            tool_input={"action": "DescribeInstances"},
            done=True,
            result='{"RequestId":"req-1"}',
            metadata=metadata,
        )

        compact = renderer._render_tool_result(record)
        assert compact is not None
        assert "atomic compact summary" in compact.plain
        assert "mutable instance renderer" not in compact.plain

        renderer._verbose = True
        verbose = renderer._render_tool_result(record)
        assert verbose is not None
        assert "atomic verbose summary" in verbose.plain
        assert "mutable instance renderer" not in verbose.plain

        renderer._verbose = False
        renderer.replay_history(
            [
                Message(
                    role="assistant",
                    content=[
                        ToolUseBlock(
                            id="tool-aliyun",
                            name="aliyun_api",
                            input={"action": "DescribeInstances"},
                        )
                    ],
                ),
                Message(
                    role="user",
                    content=[
                        ToolResultBlock(
                            tool_use_id="tool-aliyun",
                            content='{"RequestId":"req-1"}',
                            metadata=metadata,
                        )
                    ],
                ),
            ]
        )
        output = renderer.console.file.getvalue()
        assert "atomic compact summary" in output
        assert "mutable instance renderer" not in output

    @pytest.mark.asyncio
    async def test_concurrent_marked_aliyun_results_keep_atomic_render_metadata_isolated(self):
        registry = ToolRegistry()
        registry.register(AliyunInstanceRendererTool())
        renderer = Renderer(make_console(), registry, status_callback=lambda: "ready")

        def marked_metadata(request_id: str) -> dict:
            return {
                "aliyun_http": {
                    "contract_version": "aliyun_body_v1",
                    "product": "Ecs",
                    "version": "2014-05-26",
                    "action": "DescribeInstances",
                    "status": 200,
                    "status_class": "2xx",
                    "response_mode": "json",
                    "body_format": "json",
                    "content_state": "inline_final",
                },
                "request_id": request_id,
            }

        async def build_record(request_id: str) -> _ToolCallRecord:
            output = json.dumps({"RequestId": request_id})
            metadata = await asyncio.to_thread(
                AgentLoop._tool_result_render_metadata,
                marked_metadata(request_id),
                registry.get("aliyun_api"),
                output,
                is_error=False,
                tool_name="aliyun_api",
                tool_input={"action": "DescribeInstances"},
            )
            return _ToolCallRecord(
                tool_name="aliyun_api",
                tool_input={"action": "DescribeInstances"},
                done=True,
                result=output,
                metadata=metadata,
            )

        record_a, record_b = await asyncio.gather(build_record("req-a"), build_record("req-b"))
        rendered_a, rendered_b = await asyncio.gather(
            asyncio.to_thread(renderer._render_tool_result, record_a),
            asyncio.to_thread(renderer._render_tool_result, record_b),
        )

        assert rendered_a is not None
        assert rendered_b is not None
        assert "req-a" in rendered_a.plain
        assert "req-b" not in rendered_a.plain
        assert "req-b" in rendered_b.plain
        assert "req-a" not in rendered_b.plain
        assert "mutable instance renderer" not in rendered_a.plain
        assert "mutable instance renderer" not in rendered_b.plain

    def test_unmarked_aliyun_result_keeps_registered_instance_renderer_priority(self):
        registry = ToolRegistry()
        registry.register(AliyunInstanceRendererTool())
        renderer = Renderer(make_console(), registry, status_callback=lambda: "ready")
        record = _ToolCallRecord(
            tool_name="aliyun_api",
            tool_input={},
            done=True,
            result='{"status":200,"body":{"RequestId":"old"}}',
            metadata={TOOL_RENDER_METADATA_KEY: {TOOL_RENDER_RESULT_COMPACT_KEY: "metadata fallback"}},
        )

        rendered = renderer._render_tool_result(record)

        assert rendered is not None
        assert "mutable instance renderer" in rendered.plain
        assert "metadata fallback" not in rendered.plain

    def test_replay_history_summarizes_legacy_complete_step_error_without_metadata(self):
        renderer = Renderer(make_console(), ToolRegistry(), status_callback=lambda: "ready")
        long_error = (
            "Invalid input for tool 'complete_step': 'conclusion' is a required property\n"
            "Current step: intent_parsing\n"
            "conclusion must match this schema summary:\n"
            '{"type": "object", "required": ["is_infra_intent", "confidence"]}'
        )
        messages = [
            Message(
                role="assistant",
                content=[ToolUseBlock(id="tu_bad", name="complete_step", input={})],
            ),
            Message(
                role="user",
                content=[ToolResultBlock(tool_use_id="tu_bad", content=long_error, is_error=True)],
            ),
        ]

        renderer.replay_history(messages)

        output = renderer.console.file.getvalue()
        assert "complete_step validation failed." in output
        assert "'conclusion' is a required property" not in output
        assert "schema summary" not in output

    def test_replay_history_summarizes_legacy_ask_user_question_error_without_metadata(self):
        renderer = Renderer(make_console(), ToolRegistry(), status_callback=lambda: "ready")
        long_error = (
            "Invalid input for tool 'ask_user_question': "
            "[{'id': 'tech_stack_nodejs', 'label': 'Node.js'}] is not of type 'object'. "
            "Please provide all required parameters as defined in the tool schema."
        )
        messages = [
            Message(
                role="assistant",
                content=[ToolUseBlock(id="tu_bad", name="ask_user_question", input={})],
            ),
            Message(
                role="user",
                content=[ToolResultBlock(tool_use_id="tu_bad", content=long_error, is_error=True)],
            ),
        ]

        renderer.replay_history(messages)

        output = renderer.console.file.getvalue()
        assert "ask_user_question validation failed." in output
        assert "tech_stack_nodejs" not in output
        assert "not of type" not in output

    def test_render_tool_result_sanitizes_mcp_public_output_without_mutating_result(self):
        renderer = make_renderer()
        raw_result = (
            "command=IAC_PRIVATE_COMMAND_ARG_MARKER_56 "
            "metadata=IAC_PRIVATE_NESTED_METADATA_MARKER_56 "
            "url=https://example.test/mcp?Signature=IAC_PRIVATE_QUERY_MARKER_56 "
            "path=file:///Users/alice/.iac-code/settings.yml"
        )
        record = _ToolCallRecord(tool_name="mcp__remote__echo", tool_input={}, done=True, result=raw_result)

        line = renderer._render_tool_result(record)

        assert line is not None
        assert "IAC_PRIVATE_COMMAND_ARG_MARKER_56" not in line.plain
        assert "IAC_PRIVATE_NESTED_METADATA_MARKER_56" not in line.plain
        assert "IAC_PRIVATE_QUERY_MARKER_56" not in line.plain
        assert "/Users/alice" not in line.plain
        assert "[REDACTED]" in line.plain
        assert "[PATH]" in line.plain
        assert record.result == raw_result

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

    def test_render_mcp_progress_redacts_public_message_text(self):
        renderer = make_renderer()

        line = renderer._render_mcp_progress(
            MCPProgressEvent(
                server_name="live",
                tool_name="echo",
                progress=1,
                total=2,
                message="api_key=sk-live-secret /Users/alice/.iac-code/settings.yml",
                tool_use_id="tool-1",
            )
        )

        assert "MCP live:echo: 1/2" in line.plain
        assert "sk-live-secret" not in line.plain
        assert "/Users/alice" not in line.plain
        assert "api_key=[REDACTED]" in line.plain

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

    def test_render_tool_header_localizes_pipeline_ros_template_tool_name_without_registry_tool(self):
        renderer = make_renderer()
        record = _ToolCallRecord(tool_name="ros_estimate_template_cost", tool_input={}, done=True)

        header = renderer._render_tool_header(record)

        assert "ROS Estimate Cost" in header.plain
        assert "ros_estimate_template_cost" not in header.plain

    def test_render_tool_header_uses_zh_translation_for_ros_parameter_recommendation_tool(self, monkeypatch):
        from iac_code.i18n import setup_i18n

        monkeypatch.setenv("LANGUAGE", "zh")
        setup_i18n()
        try:
            renderer = make_renderer()
            record = _ToolCallRecord(
                tool_name="ros_get_template_parameter_constraints",
                tool_input={},
                done=True,
            )

            header = renderer._render_tool_header(record)

            assert header.plain == "● ROS 模板参数推荐"
        finally:
            monkeypatch.setenv("LANGUAGE", "en")
            setup_i18n()

    def test_render_tool_result_summarizes_pipeline_ros_template_tool_without_registry_tool(self):
        renderer = make_renderer()
        record = _ToolCallRecord(
            tool_name="ros_estimate_template_cost",
            tool_input={},
            done=True,
            result='{\n  "RequestId": "REQ-42",\n  "Resources": ["long output"]\n}',
        )

        line = renderer._render_tool_result(record)

        assert line is not None
        assert "Call succeeded (RequestId: REQ-42)" in line.plain
        assert "Resources" not in line.plain

    def test_render_tool_result_summarizes_pipeline_ros_deploy_without_registry_tool(self):
        renderer = make_renderer()
        record = _ToolCallRecord(
            tool_name="ros_deploy",
            tool_input={},
            done=True,
            is_error=True,
            result=json.dumps(
                {
                    "stack_id": "a463b158-5429-4a2d-9173-825271c28dcb",
                    "stack_name": "single-vswitch-20260706-k7m3x9",
                    "status": "CREATE_FAILED",
                    "status_reason": (
                        "Resource CREATE failed: VPCResourceException: resources.VSwitch: "
                        "code: InvalidCidrBlock.Overlapped, message: The CIDR block 192.168.200.0/24 "
                        "Overlapped exists CIDR block."
                    ),
                    "is_success": False,
                },
                indent=2,
            ),
        )

        line = renderer._render_tool_result(record)

        assert line is not None
        assert "single-vswitch-20260706-k7m3x9 creation failed: CIDR block overlapped (a463b158)" in line.plain
        assert "status_reason" not in line.plain

    def test_render_tool_header_localizes_infraguard_scan_tool_name(self):
        renderer = make_renderer()
        record = _ToolCallRecord(tool_name="infraguard_scan", tool_input={}, done=True)

        header = renderer._render_tool_header(record)

        assert "InfraGuard scan" in header.plain
        assert "infraguard_scan" not in header.plain

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
    async def test_prompt_permission_allow_once(self, monkeypatch, tmp_path):
        monkeypatch.setenv("IAC_CODE_CONFIG_DIR", str(tmp_path))
        renderer = make_renderer()
        event = PermissionRequestEvent(
            tool_name="demo",
            tool_input={"path": "a.txt"},
            tool_use_id="toolu-demo",
            response_future=asyncio.get_running_loop().create_future(),
        )

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
