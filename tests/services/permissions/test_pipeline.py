import pytest

from iac_code.services.permissions.pipeline import check_tool_permission
from iac_code.tools.base import Tool, ToolResult
from iac_code.tools.bash.bash_tool import BashTool
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
        target.write_text("outside")

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
        target.write_text("outside")

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
        (outside / "secret.txt").write_text("secret")

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
        (outside / "secret.txt").write_text("secret")
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
        (outside / "allowed.txt").write_text("allowed")
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
        target.write_text("before")

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
        target.write_text("inside")

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
    async def test_tool_level_allow(self):
        ctx = _ctx(allow={"user_settings": ["write_file"]})
        r = await check_tool_permission(FakeWriteTool(), {}, ctx)
        assert r.behavior == "allow"

    @pytest.mark.asyncio
    async def test_bypass_mode_allows(self):
        ctx = _ctx(mode=PermissionMode.BYPASS_PERMISSIONS)
        r = await check_tool_permission(FakeWriteTool(), {}, ctx)
        assert r.behavior == "allow"

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
        target.write_text("outside")

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
    async def test_default_mode_asks(self):
        ctx = _ctx(mode=PermissionMode.DEFAULT)
        r = await check_tool_permission(FakeWriteTool(), {}, ctx)
        assert r.behavior == "ask"
