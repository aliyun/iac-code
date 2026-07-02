use iac_code_providers::{is_qwenpaw_available, provider_descriptor, ProviderDescriptor};

use crate::cli_i18n::{tr, tr_dynamic};

#[derive(Clone, Debug)]
pub(super) struct RawAuthProviderGroup {
    pub(super) name: &'static str,
    pub(super) keys: &'static [&'static str],
}

#[derive(Clone, Debug)]
pub(super) struct RawAuthPartnerSource {
    pub(super) key: &'static str,
    pub(super) display_name: &'static str,
}

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub(crate) enum RawAuthLlmGroupChoice {
    ThirdParty,
    ProviderGroup(usize),
}

pub(crate) fn raw_auth_llm_group_choice(
    show_third_party: bool,
    group_index: usize,
    group_count: usize,
) -> Option<RawAuthLlmGroupChoice> {
    if show_third_party && group_index == 0 {
        return Some(RawAuthLlmGroupChoice::ThirdParty);
    }
    let provider_group_index = group_index.checked_sub(usize::from(show_third_party))?;
    if provider_group_index < group_count {
        Some(RawAuthLlmGroupChoice::ProviderGroup(provider_group_index))
    } else {
        None
    }
}

pub(crate) fn raw_auth_provider_display_name(provider: &ProviderDescriptor) -> String {
    tr_dynamic(&provider.display_name)
}

pub(crate) fn raw_auth_configured_provider_model_message(
    provider_display_name: &str,
    model: &str,
) -> String {
    tr("{status}: {provider} / {model}")
        .replace("{status}", &tr("Configured"))
        .replace("{provider}", provider_display_name)
        .replace("{model}", model)
}

pub(super) fn raw_auth_configured_provider_message(provider_display_name: &str) -> String {
    tr("{status}: {provider}")
        .replace("{status}", &tr("Configured"))
        .replace("{provider}", provider_display_name)
}

pub(super) fn raw_auth_current_label(mut label: String, current: bool) -> String {
    if current {
        label.push_str(&tr(" (current)"));
    }
    label
}

pub(super) fn raw_auth_providers_for_group(
    group: &RawAuthProviderGroup,
) -> Vec<ProviderDescriptor> {
    group
        .keys
        .iter()
        .filter_map(|key| provider_descriptor(key))
        .collect()
}

pub(super) fn raw_auth_provider_groups() -> Vec<RawAuthProviderGroup> {
    vec![
        RawAuthProviderGroup {
            name: "Alibaba Cloud",
            keys: &[
                "dashscope",
                "dashscope_token_plan",
                "aliyun_codingplan",
                "aliyun_codingplan_intl",
                "modelscope",
            ],
        },
        RawAuthProviderGroup {
            name: "ZhiPu AI",
            keys: &[
                "zhipu_cn",
                "zhipu_intl",
                "zhipu_cn_codingplan",
                "zhipu_intl_codingplan",
            ],
        },
        RawAuthProviderGroup {
            name: "Kimi",
            keys: &["kimi_cn", "kimi_intl"],
        },
        RawAuthProviderGroup {
            name: "MiniMax",
            keys: &["minimax_cn", "minimax_intl"],
        },
        RawAuthProviderGroup {
            name: "Volcengine",
            keys: &["volcengine_cn", "volcengine_cn_codingplan"],
        },
        RawAuthProviderGroup {
            name: "SiliconFlow",
            keys: &["siliconflow_cn", "siliconflow_intl"],
        },
        RawAuthProviderGroup {
            name: "DeepSeek",
            keys: &["deepseek"],
        },
        RawAuthProviderGroup {
            name: "OpenAI",
            keys: &["openai"],
        },
        RawAuthProviderGroup {
            name: "Anthropic",
            keys: &["anthropic"],
        },
        RawAuthProviderGroup {
            name: "Google Gemini",
            keys: &["gemini"],
        },
        RawAuthProviderGroup {
            name: "Azure OpenAI",
            keys: &["azure_openai"],
        },
        RawAuthProviderGroup {
            name: "OpenRouter",
            keys: &["openrouter"],
        },
        RawAuthProviderGroup {
            name: "Local",
            keys: &["ollama", "lmstudio"],
        },
        RawAuthProviderGroup {
            name: "Compatible",
            keys: &["openapi_compatible", "anthropic_compatible"],
        },
    ]
}

pub(super) fn raw_auth_partner_sources(current_llm_source: &str) -> Vec<RawAuthPartnerSource> {
    if current_llm_source == "qwenpaw" || is_qwenpaw_available() {
        return vec![RawAuthPartnerSource {
            key: "qwenpaw",
            display_name: "QwenPaw",
        }];
    }
    Vec::new()
}

pub(super) fn raw_auth_is_partner_source(source: &str) -> bool {
    source == "qwenpaw"
}
