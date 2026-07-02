use std::collections::BTreeSet;
use std::fs;
use std::io::ErrorKind;

use crate::file_security::write_private_file;
use crate::paths::ConfigPaths;
use crate::{ConfigError, ConfigResult};

use super::yaml_edit::{quote_yaml_scalar, remove_root_yaml_block};

pub fn normalize_skill_name(name: &str) -> String {
    name.trim_start_matches(['/', '$'])
        .trim()
        .to_ascii_lowercase()
}

pub fn load_disabled_skills(paths: &ConfigPaths) -> ConfigResult<BTreeSet<String>> {
    match fs::read_to_string(&paths.settings_path) {
        Ok(content) => Ok(parse_disabled_skills(&content)),
        Err(error) if error.kind() == ErrorKind::NotFound => Ok(BTreeSet::new()),
        Err(error) => Err(ConfigError::from(error)),
    }
}

pub fn save_disabled_skills<I, J, S, T>(
    paths: &ConfigPaths,
    disabled: I,
    locked_skill_names: J,
) -> ConfigResult<()>
where
    I: IntoIterator<Item = S>,
    J: IntoIterator<Item = T>,
    S: AsRef<str>,
    T: AsRef<str>,
{
    let locked = locked_skill_names
        .into_iter()
        .map(|name| normalize_skill_name(name.as_ref()))
        .collect::<BTreeSet<_>>();
    let normalized = disabled
        .into_iter()
        .map(|name| normalize_skill_name(name.as_ref()))
        .filter(|name| !name.is_empty() && !locked.contains(name))
        .collect::<BTreeSet<_>>();

    let existing = match fs::read_to_string(&paths.settings_path) {
        Ok(content) => content,
        Err(error) if error.kind() == ErrorKind::NotFound => String::new(),
        Err(error) => return Err(ConfigError::from(error)),
    };
    let mut content = remove_root_yaml_block(&existing, "disabled_skills");
    if !normalized.is_empty() {
        if !content.is_empty() && !content.ends_with('\n') {
            content.push('\n');
        }
        content.push_str("disabled_skills:\n");
        for name in normalized {
            content.push_str("- ");
            content.push_str(&quote_yaml_scalar(&name));
            content.push('\n');
        }
    }

    write_private_file(&paths.settings_path, content).map_err(ConfigError::from)
}

fn parse_disabled_skills(content: &str) -> BTreeSet<String> {
    let mut disabled = BTreeSet::new();
    let mut in_block = false;
    let mut block_indent = 0_usize;

    for line in content.lines() {
        let trimmed_end = line.trim_end();
        let trimmed_start = trimmed_end.trim_start();
        if trimmed_start.is_empty() || trimmed_start.starts_with('#') {
            continue;
        }
        let indent = trimmed_end.len() - trimmed_start.len();

        if !in_block {
            let Some((key, value)) = trimmed_start.split_once(':') else {
                continue;
            };
            if indent == 0 && key.trim() == "disabled_skills" {
                if let Some(items) = parse_inline_yaml_string_list(value.trim()) {
                    disabled.extend(items.into_iter().map(|item| normalize_skill_name(&item)));
                    return disabled
                        .into_iter()
                        .filter(|name| !name.is_empty())
                        .collect();
                }
                in_block = value.trim().is_empty();
                block_indent = indent;
            }
            continue;
        }

        if indent <= block_indent && !trimmed_start.starts_with('-') {
            break;
        }
        let item = trimmed_start.strip_prefix('-').map(str::trim);
        if let Some(value) = item.and_then(parse_yaml_string_scalar) {
            let name = normalize_skill_name(&value);
            if !name.is_empty() {
                disabled.insert(name);
            }
        }
    }

    disabled
}

fn parse_inline_yaml_string_list(value: &str) -> Option<Vec<String>> {
    let trimmed = value.trim();
    if trimmed.is_empty() {
        return None;
    }
    if !(trimmed.starts_with('[') && trimmed.ends_with(']')) {
        return Some(Vec::new());
    }
    Some(
        trimmed[1..trimmed.len() - 1]
            .split(',')
            .filter_map(|item| parse_yaml_string_scalar(item.trim()))
            .collect(),
    )
}

fn parse_yaml_string_scalar(value: &str) -> Option<String> {
    let trimmed = value.trim();
    if trimmed.is_empty() {
        return Some(String::new());
    }
    if is_quoted_yaml_scalar(trimmed) {
        return Some(unquote_yaml(trimmed));
    }
    let lower = trimmed.to_ascii_lowercase();
    if matches!(lower.as_str(), "true" | "false" | "null" | "~")
        || trimmed.parse::<i64>().is_ok()
        || trimmed.parse::<f64>().is_ok()
        || trimmed.starts_with('[')
        || trimmed.starts_with('{')
    {
        return None;
    }
    Some(trimmed.to_owned())
}

fn is_quoted_yaml_scalar(value: &str) -> bool {
    let bytes = value.as_bytes();
    bytes.len() >= 2
        && ((bytes[0] == b'"' && bytes[bytes.len() - 1] == b'"')
            || (bytes[0] == b'\'' && bytes[bytes.len() - 1] == b'\''))
}

fn unquote_yaml(value: &str) -> String {
    if is_quoted_yaml_scalar(value) {
        return value[1..value.len() - 1]
            .replace("\\\"", "\"")
            .replace("\\\\", "\\");
    }
    value.to_owned()
}
