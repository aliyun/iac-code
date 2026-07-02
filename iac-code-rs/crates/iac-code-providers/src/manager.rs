use std::collections::BTreeMap;

use crate::provider_descriptor;

#[derive(Clone, Debug, PartialEq, Eq)]
pub struct ProviderConfig {
    pub provider_key: String,
    pub model: String,
    pub api_key: Option<String>,
    pub base_url: Option<String>,
    pub effort: Option<String>,
    pub supports_stream_options: bool,
}

impl ProviderConfig {
    pub(crate) fn with_model(&self, model: &str) -> Self {
        let mut config = self.clone();
        config.model = model.to_owned();
        config
    }
}

pub(crate) fn fallback_model(model: &str) -> Option<&'static str> {
    match model {
        "claude-opus-4-7" => Some("claude-haiku-4-5-20251001"),
        "claude-opus-4-6" => Some("claude-haiku-4-5-20251001"),
        "claude-sonnet-4-6" => Some("claude-haiku-4-5-20251001"),
        "claude-sonnet-4-6-1m" => Some("claude-haiku-4-5-20251001"),
        "gpt-5.5" => Some("gpt-5.4"),
        "gpt-5.4" => Some("gpt-5.4-mini"),
        "qwen3.6-plus" => Some("qwen3.5-plus"),
        "deepseek-v4-pro" => Some("deepseek-v4-flash"),
        _ => None,
    }
}

pub fn create_provider_config(
    model: &str,
    credentials: &BTreeMap<String, String>,
    provider_key_override: Option<&str>,
    base_url_override: Option<&str>,
    saved_base_url: Option<&str>,
) -> Result<ProviderConfig, String> {
    let provider_key = provider_key_override
        .map(str::to_owned)
        .or_else(|| detect_provider_name(model))
        .ok_or_else(|| {
            format!("Cannot determine provider for model: {model}. Run /auth to configure.")
        })?;
    let descriptor = provider_descriptor(&provider_key).ok_or_else(|| {
        format!("Unknown provider key: '{provider_key}'. Run /auth to configure.")
    })?;

    let api_key = credentials
        .get(&provider_key)
        .filter(|value| !value.is_empty())
        .cloned()
        .or_else(|| local_provider_default_api_key(&provider_key).map(str::to_owned));
    if descriptor.require_api_key && api_key.is_none() {
        return Err(format!(
            "No API key configured for provider '{}' (model: {}). Run /auth to configure.",
            descriptor.display_name, model
        ));
    }

    let base_url = base_url_override
        .filter(|value| !value.is_empty())
        .map(str::to_owned)
        .or(descriptor.base_url)
        .or_else(|| {
            saved_base_url
                .filter(|value| !value.is_empty())
                .map(str::to_owned)
        });

    Ok(ProviderConfig {
        provider_key,
        model: model.to_owned(),
        api_key,
        base_url,
        effort: None,
        supports_stream_options: descriptor.supports_stream_options,
    })
}

fn local_provider_default_api_key(provider_key: &str) -> Option<&'static str> {
    match provider_key {
        "ollama" => Some("ollama"),
        "lmstudio" => Some("lm-studio"),
        _ => None,
    }
}

fn detect_provider_name(model: &str) -> Option<String> {
    let model_lower = model.to_ascii_lowercase();
    for (prefix, provider) in [
        ("claude-", "anthropic"),
        ("gpt-", "openai"),
        ("o1-", "openai"),
        ("o3-", "openai"),
        ("o4-", "openai"),
        ("qwen", "dashscope"),
        ("deepseek-", "deepseek"),
        ("gemini-", "gemini"),
        ("glm-", "zhipu_cn"),
        ("kimi-", "kimi_cn"),
        ("minimax-", "minimax_cn"),
        ("doubao-", "volcengine_cn"),
    ] {
        if model_lower.starts_with(prefix) {
            return Some(provider.to_owned());
        }
    }
    None
}
