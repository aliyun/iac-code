use super::AgentLoop;
use iac_code_protocol::message::{AgentMessageContent, Conversation};
use iac_code_protocol::StreamEvent;
use iac_code_providers::EventProvider;
use iac_code_tools::ToolExecutor;

const COMPACTION_SYSTEM_PROMPT: &str =
    "You are a helpful assistant that summarizes conversations concisely.";
const PRESERVE_RECENT_TURNS: usize = 3;

#[derive(Clone, Debug, PartialEq, Eq)]
pub struct CompactResult {
    pub status: String,
    pub original_tokens: u64,
    pub compacted_tokens: u64,
    pub preserve_recent_turns: u32,
}

impl CompactResult {
    fn new(status: &str) -> Self {
        Self {
            status: status.to_owned(),
            original_tokens: 0,
            compacted_tokens: 0,
            preserve_recent_turns: PRESERVE_RECENT_TURNS as u32,
        }
    }

    fn success(original_tokens: u64, compacted_tokens: u64) -> Self {
        Self {
            status: "success".to_owned(),
            original_tokens,
            compacted_tokens,
            preserve_recent_turns: PRESERVE_RECENT_TURNS as u32,
        }
    }
}

impl<P, T> AgentLoop<P, T>
where
    P: EventProvider,
    T: ToolExecutor,
{
    pub fn compact(&mut self) -> CompactResult {
        if self.context_manager.messages().is_empty() {
            return CompactResult::new("empty");
        }

        let compaction_prompt = self.context_manager.build_compaction_prompt();
        if compaction_prompt.is_empty() {
            return CompactResult::new("too_short");
        }

        if self.context_manager.total_tokens() < self.context_manager.compact_buffer() {
            return CompactResult::new("too_small");
        }

        let mut compaction_conversation = Conversation::default();
        compaction_conversation.add_user_message(AgentMessageContent::Text(compaction_prompt));
        let mut summary = String::new();
        let mut message_ended = false;
        for event in
            self.provider
                .stream_events(&compaction_conversation, COMPACTION_SYSTEM_PROMPT, &[], 1)
        {
            match event {
                StreamEvent::TextDelta(delta) => summary.push_str(&delta.text),
                StreamEvent::Tombstone(_) => summary.clear(),
                StreamEvent::MessageEnd(_) => message_ended = true,
                _ => {}
            }
        }

        if !message_ended || summary.trim().is_empty() {
            return CompactResult::new("failed");
        }

        let (original_tokens, compacted_tokens) = self.context_manager.apply_compaction(&summary);

        CompactResult::success(original_tokens, compacted_tokens)
    }
}
