use iac_code_protocol::json::{self, JsonValue};

use crate::ProviderConfig;

use super::payload::{anthropic_model_alias, anthropic_thinking_budget};
use super::AnthropicProvider;

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

impl AnthropicProvider {
    pub fn known_max_output_tokens(model: &str) -> Option<u64> {
        // Standard Messages capability; the larger batch-only beta is a separate protocol.
        match anthropic_model_alias(model).0.to_ascii_lowercase().as_str() {
            "claude-opus-4-7" | "claude-opus-4-6" | "claude-sonnet-4-6" => Some(128_000),
            "claude-haiku-4-5" | "claude-haiku-4-5-20251001" => Some(64_000),
            _ => None,
        }
    }

    pub(super) fn required_max_tokens(&self) -> Result<Option<u64>, String> {
        // MiniMax's Messages OpenAPI requires only model/messages; max_tokens is optional.
        if matches!(
            self.config.provider_key.as_str(),
            "minimax_cn" | "minimax_intl"
        ) {
            return Ok(None);
        }
        let (model, _) = anthropic_model_alias(&self.config.model);
        if self.config.provider_key != "anthropic_compatible" {
            if let Some(max_tokens) = Self::known_max_output_tokens(model) {
                return Ok(Some(max_tokens));
            }
        }

        let mut url =
            reqwest::Url::parse(&messages_url(&self.config)).map_err(|error| error.to_string())?;
        url.path_segments_mut()
            .map_err(|_| "The provider base URL cannot address model metadata.".to_owned())?
            .pop()
            .push("models")
            .push(model);
        let client = reqwest::blocking::Client::builder()
            .timeout(std::time::Duration::from_secs(30))
            .build()
            .map_err(|error| error.without_url().to_string())?;
        let response = self
            .authenticated_request(client.get(url))
            .send()
            .and_then(reqwest::blocking::Response::error_for_status)
            .and_then(reqwest::blocking::Response::text)
            .map_err(|error| {
                format!(
                    "Cannot determine model max_tokens for '{model}': {}",
                    error.without_url()
                )
            })?;
        let metadata: serde_json::Value = serde_json::from_str(&response)
            .map_err(|error| format!("Invalid model metadata for '{model}': {error}"))?;
        metadata
            .get("max_tokens")
            .and_then(serde_json::Value::as_u64)
            .filter(|value| *value > 0)
            .ok_or_else(|| {
                format!(
                    "Model metadata for '{model}' must provide a positive max_tokens capability."
                )
            })
            .map(Some)
    }

    pub(super) fn authenticated_request(
        &self,
        mut request: reqwest::blocking::RequestBuilder,
    ) -> reqwest::blocking::RequestBuilder {
        request = request
            .header("content-type", "application/json")
            .header("anthropic-version", ANTHROPIC_VERSION);
        if let Some(api_key) = &self.config.api_key {
            request = request.header("x-api-key", api_key);
        }
        if let Some(beta_header) = anthropic_model_alias(&self.config.model).1 {
            request = request.header("anthropic-beta", beta_header);
        }
        request
    }
}

pub(super) fn is_retryable_http_status(status: u16) -> bool {
    matches!(status, 408 | 409 | 429 | 500 | 502 | 503 | 529)
}
