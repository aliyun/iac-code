"""Runtime wrappers for Web chat turns."""

from __future__ import annotations

import asyncio
import copy
import logging
import time
import uuid
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any

from iac_code.agent.message import ContentBlock, ImageBlock, TextBlock
from iac_code.i18n import _
from iac_code.mcp.manager import mcp_status_metadata
from iac_code.mcp.prompt_dispatch import mcp_prompt_command_stream
from iac_code.services.agent_factory import AgentFactoryOptions, create_agent_runtime
from iac_code.services.telemetry import flush_telemetry, use_session_id
from iac_code.types.stream_events import (
    AskUserQuestionEvent,
    MessageEndEvent,
    PermissionRequestEvent,
    QueuedInputSubmittedEvent,
    SubPipelineStreamEvent,
    Usage,
)
from iac_code.web.events import WebEventTranslator, usage_payload
from iac_code.web.session_manager import WebSession, WebSessionManager, _camelize

TURN_ALREADY_RUNNING = {"accepted": False, "reason": "turn already running"}
WEB_RUNTIME_CLOSE_TIMEOUT_SECONDS = 2.0
WEB_TELEMETRY_FLUSH_TIMEOUT_SECONDS = 2.0
logger = logging.getLogger(__name__)


def load_saved_model() -> str | None:
    """Keep the historical test seam while resolving config at call time."""
    from iac_code.config import load_saved_model as load_configured_model

    return load_configured_model()


@dataclass(frozen=True)
class WebModelSelection:
    provider: str | None
    model: str
    effort: str | None
    provider_api_key: str | None = field(default=None, repr=False)
    provider_base_url: str | None = None
    provider_config_frozen: bool = False
    provider_config_override: dict[str, Any] | None = field(default=None, repr=False)


@dataclass(frozen=True)
class WebTurnRequest:
    text: str
    image_ids: list[str]
    file_refs: list[str]
    source: str = "composer"
    turn_id: str | None = None
    context_modifier: Any | None = None
    model_selection: WebModelSelection | None = None


def _turn_id() -> str:
    return uuid.uuid4().hex


def _message_id() -> str:
    return uuid.uuid4().hex


def _user_message_payload(request: WebTurnRequest, *, turn_id: str) -> dict[str, Any]:
    return {
        "turnId": turn_id,
        "text": request.text,
        "imageIds": list(request.image_ids),
        "fileRefs": list(request.file_refs),
        "source": request.source,
    }


def _turn_done_payload(*, turn_id: str) -> dict[str, Any]:
    return {
        "turnId": turn_id,
        "interrupted": False,
        "canceled": False,
    }


class _TurnMessageIdSessionStorage:
    """Stamp Web live IDs onto messages persisted by one ephemeral AgentLoop."""

    def __init__(
        self,
        delegate: Any,
        *,
        turn_id: str,
        assistant_message_id: Any,
        image_ids: list[str] | None = None,
        file_refs: list[str] | None = None,
    ) -> None:
        self._delegate = delegate
        self._turn_id = turn_id
        self._assistant_message_id = assistant_message_id
        self._image_ids = list(image_ids or [])
        self._file_refs = list(file_refs or [])
        self._initial_user_stamped = False
        self._stamped_messages: list[Any] = []

    def __getattr__(self, name: str) -> Any:
        return getattr(self._delegate, name)

    def append(self, cwd: Any, session_id: str, message: Any, *args: Any, **kwargs: Any) -> Any:
        from iac_code.agent.message import Message

        if isinstance(message, Message):
            metadata = dict(message.metadata)
            stable_id: str | None = None
            if message.role == "user":
                existing_id = metadata.get("messageId")
                if isinstance(existing_id, str) and existing_id:
                    stable_id = existing_id
                elif not self._initial_user_stamped:
                    stable_id = "user-{}".format(self._turn_id)
                    self._initial_user_stamped = True
                    if self._image_ids:
                        metadata.setdefault("imageIds", self._image_ids)
                    if self._file_refs:
                        metadata.setdefault("fileRefs", self._file_refs)
            elif message.role == "assistant":
                candidate = self._assistant_message_id()
                if isinstance(candidate, str) and candidate:
                    stable_id = candidate
            if stable_id:
                metadata.setdefault("turnId", self._turn_id)
                metadata.setdefault("messageId", stable_id)
                message = message.model_copy(update={"metadata": metadata})
                self._stamped_messages.append(message.model_copy(deep=True))
        return self._delegate.append(cwd, session_id, message, *args, **kwargs)

    def save(self, cwd: Any, session_id: str, messages: list[Any], *args: Any, **kwargs: Any) -> Any:
        """Preserve live IDs when AgentLoop rewrites the complete context.

        AgentLoop stores different ``Message`` objects in its context and in the
        append-only persistence call. Match this turn's stamped copies from the
        tail so repeated historical text cannot steal the current turn's IDs.
        """
        from iac_code.agent.message import Message

        merged = list(messages)
        search_end = len(merged)
        for stamped in reversed(self._stamped_messages):
            for index in range(search_end - 1, -1, -1):
                candidate = merged[index]
                if not isinstance(candidate, Message):
                    continue
                if candidate.role != stamped.role or candidate.content != stamped.content:
                    continue
                metadata = dict(candidate.metadata)
                for key in ("turnId", "messageId", "imageIds", "fileRefs"):
                    value = stamped.metadata.get(key)
                    if value:
                        metadata.setdefault(key, value)
                merged[index] = candidate.model_copy(update={"metadata": metadata})
                search_end = index
                break
        return self._delegate.save(cwd, session_id, merged, *args, **kwargs)


def _attach_turn_message_ids(
    agent_loop: Any,
    *,
    turn_id: str,
    translator: WebEventTranslator,
    image_ids: list[str] | None = None,
    file_refs: list[str] | None = None,
) -> None:
    storage = getattr(agent_loop, "_session_storage", None)
    if storage is None or isinstance(storage, _TurnMessageIdSessionStorage):
        return
    agent_loop._session_storage = _TurnMessageIdSessionStorage(
        storage,
        turn_id=turn_id,
        assistant_message_id=lambda: translator.current_message_id,
        image_ids=image_ids,
        file_refs=file_refs,
    )


def model_selection_for_session(session: WebSession) -> WebModelSelection:
    from iac_code import config

    provider = getattr(session, "provider", None) or config.get_active_provider_key()
    model = getattr(session, "model", None) or load_saved_model() or config.DEFAULT_MODEL
    effort = getattr(session, "effort", None) or config.load_saved_effort()
    if provider is None and config.get_llm_source() == "qwenpaw":
        from iac_code.services.qwenpaw_source import load_from_qwenpaw

        partner = load_from_qwenpaw()
        if partner is not None:
            return WebModelSelection(
                provider=partner.provider_key,
                model=partner.model,
                effort=effort,
                provider_api_key=partner.api_key,
                provider_base_url=partner.base_url,
                provider_config_frozen=True,
                provider_config_override={},
            )
    if provider is None:
        from iac_code.providers.manager import _detect_provider_name

        try:
            provider = _detect_provider_name(model)
        except ValueError:
            return WebModelSelection(provider=None, model=model, effort=effort)
    provider_config = copy.deepcopy(config.get_provider_config(provider))
    credentials = config.load_credentials(model=model)
    base_url = provider_config.get("apiBase")
    return WebModelSelection(
        provider=provider,
        model=model,
        effort=effort,
        provider_api_key=credentials.get(provider, ""),
        provider_base_url=base_url if isinstance(base_url, str) and base_url else None,
        provider_config_frozen=True,
        provider_config_override=provider_config,
    )


def agent_factory_options_for_session(
    session: WebSession,
    manager: WebSessionManager,
    *,
    model_selection: WebModelSelection | None = None,
    disable_external_services: bool = False,
) -> AgentFactoryOptions:
    """Build the same AgentFactory options for every Web operation.

    disable_external_services=True 用于会话切换时的离线上下文核算:不连接 MCP、不读钥匙串,
    只算系统提示 + 本地工具定义开销(见 prime_session_context_overhead)。
    """
    selection = model_selection or model_selection_for_session(session)
    provider_config_override = selection.provider_config_override
    if session.thinking_enabled is not None:
        # 会话级 thinking 覆盖：只作用于本会话本回合的内存副本，不落 settings.yml。
        # provider 构造（providers.manager）会从该副本读取 thinkingEnabled。
        provider_config_override = dict(provider_config_override or {})
        provider_config_override["thinkingEnabled"] = session.thinking_enabled
    return AgentFactoryOptions(
        model=selection.model,
        session_id=session.session_id,
        cwd=session.cwd,
        resume_messages=manager.load_resume_messages(session.session_id, cwd=session.cwd),
        provider_key_override=selection.provider,
        provider_api_key_override=selection.provider_api_key,
        provider_base_url_override=selection.provider_base_url,
        provider_config_frozen=selection.provider_config_frozen,
        provider_config_override=provider_config_override,
        effort_override=selection.effort,
        mcp_elicitation_handler=_make_web_mcp_elicitation_handler(session, manager),
        disable_external_services=disable_external_services,
        source="web-runtime",
    )


def _make_web_mcp_elicitation_handler(session: WebSession, manager: WebSessionManager) -> Any:
    """Bind an MCP elicitation handler to this session's browser request/answer bridge."""

    async def handler(server_name: str, params: Mapping[str, Any]) -> dict[str, Any]:
        return await manager.request_mcp_elicitation(session, server_name, params)

    return handler


def create_session_agent_runtime(
    session: WebSession,
    manager: WebSessionManager,
    *,
    model_selection: WebModelSelection | None = None,
    disable_external_services: bool = False,
) -> Any:
    return create_agent_runtime(
        agent_factory_options_for_session(
            session,
            manager,
            model_selection=model_selection,
            disable_external_services=disable_external_services,
        )
    )


async def create_session_agent_runtime_in_thread(
    session: WebSession,
    manager: WebSessionManager,
    *,
    model_selection: WebModelSelection | None = None,
    disable_external_services: bool = False,
    lifecycle_owner: set[asyncio.Future[Any]] | None = None,
) -> Any:
    """Create a session runtime off-loop and retain cleanup ownership across cancellation."""
    creation_task = asyncio.create_task(
        asyncio.to_thread(
            create_session_agent_runtime,
            session,
            manager,
            model_selection=model_selection,
            disable_external_services=disable_external_services,
        )
    )
    if lifecycle_owner is not None:
        lifecycle_owner.add(creation_task)
    try:
        runtime = await asyncio.shield(creation_task)
        if lifecycle_owner is not None:
            lifecycle_owner.discard(creation_task)
        return runtime
    except asyncio.CancelledError:
        creation_task.add_done_callback(lambda task: _close_late_created_runtime(task, lifecycle_owner=lifecycle_owner))
        raise
    except BaseException:
        if lifecycle_owner is not None:
            lifecycle_owner.discard(creation_task)
        raise


def _close_late_created_runtime(
    task: asyncio.Task[Any],
    *,
    lifecycle_owner: set[asyncio.Future[Any]] | None = None,
) -> None:
    try:
        runtime = task.result()
    except asyncio.CancelledError:
        if lifecycle_owner is not None:
            lifecycle_owner.discard(task)
        return
    except Exception:
        if lifecycle_owner is not None:
            lifecycle_owner.discard(task)
        logger.exception("Web agent runtime creation failed after its caller was cancelled")
        return
    close_task = asyncio.create_task(close_agent_runtime(runtime, lifecycle_owner=lifecycle_owner))
    if lifecycle_owner is not None:
        lifecycle_owner.add(close_task)
        close_task.add_done_callback(lifecycle_owner.discard)
        lifecycle_owner.discard(task)


async def close_agent_runtime(
    runtime: Any | None,
    *,
    lifecycle_owner: set[asyncio.Future[Any]] | None = None,
) -> None:
    """Close an AgentRuntime without allowing cleanup failures to mask the turn."""
    close = getattr(runtime, "aclose", None)
    if not callable(close):
        return
    close_task = asyncio.create_task(close())
    if lifecycle_owner is not None:
        lifecycle_owner.add(close_task)
        close_task.add_done_callback(lifecycle_owner.discard)
    try:
        done, _pending = await asyncio.wait({close_task}, timeout=WEB_RUNTIME_CLOSE_TIMEOUT_SECONDS)
    except asyncio.CancelledError:
        close_task.cancel()
        close_task.add_done_callback(_consume_close_task_result)
        raise
    if not done:
        logger.warning("Timed out closing Web agent runtime")
        close_task.cancel()
        close_task.add_done_callback(_consume_close_task_result)
        return
    try:
        close_task.result()
    except asyncio.CancelledError:
        logger.warning("Web agent runtime close was cancelled")
    except Exception:
        logger.exception("Failed to close Web agent runtime")


def _consume_close_task_result(task: asyncio.Task[Any]) -> None:
    try:
        task.result()
    except asyncio.CancelledError:
        pass
    except Exception:
        logger.exception("Web agent runtime close failed after timeout")


async def flush_web_telemetry() -> None:
    """Force a bounded telemetry flush without blocking the event loop."""
    try:
        await asyncio.wait_for(
            asyncio.to_thread(flush_telemetry),
            timeout=WEB_TELEMETRY_FLUSH_TIMEOUT_SECONDS,
        )
    except TimeoutError:
        logger.warning("Timed out flushing Web telemetry")
    except Exception:
        logger.exception("Failed to flush Web telemetry")


def runtime_mcp_status(runtime: Any) -> dict[str, Any] | None:
    return mcp_status_metadata(
        getattr(runtime, "mcp_manager", None),
        warnings=getattr(runtime, "mcp_config_warnings", None),
        pending_configs=getattr(runtime, "mcp_pending_configs", None),
    )


class FakeStreamRuntime:
    """Test runtime that emits a complete fake assistant stream."""

    def __init__(self, session: WebSession, assistant_text: str) -> None:
        self.session = session
        self.assistant_text = assistant_text

    async def start_turn(self, request: WebTurnRequest) -> dict[str, Any]:
        if self.session.turn_lock.locked():
            return dict(TURN_ALREADY_RUNNING)

        turn_id = request.turn_id or _turn_id()
        message_id = _message_id()
        async with self.session.turn_lock:
            self.session.active_turn_task = asyncio.current_task()
            try:
                await self.session.events.publish("user.message", _user_message_payload(request, turn_id=turn_id))
                await self.session.events.publish(
                    "assistant.message.start",
                    {
                        "turnId": turn_id,
                        "messageId": message_id,
                    },
                )
                await self.session.events.publish(
                    "assistant.text.delta",
                    {
                        "turnId": turn_id,
                        "messageId": message_id,
                        "delta": self.assistant_text,
                    },
                )
                await self.session.events.publish(
                    "assistant.message.end",
                    {
                        "turnId": turn_id,
                        "messageId": message_id,
                        "finishReason": "stop",
                    },
                )
                await self.session.events.publish("turn.done", _turn_done_payload(turn_id=turn_id))
            finally:
                if self.session.active_turn_task is asyncio.current_task():
                    self.session.active_turn_task = None
        return {"accepted": True, "turnId": turn_id}


class WebSessionRuntime:
    """Production runtime wrapper for normal Web chat turns."""

    def __init__(
        self,
        session: WebSession,
        *,
        manager: WebSessionManager,
        lifecycle_owner: set[asyncio.Future[Any]] | None = None,
    ) -> None:
        self.session = session
        self.manager = manager
        self.lifecycle_owner = lifecycle_owner

    async def start_turn(self, request: WebTurnRequest) -> dict[str, Any]:
        if self.session.turn_lock.locked():
            return dict(TURN_ALREADY_RUNNING)

        turn_id = request.turn_id or _turn_id()
        model_selection = request.model_selection or model_selection_for_session(self.session)
        usage = Usage()
        agent_runtime: Any | None = None
        input_consumed = False
        async with self.session.turn_lock:
            self.session.active_turn_task = asyncio.current_task()
            # 记录本轮为「上一次操作」,让侧边栏相对时间反映真实活动(否则一直显示距创建多久)。
            self.manager.touch_session_activity(self.session)
            # 新一轮开始即清未读:会话重新「进行中」后不应再被当作「已结束待查看」,
            # 否则 unread 与进行中并存,侧栏列表快照会把未读圆点画在运行中的会话上。
            self.manager.mark_session_running(self.session)
            with use_session_id(self.session.session_id):
                try:
                    user_input = self._build_user_input(request)
                    agent_runtime = await create_session_agent_runtime_in_thread(
                        self.session,
                        self.manager,
                        model_selection=model_selection,
                        lifecycle_owner=self.lifecycle_owner,
                    )
                    self._attach_session_permission_context(agent_runtime)
                    await self._attach_mcp_status_updates(agent_runtime)
                    agent_loop = getattr(agent_runtime, "agent_loop", None)
                    if request.context_modifier is not None and hasattr(agent_loop, "_apply_context_modifier"):
                        agent_loop._apply_context_modifier(request.context_modifier)
                    # 暴露活跃 agent_loop / turn id，供“引导/立即插队”端点在本轮期间即时注入。
                    self.session.active_agent_loop = agent_loop
                    self.session.active_turn_id = turn_id
                    user_event = await self.session.events.publish(
                        "user.message", _user_message_payload(request, turn_id=turn_id)
                    )
                    input_consumed = True
                    self.manager.schedule_llm_title(self.session, text=request.text, image_ids=request.image_ids)
                    # 记录本轮“回放下界”:重载进行中会话时只回放本轮事件(尚未持久化),已完成
                    # 轮次由存储转录提供,避免完成轮次被回放而重复渲染。
                    self.session.active_turn_floor_sequence = int(user_event["sequence"]) - 1
                    turn_started = time.monotonic()
                    translator = WebEventTranslator(self.session.session_id)
                    _attach_turn_message_ids(
                        agent_loop,
                        turn_id=turn_id,
                        translator=translator,
                        image_ids=request.image_ids,
                        file_refs=request.file_refs,
                    )
                    stream = await mcp_prompt_command_stream(
                        agent_loop=agent_runtime.agent_loop,
                        commands=getattr(agent_runtime, "command_registry", None),
                        prompt=user_input,
                        session_id=self.session.session_id,
                    )
                    if stream is None:
                        stream = agent_runtime.agent_loop.run_streaming(user_input)
                    # 排队消息不再在本轮内批量注入(那会让 LLM 把同批重复消息合并、少执行);
                    # 改由 app 层在本轮结束后逐条、各自独立成 turn 顺序处理。故此处不传
                    # queued_input_provider。“引导/steer”仍是显式的即时插队(见 session_manager)。
                    async for stream_event in stream:
                        inner_event, sub_pipeline_payload = _unwrap_sub_pipeline_event(stream_event)
                        message_end_context_usage: dict[str, Any] | None = None
                        if isinstance(inner_event, MessageEndEvent):
                            _accumulate_usage(usage, inner_event.usage)
                            # 每个模型往返结束即快照实时上下文用量,附到 message.end,
                            # 让前端在本轮进行中就刷新上下文圆环(而非仅在会话加载/切换时)。
                            message_end_context_usage = _live_context_usage(agent_runtime)
                            _cache_session_context_overhead(self.session, message_end_context_usage)
                        if isinstance(inner_event, PermissionRequestEvent):
                            payload = _permission_request_payload(
                                inner_event,
                                turn_id=turn_id,
                                allow_always=self._tool_supports_blanket_allow(agent_runtime, inner_event.tool_name),
                            )
                            payload.update(sub_pipeline_payload)
                            request_id = self.manager.add_permission_request(
                                self.session,
                                payload,
                                future=inner_event.response_future,
                                audit_event=inner_event,
                            )
                            await self._await_permission_request(request_id, inner_event)
                            continue
                        if isinstance(inner_event, AskUserQuestionEvent):
                            payload = _question_request_payload(inner_event, turn_id=turn_id)
                            payload.update(sub_pipeline_payload)
                            request_id = self.manager.add_question_request(
                                self.session,
                                payload,
                                future=inner_event.response_future,
                            )
                            await self._await_question_request(request_id, inner_event)
                            continue
                        if isinstance(inner_event, QueuedInputSubmittedEvent):
                            # agent 在本轮中消费了一条排队输入:它已被注入上下文并持久化,刷新后会作为
                            # 独立用户消息出现。这里必须同步补一条 user.message 用户气泡,否则实时视图只移除
                            # 排队 chip 却不显示气泡,连发多条会折叠成“看不见”,用户数不清究竟执行了几次。
                            # 每条用显式且唯一的 messageId(复刻 steer 的 user-<turnId>-…-<uuid> 方案),
                            # 否则同 turnId 缺省会折叠成一个气泡并覆盖首条 prompt。
                            await self.session.events.publish(
                                "user.message",
                                {
                                    "messageId": inner_event.message_id
                                    or "user-{}-queued-{}".format(turn_id, uuid.uuid4().hex[:8]),
                                    "turnId": turn_id,
                                    "text": inner_event.text,
                                    "imageIds": [],
                                    "fileRefs": [],
                                    "source": "queued",
                                },
                            )
                            translated = translator.translate_stream_event(stream_event, turn_id=turn_id)
                            await self.session.events.publish(translated["type"], translated["payload"])
                            continue
                        translated = translator.translate_stream_event(stream_event, turn_id=turn_id)
                        if message_end_context_usage is not None and translated.get("type") == "assistant.message.end":
                            translated["payload"]["contextUsage"] = message_end_context_usage
                        await self.session.events.publish(translated["type"], translated["payload"])
                    # 记录本轮耗时：持久化到最后一条 assistant 消息，服务器重启后仍可显示「已处理 <时间>」。
                    elapsed = time.monotonic() - turn_started
                    if elapsed >= 1.0:
                        self._stamp_turn_elapsed(agent_runtime, elapsed)
                    done_payload = _turn_done_payload(turn_id=turn_id)
                    done_payload["elapsedMs"] = int(elapsed * 1000)
                    done_payload["usage"] = usage_payload(usage)
                    # 本轮结算后的上下文用量(含最后一轮工具结果),让圆环停在准确的最终值。
                    final_context_usage = _live_context_usage(agent_runtime)
                    if final_context_usage is not None:
                        done_payload["contextUsage"] = final_context_usage
                        _cache_session_context_overhead(self.session, final_context_usage)
                    await self.session.events.publish("turn.done", done_payload)
                    # 正常结束:若此刻无人在看(用户已切走、无活跃 SSE 订阅),标记为未读。
                    self.manager.mark_session_completed(self.session)
                except asyncio.CancelledError:
                    self.manager.cancel_pending_requests_for_session(self.session)
                    canceled_payload = _turn_done_payload(turn_id=turn_id)
                    canceled_payload["interrupted"] = True
                    canceled_payload["canceled"] = True
                    canceled_payload["usage"] = usage_payload(usage)
                    await self.session.events.publish("turn.done", canceled_payload)
                    return {
                        "accepted": False,
                        "reason": "turn canceled",
                        "turnId": turn_id,
                        "inputConsumed": input_consumed,
                    }
                except Exception as exc:
                    self.manager.cancel_pending_requests_for_session(self.session)
                    await self.session.events.publish(
                        "error",
                        {
                            "turnId": turn_id,
                            "message": str(exc)[:500],
                            "retryable": False,
                        },
                    )
                    failed_payload = _turn_done_payload(turn_id=turn_id)
                    failed_payload["failed"] = True
                    failed_payload["usage"] = usage_payload(usage)
                    await self.session.events.publish("turn.done", failed_payload)
                    return {
                        "accepted": False,
                        "reason": "runtime error",
                        "turnId": turn_id,
                        "inputConsumed": input_consumed,
                    }
                finally:
                    try:
                        await close_agent_runtime(agent_runtime, lifecycle_owner=self.lifecycle_owner)
                    finally:
                        try:
                            await flush_web_telemetry()
                        finally:
                            if self.session.active_turn_task is asyncio.current_task():
                                self.session.active_turn_task = None
                                self.session.active_agent_loop = None
                                self.session.active_turn_id = None
                                self.session.active_turn_floor_sequence = None
        return {"accepted": True, "turnId": turn_id, "inputConsumed": True}

    async def _attach_mcp_status_updates(self, runtime: Any) -> None:
        async def publish_status(_server_name: str = "", _capability: str = "") -> None:
            status = runtime_mcp_status(runtime)
            if status is not None:
                await self.session.events.publish("mcp.status.updated", status)

        add_listener = getattr(runtime, "add_mcp_change_listener", None)
        if callable(add_listener):
            add_listener(publish_status)
        await publish_status()

    def _stamp_turn_elapsed(self, runtime: Any, elapsed: float) -> None:
        """Persist the turn duration onto the last assistant message (best-effort)."""
        agent_loop = getattr(runtime, "agent_loop", None)
        stamp = getattr(agent_loop, "stamp_last_turn_elapsed", None)
        if callable(stamp):
            try:
                stamp(elapsed)
            except Exception:  # noqa: BLE001 - persistence is best-effort, never fail the turn
                pass

    def _attach_session_permission_context(self, runtime: Any) -> None:
        agent_loop = getattr(runtime, "agent_loop", None)
        if agent_loop is None:
            return
        runtime_context = getattr(agent_loop, "_permission_context", None)
        if self.session.permission_context is None and runtime_context is not None:
            # 首次运行本会话：采用运行时新建的 context，但要把会话已选定的权限模式
            # （如新会话草稿里选的「完全访问」）应用上去，否则会退回默认模式。
            if self.session.permission_mode is not None:
                runtime_context.mode = self.session.permission_mode
            self.session.permission_context = runtime_context
        if self.session.permission_context is not None:
            setattr(agent_loop, "_permission_context", self.session.permission_context)
            setattr(agent_loop, "_permission_context_getter", lambda: self.session.permission_context)
            tool_registry = getattr(runtime, "tool_registry", None)
            agent_tool = tool_registry.get("agent") if hasattr(tool_registry, "get") else None
            if agent_tool is not None and hasattr(agent_tool, "_permission_context"):
                setattr(agent_tool, "_permission_context", self.session.permission_context)
            if agent_tool is not None and hasattr(agent_tool, "_permission_context_getter"):
                setattr(agent_tool, "_permission_context_getter", lambda: self.session.permission_context)

    def _tool_supports_blanket_allow(self, runtime: Any, tool_name: str) -> bool:
        tool_registry = getattr(runtime, "tool_registry", None)
        tool = tool_registry.get(tool_name) if hasattr(tool_registry, "get") else None
        return bool(getattr(tool, "supports_blanket_allow", False))

    async def _await_permission_request(self, request_id: str, event: PermissionRequestEvent) -> None:
        if event.response_future is None:
            self.manager.cancel_permission_request(request_id, session_id=self.session.session_id)
            return
        try:
            await asyncio.shield(event.response_future)
        except asyncio.CancelledError:
            self.manager.cancel_permission_request(request_id, session_id=self.session.session_id)
            raise
        self.manager.discard_permission_request(request_id, session_id=self.session.session_id)

    async def _await_question_request(self, request_id: str, event: AskUserQuestionEvent) -> None:
        if event.response_future is None:
            self.manager.cancel_question_request(request_id, session_id=self.session.session_id)
            return
        try:
            await asyncio.shield(event.response_future)
        except asyncio.CancelledError:
            self.manager.cancel_question_request(request_id, session_id=self.session.session_id)
            raise
        self.manager.discard_question_request(request_id, session_id=self.session.session_id)

    def _build_user_input(self, request: WebTurnRequest) -> str | list[ContentBlock]:
        text = _append_file_references(request.text, request.file_refs, cwd=self.session.cwd)
        if not request.image_ids:
            return text

        from iac_code.web.images import load_cached_image

        blocks: list[ContentBlock] = []
        if text:
            blocks.append(TextBlock(text=text))
        for image_id in request.image_ids:
            image = load_cached_image(image_id, cwd=self.session.cwd, session_id=self.session.session_id)
            blocks.append(
                ImageBlock(
                    media_type=image.media_type,
                    data=image.base64_data,
                )
            )
        return blocks


def _append_file_references(text: str, file_refs: list[str], *, cwd: str) -> str:
    if not file_refs:
        return text

    from iac_code.web.files import safe_file_references

    references = safe_file_references(file_refs, cwd=cwd)
    if not references:
        return text
    reference_text = "\n".join("- {}".format(reference) for reference in references)
    if text.strip():
        return _("{}\n\nReferenced files:\n{}").format(text, reference_text)
    return _("Referenced files:\n{}").format(reference_text)


def _unwrap_sub_pipeline_event(stream_event: object) -> tuple[object, dict[str, Any]]:
    payload: dict[str, Any] = {}
    while isinstance(stream_event, SubPipelineStreamEvent):
        payload["subPipelineId"] = stream_event.sub_pipeline_id
        payload["candidateIndex"] = stream_event.candidate_index
        stream_event = stream_event.inner
    return stream_event, payload


def _accumulate_usage(total: Usage, usage: Usage) -> None:
    total.input_tokens += usage.input_tokens
    total.output_tokens += usage.output_tokens
    total.cache_creation_input_tokens += usage.cache_creation_input_tokens
    total.cache_read_input_tokens += usage.cache_read_input_tokens


def _live_context_usage(runtime: object) -> dict[str, Any] | None:
    """本轮进行中的实时上下文用量,取自活跃 agent_loop 的 context_manager。

    与 `/status`(session_manager 里据持久化消息重建)同源,但直接读活跃循环的内存
    context_manager,无需重新落盘/加载。用于让 composer 上下文圆环在 turn 进行中(每个
    模型往返)即时刷新。取不到时返回 None(前端据此不改动圆环)。
    """
    agent_loop = getattr(runtime, "agent_loop", None)
    context_manager = getattr(agent_loop, "context_manager", None)
    if context_manager is None:
        return None
    try:
        return _camelize(context_manager.get_usage())
    except Exception:
        return None


def _cache_session_context_overhead(session: Any, usage: dict[str, Any] | None) -> None:
    """把实时用量里的系统提示/工具定义开销缓存到会话。

    /status 据持久化消息重建上下文用量时拿不到系统提示与工具定义,会比这里的实时圆环少算这两项;
    缓存后让重载路径补齐,两处口径一致(见 session_manager._context_usage_payload)。
    """
    if session is None or not isinstance(usage, dict):
        return
    system_prompt_tokens = usage.get("systemPromptTokens")
    tool_definition_tokens = usage.get("toolDefinitionTokens")
    if isinstance(system_prompt_tokens, int) and system_prompt_tokens >= 0:
        session.context_system_prompt_tokens = system_prompt_tokens
    if isinstance(tool_definition_tokens, int) and tool_definition_tokens >= 0:
        session.context_tool_definition_tokens = tool_definition_tokens


async def prime_session_context_overhead(
    session: WebSession,
    manager: WebSessionManager,
    *,
    lifecycle_owner: set[asyncio.Future[Any]] | None = None,
) -> None:
    """切换/打开会话时一次性算出系统提示 + 工具定义开销并缓存到会话。

    /status 与 get_session 据持久化消息重建上下文用量,拿不到系统提示与工具定义;服务器重启后、
    首个实时回合之前,会比 composer 圆环少算这约 13k 固定开销(见 _cache_session_context_overhead),
    表现为恢复会话时状态面板严重偏低。切换会话是低频动作,这里临时建一次 runtime 读取真实开销缓存起来
    (AgentLoop.__init__ 已在构造时同步系统提示与工具定义,直接读 get_usage 即含两项),之后的实时回合
    继续自动纠正。仅在开销未知(两项皆 0)时计算——建过一次或已有实时数据即跳过;失败安全降级为 0,
    维持既有行为,绝不阻断会话切换。

    会话切换只读展示历史会话,不应产生外部副作用,因此这里用 disable_external_services 的离线核算
    runtime:不连接 MCP、不读取 MCP 钥匙串(避免 macOS 反复弹出 iac-code:mcp 授权窗)、不发起 Provider/
    云请求。代价是动态 MCP 工具定义暂不计入本地基线,待首个真实回合启动正常 runtime 后用精确值自动纠正。
    """
    if session is None:
        return
    if session.context_system_prompt_tokens or session.context_tool_definition_tokens:
        return
    runtime: Any = None
    try:
        runtime = await create_session_agent_runtime_in_thread(
            session,
            manager,
            disable_external_services=True,
            lifecycle_owner=lifecycle_owner,
        )
        _cache_session_context_overhead(session, _live_context_usage(runtime))
    except asyncio.CancelledError:
        raise
    except Exception:
        logger.exception("Failed to prime session context overhead")
    finally:
        if runtime is not None:
            await close_agent_runtime(runtime, lifecycle_owner=lifecycle_owner)


def _permission_request_payload(
    event: PermissionRequestEvent,
    *,
    turn_id: str,
    allow_always: bool = False,
) -> dict[str, Any]:
    return {
        "turnId": turn_id,
        "toolName": event.tool_name,
        "toolUseId": event.tool_use_id,
        "toolInput": event.tool_input,
        "message": _permission_message(event),
        "suggestions": _permission_suggestions(event),
        "allowAlways": allow_always,
    }


def _question_request_payload(event: AskUserQuestionEvent, *, turn_id: str) -> dict[str, Any]:
    return {
        "turnId": turn_id,
        "toolUseId": event.tool_use_id,
        "question": event.question,
        "options": event.options,
        "allowFreeText": event.allow_free_text,
        "freeTextPrompt": event.free_text_prompt,
    }


def _permission_message(event: PermissionRequestEvent) -> str:
    permission_result = event.permission_result
    message = getattr(permission_result, "message", "") if permission_result is not None else ""
    return str(message) if message else _("Allow {}?").format(event.tool_name)


def _permission_suggestions(event: PermissionRequestEvent) -> list[dict[str, str]]:
    permission_result = event.permission_result
    raw_suggestions = getattr(permission_result, "suggestions", None) if permission_result is not None else None
    if not raw_suggestions:
        return []
    return [
        {
            "toolName": str(getattr(suggestion, "tool_name", "")),
            "ruleContent": str(getattr(suggestion, "rule_content", "")),
        }
        for suggestion in raw_suggestions
    ]
