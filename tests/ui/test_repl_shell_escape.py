"""Tests for REPL shell escape handling."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from iac_code.services.permissions.audit import fingerprint_text
from iac_code.tools.base import Tool, ToolResult
from iac_code.tools.bash.bash_tool import BashTool
from iac_code.types.permissions import (
    PermissionAuditMetadata,
    PermissionAuditSettings,
    PermissionResult,
    ToolPermissionContext,
)
from iac_code.ui.core.input_history import InputHistory
from iac_code.ui.repl import InlineREPL


class FakeRenderer:
    def __init__(self, permission_allowed: bool = True) -> None:
        self.messages: list[tuple[str, str]] = []
        self.recorded_turns: list[str] = []
        self.user_messages: list[str] = []
        self.command_results: list[tuple[str, str]] = []
        self.permission_allowed = permission_allowed
        self.permission_events = []

    def print_system_message(self, text: str, style: str = "yellow") -> None:
        self.messages.append((text, style))

    def record_user_turn(self, text: str) -> None:
        self.recorded_turns.append(text)

    def print_user_message(self, text: str) -> None:
        self.user_messages.append(text)

    def print_command_result(self, command: str, result: str) -> None:
        self.command_results.append((command, result))

    async def prompt_permission(self, event) -> bool:
        self.permission_events.append(event)
        return self.permission_allowed


class RecordingHistory:
    def __init__(self) -> None:
        self.appended: list[str] = []

    def append(self, entry: str) -> None:
        self.appended.append(entry)

    def reset_navigation(self) -> None:
        pass


class RecordingContextManager:
    def __init__(self) -> None:
        self.user_messages: list[str] = []
        self.assistant_messages: list[str] = []

    def get_messages(self) -> list:
        return []

    def add_user_message(self, message: str) -> None:
        self.user_messages.append(message)

    def add_assistant_message(self, message: str) -> None:
        self.assistant_messages.append(message)


class FakeBashTool(Tool):
    def __init__(self, result: ToolResult, permission: PermissionResult | None = None) -> None:
        self.result = result
        self.permission = permission or PermissionResult(behavior="allow")
        self.calls: list[tuple[dict, str]] = []

    @property
    def name(self) -> str:
        return "bash"

    @property
    def description(self) -> str:
        return "Fake bash"

    @property
    def input_schema(self) -> dict:
        return {
            "type": "object",
            "properties": {"command": {"type": "string"}},
            "required": ["command"],
        }

    async def check_permissions(self, input: dict, context=None) -> PermissionResult:
        return self.permission

    async def execute(self, *, tool_input: dict, context) -> ToolResult:
        self.calls.append((tool_input, context.cwd))
        return self.result


def make_repl(
    tool: FakeBashTool | None,
    cwd: str,
    *,
    permission_context: ToolPermissionContext | None = None,
    permission_allowed: bool = True,
) -> InlineREPL:
    repl = InlineREPL.__new__(InlineREPL)
    repl._original_cwd = cwd
    repl.renderer = FakeRenderer(permission_allowed=permission_allowed)
    repl._history = RecordingHistory()
    repl._command_log = []
    repl._streaming_error_log = []
    repl._agent_loop = SimpleNamespace(context_manager=RecordingContextManager())
    repl.store = SimpleNamespace(get_state=lambda: SimpleNamespace(permission_context=permission_context))
    repl.tool_registry = SimpleNamespace(get=lambda name: tool if name == "bash" else None)
    return repl


def test_shell_escape_permission_context_supports_trusted_read_directories(tmp_path):
    trusted = str(tmp_path / "config" / "tool-results" / "session-1")
    permission_context = ToolPermissionContext(cwd=str(tmp_path), trusted_read_directories=[trusted])
    tool = FakeBashTool(ToolResult.success("unused"))

    repl = make_repl(tool, str(tmp_path), permission_context=permission_context)
    permission_context = repl.store.get_state().permission_context

    assert permission_context.trusted_read_directories == [trusted]


@pytest.mark.asyncio
async def test_shell_escape_executes_registered_bash_tool(tmp_path):
    tool = FakeBashTool(ToolResult.success("STDOUT:\nhello\nExit code: 0"))
    repl = make_repl(tool, str(tmp_path))

    await repl._handle_shell_escape("!echo hello")

    assert tool.calls == [({"command": "echo hello"}, str(tmp_path))]
    assert ("$ echo hello", "dim") in repl.renderer.messages
    assert ("STDOUT:\nhello\nExit code: 0", "white") in repl.renderer.messages
    assert repl.renderer.recorded_turns == []
    assert repl._history.appended == []
    assert repl._command_log == [("!echo hello", "$ echo hello\nSTDOUT:\nhello\nExit code: 0", 0, False)]
    assert repl._agent_loop.context_manager.user_messages == []
    assert repl._agent_loop.context_manager.assistant_messages == []


@pytest.mark.asyncio
async def test_shell_escape_uses_tool_executor_not_direct_tool_execute(tmp_path, monkeypatch):
    tool = FakeBashTool(ToolResult.error("direct execution should not run"))
    repl = make_repl(tool, str(tmp_path))
    captured = {}

    async def fake_execute_batch(self, calls, context):
        captured["calls"] = calls
        captured["cwd"] = context.cwd
        return [ToolResult.success("executor output")]

    monkeypatch.setattr("iac_code.tools.tool_executor.ToolExecutor.execute_batch", fake_execute_batch)

    await repl._handle_shell_escape("!echo via executor")

    assert tool.calls == []
    assert captured["cwd"] == str(tmp_path)
    call = captured["calls"][0]
    assert call.id == "shell-escape"
    assert call.name == "bash"
    assert call.input == {"command": "echo via executor"}
    assert ("executor output", "white") in repl.renderer.messages


@pytest.mark.asyncio
async def test_shell_escape_empty_command_prints_usage_without_execution(tmp_path):
    tool = FakeBashTool(ToolResult.success("unused"))
    repl = make_repl(tool, str(tmp_path))

    await repl._handle_shell_escape("!   ")

    assert tool.calls == []
    assert repl.renderer.messages == [("Usage: !<shell command>", "yellow")]


@pytest.mark.asyncio
async def test_shell_escape_error_result_prints_red_output(tmp_path):
    tool = FakeBashTool(ToolResult.error("STDERR:\nnot found\nExit code: 127"))
    repl = make_repl(tool, str(tmp_path))

    await repl._handle_shell_escape("!missing-command")

    assert tool.calls == [({"command": "missing-command"}, str(tmp_path))]
    assert ("STDERR:\nnot found\nExit code: 127", "red") in repl.renderer.messages
    assert repl._command_log == [("!missing-command", "$ missing-command\nSTDERR:\nnot found\nExit code: 127", 0, True)]


@pytest.mark.asyncio
async def test_shell_escape_missing_bash_tool_prints_error(tmp_path):
    repl = make_repl(None, str(tmp_path))

    await repl._handle_shell_escape("!echo hello")

    assert repl.renderer.messages == [("Shell command support is unavailable.", "red")]


@pytest.mark.asyncio
async def test_shell_escape_permission_deny_does_not_execute(tmp_path):
    tool = FakeBashTool(ToolResult.success("unused"), PermissionResult(behavior="deny", message="blocked"))
    repl = make_repl(tool, str(tmp_path), permission_context=ToolPermissionContext(cwd=str(tmp_path)))

    await repl._handle_shell_escape("!mkdir blocked")

    assert tool.calls == []
    assert repl.renderer.messages == [("blocked", "red")]


@pytest.mark.asyncio
async def test_shell_escape_permission_allow_is_audited_without_raw_command(tmp_path, monkeypatch):
    audit_records = []
    tool = FakeBashTool(
        ToolResult.success("STDOUT:\nok\nExit code: 0"),
        PermissionResult(
            behavior="allow",
            audit=PermissionAuditMetadata(
                scope="settings_rule",
                source="permission_pipeline",
                rule_source="project_settings",
                reason_type="rule",
                is_read_only=False,
            ),
        ),
    )
    repl = make_repl(tool, str(tmp_path), permission_context=ToolPermissionContext(cwd=str(tmp_path)))
    monkeypatch.setattr(
        "iac_code.ui.repl.emit_permission_audit",
        lambda record, settings=None: audit_records.append(record),
        raising=False,
    )

    await repl._handle_shell_escape("!echo secret-value")

    assert tool.calls == [({"command": "echo secret-value"}, str(tmp_path))]
    assert len(audit_records) == 1
    record = audit_records[0]
    assert record.source == "permission_pipeline"
    assert record.scope == "settings_rule"
    assert record.decision == "allow"
    assert record.operation == {"is_read_only": False}
    assert "echo secret-value" not in str(record.input_summary)


@pytest.mark.asyncio
async def test_shell_escape_permission_allow_denies_when_audit_log_write_fails(tmp_path, monkeypatch):
    tool = FakeBashTool(
        ToolResult.success("unused"),
        PermissionResult(
            behavior="allow",
            audit=PermissionAuditMetadata(
                scope="settings_rule",
                source="permission_pipeline",
                rule_source="project_settings",
                reason_type="rule",
                is_read_only=False,
            ),
        ),
    )
    repl = make_repl(tool, str(tmp_path), permission_context=ToolPermissionContext(cwd=str(tmp_path)))
    monkeypatch.setattr("iac_code.ui.repl.emit_permission_audit", lambda record, settings=None: False, raising=False)

    await repl._handle_shell_escape("!mkdir blocked")

    assert tool.calls == []
    assert repl.renderer.messages == [("Permission denied.", "red")]


@pytest.mark.asyncio
async def test_shell_escape_permission_denial_is_audited(tmp_path, monkeypatch):
    audit_records = []
    tool = FakeBashTool(
        ToolResult.success("unused"),
        PermissionResult(
            behavior="deny",
            message="blocked",
            audit=PermissionAuditMetadata(
                scope="settings_rule",
                source="permission_pipeline",
                rule_source="project_settings",
                reason_type="rule",
                is_read_only=False,
            ),
        ),
    )
    repl = make_repl(tool, str(tmp_path), permission_context=ToolPermissionContext(cwd=str(tmp_path)))
    monkeypatch.setattr(
        "iac_code.ui.repl.emit_permission_audit",
        lambda record, settings=None: audit_records.append(record),
        raising=False,
    )

    await repl._handle_shell_escape("!mkdir blocked")

    assert tool.calls == []
    assert repl.renderer.messages == [("blocked", "red")]
    assert len(audit_records) == 1
    record = audit_records[0]
    assert record.source == "permission_pipeline"
    assert record.scope == "settings_rule"
    assert record.decision == "deny"
    assert record.tool_name == "bash"


@pytest.mark.asyncio
async def test_shell_escape_read_only_permission_denial_is_audited(tmp_path, monkeypatch):
    audit_records = []
    tool = FakeBashTool(
        ToolResult.success("unused"),
        PermissionResult(
            behavior="deny",
            message="blocked",
            audit=PermissionAuditMetadata(
                scope="read_only",
                source="permission_pipeline",
                reason_type="read_only",
                is_read_only=True,
            ),
        ),
    )
    repl = make_repl(tool, str(tmp_path), permission_context=ToolPermissionContext(cwd=str(tmp_path)))
    monkeypatch.setattr(
        "iac_code.ui.repl.emit_permission_audit",
        lambda record, settings=None: audit_records.append(record),
        raising=False,
    )

    await repl._handle_shell_escape("!cat blocked")

    assert tool.calls == []
    assert repl.renderer.messages == [("blocked", "red")]
    assert len(audit_records) == 1
    assert audit_records[0].decision == "deny"
    assert audit_records[0].scope == "read_only"


@pytest.mark.asyncio
async def test_shell_escape_bash_internal_allow_rule_emits_audit(tmp_path, monkeypatch):
    audit_records = []
    settings_seen = []
    tool = BashTool()
    permission_context = ToolPermissionContext(
        cwd=str(tmp_path),
        allow_rules={"session": ["bash(mkdir:*)"]},
        audit_settings=PermissionAuditSettings(include_tool_input=True),
    )
    repl = make_repl(tool, str(tmp_path), permission_context=permission_context)

    async def fake_execute_batch(self, calls, context):
        return [ToolResult.success("Exit code: 0")]

    monkeypatch.setattr("iac_code.tools.tool_executor.ToolExecutor.execute_batch", fake_execute_batch)
    monkeypatch.setattr(
        "iac_code.ui.repl.emit_permission_audit",
        lambda record, settings=None: (audit_records.append(record), settings_seen.append(settings)),
        raising=False,
    )

    await repl._handle_shell_escape("!mkdir foo")

    assert len(audit_records) == 1
    record = audit_records[0]
    assert record.decision == "allow"
    assert record.scope == "session_rule"
    assert record.source == "permission_pipeline"
    assert record.rule_source == "session"
    assert record.rule == "bash(mkdir:*)"
    assert record.operation == {"is_read_only": False}
    assert record.tool_input_redacted == {
        "command": {"type": "str", "length": 9, "fingerprint": fingerprint_text("mkdir foo")}
    }
    assert settings_seen == [permission_context.audit_settings]


@pytest.mark.asyncio
async def test_shell_escape_bash_internal_deny_rule_emits_audit(tmp_path, monkeypatch):
    audit_records = []
    settings_seen = []
    tool = BashTool()
    permission_context = ToolPermissionContext(
        cwd=str(tmp_path),
        deny_rules={"session": ["bash(mkdir:*)"]},
        audit_settings=PermissionAuditSettings(include_tool_input=True),
    )
    repl = make_repl(tool, str(tmp_path), permission_context=permission_context)
    monkeypatch.setattr(
        "iac_code.ui.repl.emit_permission_audit",
        lambda record, settings=None: (audit_records.append(record), settings_seen.append(settings)),
        raising=False,
    )

    await repl._handle_shell_escape("!mkdir blocked")

    assert len(audit_records) == 1
    record = audit_records[0]
    assert record.decision == "deny"
    assert record.scope == "session_rule"
    assert record.source == "permission_pipeline"
    assert record.rule_source == "session"
    assert record.rule == "bash(mkdir:*)"
    assert record.operation == {"is_read_only": False}
    assert record.tool_input_redacted == {
        "command": {"type": "str", "length": 13, "fingerprint": fingerprint_text("mkdir blocked")}
    }
    assert settings_seen == [permission_context.audit_settings]


@pytest.mark.asyncio
async def test_shell_escape_permission_prompt_rejection_does_not_execute(tmp_path):
    settings = PermissionAuditSettings(include_tool_input=True)
    metadata = PermissionAuditMetadata(
        scope="once",
        source="permission_pipeline",
        reason_type="untrusted_write",
        is_read_only=False,
    )
    tool = FakeBashTool(
        ToolResult.success("unused"),
        PermissionResult(behavior="ask", message="confirm", audit=metadata),
    )
    repl = make_repl(
        tool,
        str(tmp_path),
        permission_context=ToolPermissionContext(cwd=str(tmp_path), audit_settings=settings),
        permission_allowed=False,
    )

    await repl._handle_shell_escape("!mkdir maybe")

    assert tool.calls == []
    assert repl.renderer.messages == [("Permission denied.", "red")]
    assert [event.tool_input for event in repl.renderer.permission_events] == [{"command": "mkdir maybe"}]
    assert repl.renderer.permission_events[0].audit_context == {
        "session_id": "",
        "settings": settings,
        "metadata": metadata,
    }


@pytest.mark.asyncio
async def test_interactive_shell_escape_resets_history_navigation_without_appending(tmp_path):
    history = InputHistory(str(tmp_path / "history"))
    history.append("previous prompt")
    assert history.navigate(-1, current_input="draft") == "previous prompt"
    assert history.is_navigating is True

    repl = InlineREPL.__new__(InlineREPL)
    repl._history = history
    handled: list[str] = []

    async def handle_shell_escape(user_input: str) -> None:
        handled.append(user_input)

    repl._handle_shell_escape = handle_shell_escape

    await repl._handle_interactive_shell_escape("!echo hello")

    assert handled == ["!echo hello"]
    assert history.is_navigating is False
    assert history.search("") == ["previous prompt"]


def test_refresh_banner_replays_shell_escape_command(tmp_path):
    from iac_code.state.app_state import AppState

    repl = InlineREPL.__new__(InlineREPL)
    repl._session_id = "session-1"
    repl._session_name = "deploy-prod"
    repl.store = SimpleNamespace(get_state=lambda: AppState(model="test-model", cwd=str(tmp_path)))
    repl.console = SimpleNamespace(
        file=SimpleNamespace(write=lambda _text: None, flush=lambda: None),
        print=lambda *_: None,
    )
    repl.renderer = FakeRenderer()
    repl._agent_loop = SimpleNamespace(context_manager=SimpleNamespace(get_messages=lambda: []))
    repl._streaming_error_log = []
    repl._command_log = [("!echo hello", "$ echo hello\nhello", 0, False)]

    repl._refresh_banner()

    assert repl.renderer.user_messages == ["!echo hello"]
    assert repl.renderer.command_results == [("!echo hello", "$ echo hello\nhello")]
