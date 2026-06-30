import pytest

from iac_code.services.permissions.pipeline import check_tool_permission
from iac_code.tools.base import Tool, ToolResult
from iac_code.tools.bash.bash_tool import BashTool
from iac_code.tools.cloud.aliyun.aliyun_api import AliyunApi
from iac_code.tools.edit_file import EditFileTool
from iac_code.tools.glob import GlobTool
from iac_code.tools.grep import GrepTool
from iac_code.tools.list_files import ListFilesTool
from iac_code.tools.read_file import ReadFileTool
from iac_code.tools.web_fetch import WebFetchTool
from iac_code.tools.write_file import WriteFileTool
from iac_code.types.permissions import PermissionMode, ToolPermissionContext


class FakeReadTool(Tool):
    @property
    def name(self):
        return "read_file"

    @property
    def description(self):
        return "Read a file"

    @property
    def input_schema(self):
        return {"type": "object", "properties": {}}

    async def execute(self, *, tool_input, context):
        return ToolResult.success("ok")

    def is_read_only(self, input=None):
        return True


class FakeWriteTool(Tool):
    @property
    def name(self):
        return "write_file"

    @property
    def description(self):
        return "Write a file"

    @property
    def input_schema(self):
        return {"type": "object", "properties": {}}

    async def execute(self, *, tool_input, context):
        return ToolResult.success("ok")


def _ctx(mode=PermissionMode.DEFAULT, deny=None, allow=None, ask=None):
    return ToolPermissionContext(
        mode=mode,
        cwd="/tmp",
        allow_rules=allow or {},
        deny_rules=deny or {},
        ask_rules=ask or {},
    )


class TestPipeline:
    @pytest.mark.asyncio
    async def test_readonly_tool_auto_allowed(self):
        r = await check_tool_permission(FakeReadTool(), {}, _ctx())
        assert r.behavior == "allow"

    @pytest.mark.asyncio
    async def test_read_file_outside_project_asks(self, tmp_path):
        project = tmp_path / "project"
        outside = tmp_path / "outside"
        project.mkdir()
        outside.mkdir()
        target = outside / "notes.txt"
        target.write_text("outside", encoding="utf-8")

        r = await check_tool_permission(
            ReadFileTool(),
            {"path": str(target)},
            ToolPermissionContext(cwd=str(project)),
        )

        assert r.behavior == "ask"

    @pytest.mark.asyncio
    async def test_bare_read_file_allow_does_not_override_path_constraint(self, tmp_path):
        project = tmp_path / "project"
        outside = tmp_path / "outside"
        project.mkdir()
        outside.mkdir()
        target = outside / "notes.txt"
        target.write_text("outside", encoding="utf-8")

        r = await check_tool_permission(
            ReadFileTool(),
            {"path": str(target)},
            ToolPermissionContext(cwd=str(project), allow_rules={"user_settings": ["read_file"]}),
        )

        assert r.behavior == "ask"
        assert r.reason is not None
        assert r.reason.type == "path_constraint"

    @pytest.mark.parametrize(
        ("tool", "tool_input"),
        [
            (ListFilesTool(), {}),
            (GlobTool(), {"pattern": "**/*"}),
            (GrepTool(), {"pattern": "needle"}),
        ],
    )
    @pytest.mark.asyncio
    async def test_read_directory_tools_outside_project_ask(self, tool, tool_input, tmp_path):
        project = tmp_path / "project"
        outside = tmp_path / "outside"
        project.mkdir()
        outside.mkdir()

        r = await check_tool_permission(
            tool,
            {**tool_input, "path": str(outside)},
            ToolPermissionContext(cwd=str(project)),
        )

        assert r.behavior == "ask"
        assert r.reason is not None
        assert r.reason.type == "path_constraint"

    @pytest.mark.asyncio
    async def test_glob_pattern_cannot_escape_search_root(self, tmp_path):
        project = tmp_path / "project"
        outside = tmp_path / "outside"
        project.mkdir()
        outside.mkdir()
        (outside / "secret.txt").write_text("secret", encoding="utf-8")

        r = await check_tool_permission(
            GlobTool(),
            {"pattern": "../outside/*", "path": str(project)},
            ToolPermissionContext(cwd=str(project)),
        )

        assert r.behavior == "ask"
        assert r.reason is not None
        assert r.reason.type == "path_constraint"

    @pytest.mark.asyncio
    async def test_glob_symlink_match_outside_project_asks(self, tmp_path):
        project = tmp_path / "project"
        outside = tmp_path / "outside"
        project.mkdir()
        outside.mkdir()
        (outside / "secret.txt").write_text("secret", encoding="utf-8")
        (project / "link-outside").symlink_to(outside, target_is_directory=True)

        r = await check_tool_permission(
            GlobTool(),
            {"pattern": "link-outside/*", "path": str(project)},
            ToolPermissionContext(cwd=str(project)),
        )

        assert r.behavior == "ask"
        assert r.reason is not None
        assert r.reason.type == "path_constraint"

    @pytest.mark.asyncio
    async def test_glob_symlink_match_under_additional_directory_allows(self, tmp_path):
        project = tmp_path / "project"
        outside = tmp_path / "outside"
        project.mkdir()
        outside.mkdir()
        (outside / "allowed.txt").write_text("allowed", encoding="utf-8")
        (project / "link-outside").symlink_to(outside, target_is_directory=True)

        r = await check_tool_permission(
            GlobTool(),
            {"pattern": "link-outside/*", "path": str(project)},
            ToolPermissionContext(cwd=str(project), additional_directories=[str(outside)]),
        )

        assert r.behavior == "allow"

    @pytest.mark.parametrize(
        ("tool", "tool_input"),
        [
            (WriteFileTool(), {"content": "outside"}),
            (EditFileTool(), {"old_string": "before", "new_string": "after"}),
        ],
    )
    @pytest.mark.asyncio
    async def test_bare_write_tool_allow_does_not_override_path_constraint(self, tool, tool_input, tmp_path):
        project = tmp_path / "project"
        outside = tmp_path / "outside"
        project.mkdir()
        outside.mkdir()
        target = outside / "target.txt"
        target.write_text("before", encoding="utf-8")

        r = await check_tool_permission(
            tool,
            {**tool_input, "path": str(target)},
            ToolPermissionContext(cwd=str(project), allow_rules={"user_settings": [tool.name]}),
        )

        assert r.behavior == "ask"
        assert r.reason is not None
        assert r.reason.type == "path_constraint"

    @pytest.mark.parametrize(
        ("tool", "tool_input"),
        [
            (ReadFileTool(), {}),
            (WebFetchTool(), {"url": "https://example.com"}),
        ],
    )
    @pytest.mark.asyncio
    async def test_bare_ask_rule_forces_prompt_for_auto_allowed_tools(self, tool, tool_input, tmp_path):
        project = tmp_path / "project"
        project.mkdir()
        target = project / "notes.txt"
        target.write_text("inside", encoding="utf-8")

        if tool.name == "read_file":
            tool_input = {"path": str(target)}

        r = await check_tool_permission(
            tool,
            tool_input,
            ToolPermissionContext(cwd=str(project), ask_rules={"user_settings": [tool.name]}),
        )

        assert r.behavior == "ask"
        assert r.reason is not None
        assert r.reason.type == "rule"

    @pytest.mark.asyncio
    async def test_bare_bash_allow_does_not_override_dangerous_argument(self, tmp_path):
        r = await check_tool_permission(
            BashTool(),
            {"command": "find . -delete"},
            ToolPermissionContext(cwd=str(tmp_path), allow_rules={"user_settings": ["bash"]}),
        )

        assert r.behavior == "ask"
        assert r.reason is not None
        assert r.reason.type == "dangerous_readonly_argument"

    @pytest.mark.asyncio
    async def test_bare_bash_allow_does_not_override_read_path_constraint(self, tmp_path):
        r = await check_tool_permission(
            BashTool(),
            {"command": "cat /etc/passwd"},
            ToolPermissionContext(cwd=str(tmp_path), allow_rules={"user_settings": ["bash"]}),
        )

        assert r.behavior == "ask"
        assert r.reason is not None
        assert r.reason.type == "path_constraint"

    @pytest.mark.asyncio
    async def test_tool_level_deny(self):
        ctx = _ctx(deny={"user_settings": ["write_file"]})
        r = await check_tool_permission(FakeWriteTool(), {}, ctx)
        assert r.behavior == "deny"

    @pytest.mark.asyncio
    async def test_deny_rule_includes_audit_metadata(self):
        r = await check_tool_permission(FakeWriteTool(), {}, _ctx(deny={"user_settings": ["write_file"]}))
        assert r.behavior == "deny"
        assert r.audit is not None
        assert r.audit.scope == "settings_rule"
        assert r.audit.rule_source == "user_settings"
        assert r.audit.reason_detail == "matched deny rule: write_file"

    @pytest.mark.asyncio
    async def test_cli_bare_deny_rule_includes_cli_rule_audit_scope(self):
        r = await check_tool_permission(FakeWriteTool(), {}, _ctx(deny={"cli_arg": ["write_file"]}))
        assert r.behavior == "deny"
        assert r.audit is not None
        assert r.audit.scope == "cli_rule"
        assert r.audit.rule_source == "cli_arg"
        assert r.audit.rule == "write_file"

    @pytest.mark.asyncio
    async def test_tool_level_ask_rule_includes_audit_metadata(self):
        r = await check_tool_permission(FakeReadTool(), {}, _ctx(ask={"project_settings": ["read_file"]}))
        assert r.behavior == "ask"
        assert r.audit is not None
        assert r.audit.scope == "settings_rule"
        assert r.audit.rule_source == "project_settings"
        assert r.audit.rule == "read_file"
        assert r.audit.reason_type == "rule"
        assert r.audit.reason_detail == "matched ask rule(s): read_file"
        assert r.audit.is_read_only is True

    @pytest.mark.asyncio
    async def test_tool_level_allow(self):
        ctx = _ctx(allow={"user_settings": ["write_file"]})
        r = await check_tool_permission(FakeWriteTool(), {}, ctx)
        assert r.behavior == "allow"

    @pytest.mark.asyncio
    async def test_bare_allow_rule_includes_audit_metadata(self):
        r = await check_tool_permission(FakeWriteTool(), {}, _ctx(allow={"user_settings": ["write_file"]}))
        assert r.behavior == "allow"
        assert r.audit is not None
        assert r.audit.scope == "settings_rule"
        assert r.audit.rule_source == "user_settings"
        assert r.audit.rule == "write_file"
        assert r.audit.reason_detail == "matched allow rule: write_file"

    @pytest.mark.asyncio
    async def test_session_bare_allow_rule_includes_session_rule_audit_scope(self):
        r = await check_tool_permission(FakeWriteTool(), {}, _ctx(allow={"session": ["write_file"]}))
        assert r.behavior == "allow"
        assert r.audit is not None
        assert r.audit.scope == "session_rule"
        assert r.audit.rule_source == "session"
        assert r.audit.rule == "write_file"

    @pytest.mark.asyncio
    async def test_bare_aliyun_api_allow_does_not_auto_allow_aliyun_write(self):
        r = await check_tool_permission(
            AliyunApi(),
            {"product": "ros", "action": "CreateStack"},
            _ctx(allow={"user_settings": ["aliyun_api"]}),
        )
        assert r.behavior == "ask"
        assert r.audit is not None
        assert r.audit.scope == "once"
        assert r.audit.operation["action"] == "CreateStack"
        assert r.audit.is_read_only is False

    @pytest.mark.asyncio
    async def test_bypass_mode_allows(self):
        ctx = _ctx(mode=PermissionMode.BYPASS_PERMISSIONS)
        r = await check_tool_permission(FakeWriteTool(), {}, ctx)
        assert r.behavior == "allow"

    @pytest.mark.asyncio
    async def test_bypass_mode_includes_audit_metadata(self):
        r = await check_tool_permission(FakeWriteTool(), {}, _ctx(mode=PermissionMode.BYPASS_PERMISSIONS))
        assert r.behavior == "allow"
        assert r.audit is not None
        assert r.audit.scope == "mode"
        assert r.audit.rule_source == "mode"

    @pytest.mark.asyncio
    async def test_aliyun_api_bypass_mode_allows_write_with_audit(self):
        r = await check_tool_permission(
            AliyunApi(),
            {"product": "ros", "action": "CreateStack"},
            _ctx(mode=PermissionMode.BYPASS_PERMISSIONS),
        )
        assert r.behavior == "allow"
        assert r.audit is not None
        assert r.audit.scope == "mode"
        assert r.audit.rule_source == "mode"
        assert r.audit.reason_type == "bypass_permissions"
        assert r.audit.operation["action"] == "CreateStack"
        assert r.audit.is_read_only is False

    @pytest.mark.asyncio
    async def test_aliyun_api_bypass_mode_preserves_explicit_write_rule_audit(self):
        r = await check_tool_permission(
            AliyunApi(),
            {"product": "ros", "action": "CreateStack"},
            _ctx(
                mode=PermissionMode.BYPASS_PERMISSIONS,
                allow={"user_settings": ["aliyun_api(ros:CreateStack)"]},
            ),
        )

        assert r.behavior == "allow"
        assert r.audit is not None
        assert r.audit.scope == "settings_rule"
        assert r.audit.rule_source == "user_settings"
        assert r.audit.rule == "ros:CreateStack"
        assert r.audit.reason_type == "rule"
        assert r.audit.operation["action"] == "CreateStack"
        assert r.audit.is_read_only is False

    @pytest.mark.parametrize(
        ("tool", "tool_input"),
        [
            (ReadFileTool(), {}),
            (ListFilesTool(), {}),
            (WriteFileTool(), {"content": "outside"}),
            (BashTool(), {}),
        ],
    )
    @pytest.mark.asyncio
    async def test_bypass_mode_allows_path_constraints(self, tool, tool_input, tmp_path):
        project = tmp_path / "project"
        outside = tmp_path / "outside"
        project.mkdir()
        outside.mkdir()
        target = outside / "target.txt"
        target.write_text("outside", encoding="utf-8")

        if tool.name == "bash":
            tool_input = {"command": "cat {}".format(target)}
        else:
            tool_input = {**tool_input, "path": str(target)}

        r = await check_tool_permission(
            tool,
            tool_input,
            ToolPermissionContext(cwd=str(project), mode=PermissionMode.BYPASS_PERMISSIONS),
        )

        assert r.behavior == "allow"

    @pytest.mark.asyncio
    async def test_dont_ask_converts_to_deny(self):
        ctx = _ctx(mode=PermissionMode.DONT_ASK)
        r = await check_tool_permission(FakeWriteTool(), {}, ctx)
        assert r.behavior == "deny"

    @pytest.mark.asyncio
    async def test_dont_ask_mode_includes_audit_metadata(self):
        r = await check_tool_permission(FakeWriteTool(), {}, _ctx(mode=PermissionMode.DONT_ASK))
        assert r.behavior == "deny"
        assert r.audit is not None
        assert r.audit.scope == "mode"
        assert r.audit.rule_source == "mode"

    @pytest.mark.asyncio
    async def test_aliyun_api_dont_ask_mode_preserves_tool_audit_operation(self):
        r = await check_tool_permission(
            AliyunApi(),
            {"product": "ros", "action": "CreateStack"},
            _ctx(mode=PermissionMode.DONT_ASK),
        )
        assert r.behavior == "deny"
        assert r.audit is not None
        assert r.audit.scope == "mode"
        assert r.audit.rule_source == "mode"
        assert r.audit.reason_type == "dont_ask"
        assert r.audit.operation["action"] == "CreateStack"
        assert r.audit.is_read_only is False

    @pytest.mark.asyncio
    async def test_default_mode_asks(self):
        ctx = _ctx(mode=PermissionMode.DEFAULT)
        r = await check_tool_permission(FakeWriteTool(), {}, ctx)
        assert r.behavior == "ask"
