use crate::paths::ConfigPaths;
use crate::{ConfigError, ConfigResult};

use super::active_provider::get_active_provider_key;
use super::file_io::{read_settings_content, write_settings_content};
use super::providers::{provider_display_name, resolve_provider_key};
use super::yaml_edit::{
    remove_root_yaml_block, upsert_provider_string_setting, upsert_root_string_setting,
};

pub fn save_active_provider_model(paths: &ConfigPaths, model: &str) -> ConfigResult<()> {
    let Some(provider_key) = get_active_provider_key(paths)? else {
        return Err(ConfigError::InvalidProvider("activeProvider".to_owned()));
    };
    let existing = read_settings_content(paths)?;
    let content = upsert_provider_string_setting(&existing, &provider_key, "model", model);
    write_settings_content(paths, content)
}

pub fn save_active_provider_config(
    paths: &ConfigPaths,
    key_name: &str,
    model: &str,
    api_base: Option<&str>,
) -> ConfigResult<()> {
    let key_name = resolve_provider_key(key_name)?;
    let provider_name = provider_display_name(&key_name);
    let existing = read_settings_content(paths)?;
    let mut content = upsert_root_string_setting(&existing, "activeProvider", &key_name);
    content = upsert_provider_string_setting(&content, &key_name, "name", provider_name);
    content = upsert_provider_string_setting(&content, &key_name, "model", model);
    if let Some(api_base) = api_base.filter(|value| !value.trim().is_empty()) {
        content = upsert_provider_string_setting(&content, &key_name, "apiBase", api_base.trim());
    }
    write_settings_content(paths, content)
}

pub fn save_llm_source(paths: &ConfigPaths, source: &str) -> ConfigResult<()> {
    let existing = read_settings_content(paths)?;
    let content = remove_root_yaml_block(&existing, "activeProvider");
    let content = upsert_root_string_setting(&content, "llm_source", source);
    write_settings_content(paths, content)
}

pub fn save_saved_effort(paths: &ConfigPaths, effort: &str) -> ConfigResult<()> {
    let existing = read_settings_content(paths)?;
    let content = upsert_root_string_setting(&existing, "effort", effort);
    write_settings_content(paths, content)
}

pub fn save_active_provider_effort(paths: &ConfigPaths, effort: &str) -> ConfigResult<()> {
    let Some(provider_key) = get_active_provider_key(paths)? else {
        return Err(ConfigError::InvalidProvider("activeProvider".to_owned()));
    };
    let existing = read_settings_content(paths)?;
    let content = upsert_provider_string_setting(&existing, &provider_key, "effort", effort);
    write_settings_content(paths, content)
}
