import pytest
from a2a.types import TaskArtifactUpdateEvent
from google.protobuf.json_format import MessageToDict

from iac_code.a2a.events import _ERROR_TEXT_MAX_CHARS, _METADATA_MAX_CHARS, _truncate, publish_stream_event
from iac_code.a2a.exposure import A2AExposureType
from iac_code.services.permissions.audit import fingerprint_text
from iac_code.types.stream_events import (
    ErrorEvent,
    MCPProgressEvent,
    MessageEndEvent,
    PermissionRequestEvent,
    SubPipelineStreamEvent,
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
async def test_permission_request_is_denied_by_default_and_uses_shape_only_tool_input() -> None:
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
    assert dumped["metadata"]["iac_code"]["permission"]["toolInput"]["cmd"] == {
        "type": "str",
        "length": len(long_value),
        "fingerprint": fingerprint_text(long_value),
    }


@pytest.mark.asyncio
async def test_mcp_progress_publishes_tool_trace_metadata() -> None:
    queue = FakeEventQueue()

    await publish_stream_event(
        queue,
        task_id="task-1",
        context_id="ctx-1",
        event=MCPProgressEvent(
            server_name="live",
            tool_name="echo",
            progress=1,
            total=2,
            message="halfway",
            tool_use_id="tool-1",
        ),
    )

    dumped = dump(queue.events[0])
    tool = dumped["metadata"]["iac_code"]["tool"]
    assert tool["status"] == "progress"
    assert tool["toolUseId"] == "tool-1"
    assert tool["mcp"]["serverName"] == "live"
    assert tool["mcp"]["toolName"] == "echo"
    assert tool["mcp"]["progress"] == 1
    assert tool["mcp"]["total"] == 2
    assert tool["mcp"]["message"] == "halfway"


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
    assert tool_input["cmd"] == {
        "type": "str",
        "length": len(event.tool_input["cmd"]),
        "fingerprint": fingerprint_text(event.tool_input["cmd"]),
    }


@pytest.mark.asyncio
async def test_permission_request_tool_input_redacts_nested_secret_fields() -> None:
    queue = FakeEventQueue()
    event = PermissionRequestEvent(
        tool_name="bash",
        tool_input={
            "product": "ros",
            "action": "CreateStack",
            "customerEmail": "alice@example.com",
            "params": {
                "StackName": "demo",
                "customer-prod-123": "tenant-id",
                "AccessKeySecret": "secret-value",
                "private_key": "private-secret",
                "Signature": "signature-secret",
            },
        },
        tool_use_id="tool-1",
    )

    await publish_stream_event(queue, task_id="task-1", context_id="ctx-1", event=event)

    dumped = dump(queue.events[0])
    tool_input = dumped["metadata"]["iac_code"]["permission"]["toolInput"]
    for forbidden in (
        "secret-value",
        "private-secret",
        "signature-secret",
        "AccessKeySecret",
        "private_key",
        "Signature",
        "customerEmail",
        "customer-prod-123",
    ):
        assert forbidden not in str(tool_input)
    assert tool_input[fingerprint_text("customerEmail")] == {
        "type": "str",
        "length": len("alice@example.com"),
        "fingerprint": fingerprint_text("alice@example.com"),
    }
    assert tool_input["params"][fingerprint_text("StackName")] == {
        "type": "str",
        "length": 4,
        "fingerprint": fingerprint_text("demo"),
    }
    assert tool_input["params"][fingerprint_text("customer-prod-123")] == {
        "type": "str",
        "length": len("tenant-id"),
        "fingerprint": fingerprint_text("tenant-id"),
    }
    assert tool_input["params"][fingerprint_text("AccessKeySecret")] == {"redacted": True}
    assert tool_input["params"][fingerprint_text("private_key")] == {"redacted": True}
    assert tool_input["params"][fingerprint_text("Signature")] == {"redacted": True}


@pytest.mark.asyncio
async def test_aliyun_permission_metadata_uses_summary_for_sensitive_safe_fields() -> None:
    queue = FakeEventQueue()
    pem = "-----BEGIN PRIVATE KEY-----\nprivate-body\n-----END PRIVATE KEY-----"
    event = PermissionRequestEvent(
        tool_name="aliyun_api",
        tool_input={
            "product": "ros",
            "action": "CreateStack",
            "params": {"TemplateBody": pem, "StackName": "demo"},
        },
        tool_use_id="tool-1",
    )

    await publish_stream_event(queue, task_id="task-1", context_id="ctx-1", event=event)

    dumped = dump(queue.events[0])
    permission = dumped["metadata"]["iac_code"]["permission"]
    rendered = str(permission)
    assert "toolInput" not in permission
    assert permission["inputSummary"]["tool_name"] == "aliyun_api"
    assert permission["inputSummary"]["params_fields"] == sorted(
        [fingerprint_text("StackName"), fingerprint_text("TemplateBody")]
    )
    assert permission["inputSummary"]["params_field_count"] == 2
    assert "StackName" not in rendered
    assert "TemplateBody" not in rendered
    assert "private-body" not in rendered
    assert "BEGIN PRIVATE KEY" not in rendered


@pytest.mark.asyncio
async def test_permission_request_tool_input_redacts_sensitive_keys() -> None:
    queue = FakeEventQueue()
    event = PermissionRequestEvent(
        tool_name="bash",
        tool_input={
            "cmd": "pwd",
            "api_key": "plain-api-key",
            "nested": [{"accessKeySecret": "nested-access-key-secret"}],
            "headers": {"Authorization": "Bearer auth-token-secret"},
        },
        tool_use_id="tool-1",
    )

    await publish_stream_event(queue, task_id="task-1", context_id="ctx-1", event=event)

    dumped = dump(queue.events[0])
    tool_input = dumped["metadata"]["iac_code"]["permission"]["toolInput"]
    assert tool_input["cmd"] == {
        "type": "str",
        "length": len("pwd"),
        "fingerprint": fingerprint_text("pwd"),
    }
    assert tool_input[fingerprint_text("api_key")] == {"redacted": True}
    assert tool_input[fingerprint_text("nested")] == {"type": "array", "length": 1}
    assert tool_input["headers"][fingerprint_text("Authorization")] == {"redacted": True}
    rendered = str(tool_input)
    assert "api_key" not in rendered
    assert "nested" not in rendered
    assert "Authorization" not in rendered
    assert "plain-api-key" not in rendered
    assert "nested-access-key-secret" not in rendered
    assert "auth-token-secret" not in rendered


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
async def test_wrapped_permission_request_uses_inner_event() -> None:
    queue = FakeEventQueue()
    future = pending_future()
    inner = PermissionRequestEvent(
        tool_name="bash",
        tool_input={"cmd": "pwd"},
        tool_use_id="tool-1",
        response_future=future,
    )
    event = SubPipelineStreamEvent(sub_pipeline_id="candidate-1", candidate_index=0, inner=inner)
    seen: list[PermissionRequestEvent] = []

    async def approve(request: PermissionRequestEvent) -> bool:
        seen.append(request)
        return True

    await publish_stream_event(
        queue,
        task_id="task-1",
        context_id="ctx-1",
        event=event,
        permission_resolver=approve,
    )

    assert seen == [inner]
    assert future.done()
    assert future.result() is True
    dumped = dump(queue.events[0])
    permission = dumped["metadata"]["iac_code"]["permission"]
    assert permission["autoApproved"] is True
    assert permission["toolName"] == "bash"
    assert permission["toolUseId"] == "tool-1"
    assert permission["toolInput"]["cmd"] == {
        "type": "str",
        "length": 3,
        "fingerprint": fingerprint_text("pwd"),
    }


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
    assert "partialJson" not in dumped[1]["metadata"]["iac_code"]["tool"]
    assert dumped[1]["metadata"]["iac_code"]["tool"]["partialJsonLength"] == 6
    assert dumped[2]["metadata"]["iac_code"]["tool"]["status"] == "input_complete"
    assert dumped[2]["metadata"]["iac_code"]["tool"]["name"] == "bash"
    assert dumped[2]["metadata"]["iac_code"]["tool"]["inputSummary"] == {
        "tool_name": "bash",
        "fields": {"cmd": {"type": "str"}},
    }
    assert "input" not in dumped[2]["metadata"]["iac_code"]["tool"]
    assert dumped[3]["metadata"]["iac_code"]["tool"]["status"] == "completed"


@pytest.mark.asyncio
async def test_tool_input_delta_metadata_omits_raw_partial_json() -> None:
    queue = FakeEventQueue()

    await publish_stream_event(
        queue,
        task_id="task-1",
        context_id="ctx-1",
        event=ToolInputDeltaEvent(tool_use_id="tool-1", partial_json='ature":"signature-secret"'),
    )

    dumped = dump(queue.events[0])
    tool_metadata = dumped["metadata"]["iac_code"]["tool"]
    rendered = str(tool_metadata)
    assert tool_metadata["status"] == "input_delta"
    assert tool_metadata["partialJsonLength"] == len('ature":"signature-secret"')
    assert "partialJson" not in tool_metadata
    assert "signature-secret" not in rendered


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
    tool = dumped["metadata"]["iac_code"]["tool"]
    rendered = str(tool)
    assert "input" not in tool
    assert tool["inputSummary"] == {"tool_name": "bash", "fields": {"cmd": {"type": "str"}}}
    assert "sk-live-secret" not in rendered
    assert "Authorization: Bearer" not in rendered
    assert "/Users/alice" not in rendered


@pytest.mark.asyncio
async def test_tool_use_input_metadata_redacts_structured_secret_fields() -> None:
    queue = FakeEventQueue()

    await publish_stream_event(
        queue,
        task_id="task-1",
        context_id="ctx-1",
        event=ToolUseEndEvent(
            tool_use_id="tool-1",
            name="bash",
            input={
                "product": "ros",
                "action": "CreateStack",
                "params": {
                    "StackName": "demo",
                    "AccessKeySecret": "secret-value",
                    "Signature": "signature-secret",
                    "private_key": "private-secret",
                    "Authorization": "Bearer bearer-secret",
                    "apiKey": "api-secret",
                },
            },
        ),
    )

    dumped = dump(queue.events[0])
    tool = dumped["metadata"]["iac_code"]["tool"]
    tool_input = tool["inputSummary"]["fields"]
    for forbidden in (
        "secret-value",
        "signature-secret",
        "private-secret",
        "bearer-secret",
        "api-secret",
        "AccessKeySecret",
        "Signature",
        "private_key",
        "Authorization",
        "apiKey",
    ):
        assert forbidden not in str(tool_input)
    assert "input" not in tool
    assert tool_input["params"]["fields"][fingerprint_text("StackName")] == {"type": "str"}
    assert tool_input["params"]["fields"][fingerprint_text("AccessKeySecret")] == {"type": "str"}
    assert tool_input["params"]["fields"][fingerprint_text("Signature")] == {"type": "str"}
    assert tool_input["params"]["fields"][fingerprint_text("private_key")] == {"type": "str"}
    assert tool_input["params"]["fields"][fingerprint_text("Authorization")] == {"type": "str"}
    assert tool_input["params"]["fields"][fingerprint_text("apiKey")] == {"type": "str"}


@pytest.mark.asyncio
async def test_aliyun_tool_use_input_metadata_uses_summary_for_sensitive_safe_fields() -> None:
    queue = FakeEventQueue()
    pem = "-----BEGIN PRIVATE KEY-----\nprivate-body\n-----END PRIVATE KEY-----"

    await publish_stream_event(
        queue,
        task_id="task-1",
        context_id="ctx-1",
        event=ToolUseEndEvent(
            tool_use_id="tool-1",
            name="aliyun_api",
            input={
                "product": "ros",
                "action": "CreateStack",
                "params": {"TemplateBody": pem, "StackName": "demo"},
            },
        ),
    )

    dumped = dump(queue.events[0])
    tool = dumped["metadata"]["iac_code"]["tool"]
    rendered = str(tool)
    assert "input" not in tool
    assert tool["inputSummary"]["tool_name"] == "aliyun_api"
    assert tool["inputSummary"]["params_fields"] == sorted(
        [fingerprint_text("StackName"), fingerprint_text("TemplateBody")]
    )
    assert tool["inputSummary"]["params_field_count"] == 2
    assert "StackName" not in rendered
    assert "TemplateBody" not in rendered
    assert "private-body" not in rendered
    assert "BEGIN PRIVATE KEY" not in rendered


@pytest.mark.asyncio
async def test_tool_use_input_metadata_redacts_sensitive_keys() -> None:
    queue = FakeEventQueue()

    await publish_stream_event(
        queue,
        task_id="task-1",
        context_id="ctx-1",
        event=ToolUseEndEvent(
            tool_use_id="tool-1",
            name="bash",
            input={
                "cmd": "pwd",
                "apiKey": "plain-api-key",
                "env": {"ALIBABA_CLOUD_ACCESS_KEY_SECRET": "ak-secret"},
                "headers": [{"x-acs-security-token": "sts-token"}],
            },
        ),
    )

    dumped = dump(queue.events[0])
    tool = dumped["metadata"]["iac_code"]["tool"]
    assert "input" not in tool
    summary = tool["inputSummary"]
    fields = summary["fields"]
    assert summary["tool_name"] == "bash"
    assert fields["cmd"] == {"type": "str"}
    assert fields[fingerprint_text("apiKey")] == {"type": "str"}
    assert fields[fingerprint_text("env")]["type"] == "object"
    assert fields["headers"] == {"type": "array", "length": 1}
    rendered = str(tool)
    assert "apiKey" not in rendered
    assert "env" not in rendered
    assert "plain-api-key" not in rendered
    assert "ak-secret" not in rendered
    assert "sts-token" not in rendered


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
    tool = dumped["metadata"]["iac_code"]["tool"]
    rendered = str(tool)
    assert "input" not in tool
    assert tool["inputSummary"]["fields"]["cmd"] == {"type": "str"}
    assert tool["inputSummary"]["fields"][fingerprint_text("note")] == {"type": "str"}
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
    tool = dumped["metadata"]["iac_code"]["tool"]
    rendered = str(tool)
    assert "input" not in tool
    assert tool["inputSummary"]["fields"]["cmd"] == {"type": "str"}
    assert "%2FUsers" not in rendered
    assert ".iac-code" not in rendered


@pytest.mark.asyncio
async def test_tool_use_input_summary_fingerprints_business_field_names() -> None:
    queue = FakeEventQueue()

    await publish_stream_event(
        queue,
        task_id="task-1",
        context_id="ctx-1",
        event=ToolUseEndEvent(
            tool_use_id="tool-1",
            name="bash",
            input={
                "cmd": "git status",
                "customerEmail": "alice@example.com",
                "customer-prod-123": "tenant-id",
            },
        ),
    )

    tool = dump(queue.events[0])["metadata"]["iac_code"]["tool"]
    rendered = str(tool)
    assert "input" not in tool
    assert tool["inputSummary"]["tool_name"] == "bash"
    fields = tool["inputSummary"]["fields"]
    assert fields["cmd"] == {"type": "str"}
    assert fields[fingerprint_text("customerEmail")] == {"type": "str"}
    assert fields[fingerprint_text("customer-prod-123")] == {"type": "str"}
    assert "customerEmail" not in rendered
    assert "customer-prod-123" not in rendered
    assert "alice@example.com" not in rendered
    assert "tenant-id" not in rendered


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
