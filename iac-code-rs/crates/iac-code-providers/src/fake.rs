use iac_code_protocol::json;
use iac_code_protocol::message::{AgentMessageContent, Conversation};
use iac_code_protocol::provider::ToolDefinition;
use iac_code_protocol::{
    MessageEndEvent, MessageStartEvent, StreamEvent, TextDeltaEvent, ToolUseEndEvent,
    ToolUseStartEvent, Usage,
};

use crate::EventProvider;

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum FakeScenario {
    Text,
    MaxTurns,
    ConversationLength,
    WriteFileAutoApprove,
}

const WRITE_FILE_TOOL_USE_ID: &str = "call_write_auto";

#[derive(Clone, Debug, PartialEq, Eq)]
pub struct FakeProvider {
    scenario: FakeScenario,
}

impl FakeProvider {
    pub fn new(scenario: FakeScenario) -> Self {
        Self { scenario }
    }

    pub fn requires_tool_executor(&self) -> bool {
        matches!(self.scenario, FakeScenario::WriteFileAutoApprove)
    }

    pub fn stream(&self, prompt: &str, max_turns: u32) -> Vec<StreamEvent> {
        let mut conversation = Conversation::default();
        conversation.add_user_message(AgentMessageContent::Text(prompt.to_owned()));
        self.stream_events(&conversation, "", &[], max_turns)
    }
}

impl EventProvider for FakeProvider {
    fn stream_events(
        &self,
        conversation: &Conversation,
        _system: &str,
        _tools: &[ToolDefinition],
        max_turns: u32,
    ) -> Vec<StreamEvent> {
        if self.scenario == FakeScenario::MaxTurns || max_turns == 0 {
            return vec![StreamEvent::MessageEnd(MessageEndEvent {
                stop_reason: "max_turns".into(),
                usage: Usage::default(),
            })];
        }

        if self.scenario == FakeScenario::WriteFileAutoApprove {
            return write_file_auto_approve_events(conversation);
        }

        let prompt = conversation
            .messages
            .iter()
            .rev()
            .find(|message| message.role == "user")
            .map(|message| message.get_text())
            .unwrap_or_default();

        let text = if self.scenario == FakeScenario::ConversationLength {
            format!(
                "conversation messages: {}; last prompt: {}",
                conversation.messages.len(),
                prompt
            )
        } else {
            format!("fixture response: {}", prompt)
        };

        vec![
            StreamEvent::MessageStart(MessageStartEvent {
                message_id: "msg_1".into(),
            }),
            StreamEvent::TextDelta(TextDeltaEvent { text }),
            StreamEvent::MessageEnd(MessageEndEvent {
                stop_reason: "end_turn".into(),
                usage: Usage {
                    input_tokens: 1,
                    output_tokens: 2,
                    cache_creation_input_tokens: 3,
                    cache_read_input_tokens: 4,
                },
            }),
        ]
    }
}

fn write_file_auto_approve_events(conversation: &Conversation) -> Vec<StreamEvent> {
    if has_write_file_tool_result(conversation) {
        return vec![
            StreamEvent::MessageStart(MessageStartEvent {
                message_id: "msg_2".into(),
            }),
            StreamEvent::TextDelta(TextDeltaEvent {
                text: "permission auto approve complete".into(),
            }),
            StreamEvent::MessageEnd(MessageEndEvent {
                stop_reason: "end_turn".into(),
                usage: Usage {
                    input_tokens: 5,
                    output_tokens: 6,
                    cache_creation_input_tokens: 0,
                    cache_read_input_tokens: 0,
                },
            }),
        ];
    }

    vec![
        StreamEvent::MessageStart(MessageStartEvent {
            message_id: "msg_1".into(),
        }),
        StreamEvent::ToolUseStart(ToolUseStartEvent {
            tool_use_id: WRITE_FILE_TOOL_USE_ID.into(),
            name: "write_file".into(),
        }),
        StreamEvent::ToolUseEnd(ToolUseEndEvent {
            tool_use_id: WRITE_FILE_TOOL_USE_ID.into(),
            name: "write_file".into(),
            input: json::object([
                ("content", json::string("beta\n")),
                ("path", json::string("auto-approved.txt")),
            ]),
        }),
        StreamEvent::MessageEnd(MessageEndEvent {
            stop_reason: "tool_calls".into(),
            usage: Usage {
                input_tokens: 3,
                output_tokens: 4,
                cache_creation_input_tokens: 0,
                cache_read_input_tokens: 0,
            },
        }),
    ]
}

fn has_write_file_tool_result(conversation: &Conversation) -> bool {
    conversation.messages.iter().any(|message| {
        message
            .get_tool_result_blocks()
            .into_iter()
            .any(|result| result.tool_use_id == WRITE_FILE_TOOL_USE_ID)
    })
}
