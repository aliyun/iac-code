"""Event primitives and replay buffer for the local Web workbench."""

from __future__ import annotations

import asyncio
import json
import logging
import math
from collections import deque
from collections.abc import AsyncIterator, Callable, Iterator, Mapping
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import fields, is_dataclass
from datetime import date, datetime, time, timezone
from os import PathLike
from pathlib import PurePath
from typing import Any

from iac_code.mcp.progress import mcp_progress_metadata
from iac_code.types.stream_events import (
    AskUserQuestionEvent,
    CandidateDetailEvent,
    CompactionEvent,
    DiagramEvent,
    ErrorEvent,
    MCPProgressEvent,
    MessageEndEvent,
    MessageStartEvent,
    PermissionRequestEvent,
    PlanEvent,
    QueuedInputSubmittedEvent,
    ResourceObservedEvent,
    StackInstancesProgressEvent,
    StackOperationStartedEvent,
    StackProgressEvent,
    StreamEvent,
    SubAgentToolEvent,
    SubPipelineStreamEvent,
    TaskNotificationEvent,
    TextDeltaEvent,
    ThinkingDeltaEvent,
    TombstoneEvent,
    ToolInputDeltaEvent,
    ToolResultEvent,
    ToolUseEndEvent,
    ToolUseStartEvent,
    Usage,
)

logger = logging.getLogger(__name__)
PIPELINE_IDENTITY_FIELDS = ("contextId", "taskId", "lastSequence")
_EVENT_OBSERVER: ContextVar[Callable[[dict[str, Any]], None] | None] = ContextVar(
    "iac_code_web_event_observer",
    default=None,
)
_REGION_FIELDS = ("regionId", "RegionId", "region_id", "region")
_SSE_EVENT_TYPE_ALIASES = {
    "error": "app.error",
}


def _notify_event_observer(observer: Callable[[dict[str, Any]], None], event: dict[str, Any]) -> None:
    try:
        observer(event)
    except Exception:
        logger.exception("Web event observer failed")


@contextmanager
def observe_published_events(observer: Callable[[dict[str, Any]], None]) -> Iterator[None]:
    """Observe events published in the current async context."""
    previous = _EVENT_OBSERVER.get()
    if previous is None:
        token = _EVENT_OBSERVER.set(observer)
    else:

        def chained_observer(event: dict[str, Any]) -> None:
            _notify_event_observer(previous, event)
            _notify_event_observer(observer, event)

        token = _EVENT_OBSERVER.set(chained_observer)
    try:
        yield
    finally:
        _EVENT_OBSERVER.reset(token)


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _is_dataclass_instance(value: Any) -> bool:
    return is_dataclass(value) and not isinstance(value, type)


def _coerce_for_json(value: Any) -> Any:
    if _is_dataclass_instance(value):
        return {field.name: _coerce_for_json(getattr(value, field.name)) for field in fields(value)}
    if isinstance(value, Mapping):
        return {str(key): _coerce_for_json(item) for key, item in value.items()}
    if isinstance(value, (list, tuple, set, frozenset)):
        return [_coerce_for_json(item) for item in value]
    return value


def _normalize_event_value(value: Any) -> Any:
    if value is None or isinstance(value, (int, bool)):
        return value
    if isinstance(value, str):
        return value
    if isinstance(value, float):
        if math.isfinite(value):
            return value
        if math.isnan(value):
            return "nan"
        return "inf" if value > 0 else "-inf"
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, (date, time)):
        return value.isoformat()
    if isinstance(value, PurePath):
        # POSIX separators keep the serialized path stable across platforms
        # (str() on a Windows path would emit backslashes).
        return value.as_posix()
    if isinstance(value, PathLike):
        return str(value)
    if isinstance(value, (bytes, bytearray, memoryview)):
        return bytes(value).decode("utf-8", errors="replace")
    if isinstance(value, Mapping):
        return {str(key): _normalize_event_value(item) for key, item in value.items()}
    if isinstance(value, (list, tuple, set, frozenset)):
        return [_normalize_event_value(item) for item in value]
    return str(value)


def normalize_event_payload(payload: dict[str, Any]) -> dict[str, Any]:
    """Return a JSON-safe local Web payload copy without generic redaction."""
    return _normalize_event_value(_coerce_for_json(payload))


def _first_region_id(items: list[dict[str, Any]]) -> str | None:
    for item in items:
        if not isinstance(item, Mapping):
            continue
        for field in _REGION_FIELDS:
            value = item.get(field)
            if isinstance(value, str) and value.strip():
                return value.strip()
    return None


def _stack_deployment_succeeded(status: str) -> bool:
    return status == "CREATE_COMPLETE"


def make_event(session_id: str, sequence: int, event_type: str, payload: dict[str, Any]) -> dict[str, Any]:
    """Create a browser-safe event dictionary with JSON-safe payload contents."""
    normalized_payload = normalize_event_payload(payload)
    event = {
        "type": event_type,
        "sequence": sequence,
        "sessionId": session_id,
        "createdAt": _utc_now(),
        "payload": normalized_payload,
    }
    for field in PIPELINE_IDENTITY_FIELDS:
        if field in normalized_payload:
            event[field] = normalized_payload[field]
    return event


def make_resync_event(session_id: str, *, after_sequence: int, floor_sequence: int) -> dict[str, Any]:
    """Create an ephemeral event telling clients to resync from session state."""
    return make_event(
        session_id,
        0,
        "session.resync.required",
        {
            "afterSequence": after_sequence,
            "floorSequence": floor_sequence,
        },
    )


class WebEventTranslator:
    """Translate agent stream callbacks into browser-safe Web events.

    Returned events intentionally use sequence 0. Callers publish the type and
    payload through WebEventBuffer so replay order is assigned in one place.
    """

    def __init__(self, session_id: str) -> None:
        self.session_id = session_id
        self._current_message_id: str | None = None
        self._sub_pipeline_message_ids: dict[str, str | None] = {}

    @property
    def current_message_id(self) -> str | None:
        """ID of the provider message currently being translated."""
        return self._current_message_id

    def _make(self, event_type: str, payload: dict[str, Any]) -> dict[str, Any]:
        return make_event(self.session_id, 0, event_type, payload)

    def assistant_message_start(
        self,
        *,
        turn_id: str,
        message_id: str,
        provider: str | None = None,
        model: str | None = None,
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "turnId": turn_id,
            "messageId": message_id,
        }
        if provider is not None:
            payload["provider"] = provider
        if model is not None:
            payload["model"] = model
        return self._make("assistant.message.start", payload)

    def assistant_start(
        self,
        *,
        turn_id: str,
        message_id: str,
        provider: str | None = None,
        model: str | None = None,
    ) -> dict[str, Any]:
        return self.assistant_message_start(
            turn_id=turn_id,
            message_id=message_id,
            provider=provider,
            model=model,
        )

    def assistant_text_delta(self, *, message_id: str, delta: str, turn_id: str | None = None) -> dict[str, Any]:
        payload = {
            "messageId": message_id,
            "delta": delta,
        }
        if turn_id is not None:
            payload["turnId"] = turn_id
        return self._make("assistant.text.delta", payload)

    def assistant_thinking_delta(
        self,
        *,
        message_id: str | None,
        delta: str,
        turn_id: str | None = None,
    ) -> dict[str, Any]:
        payload = {
            "messageId": message_id,
            "delta": delta,
        }
        if turn_id is not None:
            payload["turnId"] = turn_id
        return self._make("assistant.thinking.delta", payload)

    def tool_started(
        self,
        *,
        tool_use_id: str,
        tool_name: str,
        parent_tool_use_id: str | None = None,
        message_id: str | None = None,
        turn_id: str | None = None,
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "toolUseId": tool_use_id,
            "toolName": tool_name,
            "parentToolUseId": parent_tool_use_id,
            "status": "running",
        }
        # 携带 messageId/turnId，前端才能把工具卡片挂到对应的助手消息上，
        # 与会话恢复时的行内渲染保持一致（否则实时流里工具会掉到底部活动区）。
        if message_id:
            payload["messageId"] = message_id
        if turn_id:
            payload["turnId"] = turn_id
        return self._make("tool.started", payload)

    def tool_input_delta(self, *, tool_use_id: str, delta: str) -> dict[str, Any]:
        return self._make(
            "tool.input.delta",
            {
                "toolUseId": tool_use_id,
                "delta": delta,
            },
        )

    def tool_result(
        self,
        *,
        tool_use_id: str,
        result_kind: str,
        summary: Any,
        artifacts: list[Any],
    ) -> dict[str, Any]:
        return self._make(
            "tool.result",
            {
                "toolUseId": tool_use_id,
                "resultKind": result_kind,
                "summary": summary,
                "artifacts": list(artifacts),
            },
        )

    def tool_finished(
        self,
        *,
        tool_use_id: str,
        status: str,
        elapsed_ms: int | float | None,
        summary: Any | None,
    ) -> dict[str, Any]:
        return self._make(
            "tool.finished",
            {
                "toolUseId": tool_use_id,
                "status": status,
                "elapsedMs": elapsed_ms,
                "summary": summary,
            },
        )

    def assistant_message_tombstone(self, *, message_id: str, affected_tool_use_ids: list[str]) -> dict[str, Any]:
        return self._make(
            "assistant.message.tombstone",
            {
                "messageId": message_id,
                "affectedToolUseIds": list(affected_tool_use_ids),
            },
        )

    def tombstone(self, *, message_id: str, affected_tool_use_ids: list[str]) -> dict[str, Any]:
        return self.assistant_message_tombstone(
            message_id=message_id,
            affected_tool_use_ids=affected_tool_use_ids,
        )

    def translate_stream_event(self, event: StreamEvent, *, turn_id: str) -> dict[str, Any]:
        """Translate one AgentLoop stream event into the Web event contract."""
        if isinstance(event, SubPipelineStreamEvent):
            parent_message_id = self._current_message_id
            self._current_message_id = self._sub_pipeline_message_ids.get(event.sub_pipeline_id)
            try:
                translated = self.translate_stream_event(event.inner, turn_id=turn_id)
                self._sub_pipeline_message_ids[event.sub_pipeline_id] = self._current_message_id
            finally:
                self._current_message_id = parent_message_id
            payload = dict(translated["payload"])
            payload["subPipelineId"] = event.sub_pipeline_id
            payload["candidateIndex"] = event.candidate_index
            return self._make(str(translated["type"]), payload)
        if isinstance(event, MessageStartEvent):
            self._current_message_id = event.message_id
            return self.assistant_message_start(turn_id=turn_id, message_id=event.message_id)
        if isinstance(event, TextDeltaEvent):
            return self.assistant_text_delta(
                turn_id=turn_id,
                message_id=self._current_message_id or "",
                delta=event.text,
            )
        if isinstance(event, ThinkingDeltaEvent):
            return self.assistant_thinking_delta(
                turn_id=turn_id,
                message_id=self._current_message_id,
                delta=event.text,
            )
        if isinstance(event, ToolUseStartEvent):
            return self.tool_started(
                tool_use_id=event.tool_use_id,
                tool_name=event.name,
                message_id=self._current_message_id,
                turn_id=turn_id,
            )
        if isinstance(event, ToolInputDeltaEvent):
            return self.tool_input_delta(tool_use_id=event.tool_use_id, delta=event.partial_json)
        if isinstance(event, ToolUseEndEvent):
            return self.tool_finished(
                tool_use_id=event.tool_use_id,
                status="input_complete",
                elapsed_ms=None,
                summary={"toolName": event.name, "input": event.input},
            )
        if isinstance(event, ToolResultEvent):
            from iac_code.tools.cloud.aliyun.result_contract import ALIYUN_HTTP_METADATA_KEY
            from iac_code.types.stream_events import TOOL_RENDER_METADATA_KEY

            public_metadata = dict(event.metadata or {})
            # 与回放路径对齐:内部渲染载体(_iac_code_tool_render)与阿里云 HTTP 诊断
            # (aliyun_http)都是内部键,不能作为「Artifacts」原样下发给前端。
            public_metadata.pop(ALIYUN_HTTP_METADATA_KEY, None)
            public_metadata.pop(TOOL_RENDER_METADATA_KEY, None)
            return self.tool_result(
                tool_use_id=event.tool_use_id,
                result_kind="error" if event.is_error else "text",
                summary=event.result,
                artifacts=[public_metadata] if public_metadata else [],
            )
        if isinstance(event, MCPProgressEvent):
            payload = mcp_progress_metadata(event)
            payload["turnId"] = turn_id
            return self._make("tool.progress", payload)
        if isinstance(event, MessageEndEvent):
            return self._make(
                "assistant.message.end",
                {
                    "turnId": turn_id,
                    "messageId": self._current_message_id,
                    "finishReason": event.stop_reason,
                    "usage": usage_payload(event.usage),
                },
            )
        if isinstance(event, PermissionRequestEvent):
            return self._make(
                "permission.request",
                {
                    "turnId": turn_id,
                    "toolName": event.tool_name,
                    "toolUseId": event.tool_use_id,
                    "toolInput": event.tool_input,
                },
            )
        if isinstance(event, AskUserQuestionEvent):
            return self._make(
                "question.request",
                {
                    "turnId": turn_id,
                    "toolUseId": event.tool_use_id,
                    "question": event.question,
                    "options": event.options,
                    "allowFreeText": event.allow_free_text,
                    "freeTextPrompt": event.free_text_prompt,
                },
            )
        if isinstance(event, TombstoneEvent):
            return self.assistant_message_tombstone(
                message_id=event.message_id,
                affected_tool_use_ids=list(event.affected_tool_use_ids),
            )
        if isinstance(event, TaskNotificationEvent):
            return self._make(
                "task.notification",
                {
                    "taskId": event.task_id,
                    "description": event.description,
                    "status": event.status,
                    "result": event.result,
                    "error": event.error,
                },
            )
        if isinstance(event, QueuedInputSubmittedEvent):
            payload: dict[str, Any] = {
                "turnId": turn_id,
                "text": event.text,
            }
            if event.message_id:
                payload["messageId"] = event.message_id
            return self._make(
                "queued-input.submitted",
                payload,
            )
        if isinstance(event, CompactionEvent):
            if event.phase == "started":
                return self._make(
                    "compaction.started",
                    {
                        "auto": True,
                        "state": "started",
                        "available": True,
                    },
                )
            if event.phase == "failed":
                return self._make(
                    "compaction.finished",
                    {
                        "auto": True,
                        "state": "failed",
                        "reason": event.reason,
                    },
                )
            return self._make(
                "compaction.finished",
                {
                    "originalTokens": event.original_tokens,
                    "compactedTokens": event.compacted_tokens,
                },
            )
        if isinstance(event, ErrorEvent):
            return self._make(
                "error",
                {
                    "message": event.error,
                    "retryable": event.is_retryable,
                    "errorId": event.error_id,
                },
            )
        if isinstance(event, SubAgentToolEvent):
            return self._make(
                "subagent.event",
                {
                    "parentToolUseId": event.parent_tool_use_id,
                    "childToolName": event.child_tool_name,
                    "childToolInput": event.child_tool_input,
                    "isDone": event.is_done,
                    "isError": event.is_error,
                },
            )
        if isinstance(event, ResourceObservedEvent):
            return self._make(
                "resource.observed",
                {
                    "turnId": turn_id,
                    "provider": event.provider,
                    "resourceType": event.resource_type,
                    "resourceId": event.resource_id,
                    "resourceName": event.resource_name,
                    "regionId": event.region_id,
                    "action": event.action,
                    "toolName": event.tool_name,
                    "toolUseId": event.tool_use_id,
                    "metadata": event.metadata,
                },
            )
        if isinstance(event, StackOperationStartedEvent):
            # t0 for non-create stack operations, bridged onto the same "resource.observed"
            # SSE the frontend already consumes so *_IN_PROGRESS shows immediately.
            return self._make(
                "resource.observed",
                {
                    "turnId": turn_id,
                    "provider": event.provider,
                    "resourceType": "stack",
                    "resourceId": event.stack_id,
                    "resourceName": event.stack_name,
                    "regionId": event.region_id,
                    "action": event.action,
                    "toolName": event.tool_name,
                    "toolUseId": event.tool_use_id,
                    "metadata": {},
                },
            )
        if isinstance(event, DiagramEvent):
            return self._make(
                "diagram.render",
                {
                    "candidateName": event.candidate_name,
                    "templateContent": event.template_content,
                    "mermaidSource": event.mermaid_source,
                    "candidateIndex": event.candidate_index,
                },
            )
        if isinstance(event, CandidateDetailEvent):
            return self._make(
                "candidate.detail",
                {
                    "toolUseId": event.tool_use_id,
                    "candidateName": event.candidate_name,
                    "summary": event.summary,
                    "costItems": event.cost_items,
                    "totalMonthlyCost": event.total_monthly_cost,
                    "candidateIndex": event.candidate_index,
                },
            )
        if isinstance(event, StackProgressEvent):
            return self._make(
                "pipeline.event",
                {
                    "kind": "stack.progress",
                    "toolUseId": event.tool_use_id,
                    "stackId": event.stack_id,
                    "stackName": event.stack_name,
                    "regionId": event.region_id or _first_region_id(event.resources),
                    "status": event.status,
                    "progress": event.progress_percentage,
                    "progressPercentage": event.progress_percentage,
                    "deploymentSucceeded": _stack_deployment_succeeded(event.status),
                    "deploymentComplete": _stack_deployment_succeeded(event.status),
                    "resources": event.resources,
                    "elapsedSeconds": event.elapsed_seconds,
                },
            )
        if isinstance(event, StackInstancesProgressEvent):
            return self._make(
                "pipeline.event",
                {
                    "kind": "stack.instances.progress",
                    "toolUseId": event.tool_use_id,
                    "stackGroupName": event.stack_group_name,
                    "operationId": event.operation_id,
                    "regionId": _first_region_id(event.instances),
                    "status": event.status,
                    "progress": event.progress_percentage,
                    "progressPercentage": event.progress_percentage,
                    "instances": event.instances,
                    "elapsedSeconds": event.elapsed_seconds,
                },
            )
        if isinstance(event, PlanEvent):
            return self._make(
                "plan.updated",
                {
                    "turnId": turn_id,
                    "steps": [
                        {
                            "content": step.content,
                            "status": step.status,
                            "priority": step.priority,
                        }
                        for step in event.steps
                    ],
                },
            )
        return self._make("debug.stream_event", {"event": event})


def usage_payload(usage: Usage) -> dict[str, int]:
    return {
        "inputTokens": usage.input_tokens,
        "outputTokens": usage.output_tokens,
        "cacheCreationInputTokens": usage.cache_creation_input_tokens,
        "cacheReadInputTokens": usage.cache_read_input_tokens,
        "totalTokens": usage.total_tokens,
    }


async def publish_translated_event(session: Any, event: Mapping[str, Any]) -> dict[str, Any]:
    """Publish translated output through a session buffer so it receives a real sequence."""
    return await session.events.publish(str(event["type"]), dict(event["payload"]))


class WebEventBuffer:
    """Bounded per-session event buffer with replay and live streaming support."""

    def __init__(self, session_id: str, max_events: int = 500) -> None:
        if max_events <= 0:
            raise ValueError("max_events must be positive")
        self.session_id = session_id
        self.max_events = max_events
        self._events: deque[dict[str, Any]] = deque(maxlen=max_events)
        self._next_sequence = 1
        self._condition = asyncio.Condition()
        # 活跃的 stream_after 消费者数量（即实时 SSE 观看者）。用于判定会话结束时是否有人在看。
        self._subscribers = 0

    @property
    def subscriber_count(self) -> int:
        """Return the number of live stream_after consumers currently attached."""
        return self._subscribers

    @property
    def floor_sequence(self) -> int:
        """Return the earliest event sequence still available for replay."""
        if not self._events:
            return 0
        return int(self._events[0]["sequence"])

    @property
    def latest_sequence(self) -> int:
        """Return the most recent sequence assigned by this buffer."""
        return self._next_sequence - 1

    def ensure_sequence_above(self, floor: int) -> None:
        """Monotonically raise the next sequence so it outranks ``floor``.

        前端重载会把存储转录里的可见行按位置重新编号为 1..N,再让实时事件按 buffer
        序号排序。服务器重启后 buffer 从 1 重新计数,新实时事件(如流水线恢复后补发的
        step 标记)会拿到 1..k 的低序号,排到存储行(1..N)之上——顺序错乱(Issue 3)。
        转录加载时以可见行数 N 播种,确保后续 ``append`` 的序号 > N。单调:只抬不降,
        绝不回退已推进的计数器,故对活跃 buffer 的重复调用是安全的无操作。
        """
        target = int(floor) + 1
        if target > self._next_sequence:
            self._next_sequence = target

    def append(self, event_type: str, payload: dict[str, Any]) -> dict[str, Any]:
        """Append an event and return the created event dictionary."""
        event = make_event(self.session_id, self._next_sequence, event_type, payload)
        self._next_sequence += 1
        self._events.append(event)
        observer = _EVENT_OBSERVER.get()
        if observer is not None:
            _notify_event_observer(observer, event)
        self._notify_waiters_from_running_loop()
        return event

    def _notify_waiters_from_running_loop(self) -> None:
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return
        loop.create_task(self._notify_waiters())

    async def _notify_waiters(self) -> None:
        async with self._condition:
            self._condition.notify_all()

    async def publish(self, event_type: str, payload: dict[str, Any]) -> dict[str, Any]:
        """Append an event and wake live stream listeners."""
        async with self._condition:
            event = self.append(event_type, payload)
            self._condition.notify_all()
            return event

    def replay_after(self, after_sequence: int) -> list[dict[str, Any]]:
        """Return buffered events with sequence numbers greater than after_sequence."""
        return [event for event in self._events if int(event["sequence"]) > after_sequence]

    def requires_resync(self, *, after_sequence: int) -> bool:
        """Return whether after_sequence is too old to replay without gaps."""
        return bool(self._events) and after_sequence < self.floor_sequence - 1

    async def stream_after(self, after_sequence: int) -> AsyncIterator[dict[str, Any]]:
        """Yield replayable buffered events first, then live published events."""
        # 进入即计一名实时观看者，任何退出路径(正常结束/resync 提前 return/生成器被关闭)
        # 都经 finally 归还，保证 subscriber_count 精确反映当前在看人数。
        self._subscribers += 1
        try:
            last_sequence = after_sequence
            if self.requires_resync(after_sequence=last_sequence):
                yield make_resync_event(
                    self.session_id,
                    after_sequence=last_sequence,
                    floor_sequence=self.floor_sequence,
                )
                return

            for event in self.replay_after(after_sequence):
                last_sequence = int(event["sequence"])
                yield event

            while True:
                async with self._condition:
                    await self._condition.wait_for(lambda: self.latest_sequence > last_sequence)
                    if self.requires_resync(after_sequence=last_sequence):
                        resync_event = make_resync_event(
                            self.session_id,
                            after_sequence=last_sequence,
                            floor_sequence=self.floor_sequence,
                        )
                        events = []
                    else:
                        resync_event = None
                        events = self.replay_after(last_sequence)
                        if not events:
                            # latest_sequence 被 ensure_sequence_above 播种抬高,但这些序号从未
                            # append(重启后 buffer 为空/半空)。此时谓词 latest>last 恒真却无事件
                            # 可放、requires_resync 亦为假——推进游标吸收这段「幽灵间隙」,否则生成
                            # 器会在此空转、独占单线程事件循环令整个服务器卡死(恢复流水线会话触发)。
                            last_sequence = self.latest_sequence
                if resync_event is not None:
                    yield resync_event
                    return
                for event in events:
                    last_sequence = int(event["sequence"])
                    yield event
        finally:
            self._subscribers -= 1


def encode_sse(event: dict[str, Any]) -> str:
    """Encode an event dictionary using Server-Sent Events framing."""
    data = json.dumps(event, ensure_ascii=False, allow_nan=False, separators=(",", ":"))
    event_type = _SSE_EVENT_TYPE_ALIASES.get(str(event["type"]), str(event["type"]))
    return "event: {}\nid: {}\ndata: {}\n\n".format(event_type, event["sequence"], data)
