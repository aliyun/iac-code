use std::collections::BTreeSet;
use std::path::Path;

use iac_code_protocol::permission::{PermissionResult, PermissionRuleValue};

use super::parser::ParsedCommand;
use super::rules::normalize_command;

const DANGEROUS_BUILTINS: &[&str] = &["eval", "exec", "source", "."];

pub(super) fn with_suggestions_if_needed(
    mut result: PermissionResult,
    command: &str,
    commands: Option<&[ParsedCommand]>,
    sub_results: Option<&[PermissionResult]>,
) -> PermissionResult {
    if result.suggestions.is_some()
        || result
            .reason
            .as_ref()
            .is_some_and(|reason| reason.type_name == "dangerous_readonly_argument")
    {
        return result;
    }
    let suggestions = commands.map_or_else(
        || generate_suggestions_from_text(command),
        |commands| generate_suggestions(commands, sub_results),
    );
    if !suggestions.is_empty() {
        result.suggestions = Some(suggestions);
    }
    result
}

pub(super) fn merge_permission_results(results: &[PermissionResult]) -> PermissionResult {
    results
        .iter()
        .enumerate()
        .min_by_key(|(index, result)| (behavior_order(&result.behavior), *index))
        .map(|(_, result)| result.clone())
        .unwrap_or_else(PermissionResult::passthrough)
}

fn generate_suggestions(
    commands: &[ParsedCommand],
    sub_results: Option<&[PermissionResult]>,
) -> Vec<PermissionRuleValue> {
    let mut seen = BTreeSet::new();
    let mut suggestions = Vec::new();
    for (index, command) in commands.iter().enumerate() {
        if command.argv.is_empty()
            || sub_results
                .and_then(|results| results.get(index))
                .is_some_and(|result| result.behavior == "allow")
        {
            continue;
        }
        let base = basename(&command.argv[0]);
        if base.is_empty() || DANGEROUS_BUILTINS.contains(&base.as_str()) {
            continue;
        }
        let rule_content = format!("{base}:*");
        if seen.insert(rule_content.clone()) {
            suggestions.push(PermissionRuleValue {
                tool_name: "bash".into(),
                rule_content,
            });
        }
    }
    suggestions
}

fn generate_suggestions_from_text(command: &str) -> Vec<PermissionRuleValue> {
    let normalized = normalize_command(command.trim());
    let first = normalized.split_whitespace().next().unwrap_or_default();
    if first.is_empty() {
        return Vec::new();
    }
    let base = basename(first);
    vec![PermissionRuleValue {
        tool_name: "bash".into(),
        rule_content: format!("{base}:*"),
    }]
}

fn behavior_order(behavior: &str) -> u8 {
    match behavior {
        "deny" => 0,
        "ask" => 1,
        "passthrough" => 2,
        "allow" => 3,
        _ => 4,
    }
}

fn basename(path: &str) -> String {
    Path::new(path)
        .file_name()
        .map(|name| name.to_string_lossy().into_owned())
        .unwrap_or_else(|| path.into())
}
