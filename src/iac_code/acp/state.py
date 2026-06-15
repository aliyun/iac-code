"""TurnState / ToolCallState — track per-turn and per-tool-call state."""

from __future__ import annotations

import json
import time
from dataclasses import dataclass, field


@dataclass
class ToolCallState:
    """Track the streaming state of a single tool call."""

    tool_call_id: str
    tool_name: str
    accumulated_input: str = ""
    title: str = ""
    start_time: float = 0.0

    def __post_init__(self) -> None:
        if self.start_time == 0.0:
            self.start_time = time.monotonic()

    @property
    def elapsed_ms(self) -> int:
        """Milliseconds elapsed since the tool call started."""
        return int((time.monotonic() - self.start_time) * 1000)

    def update_input(self, delta: str) -> None:
        self.accumulated_input += delta
        self._update_title()

    def _update_title(self) -> None:
        """Compute a display title from tool name + streamed arguments."""
        subtitle = _extract_key_argument(self.tool_name, self.accumulated_input)
        title = display_tool_title(self.tool_name)
        self.title = f"{title}: {subtitle}" if subtitle else title


@dataclass
class TurnState:
    """Track all state within a single prompt turn."""

    turn_id: str
    tool_calls: dict[str, ToolCallState] = field(default_factory=dict)

    def start_tool_call(self, tool_call_id: str, tool_name: str) -> ToolCallState:
        state = ToolCallState(tool_call_id=tool_call_id, tool_name=tool_name)
        state.title = display_tool_title(tool_name)
        self.tool_calls[tool_call_id] = state
        return state

    def get_tool_call(self, tool_call_id: str) -> ToolCallState | None:
        return self.tool_calls.get(tool_call_id)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

# Map of tool name -> JSON key whose value makes a good subtitle.
_KEY_ARG_MAP: dict[str, str] = {
    "bash": "command",
    "read_file": "file_path",
    "write_file": "file_path",
    "edit_file": "file_path",
    "glob": "pattern",
    "grep": "pattern",
    "list_files": "path",
    "web_fetch": "url",
}

_SUBTITLE_MAX_LEN = 60


def display_tool_title(tool_name: str) -> str:
    from iac_code.pipeline.display_names import known_tool_display_name

    return known_tool_display_name(tool_name) or tool_name


def _extract_key_argument(tool_name: str, raw_json: str) -> str:
    """Best-effort extraction of a subtitle from partial/complete JSON args."""
    key = _KEY_ARG_MAP.get(tool_name)
    if not key:
        return ""
    try:
        obj = json.loads(raw_json)
        value = obj.get(key, "")
        if isinstance(value, str) and value:
            return value[:_SUBTITLE_MAX_LEN]
    except (json.JSONDecodeError, TypeError, AttributeError):
        # Partial JSON — fall back to naive substring search.
        marker = f'"{key}"'
        idx = raw_json.find(marker)
        if idx == -1:
            return ""
        # Skip past `"key": "` to grab value chars.
        rest = raw_json[idx + len(marker) :]
        rest = rest.lstrip(": ")
        if rest.startswith('"'):
            rest = rest[1:]
            end = rest.find('"')
            snippet = rest[:end] if end != -1 else rest
            return snippet[:_SUBTITLE_MAX_LEN]
    return ""
