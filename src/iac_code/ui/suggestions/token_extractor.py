"""Token extractor for suggestion triggers."""

from __future__ import annotations

import re

from iac_code.ui.suggestions.types import CompletionToken

# Characters that can form part of a token
_TOKEN_CHARS = re.compile(r"[\w._\-/\\~@#!$]")


def _is_token_char(ch: str) -> bool:
    return bool(_TOKEN_CHARS.match(ch))


class TokenExtractor:
    """Extracts completion tokens from input text based on cursor position."""

    def extract(self, text: str, cursor_pos: int) -> CompletionToken | None:
        """Walk backwards from cursor_pos to find a completion token.

        Returns a CompletionToken if a valid trigger is found, else None.
        """
        if not text or cursor_pos == 0:
            return None

        # Clamp cursor_pos to valid range
        end = min(cursor_pos, len(text))

        slash_token = self._extract_slash_command(text, end)
        if slash_token is not None:
            return slash_token

        # Walk backwards to find start of token
        token_start = end
        while token_start > 0 and _is_token_char(text[token_start - 1]):
            token_start -= 1

        if token_start == end:
            # No token characters before cursor
            return None

        token_text = text[token_start:end]

        if not token_text:
            return None

        first_char = token_text[0]

        if first_char == "/":
            # "/" trigger: only valid at line start or after whitespace
            if token_start == 0 or text[token_start - 1] in (" ", "\t", "\n"):
                return CompletionToken(
                    text=token_text,
                    start=token_start,
                    end=end,
                    trigger="/",
                )
            return None

        if first_char == "$":
            # "$" trigger: skills only; same placement rule as "/".
            if token_start == 0 or text[token_start - 1] in (" ", "\t", "\n"):
                return CompletionToken(
                    text=token_text,
                    start=token_start,
                    end=end,
                    trigger="$",
                )
            return None

        if first_char == "@":
            return CompletionToken(
                text=token_text,
                start=token_start,
                end=end,
                trigger="@",
            )

        if first_char == "!":
            # "!" trigger: only valid at line start
            if token_start == 0:
                return CompletionToken(
                    text=token_text,
                    start=token_start,
                    end=end,
                    trigger="!",
                )
            return None

        return None

    @staticmethod
    def _extract_slash_command(text: str, end: int) -> CompletionToken | None:
        """Return a slash-command token spanning arguments on the current line."""
        line_start = text.rfind("\n", 0, end) + 1
        for index in range(line_start, end):
            if text[index] != "/":
                continue
            if index == 0 or text[index - 1] in (" ", "\t", "\n"):
                return CompletionToken(
                    text=text[index:end],
                    start=index,
                    end=end,
                    trigger="/",
                )
        return None
