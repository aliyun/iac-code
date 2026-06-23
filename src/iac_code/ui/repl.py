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
from datetime import datetime
from io import StringIO
from types import ModuleType
from typing import TYPE_CHECKING, Any, Literal, cast

from loguru import logger
from rich.console import Console, Group
from rich.live import Live
from rich.text import Text

from iac_code.agent.agent_loop import AgentLoop
from iac_code.agent.message import ContentBlock, ImageBlock, Message
from iac_code.agent.system_prompt import build_system_prompt
from iac_code.commands import create_default_registry
from iac_code.commands.registry import CommandResult, LocalCommand, PromptCommand
from iac_code.config import get_active_provider_key, get_config_dir, get_history_path, load_credentials
from iac_code.i18n import _
from iac_code.memory.memory_manager import MemoryManager
from iac_code.memory.project_memory import ProjectMemoryRuntime
from iac_code.memory.recall import MemoryRecallService
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
from iac_code.types.stream_events import (
    AskUserQuestionEvent,
    CandidateDetailEvent,
    DiagramEvent,
    PermissionRequestEvent,
    SubPipelineStreamEvent,
    TextDeltaEvent,
    ToolInputDeltaEvent,
    ToolUseStartEvent,
)
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
from iac_code.utils.json_utils import extract_json_int_value, extract_json_string_value
from iac_code.utils.project_paths import format_resume_command, same_project_path

if TYPE_CHECKING:
    from iac_code.pipeline import PipelineRunner
    from iac_code.pipeline.config import RunMode
    from iac_code.pipeline.engine.events import PipelineEvent
    from iac_code.pipeline.engine.user_input import PipelineUserInput

termios: ModuleType | None
try:
    import termios as _termios
except ImportError:  # Windows
    termios = None
else:
    termios = _termios


# Slash commands that remain available mid-pipeline regardless of the
# pipeline's allow_user_escapes.command setting (problem 5). Permanent whitelist
# so users are never locked out of the basics while a pipeline is running.
_PIPELINE_SAFE_COMMANDS: frozenset[str] = frozenset({"/exit", "/help", "/status", "/prompt", "/resume"})
PipelineHandoffResult = Literal["not_applicable", "succeeded", "failed"]


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
        self._runtime_current_time = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
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
        self._runtime_mode = self._resolve_initial_runtime_mode(resume_session_id)
        from iac_code.utils.image.store import ImageStore

        self._image_store = ImageStore(session_id=self._session_id)
        self._resume_messages = self._load_resume_messages(resume_session_id)
        self._session_name = self._load_current_session_name()
        self._task_manager = TaskManager()
        self._notification_queue = NotificationQueue()
        self._command_log: list[tuple[str, str, int, bool]] = []
        self._streaming_error_log: list[tuple[str, int]] = []

        legacy_memory_dir = str(get_config_dir() / "memory")
        self._legacy_memory_manager = MemoryManager(memory_dir=legacy_memory_dir)
        self._memory_runtime = ProjectMemoryRuntime(self._original_cwd)
        self._memory_manager = self._memory_runtime.memory_manager
        self._memory_recall_service = MemoryRecallService(
            memory_manager=self._memory_manager,
            provider_manager=self._provider_manager,
        )

        # Register new tools
        from iac_code.agent.agent_tool import AgentTool
        from iac_code.memory.memory_tools import ReadMemoryTool, WriteMemoryTool
        from iac_code.tasks.task_tools import TaskGetTool, TaskListTool, TaskStopTool

        memory_context = self._refresh_memory_context()
        self.tool_registry.register(
            AgentTool(
                task_manager=self._task_manager,
                provider_manager=self._provider_manager,
                tool_registry=self.tool_registry,
                system_prompt=build_system_prompt(
                    cwd=os.getcwd(),
                    memory_context=memory_context,
                    current_time=self._runtime_current_time,
                ),
                notification_queue=self._notification_queue,
            )
        )
        self.tool_registry.register(ReadMemoryTool(self._memory_manager))
        self.tool_registry.register(WriteMemoryTool(self._memory_manager))
        self.tool_registry.register(TaskListTool(self._task_manager))
        self.tool_registry.register(TaskGetTool(self._task_manager))
        self.tool_registry.register(TaskStopTool(self._task_manager))

        cwd = os.getcwd()
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
                cwd=cwd,
                memory_context=memory_context,
                skill_listing=self._skill_listing,
                current_time=self._runtime_current_time,
            ),
            tool_registry=self.tool_registry,
            session_storage=self._session_storage,
            session_id=self._session_id,
            resume_messages=self._resume_messages or None,
            cwd=self._original_cwd,
            permission_context=permission_context,
            permission_context_getter=lambda: self.store.get_state().permission_context,
            auto_trigger_skills=skill_commands,
            memory_recall_service=self._memory_recall_service,
            system_prompt_refresher=self._build_current_system_prompt,
        )
        self.renderer = Renderer(
            self.console,
            self.tool_registry,
            status_callback=self._status_text,
            app_state_store=self.store,
            image_path_resolver=self._image_store.get_path,
            image_block_path_resolver=self._image_store.store_block,
        )

        self._pipeline: PipelineRunner | None = None
        self._pipeline_waiting_input: bool = False
        self._pipeline_restored_status: str | None = None
        self._pipeline_display_recorder = None
        self._pipeline_display_current_step_id: str | None = None

        # Keybinding manager
        self._keybinding_manager = KeybindingManager()

        # Input history
        self._history = InputHistory(str(get_history_path()))

        # Suggestion aggregator with all 4 providers
        cwd = os.getcwd()
        self._suggestion_aggregator = SuggestionAggregator(
            [
                CommandProvider(self.command_registry, memory_manager=self._legacy_memory_manager),
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

    def _refresh_memory_context(self):
        runtime = getattr(self, "_memory_runtime", None)
        if runtime is None:
            return getattr(self, "_memory_context", None)
        self._memory_context = runtime.build_memory_context()
        return self._memory_context

    def _build_current_system_prompt(self) -> str:
        return build_system_prompt(
            cwd=os.getcwd(),
            memory_context=self._refresh_memory_context(),
            skill_listing=getattr(self, "_skill_listing", ""),
            current_time=getattr(self, "_runtime_current_time", None),
        )

    def _refresh_system_prompt(self) -> str:
        system_prompt = self._build_current_system_prompt()
        agent_loop = getattr(self, "_agent_loop", None)
        if agent_loop is not None:
            agent_loop.set_provider(self._provider_manager, system_prompt=system_prompt)
        return system_prompt

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

        memory_context = self._refresh_memory_context()
        self.tool_registry.register(
            SkillTool(
                command_registry=self.command_registry,
                disabled_skills=self._disabled_skill_commands,
                session_id=self._session_id,
                cwd=cwd,
                provider_manager=self._provider_manager,
                tool_registry=self.tool_registry,
                system_prompt=build_system_prompt(
                    cwd=cwd,
                    memory_context=memory_context,
                    current_time=self._runtime_current_time,
                ),
            )
        )

        skill_commands = self.command_registry.get_model_invocable_skills()
        self._skill_listing = build_skill_listing(skill_commands)

        if hasattr(self, "_agent_loop"):
            self._agent_loop.set_auto_trigger_skills(skill_commands)
            self._agent_loop.set_provider(self._provider_manager, system_prompt=self._build_current_system_prompt())

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
            self._replay_resume_messages(self._resume_messages)
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
        loop = asyncio.get_running_loop()
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

        if initial_prompt is None:
            resumed_pipeline = await self._resume_pipeline_sidecar_on_startup()
            if not resumed_pipeline:
                await self._maybe_start_normal_chat_cleanup_on_startup()

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

                    # 问题 5：pipeline 模式 gate user escapes
                    if self._maybe_block_user_escape(user_input):
                        self._clear_cancel_state()
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
        from iac_code.agent.message import RECALLED_MEMORY_MARKER, is_recalled_memory_message
        from iac_code.pipeline.engine.cleanup import is_cleanup_prompt_message

        entries: list[str] = []
        seen: set[str] = set()

        def add_text(text: str) -> None:
            cleaned = text.strip()
            if not cleaned or cleaned in seen:
                return
            if RECALLED_MEMORY_MARKER in cleaned:
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
            if (
                is_recalled_memory_message(msg)
                or is_cleanup_prompt_message(msg)
                or Renderer.is_internal_skill_context_message(msg)
            ):
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

    def _is_pipeline_safe_command(self, user_input: str) -> bool:
        """Commands always allowed mid-pipeline regardless of allow_user_escapes.command."""
        first = user_input.split(None, 1)[0] if user_input else ""
        return first in _PIPELINE_SAFE_COMMANDS

    def _pipeline_memory_content_getter(self) -> None:
        """Return pipeline prompt memory provider.

        Pipeline steps should not receive all auto-memory topic bodies in the
        system prompt. They also intentionally do not receive MemoryRecallService,
        so no side recall is triggered. Relevant topic memories are available
        through the explicit read_memory tool when a step's tool policy allows it.
        """
        return None

    def _maybe_block_user_escape(self, user_input: str) -> bool:
        """Return True if the input is a gated escape and we should NOT process it.

        Side effect: prints a yellow system message explaining why.
        """
        if self._pipeline is not None:
            escapes = self._pipeline.allow_user_escapes
        else:
            from iac_code.pipeline.config import RunMode

            if self._get_runtime_mode() != RunMode.PIPELINE:
                return False
            from iac_code.pipeline.engine.step_spec import AllowUserEscapes

            escapes = AllowUserEscapes()
        if user_input.startswith("!") and not escapes.shell:
            self.renderer.print_system_message(
                _("Shell escapes are disabled in this pipeline."),
                style="yellow",
            )
            return True
        if user_input.startswith("$") and not escapes.skill:
            self.renderer.print_system_message(
                _("Skill triggers are disabled in this pipeline."),
                style="yellow",
            )
            return True
        if self.command_registry.is_command(user_input) and not escapes.command:
            if not self._is_pipeline_safe_command(user_input):
                allowed = ", ".join(sorted(_PIPELINE_SAFE_COMMANDS))
                self.renderer.print_system_message(
                    _("Slash commands are disabled in this pipeline. Allowed: {allowed}").format(allowed=allowed),
                    style="yellow",
                )
                return True
        return False

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

    def _replay_resume_messages(self, messages: list[Message]) -> None:
        model = self._load_pipeline_display_replay_model()
        split_at = self._pipeline_display_replay_insert_index(messages) if model is not None else None
        if model is None or split_at is None:
            self.renderer.replay_history(self._pipeline_visible_resume_messages(messages))
            return
        before = self._pipeline_visible_resume_messages(messages[:split_at])
        after = self._pipeline_visible_resume_messages(messages[split_at:])
        if before:
            self.renderer.replay_history(before)
        from iac_code.ui.pipeline_display_replay import PipelineDisplayReplayRenderer

        PipelineDisplayReplayRenderer(
            self.console,
            history_replayer=self.renderer.replay_history,
            history_renderable_factory=self._render_pipeline_display_transcript_window,
            transcript_loader=self._load_pipeline_display_transcript_messages,
        ).render(model)
        if after:
            self.console.print()
            self.renderer.replay_history(after)

    @classmethod
    def _pipeline_display_replay_insert_index(cls, messages: list[Message]) -> int | None:
        abort_notice = cls._pipeline_abort_notice_text()
        for index, message in enumerate(messages):
            if message.role == "assistant" and message.content == abort_notice:
                return index
        for index, message in enumerate(messages):
            if cls._is_pipeline_handoff_context_message(message):
                return index
        last_user_index = None
        for index, message in enumerate(messages):
            if message.role == "user" and isinstance(message.content, str):
                last_user_index = index
        return last_user_index + 1 if last_user_index is not None else None

    @classmethod
    def _pipeline_visible_resume_messages(cls, messages: list[Message]) -> list[Message]:
        from iac_code.pipeline.engine.cleanup import is_cleanup_prompt_message

        return [
            message
            for message in messages
            if not cls._is_pipeline_handoff_context_message(message) and not is_cleanup_prompt_message(message)
        ]

    @staticmethod
    def _is_pipeline_handoff_context_message(message: Message) -> bool:
        return isinstance(message.content, str) and message.content.startswith("[Pipeline Handoff Context]")

    def _load_pipeline_display_replay_model(self, *, include_nonterminal: bool = False):
        from pathlib import Path

        from iac_code.pipeline.config import get_working_directory
        from iac_code.pipeline.engine.display_replay import (
            DISPLAY_TRANSCRIPT_FILENAME,
            PipelineDisplayReducer,
            load_display_events,
        )

        pipeline_cwd = get_working_directory() or self._original_cwd
        terminal_status = self._terminal_pipeline_status(pipeline_cwd, self._session_id)
        allowed_statuses = {"completed", "failed", "user_aborted"}
        if include_nonterminal:
            allowed_statuses.update({"running", "waiting_input"})
        if terminal_status not in allowed_statuses:
            return None
        try:
            display_path = (
                Path(self._session_storage.session_dir(pipeline_cwd, self._session_id))
                / "pipeline"
                / DISPLAY_TRANSCRIPT_FILENAME
            )
            events = load_display_events(display_path)
            if not events:
                return None
            from iac_code.pipeline.engine.session import PipelineSession

            sidecar = PipelineSession(
                Path(self._session_storage.session_dir(pipeline_cwd, self._session_id)) / "pipeline"
            )
            restore_result = sidecar.restore_sync({})
            model = PipelineDisplayReducer().reduce(events, restore_result.attempts)
            return model if model.attempts else None
        except Exception as exc:
            logger.warning("Failed to load pipeline display replay model: {}", exc)
            return None

    def _load_pipeline_display_transcript_messages(self, transcript_id: str) -> list[Message]:
        from pathlib import Path

        from iac_code.pipeline.config import get_working_directory
        from iac_code.pipeline.engine.transcript_storage import PipelineTranscriptStorage

        if not isinstance(transcript_id, str) or not transcript_id:
            return []
        pipeline_cwd = get_working_directory() or self._original_cwd
        try:
            sidecar_dir = Path(self._session_storage.session_dir(pipeline_cwd, self._session_id)) / "pipeline"
            transcript_storage = PipelineTranscriptStorage(sidecar_dir)
            loaded = transcript_storage.load(pipeline_cwd, transcript_id)
            return transcript_storage.repair_interrupted(loaded)
        except Exception as exc:
            logger.warning("Failed to load pipeline display transcript {}: {}", transcript_id, exc)
            return []

    def _render_pipeline_display_transcript_window(self, messages: list[Message]) -> Text:
        if not messages:
            return Text("")

        stream = StringIO()
        width = self.console.width or 100
        height = self.console.height or 24
        temp_console = Console(file=stream, force_terminal=True, width=width, height=height)
        temp_renderer = Renderer(
            temp_console,
            self.tool_registry,
            status_callback=self._status_text,
            app_state_store=self.store,
            image_path_resolver=self._image_store.get_path,
            image_block_path_resolver=self._image_store.store_block,
        )
        temp_renderer.replay_history(messages)
        rendered = stream.getvalue().rstrip()
        if not rendered:
            return Text("")
        return Text.from_ansi(self._tail_pipeline_display_window(rendered, max_lines=max(height - 8, 5)))

    @staticmethod
    def _tail_pipeline_display_window(text: str, *, max_lines: int) -> str:
        lines = text.splitlines()
        if len(lines) <= max_lines:
            return text
        return "\n".join(lines[-max_lines:])

    # ------------------------------------------------------------------
    # Chat handling
    # ------------------------------------------------------------------

    def _get_runtime_mode(self) -> RunMode:
        from iac_code.pipeline.config import get_run_mode

        runtime_mode = getattr(self, "_runtime_mode", None)
        if runtime_mode is not None:
            return runtime_mode
        return get_run_mode()

    def _set_runtime_mode(self, mode: RunMode) -> None:
        self._runtime_mode = mode

    async def _handle_chat_continue(self) -> list[str]:
        """Continue the agent loop after injecting messages (e.g., skill prompt).

        Unlike _handle_chat, this doesn't add a new user message — the messages
        were already injected into the context.
        """
        # U-I17: not valid in pipeline mode (callers should use _handle_pipeline_chat).
        from iac_code.pipeline.config import RunMode

        if self._get_runtime_mode() == RunMode.PIPELINE:
            logger.error("_handle_chat_continue called in pipeline mode; this is a bug")
            return []

        if self._block_if_cleanup_ledger_unreadable():
            return []

        self.store.set_state(is_busy=True)
        try:
            streaming_input = StreamingInputBuffer()
            events = self._wrap_cleanup_observer(
                self._agent_loop.run_streaming(
                    "",
                    queued_input_provider=lambda: streaming_input.drain_queued_inputs(self._should_submit_mid_turn),
                )
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
            self._prune_cleanup_prompts_if_no_pending_cleanup()
            self.store.set_state(is_busy=False)

    def _cleanup_ledger_for_pipeline(self, pipeline: object | None):
        if pipeline is None:
            return None
        from iac_code.pipeline.engine.cleanup import CleanupLedger

        getter = getattr(pipeline, "cleanup_ledger", None)
        if not callable(getter):
            return None
        try:
            ledger = getter()
        except Exception:
            logger.warning("Failed to load pipeline cleanup ledger", exc_info=True)
            return None
        return ledger if isinstance(ledger, CleanupLedger) else None

    def _cleanup_ledger_for_normal_chat(self):
        from pathlib import Path

        from iac_code.pipeline.engine.cleanup import CleanupLedger

        prompt_path = self._cleanup_ledger_path_from_active_prompt()
        if prompt_path is not None:
            return CleanupLedger(prompt_path)

        explicit_path = getattr(self, "_pipeline_cleanup_ledger_path", None)
        if explicit_path:
            ledger = CleanupLedger(Path(explicit_path))
            has_active_prompt = self._cleanup_prompt_exists_anywhere()
            if has_active_prompt:
                return ledger
            if not ledger.path.exists():
                self._clear_pipeline_cleanup_ledger_path(ledger.path)
                return None
            if ledger.load_failed():
                return ledger
            if ledger.pending_resources():
                return ledger
            self._clear_pipeline_cleanup_ledger_path(ledger.path)
            return None

        has_active_prompt = self._cleanup_prompt_exists_anywhere()

        candidate_cwds: list[str] = []
        try:
            from iac_code.pipeline.config import get_working_directory

            pipeline_cwd = get_working_directory()
            if pipeline_cwd:
                candidate_cwds.append(pipeline_cwd)
        except Exception:
            pass
        original_cwd = getattr(self, "_original_cwd", None)
        if original_cwd:
            candidate_cwds.append(original_cwd)

        session_storage = getattr(self, "_session_storage", None)
        session_id = getattr(self, "_session_id", None)
        if session_storage is None or not isinstance(session_id, str) or not session_id:
            return None

        seen: set[str] = set()
        completed_prompt_ledger = None
        for cwd in candidate_cwds:
            if cwd in seen:
                continue
            seen.add(cwd)
            try:
                path = Path(session_storage.session_dir(cwd, session_id)) / "pipeline" / "cleanup.yaml"
            except Exception:
                continue
            if path.exists():
                ledger = CleanupLedger(path)
                if ledger.load_failed():
                    continue
                if ledger.pending_resources():
                    return ledger
                if has_active_prompt and completed_prompt_ledger is None:
                    completed_prompt_ledger = ledger
        return completed_prompt_ledger

    def _cleanup_ledger_for_resume_summary(self):
        from pathlib import Path

        from iac_code.pipeline.engine.cleanup import CleanupLedger

        prompt_path = self._cleanup_ledger_path_from_any_cleanup_prompt()
        if prompt_path is not None:
            return CleanupLedger(prompt_path)

        explicit_path = getattr(self, "_pipeline_cleanup_ledger_path", None)
        if explicit_path:
            return CleanupLedger(Path(explicit_path))

        return None

    def _clear_pipeline_cleanup_ledger_path(self, path=None) -> None:
        from pathlib import Path

        explicit_path = getattr(self, "_pipeline_cleanup_ledger_path", None)
        if explicit_path is None:
            return
        if path is not None:
            try:
                if Path(explicit_path) != Path(path):
                    return
            except TypeError:
                return
        try:
            delattr(self, "_pipeline_cleanup_ledger_path")
        except AttributeError:
            pass

    def _cleanup_ledger_path_from_active_prompt(self):
        from pathlib import Path

        from iac_code.pipeline.engine.cleanup import cleanup_prompt_ledger_path, is_active_cleanup_prompt_message

        for message in [*self._cleanup_prompt_messages_from_context(), *self._cleanup_prompt_messages_from_session()]:
            if not is_active_cleanup_prompt_message(message):
                continue
            ledger_path = cleanup_prompt_ledger_path(message)
            if ledger_path:
                return Path(ledger_path)
        return None

    def _cleanup_ledger_path_from_any_cleanup_prompt(self):
        from pathlib import Path

        from iac_code.pipeline.engine.cleanup import cleanup_prompt_ledger_path, is_cleanup_prompt_message

        for message in [*self._cleanup_prompt_messages_from_context(), *self._cleanup_prompt_messages_from_session()]:
            if not is_cleanup_prompt_message(message):
                continue
            ledger_path = cleanup_prompt_ledger_path(message)
            if ledger_path:
                return Path(ledger_path)
        return None

    def _wrap_cleanup_observer(self, events, *, ledger=None):
        from iac_code.pipeline.engine.cleanup import CleanupLedger, CleanupObserver

        cleanup_ledger = ledger or self._cleanup_ledger_for_normal_chat()
        if not isinstance(cleanup_ledger, CleanupLedger):
            return events
        if cleanup_ledger.load_failed():
            return events

        async def observed_stream():
            observer = CleanupObserver(cleanup_ledger)
            previous = self._cleanup_resource_state_map(cleanup_ledger)
            async for event in events:
                observer.observe(event)
                previous = self._print_cleanup_status_changes(cleanup_ledger, previous)
                yield event

        return observed_stream()

    @staticmethod
    def _cleanup_resource_state(resource) -> tuple[object, ...]:
        return (
            getattr(resource, "cleanup_status", None),
            getattr(resource, "progress_status", None),
            getattr(resource, "progress_percentage", None),
            getattr(resource, "cleanup_tool_use_id", None),
            getattr(resource, "last_error", None),
        )

    def _cleanup_resource_state_map(self, ledger) -> dict[str, tuple[object, ...]]:
        try:
            resources = ledger.cleanup_resources()
        except Exception:
            return {}
        return {resource.key: self._cleanup_resource_state(resource) for resource in resources}

    def _print_cleanup_status_changes(
        self,
        ledger,
        previous: dict[str, tuple[object, ...]],
    ) -> dict[str, tuple[object, ...]]:
        try:
            resources = ledger.cleanup_resources()
        except Exception:
            return previous
        current = {resource.key: self._cleanup_resource_state(resource) for resource in resources}
        printer = getattr(getattr(self, "renderer", None), "print_system_message", None)
        if not callable(printer):
            return current
        for resource in resources:
            state = current.get(resource.key)
            if state is None or previous.get(resource.key) == state:
                continue
            message = self._cleanup_resource_status_message(resource)
            if not message:
                continue
            printer(message, style=self._cleanup_status_style(getattr(resource, "cleanup_status", "")))
        return current

    @staticmethod
    def _cleanup_status_style(status: str) -> str:
        if status == "failed":
            return "red"
        if status in {"completed", "skipped"}:
            return "green"
        return "yellow"

    @staticmethod
    def _cleanup_resource_status_message(resource) -> str:
        status = str(getattr(resource, "cleanup_status", "") or "pending")
        resource_id = str(getattr(resource, "resource_id", "") or "")
        label = str(getattr(resource, "resource_name", "") or resource_id)
        region = str(getattr(resource, "region_id", "") or "unknown")
        progress = str(getattr(resource, "progress_status", "") or status)
        last_error = str(getattr(resource, "last_error", "") or "")
        badge = InlineREPL._cleanup_status_badge(status, progress)
        detail = InlineREPL._cleanup_status_detail(status, progress)
        parts = [
            _("↺ 回滚清理 [{badge}] {label}").format(badge=badge, label=label),
            _("{kind} {resource_id}").format(
                kind=InlineREPL._cleanup_resource_kind_label(resource),
                resource_id=InlineREPL._short_cleanup_resource_id(resource_id),
            ),
            region,
            detail,
        ]
        if last_error:
            parts.append(_("错误：{error}").format(error=InlineREPL._safe_cleanup_error(last_error)))
        return " · ".join(part for part in parts if part)

    @staticmethod
    def _cleanup_status_badge(status: str, progress: str) -> str:
        if status == "started":
            return _("删除中")
        if status == "completed":
            return _("完成")
        if status == "failed":
            return _("失败")
        if status == "skipped":
            return _("跳过")
        if status == "pending":
            return _("待处理")
        if progress and not progress.startswith("DELETE"):
            return _("检查")
        if progress in {"DELETE_REQUESTED", "DELETE_STARTED", "DELETE_IN_PROGRESS"}:
            return _("删除中")
        return _("进度")

    @staticmethod
    def _cleanup_status_detail(status: str, progress: str) -> str:
        if status == "started":
            if progress:
                return _("DeleteStack 已提交，等待删除完成（{progress}）").format(progress=progress)
            return _("DeleteStack 已提交，等待删除完成")
        if status == "completed":
            return progress or "completed"
        if status == "failed":
            return progress or "failed"
        if status == "skipped":
            return _("已跳过")
        if progress == "DELETE_IN_PROGRESS":
            return _("正在删除（{progress}）").format(progress=progress)
        if progress in {"DELETE_REQUESTED", "DELETE_STARTED"}:
            return _("DeleteStack 已提交，等待删除完成（{progress}）").format(progress=progress)
        if progress and not progress.startswith("DELETE"):
            return _("{progress}，需要删除").format(progress=progress)
        return progress or status

    @staticmethod
    def _cleanup_resource_kind_label(resource) -> str:
        provider = str(getattr(resource, "provider", "") or "").lower()
        resource_type = str(getattr(resource, "resource_type", "") or "").lower()
        if provider == "ros" and resource_type == "stack":
            return _("资源栈")
        return _("资源")

    @staticmethod
    def _short_cleanup_resource_id(resource_id: str) -> str:
        if len(resource_id) <= 18:
            return resource_id
        return "{}…{}".format(resource_id[:8], resource_id[-4:])

    @staticmethod
    def _safe_cleanup_error(error: str) -> str:
        from iac_code.utils.public_errors import sanitize_public_text

        sanitized = sanitize_public_text(error)
        return sanitized[:1000] + "..." if len(sanitized) > 1000 else sanitized

    def _remove_cleanup_prompts_from_context(self) -> int:
        context_manager = getattr(getattr(self, "_agent_loop", None), "context_manager", None)
        remover = getattr(context_manager, "remove_cleanup_prompt_messages", None)
        if not callable(remover):
            return 0
        try:
            removed = remover()
        except Exception:
            logger.warning("Failed to remove pipeline cleanup prompt from context", exc_info=True)
            return 0
        return removed if isinstance(removed, int) else 0

    def _warn_cleanup_ledger_load_failed(self, ledger) -> None:
        if getattr(self, "_cleanup_ledger_load_failed_warning_printed", False):
            return
        self._cleanup_ledger_load_failed_warning_printed = True
        load_error = ""
        get_load_error = getattr(ledger, "load_error", None)
        if callable(get_load_error):
            try:
                load_error = get_load_error() or ""
            except Exception:
                load_error = ""
        if load_error:
            logger.warning("Pipeline cleanup ledger is unreadable: %s", load_error)
        else:
            ledger_path = getattr(ledger, "path", None)
            if ledger_path:
                logger.warning("Pipeline cleanup ledger is unavailable: %s", ledger_path)
        self.renderer.print_system_message(
            _("无法读取回滚清理记录，已保留清理提示，请稍后继续或手动检查。"),
            style="yellow",
        )

    def _prune_cleanup_prompts_if_no_pending_cleanup(self, ledger=None) -> None:
        cleanup_ledger = ledger or self._cleanup_ledger_for_normal_chat()
        if self._cleanup_ledger_unavailable_with_prompt(cleanup_ledger):
            self._warn_cleanup_ledger_load_failed(cleanup_ledger)
            return
        if cleanup_ledger is not None:
            load_failed = getattr(cleanup_ledger, "load_failed", None)
            if callable(load_failed) and load_failed():
                self._warn_cleanup_ledger_load_failed(cleanup_ledger)
                return
        if cleanup_ledger is None or not cleanup_ledger.pending_resources():
            if cleanup_ledger is not None:
                self._mark_cleanup_prompts_completed(cleanup_ledger)
                self._clear_pipeline_cleanup_ledger_path(getattr(cleanup_ledger, "path", None))
            self._remove_cleanup_prompts_from_context()

    def _print_cleanup_resume_summary(self) -> None:
        ledger = self._cleanup_ledger_for_resume_summary()
        if ledger is None:
            return
        load_failed = getattr(ledger, "load_failed", None)
        if callable(load_failed) and load_failed():
            return
        ledger_path = str(getattr(ledger, "path", "") or "")
        printed_paths = getattr(self, "_cleanup_resume_summary_printed_paths", set())
        if ledger_path and ledger_path in printed_paths:
            return
        try:
            resume_resources = self._cleanup_resume_resources(ledger)
        except Exception:
            return
        if not resume_resources:
            return
        printer = getattr(getattr(self, "renderer", None), "print_system_message", None)
        if not callable(printer):
            return
        printer(
            self._cleanup_resume_summary_message(resume_resources),
            style=self._cleanup_resume_summary_style(resume_resources),
        )
        detail_resources = [
            resource for resource in resume_resources if self._cleanup_resume_should_show_detail(resource)
        ]
        visible_resources = detail_resources[-5:]
        for resource in visible_resources:
            printer(
                self._cleanup_resume_resource_line(resource),
                style=self._cleanup_status_style(str(getattr(resource, "cleanup_status", "") or "")),
            )
        if len(detail_resources) > 5:
            printer(_("还有 {count} 个需要关注的资源未显示。").format(count=len(detail_resources) - 5), style="yellow")
        if ledger_path:
            printed_paths = set(printed_paths)
            printed_paths.add(ledger_path)
            self._cleanup_resume_summary_printed_paths = printed_paths

    @staticmethod
    def _cleanup_resume_resources(ledger) -> list[Any]:
        resources = ledger.cleanup_resources()
        history_resources = InlineREPL._cleanup_resume_history_resources(ledger)
        if not history_resources:
            return resources

        history_by_key = {resource.key: resource for resource in history_resources}
        merged: list[Any] = []
        seen: set[str] = set()
        for resource in resources:
            key = resource.key
            merged.append(history_by_key.get(key, resource))
            seen.add(key)
        for resource in history_resources:
            if resource.key not in seen:
                merged.append(resource)
        return merged

    @staticmethod
    def _cleanup_resume_summary_message(resources: list[Any]) -> str:
        total = len(resources)
        counts = InlineREPL._cleanup_resume_status_counts(resources)
        if total > 0 and counts["completed"] == total:
            return _("↺ 回滚清理恢复：{count} 条记录均已完成。").format(count=total)

        parts: list[str] = []
        for key, label in (
            ("failed", _("失败")),
            ("pending", _("待处理")),
            ("active", _("进行中")),
            ("completed", _("已完成")),
            ("skipped", _("已跳过")),
        ):
            count = counts[key]
            if count:
                parts.append(_("{count} 条{label}").format(count=count, label=label))
        if parts:
            return _("↺ 回滚清理恢复：{count} 条记录，{summary}。").format(
                count=total,
                summary="，".join(parts),
            )
        return _("↺ 回滚清理恢复：{count} 条记录。").format(count=total)

    @staticmethod
    def _cleanup_resume_status_counts(resources: list[Any]) -> dict[str, int]:
        counts = {
            "pending": 0,
            "active": 0,
            "completed": 0,
            "failed": 0,
            "skipped": 0,
        }
        for resource in resources:
            status = str(getattr(resource, "cleanup_status", "") or "pending")
            if status in {"started", "in_progress"}:
                counts["active"] += 1
            elif status in counts:
                counts[status] += 1
            else:
                counts["pending"] += 1
        return counts

    @staticmethod
    def _cleanup_resume_summary_style(resources: list[Any]) -> str:
        if resources and all(
            str(getattr(resource, "cleanup_status", "") or "pending") in {"completed", "skipped"}
            for resource in resources
        ):
            return "green"
        return "yellow"

    @staticmethod
    def _cleanup_resume_should_show_detail(resource) -> bool:
        status = str(getattr(resource, "cleanup_status", "") or "pending")
        return status not in {"completed", "skipped"}

    @staticmethod
    def _cleanup_resume_history_resources(ledger) -> list[Any]:
        from iac_code.pipeline.engine.cleanup import CleanupResource

        get_history = getattr(ledger, "history_entries", None)
        if not callable(get_history):
            return []
        resources_by_key = {}
        for entry in get_history():
            event_type = str(entry.get("type") or "")
            if event_type not in {
                "cleanup_started",
                "cleanup_progress",
                "cleanup_completed",
                "cleanup_failed",
                "cleanup_skipped",
                "cleanup_pending",
            }:
                continue
            resource_data = dict(entry.get("resource") or {})
            if not resource_data:
                continue
            for key in (
                "cleanup_status",
                "cleanup_tool_use_id",
                "cleanup_action",
                "progress_status",
                "progress_percentage",
                "last_error",
            ):
                if entry.get(key) is not None:
                    resource_data[key] = entry[key]
            if entry.get("timestamp") is not None:
                resource_data["updated_at"] = entry["timestamp"]
            resource = CleanupResource.from_dict(resource_data)
            if resource.resource_id:
                resources_by_key.pop(resource.key, None)
                resources_by_key[resource.key] = resource
        return list(resources_by_key.values())

    @staticmethod
    def _cleanup_resume_resource_line(resource) -> str:
        status = str(getattr(resource, "cleanup_status", "") or "pending")
        resource_id = str(getattr(resource, "resource_id", "") or "")
        label = str(getattr(resource, "resource_name", "") or resource_id)
        region = str(getattr(resource, "region_id", "") or "unknown")
        progress = str(getattr(resource, "progress_status", "") or status)
        last_error = str(getattr(resource, "last_error", "") or "")
        badge = InlineREPL._cleanup_status_badge(status, progress)
        detail = InlineREPL._cleanup_status_detail(status, progress)
        parts = [
            _("  [{badge}] {label}").format(badge=badge, label=label),
            _("{kind} {resource_id}").format(
                kind=InlineREPL._cleanup_resource_kind_label(resource),
                resource_id=InlineREPL._short_cleanup_resource_id(resource_id),
            ),
            region,
            detail,
        ]
        if last_error:
            parts.append(_("错误：{error}").format(error=InlineREPL._safe_cleanup_error(last_error)))
        return " · ".join(part for part in parts if part)

    async def _maybe_start_normal_chat_cleanup_on_startup(self) -> bool:
        from iac_code.pipeline.config import RunMode

        if self._get_runtime_mode() != RunMode.NORMAL:
            return False
        self._print_cleanup_resume_summary()
        ledger = self._cleanup_ledger_for_normal_chat()
        if self._cleanup_ledger_unavailable_with_prompt(ledger):
            self._warn_cleanup_ledger_load_failed(ledger)
            return False
        if ledger is None:
            self._remove_cleanup_prompts_from_context()
            return False
        if ledger.load_failed():
            self._warn_cleanup_ledger_load_failed(ledger)
            return False
        if not ledger.pending_resources():
            self._prune_cleanup_prompts_if_no_pending_cleanup(ledger)
            return False
        return await self._start_pipeline_cleanup_from_ledger(ledger)

    async def _maybe_start_pipeline_cleanup(self, pipeline: object | None) -> bool:
        from iac_code.pipeline.config import RunMode

        if pipeline is None or self._get_runtime_mode() != RunMode.NORMAL:
            return False
        ledger = self._cleanup_ledger_for_pipeline(pipeline)
        if ledger is None:
            return False
        return await self._start_pipeline_cleanup_from_ledger(ledger)

    async def _start_pipeline_cleanup_from_ledger(self, ledger) -> bool:
        from iac_code.pipeline.engine.cleanup import create_cleanup_prompt_message

        load_failed = getattr(ledger, "load_failed", None)
        if callable(load_failed) and load_failed():
            self._warn_cleanup_ledger_load_failed(ledger)
            return False
        cleanup_prompt = ledger.build_pending_prompt()
        if cleanup_prompt is None:
            return False

        self._pipeline_cleanup_ledger_path = ledger.path
        ledger.record_prompt_queued(cleanup_prompt, ui_surface="repl")
        self.renderer.print_system_message("\n" + cleanup_prompt.status_message, style="yellow")
        session_prompt_exists = self._cleanup_prompt_exists_in_session(cleanup_prompt.prompt)
        self._remove_cleanup_prompts_from_context()
        message = create_cleanup_prompt_message(
            cleanup_prompt.prompt,
            cleanup_ledger_path=ledger.path,
            cleanup_status="pending",
        )
        try:
            injected = self._agent_loop.context_manager.add_raw_message(message.to_dict())
            if not session_prompt_exists:
                self._session_storage.append(
                    self._original_cwd,
                    self._session_id,
                    injected,
                    git_branch=self.current_git_branch(),
                )
        except Exception as exc:
            logger.warning("Failed to inject pipeline cleanup prompt: %s", exc)
            self.renderer.print_system_message(
                _("Detected rollback cleanup resources, but cleanup prompt injection failed."),
                style="yellow",
            )
            return False

        self.store.set_state(is_busy=True)
        try:
            streaming_input = StreamingInputBuffer()
            events = self._wrap_cleanup_observer(self._agent_loop.continue_streaming(), ledger=ledger)
            result = await self.renderer.run_streaming_output(
                events,
                permission_handler=self.renderer.prompt_permission,
                streaming_input=streaming_input,
            )
            elapsed, queued_inputs, draft_input = self._normalize_streaming_output_result(result)
            self._streaming_draft_input = draft_input
            if elapsed >= 1.0:
                self._agent_loop.stamp_last_turn_elapsed(elapsed)
            if queued_inputs:
                self._streaming_draft_input = "\n".join([*queued_inputs, draft_input]).strip()
            if self.renderer._last_streaming_errors:
                msg_count = len(self._agent_loop.context_manager.get_messages())
                for err in self.renderer._last_streaming_errors:
                    self._streaming_error_log.append((err, msg_count))
        finally:
            self._prune_cleanup_prompts_if_no_pending_cleanup(ledger)
            self.store.set_state(is_busy=False)
        return True

    def _cleanup_prompt_messages_from_context(self):
        context_manager = getattr(getattr(self, "_agent_loop", None), "context_manager", None)
        get_messages = getattr(context_manager, "get_messages", None)
        if not callable(get_messages):
            return []
        try:
            messages = get_messages()
        except Exception:
            return []
        return messages if isinstance(messages, list) else []

    def _cleanup_prompt_messages_from_session(self):
        session_storage = getattr(self, "_session_storage", None)
        load = getattr(session_storage, "load", None)
        if not callable(load):
            return []
        original_cwd = getattr(self, "_original_cwd", None)
        session_id = getattr(self, "_session_id", None)
        if not isinstance(original_cwd, str) or not isinstance(session_id, str):
            return []
        try:
            messages = load(original_cwd, session_id)
        except Exception:
            return []
        return messages if isinstance(messages, list) else []

    def _mark_cleanup_prompts_completed(self, ledger) -> None:
        from iac_code.pipeline.engine.cleanup import mark_cleanup_prompt_message_completed

        ledger_path = getattr(ledger, "path", None)
        for message in self._cleanup_prompt_messages_from_context():
            mark_cleanup_prompt_message_completed(message, cleanup_ledger_path=ledger_path)

        session_storage = getattr(self, "_session_storage", None)
        save = getattr(session_storage, "save", None)
        if not callable(save):
            return
        messages = self._cleanup_prompt_messages_from_session()
        changed = False
        for message in messages:
            changed = mark_cleanup_prompt_message_completed(message, cleanup_ledger_path=ledger_path) or changed
        if not changed:
            return
        original_cwd = getattr(self, "_original_cwd", None)
        session_id = getattr(self, "_session_id", None)
        if not isinstance(original_cwd, str) or not isinstance(session_id, str):
            return
        try:
            save(
                original_cwd,
                session_id,
                messages,
                git_branch=self.current_git_branch(),
            )
        except Exception:
            logger.warning("Failed to mark pipeline cleanup prompt completed in session", exc_info=True)

    def _cleanup_prompt_exists_in_context(self, prompt: str) -> bool:
        from iac_code.pipeline.engine.cleanup import is_active_cleanup_prompt_message

        return any(
            is_active_cleanup_prompt_message(message) and message.content == prompt
            for message in self._cleanup_prompt_messages_from_context()
        )

    def _cleanup_prompt_exists_in_session(self, prompt: str) -> bool:
        from iac_code.pipeline.engine.cleanup import is_active_cleanup_prompt_message

        return any(
            is_active_cleanup_prompt_message(message) and message.content == prompt
            for message in self._cleanup_prompt_messages_from_session()
        )

    def _context_has_cleanup_prompt(self) -> bool:
        from iac_code.pipeline.engine.cleanup import is_active_cleanup_prompt_message

        return any(
            is_active_cleanup_prompt_message(message) for message in self._cleanup_prompt_messages_from_context()
        )

    def _session_has_cleanup_prompt(self) -> bool:
        from iac_code.pipeline.engine.cleanup import is_active_cleanup_prompt_message

        return any(
            is_active_cleanup_prompt_message(message) for message in self._cleanup_prompt_messages_from_session()
        )

    def _cleanup_prompt_exists_anywhere(self) -> bool:
        return self._context_has_cleanup_prompt() or self._session_has_cleanup_prompt()

    def _cleanup_ledger_unavailable_with_prompt(self, ledger) -> bool:
        if not self._cleanup_prompt_exists_anywhere():
            return False
        if ledger is None:
            return True
        path = getattr(ledger, "path", None)
        try:
            if path is not None and not path.exists():
                return True
        except Exception:
            return True
        load_failed = getattr(ledger, "load_failed", None)
        return bool(callable(load_failed) and load_failed())

    def _block_if_cleanup_ledger_unreadable(self) -> bool:
        ledger = self._cleanup_ledger_for_normal_chat()
        if not self._cleanup_ledger_unavailable_with_prompt(ledger):
            return False
        self._warn_cleanup_ledger_load_failed(ledger)
        return True

    async def _run_pending_cleanup_before_normal_turn(self, *, draft_text: str) -> bool:
        ledger = self._cleanup_ledger_for_normal_chat()
        if self._cleanup_ledger_unavailable_with_prompt(ledger):
            self._warn_cleanup_ledger_load_failed(ledger)
            self._streaming_draft_input = draft_text
            return False
        if ledger is None:
            return True
        if ledger.load_failed():
            if self._context_has_cleanup_prompt():
                self._warn_cleanup_ledger_load_failed(ledger)
                self._streaming_draft_input = draft_text
                return False
            return True
        if not ledger.pending_resources():
            self._mark_cleanup_prompts_completed(ledger)
            self._remove_cleanup_prompts_from_context()
            return True

        if not await self._start_pipeline_cleanup_from_ledger(ledger):
            self._streaming_draft_input = draft_text
            return False
        if ledger.load_failed() or ledger.pending_resources():
            self._streaming_draft_input = draft_text
            self.renderer.print_system_message(
                _("Rollback cleanup is still in progress. Please continue after cleanup completes."),
                style="yellow",
            )
            return False
        return True

    async def _handle_chat(self, user_input: PromptInputResult | str) -> list[str]:
        """Send the user message to the agent loop and stream output."""
        from iac_code.pipeline.config import RunMode

        if self._get_runtime_mode() == RunMode.PIPELINE:
            await self._handle_pipeline_chat(self._pipeline_user_input_from_repl_input(user_input))
            return []

        draft_text = user_input.text if isinstance(user_input, PromptInputResult) else user_input
        if not await self._run_pending_cleanup_before_normal_turn(draft_text=draft_text):
            return []

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
            events = self._wrap_cleanup_observer(
                self._agent_loop.run_streaming(
                    payload,
                    queued_input_provider=lambda: streaming_input.drain_queued_inputs(self._should_submit_mid_turn),
                )
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
            self._prune_cleanup_prompts_if_no_pending_cleanup()
            self.store.set_state(is_busy=False)

    async def _flush_pipeline_telemetry(self) -> None:
        from iac_code.services.telemetry import flush_telemetry

        try:
            await asyncio.to_thread(flush_telemetry)
        except Exception:
            logger.debug("flush_telemetry after pipeline boundary failed", exc_info=True)

    def _refresh_pipeline_display_recorder(self) -> None:
        from pathlib import Path

        pipeline = getattr(self, "_pipeline", None)
        transcript_path = getattr(pipeline, "display_transcript_path", None) if pipeline is not None else None
        if not isinstance(transcript_path, (str, Path)):
            self._pipeline_display_recorder = None
            return
        try:
            from iac_code.pipeline.engine.display_replay import PipelineDisplayRecorder

            self._pipeline_display_recorder = PipelineDisplayRecorder(transcript_path)
        except Exception as exc:
            logger.warning("Failed to initialize pipeline display recorder: {}", exc)
            self._pipeline_display_recorder = None

    def _record_pipeline_display_event(self, event) -> None:
        recorder = getattr(self, "_pipeline_display_recorder", None)
        if recorder is None:
            return
        try:
            recorder.record_pipeline_event(event)
        except Exception as exc:
            logger.warning("Failed to record pipeline display event: {}", exc)

    def _record_pipeline_display_tool_use(
        self,
        event,
        *,
        step_id: str | None = None,
        sub_pipeline_id: str | None = None,
    ) -> None:
        if getattr(event, "name", "") != "complete_step":
            return
        recorder = getattr(self, "_pipeline_display_recorder", None)
        if recorder is None:
            return
        try:
            recorder.record_tool_use(
                event,
                step_id=step_id or getattr(self, "_pipeline_display_current_step_id", None),
                sub_pipeline_id=sub_pipeline_id,
            )
        except Exception as exc:
            logger.warning("Failed to record pipeline display tool use: {}", exc)

    def _record_pipeline_display_candidate_diagram(self, event, *, step_id: str | None = None) -> None:
        recorder = getattr(self, "_pipeline_display_recorder", None)
        if recorder is None:
            return
        try:
            recorder.record_candidate_diagram(
                event,
                step_id=step_id or getattr(self, "_pipeline_display_current_step_id", None),
            )
        except Exception as exc:
            logger.warning("Failed to record pipeline display candidate diagram: {}", exc)

    def _record_pipeline_display_candidate_detail(self, event, *, step_id: str | None = None) -> None:
        recorder = getattr(self, "_pipeline_display_recorder", None)
        if recorder is None:
            return
        try:
            recorder.record_candidate_detail(
                event,
                step_id=step_id or getattr(self, "_pipeline_display_current_step_id", None),
            )
        except Exception as exc:
            logger.warning("Failed to record pipeline display candidate detail: {}", exc)

    def _record_pipeline_display_candidate_selected(
        self,
        *,
        step_id: str | None,
        candidate_name: str,
        candidate_index: int | None,
    ) -> None:
        recorder = getattr(self, "_pipeline_display_recorder", None)
        if recorder is None:
            return
        try:
            recorder.record_candidate_selected(
                step_id=step_id,
                candidate_name=candidate_name,
                candidate_index=candidate_index,
            )
        except Exception as exc:
            logger.warning("Failed to record pipeline display candidate selection: {}", exc)

    def _record_pipeline_display_user_aborted(self) -> None:
        recorder = getattr(self, "_pipeline_display_recorder", None)
        if recorder is None:
            return
        try:
            recorder.record_user_aborted()
        except Exception as exc:
            logger.warning("Failed to record pipeline display user abort: {}", exc)

    async def ensure_pipeline_restored_for_prompt(self) -> bool:
        """Restore a resumable pipeline sidecar so /prompt can inspect the real AgentLoop context."""
        from iac_code.pipeline import create_pipeline
        from iac_code.pipeline.config import RunMode, get_pipeline_name, get_working_directory

        if self._pipeline is not None:
            return True
        if self._get_runtime_mode() != RunMode.PIPELINE:
            return False

        pipeline_cwd = get_working_directory() or self._original_cwd
        if not self._detect_pipeline_session(pipeline_cwd, self._session_id):
            return False

        self._pipeline = create_pipeline(
            name=get_pipeline_name(),
            provider_manager=self._provider_manager,
            base_tool_registry=self.tool_registry,
            session_storage=self._session_storage,
            session_id=self._session_id,
            cwd=pipeline_cwd,
            permission_context_getter=lambda: self.store.get_state().permission_context,
            memory_content_getter=self._pipeline_memory_content_getter(),
            auto_trigger_skills=self.command_registry.get_model_invocable_skills(),
            resume_from_sidecar=True,
        )
        self._refresh_pipeline_display_recorder()
        restored = self._pipeline.sidecar_restore_result
        if restored is None:
            restored = await self._pipeline.restore_from_sidecar()
        if restored.ok is False:
            detail = restored.reason or restored.status
            if detail:
                self.renderer.print_system_message(
                    _("Ignoring saved pipeline state: {reason}").format(reason=detail),
                    style="yellow",
                )
            self._pipeline = None
            self._pipeline_waiting_input = False
            self._pipeline_restored_status = None
            return False

        self._pipeline_restored_status = restored.status
        self._pipeline_waiting_input = restored.status == "waiting_input"
        return True

    def _pipeline_user_input_from_repl_input(
        self, user_input: PromptInputResult | str | "PipelineUserInput" | None
    ) -> "PipelineUserInput":
        """Convert REPL input to the pipeline wrapper used by model-facing entry points."""
        from iac_code.pipeline.engine.user_input import normalize_pipeline_user_input
        from iac_code.utils.image.processor import process_user_input

        if isinstance(user_input, PromptInputResult):
            blocks = process_user_input(user_input.text, pasted_contents=user_input.pasted_contents)
            content: str | list[ContentBlock]
            if any(isinstance(block, ImageBlock) for block in blocks):
                content = blocks
            else:
                content = user_input.text
            return normalize_pipeline_user_input(content, display_text=user_input.text)
        return normalize_pipeline_user_input(user_input)

    async def _read_pipeline_interrupt_input(self) -> "PipelineUserInput":
        user_input = await self._prompt_input.get_input(prompt="✎ ", transient=True)
        if user_input is not None:
            make_result = getattr(self._prompt_input, "make_result", None)
            if callable(make_result):
                result = make_result()
                if isinstance(result, PromptInputResult):
                    return self._pipeline_user_input_from_repl_input(result)
        return self._pipeline_user_input_from_repl_input(user_input)

    async def _handle_pipeline_chat(self, user_input: str | "PipelineUserInput") -> None:
        """Drive the pipeline and render output."""
        from iac_code.pipeline import create_pipeline
        from iac_code.pipeline.config import get_pipeline_name, get_working_directory
        from iac_code.pipeline.engine.user_input import normalize_pipeline_user_input

        pipeline_input = normalize_pipeline_user_input(user_input)
        self.renderer.record_user_turn(pipeline_input.display_text)

        if self._pipeline is None:
            pipeline_cwd = get_working_directory() or self._original_cwd
            self._pipeline = create_pipeline(
                name=get_pipeline_name(),
                provider_manager=self._provider_manager,
                base_tool_registry=self.tool_registry,
                session_storage=self._session_storage,
                session_id=self._session_id,
                cwd=pipeline_cwd,
                permission_context_getter=lambda: self.store.get_state().permission_context,
                memory_content_getter=self._pipeline_memory_content_getter(),
                auto_trigger_skills=self.command_registry.get_model_invocable_skills(),
            )
            self._refresh_pipeline_display_recorder()
            restored = None
            if self._detect_pipeline_session(pipeline_cwd, self._session_id):
                restored = await self._pipeline.restore_from_sidecar()
                if restored.ok is False:
                    detail = restored.reason or restored.status
                    if detail:
                        self.renderer.print_system_message(
                            _("Ignoring saved pipeline state: {reason}").format(reason=detail),
                            style="yellow",
                        )
            resume_waiting_candidate_selection = False
            event_stream = None
            if restored and restored.ok and restored.status == "waiting_input":
                self._pipeline_waiting_input = False
                if self._pipeline_current_step_is_candidate_selection() is True:
                    resume_waiting_candidate_selection = True
                else:
                    event_stream = cast(Any, self._pipeline).resume(pipeline_input)
            elif restored and restored.ok and restored.status == "running":
                self._pipeline_waiting_input = False
                event_stream = cast(Any, self._pipeline).continue_from_sidecar(user_input=pipeline_input)
            else:
                self._persist_pipeline_visible_user_turn(pipeline_input)
                event_stream = cast(Any, self._pipeline).run(pipeline_input)
        else:
            self._refresh_pipeline_display_recorder()
            self._pipeline_waiting_input = False
            restored_status = getattr(self, "_pipeline_restored_status", None)
            self._pipeline_restored_status = None
            resume_waiting_candidate_selection = False
            event_stream = None
            if restored_status == "running":
                event_stream = cast(Any, self._pipeline).continue_from_sidecar(user_input=pipeline_input)
            elif restored_status == "waiting_input":
                if self._pipeline_current_step_is_candidate_selection() is True:
                    resume_waiting_candidate_selection = True
                else:
                    event_stream = cast(Any, self._pipeline).resume(pipeline_input)
            else:
                event_stream = cast(Any, self._pipeline).resume(pipeline_input)

        # No except for CancelledError/KeyboardInterrupt here: Ctrl+C must
        # propagate to the run() loop's single handler (which keeps the REPL
        # alive and prints the interrupt message). Swallowing it here would
        # violate the asyncio cancellation contract. The finally still runs,
        # tearing down the pipeline regardless of how the stream ended.
        terminal_event = None
        try:
            self.store.set_state(is_busy=True)
            if resume_waiting_candidate_selection:
                terminal_event = await self._resume_waiting_candidate_selection_from_sidecar()
                if terminal_event is None:
                    self._pipeline_waiting_input = True
            else:
                assert event_stream is not None
                terminal_event = await self._render_pipeline_stream(event_stream)
        finally:
            self.store.set_state(is_busy=False)
            pipeline_for_flush = self._pipeline
            self._finalize_pipeline_after_render(terminal_event)
            if pipeline_for_flush is not None:
                await self._flush_pipeline_telemetry()
                await self._maybe_start_pipeline_cleanup(pipeline_for_flush)

    def _pipeline_current_step_is_candidate_selection(self) -> bool:
        pipeline = getattr(self, "_pipeline", None)
        if pipeline is None:
            return False
        try:
            return getattr(pipeline.state_machine.current_step, "ui_mode", "") == "candidate_selection"
        except (AttributeError, IndexError):
            return False

    async def _resume_pipeline_sidecar_on_startup(self) -> bool:
        from iac_code.pipeline.config import RunMode

        if self._get_runtime_mode() != RunMode.PIPELINE:
            return False
        restored = await self.ensure_pipeline_restored_for_prompt()
        if not restored:
            return False
        self._render_pipeline_display_replay_on_startup()
        if (
            self._pipeline_restored_status != "waiting_input"
            or self._pipeline_current_step_is_candidate_selection() is not True
        ):
            return False

        terminal_event = None
        try:
            self.store.set_state(is_busy=True)
            terminal_event = await self._resume_waiting_candidate_selection_from_sidecar()
            if terminal_event is None:
                self._pipeline_waiting_input = True
        finally:
            self.store.set_state(is_busy=False)
            pipeline_for_flush = self._pipeline
            self._finalize_pipeline_after_render(terminal_event)
            if pipeline_for_flush is not None:
                await self._flush_pipeline_telemetry()
                await self._maybe_start_pipeline_cleanup(pipeline_for_flush)
        return True

    def _render_pipeline_display_replay_on_startup(self) -> None:
        model = self._load_pipeline_display_replay_model(include_nonterminal=True)
        if model is None:
            self._ensure_pipeline_progress_state()
            return

        self._seed_pipeline_progress_state_from_replay_model(model)
        messages = self._session_storage.load(self._original_cwd, self._session_id)
        repaired = self._session_storage.repair_interrupted(messages)
        visible_messages = self._pipeline_visible_resume_messages(repaired)
        if visible_messages:
            self.renderer.replay_history(visible_messages)

        from iac_code.ui.pipeline_display_replay import PipelineDisplayReplayRenderer

        PipelineDisplayReplayRenderer(
            self.console,
            history_replayer=self.renderer.replay_history,
            history_renderable_factory=self._render_pipeline_display_transcript_window,
            transcript_loader=self._load_pipeline_display_transcript_messages,
        ).render(self._startup_replay_model_for_interactive_resume(model))

    def _seed_pipeline_progress_state_from_replay_model(self, model) -> None:
        step_names = self._pipeline_step_names_from_active_pipeline()
        if not step_names:
            step_names = self._pipeline_step_names_from_replay_model(model)
        completed_indices: set[int] = set()
        for attempt in model.attempts:
            if attempt.status != "completed":
                continue
            if attempt.index is not None:
                completed_indices.add(attempt.index - 1)
            elif attempt.step_id in step_names:
                completed_indices.add(step_names.index(attempt.step_id))
        self._pipeline_step_names = step_names
        self._pipeline_completed_indices = completed_indices
        duration_s = getattr(model, "duration_s", None)
        self._pipeline_start_time = time.time() - duration_s if isinstance(duration_s, (int, float)) else time.time()

    def _pipeline_step_names_from_active_pipeline(self) -> list[str]:
        pipeline = getattr(self, "_pipeline", None)
        if pipeline is None:
            return []
        try:
            order = getattr(pipeline.state_machine, "_order", [])
        except AttributeError:
            return []
        return [str(step_id) for step_id in order]

    @staticmethod
    def _pipeline_step_names_from_replay_model(model) -> list[str]:
        ordered: list[tuple[int, str]] = []
        fallback: list[str] = []
        for attempt in model.attempts:
            if attempt.step_id not in fallback:
                fallback.append(attempt.step_id)
            if attempt.index is not None:
                ordered.append((attempt.index, attempt.step_id))
        if ordered:
            return [step_id for _index, step_id in sorted(dict(ordered).items())]
        return fallback

    @staticmethod
    def _startup_replay_model_for_interactive_resume(model):
        import copy

        from iac_code.pipeline.engine.display_replay import DisplayCandidateSelection

        replay_model = copy.deepcopy(model)
        for attempt in reversed(replay_model.attempts):
            if attempt.status == "waiting_input" and attempt.ui_mode == "candidate_selection":
                attempt.status = "running"
                attempt.candidate_selection = DisplayCandidateSelection()
                break
        return replay_model

    def _ensure_pipeline_progress_state(self) -> None:
        if not hasattr(self, "_pipeline_step_names"):
            self._pipeline_step_names = []
        if not hasattr(self, "_pipeline_completed_indices"):
            self._pipeline_completed_indices = set()
        if not hasattr(self, "_pipeline_start_time"):
            self._pipeline_start_time = time.time()

    async def _resume_waiting_candidate_selection_from_sidecar(self) -> PipelineEvent | None:
        from iac_code.pipeline.engine.events import PipelineEvent, PipelineEventType
        from iac_code.types.stream_events import CandidateDetailEvent, DiagramEvent

        model = self._load_pipeline_display_replay_model(include_nonterminal=True)
        if model is None:
            return None
        attempt = next(
            (
                item
                for item in reversed(model.attempts)
                if item.ui_mode == "candidate_selection" and item.candidate_selection.state == "waiting"
            ),
            None,
        )
        if attempt is None:
            return None
        selection = attempt.candidate_selection
        self._pipeline_display_current_step_id = attempt.step_id

        async def restored_selection_stream():
            ordered_candidates = sorted(
                selection.candidates.values(),
                key=lambda candidate: (
                    candidate.candidate_index is None,
                    candidate.candidate_index if candidate.candidate_index is not None else candidate.name,
                ),
            )
            for index, candidate in enumerate(ordered_candidates):
                if candidate.mermaid_source:
                    yield DiagramEvent(
                        candidate_name=candidate.name,
                        candidate_index=candidate.candidate_index,
                        template_content="",
                        mermaid_source=candidate.mermaid_source,
                    )
                if candidate.summary or candidate.cost_items or candidate.total_monthly_cost:
                    yield CandidateDetailEvent(
                        tool_use_id=f"restored_candidate_detail_{index}",
                        candidate_name=candidate.name,
                        candidate_index=candidate.candidate_index,
                        summary=candidate.summary,
                        cost_items=candidate.cost_items,
                        total_monthly_cost=candidate.total_monthly_cost,
                    )
            yield PipelineEvent(
                type=PipelineEventType.USER_INPUT_REQUIRED,
                step_id=attempt.step_id,
                timestamp=time.time(),
                data={
                    "step_id": attempt.step_id,
                    "prompt": selection.prompt,
                    "options": selection.options,
                },
            )

        result = await self._render_candidate_selection_tabs(restored_selection_stream())
        return result if isinstance(result, PipelineEvent) else None

    def _clear_pipeline_runtime_state(self) -> None:
        self._pipeline = None
        self._pipeline_waiting_input = False
        self._pipeline_restored_status = None
        self._pipeline_display_recorder = None
        self._pipeline_display_current_step_id = None

    def _finalize_pipeline_after_render(self, terminal_event: PipelineEvent | None) -> None:
        # Keep terminal sidecars on disk for debugging. Terminal metadata
        # controls whether they are resumable.
        handoff_result = self._handoff_pipeline_to_normal(terminal_event)
        if handoff_result in {"succeeded", "failed"}:
            self._clear_pipeline_runtime_state()
        elif self._pipeline is not None and self._pipeline.sidecar_status == "failed":
            self._clear_pipeline_runtime_state()
        elif self._pipeline is not None and self._pipeline.state_machine.is_complete:
            self._clear_pipeline_runtime_state()
        elif self._pipeline is not None and not self._pipeline_waiting_input:
            self._pipeline.mark_user_aborted("pipeline interrupted by user or renderer cancellation")
            self._switch_user_aborted_pipeline_to_normal()
            self._clear_pipeline_runtime_state()

    def _handoff_pipeline_to_normal(self, terminal_event: PipelineEvent | None) -> PipelineHandoffResult:
        from iac_code.pipeline.config import RunMode
        from iac_code.pipeline.engine.events import PipelineEventType

        pipeline = self._pipeline
        if pipeline is None or terminal_event is None:
            return "not_applicable"
        if terminal_event.type != PipelineEventType.PIPELINE_COMPLETED:
            return "not_applicable"
        if not pipeline.should_switch_to_normal(terminal_event.data):
            return "not_applicable"

        try:
            pipeline.mark_normal_handoff(status="succeeded", failed_reason=None)
        except Exception as exc:
            logger.opt(exception=True).warning("Pipeline handoff metadata persistence failed: {}", exc)
            self.renderer.print_system_message(
                _("Pipeline state persistence failed. Normal chat handoff was not marked durable."),
                style="yellow",
            )
            return "failed"

        try:
            summary = pipeline.build_normal_handoff_summary(terminal_event.data)
            injected = self._agent_loop.context_manager.add_raw_message({"role": "user", "content": summary})
            # Persist into the normal AgentLoop session partition. Future normal
            # turns are stored under _original_cwd, even if the pipeline sidecar
            # used IAC_CODE_CWD for its own resumable state.
            self._session_storage.append(
                self._original_cwd,
                self._session_id,
                injected,
                git_branch=self.current_git_branch(),
            )
        except Exception as exc:
            try:
                pipeline.mark_normal_handoff(status="failed", failed_reason=str(exc))
            except Exception as persistence_exc:
                logger.opt(exception=True).warning(
                    "Pipeline handoff failure metadata persistence failed: {}",
                    persistence_exc,
                )
                self.renderer.print_system_message(
                    _("Pipeline state persistence failed. Normal chat handoff was not marked durable."),
                    style="yellow",
                )
                return "failed"
            logger.opt(exception=True).warning("Pipeline-to-normal handoff injection failed: {}", exc)
            self.renderer.print_system_message(
                _("Pipeline completed, but the handoff context could not be injected or saved."),
                style="yellow",
            )
            return "failed"
        self._set_runtime_mode(RunMode.NORMAL)
        self.renderer.print_system_message(
            _("Pipeline completed. Normal chat is now active."),
            style="green",
        )
        return "succeeded"

    def _handle_pipeline_state_persistence_failure(self, exc: Exception) -> None:
        logger.opt(exception=True).warning("Pipeline state persistence failed during interrupt handling: {}", exc)
        self._last_interrupt_paused = True
        self._pipeline_waiting_input = False
        pause_agent_loops = getattr(self._pipeline, "pause_agent_loops", None)
        if callable(pause_agent_loops):
            pause_agent_loops()
        self.renderer.print_system_message(
            _("Pipeline state persistence failed. The pipeline is paused; do not continue until state is durable."),
            style="yellow",
        )

    async def _handle_mid_pipeline_message(
        self, msg: PromptInputResult | str | "PipelineUserInput", suppress_render: bool = False
    ) -> tuple[bool, str]:
        """Process a user message received during pipeline execution via judge.

        Returns (needs_restart, feedback_text). When suppress_render is True,
        the caller is responsible for displaying feedback_text (e.g. by injecting
        it into a Live content area instead of printing to scrollback).
        """
        if self._pipeline is None:
            return False, ""
        from iac_code.pipeline.engine.pipeline_runner import PipelineStatePersistenceError

        pipeline_input = self._pipeline_user_input_from_repl_input(msg)
        if pipeline_input.is_empty:
            return False, ""
        display_text = pipeline_input.display_text

        from rich.spinner import Spinner

        with Live(
            Spinner("dots", text=_("Judging your input...")),
            console=self.console,
            refresh_per_second=10,
            transient=True,
        ):
            verdict = await cast(Any, self._pipeline).handle_user_interrupt(pipeline_input)

        self._last_interrupt_paused = bool(getattr(verdict, "paused", False))
        if verdict.action == "continue":
            feedback = self._format_interrupt_feedback("continue", display_text, verdict)
            if getattr(verdict, "paused", False):
                save_interrupt_pause = getattr(self._pipeline, "save_interrupt_pause", None)
                if callable(save_interrupt_pause):
                    try:
                        await save_interrupt_pause(verdict)
                    except PipelineStatePersistenceError as exc:
                        self._handle_pipeline_state_persistence_failure(exc)
                        return False, ""
                self._pipeline_waiting_input = True
            # P-I18: surface ambiguous continue verdicts so users see their input wasn't understood
            if verdict.reason and verdict.reason.startswith("[ambiguous]"):
                self.renderer.print_system_message(
                    _(
                        "Note: your input wasn't clearly understood and was treated as chitchat. "
                        "To interrupt, be more explicit (e.g. 'switch to cheaper plan')."
                    ),
                    style="yellow",
                )
            if not suppress_render:
                self._render_interrupt_feedback("continue", display_text, verdict)
            return False, feedback
        if verdict.action == "supplement":
            feedback = self._format_interrupt_feedback("supplement", display_text, verdict)
            if not suppress_render:
                self._render_interrupt_feedback("supplement", display_text, verdict)
            return False, feedback
        if verdict.action == "hard_interrupt":
            try:
                if pipeline_input.has_images:
                    is_parent_rollback = self._pipeline.apply_hard_interrupt(verdict, source_input=pipeline_input)
                else:
                    is_parent_rollback = self._pipeline.apply_hard_interrupt(verdict)
            except PipelineStatePersistenceError as exc:
                self._handle_pipeline_state_persistence_failure(exc)
                return False, ""
            applied_verdict = getattr(self._pipeline, "last_applied_interrupt_verdict", None)
            feedback_verdict = (
                applied_verdict if getattr(applied_verdict, "action", None) == "hard_interrupt" else verdict
            )
            if not is_parent_rollback:
                feedback = self._format_interrupt_feedback("hard_interrupt_candidate", display_text, feedback_verdict)
                if not suppress_render:
                    self._render_interrupt_feedback("hard_interrupt_candidate", display_text, feedback_verdict)
                return False, feedback
            feedback = self._format_interrupt_feedback("hard_interrupt_parent", display_text, feedback_verdict)
            if not suppress_render:
                self._render_interrupt_feedback("hard_interrupt_parent", display_text, feedback_verdict)
            return True, feedback
        return False, ""

    def _format_interrupt_feedback(self, kind: str, user_msg: str, verdict) -> str:
        """Format interrupt feedback as plain text for injection into content areas."""
        from iac_code.pipeline.display_names import display_step_name

        reason = verdict.reason or ""
        if kind == "continue" and getattr(verdict, "paused", False):
            kind = "paused"
        if kind == "continue" and reason.startswith(("judge failed", "parse failed")):
            kind = "judge_failed"
        if kind == "supplement" and reason.startswith("supplement_dropped"):
            kind = "supplement_dropped"

        if kind == "judge_failed":
            return _(
                "⚠ Interrupt judging did not finish; your message was not processed\n  You said: {user_msg}"
            ).format(user_msg=user_msg)
        elif kind == "paused":
            return _(
                "⚠ Interrupt judging did not finish; the pipeline was paused to avoid continuing side-effect steps\n"
                "  You said: {user_msg}"
            ).format(user_msg=user_msg)
        elif kind == "supplement_dropped":
            return _(
                "⚠ The message was treated as supplemental input, but there is no AgentLoop to inject it into\n"
                "  You said: {user_msg}"
            ).format(user_msg=user_msg)
        elif kind == "continue":
            return _("· Message is unrelated to the current step and was ignored\n  You said: {user_msg}").format(
                user_msg=user_msg
            )
        elif kind == "supplement":
            target = display_step_name(verdict.supplement_target) if verdict.supplement_target else _("current step")
            return _("✎ Added to {target}\n  You said: {user_msg}").format(target=target, user_msg=user_msg)
        elif kind == "hard_interrupt_parent":
            reason_line = "\n  " + _("Reason: {reason}").format(reason=reason) if reason else ""
            return _("⚠ Interrupted → rolled back to {target}\n  You said: {user_msg}{reason_line}").format(
                target=display_step_name(verdict.rollback_target) if verdict.rollback_target else "?",
                user_msg=user_msg,
                reason_line=reason_line,
            )
        elif kind == "hard_interrupt_candidate":
            scope = verdict.candidate_scope or "?"
            target = display_step_name(verdict.rollback_target) if verdict.rollback_target else "?"
            reason_line = "\n  " + _("Reason: {reason}").format(reason=reason) if reason else ""
            return _(
                "⚠ Candidate {scope} restarted → starting again from {target}\n  You said: {user_msg}{reason_line}"
            ).format(
                scope=scope,
                target=target,
                user_msg=user_msg,
                reason_line=reason_line,
            )
        return ""

    def _render_interrupt_feedback(self, kind: str, user_msg: str, verdict) -> None:
        """Render structured feedback for an interrupt judge verdict.

        Failure modes detected via verdict.reason prefix and rendered in a
        Panel so they can't be silently lost in scrollback when the streaming
        Live region restarts right after.
        """
        from rich.panel import Panel

        from iac_code.pipeline.display_names import display_step_name

        reason = verdict.reason or ""
        # Detect silent-failure modes that leak through as action="continue"
        if kind == "continue" and getattr(verdict, "paused", False):
            kind = "paused"
        if kind == "continue" and reason.startswith(("judge failed", "parse failed")):
            kind = "judge_failed"
        # Detect supplement that found no AgentLoop to inject into
        if kind == "supplement" and reason.startswith("supplement_dropped"):
            kind = "supplement_dropped"

        body = Text()
        if kind == "judge_failed":
            body.append(_("⚠ Interrupt judging did not finish; your message was not processed\n"), style="bold yellow")
            body.append(_("The pipeline continued; press Esc again to retry.\n"), style="yellow")
        elif kind == "paused":
            body.append(_("⚠ Interrupt judging did not finish; the pipeline is paused\n"), style="bold yellow")
            body.append(
                _("Confirm whether to continue, roll back, or cancel before side-effect steps proceed.\n"),
                style="yellow",
            )
        elif kind == "supplement_dropped":
            body.append(
                _("⚠ Message was treated as supplemental input, but no AgentLoop is available in this parallel run\n"),
                style="bold yellow",
            )
            body.append(
                _("The judge should return supplement_target=candidate_index:N or hard_interrupt.\n"),
                style="yellow",
            )
        elif kind == "continue":
            body.append(_("· Message is unrelated to the current step and was ignored\n"), style="dim")
        elif kind == "supplement":
            target = display_step_name(verdict.supplement_target) if verdict.supplement_target else _("current step")
            body.append(_("✎ Added to "), style="green")
            body.append(target, style="bold green")
            body.append("\n", style="green")
        elif kind == "hard_interrupt_parent":
            body.append(_("⚠ Interrupted → rolled back to "), style="yellow")
            target = display_step_name(verdict.rollback_target) if verdict.rollback_target else "?"
            body.append(target, style="bold yellow")
            body.append("\n", style="yellow")
        elif kind == "hard_interrupt_candidate":
            body.append(_("⚠ Candidate "), style="yellow")
            body.append(verdict.candidate_scope or "?", style="bold yellow")
            body.append(_(" restarted → starting again from "), style="yellow")
            target = display_step_name(verdict.rollback_target) if verdict.rollback_target else "?"
            body.append(target, style="bold yellow")
            body.append("\n", style="yellow")
        body.append(_("  You said: {user_msg}\n").format(user_msg=user_msg), style="cyan")
        body.append(_("  Reason: {reason}").format(reason=reason), style="dim")

        if kind in ("judge_failed", "supplement_dropped", "paused"):
            # Wrap in panel so it's hard to miss when the parallel-tabs Live
            # region restarts and pushes scrollback content upward.
            self.console.print(Panel(body, border_style="yellow", title=_("Interrupt handling")))
        else:
            self.console.print(body)

    def _render_interrupt_feedback_inline(self, feedback: str) -> None:
        """Print interrupt feedback as a styled panel (used after Live has stopped)."""
        from rich.panel import Panel

        self.renderer.console.print(Panel(Text(feedback), border_style="yellow", title=_("Interrupt handling")))

    async def _restart_pipeline_stream_after_interrupt(self, old_stream, completed_indices):
        """Swap in a fresh event_stream after a hard interrupt.

        Closes the old generator (defensive — aclose is idempotent in CPython
        but wrapping protects against generator/runtime changes), narrows
        completed_indices to steps strictly before the new current step, and
        returns the post-interrupt event stream.
        """
        assert self._pipeline is not None  # callers guard with `if self._pipeline`
        new_stream = self._pipeline.continue_after_interrupt()
        try:
            await old_stream.aclose()
        except Exception:
            logger.debug("old event_stream aclose during restart failed", exc_info=True)
        completed_indices.intersection_update(range(self._pipeline.state_machine.current_step_index))
        return new_stream

    async def _render_pipeline_stream(self, event_stream) -> PipelineEvent | None:
        """Render mixed pipeline + agent events with animated progress bar."""
        from iac_code.pipeline.engine.events import PipelineEvent, PipelineEventType
        from iac_code.pipeline.engine.types import StepResult
        from iac_code.pipeline.engine.ui_contract import PipelineStepType, PipelineUiMode

        self._ensure_pipeline_progress_state()
        agent_events_queue: asyncio.Queue | None = None
        renderer_task: asyncio.Task | None = None
        step_names: list[str] = list(self._pipeline_step_names)
        completed_indices: set[int] = set(self._pipeline_completed_indices)
        current_index: int = -1
        spinner_frame: list[int] = [0]

        # Esc detection runs inside the renderer's existing key listener via
        # this callback — a separate stdin reader would race with the renderer
        # for bytes and silently drop ~half of the user's Esc presses.
        interrupt_requested = asyncio.Event()

        def _on_escape():
            interrupt_requested.set()
            if self._pipeline:
                self._pipeline.pause_agent_loops()

        async def _agent_event_gen(q: asyncio.Queue):
            while True:
                event = await q.get()
                if event is None:
                    return
                yield event

        def _make_header_fn():
            def _header():
                spinner_frame[0] += 1
                return self._build_progress_bar(step_names, completed_indices, current_index, spinner_frame[0])

            return _header

        async def _stop_renderer() -> bool:
            nonlocal renderer_task, agent_events_queue
            if renderer_task is not None and not renderer_task.done() and agent_events_queue is not None:
                await agent_events_queue.put(None)
                # Bounded wait mirrors the final cleanup: a wedged renderer must
                # not hang the transition between render phases.
                try:
                    await asyncio.wait_for(renderer_task, timeout=3.0)
                except asyncio.TimeoutError:
                    renderer_task.cancel()
                    try:
                        await renderer_task
                    except asyncio.CancelledError:
                        pass
                renderer_task = None
                return True
            return False

        try:
            while True:
                restarted = False
                async for event in event_stream:
                    # Check for Esc interrupt
                    if interrupt_requested.is_set():
                        # Freeze AgentLoops at next turn boundary so candidates
                        # don't race ahead while the user is typing or the
                        # judge LLM is in flight. Pause BEFORE stopping the
                        # renderer so no extra events queue up during teardown.
                        if self._pipeline:
                            self._last_interrupt_paused = False
                            self._pipeline.pause_agent_loops()
                        try:
                            had_renderer = await _stop_renderer()
                            user_input = await self._read_pipeline_interrupt_input()
                            if not user_input.is_empty:
                                needs_restart, feedback = await self._handle_mid_pipeline_message(
                                    user_input, suppress_render=True
                                )
                                if needs_restart and self._pipeline:
                                    event_stream = await self._restart_pipeline_stream_after_interrupt(
                                        event_stream, completed_indices
                                    )
                                    restarted = True
                                    interrupt_requested.clear()
                                    break
                            else:
                                feedback = ""
                        finally:
                            if self._pipeline and not getattr(self, "_last_interrupt_paused", False):
                                self._pipeline.resume_agent_loops()
                        interrupt_requested.clear()
                        if self._pipeline_waiting_input:
                            return None
                        if had_renderer:
                            agent_events_queue = asyncio.Queue()
                            renderer_task = asyncio.create_task(
                                self.renderer.run_streaming_output(
                                    _agent_event_gen(agent_events_queue),
                                    permission_handler=self.renderer.prompt_permission,
                                    live_header=_make_header_fn(),
                                    on_escape=_on_escape,
                                )
                            )
                            if feedback:
                                await agent_events_queue.put(TextDeltaEvent(text="\n" + feedback + "\n"))
                        continue

                    if isinstance(event, PipelineEvent):
                        self._record_pipeline_display_event(event)
                        if event.type == PipelineEventType.STEP_STARTED:
                            self._pipeline_display_current_step_id = event.step_id
                        # Only tear down the renderer at boundaries that genuinely
                        # end its lifetime. STEP_COMPLETED is NOT such a boundary —
                        # the agent loop may still emit MessageEndEvent / ToolResultEvent
                        # immediately after, which would otherwise be silently
                        # dropped (U-C2). Tear down on STEP_STARTED (new step
                        # gets a new renderer below), USER_INPUT_REQUIRED (REPL
                        # takes over input), or PIPELINE_COMPLETED.
                        teardown_events = (
                            PipelineEventType.STEP_STARTED,
                            PipelineEventType.USER_INPUT_REQUIRED,
                            PipelineEventType.PIPELINE_COMPLETED,
                        )
                        if event.type in teardown_events:
                            if (
                                renderer_task is not None
                                and not renderer_task.done()
                                and agent_events_queue is not None
                            ):
                                await agent_events_queue.put(None)
                                await renderer_task
                                renderer_task = None
                                agent_events_queue = None

                        # Detect candidate selection step — enter tabbed selection mode
                        if (
                            event.type == PipelineEventType.STEP_STARTED
                            and event.data.get("ui_mode") == PipelineUiMode.CANDIDATE_SELECTION.value
                        ):
                            current_index = event.data.get("index", 1) - 1
                            self._update_pipeline_state_from_event(event)
                            self._render_pipeline_event(event)
                            selection_result = await self._render_candidate_selection_tabs(
                                event_stream, progress_bar_fn=_make_header_fn()
                            )
                            if (
                                isinstance(selection_result, PipelineEvent)
                                and selection_result.type == PipelineEventType.PIPELINE_COMPLETED
                            ):
                                return selection_result
                            if self._pipeline_waiting_input:
                                return None
                            if selection_result is True and self._pipeline:
                                self._pipeline_waiting_input = False
                                event_stream = await self._restart_pipeline_stream_after_interrupt(
                                    event_stream, completed_indices
                                )
                                restarted = True
                                break
                            completed_indices.add(current_index)
                            continue

                        # Detect parallel sub-pipeline step — enter tab mode
                        if (
                            event.type == PipelineEventType.STEP_STARTED
                            and event.data.get("step_type") == PipelineStepType.PARALLEL_SUB_PIPELINE.value
                        ):
                            current_index = event.data.get("index", 1) - 1
                            self._update_pipeline_state_from_event(event)
                            self._render_pipeline_event(event)
                            tabs_interrupted = await self._render_parallel_tabs(
                                event_stream, progress_bar_fn=_make_header_fn()
                            )
                            if (
                                isinstance(tabs_interrupted, PipelineEvent)
                                and tabs_interrupted.type == PipelineEventType.PIPELINE_COMPLETED
                            ):
                                return tabs_interrupted
                            if self._pipeline_waiting_input:
                                return None
                            if tabs_interrupted is True and self._pipeline:
                                event_stream = await self._restart_pipeline_stream_after_interrupt(
                                    event_stream, completed_indices
                                )
                                restarted = True
                                break
                            completed_indices.add(current_index)
                            continue

                        self._update_pipeline_state_from_event(event)
                        self._render_pipeline_event(event)

                        if event.type == PipelineEventType.PIPELINE_COMPLETED:
                            return event

                        if event.type == PipelineEventType.PIPELINE_STARTED:
                            step_names = self._pipeline_step_names
                            completed_indices = self._pipeline_completed_indices

                        if event.type == PipelineEventType.USER_INPUT_REQUIRED:
                            # Renderer + queue already torn down by the top-level teardown guard
                            # for this event type. Just mark the waiting flag and return.
                            self._pipeline_waiting_input = True
                            return

                        if event.type == PipelineEventType.STEP_STARTED:
                            current_index = event.data.get("index", 1) - 1
                            spinner_frame[0] = 0
                            agent_events_queue = asyncio.Queue()
                            renderer_task = asyncio.create_task(
                                self.renderer.run_streaming_output(
                                    _agent_event_gen(agent_events_queue),
                                    permission_handler=self.renderer.prompt_permission,
                                    live_header=_make_header_fn(),
                                    on_escape=_on_escape,
                                )
                            )

                        if event.type == PipelineEventType.STEP_COMPLETED:
                            step_id = event.step_id or ""
                            idx = next((i for i, n in enumerate(step_names) if n == step_id), -1)
                            if idx >= 0:
                                completed_indices.add(idx)

                    elif isinstance(event, (StepResult, SubPipelineStreamEvent)):
                        continue
                    elif isinstance(event, AskUserQuestionEvent):
                        had_renderer = await _stop_renderer()
                        try:
                            answer = await self.renderer.prompt_user_question(event)
                        except (asyncio.CancelledError, KeyboardInterrupt):
                            if event.response_future is not None and not event.response_future.done():
                                event.response_future.set_result(None)
                            raise
                        except Exception as exc:
                            if event.response_future is not None and not event.response_future.done():
                                event.response_future.set_result(None)
                            msg = _("Error: {error}").format(error=str(exc))
                            self.renderer.print_system_message(msg, style="red")
                        else:
                            if event.response_future is not None and not event.response_future.done():
                                event.response_future.set_result(answer)

                        if had_renderer:
                            agent_events_queue = asyncio.Queue()
                            renderer_task = asyncio.create_task(
                                self.renderer.run_streaming_output(
                                    _agent_event_gen(agent_events_queue),
                                    permission_handler=self.renderer.prompt_permission,
                                    live_header=_make_header_fn(),
                                    on_escape=_on_escape,
                                )
                            )
                    else:
                        if isinstance(event, ToolUseStartEvent):
                            self._record_pipeline_display_tool_use(event)
                        if renderer_task is not None and not renderer_task.done() and agent_events_queue is not None:
                            await agent_events_queue.put(event)
                        else:
                            logger.warning(
                                "dropped agent event in pipeline gap: {}",
                                type(event).__name__,
                            )
                else:
                    break  # Stream finished naturally

                if not restarted:
                    break
        finally:
            # aclose() may raise CancelledError (a BaseException, not Exception)
            # if the cleanup chain is itself cancelled. The renderer teardown
            # lives in a finally so it runs regardless — otherwise that exception
            # would skip it and orphan renderer_task — and the CancelledError
            # still propagates afterwards (asyncio contract).
            try:
                await event_stream.aclose()
            except Exception:
                logger.debug("event_stream aclose failed", exc_info=True)
            finally:
                if renderer_task is not None and not renderer_task.done():
                    if agent_events_queue is not None:
                        await agent_events_queue.put(None)
                    try:
                        await asyncio.wait_for(renderer_task, timeout=3.0)
                    except asyncio.TimeoutError:
                        renderer_task.cancel()
                        try:
                            await renderer_task
                        except asyncio.CancelledError:
                            pass
                        except Exception as exc:
                            logger.warning("renderer_task cleanup failed: %s", exc, exc_info=True)
                    except asyncio.CancelledError:
                        pass
                    except Exception as exc:
                        logger.warning("renderer_task cleanup failed: %s", exc, exc_info=True)

    async def _render_candidate_selection_tabs(
        self, event_stream, progress_bar_fn=None
    ) -> str | bool | PipelineEvent | None:
        """Render candidate selection with tabbed architecture diagrams and details.

        Returns the selected candidate name, True for a restart-triggering
        hard interrupt, a terminal PIPELINE_COMPLETED event, or None.
        """
        from iac_code.pipeline.engine.events import PipelineEvent, PipelineEventType
        from iac_code.pipeline.engine.types import StepResult
        from iac_code.pipeline.engine.ui_contract import encode_selected_candidate
        from iac_code.ui.components.candidate_selection import CandidateSelectionRenderer
        from iac_code.ui.core.raw_input import RawInputCapture

        tabs = CandidateSelectionRenderer(console=self.renderer.console)
        waiting_input = False
        selected = None
        interrupted = False
        interrupt_feedback = ""
        terminal_event: PipelineEvent | None = None

        detail_tool_ids: set[str] = set()
        detail_accumulated: dict[str, str] = {}

        live = Live(
            console=self.renderer.console,
            refresh_per_second=12,
            transient=True,
        )

        def _live_update(content):
            if progress_bar_fn is not None:
                live.update(Group(content, progress_bar_fn()))
            else:
                live.update(content)

        stop_keys = asyncio.Event()
        interrupt_requested = asyncio.Event()
        parent_task = asyncio.current_task()

        def _request_pipeline_cancel() -> None:
            self._pipeline_waiting_input = False
            stop_keys.set()
            if parent_task is not None and not parent_task.done():
                parent_task.cancel()

        async def key_reader():
            loop = asyncio.get_running_loop()
            try:
                with RawInputCapture(use_cbreak=True) as cap:
                    while not stop_keys.is_set():
                        key_event = await loop.run_in_executor(None, cap.read_key, 0.1)
                        if key_event is None:
                            continue

                        if key_event.ctrl and key_event.key == "c":
                            _request_pipeline_cancel()
                            return

                        if key_event.key == "escape":
                            interrupt_requested.set()
                            if self._pipeline:
                                self._pipeline.pause_agent_loops()
                            return
                        if waiting_input and key_event.key == "enter":
                            nonlocal selected
                            candidate_selection = tabs.confirm_selection()
                            if candidate_selection.selected_candidate_name:
                                selected = candidate_selection
                                stop_keys.set()
                            continue
                        if tabs.handle_key(key_event):
                            _live_update(tabs.render())
            except (OSError, ValueError):
                pass

        async def _handle_esc_interrupt() -> bool:
            """Handle ESC interrupt prompt. Returns True if pipeline restarted."""
            nonlocal interrupt_feedback
            if self._pipeline:
                self._last_interrupt_paused = False
                self._pipeline.pause_agent_loops()
            live_stopped = False
            try:
                await _cancel_key_task()
                tabs.set_status_message("✎")
                _live_update(tabs.render())

                live.stop()
                live_stopped = True
                user_input = await self._read_pipeline_interrupt_input()
                live.start()
                live_stopped = False

                if not user_input.is_empty:
                    tabs.set_status_message(_("Judging your input..."))
                    _live_update(tabs.render())
                    needs_restart, feedback = await self._handle_mid_pipeline_message(user_input, suppress_render=True)
                    if feedback:
                        tabs.set_status_message(feedback)
                    else:
                        tabs.set_status_message("")
                    if needs_restart:
                        interrupt_feedback = feedback
                        return True
                else:
                    tabs.set_status_message("")
            finally:
                if live_stopped:
                    live.start()
                if self._pipeline and not getattr(self, "_last_interrupt_paused", False):
                    self._pipeline.resume_agent_loops()
            interrupt_requested.clear()
            _live_update(tabs.render())
            return False

        key_task: asyncio.Task | None = None

        async def _cancel_key_task() -> None:
            nonlocal key_task
            if key_task and not key_task.done():
                key_task.cancel()
                try:
                    await asyncio.shield(key_task)
                except asyncio.CancelledError:
                    if not key_task.done() or not key_task.cancelled():
                        raise
                except OSError:
                    pass
            key_task = None

        async def _stop_key_reader() -> None:
            stop_keys.set()
            await _cancel_key_task()

        try:
            live.start()
            key_task = asyncio.create_task(key_reader())

            async for event in event_stream:
                if interrupt_requested.is_set():
                    if await _handle_esc_interrupt():
                        interrupted = True
                        await _stop_key_reader()
                        break
                    if self._pipeline_waiting_input and getattr(self, "_last_interrupt_paused", False):
                        await _stop_key_reader()
                        return None
                    key_task = asyncio.create_task(key_reader())

                if isinstance(event, PipelineEvent):
                    if event.type == PipelineEventType.USER_INPUT_REQUIRED:
                        recorder = getattr(self, "_pipeline_display_recorder", None)
                        if recorder is not None:
                            try:
                                recorder.record(
                                    "candidate_selection_ready",
                                    step_id=getattr(self, "_pipeline_display_current_step_id", None),
                                    payload=dict(event.data),
                                    timestamp=event.timestamp,
                                )
                            except Exception as exc:
                                logger.warning("Failed to record candidate selection ready event: {}", exc)
                    else:
                        self._record_pipeline_display_event(event)
                    if event.type == PipelineEventType.USER_INPUT_REQUIRED:
                        options = event.data.get("options", [])
                        tabs.seed_candidates(options if isinstance(options, list) else [])
                        waiting_input = True
                        tabs.enter_selection_mode()
                        self._pipeline_waiting_input = True
                        _live_update(tabs.render())
                        while not stop_keys.is_set():
                            done, _pending = await asyncio.wait(
                                [
                                    asyncio.ensure_future(stop_keys.wait()),
                                    asyncio.ensure_future(interrupt_requested.wait()),
                                ],
                                return_when=asyncio.FIRST_COMPLETED,
                            )
                            if interrupt_requested.is_set():
                                if await _handle_esc_interrupt():
                                    interrupted = True
                                    await _stop_key_reader()
                                    break
                                if self._pipeline_waiting_input and getattr(self, "_last_interrupt_paused", False):
                                    await _stop_key_reader()
                                    return None
                                key_task = asyncio.create_task(key_reader())
                                continue
                            break
                        break

                    if event.type == PipelineEventType.STEP_COMPLETED:
                        continue

                    if event.type in (
                        PipelineEventType.STEP_FAILED,
                        PipelineEventType.PIPELINE_COMPLETED,
                        PipelineEventType.ROLLBACK_TRIGGERED,
                    ):
                        if event.type == PipelineEventType.PIPELINE_COMPLETED:
                            terminal_event = event
                        break

                elif isinstance(event, DiagramEvent):
                    self._record_pipeline_display_candidate_diagram(event)
                    tabs.add_diagram(
                        event.candidate_name,
                        event.mermaid_source,
                        candidate_index=event.candidate_index,
                    )

                elif isinstance(event, CandidateDetailEvent):
                    self._record_pipeline_display_candidate_detail(event)
                    # U-I14: pass tool_use_id as the dedup key so multiple
                    # show_candidate_detail calls for the same candidate_name
                    # don't silently overwrite each other in the renderer.
                    tabs.add_detail(
                        event.tool_use_id,
                        event.candidate_name,
                        event.summary,
                        event.cost_items,
                        event.total_monthly_cost,
                        candidate_index=event.candidate_index,
                    )
                    expired = [tid for tid in detail_tool_ids if tid in detail_accumulated]
                    for tid in expired:
                        detail_tool_ids.discard(tid)
                        detail_accumulated.pop(tid, None)

                elif isinstance(event, ToolUseStartEvent):
                    self._record_pipeline_display_tool_use(event)
                    if event.name == "show_candidate_detail":
                        detail_tool_ids.add(event.tool_use_id)
                        detail_accumulated[event.tool_use_id] = ""

                elif isinstance(event, ToolInputDeltaEvent):
                    if event.tool_use_id in detail_tool_ids:
                        detail_accumulated[event.tool_use_id] += event.partial_json
                        acc = detail_accumulated[event.tool_use_id]
                        cname = extract_json_string_value(acc, "candidate_name")
                        candidate_index = extract_json_int_value(acc, "candidate_index")
                        summary = extract_json_string_value(acc, "summary", allow_partial=True)
                        if cname and summary:
                            tabs.update_streaming_summary(cname, summary, candidate_index=candidate_index)

                elif isinstance(event, StepResult):
                    continue

                if tabs.tab_count > 0:
                    _live_update(tabs.render())

        except (asyncio.CancelledError, KeyboardInterrupt):
            self._pipeline_waiting_input = False
            raise
        finally:
            try:
                await _stop_key_reader()
            finally:
                live.stop()

        if interrupted:
            self._pipeline_waiting_input = False
            try:
                await event_stream.aclose()
            except Exception:
                pass
            static_content = tabs.render_selected_static()
            if static_content is not None:
                self.renderer.console.print()
                self.renderer.console.print(static_content)
            if interrupt_feedback:
                self.renderer.console.print()
                self._render_interrupt_feedback_inline(interrupt_feedback)
            return True

        if terminal_event is not None:
            return terminal_event

        if selected and self._pipeline is not None:
            self._pipeline_waiting_input = False
            selected_name = selected.selected_candidate_name
            selected_label = selected.display_label or selected_name
            self._record_pipeline_display_candidate_selected(
                step_id=getattr(self, "_pipeline_display_current_step_id", None),
                candidate_name=selected.selected_candidate_name,
                candidate_index=selected.selected_candidate_index,
            )
            self.renderer.console.print()
            self.renderer.console.print("  [green]✓[/] {} [bold]{}[/]".format(_("Selected:"), selected_label))
            static_content = tabs.render_selected_static()
            if static_content is not None:
                self.renderer.console.print()
                self.renderer.console.print(static_content)
            # U-I7: release the outer candidate-selection stream before recursing
            # into _render_pipeline_stream on the resumed stream. Without this,
            # the outer generator is only implicitly drained (via _continue_from_current
            # returning early on USER_INPUT_REQUIRED) — a fragile contract.
            try:
                await event_stream.aclose()
            except Exception:
                pass
            resume_payload = encode_selected_candidate(
                selected.selected_candidate_name,
                selected.selected_candidate_index,
            )
            event_stream = self._pipeline.resume(resume_payload)
            try:
                self.store.set_state(is_busy=True)
                terminal_event = await self._render_pipeline_stream(event_stream)
            finally:
                self.store.set_state(is_busy=False)
            if terminal_event is not None:
                return terminal_event

        return selected.selected_candidate_name if selected else None

    def _create_parallel_live(self) -> Live:
        return Live(
            console=self.renderer.console,
            refresh_per_second=12,
            transient=True,
        )

    async def _render_parallel_tabs(self, event_stream, progress_bar_fn=None) -> bool | PipelineEvent | None:
        """Render parallel sub-pipeline execution with tab switching UI.

        Returns True if a hard_interrupt occurred and the pipeline stream needs
        restart, a terminal PIPELINE_COMPLETED event, or False.

        Uses StreamAccumulator per candidate to process events the same way
        as run_streaming_output, then calls renderer._render_segments to
        render the selected tab's content with full markdown/tool formatting.
        """
        from iac_code.pipeline.display_names import display_step_name
        from iac_code.pipeline.engine.events import PipelineEvent, PipelineEventType
        from iac_code.pipeline.engine.types import StepResult
        from iac_code.ui.components.parallel_tabs import CandidateState, CandidateStatus, ParallelTabsRenderer
        from iac_code.ui.core.raw_input import RawInputCapture
        from iac_code.ui.stream_accumulator import StreamAccumulator

        candidates: list[CandidateState] = []
        tabs_renderer: ParallelTabsRenderer | None = None
        step_counters: dict[str, int] = {}
        accumulators: dict[str, StreamAccumulator] = {}
        terminal_event: PipelineEvent | None = None

        live = self._create_parallel_live()

        stop_keys = asyncio.Event()
        interrupt_requested = asyncio.Event()
        parent_task = asyncio.current_task()

        def _request_pipeline_cancel() -> None:
            self._pipeline_waiting_input = False
            stop_keys.set()
            if parent_task is not None and not parent_task.done():
                parent_task.cancel()

        async def key_reader():
            loop = asyncio.get_running_loop()
            try:
                with RawInputCapture(use_cbreak=True) as cap:
                    while not stop_keys.is_set():
                        key_event = await loop.run_in_executor(None, cap.read_key, 0.1)
                        if key_event is None:
                            continue

                        if key_event.ctrl and key_event.key == "c":
                            _request_pipeline_cancel()
                            return

                        if key_event.key == "escape":
                            interrupt_requested.set()
                            if self._pipeline:
                                self._pipeline.pause_agent_loops()
                            return
                        if tabs_renderer:
                            tabs_renderer.handle_key(key_event)
            except (OSError, ValueError):
                pass

        async def _cancel_key_task() -> None:
            nonlocal key_task
            if key_task and not key_task.done():
                key_task.cancel()
                try:
                    await asyncio.shield(key_task)
                except asyncio.CancelledError:
                    if not key_task.done() or not key_task.cancelled():
                        raise
                except OSError:
                    pass
            key_task = None

        async def _stop_key_reader() -> None:
            stop_keys.set()
            await _cancel_key_task()

        def _new_live() -> Live:
            return self._create_parallel_live()

        def _update_live():
            if not tabs_renderer:
                return
            selected = tabs_renderer.selected_index
            if selected < len(candidates):
                sub_id = candidates[selected].sub_pipeline_id
                acc = accumulators.get(sub_id)
                if acc:
                    content = self.renderer._render_segments(
                        acc.segments, None, acc.text_buffer, thinking_buffer=acc.thinking_buffer, embedded=True
                    )
                    rendered = tabs_renderer.render_with_content(content)
                    if progress_bar_fn is not None:
                        live.update(Group(rendered, progress_bar_fn()))
                    else:
                        live.update(rendered)
                    return
            rendered = tabs_renderer.render()
            if progress_bar_fn is not None:
                live.update(Group(rendered, progress_bar_fn()))
            else:
                live.update(rendered)

        key_task: asyncio.Task | None = None

        async def _prompt_child_permission(sub_id: str, inner: PermissionRequestEvent) -> None:
            nonlocal key_task, live
            response_future = inner.response_future
            if response_future is None or response_future.done():
                return

            allowed = False
            try:
                await _stop_key_reader()
                if tabs_renderer:
                    tabs_renderer.set_input_line(None)
                live.stop()
                allowed = await self.renderer.prompt_permission(inner)
            except asyncio.CancelledError:
                if not response_future.done():
                    response_future.set_result(False)
                raise
            except Exception:
                logger.warning("Permission prompt failed for parallel sub-pipeline {}", sub_id, exc_info=True)
                allowed = False
            if not response_future.done():
                response_future.set_result(allowed)
            stop_keys.clear()
            live = _new_live()
            live.start()
            key_task = asyncio.create_task(key_reader())
            _update_live()

        try:
            live.start()
            key_task = asyncio.create_task(key_reader())

            async for event in event_stream:
                # Check for Esc interrupt
                if interrupt_requested.is_set():
                    if self._pipeline:
                        self._last_interrupt_paused = False
                        self._pipeline.pause_agent_loops()
                    live_stopped = False
                    try:
                        await _cancel_key_task()
                        if tabs_renderer:
                            tabs_renderer.set_input_line("✎")
                        _update_live()

                        live.stop()
                        live_stopped = True
                        user_input = await self._read_pipeline_interrupt_input()
                        live.start()
                        live_stopped = False
                        if tabs_renderer:
                            tabs_renderer.set_input_line(None)

                        if not user_input.is_empty:
                            if tabs_renderer:
                                tabs_renderer.set_input_line(_("Judging your input..."))
                            _update_live()
                            needs_restart, feedback = await self._handle_mid_pipeline_message(
                                user_input, suppress_render=True
                            )
                            if tabs_renderer:
                                tabs_renderer.set_input_line(None)
                            if needs_restart:
                                # Unlike _render_candidate_selection_tabs (which
                                # snapshots the committed selection), the parallel
                                # candidates are mid-execution and discarded by the
                                # rollback, so there's no meaningful state to print
                                # — a half-streamed "✓ 完成" would be misleading.
                                # Return True and let the caller close + restart the
                                # stream; the transient Live content is dropped on
                                # purpose.
                                await _stop_key_reader()
                                return True
                            if self._pipeline_waiting_input and getattr(self, "_last_interrupt_paused", False):
                                await _stop_key_reader()
                                return None
                            if feedback:
                                for acc in accumulators.values():
                                    acc.text_buffer += "\n" + feedback + "\n"
                    finally:
                        if live_stopped:
                            live.start()
                        if self._pipeline and not getattr(self, "_last_interrupt_paused", False):
                            self._pipeline.resume_agent_loops()
                    interrupt_requested.clear()
                    key_task = asyncio.create_task(key_reader())
                    _update_live()
                    continue

                if isinstance(event, PipelineEvent):
                    self._record_pipeline_display_event(event)
                    if event.type == PipelineEventType.SUB_PIPELINE_STARTED:
                        cs = CandidateState(
                            sub_pipeline_id=event.data["sub_pipeline_id"],
                            candidate_index=event.data["candidate_index"],
                            name=event.data.get(
                                "candidate_name",
                                _("Candidate {index}").format(index=event.data["candidate_index"] + 1),
                            ),
                            total_steps=event.data.get("total_steps", 3),
                        )
                        replaced = False
                        for ci, existing in enumerate(candidates):
                            if existing.candidate_index == cs.candidate_index:
                                old_id = existing.sub_pipeline_id
                                candidates[ci] = cs
                                accumulators.pop(old_id, None)
                                step_counters.pop(old_id, None)
                                replaced = True
                                break
                        if not replaced:
                            candidates.append(cs)
                        step_counters[cs.sub_pipeline_id] = 0
                        accumulators[cs.sub_pipeline_id] = StreamAccumulator()
                        tabs_renderer = ParallelTabsRenderer(candidates=candidates, console=self.renderer.console)

                    elif event.type == PipelineEventType.SUB_STEP_STARTED:
                        sub_id = event.data.get("sub_pipeline_id", "")
                        step_name = event.data.get("step_id", "")
                        step_idx = event.data.get("step_index", 0)
                        if tabs_renderer:
                            tabs_renderer.update_step(sub_id, display_step_name(step_name), step_idx + 1)

                    elif event.type == PipelineEventType.SUB_STEP_COMPLETED:
                        sub_id = event.data.get("sub_pipeline_id", "")
                        if sub_id in step_counters:
                            step_counters[sub_id] += 1
                            if tabs_renderer:
                                tabs_renderer.update_step(sub_id, step_name="", completed=step_counters[sub_id])

                    elif event.type == PipelineEventType.SUB_PIPELINE_COMPLETED:
                        sub_id = event.data.get("sub_pipeline_id", "")
                        if sub_id in accumulators:
                            accumulators[sub_id].finalize_text()
                        if tabs_renderer:
                            if event.data.get("failed", False):
                                tabs_renderer.mark_failed(sub_id, error=event.data.get("error", ""))
                            else:
                                tabs_renderer.mark_done(sub_id)

                    elif event.type == PipelineEventType.STEP_COMPLETED:
                        break

                    elif event.type in (
                        PipelineEventType.STEP_STARTED,
                        PipelineEventType.PIPELINE_COMPLETED,
                        PipelineEventType.STEP_FAILED,
                    ):
                        if event.type == PipelineEventType.PIPELINE_COMPLETED:
                            terminal_event = event
                        break

                elif isinstance(event, SubPipelineStreamEvent):
                    sub_id = event.sub_pipeline_id
                    inner = event.inner
                    if isinstance(inner, PermissionRequestEvent):
                        await _prompt_child_permission(sub_id, inner)
                        continue
                    if isinstance(inner, ToolUseStartEvent):
                        self._record_pipeline_display_tool_use(inner, sub_pipeline_id=sub_id)
                    acc = accumulators.get(sub_id)
                    if acc:
                        acc.process(inner)

                elif isinstance(event, StepResult):
                    continue

                if tabs_renderer:
                    _update_live()

        except (asyncio.CancelledError, KeyboardInterrupt):
            self._pipeline_waiting_input = False
            raise
        finally:
            stop_keys.set()
            try:
                await _cancel_key_task()
            finally:
                live.stop()

        if tabs_renderer:
            self.renderer.console.print()
            summary = Text()
            for c in candidates:
                if c.status == CandidateStatus.DONE:
                    summary.append(_("  ✓ {name}: Completed\n").format(name=c.name), style="green")
                elif c.status == CandidateStatus.FAILED:
                    summary.append(_("  ✘ {name}: Failed").format(name=c.name), style="red")
                    if c.error:
                        summary.append(" — {}".format(c.error), style="dim red")
                    summary.append("\n")
            self.renderer.console.print(summary)

        if terminal_event is not None:
            return terminal_event
        return False

    _SPINNER_CHARS = "◐◓◑◒"

    def _build_progress_bar(self, step_names: list[str], completed: set[int], current_index: int, spinner_frame: int):
        """Build horizontal progress bar as a Rich Text object."""
        from iac_code.pipeline.display_names import display_step_name
        from iac_code.ui.pipeline_styles import PIPELINE_ACTIVE_PROGRESS_STYLE

        bar = Text()
        for i, name in enumerate(step_names):
            label = display_step_name(name)
            if i > 0:
                bar.append(" → ", style="dim")
            if i in completed:
                bar.append(f"✓ {label}", style="green")
            elif i == current_index:
                char = self._SPINNER_CHARS[spinner_frame % 4]
                bar.append(f"{char} {label}", style=PIPELINE_ACTIVE_PROGRESS_STYLE)
            else:
                bar.append(label, style="dim")
        return bar

    def _update_pipeline_state_from_event(self, event):
        """Pre-render: update instance state from event metadata.

        Kept separate from _render_pipeline_event so render is pure (U-I16).
        """
        from iac_code.pipeline.engine.events import PipelineEventType

        if event.type == PipelineEventType.PIPELINE_STARTED:
            self._pipeline_step_names = event.data.get("step_names", [])
            self._pipeline_start_time = time.time()
            self._pipeline_completed_indices = set()

    def _render_pipeline_event(self, event):
        from rich.panel import Panel

        from iac_code.pipeline.display_names import display_pipeline_name, display_step_name
        from iac_code.pipeline.engine.events import PipelineEventType
        from iac_code.ui.pipeline_styles import PIPELINE_PANEL_BORDER_STYLE, pipeline_step_header, pipeline_title

        con = self.renderer.console

        match event.type:
            case PipelineEventType.PIPELINE_STARTED:
                name = event.data.get("pipeline_type", "Pipeline")
                title = _("AI {name} Pipeline").format(name=display_pipeline_name(name))
                con.print()
                con.print(pipeline_title(title))
                con.print()
            case PipelineEventType.STEP_STARTED:
                step_id = event.step_id or ""
                idx = event.data.get("index", 1)
                total = event.data.get("total", len(self._pipeline_step_names))
                con.print()
                con.print(pipeline_step_header(f"● {display_step_name(step_id)} ({idx}/{total})"))
            case PipelineEventType.STEP_COMPLETED:
                pass
            case PipelineEventType.STEP_FAILED:
                err = event.data.get("error", "")
                step_id = event.step_id or ""
                con.print(f"  [red]✗ {display_step_name(step_id)}[/] [dim]── {err}[/]")
            case PipelineEventType.USER_INPUT_REQUIRED:
                options = event.data.get("options", [])
                prompt_text = event.data.get("prompt", "")
                if prompt_text:
                    con.print(f"\n{prompt_text}")
                if options:
                    for i, opt in enumerate(options, 1):
                        name = opt.get("name", _("Option {index}").format(index=i))
                        summary = opt.get("summary", opt.get("description", ""))
                        body = summary or name
                        con.print(Panel(body, title=f"[bold]{i}. {name}[/]", border_style=PIPELINE_PANEL_BORDER_STYLE))
                    con.print("\n" + _("Please enter your choice:"))
            case PipelineEventType.ROLLBACK_TRIGGERED:
                con.print(
                    "  [yellow]⟲[/] "
                    + _("Rollback: {from_step} → {to_step}").format(
                        from_step=display_step_name(str(event.data.get("from_step") or "")),
                        to_step=display_step_name(str(event.data.get("to_step") or "")),
                    )
                )
            case PipelineEventType.PIPELINE_COMPLETED:
                if not event.data.get("early_exit") and not event.data.get("failed"):
                    elapsed = time.time() - getattr(self, "_pipeline_start_time", time.time())
                    if elapsed >= 60:
                        elapsed_str = f"{elapsed / 60:.1f}m"
                    else:
                        elapsed_str = f"{elapsed:.1f}s"
                    con.print()
                    con.print(
                        "  [green]{}[/] [dim]{}[/]".format(
                            _("✔ Pipeline completed"),
                            _("── total time {duration}").format(duration=elapsed_str),
                        )
                    )
            case _:
                pass

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
        self._refresh_system_prompt()

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

    def _detect_pipeline_session(self, cwd: str, session_id: str) -> bool:
        """Check if a session has an actively resumable pipeline sidecar.

        Sidecar lives at ``<session_id>/pipeline/`` under the session dir,
        nested with main's directory-format session layout (problem 4).
        """
        from pathlib import Path

        from iac_code.pipeline.engine.session import PipelineSession

        raw_session_dir = self._session_storage.session_dir(cwd, session_id)
        if not isinstance(raw_session_dir, (str, Path)):
            return False
        sidecar = PipelineSession(Path(raw_session_dir) / "pipeline")
        return sidecar.has_resumable_status()

    def _resolve_initial_runtime_mode(self, resume_session_id: str | bool | None) -> RunMode:
        """Resolve startup routing once, then keep it session-local.

        A fresh explicit pipeline launch still enters pipeline mode via
        IAC_CODE_MODE. When resuming, pipeline mode only takes over if the
        target session has an active sidecar; otherwise normal chat history
        should load even if the parent process still has IAC_CODE_MODE set.
        """
        from iac_code.pipeline.config import RunMode, get_run_mode, get_working_directory

        mode = get_run_mode()
        if resume_session_id is None:
            return mode
        pipeline_cwd = get_working_directory() or self._original_cwd
        if self._detect_pipeline_session(pipeline_cwd, self._session_id):
            return RunMode.PIPELINE
        if mode == RunMode.PIPELINE:
            return RunMode.NORMAL
        return mode

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
        """Load and repair saved messages when resuming a session.

        The pipeline sidecar takes priority ONLY in pipeline mode. In normal
        mode, even if a pipeline sidecar exists from a prior run, we MUST
        load the chat history — the user explicitly switched modes and
        expects their conversation back (N-I1).
        """
        if resume is None:
            return []
        # Lazy import to avoid pulling pipeline subsystem into normal-mode
        # startup (a separate clean-up tracked under N-I2).
        from iac_code.pipeline.config import RunMode, get_working_directory

        pipeline_cwd = get_working_directory() or self._original_cwd
        if self._get_runtime_mode() == RunMode.PIPELINE and self._detect_pipeline_session(
            pipeline_cwd, self._session_id
        ):
            return []
        messages = self._session_storage.load(self._original_cwd, self._session_id)
        repaired = self._session_storage.repair_interrupted(messages)
        if not repaired:
            repaired = self._load_terminal_pipeline_initial_user_message(pipeline_cwd, self._session_id)
        return self._with_terminal_pipeline_abort_notice(repaired, pipeline_cwd, self._session_id)

    @staticmethod
    def _pipeline_abort_notice_text() -> str:
        return _("Pipeline was interrupted. Switched to normal chat; you can continue from here.")

    def _switch_user_aborted_pipeline_to_normal(self) -> None:
        """Switch the current REPL to normal chat after a user-aborted pipeline."""
        from iac_code.pipeline.config import RunMode

        self._record_pipeline_display_user_aborted()
        self._set_runtime_mode(RunMode.NORMAL)
        try:
            messages = self._session_storage.load(self._original_cwd, self._session_id)
            repaired = self._session_storage.repair_interrupted(messages)
            self._agent_loop.replace_session(self._session_id, repaired or None)
            if self._has_pipeline_abort_notice(repaired):
                return
            notice_text = self._pipeline_abort_notice_text()
            self.renderer.print_system_message(notice_text, style="yellow")
            injected = self._agent_loop.context_manager.add_raw_message({"role": "assistant", "content": notice_text})
            self._session_storage.append(
                self._original_cwd,
                self._session_id,
                injected,
                git_branch=self.current_git_branch(),
            )
        except Exception as exc:
            logger.warning("Failed to switch aborted pipeline to normal chat: {}", exc)

    def _with_terminal_pipeline_abort_notice(
        self,
        messages: list[Message],
        pipeline_cwd: str,
        session_id: str,
    ) -> list[Message]:
        """Add a replay-only abort notice for terminal pipeline sessions."""
        if not messages or self._has_pipeline_abort_notice(messages):
            return messages
        plain_user_turns = [
            message
            for message in messages
            if self._message_role(message) == "user" and isinstance(self._message_content(message), str)
        ]
        assistant_turns = [message for message in messages if self._message_role(message) == "assistant"]
        if len(plain_user_turns) != 1 or assistant_turns:
            return messages
        status = self._terminal_pipeline_status(pipeline_cwd, session_id)
        if status != "user_aborted":
            return messages
        return [*messages, Message(role="assistant", content=self._pipeline_abort_notice_text())]

    @classmethod
    def _has_pipeline_abort_notice(cls, messages: list[Message]) -> bool:
        notice = cls._pipeline_abort_notice_text()
        return any(
            cls._message_role(message) == "assistant" and cls._message_content(message) == notice
            for message in messages
        )

    @staticmethod
    def _message_role(message: Message | dict[str, Any]) -> str | None:
        if isinstance(message, dict):
            value = message.get("role")
            return value if isinstance(value, str) else None
        value = getattr(message, "role", None)
        return value if isinstance(value, str) else None

    @staticmethod
    def _message_content(message: Message | dict[str, Any]) -> Any:
        if isinstance(message, dict):
            return message.get("content")
        return getattr(message, "content", None)

    def _terminal_pipeline_status(self, pipeline_cwd: str, session_id: str) -> str | None:
        from pathlib import Path

        from iac_code.pipeline.engine.session import PipelineSession

        try:
            sidecar = PipelineSession(Path(self._session_storage.session_dir(pipeline_cwd, session_id)) / "pipeline")
            if not sidecar.exists():
                return None
            result = sidecar.restore_sync({})
            return result.status
        except Exception as exc:
            logger.warning("Failed to inspect terminal pipeline sidecar: {}", exc)
            return None

    def _persist_pipeline_visible_user_turn(self, user_input: str | "PipelineUserInput") -> None:
        """Persist the user-visible pipeline prompt into the root session."""
        from iac_code.pipeline.engine.user_input import normalize_pipeline_user_input

        pipeline_input = normalize_pipeline_user_input(user_input)
        if pipeline_input.is_empty:
            return
        visible_input = pipeline_input.content if pipeline_input.has_images else pipeline_input.display_text
        try:
            injected = self._agent_loop.context_manager.add_raw_message({"role": "user", "content": visible_input})
            self._session_storage.append(
                self._original_cwd,
                self._session_id,
                injected,
                git_branch=self.current_git_branch(),
            )
        except Exception as exc:
            logger.warning("Failed to persist pipeline user turn to root session: {}", exc)

    def _load_terminal_pipeline_initial_user_message(self, pipeline_cwd: str, session_id: str) -> list:
        """Recover the first visible pipeline prompt for terminal legacy sessions.

        Internal step transcripts are not normal chat history. This fallback is
        intentionally narrow: when the root session has no role messages and
        the sidecar is terminal, expose only the first plain user text so
        ``--resume`` does not look empty after a user-aborted pipeline.
        """
        from pathlib import Path

        from iac_code.pipeline.engine.session import PipelineSession
        from iac_code.pipeline.engine.transcript_storage import PipelineTranscriptStorage

        try:
            sidecar = PipelineSession(Path(self._session_storage.session_dir(pipeline_cwd, session_id)) / "pipeline")
            if not sidecar.exists():
                return []
            restore_result = sidecar.restore_sync({})
            if restore_result.status not in {"completed", "user_aborted", "failed", "discarded"}:
                return []
            transcript_storage = PipelineTranscriptStorage(sidecar.session_dir)
            transcript_ids: list[str] = []
            attempts = restore_result.attempts or {}
            items = attempts.get("items") if isinstance(attempts, dict) else None
            if isinstance(items, dict):
                attempt_items = cast(dict[Any, Any], items)
                for _attempt_id, raw_attempt in sorted(attempt_items.items()):
                    if not isinstance(raw_attempt, dict):
                        continue
                    attempt = cast(dict[str, Any], raw_attempt)
                    transcript_id = attempt.get("transcript_id")
                    if isinstance(transcript_id, str) and transcript_id:
                        transcript_ids.append(transcript_id)
            if not transcript_ids:
                transcript_ids = transcript_storage.list_transcript_ids()
            for transcript_id in transcript_ids:
                for message in transcript_storage.load(pipeline_cwd, transcript_id):
                    if message.role == "user" and isinstance(message.content, str) and message.content.strip():
                        return [message]
        except Exception as exc:
            logger.warning("Failed to load terminal pipeline prompt fallback: {}", exc)
        return []

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
        # getattr-guard: existing tests construct minimal InlineREPL instances
        # via object.__new__ that never set `_pipeline`. Treat that the same as
        # normal (non-pipeline) mode.
        if getattr(self, "_pipeline", None) is None:
            return self._build_normal_status()
        return self._build_pipeline_status()

    def _build_normal_status(self) -> dict[str, Any]:
        """Existing /status logic (extracted intact). Normal-mode regression-safe."""
        state = self.store.get_state()
        messages = self._agent_loop.context_manager.get_messages()
        refresh_usage = getattr(self._agent_loop, "refresh_session_usage", None)
        if callable(refresh_usage):
            refresh_usage()
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
            "memory_recall": self._agent_loop.get_memory_recall_stats()
            if hasattr(self._agent_loop, "get_memory_recall_stats")
            else {},
        }

    def _build_pipeline_status(self) -> dict[str, Any]:
        """Pipeline-aware /status: aggregate token usage across active candidate loops."""
        pipeline = self._pipeline
        assert pipeline is not None  # callers already gated on `_pipeline is None`
        state = self.store.get_state()
        loops = list(pipeline.iter_active_agent_loops())
        return {
            "session_id": self._session_id,
            "resumed": self._was_resumed,
            "provider": self._status_provider_display(),
            "model": self._status_model(state.model),
            "region": self._status_region(),
            "cwd": self._original_cwd,
            "pipeline": {
                "name": pipeline._loaded.name,
                "current_step": pipeline.state_machine.current_step.step_id,
                "step_index": pipeline.state_machine.current_step_index + 1,
                "total_steps": len(pipeline._loaded.steps),
            },
            "api_usage": self._aggregate_session_usage(loops),
            "turn_count": 0,
            "max_turns": max((loop.max_turns for loop in loops), default=0),
            "context_usage": self._aggregate_context_usage(loops),
        }

    def _aggregate_session_usage(self, loops: list[Any]) -> dict[str, int]:
        """Sum session usage across active agent loops, surviving per-loop races."""
        totals: dict[str, int] = {}
        for loop in loops:
            try:
                usage = loop.get_session_usage()
            except Exception:
                continue
            if isinstance(usage, dict):
                for k, v in usage.items():
                    if isinstance(v, (int, float)):
                        totals[k] = totals.get(k, 0) + v
        return totals

    def _aggregate_context_usage(self, loops: list[Any]) -> dict[str, int]:
        """Max context_usage across active loops — 'how full is the worst tab'."""
        totals: dict[str, int] = {}
        for loop in loops:
            try:
                usage = loop.get_context_usage()
            except Exception:
                continue
            if isinstance(usage, dict):
                for k, v in usage.items():
                    if isinstance(v, (int, float)):
                        totals[k] = max(totals.get(k, 0), v)
        return totals

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
        from iac_code.agent.message import ToolResultBlock, is_recalled_memory_message
        from iac_code.pipeline.engine.cleanup import is_cleanup_prompt_message

        turns = 0
        for message in messages:
            if getattr(message, "role", None) != "user":
                continue
            if is_recalled_memory_message(message) or is_cleanup_prompt_message(message):
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
        from iac_code.pipeline.config import get_working_directory
        from iac_code.services.permissions.trusted_roots import build_session_trusted_read_directories

        old_session_id = self._session_id
        pipeline_cwd = get_working_directory() or self._original_cwd
        new_messages = self._session_storage.load(self._original_cwd, new_session_id)
        new_messages = self._session_storage.repair_interrupted(new_messages)
        if not new_messages:
            new_messages = self._load_terminal_pipeline_initial_user_message(pipeline_cwd, new_session_id)
        new_messages = self._with_terminal_pipeline_abort_notice(new_messages, pipeline_cwd, new_session_id)
        self._agent_loop.replace_session(new_session_id, new_messages or None)
        self._session_id = new_session_id
        self._clear_pipeline_cleanup_ledger_path()
        self._was_resumed = True
        self._session_name = self._load_current_session_name()

        state = self.store.get_state()
        permission_context = getattr(state, "permission_context", None)
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
        self._print_cleanup_resume_summary()
        self._prune_cleanup_prompts_if_no_pending_cleanup()
        if new_messages:
            self._replay_resume_messages(new_messages)
            self.console.print()

    async def swap_session_async(self, new_session_id: str) -> None:
        """Async variant of swap_session for mid-pipeline sidecar detection.

        Steps:
          1. Clear self._pipeline (防旧 PipelineRunner 把状态写到新 session sidecar)
          2. 走原 swap_session sync 逻辑（load messages、replace_session、清屏、replay banner）
          3. 探测目标 session 是否有 pipeline sidecar
          4. 若有 → 弹确认 UI；用户选 resume → 重建 self._pipeline
        """
        # Step 1: clear pipeline so the old PipelineRunner can't write to new
        # session's sidecar after the id rotation in step 2.
        self._pipeline = None
        self._pipeline_waiting_input = False
        self._pipeline_restored_status = None

        # Step 2: delegate to existing sync swap (handles message reload, clear,
        # banner, history replay).
        self.swap_session(new_session_id)
        from iac_code.pipeline.config import RunMode

        self._set_runtime_mode(RunMode.NORMAL)

        # Step 3: detect target sidecar
        from iac_code.pipeline.config import get_pipeline_name, get_working_directory
        from iac_code.pipeline.engine.session import PipelineSession

        pipeline_cwd = get_working_directory() or self._original_cwd
        sidecar = PipelineSession(self._session_storage.session_dir(pipeline_cwd, new_session_id) / "pipeline")
        if not sidecar.has_resumable_status():
            return

        # Step 4: prompt user; conditionally resume
        choice = await self._confirm_pipeline_resume(sidecar.meta_path)
        if choice == "discard":
            try:
                sidecar.mark_discarded(reason="discarded from /resume picker")
            except Exception as exc:
                logger.opt(exception=True).warning("Failed to mark pipeline sidecar discarded during /resume: {}", exc)
                self.renderer.print_system_message(
                    _("Could not mark pipeline state discarded: {reason}").format(
                        reason=str(exc) or type(exc).__name__
                    ),
                    style="yellow",
                )
            return
        if choice == "resume":
            from iac_code.pipeline import create_pipeline

            self._pipeline = create_pipeline(
                name=get_pipeline_name(),
                provider_manager=self._provider_manager,
                base_tool_registry=self.tool_registry,
                session_storage=self._session_storage,
                session_id=new_session_id,
                cwd=pipeline_cwd,
                permission_context_getter=lambda: self.store.get_state().permission_context,
                memory_content_getter=self._pipeline_memory_content_getter(),
                auto_trigger_skills=self.command_registry.get_model_invocable_skills(),
                resume_from_sidecar=True,
            )
            restored = self._pipeline.sidecar_restore_result
            if restored is None or restored.ok is False:
                detail = None if restored is None else restored.reason or restored.status
                self.renderer.print_system_message(
                    _("Could not resume pipeline state: {reason}").format(reason=detail or _("unknown error")),
                    style="yellow",
                )
                self._pipeline = None
                self._pipeline_waiting_input = False
                self._pipeline_restored_status = None
                return
            self._pipeline_restored_status = restored.status
            self._set_runtime_mode(RunMode.PIPELINE)
            try:
                step_id = self._pipeline.state_machine.current_step.step_id
            except (AttributeError, IndexError):
                step_id = "?"
            from iac_code.pipeline.display_names import display_step_name

            self.renderer.print_system_message(
                _("Resumed pipeline at step: {step}").format(step=display_step_name(step_id)),
                style="yellow",
            )

    async def _confirm_pipeline_resume(self, meta_path) -> str:
        """Prompt the user whether to resume or discard the target session's
        pipeline state. Returns ``"resume"`` or ``"discard"``."""
        import yaml as _yaml

        from iac_code.pipeline.display_names import display_step_name
        from iac_code.ui.components.select import InputOption, Select, SelectLayout, TextOption

        try:
            loaded = _yaml.safe_load(meta_path.read_text(encoding="utf-8"))
        except (FileNotFoundError, OSError, UnicodeDecodeError, _yaml.YAMLError) as exc:
            self.renderer.print_system_message(
                _("Could not read pipeline state metadata: {reason}").format(reason=str(exc) or type(exc).__name__),
                style="yellow",
            )
            return "discard"
        if loaded is None:
            loaded = {}
        if not isinstance(loaded, dict):
            self.renderer.print_system_message(
                _("Pipeline state metadata is invalid; continuing as normal chat."),
                style="yellow",
            )
            return "discard"
        meta = loaded
        current_step = display_step_name(str(meta.get("current_step", "?")))

        title = _("Found pipeline state in this session (paused at: {step}).").format(step=current_step)
        # The Select component does not render a built-in title; surface the
        # context via a system message so the user sees what they're choosing
        # between.
        self.renderer.print_system_message(title)

        options: list[TextOption | InputOption] = [
            TextOption(label=_("Resume pipeline from where it left off"), value="resume"),
            TextOption(label=_("Discard pipeline state and continue as normal chat"), value="discard"),
        ]
        select = Select(options=options, default_value="resume", layout=SelectLayout.EXPANDED, visible_count=2)
        loop = asyncio.get_running_loop()
        result = await loop.run_in_executor(None, select.run)
        return result if result in ("resume", "discard") else "discard"

    async def swap_or_announce_session(self, entry) -> None:
        """Hot-swap if same project; otherwise print the resume command."""
        if entry.cwd and same_project_path(entry.cwd, self._original_cwd):
            await self.swap_session_async(entry.session_id)
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
        from iac_code.agent.message import RECALLED_MEMORY_MARKER, TextBlock, is_recalled_memory_message
        from iac_code.pipeline.engine.cleanup import is_cleanup_prompt_message

        for msg in reversed(messages):
            if msg.role != "user":
                continue
            if is_recalled_memory_message(msg) or is_cleanup_prompt_message(msg):
                continue
            content = msg.content
            if isinstance(content, str):
                if content.strip() and RECALLED_MEMORY_MARKER not in content:
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
