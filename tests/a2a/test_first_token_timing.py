"""Offline timing contracts, including transport completion and request isolation."""

from __future__ import annotations

import asyncio
import json
import logging
from types import SimpleNamespace
from unittest.mock import Mock

import httpx
import pytest

from iac_code.a2a.request_timing import FirstTokenTimingMiddleware, observe_request_data
from iac_code.a2a.transports.http import HttpA2AClient
from iac_code.services import first_token_timing as timing_module
from iac_code.services.first_token_timing import (
    RequestTiming,
    current_timing,
    mark_timing,
    time_model_stream,
    timed_lock,
    timed_phase,
    timing_headers,
    timing_phase,
    use_request_timing,
)
from iac_code.types.stream_events import MessageEndEvent, MessageStartEvent, TextDeltaEvent, ThinkingDeltaEvent, Usage


@pytest.fixture
def records(monkeypatch):
    result = []

    class Capture(logging.Handler):
        def emit(self, record):
            assert record.levelno == logging.INFO
            result.append(json.loads(record.getMessage()))

    monkeypatch.setattr(timing_module.logger, "handlers", [Capture()])
    monkeypatch.setattr(timing_module.logger, "propagate", False)
    monkeypatch.setattr(timing_module.logger, "level", logging.INFO)
    return result


class Clock:
    def __init__(self):
        self.ns = 0

    def __call__(self):
        return self.ns

    def advance(self, ms):
        self.ns += int(ms * 1_000_000)


def stages(records):
    return [record["stage"] for record in records]


def text_update(text="private model text"):
    return {
        "result": {
            "statusUpdate": {
                "taskId": "task-1",
                "contextId": "ctx-1",
                "status": {
                    "state": "TASK_STATE_WORKING",
                    "message": {
                        "messageId": "task-1-message",
                        "role": "ROLE_AGENT",
                        "parts": [{"text": text}],
                    },
                },
            }
        }
    }


def frame(payload):
    return b"data: " + json.dumps(payload, ensure_ascii=False).encode() + b"\n\n"


async def run_wire(
    chunks, *, request_id=b"request-1", send=None, status=200, protocol="a2a", sse=True, app=None, path="/"
):
    sent = []

    async def receive():
        return {"type": "http.request", "body": b"", "more_body": False}

    async def capture(message):
        sent.append(message)

    async def source(scope, receive, send):
        observe_request_data({"message": {"contextId": "ctx-1", "messageId": "input-1"}})
        await send(
            {
                "type": "http.response.start",
                "status": status,
                "headers": [(b"content-type", b"text/event-stream" if sse else b"application/json")],
            }
        )
        for index, chunk in enumerate(chunks):
            await send({"type": "http.response.body", "body": chunk, "more_body": index < len(chunks) - 1})

    await FirstTokenTimingMiddleware(app or source, protocol=protocol)(
        {"type": "http", "path": path, "method": "POST", "headers": [(b"x-request-id", request_id)]},
        receive,
        send or capture,
    )
    return sent


def test_phase_uses_monotonic_milliseconds_and_no_content(records):
    clock = Clock()
    timing = RequestTiming(
        "pop-request", clock_ns=clock, context_id="ctx-1", request_kind="session_restore", api_key="private-key"
    )
    with use_request_timing(timing):
        mark_timing("request_received")
        with timing_phase("backup_restore"):
            clock.advance(12.5)
        mark_timing("first_text_send_completed")
        timing.summary("first_text_sent")
        timing.summary("duplicate")
    assert current_timing() is None
    completed = next(r for r in records if r["stage"] == "backup_restore_completed")
    assert completed["duration_ms"] == completed["elapsed_ms"] == 12.5
    summary = records[-1]
    assert summary["first_text_sent"] is True
    assert summary["phase_totals_ms"] == {"backup_restore": 12.5}
    assert stages(records).count("first_token_summary") == 1
    assert all(r["log_type"] == "first_token_timing" and r["service"] == "iac-code" for r in records)
    assert "private-key" not in json.dumps(records)


def test_nested_restore_has_one_phase_but_separate_restores_are_numbered(records):
    @timed_phase("backup_restore", restore_result=True)
    def restore(nested=False):
        if nested:
            return restore()
        return SimpleNamespace(action="current", enabled=True, state=SimpleNamespace(generation=4))

    timing = RequestTiming("req")
    with use_request_timing(timing):
        restore(True)
        restore()
        timing.summary("completed")
    completed = [r for r in records[-1]["timeline"] if r["stage"] == "backup_restore_completed"]
    assert len(completed) == 2
    assert len({r["phase_id"] for r in completed}) == 2
    assert all(r["restore_action"] == "current" and r["generation"] == 4 for r in completed)


def test_summary_exposes_unfinished_phases_without_treating_totals_as_wall_time(records):
    clock = Clock()
    timing = RequestTiming("req", clock_ns=clock)
    with use_request_timing(timing), timing_phase("outbound_enqueue"):
        clock.advance(5)
        timing.summary("first_text_sent")
    summary = next(r for r in records if r["stage"] == "first_token_summary")
    assert summary["phase_totals_ms"] == {}
    assert summary["in_progress_phases"][0]["stage"] == "outbound_enqueue"
    assert summary["in_progress_phases"][0]["elapsed_ms"] == 5


def test_summary_is_frozen_at_send_completion_before_log_sink_delay(records, monkeypatch):
    clock = Clock()
    timing = RequestTiming("req", clock_ns=clock)
    info = timing_module.logger.info

    def slow_log(*args, **kwargs):
        info(*args, **kwargs)
        clock.advance(100)

    monkeypatch.setattr(timing_module.logger, "info", slow_log)
    with use_request_timing(timing):
        with timing_phase("outbound_enqueue"):
            clock.advance(7)
            mark_timing("first_text_send_completed")
        timing.summary("first_text_sent")
    summary = records[-1]
    assert summary["elapsed_ms"] == summary["milestones_ms"]["first_text_send_completed"]
    assert summary["in_progress_phases"][0]["elapsed_ms"] == summary["elapsed_ms"]
    assert "outbound_enqueue_completed" not in stages(records)


@pytest.mark.asyncio
@pytest.mark.parametrize("same_request_id", [False, True])
async def test_concurrent_requests_and_threads_keep_distinct_call_contexts(records, same_request_id):
    ready = asyncio.Event()
    count = 0

    async def request(index):
        nonlocal count
        timing = RequestTiming("same" if same_request_id else f"req-{index}", context_id=f"ctx-{index}")
        with use_request_timing(timing):
            mark_timing("request_received")
            count += 1
            if count == 2:
                ready.set()
            await ready.wait()
            await asyncio.to_thread(mark_timing, "thread_stage")
            mark_timing("request_completed")
            timing.summary("completed")
        return timing

    timings = await asyncio.gather(request(1), request(2))
    assert timings[0].call_id != timings[1].call_id
    for index, timing in enumerate(timings, 1):
        own = [r for r in records if r["call_id"] == timing.call_id]
        assert len(own) == 2
        assert {r["context_id"] for r in own} == {f"ctx-{index}"}
        assert "thread_stage" in stages(own[-1]["timeline"])
    assert current_timing() is None


@pytest.mark.asyncio
async def test_first_only_phases_do_not_hold_thread_lock_across_await(records):
    timing = RequestTiming("req")
    with use_request_timing(timing):
        with timing_phase("outbound_enqueue", first_only=True):
            pass
        with timing_phase("outbound_enqueue", first_only=True):
            await asyncio.wait_for(asyncio.to_thread(mark_timing, "thread_stage"), timeout=1)
    assert stages(timing.timeline).count("outbound_enqueue_started") == 1
    assert "thread_stage" in stages(timing.timeline)
    assert records == []


@pytest.mark.asyncio
async def test_lock_wait_and_failure_do_not_swallow_cancellation(records):
    lock = asyncio.Lock()
    await lock.acquire()
    timing = RequestTiming("req")
    with use_request_timing(timing):

        async def wait():
            async with timed_lock(lock):
                pytest.fail("must not acquire")

        task = asyncio.create_task(wait())
        await asyncio.sleep(0)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    assert lock.locked()
    lock.release()
    assert timing.timeline[-1]["result"] == "cancelled"
    timing.summary("cancelled")
    assert records[-1]["result"] == "cancelled"


@pytest.mark.asyncio
async def test_completed_request_does_not_log_late_background_work(records):
    timing = RequestTiming("req")
    with use_request_timing(timing):
        timing.closed = True
        await asyncio.to_thread(mark_timing, "late")
        assert timing_headers() == {}
    assert records == []
    assert timing.timeline == []


def test_summarized_request_does_not_record_late_forced_events(records):
    timing = RequestTiming("req")
    timing.record("request_received")
    timing.record("first_text_send_completed")
    timing.summary("first_text_sent")
    snapshot = list(timing.timeline)
    timing.record("request_failed", result="failed", force=True)
    timing.record("request_completed", force=True)
    assert timing.timeline == snapshot == records[-1]["timeline"]
    assert len(records) == 3


def test_summary_bounds_repeated_details_and_reports_truncation(records, monkeypatch):
    monkeypatch.setattr(timing_module, "_MAX_TIMELINE_EVENTS", 4)
    timing = RequestTiming("req")
    timing.record("request_received")
    with use_request_timing(timing):
        for _ in range(10):
            with timing_phase("session_load"):
                pass
        timing.record("first_text_send_completed")
        timing.summary("first_text_sent")
    assert len(records) == 3
    summary = records[-1]
    assert len(summary["timeline"]) == 4
    assert summary["timeline_events_dropped"] == 18
    assert "first_text_send_completed" in summary["milestones_ms"]
    assert "session_load" in summary["phase_totals_ms"]


@pytest.mark.asyncio
@pytest.mark.parametrize("thinking", [False, True])
async def test_restore_and_chat_emit_nine_or_ten_logs_with_all_internal_details_in_summary(
    records, monkeypatch, thinking
):
    from iac_code.services.telemetry.scope import use_span_attributes

    clock = Clock()
    monkeypatch.setattr(timing_module.time, "monotonic_ns", clock)

    async def restore(scope, receive, send):
        observe_request_data({"sessionId": "session-1"})
        with timing_phase("backup_restore") as details:
            clock.advance(9)
            details.update(restore_action="restored", copied_files=5)
        await send({"type": "http.response.start", "status": 200, "headers": [(b"content-type", b"application/json")]})
        await send({"type": "http.response.body", "body": b'{"status":"restored"}'})

    async def model():
        yield MessageStartEvent(message_id="response-1")
        clock.advance(2)
        if thinking:
            yield ThinkingDeltaEvent(text="private thought")
        clock.advance(3)
        yield TextDeltaEvent(text="private answer")
        yield MessageEndEvent(stop_reason="end_turn", usage=Usage())

    async def chat(scope, receive, send):
        observe_request_data({"message": {"contextId": "ctx-1"}})
        with timing_phase("backup_restore") as details:
            clock.advance(1)
            details["restore_action"] = "current"
        with timing_phase("runtime_prepare"):
            for _ in range(10):
                with timing_phase("session_load"):
                    clock.advance(1)
        for index in range(5):
            with use_span_attributes({"step_id": f"step-{index}"}):
                stream = time_model_stream(model(), llm_call_id=f"llm-{index}", attempt=index + 1)
                async for _ in stream:
                    pass
                await stream.aclose()
        mark_timing("llm_first_text_forwarded")
        mark_timing("agent_first_text_received")
        with timing_phase("outbound_enqueue"):
            clock.advance(2)
        await send({"type": "http.response.start", "status": 200, "headers": [(b"content-type", b"text/event-stream")]})
        await send({"type": "http.response.body", "body": frame(text_update())})

    await run_wire([], app=restore, path="/iac-code/session/ensure-restored", sse=False)
    await run_wire([], app=chat)
    assert len(records) == (10 if thinking else 9)
    restore_logs = [r for r in records if r["request_kind"] == "session_restore"]
    chat_logs = [r for r in records if r["request_kind"] == "a2a"]
    assert stages(restore_logs) == [
        "request_received",
        "backup_restore_started",
        "backup_restore_completed",
        "first_token_summary",
    ]
    assert stages(chat_logs) == [
        "request_received",
        "llm_request_started",
        *(["llm_first_thinking_received"] if thinking else []),
        "llm_first_text_received",
        "first_text_send_completed",
        "first_token_summary",
    ]
    assert len({r["call_id"] for r in records}) == 2
    assert {r["request_id"] for r in records} == {"request-1"}
    assert restore_logs[-1]["phase_totals_ms"]["backup_restore"] == 9
    summary = chat_logs[-1]
    assert summary["first_text_sent"] is True
    assert summary["phase_totals_ms"]["session_load"] == 10
    assert summary["timeline_events_dropped"] == 0
    timeline = summary["timeline"]
    assert stages(timeline).count("session_load_completed") == 10
    starts = [r for r in timeline if r["stage"] == "llm_request_started"]
    assert [r["attempt"] for r in starts] == [1, 2, 3, 4, 5]
    assert all(r["step_id"] == r["llm_call_id"].replace("llm", "step") for r in starts)
    assert next(r for r in timeline if r["stage"] == "backup_restore_completed")["restore_action"] == "current"
    assert "agent_first_text_received" in stages(timeline)
    assert "outbound_enqueue_completed" in stages(timeline)
    assert "private" not in json.dumps(records)


@pytest.mark.asyncio
async def test_send_completed_is_recorded_after_await_send_and_bytes_are_unchanged(records, monkeypatch):
    clock = Clock()
    monkeypatch.setattr(timing_module.time, "monotonic_ns", clock)
    body = frame(text_update())
    sent = []

    async def send(message):
        if message["type"] == "http.response.body":
            assert "first_text_send_completed" not in stages(records)
            assert "first_token_summary" not in stages(records)
            clock.advance(9)
        sent.append(message)

    await run_wire([body], send=send)
    summary = next(r for r in records if r["stage"] == "first_token_summary")
    assert summary["milestones_ms"]["first_text_send_completed"] == 9
    assert sent[-1]["body"] == body
    assert summary["request_id"] == "request-1"
    assert summary["context_id"] == "ctx-1"
    assert summary["first_text_sent"] is True
    assert "private model text" not in json.dumps(records)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "protocol,payload",
    [
        ("a2a", text_update()),
        (
            "a2a",
            {
                "result": {
                    "kind": "status-update",
                    "status": {
                        "message": {
                            "messageId": "task-1-message",
                            "role": "agent",
                            "parts": [{"text": "正文"}],
                        }
                    },
                }
            },
        ),
        (
            "a2a",
            {
                "result": {
                    "statusUpdate": {
                        "metadata": {
                            "iac_code": {
                                "pipelineBatch": {
                                    "events": [
                                        {"eventType": "thinking_delta", "data": {"text": "private thought"}},
                                        {"eventType": "text_delta", "data": {"text": "正文"}},
                                    ]
                                }
                            }
                        }
                    }
                }
            },
        ),
        ("agui", {"type": "TEXT_MESSAGE_CONTENT", "messageId": "m1", "delta": "正文"}),
    ],
)
async def test_protocol_text_detection_handles_fragmented_utf8_and_sse(records, protocol, payload):
    body = frame(payload)
    # Split every byte, including UTF-8 codepoints and frame delimiters.
    chunks = [body[i : i + 1] for i in range(len(body))]
    sent = await run_wire(chunks, protocol=protocol)
    assert b"".join(m.get("body", b"") for m in sent) == body
    assert stages(records).count("first_text_send_completed") == 1
    assert stages(records).count("first_token_summary") == 1


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "payload",
    [
        {"type": "RUN_STARTED", "runId": "run-1"},
        {"type": "TEXT_MESSAGE_CONTENT", "delta": ""},
        {
            "result": {
                "statusUpdate": {
                    "status": {
                        "message": {
                            "messageId": "task-1-TASK_STATE_WORKING",
                            "role": "ROLE_AGENT",
                            "parts": [{"text": "Working"}],
                        }
                    }
                }
            }
        },
        {"result": {"statusUpdate": {"metadata": {"iac_code": {"thinking": {"text": "private"}}}}}},
        {
            "result": {
                "statusUpdate": {
                    "metadata": {
                        "iac_code": {
                            "pipeline": {
                                "eventType": "tool_result",
                                "data": {"text": "private tool output"},
                            }
                        }
                    }
                }
            }
        },
        {"result": {"task": {"history": [{"messageId": "old-message", "role": "agent", "parts": [{"text": "old"}]}]}}},
    ],
)
async def test_status_thinking_tool_results_and_history_are_not_first_text(records, payload):
    await run_wire([b": heartbeat\n\n", frame(payload)])
    assert "first_text_send_completed" not in stages(records)
    summary = next(r for r in records if r["stage"] == "first_token_summary")
    assert summary["first_text_sent"] is False
    assert "first_text_send_completed" not in summary["milestones_ms"]


@pytest.mark.asyncio
async def test_oversized_frame_is_bounded_and_does_not_hide_following_text(records):
    body = b"data: " + b"x" * 300_000 + b"\n\n" + frame(text_update())
    sent = await run_wire([body])
    assert sent[-1]["body"] == body
    assert "first_text_send_completed" in stages(records)
    assert next(r for r in records if r["stage"] == "first_token_summary")["first_text_observation_complete"] is False


@pytest.mark.asyncio
async def test_truncated_sse_is_not_reported_as_sent_text(records):
    await run_wire([frame(text_update()).rstrip(b"\n")])
    assert "first_text_send_completed" not in stages(records)


@pytest.mark.asyncio
async def test_non_streaming_final_answer_counts_only_when_the_response_body_is_sent(records):
    payload = {
        "result": {
            "task": {
                "status": {
                    "state": "TASK_STATE_COMPLETED",
                    "message": {
                        "messageId": "task-1-TASK_STATE_COMPLETED",
                        "role": "ROLE_AGENT",
                        "parts": [{"text": "private answer"}],
                    },
                }
            }
        }
    }
    body = json.dumps(payload).encode()
    await run_wire([body[:10], body[10:]], sse=False)
    assert stages(records).count("first_text_send_completed") == 1
    assert "private answer" not in json.dumps(records)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "payload,result",
    [
        ({"result": {"statusUpdate": {"status": {"state": "TASK_STATE_FAILED"}}}}, "failed"),
        ({"type": "RUN_ERROR", "message": "private detail"}, "failed"),
        ({"result": {"statusUpdate": {"status": {"state": "TASK_STATE_INPUT_REQUIRED"}}}}, "input_required"),
    ],
)
async def test_protocol_failure_and_input_required_are_distinguished_from_empty_success(records, payload, result):
    await run_wire([frame(payload)])
    assert records[-1]["stage"] == "first_token_summary"
    assert records[-1]["result"] == result
    assert records[-1]["first_text_sent"] is False
    assert len(records) == 2
    assert "private detail" not in json.dumps(records)


def test_malformed_optional_metadata_cannot_break_request_observation(records):
    timing = RequestTiming("req")
    with use_request_timing(timing):
        observe_request_data({"message": {"metadata": {"iac_code": {"run_mode": {"bad": "value"}}}}})
    assert stages(timing.timeline) == ["request_parsed"]
    assert records == []


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", [OSError("private send error"), asyncio.CancelledError()])
async def test_send_failure_keeps_original_exception_and_emits_failed_summary(records, failure):
    async def send(message):
        if message["type"] == "http.response.body":
            raise failure

    with pytest.raises(type(failure)) as raised:
        await run_wire([frame(text_update())], send=send)
    assert raised.value is failure
    assert "first_text_send_started" in stages(records[-1]["timeline"])
    assert "first_text_send_completed" not in stages(records)
    assert records[-1]["first_text_sent"] is False
    assert len(records) == 2
    assert "private send error" not in json.dumps(records)
    assert current_timing() is None


@pytest.mark.asyncio
async def test_logging_failure_cannot_change_response(monkeypatch):
    monkeypatch.setattr(timing_module.logger, "info", Mock(side_effect=RuntimeError("log sink failed")))
    body = frame(text_update())
    sent = await run_wire([body])
    assert sent[-1]["body"] == body
    assert current_timing() is None


@pytest.mark.asyncio
@pytest.mark.parametrize("request_id", [b"", b"unsafe\ncredential=value", b"x" * 300])
async def test_invalid_request_ids_use_local_fallback_without_echoing_values(records, request_id):
    await run_wire([b"{}"], request_id=request_id, sse=False)
    assert all(r["request_id"].startswith("local-") for r in records)
    assert all(r["request_id_source"] == "local" for r in records)
    assert "credential=value" not in json.dumps(records)


@pytest.mark.asyncio
async def test_iac_http_client_propagates_request_id_without_mutating_shared_headers(records):
    requests = []

    async def handler(request):
        requests.append(request)
        return httpx.Response(200, json={"result": {}})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        client = HttpA2AClient(http_client=http)
        parent = RequestTiming("pop-request")
        with use_request_timing(parent):
            await client.send("http://iac.invalid/", {"jsonrpc": "2.0"})
        await client.send("http://iac.invalid/", {"jsonrpc": "2.0"})
    assert requests[0].headers["X-Request-Id"] == "pop-request"
    assert requests[0].headers["X-Iac-Parent-Call-Id"] == parent.call_id
    assert "X-Request-Id" not in requests[1].headers


@pytest.mark.asyncio
async def test_provider_records_received_before_buffered_forward_and_closes_once(records, monkeypatch, tmp_path):
    from tests.providers.test_manager_stream_lifecycle import (
        ControlledIterator,
        FakeProvider,
        _manager,
        _record_telemetry,
    )

    monkeypatch.setenv("IAC_CODE_CONFIG_DIR", str(tmp_path))
    monkeypatch.setattr("iac_code.providers.manager._MODEL_REFUSAL_FALLBACK_MAP", {"claude-sonnet-4-6": "unused"})
    _record_telemetry(monkeypatch)
    iterator = ControlledIterator(
        [
            MessageStartEvent(message_id="response-1"),
            ThinkingDeltaEvent(text="private thought"),
            TextDeltaEvent(text="private answer"),
            MessageEndEvent(stop_reason="end_turn", usage=Usage()),
        ]
    )
    manager = _manager(monkeypatch, FakeProvider(iterator))
    timing = RequestTiming("req")
    with use_request_timing(timing):
        stream = manager.stream([], "private system prompt")
        first = await anext(stream)
        assert isinstance(first, MessageStartEvent)
        assert "llm_first_text_received" in stages(records)
        assert "llm_first_text_forwarded" not in stages(timing.timeline)
        async for _ in stream:
            pass
    assert stages(timing.timeline).index("llm_first_text_received") < stages(timing.timeline).index(
        "llm_first_text_forwarded"
    )
    assert "llm_first_text_forwarded" not in stages(records)
    assert "llm_first_thinking_received" in stages(records)
    assert iterator.close_calls == 1
    assert "private" not in json.dumps(records)


@pytest.mark.asyncio
async def test_model_observer_ignores_empty_and_metadata_deltas(records):
    async def source():
        yield SimpleNamespace(type="text_delta", text="")
        yield SimpleNamespace(type="thinking_delta", text="private signature", is_metadata_only=True)
        yield TextDeltaEvent(text="answer")
        yield TextDeltaEvent(text="more")

    with use_request_timing(RequestTiming("req")):
        stream = time_model_stream(source(), llm_call_id="llm-1", attempt=2)
        async for _ in stream:
            pass
        await stream.aclose()
    assert stages(records) == ["llm_request_started", "llm_first_text_received"]
    assert records[-1]["attempt"] == 2


@pytest.mark.asyncio
async def test_provider_non_streaming_completion_is_measured_without_faking_first_delta(records, monkeypatch, tmp_path):
    from tests.providers.test_manager_stream_lifecycle import (
        ControlledIterator,
        FakeProvider,
        _manager,
        _record_telemetry,
    )

    monkeypatch.setenv("IAC_CODE_CONFIG_DIR", str(tmp_path))
    _record_telemetry(monkeypatch)
    manager = _manager(monkeypatch, FakeProvider(ControlledIterator([])))
    timing = RequestTiming("req")
    with use_request_timing(timing):
        await manager.complete([], "system")
    assert "llm_request_started" in stages(records)
    assert "llm_response_received" in stages(timing.timeline)
    assert "llm_response_received" not in stages(records)
    assert "llm_first_text_received" not in stages(records)
    assert next(r for r in records if r["stage"] == "llm_request_started")["streaming"] is False


@pytest.mark.asyncio
async def test_provider_cancellation_preserves_stream_cleanup(records, monkeypatch, tmp_path):
    from tests.providers.test_manager_stream_lifecycle import (
        ControlledIterator,
        FakeProvider,
        _manager,
        _record_telemetry,
    )

    monkeypatch.setenv("IAC_CODE_CONFIG_DIR", str(tmp_path))
    _record_telemetry(monkeypatch)
    iterator = ControlledIterator([asyncio.CancelledError()])
    manager = _manager(monkeypatch, FakeProvider(iterator))
    timing = RequestTiming("cancelled-request")
    with use_request_timing(timing):
        stream = manager.stream([], "private prompt")
        with pytest.raises(asyncio.CancelledError):
            await anext(stream)
        await stream.aclose()
    assert iterator.close_calls == 1
    assert next(r for r in timing.timeline if r["stage"] == "llm_request_failed")["error_type"] == "CancelledError"
    assert "private prompt" not in json.dumps(records)


@pytest.mark.asyncio
async def test_provider_retries_have_distinct_attempts_under_one_call(records, monkeypatch, tmp_path):
    from iac_code.providers.retry import RetryConfig
    from tests.providers.test_manager_stream_lifecycle import (
        ControlledIterator,
        FakeProvider,
        _manager,
        _record_telemetry,
    )

    monkeypatch.setenv("IAC_CODE_CONFIG_DIR", str(tmp_path))
    _record_telemetry(monkeypatch)

    class RetryingProvider(FakeProvider):
        calls = 0

        def stream(self, *args, **kwargs):
            self.calls += 1
            return ControlledIterator(
                [ConnectionError("private network detail")]
                if self.calls == 1
                else [
                    MessageStartEvent(message_id="ok"),
                    TextDeltaEvent(text="answer"),
                    MessageEndEvent(stop_reason="end_turn", usage=Usage()),
                ]
            )

    manager = _manager(monkeypatch, RetryingProvider(ControlledIterator([])))
    manager._retry_config = RetryConfig(max_retries=1, base_delay=0, jitter_factor=0)
    timing = RequestTiming("retry-request")
    with use_request_timing(timing):
        async for _ in manager.stream([], "system"):
            pass
        timing.summary("completed")
    starts = [r for r in records[-1]["timeline"] if r["stage"] == "llm_request_started"]
    assert [r["attempt"] for r in starts] == [1, 2]
    assert len({r["llm_call_id"] for r in starts}) == 1
    assert stages(records).count("llm_request_started") == 1
    assert len({r["call_id"] for r in records}) == 1
    assert "private network detail" not in json.dumps(records)


@pytest.mark.asyncio
async def test_parallel_model_calls_keep_pipeline_step_identity(records):
    from iac_code.services.telemetry.scope import use_span_attributes

    async def source():
        await asyncio.sleep(0)
        yield TextDeltaEvent(text="private content")

    async def step(name):
        with use_span_attributes({"step_id": name, "pipeline_run_id": "pipeline-1"}):
            stream = time_model_stream(source(), llm_call_id=name, attempt=1)
            async for _ in stream:
                pass
            await stream.aclose()

    timing = RequestTiming("req")
    with use_request_timing(timing):
        await asyncio.gather(step("left"), step("right"))
        timing.summary("completed")
    deltas = [r for r in records[-1]["timeline"] if r["stage"] == "llm_first_text_received"]
    assert len(deltas) == 2
    assert all(r["step_id"] == r["llm_call_id"] for r in deltas)
    assert stages(records).count("llm_first_text_received") == 1


@pytest.mark.asyncio
async def test_pipeline_worker_uses_enqueued_call_instead_of_its_startup_context(records):
    from iac_code.a2a.pipeline_outbound import PipelineA2AOutboundQueue

    class Publisher:
        async def persist_batch_events(self, events):
            return [{"eventType": "text_delta", "data": {"text": "private"}}]

        async def enqueue_persisted_batch(self, *args, **kwargs):
            return 1

    queue = PipelineA2AOutboundQueue(Publisher())
    old = RequestTiming("old-request")
    new = RequestTiming("new-request")
    with use_request_timing(old):
        await queue.start()
    with use_request_timing(new):
        await queue.submit(TextDeltaEvent(text="answer"))
    await queue.flush()
    await queue.close()
    assert records == []
    assert old.timeline == []
    new.summary("completed")
    assert records
    assert all(r["request_id"] == "new-request" for r in records)
    assert "outbound_persist_completed" in stages(new.timeline)
    assert "outbound_coalesce_completed" in stages(new.timeline)
    assert "outbound_enqueue_completed" in stages(new.timeline)
    assert "outbound_persist_completed" not in stages(records)


def test_real_restore_endpoint_correlates_two_calls_and_reports_restore_actions(records, monkeypatch, tmp_path):
    from starlette.testclient import TestClient

    from iac_code.a2a.app import create_app
    from iac_code.services.session_backup import SessionBackupService
    from iac_code.services.session_metadata import SESSION_LAYOUT_VERSION_V2, SessionMetadata, write_session_metadata
    from iac_code.services.session_storage import SessionStorage

    config = tmp_path / "config"
    backup = tmp_path / "backup"
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    monkeypatch.setenv("IAC_CODE_CONFIG_DIR", str(config))
    monkeypatch.setenv("IAC_CODE_CONFIG_BACKUP_DIR", str(backup))
    monkeypatch.delenv("IAC_CODE_CONFIG_BACKUP_TMP_DIR", raising=False)
    monkeypatch.setenv("IACCODE_A2A_ALLOWED_CWDS", str(workspace))
    storage = SessionStorage(projects_dir=backup / "projects")
    session_id = "restored-session"
    write_session_metadata(
        storage.session_dir(str(workspace), session_id),
        SessionMetadata(
            session_id=session_id,
            cwd=str(workspace),
            layout_version=SESSION_LAYOUT_VERSION_V2,
        ),
    )
    SessionBackupService(session_storage=storage).initialize_session(str(workspace), session_id)
    app = create_app(
        host="127.0.0.1", port=41242, token="private-auth", model="qwen3.6-plus", persistence_dir=config / "a2a"
    )
    with TestClient(app) as client:
        for action in ("restored", "current"):
            response = client.post(
                "/iac-code/session/ensure-restored",
                json={"cwd": str(workspace), "sessionId": session_id},
                headers={"Authorization": "Bearer private-auth", "X-Request-Id": "pop-restore"},
            )
            assert response.json() == {"status": action}
    completed = [r for r in records if r["stage"] == "backup_restore_completed"]
    assert [r["restore_action"] for r in completed] == ["restored", "current"]
    assert len({r["call_id"] for r in completed}) == 2
    assert all(r["request_id"] == "pop-restore" and r["iac_code_session_id"] == session_id for r in completed)
    assert all(r["request_kind"] == "session_restore" for r in completed)
    assert "private-auth" not in json.dumps(records)
    assert len(records) == 8
    assert (
        stages(records)
        == ["request_received", "backup_restore_started", "backup_restore_completed", "first_token_summary"] * 2
    )
