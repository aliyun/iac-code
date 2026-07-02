use std::collections::BTreeMap;

use iac_code_protocol::json::JsonValue;
use iac_code_protocol::permission::PermissionRuleValue;

pub const OPTION_ALLOW_ONCE: &str = "allow_once";
pub const OPTION_ALLOW_ALWAYS: &str = "allow_always";
pub const OPTION_REJECT_ONCE: &str = "reject_once";
pub const OPTION_REJECT_ALWAYS: &str = "reject_always";
pub const PREFIX_ALLOW_RULE: &str = "allow_rule:";
pub const PREFIX_DENY_RULE: &str = "deny_rule:";

#[derive(Clone, Debug, PartialEq, Eq)]
pub struct PermissionOption {
    pub option_id: String,
    pub name: String,
    pub kind: String,
}

impl PermissionOption {
    pub fn new(
        option_id: impl Into<String>,
        name: impl Into<String>,
        kind: impl Into<String>,
    ) -> Self {
        Self {
            option_id: option_id.into(),
            name: name.into(),
            kind: kind.into(),
        }
    }
}

#[derive(Clone, Debug, PartialEq, Eq)]
pub struct PermissionToolCall {
    pub tool_call_id: String,
    pub title: String,
    pub content: Vec<String>,
}

#[derive(Clone, Debug, PartialEq, Eq)]
pub enum PermissionOutcome {
    Allowed { option_id: Option<String> },
    Denied { option_id: Option<String> },
    Cancelled,
}

#[derive(Clone, Debug, PartialEq, Eq)]
pub struct PermissionResponse {
    pub outcome: PermissionOutcome,
    pub field_meta: BTreeMap<String, JsonValue>,
}

impl PermissionResponse {
    pub fn selected_option_id(&self) -> Option<&str> {
        match &self.outcome {
            PermissionOutcome::Allowed { option_id } | PermissionOutcome::Denied { option_id } => {
                option_id.as_deref()
            }
            PermissionOutcome::Cancelled => None,
        }
        .or_else(|| match self.field_meta.get("option_id") {
            Some(JsonValue::String(value)) => Some(value.as_str()),
            _ => None,
        })
    }
}

pub fn extract_permission_suggestions(value: Option<&JsonValue>) -> Vec<PermissionRuleValue> {
    let Some(JsonValue::Object(object)) = value else {
        return Vec::new();
    };
    let Some(JsonValue::Array(suggestions)) = object.get("suggestions") else {
        return Vec::new();
    };

    suggestions
        .iter()
        .filter_map(|suggestion| {
            let JsonValue::Object(suggestion) = suggestion else {
                return None;
            };
            let (Some(JsonValue::String(tool_name)), Some(JsonValue::String(rule_content))) =
                (suggestion.get("tool_name"), suggestion.get("rule_content"))
            else {
                return None;
            };
            Some(PermissionRuleValue {
                tool_name: tool_name.clone(),
                rule_content: rule_content.clone(),
            })
        })
        .collect()
}
