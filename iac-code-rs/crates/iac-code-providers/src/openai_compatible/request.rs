use iac_code_protocol::json::JsonValue;
use iac_code_protocol::message::Conversation;
use iac_code_protocol::provider::{NonStreamingResponse, ToolDefinition};
use iac_code_protocol::StreamEvent;

use super::errors::{
    is_retryable_http_status, non_streaming_response_error, streaming_response_error,
    CompleteChatError, StreamChatError,
};
use super::response::{parse_non_streaming_response, stream_events_from_response};
use super::sse::parse_sse_stream_events;
use super::stream::stream_response_events_with_sink;
use super::OpenAiCompatibleProvider;

const COMPLETE_MAX_RETRIES: usize = 5;

impl OpenAiCompatibleProvider {
    pub fn complete_chat(
        &self,
        conversation: &Conversation,
        system: &str,
        tools: &[ToolDefinition],
        max_tokens: u32,
    ) -> Result<NonStreamingResponse, String> {
        let mut last_error = None;
        for attempt in 0..=COMPLETE_MAX_RETRIES {
            match self.complete_chat_once(conversation, system, tools, max_tokens) {
                Ok(response) => return Ok(response),
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

    fn complete_chat_once(
        &self,
        conversation: &Conversation,
        system: &str,
        tools: &[ToolDefinition],
        max_tokens: u32,
    ) -> Result<NonStreamingResponse, CompleteChatError> {
        let url = self
            .chat_completions_url()
            .map_err(CompleteChatError::non_retryable)?;
        let payload = self.build_chat_payload(conversation, system, tools, max_tokens, false);
        let client = reqwest::blocking::Client::new();
        let request = self.build_chat_request(&client, url, payload);

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
        parse_non_streaming_response(&text).map_err(|error| {
            CompleteChatError::non_retryable(non_streaming_response_error(&self.config, error))
        })
    }

    pub fn stream_chat(
        &self,
        conversation: &Conversation,
        system: &str,
        tools: &[ToolDefinition],
        max_tokens: u32,
    ) -> Result<Vec<StreamEvent>, StreamChatError> {
        let url = self
            .chat_completions_url()
            .map_err(StreamChatError::without_partial)?;
        let payload = self.build_chat_payload(conversation, system, tools, max_tokens, true);
        let client = reqwest::blocking::Client::new();
        let request = self.build_chat_request(&client, url, payload);

        let response = request
            .send()
            .map_err(|error| StreamChatError::without_partial(error.to_string()))?;
        let status = response.status();
        let text = response
            .text()
            .map_err(|error| StreamChatError::without_partial(error.to_string()))?;
        if !status.is_success() {
            return Err(StreamChatError::without_partial(format!(
                "HTTP error {}: {}",
                status.as_u16(),
                text
            )));
        }
        if text.trim_start().starts_with('{') {
            return parse_non_streaming_response(&text)
                .map(stream_events_from_response)
                .map_err(|error| {
                    StreamChatError::without_partial(non_streaming_response_error(
                        &self.config,
                        error,
                    ))
                });
        }
        parse_sse_stream_events(&text)
            .map_err(|error| streaming_response_error(&self.config, error))
    }

    pub fn stream_chat_with_sink(
        &self,
        conversation: &Conversation,
        system: &str,
        tools: &[ToolDefinition],
        max_tokens: u32,
        sink: &mut dyn FnMut(&StreamEvent),
    ) -> Result<Vec<StreamEvent>, StreamChatError> {
        let url = self
            .chat_completions_url()
            .map_err(StreamChatError::without_partial)?;
        let payload = self.build_chat_payload(conversation, system, tools, max_tokens, true);
        let client = reqwest::blocking::Client::new();
        let request = self.build_chat_request(&client, url, payload);

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
            .map_err(|error| streaming_response_error(&self.config, error))
    }

    fn chat_completions_url(&self) -> Result<String, String> {
        let Some(base_url) = &self.config.base_url else {
            return Err(format!(
                "No API base URL configured for provider '{}'",
                self.config.provider_key
            ));
        };
        Ok(format!(
            "{}/chat/completions",
            base_url.trim_end_matches('/')
        ))
    }

    fn build_chat_request(
        &self,
        client: &reqwest::blocking::Client,
        url: String,
        payload: JsonValue,
    ) -> reqwest::blocking::RequestBuilder {
        let mut request = client
            .post(url)
            .header("content-type", "application/json")
            .body(payload.to_compact_json());
        if let Some(api_key) = &self.config.api_key {
            request = request.bearer_auth(api_key);
        }
        for &(name, value) in self.provider_default_headers() {
            request = request.header(name, value);
        }
        request
    }

    fn provider_default_headers(&self) -> &'static [(&'static str, &'static str)] {
        const OPENROUTER_HEADERS: &[(&str, &str)] = &[
            ("HTTP-Referer", "https://github.com/aliyun/iac-code"),
            ("X-Title", "iac-code"),
        ];

        match self.config.provider_key.as_str() {
            "openrouter" => OPENROUTER_HEADERS,
            _ => &[],
        }
    }
}
