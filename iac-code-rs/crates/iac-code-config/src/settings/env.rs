use std::env;

use crate::ConfigResult;

use super::providers::provider_lookup_key;

#[derive(Clone, Debug, Default, PartialEq, Eq)]
pub(crate) struct EnvOverrides {
    pub provider_key: Option<String>,
    pub model: Option<String>,
    pub api_base: Option<String>,
    pub api_key: Option<String>,
}

pub(crate) fn env_overrides() -> ConfigResult<EnvOverrides> {
    let provider_raw = read_env("IAC_CODE_PROVIDER");
    let provider_key = provider_raw
        .as_deref()
        .map(provider_lookup_key)
        .transpose()?;

    Ok(EnvOverrides {
        provider_key,
        model: read_env("IAC_CODE_MODEL"),
        api_base: read_env("IAC_CODE_BASE_URL"),
        api_key: read_env("IAC_CODE_API_KEY"),
    })
}

fn read_env(name: &str) -> Option<String> {
    env::var(name)
        .ok()
        .map(|value| value.trim().to_owned())
        .filter(|value| !value.is_empty())
}
