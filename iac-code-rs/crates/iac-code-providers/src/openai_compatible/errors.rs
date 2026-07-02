use iac_code_protocol::StreamEvent;

use crate::ProviderConfig;

#[derive(Clone, Debug, PartialEq)]
pub struct StreamChatError {
    pub(super) message: String,
    pub(super) partial_events: Vec<StreamEvent>,
}

#[derive(Clone, Debug, PartialEq, Eq)]
pub(super) struct CompleteChatError {
    pub(super) message: String,
    pub(super) retryable: bool,
}

impl CompleteChatError {
    pub(super) fn retryable(message: impl Into<String>) -> Self {
        Self {
            message: message.into(),
            retryable: true,
        }
    }

    pub(super) fn non_retryable(message: impl Into<String>) -> Self {
        Self {
            message: message.into(),
            retryable: false,
        }
    }
}

impl StreamChatError {
    pub(super) fn new(message: impl Into<String>, partial_events: Vec<StreamEvent>) -> Self {
        Self {
            message: message.into(),
            partial_events,
        }
    }

    pub(super) fn without_partial(message: impl Into<String>) -> Self {
        Self::new(message, Vec::new())
    }
}

pub(super) fn streaming_response_error(
    config: &ProviderConfig,
    error: StreamChatError,
) -> StreamChatError {
    if error.message == "API returned no data. Please check that your API Base URL is correct." {
        return StreamChatError::new(no_data_base_url_hint(config), error.partial_events);
    }
    error
}

pub(super) fn non_streaming_response_error(config: &ProviderConfig, error: String) -> String {
    match error.as_str() {
        "API returned an invalid response: missing choices" => {
            invalid_response_base_url_hint(config)
        }
        "API returned an invalid response: Response choices were empty." => {
            format!(
                "{} Response choices were empty.",
                invalid_response_base_url_hint(config)
            )
        }
        _ if error.starts_with("API returned an invalid response") => {
            format!("{} {}", invalid_response_base_url_hint(config), error)
        }
        _ => error,
    }
}

pub(super) fn is_retryable_http_status(status: u16) -> bool {
    matches!(status, 408 | 409 | 429 | 500 | 502 | 503 | 529)
}

fn no_data_base_url_hint(config: &ProviderConfig) -> String {
    let base_url = base_url_for_hint(config);
    format!(
        "API returned no data. Please check that your API Base URL is correct (current: {base_url}). Many OpenAI-compatible endpoints require a /v1 suffix (e.g. {base_url}/v1)."
    )
}

fn invalid_response_base_url_hint(config: &ProviderConfig) -> String {
    let base_url = base_url_for_hint(config);
    format!(
        "API returned an invalid response. Please check that your API Base URL is correct (current: {base_url}). Many OpenAI-compatible endpoints require a /v1 suffix (e.g. {base_url}/v1)."
    )
}

fn base_url_for_hint(config: &ProviderConfig) -> String {
    config
        .base_url
        .as_deref()
        .unwrap_or("")
        .trim_end_matches('/')
        .to_owned()
}
