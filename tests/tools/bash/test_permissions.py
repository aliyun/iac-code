import pytest

from iac_code.tools.bash.command_parser import SimpleCommand
from iac_code.tools.bash.permissions import bash_tool_check_permission, bash_tool_has_permission
from iac_code.types.permissions import PermissionMode, ToolPermissionContext


def _ctx(mode=PermissionMode.DEFAULT, allow=None, deny=None, ask=None, cwd="/project", trusted_read_directories=None):
    return ToolPermissionContext(
        mode=mode,
        cwd=cwd,
        allow_rules=allow or {},
        deny_rules=deny or {},
        ask_rules=ask or {},
        trusted_read_directories=trusted_read_directories or [],
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

    @pytest.mark.parametrize(
        "cmd",
        [
            "find . -delete",
            r"find . -exec sh -c 'echo marker' \;",
            r"find . -execdir sh -c 'echo marker' \;",
            r"find . -ok sh -c 'echo marker' \;",
            "fd . -x sh -c 'echo marker'",
            "fd . -X sh -c 'echo marker'",
            "fd . -xecho",
            "fd . -Xecho",
            "fd . --exec sh -c 'echo marker'",
            "fd . --exec=echo",
            "sed -i 's/a/b/' file",
            "sed -ibak 's/a/b/' file",
            "sed -Ei 's/a/b/' file",
            "sed -ni 's/a/b/' file",
            "sed -ri 's/a/b/' file",
            "sed -zi 's/a/b/' file",
            "sed -f run.sed file.txt",
            "sed --file run.sed file.txt",
            "sed -frun.sed file.txt",
            "sed -n '1e echo marker' file.txt",
            "sed 's/.*/echo marker/e' file.txt",
            "sed 's/a/echo marker/2e' file.txt",
            "sed 's/a/echo marker/g2e' file.txt",
            "sed 'e;' file.txt",
            "sed '1{e;}' file.txt",
            "sed '1{e echo marker;}' file.txt",
            "sed '1~2e echo marker' file.txt",
            "sed '1,+2e echo marker' file.txt",
            "sed '\\#foo#w/tmp/out' file.txt",
            "sed '/foo/Iw/tmp/out' file.txt",
            "sed '\\%foo%e echo marker' file.txt",
            "sed '/foo/Ie echo marker' file.txt",
            "sed '\\afooaw/tmp/out' file.txt",
            "sed '\\1foo1e echo marker' file.txt",
            "sed '\\ foo w/tmp/out' file.txt",
            "sed -Ees/a/b/w/tmp/out file.txt",
            "sed -nes/a/b/e file.txt",
            "sed 'w /tmp/out' file.txt",
            "sed '1w /tmp/out' file.txt",
            "sed 's/a/b/w /tmp/out' file.txt",
            "sed 's1foo1bar1w /tmp/out' file.txt",
            "sed 's foo bar w /tmp/out' file.txt",
            "sed 's1foo1echo marker1e' file.txt",
            "rg --pre cat needle .",
            "rg --pre=cat needle .",
            "sort --compress-program sh file.txt",
            "sort --compress-program=sh file.txt",
            "sort -o out.txt file.txt",
            "sort --output out.txt file.txt",
            "sort --output=out.txt file.txt",
        ],
    )
    @pytest.mark.asyncio
    async def test_dangerous_readonly_arguments_ask(self, cmd):
        r = await bash_tool_has_permission(cmd, _ctx())
        assert r.behavior == "ask"
        assert r.reason is not None
        assert r.reason.type == "dangerous_readonly_argument"

    @pytest.mark.asyncio
    async def test_sed_file_write_reason_detail_translates_synthetic_reason(self, monkeypatch):
        def fake_gettext(message):
            if message == "sed file write":
                return "i18n:sed file write"
            return message

        monkeypatch.setattr("iac_code.tools.bash.permissions._", fake_gettext)

        r = await bash_tool_has_permission("sed 'w /tmp/out' file.txt", _ctx())

        assert r.behavior == "ask"
        assert r.reason is not None
        assert "i18n:sed file write" in r.reason.detail

    @pytest.mark.parametrize("cmd", ["find . -delete", "rg --pre cat needle ."])
    @pytest.mark.asyncio
    async def test_dangerous_readonly_arguments_do_not_suggest_broad_allow_rules(self, cmd):
        r = await bash_tool_has_permission(cmd, _ctx())
        assert r.behavior == "ask"
        assert r.reason is not None
        assert r.reason.type == "dangerous_readonly_argument"
        assert not r.suggestions

    @pytest.mark.asyncio
    async def test_dangerous_readonly_argument_overrides_broad_allow_rule(self):
        ctx = _ctx(allow={"user_settings": ["bash(find:*)"]})
        r = await bash_tool_has_permission("find . -delete", ctx)
        assert r.behavior == "ask"
        assert r.reason is not None
        assert r.reason.type == "dangerous_readonly_argument"

    @pytest.mark.parametrize(
        "cmd",
        [
            "ls /etc",
            "ls ~/.ssh",
            "sha256sum /etc/passwd",
            "diff /etc/passwd file.txt",
            "jq . /etc/passwd",
            "rg --files /etc",
            "find -L /etc -maxdepth 1",
            "tail -f /etc/passwd",
            "cat -e /etc/passwd",
            "cat ~/notes.txt",
            "cat $HOME/notes.txt",
        ],
    )
    @pytest.mark.asyncio
    async def test_readonly_path_bypass_forms_ask(self, cmd, tmp_path):
        r = await bash_tool_has_permission(cmd, _ctx(cwd=str(tmp_path)))
        assert r.behavior == "ask"
        assert r.reason is not None
        assert r.reason.type in {"path_constraint", "safety_check"}

    @pytest.mark.parametrize("cmd", ["sed -Ees/a/b/ /etc/passwd", "sed -nes/a/b/ /etc/passwd"])
    @pytest.mark.asyncio
    async def test_sed_grouped_expression_options_preserve_read_path_constraints(self, cmd, tmp_path):
        r = await bash_tool_has_permission(cmd, _ctx(cwd=str(tmp_path)))
        assert r.behavior == "ask"
        assert r.reason is not None
        assert r.reason.type == "path_constraint"

    @pytest.mark.parametrize(
        "cmd",
        [
            "sed 'r /etc/passwd' file.txt",
            "sed 'R /etc/passwd' file.txt",
            "sed -e 'p' -e 'r /etc/passwd' file.txt",
            "sed -- 'r /etc/passwd' file.txt",
        ],
    )
    @pytest.mark.asyncio
    async def test_sed_script_read_paths_outside_cwd_ask(self, cmd, tmp_path):
        r = await bash_tool_has_permission(cmd, _ctx(cwd=str(tmp_path)))
        assert r.behavior == "ask"
        assert r.reason is not None
        assert r.reason.type == "path_constraint"

    @pytest.mark.asyncio
    async def test_input_redirect_read_path_overrides_broad_allow_rule(self, tmp_path):
        ctx = _ctx(cwd=str(tmp_path), allow={"user_settings": ["bash(cat:*)"]})
        r = await bash_tool_has_permission("cat < /etc/passwd", ctx)
        assert r.behavior == "ask"
        assert r.reason is not None
        assert r.reason.type == "path_constraint"

    @pytest.mark.parametrize(
        "cmd",
        [
            "fd --base-directory /etc passwd",
            "fd --base-directory=/etc passwd",
            "fd --search-path=/etc passwd",
        ],
    )
    @pytest.mark.asyncio
    async def test_fd_path_bearing_options_ask_outside_cwd(self, cmd, tmp_path):
        r = await bash_tool_has_permission(cmd, _ctx(cwd=str(tmp_path)))
        assert r.behavior == "ask"
        assert r.reason is not None
        assert r.reason.type == "path_constraint"

    @pytest.mark.parametrize(
        ("cmd", "rule"),
        [
            ("cp --target-directory /etc file.txt", "bash(cp:*)"),
            ("cp --target-directory=/etc file.txt", "bash(cp:*)"),
            ("cp -t /etc file.txt", "bash(cp:*)"),
            ("cp -t/etc file.txt", "bash(cp:*)"),
            ("cp -pt/etc file.txt", "bash(cp:*)"),
            ("mv --target-directory /etc file.txt", "bash(mv:*)"),
            ("mv --target-directory=/etc file.txt", "bash(mv:*)"),
            ("mv -t /etc file.txt", "bash(mv:*)"),
            ("mv -t/etc file.txt", "bash(mv:*)"),
            ("mv -vt/etc file.txt", "bash(mv:*)"),
            ("ln --target-directory /etc file.txt", "bash(ln:*)"),
            ("ln --target-directory=/etc file.txt", "bash(ln:*)"),
            ("ln -t /etc file.txt", "bash(ln:*)"),
            ("ln -t/etc file.txt", "bash(ln:*)"),
            ("ln -st/etc file.txt", "bash(ln:*)"),
            ("install --target-directory /etc file.txt", "bash(install:*)"),
            ("install --target-directory=/etc file.txt", "bash(install:*)"),
            ("install -t /etc file.txt", "bash(install:*)"),
            ("install -t/etc file.txt", "bash(install:*)"),
            ("install -Dt/etc file.txt", "bash(install:*)"),
        ],
    )
    @pytest.mark.asyncio
    async def test_target_directory_options_override_broad_allow_rule(self, cmd, rule, tmp_path):
        ctx = _ctx(cwd=str(tmp_path), allow={"user_settings": [rule]})
        r = await bash_tool_has_permission(cmd, ctx)
        assert r.behavior == "ask"
        assert r.reason is not None
        assert r.reason.type == "path_constraint"

    @pytest.mark.parametrize("cmd", ["cat <> /etc/passwd", "cat <(echo hi)"])
    @pytest.mark.asyncio
    async def test_broad_allow_rule_does_not_override_parse_or_complex_commands(self, cmd, tmp_path):
        ctx = _ctx(cwd=str(tmp_path), allow={"user_settings": ["bash(cat:*)"]})
        r = await bash_tool_has_permission(cmd, ctx)
        assert r.behavior == "ask"
        assert r.reason is not None
        assert r.reason.type in {"parse_error", "too_complex", "complex_command"}

    @pytest.mark.asyncio
    async def test_cat_etc_passwd_asks_path_constraint(self, tmp_path):
        r = await bash_tool_has_permission("cat /etc/passwd", _ctx(cwd=str(tmp_path)))
        assert r.behavior == "ask"
        assert r.reason is not None
        assert r.reason.type == "path_constraint"

    @pytest.mark.asyncio
    async def test_cat_file_under_cwd_allows(self, tmp_path):
        (tmp_path / "file.txt").write_text("ok", encoding="utf-8")
        r = await bash_tool_has_permission("cat file.txt", _ctx(cwd=str(tmp_path)))
        assert r.behavior == "allow"

    @pytest.mark.asyncio
    async def test_cat_trusted_read_directory_allows_outside_cwd(self, tmp_path):
        trusted = tmp_path / "trusted"
        trusted.mkdir()
        (trusted / "file.txt").write_text("ok", encoding="utf-8")
        cwd = tmp_path / "project"
        cwd.mkdir()
        r = await bash_tool_has_permission(
            "cat {}".format(trusted / "file.txt"),
            _ctx(cwd=str(cwd), trusted_read_directories=[str(trusted)]),
        )
        assert r.behavior == "allow"

    @pytest.mark.asyncio
    async def test_rg_without_path_under_cwd_allows(self, tmp_path):
        r = await bash_tool_has_permission("rg root", _ctx(cwd=str(tmp_path)))
        assert r.behavior == "allow"

    @pytest.mark.asyncio
    async def test_find_relative_root_under_cwd_allows(self, tmp_path):
        r = await bash_tool_has_permission("find . -name '*.py'", _ctx(cwd=str(tmp_path)))
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

    def test_complex_command_allow_rule_does_not_auto_allow(self):
        ctx = _ctx(allow={"session": ["bash(eval:*)"]})
        cmd = SimpleCommand(text="eval ls", argv=["eval", "ls"], is_complex=True)
        r = bash_tool_check_permission(cmd, ctx)
        assert r.behavior == "ask"
        assert r.reason is not None
        assert r.reason.type == "complex_command"

    def test_complex_command_deny_rule_still_works(self):
        ctx = _ctx(deny={"session": ["bash(eval:*)"]})
        cmd = SimpleCommand(text="eval ls", argv=["eval", "ls"], is_complex=True)
        r = bash_tool_check_permission(cmd, ctx)
        assert r.behavior == "deny"

    def test_non_complex_command_not_affected(self):
        cmd = SimpleCommand(text="docker build .", argv=["docker", "build", "."], is_complex=False)
        r = bash_tool_check_permission(cmd, _ctx())
        assert r.behavior == "passthrough"
