"""Interactive MCP manager dialog."""

from __future__ import annotations

import asyncio
import inspect
import shutil
import subprocess
import sys
import threading
from collections.abc import Callable
from contextlib import suppress
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from rich.console import Console, Group, RenderableType
from rich.panel import Panel
from rich.text import Text

from iac_code.i18n import _
from iac_code.mcp import cli as mcp_cli
from iac_code.mcp.config import (
    approve_project_mcp_server,
    find_project_mcp_server_file,
    list_persisted_mcp_server_entries,
    load_all_persisted_mcp_configs,
    reject_project_mcp_server,
    resolve_mcp_workspace_root,
)
from iac_code.mcp.manager import (
    MCPHealthDiagnostic,
    health_diagnostic_for_config,
    mcp_status_metadata,
    mcp_warning_metadata,
)
from iac_code.mcp.oauth import safe_oauth_resource_metadata_url
from iac_code.mcp.redaction import sanitize_mcp_public_text, strip_mcp_terminal_control_sequences
from iac_code.mcp.types import MCPConnectionState
from iac_code.ui.core.key_event import KeyEvent
from iac_code.utils.log import current_log_file, is_debug_enabled

ActionHandler = Callable[..., Any]
MetadataProvider = Callable[[], dict[str, Any] | None]
ClipboardWriter = Callable[[str], bool]
_SELECT_PAGE_SIZE = 5
_PERSISTED_MCP_SCOPES = {"project", "local", "user"}
_MCP_DIALOG_ERROR_MAX_CHARS = 1000


@dataclass(frozen=True)
class _Action:
    key: str
    label: str


@dataclass
class _OAuthFlowState:
    name: str
    pending: Any
    scope: str | None = None
    source_path: str | None = None
    initial_server_state: str | None = None
    was_effectively_authenticated: bool = False
    callback_input: str = ""
    callback_cursor: int = 0
    status: str = ""
    url_copied: bool = False
    done: bool = False
    result: Any = None
    error: BaseException | None = None
    thread: threading.Thread | None = None
    lock: threading.Lock = field(default_factory=threading.Lock)


@dataclass
class _ActionFlowState:
    name: str
    action_key: str
    title: str
    detail: str
    done: bool = False
    result: Any = None
    error: BaseException | None = None
    thread: threading.Thread | None = None
    lock: threading.Lock = field(default_factory=threading.Lock)


class MCPManagerDialog:
    """Claude-style interactive MCP manager."""

    def __init__(
        self,
        context: Any,
        *,
        metadata_provider: MetadataProvider | None = None,
        actions: dict[str, ActionHandler] | None = None,
        clipboard_writer: ClipboardWriter | None = None,
    ) -> None:
        self._context = context
        self._metadata_provider = metadata_provider or (lambda: _status_metadata_for_context(context))
        self._actions = {**_default_actions(context), **(actions or {})}
        self._clipboard_writer = clipboard_writer or _copy_to_clipboard
        self._metadata: dict[str, Any] | None = None
        self._view = "list"
        self._focused_index = 0
        self._selected_server_key: tuple[str, str, str, str] | None = None
        self._selected_tool_index = 0
        self._selected_resource_index = 0
        self._selected_prompt_index = 0
        self._auth_flow: _OAuthFlowState | None = None
        self._action_flow: _ActionFlowState | None = None
        self._done = False
        self._result_message: str | None = None

    @property
    def result_message(self) -> str | None:
        return self._result_message

    def empty_message_if_no_servers(self) -> str | None:
        metadata = self._load_metadata()
        if _ordered_server_items(metadata) or metadata.get("warnings"):
            return None
        return _("No MCP servers configured. Run `iac-code mcp --help` to learn more.")

    def run(self) -> str | None:
        """Run the dialog in raw terminal mode."""
        if not sys.stdin.isatty() or not sys.stdout.isatty():
            return _("Interactive MCP manager requires a TTY. Use `iac-code mcp list` or MCP quick commands.")
        from iac_code.ui.core.in_place_render import InPlaceRenderer
        from iac_code.ui.core.raw_input import RawInputCapture

        renderer = InPlaceRenderer(_console_for_context(self._context))
        try:
            with RawInputCapture() as cap:
                while not self._done:
                    renderer.render(self.render())
                    key_event = cap.read_key(timeout=0.1)
                    if key_event is not None:
                        self.handle_key(key_event)
        finally:
            self._close_active_auth_flow()
            renderer.clear()
        return self._result_message

    def render(self) -> RenderableType:
        self._poll_auth_flow()
        self._poll_action_flow()
        if self._view == "auth":
            body = self._render_auth()
        elif self._view == "action":
            body = self._render_action()
        elif self._view == "server":
            body = self._render_server()
        elif self._view == "tools":
            body = self._render_tools()
        elif self._view == "tool":
            body = self._render_tool_detail()
        elif self._view == "resources":
            body = self._render_resources()
        elif self._view == "resource":
            body = self._render_resource_detail()
        elif self._view == "prompts":
            body = self._render_prompts()
        elif self._view == "prompt":
            body = self._render_prompt_detail()
        else:
            body = self._render_server_list()
        return Panel(body, title=Text(_("Manage MCP servers"), style="bold"), border_style="cyan")

    def handle_key(self, key_event: KeyEvent) -> bool:
        if self._view == "auth":
            return self._handle_auth_key(key_event)
        if self._view == "action":
            return False

        key = key_event.key
        ctrl = key_event.ctrl
        if key == "r" or (ctrl and key == "r"):
            self._metadata = None
            self._result_message = _("Refreshed MCP status.")
            self._focused_index = min(self._focused_index, max(0, self._focusable_count() - 1))
            return True
        select_like_view = self._view in {"server", "tools", "resources", "prompts"}
        if key == "up" or (select_like_view and ctrl and key == "p"):
            self._move_focus(-1)
            return True
        if key == "down" or (select_like_view and ctrl and key == "n"):
            self._move_focus(1)
            return True
        if select_like_view and not ctrl:
            if key == "pageup":
                self._move_focus_page(-1)
                return True
            if key == "pagedown":
                self._move_focus_page(1)
                return True
            if key == "k" and key_event.char == "k":
                self._move_focus(-1)
                return True
            if key == "j" and key_event.char == "j":
                self._move_focus(1)
                return True
            if key_event.char and key_event.char.isdigit():
                index = int(key_event.char) - 1
                if 0 <= index < self._focusable_count():
                    self._focused_index = index
                    self._activate_focused_item()
                return True
        if key == "enter":
            self._activate_focused_item()
            return True
        if key == "escape":
            self._go_back_or_close()
            return True
        return False

    def _render_server_list(self) -> RenderableType:
        metadata = self._load_metadata()
        servers = _ordered_server_items(metadata)
        diagnostics = _diagnostic_renderables(metadata.get("warnings", []))
        if not servers:
            return Group(
                *diagnostics,
                Text(_("No MCP servers configured.")),
                Text(_("Run `iac-code mcp --help` to add or inspect MCP servers.")),
                _footer(_("Esc close")),
            )

        lines: list[RenderableType] = [*diagnostics, Text(_server_count_text(len(servers)), style="dim"), Text("")]
        focus_position = 0
        for scope, grouped_servers in _servers_grouped_by_scope(servers):
            lines.append(Text(_scope_heading_with_path(scope, grouped_servers), style="bold"))
            for server in grouped_servers:
                marker = ">" if focus_position == self._focused_index else " "
                name = _server_list_label(server, grouped_servers)
                state = _status_display(server, include_icon=True)
                lines.append(
                    Text(
                        "  {marker} {name} · {state}".format(
                            marker=marker,
                            name=name,
                            state=state,
                        )
                    )
                )
                focus_position += 1
            lines.append(Text(""))

        if self._result_message:
            lines.append(Text(""))
            lines.append(Text(self._result_message, style="green"))
        if any(_state_value(server) == "failed" for server in servers):
            lines.append(Text(_failed_server_debug_hint(), style="dim"))
        lines.append(Text(_("iac-code mcp --help for help"), style="dim"))
        lines.append(_footer(_("Enter select"), _("Esc close"), _("Up/Down navigate")))
        return Group(*lines)

    def _render_server(self) -> RenderableType:
        server = self._selected_server()
        if server is None:
            self._view = "list"
            return self._render_server_list()

        lines: list[RenderableType] = [
            Text(_server_title(server), style="bold"),
            Text(""),
            *_summary_lines(server),
            Text(""),
            Text(_("Actions"), style="bold"),
        ]
        actions = self._server_actions(server)
        index_width = len(str(len(actions)))
        for index, action in enumerate(actions):
            marker = ">" if index == self._focused_index else " "
            index_label = "{index}.".format(index=index + 1).ljust(index_width + 2)
            lines.append(
                Text(
                    "  {marker} {index}{label}".format(
                        marker=marker,
                        index=index_label,
                        label=action.label,
                    )
                )
            )
        if self._result_message:
            lines.append(Text(""))
            lines.append(Text(self._result_message, style="green"))
        lines.append(Text(""))
        lines.append(_footer(_("Enter select"), _("Esc back"), _("Up/Down navigate")))
        return Group(*lines)

    def _render_tools(self) -> RenderableType:
        server = self._selected_server()
        if server is None:
            self._view = "list"
            return self._render_server_list()

        tools = _tools_for_server(server)
        lines: list[RenderableType] = [
            Text(_("Tools for {name}").format(name=server.get("serverName")), style="bold"),
            Text(_tool_count_text(len(tools)), style="dim"),
        ]
        if not tools:
            lines.append(Text(""))
            lines.append(Text(_("No tools available")))
        start, end = _visible_select_range(len(tools), self._focused_index)
        for index in range(start, end):
            tool = tools[index]
            marker = ">" if index == self._focused_index else " "
            index_label = "{index}.".format(index=index + 1).ljust(len(str(len(tools))) + 2)
            display_name = _tool_display_name(tool)
            annotations = _annotation_display(tool.get("annotations"))
            suffix = "  {annotations}".format(annotations=annotations) if annotations else ""
            scroll_hint = _select_scroll_hint(index, start=start, end=end, count=len(tools))
            lines.append(
                Text(
                    "  {marker} {index}{display_name}{suffix}".format(
                        marker=marker,
                        index=index_label,
                        display_name=display_name,
                        suffix="{suffix}{scroll_hint}".format(suffix=suffix, scroll_hint=scroll_hint),
                    )
                )
            )
        lines.append(Text(""))
        lines.append(_footer(_("Enter select"), _("Esc back"), _("Up/Down navigate")))
        return Group(*lines)

    def _render_tool_detail(self) -> RenderableType:
        server = self._selected_server()
        if server is None:
            self._view = "list"
            return self._render_server_list()
        tool = self._selected_tool(server)
        if tool is None:
            self._view = "tools"
            return self._render_tools()

        annotations = _annotation_display(tool.get("annotations"))
        annotation_badges = _annotation_badges(annotations)
        title = _tool_display_name(tool)
        if annotation_badges:
            title = "{title} {badges}".format(title=title, badges=annotation_badges)
        lines: list[RenderableType] = [
            Text(title, style="bold"),
            Text(""),
            Text(_("Tool name: {name}").format(name=_tool_display_name(tool))),
            Text(_("Full name: {name}").format(name=_tool_full_name(tool))),
        ]
        description = _display_text(tool.get("description"))
        if description:
            lines.extend([Text(""), Text(_("Description:"), style="bold"), Text(description)])
        schema = tool.get("inputSchema") or tool.get("input_schema")
        if isinstance(schema, dict) and schema:
            parameter_lines = _schema_parameter_lines(schema)
            if parameter_lines:
                lines.extend([Text(""), Text(_("Parameters:"), style="bold"), *parameter_lines])
        lines.append(Text(""))
        lines.append(_footer(_("Esc back")))
        return Group(*lines)

    def _render_resources(self) -> RenderableType:
        server = self._selected_server()
        if server is None:
            self._view = "list"
            return self._render_server_list()

        resources = _resources_for_server(server)
        lines: list[RenderableType] = [
            Text(_("Resources for {name}").format(name=server.get("serverName")), style="bold"),
            Text(_resource_count_text(len(resources)), style="dim"),
        ]
        if not resources:
            lines.append(Text(""))
            lines.append(Text(_("No resources available")))
        start, end = _visible_select_range(len(resources), self._focused_index)
        for index in range(start, end):
            resource = resources[index]
            marker = ">" if index == self._focused_index else " "
            index_label = "{index}.".format(index=index + 1).ljust(len(str(len(resources))) + 2)
            display_name = _resource_display_name(resource)
            uri = _resource_uri(resource)
            suffix = "  {uri}".format(uri=uri) if uri and uri != display_name else ""
            scroll_hint = _select_scroll_hint(index, start=start, end=end, count=len(resources))
            lines.append(
                Text(
                    "  {marker} {index}{display_name}{suffix}{scroll_hint}".format(
                        marker=marker,
                        index=index_label,
                        display_name=display_name,
                        suffix=suffix,
                        scroll_hint=scroll_hint,
                    )
                )
            )
        lines.append(Text(""))
        lines.append(_footer(_("Enter select"), _("Esc back"), _("Up/Down navigate")))
        return Group(*lines)

    def _render_resource_detail(self) -> RenderableType:
        server = self._selected_server()
        if server is None:
            self._view = "list"
            return self._render_server_list()
        resource = self._selected_resource(server)
        if resource is None:
            self._view = "resources"
            return self._render_resources()

        lines: list[RenderableType] = [
            Text(_resource_display_name(resource), style="bold"),
            Text(""),
            Text(_("URI: {uri}").format(uri=_resource_uri(resource))),
        ]
        name = _resource_name(resource)
        if name:
            lines.append(Text(_("Name: {name}").format(name=name)))
        mime_type = _resource_mime_type(resource)
        if mime_type:
            lines.append(Text(_("MIME type: {mime_type}").format(mime_type=mime_type)))
        description = _display_text(resource.get("description"))
        if description:
            lines.extend([Text(""), Text(_("Description:"), style="bold"), Text(description)])
        lines.append(Text(""))
        lines.append(_footer(_("Esc back")))
        return Group(*lines)

    def _render_prompts(self) -> RenderableType:
        server = self._selected_server()
        if server is None:
            self._view = "list"
            return self._render_server_list()

        prompts = _prompts_for_server(server)
        lines: list[RenderableType] = [
            Text(_("Prompts for {name}").format(name=server.get("serverName")), style="bold"),
            Text(_prompt_count_text(len(prompts)), style="dim"),
        ]
        if not prompts:
            lines.append(Text(""))
            lines.append(Text(_("No prompts available")))
        start, end = _visible_select_range(len(prompts), self._focused_index)
        for index in range(start, end):
            prompt = prompts[index]
            marker = ">" if index == self._focused_index else " "
            index_label = "{index}.".format(index=index + 1).ljust(len(str(len(prompts))) + 2)
            scroll_hint = _select_scroll_hint(index, start=start, end=end, count=len(prompts))
            lines.append(
                Text(
                    "  {marker} {index}{display_name}{scroll_hint}".format(
                        marker=marker,
                        index=index_label,
                        display_name=_prompt_display_name(prompt),
                        scroll_hint=scroll_hint,
                    )
                )
            )
        lines.append(Text(""))
        lines.append(_footer(_("Enter select"), _("Esc back"), _("Up/Down navigate")))
        return Group(*lines)

    def _render_prompt_detail(self) -> RenderableType:
        server = self._selected_server()
        if server is None:
            self._view = "list"
            return self._render_server_list()
        prompt = self._selected_prompt(server)
        if prompt is None:
            self._view = "prompts"
            return self._render_prompts()

        lines: list[RenderableType] = [
            Text(_prompt_display_name(prompt), style="bold"),
            Text(""),
            Text(_("Prompt name: {name}").format(name=_prompt_display_name(prompt))),
            Text(_("Full name: {name}").format(name=_prompt_full_name(prompt))),
        ]
        description = _display_text(prompt.get("description"))
        if description:
            lines.extend([Text(""), Text(_("Description:"), style="bold"), Text(description)])
        argument_lines = _prompt_argument_lines(prompt.get("arguments"))
        if argument_lines:
            lines.extend([Text(""), Text(_("Arguments:"), style="bold"), *argument_lines])
        lines.append(Text(""))
        lines.append(_footer(_("Esc back")))
        return Group(*lines)

    def _render_auth(self) -> RenderableType:
        flow = self._auth_flow
        if flow is None:
            self._view = "server"
            return self._render_server()

        authorization_url = str(getattr(flow.pending, "authorization_url", "") or "")
        browser_opened = _("yes") if bool(getattr(flow.pending, "browser_opened", False)) else _("no")
        lines: list[RenderableType] = [
            Text(_("Authenticating {name}").format(name=flow.name), style="bold"),
            Text(""),
            Text(_("Browser opened: {status}").format(status=browser_opened)),
        ]
        if authorization_url:
            copy_hint = _("Copied!") if flow.url_copied else _("press c to copy")
            lines.extend(
                [
                    Text(
                        _("If your browser does not open automatically, copy this URL manually ({hint}).").format(
                            hint=copy_hint
                        )
                    ),
                    Text(authorization_url),
                ]
            )
        lines.extend(
            [
                Text(""),
                Text(flow.status or _("Waiting for OAuth callback.")),
                Text(_("If the redirect page shows a connection error, paste the URL from your browser address bar.")),
            ]
        )
        lines.extend([Text(""), Text(_("URL > {value}").format(value=_callback_input_display(flow)))])
        lines.append(Text(""))
        lines.append(_footer(_("c copy URL"), _("Enter submit"), _("Esc cancel")))
        return Group(*lines)

    def _render_action(self) -> RenderableType:
        flow = self._action_flow
        if flow is None:
            self._view = "server"
            return self._render_server()
        return Group(
            Text(flow.title, style="bold"),
            Text(""),
            Text(flow.detail),
            Text(_("This may take a few moments."), style="dim"),
        )

    def _move_focus(self, direction: int) -> None:
        count = self._focusable_count()
        if count <= 0:
            self._focused_index = 0
            return
        self._focused_index = (self._focused_index + direction) % count

    def _move_focus_page(self, direction: int) -> None:
        count = self._focusable_count()
        if count <= 0:
            self._focused_index = 0
            return
        target = self._focused_index + (direction * _SELECT_PAGE_SIZE)
        self._focused_index = max(0, min(count - 1, target))

    def _activate_focused_item(self) -> None:
        if self._view == "list":
            servers = _ordered_server_items(self._load_metadata())
            if not servers:
                return
            self._selected_server_key = _server_key(servers[self._focused_index])
            self._view = "server"
            self._focused_index = 0
            return

        if self._view == "server":
            server = self._selected_server()
            if server is None:
                return
            actions = self._server_actions(server)
            if not actions:
                return
            action = actions[self._focused_index]
            self._perform_server_action(server, action)
            return

        if self._view == "tools":
            server = self._selected_server()
            if server is None:
                return
            tools = _tools_for_server(server)
            if not tools:
                return
            self._selected_tool_index = self._focused_index
            self._view = "tool"
            self._focused_index = 0
            return

        if self._view == "resources":
            server = self._selected_server()
            if server is None:
                return
            resources = _resources_for_server(server)
            if not resources:
                return
            self._selected_resource_index = self._focused_index
            self._view = "resource"
            self._focused_index = 0
            return

        if self._view == "prompts":
            server = self._selected_server()
            if server is None:
                return
            prompts = _prompts_for_server(server)
            if not prompts:
                return
            self._selected_prompt_index = self._focused_index
            self._view = "prompt"
            self._focused_index = 0

    def _go_back_or_close(self) -> None:
        if self._view == "list":
            self._result_message = _("MCP dialog dismissed")
            self._done = True
            return
        if self._view == "tool":
            self._view = "tools"
            self._focused_index = min(self._selected_tool_index, max(0, self._focusable_count() - 1))
            return
        if self._view == "resource":
            self._view = "resources"
            self._focused_index = min(self._selected_resource_index, max(0, self._focusable_count() - 1))
            return
        if self._view == "prompt":
            self._view = "prompts"
            self._focused_index = min(self._selected_prompt_index, max(0, self._focusable_count() - 1))
            return
        self._view = "list" if self._view == "server" else "server"
        self._focused_index = 0

    def _perform_server_action(self, server: dict[str, Any], action: _Action) -> None:
        name = str(server.get("serverName") or "")
        scope = str(server.get("scope") or "") or None
        source_path = _server_action_source_path(server)
        if action.key == "view-tools":
            self._view = "tools"
            self._focused_index = 0
            return
        if action.key == "view-resources":
            self._view = "resources"
            self._focused_index = 0
            return
        if action.key == "view-prompts":
            self._view = "prompts"
            self._focused_index = 0
            return
        if action.key == "back":
            self._view = "list"
            self._focused_index = 0
            return
        if action.key == "reconnect":
            self._begin_action_flow(server, action)
            return

        closed_live_runtime = False
        try:
            handler = self._actions[action.key]
            if action.key in {"clear-auth", "remove"}:
                closed_live_runtime = self._close_live_runtime()
            if action.key in {"auth", "reauth"}:
                result = _run_maybe_await(
                    _call_action_handler(
                        handler,
                        name,
                        scope,
                        source_path=source_path,
                        required_scopes=self._required_auth_scopes(server),
                        resource_metadata_url=self._required_auth_resource_metadata_url(server),
                    )
                )
            else:
                result = _run_maybe_await(_call_action_handler(handler, name, scope, source_path=source_path))
            if _is_pending_oauth_flow(result):
                self._begin_auth_flow(
                    name,
                    result,
                    scope=scope,
                    source_path=source_path,
                    initial_state=_state_value(server),
                )
                return
            if action.key in {"auth", "reauth"}:
                self._refresh_live_runtime()
                self._metadata = None
                self._result_message = _post_auth_result_message(
                    self._context,
                    name,
                    scope=scope,
                    source_path=source_path,
                    initial_state=_state_value(server),
                    reconnected=_is_effectively_authenticated(server),
                )
            elif action.key == "clear-auth":
                self._result_message = str(result) if result is not None else _action_success_message(action.key, name)
            elif action.key in {"enable", "disable"}:
                self._result_message = None
            else:
                self._result_message = str(result) if result is not None else _action_success_message(action.key, name)
            if action.key not in {"auth", "reauth", "reconnect"}:
                self._refresh_live_runtime()
            self._metadata = None
            if action.key in {"enable", "disable"}:
                self._view = "list"
                self._focused_index = 0
            elif action.key in {"approve", "reject"}:
                self._view = "list"
                self._focused_index = 0
            elif action.key == "remove":
                self._done = True
            elif action.key in {"auth", "reauth", "clear-auth", "reconnect"}:
                self._done = True
            else:
                self._focused_index = min(self._focused_index, max(0, self._focusable_count() - 1))
        except Exception as exc:
            if closed_live_runtime:
                self._refresh_live_runtime()
            error = _public_error_detail(exc)
            if action.key in {"auth", "reauth"}:
                self._result_message = _("Error: {error}").format(error=error)
            elif action.key in {"enable", "disable"}:
                self._result_message = _("Failed to {action} MCP server {name!r}: {error}").format(
                    action=action.key,
                    name=name,
                    error=error,
                )
                self._done = True
            else:
                self._result_message = error

    def _server_actions(self, server: dict[str, Any]) -> list[_Action]:
        actions: list[_Action] = []
        needs_auth = _server_needs_auth(server)
        tools = _tools_for_server(server)
        resources = _resources_for_server(server)
        prompts = _prompts_for_server(server)
        state = _state_value(server)
        persisted = _has_persisted_scope(server)
        if state == "disabled":
            if not persisted:
                return actions
            actions.append(_Action("enable", _("Enable")))
            if not _is_stdio_transport(server):
                if _is_effectively_authenticated(server):
                    actions.append(_Action("reauth", _("Re-authenticate")))
                    actions.append(_Action("clear-auth", _("Clear authentication")))
                else:
                    actions.append(_Action("auth", _("Authenticate")))
            if _can_remove_server(server):
                actions.append(_Action("remove", _("Remove")))
            return actions

        if state == "pending-approval":
            if persisted:
                actions.append(_Action("approve", _("Approve")))
                actions.append(_Action("reject", _("Reject")))
                actions.append(_Action("disable", _("Disable")))
                if _can_remove_server(server):
                    actions.append(_Action("remove", _("Remove")))
            return actions

        if state in {"missing-env", "invalid-config"}:
            if persisted:
                actions.append(_Action("disable", _("Disable")))
                if _can_remove_server(server):
                    actions.append(_Action("remove", _("Remove")))
            return actions

        if _is_stdio_transport(server):
            if tools:
                actions.append(_Action("view-tools", _("View tools")))
            if resources:
                actions.append(_Action("view-resources", _("View resources")))
            if prompts:
                actions.append(_Action("view-prompts", _("View prompts")))
            if persisted:
                actions.append(_Action("reconnect", _("Reconnect")))
                actions.append(_Action("disable", _("Disable")))
                if _can_remove_server(server):
                    actions.append(_Action("remove", _("Remove")))
            return actions

        if state == "connected" and tools:
            actions.append(_Action("view-tools", _("View tools")))
        if state == "connected" and resources:
            actions.append(_Action("view-resources", _("View resources")))
        if state == "connected" and prompts:
            actions.append(_Action("view-prompts", _("View prompts")))
        if persisted:
            if _is_effectively_authenticated(server):
                actions.append(_Action("reauth", _("Re-authenticate")))
                actions.append(_Action("clear-auth", _("Clear authentication")))
            else:
                actions.append(_Action("auth", _("Authenticate")))
            if not needs_auth:
                actions.append(_Action("reconnect", _("Reconnect")))
            actions.append(_Action("disable", _("Disable")))
            if _can_remove_server(server):
                actions.append(_Action("remove", _("Remove")))
        return actions

    def _focusable_count(self) -> int:
        if self._view == "list":
            return len(_ordered_server_items(self._load_metadata()))
        if self._view == "server":
            server = self._selected_server()
            return len(self._server_actions(server)) if server is not None else 0
        if self._view == "tools":
            server = self._selected_server()
            return len(_tools_for_server(server)) if server is not None else 0
        if self._view == "resources":
            server = self._selected_server()
            return len(_resources_for_server(server)) if server is not None else 0
        if self._view == "prompts":
            server = self._selected_server()
            return len(_prompts_for_server(server)) if server is not None else 0
        return 0

    def _selected_server(self) -> dict[str, Any] | None:
        selected = self._selected_server_key
        if selected is None:
            return None
        for server in _ordered_server_items(self._load_metadata()):
            if _server_key(server) == selected:
                return server
        return None

    def _selected_tool(self, server: dict[str, Any]) -> dict[str, Any] | None:
        tools = _tools_for_server(server)
        if not tools:
            return None
        index = max(0, min(len(tools) - 1, self._selected_tool_index))
        return tools[index]

    def _selected_resource(self, server: dict[str, Any]) -> dict[str, Any] | None:
        resources = _resources_for_server(server)
        if not resources:
            return None
        index = max(0, min(len(resources) - 1, self._selected_resource_index))
        return resources[index]

    def _selected_prompt(self, server: dict[str, Any]) -> dict[str, Any] | None:
        prompts = _prompts_for_server(server)
        if not prompts:
            return None
        index = max(0, min(len(prompts) - 1, self._selected_prompt_index))
        return prompts[index]

    def _load_metadata(self) -> dict[str, Any]:
        if self._metadata is None:
            self._metadata = self._metadata_provider() or {"servers": [], "warnings": []}
        return self._metadata

    def _required_auth_scopes(self, server: dict[str, Any]) -> list[str]:
        name = str(server.get("serverName") or "")
        manager = self._matching_live_manager(server)
        required = getattr(manager, "required_auth_scopes", None) if manager is not None else None
        if callable(required):
            scopes = [str(scope) for scope in required(name) if str(scope)]
            if scopes:
                return scopes
        return _required_auth_scopes_from_metadata(server)

    def _required_auth_resource_metadata_url(self, server: dict[str, Any]) -> str | None:
        name = str(server.get("serverName") or "")
        manager = self._matching_live_manager(server)
        required = getattr(manager, "required_auth_resource_metadata_url", None) if manager is not None else None
        if callable(required):
            value = safe_oauth_resource_metadata_url(required(name))
            if value:
                return value
        return safe_oauth_resource_metadata_url(_metadata_string(server.get("authResourceMetadataUrl")))

    def _matching_live_manager(self, server: dict[str, Any]) -> Any:
        name = str(server.get("serverName") or "")
        scope = str(server.get("scope") or "") or None
        source_path = _server_action_source_path(server)
        manager = _live_mcp_manager(self._context)
        if manager is not None and _live_mcp_server_matches(
            manager,
            name,
            scope,
            source_path=source_path,
            cwd=_mcp_command_cwd(self._context),
        ):
            return manager
        return None

    def _refresh_live_runtime(self) -> None:
        repl = getattr(self._context, "repl", None)
        refresh = getattr(repl, "refresh_mcp_integrations", None) if repl is not None else None
        if callable(refresh):
            _run_maybe_await(refresh())

    def _close_live_runtime(self) -> bool:
        repl = getattr(self._context, "repl", None)
        close = getattr(repl, "_close_mcp_manager", None) if repl is not None else None
        if not callable(close):
            return False
        _run_maybe_await(close())
        return True

    def _begin_auth_flow(
        self,
        name: str,
        pending: Any,
        *,
        scope: str | None,
        source_path: str | None,
        initial_state: str | None,
    ) -> None:
        selected_server = self._selected_server()
        flow = _OAuthFlowState(
            name=name,
            pending=pending,
            scope=scope,
            source_path=source_path,
            initial_server_state=initial_state,
            was_effectively_authenticated=(
                _is_effectively_authenticated(selected_server) if selected_server is not None else False
            ),
            status=_("Waiting for OAuth callback."),
        )
        self._auth_flow = flow
        self._view = "auth"
        self._result_message = None
        self._start_auth_wait(flow)

    def _start_auth_wait(self, flow: _OAuthFlowState) -> None:
        if flow.thread is not None and flow.thread.is_alive():
            return
        flow.status = _("Waiting for OAuth callback.")

        def worker() -> None:
            try:
                result = flow.pending.wait()
            except BaseException as exc:
                with suppress(BaseException):
                    mcp_cli.cancel_pending_mcp_oauth_flow(flow.pending)
                _finish_auth_flow(flow, error=exc)
            else:
                _finish_auth_flow(flow, result=result)

        flow.thread = threading.Thread(target=worker, daemon=True)
        flow.thread.start()

    def _complete_auth_manually(self, flow: _OAuthFlowState) -> None:
        value = flow.callback_input.strip()
        if not value:
            self._start_auth_wait(flow)
            return
        flow.callback_input = ""
        flow.callback_cursor = 0
        flow.status = _("Completing OAuth authorization.")
        wait_started = flow.thread is not None

        def worker() -> None:
            try:
                result = _complete_pending_oauth_flow(flow.pending, value, wait_started=wait_started)
            except BaseException as exc:
                if _is_recoverable_manual_oauth_input_error(exc):
                    flow.status = str(exc) or _("OAuth callback did not include a code.")
                    return
                with suppress(BaseException):
                    mcp_cli.cancel_pending_mcp_oauth_flow(flow.pending)
                _finish_auth_flow(flow, error=exc)
            else:
                if result is not None:
                    _finish_auth_flow(flow, result=result)

        threading.Thread(target=worker, daemon=True).start()

    def _handle_auth_key(self, key_event: KeyEvent) -> bool:
        flow = self._auth_flow
        if flow is None:
            self._view = "server"
            return True
        key = key_event.key
        if key == "escape":
            self._cancel_auth_flow(flow)
            return True
        if key == "c" and not key_event.ctrl:
            self._copy_auth_url(flow)
            return True
        if key == "y" and key_event.ctrl:
            self._copy_auth_url(flow)
            return True
        if key == "enter":
            self._complete_auth_manually(flow)
            self._poll_auth_flow()
            return True
        if key == "left":
            flow.callback_cursor = max(0, flow.callback_cursor - 1)
            return True
        if key == "right":
            flow.callback_cursor = min(len(flow.callback_input), flow.callback_cursor + 1)
            return True
        if key == "home" or (key == "a" and key_event.ctrl):
            flow.callback_cursor = 0
            return True
        if key == "end" or (key == "e" and key_event.ctrl):
            flow.callback_cursor = len(flow.callback_input)
            return True
        if key == "backspace":
            if flow.callback_cursor > 0:
                flow.callback_input = (
                    flow.callback_input[: flow.callback_cursor - 1] + flow.callback_input[flow.callback_cursor :]
                )
                flow.callback_cursor -= 1
            return True
        if key == "delete":
            if flow.callback_cursor < len(flow.callback_input):
                flow.callback_input = (
                    flow.callback_input[: flow.callback_cursor] + flow.callback_input[flow.callback_cursor + 1 :]
                )
            return True
        if key == "paste":
            _insert_auth_input(flow, key_event.char.strip())
            return True
        if key_event.char and len(key_event.char) == 1 and not key_event.ctrl:
            _insert_auth_input(flow, key_event.char)
            return True
        return False

    def _copy_auth_url(self, flow: _OAuthFlowState) -> None:
        authorization_url = str(getattr(flow.pending, "authorization_url", "") or "")
        if not authorization_url:
            flow.status = _("No authorization URL is available to copy.")
            return
        if self._clipboard_writer(authorization_url):
            flow.url_copied = True
            flow.status = _("Copied authorization URL.")
            return
        flow.status = _("Could not copy authorization URL. Copy it manually from the text above.")

    def _cancel_auth_flow(self, flow: _OAuthFlowState) -> None:
        with suppress(BaseException):
            mcp_cli.cancel_pending_mcp_oauth_flow(flow.pending)
        self._auth_flow = None
        self._view = "server" if self._selected_server() is not None else "list"
        self._focused_index = 0
        self._result_message = None

    def _close_active_auth_flow(self) -> None:
        flow = self._auth_flow
        if flow is None:
            return
        with flow.lock:
            done = flow.done
        if not done:
            with suppress(BaseException):
                mcp_cli.cancel_pending_mcp_oauth_flow(flow.pending)
        self._auth_flow = None

    def _begin_action_flow(self, server: dict[str, Any], action: _Action) -> None:
        name = str(server.get("serverName") or "")
        scope = str(server.get("scope") or "") or None
        source_path = _server_action_source_path(server)
        if _is_stdio_transport(server):
            title = _("Reconnecting to {name}").format(name=name)
            detail = _("Restarting MCP server process")
        else:
            title = _("Connecting to {name}…").format(name=name)
            detail = _("Establishing connection to MCP server")
        flow = _ActionFlowState(name=name, action_key=action.key, title=title, detail=detail)
        self._action_flow = flow
        self._view = "action"
        self._result_message = None

        def worker() -> None:
            try:
                result = _run_maybe_await(
                    _call_action_handler(self._actions[action.key], name, scope, source_path=source_path)
                )
            except BaseException as exc:
                _finish_action_flow(flow, error=exc)
            else:
                _finish_action_flow(flow, result=result)

        flow.thread = threading.Thread(target=worker, daemon=True)
        flow.thread.start()

    def _poll_action_flow(self) -> None:
        flow = self._action_flow
        if flow is None:
            return
        with flow.lock:
            if not flow.done:
                return
            error = flow.error
            result = flow.result
        self._action_flow = None
        self._metadata = None
        if error is not None:
            self._result_message = _public_error_detail(error)
        else:
            self._result_message = (
                str(result) if result is not None else _action_success_message(flow.action_key, flow.name)
            )
        if flow.action_key == "reconnect":
            self._done = True
        else:
            self._view = "server"

    def _poll_auth_flow(self) -> None:
        flow = self._auth_flow
        if flow is None:
            return
        with flow.lock:
            if not flow.done:
                return
            error = flow.error
        self._auth_flow = None
        self._view = "server" if self._selected_server() is not None else "list"
        self._focused_index = 0
        if error is not None:
            message = _public_error_detail(error)
            self._result_message = _("Error: {error}").format(error=message)
            return
        self._refresh_live_runtime()
        self._metadata = None
        self._result_message = _post_auth_result_message(
            self._context,
            flow.name,
            scope=flow.scope,
            source_path=flow.source_path,
            initial_state=flow.initial_server_state,
            reconnected=flow.was_effectively_authenticated,
        )
        self._done = True


def _default_actions(context: Any) -> dict[str, ActionHandler]:
    command_cwd = _mcp_command_cwd(context)

    def auth(
        name: str,
        scope: str | None,
        required_scopes: list[str] | None = None,
        resource_metadata_url: str | None = None,
        source_path: str | None = None,
    ):
        return mcp_cli.start_mcp_oauth_flow(
            name,
            scope=scope,
            source_path=source_path,
            required_scopes=required_scopes,
            resource_metadata_url=resource_metadata_url,
            cwd=command_cwd,
        )

    def reauth(
        name: str,
        scope: str | None,
        required_scopes: list[str] | None = None,
        resource_metadata_url: str | None = None,
        source_path: str | None = None,
    ):
        return mcp_cli.reauthenticate_mcp_server(
            name,
            scope=scope,
            source_path=source_path,
            required_scopes=required_scopes,
            resource_metadata_url=resource_metadata_url,
            cwd=command_cwd,
        )

    return {
        "auth": auth,
        "reauth": reauth,
        "clear-auth": lambda name, scope, source_path=None: mcp_cli.reset_mcp_auth_server_command(
            name,
            scope=scope,
            source_path=source_path,
            cwd=command_cwd,
        ),
        "reconnect": lambda name, scope, source_path=None: _reconnect_mcp_server_action(
            context,
            name,
            scope,
            source_path=source_path,
        ),
        "disable": lambda name, scope, source_path=None: mcp_cli.disable_mcp_server_command(
            name,
            scope=scope,
            source_path=source_path,
            cwd=command_cwd,
        ),
        "enable": lambda name, scope, source_path=None: mcp_cli.enable_mcp_server_command(
            name,
            scope=scope,
            source_path=source_path,
            cwd=command_cwd,
        ),
        "remove": lambda name, scope, source_path=None: mcp_cli.remove_mcp_server_command(
            name,
            scope=scope,
            source_path=source_path,
            cwd=command_cwd,
        ),
        "approve": lambda name, scope, source_path=None: _set_project_approval_choice(
            context,
            name,
            approved=True,
            source_path=source_path,
        ),
        "reject": lambda name, scope, source_path=None: _set_project_approval_choice(
            context,
            name,
            approved=False,
            source_path=source_path,
        ),
    }


def _set_project_approval_choice(
    context: Any,
    name: str,
    *,
    approved: bool,
    source_path: str | None = None,
) -> str:
    cwd = _mcp_command_cwd(context)
    workspace_root = resolve_mcp_workspace_root(cwd)
    project_file = (
        Path(source_path)
        if source_path
        else find_project_mcp_server_file(
            name,
            cwd=cwd,
            workspace_root=workspace_root,
        )
    )
    if project_file is None:
        raise RuntimeError(_("MCP server {name!r} not found in project config.").format(name=name))
    if approved:
        approve_project_mcp_server(name, project_file=project_file, workspace_root=workspace_root)
        return _("Approved MCP server {name!r}.").format(name=name)
    reject_project_mcp_server(name, project_file=project_file, workspace_root=workspace_root)
    return _("Rejected MCP server {name!r}.").format(name=name)


def _call_action_handler(
    handler: ActionHandler,
    name: str,
    scope: str | None,
    *,
    source_path: str | None = None,
    **kwargs: Any,
) -> Any:
    call_kwargs = dict(kwargs)
    if source_path and _callable_accepts_keyword(handler, "source_path"):
        call_kwargs["source_path"] = source_path
    return handler(name, scope, **call_kwargs)


def _callable_accepts_keyword(handler: ActionHandler, keyword: str) -> bool:
    try:
        signature = inspect.signature(handler)
    except (TypeError, ValueError):
        return False
    for parameter in signature.parameters.values():
        if parameter.kind is inspect.Parameter.VAR_KEYWORD:
            return True
        if parameter.name == keyword and parameter.kind in {
            inspect.Parameter.KEYWORD_ONLY,
            inspect.Parameter.POSITIONAL_OR_KEYWORD,
        }:
            return True
    return False


def _status_metadata_for_context(context: Any) -> dict[str, Any] | None:
    repl = getattr(context, "repl", None)
    manager = getattr(repl, "_mcp_manager", None) if repl is not None else None
    warnings = list(getattr(repl, "mcp_config_warnings", []) or []) if repl is not None else []
    live_records = _live_mcp_records(manager)
    live_metadata = mcp_status_metadata(manager, warnings=warnings) if manager is not None else None

    servers: list[dict[str, Any]] = []
    warning_items: list[dict[str, Any]] = []
    if isinstance(live_metadata, dict):
        live_servers = [server for server in live_metadata.get("servers", []) if isinstance(server, dict)]
        servers.extend(_with_private_source_paths(live_servers, live_records))
        warning_items.extend(warning for warning in live_metadata.get("warnings", []) if isinstance(warning, dict))

    seen = {_server_key(server) for server in servers}
    cwd = _mcp_command_cwd(context)
    load_result = load_all_persisted_mcp_configs(cwd=cwd, include_pending_project=True)
    warning_items.extend(mcp_warning_metadata(warning) for warning in load_result.warnings)
    for config in [*load_result.servers, *load_result.pending]:
        diagnostic_item = _diagnostic_status_metadata(health_diagnostic_for_config(config))
        key = _server_key(diagnostic_item)
        if key in seen:
            continue
        seen.add(key)
        servers.append(diagnostic_item)
    for entry in list_persisted_mcp_server_entries(cwd=cwd):
        warning = mcp_cli._health_warning_for_entry(entry, load_result)
        if warning is None:
            continue
        diagnostic_item = _diagnostic_status_metadata(mcp_cli._health_diagnostic_for_persisted_warning(entry, warning))
        key = _server_key(diagnostic_item)
        if key in seen:
            continue
        seen.add(key)
        servers.append(diagnostic_item)

    if not servers and not warning_items:
        return None
    return {"servers": servers, "warnings": warning_items}


def _live_mcp_records(manager: Any) -> list[Any]:
    list_connections = getattr(manager, "list_connections", None)
    if not callable(list_connections):
        return []
    try:
        return list(list_connections())
    except Exception:
        return []


def _with_private_source_paths(servers: list[dict[str, Any]], records: list[Any]) -> list[dict[str, Any]]:
    enriched: list[dict[str, Any]] = []
    for index, server in enumerate(servers):
        item = dict(server)
        if index < len(records):
            scoped_config = getattr(records[index], "scoped_config", None)
            source_path = getattr(scoped_config, "source_path", None)
            if source_path:
                item["_sourcePath"] = str(source_path)
        enriched.append(item)
    return enriched


def _mcp_command_cwd(context: Any) -> Path:
    repl = getattr(context, "repl", None)
    cwd = getattr(repl, "_original_cwd", None) if repl is not None else None
    return Path(cwd) if isinstance(cwd, str) and cwd else Path.cwd()


def _canonical_source_path(source_path: str | Path | None, *, cwd: str | Path | None = None) -> Path | None:
    return mcp_cli._canonical_source_path(source_path, cwd=cwd)


def _diagnostic_status_metadata(diagnostic: MCPHealthDiagnostic) -> dict[str, Any]:
    config = diagnostic.scoped_config.config
    item: dict[str, Any] = {
        "serverName": diagnostic.name,
        "scope": diagnostic.scope.value,
        "transport": diagnostic.transport.value,
        "state": diagnostic.connection_state,
        "authState": diagnostic.auth_state,
        "configSignature": config.content_signature(),
    }
    if diagnostic.scoped_config.source_path:
        source_path = str(diagnostic.scoped_config.source_path)
        item["sourcePath"] = sanitize_mcp_public_text(source_path, fallback_summary="")
        item["_sourcePath"] = source_path
    if config.command:
        item["command"] = sanitize_mcp_public_text(config.command)
    if config.args:
        item["args"] = [sanitize_mcp_public_text(arg) for arg in config.args]
    if config.url:
        item["url"] = sanitize_mcp_public_text(config.url)
    for value, key in (
        (diagnostic.tools_count, "toolsCount"),
        (diagnostic.resources_count, "resourcesCount"),
        (diagnostic.prompts_count, "promptsCount"),
    ):
        if value is not None:
            item[key] = value
    if diagnostic.failure_reason:
        item["failureReason"] = diagnostic.failure_reason
    if diagnostic.auth_error:
        item["authError"] = diagnostic.auth_error
    if diagnostic.required_auth_scopes:
        item["requiredAuthScopes"] = list(diagnostic.required_auth_scopes)
    if diagnostic.auth_resource_metadata_url:
        item["authResourceMetadataUrl"] = diagnostic.auth_resource_metadata_url
    if diagnostic.latest_refresh_kind:
        item["latestRefreshKind"] = diagnostic.latest_refresh_kind
    if diagnostic.latest_refresh_at is not None:
        item["latestRefreshAt"] = diagnostic.latest_refresh_at
    if diagnostic.latest_refresh_failure_reason:
        item["latestRefreshFailureReason"] = diagnostic.latest_refresh_failure_reason
    return item


def _server_items(metadata: dict[str, Any]) -> list[dict[str, Any]]:
    return [server for server in metadata.get("servers", []) if isinstance(server, dict)]


def _required_auth_scopes_from_metadata(server: dict[str, Any]) -> list[str]:
    value = server.get("requiredAuthScopes")
    if isinstance(value, str):
        candidates: list[Any] = value.split()
    elif isinstance(value, list | tuple):
        candidates = list(value)
    else:
        return []
    return [scope for candidate in candidates if (scope := _metadata_string(candidate))]


def _metadata_string(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    stripped = value.strip()
    return stripped or None


def _ordered_server_items(metadata: dict[str, Any]) -> list[dict[str, Any]]:
    return [server for _scope, servers in _servers_grouped_by_scope(_server_items(metadata)) for server in servers]


def _servers_grouped_by_scope(servers: list[dict[str, Any]]) -> list[tuple[str, list[dict[str, Any]]]]:
    grouped: dict[str, list[dict[str, Any]]] = {}
    for server in servers:
        scope = str(server.get("scope") or "unknown")
        grouped.setdefault(scope, []).append(server)
    for scope_servers in grouped.values():
        scope_servers.sort(key=lambda item: str(item.get("serverName") or "").casefold())

    ordered: list[tuple[str, list[dict[str, Any]]]] = []
    for scope in ("project", "local", "user", "session"):
        if scope in grouped:
            ordered.append((scope, grouped.pop(scope)))
    for scope in sorted((scope for scope in grouped if scope != "dynamic"), key=str.casefold):
        ordered.append((scope, grouped[scope]))
    if "dynamic" in grouped:
        ordered.append(("dynamic", grouped["dynamic"]))
    return ordered


def _server_key(server: dict[str, Any]) -> tuple[str, str, str, str]:
    return (
        str(server.get("serverName") or ""),
        str(server.get("scope") or ""),
        _server_action_source_path(server) or str(server.get("sourceId") or ""),
        str(server.get("configSignature") or ""),
    )


def _server_action_source_path(server: dict[str, Any]) -> str | None:
    value = server.get("_sourcePath") or server.get("sourcePath")
    if isinstance(value, str) and value:
        return value
    return None


def _server_display_source_path(server: dict[str, Any]) -> str | None:
    value = server.get("sourcePath")
    if isinstance(value, str) and value:
        return value
    return None


def _scope_heading(scope: str) -> str:
    labels = {
        "project": _("Project MCPs"),
        "user": _("User MCPs"),
        "local": _("Local MCPs"),
        "session": _("Session MCPs"),
        "dynamic": _("Built-in MCPs"),
    }
    return labels.get(scope, _("{scope} MCPs").format(scope=scope.title()))


def _scope_heading_with_path(scope: str, servers: list[dict[str, Any]]) -> str:
    heading = _scope_heading(scope)
    if scope == "dynamic":
        return _("{heading} (always available)").format(heading=heading)
    paths = {str(server.get("sourcePath") or "") for server in servers}
    paths.discard("")
    if len(paths) == 1:
        return "{heading} ({path})".format(heading=heading, path=next(iter(paths)))
    return heading


def _server_list_label(server: dict[str, Any], scope_servers: list[dict[str, Any]]) -> str:
    name = str(server.get("serverName") or "-")
    if not _server_list_needs_source_label(server, scope_servers):
        return name
    source_path = _server_display_source_path(server)
    if not source_path:
        return name
    return "{name} ({source})".format(name=name, source=source_path)


def _server_list_needs_source_label(server: dict[str, Any], scope_servers: list[dict[str, Any]]) -> bool:
    name = str(server.get("serverName") or "")
    duplicate_names = sum(1 for item in scope_servers if str(item.get("serverName") or "") == name) > 1
    source_paths = {_server_display_source_path(item) for item in scope_servers}
    source_paths.discard(None)
    return duplicate_names or len(source_paths) > 1


def _server_count_text(count: int) -> str:
    if count == 1:
        return _("1 server")
    return _("{count} servers").format(count=count)


def _tool_count_text(count: int) -> str:
    if count == 1:
        return _("1 tool")
    return _("{count} tools").format(count=count)


def _resource_count_text(count: int) -> str:
    if count == 1:
        return _("1 resource")
    return _("{count} resources").format(count=count)


def _prompt_count_text(count: int) -> str:
    if count == 1:
        return _("1 prompt")
    return _("{count} prompts").format(count=count)


def _server_title(server: dict[str, Any]) -> str:
    name = str(server.get("serverName") or _("Unknown"))
    return _("{name} MCP Server").format(name=_capitalize_first(name))


def _capitalize_first(value: str) -> str:
    return value[:1].upper() + value[1:]


def _summary_lines(server: dict[str, Any]) -> list[RenderableType]:
    source_path = str(server.get("sourcePath") or "")
    config_location = str(server.get("scope") or "-")
    if source_path:
        config_location = "{scope} ({path})".format(scope=config_location, path=source_path)
    is_stdio = _is_stdio_transport(server)
    rows: list[tuple[str, Any]] = [
        (_("Status"), _status_display(server, include_icon=True, show_reconnect_attempt=False)),
    ]
    if not is_stdio:
        rows.append((_("Auth"), _auth_display(server, include_icon=True)))
    command = server.get("command")
    if command:
        rows.append((_("Command"), command))
    args = server.get("args")
    if isinstance(args, list) and args:
        rows.append((_("Args"), " ".join(str(arg) for arg in args)))
    url = server.get("url")
    if url:
        rows.append((_("URL"), url))
    rows.extend(
        [
            (_("Config location"), config_location),
        ]
    )
    rows.extend(_failure_detail_rows(server))
    if _state_value(server) == "connected":
        rows.append((_("Capabilities"), _capability_count_display(server)))
        for label, count_key, formatter in (
            (_("Tools"), "toolsCount", _tool_count_text),
            (_("Resources"), "resourcesCount", _resource_count_text),
            (_("Prompts"), "promptsCount", _prompt_count_text),
        ):
            count = server.get(count_key)
            if isinstance(count, int):
                rows.append((label, formatter(count)))
    return [Text("{label}: {value}".format(label=label, value=value)) for label, value in rows]


def _failure_detail_rows(server: dict[str, Any]) -> list[tuple[str, str]]:
    rows: list[tuple[str, str]] = []
    for key, label in (
        ("failureReason", _("Failure")),
        ("latestFailureReason", _("Latest failure")),
        ("latestRefreshFailureReason", _("Latest refresh failure")),
    ):
        value = _failure_detail_text(server.get(key))
        if value:
            rows.append((label, value))
    capability_errors = server.get("capabilityErrors")
    if isinstance(capability_errors, dict):
        for capability, error in capability_errors.items():
            value = _failure_detail_text(error)
            if value:
                rows.append((_capability_refresh_label(str(capability)), value))
    return rows


def _failure_detail_text(value: Any) -> str:
    if not isinstance(value, str):
        return ""
    sanitized = sanitize_mcp_public_text(value, fallback_summary="")
    return sanitized.strip()


def _public_error_detail(exc: BaseException) -> str:
    message = str(exc) or exc.__class__.__name__
    sanitized = sanitize_mcp_public_text(message, fallback_summary=exc.__class__.__name__).strip()
    return _truncate_public_dialog_text(sanitized or exc.__class__.__name__)


def _truncate_public_dialog_text(value: str, *, max_chars: int = _MCP_DIALOG_ERROR_MAX_CHARS) -> str:
    if len(value) <= max_chars:
        return value
    marker = _("[truncated]")
    if max_chars <= len(marker):
        return marker[:max_chars]
    return value[: max_chars - len(marker)].rstrip() + marker


def _display_text(value: Any, *, fallback: str = "") -> str:
    if value is None:
        return fallback
    sanitized = strip_mcp_terminal_control_sequences(value)
    return sanitized if sanitized else fallback


def _capability_refresh_label(capability: str) -> str:
    labels = {
        "tools": _("Tools refresh"),
        "resources": _("Resources refresh"),
        "prompts": _("Prompts refresh"),
    }
    if capability in labels:
        return labels[capability]
    return _("{capability} refresh").format(capability=capability)


def _tools_for_server(server: dict[str, Any]) -> list[dict[str, Any]]:
    tools = server.get("tools")
    return [tool for tool in tools if isinstance(tool, dict)] if isinstance(tools, list) else []


def _resources_for_server(server: dict[str, Any]) -> list[dict[str, Any]]:
    resources = server.get("resources")
    return [resource for resource in resources if isinstance(resource, dict)] if isinstance(resources, list) else []


def _prompts_for_server(server: dict[str, Any]) -> list[dict[str, Any]]:
    prompts = server.get("prompts")
    return [prompt for prompt in prompts if isinstance(prompt, dict)] if isinstance(prompts, list) else []


def _tool_display_name(tool: dict[str, Any]) -> str:
    return _display_text(tool.get("originalToolName") or tool.get("name") or tool.get("publicName"), fallback="-")


def _tool_full_name(tool: dict[str, Any]) -> str:
    return _display_text(tool.get("publicName") or tool.get("name") or tool.get("originalToolName"), fallback="-")


def _resource_display_name(resource: dict[str, Any]) -> str:
    return _display_text(resource.get("title") or resource.get("name") or resource.get("uri"), fallback="-")


def _resource_name(resource: dict[str, Any]) -> str:
    return _display_text(resource.get("name") or resource.get("originalResourceName"))


def _resource_uri(resource: dict[str, Any]) -> str:
    return _display_text(resource.get("uri"), fallback="-")


def _resource_mime_type(resource: dict[str, Any]) -> str:
    return _display_text(resource.get("mimeType") or resource.get("mime_type"))


def _prompt_display_name(prompt: dict[str, Any]) -> str:
    return _display_text(
        prompt.get("originalPromptName") or prompt.get("name") or prompt.get("publicName"),
        fallback="-",
    )


def _prompt_full_name(prompt: dict[str, Any]) -> str:
    return _display_text(
        prompt.get("publicName") or prompt.get("name") or prompt.get("originalPromptName"),
        fallback="-",
    )


def _state_value(server: dict[str, Any]) -> str:
    value = str(server.get("state") or "unknown")
    if value == MCPConnectionState.NEEDS_AUTH.value:
        return "needs-auth"
    return value


def _status_display(
    server: dict[str, Any],
    *,
    include_icon: bool = False,
    show_reconnect_attempt: bool = True,
) -> str:
    state = _state_value(server)
    if state == "needs-auth":
        return _with_icon(_("needs authentication"), _status_icon(state), include_icon=include_icon)
    if state == "pending":
        retry_count = server.get("retryCount")
        max_attempts = server.get("maxReconnectAttempts")
        if show_reconnect_attempt and isinstance(retry_count, int) and retry_count > 0:
            if isinstance(max_attempts, int) and max_attempts > 0:
                label = _("reconnecting ({attempt}/{max})…").format(attempt=retry_count, max=max_attempts)
            else:
                label = _("reconnecting…")
            return _with_icon(label, _status_icon(state), include_icon=include_icon)
        return _with_icon(_("connecting…"), _status_icon(state), include_icon=include_icon)
    if state == "pending-approval":
        return _with_icon(_("pending approval"), _status_icon(state), include_icon=include_icon)
    return _with_icon(_status_label(state), _status_icon(state), include_icon=include_icon)


def _status_label(state: str) -> str:
    labels = {
        "connected": _("connected"),
        "failed": _("failed"),
        "disabled": _("disabled"),
        "missing-env": _("missing-env"),
        "invalid-config": _("invalid-config"),
        "skipped": _("skipped"),
        "unknown": _("unknown"),
    }
    return labels.get(state, state.replace("_", "-"))


def _status_icon(state: str) -> str:
    return {
        "connected": "✓",
        "disabled": "○",
        "pending": "○",
        "pending-approval": "○",
        "needs-auth": "△",
    }.get(state, "✖")


def _failed_server_debug_hint() -> str:
    if is_debug_enabled():
        path = current_log_file()
        if path is not None:
            return _("※ Debug logging is enabled. Log file: {path}").format(path=path)
        return _("※ Debug logging is enabled; check the log file for MCP error details")
    return _("※ Run iac-code --debug to see error logs")


def _auth_icon(authenticated: bool) -> str:
    return "✓" if authenticated else "✖"


def _with_icon(label: str, icon: str, *, include_icon: bool) -> str:
    if not include_icon:
        return label
    return "{icon} {label}".format(icon=icon, label=label)


def _server_needs_auth(server: dict[str, Any]) -> bool:
    return _state_value(server) == "needs-auth" or str(server.get("authState") or "") == "needs-auth"


def _is_stdio_transport(server: dict[str, Any]) -> bool:
    return str(server.get("transport") or "") == "stdio"


def _has_persisted_scope(server: dict[str, Any]) -> bool:
    return str(server.get("scope") or "") in _PERSISTED_MCP_SCOPES


def _can_remove_server(server: dict[str, Any]) -> bool:
    return _has_persisted_scope(server)


def _is_effectively_authenticated(server: dict[str, Any]) -> bool:
    auth_state = str(server.get("authState") or "")
    if auth_state not in {"", "-", "needs-auth", "not-configured"}:
        return True
    if auth_state == "not-configured" or _server_needs_auth(server):
        return False
    if _state_value(server) != "connected":
        return False
    return bool(_tools_for_server(server)) or _positive_count(server.get("toolsCount"))


def _auth_display(server: dict[str, Any], *, include_icon: bool = False) -> str:
    authenticated = _is_effectively_authenticated(server)
    label = _("authenticated") if authenticated else _("not authenticated")
    return _with_icon(label, _auth_icon(authenticated), include_icon=include_icon)


def _positive_count(value: Any) -> bool:
    return isinstance(value, int) and value > 0


def _visible_select_range(count: int, focused_index: int) -> tuple[int, int]:
    if count <= _SELECT_PAGE_SIZE:
        return 0, count
    focused = max(0, min(count - 1, focused_index))
    end = min(count, max(_SELECT_PAGE_SIZE, focused + 1))
    start = max(0, end - _SELECT_PAGE_SIZE)
    return start, end


def _select_scroll_hint(index: int, *, start: int, end: int, count: int) -> str:
    hints: list[str] = []
    if index == start and start > 0:
        hints.append("↑")
    if index == end - 1 and end < count:
        hints.append("↓")
    return "  {hints}".format(hints="".join(hints)) if hints else ""


def _capability_count_display(server: dict[str, Any]) -> str:
    capabilities: list[str] = []
    if _positive_count(server.get("toolsCount")):
        capabilities.append(_("tools"))
    if _positive_count(server.get("resourcesCount")):
        capabilities.append(_("resources"))
    if _positive_count(server.get("promptsCount")):
        capabilities.append(_("prompts"))
    return ", ".join(capabilities) if capabilities else _("none")


def _format_count(value: Any) -> str:
    return str(value) if isinstance(value, int) else "-"


def _annotation_display(value: Any) -> str:
    if not isinstance(value, dict):
        return ""
    labels: list[str] = []
    for keys, label in (
        (("readOnlyHint", "read_only", "readOnly"), _("read-only")),
        (("destructiveHint", "destructive"), _("destructive")),
        (("openWorldHint", "open_world"), _("open-world")),
    ):
        if any(bool(value.get(key)) for key in keys):
            labels.append(label)
    return ", ".join(labels)


def _annotation_badges(labels: str) -> str:
    if not labels:
        return ""
    return " ".join("[{label}]".format(label=label.strip()) for label in labels.split(",") if label.strip())


def _schema_parameter_lines(schema: dict[str, Any]) -> list[RenderableType]:
    properties = schema.get("properties")
    if not isinstance(properties, dict) or not properties:
        return []
    required_raw = schema.get("required")
    required = {str(value) for value in required_raw} if isinstance(required_raw, list) else set()
    lines: list[RenderableType] = []
    for name, value in properties.items():
        raw_name = str(name)
        display_name = _display_text(raw_name)
        property_schema = value if isinstance(value, dict) else {}
        type_value = property_schema.get("type") or _("unknown")
        if isinstance(type_value, list):
            type_label = " | ".join(_display_text(item) for item in type_value)
        else:
            type_label = _display_text(type_value)
        required_suffix = _(" (required)") if raw_name in required else ""
        description = _display_text(property_schema.get("description"))
        description_suffix = " - {description}".format(description=description) if description else ""
        lines.append(
            Text(
                "  • {name}{required}: {type}{description}".format(
                    name=display_name,
                    required=required_suffix,
                    type=type_label,
                    description=description_suffix,
                )
            )
        )
    return lines


def _prompt_argument_lines(value: Any) -> list[RenderableType]:
    if isinstance(value, list):
        arguments = [item for item in value if isinstance(item, dict)]
    elif isinstance(value, dict):
        arguments = [
            {"name": name, **item} if isinstance(item, dict) else {"name": name} for name, item in value.items()
        ]
    else:
        return []
    lines: list[RenderableType] = []
    for argument in arguments:
        name = _display_text(argument.get("name"))
        if not name:
            continue
        required_suffix = _(" (required)") if bool(argument.get("required")) else ""
        description = _display_text(argument.get("description"))
        description_suffix = " - {description}".format(description=description) if description else ""
        lines.append(
            Text(
                "  • {name}{required}{description}".format(
                    name=name,
                    required=required_suffix,
                    description=description_suffix,
                )
            )
        )
    return lines


def _callback_input_display(flow: _OAuthFlowState) -> str:
    value = flow.callback_input
    cursor = max(0, min(len(value), flow.callback_cursor))
    if not value:
        return ""
    return value[:cursor] + "|" + value[cursor:]


def _insert_auth_input(flow: _OAuthFlowState, value: str) -> None:
    if not value:
        return
    cursor = max(0, min(len(flow.callback_input), flow.callback_cursor))
    flow.callback_input = flow.callback_input[:cursor] + value + flow.callback_input[cursor:]
    flow.callback_cursor = cursor + len(value)


def _is_pending_oauth_flow(value: Any) -> bool:
    return (
        bool(getattr(value, "authorization_url", None))
        and callable(getattr(value, "wait", None))
        and callable(getattr(value, "complete_manually", None))
        and callable(getattr(value, "close", None))
    )


def _complete_pending_oauth_flow(pending: Any, value: str, *, wait_started: bool) -> Any:
    submit = getattr(pending, "submit_manually", None)
    if wait_started and callable(submit):
        submit(value)
        return None
    return pending.complete_manually(value)


def _is_recoverable_manual_oauth_input_error(exc: BaseException) -> bool:
    message = str(exc)
    return message in {
        _("OAuth callback did not include a code."),
        _("OAuth manual callback input was empty."),
    }


def _finish_auth_flow(
    flow: _OAuthFlowState,
    *,
    result: Any = None,
    error: BaseException | None = None,
) -> None:
    with flow.lock:
        if flow.done:
            return
        flow.result = result
        flow.error = error
        flow.done = True


def _finish_action_flow(
    flow: _ActionFlowState,
    *,
    result: Any = None,
    error: BaseException | None = None,
) -> None:
    with flow.lock:
        if flow.done:
            return
        flow.result = result
        flow.error = error
        flow.done = True


def _diagnostic_renderables(warnings: Any) -> list[RenderableType]:
    if not isinstance(warnings, list) or not warnings:
        return []
    grouped: dict[tuple[str, str], list[dict[str, Any]]] = {}
    for warning in warnings:
        if not isinstance(warning, dict):
            continue
        scope = str(warning.get("scope") or warning.get("source") or "unknown")
        source_path = str(warning.get("sourcePath") or warning.get("source") or "")
        grouped.setdefault((scope, source_path), []).append(warning)
    if not grouped:
        return []

    lines: list[RenderableType] = [
        Text(_("MCP Config Diagnostics"), style="bold"),
        Text(_("For help configuring MCP servers, run `iac-code mcp --help`."), style="dim"),
        Text(""),
    ]
    sorted_groups = sorted(
        grouped.items(),
        key=lambda item: (_scope_sort_key(item[0][0]), item[0][1]),
    )
    for (scope, source_path), items in sorted_groups:
        has_error = any(_diagnostic_severity(item) == "fatal" for item in items)
        status = _("Failed to parse") if has_error else _("Contains warnings")
        lines.append(Text("[{status}] {scope}".format(status=status, scope=_scope_heading(scope))))
        if source_path:
            lines.append(Text(_("Location: {path}").format(path=source_path), style="dim"))
        for item in items:
            lines.append(Text("  {line}".format(line=_diagnostic_item_text(item))))
        lines.append(Text(""))
    return lines


def _scope_sort_key(scope: str) -> tuple[int, str]:
    order = {"project": 0, "local": 1, "user": 2, "session": 3, "dynamic": 99}
    return (order.get(scope, 50), scope)


def _diagnostic_severity(item: dict[str, Any]) -> str:
    severity = str(item.get("severity") or "")
    if severity:
        return severity
    code = str(item.get("code") or "")
    return "fatal" if code in {"invalid_config", "parse_error", "fatal"} else "warning"


def _diagnostic_item_text(item: dict[str, Any]) -> str:
    label = _("[Error]") if _diagnostic_severity(item) == "fatal" else _("[Warning]")
    server_name = str(item.get("serverName") or "")
    path = str(item.get("path") or "")
    message = str(item.get("message") or "")
    parts = [label]
    if server_name:
        parts.append("[{name}]".format(name=server_name))
    if path:
        parts.append("{path}:".format(path=path))
    if message:
        parts.append(message)
    return " ".join(parts)


def _footer(*items: str) -> Text:
    text = Text("")
    text.append("  ".join(items), style="dim")
    return text


def _console_for_context(context: Any) -> Console:
    console = getattr(context, "console", None)
    return console if isinstance(console, Console) else Console()


def _run_maybe_await(value: Any) -> Any:
    if not inspect.isawaitable(value):
        return value
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(value)

    result: list[Any] = []
    error: list[BaseException] = []

    def runner() -> None:
        try:
            result.append(asyncio.run(value))
        except BaseException as exc:
            error.append(exc)

    thread = threading.Thread(target=runner, daemon=True)
    thread.start()
    thread.join()
    if error:
        raise error[0]
    return result[0] if result else None


def _copy_to_clipboard(text: str) -> bool:
    candidates: list[list[str]] = []
    if sys.platform == "darwin":
        candidates.append(["pbcopy"])
    elif sys.platform.startswith("linux"):
        candidates.append(["wl-copy"])
        candidates.append(["xclip", "-selection", "clipboard"])
    elif sys.platform == "win32":
        candidates.append(["clip"])

    for command in candidates:
        if shutil.which(command[0]) is None:
            continue
        try:
            subprocess.run(
                command,
                input=text,
                text=True,
                check=True,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            return True
        except Exception:
            continue
    return False


async def _reconnect_mcp_server_action(
    context: Any,
    name: str,
    scope: str | None,
    *,
    source_path: str | None = None,
) -> str:
    manager = _live_mcp_manager(context)
    if manager is not None and _live_mcp_server_matches(
        manager,
        name,
        scope,
        source_path=source_path,
        cwd=_mcp_command_cwd(context),
    ):
        reconnect = getattr(manager, "reconnect", None)
        if not callable(reconnect):
            return _reconnect_state_message(name, "failed")
        try:
            value = reconnect(name)
            if inspect.isawaitable(value):
                await value
        except Exception as exc:
            return _("Error reconnecting to {name}: {error}").format(
                name=name,
                error=_public_error_detail(exc),
            )
        state = _live_mcp_connection_state(manager, name)
        return _reconnect_state_message(name, state)

    command_cwd = _mcp_command_cwd(context)
    return _reconnect_result_message(
        name,
        _call_cli_reconnect_mcp_server(name=name, scope=scope, source_path=source_path, cwd=command_cwd),
    )


def _call_cli_reconnect_mcp_server(
    *,
    name: str,
    scope: str | None,
    source_path: str | None,
    cwd: str | Path | None,
) -> Any:
    reconnect = mcp_cli.reconnect_mcp_server
    kwargs: dict[str, Any] = {"name": name, "scope": scope}
    if source_path and _callable_accepts_keyword(reconnect, "source_path"):
        kwargs["source_path"] = source_path
    if cwd is not None and _callable_accepts_keyword(reconnect, "cwd"):
        kwargs["cwd"] = cwd
    return reconnect(**kwargs)


def _live_mcp_manager(context: Any) -> Any:
    repl = getattr(context, "repl", None)
    return getattr(repl, "_mcp_manager", None) if repl is not None else None


def _live_mcp_server_exists(manager: Any, name: str) -> bool:
    return _live_mcp_server_matches(manager, name, None)


def _live_mcp_server_matches(
    manager: Any,
    name: str,
    scope: str | None,
    *,
    source_path: str | None = None,
    cwd: str | Path | None = None,
) -> bool:
    connection = getattr(manager, "connection", None)
    if callable(connection):
        try:
            record = connection(name)
        except Exception:
            return False
        record_scope = _record_scope_value(record)
        return (scope is None or record_scope is None or record_scope == scope) and _record_source_path_matches(
            record,
            source_path,
            cwd=cwd,
        )
    list_connections = getattr(manager, "list_connections", None)
    if callable(list_connections):
        try:
            return any(
                getattr(record, "name", None) == name
                and (scope is None or _record_scope_value(record) is None or _record_scope_value(record) == scope)
                and _record_source_path_matches(record, source_path, cwd=cwd)
                for record in list_connections()
            )
        except Exception:
            return False
    return False


def _record_scope_value(record: Any) -> str | None:
    scoped_config = getattr(record, "scoped_config", None)
    scope = getattr(scoped_config, "scope", None)
    value = getattr(scope, "value", scope)
    return str(value) if value is not None else None


def _record_source_path_matches(record: Any, source_path: str | None, *, cwd: str | Path | None = None) -> bool:
    if not source_path:
        return True
    scoped_config = getattr(record, "scoped_config", None)
    record_source_path = getattr(scoped_config, "source_path", None)
    if not record_source_path:
        return False
    return _canonical_source_path(str(record_source_path), cwd=cwd) == _canonical_source_path(source_path, cwd=cwd)


def _live_mcp_connection_state(manager: Any, name: str) -> str:
    connection_state = getattr(manager, "connection_state", None)
    if callable(connection_state):
        try:
            state = connection_state(name)
        except Exception:
            state = None
        state_value = getattr(state, "value", state)
        if state_value:
            return str(state_value)
    connection = getattr(manager, "connection", None)
    if callable(connection):
        try:
            record = connection(name)
        except Exception:
            return "unknown"
        state = getattr(record, "state", None)
        return str(getattr(state, "value", state or "unknown"))
    return "unknown"


def _reconnect_result_message(name: str, diagnostics: list[MCPHealthDiagnostic]) -> str:
    if not diagnostics:
        return _("No MCP servers configured.")
    diagnostic = diagnostics[0]
    return _reconnect_state_message(name, diagnostic.connection_state)


def _reconnect_state_message(name: str, state: str) -> str:
    normalized = state.replace("_", "-")
    if normalized == MCPConnectionState.CONNECTED.value.replace("_", "-"):
        return _("Reconnected to {name}.").format(name=name)
    if normalized == MCPConnectionState.NEEDS_AUTH.value.replace("_", "-"):
        return _("{name} requires authentication. Use the 'Authenticate' option.").format(name=name)
    if normalized == MCPConnectionState.FAILED.value.replace("_", "-"):
        return _("Failed to reconnect to {name}.").format(name=name)
    return _("Unknown result when reconnecting to {name}.").format(name=name)


def _auth_success_message(name: str, *, reconnected: bool) -> str:
    if reconnected:
        return _("Authentication successful. Reconnected to {name}.").format(name=name)
    return _("Authentication successful. Connected to {name}.").format(name=name)


def _post_auth_result_message(
    context: Any,
    name: str,
    *,
    scope: str | None = None,
    source_path: str | None = None,
    initial_state: str | None = None,
    reconnected: bool,
) -> str:
    if initial_state == "disabled":
        return _("Authentication successful. Enable {name} to connect.").format(name=name)
    manager = _live_mcp_manager(context)
    if manager is not None and _live_mcp_server_matches(
        manager,
        name,
        scope,
        source_path=source_path,
        cwd=_mcp_command_cwd(context),
    ):
        state = _live_mcp_connection_state(manager, name).replace("_", "-")
        if state == MCPConnectionState.CONNECTED.value.replace("_", "-"):
            return _auth_success_message(name, reconnected=reconnected)
        if state == MCPConnectionState.NEEDS_AUTH.value.replace("_", "-"):
            return _(
                "Authentication successful, but server still requires authentication. "
                "You may need to manually restart iac-code."
            )
    return _(
        "Authentication successful, but server reconnection failed. "
        "You may need to manually restart iac-code for the changes to take effect."
    )


def _action_success_message(action: str, name: str) -> str:
    messages = {
        "auth": _("Authentication successful. Connected to {name}."),
        "reauth": _("Authentication successful. Reconnected to {name}."),
        "clear-auth": _("Authentication cleared for {name}."),
        "reconnect": _("Reconnected to {name}."),
        "disable": _("Disabled MCP server {name!r}."),
        "enable": _("Enabled MCP server {name!r}."),
        "remove": _("Removed MCP server {name!r}."),
        "approve": _("Approved MCP server {name!r}."),
        "reject": _("Rejected MCP server {name!r}."),
    }
    return messages.get(action, _("Updated MCP server {name!r}.")).format(name=name)
