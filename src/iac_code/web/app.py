"""Starlette application factory for the local Web workbench."""

from __future__ import annotations

import asyncio
import base64
import binascii
import json
import logging
import os
import subprocess
import threading
import time
import uuid
from collections.abc import Callable
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from functools import partial
from html import escape
from pathlib import Path
from typing import TYPE_CHECKING, Any, Protocol, cast

from iac_code import __version__ as _iac_version
from iac_code.i18n import _, load_webui_catalog, resolve_ui_language
from iac_code.services.update_checker import (
    get_pending_update,
    run_update_command,
    suppress_version,
)


def _iso_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _pipeline_snapshot_switched_to_normal(snapshot: dict[str, Any] | None) -> bool:
    """True when a pipeline public snapshot records a completed handoff to normal
    chat (``normalHandoff`` with ``switch_to_normal`` / ``normal``). Mirrors the
    executor's own gate (a2a/executor.py) so persist-time tagging of post-handoff
    prompts matches the runtime's routing decision."""
    if not isinstance(snapshot, dict):
        return False
    handoff = snapshot.get("normalHandoff")
    if not isinstance(handoff, dict):
        return False
    return handoff.get("action") == "switch_to_normal" and handoff.get("targetMode") == "normal"


if TYPE_CHECKING:
    from iac_code.web.pipeline_actions import PipelineActionRunner
    from iac_code.web.runtime import WebTurnRequest
    from iac_code.web.session_manager import WebSession, WebSessionManager

STATIC_DIR = Path(__file__).with_name("static")
logger = logging.getLogger(__name__)
DEFAULT_SESSION_LIST_LIMIT = 50
MAX_SESSION_LIST_LIMIT = 200
DEFAULT_PROJECT_LIST_LIMIT = None
DEFAULT_PROJECT_SESSION_LIMIT = 5
MAX_PROJECT_LIST_LIMIT = 1000
MAX_PROJECT_SESSION_LIMIT = 200
WEB_SHUTDOWN_TASK_TIMEOUT_SECONDS = 5.0


class _SuppressAllRedactionMiddleware:
    """Keep the loopback Web request context on the existing local policy.

    User-visible Web data no longer calls generic redactors.  The suppression
    context remains for unchanged consumers such as permission audit; strict
    server-log and telemetry sanitizers explicitly ignore it.
    """

    def __init__(self, app: Any) -> None:
        self.app = app

    async def __call__(self, scope: Any, receive: Any, send: Any) -> None:
        from iac_code.utils.public_errors import suppress_all_redaction

        with suppress_all_redaction():
            await self.app(scope, receive, send)


class WebRuntimeProtocol(Protocol):
    async def start_turn(self, request: WebTurnRequest) -> dict[str, Any]: ...


class WebShellRunnerProtocol(Protocol):
    async def run(self, session: WebSession, command: str) -> dict[str, Any]: ...


class TurnReservationCanceledError(Exception):
    """Raised when a pre-start turn reservation was canceled by stop/interrupt."""


WebRuntimeFactory = Callable[["WebSession"], WebRuntimeProtocol]
WebShellRunnerFactory = Callable[[], WebShellRunnerProtocol]
WebPipelineActionRunnerFactory = Callable[[], "PipelineActionRunner"]


def create_app(
    *,
    session_manager: WebSessionManager | None = None,
    runtime_factory: WebRuntimeFactory | None = None,
    shell_runner_factory: WebShellRunnerFactory | None = None,
    pipeline_action_runner_factory: WebPipelineActionRunnerFactory | None = None,
    expose_local_paths: bool = False,
) -> Any:
    """Create the loopback-only Web workbench without generic user-data redaction.

    ``expose_local_paths`` is retained as a compatibility-only argument; all
    local Web sessions now use the same no-redaction policy.
    """
    del expose_local_paths
    try:
        from starlette.applications import Starlette
        from starlette.concurrency import run_in_threadpool
        from starlette.middleware import Middleware
        from starlette.responses import HTMLResponse, JSONResponse, Response, StreamingResponse
        from starlette.routing import Mount, Route
        from starlette.staticfiles import StaticFiles
    except ImportError as exc:  # pragma: no cover - exercised when optional extra is absent
        raise RuntimeError("Web dependencies are missing. Install with: pip install 'iac-code[http]'") from exc

    from iac_code.commands import create_default_registry
    from iac_code.commands.registry import CommandRegistry, PromptCommand
    from iac_code.config import (
        get_provider_config,
        load_credentials,
    )
    from iac_code.services.capabilities.multimodal import is_model_multimodal
    from iac_code.skills.bundled import init_bundled_skills
    from iac_code.skills.discovery import discover_all_skills
    from iac_code.skills.management import build_skill_management_state
    from iac_code.skills.settings import load_disabled_skills
    from iac_code.ui.suggestions.command_provider import CommandProvider
    from iac_code.ui.suggestions.directory_provider import DirectoryProvider
    from iac_code.ui.suggestions.file_provider import FileProvider
    from iac_code.ui.suggestions.shell_history_provider import ShellHistoryProvider
    from iac_code.ui.suggestions.skill_provider import SkillProvider
    from iac_code.ui.suggestions.types import CompletionToken, SuggestionItem
    from iac_code.web import mcp_settings
    from iac_code.web.cleanup import cleanup_blocks_normal_chat, session_cleanup_summary
    from iac_code.web.commands import WebCommandDispatcher, command_metadata
    from iac_code.web.events import encode_sse, make_resync_event, normalize_event_payload, observe_published_events
    from iac_code.web.mcp_settings import MCPWebError
    from iac_code.web.memory import (
        delete_legacy_memory,
        legacy_memory_summaries,
        memory_payload,
        memory_projects,
        resolve_project_cwd,
        save_auto_memory,
        save_project_instruction,
        save_user_instruction,
    )
    from iac_code.web.outputs import (
        OutputFileMissing,
        OutputPathForbidden,
        outputs_payload,
        read_output_file,
    )
    from iac_code.web.permissions import (
        PERMISSION_CHOICES,
        elicitation_result_from_body,
        offered_permission_choice_ids,
    )
    from iac_code.web.pipeline import (
        PipelineCandidateSelectionRequestError,
        PipelineStateNotFoundError,
        PipelineStateRequestError,
        parse_candidate_selection_body,
        pipeline_state_from_query,
    )
    from iac_code.web.pipeline_actions import create_pipeline_action_runner, load_pipeline_snapshot
    from iac_code.web.pipeline_prerequisites import (
        inspect_review_step_prerequisite,
        install_in_progress,
        stream_install_review_step_prerequisite,
    )
    from iac_code.web.runtime import (
        WebModelSelection,
        WebSessionRuntime,
        WebTurnRequest,
        close_agent_runtime,
        create_session_agent_runtime_in_thread,
        flush_web_telemetry,
        model_selection_for_session,
        prime_session_context_overhead,
        runtime_mcp_status,
    )
    from iac_code.web.session_manager import (
        QueuedInputActionError,
        WebMode,
        WebSessionManager,
        compute_replay_sequence,
    )
    from iac_code.web.settings import (
        aliyun_cloud_summary,
        clear_provider_config,
        get_appearance_theme,
        get_session_defaults,
        get_ui_language,
        is_foreign_normal_visible,
        is_foreign_pipeline_visible,
        login_aliyun_oauth,
        providers_payload,
        save_active_provider,
        save_aliyun_cloud,
        save_appearance_theme,
        save_foreign_sessions_visibility,
        save_provider_config,
        save_selling_review_step,
        save_session_defaults,
        save_ui_language,
        selling_review_step_settings,
        set_active_provider,
        ui_language_payload,
    )
    from iac_code.web.shell import WebShellEscapeRunner
    from iac_code.web.skills import save_disabled_payload, skills_payload

    manager: WebSessionManager = session_manager or WebSessionManager()
    from iac_code.web.diagram_optimizer import DiagramOptimizationCoordinator

    diagram_optimization_coordinator = DiagramOptimizationCoordinator()
    make_runtime: WebRuntimeFactory = runtime_factory or (lambda session: WebSessionRuntime(session, manager=manager))
    make_shell_runner: WebShellRunnerFactory = shell_runner_factory or (lambda: WebShellEscapeRunner(manager))
    make_pipeline_action_runner: WebPipelineActionRunnerFactory = (
        pipeline_action_runner_factory or create_pipeline_action_runner
    )
    command_dispatcher = WebCommandDispatcher(manager)
    shell_runner = make_shell_runner()
    pipeline_action_runner = make_pipeline_action_runner()
    init_bundled_skills()

    # Set once app-wide shutdown begins so follow-up work (e.g. post-stop queue
    # draining) can bail out instead of resurrecting turns during teardown.
    # Single-key dict avoids threading ``nonlocal`` through nested closures.
    shutdown_state = {"initiated": False}

    def request_task_cancellation(
        task: asyncio.Future[Any],
        cancel: Callable[[], object],
        *,
        running_loop: asyncio.AbstractEventLoop,
    ) -> tuple[asyncio.Future[Any], bool] | None:
        if task.done():
            return None
        owner_loop = task.get_loop()

        def cancel_if_pending() -> None:
            if not task.done():
                cancel()

        async def cancel_and_wait() -> None:
            cancel_if_pending()
            try:
                await task
            except BaseException:
                pass

        try:
            if owner_loop.is_closed():
                cancel_if_pending()
                return None
            if owner_loop is running_loop:
                cancel_if_pending()
                return task, False
            if owner_loop.is_running():
                cancellation = cancel_and_wait()
                try:
                    foreign_waiter = asyncio.run_coroutine_threadsafe(cancellation, owner_loop)
                except RuntimeError:
                    cancellation.close()
                    if not owner_loop.is_closed():
                        raise
                    return None
                return asyncio.wrap_future(foreign_waiter, loop=running_loop), True
            try:
                owner_loop.call_soon_threadsafe(cancel_if_pending)
            except RuntimeError:
                if not owner_loop.is_closed():
                    raise
                cancel_if_pending()
        except RuntimeError:
            if not owner_loop.is_closed():
                raise
        return None

    async def shutdown_session_work() -> None:
        shutdown_state["initiated"] = True
        sessions = manager.loaded_sessions()
        running_loop = asyncio.get_running_loop()
        cleanup_error: BaseException | None = None
        cleanup_traceback = None
        active_tasks: dict[
            asyncio.Future[Any],
            tuple[asyncio.Future[Any], Callable[[], object], bool],
        ] = {}
        consumed_waiters: set[asyncio.Future[Any]] = set()

        def record_cleanup_error(error: BaseException) -> None:
            nonlocal cleanup_error, cleanup_traceback
            if cleanup_error is None:
                cleanup_error = error
                cleanup_traceback = error.__traceback__
            else:
                logger.exception("Additional web session cleanup operation failed")

        async def release_turn_admission_lock(session: WebSession) -> None:
            lock = session.turn_admission_lock
            if not lock.locked():
                return
            owner_loop = lock.owner_loop or getattr(lock, "_loop", None)
            if owner_loop is None or owner_loop is running_loop or not owner_loop.is_running():
                if owner_loop is not None and owner_loop.is_closed():
                    session.turn_admission_lock = type(lock)()
                else:
                    lock.release()
                return

            async def release_on_owner_loop() -> None:
                if lock.locked():
                    lock.release()

            release_operation = release_on_owner_loop()
            try:
                foreign_release = asyncio.run_coroutine_threadsafe(release_operation, owner_loop)
            except RuntimeError:
                release_operation.close()
                if owner_loop.is_closed():
                    session.turn_admission_lock = type(lock)()
                    return
                raise
            await asyncio.wait_for(
                asyncio.wrap_future(foreign_release, loop=running_loop),
                timeout=WEB_SHUTDOWN_TASK_TIMEOUT_SECONDS,
            )

        def track_task(task: asyncio.Future[Any], cancel: Callable[[], object]) -> None:
            cancellation = request_task_cancellation(
                task,
                cancel,
                running_loop=running_loop,
            )
            if cancellation is not None:
                cancellation_waiter, reports_cleanup_error = cancellation
                active_tasks[cancellation_waiter] = task, cancel, reports_cleanup_error

        async def consume_waiters(
            waiters: set[asyncio.Future[Any]],
            waiter_tasks: dict[
                asyncio.Future[Any],
                tuple[asyncio.Future[Any], Callable[[], object], bool],
            ],
        ) -> None:
            pending_results = [waiter for waiter in waiters if waiter not in consumed_waiters]
            if not pending_results:
                return
            results = await asyncio.gather(*pending_results, return_exceptions=True)
            consumed_waiters.update(pending_results)
            for waiter, result in zip(pending_results, results, strict=True):
                reports_cleanup_error = waiter_tasks[waiter][2]
                if (
                    reports_cleanup_error
                    and isinstance(result, BaseException)
                    and not isinstance(result, asyncio.CancelledError)
                ):
                    record_cleanup_error(result)

        try:
            for session in sessions:
                try:
                    manager.cancel_pending_requests_for_session(session)
                except BaseException as error:
                    record_cleanup_error(error)

                admission_owner = session.turn_admission_lock.owner_task
                if isinstance(admission_owner, asyncio.Future) and admission_owner is not asyncio.current_task():
                    try:
                        track_task(admission_owner, admission_owner.cancel)
                    except BaseException as error:
                        record_cleanup_error(error)

                turn_task = session.active_turn_task
                if isinstance(turn_task, asyncio.Future) and turn_task is not admission_owner:
                    cancel_turn = partial(cancel_active_turn_task, turn_task)
                    try:
                        track_task(turn_task, cancel_turn)
                    except BaseException as error:
                        record_cleanup_error(error)
                for local_task in tuple(session.active_local_tasks):
                    if local_task is admission_owner or local_task is turn_task:
                        continue
                    try:
                        track_task(local_task, local_task.cancel)
                    except BaseException as error:
                        record_cleanup_error(error)

            first_waiters = set(active_tasks)
            if first_waiters:
                completed, _pending = await asyncio.wait(
                    first_waiters,
                    timeout=WEB_SHUTDOWN_TASK_TIMEOUT_SECONDS,
                )
                if completed:
                    await consume_waiters(completed, active_tasks)
                retry_candidates = {
                    waiter
                    for waiter, (task, _cancel, _reports_cleanup_error) in active_tasks.items()
                    if not task.done()
                }
                if retry_candidates:
                    logger.warning("Timed out waiting for %d web task(s) during shutdown", len(retry_candidates))
                    retry_tasks: dict[
                        asyncio.Future[Any],
                        tuple[asyncio.Future[Any], Callable[[], object], bool],
                    ] = {}
                    for waiter in retry_candidates:
                        task, cancel, _reports_cleanup_error = active_tasks[waiter]
                        retry_cancellation = request_task_cancellation(
                            task,
                            cancel,
                            running_loop=running_loop,
                        )
                        if retry_cancellation is not None:
                            retry_waiter, reports_cleanup_error = retry_cancellation
                            retry_tasks[retry_waiter] = task, cancel, reports_cleanup_error
                    retry_waiters = set(retry_tasks)
                    if retry_waiters:
                        retry_completed, _retry_pending = await asyncio.wait(
                            retry_waiters,
                            timeout=WEB_SHUTDOWN_TASK_TIMEOUT_SECONDS,
                        )
                        if retry_completed:
                            await consume_waiters(retry_completed, retry_tasks)
                    remaining_tasks = {
                        task for task, _cancel, _reports_cleanup_error in retry_tasks.values() if not task.done()
                    }
                    if remaining_tasks:
                        logger.warning(
                            "%d web task(s) remained pending after a second shutdown cancellation",
                            len(remaining_tasks),
                        )
                    all_waiters = first_waiters.union(retry_waiters)
                    unfinished_waiters = {waiter for waiter in all_waiters if not waiter.done()}
                    for waiter in unfinished_waiters:
                        waiter.cancel()
                    await asyncio.sleep(0)
                    finished_waiters = {waiter for waiter in all_waiters if waiter.done()}
                    if finished_waiters:
                        all_tasks = dict(active_tasks)
                        all_tasks.update(retry_tasks)
                        await consume_waiters(finished_waiters, all_tasks)
        except BaseException as error:
            record_cleanup_error(error)
        finally:
            for session in sessions:
                session.active_turn_task = None
                session.active_local_tasks.clear()
                try:
                    await release_turn_admission_lock(session)
                except BaseException as error:
                    record_cleanup_error(error)

        if cleanup_error is not None:
            raise cleanup_error.with_traceback(cleanup_traceback)

    @asynccontextmanager
    async def lifespan(_app):
        startup = getattr(pipeline_action_runner, "startup", None)
        shutdown = getattr(pipeline_action_runner, "shutdown", None)
        body_failed = False
        try:
            if startup is not None:
                await startup()
            yield
        except BaseException:
            body_failed = True
            raise
        finally:
            if body_failed:
                try:
                    await shutdown_session_work()
                except BaseException:
                    logger.exception("Web session cleanup failed while the application lifespan was already failing")
                if shutdown is not None:
                    try:
                        await shutdown()
                    except BaseException:
                        logger.exception(
                            "Pipeline runner shutdown failed while the application lifespan was already failing"
                        )
            else:
                try:
                    await shutdown_session_work()
                except BaseException:
                    if shutdown is not None:
                        try:
                            await shutdown()
                        except BaseException:
                            logger.exception(
                                "Pipeline runner shutdown failed while web session cleanup was already failing"
                            )
                    raise
                else:
                    if shutdown is not None:
                        await shutdown()

    class NoCacheStaticFiles(StaticFiles):
        def file_response(self, full_path, stat_result, scope, status_code=200):
            response = super().file_response(full_path, stat_result, scope, status_code=status_code)
            response.headers["Cache-Control"] = "no-store"
            return response

    def json_error(message: str, status_code: int, *, code: str | None = None) -> JSONResponse:
        error = {"message": message}
        if code is not None:
            error["code"] = code
        return JSONResponse({"error": error}, status_code=status_code)

    def foreign_read_only_response(session: WebSession) -> JSONResponse | None:
        if not manager.is_session_read_only(session):
            return None
        return json_error(
            _("This session was created outside the web entry and is read-only."),
            409,
            code="foreign_read_only",
        )

    def public_exception_message(exc: BaseException) -> str:
        return str(exc)[:500]

    def active_model_selection(session: WebSession) -> WebModelSelection:
        return model_selection_for_session(session)

    def active_model_supports_images(
        session: WebSession,
        *,
        model_selection: WebModelSelection | None = None,
    ) -> tuple[bool, str]:
        selection = model_selection or active_model_selection(session)
        model = selection.model
        provider_key = selection.provider
        provider_config: dict[str, Any] = {}
        if provider_key and not selection.provider_config_frozen:
            try:
                provider_config = dict(get_provider_config(provider_key))
            except Exception:
                provider_config = {}
        base_url = selection.provider_base_url
        api_key = selection.provider_api_key
        if not selection.provider_config_frozen:
            base_url = provider_config.get("apiBase") if isinstance(provider_config.get("apiBase"), str) else None
            try:
                credentials = load_credentials(model=model)
            except Exception:
                credentials = {}
            api_key = credentials.get(provider_key, "") if provider_key else None
        return (
            is_model_multimodal(model, provider_key=provider_key, base_url=base_url, api_key=api_key),
            model,
        )

    def image_capability_error(
        session: WebSession,
        image_ids: list[str],
        *,
        model_selection: WebModelSelection | None = None,
    ) -> JSONResponse | None:
        if not image_ids:
            return None
        supports_images, model = active_model_supports_images(session, model_selection=model_selection)
        if supports_images:
            return None
        return json_error(_("Current model {} does not support image input.").format(model), 400)

    def suggestion_to_json(item: SuggestionItem) -> dict[str, str]:
        payload = {
            "label": "{} {}".format(item.display_text, item.description).strip(),
            "value": item.completion.rstrip(),
            "kind": item.source,
        }
        if item.origin:
            payload["origin"] = item.origin
        return payload

    def suggestion_token(trigger: str, query: str) -> CompletionToken:
        text = "{}{}".format(trigger, query)
        return CompletionToken(text=text, start=0, end=len(text), trigger=trigger)

    def command_registry_for_cwd(cwd: Path) -> CommandRegistry:
        registry = create_default_registry()
        skill_state = build_skill_management_state(discover_all_skills(str(cwd)), load_disabled_skills())
        for cmd in skill_state.enabled_commands:
            existing = registry.get(cmd.name)
            if existing is not None and not isinstance(existing, PromptCommand):
                continue
            registry.register(cmd)
        return registry

    def skill_suggestion_provider_for_cwd(cwd: Path) -> SkillProvider:
        return SkillProvider(command_registry_for_cwd(cwd))

    dynamic_suggestion_cache_ttl_seconds = 2.0
    # 斜杠菜单最多等待动态快照(含 MCP)这么久,超时即回退到过期/静态快照,避免慢 MCP 卡住 UI。
    dynamic_suggestion_wait_seconds = 0.25
    dynamic_suggestion_generation = 0
    dynamic_suggestion_cache: dict[
        tuple[str, str, str, str, str, int],
        tuple[float, CommandRegistry, dict[str, Any] | None],
    ] = {}
    dynamic_suggestion_tasks: dict[
        tuple[str, str, str, str, str, int],
        asyncio.Task[tuple[CommandRegistry, dict[str, Any] | None]],
    ] = {}
    dynamic_command_placeholders: dict[asyncio.Task[Any], asyncio.Future[Any]] = {}

    def invalidate_dynamic_suggestions() -> None:
        nonlocal dynamic_suggestion_generation
        dynamic_suggestion_generation += 1
        dynamic_suggestion_cache.clear()

    def dynamic_suggestion_key(session: WebSession | None, cwd: Path) -> tuple[str, str, str, str, str, int]:
        if session is None:
            return ("", str(cwd), "", "", "", dynamic_suggestion_generation)
        return (
            session.session_id,
            str(cwd),
            session.provider or "",
            session.model or "",
            session.effort or "",
            dynamic_suggestion_generation,
        )

    async def build_dynamic_suggestion_snapshot(
        session: WebSession | None,
        cwd: Path,
        cache_key: tuple[str, str, str, str, str, int],
    ) -> tuple[CommandRegistry, dict[str, Any] | None]:
        registry = await run_in_threadpool(command_registry_for_cwd, cwd)
        mcp_status = None
        runtime = None
        if session is not None:
            try:
                runtime = await create_session_agent_runtime_in_thread(session, manager)
                registry = getattr(runtime, "command_registry", None) or registry
                mcp_status = runtime_mcp_status(runtime)
            except Exception:
                logger.debug("Dynamic Web command discovery is unavailable")
            finally:
                await close_agent_runtime(runtime)

        expires_at = time.monotonic() + dynamic_suggestion_cache_ttl_seconds
        dynamic_suggestion_cache[cache_key] = (expires_at, registry, mcp_status)
        now = time.monotonic()
        for stale_key, (stale_expiry, _registry, _status) in list(dynamic_suggestion_cache.items()):
            if stale_expiry <= now:
                dynamic_suggestion_cache.pop(stale_key, None)
        return registry, mcp_status

    async def dynamic_suggestion_snapshot(
        session: WebSession | None,
        cwd: Path,
    ) -> tuple[CommandRegistry, dict[str, Any] | None]:
        cache_key = dynamic_suggestion_key(session, cwd)
        cached = dynamic_suggestion_cache.get(cache_key)
        if cached is not None and cached[0] > time.monotonic():
            return cached[1], cached[2]

        task = dynamic_suggestion_tasks.get(cache_key)
        if task is None:
            task = asyncio.create_task(build_dynamic_suggestion_snapshot(session, cwd, cache_key))
            dynamic_suggestion_tasks[cache_key] = task

            def discard_finished(finished: asyncio.Task[Any]) -> None:
                if dynamic_suggestion_tasks.get(cache_key) is finished:
                    dynamic_suggestion_tasks.pop(cache_key, None)

            task.add_done_callback(discard_finished)

        # 一个慢的 MCP connect_all() 不能卡住斜杠菜单。只短暂等待新快照就绪;等不到就
        # 立即回退到「上一份过期快照」或「静态注册表(内置命令 + 本地技能,无需 runtime)」,
        # 让后台任务把缓存捂热,下一次按键即可拿到含 MCP 的完整命令集。
        try:
            return await asyncio.wait_for(asyncio.shield(task), timeout=dynamic_suggestion_wait_seconds)
        except asyncio.TimeoutError:
            if cached is not None:
                return cached[1], cached[2]
            static_registry = await run_in_threadpool(command_registry_for_cwd, cwd)
            return static_registry, None

    def prompt_command_for_input(session: WebSession, command_text: str) -> tuple[PromptCommand, str] | None:
        registry = command_registry_for_cwd(Path(session.cwd))
        name, args = registry.parse(command_text)
        command = registry.get(name)
        if not isinstance(command, PromptCommand):
            return None
        return command, " ".join(args) if args else ""

    def command_is_missing_from_static_registry(session: WebSession, command_text: str) -> bool:
        if not command_text.lstrip().startswith(("/", "$")):
            return False
        registry = command_registry_for_cwd(Path(session.cwd))
        name, _args = registry.parse(command_text)
        return registry.get(name) is None

    async def dynamic_prompt_command_for_input(
        session: WebSession,
        command_text: str,
    ) -> tuple[PromptCommand, str, Any] | None:
        """Resolve one AgentFactory-provided MCP prompt without leaking its runtime."""
        try:
            runtime = await create_session_agent_runtime_in_thread(session, manager)
        except Exception:
            logger.debug("Dynamic Web command dispatch is unavailable")
            return None
        try:
            registry = getattr(runtime, "command_registry", None)
            parse = getattr(registry, "parse", None)
            get = getattr(registry, "get", None)
            if not callable(parse) or not callable(get):
                return None
            name, args = parse(command_text)
            command = get(name)
            if not isinstance(command, PromptCommand):
                return None
            skill_args = " ".join(args) if args else ""
            if session.mode == "pipeline" and not session.allow_user_escapes.skill:
                return command, skill_args, None
            return command, skill_args, None
        finally:
            await close_agent_runtime(runtime)

    def validate_turn_attachments(session: WebSession, image_ids: list[str], file_refs: list[str]) -> None:
        if image_ids:
            from iac_code.web.images import load_cached_image

            for image_id in image_ids:
                load_cached_image(image_id, cwd=session.cwd, session_id=session.session_id)
        if file_refs:
            from iac_code.web.files import safe_file_references

            safe_file_references(file_refs, cwd=session.cwd, must_exist=True)

    def image_preview_url(image_id: str, session_id: str) -> str:
        return "/api/images/{}?sessionId={}".format(image_id, session_id)

    def image_upload_payload(cached_image, session_ref: str) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "imageId": cached_image.image_id,
            "mediaType": cached_image.media_type,
            "previewUrl": image_preview_url(cached_image.image_id, session_ref),
        }
        if not cached_image.recovery_available:
            payload["recoveryAvailable"] = False
        if cached_image.warning:
            payload["warning"] = cached_image.warning
        return payload

    def reject_obviously_oversized_image_payload(image_data_base64: str) -> None:
        from iac_code.web.images import MAX_IMAGE_BYTES

        max_encoded_length = ((MAX_IMAGE_BYTES + 2) // 3) * 4
        if len(image_data_base64) > max_encoded_length:
            raise ValueError(_("image data is too large"))

    def active_turn_running(session: WebSession) -> bool:
        return session.turn_lock.locked() or (
            session.active_turn_task is not None and not session.active_turn_task.done()
        )

    def active_local_work_running(session: WebSession) -> bool:
        completed = {task for task in session.active_local_tasks if task.done()}
        session.active_local_tasks.difference_update(completed)
        return bool(session.active_local_tasks)

    def active_session_work_running(session: WebSession) -> bool:
        return active_turn_running(session) or active_local_work_running(session)

    def session_archived_response(session: WebSession) -> JSONResponse | None:
        if not session.archived:
            return None
        return json_error(_("session is archived"), 409, code="session_archived")

    async def delete_session_if_idle(session: WebSession) -> bool | None:
        async with session.turn_admission_lock:
            if manager.get_session(session.web_session_id) is not session:
                return False
            if active_session_work_running(session):
                return None
            return manager.delete_session(session.web_session_id)

    async def reserve_agent_turn(session: WebSession) -> tuple[str, asyncio.Future[Any]] | None:
        await session.turn_admission_lock.acquire()
        if (
            manager.get_session(session.web_session_id) is not session
            or session.archived
            or active_turn_running(session)
        ):
            session.turn_admission_lock.release()
            return None
        turn_id = uuid.uuid4().hex
        placeholder: asyncio.Future[Any] = asyncio.get_running_loop().create_future()
        session.active_turn_task = placeholder
        return turn_id, placeholder

    def release_agent_turn_reservation(session: WebSession, placeholder: asyncio.Future[Any]) -> None:
        if session.active_turn_task is not placeholder:
            return
        session.active_turn_task = None
        if session.turn_admission_lock.locked():
            session.turn_admission_lock.release()

    def cancel_active_turn_task(task: asyncio.Future[Any]) -> None:
        if isinstance(task, asyncio.Task):
            placeholder = dynamic_command_placeholders.get(task)
            if placeholder is not None:
                placeholder.cancel()
        task.cancel()

    async def reserve_pipeline_action(
        session: WebSession, *, wait_for_admission: bool = False
    ) -> asyncio.Future[Any] | None:
        if session.turn_admission_lock.locked():
            if active_turn_running(session) or not wait_for_admission:
                return None
        await session.turn_admission_lock.acquire()
        if (
            manager.get_session(session.web_session_id) is not session
            or session.archived
            or active_turn_running(session)
        ):
            session.turn_admission_lock.release()
            return None
        placeholder: asyncio.Future[Any] = asyncio.get_running_loop().create_future()
        session.active_turn_task = placeholder
        return placeholder

    def release_pipeline_action_reservation(session: WebSession, placeholder: asyncio.Future[Any]) -> None:
        if session.active_turn_task is placeholder:
            session.active_turn_task = None
        if session.turn_admission_lock.locked():
            session.turn_admission_lock.release()

    def transfer_pipeline_action_reservation(
        session: WebSession,
        placeholder: asyncio.Future[Any],
    ) -> asyncio.Task[Any]:
        owner = asyncio.current_task()
        assert owner is not None
        if session.active_turn_task is not placeholder or placeholder.cancelled():
            release_pipeline_action_reservation(session, placeholder)
            raise TurnReservationCanceledError
        session.active_turn_task = owner
        if session.turn_admission_lock.locked():
            session.turn_admission_lock.release()
        return owner

    async def release_pipeline_action_owner(session: WebSession, owner: asyncio.Task[Any]) -> None:
        async with session.turn_admission_lock:
            if session.active_turn_task is owner:
                session.active_turn_task = None

    def turn_busy_response() -> JSONResponse:
        return JSONResponse({"accepted": False, "reason": "turn already running"}, status_code=409)

    async def start_background_turn(
        session: WebSession,
        *,
        text: str,
        image_ids: list[str] | None = None,
        file_refs: list[str] | None = None,
        source: str = "composer",
        context_modifier: Any | None = None,
        model_selection: WebModelSelection | None = None,
        reservation: tuple[str, asyncio.Future[Any]] | None = None,
    ) -> str:
        if reservation is None:
            reservation = await reserve_agent_turn(session)
            if reservation is None:
                raise RuntimeError("turn already running")
        turn_id, placeholder = reservation
        if placeholder.cancelled():
            release_agent_turn_reservation(session, placeholder)
            raise TurnReservationCanceledError
        try:
            if placeholder.cancelled():
                raise TurnReservationCanceledError
            selection = model_selection or active_model_selection(session)
            runtime = make_runtime(session)
            turn_request = WebTurnRequest(
                text=text,
                image_ids=image_ids or [],
                file_refs=file_refs or [],
                source=source,
                turn_id=turn_id,
                context_modifier=context_modifier,
                model_selection=selection,
            )
            task = asyncio.create_task(_run_turn_task(session, runtime, turn_request))
            if session.active_turn_task is not placeholder or placeholder.cancelled():
                task.cancel()
                raise TurnReservationCanceledError
            session.active_turn_task = task
        except Exception:
            release_agent_turn_reservation(session, placeholder)
            raise
        if session.turn_admission_lock.locked():
            session.turn_admission_lock.release()
        await asyncio.sleep(0)
        return turn_id

    def ensure_pipeline_identity(session: WebSession) -> tuple[str, str]:
        context_id = session.context_id or "ctx-{}".format(uuid.uuid4().hex[:12])
        task_id = session.task_id or "task-{}".format(uuid.uuid4().hex[:12])
        manager.attach_pipeline_identity(
            session,
            context_id=context_id,
            task_id=task_id,
            pipeline_name=session.pipeline_name or "selling",
        )
        return context_id, task_id

    async def _run_pipeline_turn_task(
        session: WebSession,
        *,
        turn_id: str,
        text: str,
        image_ids: list[str],
        file_refs: list[str],
        source: str,
        model_selection: WebModelSelection,
    ) -> None:
        drain_queued_after_turn = False
        input_consumed = False
        try:
            async with session.turn_lock:
                session.active_turn_task = asyncio.current_task()
                session.status = "running"
                # 新一轮开始即清未读(与普通回合一致):进行中的会话不应残留未读圆点。
                manager.mark_session_running(session)
                # 流水线会话的 prompt 不进 web 会话自身 JSONL，index 派生的标题恒为「(empty)」、
                # 会话在侧栏被过滤掉。首个回合用 prompt 文本补上标题(内存 + web sidecar），
                # 并广播 session.updated 让侧栏立即出现该会话、名称正确。
                if manager.apply_pipeline_auto_title(session, text):
                    await session.events.publish("session.updated", {"title": session.title})
                # 即时占位标题只做「立刻出现在侧栏」;与普通回合一致,首个回合后台用 LLM 生成
                # 正式标题刷新占位(pending_llm_title 守卫保证 once-only、旧会话/重开不触发)。
                manager.schedule_llm_title(session, text=text, image_ids=image_ids)
                # 流水线回合的对话进 A2A/pipeline 存储，web 会话 JSONL 里没有用户消息，刷新后
                # 主转录区连第一条 prompt 都会丢失。这里把 prompt 落进 web 会话自身的 JSONL，
                # 让恢复路径(load_resume_messages)能读回并渲染成用户气泡，与普通回合对齐。
                # 若流水线已交接给普通对话(snapshot 里有 normalHandoff)，本回合属于交接后的普通
                # 对话，打上 normalChat 标记，恢复时才能把「↪ 普通对话」分隔准确插在首条普通消息前。
                normal_chat_turn = _pipeline_snapshot_switched_to_normal(
                    await load_pipeline_snapshot(context_id=session.context_id, task_id=session.task_id)
                )
                manager.persist_pipeline_user_prompt(
                    session,
                    text,
                    normal_chat=normal_chat_turn,
                    turn_id=turn_id,
                    image_ids=image_ids,
                    file_refs=file_refs,
                )
                input_consumed = True
                # 流水线回合此前只发 pipeline.event，从不发 user.message，导致输入 prompt 后
                # 主转录区一直停在空状态(进度只在隐藏的 Pipeline 标签页渲染)。这里补发一条
                # user.message，让 prompt 立即渲染成用户气泡、空状态消失，复刻普通回合
                # (runtime.py 的 start_turn)的实时渲染路径。
                await session.events.publish(
                    "user.message",
                    {
                        "turnId": turn_id,
                        "text": text,
                        "imageIds": list(image_ids),
                        "fileRefs": list(file_refs),
                        "source": "pipeline",
                    },
                )
                await session.events.publish(
                    "pipeline.event",
                    {
                        "kind": "pipeline.web_turn.started",
                        "turnId": turn_id,
                        "mode": "pipeline",
                        "pipelineName": session.pipeline_name,
                        "contextId": session.context_id,
                        "taskId": session.task_id,
                    },
                )
                result = await pipeline_action_runner.start(
                    session,
                    text,
                    image_ids,
                    file_refs,
                    model_selection=model_selection,
                    event_sink=lambda evs: publish_pipeline_live_events(session, evs),
                    permission_resolver=make_pipeline_permission_resolver(session),
                    envelope_observer=lambda env: diagram_optimization_coordinator.maybe_trigger(session, manager, env),
                )
                await publish_pipeline_action_events(
                    session,
                    list(result.events),
                    base_payload={
                        "turnId": turn_id,
                        "contextId": session.context_id,
                        "taskId": session.task_id,
                        "mode": "pipeline",
                    },
                )
                # 终态(failed/canceled)不再走通用 error 事件:那会被前端存进 lastError 并
                # 无条件钉在消息栈最底部(永远显示为最新一条、固定红色、英文)。改由主转录里
                # 交接前的彩色「流水线结局」行(pipeline_outcome marker)承载,位置正确、按结局
                # 着色、中文。仅普通动作校验失败(404/400/500 等,terminal_outcome 为 None)才报错。
                if not result.accepted and result.terminal_outcome is None:
                    await session.events.publish(
                        "error",
                        {
                            "turnId": turn_id,
                            "message": result.response.get("error", {}).get("message", _("pipeline action failed")),
                            "retryable": result.status_code < 500,
                            "contextId": session.context_id,
                            "taskId": session.task_id,
                        },
                    )
                await session.events.publish(
                    "turn.done",
                    {
                        "turnId": turn_id,
                        "mode": "pipeline",
                        "interrupted": False,
                        "canceled": False,
                        "failed": not result.accepted,
                        "contextId": session.context_id,
                        "taskId": session.task_id,
                    },
                )
                if result.accepted:
                    manager.mark_session_completed(session)
                # 本回合若把流水线交接给普通对话,落模式为 normal 并广播,后续输入才走普通
                # agent 运行时(Issue 4)。contextId/taskId 保留,reload 仍能重建流水线转录。
                if await maybe_switch_session_to_normal(session):
                    await session.events.publish(
                        "session.updated",
                        {
                            "mode": session.mode,
                            "contextId": session.context_id,
                            "taskId": session.task_id,
                        },
                    )
                drain_queued_after_turn = bool(result.accepted)
        except asyncio.CancelledError:
            await session.events.publish(
                "turn.done",
                {
                    "turnId": turn_id,
                    "mode": "pipeline",
                    "interrupted": True,
                    "canceled": True,
                    "contextId": session.context_id,
                    "taskId": session.task_id,
                },
            )
            raise
        except Exception as exc:
            await session.events.publish(
                "error",
                {
                    "turnId": turn_id,
                    "message": public_exception_message(exc),
                    "retryable": False,
                    "contextId": session.context_id,
                    "taskId": session.task_id,
                },
            )
            await session.events.publish(
                "turn.done",
                {
                    "turnId": turn_id,
                    "mode": "pipeline",
                    "interrupted": False,
                    "canceled": False,
                    "failed": True,
                    "contextId": session.context_id,
                    "taskId": session.task_id,
                },
            )
        finally:
            restore_unpublished_turn_input(
                session,
                turn_id=turn_id,
                text=text,
                image_ids=image_ids,
                file_refs=file_refs,
                source=source,
                input_consumed=input_consumed,
            )
            session.status = "idle"
            current_task = asyncio.current_task()
            if session.active_turn_task is current_task:
                session.active_turn_task = None
        if drain_queued_after_turn:
            await start_next_queued_turn(session)

    async def start_background_pipeline_turn(
        session: WebSession,
        *,
        text: str,
        image_ids: list[str] | None = None,
        file_refs: list[str] | None = None,
        source: str = "composer",
        model_selection: WebModelSelection | None = None,
        reservation: asyncio.Future[Any] | None = None,
    ) -> str:
        turn_id = uuid.uuid4().hex
        try:
            if reservation is not None and reservation.cancelled():
                raise TurnReservationCanceledError
            ensure_pipeline_identity(session)
            selection = model_selection or active_model_selection(session)
            task = asyncio.create_task(
                _run_pipeline_turn_task(
                    session,
                    turn_id=turn_id,
                    text=text,
                    image_ids=image_ids or [],
                    file_refs=file_refs or [],
                    source=source,
                    model_selection=selection,
                )
            )
            if reservation is not None and session.active_turn_task is not reservation:
                task.cancel()
                raise TurnReservationCanceledError
            session.active_turn_task = task
        except Exception:
            if reservation is not None:
                release_pipeline_action_reservation(session, reservation)
            raise
        if reservation is not None and session.turn_admission_lock.locked():
            session.turn_admission_lock.release()
        await asyncio.sleep(0)
        return turn_id

    def restore_queued_input(session: WebSession, text: str, *, turn_id: str | None = None) -> None:
        session.queued_inputs.insert(0, text)
        payload: dict[str, Any] = {
            "text": text,
            "draft": session.draft,
            "restored": True,
            "index": 0,
        }
        if turn_id is not None:
            payload["turnId"] = turn_id
        session.events.append("queued-input.accepted", payload)

    def turn_input_published(
        session: WebSession,
        *,
        turn_id: str | None,
        text: str,
        source: str,
        after_sequence: int = 0,
    ) -> bool:
        if turn_id is not None and turn_id in session.consumed_turn_ids:
            return True
        for event in session.events.replay_after(after_sequence):
            if event.get("type") != "user.message":
                continue
            payload = event.get("payload")
            if not isinstance(payload, dict):
                continue
            if turn_id is not None:
                if payload.get("turnId") == turn_id:
                    return True
                continue
            if payload.get("source") == source and payload.get("text") == text:
                return True
        return False

    def restore_unpublished_turn_input(
        session: WebSession,
        *,
        turn_id: str | None,
        text: str,
        image_ids: list[str],
        file_refs: list[str],
        source: str,
        after_sequence: int = 0,
        input_consumed: bool | None = None,
    ) -> bool:
        if input_consumed is True:
            return False
        if input_consumed is None and turn_input_published(
            session,
            turn_id=turn_id,
            text=text,
            source=source,
            after_sequence=after_sequence,
        ):
            return False
        if source == "queued":
            restore_queued_input(session, text, turn_id=turn_id)
            return True
        session.draft = text
        session.events.append(
            "draft.updated",
            {
                "turnId": turn_id,
                "draft": text,
                "reason": "turn_failed_before_user_message",
                "restored": True,
                "imageIds": list(image_ids),
                "fileRefs": list(file_refs),
            },
        )
        return True

    async def start_next_queued_turn(session: WebSession) -> None:
        if not session.queued_inputs:
            return
        if session.mode == "pipeline":
            placeholder = await reserve_pipeline_action(session)
            if placeholder is None:
                return
            next_text = manager.pop_next_queued_input(session)
            if next_text is None:
                release_pipeline_action_reservation(session, placeholder)
                return
            try:
                await start_background_pipeline_turn(
                    session,
                    text=next_text,
                    source="queued",
                    reservation=placeholder,
                )
            except Exception:
                release_pipeline_action_reservation(session, placeholder)
                restore_queued_input(session, next_text)
                logger.exception("Failed to start queued pipeline turn")
            return

        reservation = await reserve_agent_turn(session)
        if reservation is None:
            return
        next_text = manager.pop_next_queued_input(session)
        if next_text is None:
            release_agent_turn_reservation(session, reservation[1])
            return
        try:
            await start_background_turn(
                session,
                text=next_text,
                source="queued",
                reservation=reservation,
            )
        except Exception:
            release_agent_turn_reservation(session, reservation[1])
            restore_queued_input(session, next_text)
            logger.exception("Failed to start queued agent turn")

    async def _drain_queue_after_stop(session: WebSession, stopped_task: asyncio.Task[Any]) -> None:
        """After a user STOP cancels a turn, auto-submit any queued inputs.

        Waits for the cancelled turn to fully unwind (its ``finally`` clears
        ``active_turn_task`` so the next reservation can be admitted), then starts
        the next queued turn — whose own ``_run_turn_task`` while-loop drains the
        rest. Bails out if app shutdown began, the session was swapped out, or it
        was archived, so teardown never resurrects a turn.
        """
        current = asyncio.current_task()
        try:
            await asyncio.gather(stopped_task, return_exceptions=True)
            if shutdown_state["initiated"]:
                return
            if session not in manager.loaded_sessions() or session.archived:
                return
            await start_next_queued_turn(session)
        except Exception:
            logger.exception("Failed to drain queue after stop")
        finally:
            if current is not None:
                session.active_local_tasks.discard(current)

    async def not_found(_request, _exc):
        return json_error(_("not found"), 404)

    def optional_string(data: dict, field: str) -> str | None:
        if field not in data:
            return None
        value = data[field]
        if not isinstance(value, str):
            raise ValueError("{} must be a string".format(field))
        return value

    def string_with_default(data: dict, field: str, default: str = "") -> str:
        value = optional_string(data, field)
        return default if value is None else value

    def required_string(data: dict, field: str) -> str:
        if field not in data:
            raise ValueError("{} is required".format(field))
        value = data[field]
        if not isinstance(value, str):
            raise ValueError("{} must be a string".format(field))
        return value

    def optional_string_list(data: dict, field: str) -> list[str]:
        if field not in data:
            return []
        value = data[field]
        if not isinstance(value, list) or any(not isinstance(item, str) for item in value):
            raise ValueError("{} must be a list of strings".format(field))
        return value

    def required_string_list(data: dict, field: str) -> list[str]:
        if field not in data:
            raise ValueError("{} is required".format(field))
        return optional_string_list(data, field)

    def query_string(request, field: str, default: str = "") -> str:
        value = request.query_params.get(field, default)
        return value if isinstance(value, str) else default

    def parse_slash_command_text(command_text: str) -> tuple[str, str] | None:
        stripped = command_text.strip()
        if not stripped.startswith("/"):
            return None
        command, _, argument = stripped[1:].partition(" ")
        return command.lower(), argument

    def query_int(request, field: str, default: int | None = None) -> int | None:
        raw_value = request.query_params.get(field)
        if raw_value in {None, ""}:
            return default
        try:
            value = int(raw_value)
        except ValueError as exc:
            raise ValueError("{} must be an integer".format(field)) from exc
        if value < 0:
            raise ValueError("{} must be non-negative".format(field))
        return value

    def cwd_for_session_id(session_id: str) -> Path | None:
        if not session_id:
            return Path(manager.cwd)
        session = manager.get_session(session_id)
        if session is None:
            return None
        return Path(session.cwd)

    def known_project_entries() -> list[dict[str, Any]]:
        # 记忆/插件/AGENTS 的项目选择器与 cwd 解析需覆盖无会话的空项目(侧栏已隐藏它们),
        # 故此处 include_empty=True,保持这些面板可管理空项目的既有行为。
        projects, _project_total, _session_total = manager.list_session_projects(project_limit=None, include_empty=True)
        return [*projects, *manager.list_pinned_projects()]

    def optional_bool_map(data: dict, field: str) -> dict[str, bool] | None:
        if field not in data:
            return None
        value = data[field]
        if not isinstance(value, dict):
            raise ValueError("{} must be an object".format(field))
        allowed_keys = {"skill", "command", "shell"}
        normalized: dict[str, bool] = {}
        for key, item in value.items():
            if key not in allowed_keys:
                raise ValueError("{} contains an unknown key".format(field))
            if not isinstance(item, bool):
                raise ValueError("{}.{} must be a boolean".format(field, key))
            normalized[key] = item
        return normalized

    def apply_allow_user_escapes_update(session: WebSession, values: dict[str, bool]) -> None:
        from iac_code.pipeline.engine.step_spec import AllowUserEscapes

        session.allow_user_escapes = AllowUserEscapes(
            skill=values.get("skill", session.allow_user_escapes.skill),
            command=values.get("command", session.allow_user_escapes.command),
            shell=values.get("shell", session.allow_user_escapes.shell),
        )
        session.events.append(
            "session.updated",
            {
                "allowUserEscapes": {
                    "skill": session.allow_user_escapes.skill,
                    "command": session.allow_user_escapes.command,
                    "shell": session.allow_user_escapes.shell,
                },
            },
        )

    def local_shell_start_payload(shell_events: list[dict[str, Any]], *, command: str) -> dict[str, Any]:
        for event in reversed(shell_events):
            if event["type"] == "local.shell.start":
                payload = event.get("payload", {})
                if not isinstance(payload, dict):
                    continue
                copied_payload = dict(payload)
                if copied_payload.get("command") == command:
                    return copied_payload
        return {}

    def has_matching_local_shell_end(
        shell_events: list[dict[str, Any]],
        *,
        command: str,
        shell_use_id: str | None,
    ) -> bool:
        for event in shell_events:
            if event["type"] != "local.shell.end":
                continue
            payload = event.get("payload", {})
            if not isinstance(payload, dict):
                continue
            payload_shell_use_id = payload.get("shellUseId") or payload.get("toolUseId")
            if isinstance(shell_use_id, str) and shell_use_id:
                if payload_shell_use_id == shell_use_id:
                    return True
                if not payload_shell_use_id and payload.get("command") == command:
                    return True
                continue
            if payload.get("command") == command:
                return True
        return False

    def session_accepts_pipeline_input(session: WebSession) -> bool:
        return session.mode == "pipeline" and bool(session.context_id and session.task_id)

    async def maybe_switch_session_to_normal(
        session: WebSession,
        *,
        admission_reserved: bool = False,
    ) -> bool:
        """流水线已交接给普通对话时,把会话模式从 pipeline 翻转为 normal。

        交接后若模式仍是 pipeline,post_message 会把用户输入继续路由到流水线路径、被引擎忽略,
        表现为「进入普通对话后继续对话完全没反应」(Issue 4)。回合结束时(引擎刚写下 handoff)
        及 post_message 前(进程重启后的兜底)各调一次,据 A2A 快照的 normalHandoff 判定。
        """
        if session.mode != "pipeline":
            return False
        snapshot = await load_pipeline_snapshot(context_id=session.context_id, task_id=session.task_id)
        if not _pipeline_snapshot_switched_to_normal(snapshot):
            return False

        def apply_handoff() -> bool:
            if manager.get_session(session.web_session_id) is not session or session.archived:
                return False
            # 翻转模式前,把引擎生成的交接摘要(normalHandoff.summary)落入 web 会话 JSONL,交接后的
            # 普通回合经 load_resume_messages 才能读到流水线上下文,LLM 才知道「刚才创建了什么」。
            # 摘要与 CLI _handoff_pipeline_to_normal 注入的内容同源;幂等,重启兜底重复调用不会重复注入。
            handoff = snapshot.get("normalHandoff") if isinstance(snapshot, dict) else None
            summary = handoff.get("summary") if isinstance(handoff, dict) else None
            manager.persist_pipeline_handoff_context(session, summary)
            return manager.switch_session_to_normal_after_handoff(session)

        if admission_reserved:
            return apply_handoff()
        async with session.turn_admission_lock:
            return apply_handoff()

    def make_pipeline_permission_resolver(session: WebSession):
        """构造一个会话绑定的流水线权限解析器(Issue 6)。

        流水线执行器过去拿不到 resolver,交互模式下会把每个工具直接拒绝,表现为「选择了
        请求批准却没弹权限界面、直接失败」。这里返回一个 async 回调:引擎每遇到一个工具权限
        请求就调用它,我们据请求构造 web 权限载荷、经 manager 注册出一条 permission.request
        SSE(前端据此弹审批界面),再阻塞等待用户作答的 future,把布尔结果回传给引擎。

        多个 sub-pipeline 并发触发时各自拿到独立 request_id/future,前端逐个排队审批。回合被
        取消时 future 被 cancel:清理该 pending 并向上抛,让执行器把该工具当作拒绝处理。
        """
        from iac_code.web.runtime import _permission_request_payload

        async def resolver(event: Any) -> bool:
            payload = _permission_request_payload(
                event,
                turn_id=session.active_turn_id or "",
                allow_always=False,
            )
            future: asyncio.Future[Any] = asyncio.get_running_loop().create_future()
            request_id = manager.add_permission_request(session, payload, future=future)
            try:
                result = await asyncio.shield(future)
            except asyncio.CancelledError:
                manager.cancel_permission_request(request_id, session_id=session.session_id)
                raise
            return bool(result)

        return resolver

    def require_pipeline_metadata(session: WebSession) -> None:
        if not session.context_id or not session.task_id:
            raise ValueError(_("pipeline contextId and taskId are required"))
        from iac_code.a2a.types import validate_protocol_id

        try:
            validate_protocol_id(session.context_id)
            validate_protocol_id(session.task_id)
        except ValueError as exc:
            raise ValueError(_("pipeline contextId or taskId is invalid")) from exc

    def session_candidate_payload(session: WebSession) -> dict[str, Any]:
        return normalize_event_payload(
            {
                "sessionId": session.session_id,
                "webSessionId": session.web_session_id,
                "cwd": session.cwd,
                "title": session.title,
                "mode": session.mode,
                "pipelineName": session.pipeline_name,
                "contextId": session.context_id,
                "taskId": session.task_id,
                "updatedAt": session.updated_at,
            }
        )

    def session_entry_candidate_payload(entry) -> dict[str, Any]:
        return normalize_event_payload(
            {
                "sessionId": entry.session_id,
                "cwd": entry.cwd,
                "title": entry.name or entry.title,
                "name": entry.name,
                "gitBranch": entry.git_branch,
                "projectName": entry.project_name,
                "mtime": entry.mtime,
                "sizeBytes": entry.size_bytes,
                "isLegacy": entry.is_legacy,
            }
        )

    def matching_resume_sessions(current: WebSession, argument: str) -> list[Any]:
        needle = argument.strip()
        if not needle:
            return []

        def match_entries(entries):
            for matcher in (
                lambda entry: entry.session_id == needle,
                lambda entry: entry.session_id.startswith(needle),
                lambda entry: entry.name == needle or entry.title == needle,
            ):
                matches = [entry for entry in entries if matcher(entry)]
                if matches:
                    return matches
            return []

        current_entries = manager.index.list_for_cwd(current.cwd)
        current_matches = match_entries(current_entries)
        if current_matches:
            return current_matches

        current_keys = {(entry.cwd, entry.session_id) for entry in current_entries}
        global_entries = [
            entry for entry in manager.index.list_all_projects() if (entry.cwd, entry.session_id) not in current_keys
        ]
        return match_entries(global_entries)

    def resume_candidates_for_current_project_first(current: WebSession) -> list[dict[str, Any]]:
        candidates: list[dict[str, Any]] = []
        seen: set[tuple[str, str]] = set()
        for entries in (manager.index.list_for_cwd(current.cwd), manager.index.list_all_projects()):
            for entry in entries:
                key = (entry.cwd, entry.session_id)
                if key in seen:
                    continue
                seen.add(key)
                candidates.append(session_entry_candidate_payload(entry))
        return candidates

    def resume_command_result(current: WebSession, argument: str) -> tuple[dict[str, Any], int]:
        if not argument.strip():
            return (
                {
                    "accepted": False,
                    "command": "resume",
                    "action": "open_resume_chooser",
                    "candidates": resume_candidates_for_current_project_first(current),
                },
                200,
            )
        matches = matching_resume_sessions(current, argument)
        if not matches:
            return (
                {
                    "accepted": False,
                    "command": "resume",
                    "error": {"code": "resume_not_found", "message": _("session not found")},
                    "candidates": [],
                },
                404,
            )
        if len(matches) > 1:
            return (
                {
                    "accepted": False,
                    "command": "resume",
                    "error": {"code": "resume_ambiguous", "message": _("resume target is ambiguous")},
                    "candidates": [session_entry_candidate_payload(match) for match in matches],
                },
                409,
            )
        target_entry = matches[0]
        target = manager.create_session(cwd=target_entry.cwd, session_id=target_entry.session_id)
        same_project = Path(target.cwd).resolve() == Path(current.cwd).resolve()
        return (
            {
                "accepted": True,
                "command": "resume",
                "action": "reload_session" if same_project else "open_session",
                "session": target.to_dict(),
                "messages": manager.load_visible_messages(target.session_id, cwd=target.cwd),
            },
            200,
        )

    async def publish_pipeline_action_events(
        session: WebSession,
        events: list[dict[str, Any]],
        *,
        base_payload: dict[str, Any],
    ) -> None:
        allowed_web_event_types = {"pipeline.event", "pipeline.snapshot", "candidate.detail", "candidate.diagram"}
        for event_payload in events:
            payload = dict(event_payload)
            event_type = str(payload.pop("webEventType", "pipeline.event"))
            if event_type not in allowed_web_event_types:
                event_type = "pipeline.event"
            payload.update({key: value for key, value in base_payload.items() if key not in payload})
            await session.events.publish(event_type, payload)

    async def publish_pipeline_live_events(session: WebSession, web_events: list[dict[str, Any]]) -> None:
        # Live-forward translated pipeline SSE events (pipeline.step.marker /
        # assistant.message.* / tool.*) so the main transcript streams char-by-char
        # while the pipeline runs, exactly like a normal chat turn.
        for event in web_events:
            event_type = event.get("type")
            if not event_type:
                continue
            await session.events.publish(event_type, event.get("payload") or {})

    async def json_object_body(request) -> dict:
        body = await request.body()
        if not body:
            return {}
        try:
            data = json.loads(body)
        except json.JSONDecodeError as exc:
            raise ValueError(_("malformed JSON request body")) from exc
        if not isinstance(data, dict):
            raise ValueError(_("request body must be a JSON object"))
        return data

    def validate_permission_answer(data: dict) -> dict:
        session_id = required_string(data, "sessionId")
        choice = required_string(data, "choice")
        if choice not in PERMISSION_CHOICES:
            raise ValueError(_("choice is invalid"))
        return {"sessionId": session_id, "choice": choice}

    def validate_question_answer(data: dict) -> dict:
        session_id = required_string(data, "sessionId")
        for field in ("selected_id", "selected_label", "free_text"):
            if field not in data:
                raise ValueError("{} is required".format(field))
            if not isinstance(data[field], str):
                raise ValueError("{} must be a string".format(field))
        return {
            "sessionId": session_id,
            "selected_id": data["selected_id"],
            "selected_label": data["selected_label"],
            "free_text": data["free_text"],
        }

    def required_bool(data: dict, field: str) -> bool:
        if field not in data:
            raise ValueError("{} is required".format(field))
        value = data[field]
        if not isinstance(value, bool):
            raise ValueError("{} must be a boolean".format(field))
        return value

    def question_option_ids(payload: dict[str, Any]) -> set[str]:
        options = payload.get("options")
        if not isinstance(options, list):
            return set()
        return {
            str(option["id"])
            for option in options
            if isinstance(option, dict) and isinstance(option.get("id"), str) and option.get("id")
        }

    def question_option_label(payload: dict[str, Any], selected_id: str) -> str | None:
        options = payload.get("options")
        if not isinstance(options, list):
            return None
        for option in options:
            if not isinstance(option, dict) or not isinstance(option.get("id"), str):
                continue
            if option["id"] == selected_id:
                return str(option.get("label") or selected_id)
        return None

    def question_allows_free_text(payload: dict[str, Any]) -> bool:
        return payload.get("allowFreeText") is True or payload.get("allow_free_text") is True

    def normalize_question_answer_for_pending(answer: dict[str, str], payload: dict[str, Any]) -> dict[str, str]:
        selected_id = answer["selected_id"]
        has_free_text = bool(answer["free_text"].strip())
        if has_free_text and not question_allows_free_text(payload):
            raise ValueError(_("free_text is not allowed"))
        if selected_id:
            selected_label = question_option_label(payload, selected_id)
            if selected_label is None:
                raise ValueError(_("selected_id was not offered"))
            return {
                "selected_id": selected_id,
                "selected_label": selected_label,
                "free_text": answer["free_text"],
            }
        if answer["selected_label"].strip():
            raise ValueError(_("selected_label is not allowed without selected_id"))
        if question_allows_free_text(payload) and has_free_text:
            return {
                "selected_id": "",
                "selected_label": "",
                "free_text": answer["free_text"],
            }
        raise ValueError(_("selected_id is required unless free text is allowed"))

    def event_cursor(request) -> int:
        cursors: list[int] = []
        for raw_value in (
            request.query_params.get("afterSequence"),
            request.headers.get("last-event-id"),
        ):
            if raw_value in {None, ""}:
                continue
            try:
                after_sequence = int(raw_value)
            except ValueError as exc:
                raise ValueError(_("afterSequence must be an integer")) from exc
            if after_sequence < 0:
                raise ValueError("afterSequence must be non-negative")
            cursors.append(after_sequence)
        return max(cursors, default=0)

    async def health(_request):
        return JSONResponse({"service": "iac-code-web", "status": "ok"})

    async def restart_server(_request):
        from iac_code.web import server

        server.schedule_restart()
        return JSONResponse({"status": "restarting"}, status_code=202)

    # 更新能力:apply-job 状态随 app 实例隔离(闭包,非模块级),避免测试间串状态。
    _update_apply = {"state": "idle", "error": None}
    _update_apply_lock = threading.Lock()

    def _capturing_run(cmd, **kwargs):
        # run_update_command 默认 stdout/stderr=None(继承);web 需捕获 stderr 以展示错误。
        kwargs.pop("stdout", None)
        kwargs.pop("stderr", None)
        return subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, check=False)

    async def update_status(_request):
        pending = get_pending_update(current_version=_iac_version)
        with _update_apply_lock:
            apply_state = _update_apply["state"]
            apply_error = _update_apply["error"]
        available = pending is not None
        return JSONResponse(
            {
                "available": available,
                "currentVersion": pending.current_version if available else None,
                "latestVersion": pending.version if available else None,
                "releaseNotesUrl": pending.release_notes_url if available else None,
                "applyState": apply_state,
                "error": apply_error,
            }
        )

    async def update_apply(_request):
        pending = get_pending_update(current_version=_iac_version)
        if pending is None:
            return JSONResponse({"error": {"message": _("No update is currently available.")}}, status_code=409)
        with _update_apply_lock:
            if _update_apply["state"] == "running":
                return JSONResponse({"error": {"message": _("An update is already in progress.")}}, status_code=409)
            _update_apply["state"] = "running"
            _update_apply["error"] = None

        def _run() -> None:
            try:
                result = run_update_command(pending, subprocess_run=_capturing_run)
            except Exception as exc:  # noqa: BLE001 - 升级失败原因透传给前端
                with _update_apply_lock:
                    _update_apply["state"] = "failed"
                    _update_apply["error"] = str(exc)[:500]
                return
            with _update_apply_lock:
                if result.returncode == 0:
                    _update_apply["state"] = "done"
                    _update_apply["error"] = None
                else:
                    detail = (result.stderr or result.stdout or "").strip()
                    _update_apply["state"] = "failed"
                    _update_apply["error"] = (
                        detail or _("The update command exited with code {}.").format(result.returncode)
                    )[:500]

        threading.Thread(target=_run, name="iac-code-web-update-apply", daemon=True).start()
        return JSONResponse({"status": "updating"}, status_code=202)

    async def update_dismiss(_request):
        pending = get_pending_update(current_version=_iac_version)
        if pending is not None:
            suppress_version(pending.version)
        return Response(status_code=204)

    async def index(_request):
        html = (STATIC_DIR / "index.html").read_text(encoding="utf-8")
        theme = get_appearance_theme()
        lang = resolve_ui_language(get_ui_language())
        catalog = load_webui_catalog(lang)
        html = html.replace(
            '<html lang="zh-CN">',
            '<html lang="{}" data-theme="{}">'.format(lang, theme),
            1,
        )
        i18n_script = "<script>window.__IAC_I18N__ = {};</script>".format(
            json.dumps({"lang": lang, "messages": catalog}, ensure_ascii=False).replace("<", "\\u003c")
        )
        html = html.replace("</head>", i18n_script + "\n  </head>", 1)
        # 把新会话默认(权限/模式)注入 <body>,让首屏创建的草稿即刻采用,避免异步拉取的闪烁。
        # 放在 <body> 而非 <html>,以免破坏对主题标签精确形态的既有断言。
        defaults = get_session_defaults()
        html = html.replace(
            "<body>",
            '<body data-default-permission-mode="{}" data-default-mode="{}" data-default-pipeline-name="{}">'.format(
                escape(defaults["permissionMode"], quote=True),
                escape(defaults["mode"], quote=True),
                escape(defaults["pipelineName"], quote=True),
            ),
            1,
        )
        return HTMLResponse(html, headers={"Cache-Control": "no-store"})

    async def create_session(request):
        try:
            data = await json_object_body(request)
            cwd = optional_string(data, "cwd")
            raw_mode = optional_string(data, "mode") if "mode" in data else os.environ.get("IAC_CODE_MODE", "normal")
            if raw_mode not in {"normal", "pipeline"}:
                raise ValueError(_("mode must be normal or pipeline"))
            mode = cast(WebMode, raw_mode)
            pipeline_name = (
                optional_string(data, "pipelineName")
                if "pipelineName" in data
                else os.environ.get("IAC_CODE_PIPELINE_NAME")
            )
            context_id = optional_string(data, "contextId")
            task_id = optional_string(data, "taskId")
            session_id = optional_string(data, "sessionId")
            allow_user_escapes = optional_bool_map(data, "allowUserEscapes")
            permission_mode = optional_string(data, "permissionMode")
            provider = optional_string(data, "provider")
            model = optional_string(data, "model")
            effort = optional_string(data, "effort")
            session = manager.create_session(
                cwd=cwd,
                mode=mode,
                pipeline_name=pipeline_name,
                context_id=context_id,
                task_id=task_id,
                session_id=session_id,
                allow_user_escapes=allow_user_escapes,
                permission_mode=permission_mode,
                provider=provider,
                model=model,
                effort=effort,
            )
        except ValueError as exc:
            return json_error(str(exc), 400)
        session.events.append("session.started", normalize_event_payload(session.to_dict()))
        if session.mode == "normal":
            try:
                cleanup = await session_cleanup_summary(session)
            except Exception:
                logger.exception("Failed to load web session cleanup state")
                cleanup = None
            if cleanup and cleanup_blocks_normal_chat(cleanup.get("status")):
                await session.events.publish("cleanup.status", cleanup)
        return JSONResponse(normalize_event_payload(session.to_dict()), status_code=201)

    async def _run_turn_task(session: WebSession, runtime: WebRuntimeProtocol, turn_request: WebTurnRequest) -> None:
        initial_event_floor = session.events.latest_sequence
        tracked_turn_ids = {turn_request.turn_id} if turn_request.turn_id is not None else set()

        def record_consumed_input(event: dict[str, Any]) -> None:
            if event.get("type") != "user.message":
                return
            payload = event.get("payload")
            published_turn_id = payload.get("turnId") if isinstance(payload, dict) else None
            if isinstance(published_turn_id, str) and published_turn_id:
                session.consumed_turn_ids.add(published_turn_id)

        async def start_observed_turn(request: WebTurnRequest) -> dict[str, Any]:
            if request.turn_id is not None:
                tracked_turn_ids.add(request.turn_id)
            with observe_published_events(record_consumed_input):
                turn_result = await runtime.start_turn(request)
            if (
                request.turn_id is not None
                and isinstance(turn_result, dict)
                and turn_result.get("inputConsumed") is True
            ):
                session.consumed_turn_ids.add(request.turn_id)
            return turn_result

        try:
            result = await start_observed_turn(turn_request)
            if not (isinstance(result, dict) and result.get("accepted")):
                restore_unpublished_turn_input(
                    session,
                    turn_id=turn_request.turn_id,
                    text=turn_request.text,
                    image_ids=turn_request.image_ids,
                    file_refs=turn_request.file_refs,
                    source=turn_request.source,
                    after_sequence=initial_event_floor,
                    input_consumed=result.get("inputConsumed") if isinstance(result, dict) else None,
                )
            # 排队消息逐条、各自独立成 turn 依次处理(用户要求「一条条发」),而不是在本轮
            # mid-turn 一次性全部注入。每弹出一条即由 pop_next_queued_input 发
            # queued-input.removed 让前端移除 chip,新 turn 会为其发独立的 user.message 气泡
            # 与 turn.done。若某轮未正常完成(中断/出错/被拒,accepted=False),停止继续排空,
            # 剩余消息保留在队列中,避免丢失或被误当作新一轮执行。
            while isinstance(result, dict) and result.get("accepted"):
                next_text = manager.pop_next_queued_input(session)
                if next_text is None:
                    break
                queued_event_floor = session.events.latest_sequence
                queued_turn_id = uuid.uuid4().hex
                try:
                    result = await start_observed_turn(
                        WebTurnRequest(
                            text=next_text,
                            image_ids=[],
                            file_refs=[],
                            source="queued",
                            turn_id=queued_turn_id,
                            model_selection=active_model_selection(session),
                        )
                    )
                except BaseException:
                    restore_unpublished_turn_input(
                        session,
                        turn_id=queued_turn_id,
                        text=next_text,
                        image_ids=[],
                        file_refs=[],
                        source="queued",
                        after_sequence=queued_event_floor,
                    )
                    raise
                if not (isinstance(result, dict) and result.get("accepted")):
                    restore_unpublished_turn_input(
                        session,
                        turn_id=queued_turn_id,
                        text=next_text,
                        image_ids=[],
                        file_refs=[],
                        source="queued",
                        after_sequence=queued_event_floor,
                        input_consumed=result.get("inputConsumed") if isinstance(result, dict) else None,
                    )
        except asyncio.CancelledError:
            restore_unpublished_turn_input(
                session,
                turn_id=turn_request.turn_id,
                text=turn_request.text,
                image_ids=turn_request.image_ids,
                file_refs=turn_request.file_refs,
                source=turn_request.source,
                after_sequence=initial_event_floor,
            )
            raise
        except Exception as exc:
            restore_unpublished_turn_input(
                session,
                turn_id=turn_request.turn_id,
                text=turn_request.text,
                image_ids=turn_request.image_ids,
                file_refs=turn_request.file_refs,
                source=turn_request.source,
                after_sequence=initial_event_floor,
            )
            await session.events.publish(
                "error",
                {
                    "turnId": turn_request.turn_id,
                    "message": public_exception_message(exc),
                    "retryable": False,
                },
            )
            await session.events.publish(
                "turn.done",
                {
                    "turnId": turn_request.turn_id,
                    "interrupted": False,
                    "canceled": False,
                    "failed": True,
                },
            )
        finally:
            current_task = asyncio.current_task()
            if session.active_turn_task is current_task:
                session.active_turn_task = None
            session.consumed_turn_ids.difference_update(tracked_turn_ids)

    async def post_message(request):
        session_id = request.path_params["session_id"]
        session = manager.get_session(session_id)
        if session is None:
            return json_error(_("session not found"), 404)
        if (read_only := foreign_read_only_response(session)) is not None:
            return read_only
        if (archived := session_archived_response(session)) is not None:
            return archived
        try:
            data = await json_object_body(request)
            text = string_with_default(data, "text")
            image_ids = optional_string_list(data, "imageIds")
            file_refs = optional_string_list(data, "fileRefs")
            if not text.strip() and not image_ids and not file_refs:
                raise ValueError(_("message text, image, or file is required"))
        except ValueError as exc:
            return json_error(str(exc), 400)

        if (capability_error := image_capability_error(session, image_ids)) is not None:
            return capability_error
        try:
            validate_turn_attachments(session, image_ids, file_refs)
        except (FileNotFoundError, ValueError) as exc:
            return json_error(str(exc), 400)

        # 兜底:若进程在交接后重启、模式仍停留在 pipeline,收到普通输入时据快照翻转为 normal,
        # 让本条输入直接落到下面的普通路径(Issue 4 的重启场景)。
        if session.mode == "pipeline" and await maybe_switch_session_to_normal(session):
            await session.events.publish(
                "session.updated",
                {"mode": session.mode, "contextId": session.context_id, "taskId": session.task_id},
            )

        if session.mode == "pipeline":
            reservation = await reserve_pipeline_action(session, wait_for_admission=True)
            if reservation is None:
                if manager.get_session(session.web_session_id) is not session:
                    return json_error(_("session not found"), 404)
                if (archived := session_archived_response(session)) is not None:
                    return archived
                return turn_busy_response()
            owns_reservation = True
            try:
                model_selection = active_model_selection(session)
                if (
                    capability_error := image_capability_error(
                        session,
                        image_ids,
                        model_selection=model_selection,
                    )
                ) is not None:
                    release_pipeline_action_reservation(session, reservation)
                    owns_reservation = False
                    return capability_error
                owns_reservation = False
                turn_id = await start_background_pipeline_turn(
                    session,
                    text=text,
                    image_ids=image_ids,
                    file_refs=file_refs,
                    model_selection=model_selection,
                    reservation=reservation,
                )
            except TurnReservationCanceledError:
                return JSONResponse(
                    {
                        "accepted": False,
                        "reason": "turn canceled",
                        "canceled": True,
                        "interrupted": True,
                    },
                    status_code=409,
                )
            finally:
                if owns_reservation:
                    release_pipeline_action_reservation(session, reservation)
            return JSONResponse(
                {
                    "accepted": True,
                    "turnId": turn_id,
                    "mode": "pipeline",
                    "contextId": session.context_id,
                    "taskId": session.task_id,
                },
                status_code=202,
            )

        reservation = await reserve_agent_turn(session)
        if reservation is None:
            if manager.get_session(session.web_session_id) is not session:
                return json_error(_("session not found"), 404)
            if (archived := session_archived_response(session)) is not None:
                return archived
            return turn_busy_response()
        _reserved, placeholder = reservation
        owns_reservation = True
        try:
            model_selection = active_model_selection(session)
            if (
                capability_error := image_capability_error(
                    session,
                    image_ids,
                    model_selection=model_selection,
                )
            ) is not None:
                release_agent_turn_reservation(session, placeholder)
                owns_reservation = False
                return capability_error
            cleanup = await session_cleanup_summary(session)
            if cleanup_blocks_normal_chat(cleanup.get("status")):
                session.draft = text
                await session.events.publish(
                    "cleanup.status",
                    {
                        **cleanup,
                        "blockedInputPreserved": True,
                    },
                )
                await session.events.publish(
                    "draft.updated",
                    {
                        "draft": text,
                        "reason": "cleanup_blocks_normal_chat",
                    },
                )
                release_agent_turn_reservation(session, placeholder)
                owns_reservation = False
                return JSONResponse(
                    {
                        "accepted": False,
                        "reason": "cleanup_blocks_normal_chat",
                        "cleanup": cleanup,
                        "draft": text,
                    },
                    status_code=409,
                )

            turn_id = await start_background_turn(
                session,
                text=text,
                image_ids=image_ids,
                file_refs=file_refs,
                model_selection=model_selection,
                reservation=reservation,
            )
            owns_reservation = False
            return JSONResponse(
                {
                    "accepted": True,
                    "turnId": turn_id,
                },
                status_code=202,
            )
        except asyncio.CancelledError:
            if owns_reservation:
                release_agent_turn_reservation(session, placeholder)
            raise
        except TurnReservationCanceledError:
            return JSONResponse(
                {
                    "accepted": False,
                    "reason": "turn canceled",
                    "canceled": True,
                    "interrupted": True,
                },
                status_code=409,
            )
        except Exception:
            if owns_reservation:
                release_agent_turn_reservation(session, placeholder)
            raise

    async def post_image(request):
        request_lifecycle_epoch = manager.session_lifecycle_epoch
        scoped_session_ref = request.path_params.get("session_id")
        scoped_session = manager.get_session(scoped_session_ref) if scoped_session_ref else None
        if scoped_session_ref and scoped_session is None:
            return json_error(_("session not found"), 404)
        try:
            data = await json_object_body(request)
            session_id = scoped_session_ref or required_string(data, "sessionId")
            media_type = required_string(data, "mediaType")
            image_data_base64 = required_string(data, "data")
            reject_obviously_oversized_image_payload(image_data_base64)
            image_bytes = base64.b64decode(image_data_base64, validate=True)
        except (ValueError, binascii.Error) as exc:
            return json_error(str(exc), 400)
        session = scoped_session or manager.get_session(session_id)
        if session is None:
            return json_error(_("session not found"), 404)
        if scoped_session is None and manager.session_reference_mutated_since(
            session_id, session, request_lifecycle_epoch
        ):
            return json_error(_("session not found"), 404)
        async with session.turn_admission_lock:
            if manager.get_session(session.web_session_id) is not session:
                return json_error(_("session not found"), 404)
            if (read_only := foreign_read_only_response(session)) is not None:
                return read_only
            if (archived := session_archived_response(session)) is not None:
                return archived
            supports_images, model = active_model_supports_images(session)
            if not supports_images:
                return json_error(
                    _("Current model {} does not support image input.").format(model),
                    400,
                )

            from iac_code.web.images import store_cached_image

            image_id = uuid.uuid4().hex
            try:
                cached_image = store_cached_image(
                    image_id,
                    image_bytes,
                    media_type=media_type,
                    cwd=session.cwd,
                    session_id=session.session_id,
                )
            except ValueError as exc:
                return json_error(str(exc), 400)
            except OSError as exc:
                return json_error(str(exc), 507)
        return JSONResponse(
            image_upload_payload(cached_image, session.web_session_id),
            status_code=201,
        )

    async def get_cached_image(request):
        image_id = request.path_params["image_id"]
        session_id = query_string(request, "sessionId", "")
        if not session_id:
            return json_error(_("sessionId is required"), 400)
        session = manager.get_session(session_id)
        if session is None:
            return json_error(_("image not found"), 404)

        from iac_code.web.images import load_cached_image

        try:
            image = load_cached_image(image_id, cwd=session.cwd, session_id=session.session_id)
        except (FileNotFoundError, ValueError):
            return json_error(_("image not found"), 404)
        return Response(image.data, media_type=image.media_type)

    async def get_messages(request):
        session_id = request.path_params["session_id"]
        session = manager.get_session(session_id)
        if session is None:
            return json_error(_("session not found"), 404)
        transcript = manager.load_visible_transcript(session.session_id, cwd=session.cwd)
        # 服务器重启后 buffer 从序号 1 重新计数,而前端把存储转录里的可见行按位置重排为
        # 1..N。若不播种,流水线恢复后补发的实时事件(如 step 标记)会拿到低序号排到存储行
        # 之上(Issue 3:step5 排到 step4 上方)。以可见行数播种,令后续实时事件序号 > N。
        session.events.ensure_sequence_above(len(transcript.get("messages", [])))
        return JSONResponse(transcript)

    async def get_outputs(request):
        session_id = request.path_params["session_id"]
        session = manager.get_session(session_id)
        if session is None:
            return json_error(_("session not found"), 404)
        # 注入协调器在途优化集:优化进度态在前端易失(resync 清空),后端权威 optimizing 标志让
        # 正在优化的候选徽标跨 resync 保持「优化中」,不再倒退成「待优化」。
        optimizing = frozenset(
            diagram_optimization_coordinator.optimizing_indices(getattr(session, "context_id", None))
        )
        return JSONResponse(outputs_payload(manager, session, optimizing))

    async def get_output_file(request):
        session_id = request.path_params["session_id"]
        session = manager.get_session(session_id)
        if session is None:
            return json_error(_("session not found"), 404)
        path = query_string(request, "path", "")
        if not path:
            return json_error(_("path required"), 400)
        # 只放行面板已派生的输出文件(可能在 cwd 之外,如 /tmp),不放开任意路径穿越。
        allowed = {entry["path"] for entry in outputs_payload(manager, session)["files"]}
        try:
            return JSONResponse(read_output_file(session, path, allowed_paths=allowed))
        except OutputPathForbidden:
            return json_error(_("forbidden"), 403)
        except OutputFileMissing:
            return json_error(_("file not found"), 404)

    async def get_file_search(request):
        session_id = query_string(request, "sessionId", "")
        session = manager.get_session(session_id) if session_id else None
        if session is None:
            return json_error(_("session not found"), 404)
        try:
            query = query_string(request, "q", "")
            limit = query_int(request, "limit")
            context = query_int(request, "context")
            from iac_code.web.files import search_files

            return JSONResponse({"results": search_files(session.cwd, query, limit=limit, context=context)})
        except ValueError as exc:
            return json_error(str(exc), 400)

    async def get_file_quick_open(request):
        session_id = query_string(request, "sessionId", "")
        session = manager.get_session(session_id) if session_id else None
        if session is None:
            return json_error(_("session not found"), 404)
        try:
            query = query_string(request, "q", "")
            limit = query_int(request, "limit")
            from iac_code.web.files import quick_open_files

            return JSONResponse({"files": quick_open_files(session.cwd, query, limit=limit)})
        except ValueError as exc:
            return json_error(str(exc), 400)

    async def get_history_search(request):
        try:
            query = query_string(request, "q", "")
            limit = query_int(request, "limit")
            session_id = query_string(request, "sessionId", "")
            from iac_code.web.files import search_input_history, search_visible_user_history

            entries = search_input_history(query, limit=limit)
            if session_id:
                session = manager.get_session(session_id)
                if session is None:
                    return json_error(_("session not found"), 404)
                entries.extend(
                    search_visible_user_history(
                        manager.load_visible_messages(session.session_id, cwd=session.cwd),
                        query,
                        session_id=session.web_session_id,
                        limit=limit,
                    )
                )
            return JSONResponse({"entries": entries})
        except ValueError as exc:
            return json_error(str(exc), 400)

    async def get_transcript(request):
        session_id = query_string(request, "sessionId", "")
        session = manager.get_session(session_id) if session_id else None
        if session is None:
            return json_error(_("session not found"), 404)
        from iac_code.web.files import transcript_for_identifier

        identifier = request.path_params["turn_id"]
        transcript = transcript_for_identifier(
            identifier,
            visible_messages=manager.load_visible_messages(session.session_id, cwd=session.cwd),
            resume_messages=manager.load_resume_messages(session.session_id, cwd=session.cwd),
        )
        if transcript is None:
            return json_error(_("transcript not found"), 404)
        return JSONResponse(transcript)

    async def list_sessions(request):
        try:
            limit = query_int(request, "limit", DEFAULT_SESSION_LIST_LIMIT)
            project_limit = query_int(request, "projectLimit", DEFAULT_PROJECT_LIST_LIMIT)
            per_project_limit = query_int(request, "perProjectLimit", DEFAULT_PROJECT_SESSION_LIMIT)
        except ValueError as exc:
            return json_error(str(exc), 400)
        if limit is None:
            limit = DEFAULT_SESSION_LIST_LIMIT
        limit = min(limit, MAX_SESSION_LIST_LIMIT)
        if per_project_limit is None:
            per_project_limit = DEFAULT_PROJECT_SESSION_LIMIT
        if project_limit is not None:
            project_limit = min(project_limit, MAX_PROJECT_LIST_LIMIT)
        per_project_limit = min(per_project_limit, MAX_PROJECT_SESSION_LIMIT)

        cwd = query_string(request, "cwd", "").strip()
        if cwd:
            sessions, total = manager.list_project_sessions(cwd, limit=limit)
            return JSONResponse(
                {
                    "sessions": [session.to_dict() for session in sessions],
                    "total": total,
                    "limit": limit,
                    "hasMore": total > len(sessions),
                },
                headers={"Cache-Control": "no-store"},
            )

        # 一次请求内复用同一份全项目扫描:这 4 个方法否则各自全量 stat + 解析所有会话文件,
        # 把 3~4 次全量扫描压到一次。冷启动首屏收益最大,稳态下也随会话数线性省一截。
        with manager.batch_reads():
            visible_sessions, _raw_total = manager.list_sessions_page(limit=None)
            sessions = visible_sessions[:limit]
            total = len(visible_sessions)
            pinned_sessions = manager.list_pinned_sessions()
            projects, project_total, total_project_sessions = manager.list_session_projects(
                project_limit=project_limit,
                per_project_limit=per_project_limit,
            )
            pinned_projects = manager.list_pinned_projects(per_project_limit=per_project_limit)

        def project_payload(project: dict[str, Any]) -> dict[str, Any]:
            return {
                "cwd": project["cwd"],
                "label": project.get("label"),
                "sessions": [session.to_dict() for session in project["sessions"]],
                "total": project["total"],
                "hasMore": bool(project.get("hasMore")),
                "pinned": bool(project.get("pinned")),
                "pinnedAt": project.get("pinnedAt"),
                "archived": bool(project.get("archived")),
                "collapsed": bool(project.get("collapsed")),
            }

        return JSONResponse(
            {
                "sessions": [session.to_dict() for session in sessions],
                "total": total,
                "limit": limit,
                "hasMore": total > len(sessions),
                "pinnedSessions": [session.to_dict() for session in pinned_sessions],
                "pinnedProjects": [project_payload(project) for project in pinned_projects],
                "projects": [project_payload(project) for project in projects],
                "projectTotal": project_total,
                "projectLimit": project_limit,
                "perProjectLimit": per_project_limit,
                "totalProjectSessions": total_project_sessions,
            },
            headers={"Cache-Control": "no-store"},
        )

    async def list_archived_sessions(_request):
        def archived_project_payload(project: dict[str, Any]) -> dict[str, Any]:
            return {
                "cwd": project["cwd"],
                "label": project.get("label"),
                "sessions": [session.to_dict() for session in project["sessions"]],
                "total": project["total"],
                "hasMore": bool(project.get("hasMore")),
                "pinned": bool(project.get("pinned")),
                "pinnedAt": project.get("pinnedAt"),
                "archived": bool(project.get("archived")),
                "collapsed": bool(project.get("collapsed")),
            }

        # list_archived_projects 同样全量扫描并逐会话读 settings.yml;放进 batch_reads 窗口
        # 复用索引快照与 foreign_hidden 缓存,避免「已归档会话」面板的秒级加载。
        with manager.batch_reads():
            archived = manager.list_archived_projects()
        return JSONResponse(
            {"projects": [archived_project_payload(project) for project in archived]},
            headers={"Cache-Control": "no-store"},
        )

    async def delete_archived_sessions_route(request):
        cwd = query_string(request, "cwd", "").strip() or None
        deleted = 0
        for project in manager.list_archived_projects():
            if cwd is not None and project["cwd"] != cwd:
                continue
            for session in list(project["sessions"]):
                if manager.is_session_read_only(session):
                    continue
                result = await delete_session_if_idle(session)
                if result:
                    deleted += 1
        return JSONResponse({"deleted": deleted})

    async def search_sessions_route(request):
        query = query_string(request, "q", "")
        try:
            limit = query_int(request, "limit", 50)
        except ValueError as exc:
            return json_error(str(exc), 400)
        if limit is None:
            limit = 50
        limit = min(limit, MAX_SESSION_LIST_LIMIT)
        include_archived = query_string(request, "archived", "").strip().lower() == "true"
        # 一次搜索里 search_sessions 会对每个会话调用 _foreign_hidden(逐会话重读 settings.yml)
        # 并多次全量扫描;放进 batch_reads 窗口复用索引快照与 foreign_hidden 缓存,压掉逐键延迟。
        with manager.batch_reads():
            results, total = manager.search_sessions(query, limit=limit, include_archived=include_archived)
        return JSONResponse(
            {"results": results, "total": total, "limit": limit},
            headers={"Cache-Control": "no-store"},
        )

    async def delete_session_route(request):
        session_id = request.path_params["session_id"]
        session = manager.get_session(session_id)
        if session is None:
            return JSONResponse({"deleted": False, "sessionId": session_id}, status_code=404)
        if (read_only := foreign_read_only_response(session)) is not None:
            return read_only
        deleted = await delete_session_if_idle(session)
        if deleted is None:
            return JSONResponse(
                {"deleted": False, "sessionId": session.session_id, "reason": "turn already running"},
                status_code=409,
            )
        return JSONResponse({"deleted": deleted, "sessionId": session_id}, status_code=200)

    async def get_session(_request):
        session_id = _request.path_params["session_id"]
        session = manager.get_session(session_id)
        if session is None:
            return json_error(_("session not found"), 404)
        # 切换会话时补齐系统提示 + 工具定义开销(重启后首个实时回合前 /status 会少算这约 13k),
        # 令切换即刻显示的用量与 composer 圆环口径一致;仅在开销未知时建一次 runtime,失败降级为 0。
        await prime_session_context_overhead(session, manager)
        return JSONResponse(session_payload(session))

    async def patch_session(request):
        session_id = request.path_params["session_id"]
        session = manager.get_session(session_id)
        if session is None:
            return json_error(_("session not found"), 404)
        try:
            data = await json_object_body(request)
            supported_fields = {"title", "name", "debugEnabled", "allowUserEscapes", "pinned", "archived"}
            unsupported_fields = sorted(set(data) - supported_fields)
            if unsupported_fields:
                raise ValueError("unsupported session fields: {}".format(", ".join(unsupported_fields)))
            title = None
            if "title" in data or "name" in data:
                field = "title" if "title" in data else "name"
                title = required_string(data, field)
            debug_enabled = None
            if "debugEnabled" in data:
                debug_enabled = data["debugEnabled"]
                if not isinstance(debug_enabled, bool):
                    raise ValueError(_("debugEnabled must be a boolean"))
            allow_user_escapes = optional_bool_map(data, "allowUserEscapes")
            pinned = data.get("pinned") if "pinned" in data else None
            if pinned is not None and not isinstance(pinned, bool):
                raise ValueError(_("pinned must be a boolean"))
            archived = data.get("archived") if "archived" in data else None
            if archived is not None and not isinstance(archived, bool):
                raise ValueError(_("archived must be a boolean"))
        except ValueError as exc:
            return json_error(str(exc), 400)
        if manager.is_session_read_only(session) and set(data) - {"pinned", "archived"}:
            return foreign_read_only_response(session)

        def apply_session_updates() -> None:
            if title is not None:
                manager.rename_session(session, title)
            if debug_enabled is not None:
                manager.toggle_debug(session, enabled=debug_enabled)
            if allow_user_escapes is not None:
                apply_allow_user_escapes_update(session, allow_user_escapes)
            if pinned is not None:
                session.pinned = pinned
                session.pinned_at = _iso_now() if pinned else None
            if archived is not None:
                session.archived = archived
                if archived:
                    session.pinned = False
                    session.pinned_at = None
            manager.persist_web_metadata(session)

        try:
            async with session.turn_admission_lock:
                if manager.get_session(session.web_session_id) is not session:
                    return json_error(_("session not found"), 404)
                if archived is True and (
                    active_session_work_running(session)
                    or session.pending_permissions
                    or session.pending_questions
                    or session.pending_elicitations
                ):
                    return json_error(
                        "session has work in progress",
                        409,
                        code="session_busy",
                    )
                apply_session_updates()
        except ValueError as exc:
            return json_error(str(exc), 400)
        return JSONResponse(normalize_event_payload(session.to_dict()))

    async def put_session_permission_mode(request):
        session_id = request.path_params["session_id"]
        session = manager.get_session(session_id)
        if session is None:
            return json_error(_("session not found"), 404)
        if (read_only := foreign_read_only_response(session)) is not None:
            return read_only
        try:
            data = await json_object_body(request)
            mode = required_string(data, "mode")
            async with session.turn_admission_lock:
                if manager.get_session(session.web_session_id) is not session:
                    return json_error(_("session not found"), 404)
                manager.set_permission_mode(session, mode)
                manager.persist_web_metadata(session)
        except ValueError as exc:
            return json_error(str(exc), 400)
        return JSONResponse(normalize_event_payload(session.to_dict()))

    async def put_session_thinking_enabled(request):
        session_id = request.path_params["session_id"]
        session = manager.get_session(session_id)
        if session is None:
            return json_error(_("session not found"), 404)
        if (read_only := foreign_read_only_response(session)) is not None:
            return read_only
        try:
            data = await json_object_body(request)
            if "enabled" not in data:
                raise ValueError(_("enabled is required"))
            raw = data["enabled"]
            if raw is not None and not isinstance(raw, bool):
                raise ValueError(_("enabled must be a boolean or null"))
            async with session.turn_admission_lock:
                if manager.get_session(session.web_session_id) is not session:
                    return json_error(_("session not found"), 404)
                manager.set_thinking_enabled(session, raw)
                manager.persist_web_metadata(session)
        except ValueError as exc:
            return json_error(str(exc), 400)
        return JSONResponse(normalize_event_payload(session.to_dict()))

    async def put_session_model(request):
        session_id = request.path_params["session_id"]
        session = manager.get_session(session_id)
        if session is None:
            return json_error(_("session not found"), 404)
        if (read_only := foreign_read_only_response(session)) is not None:
            return read_only
        try:
            data = await json_object_body(request)
            provider = required_string(data, "provider")
            model = required_string(data, "model")
            effort = optional_string(data, "effort")
            async with session.turn_admission_lock:
                if manager.get_session(session.web_session_id) is not session:
                    return json_error(_("session not found"), 404)
                manager.set_session_model(session, provider=provider, model=model, effort=effort)
        except ValueError as exc:
            return json_error(str(exc), 400)
        return JSONResponse(normalize_event_payload(session.to_dict()))

    async def delete_session_model(request):
        session_id = request.path_params["session_id"]
        session = manager.get_session(session_id)
        if session is None:
            return json_error(_("session not found"), 404)
        if (read_only := foreign_read_only_response(session)) is not None:
            return read_only
        async with session.turn_admission_lock:
            if manager.get_session(session.web_session_id) is not session:
                return json_error(_("session not found"), 404)
            manager.clear_session_model(session)
        return JSONResponse(normalize_event_payload(session.to_dict()))

    async def patch_project(request):
        try:
            data = await json_object_body(request)
            cwd = required_string(data, "cwd")
            supported_fields = {"cwd", "pinned", "archived", "hidden", "collapsed", "label"}
            unsupported_fields = sorted(set(data) - supported_fields)
            if unsupported_fields:
                raise ValueError("unsupported project fields: {}".format(", ".join(unsupported_fields)))
            kwargs: dict[str, Any] = {}
            for flag in ("pinned", "archived", "hidden", "collapsed"):
                if flag in data:
                    if not isinstance(data[flag], bool):
                        raise ValueError("{} must be a boolean".format(flag))
                    kwargs[flag] = data[flag]
            if "label" in data:
                label = data["label"]
                if label is None or (isinstance(label, str) and not label.strip()):
                    kwargs["clear_label"] = True
                elif isinstance(label, str):
                    kwargs["label"] = label
                else:
                    raise ValueError(_("label must be a string"))
        except ValueError as exc:
            return json_error(str(exc), 400)
        metadata = manager.update_project_metadata(cwd, **kwargs)
        return JSONResponse({"cwd": cwd, **metadata})

    async def reveal_project(request):
        try:
            data = await json_object_body(request)
            cwd = required_string(data, "cwd")
        except ValueError as exc:
            return json_error(str(exc), 400)
        target = Path(cwd).expanduser()
        if not target.is_dir():
            return json_error(_("project directory is not available on disk"), 404)
        try:
            import subprocess
            import sys

            if sys.platform == "darwin":
                command = ["open", str(target)]
            elif os.name == "nt":
                command = ["explorer", str(target)]
            else:
                command = ["xdg-open", str(target)]
            subprocess.Popen(command, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        except OSError as exc:
            return json_error(_("failed to reveal project: {}").format(exc), 500)
        return JSONResponse({"cwd": cwd, "revealed": True})

    async def archive_project_sessions_route(request):
        try:
            data = await json_object_body(request)
            cwd = required_string(data, "cwd")
        except ValueError as exc:
            return json_error(str(exc), 400)
        archived = await manager.archive_project_sessions(cwd)
        return JSONResponse({"cwd": cwd, "archived": archived})

    def latest_error_payload(session: WebSession) -> dict[str, Any] | None:
        for event in reversed(session.events.replay_after(0)):
            if event.get("type") != "error":
                continue
            payload = event.get("payload")
            return dict(payload) if isinstance(payload, dict) else {}
        return None

    def latest_debug_payload(session: WebSession) -> dict[str, Any] | None:
        for event in reversed(session.events.replay_after(0)):
            payload = event.get("payload")
            if not isinstance(payload, dict):
                continue
            if event.get("type") == "debug.stream_event":
                return dict(payload)
            if event.get("type") == "session.updated":
                if "debugEnabled" in payload:
                    return {"debugEnabled": payload["debugEnabled"]}
                if "debug" in payload:
                    return {"debugEnabled": payload["debug"]}
        return None

    async def pipeline_recovery_payload(session: WebSession) -> dict[str, Any]:
        display_replay = manager.load_pipeline_display_replay(session.session_id, cwd=session.cwd)
        if not session.context_id and not session.task_id:
            if not display_replay:
                return {}
            return normalize_event_payload(
                {
                    "pipelineName": display_replay.get("pipelineName") or session.pipeline_name,
                    "displayReplay": display_replay,
                }
            )
        try:
            state = await pipeline_state_from_query(
                {
                    "contextId": session.context_id or "",
                    "taskId": session.task_id or "",
                }
            )
        except Exception:
            return {}
        snapshot = state.get("snapshot") if isinstance(state, dict) else None
        if not isinstance(snapshot, dict):
            return {}
        control = snapshot.get("control") if isinstance(snapshot.get("control"), dict) else {}
        display = snapshot.get("display") if isinstance(snapshot.get("display"), dict) else {}
        waiting_input = (
            snapshot.get("waitingInput")
            or control.get("waitingInput")
            or control.get("inputRequired")
            or _last_list_item(control.get("inputHistory"))
        )
        handoff = snapshot.get("handoff") or control.get("handoff") or _last_list_item(control.get("handoffHistory"))
        cleanup = snapshot.get("cleanup") if isinstance(snapshot.get("cleanup"), dict) else None
        return normalize_event_payload(
            {
                "pipelineName": snapshot.get("pipelineName") or session.pipeline_name,
                "pipelineRunId": snapshot.get("pipelineRunId") or snapshot.get("contextId") or session.context_id,
                "contextId": snapshot.get("contextId") or session.context_id,
                "taskId": snapshot.get("taskId") or session.task_id,
                "lastSequence": snapshot.get("lastSequence"),
                "currentStep": snapshot.get("currentStep") or control.get("currentStep"),
                "candidate": snapshot.get("candidate") or control.get("candidate"),
                "waitingInput": waiting_input,
                "cleanupStatus": cleanup.get("status") if cleanup else None,
                "cleanup": cleanup,
                "handoffStatus": _status_from_mapping(handoff),
                "handoff": handoff,
                "warningHistory": snapshot.get("warningHistory")
                or control.get("warningHistory")
                or display.get("warningHistory")
                or [],
                "rollbackHistory": snapshot.get("rollbackHistory") or control.get("rollbackHistory") or [],
                "candidateRestarts": snapshot.get("candidateRestarts") or control.get("candidateRestarts") or [],
                "snapshot": snapshot,
                "displayReplay": display_replay,
            }
        )

    def _last_list_item(value: Any) -> Any:
        return value[-1] if isinstance(value, list) and value else None

    def _status_from_mapping(value: Any) -> str | None:
        if isinstance(value, dict):
            status = value.get("status")
            return status if isinstance(status, str) else None
        return None

    def merge_pipeline_recovery(base: dict[str, Any], recovered: dict[str, Any]) -> dict[str, Any]:
        if not recovered:
            return base
        merged = dict(base)
        for key, value in recovered.items():
            if value is not None:
                merged[key] = value
        return merged

    def session_payload(session: WebSession) -> dict[str, Any]:
        payload = session.to_dict()
        status_payload = manager.status(session)
        payload["contextUsage"] = status_payload.get("contextUsage", {})
        display_replay = manager.load_pipeline_display_replay(session.session_id, cwd=session.cwd)
        if display_replay:
            pipeline = dict(payload.get("pipeline", {}))
            pipeline["displayReplay"] = display_replay
            pipeline["pipelineName"] = pipeline.get("pipelineName") or display_replay.get("pipelineName")
            payload["pipeline"] = pipeline
            payload["mode"] = "pipeline"
        return normalize_event_payload(payload)

    def provider_prompt_sections(status_payload: dict[str, Any]) -> list[dict[str, Any]]:
        active = status_payload.get("activeProvider")
        if isinstance(active, dict):
            summary = provider_summary_for_recovery(active)
            return [summary] if summary is not None else []
        return []

    def provider_summary_for_recovery(active: Any) -> dict[str, Any] | None:
        if not isinstance(active, dict):
            return None
        return {
            "provider": active.get("provider"),
            "model": active.get("model"),
            "effort": active.get("effort"),
            "apiBase": active.get("apiBase"),
            "credentialConfigured": bool(active.get("hasApiKey")),
        }

    def tool_definition_sections() -> list[dict[str, Any]]:
        return [
            {
                "name": command["name"],
                "description": command["description"],
                "source": "web-command",
            }
            for command in command_metadata()
        ]

    def memory_sections_for_prompt(session: WebSession) -> list[dict[str, Any]]:
        try:
            payload = memory_payload(Path(session.cwd))
        except Exception:
            return []
        sections: list[dict[str, Any]] = []
        for scope in ("project", "user"):
            item = payload.get(scope)
            if isinstance(item, dict) and (item.get("content") or item.get("path")):
                sections.append({"scope": scope, **item})
        sections.append({"scope": "auto", "enabled": payload.get("autoMemoryEnabled")})
        legacy = payload.get("legacy")
        if isinstance(legacy, list) and legacy:
            sections.append({"scope": "legacy", "memories": legacy})
        return sections

    def cleanup_prompt_summary_from_pipeline(pipeline: dict[str, Any]) -> dict[str, Any]:
        cleanup = pipeline.get("cleanup")
        if not isinstance(cleanup, dict):
            return {"available": False, "status": "none"}
        return {
            "available": True,
            "status": cleanup.get("status") or "unknown",
            "resourceCount": cleanup.get("resourceCount"),
            "resources": cleanup.get("resources") if isinstance(cleanup.get("resources"), list) else [],
            "prompt": cleanup.get("prompt"),
        }

    async def prompt_snapshot(session: WebSession) -> dict[str, Any]:
        visible_messages = manager.load_visible_messages(session.session_id, cwd=session.cwd)
        resume_messages = manager.load_resume_messages(session.session_id, cwd=session.cwd)
        status_payload = manager.status(session)
        pipeline = merge_pipeline_recovery(
            dict(session.to_dict().get("pipeline", {})),
            await pipeline_recovery_payload(session),
        )
        return normalize_event_payload(
            {
                "sessionId": session.session_id,
                "webSessionId": session.web_session_id,
                "redacted": False,
                "available": True,
                "mode": session.mode,
                "cwd": session.cwd,
                "title": session.title,
                "status": session.status,
                "debugEnabled": session.debug_enabled,
                "provider": status_payload.get("provider"),
                "model": status_payload.get("model"),
                "effort": status_payload.get("effort"),
                "activeProvider": status_payload.get("activeProvider"),
                "cloud": status_payload.get("cloud"),
                "pipeline": pipeline,
                "sources": {
                    "normal": {
                        "sessionId": session.session_id,
                        "messageCount": len(resume_messages),
                    },
                    "pipeline": pipeline,
                },
                "systemSections": [
                    {
                        "id": "session",
                        "title": "Session",
                        "content": "mode={}; cwd={}".format(session.mode, session.cwd),
                    }
                ],
                "providerMessages": provider_prompt_sections(status_payload),
                "toolDefinitions": tool_definition_sections(),
                "memorySections": memory_sections_for_prompt(session),
                "cleanupPromptSummary": cleanup_prompt_summary_from_pipeline(pipeline),
                "messageCounts": {
                    "visible": len(visible_messages),
                    "resume": len(resume_messages),
                },
                "visibleMessages": visible_messages[-20:],
                "latestSequence": session.events.latest_sequence,
                "currentTurnActive": session.active_turn_task is not None
                and not session.active_turn_task.done()
                or session.turn_lock.locked(),
                "sections": [
                    {
                        "id": "session",
                        "title": "Session",
                        "items": [
                            {"label": "Mode", "value": session.mode},
                            {"label": "Status", "value": session.status},
                            {"label": "Model", "value": status_payload.get("model") or ""},
                            {"label": "Effort", "value": status_payload.get("effort") or ""},
                            {"label": "Visible messages", "value": len(visible_messages)},
                            {"label": "Resume messages", "value": len(resume_messages)},
                        ],
                    },
                    {
                        "id": "pipeline",
                        "title": "Pipeline",
                        "items": [
                            {"label": "Pipeline", "value": session.pipeline_name or ""},
                            {"label": "Context", "value": session.context_id or ""},
                            {"label": "Task", "value": session.task_id or ""},
                        ],
                    },
                ],
            }
        )

    def compaction_payload(result: Any, *, status: str | None = None, reason: str | None = None) -> dict[str, Any]:
        state = status or str(getattr(result, "status", "failed"))
        payload: dict[str, Any] = {
            "accepted": state == "success",
            "state": state,
            "available": True,
            "originalTokens": int(getattr(result, "original_tokens", 0) or 0),
            "compactedTokens": int(getattr(result, "compacted_tokens", 0) or 0),
            "preserveRecentTurns": int(getattr(result, "preserve_recent_turns", 0) or 0),
        }
        if reason:
            payload["reason"] = reason
        return normalize_event_payload(payload)

    async def run_session_compaction(session: WebSession) -> tuple[dict[str, Any] | None, int]:
        # 占用 turn 名额:压缩期间任何并发提交都会被 active_turn_running 判为忙,前端据此把
        # 输入排队(而非新起 turn),压缩完成后再由本函数排空——满足「压缩时提交也要等压缩完成」。
        reservation = await reserve_agent_turn(session)
        if reservation is None:
            if manager.get_session(session.web_session_id) is not session:
                return None, 404
            if session.archived:
                return None, 409
            payload = compaction_payload(None, status="blocked", reason="turn already running")
            await session.events.publish("compaction.finished", payload)
            return payload, 409
        _turn_id, placeholder = reservation
        # 释放准入锁(保留 active_turn_task=placeholder 占位),使排队/查询端点在压缩期间不被阻塞。
        if session.turn_admission_lock.locked():
            session.turn_admission_lock.release()

        await session.events.publish(
            "compaction.started",
            {
                "state": "started",
                "available": True,
            },
        )
        runtime = None
        from iac_code.services.telemetry import use_session_id

        try:
            with use_session_id(session.session_id):
                try:
                    runtime = await create_session_agent_runtime_in_thread(session, manager)
                    result = await runtime.agent_loop.compact()
                    payload = compaction_payload(result)
                except Exception as exc:
                    payload = compaction_payload(None, status="failed", reason=public_exception_message(exc))
                finally:
                    await close_agent_runtime(runtime)
                    await flush_web_telemetry()
        finally:
            release_agent_turn_reservation(session, placeholder)
        await session.events.publish("compaction.finished", payload)
        # 压缩期间排队的输入:压缩完成后以统一准入/恢复路径启动队首，其余由 turn 完成路径继续排空。
        await start_next_queued_turn(session)
        return payload, 202

    async def get_session_prompt(request):
        session_id = request.path_params["session_id"]
        session = manager.get_session(session_id)
        if session is None:
            return json_error(_("session not found"), 404)
        return JSONResponse(await prompt_snapshot(session))

    async def post_session_compact(request):
        session_id = request.path_params["session_id"]
        session = manager.get_session(session_id)
        if session is None:
            return json_error(_("session not found"), 404)
        if (read_only := foreign_read_only_response(session)) is not None:
            return read_only
        if (archived := session_archived_response(session)) is not None:
            return archived
        payload, status_code = await run_session_compaction(session)
        if payload is None:
            if status_code == 404:
                return json_error(_("session not found"), 404)
            return session_archived_response(session) or json_error(
                _("session is archived"),
                409,
                code="session_archived",
            )
        return JSONResponse(payload, status_code=status_code)

    async def get_session_debug(request):
        session_id = request.path_params["session_id"]
        session = manager.get_session(session_id)
        if session is None:
            return json_error(_("session not found"), 404)
        current_turn_active = (
            session.active_turn_task is not None and not session.active_turn_task.done()
        ) or session.turn_lock.locked()
        status_payload = manager.status(session)
        pipeline = merge_pipeline_recovery(
            dict(session.to_dict().get("pipeline", {})),
            await pipeline_recovery_payload(session),
        )
        payload = {
            "sessionId": session.session_id,
            "webSessionId": session.web_session_id,
            "cwd": session.cwd,
            "debugEnabled": session.debug_enabled,
            "mode": session.mode,
            "status": session.status,
            "latestSequence": session.events.latest_sequence,
            "replaySequence": compute_replay_sequence(
                latest_sequence=session.events.latest_sequence,
                floor_sequence=session.events.floor_sequence,
                is_pipeline=session.mode == "pipeline" or session.context_id is not None,
                active_turn=current_turn_active,
                active_turn_floor_sequence=session.active_turn_floor_sequence,
            ),
            "pendingPermissionCount": len(session.pending_permissions),
            "pendingQuestionCount": len(session.pending_questions),
            "pendingElicitationCount": len(session.pending_elicitations),
            "pending": {
                "permissions": len(session.pending_permissions),
                "questions": len(session.pending_questions),
                "elicitations": len(session.pending_elicitations),
                "queuedInputs": len(session.queued_inputs),
            },
            "currentTurnActive": current_turn_active,
            "lastError": latest_error_payload(session),
            "lastDebug": latest_debug_payload(session),
            "pipeline": pipeline,
            "permissionRules": {
                "session": {
                    "allow": [],
                    "deny": [],
                }
            },
            "toolTimeline": [
                event
                for event in session.events.replay_after(0)
                if event.get("type") in {"tool.started", "tool.finished", "tool.result", "local.shell.start"}
            ],
            "providerSummary": provider_summary_for_recovery(status_payload.get("activeProvider")),
            "cloudSummary": status_payload.get("cloud"),
            "messageCounts": status_payload.get("messageCounts"),
            "usage": status_payload.get("usage"),
        }
        return JSONResponse(normalize_event_payload(payload))

    async def get_commands(_request):
        return JSONResponse({"commands": command_metadata()})

    async def get_providers(_request):
        return JSONResponse(providers_payload())

    async def put_provider_config(request):
        try:
            data = await json_object_body(request)
            payload = save_provider_config(data)
        except ValueError as exc:
            return json_error(str(exc), 400)
        return JSONResponse(payload)

    async def delete_provider_config(request):
        try:
            data = await json_object_body(request)
            payload = clear_provider_config(data)
        except ValueError as exc:
            return json_error(str(exc), 400)
        return JSONResponse(payload)

    async def put_active_provider(request):
        try:
            data = await json_object_body(request)
            payload = set_active_provider(data)
        except ValueError as exc:
            return json_error(str(exc), 400)
        return JSONResponse(payload)

    async def get_cloud_aliyun(_request):
        try:
            payload = aliyun_cloud_summary()
        except ValueError as exc:
            return json_error(str(exc), 400)
        return JSONResponse(payload)

    async def put_cloud_aliyun(request):
        try:
            data = await json_object_body(request)
            payload = save_aliyun_cloud(data)
        except ValueError as exc:
            return json_error(str(exc), 400)
        return JSONResponse(payload)

    async def post_cloud_aliyun_oauth_login(request):
        try:
            data = await json_object_body(request)
            payload = await run_in_threadpool(login_aliyun_oauth, data)
        except ValueError as exc:
            return json_error(str(exc), 400)
        return JSONResponse(payload)

    async def get_suggestions(request):
        kind = query_string(request, "kind", "command")
        query = query_string(request, "q", "").strip()
        session_id = query_string(request, "sessionId", "")
        session = manager.get_session(session_id) if session_id else None
        cwd = Path(session.cwd if session is not None else manager.cwd)

        if kind in {"command", "skill"}:
            registry, mcp_status = await dynamic_suggestion_snapshot(session, cwd)
            if kind == "command":
                provider = CommandProvider(registry)
                token = suggestion_token("/", query)
            else:
                provider = SkillProvider(registry)
                token = suggestion_token("$", query)
            items = provider.provide(token)
            if kind == "command":
                # thinking_enabled 在 Web 端改由 composer 工具栏切换按钮承载，从斜杠菜单隐藏。
                items = [item for item in items if item.completion.strip().lstrip("/") != "thinking_enabled"]
            suggestions = [suggestion_to_json(item) for item in items]
            payload: dict[str, Any] = {"suggestions": suggestions[:25]}
            if mcp_status is not None:
                payload["mcpStatus"] = mcp_status
            return JSONResponse(payload)

        if kind == "file":
            token = suggestion_token("@", query)
            providers = [FileProvider(str(cwd)), DirectoryProvider(str(cwd))]
            seen: set[str] = set()
            suggestions = []
            for provider in providers:
                for item in provider.provide(token):
                    if item.completion in seen:
                        continue
                    seen.add(item.completion)
                    suggestions.append(suggestion_to_json(item))
                    if len(suggestions) >= 25:
                        break
                if len(suggestions) >= 25:
                    break
            return JSONResponse({"suggestions": suggestions})

        if kind == "shell":
            suggestions = [
                suggestion_to_json(item) for item in ShellHistoryProvider().provide(suggestion_token("!", query))
            ]
            return JSONResponse({"suggestions": suggestions})

        return json_error(_("suggestion kind is invalid"), 400)

    async def get_memory(request):
        cwd_param = query_string(request, "cwd", "").strip()
        if cwd_param:
            # 项目枚举(known_project_entries)会全量扫描两趟并逐会话读 settings.yml;放进
            # batch_reads 窗口复用索引快照与 foreign_hidden 缓存,压掉「加载项目」的秒级延迟。
            with manager.batch_reads():
                cwd = resolve_project_cwd(cwd_param, Path(manager.cwd), known_project_entries())
            if cwd is None:
                return json_error(_("project not found"), 404)
            return JSONResponse(memory_payload(cwd))
        cwd = cwd_for_session_id(query_string(request, "sessionId", ""))
        if cwd is None:
            return json_error(_("session not found"), 404)
        return JSONResponse(memory_payload(cwd))

    async def get_memory_projects(request):
        # 记忆/插件/MCP 面板的项目选择器共用此端点;known_project_entries 的双趟全量扫描
        # 是首屏「加载项目」慢的主因,故放进 batch_reads 窗口(见 list_sessions/search)。
        with manager.batch_reads():
            projects = memory_projects(Path(manager.cwd), known_project_entries())
        return JSONResponse({"projects": projects})

    async def put_memory_project(request):
        try:
            data = await json_object_body(request)
            content = required_string(data, "content")
            cwd_param = optional_string(data, "cwd") or ""
            session_id = optional_string(data, "sessionId") or ""
        except ValueError as exc:
            return json_error(str(exc), 400)
        if cwd_param.strip():
            cwd = resolve_project_cwd(cwd_param, Path(manager.cwd), known_project_entries())
            if cwd is None:
                return json_error(_("project not found"), 404)
        else:
            cwd = cwd_for_session_id(session_id)
            if cwd is None:
                return json_error(_("session not found"), 404)
        try:
            return JSONResponse(save_project_instruction(cwd, content))
        except OSError as exc:
            return json_error(str(exc), 400)
        except ValueError as exc:
            return json_error(str(exc), 400)

    async def put_memory_user(request):
        try:
            data = await json_object_body(request)
            content = required_string(data, "content")
            session_id = optional_string(data, "sessionId") or ""
        except ValueError as exc:
            return json_error(str(exc), 400)
        if session_id and cwd_for_session_id(session_id) is None:
            return json_error(_("session not found"), 404)
        try:
            return JSONResponse(save_user_instruction(content))
        except OSError as exc:
            return json_error(str(exc), 400)
        except ValueError as exc:
            return json_error(str(exc), 400)

    async def put_memory_auto(request):
        try:
            data = await json_object_body(request)
            enabled = required_bool(data, "enabled")
        except ValueError as exc:
            return json_error(str(exc), 400)
        return JSONResponse(save_auto_memory(enabled))

    async def get_foreign_settings(request):
        return JSONResponse(
            {
                "showPipeline": is_foreign_pipeline_visible(),
                "showNormal": is_foreign_normal_visible(),
            }
        )

    async def put_foreign_settings(request):
        try:
            data = await json_object_body(request)
            show_pipeline = required_bool(data, "showPipeline")
            show_normal = required_bool(data, "showNormal")
        except ValueError as exc:
            return json_error(str(exc), 400)
        return JSONResponse(save_foreign_sessions_visibility(show_pipeline, show_normal))

    async def get_pipeline_review_step_settings(request):
        return JSONResponse(selling_review_step_settings())

    async def put_pipeline_review_step_settings(request):
        try:
            data = await json_object_body(request)
            enabled = required_bool(data, "enabled")
        except ValueError as exc:
            return json_error(str(exc), 400)
        return JSONResponse(save_selling_review_step(enabled))

    async def get_pipeline_review_step_prerequisite(request):
        return JSONResponse(await inspect_review_step_prerequisite())

    async def install_pipeline_review_step_prerequisite(request):
        if install_in_progress():
            return json_error(_("Installation in progress"), 409)

        async def gen():
            async for event in stream_install_review_step_prerequisite():
                yield (json.dumps(event, ensure_ascii=False) + "\n").encode("utf-8")

        return StreamingResponse(
            gen(),
            media_type="application/x-ndjson",
            headers={
                "Cache-Control": "no-cache",
                "X-Accel-Buffering": "no",
            },
        )

    async def get_appearance_settings(request):
        return JSONResponse({"theme": get_appearance_theme()})

    async def put_appearance_settings(request):
        try:
            data = await json_object_body(request)
            theme = required_string(data, "theme")
            return JSONResponse(save_appearance_theme(theme))
        except ValueError as exc:
            return json_error(str(exc), 400)

    async def get_ui_language_settings(request):
        return JSONResponse(ui_language_payload())

    async def put_ui_language_settings(request):
        try:
            data = await json_object_body(request)
            language = required_string(data, "language")
            return JSONResponse(save_ui_language(language))
        except ValueError as exc:
            return json_error(str(exc), 400)

    async def get_session_defaults_settings(request):
        return JSONResponse(get_session_defaults())

    async def put_session_defaults_settings(request):
        try:
            data = await json_object_body(request)
            permission_mode = required_string(data, "permissionMode")
            mode = required_string(data, "mode")
            pipeline_name = optional_string(data, "pipelineName")
            return JSONResponse(save_session_defaults(permission_mode, mode, pipeline_name))
        except ValueError as exc:
            return json_error(str(exc), 400)

    async def get_legacy_memory(request):
        query = query_string(request, "q", "")
        cwd_param = query_string(request, "cwd", "").strip()
        cwd = None
        if cwd_param:
            cwd = resolve_project_cwd(cwd_param, Path(manager.cwd), known_project_entries())
            if cwd is None:
                return json_error(_("project not found"), 404)
        return JSONResponse({"memories": legacy_memory_summaries(query, cwd)})

    async def delete_legacy_memory_route(request):
        memory_id = request.path_params["memory_id"]
        scope = query_string(request, "scope", "global").strip() or "global"
        cwd_param = query_string(request, "cwd", "").strip()
        cwd = None
        if scope == "project":
            if not cwd_param:
                return json_error(_("project not found"), 404)
            cwd = resolve_project_cwd(cwd_param, Path(manager.cwd), known_project_entries())
            if cwd is None:
                return json_error(_("project not found"), 404)
        try:
            deleted = delete_legacy_memory(memory_id, cwd, scope)
        except ValueError as exc:
            return json_error(str(exc), 400)
        status_code = 200 if deleted else 404
        return JSONResponse({"deleted": deleted, "memoryId": memory_id}, status_code=status_code)

    async def get_skills(request):
        cwd_param = query_string(request, "cwd", "").strip()
        if cwd_param:
            # 复用索引快照 + foreign_hidden 缓存,避免项目枚举的重复全量扫描(见 get_memory_projects)。
            with manager.batch_reads():
                cwd = resolve_project_cwd(cwd_param, Path(manager.cwd), known_project_entries())
            if cwd is None:
                return json_error(_("project not found"), 404)
        else:
            cwd = cwd_for_session_id(query_string(request, "sessionId", ""))
            if cwd is None:
                return json_error(_("session not found"), 404)
        return JSONResponse(skills_payload(cwd))

    async def put_disabled_skills(request):
        try:
            data = await json_object_body(request)
            disabled = required_string_list(data, "disabled")
            session_id = optional_string(data, "sessionId") or ""
            cwd_param = (optional_string(data, "cwd") or "").strip()
        except ValueError as exc:
            return json_error(str(exc), 400)
        if cwd_param:
            cwd = resolve_project_cwd(cwd_param, Path(manager.cwd), known_project_entries())
            if cwd is None:
                return json_error(_("project not found"), 404)
        else:
            cwd = cwd_for_session_id(session_id)
            if cwd is None:
                return json_error(_("session not found"), 404)
        try:
            payload = save_disabled_payload(cwd, disabled)
            invalidate_dynamic_suggestions()
            return JSONResponse(payload)
        except OSError as exc:
            return json_error(str(exc), 400)
        except ValueError as exc:
            return json_error(str(exc), 400)

    def mcp_cwd_from_params(cwd_param: str, session_id: str) -> Path | None:
        cwd_param = (cwd_param or "").strip()
        if cwd_param:
            # 项目枚举放进 batch_reads 窗口,复用索引快照 + foreign_hidden 缓存(见 get_memory_projects)。
            # 仅包裹同步的 cwd 解析,不跨后续 run_in_threadpool 的 await,避免长时间持有索引快照。
            with manager.batch_reads():
                return resolve_project_cwd(cwd_param, Path(manager.cwd), known_project_entries())
        return cwd_for_session_id(session_id or "")

    async def get_mcp_servers(request):
        cwd = mcp_cwd_from_params(query_string(request, "cwd", ""), query_string(request, "sessionId", ""))
        if cwd is None:
            return json_error(_("project or session not found"), 404)
        try:
            payload = await run_in_threadpool(mcp_settings.list_mcp_servers, cwd)
        except MCPWebError as exc:
            return json_error(exc.message, exc.status_code, code=exc.code)
        return JSONResponse(payload)

    async def get_mcp_check(request):
        cwd = mcp_cwd_from_params(query_string(request, "cwd", ""), query_string(request, "sessionId", ""))
        if cwd is None:
            return json_error(_("project or session not found"), 404)
        name = query_string(request, "name", "").strip() or None
        scope = query_string(request, "scope", "").strip() or None
        source_path = query_string(request, "sourcePath", "").strip() or None
        try:
            payload = await run_in_threadpool(
                partial(
                    mcp_settings.check_mcp_servers,
                    cwd,
                    name=name,
                    scope=scope,
                    source_path=source_path,
                )
            )
        except MCPWebError as exc:
            return json_error(exc.message, exc.status_code, code=exc.code)
        return JSONResponse(payload)

    async def get_mcp_capabilities(request):
        cwd = mcp_cwd_from_params(query_string(request, "cwd", ""), query_string(request, "sessionId", ""))
        if cwd is None:
            return json_error(_("project or session not found"), 404)
        name = query_string(request, "name", "").strip()
        if not name:
            return json_error(_("name is required"), 400)
        scope = query_string(request, "scope", "").strip() or None
        source_path = query_string(request, "sourcePath", "").strip() or None
        try:
            payload = await run_in_threadpool(
                partial(
                    mcp_settings.server_capabilities,
                    cwd,
                    name=name,
                    scope=scope,
                    source_path=source_path,
                )
            )
        except MCPWebError as exc:
            return json_error(exc.message, exc.status_code, code=exc.code)
        return JSONResponse(payload)

    async def _mcp_body_cwd(data: dict) -> Path | None:
        return mcp_cwd_from_params(
            (optional_string(data, "cwd") or ""),
            (optional_string(data, "sessionId") or ""),
        )

    async def post_mcp_server(request):
        try:
            data = await json_object_body(request)
            name = required_string(data, "name")
            scope = optional_string(data, "scope")
            raw_config = data.get("config")
            fields = data.get("fields")
        except ValueError as exc:
            return json_error(str(exc), 400)
        cwd = await _mcp_body_cwd(data)
        if cwd is None:
            return json_error(_("project or session not found"), 404)
        try:
            if raw_config is not None:
                payload = await run_in_threadpool(
                    partial(mcp_settings.add_mcp_server_json, cwd, name=name, config=raw_config, scope=scope)
                )
            else:
                payload = await run_in_threadpool(
                    partial(mcp_settings.add_mcp_server, cwd, name=name, fields=fields or {}, scope=scope)
                )
        except MCPWebError as exc:
            return json_error(exc.message, exc.status_code, code=exc.code)
        return JSONResponse(payload)

    async def put_mcp_server(request):
        name = request.path_params["name"]
        try:
            data = await json_object_body(request)
            scope = required_string(data, "scope")
            raw_config = data.get("config")
            fields = data.get("fields")
        except ValueError as exc:
            return json_error(str(exc), 400)
        cwd = await _mcp_body_cwd(data)
        if cwd is None:
            return json_error(_("project or session not found"), 404)
        try:
            payload = await run_in_threadpool(
                partial(
                    mcp_settings.update_mcp_server,
                    cwd,
                    name=name,
                    fields=fields,
                    config=raw_config,
                    scope=scope,
                )
            )
        except MCPWebError as exc:
            return json_error(exc.message, exc.status_code, code=exc.code)
        return JSONResponse(payload)

    async def delete_mcp_server(request):
        name = request.path_params["name"]
        scope = query_string(request, "scope", "").strip() or None
        source_path = query_string(request, "sourcePath", "").strip() or None
        cwd = mcp_cwd_from_params(query_string(request, "cwd", ""), query_string(request, "sessionId", ""))
        if cwd is None:
            return json_error(_("project or session not found"), 404)
        try:
            payload = await run_in_threadpool(
                partial(mcp_settings.remove_mcp_server, cwd, name=name, scope=scope, source_path=source_path)
            )
        except MCPWebError as exc:
            return json_error(exc.message, exc.status_code, code=exc.code)
        return JSONResponse(payload)

    async def put_mcp_enabled(request):
        name = request.path_params["name"]
        try:
            data = await json_object_body(request)
            disabled = required_bool(data, "disabled")
            scope = required_string(data, "scope")
            source_path = optional_string(data, "sourcePath")
        except ValueError as exc:
            return json_error(str(exc), 400)
        cwd = await _mcp_body_cwd(data)
        if cwd is None:
            return json_error(_("project or session not found"), 404)
        try:
            payload = await run_in_threadpool(
                partial(
                    mcp_settings.set_mcp_enabled,
                    cwd,
                    name=name,
                    disabled=disabled,
                    scope=scope,
                    source_path=source_path,
                )
            )
        except MCPWebError as exc:
            return json_error(exc.message, exc.status_code, code=exc.code)
        return JSONResponse(payload)

    async def post_mcp_approval(request):
        name = request.path_params["name"]
        try:
            data = await json_object_body(request)
            decision = required_string(data, "decision")
        except ValueError as exc:
            return json_error(str(exc), 400)
        if decision not in {"approve", "reject"}:
            return json_error(_("decision must be 'approve' or 'reject'"), 400)
        cwd = await _mcp_body_cwd(data)
        if cwd is None:
            return json_error(_("project or session not found"), 404)
        func = mcp_settings.approve_mcp_server if decision == "approve" else mcp_settings.reject_mcp_server
        try:
            payload = await run_in_threadpool(partial(func, cwd, name=name))
        except MCPWebError as exc:
            return json_error(exc.message, exc.status_code, code=exc.code)
        return JSONResponse(payload)

    async def post_mcp_reset_auth(request):
        name = request.path_params["name"]
        try:
            data = await json_object_body(request)
            scope = optional_string(data, "scope")
            source_path = optional_string(data, "sourcePath")
        except ValueError as exc:
            return json_error(str(exc), 400)
        cwd = await _mcp_body_cwd(data)
        if cwd is None:
            return json_error(_("project or session not found"), 404)
        try:
            payload = await run_in_threadpool(
                partial(mcp_settings.reset_mcp_auth, cwd, name=name, scope=scope, source_path=source_path)
            )
        except MCPWebError as exc:
            return json_error(exc.message, exc.status_code, code=exc.code)
        return JSONResponse(payload)

    async def post_mcp_auth_start(request):
        name = request.path_params["name"]
        try:
            data = await json_object_body(request)
            scope = optional_string(data, "scope")
            source_path = optional_string(data, "sourcePath")
            reauthenticate = bool(data.get("reauthenticate", False))
        except ValueError as exc:
            return json_error(str(exc), 400)
        cwd = await _mcp_body_cwd(data)
        if cwd is None:
            return json_error(_("project or session not found"), 404)
        try:
            payload = await run_in_threadpool(
                partial(
                    mcp_settings.start_mcp_auth,
                    cwd,
                    name=name,
                    scope=scope,
                    source_path=source_path,
                    reauthenticate=reauthenticate,
                )
            )
        except MCPWebError as exc:
            return json_error(exc.message, exc.status_code, code=exc.code)
        return JSONResponse(payload)

    async def post_mcp_auth_wait(request):
        flow_id = request.path_params["flow_id"]
        try:
            payload = await run_in_threadpool(mcp_settings.wait_mcp_auth, flow_id)
        except MCPWebError as exc:
            return json_error(exc.message, exc.status_code, code=exc.code)
        return JSONResponse(payload)

    async def post_mcp_auth_complete(request):
        flow_id = request.path_params["flow_id"]
        try:
            data = await json_object_body(request)
            callback_url = required_string(data, "callbackUrl")
        except ValueError as exc:
            return json_error(str(exc), 400)
        try:
            payload = await run_in_threadpool(mcp_settings.complete_mcp_auth, flow_id, callback_url)
        except MCPWebError as exc:
            return json_error(exc.message, exc.status_code, code=exc.code)
        return JSONResponse(payload)

    async def post_mcp_auth_cancel(request):
        flow_id = request.path_params["flow_id"]
        try:
            payload = await run_in_threadpool(mcp_settings.cancel_mcp_auth, flow_id)
        except MCPWebError as exc:
            return json_error(exc.message, exc.status_code, code=exc.code)
        return JSONResponse(payload)

    async def get_pipeline_state(request):
        try:
            payload = await pipeline_state_from_query(request.query_params)
        except PipelineStateRequestError as exc:
            return json_error(str(exc), 400)
        except PipelineStateNotFoundError:
            return json_error(_("pipeline state not found"), 404)
        except Exception:
            logger.exception("Failed to load web pipeline state")
            return json_error(_("internal server error"), 500)
        return JSONResponse(payload)

    async def post_pipeline_candidate_selection(request):
        try:
            data = await json_object_body(request)
            selection = parse_candidate_selection_body(data)
        except (ValueError, PipelineCandidateSelectionRequestError) as exc:
            return json_error(str(exc), 400)
        session = manager.get_session(selection.session_id)
        if session is None:
            return json_error(_("session not found"), 404)
        if (read_only := foreign_read_only_response(session)) is not None:
            return read_only
        if (archived := session_archived_response(session)) is not None:
            return archived
        reservation = await reserve_pipeline_action(session)
        if reservation is None:
            return turn_busy_response()
        try:
            action_owner = transfer_pipeline_action_reservation(session, reservation)
        except TurnReservationCanceledError:
            return JSONResponse(
                {
                    "accepted": False,
                    "reason": "turn canceled",
                    "canceled": True,
                    "interrupted": True,
                },
                status_code=409,
            )
        drain_queued_after_action = False
        try:
            if session.mode != "pipeline":
                return json_error(_("session is not a pipeline session"), 409, code="pipeline_not_active")
            try:
                require_pipeline_metadata(session)
            except ValueError as exc:
                return json_error(str(exc), 400)
            model_selection = active_model_selection(session)

            result = await pipeline_action_runner.select_candidate(
                session,
                selection,
                model_selection=model_selection,
                event_sink=lambda evs: publish_pipeline_live_events(session, evs),
                permission_resolver=make_pipeline_permission_resolver(session),
                envelope_observer=lambda env: diagram_optimization_coordinator.maybe_trigger(session, manager, env),
            )
            await publish_pipeline_action_events(
                session,
                list(result.events),
                base_payload={
                    "contextId": session.context_id,
                    "taskId": session.task_id,
                    "mode": "pipeline",
                },
            )
            # 选择候选会同步续跑流水线(部署→完成→交接),其间转发的 pipeline.step.marker /
            # assistant.message.* 把前端 currentTurnActive 置真。此前本路由不发 turn.done,导致
            # 流水线已跑完、已进入普通对话,侧栏却仍显示「运行中」、提交 prompt 还被排队(Issue 4)。
            # 补发 turn.done 清运行态;并按 _run_pipeline_turn_task 同款逻辑落交接到普通对话。
            await session.events.publish(
                "turn.done",
                {
                    "mode": "pipeline",
                    "interrupted": False,
                    "canceled": False,
                    "failed": not result.accepted,
                    "contextId": session.context_id,
                    "taskId": session.task_id,
                },
            )
            if result.accepted:
                manager.mark_session_completed(session)
            if await maybe_switch_session_to_normal(session):
                await session.events.publish(
                    "session.updated",
                    {
                        "mode": session.mode,
                        "contextId": session.context_id,
                        "taskId": session.task_id,
                    },
                )
            drain_queued_after_action = bool(result.accepted)
            return JSONResponse(result.response, status_code=result.status_code)
        except asyncio.CancelledError:
            manager.cancel_pending_requests_for_session(session)
            await session.events.publish(
                "turn.done",
                {
                    "mode": "pipeline",
                    "interrupted": True,
                    "canceled": True,
                    "contextId": session.context_id,
                    "taskId": session.task_id,
                },
            )
            return JSONResponse(
                {
                    "accepted": False,
                    "reason": "turn canceled",
                    "canceled": True,
                    "interrupted": True,
                },
                status_code=409,
            )
        finally:
            await release_pipeline_action_owner(session, action_owner)
            if drain_queued_after_action:
                await start_next_queued_turn(session)

    async def get_session_cleanup(request):
        session_id = request.path_params["session_id"]
        session = manager.get_session(session_id)
        if session is None:
            return json_error(_("session not found"), 404)
        try:
            return JSONResponse(await session_cleanup_summary(session))
        except Exception:
            logger.exception("Failed to load web session cleanup state")
            return json_error(_("internal server error"), 500)

    async def get_status(request):
        session_id = request.path_params["session_id"]
        session = manager.get_session(session_id)
        if session is None:
            return json_error(_("session not found"), 404)
        payload = manager.status(session)
        payload["providerSummary"] = provider_summary_for_recovery(payload.get("activeProvider"))
        payload["cloudSummary"] = payload.get("cloud")
        try:
            cleanup = await session_cleanup_summary(session)
        except Exception:
            logger.exception("Failed to load web session cleanup state")
            cleanup = {"status": "unavailable", "resources": [], "resourceCount": 0}
        payload["cleanup"] = cleanup
        pipeline = payload.get("pipeline")
        if isinstance(pipeline, dict):
            pipeline.update(await pipeline_recovery_payload(session))
            pipeline["cleanup"] = pipeline.get("cleanup") or cleanup
            recovered_cleanup = pipeline.get("cleanup")
            pipeline["cleanupStatus"] = (
                recovered_cleanup.get("status")
                if isinstance(recovered_cleanup, dict) and recovered_cleanup.get("status")
                else cleanup.get("status")
            )
            if session.context_id and "pipelineRunId" not in pipeline:
                pipeline["pipelineRunId"] = session.context_id
        return JSONResponse(normalize_event_payload(payload))

    async def post_command(request):
        session_id = request.path_params["session_id"]
        session = manager.get_session(session_id)
        if session is None:
            return json_error(_("session not found"), 404)
        try:
            data = await json_object_body(request)
            command_text = required_string(data, "command")
        except ValueError as exc:
            return json_error(str(exc), 400)

        def command_session_error() -> JSONResponse | None:
            if manager.get_session(session.web_session_id) is not session:
                return json_error(_("session not found"), 404)
            allowed_without_write = {"exit", "help", "?", "prompt", "status"}
            parsed_command = parse_slash_command_text(command_text)
            command_name = parsed_command[0] if parsed_command is not None else None
            if session.archived and command_name not in allowed_without_write:
                return session_archived_response(session)
            if manager.is_session_read_only(session) and command_name not in allowed_without_write:
                return foreign_read_only_response(session)
            return None

        async with session.turn_admission_lock:
            if (command_error := command_session_error()) is not None:
                return command_error

        async def finish_direct_command(result: dict[str, Any], *, status_code: int = 200) -> JSONResponse:
            await session.events.publish("command.finished", {"command": command_text, "result": result})
            return JSONResponse(result, status_code=status_code)

        slash_command = parse_slash_command_text(command_text)
        if slash_command is not None:
            command_name, command_arg = slash_command
            command_blocked_in_pipeline = (
                session.mode == "pipeline"
                and command_name not in {"exit", "help", "?", "status", "prompt", "resume"}
                and not session.allow_user_escapes.command
            )
            if command_name == "prompt":
                return await finish_direct_command(
                    {
                        "accepted": True,
                        "command": "prompt",
                        "snapshot": await prompt_snapshot(session),
                    }
                )
            if command_name == "resume":
                result, status_code = resume_command_result(session, command_arg)
                if result.get("accepted"):
                    await session.events.publish(
                        "session.resync.required",
                        {
                            "reason": "resume_command",
                            "sessionId": result["session"]["sessionId"],
                            "webSessionId": result["session"]["webSessionId"],
                            "cwd": result["session"]["cwd"],
                        },
                    )
                return await finish_direct_command(result, status_code=status_code)
            if command_name == "compact" and not command_blocked_in_pipeline:
                compact_payload, status_code = await run_session_compaction(session)
                if compact_payload is None:
                    if status_code == 404:
                        return json_error(_("session not found"), 404)
                    return session_archived_response(session) or json_error(
                        _("session is archived"),
                        409,
                        code="session_archived",
                    )
                return await finish_direct_command(
                    {
                        "command": "compact",
                        **compact_payload,
                    },
                    status_code=status_code,
                )
            if command_name in {"model", "effort"} and not command_blocked_in_pipeline:
                active = providers_payload().get("active", {})
                if command_name == "model" and not command_arg.strip():
                    return await finish_direct_command(
                        {
                            "accepted": True,
                            "command": "model",
                            "action": "open_model_selector",
                            "providers": providers_payload(),
                        }
                    )
                if command_name == "effort" and not command_arg.strip():
                    return await finish_direct_command(
                        {
                            "accepted": True,
                            "command": "effort",
                            "action": "open_effort_selector",
                            "providers": providers_payload(),
                        }
                    )
                provider_key = active.get("provider")
                model = command_arg.strip() if command_name == "model" else active.get("model")
                effort = command_arg.strip() if command_name == "effort" else active.get("effort")
                if not isinstance(provider_key, str) or not provider_key:
                    return await finish_direct_command(
                        {
                            "accepted": False,
                            "command": command_name,
                            "error": {
                                "code": "no_active_provider",
                                "message": _("No active provider is configured."),
                            },
                        },
                        status_code=400,
                    )
                if not isinstance(model, str) or not model:
                    return await finish_direct_command(
                        {
                            "accepted": False,
                            "command": command_name,
                            "error": {
                                "code": "no_active_model",
                                "message": _("No active model is configured."),
                            },
                        },
                        status_code=400,
                    )
                save_payload: dict[str, Any] = {
                    "provider": provider_key,
                    "model": model,
                }
                if command_name == "effort" and isinstance(effort, str) and effort:
                    save_payload["effort"] = effort
                api_base = active.get("apiBase")
                if isinstance(api_base, str):
                    save_payload["apiBase"] = api_base
                try:
                    saved = save_active_provider(save_payload)
                except ValueError as exc:
                    return await finish_direct_command(
                        {
                            "accepted": False,
                            "command": command_name,
                            "error": {
                                "code": "invalid_{}".format(command_name),
                                "message": str(exc),
                            },
                        },
                        status_code=400,
                    )
                return await finish_direct_command(
                    {
                        "accepted": True,
                        "command": command_name,
                        "action": "{}_updated".format(command_name),
                        "active": saved.get("active", {}),
                    }
                )

        processed_skill = None
        skill_reservation: tuple[str, asyncio.Future[Any]] | None = None
        skill_invocation = prompt_command_for_input(session, command_text)
        if skill_invocation is None and command_is_missing_from_static_registry(session, command_text):
            skill_reservation = await reserve_agent_turn(session)
            if skill_reservation is None:
                if (command_error := command_session_error()) is not None:
                    return command_error
                result = {"accepted": False, "reason": "turn already running", "command": "skill"}
                await session.events.publish("command.finished", {"command": command_text, "result": result})
                return JSONResponse(result, status_code=409)
            _reserved, dynamic_placeholder = skill_reservation
            dynamic_owner = asyncio.current_task()
            assert dynamic_owner is not None
            dynamic_command_placeholders[dynamic_owner] = dynamic_placeholder
            session.active_turn_task = dynamic_owner
            # Discovery can create an MCP-backed runtime, so release admission while retaining lifecycle ownership
            # through the real request task. Successful discovery atomically transfers ownership back to the
            # placeholder before prompt expansion starts.
            if session.turn_admission_lock.locked():
                session.turn_admission_lock.release()

            async def release_dynamic_owner() -> None:
                await session.turn_admission_lock.acquire()
                dynamic_command_placeholders.pop(dynamic_owner, None)
                if session.active_turn_task is dynamic_owner:
                    session.active_turn_task = None
                session.turn_admission_lock.release()

            try:
                dynamic_invocation = await dynamic_prompt_command_for_input(session, command_text)
                await session.turn_admission_lock.acquire()
            except asyncio.CancelledError:
                interrupted = dynamic_placeholder.cancelled()
                result = {
                    "accepted": False,
                    "command": "skill",
                    "reason": "turn canceled",
                    "canceled": True,
                    "interrupted": interrupted,
                }
                try:
                    if interrupted:
                        await session.events.publish("command.finished", {"command": command_text, "result": result})
                finally:
                    await release_dynamic_owner()
                if interrupted:
                    return JSONResponse(result, status_code=409)
                raise
            except Exception as exc:
                try:
                    result = {
                        "accepted": False,
                        "command": "skill",
                        "error": {"code": "skill_failed", "message": public_exception_message(exc)},
                    }
                    await session.events.publish("command.finished", {"command": command_text, "result": result})
                finally:
                    await release_dynamic_owner()
                skill_reservation = None
                return JSONResponse(result, status_code=400)
            dynamic_command_placeholders.pop(dynamic_owner, None)
            if (
                manager.get_session(session.web_session_id) is not session
                or session.archived
                or session.active_turn_task is not dynamic_owner
                or dynamic_placeholder.cancelled()
            ):
                if session.active_turn_task is dynamic_owner:
                    session.active_turn_task = None
                session.turn_admission_lock.release()
                skill_reservation = None
                if (command_error := command_session_error()) is not None:
                    return command_error
                result = {
                    "accepted": False,
                    "command": "skill",
                    "reason": "turn canceled",
                    "canceled": True,
                    "interrupted": True,
                }
                await session.events.publish("command.finished", {"command": command_text, "result": result})
                return JSONResponse(result, status_code=409)
            session.active_turn_task = dynamic_placeholder
            if dynamic_invocation is not None:
                command, skill_args, processed_skill = dynamic_invocation
                skill_invocation = command, skill_args
            else:
                release_agent_turn_reservation(session, dynamic_placeholder)
                skill_reservation = None
        if skill_invocation is not None:
            command, skill_args = skill_invocation
            if session.mode == "pipeline" and not session.allow_user_escapes.skill:
                if skill_reservation is not None:
                    release_agent_turn_reservation(session, skill_reservation[1])
                result = {
                    "accepted": False,
                    "command": command.name,
                    "error": {
                        "code": "user_escape_not_allowed_in_pipeline",
                        "message": _("user escape commands are not available in pipeline mode"),
                    },
                }
                await session.events.publish("command.finished", {"command": command_text, "result": result})
                return JSONResponse(result, status_code=400)
            reservation = skill_reservation or await reserve_agent_turn(session)
            if reservation is None:
                if (command_error := command_session_error()) is not None:
                    return command_error
                result = {"accepted": False, "reason": "turn already running", "command": command.name}
                await session.events.publish("command.finished", {"command": command_text, "result": result})
                return JSONResponse(result, status_code=409)
            _reserved, placeholder = reservation
            owns_reservation = True
            try:
                if processed_skill is None:
                    from iac_code.skills.processor import process_prompt_command

                    processed = await process_prompt_command(
                        command,
                        skill_args,
                        session_id=session.session_id,
                        cwd=session.cwd,
                    )
                else:
                    processed = processed_skill
            except asyncio.CancelledError:
                if owns_reservation:
                    release_agent_turn_reservation(session, placeholder)
                raise
            except Exception as exc:
                if owns_reservation:
                    release_agent_turn_reservation(session, placeholder)
                result = {
                    "accepted": False,
                    "command": command.name,
                    "error": {"code": "skill_failed", "message": public_exception_message(exc)},
                }
                await session.events.publish("command.finished", {"command": command_text, "result": result})
                return JSONResponse(result, status_code=400)
            prompt_text = processed.prompt_content
            if processed.new_messages:
                first_message = processed.new_messages[0]
                content = first_message.get("content") if isinstance(first_message, dict) else None
                if isinstance(content, str) and content:
                    prompt_text = content
            try:
                turn_id = await start_background_turn(
                    session,
                    text=prompt_text,
                    source="skill",
                    context_modifier=processed.context_modifier,
                    reservation=reservation,
                )
                owns_reservation = False
            except TurnReservationCanceledError:
                owns_reservation = False
                result = {
                    "accepted": False,
                    "command": "skill",
                    "skill": command.name,
                    "reason": "turn canceled",
                    "canceled": True,
                    "interrupted": True,
                }
                await session.events.publish("command.finished", {"command": command_text, "result": result})
                return JSONResponse(result, status_code=409)
            except asyncio.CancelledError:
                if owns_reservation:
                    release_agent_turn_reservation(session, placeholder)
                raise
            result = {
                "accepted": True,
                "command": "skill",
                "skill": command.name,
                "turnId": turn_id,
                "entersAgentContext": True,
            }
            await session.events.publish("command.finished", {"command": command_text, "result": result})
            return JSONResponse(result, status_code=202)

        async with session.turn_admission_lock:
            if (command_error := command_session_error()) is not None:
                return command_error
            result = command_dispatcher.dispatch(session.web_session_id, command_text)
        if result.get("accepted") and result.get("command") == "skill":
            result = {
                "accepted": False,
                "command": "skill",
                "error": {
                    "code": "unknown_skill",
                    "message": "unknown skill: {}".format(str(result.get("skill") or "").strip()),
                },
            }
        shell_task: asyncio.Task[Any] | None = None
        try:
            if result.get("accepted") and result.get("command") == "shell_escape":
                shell_events: list[dict[str, Any]] = []

                def record_shell_event(event: dict[str, Any]) -> None:
                    if event.get("sessionId") == session.session_id and event["type"] in {
                        "local.shell.start",
                        "local.shell.end",
                    }:
                        shell_events.append(event)

                shell_task = asyncio.current_task()
                if shell_task is None:
                    raise RuntimeError("shell request task is unavailable")
                async with session.turn_admission_lock:
                    if manager.get_session(session.web_session_id) is not session:
                        return json_error(_("session not found"), 404)
                    if (archived := session_archived_response(session)) is not None:
                        return archived
                    session.active_local_tasks.add(shell_task)
                try:
                    with observe_published_events(record_shell_event):
                        await shell_runner.run(session, str(result.get("shell", "")))
                except asyncio.CancelledError:
                    result = {
                        **result,
                        "accepted": False,
                        "error": {
                            "code": "shell_escape_canceled",
                            "message": _("Shell command canceled."),
                        },
                    }
                    await session.events.publish(
                        "command.finished",
                        {
                            "command": command_text,
                            "result": result,
                        },
                    )
                    raise
                except Exception as exc:
                    public_message = public_exception_message(exc)
                    shell_command = str(result.get("shell", ""))
                    normalized_shell_command = str(normalize_event_payload({"command": shell_command})["command"])
                    start_payload = local_shell_start_payload(
                        shell_events,
                        command=normalized_shell_command,
                    )
                    shell_use_id = start_payload.get("shellUseId") or start_payload.get("toolUseId")
                    shell_use_id = shell_use_id if isinstance(shell_use_id, str) and shell_use_id else None
                    if not has_matching_local_shell_end(
                        shell_events,
                        command=normalized_shell_command,
                        shell_use_id=shell_use_id,
                    ):
                        if shell_use_id is None:
                            shell_use_id = "local-shell-fallback-{}".format(uuid.uuid4().hex)
                        end_payload: dict[str, Any] = {
                            "command": shell_command,
                            "exitCode": 1,
                            "stdout": "",
                            "stderr": public_message,
                            "local": True,
                            "entersAgentContext": False,
                        }
                        if shell_use_id is not None:
                            end_payload["shellUseId"] = shell_use_id
                            end_payload["toolUseId"] = shell_use_id
                        await session.events.publish(
                            "local.shell.end",
                            end_payload,
                        )
                    result = {
                        **result,
                        "accepted": False,
                        "error": {
                            "code": "shell_escape_failed",
                            "message": public_message,
                        },
                    }
            await session.events.publish(
                "command.finished",
                {
                    "command": command_text,
                    "result": result,
                },
            )
            error = result.get("error")
            if isinstance(error, dict) and error.get("code") == "shell_escape_failed":
                status_code = 500
            else:
                status_code = 200 if result.get("accepted") else 400
            return JSONResponse(result, status_code=status_code)
        finally:
            if shell_task is not None:
                session.active_local_tasks.discard(shell_task)

    async def answer_permission(request):
        request_id = request.path_params["request_id"]
        try:
            data = await json_object_body(request)
            answer = validate_permission_answer(data)
        except ValueError as exc:
            return json_error(str(exc), 400)
        pending = manager.get_pending_permission(request_id, session_id=answer["sessionId"])
        if pending is None:
            return JSONResponse({"requestId": request_id, "resolved": False}, status_code=404)
        if answer["choice"] not in offered_permission_choice_ids(pending.payload):
            return json_error(_("choice was not offered"), 400)
        result = manager.resolve_permission(request_id, {"choice": answer["choice"]}, session_id=answer["sessionId"])
        status_code = 200 if result["resolved"] else 404
        return JSONResponse(result, status_code=status_code)

    async def answer_question(request):
        request_id = request.path_params["request_id"]
        try:
            data = await json_object_body(request)
            answer = validate_question_answer(data)
        except ValueError as exc:
            return json_error(str(exc), 400)
        pending = manager.get_pending_question(request_id, session_id=answer["sessionId"])
        if pending is None:
            return JSONResponse({"requestId": request_id, "resolved": False}, status_code=404)
        try:
            normalized_answer = normalize_question_answer_for_pending(answer, pending.payload)
        except ValueError as exc:
            return json_error(str(exc), 400)
        result = manager.resolve_question(
            request_id,
            {**normalized_answer, "sessionId": answer["sessionId"]},
            session_id=answer["sessionId"],
        )
        status_code = 200 if result["resolved"] else 404
        return JSONResponse(result, status_code=status_code)

    async def answer_elicitation(request):
        request_id = request.path_params["request_id"]
        try:
            data = await json_object_body(request)
            session_id = required_string(data, "sessionId")
        except ValueError as exc:
            return json_error(str(exc), 400)
        pending = manager.get_pending_elicitation(request_id, session_id=session_id)
        if pending is None:
            return JSONResponse({"requestId": request_id, "resolved": False}, status_code=404)
        try:
            elicitation_result = elicitation_result_from_body(data, schema=pending.schema)
        except ValueError as exc:
            return json_error(str(exc), 400)
        result = manager.resolve_elicitation(request_id, elicitation_result, session_id=session_id)
        status_code = 200 if result["resolved"] else 404
        return JSONResponse(result, status_code=status_code)

    async def post_queued_input(request):
        session_id = request.path_params["session_id"]
        session = manager.get_session(session_id)
        if session is None:
            return json_error(_("session not found"), 404)
        if (read_only := foreign_read_only_response(session)) is not None:
            return read_only
        if (archived := session_archived_response(session)) is not None:
            return archived
        try:
            data = await json_object_body(request)
            text = string_with_default(data, "text")
        except ValueError as exc:
            return json_error(str(exc), 400)
        # Active turns already own admission. Queueing is a synchronous list/event
        # mutation, so accepting it here lets the owning action observe and drain the
        # follow-up without waiting for that action to release its reservation.
        if active_turn_running(session):
            return JSONResponse(manager.classify_queued_input(session, text))
        async with session.turn_admission_lock:
            if manager.get_session(session.web_session_id) is not session:
                return json_error(_("session not found"), 404)
            if (archived := session_archived_response(session)) is not None:
                return archived
            return JSONResponse(manager.classify_queued_input(session, text))

    def _parse_queued_index(request) -> int:
        raw = request.path_params["index"]
        try:
            return int(raw)
        except (TypeError, ValueError) as exc:
            raise ValueError(_("index must be an integer")) from exc

    async def delete_queued_input(request):
        session_id = request.path_params["session_id"]
        session = manager.get_session(session_id)
        if session is None:
            return json_error(_("session not found"), 404)
        if (read_only := foreign_read_only_response(session)) is not None:
            return read_only
        if (archived := session_archived_response(session)) is not None:
            return archived
        try:
            index = _parse_queued_index(request)
            data = await json_object_body(request)
            expected_text = required_string(data, "expectedText")
        except ValueError as exc:
            return json_error(str(exc), 400)
        async with session.turn_admission_lock:
            if manager.get_session(session.web_session_id) is not session:
                return json_error(_("session not found"), 404)
            if (archived := session_archived_response(session)) is not None:
                return archived
            try:
                return JSONResponse(manager.delete_queued_input(session, index, expected_text=expected_text))
            except QueuedInputActionError as exc:
                return json_error(str(exc), exc.status)

    async def patch_queued_input(request):
        session_id = request.path_params["session_id"]
        session = manager.get_session(session_id)
        if session is None:
            return json_error(_("session not found"), 404)
        if (read_only := foreign_read_only_response(session)) is not None:
            return read_only
        if (archived := session_archived_response(session)) is not None:
            return archived
        try:
            index = _parse_queued_index(request)
            data = await json_object_body(request)
            text = required_string(data, "text")
            expected_text = required_string(data, "expectedText")
        except ValueError as exc:
            return json_error(str(exc), 400)
        async with session.turn_admission_lock:
            if manager.get_session(session.web_session_id) is not session:
                return json_error(_("session not found"), 404)
            if (archived := session_archived_response(session)) is not None:
                return archived
            try:
                return JSONResponse(manager.edit_queued_input(session, index, text=text, expected_text=expected_text))
            except QueuedInputActionError as exc:
                return json_error(str(exc), exc.status)

    async def steer_queued_input(request):
        session_id = request.path_params["session_id"]
        session = manager.get_session(session_id)
        if session is None:
            return json_error(_("session not found"), 404)
        if (read_only := foreign_read_only_response(session)) is not None:
            return read_only
        if (archived := session_archived_response(session)) is not None:
            return archived
        try:
            index = _parse_queued_index(request)
            data = await json_object_body(request)
            expected_text = required_string(data, "expectedText")
        except ValueError as exc:
            return json_error(str(exc), 400)
        async with session.turn_admission_lock:
            if manager.get_session(session.web_session_id) is not session:
                return json_error(_("session not found"), 404)
            if (archived := session_archived_response(session)) is not None:
                return archived
            try:
                return JSONResponse(manager.steer_queued_input(session, index, expected_text=expected_text))
            except QueuedInputActionError as exc:
                return json_error(str(exc), exc.status)

    async def interrupt_session(request):
        session_id = request.path_params["session_id"]
        session = manager.get_session(session_id)
        if session is None:
            return json_error(_("session not found"), 404)
        if (read_only := foreign_read_only_response(session)) is not None:
            return read_only
        if (archived := session_archived_response(session)) is not None:
            return archived
        try:
            data = await json_object_body(request)
            message = string_with_default(data, "message")
            image_ids = optional_string_list(data, "imageIds")
            file_refs = optional_string_list(data, "fileRefs")
        except ValueError as exc:
            return json_error(str(exc), 400)
        if session.mode == "normal" and (image_ids or file_refs):
            return JSONResponse(
                {
                    "accepted": False,
                    "error": {
                        "code": "interrupt_attachments_not_supported",
                        "message": _("normal-mode interrupts do not support attachments"),
                    },
                    "draft": {
                        "message": message,
                        "imageIds": image_ids,
                        "fileRefs": file_refs,
                    },
                },
                status_code=400,
            )
        try:
            validate_turn_attachments(session, image_ids, file_refs)
        except (FileNotFoundError, ValueError) as exc:
            return json_error(str(exc), 400)
        if session.mode == "pipeline":
            # 纯停止(空消息)且流水线回合正在运行:reserve_pipeline_action 会因回合持锁
            # 返回 None → 409(turn_busy),导致「取消」完全无效。此时应直接取消运行中的
            # 回合任务(与 normal 分支一致),由 _run_pipeline_turn_task 的 CancelledError
            # 处理器广播 turn.done{interrupted,canceled}。带消息的转向仍走 reservation 路径。
            if not message.strip():
                async with session.turn_admission_lock:
                    if manager.get_session(session.web_session_id) is not session:
                        return json_error(_("session not found"), 404)
                    if (archived := session_archived_response(session)) is not None:
                        return archived
                    active_turn_task = session.active_turn_task
                    is_turn_running = active_turn_task is not None and not active_turn_task.done()
                    if is_turn_running:
                        manager.cancel_pending_requests_for_session(session)
                        cancel_active_turn_task(active_turn_task)
                        await session.events.publish(
                            "interrupt.accepted",
                            {
                                "message": message,
                                "mode": session.mode,
                                "imageIds": image_ids,
                                "fileRefs": file_refs,
                            },
                        )
                        return JSONResponse({"accepted": True})
            if session.pipeline_interrupt_lock.locked():
                if manager.get_session(session.web_session_id) is not session:
                    return json_error(_("session not found"), 404)
                if (archived := session_archived_response(session)) is not None:
                    return archived
                return turn_busy_response()
            await session.pipeline_interrupt_lock.acquire()
            try:
                if session.mode != "pipeline":
                    return json_error(_("session is not a pipeline session"), 409, code="pipeline_not_active")
                model_selection = active_model_selection(session)
                if (
                    capability_error := image_capability_error(
                        session,
                        image_ids,
                        model_selection=model_selection,
                    )
                ) is not None:
                    return capability_error
                try:
                    require_pipeline_metadata(session)
                except ValueError as exc:
                    return json_error(str(exc), 400)
                result = await pipeline_action_runner.interrupt(
                    session,
                    message,
                    image_ids,
                    file_refs,
                    model_selection=model_selection,
                    event_sink=lambda evs: publish_pipeline_live_events(session, evs),
                    permission_resolver=make_pipeline_permission_resolver(session),
                )
                await publish_pipeline_action_events(
                    session,
                    list(result.events),
                    base_payload={
                        "pipelineInterrupt": True,
                        "mode": "pipeline",
                        "contextId": session.context_id,
                        "taskId": session.task_id,
                        "message": message,
                        "imageIds": image_ids,
                        "fileRefs": file_refs,
                    },
                )
                return JSONResponse(result.response, status_code=result.status_code)
            finally:
                session.pipeline_interrupt_lock.release()

        active_turn_task = session.active_turn_task
        pre_start_reservation = (
            session.turn_admission_lock.locked()
            and active_turn_task is not None
            and not active_turn_task.done()
            and not isinstance(active_turn_task, asyncio.Task)
        )
        if pre_start_reservation:
            if (archived := session_archived_response(session)) is not None:
                return archived
            manager.cancel_pending_requests_for_session(session)
            if not message.strip():
                cancel_active_turn_task(active_turn_task)
            else:
                manager.classify_queued_input(session, message)
            await session.events.publish(
                "interrupt.accepted",
                {
                    "message": message,
                    "mode": session.mode,
                    "imageIds": image_ids,
                    "fileRefs": file_refs,
                },
            )
            return JSONResponse({"accepted": True})

        async with session.turn_admission_lock:
            if manager.get_session(session.web_session_id) is not session:
                return json_error(_("session not found"), 404)
            if (archived := session_archived_response(session)) is not None:
                return archived
            manager.cancel_pending_requests_for_session(session)
            active_turn_task = session.active_turn_task
            if not message.strip() and active_turn_task is not None and not active_turn_task.done():
                cancel_active_turn_task(active_turn_task)
                if isinstance(active_turn_task, asyncio.Task) and session.queued_inputs:
                    # Auto-submit any queued inputs once the cancelled turn fully
                    # unwinds. Register in active_local_tasks before releasing the
                    # admission lock so a concurrent shutdown snapshot includes it.
                    drain_task = asyncio.create_task(_drain_queue_after_stop(session, active_turn_task))
                    session.active_local_tasks.add(drain_task)
            elif message.strip() and active_turn_task is not None and not active_turn_task.done():
                manager.classify_queued_input(session, message)
            await session.events.publish(
                "interrupt.accepted",
                {
                    "message": message,
                    "mode": session.mode,
                    "imageIds": image_ids,
                    "fileRefs": file_refs,
                },
            )
            return JSONResponse({"accepted": True})

    async def stream_session_events(request):
        session_id = request.path_params["session_id"]
        session = manager.get_session(session_id)
        if session is None:
            return json_error(_("session not found"), 404)
        try:
            after_sequence = event_cursor(request)
        except ValueError as exc:
            return json_error(str(exc), 400)

        async def event_stream():
            # 仅在响应体生成器真正启动时标记已读。StreamingResponse 创建与订阅启动之间
            # 存在窗口，过早清除会让该窗口内完成的回合错误地保持“已读”。
            manager.mark_session_viewed(session)
            if session.events.requires_resync(after_sequence=after_sequence):
                yield encode_sse(
                    make_resync_event(
                        session.session_id,
                        after_sequence=after_sequence,
                        floor_sequence=session.events.floor_sequence,
                    )
                )
                return

            async for event in session.events.stream_after(after_sequence):
                yield encode_sse(event)

        return StreamingResponse(
            event_stream(),
            media_type="text/event-stream",
            headers={
                "Cache-Control": "no-cache",
                "X-Accel-Buffering": "no",
            },
        )

    return Starlette(
        lifespan=lifespan,
        middleware=[Middleware(_SuppressAllRedactionMiddleware)],
        routes=[
            Route("/health", health, methods=["GET"]),
            Route("/api/server/restart", restart_server, methods=["POST"]),
            Route("/api/update/status", update_status, methods=["GET"]),
            Route("/api/update/apply", update_apply, methods=["POST"]),
            Route("/api/update/dismiss", update_dismiss, methods=["POST"]),
            Route("/", index, methods=["GET"]),
            Route("/api/sessions", create_session, methods=["POST"]),
            Route("/api/sessions", list_sessions, methods=["GET"]),
            Route("/api/sessions/archived", list_archived_sessions, methods=["GET"]),
            Route("/api/sessions/archived", delete_archived_sessions_route, methods=["DELETE"]),
            Route("/api/sessions/search", search_sessions_route, methods=["GET"]),
            Route("/api/projects", patch_project, methods=["PATCH"]),
            Route("/api/projects/reveal", reveal_project, methods=["POST"]),
            Route("/api/projects/archive-sessions", archive_project_sessions_route, methods=["POST"]),
            Route("/api/commands", get_commands, methods=["GET"]),
            Route("/api/providers", get_providers, methods=["GET"]),
            Route("/api/providers/config", put_provider_config, methods=["PUT"]),
            Route("/api/providers/config", delete_provider_config, methods=["DELETE"]),
            Route("/api/providers/active", put_active_provider, methods=["PUT"]),
            Route("/api/cloud/aliyun", get_cloud_aliyun, methods=["GET"]),
            Route("/api/cloud/aliyun", put_cloud_aliyun, methods=["PUT"]),
            Route("/api/cloud/aliyun/oauth-login", post_cloud_aliyun_oauth_login, methods=["POST"]),
            Route("/api/suggestions", get_suggestions, methods=["GET"]),
            Route("/api/memory", get_memory, methods=["GET"]),
            Route("/api/memory/projects", get_memory_projects, methods=["GET"]),
            Route("/api/memory/project", put_memory_project, methods=["PUT"]),
            Route("/api/memory/user", put_memory_user, methods=["PUT"]),
            Route("/api/memory/auto", put_memory_auto, methods=["PUT"]),
            Route("/api/settings/foreign-sessions", get_foreign_settings, methods=["GET"]),
            Route("/api/settings/foreign-sessions", put_foreign_settings, methods=["PUT"]),
            Route("/api/settings/pipeline-review-step", get_pipeline_review_step_settings, methods=["GET"]),
            Route("/api/settings/pipeline-review-step", put_pipeline_review_step_settings, methods=["PUT"]),
            Route(
                "/api/settings/pipeline-review-step/prerequisite",
                get_pipeline_review_step_prerequisite,
                methods=["GET"],
            ),
            Route(
                "/api/settings/pipeline-review-step/install",
                install_pipeline_review_step_prerequisite,
                methods=["POST"],
            ),
            Route("/api/settings/appearance", get_appearance_settings, methods=["GET"]),
            Route("/api/settings/appearance", put_appearance_settings, methods=["PUT"]),
            Route("/api/settings/ui-language", get_ui_language_settings, methods=["GET"]),
            Route("/api/settings/ui-language", put_ui_language_settings, methods=["PUT"]),
            Route("/api/settings/session-defaults", get_session_defaults_settings, methods=["GET"]),
            Route("/api/settings/session-defaults", put_session_defaults_settings, methods=["PUT"]),
            Route("/api/memory/legacy", get_legacy_memory, methods=["GET"]),
            Route("/api/memory/legacy/{memory_id}", delete_legacy_memory_route, methods=["DELETE"]),
            Route("/api/skills", get_skills, methods=["GET"]),
            Route("/api/skills/disabled", put_disabled_skills, methods=["PUT"]),
            Route("/api/mcp/servers", get_mcp_servers, methods=["GET"]),
            Route("/api/mcp/servers", post_mcp_server, methods=["POST"]),
            Route("/api/mcp/check", get_mcp_check, methods=["GET"]),
            Route("/api/mcp/capabilities", get_mcp_capabilities, methods=["GET"]),
            Route("/api/mcp/servers/{name}", put_mcp_server, methods=["PUT"]),
            Route("/api/mcp/servers/{name}", delete_mcp_server, methods=["DELETE"]),
            Route("/api/mcp/servers/{name}/enabled", put_mcp_enabled, methods=["PUT"]),
            Route("/api/mcp/servers/{name}/approval", post_mcp_approval, methods=["POST"]),
            Route("/api/mcp/servers/{name}/reset-auth", post_mcp_reset_auth, methods=["POST"]),
            Route("/api/mcp/servers/{name}/auth", post_mcp_auth_start, methods=["POST"]),
            Route("/api/mcp/auth/{flow_id}/wait", post_mcp_auth_wait, methods=["POST"]),
            Route("/api/mcp/auth/{flow_id}/complete", post_mcp_auth_complete, methods=["POST"]),
            Route("/api/mcp/auth/{flow_id}/cancel", post_mcp_auth_cancel, methods=["POST"]),
            Route("/api/images", post_image, methods=["POST"]),
            Route("/api/images/{image_id}", get_cached_image, methods=["GET"]),
            Route("/api/files/search", get_file_search, methods=["GET"]),
            Route("/api/files/quick-open", get_file_quick_open, methods=["GET"]),
            Route("/api/history/search", get_history_search, methods=["GET"]),
            Route("/api/transcript/{turn_id}", get_transcript, methods=["GET"]),
            Route("/api/pipeline/state", get_pipeline_state, methods=["GET"]),
            Route("/api/pipeline/candidates/select", post_pipeline_candidate_selection, methods=["POST"]),
            Route("/api/permissions/{request_id}/answer", answer_permission, methods=["POST"]),
            Route("/api/questions/{request_id}/answer", answer_question, methods=["POST"]),
            Route("/api/elicitations/{request_id}/answer", answer_elicitation, methods=["POST"]),
            Route("/api/sessions/{session_id}/commands", post_command, methods=["POST"]),
            Route("/api/sessions/{session_id}/cleanup", get_session_cleanup, methods=["GET"]),
            Route("/api/sessions/{session_id}/status", get_status, methods=["GET"]),
            Route("/api/sessions/{session_id}/prompt", get_session_prompt, methods=["GET"]),
            Route("/api/sessions/{session_id}/compact", post_session_compact, methods=["POST"]),
            Route("/api/sessions/{session_id}/debug", get_session_debug, methods=["GET"]),
            Route("/api/sessions/{session_id}/images", post_image, methods=["POST"]),
            Route("/api/sessions/{session_id}/messages", get_messages, methods=["GET"]),
            Route("/api/sessions/{session_id}/messages", post_message, methods=["POST"]),
            Route("/api/sessions/{session_id}/outputs", get_outputs, methods=["GET"]),
            Route("/api/sessions/{session_id}/outputs/file", get_output_file, methods=["GET"]),
            Route("/api/sessions/{session_id}/queued-inputs", post_queued_input, methods=["POST"]),
            Route(
                "/api/sessions/{session_id}/queued-inputs/{index}",
                delete_queued_input,
                methods=["DELETE"],
            ),
            Route(
                "/api/sessions/{session_id}/queued-inputs/{index}",
                patch_queued_input,
                methods=["PATCH"],
            ),
            Route(
                "/api/sessions/{session_id}/queued-inputs/{index}/steer",
                steer_queued_input,
                methods=["POST"],
            ),
            Route("/api/sessions/{session_id}/interrupt", interrupt_session, methods=["POST"]),
            Route("/api/sessions/{session_id}/events", stream_session_events, methods=["GET"]),
            Route("/api/sessions/{session_id}/permission-mode", put_session_permission_mode, methods=["PUT"]),
            Route("/api/sessions/{session_id}/thinking-enabled", put_session_thinking_enabled, methods=["PUT"]),
            Route("/api/sessions/{session_id}/model", put_session_model, methods=["PUT"]),
            Route("/api/sessions/{session_id}/model", delete_session_model, methods=["DELETE"]),
            Route("/api/sessions/{session_id}", patch_session, methods=["PATCH"]),
            Route("/api/sessions/{session_id}", get_session, methods=["GET"]),
            Route("/api/sessions/{session_id}", delete_session_route, methods=["DELETE"]),
            Mount("/static", NoCacheStaticFiles(directory=STATIC_DIR), name="static"),
        ],
        exception_handlers={404: not_found},
    )
