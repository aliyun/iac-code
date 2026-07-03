use iac_code_a2a::artifacts::A2AArtifactStore;
use iac_code_a2a::events::{
    publish_stream_event, truncate_metadata, A2AEvent, A2AExposureType, PublishOptions,
    ERROR_TEXT_MAX_CHARS, METADATA_MAX_CHARS,
};
use iac_code_protocol::json::{self, JsonValue};
use iac_code_protocol::{
    ErrorEvent, MessageEndEvent, PermissionRequestEvent, StreamEvent, TextDeltaEvent,
    ThinkingDeltaEvent, ToolInputDeltaEvent, ToolResultEvent, ToolUseEndEvent, ToolUseStartEvent,
    Usage,
};

#[test]
fn text_delta_publishes_agent_message_and_empty_text_is_ignored() {
    let result = publish_stream_event(
        "task-1",
        "ctx-1",
        &StreamEvent::TextDelta(TextDeltaEvent {
            text: "hello".into(),
        }),
        PublishOptions::default(),
        None,
    );

    assert_eq!(result.text, Some("hello".into()));
    assert_eq!(result.events.len(), 1);
    let update = status_update_event(&result.events[0]);
    assert_eq!(update.state, "TASK_STATE_WORKING");
    assert_eq!(update.message.as_ref().expect("message").role, "ROLE_AGENT");
    assert_eq!(update.message.as_ref().expect("message").text, "hello");

    let empty = publish_stream_event(
        "task-1",
        "ctx-1",
        &StreamEvent::TextDelta(TextDeltaEvent {
            text: String::new(),
        }),
        PublishOptions::default(),
        None,
    );
    assert!(empty.events.is_empty());
    assert_eq!(empty.text, None);
}

#[test]
fn thinking_delta_is_ignored_unless_raw_thinking_exposure_is_enabled() {
    let hidden = publish_stream_event(
        "task-1",
        "ctx-1",
        &StreamEvent::ThinkingDelta(ThinkingDeltaEvent {
            text: "hidden".into(),
        }),
        PublishOptions::default(),
        None,
    );
    assert!(hidden.events.is_empty());

    let visible = publish_stream_event(
        "task-1",
        "ctx-1",
        &StreamEvent::ThinkingDelta(ThinkingDeltaEvent {
            text: "visible".into(),
        }),
        PublishOptions::default().with_exposure([A2AExposureType::RawThinking]),
        None,
    );
    let update = status_update_event(&visible.events[0]);
    assert_eq!(
        update.metadata,
        json::object([(
            "iac_code",
            json::object([(
                "thinking",
                json::object([
                    ("type", json::string("raw_thinking")),
                    ("text", json::string("visible")),
                ]),
            )]),
        )])
    );
}

#[test]
fn tool_trace_events_publish_metadata_by_default() {
    let events = [
        StreamEvent::ToolUseStart(ToolUseStartEvent {
            tool_use_id: "tool-1".into(),
            name: "bash".into(),
        }),
        StreamEvent::ToolInputDelta(ToolInputDeltaEvent {
            tool_use_id: "tool-1".into(),
            partial_json: "{\"cmd\"".into(),
        }),
        StreamEvent::ToolUseEnd(ToolUseEndEvent {
            tool_use_id: "tool-1".into(),
            name: "bash".into(),
            input: json::object([("cmd", json::string("pwd"))]),
        }),
        StreamEvent::ToolResult(ToolResultEvent {
            tool_use_id: "tool-1".into(),
            tool_name: "bash".into(),
            result: "ok".into(),
            is_error: false,
        }),
    ];

    let statuses = events
        .iter()
        .flat_map(|event| {
            publish_stream_event("task-1", "ctx-1", event, PublishOptions::default(), None).events
        })
        .map(|event| status_update_event(&event).metadata.clone())
        .collect::<Vec<_>>();

    assert_eq!(tool_status(&statuses[0]), Some("started"));
    assert_eq!(tool_status(&statuses[1]), Some("input_delta"));
    assert_eq!(tool_status(&statuses[2]), Some("input_complete"));
    assert_eq!(tool_status(&statuses[3]), Some("completed"));
}

#[test]
fn tool_trace_events_are_suppressed_when_exposure_is_disabled() {
    let result = publish_stream_event(
        "task-1",
        "ctx-1",
        &StreamEvent::ToolUseStart(ToolUseStartEvent {
            tool_use_id: "tool-1".into(),
            name: "bash".into(),
        }),
        PublishOptions::default().with_exposure([]),
        None,
    );
    assert!(result.events.is_empty());
}

#[test]
fn tool_result_artifacts_publish_artifact_update_and_sanitized_tool_metadata() {
    let root = temp_root("events-artifact");
    let store = A2AArtifactStore::new(root.join("artifacts"));
    let payload = json::object([
        ("content", json::string("tool completed")),
        (
            "artifact",
            json::object([
                ("filename", json::string("report.txt")),
                ("mediaType", json::string("text/plain")),
                ("content", json::string("hello artifact")),
            ]),
        ),
    ])
    .to_compact_json();

    let result = publish_stream_event(
        "task-1",
        "ctx-1",
        &StreamEvent::ToolResult(ToolResultEvent {
            tool_use_id: "tool-1".into(),
            tool_name: "render".into(),
            result: payload,
            is_error: false,
        }),
        PublishOptions::default().with_artifact_store(store.clone()),
        None,
    );

    assert_eq!(result.events.len(), 2);
    let A2AEvent::TaskArtifactUpdate(artifact_update) = &result.events[0] else {
        panic!("expected artifact update");
    };
    assert_eq!(artifact_update.task_id, "task-1");
    assert_eq!(artifact_update.context_id, "ctx-1");
    assert_eq!(artifact_update.artifact.name, "report.txt");
    assert_eq!(artifact_update.artifact.parts.len(), 1);
    assert_eq!(artifact_update.artifact.parts[0].filename, "report.txt");
    assert_eq!(artifact_update.artifact.parts[0].media_type, "text/plain");
    assert_eq!(
        std::fs::read_to_string(
            store
                .path_for(&artifact_update.artifact.artifact_id)
                .expect("artifact path")
        )
        .expect("artifact content"),
        "hello artifact"
    );
    assert_eq!(
        artifact_update.artifact.metadata,
        artifact_update.artifact.parts[0].metadata
    );

    let A2AEvent::TaskStatusUpdate(update) = &result.events[1] else {
        panic!("expected status update");
    };
    let tool = tool_metadata(&update.metadata).expect("tool metadata");
    assert_eq!(json_string_map(tool, "status"), Some("completed"));
    assert_eq!(json_string_map(tool, "toolUseId"), Some("tool-1"));
    let JsonValue::Object(result_metadata) = tool.get("result").expect("tool result metadata")
    else {
        panic!("expected object result metadata");
    };
    let JsonValue::Object(artifact_metadata) = result_metadata
        .get("artifact")
        .expect("sanitized artifact metadata")
    else {
        panic!("expected artifact metadata object");
    };
    assert!(!artifact_metadata.contains_key("content"));
    assert_eq!(
        json_string_map(artifact_metadata, "filename"),
        Some("report.txt")
    );
    let JsonValue::Object(saved_artifact_metadata) =
        tool.get("artifact").expect("saved artifact metadata")
    else {
        panic!("expected saved artifact metadata");
    };
    assert_eq!(
        json_string_map(saved_artifact_metadata, "artifactId"),
        Some(artifact_update.artifact.artifact_id.as_str())
    );

    std::fs::remove_dir_all(root).ok();
}

#[test]
fn tool_result_artifacts_publish_even_when_tool_trace_is_disabled() {
    let root = temp_root("events-artifact-no-trace");
    let store = A2AArtifactStore::new(root.join("artifacts"));
    let result = publish_stream_event(
        "task-1",
        "ctx-1",
        &StreamEvent::ToolResult(ToolResultEvent {
            tool_use_id: "tool-1".into(),
            tool_name: "render".into(),
            result: json::object([(
                "artifact",
                json::object([
                    ("filename", json::string("data.bin")),
                    ("media_type", json::string("application/octet-stream")),
                    ("base64", json::string("AAFiYXNlNjQ=")),
                ]),
            )])
            .to_compact_json(),
            is_error: false,
        }),
        PublishOptions::default()
            .with_exposure([])
            .with_artifact_store(store),
        None,
    );

    assert_eq!(result.events.len(), 1);
    let A2AEvent::TaskArtifactUpdate(update) = &result.events[0] else {
        panic!("expected artifact update");
    };
    assert_eq!(update.artifact.name, "data.bin");
    assert_eq!(
        json_number_map(&update.artifact.metadata, "byteSize"),
        Some("8")
    );

    std::fs::remove_dir_all(root).ok();
}

#[test]
fn permission_request_uses_default_auto_approve_or_resolver_and_truncates_metadata() {
    let long_value = "x".repeat(METADATA_MAX_CHARS + 100);
    let event = StreamEvent::PermissionRequest(PermissionRequestEvent {
        tool_name: "bash".into(),
        tool_input: json::object([("cmd", json::string(long_value))]),
        tool_use_id: "tool-1".into(),
        permission_result: None,
    });

    let denied = publish_stream_event("task-1", "ctx-1", &event, PublishOptions::default(), None);
    assert_eq!(denied.permission_decision, Some(false));
    let update = status_update_event(&denied.events[0]);
    assert_eq!(permission_auto_approved(&update.metadata), Some(false));
    assert_eq!(
        permission_tool_input_cmd_len(&update.metadata),
        Some(METADATA_MAX_CHARS)
    );

    let mut seen = Vec::new();
    let approved = publish_stream_event(
        "task-1",
        "ctx-1",
        &event,
        PublishOptions::default(),
        Some(&mut |request| {
            seen.push(request.tool_use_id.clone());
            true
        }),
    );
    assert_eq!(seen, vec!["tool-1"]);
    assert_eq!(approved.permission_decision, Some(true));
}

#[test]
fn message_end_publishes_usage_metadata() {
    let result = publish_stream_event(
        "task-1",
        "ctx-1",
        &StreamEvent::MessageEnd(MessageEndEvent {
            stop_reason: "end_turn".into(),
            usage: Usage {
                input_tokens: 2,
                output_tokens: 3,
                cache_creation_input_tokens: 0,
                cache_read_input_tokens: 0,
            },
        }),
        PublishOptions::default(),
        None,
    );

    let update = status_update_event(&result.events[0]);
    assert_eq!(
        update.metadata,
        json::object([(
            "iac_code",
            json::object([(
                "usage",
                json::object([
                    ("inputTokens", json::number(2)),
                    ("outputTokens", json::number(3)),
                    ("totalTokens", json::number(5)),
                ]),
            )]),
        )])
    );
}

#[test]
fn error_events_map_retryable_and_nonretryable_states() {
    let retryable = publish_stream_event(
        "task-1",
        "ctx-1",
        &StreamEvent::Error(ErrorEvent {
            error: "should not leak".into(),
            is_retryable: true,
        }),
        PublishOptions::default(),
        None,
    );
    let retryable_update = status_update_event(&retryable.events[0]);
    assert_eq!(retryable_update.state, "TASK_STATE_INPUT_REQUIRED");
    assert_eq!(
        retryable_update.message.as_ref().expect("message").text,
        "A temporary error occurred. Please retry."
    );

    let long_error = "X".repeat(ERROR_TEXT_MAX_CHARS + 500);
    let failed = publish_stream_event(
        "task-1",
        "ctx-1",
        &StreamEvent::Error(ErrorEvent {
            error: long_error,
            is_retryable: false,
        }),
        PublishOptions::default(),
        None,
    );
    let failed_update = status_update_event(&failed.events[0]);
    assert_eq!(failed_update.state, "TASK_STATE_FAILED");
    assert_eq!(
        failed_update.message.as_ref().expect("message").text.len(),
        ERROR_TEXT_MAX_CHARS
    );
}

#[test]
fn truncate_metadata_limits_nested_depth_and_string_length() {
    let mut value = json::string("leaf");
    for _ in 0..80 {
        value = json::object([("next", value)]);
    }
    let truncated = truncate_metadata(&value);

    let mut current = &truncated;
    for _ in 0..32 {
        let JsonValue::Object(object) = current else {
            panic!("expected object");
        };
        current = object.get("next").expect("next");
    }
    assert_eq!(current, &json::string("[truncated-depth]"));

    assert_eq!(
        truncate_metadata(&json::string("x".repeat(METADATA_MAX_CHARS + 1))),
        json::string("x".repeat(METADATA_MAX_CHARS))
    );
}

fn tool_status(metadata: &JsonValue) -> Option<&str> {
    let tool = tool_metadata(metadata)?;
    json_string_map(tool, "status")
}

fn status_update_event(event: &A2AEvent) -> &iac_code_a2a::events::TaskStatusUpdate {
    match event {
        A2AEvent::TaskStatusUpdate(update) => update,
        A2AEvent::TaskArtifactUpdate(_) => panic!("expected task status update"),
    }
}

fn tool_metadata(metadata: &JsonValue) -> Option<&std::collections::BTreeMap<String, JsonValue>> {
    let JsonValue::Object(root) = metadata else {
        return None;
    };
    let JsonValue::Object(iac) = root.get("iac_code")? else {
        return None;
    };
    let JsonValue::Object(tool) = iac.get("tool")? else {
        return None;
    };
    Some(tool)
}

fn json_string_map<'a>(
    value: &'a std::collections::BTreeMap<String, JsonValue>,
    key: &str,
) -> Option<&'a str> {
    let JsonValue::String(value) = value.get(key)? else {
        return None;
    };
    Some(value)
}

fn json_number_map<'a>(value: &'a JsonValue, key: &str) -> Option<&'a str> {
    let JsonValue::Object(value) = value else {
        return None;
    };
    let JsonValue::Number(value) = value.get(key)? else {
        return None;
    };
    Some(value)
}

fn permission_auto_approved(metadata: &JsonValue) -> Option<bool> {
    let permission = permission_metadata(metadata)?;
    let JsonValue::Bool(value) = permission.get("autoApproved")? else {
        return None;
    };
    Some(*value)
}

fn permission_tool_input_cmd_len(metadata: &JsonValue) -> Option<usize> {
    let permission = permission_metadata(metadata)?;
    let JsonValue::Object(input) = permission.get("toolInput")? else {
        return None;
    };
    let JsonValue::String(cmd) = input.get("cmd")? else {
        return None;
    };
    Some(cmd.len())
}

fn permission_metadata(
    metadata: &JsonValue,
) -> Option<&std::collections::BTreeMap<String, JsonValue>> {
    let JsonValue::Object(root) = metadata else {
        return None;
    };
    let JsonValue::Object(iac) = root.get("iac_code")? else {
        return None;
    };
    let JsonValue::Object(permission) = iac.get("permission")? else {
        return None;
    };
    Some(permission)
}

fn temp_root(name: &str) -> std::path::PathBuf {
    let nanos = std::time::SystemTime::now()
        .duration_since(std::time::UNIX_EPOCH)
        .expect("clock should be after unix epoch")
        .as_nanos();
    std::env::temp_dir().join(format!("iac-code-rs-a2a-events-{name}-{nanos}"))
}
