use std::cell::RefCell;
use std::fs;
use std::path::PathBuf;
use std::time::{SystemTime, UNIX_EPOCH};

use iac_code_core::AgentLoop;
use iac_code_protocol::message::{
    AgentContentBlock, AgentMessageContent, Conversation, TextBlock, ToolResultBlock, ToolUseBlock,
};
use iac_code_protocol::provider::ToolDefinition;
use iac_code_protocol::{
    json, MessageEndEvent, MessageStartEvent, StreamEvent, SubAgentToolEvent, ToolResultEvent,
    ToolUseEndEvent, ToolUseStartEvent, Usage,
};
use iac_code_providers::EventProvider;
use iac_code_tools::{ToolCallRequest, ToolExecutor, ToolResult};

#[test]
fn agent_loop_executes_tool_use_end_and_records_tool_result_message() {
    let provider = ToolCallingProvider;
    let tool_executor = RecordingToolExecutor::default();
    let mut loop_runner = AgentLoop::with_tool_executor(provider, 1, tool_executor);

    let events = loop_runner.run_streaming("read status");

    assert_eq!(
        events,
        vec![
            StreamEvent::MessageStart(MessageStartEvent {
                message_id: "msg_1".into(),
            }),
            StreamEvent::ToolUseStart(ToolUseStartEvent {
                tool_use_id: "toolu_1".into(),
                name: "echo_tool".into(),
            }),
            StreamEvent::ToolUseEnd(ToolUseEndEvent {
                tool_use_id: "toolu_1".into(),
                name: "echo_tool".into(),
                input: json::object([("text", json::string("hello"))]),
            }),
            StreamEvent::MessageEnd(MessageEndEvent {
                stop_reason: "tool_use".into(),
                usage: Usage {
                    input_tokens: 3,
                    output_tokens: 5,
                    cache_creation_input_tokens: 0,
                    cache_read_input_tokens: 0,
                },
            }),
            StreamEvent::ToolResult(ToolResultEvent {
                tool_use_id: "toolu_1".into(),
                tool_name: "echo_tool".into(),
                result: "echo: hello".into(),
                is_error: false,
            }),
            StreamEvent::MessageEnd(MessageEndEvent {
                stop_reason: "max_turns".into(),
                usage: Usage::default(),
            }),
        ]
    );

    let executor = loop_runner.tool_executor();
    assert_eq!(
        executor.requests.borrow().as_slice(),
        &[ToolCallRequest {
            tool_use_id: "toolu_1".into(),
            tool_name: "echo_tool".into(),
            input: json::object([("text", json::string("hello"))]),
        }]
    );
    assert_eq!(loop_runner.conversation().messages.len(), 3);
    assert!(loop_runner.conversation().messages[0].token_count > 0);
    assert_eq!(loop_runner.conversation().messages[1].role, "assistant");
    assert_eq!(
        loop_runner.conversation().messages[1].content,
        AgentMessageContent::Blocks(vec![AgentContentBlock::ToolUse(ToolUseBlock {
            id: "toolu_1".into(),
            name: "echo_tool".into(),
            input: json::object([("text", json::string("hello"))]),
        })])
    );
    assert!(loop_runner.conversation().messages[1].token_count > 0);
    assert_eq!(loop_runner.conversation().messages[2].role, "user");
    assert_eq!(
        loop_runner.conversation().messages[2].content,
        AgentMessageContent::Blocks(vec![AgentContentBlock::ToolResult(ToolResultBlock {
            tool_use_id: "toolu_1".into(),
            content: "echo: hello".into(),
            is_error: false,
        })])
    );
    assert!(loop_runner.conversation().messages[2].token_count > 0);

    let usage = loop_runner.context_usage();
    assert_eq!(usage.message_count, 3);
    assert!(usage.user_message_tokens > 0);
    assert!(usage.assistant_message_tokens > 0);
    assert!(usage.tool_result_tokens > 0);
    assert_eq!(usage.total_tokens, loop_runner.total_tokens());
}

#[test]
fn agent_loop_externalizes_large_tool_results_like_python() {
    let provider = ToolCallingProvider;
    let storage_dir = unique_temp_dir("iac-code-rs-large-tool-results");
    let full_result = "x".repeat(50_001);
    let expected_path = storage_dir.join("toolu_1.txt");
    let expected_preview = format!(
        "{}\n\n... [truncated \u{2014} full output (50001 chars) saved to {}]",
        "x".repeat(2_000),
        expected_path.display()
    );
    let tool_executor = LargeToolResultExecutor {
        content: full_result.clone(),
    };
    let mut loop_runner = AgentLoop::with_tool_executor(provider, 1, tool_executor)
        .with_result_storage_dir(&storage_dir);

    let events = loop_runner.run_streaming("read large output");

    assert!(events.iter().any(|event| {
        matches!(
            event,
            StreamEvent::ToolResult(ToolResultEvent { result, .. }) if result == &expected_preview
        )
    }));
    assert_eq!(
        fs::read_to_string(&expected_path).expect("externalized result should be readable"),
        full_result
    );
    assert_eq!(
        loop_runner.conversation().messages[2].content,
        AgentMessageContent::Blocks(vec![AgentContentBlock::ToolResult(ToolResultBlock {
            tool_use_id: "toolu_1".into(),
            content: expected_preview,
            is_error: false,
        })])
    );

    fs::remove_dir_all(storage_dir).ok();
}

#[test]
fn agent_loop_forwards_tool_result_stream_events_before_final_tool_result() {
    let provider = ToolCallingProvider;
    let tool_executor = ProgressToolExecutor;
    let mut loop_runner = AgentLoop::with_tool_executor(provider, 1, tool_executor);

    let events = loop_runner.run_streaming("delegate");

    assert_eq!(
        events,
        vec![
            StreamEvent::MessageStart(MessageStartEvent {
                message_id: "msg_1".into(),
            }),
            StreamEvent::ToolUseStart(ToolUseStartEvent {
                tool_use_id: "toolu_1".into(),
                name: "echo_tool".into(),
            }),
            StreamEvent::ToolUseEnd(ToolUseEndEvent {
                tool_use_id: "toolu_1".into(),
                name: "echo_tool".into(),
                input: json::object([("text", json::string("hello"))]),
            }),
            StreamEvent::MessageEnd(MessageEndEvent {
                stop_reason: "tool_use".into(),
                usage: Usage {
                    input_tokens: 3,
                    output_tokens: 5,
                    cache_creation_input_tokens: 0,
                    cache_read_input_tokens: 0,
                },
            }),
            StreamEvent::SubAgentTool(SubAgentToolEvent {
                parent_tool_use_id: "toolu_1".into(),
                child_tool_name: "read_file".into(),
                child_tool_input: json::object([("path", json::string("src/main.rs"))]),
                is_done: false,
                is_error: false,
            }),
            StreamEvent::SubAgentTool(SubAgentToolEvent {
                parent_tool_use_id: "toolu_1".into(),
                child_tool_name: "read_file".into(),
                child_tool_input: json::object([("path", json::string("src/main.rs"))]),
                is_done: true,
                is_error: false,
            }),
            StreamEvent::ToolResult(ToolResultEvent {
                tool_use_id: "toolu_1".into(),
                tool_name: "echo_tool".into(),
                result: "child done".into(),
                is_error: false,
            }),
            StreamEvent::MessageEnd(MessageEndEvent {
                stop_reason: "max_turns".into(),
                usage: Usage::default(),
            }),
        ]
    );
}

#[test]
fn agent_loop_executes_same_turn_tools_through_batch_executor() {
    let provider = TwoToolCallingProvider;
    let tool_executor = BatchRecordingToolExecutor::default();
    let mut loop_runner = AgentLoop::with_tool_executor(provider, 1, tool_executor);

    let events = loop_runner.run_streaming("run two tools");

    assert_eq!(
        events,
        vec![
            StreamEvent::MessageStart(MessageStartEvent {
                message_id: "msg_1".into(),
            }),
            StreamEvent::ToolUseStart(ToolUseStartEvent {
                tool_use_id: "toolu_1".into(),
                name: "first_tool".into(),
            }),
            StreamEvent::ToolUseEnd(ToolUseEndEvent {
                tool_use_id: "toolu_1".into(),
                name: "first_tool".into(),
                input: json::object([("value", json::string("first"))]),
            }),
            StreamEvent::ToolUseStart(ToolUseStartEvent {
                tool_use_id: "toolu_2".into(),
                name: "second_tool".into(),
            }),
            StreamEvent::ToolUseEnd(ToolUseEndEvent {
                tool_use_id: "toolu_2".into(),
                name: "second_tool".into(),
                input: json::object([("value", json::string("second"))]),
            }),
            StreamEvent::MessageEnd(MessageEndEvent {
                stop_reason: "tool_use".into(),
                usage: Usage::default(),
            }),
            StreamEvent::ToolResult(ToolResultEvent {
                tool_use_id: "toolu_1".into(),
                tool_name: "first_tool".into(),
                result: "batch result 1".into(),
                is_error: false,
            }),
            StreamEvent::ToolResult(ToolResultEvent {
                tool_use_id: "toolu_2".into(),
                tool_name: "second_tool".into(),
                result: "batch result 2".into(),
                is_error: false,
            }),
            StreamEvent::MessageEnd(MessageEndEvent {
                stop_reason: "max_turns".into(),
                usage: Usage::default(),
            }),
        ]
    );

    let batch_requests = loop_runner.tool_executor().batch_requests.borrow();
    assert_eq!(batch_requests.len(), 1);
    assert_eq!(
        batch_requests[0],
        vec![
            ToolCallRequest {
                tool_use_id: "toolu_1".into(),
                tool_name: "first_tool".into(),
                input: json::object([("value", json::string("first"))]),
            },
            ToolCallRequest {
                tool_use_id: "toolu_2".into(),
                tool_name: "second_tool".into(),
                input: json::object([("value", json::string("second"))]),
            },
        ]
    );
}

#[test]
fn agent_loop_continues_provider_after_tool_result_until_end_turn() {
    let provider = ContinuingProvider::default();
    let tool_executor = RecordingToolExecutor::default();
    let mut loop_runner = AgentLoop::with_tool_executor(provider, 2, tool_executor);

    let events = loop_runner.run_streaming("read status");

    assert_eq!(
        events,
        vec![
            StreamEvent::MessageStart(MessageStartEvent {
                message_id: "msg_1".into(),
            }),
            StreamEvent::ToolUseStart(ToolUseStartEvent {
                tool_use_id: "toolu_1".into(),
                name: "echo_tool".into(),
            }),
            StreamEvent::ToolUseEnd(ToolUseEndEvent {
                tool_use_id: "toolu_1".into(),
                name: "echo_tool".into(),
                input: json::object([("text", json::string("hello"))]),
            }),
            StreamEvent::MessageEnd(MessageEndEvent {
                stop_reason: "tool_use".into(),
                usage: Usage::default(),
            }),
            StreamEvent::ToolResult(ToolResultEvent {
                tool_use_id: "toolu_1".into(),
                tool_name: "echo_tool".into(),
                result: "echo: hello".into(),
                is_error: false,
            }),
            StreamEvent::MessageStart(MessageStartEvent {
                message_id: "msg_2".into(),
            }),
            StreamEvent::TextDelta(iac_code_protocol::TextDeltaEvent {
                text: "done after tool".into(),
            }),
            StreamEvent::MessageEnd(MessageEndEvent {
                stop_reason: "end_turn".into(),
                usage: Usage::default(),
            }),
        ]
    );

    let provider_calls = loop_runner.provider().conversations.borrow();
    assert_eq!(provider_calls.len(), 2);
    assert_eq!(
        provider_calls[0]
            .messages
            .iter()
            .map(|message| message.role.as_str())
            .collect::<Vec<_>>(),
        vec!["user"]
    );
    assert_eq!(
        provider_calls[1]
            .messages
            .iter()
            .map(|message| message.role.as_str())
            .collect::<Vec<_>>(),
        vec!["user", "assistant", "user"]
    );
    assert_eq!(
        provider_calls[1].messages[2].content,
        AgentMessageContent::Blocks(vec![AgentContentBlock::ToolResult(ToolResultBlock {
            tool_use_id: "toolu_1".into(),
            content: "echo: hello".into(),
            is_error: false,
        })])
    );
    drop(provider_calls);

    assert_eq!(loop_runner.conversation().messages.len(), 4);
    assert_eq!(
        loop_runner.conversation().messages[3].content,
        AgentMessageContent::Blocks(vec![AgentContentBlock::Text(TextBlock {
            text: "done after tool".into(),
        })])
    );
}

#[test]
fn agent_loop_stops_after_cancelled_tool_result_without_next_provider_turn() {
    let provider = ContinuingProvider::default();
    let tool_executor = CancelledToolExecutor;
    let mut loop_runner = AgentLoop::with_tool_executor(provider, 2, tool_executor);

    let events = loop_runner.run_streaming("read status");

    assert_eq!(
        events,
        vec![
            StreamEvent::MessageStart(MessageStartEvent {
                message_id: "msg_1".into(),
            }),
            StreamEvent::ToolUseStart(ToolUseStartEvent {
                tool_use_id: "toolu_1".into(),
                name: "echo_tool".into(),
            }),
            StreamEvent::ToolUseEnd(ToolUseEndEvent {
                tool_use_id: "toolu_1".into(),
                name: "echo_tool".into(),
                input: json::object([("text", json::string("hello"))]),
            }),
            StreamEvent::MessageEnd(MessageEndEvent {
                stop_reason: "tool_use".into(),
                usage: Usage::default(),
            }),
            StreamEvent::ToolResult(ToolResultEvent {
                tool_use_id: "toolu_1".into(),
                tool_name: "echo_tool".into(),
                result: "Tool execution cancelled.".into(),
                is_error: true,
            }),
            StreamEvent::MessageEnd(MessageEndEvent {
                stop_reason: "cancelled".into(),
                usage: Usage::default(),
            }),
        ]
    );

    let provider_calls = loop_runner.provider().conversations.borrow();
    assert_eq!(provider_calls.len(), 1);
    drop(provider_calls);

    assert_eq!(loop_runner.conversation().messages.len(), 3);
}

#[test]
fn agent_loop_injects_tool_result_new_messages_before_next_provider_turn() {
    let provider = ContinuingProvider::default();
    let tool_executor = NewMessageToolExecutor;
    let mut loop_runner = AgentLoop::with_tool_executor(provider, 2, tool_executor);

    let events = loop_runner.run_streaming("load skill");

    assert!(events.iter().any(|event| {
        matches!(
            event,
            StreamEvent::ToolResult(ToolResultEvent {
                result,
                is_error: false,
                ..
            }) if result == "Skill 'demo' loaded (inline)."
        )
    }));
    let provider_calls = loop_runner.provider().conversations.borrow();
    assert_eq!(provider_calls.len(), 2);
    assert_eq!(
        provider_calls[1]
            .messages
            .iter()
            .map(|message| message.role.as_str())
            .collect::<Vec<_>>(),
        vec!["user", "assistant", "user", "user"]
    );
    assert_eq!(
        provider_calls[1].messages[3].content,
        AgentMessageContent::Text("<skill-name>demo</skill-name>\n\nUse the demo skill.".into())
    );
}

#[test]
fn agent_loop_passes_tool_definitions_to_provider_turns() {
    let provider = ToolAwareProvider::default();
    let tool_executor = DefinitionToolExecutor;
    let mut loop_runner = AgentLoop::with_tool_executor(provider, 1, tool_executor);

    let events = loop_runner.run_streaming("use tools");

    assert_eq!(
        events,
        vec![
            StreamEvent::MessageStart(MessageStartEvent {
                message_id: "msg_1".into(),
            }),
            StreamEvent::MessageEnd(MessageEndEvent {
                stop_reason: "end_turn".into(),
                usage: Usage::default(),
            }),
        ]
    );
    assert_eq!(
        loop_runner.provider().tool_definitions.borrow().as_slice(),
        &[vec![ToolDefinition {
            name: "echo_tool".into(),
            description: "Echo text".into(),
            input_schema: json::object([
                ("type", json::string("object")),
                (
                    "properties",
                    json::object([("text", json::object([("type", json::string("string"))]))]),
                ),
            ]),
        }]]
    );
}

#[test]
fn agent_loop_ignores_tool_use_end_without_start_like_python() {
    let provider = OrphanToolEndProvider;
    let tool_executor = DefinitionToolExecutor;
    let mut loop_runner = AgentLoop::with_tool_executor(provider, 1, tool_executor);

    let events = loop_runner.run_streaming("orphan tool end");

    assert_eq!(
        events,
        vec![
            StreamEvent::MessageStart(MessageStartEvent {
                message_id: "msg_1".into(),
            }),
            StreamEvent::ToolUseEnd(ToolUseEndEvent {
                tool_use_id: "toolu_orphan".into(),
                name: "orphan_tool".into(),
                input: json::object([("value", json::string("ignored"))]),
            }),
            StreamEvent::MessageEnd(MessageEndEvent {
                stop_reason: "tool_use".into(),
                usage: Usage::default(),
            }),
        ]
    );
    assert_eq!(loop_runner.conversation().messages.len(), 1);
}

struct ToolCallingProvider;

impl EventProvider for ToolCallingProvider {
    fn stream_events(
        &self,
        _conversation: &Conversation,
        _system: &str,
        _tools: &[ToolDefinition],
        _max_turns: u32,
    ) -> Vec<StreamEvent> {
        vec![
            StreamEvent::MessageStart(MessageStartEvent {
                message_id: "msg_1".into(),
            }),
            StreamEvent::ToolUseStart(ToolUseStartEvent {
                tool_use_id: "toolu_1".into(),
                name: "echo_tool".into(),
            }),
            StreamEvent::ToolUseEnd(ToolUseEndEvent {
                tool_use_id: "toolu_1".into(),
                name: "echo_tool".into(),
                input: json::object([("text", json::string("hello"))]),
            }),
            StreamEvent::MessageEnd(MessageEndEvent {
                stop_reason: "tool_use".into(),
                usage: Usage {
                    input_tokens: 3,
                    output_tokens: 5,
                    cache_creation_input_tokens: 0,
                    cache_read_input_tokens: 0,
                },
            }),
        ]
    }
}

struct TwoToolCallingProvider;

impl EventProvider for TwoToolCallingProvider {
    fn stream_events(
        &self,
        _conversation: &Conversation,
        _system: &str,
        _tools: &[ToolDefinition],
        _max_turns: u32,
    ) -> Vec<StreamEvent> {
        vec![
            StreamEvent::MessageStart(MessageStartEvent {
                message_id: "msg_1".into(),
            }),
            StreamEvent::ToolUseStart(ToolUseStartEvent {
                tool_use_id: "toolu_1".into(),
                name: "first_tool".into(),
            }),
            StreamEvent::ToolUseEnd(ToolUseEndEvent {
                tool_use_id: "toolu_1".into(),
                name: "first_tool".into(),
                input: json::object([("value", json::string("first"))]),
            }),
            StreamEvent::ToolUseStart(ToolUseStartEvent {
                tool_use_id: "toolu_2".into(),
                name: "second_tool".into(),
            }),
            StreamEvent::ToolUseEnd(ToolUseEndEvent {
                tool_use_id: "toolu_2".into(),
                name: "second_tool".into(),
                input: json::object([("value", json::string("second"))]),
            }),
            StreamEvent::MessageEnd(MessageEndEvent {
                stop_reason: "tool_use".into(),
                usage: Usage::default(),
            }),
        ]
    }
}

#[derive(Default)]
struct ContinuingProvider {
    conversations: RefCell<Vec<Conversation>>,
}

impl EventProvider for ContinuingProvider {
    fn stream_events(
        &self,
        conversation: &Conversation,
        _system: &str,
        _tools: &[ToolDefinition],
        _max_turns: u32,
    ) -> Vec<StreamEvent> {
        let call_index = self.conversations.borrow().len();
        self.conversations.borrow_mut().push(conversation.clone());

        if call_index == 0 {
            return vec![
                StreamEvent::MessageStart(MessageStartEvent {
                    message_id: "msg_1".into(),
                }),
                StreamEvent::ToolUseStart(ToolUseStartEvent {
                    tool_use_id: "toolu_1".into(),
                    name: "echo_tool".into(),
                }),
                StreamEvent::ToolUseEnd(ToolUseEndEvent {
                    tool_use_id: "toolu_1".into(),
                    name: "echo_tool".into(),
                    input: json::object([("text", json::string("hello"))]),
                }),
                StreamEvent::MessageEnd(MessageEndEvent {
                    stop_reason: "tool_use".into(),
                    usage: Usage::default(),
                }),
            ];
        }

        vec![
            StreamEvent::MessageStart(MessageStartEvent {
                message_id: "msg_2".into(),
            }),
            StreamEvent::TextDelta(iac_code_protocol::TextDeltaEvent {
                text: "done after tool".into(),
            }),
            StreamEvent::MessageEnd(MessageEndEvent {
                stop_reason: "end_turn".into(),
                usage: Usage::default(),
            }),
        ]
    }
}

#[derive(Default)]
struct ToolAwareProvider {
    tool_definitions: RefCell<Vec<Vec<ToolDefinition>>>,
}

impl EventProvider for ToolAwareProvider {
    fn stream_events(
        &self,
        _conversation: &Conversation,
        _system: &str,
        tools: &[ToolDefinition],
        _max_turns: u32,
    ) -> Vec<StreamEvent> {
        self.tool_definitions.borrow_mut().push(tools.to_vec());
        vec![
            StreamEvent::MessageStart(MessageStartEvent {
                message_id: "msg_1".into(),
            }),
            StreamEvent::MessageEnd(MessageEndEvent {
                stop_reason: "end_turn".into(),
                usage: Usage::default(),
            }),
        ]
    }
}

struct OrphanToolEndProvider;

impl EventProvider for OrphanToolEndProvider {
    fn stream_events(
        &self,
        _conversation: &Conversation,
        _system: &str,
        _tools: &[ToolDefinition],
        _max_turns: u32,
    ) -> Vec<StreamEvent> {
        vec![
            StreamEvent::MessageStart(MessageStartEvent {
                message_id: "msg_1".into(),
            }),
            StreamEvent::ToolUseEnd(ToolUseEndEvent {
                tool_use_id: "toolu_orphan".into(),
                name: "orphan_tool".into(),
                input: json::object([("value", json::string("ignored"))]),
            }),
            StreamEvent::MessageEnd(MessageEndEvent {
                stop_reason: "tool_use".into(),
                usage: Usage::default(),
            }),
        ]
    }
}

#[derive(Default)]
struct RecordingToolExecutor {
    requests: RefCell<Vec<ToolCallRequest>>,
}

impl ToolExecutor for RecordingToolExecutor {
    fn execute(&self, request: ToolCallRequest) -> ToolResult {
        self.requests.borrow_mut().push(request);
        ToolResult::success("echo: hello")
    }
}

struct NewMessageToolExecutor;

impl ToolExecutor for NewMessageToolExecutor {
    fn execute(&self, _request: ToolCallRequest) -> ToolResult {
        ToolResult {
            content: "Skill 'demo' loaded (inline).".into(),
            is_error: false,
            cancelled: false,
            new_messages: vec![json::object([
                ("role", json::string("user")),
                (
                    "content",
                    json::string("<skill-name>demo</skill-name>\n\nUse the demo skill."),
                ),
            ])],
            context_modifier: None,
            stream_events: Vec::new(),
        }
    }
}

struct LargeToolResultExecutor {
    content: String,
}

impl ToolExecutor for LargeToolResultExecutor {
    fn execute(&self, _request: ToolCallRequest) -> ToolResult {
        ToolResult::success(self.content.clone())
    }
}

struct ProgressToolExecutor;

impl ToolExecutor for ProgressToolExecutor {
    fn execute(&self, _request: ToolCallRequest) -> ToolResult {
        ToolResult {
            content: "child done".into(),
            is_error: false,
            cancelled: false,
            new_messages: Vec::new(),
            context_modifier: None,
            stream_events: vec![
                StreamEvent::SubAgentTool(SubAgentToolEvent {
                    parent_tool_use_id: "toolu_1".into(),
                    child_tool_name: "read_file".into(),
                    child_tool_input: json::object([("path", json::string("src/main.rs"))]),
                    is_done: false,
                    is_error: false,
                }),
                StreamEvent::SubAgentTool(SubAgentToolEvent {
                    parent_tool_use_id: "toolu_1".into(),
                    child_tool_name: "read_file".into(),
                    child_tool_input: json::object([("path", json::string("src/main.rs"))]),
                    is_done: true,
                    is_error: false,
                }),
            ],
        }
    }
}

struct CancelledToolExecutor;

impl ToolExecutor for CancelledToolExecutor {
    fn execute(&self, _request: ToolCallRequest) -> ToolResult {
        ToolResult::cancelled("Tool execution cancelled.")
    }
}

#[derive(Default)]
struct BatchRecordingToolExecutor {
    batch_requests: RefCell<Vec<Vec<ToolCallRequest>>>,
}

impl ToolExecutor for BatchRecordingToolExecutor {
    fn execute(&self, request: ToolCallRequest) -> ToolResult {
        ToolResult::error(format!(
            "single execute should not be called for {}",
            request.tool_use_id
        ))
    }

    fn execute_batch(&self, requests: &[ToolCallRequest]) -> Vec<ToolResult> {
        self.batch_requests.borrow_mut().push(requests.to_vec());
        vec![
            ToolResult::success("batch result 1"),
            ToolResult::success("batch result 2"),
        ]
    }
}

struct DefinitionToolExecutor;

impl ToolExecutor for DefinitionToolExecutor {
    fn tool_definitions(&self) -> Vec<ToolDefinition> {
        vec![ToolDefinition {
            name: "echo_tool".into(),
            description: "Echo text".into(),
            input_schema: json::object([
                ("type", json::string("object")),
                (
                    "properties",
                    json::object([("text", json::object([("type", json::string("string"))]))]),
                ),
            ]),
        }]
    }

    fn execute(&self, _request: ToolCallRequest) -> ToolResult {
        ToolResult::error("unexpected execution")
    }
}

fn unique_temp_dir(prefix: &str) -> PathBuf {
    let nanos = SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .expect("system clock should be after unix epoch")
        .as_nanos();
    std::env::temp_dir().join(format!("{prefix}-{}-{nanos}", std::process::id()))
}
