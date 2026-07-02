use std::collections::BTreeMap;

use crate::simple_yaml::YamlValue;
use crate::{ConfigError, ConfigResult};

pub const DEFAULT_MODEL: &str = "qwen3.7-max";

pub const PROVIDER_KEYS: &[&str] = &[
    "dashscope",
    "dashscope_token_plan",
    "openai",
    "anthropic",
    "deepseek",
    "openapi_compatible",
    "anthropic_compatible",
    "gemini",
    "kimi_cn",
    "kimi_intl",
    "minimax_cn",
    "minimax_intl",
    "zhipu_cn",
    "zhipu_intl",
    "volcengine_cn",
    "siliconflow_cn",
    "siliconflow_intl",
    "ollama",
    "lmstudio",
    "openrouter",
    "azure_openai",
    "modelscope",
    "aliyun_codingplan",
    "aliyun_codingplan_intl",
    "zhipu_cn_codingplan",
    "zhipu_intl_codingplan",
    "volcengine_cn_codingplan",
];

const PROVIDER_NAMES: &[(&str, &str)] = &[
    ("dashscope", "DashScope"),
    ("dashscope_token_plan", "DashScope Token Plan"),
    ("openai", "OpenAI"),
    ("anthropic", "Anthropic"),
    ("deepseek", "DeepSeek"),
    ("openapi_compatible", "OpenAPI Compatible"),
    ("anthropic_compatible", "Anthropic Compatible"),
    ("gemini", "Gemini"),
    ("kimi_cn", "Kimi CN"),
    ("kimi_intl", "Kimi Intl"),
    ("minimax_cn", "MiniMax CN"),
    ("minimax_intl", "MiniMax Intl"),
    ("zhipu_cn", "ZhiPu CN"),
    ("zhipu_intl", "ZhiPu Intl"),
    ("volcengine_cn", "Volcengine CN"),
    ("siliconflow_cn", "SiliconFlow CN"),
    ("siliconflow_intl", "SiliconFlow Intl"),
    ("ollama", "Ollama"),
    ("lmstudio", "LM Studio"),
    ("openrouter", "OpenRouter"),
    ("azure_openai", "Azure OpenAI"),
    ("modelscope", "ModelScope"),
    ("aliyun_codingplan", "Aliyun CodingPlan"),
    ("aliyun_codingplan_intl", "Aliyun CodingPlan Intl"),
    ("zhipu_cn_codingplan", "ZhiPu CN CodingPlan"),
    ("zhipu_intl_codingplan", "ZhiPu Intl CodingPlan"),
    ("volcengine_cn_codingplan", "Volcengine CodingPlan"),
];

const PARTNER_SOURCE_NAMES: &[(&str, &str)] = &[("qwenpaw", "QwenPaw")];

const LEGACY_KEY_NAME_ALIASES: &[(&str, &str)] = &[
    ("bailian", "dashscope"),
    ("openai_compatible", "openapi_compatible"),
];

const MODEL_PREFIX_TO_PROVIDER: &[(&str, &str)] = &[
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
];

pub(crate) fn infer_provider_key_from_model(model: &str) -> Option<&'static str> {
    let model_lower = model.to_ascii_lowercase();
    MODEL_PREFIX_TO_PROVIDER
        .iter()
        .find_map(|(prefix, provider)| model_lower.starts_with(prefix).then_some(*provider))
}

pub(crate) fn is_provider_key(value: &str) -> bool {
    PROVIDER_KEYS.contains(&value)
}

pub fn resolve_provider_key(value: &str) -> ConfigResult<String> {
    provider_lookup_key(value)
}

pub fn provider_display_name(key_name: &str) -> &str {
    PROVIDER_NAMES
        .iter()
        .find_map(|(key, name)| (*key == key_name).then_some(*name))
        .unwrap_or(key_name)
}

pub fn partner_source_display_name(source: &str) -> &str {
    PARTNER_SOURCE_NAMES
        .iter()
        .find_map(|(key, name)| (*key == source).then_some(*name))
        .unwrap_or(source)
}

pub(super) fn provider_entry<'a>(
    providers: &'a BTreeMap<String, YamlValue>,
    key_name: &str,
) -> Option<&'a YamlValue> {
    providers.get(key_name).or_else(|| {
        LEGACY_KEY_NAME_ALIASES
            .iter()
            .find_map(|(legacy, canonical)| {
                (*canonical == key_name)
                    .then(|| providers.get(*legacy))
                    .flatten()
            })
    })
}

pub(super) fn provider_lookup_key(value: &str) -> ConfigResult<String> {
    let normalized = normalize_provider_lookup_name(value);

    for key in PROVIDER_KEYS {
        if normalize_provider_lookup_name(key) == normalized {
            return Ok((*key).to_owned());
        }
    }

    for (key, name) in PROVIDER_NAMES {
        if normalize_provider_lookup_name(name) == normalized {
            return Ok((*key).to_owned());
        }
    }

    for (legacy, canonical) in LEGACY_KEY_NAME_ALIASES {
        if normalize_provider_lookup_name(legacy) == normalized {
            return Ok((*canonical).to_owned());
        }
    }

    Err(ConfigError::InvalidValue(format!(
        "Invalid IAC_CODE_PROVIDER value: '{}'. Valid values (case-insensitive): {}",
        value,
        valid_provider_names()
    )))
}

pub(super) fn canonical_provider_key(value: &str) -> String {
    LEGACY_KEY_NAME_ALIASES
        .iter()
        .find_map(|(legacy, canonical)| (*legacy == value).then_some((*canonical).to_owned()))
        .unwrap_or_else(|| value.to_owned())
}

fn valid_provider_names() -> String {
    PROVIDER_NAMES
        .iter()
        .map(|(_, name)| *name)
        .collect::<Vec<_>>()
        .join(", ")
}

fn normalize_provider_lookup_name(value: &str) -> String {
    value
        .chars()
        .filter(|character| !matches!(character, ' ' | '-' | '_'))
        .flat_map(char::to_lowercase)
        .collect()
}
