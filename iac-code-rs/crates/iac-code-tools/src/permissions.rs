use std::collections::BTreeMap;

use iac_code_protocol::json::JsonValue;
use iac_code_protocol::permission::{
    PermissionDecisionReason, PermissionMode, PermissionResult, ToolPermissionContext,
};

use crate::Tool;

const STICKY_ASK_REASONS: &[&str] = &[
    "safety_check",
    "path_constraint",
    "dangerous_readonly_argument",
    "complex_command",
    "parse_error",
    "too_complex",
];

pub fn check_tool_permission(
    tool: &dyn Tool,
    input: &JsonValue,
    context: &ToolPermissionContext,
) -> PermissionResult {
    let tool_level_ask = get_tool_rule(tool.name(), &context.ask_rules).is_some();

    if get_tool_rule(tool.name(), &context.deny_rules).is_some() {
        return PermissionResult::deny();
    }

    let mut result = tool.check_permissions(input, context);

    if result.behavior == "deny" {
        return result;
    }

    if is_safety_check_ask(&result) {
        return result;
    }

    if result.behavior == "ask" && tool_level_ask {
        return result;
    }

    if result.behavior == "allow" && tool_level_ask {
        let detail = format!("matched ask rule(s): {}", tool.name());
        return PermissionResult {
            behavior: "ask".into(),
            message: detail.clone(),
            reason: Some(PermissionDecisionReason {
                type_name: "rule".into(),
                detail,
            }),
            suggestions: None,
        };
    }

    if context.mode == PermissionMode::BypassPermissions && !is_safety_check_ask(&result) {
        return PermissionResult::allow();
    }

    if is_sticky_ask(&result) {
        return result;
    }

    if get_tool_rule(tool.name(), &context.allow_rules).is_some()
        && matches!(result.behavior.as_str(), "passthrough" | "ask")
        && tool.supports_blanket_allow()
    {
        return PermissionResult::allow();
    }

    if result.behavior == "passthrough" {
        result = PermissionResult {
            behavior: "ask".into(),
            message: format!("Allow {}?", tool.user_facing_name(input)),
            reason: None,
            suggestions: result.suggestions,
        };
    }

    if context.mode == PermissionMode::DontAsk && result.behavior == "ask" {
        return PermissionResult::deny();
    }

    result
}

fn get_tool_rule<'a>(
    tool_name: &str,
    rules_by_source: &'a BTreeMap<String, Vec<String>>,
) -> Option<&'a str> {
    rules_by_source.iter().find_map(|(source, rules)| {
        rules
            .iter()
            .any(|rule| rule == tool_name)
            .then_some(source.as_str())
    })
}

fn is_safety_check_ask(result: &PermissionResult) -> bool {
    result.behavior == "ask" && reason_type(result) == Some("safety_check")
}

fn is_sticky_ask(result: &PermissionResult) -> bool {
    result.behavior == "ask"
        && reason_type(result).is_some_and(|reason| STICKY_ASK_REASONS.contains(&reason))
}

fn reason_type(result: &PermissionResult) -> Option<&str> {
    result
        .reason
        .as_ref()
        .map(|reason| reason.type_name.as_str())
}
