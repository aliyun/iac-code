"""Command registry — unified registration for local commands and skill-based commands."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Awaitable, Callable, Literal

from iac_code.types.skill_source import SkillSource

if TYPE_CHECKING:
    from iac_code.skills.skill_definition import SkillDefinition


@dataclass
class Command:
    """Base class for all commands (both local and skill-based).

    Common fields shared by all command types.
    """

    name: str
    description: str
    aliases: list[str] = field(default_factory=list)
    hidden: bool = False

    @property
    def is_skill(self) -> bool:
        return False


@dataclass
class LocalCommand(Command):
    """Built-in slash command with a handler function.

    Examples: /help, /model, /clear, /compact
    """

    handler: Callable[..., Awaitable[Any]] | None = None
    arg_names: list[str] = field(default_factory=list)
    arg_hint: str | None = None
    """Inline hint shown as ghost text after the command name (e.g. "[on|off]")."""
    progress_label: str | None = None
    """When set, the REPL shows a spinner with this label while the handler runs.
    Use for commands that perform slow async work (e.g. an LLM call)."""
    history_mode: Literal["persist", "session", "none"] = "persist"
    """Controls how the command is recorded in input history.
    - "persist": saved to disk (default, for normal commands).
    - "session": kept in memory for the current session only.
    - "none": never recorded in history.
    """


@dataclass(frozen=True)
class CommandResult:
    """Structured result for local commands that need UI metadata."""

    message: str
    is_error: bool = False
    refresh_banner: bool = False

    def __bool__(self) -> bool:
        return bool(self.message)


@dataclass
class PromptCommand(Command):
    """Skill-based command backed by a SkillDefinition.

    No handler needed — REPL and SkillTool both route through
    process_prompt_command() directly based on is_skill check.
    """

    skill: SkillDefinition | None = field(default=None, repr=False)
    source: SkillSource = SkillSource.PROJECT

    @property
    def is_skill(self) -> bool:
        return True

    @property
    def when_to_use(self) -> str:
        return self.skill.when_to_use if self.skill else ""

    @property
    def user_invocable(self) -> bool:
        return self.skill.is_user_invocable if self.skill else True

    @property
    def model_invocable(self) -> bool:
        return True

    @property
    def content_length(self) -> int:
        return self.skill.content_length if self.skill else 0


# Type alias
AnyCommand = LocalCommand | PromptCommand


def _subsequence_score(query: str, target: str) -> float | None:
    """Check if query is a subsequence of target, return score (lower is better) or None."""
    query = query.lower()
    target = target.lower()
    qi = 0
    positions: list[int] = []
    for ti, ch in enumerate(target):
        if qi < len(query) and ch == query[qi]:
            positions.append(ti)
            qi += 1
    if qi < len(query):
        return None
    # Score: prefer consecutive matches and matches near the start
    gap_penalty = sum(positions[i] - positions[i - 1] - 1 for i in range(1, len(positions)))
    start_penalty = positions[0] if positions else 0
    return start_penalty + gap_penalty


@dataclass
class FuzzyMatch:
    """A fuzzy match result with scoring."""

    command: Command
    name: str  # The matched name (could be alias)
    priority: int  # 0=exact, 1=prefix, 2=alias_exact, 3=alias_prefix, 4=subsequence, 5=desc_keyword
    score: float  # Lower is better within the same priority


class CommandRegistry:
    """Unified registry for both local commands and prompt skills."""

    def __init__(self) -> None:
        self._commands: dict[str, Command] = {}
        self._skill_usage_counts: dict[str, int] = {}

    def register(self, command: Command) -> None:
        """Register a command or skill."""
        self._commands[command.name] = command
        for alias in command.aliases:
            self._commands[alias] = command

    def unregister(self, name: str) -> None:
        """Unregister a command and aliases that point at it."""
        command = self._commands.pop(name, None)
        if command is None:
            return
        for alias in command.aliases:
            if self._commands.get(alias) is command:
                del self._commands[alias]

    def clear_prompt_commands(self) -> None:
        """Remove all skill-backed commands while preserving local commands."""
        for name, command in list(self._commands.items()):
            if isinstance(command, PromptCommand):
                del self._commands[name]

    def get(self, name: str) -> Command | None:
        """Get command by name or alias."""
        return self._commands.get(name)

    def get_all(self) -> list[Command]:
        """Get all unique, non-hidden commands (including skills)."""
        seen = set()
        result = []
        for cmd in self._commands.values():
            if cmd.name not in seen and not cmd.hidden:
                seen.add(cmd.name)
                result.append(cmd)
        return sorted(result, key=lambda c: c.name)

    # --- Skill-specific queries ---

    def get_skills(self) -> list[PromptCommand]:
        """Return all prompt-type commands (skills)."""
        return [c for c in self.get_all() if isinstance(c, PromptCommand)]

    def get_user_invocable_skills(self) -> list[PromptCommand]:
        """Return skills that users can invoke via /skill-name."""
        return [c for c in self.get_skills() if c.user_invocable]

    def get_model_invocable_skills(self) -> list[PromptCommand]:
        """Return skills that the model can invoke via Skill tool."""
        return [c for c in self.get_skills() if c.model_invocable]

    def record_skill_usage(self, name: str) -> None:
        """Record a skill usage for frequency-based sorting."""
        self._skill_usage_counts[name] = self._skill_usage_counts.get(name, 0) + 1

    # --- Existing methods ---

    def get_completions(self, prefix: str) -> list[str]:
        """Auto-completion: return command names matching prefix"""
        return sorted(name for name in self._commands if name.startswith(prefix) and not self._commands[name].hidden)

    def fuzzy_search(self, query: str) -> list[FuzzyMatch]:
        """Fuzzy search commands. Returns matches sorted by priority then score.

        Priority order:
        0 - Exact name match
        1 - Name prefix match
        2 - Exact alias match
        3 - Alias prefix match
        4 - Subsequence match on name
        5 - Description keyword match
        """
        if not query:
            return [FuzzyMatch(command=cmd, name=cmd.name, priority=0, score=0) for cmd in self.get_all()]

        q = query.lower()
        matches: list[FuzzyMatch] = []

        for cmd in self.get_all():
            name_lower = cmd.name.lower()

            # Exact name match
            if name_lower == q:
                matches.append(FuzzyMatch(cmd, cmd.name, priority=0, score=0))
                continue

            # Name prefix match
            if name_lower.startswith(q):
                matches.append(FuzzyMatch(cmd, cmd.name, priority=1, score=len(cmd.name)))
                continue

            # Alias matches
            alias_matched = False
            for alias in cmd.aliases:
                alias_lower = alias.lower()
                if alias_lower == q:
                    matches.append(FuzzyMatch(cmd, alias, priority=2, score=0))
                    alias_matched = True
                    break
                if alias_lower.startswith(q):
                    matches.append(FuzzyMatch(cmd, alias, priority=3, score=len(alias)))
                    alias_matched = True
                    break
            if alias_matched:
                continue

            # Subsequence match on name
            sub_score = _subsequence_score(q, cmd.name)
            if sub_score is not None:
                matches.append(FuzzyMatch(cmd, cmd.name, priority=4, score=sub_score))
                continue

            # Description keyword match
            desc_lower = cmd.description.lower()
            if q in desc_lower:
                matches.append(FuzzyMatch(cmd, cmd.name, priority=5, score=desc_lower.index(q)))
                continue

        matches.sort(key=lambda m: (m.priority, m.score))
        return matches

    def get_best_prefix_match(self, partial: str) -> str | None:
        """Find the best prefix-matching command name for ghost text."""
        if not partial:
            return None
        q = partial.lower()
        for cmd in self.get_all():
            if cmd.name.lower().startswith(q):
                return cmd.name
        for cmd in self.get_all():
            for alias in cmd.aliases:
                if alias.lower().startswith(q):
                    return alias
        return None

    def is_command(self, text: str) -> bool:
        """Check if text is a command (``/``) or skill (``$``) invocation."""
        return text.startswith(("/", "$"))

    def parse(self, text: str) -> tuple[str, list[str]]:
        """Parse command text, return (command name, argument list).

        Accepts both the ``/`` trigger (commands + skills) and the ``$``
        trigger (skills only).
        """
        parts = text.lstrip("/$").split()
        name = parts[0] if parts else ""
        args = parts[1:] if len(parts) > 1 else []
        return name, args
