from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from iac_code.agent.message import Message as AgentMessage
from iac_code.commands import create_default_registry
from iac_code.commands import prompt as prompt_module
from iac_code.pipeline.config import RunMode
from iac_code.pipeline.engine.cleanup import create_cleanup_prompt_message
from iac_code.providers.base import ContentBlock, Message, ToolDefinition


class _FakeAgentLoop:
    system_prompt = "# Stale\nold"
    session_id = "session-123"

    def _get_provider_messages(self):
        return [
            Message.user("hello from user"),
            Message(
                role="assistant",
                content=[
                    ContentBlock(type="text", text="assistant text"),
                    ContentBlock(type="tool_use", tool_use_id="toolu_1", name="read_file", input={"path": "a.py"}),
                ],
            ),
        ]

    def _get_tool_definitions(self):
        return [
            ToolDefinition(
                name="read_file",
                description="Read a file",
                input_schema={"type": "object", "properties": {"path": {"type": "string"}}},
            )
        ]


class _FakeAgentLoopWithLastRequest(_FakeAgentLoop):
    def _get_provider_messages(self):
        return [Message.user("current runtime message")]

    def get_last_provider_request_snapshot(self):
        return {
            "system_prompt": "# Sent System\nactual sent system",
            "provider_messages": [
                Message.user("actual user question"),
                Message.user(
                    "<system-reminder>\n"
                    "Relevant persistent memories recalled for this conversation:\n\n"
                    "# Recalled Memory\n"
                    "Prefer ROS YAML.\n"
                    "</system-reminder>"
                ),
            ],
            "tools": self._get_tool_definitions(),
        }


def test_default_registry_hides_prompt_command():
    registry = create_default_registry()

    command = registry.get("prompt")

    assert command is not None
    assert command.hidden is True
    assert "prompt" not in {cmd.name for cmd in registry.get_all()}
    assert "prompt" not in registry.get_completions("p")


def test_prompt_html_uses_tabs_without_memory_tab():
    html = prompt_module.render_prompt_html(
        {
            "metadata": {"session_id": "abc"},
            "system_prompt": "# Memory\nProject memory index",
            "system_sections": [{"title": "Memory", "content": "# Memory\nProject memory index", "zone": "dynamic"}],
            "provider_messages": [{"role": "user", "content": "hello"}],
            "tools": [{"name": "read_file", "description": "Read", "input_schema": {"type": "object"}}],
        }
    )

    assert 'role="tablist"' in html
    assert 'data-tab-target="all"' in html
    assert 'data-tab-target="system"' in html
    assert 'data-tab-target="messages"' in html
    assert 'data-tab-target="tools"' in html
    assert 'data-tab-target="memory"' not in html
    assert "<h2>Memory</h2>" not in html
    assert "Prompt Assembly Order" in html
    assert "1. System Prompt" in html
    assert "2. Provider Messages" in html
    assert "3. Tools" in html
    assert "Project memory index" in html


def test_prompt_snapshot_prefers_last_provider_request_with_recalled_memory():
    repl = SimpleNamespace(
        _agent_loop=_FakeAgentLoopWithLastRequest(),
        get_status_snapshot=lambda: {"session_id": "session-123"},
    )

    snapshot = prompt_module.build_prompt_snapshot(repl)
    html = prompt_module.render_prompt_html(snapshot)

    assert snapshot["metadata"]["source"] == "Last main-model request"
    assert "actual sent system" in html
    assert "actual user question" in html
    assert "Relevant persistent memories recalled for this conversation" in html
    assert "Prefer ROS YAML." in html
    assert "current runtime message" not in html
    assert "recalled memory" in html
    assert "provider-only" not in html
    assert "hidden conversation" in html


def test_prompt_snapshot_includes_hidden_cleanup_prompt_from_session():
    cleanup = create_cleanup_prompt_message(
        "检测到 pipeline rollback 后仍需要清理的云资源。\n待清理资源：stack-123",
        cleanup_status="pending",
    )
    repl = SimpleNamespace(
        _agent_loop=_FakeAgentLoopWithLastRequest(),
        _session_storage=SimpleNamespace(load=lambda cwd, session_id: [cleanup]),
        _original_cwd="/repo",
        _session_id="session-123",
        get_status_snapshot=lambda: {"session_id": "session-123", "cwd": "/repo"},
    )

    snapshot = prompt_module.build_prompt_snapshot(repl)
    html = prompt_module.render_prompt_html(snapshot)

    assert "待清理资源：stack-123" in html
    assert "cleanup prompt" in html
    assert 'data-tab-target="cleanup"' in html
    assert 'data-tab-panel="cleanup"' in html
    assert "Cleanup Prompts" in html


def test_prompt_snapshot_inserts_removed_cleanup_prompt_between_session_anchors():
    class _AnchoredLastRequestLoop(_FakeAgentLoop):
        def get_last_provider_request_snapshot(self):
            return {
                "system_prompt": "# Sent System\nactual sent system",
                "provider_messages": [
                    Message.user("before cleanup"),
                    Message(role="assistant", content="after cleanup"),
                ],
                "tools": [],
            }

    cleanup = create_cleanup_prompt_message(
        "检测到 pipeline rollback 后仍需要清理的云资源。\n待清理资源：stack-anchored",
        cleanup_status="completed",
    )
    repl = SimpleNamespace(
        _agent_loop=_AnchoredLastRequestLoop(),
        _session_storage=SimpleNamespace(
            load=lambda cwd, session_id: [
                AgentMessage(role="user", content="before cleanup"),
                cleanup,
                AgentMessage(role="assistant", content="after cleanup"),
            ]
        ),
        _original_cwd="/repo",
        _session_id="session-123",
        get_status_snapshot=lambda: {"session_id": "session-123", "cwd": "/repo"},
    )

    snapshot = prompt_module.build_prompt_snapshot(repl)
    messages = snapshot["provider_messages"]

    assert [message["content"] for message in messages] == [
        "before cleanup",
        "检测到 pipeline rollback 后仍需要清理的云资源。\n待清理资源：stack-anchored",
        "after cleanup",
    ]
    assert messages[1]["badge"] == "cleanup prompt · removed"
    assert snapshot["cleanup_prompts"][0]["content"].endswith("stack-anchored")


def test_prompt_snapshot_keeps_unanchored_removed_cleanup_prompt_out_of_provider_messages():
    class _UnanchoredLastRequestLoop(_FakeAgentLoop):
        def get_last_provider_request_snapshot(self):
            return {
                "system_prompt": "# Sent System\nactual sent system",
                "provider_messages": [Message(role="assistant", content="after cleanup")],
                "tools": [],
            }

    cleanup = create_cleanup_prompt_message(
        "检测到 pipeline rollback 后仍需要清理的云资源。\n待清理资源：stack-unanchored",
        cleanup_status="completed",
    )
    repl = SimpleNamespace(
        _agent_loop=_UnanchoredLastRequestLoop(),
        _session_storage=SimpleNamespace(
            load=lambda cwd, session_id: [
                cleanup,
                AgentMessage(role="assistant", content="after cleanup"),
            ]
        ),
        _original_cwd="/repo",
        _session_id="session-123",
        get_status_snapshot=lambda: {"session_id": "session-123", "cwd": "/repo"},
    )

    snapshot = prompt_module.build_prompt_snapshot(repl)
    html = prompt_module.render_prompt_html(snapshot)

    assert [message["content"] for message in snapshot["provider_messages"]] == ["after cleanup"]
    assert snapshot["cleanup_prompts"][0]["content"].endswith("stack-unanchored")
    assert "待清理资源：stack-unanchored" in html
    assert 'data-tab-target="cleanup"' in html


def test_prompt_html_hides_cleanup_tab_when_no_cleanup_prompt():
    html = prompt_module.render_prompt_html(
        {
            "metadata": {"session_id": "abc"},
            "system_prompt": "# System\nPrompt",
            "system_sections": [{"title": "System", "content": "# System\nPrompt", "zone": "static"}],
            "provider_messages": [{"role": "user", "content": "hello"}],
            "tools": [],
        }
    )

    assert 'data-tab-target="cleanup"' not in html
    assert 'data-tab-panel="cleanup"' not in html


@pytest.mark.asyncio
async def test_prompt_command_exports_html_and_opens(tmp_path, monkeypatch):
    opened: list[object] = []
    monkeypatch.setattr(prompt_module, "_open_path", lambda path: opened.append(path))

    repl = SimpleNamespace(
        _agent_loop=_FakeAgentLoop(),
        _memory_context=SimpleNamespace(
            instruction_memory_content="Project instruction memory",
            memory_index_content="ros-yaml.md - ROS YAML preference",
            memory_mechanics_content="Use read_memory for full topic files.",
        ),
        _build_current_system_prompt=lambda: (
            "Identity preamble\n\n"
            "# System Rules\n"
            "Follow the rules.\n\n"
            "--- DYNAMIC_BOUNDARY ---\n\n"
            "# Environment\n"
            "- Working directory: `/tmp/project`"
        ),
        get_status_snapshot=lambda: {
            "session_id": "session-123",
            "provider": "DashScope",
            "model": "qwen3.7-max",
            "cwd": "/tmp/project",
        },
    )
    context = SimpleNamespace(repl=repl)

    result = await prompt_module.prompt_command(context=context, output_dir=tmp_path)

    assert result is not None
    assert "Prompt exported and opened" in result
    assert len(opened) == 1
    html_path = opened[0]
    assert html_path.parent == tmp_path
    assert html_path.suffix == ".html"

    html = html_path.read_text(encoding="utf-8")
    assert "Prompt Snapshot" in html
    assert "System Prompt" in html
    assert "System Rules" in html
    assert "Provider Messages" in html
    assert "hello from user" in html
    assert "assistant text" in html
    assert "Tools" in html
    assert "read_file" in html
    assert "qwen3.7-max" in html


@pytest.mark.asyncio
async def test_prompt_command_requires_repl_context():
    result = await prompt_module.prompt_command(context=MagicMock(repl=None))

    assert "REPL context" in result


@pytest.mark.asyncio
async def test_prompt_command_exports_pipeline_prompt_context(tmp_path):
    ensure = AsyncMock(return_value=True)
    prompt_context = SimpleNamespace(
        scope="parent",
        step_id="architecture_planning",
        system_prompt="real system prompt",
        messages=[AgentMessage(role="user", content="original prompt")],
        agent_loop_session_id="transcript_att_0001",
        initial_prompt="fallback initial prompt",
        candidate_index=None,
        candidate_name="",
        sub_pipeline_id="",
    )
    pipeline = SimpleNamespace(get_prompt_contexts=lambda: [prompt_context])
    session_dir = tmp_path / "root-session"
    repl = SimpleNamespace(
        ensure_pipeline_restored_for_prompt=ensure,
        _pipeline=pipeline,
        _session_storage=SimpleNamespace(session_dir=lambda cwd, session_id: session_dir),
        _original_cwd="/repo",
        _session_id="root-session",
        get_status_snapshot=lambda: {"session_id": "root-session", "cwd": "/repo"},
    )
    opened_urls: list[str] = []

    result = await prompt_module.prompt_command(
        context=SimpleNamespace(repl=repl),
        browser_opener=lambda url: opened_urls.append(url) or True,
    )

    ensure.assert_awaited_once()
    assert "real system prompt" not in result
    assert "original prompt" not in result

    html_path = session_dir / "prompt.html"
    assert opened_urls == [html_path.resolve().as_uri()]
    html = html_path.read_text(encoding="utf-8")
    assert "Step architecture_planning" in html
    assert "transcript_att_0001" in html
    assert "real system prompt" in html
    assert "[user]" in html
    assert "original prompt" in html


@pytest.mark.asyncio
async def test_prompt_command_uses_normal_snapshot_with_cleanup_prompt_after_pipeline_handoff(tmp_path):
    ensure = AsyncMock(return_value=False)
    cleanup = create_cleanup_prompt_message(
        "检测到 pipeline rollback 后仍需要清理的云资源。\n待清理资源：stack-cleanup",
        cleanup_status="completed",
    )
    prompt_context = SimpleNamespace(
        scope="parent",
        step_id="deploying",
        system_prompt="stale pipeline prompt",
        messages=[AgentMessage(role="user", content="stale pipeline message")],
        agent_loop_session_id="transcript_deploying",
        initial_prompt="",
        candidate_index=None,
        candidate_name="",
        sub_pipeline_id="",
    )
    session_dir = tmp_path / "root-session"
    repl = SimpleNamespace(
        ensure_pipeline_restored_for_prompt=ensure,
        _get_runtime_mode=lambda: RunMode.NORMAL,
        _pipeline=SimpleNamespace(get_prompt_contexts=lambda: [prompt_context]),
        _agent_loop=_FakeAgentLoopWithLastRequest(),
        _session_storage=SimpleNamespace(
            load=lambda cwd, session_id: [cleanup],
            session_dir=lambda cwd, session_id: session_dir,
        ),
        _original_cwd="/repo",
        _session_id="root-session",
        get_status_snapshot=lambda: {"session_id": "root-session", "cwd": "/repo"},
    )
    opened_urls: list[str] = []

    await prompt_module.prompt_command(
        context=SimpleNamespace(repl=repl),
        browser_opener=lambda url: opened_urls.append(url) or True,
    )

    html = (session_dir / "prompt.html").read_text(encoding="utf-8")
    assert "待清理资源：stack-cleanup" in html
    assert "cleanup prompt" in html
    assert "stale pipeline prompt" not in html
    assert "stale pipeline message" not in html


@pytest.mark.asyncio
async def test_prompt_command_escapes_pipeline_prompt_html(tmp_path):
    ensure = AsyncMock(return_value=True)
    prompt_context = SimpleNamespace(
        scope="parent",
        step_id="reviewing",
        system_prompt="<script>alert('system')</script>",
        messages=[AgentMessage(role="user", content="<script>alert('message')</script>")],
        agent_loop_session_id="transcript_review",
        initial_prompt="",
        candidate_index=None,
        candidate_name="",
        sub_pipeline_id="",
    )
    session_dir = tmp_path / "root-session"
    repl = SimpleNamespace(
        ensure_pipeline_restored_for_prompt=ensure,
        _pipeline=SimpleNamespace(get_prompt_contexts=lambda: [prompt_context]),
        _session_storage=SimpleNamespace(session_dir=lambda cwd, session_id: session_dir),
        _original_cwd="/repo",
        _session_id="root-session",
        get_status_snapshot=lambda: {"session_id": "root-session", "cwd": "/repo"},
    )

    await prompt_module.prompt_command(
        context=SimpleNamespace(repl=repl),
        browser_opener=lambda url: True,
    )

    html = (session_dir / "prompt.html").read_text(encoding="utf-8")
    assert "<script>alert('system')</script>" not in html
    assert "&lt;script&gt;alert(&#x27;system&#x27;)&lt;/script&gt;" in html
