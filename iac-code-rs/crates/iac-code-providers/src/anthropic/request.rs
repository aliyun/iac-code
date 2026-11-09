use iac_code_protocol::json::{self, JsonValue};
use iac_code_protocol::message::Conversation;
use iac_code_protocol::provider::{NonStreamingResponse, ToolDefinition};
use iac_code_protocol::StreamEvent;

use super::config::{
    is_retryable_http_status, messages_url, thinking_payload, COMPLETE_MAX_RETRIES,
};
use super::errors::CompleteChatError;
use super::payload::{
    anthropic_model_alias, anthropic_thinking_budget, convert_message, convert_tools,
};
use super::response::{parse_non_streaming_response, stream_events_from_response};
use super::sse::{parse_sse_stream_events, stream_response_events_with_sink};
use super::{AnthropicProvider, StreamChatError};

impl AnthropicProvider {
    pub fn build_messages_payload(
        &self,
        conversation: &Conversation,
        system: &str,
        tools: &[ToolDefinition],
    ) -> Result<JsonValue, String> {
        self.build_messages_payload_with_stream(conversation, system, tools, false)
    }

    fn build_messages_payload_with_stream(
        &self,
        conversation: &Conversation,
        system: &str,
        tools: &[ToolDefinition],
        stream: bool,
    ) -> Result<JsonValue, String> {
        let (model, _) = anthropic_model_alias(&self.config.model);
        let mut entries = vec![
            ("model", json::string(model)),
            ("system", json::string(system)),
            ("messages", self.build_api_messages(conversation)),
        ];
        if !tools.is_empty() {
            entries.push(("tools", convert_tools(tools)));
        }
        if let Some(max_tokens) = self.required_max_tokens()? {
            if anthropic_thinking_budget(
                &self.config.provider_key,
                &self.config.model,
                self.config.effort.as_deref(),
            )
            .is_some_and(|budget| u64::from(budget) >= max_tokens)
            {
                return Err(
                    "Anthropic thinking_budget must be below the model's max_tokens capability"
                        .to_owned(),
                );
            }
            entries.push(("max_tokens", json::number(max_tokens)));
        }
        if let Some(thinking) = thinking_payload(&self.config) {
            entries.push(("thinking", thinking));
        }
        if stream {
            entries.push(("stream", json::bool_value(true)));
        }
        Ok(json::object(entries))
    }

    pub fn complete_chat(
        &self,
        conversation: &Conversation,
        system: &str,
        tools: &[ToolDefinition],
    ) -> Result<NonStreamingResponse, String> {
        let payload =
            self.build_messages_payload_with_stream(conversation, system, tools, false)?;
        let text = self.send_messages_request_with_retry(payload)?;
        parse_non_streaming_response(&text)
    }

    pub fn stream_chat(
        &self,
        conversation: &Conversation,
        system: &str,
        tools: &[ToolDefinition],
    ) -> Result<Vec<StreamEvent>, StreamChatError> {
        let payload = self
            .build_messages_payload_with_stream(conversation, system, tools, true)
            .map_err(StreamChatError::without_partial)?;
        let text = self
            .send_messages_request(payload)
            .map_err(StreamChatError::without_partial)?;
        if text.trim_start().starts_with('{') {
            return parse_non_streaming_response(&text)
                .map(stream_events_from_response)
                .map_err(StreamChatError::without_partial);
        }
        parse_sse_stream_events(&text)
    }

    pub fn stream_chat_with_sink(
        &self,
        conversation: &Conversation,
        system: &str,
        tools: &[ToolDefinition],
        sink: &mut dyn FnMut(&StreamEvent),
    ) -> Result<Vec<StreamEvent>, StreamChatError> {
        let payload = self
            .build_messages_payload_with_stream(conversation, system, tools, true)
            .map_err(StreamChatError::without_partial)?;
        let client = reqwest::blocking::Client::new();
        let request = self.authenticated_request(
            client
                .post(messages_url(&self.config))
                .body(payload.to_compact_json()),
        );

        let response = request
            .send()
            .map_err(|error| StreamChatError::without_partial(error.to_string()))?;
        let status = response.status();
        if !status.is_success() {
            let text = response
                .text()
                .map_err(|error| StreamChatError::without_partial(error.to_string()))?;
            return Err(StreamChatError::without_partial(format!(
                "HTTP error {}: {}",
                status.as_u16(),
                text
            )));
        }
        stream_response_events_with_sink(response, sink)
    }

    fn build_api_messages(&self, conversation: &Conversation) -> JsonValue {
        json::array(conversation.messages.iter().map(convert_message))
    }

    fn send_messages_request(&self, payload: JsonValue) -> Result<String, String> {
        self.send_messages_request_once(payload)
            .map_err(|error| error.message)
    }

    fn send_messages_request_with_retry(&self, payload: JsonValue) -> Result<String, String> {
        let mut last_error = None;
        for attempt in 0..=COMPLETE_MAX_RETRIES {
            match self.send_messages_request_once(payload.clone()) {
                Ok(text) => return Ok(text),
                Err(error) => {
                    let should_retry = error.retryable && attempt < COMPLETE_MAX_RETRIES;
                    last_error = Some(error.message);
                    if !should_retry {
                        break;
                    }
                }
            }
        }
        Err(last_error.unwrap_or_else(|| "Completion failed.".to_owned()))
    }

    fn send_messages_request_once(&self, payload: JsonValue) -> Result<String, CompleteChatError> {
        let client = reqwest::blocking::Client::new();
        let request = self.authenticated_request(
            client
                .post(messages_url(&self.config))
                .body(payload.to_compact_json()),
        );

        let response = request
            .send()
            .map_err(|error| CompleteChatError::retryable(error.to_string()))?;
        let status = response.status();
        let text = response
            .text()
            .map_err(|error| CompleteChatError::retryable(error.to_string()))?;
        if !status.is_success() {
            let message = format!("HTTP error {}: {}", status.as_u16(), text);
            return Err(if is_retryable_http_status(status.as_u16()) {
                CompleteChatError::retryable(message)
            } else {
                CompleteChatError::non_retryable(message)
            });
        }
        Ok(text)
    }
}
