use iac_code_exec::HeadlessRunResult;
use iac_code_protocol::json;
use iac_code_protocol::json::JsonValue;
use iac_code_protocol::message::Conversation;
use iac_code_protocol::{StreamEvent, SubAgentToolEvent, ToolResultEvent, ToolUseEndEvent};

use crate::headless_subagent::{
    sub_agent_error_detail, sub_agent_tool_events_from_child_events, truncate_sub_agent_output,
};

#[test]
fn child_agent_events_convert_to_parent_subagent_tool_progress() {
    let events = sub_agent_tool_events_from_child_events(&[
        StreamEvent::ToolUseEnd(ToolUseEndEvent {
            tool_use_id: "child-tool-1".into(),
            name: "read_file".into(),
            input: json::object([("path", json::string("main.rs"))]),
        }),
        StreamEvent::ToolResult(ToolResultEvent {
            tool_use_id: "child-tool-1".into(),
            tool_name: "read_file".into(),
            result: "ok".into(),
            is_error: false,
        }),
        StreamEvent::ToolResult(ToolResultEvent {
            tool_use_id: "child-tool-2".into(),
            tool_name: "bash".into(),
            result: "denied".into(),
            is_error: true,
        }),
    ]);

    assert_eq!(
        events,
        vec![
            SubAgentToolEvent {
                parent_tool_use_id: String::new(),
                child_tool_name: "read_file".into(),
                child_tool_input: json::object([("path", json::string("main.rs"))]),
                is_done: false,
                is_error: false,
            },
            SubAgentToolEvent {
                parent_tool_use_id: String::new(),
                child_tool_name: "read_file".into(),
                child_tool_input: json::object([("path", json::string("main.rs"))]),
                is_done: true,
                is_error: false,
            },
            SubAgentToolEvent {
                parent_tool_use_id: String::new(),
                child_tool_name: "bash".into(),
                child_tool_input: json::object(Vec::<(&str, JsonValue)>::new()),
                is_done: true,
                is_error: true,
            },
        ]
    );
}

#[test]
fn sub_agent_output_is_truncated_to_python_word_limit() {
    let output = (0..501)
        .map(|index| format!("word{index}"))
        .collect::<Vec<_>>()
        .join(" ");

    let truncated = truncate_sub_agent_output(&output);

    assert!(truncated.ends_with("\n\n[... truncated to 500 words]"));
    let visible = truncated
        .split_once("\n\n")
        .expect("truncation marker should be separated")
        .0;
    assert_eq!(visible.split_whitespace().count(), 500);
    assert!(visible.contains("word499"));
    assert!(!visible.contains("word500"));
}

#[test]
fn sub_agent_max_turns_exit_code_is_not_treated_as_tool_error() {
    let result = HeadlessRunResult {
        exit_code: iac_code_exec::EXIT_MAX_TURNS,
        stdout: "partial answer\n".into(),
        stderr: String::new(),
        conversation: Conversation::default(),
        token_count: 0,
        events: Vec::new(),
    };

    assert_eq!(sub_agent_error_detail(&result), None);
}

#[test]
fn sub_agent_error_exit_code_prefers_stderr_detail() {
    let result = HeadlessRunResult {
        exit_code: iac_code_exec::EXIT_ERROR,
        stdout: "partial answer\n".into(),
        stderr: "provider failed\n".into(),
        conversation: Conversation::default(),
        token_count: 0,
        events: Vec::new(),
    };

    assert_eq!(
        sub_agent_error_detail(&result),
        Some("provider failed".to_owned())
    );
}
