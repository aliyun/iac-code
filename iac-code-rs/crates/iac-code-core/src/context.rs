use iac_code_protocol::message::{
    AgentContentBlock, AgentMessage, AgentMessageContent, Conversation, ToolResultBlock,
};
use iac_code_protocol::provider::ToolDefinition;

mod config;
mod token;

pub use config::{context_window_config, ContextWindowConfig};
pub use token::{TokenBudget, TokenCounter};

#[derive(Clone, Debug, PartialEq)]
pub struct ContextUsage {
    pub system_prompt_tokens: u64,
    pub tool_definition_tokens: u64,
    pub user_message_tokens: u64,
    pub assistant_message_tokens: u64,
    pub tool_result_tokens: u64,
    pub total_tokens: u64,
    pub context_window: u64,
    pub usage_percent: f64,
    pub message_count: usize,
}

#[derive(Clone, Debug)]
pub struct ContextManager {
    system_prompt: String,
    conversation: Conversation,
    model: String,
    token_counter: TokenCounter,
    config: ContextWindowConfig,
    system_prompt_tokens: u64,
    tool_definitions: Vec<ToolDefinition>,
    tool_definition_tokens: u64,
}

impl ContextManager {
    pub fn new(system_prompt: impl Into<String>, model: impl Into<String>) -> Self {
        let system_prompt = system_prompt.into();
        let model = model.into();
        let token_counter = TokenCounter::new(&model);
        let system_prompt_tokens = token_counter.count_text(&system_prompt);
        Self {
            system_prompt,
            conversation: Conversation::default(),
            config: context_window_config(&model),
            model,
            token_counter,
            system_prompt_tokens,
            tool_definitions: Vec::new(),
            tool_definition_tokens: 0,
        }
    }

    pub fn system_prompt(&self) -> &str {
        &self.system_prompt
    }

    pub fn model(&self) -> &str {
        &self.model
    }

    pub fn preserve_recent_turns(&self) -> usize {
        self.config.preserve_recent_turns
    }

    pub fn context_window(&self) -> u64 {
        self.config.context_window
    }

    pub fn set_model(&mut self, model: impl Into<String>) {
        let model = model.into();
        if model == self.model {
            return;
        }
        self.model = model;
        self.token_counter = TokenCounter::new(&self.model);
        self.config = context_window_config(&self.model);
        self.system_prompt_tokens = self.token_counter.count_text(&self.system_prompt);
        self.tool_definition_tokens = self
            .token_counter
            .count_tool_definitions(&self.tool_definitions);
        let counter = self.token_counter.clone();
        for message in &mut self.conversation.messages {
            message.token_count = counter.count_message(message);
        }
    }

    pub fn set_system_prompt(&mut self, system_prompt: impl Into<String>) {
        let system_prompt = system_prompt.into();
        if system_prompt == self.system_prompt {
            return;
        }
        self.system_prompt = system_prompt;
        self.system_prompt_tokens = self.token_counter.count_text(&self.system_prompt);
    }

    pub fn set_tool_definitions(&mut self, tool_definitions: Vec<ToolDefinition>) {
        self.tool_definitions = tool_definitions;
        self.tool_definition_tokens = self
            .token_counter
            .count_tool_definitions(&self.tool_definitions);
    }

    pub fn add_user_message(&mut self, content: AgentMessageContent) -> &AgentMessage {
        self.conversation.add_user_message(content);
        self.refresh_last_message_tokens()
    }

    pub fn add_assistant_message(&mut self, content: AgentMessageContent) -> &AgentMessage {
        self.conversation.add_assistant_message(content);
        self.refresh_last_message_tokens()
    }

    pub fn add_tool_results(&mut self, tool_results: Vec<ToolResultBlock>) -> &AgentMessage {
        self.conversation
            .add_user_message(AgentMessageContent::Blocks(
                tool_results
                    .into_iter()
                    .map(AgentContentBlock::ToolResult)
                    .collect(),
            ));
        self.refresh_last_message_tokens()
    }

    pub fn load_messages(&mut self, messages: Vec<AgentMessage>) {
        self.conversation.messages.clear();
        let counter = self.token_counter.clone();
        self.conversation
            .messages
            .extend(messages.into_iter().map(|mut message| {
                if message.token_count == 0 {
                    message.token_count = counter.count_message(&message);
                }
                message
            }));
    }

    pub fn messages(&self) -> &[AgentMessage] {
        &self.conversation.messages
    }

    pub fn conversation(&self) -> &Conversation {
        &self.conversation
    }

    pub fn total_tokens(&self) -> u64 {
        self.system_prompt_tokens
            .saturating_add(self.tool_definition_tokens)
            .saturating_add(self.conversation_tokens())
    }

    pub fn usage(&self) -> ContextUsage {
        let mut user_message_tokens = 0_u64;
        let mut assistant_message_tokens = 0_u64;
        let mut tool_result_tokens = 0_u64;

        for message in &self.conversation.messages {
            match message.role.as_str() {
                "user" if has_tool_result(message) => {
                    tool_result_tokens = tool_result_tokens.saturating_add(message.token_count);
                }
                "user" => {
                    user_message_tokens = user_message_tokens.saturating_add(message.token_count);
                }
                "assistant" => {
                    assistant_message_tokens =
                        assistant_message_tokens.saturating_add(message.token_count);
                }
                _ => {}
            }
        }

        let total_tokens = self
            .system_prompt_tokens
            .saturating_add(self.tool_definition_tokens)
            .saturating_add(user_message_tokens)
            .saturating_add(assistant_message_tokens)
            .saturating_add(tool_result_tokens);
        ContextUsage {
            system_prompt_tokens: self.system_prompt_tokens,
            tool_definition_tokens: self.tool_definition_tokens,
            user_message_tokens,
            assistant_message_tokens,
            tool_result_tokens,
            total_tokens,
            context_window: self.config.context_window,
            usage_percent: if self.config.context_window == 0 {
                0.0
            } else {
                (total_tokens as f64 / self.config.context_window as f64) * 100.0
            },
            message_count: self.conversation.messages.len(),
        }
    }

    pub fn needs_compaction(&self) -> bool {
        let threshold = self.config.context_window as f64 * self.config.compact_threshold;
        self.total_tokens() as f64 > threshold
    }

    pub fn compact_buffer(&self) -> u64 {
        self.config.compact_buffer
    }

    pub fn build_compaction_prompt(&self) -> String {
        let (old_messages, _) = self.split_messages_for_compaction();
        build_compaction_prompt(old_messages)
    }

    pub fn apply_compaction(&mut self, summary: &str) -> (u64, u64) {
        let original_tokens = self.conversation_tokens();
        let (_, recent_messages) = self.split_messages_for_compaction();
        let mut summary_message = AgentMessage {
            role: "user".to_owned(),
            content: AgentMessageContent::Text(format!("[Conversation Summary]\n{summary}")),
            token_count: 0,
            elapsed_seconds: 0.0,
        };
        summary_message.token_count = self.token_counter.count_message(&summary_message);

        let mut compacted_messages = Vec::with_capacity(1 + recent_messages.len());
        compacted_messages.push(summary_message);
        compacted_messages.extend_from_slice(recent_messages);
        self.conversation.messages = compacted_messages;
        (original_tokens, self.conversation_tokens())
    }

    fn refresh_last_message_tokens(&mut self) -> &AgentMessage {
        let index = self
            .conversation
            .messages
            .len()
            .checked_sub(1)
            .expect("message was just added");
        let token_count = self
            .token_counter
            .count_message(&self.conversation.messages[index]);
        self.conversation.messages[index].token_count = token_count;
        &self.conversation.messages[index]
    }

    fn conversation_tokens(&self) -> u64 {
        self.conversation
            .messages
            .iter()
            .map(|message| message.token_count)
            .sum()
    }

    fn split_messages_for_compaction(&self) -> (&[AgentMessage], &[AgentMessage]) {
        let messages = &self.conversation.messages;
        let preserve_count = self.config.preserve_recent_turns * 2;
        if messages.len() <= preserve_count {
            return (&[], messages.as_slice());
        }
        let split_point = find_safe_compaction_split(messages, messages.len() - preserve_count);
        messages.split_at(split_point)
    }
}

fn has_tool_result(message: &AgentMessage) -> bool {
    let AgentMessageContent::Blocks(blocks) = &message.content else {
        return false;
    };
    blocks
        .iter()
        .any(|block| matches!(block, AgentContentBlock::ToolResult(_)))
}

fn find_safe_compaction_split(messages: &[AgentMessage], split_point: usize) -> usize {
    let mut split_point = split_point.min(messages.len());
    while split_point > 0 {
        let mut old_tool_uses = Vec::<(String, usize)>::new();
        let mut old_tool_results = Vec::<String>::new();

        for (index, message) in messages[..split_point].iter().enumerate() {
            for tool_use_id in tool_use_ids(message) {
                if !old_tool_uses
                    .iter()
                    .any(|(existing, _)| existing == &tool_use_id)
                {
                    old_tool_uses.push((tool_use_id, index));
                }
            }
            old_tool_results.extend(tool_result_ids(message));
        }

        let Some(first_unpaired_index) = old_tool_uses
            .iter()
            .filter_map(|(tool_use_id, index)| {
                (!old_tool_results.iter().any(|result| result == tool_use_id)).then_some(*index)
            })
            .min()
        else {
            return split_point;
        };
        split_point = first_unpaired_index;
    }
    split_point
}

fn tool_use_ids(message: &AgentMessage) -> Vec<String> {
    let AgentMessageContent::Blocks(blocks) = &message.content else {
        return Vec::new();
    };
    blocks
        .iter()
        .filter_map(|block| match block {
            AgentContentBlock::ToolUse(tool_use) => Some(tool_use.id.clone()),
            _ => None,
        })
        .collect()
}

fn tool_result_ids(message: &AgentMessage) -> Vec<String> {
    let AgentMessageContent::Blocks(blocks) = &message.content else {
        return Vec::new();
    };
    blocks
        .iter()
        .filter_map(|block| match block {
            AgentContentBlock::ToolResult(tool_result) => Some(tool_result.tool_use_id.clone()),
            _ => None,
        })
        .collect()
}

fn build_compaction_prompt(old_messages: &[AgentMessage]) -> String {
    let conversation_text = old_messages
        .iter()
        .filter_map(|message| {
            let text = message.get_text();
            (!text.is_empty()).then(|| format!("{}: {text}", message.role.to_uppercase()))
        })
        .collect::<Vec<_>>()
        .join("\n");
    if conversation_text.is_empty() {
        return String::new();
    }

    format!(
        "Please provide a concise summary of this conversation so far. \
Focus on:\n\
1. Key decisions made\n\
2. Important code changes or file modifications\n\
3. Current task status and next steps\n\
4. Any errors encountered and how they were resolved\n\n\
Keep the summary focused and actionable. Preserve specific file paths, \
function names, and technical details that are needed to continue the work.\n\n\
Conversation:\n{conversation_text}"
    )
}
