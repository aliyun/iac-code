use crate::config::is_essential_traffic_only;
use crate::constants::{
    BUNDLED_SKILLS, CUSTOM_ROS_RESOURCE_PLACEHOLDER, CUSTOM_SKILL_PLACEHOLDER,
    CUSTOM_TF_PROVIDER_PLACEHOLDER, CUSTOM_TF_RESOURCE_PLACEHOLDER, KNOWN_MODELS,
    MCP_TOOL_PLACEHOLDER, OTHER_MODEL_PLACEHOLDER, ROS_ALLOWED_PREFIXES,
    TERRAFORM_OFFICIAL_PROVIDERS,
};

const MAX_ERROR_MSG_BYTES: usize = 512;
const TRUNCATION_MARKER: &str = "... (truncated)";

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum ResourceKind {
    Ros,
    Terraform,
}

pub fn sanitize_error_message(raw: Option<&str>) -> Option<String> {
    let raw = raw?;
    if is_essential_traffic_only() {
        return None;
    }
    let cleaned = collapse_control_chars(raw).trim().to_owned();
    Some(truncate_bytes_with_marker(
        &cleaned,
        MAX_ERROR_MSG_BYTES,
        TRUNCATION_MARKER,
    ))
}

pub fn sanitize_skill_name(raw: Option<&str>) -> Option<String> {
    raw.map(|value| {
        if BUNDLED_SKILLS.contains(&value) {
            value.to_owned()
        } else {
            CUSTOM_SKILL_PLACEHOLDER.to_owned()
        }
    })
}

pub fn sanitize_resource_type(raw: &str, kind: ResourceKind) -> String {
    match kind {
        ResourceKind::Ros => {
            if ROS_ALLOWED_PREFIXES
                .iter()
                .any(|prefix| raw.starts_with(prefix))
            {
                raw.to_owned()
            } else {
                CUSTOM_ROS_RESOURCE_PLACEHOLDER.to_owned()
            }
        }
        ResourceKind::Terraform => {
            let Some((provider, _resource)) = raw.split_once('_') else {
                return CUSTOM_TF_RESOURCE_PLACEHOLDER.to_owned();
            };
            if TERRAFORM_OFFICIAL_PROVIDERS.contains(&provider) {
                raw.to_owned()
            } else {
                CUSTOM_TF_RESOURCE_PLACEHOLDER.to_owned()
            }
        }
    }
}

pub fn sanitize_terraform_provider(raw: &str) -> String {
    if TERRAFORM_OFFICIAL_PROVIDERS.contains(&raw) {
        raw.to_owned()
    } else {
        CUSTOM_TF_PROVIDER_PLACEHOLDER.to_owned()
    }
}

pub fn sanitize_model_name(raw: &str) -> String {
    let base = trim_dev_version_suffix(raw);
    if KNOWN_MODELS.contains(&base) {
        base.to_owned()
    } else {
        OTHER_MODEL_PLACEHOLDER.to_owned()
    }
}

pub fn sanitize_tool_name(raw: &str) -> String {
    if raw.starts_with("mcp__") {
        MCP_TOOL_PLACEHOLDER.to_owned()
    } else {
        raw.to_owned()
    }
}

pub fn bucket_resource_count(n: i64) -> &'static str {
    if n <= 5 {
        "1-5"
    } else if n <= 20 {
        "6-20"
    } else if n <= 50 {
        "21-50"
    } else {
        "50+"
    }
}

fn collapse_control_chars(raw: &str) -> String {
    let mut output = String::new();
    let mut in_control = false;
    for character in raw.chars() {
        if matches!(character, '\n' | '\r' | '\t') || character <= '\u{1f}' {
            if !in_control {
                output.push(' ');
                in_control = true;
            }
            continue;
        }
        output.push(character);
        in_control = false;
    }
    output
}

fn truncate_bytes_with_marker(value: &str, max_bytes: usize, marker: &str) -> String {
    if value.len() <= max_bytes {
        return value.to_owned();
    }
    let keep = max_bytes.saturating_sub(marker.len());
    let mut end = 0;
    for (index, character) in value.char_indices() {
        let next = index + character.len_utf8();
        if next > keep {
            break;
        }
        end = next;
    }
    format!("{}{}", &value[..end], marker)
}

fn trim_dev_version_suffix(raw: &str) -> &str {
    let Some((base, suffix)) = raw.rsplit_once('-') else {
        return raw;
    };
    if suffix.len() == 8 && suffix.chars().all(|character| character.is_ascii_digit()) {
        base
    } else {
        raw
    }
}
