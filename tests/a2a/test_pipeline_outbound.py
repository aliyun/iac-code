from __future__ import annotations

import asyncio
import inspect
from dataclasses import dataclass
from typing import Any

import pytest

import iac_code.a2a.pipeline_outbound as pipeline_outbound_module
from iac_code.a2a.pipeline_outbound import PipelineA2AOutboundQueue, _estimated_event_size
from iac_code.types.stream_events import (
    PermissionRequestEvent,
    SubPipelineStreamEvent,
    TextDeltaEvent,
    ThinkingDeltaEvent,
    ToolResultEvent,
)


@dataclass
class FakePreparedPermission:
    request: PermissionRequestEvent
    envelopes: list[dict[str, Any]]
    approved: bool


class RecordingPublisher:
    def __init__(self, *, block_first_send: bool = False) -> None:
        self.frames: list[tuple[str, list[dict[str, Any]]]] = []
        self.local_envelope_frames: list[list[dict[str, Any]] | None] = []
        self.persisted_events: list[Any] = []
        self.order: list[str] = []
        self.first_send_started = asyncio.Event()
        self.release_first_send = asyncio.Event()
        self.block_first_send = block_first_send
        self.send_error: BaseException | None = None
        self._sequence = 0
        self._send_count = 0

    async def persist_batch_events(self, events: list[Any]) -> list[dict[str, Any]]:
        self.persisted_events.extend(events)
        envelopes: list[dict[str, Any]] = []
        for event in events:
            self._sequence += 1
            envelopes.append(_envelope(event, self._sequence))
        return envelopes

    async def enqueue_persisted_batch(
        self,
        envelopes: list[dict[str, Any]],
        *,
        wait_for_transport: bool = False,
        local_envelopes: list[dict[str, Any]] | None = None,
    ) -> None:
        assert wait_for_transport is True
        self.local_envelope_frames.append(list(local_envelopes) if local_envelopes is not None else None)
        await self._record_frame("batch", envelopes)

    async def prepare_permission_event(
        self,
        event: Any,
        *,
        approved: bool,
        resolver_used: bool,
    ) -> FakePreparedPermission:
        request = _inner(event)
        assert isinstance(request, PermissionRequestEvent)
        envelopes = await self.persist_batch_events([event])
        return FakePreparedPermission(request=request, envelopes=envelopes, approved=approved)

    async def resolve_permission_event(
        self,
        event: Any,
        *,
        permission_resolver: Any = None,
        auto_approve_permissions: bool = False,
    ) -> bool:
        request = _inner(event)
        assert isinstance(request, PermissionRequestEvent)
        approved = auto_approve_permissions
        if permission_resolver is not None:
            result = permission_resolver(request)
            approved = bool(await result) if inspect.isawaitable(result) else bool(result)
        return approved

    async def enqueue_prepared_permission(self, prepared: FakePreparedPermission) -> bool:
        await self._record_frame("permission", prepared.envelopes)
        return bool(prepared.envelopes)

    def complete_prepared_permission(self, prepared: FakePreparedPermission, *, delivered: bool) -> None:
        future = prepared.request.response_future
        if future is not None and not future.done():
            future.set_result(prepared.approved and delivered)

    def fail_permission_event(self, event: Any) -> None:
        request = _inner(event)
        assert isinstance(request, PermissionRequestEvent)
        if request.response_future is not None and not request.response_future.done():
            request.response_future.set_result(False)

    async def _record_frame(self, kind: str, envelopes: list[dict[str, Any]]) -> None:
        self._send_count += 1
        self.frames.append((kind, envelopes))
        self.order.append(kind)
        if self._send_count == 1:
            self.first_send_started.set()
            if self.block_first_send:
                await self.release_first_send.wait()
        if self.send_error is not None:
            raise self.send_error


@pytest.mark.asyncio
async def test_sender_idle_sends_first_event_immediately() -> None:
    publisher = RecordingPublisher()
    outbound = PipelineA2AOutboundQueue(publisher, max_batch_delay_seconds=10)

    await outbound.start()
    await outbound.submit(TextDeltaEvent(text="now"))
    await asyncio.wait_for(publisher.first_send_started.wait(), timeout=0.2)
    await outbound.close()

    assert _frame_texts(publisher.frames[0]) == ["now"]


@pytest.mark.asyncio
async def test_sender_busy_drains_all_candidate_queues_into_one_next_batch() -> None:
    publisher = RecordingPublisher(block_first_send=True)
    outbound = PipelineA2AOutboundQueue(publisher, max_batch_delay_seconds=10)

    await outbound.start()
    await outbound.submit(TextDeltaEvent(text="in-flight"))
    await asyncio.wait_for(publisher.first_send_started.wait(), timeout=0.2)

    await outbound.submit(_candidate(0, TextDeltaEvent(text="A1")))
    await outbound.submit(_candidate(1, TextDeltaEvent(text="B1")))
    await outbound.submit(_candidate(0, TextDeltaEvent(text="A2")))
    publisher.release_first_send.set()
    await outbound.close()

    assert len(publisher.frames) == 2
    assert _frame_texts(publisher.frames[1]) == ["B1", "A1A2"]


@pytest.mark.asyncio
async def test_publish_batch_forwards_uncoalesced_envelopes_to_local_web_sink() -> None:
    # 回归 bug 9be9e9d9:extreme_performance 批量路径把同源 delta 合并成一帧发往远程 A2A,
    # 但订阅 web 会话的浏览器实时流必须拿到*未合并*的逐条 envelope(经 loopback sink),
    # 否则整段流水线运行期间界面零更新。锁定:远程帧合并、本地帧不合并。
    publisher = RecordingPublisher(block_first_send=True)
    outbound = PipelineA2AOutboundQueue(publisher, max_batch_delay_seconds=10)

    await outbound.start()
    await outbound.submit(TextDeltaEvent(text="in-flight"))
    await asyncio.wait_for(publisher.first_send_started.wait(), timeout=0.2)

    await outbound.submit(_candidate(0, TextDeltaEvent(text="A1")))
    await outbound.submit(_candidate(1, TextDeltaEvent(text="B1")))
    await outbound.submit(_candidate(0, TextDeltaEvent(text="A2")))
    publisher.release_first_send.set()
    await outbound.close()

    # 远程帧:同源 delta 合并(B1 单独,A1+A2 合成 A1A2),2 条。
    assert _frame_texts(publisher.frames[1]) == ["B1", "A1A2"]
    # loopback web sink:同一帧收到未合并的逐条 envelope(3 条,提交顺序),浏览器据此逐字渲染。
    local_frame = publisher.local_envelope_frames[1]
    assert local_frame is not None
    assert [envelope["data"]["text"] for envelope in local_frame] == ["A1", "B1", "A2"]


@pytest.mark.asyncio
async def test_count_threshold_freezes_all_current_sources_not_candidate_chunks() -> None:
    publisher = RecordingPublisher(block_first_send=True)
    outbound = PipelineA2AOutboundQueue(publisher, max_batch_events=3, max_batch_delay_seconds=10)

    await outbound.start()
    await outbound.submit(TextDeltaEvent(text="in-flight"))
    await publisher.first_send_started.wait()
    await outbound.submit(_candidate(0, TextDeltaEvent(text="A")))
    await outbound.submit(_candidate(1, TextDeltaEvent(text="B")))
    await outbound.submit(_candidate(2, TextDeltaEvent(text="C")))
    publisher.release_first_send.set()
    await outbound.close()

    assert _frame_texts(publisher.frames[1]) == ["A", "B", "C"]


@pytest.mark.asyncio
async def test_time_threshold_freezes_ready_batch_while_sender_is_busy() -> None:
    publisher = RecordingPublisher(block_first_send=True)
    outbound = PipelineA2AOutboundQueue(publisher, max_batch_delay_seconds=0.01)

    await outbound.start()
    await outbound.submit(TextDeltaEvent(text="in-flight"))
    await publisher.first_send_started.wait()
    await outbound.submit(TextDeltaEvent(text="A"))
    await outbound.submit(TextDeltaEvent(text="B"))
    await asyncio.sleep(0.03)
    await outbound.submit(TextDeltaEvent(text="C"))
    publisher.release_first_send.set()
    await outbound.close()

    assert [_frame_texts(frame) for frame in publisher.frames] == [["in-flight"], ["ABC"]]


@pytest.mark.asyncio
async def test_byte_threshold_is_a_snapshot_trigger_not_a_batch_size_limit() -> None:
    publisher = RecordingPublisher(block_first_send=True)
    outbound = PipelineA2AOutboundQueue(publisher, max_batch_bytes=1, max_batch_delay_seconds=10)

    await outbound.start()
    await outbound.submit(TextDeltaEvent(text="in-flight"))
    await publisher.first_send_started.wait()
    await outbound.submit(_candidate(0, TextDeltaEvent(text="large-A")))
    await outbound.submit(_candidate(1, TextDeltaEvent(text="large-B")))
    publisher.release_first_send.set()
    await outbound.close()

    assert [_frame_texts(frame) for frame in publisher.frames[1:]] == [["large-A", "large-B"]]


@pytest.mark.asyncio
async def test_sender_recombines_multiple_elapsed_windows_into_one_backlog_batch() -> None:
    publisher = RecordingPublisher(block_first_send=True)
    outbound = PipelineA2AOutboundQueue(publisher, max_batch_delay_seconds=0.01)

    await outbound.start()
    await outbound.submit(TextDeltaEvent(text="in-flight"))
    await publisher.first_send_started.wait()
    await outbound.submit(TextDeltaEvent(text="A"))
    await asyncio.sleep(0.02)
    await outbound.submit(TextDeltaEvent(text="B"))
    await asyncio.sleep(0.02)
    await outbound.submit(TextDeltaEvent(text="C"))
    await asyncio.sleep(0.02)
    publisher.release_first_send.set()
    await outbound.close()

    assert [_frame_texts(frame) for frame in publisher.frames] == [["in-flight"], ["ABC"]]


@pytest.mark.asyncio
async def test_mixed_event_types_share_the_same_normal_batch() -> None:
    publisher = RecordingPublisher(block_first_send=True)
    outbound = PipelineA2AOutboundQueue(publisher, max_batch_delay_seconds=10)

    await outbound.start()
    await outbound.submit(TextDeltaEvent(text="in-flight"))
    await publisher.first_send_started.wait()
    await outbound.submit(TextDeltaEvent(text="answer"))
    await outbound.submit(ToolResultEvent(tool_use_id="tool", tool_name="write_file", result="ok"))
    await outbound.submit(ThinkingDeltaEvent(text="reason"))
    publisher.release_first_send.set()
    await outbound.close()

    assert [item["eventType"] for item in publisher.frames[1][1]] == [
        "text_delta",
        "tool_result",
        "thinking_delta",
    ]


@pytest.mark.asyncio
async def test_non_delta_breaks_delta_coalescing_within_one_source() -> None:
    publisher = RecordingPublisher(block_first_send=True)
    outbound = PipelineA2AOutboundQueue(publisher, max_batch_delay_seconds=10)

    await outbound.start()
    await outbound.submit(TextDeltaEvent(text="in-flight"))
    await publisher.first_send_started.wait()
    await outbound.submit(_candidate(0, TextDeltaEvent(text="A1")))
    await outbound.submit(_candidate(0, ToolResultEvent(tool_use_id="tool", tool_name="bash", result="ok")))
    await outbound.submit(_candidate(0, TextDeltaEvent(text="A2")))
    publisher.release_first_send.set()
    await outbound.close()

    assert [item["eventType"] for item in publisher.frames[1][1]] == [
        "text_delta",
        "tool_result",
        "text_delta",
    ]


@pytest.mark.asyncio
async def test_permission_sends_same_source_prefix_then_overtakes_unrelated_backlog() -> None:
    publisher = RecordingPublisher(block_first_send=True)
    outbound = PipelineA2AOutboundQueue(publisher, max_batch_delay_seconds=10)
    response = asyncio.get_running_loop().create_future()

    await outbound.start()
    await outbound.submit(TextDeltaEvent(text="in-flight"))
    await publisher.first_send_started.wait()
    await outbound.submit(_candidate(0, TextDeltaEvent(text="A-prefix")))
    await outbound.submit(_candidate(1, TextDeltaEvent(text="B-backlog")))
    await outbound.submit(
        _candidate(
            0,
            PermissionRequestEvent(
                tool_name="write_file",
                tool_input={},
                tool_use_id="permission",
                response_future=response,
            ),
        ),
        auto_approve_permissions=True,
    )
    publisher.release_first_send.set()
    await outbound.close()

    assert [kind for kind, _envelopes in publisher.frames] == ["batch", "batch", "permission", "batch"]
    assert _frame_texts(publisher.frames[1]) == ["A-prefix"]
    assert _frame_texts(publisher.frames[3]) == ["B-backlog"]
    assert [getattr(_inner(event), "type", None) for event in publisher.persisted_events] == [
        "text_delta",
        "text_delta",
        "permission_request",
        "text_delta",
    ]
    assert response.result() is True


@pytest.mark.asyncio
async def test_slow_permission_resolution_does_not_block_other_sources() -> None:
    publisher = RecordingPublisher()
    outbound = PipelineA2AOutboundQueue(publisher, max_batch_delay_seconds=10)
    resolver_started = asyncio.Event()
    release_resolver = asyncio.Event()
    response = asyncio.get_running_loop().create_future()

    async def resolve(_request: PermissionRequestEvent) -> bool:
        resolver_started.set()
        await release_resolver.wait()
        return True

    await outbound.start()
    await outbound.submit(
        _candidate(
            0,
            PermissionRequestEvent(
                tool_name="write_file",
                tool_input={},
                tool_use_id="permission",
                response_future=response,
            ),
        ),
        permission_resolver=resolve,
    )
    await resolver_started.wait()
    await outbound.submit(_candidate(1, TextDeltaEvent(text="B-progress")))
    await asyncio.wait_for(publisher.first_send_started.wait(), timeout=0.2)

    assert [kind for kind, _envelopes in publisher.frames] == ["batch"]
    release_resolver.set()
    await outbound.close()

    assert [kind for kind, _envelopes in publisher.frames] == ["batch", "permission"]
    assert response.result() is True


@pytest.mark.asyncio
async def test_slow_permission_does_not_starve_later_ready_permission() -> None:
    publisher = RecordingPublisher()
    outbound = PipelineA2AOutboundQueue(publisher, max_batch_delay_seconds=10)
    resolver_started = asyncio.Event()
    release_resolver = asyncio.Event()
    slow_response = asyncio.get_running_loop().create_future()
    ready_response = asyncio.get_running_loop().create_future()

    async def resolve(_request: PermissionRequestEvent) -> bool:
        resolver_started.set()
        await release_resolver.wait()
        return True

    await outbound.start()
    await outbound.submit(
        _candidate(0, PermissionRequestEvent("write_file", {}, "slow", response_future=slow_response)),
        permission_resolver=resolve,
    )
    await resolver_started.wait()
    await outbound.submit(
        _candidate(1, PermissionRequestEvent("edit_file", {}, "ready", response_future=ready_response)),
        auto_approve_permissions=True,
    )

    await asyncio.wait_for(publisher.first_send_started.wait(), timeout=0.2)
    assert ready_response.result() is True
    assert slow_response.done() is False

    release_resolver.set()
    await outbound.close()
    assert [kind for kind, _envelopes in publisher.frames] == ["permission", "permission"]
    assert [
        _inner(event).tool_use_id
        for event in publisher.persisted_events
        if isinstance(_inner(event), PermissionRequestEvent)
    ] == ["ready", "slow"]


@pytest.mark.asyncio
async def test_later_same_source_permission_waits_for_earlier_permission_gate() -> None:
    publisher = RecordingPublisher()
    outbound = PipelineA2AOutboundQueue(publisher, max_batch_delay_seconds=10)
    resolver_started = asyncio.Event()
    release_resolver = asyncio.Event()
    first_response = asyncio.get_running_loop().create_future()
    second_response = asyncio.get_running_loop().create_future()

    async def resolve(_request: PermissionRequestEvent) -> bool:
        resolver_started.set()
        await release_resolver.wait()
        return True

    await outbound.start()
    await outbound.submit(
        _candidate(0, PermissionRequestEvent("write_file", {}, "first", response_future=first_response)),
        permission_resolver=resolve,
    )
    await resolver_started.wait()
    await outbound.submit(_candidate(0, TextDeltaEvent(text="between")))
    await outbound.submit(
        _candidate(0, PermissionRequestEvent("edit_file", {}, "second", response_future=second_response)),
        auto_approve_permissions=True,
    )

    await asyncio.sleep(0.02)
    assert publisher.frames == []
    assert second_response.done() is False

    release_resolver.set()
    await outbound.close()
    assert [kind for kind, _envelopes in publisher.frames] == ["permission", "batch", "permission"]
    assert _frame_texts(publisher.frames[1]) == ["between"]
    assert first_response.result() is True
    assert second_response.result() is True


@pytest.mark.asyncio
async def test_events_after_permission_from_same_source_wait_for_permission_delivery() -> None:
    publisher = RecordingPublisher()
    outbound = PipelineA2AOutboundQueue(publisher, max_batch_delay_seconds=10)
    release_resolver = asyncio.Event()

    async def resolve(_request: PermissionRequestEvent) -> bool:
        await release_resolver.wait()
        return True

    await outbound.start()
    await outbound.submit(
        _candidate(0, PermissionRequestEvent("write_file", {}, "permission")),
        permission_resolver=resolve,
    )
    await outbound.submit(_candidate(0, TextDeltaEvent(text="after")))
    await asyncio.sleep(0.02)
    assert publisher.frames == []

    release_resolver.set()
    await outbound.close()
    assert [kind for kind, _envelopes in publisher.frames] == ["permission", "batch"]


@pytest.mark.asyncio
async def test_serialized_callback_is_a_boundary_without_blocking_admission() -> None:
    publisher = RecordingPublisher(block_first_send=True)
    outbound = PipelineA2AOutboundQueue(publisher, max_batch_delay_seconds=10)

    async def callback() -> None:
        publisher.order.append("callback")

    await outbound.start()
    await outbound.submit(TextDeltaEvent(text="before"))
    await publisher.first_send_started.wait()
    callback_task = asyncio.create_task(outbound.run_serialized(callback))
    await asyncio.sleep(0)
    await outbound.submit(TextDeltaEvent(text="after"))
    assert callback_task.done() is False

    publisher.release_first_send.set()
    await callback_task
    await outbound.close()
    assert publisher.order == ["batch", "callback", "batch"]


@pytest.mark.asyncio
async def test_later_permission_does_not_move_same_source_prefix_across_callback_boundary() -> None:
    publisher = RecordingPublisher(block_first_send=True)
    outbound = PipelineA2AOutboundQueue(publisher, max_batch_delay_seconds=10)

    async def callback() -> None:
        publisher.order.append("callback")

    await outbound.start()
    await outbound.submit(TextDeltaEvent(text="in-flight"))
    await publisher.first_send_started.wait()
    await outbound.submit(_candidate(0, TextDeltaEvent(text="before-callback")))
    callback_task = asyncio.create_task(outbound.run_serialized(callback))
    await asyncio.sleep(0)
    await outbound.submit(
        _candidate(0, PermissionRequestEvent("write_file", {}, "permission")),
        auto_approve_permissions=True,
    )

    publisher.release_first_send.set()
    await callback_task
    await outbound.close()

    assert publisher.order == ["batch", "batch", "callback", "permission"]


@pytest.mark.asyncio
async def test_flush_waits_for_prior_events_but_not_later_events() -> None:
    class BlockingSecondPublisher(RecordingPublisher):
        def __init__(self) -> None:
            super().__init__(block_first_send=True)
            self.second_send_started = asyncio.Event()
            self.release_second_send = asyncio.Event()

        async def _record_frame(self, kind: str, envelopes: list[dict[str, Any]]) -> None:
            await super()._record_frame(kind, envelopes)
            if self._send_count == 2:
                self.second_send_started.set()
                await self.release_second_send.wait()

    publisher = BlockingSecondPublisher()
    outbound = PipelineA2AOutboundQueue(publisher, max_batch_delay_seconds=10)

    await outbound.start()
    await outbound.submit(TextDeltaEvent(text="before"))
    await publisher.first_send_started.wait()
    flush_task = asyncio.create_task(outbound.flush())
    await asyncio.sleep(0)
    await outbound.submit(TextDeltaEvent(text="after"))
    publisher.release_first_send.set()
    await asyncio.wait_for(publisher.second_send_started.wait(), timeout=0.2)
    await asyncio.wait_for(flush_task, timeout=0.2)

    publisher.release_second_send.set()
    await outbound.close()
    assert len(publisher.frames) == 2


@pytest.mark.asyncio
async def test_close_drains_all_accepted_events() -> None:
    publisher = RecordingPublisher(block_first_send=True)
    outbound = PipelineA2AOutboundQueue(publisher, max_batch_delay_seconds=10)

    await outbound.start()
    await outbound.submit(TextDeltaEvent(text="first"))
    await publisher.first_send_started.wait()
    for index in range(500):
        await outbound.submit(TextDeltaEvent(text=str(index)))
    publisher.release_first_send.set()
    await outbound.close()

    assert len(publisher.persisted_events) == 501


@pytest.mark.asyncio
async def test_submit_is_not_backpressured_by_a_busy_network_sender() -> None:
    publisher = RecordingPublisher(block_first_send=True)
    outbound = PipelineA2AOutboundQueue(publisher, max_batch_delay_seconds=10)

    await outbound.start()
    await outbound.submit(TextDeltaEvent(text="first"))
    await publisher.first_send_started.wait()
    # Real backpressure would block these submissions indefinitely, so any generous
    # timeout still detects it; keep the bound loose enough to survive slow,
    # oversubscribed CI runners without flaking.
    await asyncio.wait_for(
        asyncio.gather(*(outbound.submit(TextDeltaEvent(text=str(index))) for index in range(1_000))),
        timeout=5.0,
    )
    publisher.release_first_send.set()
    await outbound.close()


@pytest.mark.asyncio
async def test_after_delivery_runs_only_after_successful_transport_delivery() -> None:
    publisher = RecordingPublisher(block_first_send=True)
    outbound = PipelineA2AOutboundQueue(publisher)
    delivered: list[str] = []

    await outbound.start()
    await outbound.submit(TextDeltaEvent(text="text"), after_delivery=lambda: delivered.append("text"))
    await publisher.first_send_started.wait()
    assert delivered == []
    publisher.release_first_send.set()
    await outbound.close()
    assert delivered == ["text"]


@pytest.mark.asyncio
async def test_sender_failure_is_observed_by_flush_and_future_submissions() -> None:
    publisher = RecordingPublisher()
    publisher.send_error = RuntimeError("sender failed")
    outbound = PipelineA2AOutboundQueue(publisher)

    await outbound.start()
    await outbound.submit(TextDeltaEvent(text="text"))
    with pytest.raises(RuntimeError, match="sender failed"):
        await outbound.flush()
    with pytest.raises(RuntimeError, match="sender failed"):
        await outbound.submit(TextDeltaEvent(text="later"))


@pytest.mark.asyncio
async def test_abort_cancels_and_joins_blocked_worker_and_fails_callback() -> None:
    publisher = RecordingPublisher(block_first_send=True)
    outbound = PipelineA2AOutboundQueue(publisher)

    await outbound.start()
    await outbound.submit(TextDeltaEvent(text="in-flight"))
    await publisher.first_send_started.wait()
    callback = asyncio.create_task(outbound.run_serialized(lambda: asyncio.sleep(0)))
    await asyncio.sleep(0)
    await outbound.abort()

    with pytest.raises(RuntimeError, match="aborted"):
        await callback
    assert outbound._worker is not None
    assert outbound._worker.done()


@pytest.mark.asyncio
async def test_submit_returns_text_for_text_delta_without_waiting_for_delivery() -> None:
    publisher = RecordingPublisher(block_first_send=True)
    outbound = PipelineA2AOutboundQueue(publisher)

    await outbound.start()
    assert await outbound.submit(TextDeltaEvent(text="answer")) == "answer"
    await publisher.first_send_started.wait()
    assert await outbound.submit(ThinkingDeltaEvent(text="reason")) is None
    publisher.release_first_send.set()
    await outbound.close()


def test_event_size_estimation_is_bounded_without_copying_or_rendering_large_values() -> None:
    class LargeString(str):
        def encode(self, *args, **kwargs):
            raise AssertionError("large strings should be capped before encoding")

    event = ToolResultEvent(
        tool_use_id="tool-1",
        tool_name="large",
        result=LargeString("x" * 2_048),
    )

    assert _estimated_event_size(event, limit=1_024) == 1_024


@pytest.mark.asyncio
async def test_submit_estimates_size_before_acquiring_coordinator_lock(monkeypatch) -> None:
    publisher = RecordingPublisher()
    outbound = PipelineA2AOutboundQueue(publisher)
    lock_states: list[bool] = []

    def estimate(_event: Any, *, limit: int) -> int:
        assert limit > 0
        lock_states.append(outbound._lock.locked())
        return 1

    monkeypatch.setattr(pipeline_outbound_module, "_estimated_event_size", estimate)
    await outbound.start()
    await outbound.submit(TextDeltaEvent(text="text"))
    await outbound.close()

    assert lock_states == [False]


def _candidate(index: int, event: Any) -> SubPipelineStreamEvent:
    return SubPipelineStreamEvent(sub_pipeline_id="evaluate", candidate_index=index, inner=event)


def _inner(event: Any) -> Any:
    while isinstance(event, SubPipelineStreamEvent):
        event = event.inner
    return event


def _envelope(event: Any, sequence: int) -> dict[str, Any]:
    candidate_path: list[tuple[str, int]] = []
    while isinstance(event, SubPipelineStreamEvent):
        candidate_path.append((event.sub_pipeline_id, event.candidate_index))
        event = event.inner

    event_type = "permission_requested" if isinstance(event, PermissionRequestEvent) else event.type
    data: dict[str, Any]
    if isinstance(event, (TextDeltaEvent, ThinkingDeltaEvent)):
        data = {"text": event.text}
        if isinstance(event, ThinkingDeltaEvent):
            data["type"] = "raw_thinking"
    elif isinstance(event, ToolResultEvent):
        data = {"toolName": event.tool_name, "result": event.result}
    else:
        data = {}
    envelope: dict[str, Any] = {
        "eventId": f"evt-{sequence}",
        "sequence": sequence,
        "createdAt": f"2026-07-18T00:00:{sequence:02d}Z",
        "eventType": event_type,
        "pipelineRunId": "run-1",
        "taskId": "task-1",
        "contextId": "context-1",
        "status": "working",
        "data": data,
    }
    if candidate_path:
        source = "/".join(f"{sub_pipeline_id}:{index}" for sub_pipeline_id, index in candidate_path)
        envelope["scope"] = "candidate_step"
        envelope["candidate"] = {"runId": source, "index": candidate_path[-1][1], "attempt": 1}
        envelope["candidateStep"] = {"runId": f"{source}/step", "attempt": 1}
    else:
        envelope["scope"] = "pipeline"
    return envelope


def _frame_texts(frame: tuple[str, list[dict[str, Any]]]) -> list[str]:
    return [str(item.get("data", {}).get("text")) for item in frame[1] if "text" in item.get("data", {})]
