use std::collections::BTreeMap;

use crate::paths::ConfigPaths;
use crate::simple_yaml::{self, YamlValue};
use crate::ConfigResult;

use super::env::env_overrides;
use super::providers::{canonical_provider_key, provider_entry};
use super::store::load_settings;

pub fn get_active_provider_key(paths: &ConfigPaths) -> ConfigResult<Option<String>> {
    if let Some(provider_key) = env_overrides()?.provider_key {
        return Ok(Some(provider_key));
    }

    let settings = load_settings(paths)?;
    Ok(settings
        .get("activeProvider")
        .and_then(YamlValue::as_str)
        .filter(|value| !value.is_empty())
        .map(canonical_provider_key))
}

pub fn get_provider_config(
    paths: &ConfigPaths,
    key_name: &str,
) -> ConfigResult<BTreeMap<String, String>> {
    let settings = load_settings(paths)?;
    let mut entry = settings
        .get("providers")
        .and_then(YamlValue::as_map)
        .and_then(|providers| provider_entry(providers, key_name))
        .map(|value| simple_yaml::string_map(Some(value)))
        .unwrap_or_default();

    let env = env_overrides()?;
    let active_key = if let Some(provider_key) = env.provider_key.clone() {
        Some(provider_key)
    } else {
        settings
            .get("activeProvider")
            .and_then(YamlValue::as_str)
            .filter(|value| !value.is_empty())
            .map(canonical_provider_key)
    };

    if active_key.as_deref() == Some(key_name) {
        if let Some(model) = env.model {
            entry.insert("model".to_owned(), model);
        }
        if let Some(api_base) = env.api_base {
            if key_name == "openapi_compatible" {
                entry.insert("apiBase".to_owned(), api_base);
            }
        }
    }

    Ok(entry)
}

pub fn load_saved_model(paths: &ConfigPaths) -> ConfigResult<Option<String>> {
    let Some(key) = get_active_provider_key(paths)? else {
        return Ok(None);
    };
    Ok(get_provider_config(paths, &key)?
        .get("model")
        .filter(|value| !value.is_empty())
        .cloned())
}

pub fn load_saved_effort(paths: &ConfigPaths) -> ConfigResult<Option<String>> {
    Ok(load_settings(paths)?
        .get("effort")
        .and_then(YamlValue::as_str)
        .map(str::to_owned))
}

pub fn load_active_provider_effort(paths: &ConfigPaths) -> ConfigResult<Option<String>> {
    let Some(key) = get_active_provider_key(paths)? else {
        return Ok(None);
    };
    Ok(get_provider_config(paths, &key)?
        .get("effort")
        .filter(|value| !value.is_empty())
        .cloned())
}

pub fn load_active_provider_config(
    paths: &ConfigPaths,
) -> ConfigResult<Option<BTreeMap<String, String>>> {
    let Some(key) = get_active_provider_key(paths)? else {
        return Ok(None);
    };
    let mut config = get_provider_config(paths, &key)?;
    config.insert("keyName".to_owned(), key);
    Ok(Some(config))
}

pub fn get_llm_source(paths: &ConfigPaths) -> ConfigResult<String> {
    if env_overrides()?.api_key.is_some() {
        return Ok("env".to_owned());
    }

    let settings = load_settings(paths)?;
    if settings
        .get("activeProvider")
        .and_then(YamlValue::as_str)
        .is_some_and(|value| !value.trim().is_empty())
    {
        return Ok("local".to_owned());
    }

    Ok(settings
        .get("llm_source")
        .and_then(YamlValue::as_str)
        .filter(|value| !value.trim().is_empty())
        .map(str::trim)
        .unwrap_or("local")
        .to_owned())
}
