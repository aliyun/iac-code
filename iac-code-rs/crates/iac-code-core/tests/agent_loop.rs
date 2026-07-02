use std::cell::Cell;

use iac_code_core::AgentLoop;
use iac_code_protocol::message::{
    AgentContentBlock, AgentMessageContent, Conversation, ImageBlock, TextBlock,
};
use iac_code_protocol::provider::ToolDefinition;
use iac_code_protocol::{MessageEndEvent, MessageStartEvent, StreamEvent, TextDeltaEvent, Usage};
use iac_code_providers::EventProvider;

#[test]
fn agent_loop_records_user_prompt_and_forwards_provider_events() {
    let provider = RecordingProvider::default();
    let mut loop_runner = AgentLoop::new(provider, 100);

    let events = loop_runner.run_streaming("hello");

    assert_eq!(
        events,
        vec![
            StreamEvent::MessageStart(MessageStartEvent {
                message_id: "msg_1".into(),
            }),
            StreamEvent::TextDelta(TextDeltaEvent {
                text: "core response".into(),
            }),
            StreamEvent::MessageEnd(MessageEndEvent {
                stop_reason: "end_turn".into(),
                usage: Usage {
                    input_tokens: 1,
                    output_tokens: 2,
                    cache_creation_input_tokens: 0,
                    cache_read_input_tokens: 0,
                },
            }),
        ]
    );
    assert_eq!(loop_runner.provider().calls.get(), 1);
    assert_eq!(loop_runner.conversation().messages.len(), 2);
    assert_eq!(loop_runner.conversation().messages[0].role, "user");
    assert_eq!(
        loop_runner.conversation().messages[0].content,
        AgentMessageContent::Text("hello".into())
    );
    assert!(loop_runner.conversation().messages[0].token_count > 0);
    assert_eq!(loop_runner.conversation().messages[1].role, "assistant");
    assert_eq!(
        loop_runner.conversation().messages[1].content,
        AgentMessageContent::Blocks(vec![AgentContentBlock::Text(TextBlock {
            text: "core response".into(),
        })])
    );
    assert!(loop_runner.conversation().messages[1].token_count > 0);

    let usage = loop_runner.context_usage();
    assert_eq!(usage.message_count, 2);
    assert!(usage.user_message_tokens > 0);
    assert!(usage.assistant_message_tokens > 0);
    assert_eq!(usage.tool_result_tokens, 0);
    assert_eq!(usage.total_tokens, loop_runner.total_tokens());
}

#[test]
fn agent_loop_records_structured_user_content() {
    let provider = RecordingProvider::default();
    let mut loop_runner = AgentLoop::new(provider, 100);
    let content = AgentMessageContent::Blocks(vec![
        AgentContentBlock::Text(TextBlock {
            text: "describe this".into(),
        }),
        AgentContentBlock::Image(ImageBlock {
            media_type: "image/png".into(),
            data: "base64-image".into(),
        }),
    ]);

    let events = loop_runner.run_streaming_content(content.clone());

    assert!(matches!(events.last(), Some(StreamEvent::MessageEnd(_))));
    assert_eq!(loop_runner.provider().calls.get(), 1);
    assert_eq!(loop_runner.conversation().messages.len(), 2);
    assert_eq!(loop_runner.conversation().messages[0].role, "user");
    assert_eq!(loop_runner.conversation().messages[0].content, content);
}

#[test]
fn agent_loop_max_turns_zero_returns_synthetic_event_without_provider_call() {
    let provider = RecordingProvider::default();
    let mut loop_runner = AgentLoop::new(provider, 0);

    let events = loop_runner.run_streaming("hello");

    assert_eq!(
        events,
        vec![StreamEvent::MessageEnd(MessageEndEvent {
            stop_reason: "max_turns".into(),
            usage: Usage::default(),
        })]
    );
    assert_eq!(loop_runner.provider().calls.get(), 0);
    assert_eq!(loop_runner.conversation().messages.len(), 1);
}

#[test]
fn agent_loop_updates_context_window_when_model_changes() {
    let provider = RecordingProvider::default();
    let mut loop_runner = AgentLoop::new(provider, 100);

    assert_eq!(loop_runner.context_usage().context_window, 128_000);
    loop_runner.set_model("qwen3.6-plus");

    assert_eq!(loop_runner.context_usage().context_window, 131_072);
}

#[test]
fn agent_loop_synthesizes_stream_error_when_provider_omits_message_end() {
    let provider = MissingMessageEndProvider::default();
    let mut loop_runner = AgentLoop::new(provider, 100);

    let events = loop_runner.run_streaming("hello");

    assert_eq!(
        events,
        vec![
            StreamEvent::MessageStart(MessageStartEvent {
                message_id: "msg_1".into(),
            }),
            StreamEvent::TextDelta(TextDeltaEvent {
                text: "partial response".into(),
            }),
            StreamEvent::MessageEnd(MessageEndEvent {
                stop_reason: "stream_error".into(),
                usage: Usage::default(),
            }),
        ]
    );
    assert_eq!(loop_runner.provider().calls.get(), 1);
    assert_eq!(loop_runner.conversation().messages.len(), 1);
}

#[test]
fn agent_loop_compact_replaces_old_messages_with_summary_and_preserves_recent_turns() {
    let provider = SummaryProvider::default();
    let mut loop_runner = AgentLoop::new(provider, 100);
    let mut conversation = Conversation::default();
    for index in 1..=4 {
        conversation.add_user_message(AgentMessageContent::Text(format!(
            "old prompt {index} {}",
            "alpha ".repeat(8_000)
        )));
        conversation.add_assistant_message(AgentMessageContent::Text(format!(
            "old answer {index} {}",
            "beta ".repeat(8_000)
        )));
    }
    loop_runner.set_conversation(conversation);

    let result = loop_runner.compact();

    assert_eq!(result.status, "success");
    let prompts = loop_runner.provider().compaction_prompts.borrow();
    assert_eq!(prompts.len(), 1);
    let compaction_prompt = &prompts[0];
    assert!(compaction_prompt.contains("USER: old prompt 1"));
    assert!(compaction_prompt.contains("ASSISTANT: old answer 1"));
    assert!(!compaction_prompt.contains("old prompt 2"));

    let messages = &loop_runner.conversation().messages;
    assert_eq!(messages.len(), 7);
    assert_eq!(messages[0].role, "user");
    assert_eq!(
        messages[0].content,
        AgentMessageContent::Text("[Conversation Summary]\nsummary of old prompt 1".into())
    );
    assert_eq!(
        messages[1].content,
        AgentMessageContent::Text(format!("old prompt 2 {}", "alpha ".repeat(8_000)))
    );
    assert_eq!(
        messages[6].content,
        AgentMessageContent::Text(format!("old answer 4 {}", "beta ".repeat(8_000)))
    );
}

#[test]
fn agent_loop_compact_skips_provider_when_below_compaction_buffer() {
    let provider = SummaryProvider::default();
    let mut loop_runner = AgentLoop::new(provider, 100);
    let mut conversation = Conversation::default();
    for index in 1..=4 {
        conversation.add_user_message(AgentMessageContent::Text(format!("short prompt {index}")));
        conversation
            .add_assistant_message(AgentMessageContent::Text(format!("short answer {index}")));
    }
    loop_runner.set_conversation(conversation);

    let result = loop_runner.compact();

    assert_eq!(result.status, "too_small");
    assert_eq!(loop_runner.provider().compaction_prompts.borrow().len(), 0);
    assert_eq!(loop_runner.conversation().messages.len(), 8);
}

#[derive(Default)]
struct RecordingProvider {
    calls: Cell<u32>,
}

impl EventProvider for RecordingProvider {
    fn stream_events(
        &self,
        _conversation: &Conversation,
        _system: &str,
        _tools: &[ToolDefinition],
        _max_turns: u32,
    ) -> Vec<StreamEvent> {
        self.calls.set(self.calls.get() + 1);
        vec![
            StreamEvent::MessageStart(MessageStartEvent {
                message_id: "msg_1".into(),
            }),
            StreamEvent::TextDelta(TextDeltaEvent {
                text: "core response".into(),
            }),
            StreamEvent::MessageEnd(MessageEndEvent {
                stop_reason: "end_turn".into(),
                usage: Usage {
                    input_tokens: 1,
                    output_tokens: 2,
                    cache_creation_input_tokens: 0,
                    cache_read_input_tokens: 0,
                },
            }),
        ]
    }
}

#[derive(Default)]
struct MissingMessageEndProvider {
    calls: Cell<u32>,
}

impl EventProvider for MissingMessageEndProvider {
    fn stream_events(
        &self,
        _conversation: &Conversation,
        _system: &str,
        _tools: &[ToolDefinition],
        _max_turns: u32,
    ) -> Vec<StreamEvent> {
        self.calls.set(self.calls.get() + 1);
        vec![
            StreamEvent::MessageStart(MessageStartEvent {
                message_id: "msg_1".into(),
            }),
            StreamEvent::TextDelta(TextDeltaEvent {
                text: "partial response".into(),
            }),
        ]
    }
}

#[derive(Default)]
struct SummaryProvider {
    compaction_prompts: std::cell::RefCell<Vec<String>>,
}

impl EventProvider for SummaryProvider {
    fn stream_events(
        &self,
        conversation: &Conversation,
        system: &str,
        _tools: &[ToolDefinition],
        _max_turns: u32,
    ) -> Vec<StreamEvent> {
        assert_eq!(
            system,
            "You are a helpful assistant that summarizes conversations concisely."
        );
        let prompt = conversation
            .messages
            .last()
            .map(|message| message.get_text())
            .unwrap_or_default();
        self.compaction_prompts.borrow_mut().push(prompt);
        vec![
            StreamEvent::MessageStart(MessageStartEvent {
                message_id: "summary_msg".into(),
            }),
            StreamEvent::TextDelta(TextDeltaEvent {
                text: "summary of old prompt 1".into(),
            }),
            StreamEvent::MessageEnd(MessageEndEvent {
                stop_reason: "end_turn".into(),
                usage: Usage::default(),
            }),
        ]
    }
}
