"""Main REPL loop — integrates all UI subsystems.

InlineREPL wires together:
- PromptInput (line-editor + history + suggestions)
- KeybindingManager (Ctrl+R / Ctrl+P / Ctrl+F global shortcuts)
- SuggestionAggregator (CommandProvider, FileProvider, DirectoryProvider, ShellHistoryProvider)
- InputHistory (persistent across sessions)
- Dialog launchers (HistorySearch, QuickOpen, GlobalSearch)
- CommandRegistry + AgentLoop for processing input
- Renderer for streaming output
"""

from __future__ import annotations

import asyncio
import os
import re
import signal
import subprocess
import sys
import time
from dataclasses import dataclass
from types import ModuleType
from typing import Any, cast

from loguru import logger
from rich.console import Console

from iac_code.agent.agent_loop import AgentLoop
from iac_code.agent.system_prompt import build_system_prompt
from iac_code.commands import create_default_registry
from iac_code.commands.registry import CommandResult, LocalCommand, PromptCommand
from iac_code.config import get_active_provider_key, get_config_dir, get_history_path, load_credentials
from iac_code.i18n import _
from iac_code.memory.memory_manager import MemoryManager
from iac_code.providers.manager import ProviderManager
from iac_code.providers.registry import PROVIDER_REGISTRY
from iac_code.services.session_index import SessionIndex
from iac_code.services.session_metadata import normalize_session_name
from iac_code.services.session_resolver import ResolutionStatus, resolve_session_argument
from iac_code.services.session_storage import SessionStorage
from iac_code.services.update_checker import (
    PendingUpdate,
    get_pending_update,
    run_update_command,
    start_background_update_check,
    suppress_version,
)
from iac_code.skills.settings import normalize_skill_name
from iac_code.state import AppStateStore
from iac_code.state.app_state import AppState
from iac_code.tasks.notification_queue import NotificationQueue
from iac_code.tasks.task_state import TaskManager
from iac_code.tools.base import ToolRegistry
from iac_code.ui.banner import render_update_prompt_header, render_welcome_banner
from iac_code.ui.components.select import Select, SelectLayout, TextOption
from iac_code.ui.core.input_history import InputHistory
from iac_code.ui.core.prompt_input import PromptInput, PromptInputResult
from iac_code.ui.keybindings.manager import KeyBinding, KeybindingManager
from iac_code.ui.renderer import Renderer, StreamingInputBuffer
from iac_code.ui.suggestions.aggregator import SuggestionAggregator
from iac_code.ui.suggestions.command_provider import CommandProvider
from iac_code.ui.suggestions.directory_provider import DirectoryProvider
from iac_code.ui.suggestions.file_provider import FileProvider
from iac_code.ui.suggestions.shell_history_provider import ShellHistoryProvider
from iac_code.ui.suggestions.skill_provider import SkillProvider
from iac_code.utils.background_housekeeping import start_background_housekeeping
from iac_code.utils.image.clipboard import ClipboardImage, get_image_from_clipboard, try_read_image_from_path
from iac_code.utils.image.format_detect import IMAGE_EXTENSION_REGEX
from iac_code.utils.project_paths import format_resume_command, same_project_path

termios: ModuleType | None
try:
    import termios as _termios
except ImportError:  # Windows
    termios = None
else:
    termios = _termios


class ExitREPLError(Exception):
    """Raised by /exit command to break the REPL loop."""


@dataclass
class CommandContext:
    """Context passed to command handlers."""

    console: Console
    store: AppStateStore
    repl: "InlineREPL"


def _normalize_command_result(result: object) -> tuple[str, bool, bool]:
    if result is None:
        return "", False, False
    if isinstance(result, CommandResult):
        return result.message, result.is_error, result.refresh_banner
    return str(result), False, False


class InlineREPL:
    """Inline terminal REPL integrating all subsystems."""

    def __init__(
        self,
        model: str,
        resume_session_id: str | bool | None = None,
        cli_allowed_tools: list[str] | None = None,
        cli_disallowed_tools: list[str] | None = None,
        cli_permission_mode: str | None = None,
    ) -> None:
        self.console = Console()
        # Lock the working directory for the lifetime of this REPL. All session
        # storage and project-partitioning lookups go through this — agents can
        # `cd` mid-session via Bash, but those changes must not relocate the
        # session file or split it across two project dirs.
        self._original_cwd = os.getcwd()
        self.store = AppStateStore(initial_state=AppState(model=model))
        self.command_registry = create_default_registry()
        self.tool_registry = ToolRegistry()
        self.tool_registry.register_default_tools()
        self.refresh_cloud_tools()
        self._current_model = model
        from iac_code.config import load_active_provider_config

        self._current_provider_config = load_active_provider_config()

        # Backend: Provider + Session + Tasks + Memory
        self._credentials = self._load_credentials()
        self._provider_key_override: str | None = None
        self._base_url_override: str | None = None
        self._apply_qwenpaw_config(model)
        self._provider_manager = ProviderManager(
            model=self._current_model,
            credentials=self._credentials,
            provider_key_override=self._provider_key_override,
            base_url_override=self._base_url_override,
        )
        self._session_storage = SessionStorage()
        self.session_index = SessionIndex()
        self._session_id = self._resolve_session_id(resume_session_id)
        self._was_resumed = resume_session_id is not None
        from iac_code.utils.image.store import ImageStore

        self._image_store = ImageStore(session_id=self._session_id)
        self._resume_messages = self._load_resume_messages(resume_session_id)
        self._session_name = self._load_current_session_name()
        self._task_manager = TaskManager()
        self._notification_queue = NotificationQueue()
        self._command_log: list[tuple[str, str, int, bool]] = []
        self._streaming_error_log: list[tuple[str, int]] = []

        memory_dir = str(get_config_dir() / "memory")
        self._memory_manager = MemoryManager(memory_dir=memory_dir)

        # Register new tools
        from iac_code.agent.agent_tool import AgentTool
        from iac_code.memory.memory_tools import ReadMemoryTool, WriteMemoryTool
        from iac_code.tasks.task_tools import TaskGetTool, TaskListTool, TaskStopTool

        memory_content = ""
        if hasattr(self, "_memory_manager") and self._memory_manager:
            memory_content = self._memory_manager.get_prompt_content()
        self.tool_registry.register(
            AgentTool(
                task_manager=self._task_manager,
                provider_manager=self._provider_manager,
                tool_registry=self.tool_registry,
                system_prompt=build_system_prompt(cwd=os.getcwd(), memory_content=memory_content),
                notification_queue=self._notification_queue,
            )
        )
        self.tool_registry.register(ReadMemoryTool(self._memory_manager))
        self.tool_registry.register(WriteMemoryTool(self._memory_manager))
        self.tool_registry.register(TaskListTool(self._task_manager))
        self.tool_registry.register(TaskGetTool(self._task_manager))
        self.tool_registry.register(TaskStopTool(self._task_manager))

        cwd = os.getcwd()
        self._memory_content = memory_content
        self.refresh_skills()
        skill_commands = self.command_registry.get_model_invocable_skills()

        from iac_code.services.permissions.loader import load_permission_context
        from iac_code.services.permissions.trusted_roots import build_session_trusted_read_directories

        permission_context = load_permission_context(
            self._original_cwd,
            cli_allowed=cli_allowed_tools,
            cli_disallowed=cli_disallowed_tools,
            cli_mode=cli_permission_mode,
        )
        permission_context.trusted_read_directories.extend(build_session_trusted_read_directories(self._session_id))
        self.store.set_state(permission_context=permission_context)

        agent_tool = self.tool_registry.get("agent")
        if agent_tool is not None and hasattr(agent_tool, "_permission_context"):
            setattr(agent_tool, "_permission_context", permission_context)

        self._agent_loop = AgentLoop(
            provider_manager=self._provider_manager,
            system_prompt=build_system_prompt(
                cwd=cwd, memory_content=memory_content, skill_listing=self._skill_listing
            ),
            tool_registry=self.tool_registry,
            session_storage=self._session_storage,
            session_id=self._session_id,
            resume_messages=self._resume_messages or None,
            cwd=self._original_cwd,
            permission_context=permission_context,
            permission_context_getter=lambda: self.store.get_state().permission_context,
            auto_trigger_skills=skill_commands,
        )
        self.renderer = Renderer(
            self.console,
            self.tool_registry,
            status_callback=self._status_text,
            app_state_store=self.store,
        )

        # Keybinding manager
        self._keybinding_manager = KeybindingManager()

        # Input history
        self._history = InputHistory(str(get_history_path()))

        # Suggestion aggregator with all 4 providers
        cwd = os.getcwd()
        self._suggestion_aggregator = SuggestionAggregator(
            [
                CommandProvider(self.command_registry, memory_manager=self._memory_manager),
                SkillProvider(self.command_registry),
                FileProvider(cwd),
                DirectoryProvider(cwd),
                ShellHistoryProvider(),
            ]
        )

        # PromptInput. ``paste_handler`` covers the macOS Cmd+V case: macOS
        # terminals never forward Cmd+V bytes to the app, but they DO send a
        # bracketed-paste sequence. The handler probes the system clipboard
        # for an image on every bracketed paste and attaches it inline.
        self._prompt_input = PromptInput(
            keybinding_manager=self._keybinding_manager,
            suggestion_aggregator=self._suggestion_aggregator,
            history=self._history,
            console=self.console,
            paste_handler=self._on_bracketed_paste,
            image_store=self._image_store,
        )

        self.store.subscribe(self._on_state_change)

    # ------------------------------------------------------------------
    # Public entry-point
    # ------------------------------------------------------------------

    @property
    def skill_management_items(self):
        """Return all discovered skills with management state."""
        return getattr(self, "_skill_management_items", [])

    @property
    def locked_skill_names(self):
        """Return skill names that cannot be disabled."""
        return getattr(self, "_locked_skill_names", set())

    def refresh_cloud_tools(self) -> None:
        """Register cloud tools that are available with current cloud credentials."""
        from iac_code.services.cloud_credentials import CloudCredentials
        from iac_code.tools.cloud.registry import register_cloud_tools

        register_cloud_tools(self.tool_registry, CloudCredentials())

    def refresh_skills(self) -> None:
        """Rediscover skills and refresh enabled/disabled skill state."""
        from iac_code.skills.bundled import init_bundled_skills
        from iac_code.skills.discovery import discover_all_skills
        from iac_code.skills.listing import build_skill_listing
        from iac_code.skills.management import build_skill_management_state
        from iac_code.skills.settings import load_disabled_skills
        from iac_code.skills.skill_tool import SkillTool

        init_bundled_skills()
        cwd = os.getcwd()
        all_skills = discover_all_skills(cwd)
        state = build_skill_management_state(all_skills, load_disabled_skills())
        self._skill_management_items = state.items
        self._disabled_skill_commands = state.disabled_commands
        self._locked_skill_names = state.locked_skill_names

        self.command_registry.clear_prompt_commands()
        for cmd in state.enabled_commands:
            existing = self.command_registry.get(cmd.name)
            if existing is not None and not isinstance(existing, PromptCommand):
                logger.warning(
                    "Skill '%s' (source=%s) skipped: conflicts with built-in command",
                    cmd.name,
                    cmd.source,
                )
                continue
            self.command_registry.register(cmd)

        memory_content = getattr(self, "_memory_content", "")
        self.tool_registry.register(
            SkillTool(
                command_registry=self.command_registry,
                disabled_skills=self._disabled_skill_commands,
                session_id=self._session_id,
                cwd=cwd,
                provider_manager=self._provider_manager,
                tool_registry=self.tool_registry,
                system_prompt=build_system_prompt(cwd=cwd, memory_content=memory_content),
            )
        )

        skill_commands = self.command_registry.get_model_invocable_skills()
        self._skill_listing = build_skill_listing(skill_commands)

        if hasattr(self, "_agent_loop"):
            self._agent_loop.set_auto_trigger_skills(skill_commands)
            self._agent_loop.set_provider(
                self._provider_manager,
                system_prompt=build_system_prompt(
                    cwd=cwd,
                    memory_content=memory_content,
                    skill_listing=self._skill_listing,
                ),
            )

    async def run(self, initial_prompt: str | None = None) -> None:
        """Run the REPL until the user exits.

        Args:
            initial_prompt: If provided, automatically process this as the first
                user input (e.g. from piped stdin).
        """
        # Capture session start time for duration calculation
        self._started_monotonic = time.monotonic()

        self._handle_startup_update()
        state = self.store.get_state()
        self.console.print(
            render_welcome_banner(state.model, state.cwd, session_id=self._session_id, session_name=self._session_name)
        )
        if self._resume_messages:
            self.renderer.replay_history(self._resume_messages)
            self.console.print()  # blank line before first new user turn
        start_background_housekeeping(session_id=self._session_id)
        self._start_background_update_checker()
        self._register_global_keybindings()

        # Clear IEXTEN for the whole session so macOS/BSD can't latch Ctrl+O
        # onto VDISCARD. VDISCARD toggles tty-wide output discard on a single
        # keystroke, so an ill-timed Ctrl+O between our raw-input contexts
        # (cooked gap) would silently swallow every subsequent render until
        # pressed again — looking exactly like the "stuck after multiple
        # ctrl+o" symptom. Disabling IEXTEN disables VDISCARD entirely;
        # RawInputCapture's setraw() preserves c_cc across enter/exit.
        saved_termios = None
        if termios is not None:
            try:
                fd = sys.stdin.fileno()
                saved_termios = termios.tcgetattr(fd)
                mode = termios.tcgetattr(fd)
                mode[3] = mode[3] & ~termios.IEXTEN
                termios.tcsetattr(fd, termios.TCSANOW, mode)
            except (termios.error, OSError, ValueError):
                saved_termios = None

        # Install a custom SIGINT handler that replaces asyncio's default.
        # asyncio's default handler tracks a global _interrupt_count that is
        # never reset — after one Ctrl+C, subsequent presses raise
        # KeyboardInterrupt instead of cancelling the task. Our handler
        # always cancels the main task, allowing the REPL to recover via
        # uncancel() and continue.
        loop = asyncio.get_event_loop()
        main_task = asyncio.current_task()

        def _on_sigint() -> None:
            if main_task and not main_task.done():
                main_task.cancel()

        _has_sigint_handler = False
        try:
            loop.add_signal_handler(signal.SIGINT, _on_sigint)
            _has_sigint_handler = True
        except (NotImplementedError, OSError):
            pass  # Windows or restricted environment

        first_turn = True
        last_ctrl_c_time: float = 0.0
        queued_inputs: list[str] = []
        draft_input = ""

        try:
            while True:
                try:
                    # Check for background agent notifications
                    while self._notification_queue.has_pending():
                        notification = self._notification_queue.dequeue()
                        if notification:
                            self.renderer.print_system_message(
                                f"Agent '{notification.task_id}' completed: {notification.message}"
                            )

                    # Blank line between turns
                    if not first_turn:
                        self.console.print()
                    first_turn = False

                    # Use initial_prompt for the first turn if provided
                    if queued_inputs:
                        user_input = queued_inputs.pop(0)
                        self.renderer.print_user_message(user_input)
                    elif initial_prompt is not None:
                        user_input = initial_prompt
                        initial_prompt = None
                        self.console.print(f"[bold cyan]❯[/bold cyan] {user_input}")
                    else:
                        user_input = await self._prompt_input.get_input(initial_text=draft_input)
                        draft_input = ""
                    if user_input is None:  # Ctrl+C with empty input
                        now = time.monotonic()
                        if now - last_ctrl_c_time < 1.5:
                            # Double Ctrl+C within 1.5s → exit
                            break
                        last_ctrl_c_time = now
                        self.console.print("[dim]{}[/dim]".format(_("Press Ctrl+C again to exit.")))
                        continue
                    last_ctrl_c_time = 0.0  # Reset on valid input
                    user_input = user_input.strip()
                    if not user_input:
                        continue

                    if user_input.startswith("!"):
                        await self._handle_interactive_shell_escape(user_input)
                        continue

                    if self.command_registry.is_command(user_input):
                        self._record_command_history(user_input)
                        queued_inputs.extend(await self._handle_command(user_input))
                        new_draft = self._consume_streaming_draft_input()
                        if new_draft:
                            draft_input = new_draft
                        self._clear_cancel_state()
                        continue
                    self._history.append(user_input)
                    # Capture structured result (text + pasted images) before next get_input resets state.
                    chat_input: PromptInputResult | str
                    result = self._prompt_input.make_result()
                    chat_input = result if result.pasted_contents else user_input
                    queued_inputs.extend(await self._handle_chat(chat_input))
                    new_draft = self._consume_streaming_draft_input()
                    if new_draft:
                        draft_input = new_draft
                    self._clear_cancel_state()
                except (KeyboardInterrupt, asyncio.CancelledError):
                    self._clear_cancel_state()
                    self.console.print("\n[dim]{}[/dim]".format(_("Interrupted.")))
                    continue
                except ExitREPLError:
                    break
                except EOFError:
                    break
                except OSError:
                    # Terminal fd became invalid (e.g. after double Ctrl+C during response)
                    break
        finally:
            # Persist a tail-readable last-prompt entry so the /resume picker
            # can show what the user was last doing without parsing the whole
            # JSONL. Best-effort — failures must not block shutdown.
            self._write_last_prompt_meta()
            # Emit session exit event and gracefully shutdown telemetry
            from iac_code.services.telemetry import graceful_shutdown, log_event
            from iac_code.services.telemetry.names import Events

            log_event(
                Events.SESSION_EXITED,
                {
                    "reason": "normal",
                    "duration_s": int(time.monotonic() - self._started_monotonic),
                },
            )
            graceful_shutdown()

            if _has_sigint_handler:
                loop.remove_signal_handler(signal.SIGINT)
            if saved_termios is not None and termios is not None:
                try:
                    termios.tcsetattr(sys.stdin.fileno(), termios.TCSANOW, saved_termios)
                except (termios.error, OSError, ValueError):
                    pass

        self._print_exit_text()

    async def run_once(self, prompt: str) -> None:
        """Process a single prompt and exit (non-interactive mode)."""
        stripped_prompt = prompt.strip()
        if stripped_prompt.startswith("!"):
            await self._handle_shell_escape(stripped_prompt)
        elif self.command_registry.is_command(prompt):
            await self._handle_command(prompt)
        else:
            await self._handle_chat(prompt)

    def _handle_startup_update(self) -> PendingUpdate | None:
        """Prompt for a cached update before the welcome banner."""
        if not sys.stdin.isatty():
            return None
        update = get_pending_update()
        if update is None:
            return None

        self.console.print(render_update_prompt_header(update))
        selection = Select(
            [
                TextOption(
                    label=_("Update now"),
                    value="update_now",
                    description=_("Run the shown update command and exit when it succeeds."),
                ),
                TextOption(
                    label=_("Skip"),
                    value="skip",
                    description=_("Continue with the current version for this session."),
                ),
                TextOption(
                    label=_("Skip until next version"),
                    value="skip_until_next",
                    description=_("Hide this update until a newer version is available."),
                ),
            ],
            default_value="skip",
            layout=SelectLayout.EXPANDED,
            visible_count=3,
        ).run()

        if selection == "skip_until_next":
            suppress_version(update.version)
            return None
        if selection in (None, "skip"):
            return update

        try:
            result = run_update_command(update)
        except Exception:
            logger.opt(exception=True).debug("Startup update command failed")
            self.console.print(
                "[yellow]{}[/yellow]".format(_("Update command failed. Continuing with the current version."))
            )
            return update

        if result.returncode == 0:
            self.console.print("[green]{}[/green]".format(_("Update completed. Restart iac-code to continue.")))
            from iac_code.services.telemetry import graceful_shutdown

            graceful_shutdown()
            raise SystemExit(0)

        self.console.print(
            "[yellow]{}[/yellow]".format(_("Update command failed. Continuing with the current version."))
        )
        return update

    def _start_background_update_checker(self) -> None:
        """Start the asynchronous update check for a future session."""
        start_background_update_check()

    # ------------------------------------------------------------------
    # Keybinding registration
    # ------------------------------------------------------------------

    def _register_global_keybindings(self) -> None:
        km = self._keybinding_manager
        km.push_context("global")
        km.register(KeyBinding("ctrl+r", "open_history_search", "global", self._open_history_search))
        km.register(KeyBinding("ctrl+p", "open_quick_open", "global", self._open_quick_open))
        km.register(KeyBinding("ctrl+f", "open_global_search", "global", self._open_global_search))
        km.register(KeyBinding("ctrl+o", "expand_last_turn", "global", self._expand_last_turn))
        km.register(KeyBinding("ctrl+v", "paste_image", "global", self._handle_ctrl_v_image))

    def _handle_ctrl_v_image(self) -> bool:
        """Wrapper around :func:`handle_image_paste` that surfaces the
        no-image case to the user. Ctrl+V is an explicit "paste image"
        intent — silent return is the bug we're fixing."""
        logger.info("repl: Ctrl+V pressed — invoking image paste pipeline")
        if handle_image_paste(self):
            logger.info("repl: Ctrl+V handled (image attached or warned)")
            self._prompt_input._clipboard_has_image = False
            return True
        logger.info("repl: Ctrl+V — no image found in clipboard, surfacing system message")
        msg = _("No image in clipboard.")
        self._prompt_input.schedule_action(lambda: self.renderer.print_system_message(msg, style="dim"))
        return True

    def _on_bracketed_paste(self, text: str) -> bool:
        """Bracketed-paste hook. Probes the clipboard for an image on every
        paste; if one is found, attaches it as ``[Image #N]``. Returns True
        when the bracketed-paste text should NOT also be inserted into the
        buffer (would be redundant — empty string, or just the image's file
        path / file:// URL). Otherwise returns False so PromptInput inserts
        the text normally — preserves accompanying captions like "what is
        this screenshot?"."""
        # Some terminals interleave focus events (CSI I / CSI O) around the
        # paste boundary — Cmd+V briefly steals focus to the menu bar and back
        # on macOS. The focus bytes can land *inside* our paste content and
        # would otherwise be inserted into the buffer as garbage characters.
        # Strip them early so the rest of this method sees the clean payload.
        sanitized = _strip_orphan_focus_events(text)
        if sanitized != text:
            logger.info(
                "repl: bracketed paste — stripped {} byte(s) of orphan focus events",
                len(text) - len(sanitized),
            )
        text = sanitized

        preview = text[:80].replace("\n", "\\n")
        logger.info(
            "repl: bracketed paste received — text_len={} preview={!r}",
            len(text),
            preview,
        )
        # Prefer try_read_image_from_path: if the pasted text IS an image
        # path, use the file metadata (source_path, filename) rather than
        # whatever the clipboard happens to also carry.
        img = try_read_image_from_path(text) if text else None
        if img is not None:
            logger.info("repl: bracketed paste — image resolved from text path: {}", img.source_path)
        elif _is_existing_non_image_file(text):
            # The pasted text is a path to an existing non-image file (e.g. a
            # .txt copied from Finder). macOS places a TIFF icon/preview on the
            # clipboard alongside the file path — skip clipboard image detection
            # to avoid attaching the file icon as "[Image #N]".
            logger.info(
                "repl: bracketed paste — text is an existing non-image file path, skipping clipboard image detection"
            )
            img = None
        elif not self._prompt_input._clipboard_has_image:
            # Fast path: focus-in detection already confirmed no image in
            # clipboard. Skip the expensive clipboard probe (osascript + swift
            # subprocess on macOS) to avoid multi-second lag on every paste.
            logger.info("repl: bracketed paste — focus-in detected no clipboard image, skipping probe")
            img = None
        else:
            img = get_image_from_clipboard()
            if img is not None:
                logger.info("repl: bracketed paste — image read from system clipboard ({} bytes)", len(img.data))
        if img is None:
            # No image. If text is empty / pure noise (was just focus events),
            # suppress the insert so the buffer stays clean. Otherwise return
            # False so PromptInput inserts the text as normal.
            if not text:
                logger.info("repl: bracketed paste — empty payload, no image, nothing to do")
                return True
            logger.info("repl: bracketed paste — no image; falling through to plain text insert")
            return False

        _attach_clipboard_image(self, img)

        stripped = text.strip()
        if not stripped:
            logger.info("repl: bracketed paste — text empty, suppressing insert")
            return True
        if "\n" in stripped:
            logger.info("repl: bracketed paste — multi-line text, keeping caption alongside image")
            return False
        # Strip surrounding quotes (terminal drag-and-drop / shell-quoted paths)
        unquoted = stripped.strip("'\"")
        if unquoted.startswith("file://"):
            logger.info("repl: bracketed paste — text is file:// URL, suppressing insert")
            return True
        if IMAGE_EXTENSION_REGEX.search(unquoted):
            logger.info("repl: bracketed paste — text is an image path, suppressing insert")
            return True
        logger.info("repl: bracketed paste — image attached and text inserted as caption")
        return False

    # ------------------------------------------------------------------
    # Dialog launchers
    # ------------------------------------------------------------------

    def _open_history_search(self) -> bool:
        from iac_code.ui.dialogs.history_search import HistorySearch

        messages = self._history_search_messages()
        dialog = HistorySearch(
            messages=messages,
            on_select=self._insert_text,
            on_cancel=lambda: None,
            keybinding_manager=self._keybinding_manager,
        )
        dialog.run()
        return True

    def _history_search_messages(self) -> list[dict[str, str]]:
        """Build searchable user-history rows from prompt history and conversation context."""
        entries: list[str] = []
        seen: set[str] = set()

        def add_text(text: str) -> None:
            cleaned = text.strip()
            if not cleaned or cleaned in seen:
                return
            seen.add(cleaned)
            entries.append(cleaned)

        history = getattr(self, "_history", None)
        if history is not None and hasattr(history, "entries"):
            try:
                for entry in history.entries():
                    add_text(str(entry))
            except Exception:
                pass

        try:
            context_messages = self._agent_loop.context_manager.get_messages()
        except Exception:
            context_messages = []
        for msg in context_messages:
            if getattr(msg, "role", None) != "user":
                continue
            get_text = getattr(msg, "get_text", None)
            if callable(get_text):
                add_text(get_text())
                continue
            if isinstance(msg, dict) and msg.get("role") == "user":
                add_text(str(msg.get("content", "")))

        return [{"role": "user", "content": entry} for entry in entries]

    def _open_quick_open(self) -> bool:
        from iac_code.ui.dialogs.quick_open import QuickOpen

        dialog = QuickOpen(
            root_dir=os.getcwd(),
            on_select=self._insert_text,
            on_cancel=lambda: None,
            keybinding_manager=self._keybinding_manager,
        )
        dialog.run()
        return True

    def _open_global_search(self) -> bool:
        from iac_code.ui.dialogs.global_search import GlobalSearch

        dialog = GlobalSearch(
            root_dir=os.getcwd(),
            on_select=self._insert_text,
            on_cancel=lambda: None,
            keybinding_manager=self._keybinding_manager,
        )
        dialog.run()
        return True

    def _insert_text(self, text: str) -> None:
        """Insert text into the active prompt input buffer."""
        self._prompt_input.insert_text(text)

    async def _handle_interactive_shell_escape(self, user_input: str) -> None:
        """Handle an interactive shell escape without adding it to prompt history."""
        await self._handle_shell_escape(user_input)
        self._history.reset_navigation()
        self._clear_cancel_state()

    def _expand_last_turn(self) -> bool:
        """Keybinding handler: open the verbose transcript view."""
        self._prompt_input.schedule_action(self.renderer.show_transcript)
        return True

    # ------------------------------------------------------------------
    # Command handling
    # ------------------------------------------------------------------

    async def _handle_shell_escape(self, user_input: str) -> None:
        """Execute a local shell command from a leading ! REPL input."""
        command = user_input[1:].strip()
        if not command:
            message = _("Usage: !<shell command>")
            self._record_command_log(user_input, message, is_error=True)
            self.renderer.print_system_message(message, style="yellow")
            return

        tool = self.tool_registry.get("bash")
        if tool is None:
            message = _("Shell command support is unavailable.")
            self._record_command_log(user_input, message, is_error=True)
            self.renderer.print_system_message(message, style="red")
            return

        tool_input = {"command": command}
        if not await self._request_shell_escape_permission(tool, tool_input):
            return

        from iac_code.tools.base import ToolContext
        from iac_code.tools.tool_executor import ToolCallRequest, ToolExecutor

        executor = ToolExecutor(self.tool_registry)
        results = await executor.execute_batch(
            [ToolCallRequest(id="shell-escape", name="bash", input=tool_input)],
            ToolContext(cwd=self._original_cwd),
        )
        result = results[0]
        self.renderer.print_system_message(f"$ {command}", style="dim")
        output = result.content.rstrip()
        if output:
            self.renderer.print_system_message(output, style="red" if result.is_error else "white")
        log_result = f"$ {command}" if not output else f"$ {command}\n{output}"
        self._record_command_log(user_input, log_result, is_error=result.is_error)

    async def _request_shell_escape_permission(self, tool, tool_input: dict) -> bool:
        """Check permission for a display-only shell escape before execution."""
        permission_context = self.store.get_state().permission_context
        if permission_context is not None:
            from iac_code.services.permissions.pipeline import check_tool_permission

            permission = await check_tool_permission(tool, tool_input, permission_context)
        else:
            permission = await tool.check_permissions(tool_input, {"cwd": self._original_cwd})

        if permission.behavior == "allow":
            return True
        if permission.behavior == "deny":
            self.renderer.print_system_message(permission.message or _("Permission denied."), style="red")
            return False

        from iac_code.types.stream_events import PermissionRequestEvent

        allowed = await self.renderer.prompt_permission(
            PermissionRequestEvent(
                tool_name="bash",
                tool_input=tool_input,
                tool_use_id="shell-escape",
                permission_result=permission,
            )
        )
        if not allowed:
            self.renderer.print_system_message(_("Permission denied."), style="red")
        return allowed

    async def _handle_command(self, user_input: str) -> list[str]:
        """Dispatch a slash command and print the result."""
        is_skill_trigger = user_input.startswith("$")
        name, args = self.command_registry.parse(user_input)
        cmd = self.command_registry.get(name)

        def _emit_error(message: str) -> None:
            self._record_command_log(user_input, message, is_error=True)
            self.renderer.print_system_message(message, style="red")

        if cmd is None:
            if is_skill_trigger and normalize_skill_name(name) in getattr(self, "_disabled_skill_commands", {}):
                _emit_error(_("Skill '{name}' is disabled. Run /skills to enable it.").format(name=name))
                return []
            if is_skill_trigger:
                _emit_error(_("Unknown skill: ${name}. Type / to list commands and skills.").format(name=name))
            else:
                _emit_error(_("Unknown command: /{name}. Type /help for available commands.").format(name=name))
            return []

        # The "$" trigger invokes skills only; reject built-in commands with a clear hint.
        if is_skill_trigger and not isinstance(cmd, PromptCommand):
            _emit_error(_("$ only invokes skills. Use /{name} instead.").format(name=name))
            return []

        if isinstance(cmd, PromptCommand):
            # Skill command: process via unified path
            from iac_code.skills.processor import process_prompt_command

            args_str = " ".join(args) if args else ""
            try:
                result = await process_prompt_command(cmd, args_str)
                if result.is_fork:
                    return await self._handle_chat(result.prompt_content)
                else:
                    # Inline mode: inject messages and continue agent loop
                    for msg in result.new_messages:
                        self._agent_loop.context_manager.add_raw_message(msg)
                    if result.context_modifier:
                        self._agent_loop._apply_context_modifier(result.context_modifier)
                    # Stream the agent's response to the injected skill prompt
                    return await self._handle_chat_continue()
            except Exception as exc:
                self.renderer.print_system_message(
                    _("Command error: {error}").format(error=exc),
                    style="red",
                )
            return []
        elif isinstance(cmd, LocalCommand):
            context = CommandContext(console=self.console, store=self.store, repl=self)
            if cmd.handler is None:
                self.renderer.print_system_message(
                    _("Command has no handler: {name}").format(name=cmd.name),
                    style="red",
                )
                return []
            from iac_code.config import get_active_provider_key

            prev_model = self.store.get_state().model
            prev_provider_key = get_active_provider_key()
            try:
                handler_call = cmd.handler(
                    context=context,
                    args=args,
                    registry=self.command_registry,
                    store=self.store,
                )
                if cmd.progress_label:
                    self.store.set_state(is_busy=True)
                    try:
                        result = await self.renderer.run_with_spinner(handler_call, cmd.progress_label)
                    finally:
                        self.store.set_state(is_busy=False)
                else:
                    result = await handler_call
                result_message, is_error, refresh_banner = _normalize_command_result(result)
                if result_message:
                    self._record_command_log(user_input, result_message, is_error=is_error)
                # Re-render banner when model/provider actually switched
                new_state = self.store.get_state()
                new_provider_key = get_active_provider_key()
                if refresh_banner or new_state.model != prev_model or new_provider_key != prev_provider_key:
                    self._refresh_banner()
                else:
                    if result_message:
                        if is_error:
                            self.renderer.print_system_message(result_message, style="red")
                        else:
                            self.renderer.print_command_result(user_input, result_message)
            except ExitREPLError:
                raise
            except Exception as exc:
                self.renderer.print_system_message(
                    _("Command error: {error}").format(error=exc),
                    style="red",
                )
        return []

    def _message_count(self) -> int:
        try:
            return len(self._agent_loop.context_manager.get_messages())
        except Exception:
            return 0

    @staticmethod
    def _normalize_streaming_output_result(result: object) -> tuple[float, list[str], str]:
        """Return ``(elapsed, queued_inputs, draft_input)`` from the renderer result.

        Older tests and light-weight fakes may still return the pre-queueing
        float elapsed value. Keep accepting that shape at this boundary.
        """
        elapsed = cast(Any, getattr(result, "elapsed", result))
        queued_inputs = cast(Any, getattr(result, "queued_inputs", []))
        draft_input = cast(Any, getattr(result, "draft_input", ""))
        return float(elapsed), list(queued_inputs), str(draft_input)

    def _consume_streaming_draft_input(self) -> str:
        draft = getattr(self, "_streaming_draft_input", "")
        self._streaming_draft_input = ""
        return draft

    def _should_submit_mid_turn(self, value: str) -> bool:
        stripped = value.strip()
        if not stripped or stripped.startswith("!"):
            return False
        try:
            return not self.command_registry.is_command(stripped)
        except Exception:
            return True

    def _record_command_log(self, user_input: str, result: str, *, is_error: bool) -> None:
        if hasattr(self, "_command_log"):
            self._command_log.append((user_input, result, self._message_count(), is_error))

    def _refresh_banner(self) -> None:
        """Clear screen and re-render the welcome banner, then replay history with commands."""
        self.console.file.write("\033[H\033[2J\033[3J")
        self.console.file.flush()
        state = self.store.get_state()
        self.console.print(
            render_welcome_banner(state.model, state.cwd, session_id=self._session_id, session_name=self._session_name)
        )
        messages = self._agent_loop.context_manager.get_messages()
        if not messages and not self._command_log and not self._streaming_error_log:
            return
        # Build ordered list of (position, commands) for interleaving
        cmd_at: dict[int, list[tuple[str, str, bool]]] = {}
        for cmd_input, cmd_result, at, is_error in self._command_log:
            cmd_at.setdefault(at, []).append((cmd_input, cmd_result, is_error))
        # Build ordered list of (position, errors) for interleaving
        err_at: dict[int, list[str]] = {}
        for err_text, at in self._streaming_error_log:
            err_at.setdefault(at, []).append(err_text)
        # Find split points where commands/errors need to be inserted
        split_points = sorted(set(cmd_at.keys()) | set(err_at.keys()))
        # Replay messages in segments, inserting commands/errors between segments
        prev = 0
        has_output = False
        for point in split_points:
            if point > prev and prev < len(messages):
                if has_output:
                    self.console.print()
                self.renderer.replay_history(messages[prev : min(point, len(messages))])
                has_output = True
            # Replay streaming errors at this position
            for err_text in err_at.get(point, []):
                self.renderer.print_system_message(err_text, style="bold red")
                has_output = True
            # Replay commands at this position
            for cmd_input, cmd_result, is_error in cmd_at.get(point, []):
                if has_output:
                    self.console.print()
                self.renderer.print_user_message(cmd_input)
                if is_error:
                    self.renderer.print_system_message(cmd_result, style="red")
                else:
                    self.renderer.print_command_result(cmd_input, cmd_result)
                has_output = True
            prev = max(prev, point)
        # Replay remaining messages after last command/error
        if prev < len(messages):
            if has_output:
                self.console.print()
            self.renderer.replay_history(messages[prev:])

    # ------------------------------------------------------------------
    # Chat handling
    # ------------------------------------------------------------------

    async def _handle_chat_continue(self) -> list[str]:
        """Continue the agent loop after injecting messages (e.g., skill prompt).

        Unlike _handle_chat, this doesn't add a new user message — the messages
        were already injected into the context.
        """
        self.store.set_state(is_busy=True)
        try:
            streaming_input = StreamingInputBuffer()
            events = self._agent_loop.run_streaming(
                "",
                queued_input_provider=lambda: streaming_input.drain_queued_inputs(self._should_submit_mid_turn),
            )
            result = await self.renderer.run_streaming_output(
                events,
                permission_handler=self.renderer.prompt_permission,
                streaming_input=streaming_input,
            )
            elapsed, queued_inputs, draft_input = self._normalize_streaming_output_result(result)
            self._streaming_draft_input = draft_input
            if elapsed >= 1.0:
                self._agent_loop.stamp_last_turn_elapsed(elapsed)
            if self.renderer._last_streaming_errors:
                msg_count = len(self._agent_loop.context_manager.get_messages())
                for err in self.renderer._last_streaming_errors:
                    self._streaming_error_log.append((err, msg_count))
            return queued_inputs
        finally:
            self.store.set_state(is_busy=False)

    async def _handle_chat(self, user_input: PromptInputResult | str) -> list[str]:
        """Send the user message to the agent loop and stream output."""
        from iac_code.agent.message import ContentBlock, ImageBlock
        from iac_code.utils.image.processor import process_user_input

        if isinstance(user_input, PromptInputResult):
            blocks = process_user_input(user_input.text, pasted_contents=user_input.pasted_contents)
            # Only switch to a structured payload if we actually have an image block;
            # otherwise the plain string keeps telemetry / session storage simpler.
            payload: str | list[ContentBlock]
            if any(isinstance(b, ImageBlock) for b in blocks):
                payload = blocks
            else:
                payload = user_input.text
            record_text = user_input.text
        else:
            payload = user_input
            record_text = user_input

        self.store.set_state(is_busy=True)
        self.renderer.record_user_turn(record_text)
        try:
            streaming_input = StreamingInputBuffer()
            events = self._agent_loop.run_streaming(
                payload,
                queued_input_provider=lambda: streaming_input.drain_queued_inputs(self._should_submit_mid_turn),
            )
            result = await self.renderer.run_streaming_output(
                events,
                permission_handler=self.renderer.prompt_permission,
                streaming_input=streaming_input,
            )
            elapsed, queued_inputs, draft_input = self._normalize_streaming_output_result(result)
            self._streaming_draft_input = draft_input
            if elapsed >= 1.0:
                self._agent_loop.stamp_last_turn_elapsed(elapsed)
            if self.renderer._last_streaming_errors:
                msg_count = len(self._agent_loop.context_manager.get_messages())
                for err in self.renderer._last_streaming_errors:
                    self._streaming_error_log.append((err, msg_count))
            return queued_inputs
        finally:
            self.store.set_state(is_busy=False)

    @staticmethod
    def _clear_cancel_state() -> None:
        """Reset residual cancellation state on the current task.

        When the renderer internally catches CancelledError (e.g. from
        Ctrl+C during streaming), the task's ``_num_cancels_requested``
        counter stays positive even though the error was handled.  This
        can interfere with subsequent ``await`` calls.  Calling
        ``uncancel()`` drains the counter back to zero.

        ``Task.cancelling()`` and ``Task.uncancel()`` were added in
        Python 3.11; on 3.10 the internal counter does not exist, so
        the workaround is unnecessary and safely skipped.
        """
        task = asyncio.current_task()
        if task:
            _cancelling = getattr(task, "cancelling", None)
            _uncancel = getattr(task, "uncancel", None)
            if _cancelling is not None and _uncancel is not None:
                while _cancelling():
                    _uncancel()

    # ------------------------------------------------------------------
    # State change callback
    # ------------------------------------------------------------------

    def _on_state_change(self, state: AppState) -> None:
        """React to state changes — reinitialize provider when any provider config changes."""
        from iac_code.config import load_active_provider_config

        current_config = load_active_provider_config()
        if state.model != self._current_model or current_config != self._current_provider_config:
            self._reinitialize_provider(state.model)

    def _reinitialize_provider(self, new_model: str) -> None:
        """Apply a provider/model switch in place.

        Mutates the single shared ProviderManager so AgentTool / SkillTool
        — which captured this manager at registration — pick up the change
        without re-registration. Then notifies the AgentLoop so its
        ContextManager refreshes the tokenizer/context-window config and
        the system prompt for any memory/skill updates. Recreating the
        loop would discard conversation history.
        """
        from iac_code.config import load_active_provider_config

        self._current_model = new_model
        self._current_provider_config = load_active_provider_config()
        self._provider_key_override = None
        self._base_url_override = None
        self._credentials = self._load_credentials()
        from iac_code.config import _get_env_overrides, get_llm_source

        env = _get_env_overrides()
        if not env["api_key"] and get_llm_source() == "qwenpaw":
            from iac_code.services.qwenpaw_source import QwenPawError, load_from_qwenpaw

            try:
                qwenpaw_config = load_from_qwenpaw()
            except QwenPawError as exc:
                Console(stderr=True).print(str(exc), style="bold red")
                raise SystemExit(1)
            if qwenpaw_config:
                self._current_model = qwenpaw_config.model
                self.store.set_state(model=qwenpaw_config.model)
                self._credentials = {qwenpaw_config.provider_key: qwenpaw_config.api_key or ""}
                self._provider_key_override = qwenpaw_config.provider_key
                self._base_url_override = qwenpaw_config.base_url
        self._provider_manager.reconfigure(
            self._current_model,
            self._credentials,
            provider_key_override=self._provider_key_override,
            base_url_override=self._base_url_override,
        )
        memory_content = ""
        if hasattr(self, "_memory_manager") and self._memory_manager:
            memory_content = self._memory_manager.get_prompt_content()
        skill_listing = getattr(self, "_skill_listing", "")
        new_system_prompt = build_system_prompt(
            cwd=os.getcwd(), memory_content=memory_content, skill_listing=skill_listing
        )
        self._agent_loop.set_provider(self._provider_manager, system_prompt=new_system_prompt)

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _record_command_history(self, user_input: str) -> None:
        """Record a slash command to history respecting its history_mode."""
        name, _ = self.command_registry.parse(user_input)
        cmd = self.command_registry.get(name)
        if cmd is None or not isinstance(cmd, LocalCommand):
            self._history.append(user_input)
            return
        if cmd.history_mode == "none":
            self._history.reset_navigation()
            return
        if cmd.history_mode == "session":
            self._history.append(user_input, persist=False)
            return
        self._history.append(user_input)

    def _print_exit_text(self) -> None:
        """Print the session resume hint shown when the REPL exits."""
        from rich.text import Text

        resume_arg = self._session_name or self._session_id
        self.console.print("[dim]{}[/dim]".format(_("Goodbye!")))
        self.console.print(Text(_("Resume this session with:"), style="dim"))
        self.console.print(Text("iac-code --resume {}".format(resume_arg), style="dim"))
        if self._session_name:
            self.console.print(Text("{}: {}".format(_("Session ID"), self._session_id), style="dim"))

    def _apply_qwenpaw_config(self, model: str) -> None:
        """Apply QwenPaw config if active and env vars don't override."""
        from iac_code.config import _get_env_overrides, get_llm_source

        env = _get_env_overrides()
        if env["api_key"]:
            return
        if get_llm_source() != "qwenpaw":
            return
        from iac_code.services.qwenpaw_source import QwenPawError, load_from_qwenpaw

        try:
            qwenpaw_config = load_from_qwenpaw()
        except QwenPawError as exc:
            Console(stderr=True).print(str(exc), style="bold red")
            raise SystemExit(1)
        if qwenpaw_config:
            self._current_model = qwenpaw_config.model
            self.store = AppStateStore(initial_state=AppState(model=qwenpaw_config.model))
            self._credentials = {qwenpaw_config.provider_key: qwenpaw_config.api_key or ""}
            self._provider_key_override = qwenpaw_config.provider_key
            self._base_url_override = qwenpaw_config.base_url

    def _load_credentials(self) -> dict[str, str]:
        """Load API credentials (delegates to config.load_credentials with env overlay)."""
        return load_credentials(model=self._current_model)

    def _resolve_session_id(self, resume: str | bool | None) -> str:
        """Resolve session ID for resume or create new.

        For ``--continue`` and ``--resume <id>``, sessions belonging to a
        *different* working directory are rejected with a helpful error
        instructing the user to cd into the right project first — matches
        our project-partitioned storage layout.
        """
        import uuid

        if resume is True:
            latest = self._session_storage.get_latest_session_anywhere()
            if latest is None:
                return str(uuid.uuid4())
            cwd, sid = latest
            if cwd and not same_project_path(cwd, self._original_cwd):
                raise ValueError(self._cross_project_message(cwd, sid))
            return sid
        if isinstance(resume, str) and resume:
            resolution = resolve_session_argument(self.session_index, self._original_cwd, resume)
            if resolution.status == ResolutionStatus.NOT_FOUND:
                raise ValueError(_("Session not found: {session_id}").format(session_id=resume))
            if resolution.status == ResolutionStatus.AMBIGUOUS_NAME:
                raise ValueError(self._ambiguous_resume_message(resolution.candidates))
            if resolution.entry is None:
                raise ValueError(_("Session not found: {session_id}").format(session_id=resume))
            if resolution.entry.cwd and not same_project_path(resolution.entry.cwd, self._original_cwd):
                raise ValueError(self._cross_project_message(resolution.entry.cwd, resolution.entry.session_id))
            return resolution.entry.session_id
        return str(uuid.uuid4())

    def _load_resume_messages(self, resume: str | bool | None) -> list:
        """Load and repair saved messages when resuming a session."""
        if resume is None:
            return []
        messages = self._session_storage.load(self._original_cwd, self._session_id)
        return self._session_storage.repair_interrupted(messages)

    def _load_current_session_name(self) -> str | None:
        """Read the persisted display name for the active session."""
        metadata = self._session_storage.read_metadata(self._original_cwd, self._session_id)
        return metadata.name if metadata else None

    def current_git_branch(self) -> str | None:
        """Return the current git branch for the REPL's original directory."""
        try:
            result = subprocess.run(
                ["git", "-C", self._original_cwd, "rev-parse", "--abbrev-ref", "HEAD"],
                capture_output=True,
                check=False,
                text=True,
            )
        except OSError:
            return None
        if result.returncode != 0:
            return None
        branch = result.stdout.strip()
        return branch if branch and branch != "HEAD" else None

    def rename_current_session(self, name: str) -> str:
        """Rename the active session and refresh cached session metadata."""
        result = self._session_storage.rename_session(
            self._original_cwd,
            self._session_id,
            name,
            git_branch=self.current_git_branch(),
        )
        self._session_name = self._load_current_session_name()
        return result

    async def prompt_for_session_name(self) -> str | None:
        """Prompt until the user provides a valid session name or cancels."""
        while True:
            try:
                raw_name = await self._prompt_input.get_input(_("Session name: "))
            except (EOFError, KeyboardInterrupt):
                return None
            if raw_name is None:
                return None
            if not raw_name.strip():
                self.renderer.print_system_message(_("Session name cannot be empty."), style="red")
                continue
            try:
                return normalize_session_name(raw_name)
            except ValueError as exc:
                self.renderer.print_system_message(str(exc), style="red")

    @staticmethod
    def _cross_project_message(cwd: str, session_id: str) -> str:
        cmd = format_resume_command(cwd, session_id)
        return _("This session belongs to a different directory.\nTo resume, run:\n  {cmd}").format(cmd=cmd)

    @staticmethod
    def _ambiguous_resume_message(entries) -> str:
        lines = [_("Multiple sessions match. Resume one by ID:"), ""]
        for entry in entries:
            cmd = format_resume_command(entry.cwd, entry.session_id)
            lines.append(f"  {cmd}")
        return "\n".join(lines)

    @property
    def session_id(self) -> str:
        return self._session_id

    def get_status_snapshot(self) -> dict[str, Any]:
        state = self.store.get_state()
        messages = self._agent_loop.context_manager.get_messages()
        return {
            "session_id": self._session_id,
            "resumed": self._was_resumed,
            "provider": self._status_provider_display(),
            "model": self._status_model(state.model),
            "region": self._status_region(),
            "cwd": self._original_cwd,
            "api_usage": self._agent_loop.get_session_usage(),
            "turn_count": self._count_user_turns(messages),
            "max_turns": self._agent_loop.max_turns,
            "context_usage": self._agent_loop.get_context_usage(),
        }

    def _status_provider_display(self) -> str:
        if hasattr(self._provider_manager, "get_provider_display"):
            try:
                display = self._provider_manager.get_provider_display()
            except Exception:
                display = ""
            if isinstance(display, str) and display:
                return display
        key = get_active_provider_key()
        if not key:
            return ""
        descriptor = PROVIDER_REGISTRY.get(key)
        if descriptor is not None:
            return descriptor.display_name
        return key

    def _status_model(self, fallback: str) -> str:
        if hasattr(self._provider_manager, "get_model_name"):
            try:
                model = self._provider_manager.get_model_name()
            except Exception:
                model = ""
            if isinstance(model, str) and model:
                return model
        return fallback

    @staticmethod
    def _status_region() -> str:
        from iac_code.services.cloud_credentials import CloudCredentials

        credential = CloudCredentials().get_provider("aliyun")
        return credential.region_id if credential and credential.region_id else ""

    @staticmethod
    def _count_user_turns(messages: list) -> int:
        from iac_code.agent.message import ToolResultBlock

        turns = 0
        for message in messages:
            if getattr(message, "role", None) != "user":
                continue
            content = getattr(message, "content", "")
            if isinstance(content, list) and any(isinstance(block, ToolResultBlock) for block in content):
                continue
            turns += 1
        return turns

    # ------------------------------------------------------------------
    # Session swap (used by /resume command)
    # ------------------------------------------------------------------

    def swap_session(self, new_session_id: str) -> None:
        """Replace the active session in-place (same project only)."""
        from iac_code.services.permissions.trusted_roots import build_session_trusted_read_directories

        old_session_id = self._session_id
        new_messages = self._session_storage.load(self._original_cwd, new_session_id)
        new_messages = self._session_storage.repair_interrupted(new_messages)
        self._agent_loop.replace_session(new_session_id, new_messages or None)
        self._session_id = new_session_id
        self._was_resumed = True
        self._session_name = self._load_current_session_name()

        state = self.store.get_state()
        permission_context = state.permission_context
        if permission_context is not None:
            old_roots = set(build_session_trusted_read_directories(old_session_id))
            permission_context.trusted_read_directories = [
                root for root in permission_context.trusted_read_directories if root not in old_roots
            ]
            permission_context.trusted_read_directories.extend(build_session_trusted_read_directories(new_session_id))

        # Clear screen + scrollback, redraw banner, replay history.
        self.console.file.write("\033[H\033[2J\033[3J")
        self.console.file.flush()
        self.console.print(
            render_welcome_banner(
                state.model,
                state.cwd,
                session_id=new_session_id,
                session_name=self._session_name,
            )
        )
        if new_messages:
            self.renderer.replay_history(new_messages)
            self.console.print()

    async def swap_or_announce_session(self, entry) -> None:
        """Hot-swap if same project; otherwise print the resume command."""
        if entry.cwd and same_project_path(entry.cwd, self._original_cwd):
            self.swap_session(entry.session_id)
            return
        await self._announce_cross_project(entry)

    async def _announce_cross_project(self, entry) -> None:
        cmd = format_resume_command(entry.cwd, entry.session_id)
        msg_lines = [
            "",
            _("This conversation is from a different directory."),
            "",
            _("To resume, run:"),
            f"  {cmd}",
        ]
        if self._copy_to_clipboard(cmd):
            msg_lines.append("")
            msg_lines.append(_("(Command copied to clipboard)"))
        self.renderer.print_system_message("\n".join(msg_lines))

    @staticmethod
    def _copy_to_clipboard(text: str) -> bool:
        """Best-effort clipboard copy. Returns True on success."""
        import subprocess

        candidates: list[list[str]] = []
        if sys.platform == "darwin":
            candidates.append(["pbcopy"])
        elif sys.platform.startswith("linux"):
            candidates.append(["wl-copy"])
            candidates.append(["xclip", "-selection", "clipboard"])
        elif sys.platform.startswith("win"):
            candidates.append(["clip"])
        for cmd in candidates:
            try:
                proc = subprocess.run(cmd, input=text, text=True, timeout=2.0, check=False)
                if proc.returncode == 0:
                    return True
            except (FileNotFoundError, OSError, subprocess.TimeoutExpired):
                continue
        return False

    # ------------------------------------------------------------------
    # last-prompt persistence
    # ------------------------------------------------------------------

    def _write_last_prompt_meta(self) -> None:
        """Append a ``last-prompt`` lite-meta row to the session file.

        Reads back from the in-memory context manager rather than the file
        so we don't double-parse. Silently no-ops if there's no usable
        text or the write fails.
        """
        try:
            messages = self._agent_loop.context_manager.get_messages()
        except Exception:
            return
        text = self._extract_last_user_text(messages)
        if not text:
            return
        flat = text.replace("\n", " ").strip()
        if len(flat) > 200:
            flat = flat[:200].rstrip() + "…"
        try:
            self._session_storage.append_meta(
                self._original_cwd,
                self._session_id,
                {"type": "last-prompt", "last_prompt": flat},
            )
        except Exception:
            pass

    @staticmethod
    def _extract_last_user_text(messages: list) -> str:
        """Walk messages from newest to oldest, return first plain user text."""
        from iac_code.agent.message import TextBlock

        for msg in reversed(messages):
            if msg.role != "user":
                continue
            content = msg.content
            if isinstance(content, str):
                if content.strip():
                    return content
                continue
            if isinstance(content, list):
                texts = [block.text for block in content if isinstance(block, TextBlock) and block.text]
                if texts:
                    return " ".join(texts)
        return ""

    # ------------------------------------------------------------------
    # Renderer callback
    # ------------------------------------------------------------------

    def _status_text(self) -> str:
        return self.store.get_state().model


# CSI I / CSI O are focus-in / focus-out events. Some terminals (notably on
# macOS) emit one or both around a paste because Cmd+V briefly steals focus
# to the menu bar. When this lands inside our bracketed-paste content it
# corrupts otherwise-empty payloads. Strip every occurrence regardless of
# position; mid-paste focus events should never reach the prompt buffer.
_FOCUS_EVENT_RE = re.compile(r"\x1b\[[IO]")


def _strip_orphan_focus_events(text: str) -> str:
    """Remove CSI focus-in/focus-out sequences from a paste payload."""
    return _FOCUS_EVENT_RE.sub("", text)


def _is_existing_non_image_file(text: str) -> bool:
    """Return True if *text* looks like a path to an existing non-image file.

    Handles shell-quoted paths and backslash-escaped spaces that macOS Finder
    places on the clipboard when copying files.
    """
    from pathlib import Path

    if not text or not text.strip():
        return False
    candidate = text.strip().strip("'\"").replace("\\ ", " ")
    # Must look like a filesystem path (absolute or relative that exists)
    if not candidate:
        return False
    # Skip if it matches a known image extension — let try_read_image_from_path handle those
    if IMAGE_EXTENSION_REGEX.search(candidate):
        return False
    p = Path(candidate)
    try:
        return p.exists() and p.is_file()
    except (OSError, ValueError):
        return False


def _attach_clipboard_image(repl: "InlineREPL", img: ClipboardImage) -> bool:
    """Run multimodal-capability gate, resize, persist to cache, and attach
    the image as an ``[Image #N]`` placeholder in the prompt buffer.

    Always returns True — either the image was attached, or a user-visible
    warning was scheduled (capability mismatch, resize failure). Callers
    should treat True as "event handled, do not fall through".
    """
    from iac_code.services.capabilities.multimodal import is_model_multimodal
    from iac_code.utils.image.pasted_content import PastedContent
    from iac_code.utils.image.resizer import (
        ImageResizeError,
        maybe_resize_and_downsample,
    )

    # Pass real provider context so the OpenAI-compatible auto-detect can fire
    # for unknown models on a custom endpoint.
    provider_key: str | None = None
    base_url: str | None = None
    api_key: str | None = None
    cfg = getattr(repl, "_current_provider_config", None)
    if isinstance(cfg, dict):
        provider_key = cfg.get("keyName")
        base_url = cfg.get("apiBase")
    creds = getattr(repl, "_credentials", None)
    if isinstance(creds, dict) and provider_key:
        api_key = creds.get(provider_key)

    if not is_model_multimodal(
        repl._current_model,
        provider_key=provider_key,
        base_url=base_url,
        api_key=api_key,
    ):
        logger.warning(
            "repl: model {} does not support multimodal input — refusing to attach image", repl._current_model
        )
        msg = _(
            "Current model {model} does not support image input. Use /model to switch to a vision-capable model."
        ).format(model=repl._current_model)
        repl._prompt_input.schedule_action(lambda: repl.renderer.print_system_message(msg, style="yellow"))
        return True

    try:
        resized = maybe_resize_and_downsample(img.data)
    except ImageResizeError as exc:
        logger.warning("repl: image resize/encode failed: {}", exc)
        msg = _("Image error: {err}").format(err=exc)
        repl._prompt_input.schedule_action(lambda: repl.renderer.print_system_message(msg, style="red"))
        return True

    import base64

    pid = repl._prompt_input.next_paste_id()
    pc = PastedContent(
        id=pid,
        type="image",
        content=base64.b64encode(resized.data).decode(),
        media_type=resized.media_type,
        source_path=img.source_path,
    )
    stored_path = repl._image_store.store(pc)
    if stored_path is None:
        logger.warning("repl: image store persistence failed — keeping in-memory only")
        msg = _("Failed to persist image to cache; it will only exist in memory for this turn.")
        repl._prompt_input.schedule_action(lambda: repl.renderer.print_system_message(msg, style="yellow"))
    logger.info(
        "repl: image attached as [Image #{}] (media_type={}, {} bytes raw → {} bytes encoded)",
        pid,
        resized.media_type,
        len(img.data),
        len(resized.data),
    )
    repl._prompt_input.attach_image(pc)
    return True


def handle_image_paste(repl: "InlineREPL") -> bool:
    """Handle Ctrl+V image paste. Returns True if the keybinding was consumed.

    Legacy entry point. The lazy import of ``get_image_from_clipboard`` is
    preserved so existing tests that patch
    ``iac_code.utils.image.clipboard.get_image_from_clipboard`` keep working.
    """
    from iac_code.utils.image.clipboard import get_image_from_clipboard as _get

    img = _get()
    if img is None:
        return False  # let bracket paste handle text
    return _attach_clipboard_image(repl, img)
