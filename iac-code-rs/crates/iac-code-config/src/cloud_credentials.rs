use std::collections::BTreeMap;
use std::fs;
use std::path::{Path, PathBuf};

use crate::paths::ConfigPaths;
use crate::simple_yaml::{self, YamlValue};
use crate::{ConfigError, ConfigResult};

pub const DEFAULT_REGION: &str = "cn-hangzhou";

const CREDENTIAL_MODES: &[&str] = &["AK", "StsToken", "RamRoleArn", "OAuth"];
const DEFAULT_ALIYUN_CLI_CONFIG_PATH: &str = ".aliyun/config.json";

#[derive(Clone, Debug, PartialEq, Eq)]
pub struct AliyunCredential {
    pub mode: String,
    pub access_key_id: String,
    pub access_key_secret: String,
    pub region_id: String,
    pub sts_token: String,
    pub sts_expiration: i64,
    pub ram_role_arn: String,
    pub ram_session_name: String,
    pub oauth_site_type: String,
    pub oauth_access_token: String,
    pub oauth_refresh_token: String,
    pub oauth_access_token_expire: i64,
    pub oauth_refresh_token_expire: i64,
}

impl Default for AliyunCredential {
    fn default() -> Self {
        Self {
            mode: "AK".into(),
            access_key_id: String::new(),
            access_key_secret: String::new(),
            region_id: DEFAULT_REGION.into(),
            sts_token: String::new(),
            sts_expiration: 0,
            ram_role_arn: String::new(),
            ram_session_name: String::new(),
            oauth_site_type: String::new(),
            oauth_access_token: String::new(),
            oauth_refresh_token: String::new(),
            oauth_access_token_expire: 0,
            oauth_refresh_token_expire: 0,
        }
    }
}

pub fn has_aliyun_provider(
    paths: &ConfigPaths,
    aliyun_cli_config_path: Option<&Path>,
) -> ConfigResult<bool> {
    Ok(load_aliyun_credentials(paths, aliyun_cli_config_path)?.is_some())
}

pub fn load_aliyun_credentials(
    paths: &ConfigPaths,
    aliyun_cli_config_path: Option<&Path>,
) -> ConfigResult<Option<AliyunCredential>> {
    if let Some(credential) = load_from_env(paths, aliyun_cli_config_path)? {
        return Ok(Some(credential));
    }

    if aliyun_cli_config_path.is_none() {
        if let Some(credential) = load_from_iac_code_config(&paths.cloud_credentials_path)? {
            return Ok(Some(credential));
        }
    }

    Ok(load_from_aliyun_cli(aliyun_cli_config_path))
}

pub fn load_aliyun_credentials_from_iac_code_config(
    path: &Path,
) -> ConfigResult<Option<AliyunCredential>> {
    load_from_iac_code_config(path)
}

pub fn save_aliyun_credentials(path: &Path, credential: &AliyunCredential) -> ConfigResult<()> {
    let mut cloud_credentials = simple_yaml::load_yaml_map(path).map_err(ConfigError::from)?;
    let mut aliyun_data = BTreeMap::new();
    insert_string(&mut aliyun_data, "mode", &credential.mode);
    insert_string(&mut aliyun_data, "region_id", &credential.region_id);

    match credential.mode.as_str() {
        "AK" => {
            insert_non_empty(&mut aliyun_data, "access_key_id", &credential.access_key_id);
            insert_non_empty(
                &mut aliyun_data,
                "access_key_secret",
                &credential.access_key_secret,
            );
        }
        "StsToken" => {
            insert_non_empty(&mut aliyun_data, "access_key_id", &credential.access_key_id);
            insert_non_empty(
                &mut aliyun_data,
                "access_key_secret",
                &credential.access_key_secret,
            );
            insert_non_empty(&mut aliyun_data, "sts_token", &credential.sts_token);
        }
        "RamRoleArn" => {
            insert_non_empty(&mut aliyun_data, "access_key_id", &credential.access_key_id);
            insert_non_empty(
                &mut aliyun_data,
                "access_key_secret",
                &credential.access_key_secret,
            );
            insert_non_empty(&mut aliyun_data, "ram_role_arn", &credential.ram_role_arn);
            insert_non_empty(
                &mut aliyun_data,
                "ram_session_name",
                &credential.ram_session_name,
            );
        }
        "OAuth" => {
            insert_non_empty(
                &mut aliyun_data,
                "oauth_site_type",
                &credential.oauth_site_type,
            );
            insert_non_empty(
                &mut aliyun_data,
                "oauth_access_token",
                &credential.oauth_access_token,
            );
            insert_non_empty(
                &mut aliyun_data,
                "oauth_refresh_token",
                &credential.oauth_refresh_token,
            );
            insert_non_zero(
                &mut aliyun_data,
                "oauth_access_token_expire",
                credential.oauth_access_token_expire,
            );
            insert_non_zero(
                &mut aliyun_data,
                "oauth_refresh_token_expire",
                credential.oauth_refresh_token_expire,
            );
            insert_non_empty(&mut aliyun_data, "access_key_id", &credential.access_key_id);
            insert_non_empty(
                &mut aliyun_data,
                "access_key_secret",
                &credential.access_key_secret,
            );
            insert_non_empty(&mut aliyun_data, "sts_token", &credential.sts_token);
            insert_non_zero(
                &mut aliyun_data,
                "sts_expiration",
                credential.sts_expiration,
            );
        }
        _ => {}
    }

    cloud_credentials.insert("aliyun".into(), YamlValue::Map(aliyun_data));
    simple_yaml::save_yaml_map(path, &cloud_credentials).map_err(ConfigError::from)
}

fn load_from_env(
    paths: &ConfigPaths,
    aliyun_cli_config_path: Option<&Path>,
) -> ConfigResult<Option<AliyunCredential>> {
    let access_key_id = std::env::var("ALIBABA_CLOUD_ACCESS_KEY_ID").unwrap_or_default();
    let access_key_secret = std::env::var("ALIBABA_CLOUD_ACCESS_KEY_SECRET").unwrap_or_default();
    if access_key_id.is_empty() || access_key_secret.is_empty() {
        return Ok(None);
    }

    let sts_token = std::env::var("ALIBABA_CLOUD_SECURITY_TOKEN").unwrap_or_default();
    let region_id = std::env::var("ALIBABA_CLOUD_REGION_ID")
        .ok()
        .filter(|value| !value.is_empty())
        .or_else(|| {
            load_from_iac_code_config(&paths.cloud_credentials_path)
                .ok()
                .flatten()
                .map(|credential| credential.region_id)
        })
        .or_else(|| {
            load_from_aliyun_cli(aliyun_cli_config_path).and_then(|credential| {
                (!credential.region_id.is_empty()).then_some(credential.region_id)
            })
        })
        .unwrap_or_else(|| DEFAULT_REGION.into());

    Ok(Some(AliyunCredential {
        mode: if sts_token.is_empty() {
            "AK".into()
        } else {
            "StsToken".into()
        },
        access_key_id,
        access_key_secret,
        region_id,
        sts_token,
        ..AliyunCredential::default()
    }))
}

fn load_from_iac_code_config(path: &Path) -> ConfigResult<Option<AliyunCredential>> {
    let raw = simple_yaml::load_yaml_map(path).map_err(ConfigError::from)?;
    let Some(aliyun_data) = raw.get("aliyun").and_then(YamlValue::as_map) else {
        return Ok(None);
    };

    let mode = yaml_string(aliyun_data, "mode").unwrap_or_else(|| "AK".into());
    if !CREDENTIAL_MODES.contains(&mode.as_str()) {
        return Ok(None);
    }

    Ok(Some(AliyunCredential {
        mode,
        access_key_id: yaml_string(aliyun_data, "access_key_id").unwrap_or_default(),
        access_key_secret: yaml_string(aliyun_data, "access_key_secret").unwrap_or_default(),
        region_id: yaml_string(aliyun_data, "region_id").unwrap_or_else(|| DEFAULT_REGION.into()),
        sts_token: yaml_string(aliyun_data, "sts_token").unwrap_or_default(),
        sts_expiration: yaml_i64(aliyun_data, "sts_expiration")?,
        ram_role_arn: yaml_string(aliyun_data, "ram_role_arn").unwrap_or_default(),
        ram_session_name: yaml_string(aliyun_data, "ram_session_name").unwrap_or_default(),
        oauth_site_type: yaml_string(aliyun_data, "oauth_site_type").unwrap_or_default(),
        oauth_access_token: yaml_string(aliyun_data, "oauth_access_token").unwrap_or_default(),
        oauth_refresh_token: yaml_string(aliyun_data, "oauth_refresh_token").unwrap_or_default(),
        oauth_access_token_expire: yaml_i64(aliyun_data, "oauth_access_token_expire")?,
        oauth_refresh_token_expire: yaml_i64(aliyun_data, "oauth_refresh_token_expire")?,
    }))
}

fn load_from_aliyun_cli(config_path: Option<&Path>) -> Option<AliyunCredential> {
    let path = config_path
        .map(Path::to_path_buf)
        .unwrap_or_else(default_aliyun_cli_config_path);
    let text = fs::read_to_string(path).ok()?;
    let value: serde_json::Value = serde_json::from_str(&text).ok()?;
    let profiles = value.get("profiles")?.as_array()?;
    let profile = profiles.iter().find(|profile| {
        profile
            .get("name")
            .and_then(serde_json::Value::as_str)
            .is_some_and(|name| name == "default")
    })?;

    let sts_expiration = json_i64(profile, "sts_expiration")?;
    let oauth_access_token_expire = json_i64(profile, "oauth_access_token_expire")?;
    let oauth_refresh_token_expire = json_i64(profile, "oauth_refresh_token_expire")?;

    Some(AliyunCredential {
        mode: json_string(profile, "mode").unwrap_or_else(|| "AK".into()),
        access_key_id: json_string(profile, "access_key_id").unwrap_or_default(),
        access_key_secret: json_string(profile, "access_key_secret").unwrap_or_default(),
        region_id: json_string(profile, "region_id").unwrap_or_else(|| DEFAULT_REGION.into()),
        sts_token: json_string(profile, "sts_token").unwrap_or_default(),
        sts_expiration,
        ram_role_arn: json_string(profile, "ram_role_arn").unwrap_or_default(),
        ram_session_name: json_string(profile, "ram_session_name").unwrap_or_default(),
        oauth_site_type: json_string(profile, "oauth_site_type").unwrap_or_default(),
        oauth_access_token: json_string(profile, "oauth_access_token").unwrap_or_default(),
        oauth_refresh_token: json_string(profile, "oauth_refresh_token").unwrap_or_default(),
        oauth_access_token_expire,
        oauth_refresh_token_expire,
    })
}

fn insert_string(values: &mut BTreeMap<String, YamlValue>, field: &str, value: &str) {
    values.insert(field.to_owned(), YamlValue::String(value.to_owned()));
}

fn insert_non_empty(values: &mut BTreeMap<String, YamlValue>, field: &str, value: &str) {
    if !value.is_empty() {
        insert_string(values, field, value);
    }
}

fn insert_non_zero(values: &mut BTreeMap<String, YamlValue>, field: &str, value: i64) {
    if value != 0 {
        insert_string(values, field, &value.to_string());
    }
}

fn yaml_string(
    values: &std::collections::BTreeMap<String, YamlValue>,
    field: &str,
) -> Option<String> {
    values
        .get(field)
        .and_then(YamlValue::as_str)
        .map(str::to_owned)
}

fn yaml_i64(
    values: &std::collections::BTreeMap<String, YamlValue>,
    field: &str,
) -> ConfigResult<i64> {
    let Some(value) = yaml_string(values, field) else {
        return Ok(0);
    };
    if value.is_empty() {
        return Ok(0);
    }
    value
        .parse::<i64>()
        .map_err(|_| ConfigError::InvalidValue(format!("invalid integer for {field}: {value}")))
}

fn json_string(value: &serde_json::Value, field: &str) -> Option<String> {
    value
        .get(field)
        .and_then(serde_json::Value::as_str)
        .map(str::to_owned)
}

fn json_i64(value: &serde_json::Value, field: &str) -> Option<i64> {
    let Some(value) = value.get(field) else {
        return Some(0);
    };

    if value.is_null() {
        return Some(0);
    }

    if let Some(number) = value.as_i64() {
        return Some(number);
    }

    if let Some(text) = value.as_str() {
        if text.is_empty() {
            return Some(0);
        }
        return text.parse::<i64>().ok();
    }

    None
}

fn default_aliyun_cli_config_path() -> PathBuf {
    std::env::var_os("HOME")
        .map(PathBuf::from)
        .unwrap_or_else(|| PathBuf::from("."))
        .join(DEFAULT_ALIYUN_CLI_CONFIG_PATH)
}
