import pytest
from a2a.types import TaskArtifactUpdateEvent
from google.protobuf.json_format import MessageToDict

from iac_code.a2a.events import _ERROR_TEXT_MAX_CHARS, _METADATA_MAX_CHARS, _truncate, publish_stream_event
from iac_code.a2a.exposure import A2AExposureType
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
async def test_permission_request_tool_input_redacts_secret_values() -> None:
    queue = FakeEventQueue()
    event = PermissionRequestEvent(
        tool_name="bash",
        tool_input={"cmd": 'cat /Users/alice/.iac-code/settings.yml && curl -H "Authorization: Bearer sk-live-secret"'},
        tool_use_id="tool-1",
    )

    await publish_stream_event(queue, task_id="task-1", context_id="ctx-1", event=event)

    dumped = dump(queue.events[0])
    tool_input = dumped["metadata"]["iac_code"]["permission"]["toolInput"]
    assert "sk-live-secret" not in str(tool_input)
    assert "Authorization: Bearer" not in str(tool_input)
    assert "/Users/alice" not in str(tool_input)
    assert "[REDACTED]" in str(tool_input)
    assert "[PATH]" in str(tool_input)


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
        event=ErrorEvent(error="boom with /secret/path", is_retryable=False, error_id="err-123"),
    )

    dumped = dump(queue.events[0])
    assert dumped["status"]["state"] == "TASK_STATE_FAILED"
    assert dumped["status"]["message"]["parts"][0]["text"] == "boom with /secret/path"
    assert dumped["metadata"]["iac_code"]["error"] == {"retryable": False, "errorId": "err-123"}


@pytest.mark.asyncio
async def test_error_event_redacts_public_error_text() -> None:
    queue = FakeEventQueue()

    await publish_stream_event(
        queue,
        task_id="task-1",
        context_id="ctx-1",
        event=ErrorEvent(
            error="RuntimeError: Authorization: Bearer sk-live at /Users/alice/.iac-code/settings.yml",
            is_retryable=False,
        ),
    )

    dumped = dump(queue.events[0])
    text = dumped["status"]["message"]["parts"][0]["text"]
    assert "sk-live" not in text
    assert "/Users/alice" not in text


@pytest.mark.asyncio
async def test_retryable_error_event_publishes_error_metadata() -> None:
    queue = FakeEventQueue()

    await publish_stream_event(
        queue,
        task_id="task-1",
        context_id="ctx-1",
        event=ErrorEvent(error="should not leak", is_retryable=True, error_id="err-retry"),
    )

    dumped = dump(queue.events[0])
    assert dumped["status"]["state"] == "TASK_STATE_INPUT_REQUIRED"
    assert dumped["status"]["message"]["parts"][0]["text"] == "A temporary error occurred. Please retry."
    assert dumped["metadata"]["iac_code"]["error"] == {"retryable": True, "errorId": "err-retry"}


@pytest.mark.asyncio
async def test_thinking_delta_is_explicitly_ignored() -> None:
    queue = FakeEventQueue()

    await publish_stream_event(queue, task_id="task-1", context_id="ctx-1", event=ThinkingDeltaEvent(text="hidden"))

    assert queue.events == []


@pytest.mark.asyncio
async def test_thinking_delta_publishes_raw_metadata_when_enabled() -> None:
    queue = FakeEventQueue()

    await publish_stream_event(
        queue,
        task_id="task-1",
        context_id="ctx-1",
        event=ThinkingDeltaEvent(text="visible"),
        exposure_types={A2AExposureType.RAW_THINKING},
    )

    assert len(queue.events) == 1
    dumped = dump(queue.events[0])
    assert dumped["status"]["state"] == "TASK_STATE_WORKING"
    assert dumped["metadata"]["iac_code"]["thinking"] == {
        "type": "raw_thinking",
        "text": "visible",
    }


@pytest.mark.asyncio
async def test_tool_events_are_suppressed_when_tool_trace_is_not_enabled() -> None:
    queue = FakeEventQueue()

    await publish_stream_event(
        queue,
        task_id="task-1",
        context_id="ctx-1",
        event=ToolUseStartEvent(tool_use_id="tool-1", name="bash"),
        exposure_types=frozenset(),
    )

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
async def test_tool_use_input_metadata_redacts_secret_values() -> None:
    queue = FakeEventQueue()

    await publish_stream_event(
        queue,
        task_id="task-1",
        context_id="ctx-1",
        event=ToolUseEndEvent(
            tool_use_id="tool-1",
            name="bash",
            input={"cmd": 'cat /Users/alice/.iac-code/settings.yml && curl -H "Authorization: Bearer sk-live-secret"'},
        ),
    )

    dumped = dump(queue.events[0])
    tool_input = dumped["metadata"]["iac_code"]["tool"]["input"]
    assert "sk-live-secret" not in str(tool_input)
    assert "Authorization: Bearer" not in str(tool_input)
    assert "/Users/alice" not in str(tool_input)
    assert "[REDACTED]" in str(tool_input)
    assert "[PATH]" in str(tool_input)


@pytest.mark.asyncio
async def test_tool_use_input_metadata_redacts_malformed_opaque_artifact_uri() -> None:
    queue = FakeEventQueue()
    malformed_uri = r"iac-code-artifact://artifact-1/C:\Users\alice\.iac-code\projects\demo\template.yaml"

    await publish_stream_event(
        queue,
        task_id="task-1",
        context_id="ctx-1",
        event=ToolUseEndEvent(
            tool_use_id="tool-1",
            name="bash",
            input={"cmd": f"cat {malformed_uri}", "note": malformed_uri},
        ),
    )

    dumped = dump(queue.events[0])
    tool_input = dumped["metadata"]["iac_code"]["tool"]["input"]
    rendered = str(tool_input)
    assert "[PATH]" in rendered
    assert "iac-code-artifac[PATH]" not in rendered
    assert "Users" not in rendered
    assert ".iac-code" not in rendered


@pytest.mark.asyncio
async def test_tool_use_input_metadata_redacts_percent_encoded_local_path() -> None:
    queue = FakeEventQueue()
    encoded_path = "file%3A%2F%2F%2FUsers%2Falice%2F.iac-code%2Fprojects%2Fdemo%2Ftemplate.yaml"

    await publish_stream_event(
        queue,
        task_id="task-1",
        context_id="ctx-1",
        event=ToolUseEndEvent(
            tool_use_id="tool-1",
            name="bash",
            input={"cmd": f"cat {encoded_path}"},
        ),
    )

    dumped = dump(queue.events[0])
    rendered = str(dumped["metadata"]["iac_code"]["tool"]["input"])
    assert "[PATH]" in rendered
    assert "%2FUsers" not in rendered
    assert ".iac-code" not in rendered


@pytest.mark.asyncio
async def test_failed_tool_result_metadata_is_sanitized() -> None:
    queue = FakeEventQueue()

    await publish_stream_event(
        queue,
        task_id="task-1",
        context_id="ctx-1",
        event=ToolResultEvent(
            tool_use_id="tool-1",
            tool_name="bash",
            result="Tool failed: DB_PASSWORD=hunter2 at /Users/alice/.iac-code/settings.yml",
            is_error=True,
        ),
    )

    dumped = dump(queue.events[0])
    tool = dumped["metadata"]["iac_code"]["tool"]
    assert tool["status"] == "failed"
    assert "hunter2" not in str(tool["result"])
    assert "/Users/alice" not in str(tool["result"])


@pytest.mark.asyncio
async def test_failed_tool_result_metadata_redacts_malformed_opaque_artifact_uri() -> None:
    queue = FakeEventQueue()
    malformed_uri = r"iac-code-artifact://artifact-1/C:\Users\alice\.iac-code\projects\demo\template.yaml"

    await publish_stream_event(
        queue,
        task_id="task-1",
        context_id="ctx-1",
        event=ToolResultEvent(
            tool_use_id="tool-1",
            tool_name="bash",
            result=f"Tool failed: {malformed_uri}",
            is_error=True,
        ),
    )

    dumped = dump(queue.events[0])
    rendered = str(dumped["metadata"]["iac_code"]["tool"]["result"])
    assert "[PATH]" in rendered
    assert "iac-code-artifac[PATH]" not in rendered
    assert "Users" not in rendered
    assert ".iac-code" not in rendered


@pytest.mark.asyncio
async def test_error_event_redacts_malformed_opaque_artifact_uri() -> None:
    queue = FakeEventQueue()
    malformed_uri = r"iac-code-artifact://artifact-1/C:\Users\alice\.iac-code\projects\demo\template.yaml"

    await publish_stream_event(
        queue,
        task_id="task-1",
        context_id="ctx-1",
        event=ErrorEvent(error=f"boom {malformed_uri}", is_retryable=False),
    )

    dumped = dump(queue.events[0])
    rendered = str(dumped["status"]["message"]["parts"][0]["text"])
    assert "[PATH]" in rendered
    assert "iac-code-artifac[PATH]" not in rendered
    assert "Users" not in rendered
    assert ".iac-code" not in rendered


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
async def test_tool_result_artifact_windows_filename_does_not_leak_path(tmp_path) -> None:
    from iac_code.a2a.artifacts import A2AArtifactStore

    queue = FakeEventQueue()
    store = A2AArtifactStore(tmp_path)
    result = {
        "artifact": {
            "filename": r"C:\Users\alice\.iac-code\projects\demo\template.yaml",
            "mediaType": "text/yaml",
            "content": "ROSTemplate",
        }
    }

    await publish_stream_event(
        queue,
        task_id="task-1",
        context_id="ctx-1",
        event=ToolResultEvent(tool_use_id="tool-1", tool_name="write_file", result=result, is_error=False),
        artifact_store=store,
    )

    dumped = dump(queue.events[0])
    rendered = str(dumped)
    assert dumped["artifact"]["name"] == "template.yaml"
    assert dumped["artifact"]["parts"][0]["filename"] == "template.yaml"
    assert r"C:\\" not in rendered
    assert "%5CUsers" not in rendered
    assert ".iac-code" not in rendered


@pytest.mark.asyncio
async def test_tool_result_uri_only_artifact_drops_legacy_file_uri() -> None:
    queue = FakeEventQueue()
    result = {
        "artifact": {
            "filename": "template.yaml",
            "uri": r"file://C:\Users\alice\.iac-code\projects\demo\template.yaml",
            "downloadUrl": r"file://C:\Users\alice\.iac-code\projects\demo\template.yaml",
            "publicUrl": r"iac-code-artifact://artifact-1/C:\Users\alice\.iac-code\projects\demo\template.yaml",
            "encodedOwnerUrl": "iac-code-artifact://C%3A%5CUsers%5Calice%5C.iac-code%5Cprojects%5Cdemo/template.yaml",
            "backupUri": [r"file://C:\Users\alice\.iac-code\projects\demo\template.yaml"],
            "sourceUri": r"file://C:\Users\alice\.iac-code\projects\demo\template.yaml",
            "source": r"file://C:\Users\alice\.iac-code\projects\demo\template.yaml",
            "metadata": {
                "uri": [r"file://C:\Users\alice\.iac-code\projects\demo\template.yaml"],
                "byteSize": 10,
            },
            "parts": [
                {
                    "url": r"file://C:\Users\alice\.iac-code\projects\demo\template.yaml",
                    "metadata": {"uri": r"file://C:\Users\alice\.iac-code\projects\demo\template.yaml"},
                }
            ],
        }
    }

    await publish_stream_event(
        queue,
        task_id="task-1",
        context_id="ctx-1",
        event=ToolResultEvent(tool_use_id="tool-1", tool_name="write_file", result=result, is_error=False),
    )

    dumped = dump(queue.events[0])
    artifact = dumped["metadata"]["iac_code"]["tool"]["result"]["artifact"]
    rendered = str(dumped)
    assert artifact["filename"] == "template.yaml"
    assert artifact["metadata"] == {"byteSize": 10}
    assert "uri" not in artifact
    assert "downloadUrl" not in artifact
    assert "publicUrl" not in artifact
    assert "encodedOwnerUrl" not in artifact
    assert "backupUri" not in artifact
    assert "sourceUri" not in artifact
    assert artifact["source"] == "[PATH]"
    assert "url" not in artifact["parts"][0]
    assert "uri" not in artifact["parts"][0]["metadata"]
    assert "file://" not in rendered
    assert "Users" not in rendered
    assert ".iac-code" not in rendered


@pytest.mark.asyncio
async def test_tool_result_uri_only_artifact_keeps_valid_opaque_uri() -> None:
    queue = FakeEventQueue()
    uri = "iac-code-artifact://artifact-1/template.yaml"
    result = {
        "artifact": {
            "filename": "template.yaml",
            "uri": uri,
            "downloadUrl": uri,
            "parts": [{"url": uri}],
        }
    }

    await publish_stream_event(
        queue,
        task_id="task-1",
        context_id="ctx-1",
        event=ToolResultEvent(tool_use_id="tool-1", tool_name="write_file", result=result, is_error=False),
    )

    dumped = dump(queue.events[0])
    artifact = dumped["metadata"]["iac_code"]["tool"]["result"]["artifact"]
    assert artifact["uri"] == uri
    assert artifact["downloadUrl"] == uri
    assert artifact["parts"][0]["url"] == uri
    rendered = str(dumped)
    assert "iac-code-artifac[PATH]" not in rendered
    assert "file://" not in rendered


@pytest.mark.asyncio
async def test_tool_result_artifact_list_is_sanitized() -> None:
    queue = FakeEventQueue()
    legacy_uri = r"file://C:\Users\alice\.iac-code\projects\demo\template.yaml"
    result = {
        "artifact": [
            legacy_uri,
            {
                "filename": r"C:\Users\alice\.iac-code\projects\demo\template.yaml",
                "uri": [legacy_uri],
                "parts": [legacy_uri, {"url": legacy_uri}],
            },
        ]
    }

    await publish_stream_event(
        queue,
        task_id="task-1",
        context_id="ctx-1",
        event=ToolResultEvent(tool_use_id="tool-1", tool_name="write_file", result=result, is_error=False),
    )

    dumped = dump(queue.events[0])
    artifact = dumped["metadata"]["iac_code"]["tool"]["result"]["artifact"]
    assert artifact[0] == "[PATH]"
    assert artifact[1]["filename"] == "template.yaml"
    assert "uri" not in artifact[1]
    assert artifact[1]["parts"][0] == "[PATH]"
    assert "url" not in artifact[1]["parts"][1]
    rendered = str(dumped)
    assert "file://" not in rendered
    assert "Users" not in rendered
    assert ".iac-code" not in rendered


@pytest.mark.asyncio
async def test_tool_result_artifact_scalar_is_sanitized() -> None:
    queue = FakeEventQueue()
    result = {"artifact": r"file://C:\Users\alice\.iac-code\projects\demo\template.yaml"}

    await publish_stream_event(
        queue,
        task_id="task-1",
        context_id="ctx-1",
        event=ToolResultEvent(tool_use_id="tool-1", tool_name="write_file", result=result, is_error=False),
    )

    dumped = dump(queue.events[0])
    artifact = dumped["metadata"]["iac_code"]["tool"]["result"]["artifact"]
    assert artifact == "[PATH]"
    rendered = str(dumped)
    assert "file://" not in rendered
    assert "Users" not in rendered
    assert ".iac-code" not in rendered


@pytest.mark.asyncio
async def test_tool_result_artifact_payload_keys_are_sanitized_case_insensitively() -> None:
    queue = FakeEventQueue()
    result = {
        "artifact": {
            "filename": "result.txt",
            "Content": "secret content",
            "Raw": "secret raw",
            "Base64": "c2VjcmV0",
            "Path": r"C:\Users\alice\.iac-code\projects\demo\template.yaml",
            "metadata": {"label": "safe", "api_key": "plain-secret"},
        }
    }

    await publish_stream_event(
        queue,
        task_id="task-1",
        context_id="ctx-1",
        event=ToolResultEvent(tool_use_id="tool-1", tool_name="write_file", result=result, is_error=False),
    )

    dumped = dump(queue.events[0])
    artifact = dumped["metadata"]["iac_code"]["tool"]["result"]["artifact"]
    assert artifact == {"filename": "result.txt", "metadata": {"label": "safe", "api_key": "[REDACTED]"}}
    rendered = str(dumped)
    assert "secret content" not in rendered
    assert "secret raw" not in rendered
    assert "c2VjcmV0" not in rendered
    assert "plain-secret" not in rendered
    assert "Users" not in rendered
    assert ".iac-code" not in rendered


@pytest.mark.asyncio
async def test_tool_result_metadata_sanitizes_root_artifact_list() -> None:
    queue = FakeEventQueue()
    result = [
        {
            "artifact": {
                "filename": "template.yaml",
                "Content": "RAW-TEMPLATE-CONTENT",
                "metadata": {"token": "plain-token"},
                "uri": r"file:///Users/Alice and Bob/.iac-code/projects/demo/template.yaml",
            }
        }
    ]

    await publish_stream_event(
        queue,
        task_id="task-1",
        context_id="ctx-1",
        event=ToolResultEvent(tool_use_id="tool-1", tool_name="write_file", result=result, is_error=False),
    )

    dumped = dump(queue.events[0])
    rendered = str(dumped)
    artifact = dumped["metadata"]["iac_code"]["tool"]["result"][0]["artifact"]
    assert artifact == {"filename": "template.yaml", "metadata": {"token": "[REDACTED]"}}
    assert "RAW-TEMPLATE-CONTENT" not in rendered
    assert "plain-token" not in rendered
    assert "Alice and Bob" not in rendered
    assert ".iac-code" not in rendered


@pytest.mark.asyncio
async def test_tool_result_metadata_sanitizes_case_variant_artifact_key() -> None:
    queue = FakeEventQueue()
    result = {
        "Artifact": {
            "filename": "template.yaml",
            "Content": "RAW-TEMPLATE-CONTENT",
            "uri": r"file:///Users/Alice and Bob/.iac-code/projects/demo/template.yaml",
        }
    }

    await publish_stream_event(
        queue,
        task_id="task-1",
        context_id="ctx-1",
        event=ToolResultEvent(tool_use_id="tool-1", tool_name="write_file", result=result, is_error=False),
    )

    dumped = dump(queue.events[0])
    rendered = str(dumped)
    artifact = dumped["metadata"]["iac_code"]["tool"]["result"]["Artifact"]
    assert artifact == {"filename": "template.yaml"}
    assert "RAW-TEMPLATE-CONTENT" not in rendered
    assert "Alice and Bob" not in rendered
    assert ".iac-code" not in rendered


@pytest.mark.asyncio
async def test_failed_tool_result_dict_artifact_payload_is_sanitized() -> None:
    queue = FakeEventQueue()
    result = {
        "artifact": {
            "filename": "template.yaml",
            "Content": "RAW-TEMPLATE-CONTENT",
            "Raw": "RAW",
            "Base64": "UkFX",
            "metadata": {"Authorization": "Bearer plain-auth-value"},
        },
        "api_key": "secret-key",
    }

    await publish_stream_event(
        queue,
        task_id="task-1",
        context_id="ctx-1",
        event=ToolResultEvent(tool_use_id="tool-1", tool_name="write_file", result=result, is_error=True),
    )

    dumped = dump(queue.events[0])
    rendered = str(dumped)
    result_metadata = dumped["metadata"]["iac_code"]["tool"]["result"]
    assert result_metadata == {
        "artifact": {"filename": "template.yaml", "metadata": {"Authorization": "[REDACTED]"}},
        "api_key": "[REDACTED]",
    }
    assert "RAW-TEMPLATE-CONTENT" not in rendered
    assert "plain-auth-value" not in rendered
    assert "secret-key" not in rendered


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
    assert dumped["artifact"]["parts"][0]["url"].startswith("iac-code-artifact://")
    assert dumped["artifact"]["parts"][0]["mediaType"] == "text/plain"
    assert dumped["artifact"]["metadata"]["byteSize"] == 14
    assert dumped["lastChunk"] is True
    assert dumped.get("append", False) is False
    rendered = str(dumped)
    assert "file://" not in rendered
    assert str(tmp_path) not in rendered
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
