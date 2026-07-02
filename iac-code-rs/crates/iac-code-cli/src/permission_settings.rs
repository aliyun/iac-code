use std::collections::BTreeMap;
use std::fs;
use std::path::Path;

use iac_code_config::paths::ConfigPaths;
use iac_code_protocol::permission::{PermissionMode, ToolPermissionContext};

use crate::cli_args::split_tool_rules;
use crate::yaml_config::{yaml_mapping_get, yaml_string_list};

#[derive(Clone, Debug, Default, PartialEq, Eq)]
pub(super) struct PermissionSettings {
    pub(super) allow: Vec<String>,
    pub(super) deny: Vec<String>,
    pub(super) ask: Vec<String>,
    pub(super) mode: Option<PermissionMode>,
    pub(super) additional_directories: Vec<String>,
}

pub(super) fn load_tool_permission_context(
    paths: &ConfigPaths,
    allowed_tools: &str,
    disallowed_tools: &str,
    permission_mode: &str,
    cwd: &str,
) -> Result<ToolPermissionContext, String> {
    let mut allow_rules = BTreeMap::new();
    let mut deny_rules = BTreeMap::new();
    let mut ask_rules = BTreeMap::new();
    let mut additional_directories = Vec::new();
    let mut mode = PermissionMode::Default;

    let cwd_path = Path::new(cwd);
    let project_settings_path = cwd_path.join(".iac-code").join("settings.yml");
    let local_settings_path = cwd_path.join(".iac-code").join("settings.local.yml");
    for (source_key, path) in [
        ("user_settings", paths.settings_path.as_path()),
        ("project_settings", project_settings_path.as_path()),
        ("local_settings", local_settings_path.as_path()),
    ] {
        let settings = load_permission_settings(path);
        if !settings.allow.is_empty() {
            allow_rules.insert(source_key.to_owned(), settings.allow);
        }
        if !settings.deny.is_empty() {
            deny_rules.insert(source_key.to_owned(), settings.deny);
        }
        if !settings.ask.is_empty() {
            ask_rules.insert(source_key.to_owned(), settings.ask);
        }
        additional_directories.extend(settings.additional_directories);
        if let Some(layer_mode) = settings.mode {
            mode = layer_mode;
        }
    }

    let cli_allowed = split_tool_rules(allowed_tools);
    if !cli_allowed.is_empty() {
        allow_rules.insert("cli_arg".to_owned(), cli_allowed);
    }
    let cli_disallowed = split_tool_rules(disallowed_tools);
    if !cli_disallowed.is_empty() {
        deny_rules.insert("cli_arg".to_owned(), cli_disallowed);
    }
    if !permission_mode.is_empty() {
        mode = parse_permission_mode(permission_mode);
    }

    Ok(ToolPermissionContext {
        mode,
        cwd: cwd.to_owned(),
        allow_rules,
        deny_rules,
        ask_rules,
        additional_directories,
        trusted_read_directories: Vec::new(),
    })
}

pub(super) fn session_trusted_read_directories(
    paths: &ConfigPaths,
    session_id: Option<&str>,
) -> Result<Vec<String>, String> {
    let Some(session_id) = session_id else {
        return Ok(Vec::new());
    };
    if session_id.is_empty() {
        return Ok(Vec::new());
    }
    validate_session_id_for_trusted_read_directories(session_id)?;

    let subdirs = paths.subdirs();
    Ok(vec![
        subdirs
            .tool_results
            .join(session_id)
            .to_string_lossy()
            .into_owned(),
        subdirs
            .image_cache
            .join(session_id)
            .to_string_lossy()
            .into_owned(),
    ])
}

pub(super) fn validate_session_id_for_trusted_read_directories(
    session_id: &str,
) -> Result<(), String> {
    if matches!(session_id, "." | "..") || session_id.contains('/') || session_id.contains('\\') {
        return Err(format!(
            "Invalid session id for trusted read directories: {session_id}"
        ));
    }
    Ok(())
}

fn load_permission_settings(path: &Path) -> PermissionSettings {
    let Ok(content) = fs::read_to_string(path) else {
        return PermissionSettings::default();
    };
    parse_permission_settings(&content)
}

pub(super) fn parse_permission_settings(content: &str) -> PermissionSettings {
    let Ok(value) = serde_yaml::from_str::<serde_yaml::Value>(content) else {
        return PermissionSettings::default();
    };
    let Some(root) = value.as_mapping() else {
        return PermissionSettings::default();
    };
    let Some(permissions) =
        yaml_mapping_get(root, "permissions").and_then(serde_yaml::Value::as_mapping)
    else {
        return PermissionSettings::default();
    };

    PermissionSettings {
        allow: yaml_string_list(permissions, "allow"),
        deny: yaml_string_list(permissions, "deny"),
        ask: yaml_string_list(permissions, "ask"),
        mode: yaml_mapping_get(permissions, "mode")
            .and_then(serde_yaml::Value::as_str)
            .map(str::trim)
            .filter(|value| !value.is_empty())
            .and_then(permission_mode_from_str),
        additional_directories: yaml_string_list(permissions, "additional_directories"),
    }
}

fn parse_permission_mode(value: &str) -> PermissionMode {
    permission_mode_from_str(value).unwrap_or(PermissionMode::Default)
}

fn permission_mode_from_str(value: &str) -> Option<PermissionMode> {
    match value {
        "default" => Some(PermissionMode::Default),
        "accept_edits" => Some(PermissionMode::AcceptEdits),
        "bypass_permissions" => Some(PermissionMode::BypassPermissions),
        "dont_ask" => Some(PermissionMode::DontAsk),
        _ => None,
    }
}
