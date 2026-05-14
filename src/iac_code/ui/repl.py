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
import signal
import sys
import time
from dataclasses import dataclass
from types import ModuleType

from loguru import logger
from rich.console import Console

from iac_code.agent.agent_loop import AgentLoop
from iac_code.agent.system_prompt import build_system_prompt
from iac_code.commands import create_default_registry
from iac_code.commands.registry import LocalCommand, PromptCommand
from iac_code.config import get_config_dir, get_history_path, load_credentials
from iac_code.i18n import _
from iac_code.memory.memory_manager import MemoryManager
from iac_code.providers.manager import ProviderManager
from iac_code.services.session_index import SessionIndex
from iac_code.services.session_storage import SessionStorage
from iac_code.state import AppStateStore
from iac_code.state.app_state import AppState
from iac_code.tasks.notification_queue import NotificationQueue
from iac_code.tasks.task_state import TaskManager
from iac_code.tools.base import ToolRegistry
from iac_code.ui.banner import render_welcome_banner
from iac_code.ui.core.input_history import InputHistory
from iac_code.ui.core.prompt_input import PromptInput
from iac_code.ui.keybindings.manager import KeyBinding, KeybindingManager
from iac_code.ui.renderer import Renderer
from iac_code.ui.suggestions.aggregator import SuggestionAggregator
from iac_code.ui.suggestions.command_provider import CommandProvider
from iac_code.ui.suggestions.directory_provider import DirectoryProvider
from iac_code.ui.suggestions.file_provider import FileProvider
from iac_code.ui.suggestions.shell_history_provider import ShellHistoryProvider
from iac_code.utils.background_housekeeping import start_background_housekeeping

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


class InlineREPL:
    """Inline terminal REPL integrating all subsystems."""

    def __init__(self, model: str, resume_session_id: str | bool | None = None) -> None:
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
        from iac_code.services.cloud_credentials import CloudCredentials
        from iac_code.tools.cloud.registry import register_cloud_tools

        register_cloud_tools(self.tool_registry, CloudCredentials())
        self._current_model = model
        from iac_code.config import load_active_provider_config

        self._current_provider_config = load_active_provider_config()

        # Backend: Provider + Session + Tasks + Memory
        self._credentials = self._load_credentials()
        self._provider_manager = ProviderManager(model=model, credentials=self._credentials)
        self._session_storage = SessionStorage()
        self.session_index = SessionIndex()
        self._session_id = self._resolve_session_id(resume_session_id)
        self._resume_messages = self._load_resume_messages(resume_session_id)
        self._task_manager = TaskManager()
        self._notification_queue = NotificationQueue()

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

        # === Skill system initialization ===
        from iac_code.skills.bundled import init_bundled_skills
        from iac_code.skills.discovery import discover_all_skills, skill_to_command
        from iac_code.skills.listing import build_skill_listing
        from iac_code.skills.skill_tool import SkillTool

        # 1. Initialize bundled skills (once)
        init_bundled_skills()

        # 2. Discover all skills and register to unified CommandRegistry
        cwd = os.getcwd()
        all_skills = discover_all_skills(cwd)
        for skill in all_skills:
            cmd = skill_to_command(skill)
            existing = self.command_registry.get(cmd.name)
            if existing is not None and not isinstance(existing, PromptCommand):
                logger.warning(
                    "Skill '%s' (source=%s) skipped: conflicts with built-in command",
                    cmd.name,
                    cmd.source,
                )
                continue
            self.command_registry.register(cmd)

        # 3. Register SkillTool
        self.tool_registry.register(
            SkillTool(
                command_registry=self.command_registry,
                session_id=self._session_id,
                cwd=cwd,
                provider_manager=self._provider_manager,
                tool_registry=self.tool_registry,
                system_prompt=build_system_prompt(cwd=cwd, memory_content=memory_content),
            )
        )

        # 4. Generate skill listing for system prompt
        skill_commands = self.command_registry.get_model_invocable_skills()
        self._skill_listing = build_skill_listing(skill_commands)

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
                CommandProvider(self.command_registry),
                FileProvider(cwd),
                DirectoryProvider(cwd),
                ShellHistoryProvider(),
            ]
        )

        # PromptInput
        self._prompt_input = PromptInput(
            keybinding_manager=self._keybinding_manager,
            suggestion_aggregator=self._suggestion_aggregator,
            history=self._history,
            console=self.console,
        )

        self.store.subscribe(self._on_state_change)

    # ------------------------------------------------------------------
    # Public entry-point
    # ------------------------------------------------------------------

    async def run(self, initial_prompt: str | None = None) -> None:
        """Run the REPL until the user exits.

        Args:
            initial_prompt: If provided, automatically process this as the first
                user input (e.g. from piped stdin).
        """
        # Capture session start time for duration calculation
        self._started_monotonic = time.monotonic()

        state = self.store.get_state()
        self.console.print(render_welcome_banner(state.model, state.cwd, session_id=self._session_id))
        if self._resume_messages:
            self.renderer.replay_history(self._resume_messages)
            self.console.print()  # blank line before first new user turn
        start_background_housekeeping()
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
                    if initial_prompt is not None:
                        user_input = initial_prompt
                        initial_prompt = None
                        self.console.print(f"[bold cyan]❯[/bold cyan] {user_input}")
                    else:
                        user_input = await self._prompt_input.get_input()
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
                    self._history.append(user_input)

                    if self.command_registry.is_command(user_input):
                        await self._handle_command(user_input)
                        self._clear_cancel_state()
                        continue
                    await self._handle_chat(user_input)
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

        from rich.text import Text

        self.console.print("[dim]{}[/dim]".format(_("Goodbye!")))
        self.console.print(Text(_("Resume this session with:"), style="dim"))
        self.console.print(Text(f"iac-code --resume {self._session_id}", style="dim"))

    async def run_once(self, prompt: str) -> None:
        """Process a single prompt and exit (non-interactive mode)."""
        if self.command_registry.is_command(prompt):
            await self._handle_command(prompt)
        else:
            await self._handle_chat(prompt)

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

    # ------------------------------------------------------------------
    # Dialog launchers
    # ------------------------------------------------------------------

    def _open_history_search(self) -> bool:
        from iac_code.ui.dialogs.history_search import HistorySearch

        messages = self.store.get_state().messages
        dialog = HistorySearch(
            messages=messages,
            on_select=self._insert_text,
            on_cancel=lambda: None,
            keybinding_manager=self._keybinding_manager,
        )
        dialog.run()
        return True

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
        """Insert text into the prompt input buffer (future enhancement)."""
        pass  # Will be enhanced when PromptInput supports external text insertion

    def _expand_last_turn(self) -> bool:
        """Keybinding handler: open the verbose transcript view."""
        self._prompt_input.schedule_action(self.renderer.show_transcript)
        return True

    # ------------------------------------------------------------------
    # Command handling
    # ------------------------------------------------------------------

    async def _handle_command(self, user_input: str) -> None:
        """Dispatch a slash command and print the result."""
        name, args = self.command_registry.parse(user_input)
        cmd = self.command_registry.get(name)
        if cmd is None:
            self.renderer.print_system_message(
                _("Unknown command: /{name}. Type /help for available commands.").format(name=name),
                style="red",
            )
            return

        if isinstance(cmd, PromptCommand):
            # Skill command: process via unified path
            from iac_code.skills.processor import process_prompt_command

            args_str = " ".join(args) if args else ""
            try:
                result = await process_prompt_command(cmd, args_str)
                if result.is_fork:
                    await self._handle_chat(result.prompt_content)
                else:
                    # Inline mode: inject messages and continue agent loop
                    for msg in result.new_messages:
                        self._agent_loop.context_manager.add_raw_message(msg)
                    if result.context_modifier:
                        self._agent_loop._apply_context_modifier(result.context_modifier)
                    # Stream the agent's response to the injected skill prompt
                    await self._handle_chat_continue()
            except Exception as exc:
                self.renderer.print_system_message(
                    _("Command error: {error}").format(error=exc),
                    style="red",
                )
        elif isinstance(cmd, LocalCommand):
            context = CommandContext(console=self.console, store=self.store, repl=self)
            if cmd.handler is None:
                self.renderer.print_system_message(
                    _("Command has no handler: {name}").format(name=cmd.name),
                    style="red",
                )
                return
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
                if result:
                    self.renderer.print_command_result(user_input, result)
            except ExitREPLError:
                raise
            except Exception as exc:
                self.renderer.print_system_message(
                    _("Command error: {error}").format(error=exc),
                    style="red",
                )

    # ------------------------------------------------------------------
    # Chat handling
    # ------------------------------------------------------------------

    async def _handle_chat_continue(self) -> None:
        """Continue the agent loop after injecting messages (e.g., skill prompt).

        Unlike _handle_chat, this doesn't add a new user message — the messages
        were already injected into the context.
        """
        self.store.set_state(is_busy=True)
        try:
            events = self._agent_loop.run_streaming("")
            elapsed = await self.renderer.run_streaming_output(
                events,
                permission_handler=self.renderer.prompt_permission,
            )
            if elapsed >= 1.0:
                self._agent_loop.stamp_last_turn_elapsed(elapsed)
        finally:
            self.store.set_state(is_busy=False)

    async def _handle_chat(self, user_input: str) -> None:
        """Send the user message to the agent loop and stream output."""
        self.store.set_state(is_busy=True)
        self.renderer.record_user_turn(user_input)
        try:
            events = self._agent_loop.run_streaming(user_input)
            elapsed = await self.renderer.run_streaming_output(
                events,
                permission_handler=self.renderer.prompt_permission,
            )
            if elapsed >= 1.0:
                self._agent_loop.stamp_last_turn_elapsed(elapsed)
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
        self._credentials = self._load_credentials()
        self._provider_manager.reconfigure(new_model, self._credentials)
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
            if cwd and cwd != self._original_cwd:
                raise ValueError(self._cross_project_message(cwd, sid))
            return sid
        elif isinstance(resume, str) and resume:
            if self._session_storage.exists(self._original_cwd, resume):
                return resume
            located = self._session_storage.find_session_anywhere(resume)
            if located is None:
                raise ValueError(_("Session not found: {session_id}").format(session_id=resume))
            cwd, _path = located
            if cwd and cwd != self._original_cwd:
                raise ValueError(self._cross_project_message(cwd, resume))
            return resume
        return str(uuid.uuid4())

    def _load_resume_messages(self, resume: str | bool | None) -> list:
        """Load and repair saved messages when resuming a session."""
        if resume is None:
            return []
        messages = self._session_storage.load(self._original_cwd, self._session_id)
        return self._session_storage.repair_interrupted(messages)

    @staticmethod
    def _cross_project_message(cwd: str, session_id: str) -> str:
        import shlex

        cmd = f"cd {shlex.quote(cwd)} && iac-code --resume {session_id}"
        return _("This session belongs to a different directory.\nTo resume, run:\n  {cmd}").format(cmd=cmd)

    @property
    def session_id(self) -> str:
        return self._session_id

    # ------------------------------------------------------------------
    # Session swap (used by /resume command)
    # ------------------------------------------------------------------

    def swap_session(self, new_session_id: str) -> None:
        """Replace the active session in-place (same project only)."""
        new_messages = self._session_storage.load(self._original_cwd, new_session_id)
        new_messages = self._session_storage.repair_interrupted(new_messages)
        self._agent_loop.replace_session(new_session_id, new_messages or None)
        self._session_id = new_session_id

        # Clear screen + scrollback, redraw banner, replay history.
        self.console.file.write("\033[H\033[2J\033[3J")
        self.console.file.flush()
        state = self.store.get_state()
        self.console.print(render_welcome_banner(state.model, state.cwd, session_id=new_session_id))
        if new_messages:
            self.renderer.replay_history(new_messages)
            self.console.print()

    async def swap_or_announce_session(self, entry) -> None:
        """Hot-swap if same project; otherwise print the resume command."""
        if entry.cwd and entry.cwd == self._original_cwd:
            self.swap_session(entry.session_id)
            return
        await self._announce_cross_project(entry)

    async def _announce_cross_project(self, entry) -> None:
        import shlex

        cmd = f"cd {shlex.quote(entry.cwd)} && iac-code --resume {entry.session_id}"
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
