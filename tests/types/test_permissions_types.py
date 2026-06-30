"""Tests for extended permission types."""

from __future__ import annotations

from iac_code.types.permissions import (
    PermissionDecisionReason,
    PermissionMode,
    PermissionResult,
    PermissionRule,
    PermissionRuleSource,
    PermissionRuleValue,
    ToolPermissionContext,
)


class TestPermissionModeExtended:
    def test_all_four_modes_exist(self) -> None:
        names = {m.name for m in PermissionMode}
        assert names == {
            "DEFAULT",
            "ACCEPT_EDITS",
            "BYPASS_PERMISSIONS",
            "DONT_ASK",
        }


class TestPermissionRuleSource:
    def test_all_five_sources_exist(self) -> None:
        names = {s.name for s in PermissionRuleSource}
        assert names == {
            "USER_SETTINGS",
            "PROJECT_SETTINGS",
            "LOCAL_SETTINGS",
            "CLI_ARG",
            "SESSION",
        }


class TestPermissionRuleValue:
    def test_creation(self) -> None:
        value = PermissionRuleValue(tool_name="bash", rule_content="git *")
        assert value.tool_name == "bash"
        assert value.rule_content == "git *"

    def test_empty_content(self) -> None:
        value = PermissionRuleValue(tool_name="read_file", rule_content="")
        assert value.rule_content == ""


class TestPermissionRule:
    def test_creation(self) -> None:
        value = PermissionRuleValue(tool_name="bash", rule_content="git *")
        rule = PermissionRule(
            source=PermissionRuleSource.USER_SETTINGS,
            behavior="allow",
            value=value,
        )
        assert rule.source is PermissionRuleSource.USER_SETTINGS
        assert rule.behavior == "allow"
        assert rule.value is value


class TestPermissionResultExtended:
    def test_basic_creation(self) -> None:
        result = PermissionResult(behavior="allow")
        assert result.behavior == "allow"
        assert result.message == ""
        assert result.reason is None
        assert result.suggestions is None

    def test_with_reason(self) -> None:
        reason = PermissionDecisionReason(type="readonly", detail="ls is read-only")
        result = PermissionResult(behavior="allow", reason=reason)
        assert result.reason is reason
        assert result.reason.type == "readonly"

    def test_with_suggestions(self) -> None:
        suggestions = [
            PermissionRuleValue(tool_name="bash", rule_content="git *"),
        ]
        result = PermissionResult(behavior="ask", suggestions=suggestions)
        assert len(result.suggestions) == 1

    def test_passthrough_behavior(self) -> None:
        result = PermissionResult(behavior="passthrough")
        assert result.behavior == "passthrough"


def test_permission_audit_metadata_defaults_to_safe_values() -> None:
    from iac_code.types.permissions import PermissionAuditMetadata, PermissionResult

    metadata = PermissionAuditMetadata(scope="settings_rule", source="permission_pipeline")
    result = PermissionResult(behavior="allow", audit=metadata)

    assert result.audit is metadata
    assert result.audit.scope == "settings_rule"
    assert result.audit.source == "permission_pipeline"
    assert result.audit.rule_source is None
    assert result.audit.is_read_only is None


class TestPermissionDecisionReason:
    def test_creation(self) -> None:
        reason = PermissionDecisionReason(type="rule", detail="matched bash(git *)")
        assert reason.type == "rule"
        assert reason.detail == "matched bash(git *)"


class TestToolPermissionContext:
    def test_minimal_creation(self) -> None:
        ctx = ToolPermissionContext(cwd="/tmp")
        assert ctx.mode == PermissionMode.DEFAULT
        assert ctx.cwd == "/tmp"
        assert ctx.allow_rules == {}
        assert ctx.deny_rules == {}
        assert ctx.ask_rules == {}
        assert ctx.additional_directories == []

    def test_trusted_read_directories_default_empty(self):
        ctx = ToolPermissionContext(cwd="/tmp")
        assert ctx.trusted_read_directories == []

    def test_with_rules(self) -> None:
        ctx = ToolPermissionContext(
            mode=PermissionMode.DEFAULT,
            cwd="/home/user/project",
            allow_rules={"user_settings": ["bash(git *)"]},
            deny_rules={"cli_arg": ["bash(rm -rf /)"]},
            ask_rules={},
            additional_directories=["/shared/libs"],
        )
        assert "bash(git *)" in ctx.allow_rules["user_settings"]
        assert len(ctx.additional_directories) == 1

    def test_all_rules_from_source(self) -> None:
        ctx = ToolPermissionContext(
            cwd="/tmp",
            allow_rules={
                "user_settings": ["bash(git *)"],
                "session": ["bash(npm test)"],
            },
        )
        all_allow: list[str] = []
        for rules in ctx.allow_rules.values():
            all_allow.extend(rules)
        assert len(all_allow) == 2


def test_tool_permission_context_carries_audit_settings() -> None:
    from iac_code.types.permissions import PermissionAuditSettings, ToolPermissionContext

    settings = PermissionAuditSettings(include_tool_input=True, max_file_bytes=4096, max_files=2)
    context = ToolPermissionContext(cwd="/tmp/project", audit_settings=settings)

    assert context.audit_settings.include_tool_input is True
    assert context.audit_settings.max_file_bytes == 4096
    assert context.audit_settings.max_files == 2
