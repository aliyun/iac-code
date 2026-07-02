use std::collections::BTreeMap;

use crate::json::{self, JsonValue};
use crate::ToJsonValue;

#[derive(Clone, Debug, PartialEq, Eq)]
pub enum PermissionMode {
    Default,
    AcceptEdits,
    BypassPermissions,
    DontAsk,
}

impl PermissionMode {
    pub fn as_str(&self) -> &'static str {
        match self {
            PermissionMode::Default => "default",
            PermissionMode::AcceptEdits => "accept_edits",
            PermissionMode::BypassPermissions => "bypass_permissions",
            PermissionMode::DontAsk => "dont_ask",
        }
    }
}

#[derive(Clone, Debug, PartialEq, Eq)]
pub enum PermissionRuleSource {
    UserSettings,
    ProjectSettings,
    LocalSettings,
    CliArg,
    Session,
}

impl PermissionRuleSource {
    pub fn as_str(&self) -> &'static str {
        match self {
            PermissionRuleSource::UserSettings => "user_settings",
            PermissionRuleSource::ProjectSettings => "project_settings",
            PermissionRuleSource::LocalSettings => "local_settings",
            PermissionRuleSource::CliArg => "cli_arg",
            PermissionRuleSource::Session => "session",
        }
    }
}

#[derive(Clone, Debug, PartialEq, Eq)]
pub struct PermissionRuleValue {
    pub tool_name: String,
    pub rule_content: String,
}

impl ToJsonValue for PermissionRuleValue {
    fn to_json_value(&self) -> JsonValue {
        json::object([
            ("tool_name", json::string(&self.tool_name)),
            ("rule_content", json::string(&self.rule_content)),
        ])
    }
}

#[derive(Clone, Debug, PartialEq, Eq)]
pub struct PermissionRule {
    pub source: PermissionRuleSource,
    pub behavior: String,
    pub value: PermissionRuleValue,
}

impl ToJsonValue for PermissionRule {
    fn to_json_value(&self) -> JsonValue {
        json::object([
            ("source", json::string(self.source.as_str())),
            ("behavior", json::string(&self.behavior)),
            ("value", self.value.to_json_value()),
        ])
    }
}

#[derive(Clone, Debug, PartialEq, Eq)]
pub struct PermissionDecisionReason {
    pub type_name: String,
    pub detail: String,
}

impl ToJsonValue for PermissionDecisionReason {
    fn to_json_value(&self) -> JsonValue {
        json::object([
            ("type", json::string(&self.type_name)),
            ("detail", json::string(&self.detail)),
        ])
    }
}

#[derive(Clone, Debug, PartialEq, Eq)]
pub struct PermissionResult {
    pub behavior: String,
    pub message: String,
    pub reason: Option<PermissionDecisionReason>,
    pub suggestions: Option<Vec<PermissionRuleValue>>,
}

impl PermissionResult {
    pub fn allow() -> Self {
        Self {
            behavior: "allow".into(),
            message: String::new(),
            reason: None,
            suggestions: None,
        }
    }

    pub fn deny() -> Self {
        Self {
            behavior: "deny".into(),
            message: String::new(),
            reason: None,
            suggestions: None,
        }
    }

    pub fn ask(message: impl Into<String>) -> Self {
        Self {
            behavior: "ask".into(),
            message: message.into(),
            reason: None,
            suggestions: None,
        }
    }

    pub fn passthrough() -> Self {
        Self {
            behavior: "passthrough".into(),
            message: String::new(),
            reason: None,
            suggestions: None,
        }
    }
}

impl ToJsonValue for PermissionResult {
    fn to_json_value(&self) -> JsonValue {
        json::object([
            ("behavior", json::string(&self.behavior)),
            ("message", json::string(&self.message)),
            (
                "reason",
                self.reason
                    .as_ref()
                    .map_or_else(json::null, PermissionDecisionReason::to_json_value),
            ),
            (
                "suggestions",
                self.suggestions
                    .as_ref()
                    .map_or_else(json::null, |suggestions| {
                        json::array(suggestions.iter().map(PermissionRuleValue::to_json_value))
                    }),
            ),
        ])
    }
}

#[derive(Clone, Debug, PartialEq, Eq)]
pub struct ToolPermissionContext {
    pub mode: PermissionMode,
    pub cwd: String,
    pub allow_rules: BTreeMap<String, Vec<String>>,
    pub deny_rules: BTreeMap<String, Vec<String>>,
    pub ask_rules: BTreeMap<String, Vec<String>>,
    pub additional_directories: Vec<String>,
    pub trusted_read_directories: Vec<String>,
}

impl Default for ToolPermissionContext {
    fn default() -> Self {
        Self {
            mode: PermissionMode::Default,
            cwd: String::new(),
            allow_rules: BTreeMap::new(),
            deny_rules: BTreeMap::new(),
            ask_rules: BTreeMap::new(),
            additional_directories: Vec::new(),
            trusted_read_directories: Vec::new(),
        }
    }
}

impl ToJsonValue for ToolPermissionContext {
    fn to_json_value(&self) -> JsonValue {
        json::object([
            ("mode", json::string(self.mode.as_str())),
            ("cwd", json::string(&self.cwd)),
            ("allow_rules", string_list_map(&self.allow_rules)),
            ("deny_rules", string_list_map(&self.deny_rules)),
            ("ask_rules", string_list_map(&self.ask_rules)),
            (
                "additional_directories",
                json::array(self.additional_directories.iter().map(json::string)),
            ),
            (
                "trusted_read_directories",
                json::array(self.trusted_read_directories.iter().map(json::string)),
            ),
        ])
    }
}

fn string_list_map(values: &BTreeMap<String, Vec<String>>) -> JsonValue {
    json::object(values.iter().map(|(key, value)| {
        (
            key.as_str(),
            json::array(value.iter().map(|item| json::string(item.as_str()))),
        )
    }))
}
