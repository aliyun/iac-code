import pytest
from a2a.types import TaskArtifactUpdateEvent
from google.protobuf.json_format import MessageToDict

from iac_code.a2a.events import _ERROR_TEXT_MAX_CHARS, _METADATA_MAX_CHARS, _truncate, publish_stream_event
from iac_code.types.stream_events import (
    ErrorEvent,
    MessageEndEvent,
    PermissionRequestEvent,
    TextDeltaEvent,
    ThinkingDeltaEvent,
    ToolInputDeltaEvent,
    ToolResultEvent,
    ToolUseEndEvent,
    ToolUseStartEvent,
    Usage,
)

from .fakes import FakeEventQueue, UnknownEvent, pending_future


def dump(event):
    return MessageToDict(event, preserving_proto_field_name=False)


@pytest.mark.asyncio
async def test_text_delta_publishes_agent_message() -> None:
    queue = FakeEventQueue()

    await publish_stream_event(queue, task_id="task-1", context_id="ctx-1", event=TextDeltaEvent(text="hello"))

    assert len(queue.events) == 1
    dumped = dump(queue.events[0])
    assert dumped["status"]["message"]["parts"][0]["text"] == "hello"
    assert dumped["status"]["message"]["role"] == "ROLE_AGENT"


@pytest.mark.asyncio
async def test_empty_text_delta_is_ignored() -> None:
    queue = FakeEventQueue()

    await publish_stream_event(queue, task_id="task-1", context_id="ctx-1", event=TextDeltaEvent(text=""))

    assert queue.events == []


@pytest.mark.asyncio
async def test_permission_request_is_denied_by_default_and_truncated() -> None:
    queue = FakeEventQueue()
    future = pending_future()
    long_value = "x" * (_METADATA_MAX_CHARS + 100)
    event = PermissionRequestEvent(
        tool_name="bash", tool_input={"cmd": long_value}, tool_use_id="tool-1", response_future=future
    )

    await publish_stream_event(queue, task_id="task-1", context_id="ctx-1", event=event)

    assert future.result() is False
    dumped = dump(queue.events[0])
    assert dumped["metadata"]["iac_code"]["permission"]["autoApproved"] is False
    assert len(dumped["metadata"]["iac_code"]["permission"]["toolInput"]["cmd"]) == _METADATA_MAX_CHARS


@pytest.mark.asyncio
async def test_permission_request_uses_configured_default_decision() -> None:
    queue = FakeEventQueue()
    future = pending_future()
    event = PermissionRequestEvent(
        tool_name="bash",
        tool_input={"cmd": "pwd"},
        tool_use_id="tool-1",
        response_future=future,
    )

    await publish_stream_event(
        queue,
        task_id="task-1",
        context_id="ctx-1",
        event=event,
        auto_approve_permissions=True,
    )

    assert future.result() is True
    dumped = dump(queue.events[0])
    assert dumped["metadata"]["iac_code"]["permission"]["autoApproved"] is True


@pytest.mark.asyncio
async def test_permission_request_uses_async_resolver() -> None:
    queue = FakeEventQueue()
    future = pending_future()
    event = PermissionRequestEvent(
        tool_name="bash",
        tool_input={"cmd": "pwd"},
        tool_use_id="tool-1",
        response_future=future,
    )
    seen: list[str] = []

    async def approve(request: PermissionRequestEvent) -> bool:
        seen.append(request.tool_use_id)
        return True

    await publish_stream_event(
        queue,
        task_id="task-1",
        context_id="ctx-1",
        event=event,
        permission_resolver=approve,
    )

    assert seen == ["tool-1"]
    assert future.result() is True
    dumped = dump(queue.events[0])
    assert dumped["metadata"]["iac_code"]["permission"]["autoApproved"] is True


@pytest.mark.asyncio
async def test_unknown_event_is_skipped() -> None:
    queue = FakeEventQueue()

    await publish_stream_event(queue, task_id="task-1", context_id="ctx-1", event=UnknownEvent())

    assert queue.events == []


@pytest.mark.asyncio
async def test_unknown_event_logs_debug(caplog: pytest.LogCaptureFixture) -> None:
    queue = FakeEventQueue()
    caplog.set_level("DEBUG")

    await publish_stream_event(queue, task_id="task-1", context_id="ctx-1", event=UnknownEvent())

    assert "Skipping unmapped A2A stream event: UnknownEvent" in caplog.text


def test_truncate_limits_nested_depth() -> None:
    value = "leaf"
    for _ in range(80):
        value = {"next": value}

    truncated = _truncate(value)

    current = truncated
    for _ in range(32):
        current = current["next"]
    assert current == "[truncated-depth]"


@pytest.mark.asyncio
async def test_error_event_passes_through_error_field() -> None:
    queue = FakeEventQueue()

    await publish_stream_event(
        queue,
        task_id="task-1",
        context_id="ctx-1",
        event=ErrorEvent(error="boom with /secret/path", is_retryable=False),
    )

    dumped = dump(queue.events[0])
    assert dumped["status"]["state"] == "TASK_STATE_FAILED"
    assert dumped["status"]["message"]["parts"][0]["text"] == "boom with /secret/path"


@pytest.mark.asyncio
async def test_thinking_delta_is_explicitly_ignored() -> None:
    queue = FakeEventQueue()

    await publish_stream_event(queue, task_id="task-1", context_id="ctx-1", event=ThinkingDeltaEvent(text="hidden"))

    assert queue.events == []


@pytest.mark.asyncio
async def test_tool_events_publish_metadata_updates() -> None:
    queue = FakeEventQueue()

    await publish_stream_event(
        queue, task_id="task-1", context_id="ctx-1", event=ToolUseStartEvent(tool_use_id="tool-1", name="bash")
    )
    await publish_stream_event(
        queue,
        task_id="task-1",
        context_id="ctx-1",
        event=ToolInputDeltaEvent(tool_use_id="tool-1", partial_json='{"cmd"'),
    )
    await publish_stream_event(
        queue,
        task_id="task-1",
        context_id="ctx-1",
        event=ToolUseEndEvent(tool_use_id="tool-1", name="bash", input={"cmd": "pwd"}),
    )
    await publish_stream_event(
        queue,
        task_id="task-1",
        context_id="ctx-1",
        event=ToolResultEvent(tool_use_id="tool-1", tool_name="bash", result="ok", is_error=False),
    )

    dumped = [dump(event) for event in queue.events]
    assert dumped[0]["metadata"]["iac_code"]["tool"]["status"] == "started"
    assert dumped[1]["metadata"]["iac_code"]["tool"]["status"] == "input_delta"
    assert dumped[2]["metadata"]["iac_code"]["tool"]["status"] == "input_complete"
    assert dumped[2]["metadata"]["iac_code"]["tool"]["name"] == "bash"
    assert dumped[3]["metadata"]["iac_code"]["tool"]["status"] == "completed"


@pytest.mark.asyncio
async def test_tool_result_externalizes_large_file_metadata(tmp_path) -> None:
    from iac_code.a2a.artifacts import A2AArtifactStore

    queue = FakeEventQueue()
    store = A2AArtifactStore(tmp_path)
    result = {"artifact": {"filename": "result.txt", "mediaType": "text/plain", "content": "hello artifact"}}

    await publish_stream_event(
        queue,
        task_id="task-1",
        context_id="ctx-1",
        event=ToolResultEvent(tool_use_id="tool-1", tool_name="write_file", result=result, is_error=False),
        artifact_store=store,
    )

    dumped = dump(queue.events[1])
    artifact = dumped["metadata"]["iac_code"]["tool"]["artifact"]
    assert artifact["filename"] == "result.txt"
    assert artifact["byteSize"] == 14


@pytest.mark.asyncio
async def test_tool_result_publishes_standard_artifact_update_event(tmp_path) -> None:
    from iac_code.a2a.artifacts import A2AArtifactStore

    queue = FakeEventQueue()
    store = A2AArtifactStore(tmp_path)
    result = {"artifact": {"filename": "result.txt", "mediaType": "text/plain", "content": "hello artifact"}}

    await publish_stream_event(
        queue,
        task_id="task-1",
        context_id="ctx-1",
        event=ToolResultEvent(tool_use_id="tool-1", tool_name="write_file", result=result, is_error=False),
        artifact_store=store,
    )

    artifact_event = queue.events[0]
    assert isinstance(artifact_event, TaskArtifactUpdateEvent)
    dumped = dump(artifact_event)
    assert dumped["artifact"]["name"] == "result.txt"
    assert dumped["artifact"]["parts"][0]["url"].startswith("file://")
    assert dumped["artifact"]["parts"][0]["mediaType"] == "text/plain"
    assert dumped["artifact"]["metadata"]["byteSize"] == 14
    assert dumped["lastChunk"] is True
    assert dumped.get("append", False) is False
    assert (
        dumped["artifact"]["artifactId"]
        == dump(queue.events[1])["metadata"]["iac_code"]["tool"]["artifact"]["artifactId"]
    )


@pytest.mark.asyncio
async def test_tool_result_skips_non_text_artifact_content(tmp_path) -> None:
    from iac_code.a2a.artifacts import A2AArtifactStore

    queue = FakeEventQueue()
    store = A2AArtifactStore(tmp_path)
    result = {"artifact": {"filename": "result.bin", "mediaType": "application/octet-stream", "content": b"binary"}}

    await publish_stream_event(
        queue,
        task_id="task-1",
        context_id="ctx-1",
        event=ToolResultEvent(tool_use_id="tool-1", tool_name="write_file", result=result, is_error=False),
        artifact_store=store,
    )

    dumped = dump(queue.events[0])
    assert "artifact" not in dumped["metadata"]["iac_code"]["tool"]


@pytest.mark.asyncio
async def test_tool_result_externalizes_base64_binary_artifact(tmp_path) -> None:
    from iac_code.a2a.artifacts import A2AArtifactStore

    queue = FakeEventQueue()
    store = A2AArtifactStore(tmp_path)
    result = {
        "artifact": {
            "filename": "diagram.png",
            "mediaType": "image/png",
            "bytes": "iVBORw0KGgppbWFnZQ==",
        }
    }

    await publish_stream_event(
        queue,
        task_id="task-1",
        context_id="ctx-1",
        event=ToolResultEvent(tool_use_id="tool-1", tool_name="draw", result=result, is_error=False),
        artifact_store=store,
    )

    artifact_event = queue.events[0]
    assert isinstance(artifact_event, TaskArtifactUpdateEvent)
    dumped = dump(artifact_event)
    assert dumped["artifact"]["parts"][0]["mediaType"] == "image/png"
    assert dumped["artifact"]["metadata"]["byteSize"] == 13
    artifact_metadata = dump(queue.events[1])["metadata"]["iac_code"]["tool"]["artifact"]
    assert artifact_metadata["mediaType"] == "image/png"
    assert store.path_for(artifact_metadata["artifactId"]).read_bytes() == b"\x89PNG\r\n\x1a\nimage"


@pytest.mark.asyncio
async def test_tool_result_externalizes_workspace_path_binary_artifact(tmp_path) -> None:
    from iac_code.a2a.artifacts import A2AArtifactStore

    source = tmp_path / "voice.wav"
    source.write_bytes(b"RIFFaudio")
    queue = FakeEventQueue()
    store = A2AArtifactStore(tmp_path / "artifacts")
    result = {"artifact": {"filename": "voice.wav", "mediaType": "audio/wav", "path": str(source)}}

    await publish_stream_event(
        queue,
        task_id="task-1",
        context_id="ctx-1",
        event=ToolResultEvent(tool_use_id="tool-1", tool_name="record", result=result, is_error=False),
        artifact_store=store,
    )

    artifact_metadata = dump(queue.events[1])["metadata"]["iac_code"]["tool"]["artifact"]
    assert artifact_metadata["byteSize"] == 9
    assert store.path_for(artifact_metadata["artifactId"]).read_bytes() == b"RIFFaudio"


@pytest.mark.asyncio
async def test_message_end_publishes_usage_metadata() -> None:
    queue = FakeEventQueue()

    await publish_stream_event(
        queue,
        task_id="task-1",
        context_id="ctx-1",
        event=MessageEndEvent(stop_reason="end_turn", usage=Usage(input_tokens=2, output_tokens=3)),
    )

    dumped = dump(queue.events[0])
    assert dumped["metadata"]["iac_code"]["usage"]["totalTokens"] == 5


@pytest.mark.asyncio
async def test_error_event_truncates_overlong_payload() -> None:
    queue = FakeEventQueue()
    long_error = "X" * (_ERROR_TEXT_MAX_CHARS + 500)

    await publish_stream_event(
        queue,
        task_id="task-1",
        context_id="ctx-1",
        event=ErrorEvent(error=long_error, is_retryable=False),
    )

    dumped = dump(queue.events[0])
    text = dumped["status"]["message"]["parts"][0]["text"]
    assert len(text) <= _ERROR_TEXT_MAX_CHARS
    assert text == "X" * _ERROR_TEXT_MAX_CHARS


@pytest.mark.asyncio
async def test_retryable_error_event_still_says_retry() -> None:
    queue = FakeEventQueue()

    await publish_stream_event(
        queue,
        task_id="task-1",
        context_id="ctx-1",
        event=ErrorEvent(error="should not leak", is_retryable=True),
    )

    dumped = dump(queue.events[0])
    assert dumped["status"]["state"] == "TASK_STATE_INPUT_REQUIRED"
    assert dumped["status"]["message"]["parts"][0]["text"] == "A temporary error occurred. Please retry."
