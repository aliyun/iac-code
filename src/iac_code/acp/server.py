from __future__ import annotations

import asyncio
import contextlib
import logging
import time
import uuid
from typing import Any

import acp

from iac_code import __version__
from iac_code.acp.metrics import ACPMetrics
from iac_code.acp.session import ACPSession, Message, _is_auth_error
from iac_code.acp.slash_registry import ACP_SUPPORTED_COMMANDS
from iac_code.acp.tools import replace_bash_with_acp_terminal
from iac_code.acp.types import ACPContentBlock, MCPServer
from iac_code.acp.version import negotiate_version
from iac_code.commands import LocalCommand, create_default_registry
from iac_code.config import DEFAULT_MODEL, get_active_provider_key, load_saved_model
from iac_code.i18n import _
from iac_code.services.agent_factory import AgentFactoryOptions, create_agent_runtime
from iac_code.services.session_index import SessionEntry, SessionIndex
from iac_code.services.session_resolver import ResolutionStatus, resolve_session_argument
from iac_code.services.session_storage import SessionStorage
from iac_code.utils.project_paths import format_resume_command, same_project_path

SESSION_IDLE_TIMEOUT = 3600  # 1 hour
CLEANUP_INTERVAL = 300  # 5 minutes

logger = logging.getLogger(__name__)


def _runtime_command_memory_manager(runtime: object) -> object | None:
    return getattr(runtime, "legacy_memory_manager", None) or getattr(runtime, "memory_manager", None)


class ACPServer:
    def __init__(self) -> None:
        self.conn: acp.Client | None = None
        self.client_capabilities: acp.schema.ClientCapabilities | None = None
        self.sessions: dict[str, ACPSession] = {}
        self._cleanup_task: asyncio.Task | None = None
        self.metrics: ACPMetrics = ACPMetrics()

    def on_connect(self, conn: acp.Client) -> None:
        self.conn = conn

    async def authenticate(self, method_id: str, **kwargs: Any) -> acp.schema.AuthenticateResponse | None:
        """Handle ACP ``authenticate`` requests.

        iac-code performs authentication out-of-band (env vars / credentials
        file), so this is a no-op acknowledgement that satisfies the
        :class:`acp.Agent` protocol contract.
        """
        return None

    async def ext_method(self, method: str, params: dict[str, Any]) -> dict[str, Any]:
        """Handle ACP extension method calls.

        iac-code does not implement any custom extension methods; this stub
        exists solely to satisfy the :class:`acp.Agent` protocol contract.
        """
        raise acp.RequestError.method_not_found(method)

    async def ext_notification(self, method: str, params: dict[str, Any]) -> None:
        """Handle ACP extension notifications.

        iac-code does not act on any extension notifications; the body is a
        no-op for protocol-conformance purposes only.
        """
        return None

    async def set_session_mode(
        self,
        mode_id: str,
        session_id: str,
        **kwargs: Any,
    ) -> acp.schema.SetSessionModeResponse | None:
        """Handle ACP ``session/set_mode`` requests.

        iac-code does not currently expose user-selectable session modes;
        the request is acknowledged but otherwise has no effect.  The stub
        exists to satisfy the :class:`acp.Agent` protocol contract.
        """
        return None

    async def set_session_model(
        self,
        model_id: str,
        session_id: str,
        **kwargs: Any,
    ) -> acp.schema.SetSessionModelResponse | None:
        """Handle ACP ``session/set_model`` requests.

        Models are configured via :func:`load_saved_model` / the auth flow,
        so dynamic per-session model switching is a no-op for now.  The
        stub exists to satisfy the :class:`acp.Agent` protocol contract.
        """
        return None

    def _get_session(self, session_id: str) -> ACPSession:
        session = self.sessions.get(session_id)
        if session is None:
            raise acp.RequestError.invalid_params({"session_id": "Session not found"})
        return session

    async def initialize(
        self,
        protocol_version: int,
        client_capabilities: acp.schema.ClientCapabilities | None = None,
        client_info: acp.schema.Implementation | None = None,
        **kwargs: Any,
    ) -> acp.InitializeResponse:
        negotiated = negotiate_version(protocol_version)
        self.client_capabilities = client_capabilities
        await self._start_cleanup_loop()
        logger.info(
            "ACP server initialized, protocol_version=%d, client=%s",
            negotiated.protocol_version,
            client_info.name if client_info else "unknown",
        )
        return acp.InitializeResponse(
            protocol_version=negotiated.protocol_version,
            agent_capabilities=acp.schema.AgentCapabilities(
                load_session=True,
                prompt_capabilities=acp.schema.PromptCapabilities(
                    embedded_context=True,
                    image=False,
                    audio=False,
                ),
                mcp_capabilities=acp.schema.McpCapabilities(http=False, sse=False),
                session_capabilities=acp.schema.SessionCapabilities(
                    close=acp.schema.SessionCloseCapabilities(),
                    list=acp.schema.SessionListCapabilities(),
                ),
            ),
            auth_methods=_build_auth_methods(),
            agent_info=acp.schema.Implementation(name="iac-code", version=__version__),
        )

    async def new_session(
        self,
        cwd: str,
        mcp_servers: list[MCPServer] | None = None,
        **kwargs: Any,
    ) -> acp.NewSessionResponse:
        if self.conn is None:
            raise acp.RequestError.internal_error({"error": "ACP client not connected"})

        # Convert MCP server configs from ACP protocol types to internal dicts
        mcp_configs = _convert_mcp_servers(mcp_servers)

        model = load_saved_model() or DEFAULT_MODEL
        runtime = self._create_runtime_with_auth_check(model=model, cwd=cwd)
        replace_bash_with_acp_terminal(
            runtime.tool_registry,
            self.client_capabilities,
            self.conn,
            runtime.session_id,
        )
        session = ACPSession(
            runtime.session_id,
            runtime.agent_loop,
            self.conn,
            mcp_configs=mcp_configs,
            metrics=self.metrics,
            memory_manager=_runtime_command_memory_manager(runtime),
        )
        self.sessions[session.id] = session
        self.metrics.record_session_created()
        logger.info("Session created, session_id=%s, model=%s", session.id, model)

        # Build model state for the response
        model_state = self._build_model_state(model)

        response = acp.NewSessionResponse(
            session_id=session.id,
            models=model_state,
        )

        # Push available commands to the client
        await self._push_available_commands(session.id)

        return response

    async def prompt(
        self,
        prompt: list[ACPContentBlock],
        session_id: str,
        message_id: str | None = None,
        **kwargs: Any,
    ) -> acp.PromptResponse:
        session = self._get_session(session_id)
        session.touch()
        return await session.prompt(prompt)

    async def close_session(self, session_id: str, **kwargs: Any) -> acp.schema.CloseSessionResponse:
        """Close a session, releasing all associated resources.

        Idempotent: closing an already-removed session returns success.
        """
        session = self.sessions.get(session_id)
        if session is None:
            # Already gone (cleaned up or previously closed) — return success.
            return acp.schema.CloseSessionResponse()

        # Cancel any running prompt, then release resources.
        await session.close()

        # Remove from active sessions.
        self.sessions.pop(session_id, None)
        self.metrics.record_session_closed()
        logger.info("Session %s closed and removed via close_session", session_id)
        return acp.schema.CloseSessionResponse()

    async def set_config_option(
        self,
        config_id: str,
        session_id: str,
        value: str | bool,
        **kwargs: Any,
    ) -> acp.schema.SetSessionConfigOptionResponse | None:
        """Handle dynamic config updates from the client.

        Stores the *config_id* / *value* pair in the session's dynamic config
        and returns the full list of current config options.
        """
        session = self._get_session(session_id)
        session.update_config({config_id: value})
        logger.info("Session %s config updated: %s=%r", session_id, config_id, value)
        return None

    async def cancel(self, session_id: str, **kwargs: Any) -> None:
        session = self._get_session(session_id)
        await session.cancel()

    async def list_sessions(
        self,
        cursor: str | None = None,
        cwd: str | None = None,
        **kwargs: Any,
    ) -> acp.schema.ListSessionsResponse:
        index = SessionIndex()
        entries = index.list_for_cwd(cwd) if cwd else index.list_all_projects()
        return acp.schema.ListSessionsResponse(
            sessions=[
                acp.schema.SessionInfo(
                    session_id=entry.session_id,
                    cwd=entry.cwd or cwd or "",
                    title=entry.title,
                )
                for entry in entries
            ],
            next_cursor=None,
        )

    async def load_session(
        self,
        cwd: str,
        session_id: str,
        mcp_servers: list[MCPServer] | None = None,
        **kwargs: Any,
    ) -> acp.LoadSessionResponse | None:
        """Load a persisted session and replay its history to the client.

        If the session is already active in memory it is returned directly.
        Otherwise the history is read from :class:`SessionStorage`, a fresh
        agent runtime is created, and history events are replayed as ACP
        ``session_update`` notifications so the client can rebuild its UI.
        """
        if self.conn is None:
            raise acp.RequestError.internal_error({"error": "ACP client not connected"})

        # 1. Already active in memory — return immediately
        if session_id in self.sessions:
            model = load_saved_model() or DEFAULT_MODEL
            return acp.LoadSessionResponse(models=self._build_model_state(model))

        # 2. Try to load from persistent storage
        storage = SessionStorage()
        if not storage.exists(cwd, session_id):
            raise acp.RequestError.invalid_params({"session_id": "Session not found"})

        history = storage.load(cwd, session_id)
        history = SessionStorage.repair_interrupted(history)

        mcp_configs = _convert_mcp_servers(mcp_servers)

        # 3. Rebuild agent runtime with restored history
        model = load_saved_model() or DEFAULT_MODEL
        runtime = self._create_runtime_with_auth_check(model=model, session_id=session_id, cwd=cwd)
        replace_bash_with_acp_terminal(
            runtime.tool_registry,
            self.client_capabilities,
            self.conn,
            runtime.session_id,
        )

        if history:
            runtime.agent_loop.context_manager.load_messages(history)

        # 4. Register session
        session = ACPSession(
            session_id,
            runtime.agent_loop,
            self.conn,
            mcp_configs=mcp_configs,
            metrics=self.metrics,
            memory_manager=_runtime_command_memory_manager(runtime),
        )
        self.sessions[session_id] = session
        self.metrics.record_session_created()
        logger.info("Session loaded, session_id=%s, history_messages=%d", session_id, len(history))

        # 5. Replay history events asynchronously so the client can rebuild UI
        if history:
            session._replay_task = asyncio.create_task(self._replay_session_history(session, history))

        # 6. Push available commands
        await self._push_available_commands(session_id)

        return acp.LoadSessionResponse(models=self._build_model_state(model))

    async def fork_session(
        self,
        cwd: str,
        session_id: str,
        mcp_servers: list[MCPServer] | None = None,
        **kwargs: Any,
    ) -> acp.schema.ForkSessionResponse:
        """Create a new session forked from an existing one.

        The full history of the source session is copied into a brand-new
        session with a fresh ``session_id``.  The client can then continue
        the conversation on the fork without affecting the original.
        """
        if self.conn is None:
            raise acp.RequestError.internal_error({"error": "ACP client not connected"})

        # 1. Collect history from the source session
        history: list[Message] = []
        if session_id in self.sessions:
            source = self.sessions[session_id]
            ctx = getattr(source.agent_loop, "context_manager", None)
            if ctx is not None:
                history = list(ctx.get_messages())
        else:
            storage = SessionStorage()
            if not storage.exists(cwd, session_id):
                raise acp.RequestError.invalid_params({"session_id": "Source session not found"})
            history = storage.load(cwd, session_id)
            history = SessionStorage.repair_interrupted(history)

        mcp_configs = _convert_mcp_servers(mcp_servers)

        # 2. Create a new runtime for the fork
        new_session_id = str(uuid.uuid4())
        model = load_saved_model() or DEFAULT_MODEL
        runtime = self._create_runtime_with_auth_check(model=model, session_id=new_session_id, cwd=cwd)
        replace_bash_with_acp_terminal(
            runtime.tool_registry,
            self.client_capabilities,
            self.conn,
            runtime.session_id,
        )

        # 3. Inject history into the new runtime
        if history:
            runtime.agent_loop.context_manager.load_messages(history)

        # 4. Register the forked session
        session = ACPSession(
            new_session_id,
            runtime.agent_loop,
            self.conn,
            mcp_configs=mcp_configs,
            metrics=self.metrics,
            memory_manager=_runtime_command_memory_manager(runtime),
        )
        self.sessions[new_session_id] = session
        self.metrics.record_session_created()
        logger.info("Session forked, source_session_id=%s, new_session_id=%s", session_id, new_session_id)

        # 5. Replay history so the client can show it
        if history:
            session._replay_task = asyncio.create_task(self._replay_session_history(session, history))

        await self._push_available_commands(new_session_id)

        return acp.schema.ForkSessionResponse(
            session_id=new_session_id,
            models=self._build_model_state(model),
        )

    async def resume_session(
        self,
        cwd: str,
        session_id: str,
        mcp_servers: list[MCPServer] | None = None,
        **kwargs: Any,
    ) -> acp.schema.ResumeSessionResponse:
        # 1. If session is still active in memory by exact id, enforce project ownership before returning.
        active_session = self.sessions.get(session_id)
        if active_session is not None:
            error = _active_session_project_error(cwd, session_id, session_id, active_session)
            if error is not None:
                raise error
            await self._push_available_commands(session_id)
            return acp.schema.ResumeSessionResponse()

        if self.conn is None:
            raise acp.RequestError.internal_error({"error": "ACP client not connected"})

        resolution = resolve_session_argument(SessionIndex(), cwd, session_id)
        if resolution.status == ResolutionStatus.NOT_FOUND:
            raise _invalid_params(_("Session not found"), {"session_id": session_id})
        if resolution.status == ResolutionStatus.AMBIGUOUS_NAME:
            candidate_ids = [entry.session_id for entry in resolution.candidates]
            message = _("Session name is ambiguous. Candidates: {candidates}").format(
                candidates=", ".join(candidate_ids)
            )
            raise _invalid_params(
                message,
                {
                    "session_id": session_id,
                    "candidates": [_resume_candidate_data(entry) for entry in resolution.candidates],
                },
            )

        entry = resolution.entry
        if entry is None:  # pragma: no cover - defensive guard for inconsistent resolver output
            raise _invalid_params(_("Session not found"), {"session_id": session_id})

        resolved_session_id = entry.session_id
        if entry.cwd and not same_project_path(entry.cwd, cwd):
            hint = _resume_command(entry.cwd, resolved_session_id)
            message = _("Session belongs to another project. Run: {hint}").format(hint=hint)
            raise _invalid_params(
                message,
                {
                    "session_id": session_id,
                    "resolved_session_id": resolved_session_id,
                    "cwd": entry.cwd,
                    "hint": hint,
                },
            )

        active_session = self.sessions.get(resolved_session_id)
        if active_session is not None:
            error = _active_session_project_error(cwd, session_id, resolved_session_id, active_session)
            if error is not None:
                raise error
            await self._push_available_commands(resolved_session_id)
            return acp.schema.ResumeSessionResponse()

        # 2. Try to load persisted history from SessionStorage.
        storage = SessionStorage()
        if not storage.exists(cwd, resolved_session_id):
            raise _invalid_params(_("Session not found"), {"session_id": session_id})

        history = storage.load(cwd, resolved_session_id)
        history = SessionStorage.repair_interrupted(history)

        # Convert MCP server configs from ACP protocol types to internal dicts
        mcp_configs = _convert_mcp_servers(mcp_servers)

        # 3. Rebuild agent runtime with restored history
        model = load_saved_model() or DEFAULT_MODEL
        runtime = self._create_runtime_with_auth_check(model=model, session_id=resolved_session_id, cwd=cwd)
        replace_bash_with_acp_terminal(
            runtime.tool_registry,
            self.client_capabilities,
            self.conn,
            runtime.session_id,
        )

        # Inject restored history into the agent loop
        if history:
            runtime.agent_loop.context_manager.load_messages(history)

        # 4. Register the resumed session
        session = ACPSession(
            resolved_session_id,
            runtime.agent_loop,
            self.conn,
            mcp_configs=mcp_configs,
            metrics=self.metrics,
            memory_manager=_runtime_command_memory_manager(runtime),
        )
        self.sessions[resolved_session_id] = session
        self.metrics.record_session_created()
        await self._push_available_commands(resolved_session_id)

        return acp.schema.ResumeSessionResponse()

    # ------------------------------------------------------------------
    # Runtime creation helper
    # ------------------------------------------------------------------

    @staticmethod
    def _create_runtime_with_auth_check(
        *,
        model: str,
        cwd: str,
        session_id: str | None = None,
    ):
        """Create an agent runtime, converting auth errors to ACP RequestError."""
        try:
            return create_agent_runtime(AgentFactoryOptions(model=model, session_id=session_id, cwd=cwd))
        except Exception as exc:
            if _is_auth_error(exc):
                logger.warning("Authentication error during runtime creation: %s", exc)
                raise acp.RequestError.internal_error(
                    {
                        "error": "Authentication required. Please configure your API credentials.",
                        "code": "auth_required",
                    }
                ) from exc
            raise

    # ------------------------------------------------------------------
    # Model state & available commands helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _build_model_state(model: str) -> acp.schema.SessionModelState:
        """Build SessionModelState from the active model identifier."""
        provider_key = get_active_provider_key() or "dashscope"
        return acp.schema.SessionModelState(
            available_models=[
                acp.schema.ModelInfo(
                    model_id=model,
                    name=model,
                    description=f"Active model via {provider_key}",
                ),
            ],
            current_model_id=model,
        )

    async def _replay_session_history(
        self,
        session: ACPSession,
        history: list[Message],
    ) -> None:
        """Replay history events for a loaded/forked session.

        Errors are logged but not propagated so that the session remains
        usable even if a single replay event fails.
        """
        try:
            await session.replay_history(history)
        except Exception:
            logger.exception("Failed to replay history for session %s", session.id)

    async def _push_available_commands(self, session_id: str) -> None:
        """Push the list of available slash commands to the client via session_update."""
        if self.conn is None:
            return
        registry = create_default_registry()
        commands = []
        for cmd in registry.get_all():
            if cmd.name not in ACP_SUPPORTED_COMMANDS:
                continue
            # Build input hint: prefer arg_hint, fall back to arg_names
            hint = None
            if isinstance(cmd, LocalCommand):
                if cmd.arg_hint:
                    hint = cmd.arg_hint
                elif cmd.arg_names:
                    hint = " ".join(f"[{name}]" for name in cmd.arg_names)

            input_spec = (
                acp.schema.AvailableCommandInput(root=acp.schema.UnstructuredCommandInput(hint=hint)) if hint else None
            )
            commands.append(
                acp.schema.AvailableCommand(
                    name=cmd.name,
                    description=cmd.description,
                    input=input_spec,
                )
            )
        if not commands:
            return
        await self.conn.session_update(
            session_id=session_id,
            update=acp.schema.AvailableCommandsUpdate(
                session_update="available_commands_update",
                available_commands=commands,
            ),
        )

    # ------------------------------------------------------------------
    # Cleanup loop
    # ------------------------------------------------------------------

    async def _start_cleanup_loop(self) -> None:
        """Start background cleanup loop for idle sessions."""
        if self._cleanup_task is None:
            self._cleanup_task = asyncio.create_task(self._cleanup_idle_sessions())

    async def shutdown(self) -> None:
        """Gracefully shut down the server, stopping background tasks."""
        await self.shutdown_all_sessions()

    async def shutdown_all_sessions(self) -> None:
        """Close all active sessions and stop the cleanup loop."""
        await self._stop_cleanup_loop()
        for session_id in list(self.sessions):
            session = self.sessions.pop(session_id)
            await session.close()
            self.metrics.record_session_closed()
        logger.info("All sessions shut down. Metrics: %s", self.metrics.snapshot())

    async def _stop_cleanup_loop(self) -> None:
        """Stop background cleanup loop."""
        if self._cleanup_task is not None:
            self._cleanup_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._cleanup_task
            self._cleanup_task = None

    async def _cleanup_idle_sessions(self) -> None:
        """Periodically remove idle sessions.

        A session is only considered for cleanup when:
        * it has been idle longer than ``SESSION_IDLE_TIMEOUT``, and
        * it has no in-flight prompt task (``_current_task is None`` or done),
          and
        * it has not already been closed.

        This prevents the cleanup loop from terminating an actively running
        prompt mid-execution.
        """
        while True:
            await asyncio.sleep(CLEANUP_INTERVAL)
            try:
                now = time.monotonic()
                expired: list[str] = []
                for sid, session in self.sessions.items():
                    if session.is_closed:
                        expired.append(sid)
                        continue
                    if now - session.last_active <= SESSION_IDLE_TIMEOUT:
                        continue
                    task = session._current_task
                    if task is not None and not task.done():
                        # Active prompt in progress — leave it alone for now.
                        continue
                    expired.append(sid)
                for sid in expired:
                    session = self.sessions.pop(sid, None)
                    if session is not None and not session.is_closed:
                        await session.close()
                        self.metrics.record_session_closed()
                    logger.info("Cleaned up idle session %s", sid)
            except Exception:
                logger.exception("Error during session cleanup")


# ---------------------------------------------------------------------------
# MCP server config helper
# ---------------------------------------------------------------------------


def _convert_mcp_servers(mcp_servers: list[MCPServer] | None) -> list[dict[str, Any]]:
    """Convert ACP MCP server configs to internal dicts, filtering unsupported types.

    Tolerant by design: a malformed or unsupported entry from the client must
    not abort ``new_session``. Conversion failures are logged and the offending
    entry is skipped so the session can still start with whatever configs are
    valid.
    """
    if not mcp_servers:
        return []
    from iac_code.acp.mcp import convert_mcp_configs

    try:
        configs = convert_mcp_configs(mcp_servers)
    except Exception:
        logger.exception(
            "Failed to convert MCP server configs (%d entries); proceeding with no MCP servers",
            len(mcp_servers),
        )
        return []
    if configs:
        logger.info("Received %d MCP server config(s): %s", len(configs), [c["name"] for c in configs])
    return configs


def _invalid_params(message: str, data: dict[str, Any] | None = None) -> acp.RequestError:
    """Create an ACP invalid-params error with a useful message."""
    return acp.RequestError(-32602, message, data)


def _resume_command(cwd: str, session_id: str) -> str:
    return format_resume_command(cwd, session_id)


def _active_session_cwd(session: ACPSession) -> str | None:
    cwd = getattr(session.agent_loop, "_cwd", None)
    return cwd if isinstance(cwd, str) and cwd else None


def _active_session_project_error(
    cwd: str, session_id: str, resolved_session_id: str, session: ACPSession
) -> acp.RequestError | None:
    active_cwd = _active_session_cwd(session)
    if not active_cwd or same_project_path(active_cwd, cwd):
        return None
    hint = _resume_command(active_cwd, resolved_session_id)
    message = _("Session belongs to another project. Run: {hint}").format(hint=hint)
    return _invalid_params(
        message,
        {
            "session_id": session_id,
            "resolved_session_id": resolved_session_id,
            "cwd": active_cwd,
            "hint": hint,
        },
    )


def _resume_candidate_data(entry: SessionEntry) -> dict[str, str | None]:
    return {
        "session_id": entry.session_id,
        "name": entry.name,
        "cwd": entry.cwd,
        "command": _resume_command(entry.cwd, entry.session_id),
    }


# ---------------------------------------------------------------------------
# Auth methods declaration
# ---------------------------------------------------------------------------

# Supported provider environment variables for credentials.
_PROVIDER_ENV_VARS: list[tuple[str, str, str]] = [
    ("DASHSCOPE_API_KEY", "DashScope / Qwen API Key", "https://dashscope.console.aliyun.com/"),
    ("OPENAI_API_KEY", "OpenAI API Key", "https://platform.openai.com/api-keys"),
    ("ANTHROPIC_API_KEY", "Anthropic API Key", "https://console.anthropic.com/"),
    ("DEEPSEEK_API_KEY", "DeepSeek API Key", "https://platform.deepseek.com/"),
]


def _build_auth_methods() -> list[
    acp.schema.EnvVarAuthMethod | acp.schema.TerminalAuthMethod | acp.schema.AuthMethodAgent
]:
    """Build the list of supported authentication methods for ACP initialize.

    iac-code supports multiple LLM providers. Credentials can be provided via
    environment variables or via the credentials config file
    (~/.iac-code/.credentials.yml).  The env-var method is the standard ACP
    mechanism that clients can present to users.
    """
    return [
        acp.schema.EnvVarAuthMethod(
            type="env_var",
            id=f"env_{env_name.lower()}",
            name=label,
            description=f"Set {env_name} to authenticate with this provider.",
            link=link,
            vars=[
                acp.schema.AuthEnvVar(
                    name=env_name,
                    label=label,
                    secret=True,
                    optional=False,
                ),
            ],
        )
        for env_name, label, link in _PROVIDER_ENV_VARS
    ]
