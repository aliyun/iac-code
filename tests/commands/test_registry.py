"""Tests for the commands/registry module."""

from iac_code.commands import create_default_registry
from iac_code.commands.registry import CommandRegistry, LocalCommand, PromptCommand, _subsequence_score


async def dummy_handler(**kwargs):
    """Dummy handler for testing."""
    return "executed"


class TestCommand:
    """Tests for Command dataclass."""

    def test_create_command(self):
        """Test creating a LocalCommand."""
        cmd = LocalCommand(
            name="test",
            description="A test command",
            handler=dummy_handler,
        )
        assert cmd.name == "test"
        assert cmd.description == "A test command"
        assert cmd.handler == dummy_handler
        assert cmd.aliases == []
        assert cmd.hidden is False

    def test_create_command_with_aliases(self):
        """Test creating a LocalCommand with aliases."""
        cmd = LocalCommand(
            name="help",
            description="Show help",
            handler=dummy_handler,
            aliases=["?", "h"],
        )
        assert cmd.aliases == ["?", "h"]

    def test_create_hidden_command(self):
        """Test creating a hidden LocalCommand."""
        cmd = LocalCommand(
            name="secret",
            description="Hidden command",
            handler=dummy_handler,
            hidden=True,
        )
        assert cmd.hidden is True


class TestCommandRegistry:
    """Tests for CommandRegistry."""

    def test_register_and_get(self):
        """Test registering and getting a command."""
        registry = CommandRegistry()
        cmd = LocalCommand(name="test", description="Test", handler=dummy_handler)
        registry.register(cmd)
        assert registry.get("test") is cmd

    def test_get_nonexistent_command(self):
        """Test getting a non-existent command returns None."""
        registry = CommandRegistry()
        assert registry.get("nonexistent") is None

    def test_register_with_aliases(self):
        """Test that aliases are also registered."""
        registry = CommandRegistry()
        cmd = LocalCommand(
            name="help",
            description="Help",
            handler=dummy_handler,
            aliases=["?", "h"],
        )
        registry.register(cmd)
        assert registry.get("help") is cmd
        assert registry.get("?") is cmd
        assert registry.get("h") is cmd

    def test_get_all_excludes_duplicates(self):
        """Test get_all returns unique commands."""
        registry = CommandRegistry()
        cmd = LocalCommand(
            name="help",
            description="Help",
            handler=dummy_handler,
            aliases=["?"],
        )
        registry.register(cmd)
        all_cmds = registry.get_all()
        assert len(all_cmds) == 1
        assert all_cmds[0] is cmd

    def test_get_all_excludes_hidden(self):
        """Test get_all excludes hidden commands."""
        registry = CommandRegistry()
        visible_cmd = LocalCommand(name="help", description="Help", handler=dummy_handler)
        hidden_cmd = LocalCommand(name="secret", description="Secret", handler=dummy_handler, hidden=True)
        registry.register(visible_cmd)
        registry.register(hidden_cmd)
        all_cmds = registry.get_all()
        assert len(all_cmds) == 1
        assert all_cmds[0].name == "help"

    def test_get_all_sorted_by_name(self):
        """Test get_all returns commands sorted by name."""
        registry = CommandRegistry()
        cmd_b = LocalCommand(name="beta", description="Beta", handler=dummy_handler)
        cmd_a = LocalCommand(name="alpha", description="Alpha", handler=dummy_handler)
        registry.register(cmd_b)
        registry.register(cmd_a)
        all_cmds = registry.get_all()
        assert [c.name for c in all_cmds] == ["alpha", "beta"]

    def test_is_command_with_slash(self):
        """Test is_command returns True for /command."""
        registry = CommandRegistry()
        assert registry.is_command("/help") is True
        assert registry.is_command("/anything") is True

    def test_is_command_without_slash(self):
        """Test is_command returns False for text without /."""
        registry = CommandRegistry()
        assert registry.is_command("hello") is False
        assert registry.is_command("help") is False

    def test_is_command_with_dollar(self):
        """Test is_command returns True for $skill invocations."""
        registry = CommandRegistry()
        assert registry.is_command("$deploy") is True
        assert registry.is_command("$") is True

    def test_parse_command_simple(self):
        """Test parsing a simple command."""
        registry = CommandRegistry()
        name, args = registry.parse("/help")
        assert name == "help"
        assert args == []

    def test_parse_dollar_command_simple(self):
        """Test parsing a $-triggered skill name."""
        registry = CommandRegistry()
        name, args = registry.parse("$deploy")
        assert name == "deploy"
        assert args == []

    def test_parse_dollar_command_with_args(self):
        """Test parsing a $-triggered skill with arguments."""
        registry = CommandRegistry()
        name, args = registry.parse("$deploy prod us-west")
        assert name == "deploy"
        assert args == ["prod", "us-west"]

    def test_parse_command_with_args(self):
        """Test parsing a command with arguments."""
        registry = CommandRegistry()
        name, args = registry.parse("/model gpt-4")
        assert name == "model"
        assert args == ["gpt-4"]

    def test_parse_command_multiple_args(self):
        """Test parsing a command with multiple arguments."""
        registry = CommandRegistry()
        name, args = registry.parse("/cmd arg1 arg2 arg3")
        assert name == "cmd"
        assert args == ["arg1", "arg2", "arg3"]

    def test_get_completions_matching_prefix(self):
        """Test get_completions returns matching command names."""
        registry = CommandRegistry()
        registry.register(LocalCommand(name="model", description="M", handler=dummy_handler))
        registry.register(LocalCommand(name="mode", description="Mode", handler=dummy_handler))
        registry.register(LocalCommand(name="help", description="H", handler=dummy_handler))

        completions = registry.get_completions("mo")
        assert set(completions) == {"model", "mode"}

    def test_get_completions_excludes_hidden(self):
        """Test get_completions excludes hidden commands."""
        registry = CommandRegistry()
        registry.register(LocalCommand(name="model", description="M", handler=dummy_handler))
        registry.register(LocalCommand(name="mhidden", description="H", handler=dummy_handler, hidden=True))
        completions = registry.get_completions("m")
        assert completions == ["model"]

    def test_get_completions_sorted(self):
        """Test get_completions returns sorted results."""
        registry = CommandRegistry()
        registry.register(LocalCommand(name="model", description="M", handler=dummy_handler))
        registry.register(LocalCommand(name="map", description="Map", handler=dummy_handler))
        registry.register(LocalCommand(name="mark", description="Mark", handler=dummy_handler))
        completions = registry.get_completions("m")
        assert completions == ["map", "mark", "model"]

    def test_clear_prompt_commands_preserves_local_commands(self):
        """Test clearing prompt commands removes skills while preserving built-ins."""
        registry = CommandRegistry()
        local = LocalCommand(name="help", description="Help", handler=dummy_handler, aliases=["?"])
        skill = PromptCommand(name="deploy", description="Deploy", aliases=["d"])
        registry.register(local)
        registry.register(skill)

        registry.clear_prompt_commands()

        assert registry.get("help") is local
        assert registry.get("?") is local
        assert registry.get("deploy") is None
        assert registry.get("d") is None


class TestCreateDefaultRegistry:
    """Tests for create_default_registry function."""

    def test_create_default_registry_returns_registry(self):
        """Test create_default_registry returns a CommandRegistry."""
        registry = create_default_registry()
        assert isinstance(registry, CommandRegistry)

    def test_create_default_registry_has_13_commands(self):
        """Test create_default_registry has 13 commands."""
        registry = create_default_registry()
        all_cmds = registry.get_all()
        assert len(all_cmds) == 13

    def test_create_default_registry_command_names(self):
        """Test create_default_registry has expected command names."""
        registry = create_default_registry()
        all_cmds = registry.get_all()
        names = {c.name for c in all_cmds}
        assert names == {
            "help",
            "clear",
            "model",
            "compact",
            "exit",
            "auth",
            "debug",
            "effort",
            "resume",
            "memory",
            "skills",
            "status",
            "rename",
        }

    def test_default_registry_includes_rename(self):
        """Test rename command metadata."""
        registry = create_default_registry()
        rename_cmd = registry.get("rename")
        assert rename_cmd is not None
        assert rename_cmd.arg_hint == "<name>"
        assert rename_cmd.history_mode == "session"

    def test_help_command_has_alias(self):
        """Test help command has ? alias."""
        registry = create_default_registry()
        assert registry.get("?") is not None
        assert registry.get("?") is registry.get("help")

    def test_exit_command_has_aliases(self):
        """Test exit command has quit and q aliases."""
        registry = create_default_registry()
        exit_cmd = registry.get("exit")
        assert registry.get("quit") is exit_cmd
        assert registry.get("q") is exit_cmd


class TestSubsequenceScore:
    """Tests for _subsequence_score helper function."""

    def test_empty_query_returns_zero(self):
        """Empty query is a subsequence of anything with score 0."""
        assert _subsequence_score("", "hello") == 0

    def test_no_match_returns_none(self):
        """Query that is not a subsequence returns None."""
        assert _subsequence_score("xyz", "hello") is None

    def test_exact_match_returns_zero(self):
        """Exact match returns score 0 (no gap, starts at 0)."""
        assert _subsequence_score("hello", "hello") == 0

    def test_partial_subsequence_no_gap(self):
        """Consecutive match at start: score is start_penalty only."""
        # query "he" in "help": positions [0, 1], gap_penalty=0, start_penalty=0
        assert _subsequence_score("he", "help") == 0

    def test_partial_subsequence_with_gap(self):
        """Non-consecutive match: score includes gap penalty."""
        # query "hp" in "help": positions [0, 3], gap_penalty = (3-0-1)=2, start_penalty=0
        assert _subsequence_score("hp", "help") == 2

    def test_subsequence_with_start_penalty(self):
        """Match not at start: score includes start_penalty."""
        # query "lp" in "help": positions [2, 3], gap_penalty=0, start_penalty=2
        assert _subsequence_score("lp", "help") == 2

    def test_case_insensitive(self):
        """_subsequence_score is case insensitive."""
        assert _subsequence_score("HE", "Help") == _subsequence_score("he", "help")


class TestFuzzySearch:
    """Tests for CommandRegistry.fuzzy_search covering all priority levels."""

    def _make_registry(self):
        registry = CommandRegistry()
        registry.register(
            LocalCommand(name="model", description="Switch the AI model", handler=dummy_handler, aliases=["md"])
        )
        registry.register(
            LocalCommand(name="help", description="Show help information", handler=dummy_handler, aliases=["?", "h"])
        )
        registry.register(LocalCommand(name="clear", description="Clear the screen", handler=dummy_handler))
        return registry

    def test_empty_query_returns_all(self):
        """Empty query returns all commands."""
        registry = self._make_registry()
        results = registry.fuzzy_search("")
        assert len(results) == 3

    def test_exact_name_match_priority_zero(self):
        """Exact name match gets priority 0."""
        registry = self._make_registry()
        results = registry.fuzzy_search("model")
        assert len(results) >= 1
        assert results[0].priority == 0
        assert results[0].name == "model"

    def test_name_prefix_match_priority_one(self):
        """Name prefix match gets priority 1."""
        registry = self._make_registry()
        results = registry.fuzzy_search("mo")
        assert len(results) >= 1
        assert results[0].priority == 1
        assert results[0].name == "model"

    def test_exact_alias_match_priority_two(self):
        """Exact alias match gets priority 2 (lines 192-194)."""
        registry = self._make_registry()
        results = registry.fuzzy_search("md")
        # "md" is exact alias for "model"
        assert len(results) >= 1
        assert results[0].priority == 2
        assert results[0].name == "md"
        assert results[0].command.name == "model"

    def test_alias_prefix_match_priority_three(self):
        """Alias prefix match gets priority 3 (lines 196-198)."""
        registry = self._make_registry()
        # "?" is an alias for "help"; query "?" triggers exact alias, so use a longer alias
        # Add a command with a longer alias
        registry.register(LocalCommand(name="exit", description="Exit", handler=dummy_handler, aliases=["quit"]))
        results = registry.fuzzy_search("qu")
        # "qu" is a prefix of alias "quit"
        alias_prefix_results = [r for r in results if r.priority == 3]
        assert len(alias_prefix_results) >= 1
        assert alias_prefix_results[0].name == "quit"

    def test_subsequence_match_priority_four(self):
        """Subsequence match on name gets priority 4 (lines 205-206)."""
        registry = self._make_registry()
        # "mdl" is a subsequence of "model" but not a prefix
        results = registry.fuzzy_search("mdl")
        subseq_results = [r for r in results if r.priority == 4]
        assert len(subseq_results) >= 1
        assert subseq_results[0].command.name == "model"

    def test_description_keyword_match_priority_five(self):
        """Description keyword match gets priority 5 (lines 211-212)."""
        registry = self._make_registry()
        # "screen" appears in description of "clear" but not in its name
        results = registry.fuzzy_search("screen")
        desc_results = [r for r in results if r.priority == 5]
        assert len(desc_results) >= 1
        assert desc_results[0].command.name == "clear"

    def test_no_match_returns_empty(self):
        """Query that matches nothing returns empty list."""
        registry = self._make_registry()
        results = registry.fuzzy_search("zzzzz")
        assert results == []

    def test_results_sorted_by_priority_then_score(self):
        """Results are sorted by (priority, score)."""
        registry = self._make_registry()
        results = registry.fuzzy_search("h")
        priorities = [r.priority for r in results]
        assert priorities == sorted(priorities)


class TestGetBestPrefixMatch:
    """Tests for CommandRegistry.get_best_prefix_match (lines 219-229)."""

    def test_empty_partial_returns_none(self):
        """Empty partial returns None (line 219-220)."""
        registry = CommandRegistry()
        registry.register(LocalCommand(name="help", description="Help", handler=dummy_handler))
        assert registry.get_best_prefix_match("") is None

    def test_matches_command_name(self):
        """Returns command name when prefix matches name."""
        registry = CommandRegistry()
        registry.register(LocalCommand(name="help", description="Help", handler=dummy_handler))
        assert registry.get_best_prefix_match("he") == "help"

    def test_matches_alias_when_name_not_matching(self):
        """Falls back to alias when name doesn't match prefix (lines 225-228)."""
        registry = CommandRegistry()
        registry.register(LocalCommand(name="help", description="Help", handler=dummy_handler, aliases=["?", "hh"]))
        # "hh" is an alias prefix match
        assert registry.get_best_prefix_match("hh") == "hh"

    def test_returns_none_when_no_match(self):
        """Returns None when no name or alias matches (line 229)."""
        registry = CommandRegistry()
        registry.register(LocalCommand(name="help", description="Help", handler=dummy_handler))
        assert registry.get_best_prefix_match("xyz") is None

    def test_exact_prefix_match_on_alias(self):
        """Returns alias when only alias matches the prefix."""
        registry = CommandRegistry()
        registry.register(LocalCommand(name="exit", description="Exit", handler=dummy_handler, aliases=["quit"]))
        assert registry.get_best_prefix_match("qu") == "quit"
