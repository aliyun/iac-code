use std::path::Path;

use iac_code_protocol::permission::{PermissionMode, PermissionResult, ToolPermissionContext};

use super::parser::{command_base, parse_command, ParsedCommand};
use super::path_args::{check_read_path_constraints, check_write_path_constraints};
use super::readonly::{dangerous_arg_label, dangerous_readonly_argument, is_command_readonly};
use super::result::{ask_with_reason, result_with_reason};
use super::rules::{collect_rules, find_matching_rules};
use super::safety::command_safety_check;
use super::suggestions::{merge_permission_results, with_suggestions_if_needed};

const FILESYSTEM_COMMANDS: &[&str] = &["mkdir", "touch", "rm", "rmdir", "mv", "cp", "sed"];
const MAX_SUBCOMMANDS: usize = 10;

pub(super) fn bash_tool_has_permission(
    command: &str,
    context: &ToolPermissionContext,
) -> PermissionResult {
    if !command_safety_check(command) {
        return with_suggestions_if_needed(
            ask_with_reason("safety_check", "command failed basic safety checks"),
            command,
            None,
            None,
        );
    }

    let allow_flat = collect_rules(&context.allow_rules);
    let deny_flat = collect_rules(&context.deny_rules);
    let ask_flat = collect_rules(&context.ask_rules);

    let full_matches = find_matching_rules(command, &allow_flat, &deny_flat, &ask_flat);
    if !full_matches.deny.is_empty() {
        let detail = format!(
            "matched deny rule(s) on full command: {}",
            full_matches.deny.join(", ")
        );
        return result_with_reason("deny", "rule", detail);
    }

    let parsed = parse_command(command);
    if parsed.is_empty() {
        return with_suggestions_if_needed(PermissionResult::passthrough(), command, None, None);
    }
    if parsed.len() > MAX_SUBCOMMANDS {
        let detail = format!("too many subcommands (>{MAX_SUBCOMMANDS})");
        return with_suggestions_if_needed(
            result_with_reason("ask", "compound_limit", detail),
            command,
            Some(&parsed),
            None,
        );
    }
    let cd_count = parsed
        .iter()
        .filter(|command| command_base(command).as_deref() == Some("cd"))
        .count();
    if cd_count > 1 {
        return with_suggestions_if_needed(
            ask_with_reason("compound_cd", "multiple cd commands in compound command"),
            command,
            Some(&parsed),
            None,
        );
    }
    if cd_count > 0
        && parsed
            .iter()
            .any(|command| command_base(command).as_deref() == Some("git"))
    {
        return with_suggestions_if_needed(
            ask_with_reason(
                "compound_cd_git",
                "cd combined with git in compound command",
            ),
            command,
            Some(&parsed),
            None,
        );
    }

    let sub_results = parsed
        .iter()
        .map(|command| bash_tool_check_permission(command, context, cd_count > 0))
        .collect::<Vec<PermissionResult>>();
    with_suggestions_if_needed(
        merge_permission_results(&sub_results),
        command,
        Some(&parsed),
        Some(&sub_results),
    )
}

fn bash_tool_check_permission(
    command: &ParsedCommand,
    context: &ToolPermissionContext,
    compound_has_cd: bool,
) -> PermissionResult {
    if command.argv.is_empty() {
        return PermissionResult::passthrough();
    }

    let allow_flat = collect_rules(&context.allow_rules);
    let deny_flat = collect_rules(&context.deny_rules);
    let ask_flat = collect_rules(&context.ask_rules);
    let matched = find_matching_rules(&command.text, &allow_flat, &deny_flat, &ask_flat);

    if !matched.deny.is_empty() {
        let detail = format!("matched deny rule(s): {}", matched.deny.join(", "));
        return result_with_reason("deny", "rule", detail);
    }
    if !matched.ask.is_empty() {
        let detail = format!("matched ask rule(s): {}", matched.ask.join(", "));
        return result_with_reason("ask", "rule", detail);
    }

    let path_result = check_write_path_constraints(command, context);
    if path_result.behavior != "passthrough" {
        return path_result;
    }

    if let Some(argument) = dangerous_readonly_argument(&command.argv) {
        let detail = format!(
            "dangerous readonly argument requires confirmation: {}",
            dangerous_arg_label(&argument)
        );
        return result_with_reason("ask", "dangerous_readonly_argument", detail);
    }

    let read_path_result = check_read_path_constraints(command, context, compound_has_cd);
    if read_path_result.behavior != "passthrough" {
        return read_path_result;
    }

    if command.is_complex {
        return ask_with_reason("complex_command", "complex command requires confirmation");
    }

    if !matched.allow.is_empty() {
        let detail = format!("matched allow rule(s): {}", matched.allow.join(", "));
        return result_with_reason("allow", "rule", detail);
    }

    if context.mode == PermissionMode::AcceptEdits
        && command
            .argv
            .first()
            .is_some_and(|base| FILESYSTEM_COMMANDS.contains(&basename(base).as_str()))
    {
        return PermissionResult::allow();
    }

    if is_command_readonly(command) {
        return PermissionResult::allow();
    }

    PermissionResult::passthrough()
}

fn basename(path: &str) -> String {
    Path::new(path)
        .file_name()
        .map(|name| name.to_string_lossy().into_owned())
        .unwrap_or_else(|| path.into())
}
