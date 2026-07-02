use std::path::PathBuf;

use crate::{ContextManager, ContextUsage, ResultStorage};
use iac_code_protocol::message::{
    AgentContentBlock, AgentMessageContent, Conversation, TextBlock, ThinkingBlock, ToolUseBlock,
};
use iac_code_protocol::{MessageEndEvent, StreamEvent, ToolUseEndEvent, Usage};
use iac_code_providers::EventProvider;
use iac_code_tools::{NoToolExecutor, ToolExecutor};

mod compaction;
mod provider_turn;
mod tool_execution;

#[derive(Clone, Debug)]
pub struct AgentLoop<P, T = NoToolExecutor> {
    provider: P,
    tool_executor: T,
    max_turns: u32,
    system_prompt: String,
    context_manager: ContextManager,
    result_storage: Option<ResultStorage>,
}

impl<P> AgentLoop<P, NoToolExecutor>
where
    P: EventProvider,
{
    pub fn new(provider: P, max_turns: u32) -> Self {
        Self {
            provider,
            tool_executor: NoToolExecutor,
            max_turns,
            system_prompt: String::new(),
            context_manager: ContextManager::new("", ""),
            result_storage: None,
        }
    }
}

impl<P, T> AgentLoop<P, T>
where
    P: EventProvider,
    T: ToolExecutor,
{
    pub fn with_tool_executor(provider: P, max_turns: u32, tool_executor: T) -> Self {
        Self {
            provider,
            tool_executor,
            max_turns,
            system_prompt: String::new(),
            context_manager: ContextManager::new("", ""),
            result_storage: None,
        }
    }

    pub fn with_tool_executor_and_system_prompt(
        provider: P,
        max_turns: u32,
        tool_executor: T,
        system_prompt: impl Into<String>,
    ) -> Self {
        let system_prompt = system_prompt.into();
        Self {
            provider,
            tool_executor,
            max_turns,
            system_prompt: system_prompt.clone(),
            context_manager: ContextManager::new(system_prompt, ""),
            result_storage: None,
        }
    }

    pub fn with_result_storage_dir(mut self, storage_dir: impl Into<PathBuf>) -> Self {
        self.result_storage = Some(ResultStorage::new(storage_dir));
        self
    }

    pub fn run_streaming(&mut self, prompt: &str) -> Vec<StreamEvent> {
        self.run_streaming_content(AgentMessageContent::Text(prompt.to_owned()))
    }

    pub fn run_streaming_content(&mut self, content: AgentMessageContent) -> Vec<StreamEvent> {
        self.run_streaming_content_with_sink(content, &mut |_| {})
    }

    pub fn run_streaming_content_with_sink(
        &mut self,
        content: AgentMessageContent,
        sink: &mut dyn FnMut(&StreamEvent),
    ) -> Vec<StreamEvent> {
        self.context_manager.add_user_message(content);

        if self.max_turns == 0 {
            let event = StreamEvent::MessageEnd(MessageEndEvent {
                stop_reason: "max_turns".into(),
                usage: Usage::default(),
            });
            sink(&event);
            return vec![event];
        }

        let mut events = Vec::new();
        for _turn in 0..self.max_turns {
            let turn = self.run_provider_turn(sink);
            events.extend(turn.events);
            if !turn.message_ended {
                let event = StreamEvent::MessageEnd(MessageEndEvent {
                    stop_reason: "stream_error".into(),
                    usage: Usage::default(),
                });
                sink(&event);
                events.push(event);
                return events;
            }
            self.record_assistant_message(
                &turn.text_chunks,
                &turn.thinking_chunks,
                &turn.completed_tool_uses,
            );

            if turn.completed_tool_uses.is_empty() {
                return events;
            }

            let tool_execution = self.execute_tool_uses(&turn.completed_tool_uses);
            for event in &tool_execution.events {
                sink(event);
            }
            events.extend(tool_execution.events);
            if tool_execution.cancelled {
                let event = StreamEvent::MessageEnd(MessageEndEvent {
                    stop_reason: "cancelled".into(),
                    usage: Usage::default(),
                });
                sink(&event);
                events.push(event);
                return events;
            }
        }

        let event = StreamEvent::MessageEnd(MessageEndEvent {
            stop_reason: "max_turns".into(),
            usage: Usage::default(),
        });
        sink(&event);
        events.push(event);
        events
    }

    pub fn conversation(&self) -> &Conversation {
        self.context_manager.conversation()
    }

    pub fn set_conversation(&mut self, conversation: Conversation) {
        self.context_manager.load_messages(conversation.messages);
    }

    pub fn set_model(&mut self, model: impl Into<String>) {
        self.context_manager.set_model(model);
    }

    pub fn context_usage(&self) -> ContextUsage {
        self.context_manager.usage()
    }

    pub fn total_tokens(&self) -> u64 {
        self.context_manager.total_tokens()
    }

    pub fn provider(&self) -> &P {
        &self.provider
    }

    pub fn tool_executor(&self) -> &T {
        &self.tool_executor
    }

    fn record_assistant_message(
        &mut self,
        text_chunks: &[String],
        thinking_chunks: &[String],
        completed_tool_uses: &[ToolUseEndEvent],
    ) {
        let mut blocks = Vec::new();

        let thinking = thinking_chunks.join("");
        if !thinking.is_empty() {
            blocks.push(AgentContentBlock::Thinking(ThinkingBlock { thinking }));
        }

        let text = text_chunks.join("");
        if !text.is_empty() {
            blocks.push(AgentContentBlock::Text(TextBlock { text }));
        }

        blocks.extend(completed_tool_uses.iter().map(|tool_use| {
            AgentContentBlock::ToolUse(ToolUseBlock {
                id: tool_use.tool_use_id.clone(),
                name: tool_use.name.clone(),
                input: tool_use.input.clone(),
            })
        }));

        if !blocks.is_empty() {
            self.context_manager
                .add_assistant_message(AgentMessageContent::Blocks(blocks));
        }
    }
}
