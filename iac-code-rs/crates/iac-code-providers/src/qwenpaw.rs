use std::env;
use std::fs;
use std::path::{Path, PathBuf};
#[cfg(target_os = "macos")]
use std::process::Command;

use base64::engine::general_purpose::URL_SAFE;
use base64::Engine;
use iac_code_crypto::fernet_decrypt;
use serde_json::Value;

use crate::provider_descriptor;

const ENC_PREFIX: &str = "ENC:";
const QWENPAW_PROVIDER_MAPPING: &[(&str, &str)] = &[
    ("dashscope", "dashscope"),
    ("aliyun-tokenplan", "dashscope_token_plan"),
    ("openai", "openai"),
    ("anthropic", "anthropic"),
    ("deepseek", "deepseek"),
    ("gemini", "gemini"),
    ("kimi-cn", "kimi_cn"),
    ("kimi-intl", "kimi_intl"),
    ("minimax-cn", "minimax_cn"),
    ("minimax", "minimax_intl"),
    ("zhipu-cn", "zhipu_cn"),
    ("zhipu-intl", "zhipu_intl"),
    ("volcengine-cn", "volcengine_cn"),
    ("siliconflow-cn", "siliconflow_cn"),
    ("siliconflow-intl", "siliconflow_intl"),
    ("ollama", "ollama"),
    ("lmstudio", "lmstudio"),
    ("openrouter", "openrouter"),
    ("azure-openai", "azure_openai"),
    ("modelscope", "modelscope"),
    ("aliyun-codingplan", "aliyun_codingplan"),
    ("aliyun-codingplan-intl", "aliyun_codingplan_intl"),
    ("zhipu-cn-codingplan", "zhipu_cn_codingplan"),
    ("zhipu-intl-codingplan", "zhipu_intl_codingplan"),
    ("volcengine-cn-codingplan", "volcengine_cn_codingplan"),
];

pub fn qwenpaw_provider_mappings() -> &'static [(&'static str, &'static str)] {
    QWENPAW_PROVIDER_MAPPING
}

#[derive(Clone, Debug, PartialEq, Eq)]
pub struct QwenPawConfig {
    pub model: String,
    pub provider_key: String,
    pub api_key: Option<String>,
    pub base_url: Option<String>,
}

pub fn load_from_qwenpaw() -> Result<Option<QwenPawConfig>, String> {
    let Some(secret_dir) = resolve_secret_dir() else {
        return Ok(None);
    };
    let Some(active) = read_active_model(&secret_dir) else {
        return Ok(None);
    };
    let Some(model) = string_field(&active, "model") else {
        return Ok(None);
    };
    let Some(provider_id) =
        string_field(&active, "provider_id").or_else(|| string_field(&active, "provider"))
    else {
        return Ok(None);
    };
    let provider_key = qwenpaw_provider_key(&provider_id)
        .ok_or_else(|| unknown_provider_message(&provider_id))?
        .to_owned();

    let provider_config = read_provider_config(&secret_dir, &provider_id);
    let api_key = provider_config
        .as_ref()
        .and_then(|config| string_field(config, "api_key"))
        .and_then(|value| decrypt_api_key(&secret_dir, &value));
    let base_url = provider_config
        .as_ref()
        .and_then(|config| string_field(config, "base_url"))
        .or_else(|| provider_descriptor(&provider_key).and_then(|descriptor| descriptor.base_url));

    Ok(Some(QwenPawConfig {
        model,
        provider_key,
        api_key,
        base_url,
    }))
}

pub fn is_qwenpaw_available() -> bool {
    resolve_secret_dir().is_some()
}

fn resolve_secret_dir() -> Option<PathBuf> {
    if let Some(path) = first_configured_env_secret_dir() {
        return path.is_dir().then_some(path);
    }

    if let Some(home) = env::var_os("HOME").map(PathBuf::from) {
        for name in [".qwenpaw.secret", ".copaw.secret"] {
            let path = home.join(name);
            if path.is_dir() {
                return Some(path);
            }
        }
    }

    let Ok(cwd) = env::current_dir() else {
        return None;
    };
    for dir in cwd.ancestors() {
        let path = dir.join(".secret");
        if path.is_dir() {
            return Some(path);
        }
    }
    None
}

fn first_configured_env_secret_dir() -> Option<PathBuf> {
    ["QWENPAW_SECRET_DIR", "COPAW_SECRET_DIR"]
        .into_iter()
        .find_map(|name| {
            env::var_os(name)
                .filter(|value| !value.is_empty())
                .map(PathBuf::from)
                .map(expand_home)
        })
}

fn read_active_model(secret_dir: &Path) -> Option<Value> {
    for path in [
        secret_dir.join("providers").join("active_model.json"),
        secret_dir.join("active_model.json"),
    ] {
        if let Some(value) = read_json_object(&path) {
            return Some(value);
        }
    }
    None
}

fn read_provider_config(secret_dir: &Path, provider_id: &str) -> Option<Value> {
    let providers_root = secret_dir.join("providers");
    for path in [
        providers_root
            .join("builtin")
            .join(format!("{provider_id}.json")),
        providers_root
            .join("custom")
            .join(format!("{provider_id}.json")),
        providers_root
            .join("plugin")
            .join(format!("{provider_id}.json")),
        providers_root.join(format!("{provider_id}.json")),
    ] {
        if let Some(value) = read_json_object(&path) {
            return Some(value);
        }
    }
    None
}

fn read_json_object(path: &Path) -> Option<Value> {
    let text = fs::read_to_string(path).ok()?;
    let value = serde_json::from_str::<Value>(&text).ok()?;
    value.is_object().then_some(value)
}

fn decrypt_api_key(secret_dir: &Path, raw_value: &str) -> Option<String> {
    if raw_value.is_empty() {
        return None;
    }
    let Some(ciphertext) = raw_value.strip_prefix(ENC_PREFIX) else {
        return Some(raw_value.to_owned());
    };
    let master_key = get_master_key(secret_dir)?;
    let raw_bytes = hex_decode(master_key.trim())?;
    if raw_bytes.len() < 32 {
        return None;
    }
    let fernet_key = URL_SAFE.encode(&raw_bytes[..32]);
    let plaintext = fernet_decrypt(&fernet_key, ciphertext).ok()?;
    String::from_utf8(plaintext).ok()
}

fn get_master_key(secret_dir: &Path) -> Option<String> {
    read_keychain_master_key().or_else(|| read_master_key_file(secret_dir))
}

fn read_master_key_file(secret_dir: &Path) -> Option<String> {
    let content = fs::read_to_string(secret_dir.join(".master_key")).ok()?;
    let content = content.trim();
    (!content.is_empty() && hex_decode(content).is_some()).then(|| content.to_owned())
}

#[cfg(target_os = "macos")]
fn read_keychain_master_key() -> Option<String> {
    for service in ["qwenpaw", "copaw"] {
        let output = Command::new("security")
            .args([
                "find-generic-password",
                "-s",
                service,
                "-a",
                "master_key",
                "-w",
            ])
            .output()
            .ok()?;
        if output.status.success() {
            let value = String::from_utf8(output.stdout).ok()?;
            let value = value.trim();
            if !value.is_empty() {
                return Some(value.to_owned());
            }
        }
    }
    None
}

#[cfg(not(target_os = "macos"))]
fn read_keychain_master_key() -> Option<String> {
    None
}

fn hex_decode(value: &str) -> Option<Vec<u8>> {
    if !value.len().is_multiple_of(2) {
        return None;
    }
    let mut output = Vec::with_capacity(value.len() / 2);
    for chunk in value.as_bytes().chunks_exact(2) {
        let hi = hex_value(chunk[0])?;
        let lo = hex_value(chunk[1])?;
        output.push((hi << 4) | lo);
    }
    Some(output)
}

fn hex_value(value: u8) -> Option<u8> {
    match value {
        b'0'..=b'9' => Some(value - b'0'),
        b'a'..=b'f' => Some(value - b'a' + 10),
        b'A'..=b'F' => Some(value - b'A' + 10),
        _ => None,
    }
}

fn string_field(value: &Value, field: &str) -> Option<String> {
    value
        .get(field)
        .and_then(Value::as_str)
        .filter(|value| !value.is_empty())
        .map(str::to_owned)
}

fn qwenpaw_provider_key(provider_id: &str) -> Option<&'static str> {
    QWENPAW_PROVIDER_MAPPING
        .iter()
        .find_map(|(qwenpaw_id, provider_key)| {
            (*qwenpaw_id == provider_id).then_some(*provider_key)
        })
}

fn unknown_provider_message(provider_id: &str) -> String {
    let mut supported_ids = QWENPAW_PROVIDER_MAPPING
        .iter()
        .map(|(qwenpaw_id, _)| *qwenpaw_id)
        .collect::<Vec<_>>();
    supported_ids.sort_unstable();
    format!(
        "[QwenPaw mode] Unknown provider '{provider_id}'. iac-code does not support this provider.\nSupported QwenPaw provider IDs: {}\nTo fix: switch to a supported provider in QwenPaw, or disable QwenPaw mode (remove 'llm_source: qwenpaw' from settings.yml).",
        supported_ids.join(", ")
    )
}

fn expand_home(path: PathBuf) -> PathBuf {
    let Some(raw) = path.to_str() else {
        return path;
    };
    if raw == "~" {
        return env::var_os("HOME").map(PathBuf::from).unwrap_or(path);
    }
    if let Some(stripped) = raw.strip_prefix("~/") {
        if let Some(home) = env::var_os("HOME").map(PathBuf::from) {
            return home.join(stripped);
        }
    }
    path
}
