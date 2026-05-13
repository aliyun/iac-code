"""Suggestion system types."""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class CompletionToken:
    """A token extracted from user input that triggers suggestions."""

    text: str  # e.g. "/mod" or "@src/u"
    start: int  # start position in input
    end: int  # end position in input
    trigger: str  # "/" | "@" | "!"


@dataclass(slots=True)
class SuggestionItem:
    """A single suggestion shown in the overlay."""

    id: str  # e.g. "cmd:model", "file:src/ui/input.py"
    display_text: str
    completion: str  # full text after completion
    description: str
    icon: str  # "/" command, "+" file, "◇" directory, "↑" history
    source: str  # "command" | "file" | "directory" | "shell"
    score: float
    arg_hint: str | None = None  # inline ghost-text hint shown after the full command


class SuggestionProvider(ABC):
    """Base class for suggestion providers."""

    @property
    @abstractmethod
    def trigger(self) -> str:
        """The trigger character(s) for this provider."""
        ...

    @abstractmethod
    def provide(self, token: CompletionToken) -> list[SuggestionItem]:
        """Return suggestions for the given token."""
        ...
