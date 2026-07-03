use iac_code_protocol::json::{self, JsonValue};
use iac_code_protocol::message::Conversation;
use iac_code_protocol::provider::{NonStreamingResponse, ToolDefinition};
use iac_code_protocol::StreamEvent;

use super::config::{
    adjusted_max_tokens, is_retryable_http_status, messages_url, thinking_payload,
    ANTHROPIC_VERSION, COMPLETE_MAX_RETRIES,
};
use super::errors::CompleteChatError;
use super::payload::{anthropic_model_alias, convert_message, convert_tools};
use super::response::{parse_non_streaming_response, stream_events_from_response};
use super::sse::{parse_sse_stream_events, stream_response_events_with_sink};
use super::{AnthropicProvider, StreamChatError};

impl AnthropicProvider {
    pub fn build_messages_payload(
        &self,
        conversation: &Conversation,
        system: &str,
        tools: &[ToolDefinition],
        max_tokens: u32,
    ) -> JsonValue {
        self.build_messages_payload_with_stream(conversation, system, tools, max_tokens, false)
    }

    fn build_messages_payload_with_stream(
        &self,
        conversation: &Conversation,
        system: &str,
        tools: &[ToolDefinition],
        max_tokens: u32,
        stream: bool,
    ) -> JsonValue {
        let (model, _) = anthropic_model_alias(&self.config.model);
        let mut entries = vec![
            ("model", json::string(model)),
            (
                "max_tokens",
                json::number(adjusted_max_tokens(&self.config, max_tokens)),
            ),
            ("system", json::string(system)),
            ("messages", self.build_api_messages(conversation)),
        ];
        if !tools.is_empty() {
            entries.push(("tools", convert_tools(tools)));
        }
        if let Some(thinking) = thinking_payload(&self.config) {
            entries.push(("thinking", thinking));
        }
        if stream {
            entries.push(("stream", json::bool_value(true)));
        }
        json::object(entries)
    }

    pub fn complete_chat(
        &self,
        conversation: &Conversation,
        system: &str,
        tools: &[ToolDefinition],
        max_tokens: u32,
    ) -> Result<NonStreamingResponse, String> {
        let payload =
            self.build_messages_payload_with_stream(conversation, system, tools, max_tokens, false);
        let text = self.send_messages_request_with_retry(payload)?;
        parse_non_streaming_response(&text)
    }

    pub fn stream_chat(
        &self,
        conversation: &Conversation,
        system: &str,
        tools: &[ToolDefinition],
        max_tokens: u32,
    ) -> Result<Vec<StreamEvent>, StreamChatError> {
        let payload =
            self.build_messages_payload_with_stream(conversation, system, tools, max_tokens, true);
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
        max_tokens: u32,
        sink: &mut dyn FnMut(&StreamEvent),
    ) -> Result<Vec<StreamEvent>, StreamChatError> {
        let payload =
            self.build_messages_payload_with_stream(conversation, system, tools, max_tokens, true);
        let client = reqwest::blocking::Client::new();
        let mut request = client
            .post(messages_url(&self.config))
            .header("content-type", "application/json")
            .header("anthropic-version", ANTHROPIC_VERSION)
            .body(payload.to_compact_json());
        if let Some(api_key) = &self.config.api_key {
            request = request.header("x-api-key", api_key);
        }
        let (_, beta_header) = anthropic_model_alias(&self.config.model);
        if let Some(beta_header) = beta_header {
            request = request.header("anthropic-beta", beta_header);
        }

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
        let mut request = client
            .post(messages_url(&self.config))
            .header("content-type", "application/json")
            .header("anthropic-version", ANTHROPIC_VERSION)
            .body(payload.to_compact_json());
        if let Some(api_key) = &self.config.api_key {
            request = request.header("x-api-key", api_key);
        }
        let (_, beta_header) = anthropic_model_alias(&self.config.model);
        if let Some(beta_header) = beta_header {
            request = request.header("anthropic-beta", beta_header);
        }

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
