import pytest

from iac_code.tools.bash.command_parser import SimpleCommand
from iac_code.tools.bash.permissions import bash_tool_check_permission, bash_tool_has_permission
from iac_code.types.permissions import PermissionMode, ToolPermissionContext


def _ctx(mode=PermissionMode.DEFAULT, allow=None, deny=None, ask=None, cwd="/project"):
    return ToolPermissionContext(
        mode=mode,
        cwd=cwd,
        allow_rules=allow or {},
        deny_rules=deny or {},
        ask_rules=ask or {},
    )


class TestBashToolHasPermission:
    @pytest.mark.asyncio
    async def test_readonly_command_allowed(self):
        r = await bash_tool_has_permission("ls -la", _ctx())
        assert r.behavior == "allow"

    @pytest.mark.asyncio
    async def test_deny_rule_blocks(self):
        ctx = _ctx(deny={"user_settings": ["bash(rm -rf /)"]})
        r = await bash_tool_has_permission("rm -rf /", ctx)
        assert r.behavior == "deny"

    @pytest.mark.asyncio
    async def test_allow_rule_passes(self):
        ctx = _ctx(allow={"user_settings": ["bash(git:*)"]})
        r = await bash_tool_has_permission("git push", ctx)
        assert r.behavior == "allow"

    @pytest.mark.asyncio
    async def test_unknown_command_asks(self):
        r = await bash_tool_has_permission("docker run img", _ctx())
        assert r.behavior in ("ask", "passthrough")

    @pytest.mark.asyncio
    async def test_compound_with_deny(self):
        ctx = _ctx(deny={"user_settings": ["bash(rm:*)"]})
        r = await bash_tool_has_permission("ls && rm file", ctx)
        assert r.behavior == "deny"

    @pytest.mark.asyncio
    async def test_accept_edits_allows_filesystem(self):
        ctx = _ctx(mode=PermissionMode.ACCEPT_EDITS)
        r = await bash_tool_has_permission("mkdir foo", ctx)
        assert r.behavior == "allow"


class TestBashToolCheckPermission:
    def test_deny_rule_first(self):
        ctx = _ctx(
            deny={"user_settings": ["bash(git push:*)"]},
            allow={"user_settings": ["bash(git:*)"]},
        )
        cmd = SimpleCommand(text="git push origin main", argv=["git", "push", "origin", "main"])
        r = bash_tool_check_permission(cmd, ctx)
        assert r.behavior == "deny"

    def test_readonly_auto_allow(self):
        cmd = SimpleCommand(text="cat file.txt", argv=["cat", "file.txt"])
        r = bash_tool_check_permission(cmd, _ctx())
        assert r.behavior == "allow"

    def test_passthrough_for_unknown(self):
        cmd = SimpleCommand(text="docker build .", argv=["docker", "build", "."])
        r = bash_tool_check_permission(cmd, _ctx())
        assert r.behavior == "passthrough"


class TestIsComplexPermission:
    def test_complex_command_defaults_to_ask(self):
        cmd = SimpleCommand(text="eval ls", argv=["eval", "ls"], is_complex=True)
        r = bash_tool_check_permission(cmd, _ctx())
        assert r.behavior == "ask"

    def test_complex_command_allow_rule_still_works(self):
        ctx = _ctx(allow={"session": ["bash(eval:*)"]})
        cmd = SimpleCommand(text="eval ls", argv=["eval", "ls"], is_complex=True)
        r = bash_tool_check_permission(cmd, ctx)
        assert r.behavior == "allow"

    def test_complex_command_deny_rule_still_works(self):
        ctx = _ctx(deny={"session": ["bash(eval:*)"]})
        cmd = SimpleCommand(text="eval ls", argv=["eval", "ls"], is_complex=True)
        r = bash_tool_check_permission(cmd, ctx)
        assert r.behavior == "deny"

    def test_non_complex_command_not_affected(self):
        cmd = SimpleCommand(text="docker build .", argv=["docker", "build", "."], is_complex=False)
        r = bash_tool_check_permission(cmd, _ctx())
        assert r.behavior == "passthrough"
