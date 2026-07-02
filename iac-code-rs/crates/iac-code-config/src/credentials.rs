use std::collections::BTreeMap;
use std::io::ErrorKind;

use crate::file_security::write_private_file;
use crate::paths::ConfigPaths;
use crate::settings::{
    env_overrides, get_active_provider_key, infer_provider_key_from_model, is_provider_key,
    PROVIDER_KEYS,
};
use crate::simple_yaml::{self, YamlValue};
use crate::{ConfigError, ConfigResult};

pub fn load_credentials(
    paths: &ConfigPaths,
    model: Option<&str>,
) -> ConfigResult<BTreeMap<String, String>> {
    let raw = simple_yaml::load_yaml_map(&paths.credentials_path).map_err(ConfigError::from)?;
    let mut credentials = BTreeMap::new();

    for key in PROVIDER_KEYS {
        let value = raw
            .get(*key)
            .and_then(YamlValue::as_str)
            .unwrap_or_default()
            .to_owned();
        credentials.insert((*key).to_owned(), value);
    }

    if credentials.get("dashscope").is_none_or(String::is_empty) {
        if let Some(value) = raw.get("bailian").and_then(YamlValue::as_str) {
            credentials.insert("dashscope".to_owned(), value.to_owned());
        }
    }
    if credentials
        .get("openapi_compatible")
        .is_none_or(String::is_empty)
    {
        if let Some(value) = raw.get("openai_compatible").and_then(YamlValue::as_str) {
            credentials.insert("openapi_compatible".to_owned(), value.to_owned());
        }
    }

    let env = env_overrides()?;
    if let Some(api_key) = env.api_key {
        let active_key = env
            .provider_key
            .or(get_active_provider_key(paths)?)
            .or_else(|| {
                model
                    .and_then(infer_provider_key_from_model)
                    .map(str::to_owned)
            });

        if let Some(active_key) = active_key.filter(|key| is_provider_key(key)) {
            credentials.insert(active_key, api_key);
        }
    }

    Ok(credentials)
}

pub fn save_llm_key(paths: &ConfigPaths, key_name: &str, api_key: &str) -> ConfigResult<()> {
    let mut credentials = match simple_yaml::load_yaml_map(&paths.credentials_path) {
        Ok(raw) => raw
            .into_iter()
            .filter_map(|(key, value)| value.as_str().map(|text| (key, text.to_owned())))
            .collect::<BTreeMap<_, _>>(),
        Err(error) if error.kind() == ErrorKind::NotFound => BTreeMap::new(),
        Err(error) => return Err(ConfigError::from(error)),
    };
    credentials.insert(key_name.to_owned(), api_key.to_owned());
    if key_name == "dashscope" {
        credentials.remove("bailian");
    }
    if key_name == "openapi_compatible" {
        credentials.remove("openai_compatible");
    }

    let mut content = String::new();
    for (key, value) in credentials {
        content.push_str(&key);
        content.push_str(": ");
        content.push_str(&quote_yaml_scalar(&value));
        content.push('\n');
    }
    write_private_file(&paths.credentials_path, content).map_err(ConfigError::from)
}

fn quote_yaml_scalar(value: &str) -> String {
    if value
        .chars()
        .all(|character| character.is_ascii_alphanumeric() || matches!(character, '-' | '_' | '.'))
    {
        return value.to_owned();
    }
    format!("\"{}\"", value.replace('\\', "\\\\").replace('"', "\\\""))
}
