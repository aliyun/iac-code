"""Tests for tree-sitter bash command parsing."""

from iac_code.tools.bash.command_parser import parse_command


class TestParseSimpleCommands:
    def test_single_command(self):
        r = parse_command("ls -la")
        assert r.kind == "simple"
        assert len(r.commands) == 1
        assert r.commands[0].argv[0] == "ls"

    def test_git_push(self):
        r = parse_command("git push origin main")
        assert r.kind == "simple"
        assert r.commands[0].argv == ["git", "push", "origin", "main"]

    def test_command_with_redirect(self):
        r = parse_command("echo hello > out.txt")
        assert r.kind == "simple"
        assert len(r.commands[0].redirects) >= 1


class TestParseCompoundCommands:
    def test_and_chain(self):
        r = parse_command("cd /tmp && ls")
        assert r.kind == "simple"
        assert len(r.commands) == 2

    def test_pipe(self):
        r = parse_command("ls | grep foo")
        assert r.kind == "simple"
        assert len(r.commands) == 2

    def test_semicolon(self):
        r = parse_command("echo a; echo b")
        assert r.kind == "simple"
        assert len(r.commands) == 2


class TestParseTooComplex:
    def test_command_substitution_marks_complex(self):
        r = parse_command("echo $(whoami)")
        assert r.kind == "simple"
        assert len(r.commands) == 1
        assert r.commands[0].is_complex is True

    def test_backtick_substitution_marks_complex(self):
        r = parse_command("echo `whoami`")
        assert r.kind == "simple"
        assert len(r.commands) == 1
        assert r.commands[0].is_complex is True

    def test_eval_marks_complex(self):
        r = parse_command("eval 'rm -rf /'")
        assert r.kind == "simple"
        assert len(r.commands) == 1
        assert r.commands[0].is_complex is True

    def test_source_marks_complex(self):
        r = parse_command("source ~/.bashrc")
        assert r.kind == "simple"
        assert len(r.commands) == 1
        assert r.commands[0].is_complex is True

    def test_exec_marks_complex(self):
        r = parse_command("exec /bin/bash")
        assert r.kind == "simple"
        assert len(r.commands) == 1
        assert r.commands[0].is_complex is True

    def test_standalone_subshell_marks_complex(self):
        r = parse_command("$(whoami)")
        assert r.kind == "simple"
        assert len(r.commands) == 1
        assert r.commands[0].is_complex is True


class TestIsComplexField:
    def test_simple_command_not_complex(self):
        r = parse_command("ls -la")
        assert r.kind == "simple"
        assert r.commands[0].is_complex is False

    def test_eval_in_compound_marks_only_eval_complex(self):
        r = parse_command("eval ls && mkdir -p xxx")
        assert r.kind == "simple"
        assert len(r.commands) == 2
        assert r.commands[0].is_complex is True
        assert "eval" in r.commands[0].argv[0]
        assert r.commands[1].is_complex is False
        assert r.commands[1].argv[0] == "mkdir"

    def test_exec_in_compound_marks_only_exec_complex(self):
        r = parse_command("exec /bin/bash && echo hello")
        assert r.kind == "simple"
        assert r.commands[0].is_complex is True
        assert r.commands[1].is_complex is False

    def test_source_in_compound_marks_only_source_complex(self):
        r = parse_command("source ~/.bashrc && ls")
        assert r.kind == "simple"
        assert r.commands[0].is_complex is True
        assert r.commands[1].is_complex is False

    def test_command_substitution_in_arg_marks_complex(self):
        r = parse_command("mkdir $(echo dir) && ls")
        assert r.kind == "simple"
        assert len(r.commands) == 2
        assert r.commands[0].is_complex is True
        assert r.commands[1].is_complex is False

    def test_all_simple_commands_not_complex(self):
        r = parse_command("ls && cat foo")
        assert r.kind == "simple"
        assert all(c.is_complex is False for c in r.commands)

    def test_pipe_with_eval_marks_eval_complex(self):
        r = parse_command("eval 'ls' | grep foo")
        assert r.kind == "simple"
        assert r.commands[0].is_complex is True
        assert r.commands[1].is_complex is False


class TestParseEdgeCases:
    def test_empty_command(self):
        r = parse_command("")
        assert r.kind in ("parse_error", "simple")

    def test_env_var_prefix(self):
        r = parse_command("FOO=bar git push")
        assert r.kind == "simple"
        assert len(r.commands) >= 1
