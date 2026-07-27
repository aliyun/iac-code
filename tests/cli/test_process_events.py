from __future__ import annotations

import json

from iac_code.cli.process_events import ProcessErrorMapper, ProcessEventSerializer, ProcessSerializedEvent
from iac_code.providers.manager import ProviderNotConfiguredError
from iac_code.types.stream_events import ErrorEvent, TextDeltaEvent, ToolInputDeltaEvent, ToolResultEvent


def test_process_event_serializer_reuses_stream_json_shape_for_text_delta() -> None:
    serializer = ProcessEventSerializer()

    assert serializer.serialize(TextDeltaEvent(text="hello")) == {"type": "text_delta", "text": "hello"}


def test_process_event_serializer_hides_partial_tool_input() -> None:
    serializer = ProcessEventSerializer()

    assert serializer.serialize(ToolInputDeltaEvent(tool_use_id="tool-1", partial_json='{"secret":"value"}')) == {
        "type": "tool_input_delta",
        "tool_use_id": "tool-1",
        "partial_json_length": 18,
    }


def test_process_event_serializer_sanitizes_tool_result() -> None:
    serializer = ProcessEventSerializer()

    event = ToolResultEvent(
        tool_use_id="tool-1",
        tool_name="bash",
        result="failed with token sk-live12345 at /Users/alice/.iac-code/settings.yml",
        is_error=True,
    )

    rendered = json.dumps(serializer.serialize(event), ensure_ascii=False)
    assert "sk-live12345" not in rendered
    assert "/Users/alice" not in rendered
    assert "settings.yml" not in rendered


def test_process_event_serializer_normal_aliyun_result_reuses_stream_json_projection() -> None:
    serializer = ProcessEventSerializer()

    payload = serializer.serialize(
        ToolResultEvent(
            tool_use_id="tool-aliyun",
            tool_name="aliyun_api",
            result='{"Instances":[]}',
            metadata={
                "aliyun_http": {"contract_version": "aliyun_body_v1", "status": 200},
                "ros_validation": {"valid": True},
            },
        )
    )

    assert payload["result"] == '{"Instances":[]}'
    assert payload["metadata"] == {"ros_validation": {"valid": True}}
    assert "aliyun_http" not in json.dumps(payload)


def test_process_event_serializer_pipeline_preserves_translated_public_envelope() -> None:
    serializer = ProcessEventSerializer()
    envelope = {
        "type": "pipeline_event",
        "eventType": "tool_result",
        "data": {
            "toolName": "aliyun_api",
            "toolUseId": "tool-aliyun",
            "isError": False,
            "result": '{"Instances":[]}',
        },
    }

    assert serializer.serialize(ProcessSerializedEvent(envelope)) == envelope
    assert "aliyun_http" not in json.dumps(envelope)


def test_process_error_mapper_returns_stable_provider_code() -> None:
    mapper = ProcessErrorMapper()
    payload = mapper.from_exception(ProviderNotConfiguredError("DashScope provider not configured"))

    assert payload.code == "provider_not_configured"
    assert payload.retryable is False
    assert "DashScope" in payload.message


def test_process_error_mapper_preserves_stream_error_event_retryability() -> None:
    mapper = ProcessErrorMapper()
    payload = mapper.from_event(ErrorEvent(error="temporary", is_retryable=True, error_id="err-1"))

    assert payload.code == "stream_error"
    assert payload.retryable is True
    assert payload.error_id == "err-1"
