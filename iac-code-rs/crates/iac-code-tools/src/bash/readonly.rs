use std::path::Path;

use super::parser::ParsedCommand;
use super::sed::{sed_executes_shell, sed_inplace_edit, sed_uses_script_file, sed_writes_file};
use super::subcommands::{git_readonly, package_manager_readonly};

const READONLY_BASE_COMMANDS: &[&str] = &[
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
    "basename",
    "dirname",
    "md5sum",
    "sha256sum",
    "grep",
    "egrep",
    "fgrep",
    "rg",
    "find",
    "fd",
    "ag",
    "ack",
    "locate",
    "which",
    "whereis",
    "type",
    "pwd",
    "env",
    "printenv",
    "echo",
    "printf",
    "whoami",
    "id",
    "hostname",
    "uname",
    "date",
    "uptime",
    "free",
    "ps",
    "top",
    "lsof",
    "netstat",
    "ss",
    "sort",
    "uniq",
    "cut",
    "tr",
    "diff",
    "comm",
    "jq",
    "yq",
    "true",
    "false",
    "test",
    "sed",
];

pub(super) fn dangerous_readonly_argument(argv: &[String]) -> Option<String> {
    let base = argv.first().map(|value| basename(value))?;
    match base.as_str() {
        "find" => argv
            .iter()
            .skip(1)
            .find(|arg| ["-delete", "-exec", "-execdir", "-ok", "-okdir"].contains(&arg.as_str()))
            .cloned(),
        "fd" => argv
            .iter()
            .skip(1)
            .find(|arg| fd_arg_dangerous(arg))
            .cloned(),
        "sed" => {
            if sed_inplace_edit(argv) {
                Some("sed in-place edit".into())
            } else if sed_uses_script_file(argv) {
                Some("sed script file".into())
            } else if sed_executes_shell(argv) {
                Some("sed shell execution".into())
            } else if sed_writes_file(argv) {
                Some("sed file write".into())
            } else {
                None
            }
        }
        "rg" => option_or_assignment(&argv[1..], "--pre"),
        "sort" => option_or_assignment(&argv[1..], "--compress-program")
            .or_else(|| option_or_assignment(&argv[1..], "--output"))
            .or_else(|| {
                argv.iter()
                    .skip(1)
                    .find(|arg| *arg == "-o" || arg.starts_with("-o"))
                    .cloned()
            }),
        _ => None,
    }
}

fn fd_arg_dangerous(arg: &str) -> bool {
    matches!(arg, "-x" | "-X" | "--exec" | "--exec-batch")
        || arg.starts_with("--exec=")
        || arg.starts_with("--exec-batch=")
        || (arg.len() > 2 && (arg.starts_with("-x") || arg.starts_with("-X")))
}

fn option_or_assignment(args: &[String], option: &str) -> Option<String> {
    for arg in args {
        if arg == option || arg.starts_with(&format!("{option}=")) {
            return Some(arg.clone());
        }
    }
    None
}

pub(super) fn dangerous_arg_label(arg: &str) -> &str {
    match arg {
        "sed in-place edit" => "sed in-place edit",
        "sed script file" => "sed script file",
        "sed shell execution" => "sed shell execution",
        "sed file write" => "sed file write",
        _ => arg,
    }
}

pub(super) fn is_command_readonly(command: &ParsedCommand) -> bool {
    if !command.redirects.is_empty() || command.argv.is_empty() {
        return false;
    }
    if command
        .argv
        .last()
        .is_some_and(|arg| matches!(arg.as_str(), "--version" | "-V"))
    {
        return true;
    }
    let base = basename(&command.argv[0]);
    if base == "command" && command.argv.iter().skip(1).any(|arg| arg == "-v") {
        return true;
    }
    if base == "git" && git_readonly(&command.argv) {
        return true;
    }
    if package_manager_readonly(&command.argv) {
        return true;
    }
    if dangerous_readonly_argument(&command.argv).is_some() {
        return false;
    }
    READONLY_BASE_COMMANDS.contains(&base.as_str())
}

fn basename(path: &str) -> String {
    Path::new(path)
        .file_name()
        .map(|name| name.to_string_lossy().into_owned())
        .unwrap_or_else(|| path.into())
}
