use std::fs;
use std::path::PathBuf;

use iac_code_protocol::json;
use iac_code_protocol::{
    CompactionEvent, ErrorEvent, MessageEndEvent, MessageStartEvent, PermissionRequestEvent,
    PlanEvent, PlanStep, QueuedInputSubmittedEvent, StackInstancesProgressEvent,
    StackProgressEvent, StreamEvent, SubAgentToolEvent, TaskNotificationEvent, TextDeltaEvent,
    ThinkingDeltaEvent, ToJsonValue, TombstoneEvent, ToolInputDeltaEvent, ToolResultEvent,
    ToolUseEndEvent, ToolUseStartEvent, Usage,
};

#[test]
fn stream_events_match_python_fixtures() {
    let fixtures = fixture_lines();
    assert_eq!(
        fixtures.len(),
        18,
        "fixture should cover every Python StreamEvent variant"
    );

    let actual = vec![
        MessageStartEvent {
            message_id: "msg_1".into(),
        }
        .to_compact_json(),
        TextDeltaEvent {
            text: "hello".into(),
        }
        .to_compact_json(),
        ThinkingDeltaEvent {
            text: "reasoning".into(),
        }
        .to_compact_json(),
        ToolUseStartEvent {
            tool_use_id: "toolu_1".into(),
            name: "bash".into(),
        }
        .to_compact_json(),
        ToolInputDeltaEvent {
            tool_use_id: "toolu_1".into(),
            partial_json: "{\"command\"".into(),
        }
        .to_compact_json(),
        ToolUseEndEvent {
            tool_use_id: "toolu_1".into(),
            name: "bash".into(),
            input: json::object([
                ("command", json::string("ls")),
                ("timeout", json::number(1)),
            ]),
        }
        .to_compact_json(),
        MessageEndEvent {
            stop_reason: "end_turn".into(),
            usage: Usage {
                input_tokens: 10,
                output_tokens: 20,
                cache_creation_input_tokens: 3,
                cache_read_input_tokens: 4,
            },
        }
        .to_compact_json(),
        TombstoneEvent {
            message_id: "msg_old".into(),
        }
        .to_compact_json(),
        ErrorEvent {
            error: "network timeout".into(),
            is_retryable: true,
        }
        .to_compact_json(),
        ToolResultEvent {
            tool_use_id: "toolu_1".into(),
            tool_name: "bash".into(),
            result: "ok".into(),
            is_error: false,
        }
        .to_compact_json(),
        PermissionRequestEvent {
            tool_name: "bash".into(),
            tool_input: json::object([("command", json::string("rm -rf /tmp/example"))]),
            tool_use_id: "toolu_2".into(),
            permission_result: None,
        }
        .to_compact_json(),
        CompactionEvent {
            original_tokens: 1000,
            compacted_tokens: 250,
        }
        .to_compact_json(),
        TaskNotificationEvent {
            task_id: "task_1".into(),
            description: "background task".into(),
            status: "completed".into(),
            result: Some("done".into()),
            error: None,
        }
        .to_compact_json(),
        QueuedInputSubmittedEvent {
            text: "queued prompt".into(),
        }
        .to_compact_json(),
        SubAgentToolEvent {
            parent_tool_use_id: "parent_1".into(),
            child_tool_name: "read_file".into(),
            child_tool_input: json::object([("path", json::string("README.md"))]),
            is_done: true,
            is_error: false,
        }
        .to_compact_json(),
        StackProgressEvent {
            stack_id: "stack-1".into(),
            stack_name: "demo".into(),
            status: "CREATE_IN_PROGRESS".into(),
            progress_percentage: 42.5,
            resources: vec![json::object([
                ("logical_id", json::string("Vpc")),
                ("status", json::string("CREATE_COMPLETE")),
            ])],
            elapsed_seconds: 12,
        }
        .to_compact_json(),
        StackInstancesProgressEvent {
            stack_group_name: "group".into(),
            operation_id: "op-1".into(),
            status: "RUNNING".into(),
            progress_percentage: 60,
            instances: vec![json::object([
                ("account", json::string("123456789012")),
                ("region", json::string("cn-hangzhou")),
                ("status", json::string("CURRENT")),
            ])],
            elapsed_seconds: 30,
        }
        .to_compact_json(),
        PlanEvent {
            steps: vec![
                PlanStep {
                    content: "inspect".into(),
                    status: "completed".into(),
                    priority: "high".into(),
                },
                PlanStep {
                    content: "implement".into(),
                    status: "in_progress".into(),
                    priority: "medium".into(),
                },
            ],
        }
        .to_compact_json(),
    ];

    assert_eq!(actual, fixtures);
}

#[test]
fn stream_event_enum_serializes_selected_variant() {
    let event = StreamEvent::TextDelta(TextDeltaEvent {
        text: "hello".into(),
    });

    assert_eq!(
        event.to_compact_json(),
        r#"{"text":"hello","type":"text_delta"}"#
    );
}

fn fixture_lines() -> Vec<String> {
    let mut path = PathBuf::from(env!("CARGO_MANIFEST_DIR"));
    path.push("../../fixtures/compatibility/stream_events/events.jsonl");
    fs::read_to_string(path)
        .expect("stream event fixture should be readable")
        .lines()
        .map(str::to_owned)
        .collect()
}
