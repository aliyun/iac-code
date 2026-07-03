use iac_code_protocol::json::{self, JsonValue};

use crate::ProviderConfig;

use super::payload::anthropic_thinking_budget;

pub(super) const ANTHROPIC_VERSION: &str = "2023-06-01";
pub(super) const COMPLETE_MAX_RETRIES: usize = 5;

const DEFAULT_ANTHROPIC_BASE_URL: &str = "https://api.anthropic.com";

pub(super) fn messages_url(config: &ProviderConfig) -> String {
    let base_url = config
        .base_url
        .as_deref()
        .filter(|value| !value.is_empty())
        .unwrap_or(DEFAULT_ANTHROPIC_BASE_URL)
        .trim_end_matches('/');
    if base_url.ends_with("/v1") {
        format!("{base_url}/messages")
    } else {
        format!("{base_url}/v1/messages")
    }
}

pub(super) fn thinking_payload(config: &ProviderConfig) -> Option<JsonValue> {
    let budget = anthropic_thinking_budget(
        &config.provider_key,
        &config.model,
        config.effort.as_deref(),
    )?;
    Some(json::object([
        ("type", json::string("enabled")),
        ("budget_tokens", json::number(budget)),
    ]))
}

pub(super) fn adjusted_max_tokens(config: &ProviderConfig, max_tokens: u32) -> u32 {
    anthropic_thinking_budget(
        &config.provider_key,
        &config.model,
        config.effort.as_deref(),
    )
    .map(|budget| max_tokens.max(budget + 4096))
    .unwrap_or(max_tokens)
}

pub(super) fn is_retryable_http_status(status: u16) -> bool {
    matches!(status, 408 | 409 | 429 | 500 | 502 | 503 | 529)
}
