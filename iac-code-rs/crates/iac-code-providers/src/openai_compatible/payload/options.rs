use iac_code_protocol::json::{self, JsonValue};

use super::super::OpenAiCompatibleProvider;

impl OpenAiCompatibleProvider {
    pub(super) fn thinking_payload_entries(&self) -> Vec<(&'static str, JsonValue)> {
        if is_dashscope_thinking_model(&self.config.provider_key, &self.config.model) {
            return vec![(
                "extra_body",
                json::object([("enable_thinking", json::bool_value(true))]),
            )];
        }

        if let Some((allowed_efforts, default_effort)) =
            gemini_effort_spec(&self.config.provider_key, &self.config.model)
        {
            let Some(selected_effort) = selected_effort(
                self.config.effort.as_deref(),
                allowed_efforts,
                default_effort,
            ) else {
                return Vec::new();
            };
            return vec![("reasoning_effort", json::string(selected_effort))];
        }

        let Some((allowed_efforts, default_effort)) =
            openai_effort_spec(&self.config.provider_key, &self.config.model)
        else {
            return Vec::new();
        };
        let Some(selected_effort) = selected_effort(
            self.config.effort.as_deref(),
            allowed_efforts,
            default_effort,
        ) else {
            return Vec::new();
        };
        vec![
            ("reasoning_effort", json::string(selected_effort)),
            (
                "extra_body",
                json::object([(
                    "thinking",
                    json::object([("type", json::string("enabled"))]),
                )]),
            ),
        ]
    }
}

fn openai_effort_spec(
    provider_key: &str,
    model: &str,
) -> Option<(&'static [&'static str], &'static str)> {
    const OPENAI_EFFORTS: &[&str] = &["low", "medium", "high", "xhigh"];
    const DEEPSEEK_EFFORTS: &[&str] = &["high", "max"];
    match provider_key {
        "openai"
            if matches!(
                model,
                "gpt-5.5"
                    | "gpt-5.4"
                    | "gpt-5.4-mini"
                    | "gpt-5.3-codex"
                    | "gpt-5.2"
                    | "o3"
                    | "o4-mini"
            ) =>
        {
            Some((OPENAI_EFFORTS, "high"))
        }
        "deepseek" if matches!(model, "deepseek-v4-pro" | "deepseek-v4-flash") => {
            Some((DEEPSEEK_EFFORTS, "high"))
        }
        _ => None,
    }
}

fn gemini_effort_spec(
    provider_key: &str,
    model: &str,
) -> Option<(&'static [&'static str], &'static str)> {
    const GEMINI_EFFORTS: &[&str] = &["low", "medium", "high"];
    match provider_key {
        "gemini"
            if matches!(
                model,
                "gemini-3.5-flash"
                    | "gemini-3.1-pro-preview"
                    | "gemini-3-flash-preview"
                    | "gemini-3.1-flash-lite"
                    | "gemini-3.1-flash-lite-preview"
                    | "gemini-2.5-pro"
                    | "gemini-2.5-flash"
            ) =>
        {
            Some((GEMINI_EFFORTS, "medium"))
        }
        _ => None,
    }
}

fn is_dashscope_thinking_model(provider_key: &str, model: &str) -> bool {
    let effective_provider = match provider_key {
        "aliyun_codingplan" | "aliyun_codingplan_intl" => "dashscope",
        _ => provider_key,
    };
    match effective_provider {
        "dashscope" => matches!(
            model,
            "qwen3.7-max"
                | "qwen3.7-plus"
                | "qwen3.6-max-preview"
                | "qwen3.6-plus"
                | "qwen3.5-plus"
                | "qwen3.5-flash"
                | "qwq-plus"
                | "kimi-k2.6"
                | "glm-5.1"
                | "deepseek-v4-pro"
                | "deepseek-v4-flash"
        ),
        "dashscope_token_plan" => matches!(
            model,
            "qwen3.6-plus" | "deepseek-v3.2" | "glm-5" | "MiniMax-M2.5"
        ),
        _ => false,
    }
}

fn normalized_effort(effort: Option<&str>) -> Option<String> {
    effort
        .map(str::trim)
        .filter(|value| !value.is_empty())
        .map(str::to_ascii_lowercase)
}

fn selected_effort(
    effort: Option<&str>,
    allowed_efforts: &[&str],
    default_effort: &str,
) -> Option<String> {
    let effort = normalized_effort(effort)?;
    if effort == "auto" {
        return None;
    }
    Some(if allowed_efforts.contains(&effort.as_str()) {
        effort
    } else {
        default_effort.to_owned()
    })
}
