import asyncio
import json
from contextlib import suppress
from dataclasses import dataclass
from datetime import date, datetime, timezone
from pathlib import Path
from urllib.parse import urlsplit

import pytest
from starlette.testclient import TestClient


def _decode_sse_events(body: str) -> list[dict[str, object]]:
    events: list[dict[str, object]] = []
    for block in body.strip().split("\n\n"):
        fields: dict[str, object] = {}
        for line in block.splitlines():
            name, value = line.split(": ", 1)
            fields[name] = json.loads(value) if name == "data" else value
        events.append(fields)
    return events


async def _read_stream_chunks(app, url: str, *, headers: dict[str, str] | None = None, chunks: int = 1) -> str:
    _, _, body = await _read_stream_response(app, url, headers=headers, chunks=chunks)
    return body


async def _read_stream_response(
    app,
    url: str,
    *,
    headers: dict[str, str] | None = None,
    chunks: int = 1,
) -> tuple[int, dict[str, str], str]:
    parsed = urlsplit(url)
    body_chunks: list[bytes] = []
    got_chunks = asyncio.Event()
    disconnected = asyncio.Event()
    sent_request = False
    raw_headers = [(key.lower().encode(), value.encode()) for key, value in (headers or {}).items()]
    status = 0
    response_headers: dict[str, str] = {}

    scope = {
        "type": "http",
        "http_version": "1.1",
        "method": "GET",
        "scheme": "http",
        "path": parsed.path,
        "raw_path": parsed.path.encode(),
        "query_string": parsed.query.encode(),
        "headers": raw_headers,
        "client": ("testclient", 50000),
        "server": ("testserver", 80),
        "root_path": "",
    }

    async def receive():
        nonlocal sent_request
        if not sent_request:
            sent_request = True
            return {"type": "http.request", "body": b"", "more_body": False}
        await disconnected.wait()
        return {"type": "http.disconnect"}

    async def send(message):
        nonlocal status, response_headers
        if message["type"] == "http.response.start":
            status = message["status"]
            response_headers = {key.decode().lower(): value.decode() for key, value in message.get("headers", [])}
        if message["type"] == "http.response.body" and message.get("body"):
            body_chunks.append(message["body"])
            if len(body_chunks) >= chunks:
                got_chunks.set()
        if message["type"] == "http.response.body" and not message.get("more_body", False):
            got_chunks.set()

    task = asyncio.create_task(app(scope, receive, send))
    await asyncio.wait_for(got_chunks.wait(), timeout=1)
    disconnected.set()
    task.cancel()
    with suppress(asyncio.CancelledError):
        await task
    return status, response_headers, b"".join(body_chunks).decode()


def test_event_buffer_assigns_monotonic_sequence_and_preserves_local_payload() -> None:
    from iac_code.web.events import WebEventBuffer

    buffer = WebEventBuffer("session-1")
    first = buffer.append(
        "tool.result",
        {
            "text": "ok",
            "apiKey": "sk-unsafe",
            "nested": {"access_key_secret": "secret-value"},
            "items": [{"token": "token-value"}],
        },
    )
    second = buffer.append("assistant.text.delta", {"text": "hello"})

    assert first["type"] == "tool.result"
    assert first["sequence"] == 1
    assert first["sessionId"] == "session-1"
    assert first["createdAt"]
    assert first["payload"]["apiKey"] == "sk-unsafe"
    assert first["payload"]["nested"]["access_key_secret"] == "secret-value"
    assert first["payload"]["items"][0]["token"] == "token-value"
    assert second["sequence"] == 2
    json.dumps(first)


def test_ensure_sequence_above_seeds_next_append_past_visible_row_count() -> None:
    """重启后的 buffer 从 1 计数;以可见行数播种后,实时事件序号须超过存储行(Issue 3)。"""
    from iac_code.web.events import WebEventBuffer

    buffer = WebEventBuffer("session-1")
    # 前端把 4 条存储行按位置重排为 1..4;live 事件必须落在其后。
    buffer.ensure_sequence_above(4)
    live = buffer.append("pipeline.event", {"marker": "step-5"})

    assert live["sequence"] == 5


def test_ensure_sequence_above_is_monotonic_and_never_lowers_counter() -> None:
    """单调播种:活跃 buffer 已推进的计数器不得被较小的可见行数回退。"""
    from iac_code.web.events import WebEventBuffer

    buffer = WebEventBuffer("session-1")
    buffer.append("event.one", {})
    buffer.append("event.two", {})
    buffer.append("event.three", {})  # _next_sequence 现为 4

    buffer.ensure_sequence_above(1)  # floor+1=2 < 4 → 无操作
    following = buffer.append("event.four", {})

    assert following["sequence"] == 4


def test_replay_after_sequence_returns_later_events_only() -> None:
    from iac_code.web.events import WebEventBuffer

    buffer = WebEventBuffer("session-1")
    first = buffer.append("assistant.message.start", {"messageId": "m1"})
    second = buffer.append("assistant.text.delta", {"messageId": "m1", "text": "hello"})
    third = buffer.append("assistant.message.end", {"messageId": "m1"})

    assert buffer.replay_after(0) == [first, second, third]
    assert buffer.replay_after(1) == [second, third]
    assert buffer.replay_after(3) == []


def test_bounded_buffer_reports_resync_when_cursor_is_older_than_replay_floor() -> None:
    from iac_code.web.events import WebEventBuffer

    buffer = WebEventBuffer("session-1", max_events=2)
    buffer.append("event.one", {})
    second = buffer.append("event.two", {})
    buffer.append("event.three", {})

    assert buffer.floor_sequence == second["sequence"]
    assert buffer.requires_resync(after_sequence=0)
    assert not buffer.requires_resync(after_sequence=1)


def test_stream_after_yields_resync_when_live_cursor_falls_behind_buffer_floor() -> None:
    from iac_code.web.events import WebEventBuffer

    async def consume_after_gap() -> tuple[dict[str, object], dict[str, object]]:
        buffer = WebEventBuffer("session-1", max_events=2)
        buffer.append("event.one", {})
        stream = buffer.stream_after(0)

        first = await anext(stream)
        await buffer.publish("event.two", {})
        await buffer.publish("event.three", {})
        await buffer.publish("event.four", {})
        second = await asyncio.wait_for(anext(stream), timeout=1)
        await stream.aclose()
        return first, second

    first, second = asyncio.run(consume_after_gap())

    assert first["sequence"] == 1
    assert second["type"] == "session.resync.required"
    assert second["sequence"] == 0
    assert second["payload"] == {"afterSequence": 1, "floorSequence": 3}


def test_stream_after_does_not_spin_on_seeded_phantom_sequence_gap() -> None:
    """ensure_sequence_above 抬高计数器却未 append 任何事件时,stream_after 必须阻塞等待,

    而非在 latest_sequence>last_sequence 恒真、却无事件可放的「幽灵间隙」上空转。
    重启后恢复流水线会话正是此路径(buffer 空 + 存储行数播种 + SSE 从 0 重连),
    空转会独占单线程事件循环令整个服务器卡死。谓词满足却无事件时应阻塞 → TimeoutError。
    """
    from iac_code.web.events import WebEventBuffer

    async def scenario() -> str:
        buffer = WebEventBuffer("session-1")
        # 服务器重启:buffer 为空(_events 空、计数器=1)。前端重载以 50 条存储行播种。
        buffer.ensure_sequence_above(50)  # _next_sequence=51 → latest_sequence=50,但无任何事件
        stream = buffer.stream_after(0)  # 消费者从 0 重连(replaySequence≤0 → 不带 afterSequence)
        try:
            await asyncio.wait_for(anext(stream), timeout=0.2)
            return "yielded"
        except asyncio.TimeoutError:
            return "blocked"
        finally:
            await stream.aclose()

    assert asyncio.run(scenario()) == "blocked"


def test_stream_after_delivers_real_event_after_seeded_phantom_gap() -> None:
    """吸收幽灵间隙后,后续真正 publish 的事件仍须原样投递(不丢不重),序号接续播种值。"""
    from iac_code.web.events import WebEventBuffer

    async def scenario() -> tuple[dict[str, object], dict[str, object]]:
        buffer = WebEventBuffer("session-1")
        buffer.ensure_sequence_above(50)
        stream = buffer.stream_after(0)
        pending = asyncio.create_task(anext(stream))
        await asyncio.sleep(0)  # 让生成器进入 body、越过幽灵间隙并停在 await 上
        published = await buffer.publish("pipeline.event", {"marker": "resume-step"})
        streamed = await asyncio.wait_for(pending, timeout=1)
        await stream.aclose()
        return streamed, published

    streamed, published = asyncio.run(scenario())

    assert streamed == published
    assert streamed["sequence"] == 51


def test_publish_wakes_live_stream_listener() -> None:
    from iac_code.web.events import WebEventBuffer

    async def consume_published_event() -> dict[str, object]:
        buffer = WebEventBuffer("session-1")
        stream = buffer.stream_after(0)
        pending_event = asyncio.create_task(anext(stream))
        await asyncio.sleep(0)
        published = await buffer.publish("assistant.text.delta", {"text": "hello"})
        streamed = await asyncio.wait_for(pending_event, timeout=1)
        await stream.aclose()
        assert streamed == published
        return streamed

    event = asyncio.run(consume_published_event())

    assert event["type"] == "assistant.text.delta"
    assert event["sequence"] == 1


def test_subscriber_count_tracks_active_stream_after_consumers() -> None:
    from iac_code.web.events import WebEventBuffer

    async def scenario() -> tuple[int, int, int]:
        buffer = WebEventBuffer("session-1")
        before = buffer.subscriber_count
        stream = buffer.stream_after(0)
        pending = asyncio.create_task(anext(stream))
        await asyncio.sleep(0)  # 让生成器进入 body：此刻应计入一名实时观看者。
        during = buffer.subscriber_count
        await asyncio.wait_for(buffer.publish("assistant.text.delta", {"text": "hi"}), timeout=1)
        await asyncio.wait_for(pending, timeout=1)
        await stream.aclose()
        after = buffer.subscriber_count
        return before, during, after

    before, during, after = asyncio.run(scenario())

    assert before == 0
    assert during == 1
    assert after == 0


def test_observer_errors_do_not_block_publish_or_nested_observers() -> None:
    from iac_code.web.events import WebEventBuffer, observe_published_events

    observed: list[tuple[str, str]] = []

    def failing_observer(event: dict[str, object]) -> None:
        observed.append(("failing", str(event["type"])))
        raise RuntimeError("observer boom")

    def nested_observer(event: dict[str, object]) -> None:
        observed.append(("nested", str(event["type"])))

    async def publish_with_observers() -> tuple[dict[str, object], dict[str, object]]:
        buffer = WebEventBuffer("session-1")
        stream = buffer.stream_after(0)
        pending_event = asyncio.create_task(anext(stream))
        await asyncio.sleep(0)
        with observe_published_events(failing_observer):
            with observe_published_events(nested_observer):
                published = await buffer.publish("assistant.text.delta", {"text": "hello"})
        streamed = await asyncio.wait_for(pending_event, timeout=1)
        await stream.aclose()
        return published, streamed

    published, streamed = asyncio.run(publish_with_observers())

    assert streamed == published
    assert observed == [
        ("failing", "assistant.text.delta"),
        ("nested", "assistant.text.delta"),
    ]


def test_make_event_preserves_pipeline_identity_fields_at_top_level() -> None:
    from iac_code.web.events import make_event

    event = make_event(
        "session-1",
        7,
        "pipeline.progress",
        {
            "contextId": "ctx-1",
            "taskId": "task-1",
            "lastSequence": 42,
            "secret": "unsafe",
        },
    )

    assert event["contextId"] == "ctx-1"
    assert event["taskId"] == "task-1"
    assert event["lastSequence"] == 42
    assert event["payload"]["secret"] == "unsafe"


def test_make_event_normalizes_payload_to_json_safe_values_without_local_redaction() -> None:
    from iac_code.web.events import encode_sse, make_event

    @dataclass
    class Metadata:
        title: str
        token: str

    class UnknownObject:
        def __str__(self) -> str:
            return "unknown-object"

    event = make_event(
        "session-1",
        8,
        "tool.result",
        {
            "text": "ok",
            "count": 3,
            "enabled": True,
            "empty": None,
            "created": datetime(2026, 6, 25, 12, 30, tzinfo=timezone.utc),
            "day": date(2026, 6, 25),
            "path": Path("/tmp/template.yml"),
            "blob": b"hello \xff",
            "tags": {"ros", "terraform"},
            "items": (Path("relative.txt"), b"nested"),
            "mapping": {1: Path("/tmp/one"), "secret": b"unsafe"},
            "metadata": Metadata(title="resource", token="unsafe"),
            "unknown": UnknownObject(),
            "apiKey": Path("/tmp/unsafe"),
        },
    )

    json.dumps(event)
    encoded = encode_sse(event)
    encoded_data = json.loads(encoded.split("data: ", 1)[1].strip())

    payload = event["payload"]
    assert encoded_data == event
    assert payload["text"] == "ok"
    assert payload["count"] == 3
    assert payload["enabled"] is True
    assert payload["empty"] is None
    assert payload["created"] == "2026-06-25T12:30:00+00:00"
    assert payload["day"] == "2026-06-25"
    assert payload["path"] == "/tmp/template.yml"
    assert payload["blob"] == "hello �"
    assert sorted(payload["tags"]) == ["ros", "terraform"]
    assert payload["items"] == ["relative.txt", "nested"]
    assert payload["mapping"] == {"1": "/tmp/one", "secret": "unsafe"}
    assert payload["metadata"] == {"title": "resource", "token": "unsafe"}
    assert payload["unknown"] == "unknown-object"
    assert payload["apiKey"] == "/tmp/unsafe"


def test_make_event_normalizes_non_finite_floats_for_browser_safe_json() -> None:
    from iac_code.web.events import encode_sse, make_event

    event = make_event(
        "session-1",
        9,
        "tool.result",
        {
            "metrics": {
                "nan": float("nan"),
                "pos_inf": float("inf"),
                "neg_inf": float("-inf"),
            },
            "items": [float("nan"), float("inf"), float("-inf")],
        },
    )

    json.dumps(event, allow_nan=False)
    encoded = encode_sse(event)
    data = encoded.split("data: ", 1)[1].strip()

    assert "NaN" not in data
    assert "Infinity" not in data
    assert "-Infinity" not in data
    assert json.loads(data) == event
    assert event["payload"]["metrics"] == {
        "nan": "nan",
        "pos_inf": "inf",
        "neg_inf": "-inf",
    }
    assert event["payload"]["items"] == ["nan", "inf", "-inf"]


def test_encode_sse_uses_event_id_and_json_data() -> None:
    from iac_code.web.events import encode_sse

    event = {
        "type": "assistant.text.delta",
        "sequence": 2,
        "sessionId": "session-1",
        "createdAt": "2026-06-25T00:00:00Z",
        "payload": {"text": "hello"},
    }

    encoded = encode_sse(event)

    assert encoded.startswith("event: assistant.text.delta\nid: 2\ndata: ")
    assert encoded.endswith("\n\n")
    data = encoded.split("data: ", 1)[1].strip()
    assert json.loads(data) == event


def test_encode_sse_rejects_raw_non_finite_float_tokens() -> None:
    from iac_code.web.events import encode_sse

    event = {
        "type": "tool.result",
        "sequence": 10,
        "sessionId": "session-1",
        "createdAt": "2026-06-25T00:00:00Z",
        "payload": {"value": float("nan")},
    }

    with pytest.raises(ValueError, match="Out of range float values are not JSON compliant"):
        encode_sse(event)


def test_web_session_owns_event_buffer_with_session_id(tmp_path) -> None:
    from iac_code.web.session_manager import WebSessionManager

    manager = WebSessionManager(projects_dir=tmp_path / "projects")
    session = manager.create_session(session_id="session-1")

    assert session.events.session_id == "session-1"
    assert session.events.append("session.created", {})["sessionId"] == "session-1"


def test_sse_route_replays_buffered_events_by_after_sequence(tmp_path) -> None:
    from iac_code.web.app import create_app
    from iac_code.web.session_manager import WebSessionManager

    manager = WebSessionManager(projects_dir=tmp_path / "projects")
    session = manager.create_session(session_id="session-1")
    session.events.append("assistant.message.start", {"messageId": "m1"})
    replayed = session.events.append("assistant.text.delta", {"messageId": "m1", "text": "hello"})
    app = create_app(session_manager=manager)

    body = asyncio.run(_read_stream_chunks(app, "/api/sessions/session-1/events?afterSequence=1"))
    [event] = _decode_sse_events(body)

    assert event["event"] == "assistant.text.delta"
    assert event["id"] == str(replayed["sequence"])
    assert event["data"] == replayed


def test_sse_route_returns_event_stream_response_for_existing_session(tmp_path) -> None:
    from iac_code.web.app import create_app
    from iac_code.web.session_manager import WebSessionManager

    manager = WebSessionManager(projects_dir=tmp_path / "projects")
    session = manager.create_session(session_id="session-1")
    session.events.append("assistant.text.delta", {"text": "hello"})
    app = create_app(session_manager=manager)

    status, headers, body = asyncio.run(_read_stream_response(app, "/api/sessions/session-1/events"))

    assert status == 200
    assert headers["content-type"].startswith("text/event-stream")
    assert "event: assistant.text.delta" in body


def test_sse_route_honors_last_event_id_header_when_query_cursor_is_missing(tmp_path) -> None:
    from iac_code.web.app import create_app
    from iac_code.web.session_manager import WebSessionManager

    manager = WebSessionManager(projects_dir=tmp_path / "projects")
    session = manager.create_session(session_id="session-1")
    session.events.append("assistant.message.start", {"messageId": "m1"})
    replayed = session.events.append("assistant.text.delta", {"messageId": "m1", "text": "hello"})
    app = create_app(session_manager=manager)

    body = asyncio.run(_read_stream_chunks(app, "/api/sessions/session-1/events", headers={"Last-Event-ID": "1"}))
    [event] = _decode_sse_events(body)

    assert event["event"] == "assistant.text.delta"
    assert event["id"] == str(replayed["sequence"])
    assert event["data"] == replayed


def test_sse_route_after_sequence_query_takes_precedence_over_last_event_id_header(tmp_path) -> None:
    from iac_code.web.app import create_app
    from iac_code.web.session_manager import WebSessionManager

    manager = WebSessionManager(projects_dir=tmp_path / "projects")
    session = manager.create_session(session_id="session-1")
    session.events.append("event.one", {})
    session.events.append("event.two", {})
    replayed = session.events.append("event.three", {})
    app = create_app(session_manager=manager)

    body = asyncio.run(
        _read_stream_chunks(
            app,
            "/api/sessions/session-1/events?afterSequence=2",
            headers={"Last-Event-ID": "1"},
        )
    )
    [event] = _decode_sse_events(body)

    assert event["event"] == "event.three"
    assert event["id"] == str(replayed["sequence"])
    assert event["data"] == replayed


def test_sse_route_uses_newer_last_event_id_when_query_cursor_is_stale(tmp_path) -> None:
    from iac_code.web.app import create_app
    from iac_code.web.session_manager import WebSessionManager

    manager = WebSessionManager(projects_dir=tmp_path / "projects")
    session = manager.create_session(session_id="session-1")
    session.events.append("event.one", {})
    session.events.append("event.two", {})
    replayed = session.events.append("event.three", {})
    app = create_app(session_manager=manager)

    body = asyncio.run(
        _read_stream_chunks(
            app,
            "/api/sessions/session-1/events?afterSequence=1",
            headers={"Last-Event-ID": "2"},
        )
    )
    [event] = _decode_sse_events(body)

    assert event["event"] == "event.three"
    assert event["id"] == str(replayed["sequence"])
    assert event["data"] == replayed


def test_sse_route_rejects_invalid_after_sequence_with_json_error(tmp_path) -> None:
    from iac_code.web.app import create_app
    from iac_code.web.session_manager import WebSessionManager

    manager = WebSessionManager(projects_dir=tmp_path / "projects")
    manager.create_session(session_id="session-1")
    app = create_app(session_manager=manager)

    with TestClient(app) as client:
        response = client.get("/api/sessions/session-1/events?afterSequence=not-an-int")

    assert response.status_code == 400
    assert response.json() == {"error": {"message": "afterSequence must be an integer"}}


def test_sse_route_connect_marks_session_viewed(tmp_path) -> None:
    from iac_code.web.app import create_app
    from iac_code.web.session_manager import WebSessionManager

    manager = WebSessionManager(projects_dir=tmp_path / "projects")
    session = manager.create_session(session_id="session-1")
    session.unread = True
    session.events.append("assistant.text.delta", {"text": "hello"})
    app = create_app(session_manager=manager)

    # 建立 SSE 订阅 = 用户正在查看该会话：应清除结束未读标记。
    asyncio.run(_read_stream_chunks(app, "/api/sessions/session-1/events"))

    assert session.unread is False


@pytest.mark.asyncio
async def test_sse_does_not_mark_session_viewed_before_stream_subscription_starts(tmp_path) -> None:
    from iac_code.web.app import create_app
    from iac_code.web.session_manager import WebSessionManager

    manager = WebSessionManager(projects_dir=tmp_path / "projects")
    session = manager.create_session(session_id="session-view-race")
    session.unread = True
    session.events.append("assistant.text.delta", {"text": "hello"})
    app = create_app(session_manager=manager)
    response_started = asyncio.Event()
    allow_response_start = asyncio.Event()
    response_body = asyncio.Event()
    disconnected = asyncio.Event()
    sent_request = False

    scope = {
        "type": "http",
        "http_version": "1.1",
        "method": "GET",
        "scheme": "http",
        "path": "/api/sessions/session-view-race/events",
        "raw_path": b"/api/sessions/session-view-race/events",
        "query_string": b"",
        "headers": [],
        "client": ("testclient", 50000),
        "server": ("testserver", 80),
        "root_path": "",
    }

    async def receive():
        nonlocal sent_request
        if not sent_request:
            sent_request = True
            return {"type": "http.request", "body": b"", "more_body": False}
        await disconnected.wait()
        return {"type": "http.disconnect"}

    async def send(message):
        if message["type"] == "http.response.start":
            response_started.set()
            await allow_response_start.wait()
        elif message["type"] == "http.response.body" and message.get("body"):
            response_body.set()

    task = asyncio.create_task(app(scope, receive, send))
    try:
        await asyncio.wait_for(response_started.wait(), timeout=1)
        assert session.unread is True
        allow_response_start.set()
        await asyncio.wait_for(response_body.wait(), timeout=1)
        assert session.unread is False
    finally:
        disconnected.set()
        task.cancel()
        with suppress(asyncio.CancelledError):
            await task


def test_sse_route_returns_json_404_for_missing_session(tmp_path) -> None:
    from iac_code.web.app import create_app
    from iac_code.web.session_manager import WebSessionManager

    manager = WebSessionManager(projects_dir=tmp_path / "projects")
    app = create_app(session_manager=manager)

    with TestClient(app) as client:
        response = client.get("/api/sessions/missing/events")

    assert response.status_code == 404
    assert response.headers["content-type"].startswith("application/json")
    assert response.json() == {"error": {"message": "session not found"}}


def test_sse_route_emits_resync_event_when_after_sequence_is_older_than_floor(tmp_path) -> None:
    from iac_code.web.app import create_app
    from iac_code.web.events import WebEventBuffer
    from iac_code.web.session_manager import WebSessionManager

    manager = WebSessionManager(projects_dir=tmp_path / "projects")
    session = manager.create_session(session_id="session-1")
    session.events = WebEventBuffer("session-1", max_events=2)
    session.events.append("event.one", {})
    second = session.events.append("event.two", {})
    session.events.append("event.three", {})
    app = create_app(session_manager=manager)

    body = asyncio.run(_read_stream_chunks(app, "/api/sessions/session-1/events?afterSequence=0"))
    [event] = _decode_sse_events(body)
    data = event["data"]

    assert event["event"] == "session.resync.required"
    assert data["type"] == "session.resync.required"
    assert data["payload"] == {"afterSequence": 0, "floorSequence": second["sequence"]}


def test_tombstone_replay_preserves_sequence_order_and_message_identity() -> None:
    from iac_code.web.events import WebEventBuffer

    buffer = WebEventBuffer("session-1")
    buffer.append("assistant.message.start", {"messageId": "orphaned"})
    buffer.append("assistant.text.delta", {"messageId": "orphaned", "text": "partial"})
    buffer.append("assistant.message.tombstone", {"messageId": "orphaned"})
    buffer.append("assistant.message.start", {"messageId": "fallback"})

    replayed = buffer.replay_after(0)

    assert [event["sequence"] for event in replayed] == [1, 2, 3, 4]
    assert [event["type"] for event in replayed] == [
        "assistant.message.start",
        "assistant.text.delta",
        "assistant.message.tombstone",
        "assistant.message.start",
    ]
    assert replayed[2]["payload"]["messageId"] == "orphaned"
