use std::path::Path;

use iac_code_protocol::permission::{
    PermissionDecisionReason, PermissionResult, ToolPermissionContext,
};

use super::parser::{command_base, redirect_suffix_after_fd, ParsedCommand};
use super::sed::sed_read_paths;
use crate::path_safety::{check_read_path, check_write_path};

const WRITE_PATH_COMMANDS: &[&str] = &["cp", "mv", "rm", "mkdir", "rmdir", "ln", "install"];
const READ_PATH_COMMANDS: &[&str] = &[
    "ls",
    "ll",
    "la",
    "cat",
    "head",
    "tail",
    "less",
    "more",
    "wc",
    "file",
    "stat",
    "du",
    "df",
    "tree",
    "realpath",
    "readlink",
    "md5sum",
    "sha256sum",
    "grep",
    "egrep",
    "fgrep",
    "rg",
    "ag",
    "ack",
    "find",
    "fd",
    "sed",
    "sort",
    "uniq",
    "cut",
    "diff",
    "comm",
    "jq",
    "yq",
];
const GREP_LIKE_COMMANDS: &[&str] = &["grep", "egrep", "fgrep", "rg", "ag", "ack"];
const FIRST_POSITIONAL_IS_PATTERN_COMMANDS: &[&str] = &["fd", "sed", "jq", "yq"];
const IMPLICIT_CURRENT_DIRECTORY_READ_COMMANDS: &[&str] = &[
    "ls", "ll", "la", "tree", "du", "rg", "ag", "ack", "fd", "find",
];

pub(super) fn check_write_path_constraints(
    command: &ParsedCommand,
    context: &ToolPermissionContext,
) -> PermissionResult {
    let mut candidates = write_redirect_paths(&command.redirects);
    candidates.extend(write_argv_paths(&command.argv));
    if candidates.is_empty() {
        return PermissionResult::passthrough();
    }

    for candidate in candidates {
        if has_shell_expansion(&candidate) {
            let detail = format!("path uses shell expansion: {}", candidate);
            return result_with_reason("ask", "path_constraint", detail);
        }
        let decision = check_write_path(&candidate, &context.cwd, &context.additional_directories);
        if decision.to_permission_result().behavior == "ask" {
            let result = decision.to_permission_result();
            if result
                .reason
                .as_ref()
                .is_some_and(|reason| reason.type_name == "path_constraint")
            {
                let detail = format!("path outside allowed directories: {}", candidate);
                return result_with_reason("ask", "path_constraint", detail);
            }
            return result;
        }
    }

    PermissionResult::passthrough()
}

fn write_redirect_paths(redirects: &[String]) -> Vec<String> {
    redirects
        .iter()
        .filter_map(|redirect| redirect_target(redirect, RedirectKind::Write))
        .collect()
}

fn write_argv_paths(argv: &[String]) -> Vec<String> {
    let Some(base) = argv.first().map(|value| basename(value)) else {
        return Vec::new();
    };
    if !WRITE_PATH_COMMANDS.contains(&base.as_str()) {
        return Vec::new();
    }
    let args = &argv[1..];
    let mut paths = Vec::new();
    let mut seen_double_dash = false;
    let mut index = 0;
    while index < args.len() {
        let arg = &args[index];
        if seen_double_dash {
            paths.push(arg.clone());
            index += 1;
            continue;
        }
        if arg == "--" {
            seen_double_dash = true;
            index += 1;
            continue;
        }
        if let Some((value, next_index)) = write_path_value_option(&base, args, index) {
            paths.push(value);
            index = next_index;
            continue;
        }
        if arg.starts_with('-') && arg.len() > 1 {
            index += 1;
            continue;
        }
        paths.push(arg.clone());
        index += 1;
    }
    paths
}

fn write_path_value_option(base: &str, args: &[String], index: usize) -> Option<(String, usize)> {
    if !matches!(base, "cp" | "mv" | "ln" | "install") {
        return None;
    }

    let arg = &args[index];
    if arg == "--target-directory" {
        return args.get(index + 1).map(|value| (value.clone(), index + 2));
    }
    if let Some(value) = arg.strip_prefix("--target-directory=") {
        return Some((value.to_owned(), index + 1));
    }
    if arg == "-t" {
        return args.get(index + 1).map(|value| (value.clone(), index + 2));
    }
    if let Some(value) = arg.strip_prefix("-t") {
        if !value.is_empty() {
            return Some((value.to_owned(), index + 1));
        }
    }
    if arg.starts_with('-') && !arg.starts_with("--") && arg.len() > 2 {
        let cluster = &arg[1..];
        if let Some(option_index) = cluster.find('t') {
            let value = &cluster[option_index + 1..];
            if !value.is_empty() {
                return Some((value.to_owned(), index + 1));
            }
            return args
                .get(index + 1)
                .map(|next_value| (next_value.clone(), index + 2));
        }
    }
    None
}

pub(super) fn check_read_path_constraints(
    command: &ParsedCommand,
    context: &ToolPermissionContext,
    compound_has_cd: bool,
) -> PermissionResult {
    let mut candidates = read_redirect_paths(&command.redirects);
    candidates.extend(read_argv_paths(&command.argv));
    if candidates.is_empty() {
        if compound_has_cd && command_implicitly_reads_current_directory(command) {
            return result_with_reason(
                "ask",
                "path_constraint",
                "read path after cd requires confirmation: current directory".into(),
            );
        }
        return PermissionResult::passthrough();
    }
    if compound_has_cd {
        for candidate in &candidates {
            if is_relative_read_path(candidate) {
                let detail = format!("read path after cd requires confirmation: {candidate}");
                return result_with_reason("ask", "path_constraint", detail);
            }
        }
    }
    for candidate in candidates {
        if has_shell_expansion(&candidate) {
            let detail = format!("read path uses shell expansion: {}", candidate);
            return result_with_reason("ask", "path_constraint", detail);
        }
        let decision = check_read_path(
            &candidate,
            &context.cwd,
            &context.additional_directories,
            &context.trusted_read_directories,
        );
        if decision.to_permission_result().behavior == "ask" {
            return decision.to_permission_result();
        }
    }
    PermissionResult::passthrough()
}

fn command_implicitly_reads_current_directory(command: &ParsedCommand) -> bool {
    let Some(base) = command_base(command) else {
        return false;
    };
    IMPLICIT_CURRENT_DIRECTORY_READ_COMMANDS.contains(&base.as_str())
        && read_argv_paths(&command.argv).is_empty()
}

fn is_relative_read_path(path: &str) -> bool {
    let path = trim_outer_quotes(path.trim());
    !Path::new(path).is_absolute() && !path.starts_with("~/")
}

fn read_redirect_paths(redirects: &[String]) -> Vec<String> {
    redirects
        .iter()
        .filter_map(|redirect| redirect_target(redirect, RedirectKind::Read))
        .collect()
}

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
enum RedirectKind {
    Read,
    Write,
}

fn redirect_target(redirect: &str, kind: RedirectKind) -> Option<String> {
    let trimmed = redirect.trim();
    let operator = redirect_suffix_after_fd(trimmed);
    let (operator, target) = operator.split_once(char::is_whitespace)?;
    let matches_kind = match kind {
        RedirectKind::Read => matches!(operator, "<" | "<>"),
        RedirectKind::Write => matches!(operator, ">" | ">>" | ">|" | "<>"),
    };
    matches_kind.then(|| trim_outer_quotes(target.trim()).to_owned())
}

fn trim_outer_quotes(value: &str) -> &str {
    let bytes = value.as_bytes();
    if bytes.len() >= 2
        && ((bytes[0] == b'\'' && bytes[bytes.len() - 1] == b'\'')
            || (bytes[0] == b'"' && bytes[bytes.len() - 1] == b'"'))
    {
        &value[1..value.len() - 1]
    } else {
        value
    }
}

fn read_argv_paths(argv: &[String]) -> Vec<String> {
    let Some(base) = argv.first().map(|value| basename(value)) else {
        return Vec::new();
    };
    if !READ_PATH_COMMANDS.contains(&base.as_str()) {
        return Vec::new();
    }
    if direct_read_command(&base) {
        return read_positionals(argv, 1);
    }
    if GREP_LIKE_COMMANDS.contains(&base.as_str()) {
        return grep_read_paths(argv);
    }
    if base == "find" {
        return find_read_paths(argv);
    }
    if base == "sed" {
        return sed_read_paths(argv);
    }
    if FIRST_POSITIONAL_IS_PATTERN_COMMANDS.contains(&base.as_str()) {
        return read_positionals(argv, 2);
    }
    Vec::new()
}

fn direct_read_command(base: &str) -> bool {
    READ_PATH_COMMANDS.contains(&base)
        && !GREP_LIKE_COMMANDS.contains(&base)
        && !FIRST_POSITIONAL_IS_PATTERN_COMMANDS.contains(&base)
        && base != "find"
}

fn read_positionals(argv: &[String], start: usize) -> Vec<String> {
    let mut out = Vec::new();
    let mut seen_double_dash = false;
    let mut index = start;
    while index < argv.len() {
        let arg = &argv[index];
        if seen_double_dash {
            out.push(arg.clone());
            index += 1;
            continue;
        }
        if arg == "--" {
            seen_double_dash = true;
            index += 1;
            continue;
        }
        if arg.starts_with('-') && arg.len() > 1 {
            index += 1;
            continue;
        }
        out.push(arg.clone());
        index += 1;
    }
    out
}

fn grep_read_paths(argv: &[String]) -> Vec<String> {
    let mut paths = Vec::new();
    let mut pattern_seen = false;
    let mut index = 1;
    while index < argv.len() {
        let arg = &argv[index];
        if matches!(arg.as_str(), "-f" | "--file") && index + 1 < argv.len() {
            paths.push(argv[index + 1].clone());
            index += 2;
            continue;
        }
        if matches!(arg.as_str(), "-e" | "--regexp") && index + 1 < argv.len() {
            pattern_seen = true;
            index += 2;
            continue;
        }
        if arg.starts_with('-') && arg.len() > 1 {
            index += 1;
            continue;
        }
        if pattern_seen {
            paths.push(arg.clone());
        } else {
            pattern_seen = true;
        }
        index += 1;
    }
    paths
}

fn find_read_paths(argv: &[String]) -> Vec<String> {
    let mut paths = Vec::new();
    let mut index = 1;
    while index < argv.len() {
        let arg = &argv[index];
        if matches!(arg.as_str(), "-H" | "-L" | "-P") {
            index += 1;
            continue;
        }
        if arg.starts_with('-') {
            break;
        }
        paths.push(arg.clone());
        index += 1;
    }
    paths
}

fn has_shell_expansion(path: &str) -> bool {
    path.contains('$') || path.contains('`')
}

fn result_with_reason(
    behavior: impl Into<String>,
    reason_type: impl Into<String>,
    detail: String,
) -> PermissionResult {
    PermissionResult {
        behavior: behavior.into(),
        message: detail.clone(),
        reason: Some(PermissionDecisionReason {
            type_name: reason_type.into(),
            detail,
        }),
        suggestions: None,
    }
}

fn basename(path: &str) -> String {
    Path::new(path)
        .file_name()
        .map(|name| name.to_string_lossy().into_owned())
        .unwrap_or_else(|| path.into())
}
