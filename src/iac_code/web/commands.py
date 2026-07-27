"""Command dispatcher for the local Web workbench."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from iac_code.web.mcp_settings import list_mcp_servers
from iac_code.web.session_manager import WebSessionManager

PIPELINE_SLASH_COMMAND_ALLOWLIST = {
    "exit",
    "help",
    "status",
    "mcp",
    "prompt",
    "resume",
}


def command_metadata() -> list[dict[str, str]]:
    """Return command metadata for Web clients."""
    return [
        {"name": "/status", "description": "Show session status."},
        {"name": "/mcp", "description": "Show MCP server status."},
        {"name": "/help", "description": "Show command help."},
        {"name": "/?", "description": "Show command help."},
        {"name": "/prompt", "description": "Show the active prompt snapshot."},
        {"name": "/resume", "description": "Resume the current session."},
        {"name": "/compact", "description": "Compact the current conversation."},
        {"name": "/rename", "description": "Rename the current session."},
        {"name": "/clear", "description": "Clear visible Web state."},
        {"name": "/debug", "description": "Toggle debug mode."},
        {"name": "/model", "description": "Open model controls."},
        {"name": "/effort", "description": "Set reasoning effort."},
        {"name": "/auth", "description": "Open authentication controls."},
        {"name": "/login", "description": "Open login."},
        {"name": "/memory", "description": "Open memory controls."},
        {"name": "/memory-folder", "description": "Open the memory folder."},
        {"name": "/skills", "description": "Open skill controls."},
        {"name": "/exit", "description": "Close the session."},
        {"name": "/quit", "description": "Close the session."},
        {"name": "/q", "description": "Close the session."},
    ]


class WebCommandDispatcher:
    """Dispatch Web command text into browser-safe command results."""

    def __init__(self, manager: WebSessionManager) -> None:
        self.manager = manager

    def dispatch(self, session_id: str, command_text: str) -> dict[str, Any]:
        session = self.manager.get_session(session_id)
        if session is None:
            raise ValueError("session not found")

        stripped = command_text.strip()
        if session.mode == "pipeline" and stripped.startswith("!") and not session.allow_user_escapes.shell:
            return self._reject(
                "command_not_allowed_in_pipeline",
                "shell escape commands are not available in pipeline mode",
            )
        if session.mode == "pipeline" and stripped.startswith("$") and not session.allow_user_escapes.skill:
            return self._reject(
                "user_escape_not_allowed_in_pipeline",
                "user escape commands are not available in pipeline mode",
            )
        if stripped.startswith("!"):
            shell = stripped[1:].lstrip()
            if not shell:
                return self._reject("empty_shell_escape", "Usage: !<command>")
            return {
                "accepted": True,
                "command": "shell_escape",
                "local": True,
                "entersAgentContext": False,
                "shell": shell,
            }
        if stripped.startswith("$"):
            skill = stripped[1:].strip()
            if not skill:
                return self._reject("empty_skill_command", "Usage: $<skill> [args]")
            return {
                "accepted": True,
                "command": "skill",
                "skill": skill,
                "entersAgentContext": True,
            }
        if not stripped.startswith("/"):
            return self._reject("invalid_command", "command must start with /, !, or $")

        command, argument = self._parse_slash_command(stripped)
        if (
            session.mode == "pipeline"
            and command not in PIPELINE_SLASH_COMMAND_ALLOWLIST
            and not session.allow_user_escapes.command
        ):
            return self._reject(
                "command_not_allowed_in_pipeline",
                "/{} is not available in pipeline mode".format(command),
                command=command,
            )

        if command in {"help", "?"}:
            return {
                "accepted": True,
                "command": "help",
                "commands": command_metadata(),
            }
        if command == "status":
            return {
                "accepted": True,
                "command": "status",
                "status": self.manager.status(session),
            }
        if command == "mcp":
            return {
                "accepted": True,
                "command": "mcp",
                "mcp": list_mcp_servers(Path(session.cwd)),
            }
        if command == "clear":
            self.manager.clear_visible_state(session)
            return {"accepted": True, "command": "clear", "cleared": True}
        if command == "debug":
            debug_action = argument.strip().lower()
            if debug_action in {"", "status"}:
                return {
                    "accepted": True,
                    "command": "debug",
                    "enabled": session.debug_enabled,
                }
            if debug_action not in {"on", "off"}:
                return self._reject(
                    "invalid_debug_argument",
                    "Usage: /debug [on|off|status]",
                    command=command,
                )
            return {
                "accepted": True,
                "command": "debug",
                "enabled": self.manager.toggle_debug(session, enabled=debug_action == "on"),
            }
        if command == "compact":
            return {"accepted": True, "command": "compact", "action": "compact_session"}
        if command == "rename":
            title = argument.strip()
            if not title:
                return self._reject("invalid_session_name", "Usage: /rename <name>", command=command)
            try:
                self.manager.rename_session(session, title)
            except ValueError as exc:
                return self._reject("invalid_session_name", str(exc), command=command)
            return {
                "accepted": True,
                "command": "rename",
                "sessionId": session.session_id,
                "title": session.title,
                "action": "rename_session",
            }
        if command == "resume":
            return {"accepted": True, "command": "resume", "action": "resume"}
        if command == "prompt":
            return {
                "accepted": True,
                "command": "prompt",
                "action": "show_prompt_snapshot",
            }
        if command == "model":
            return {"accepted": True, "command": "model", "action": "open_model_selector"}
        if command == "effort":
            return {"accepted": True, "command": "effort", "action": "open_effort_selector"}
        if command == "auth":
            return {"accepted": True, "command": "auth", "action": "open_settings", "panel": "provider"}
        if command == "login":
            return {"accepted": True, "command": "login", "action": "open_settings", "panel": "provider"}
        if command == "memory":
            return {"accepted": True, "command": "memory", "action": "open_panel", "panel": "memory"}
        if command == "memory-folder":
            return {"accepted": True, "command": "memory-folder", "action": "open_panel", "panel": "memory"}
        if command == "skills":
            return {"accepted": True, "command": "skills", "action": "open_panel", "panel": "skills"}
        if command in {"exit", "quit", "q"}:
            return {"accepted": True, "command": command, "action": "close_session_runtime"}
        return self._reject("unknown_command", "unknown command: /{}".format(command), command=command)

    def _parse_slash_command(self, text: str) -> tuple[str, str]:
        command, _, argument = text[1:].partition(" ")
        return command.lower(), argument

    def _reject(self, code: str, message: str, *, command: str | None = None) -> dict[str, Any]:
        result: dict[str, Any] = {
            "accepted": False,
            "error": {
                "code": code,
                "message": message,
            },
        }
        if command is not None:
            result["command"] = command
        return result
