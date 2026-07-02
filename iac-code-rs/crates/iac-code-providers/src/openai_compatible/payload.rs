mod arguments;
mod messages;
mod options;
mod system;
mod tools;

use iac_code_protocol::json::{self, JsonValue};
use iac_code_protocol::message::Conversation;
use iac_code_protocol::provider::ToolDefinition;

use super::OpenAiCompatibleProvider;
use messages::convert_message;
use system::{
    build_system_message, is_dashscope_explicit_cache_model, mark_last_user_message_cacheable,
};
use tools::convert_tools;

impl OpenAiCompatibleProvider {
    pub fn build_chat_payload(
        &self,
        conversation: &Conversation,
        system: &str,
        tools: &[ToolDefinition],
        max_tokens: u32,
        stream: bool,
    ) -> JsonValue {
        let mut entries = vec![
            ("model", json::string(&self.config.model)),
            ("messages", self.build_api_messages(conversation, system)),
            ("max_tokens", json::number(max_tokens)),
        ];
        if stream {
            entries.push(("stream", json::bool_value(true)));
            if self.config.supports_stream_options {
                entries.push((
                    "stream_options",
                    json::object([("include_usage", json::bool_value(true))]),
                ));
            }
        }
        if !tools.is_empty() {
            entries.push(("tools", convert_tools(tools)));
        }
        entries.extend(self.thinking_payload_entries());
        json::object(entries)
    }

    fn build_api_messages(&self, conversation: &Conversation, system: &str) -> JsonValue {
        let mut messages = Vec::new();
        if !system.is_empty() {
            messages.push(build_system_message(
                &self.config.provider_key,
                &self.config.model,
                system,
            ));
        }
        for message in &conversation.messages {
            messages.extend(convert_message(message));
        }
        if is_dashscope_explicit_cache_model(&self.config.provider_key, &self.config.model) {
            mark_last_user_message_cacheable(&mut messages);
        }
        json::array(messages)
    }
}
