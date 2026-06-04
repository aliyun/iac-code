import shlex

import pytest

from iac_code.tools.bash.command_parser import SimpleCommand
from iac_code.tools.bash.readonly_commands import is_command_readonly


def _readonly(command: str) -> bool:
    return is_command_readonly(SimpleCommand(text=command, argv=shlex.split(command), redirects=[]))


class TestReadonlyBasicCommands:
    @pytest.mark.parametrize("cmd", ["ls", "ls -la", "cat foo.txt", "head -n5 file", "tail file", "wc -l file"])
    def test_filesystem_view_commands(self, cmd):
        assert is_command_readonly(SimpleCommand(text=cmd, argv=cmd.split(), redirects=[])) is True

    @pytest.mark.parametrize("cmd", ["grep pattern file", "rg foo", "find . -name '*.py'", "which python"])
    def test_search_commands(self, cmd):
        assert is_command_readonly(SimpleCommand(text=cmd, argv=cmd.split(), redirects=[])) is True

    @pytest.mark.parametrize("cmd", ["pwd", "env", "whoami", "hostname", "uname -a", "date"])
    def test_system_info_commands(self, cmd):
        assert is_command_readonly(SimpleCommand(text=cmd, argv=cmd.split(), redirects=[])) is True

    @pytest.mark.parametrize("cmd", ["echo hello", "printf '%s' foo"])
    def test_output_commands(self, cmd):
        assert is_command_readonly(SimpleCommand(text=cmd, argv=cmd.split(), redirects=[])) is True


class TestReadonlyGitCommands:
    @pytest.mark.parametrize(
        "cmd",
        [
            "git status",
            "git log",
            "git diff",
            "git show HEAD",
            "git branch",
            "git tag",
            "git blame file.py",
        ],
    )
    def test_git_readonly(self, cmd):
        assert is_command_readonly(SimpleCommand(text=cmd, argv=cmd.split(), redirects=[])) is True

    @pytest.mark.parametrize(
        "cmd",
        [
            "git push",
            "git commit -m 'msg'",
            "git checkout main",
            "git merge dev",
            "git rebase main",
        ],
    )
    def test_git_write_not_readonly(self, cmd):
        assert is_command_readonly(SimpleCommand(text=cmd, argv=cmd.split(), redirects=[])) is False


class TestReadonlyVersionCommands:
    @pytest.mark.parametrize("cmd", ["python --version", "node --version", "cargo --version"])
    def test_version_flags(self, cmd):
        assert is_command_readonly(SimpleCommand(text=cmd, argv=cmd.split(), redirects=[])) is True


class TestNotReadonly:
    @pytest.mark.parametrize(
        "cmd",
        [
            "rm -rf /",
            "mv a b",
            "cp a b",
            "mkdir dir",
            "python script.py",
            "node app.js",
            "curl https://example.com",
            "wget file",
            "npm install",
            "pip install pkg",
            "docker run img",
            "ssh host",
            "chmod 755 file",
            "sed -i 's/a/b/' file",
        ],
    )
    def test_write_and_dangerous_commands(self, cmd):
        assert is_command_readonly(SimpleCommand(text=cmd, argv=cmd.split(), redirects=[])) is False

    @pytest.mark.parametrize(
        "cmd",
        [
            "find . -delete",
            "find . -exec sh -c 'echo marker' ;",
            "find . -execdir sh -c 'echo marker' ;",
            "find . -ok sh -c 'echo marker' ;",
            "find . -okdir sh -c 'echo marker' ;",
            "fd . -x sh -c 'echo marker'",
            "fd . -X sh -c 'echo marker'",
            "fd . -xecho",
            "fd . -Xecho",
            "fd . --exec sh -c 'echo marker'",
            "fd . --exec=echo",
            "rg --pre 'sh -c echo-marker' needle .",
            "rg --pre=cat needle .",
            "sort --compress-program sh file.txt",
            "sort --compress-program=sh file.txt",
            "sort -o out.txt file.txt",
            "sort --output out.txt file.txt",
            "sort --output=out.txt file.txt",
            "sed -ibak 's/a/b/' file",
            "sed -Ei 's/a/b/' file",
            "sed -ni 's/a/b/' file",
            "sed -ri 's/a/b/' file",
            "sed -zi 's/a/b/' file",
            "sed -f run.sed file.txt",
            "sed --file run.sed file.txt",
            "sed -frun.sed file.txt",
            "sed -n '1e echo marker' file.txt",
            "sed '1!e echo marker' file.txt",
            "sed '/foo/!e echo marker' file.txt",
            "sed 's/.*/echo marker/e' file.txt",
        ],
    )
    def test_dangerous_readonly_arguments_are_not_readonly(self, cmd):
        assert _readonly(cmd) is False

    @pytest.mark.parametrize(
        "cmd",
        [
            "find . -name '*.py'",
            "fd pattern src",
            "rg needle src",
            "sort file.txt",
        ],
    )
    def test_safe_readonly_arguments_remain_readonly(self, cmd):
        assert _readonly(cmd) is True

    @pytest.mark.parametrize("cmd", ["pip list", "pip3 list", "pip3.11 list"])
    def test_pip_like_base_readonly(self, cmd):
        assert _readonly(cmd) is True

    @pytest.mark.parametrize("cmd", ["pipx list", "pip-audit list", "pip-compile list", "pipeline-deploy list"])
    def test_non_pip_like_base_not_readonly(self, cmd):
        assert _readonly(cmd) is False


class TestRedirectDisqualifies:
    def test_echo_with_redirect(self):
        cmd = SimpleCommand(text="echo hello > out.txt", argv=["echo", "hello"], redirects=["> out.txt"])
        assert is_command_readonly(cmd) is False

    def test_cat_without_redirect(self):
        cmd = SimpleCommand(text="cat file.txt", argv=["cat", "file.txt"], redirects=[])
        assert is_command_readonly(cmd) is True
