use std::io::{self, Read};

use crate::cli_help::no_such_command_message;

#[derive(Clone, Debug, Default)]
pub(super) struct Cli {
    pub(super) prompt: String,
    pub(super) model: String,
    pub(super) output_format: String,
    pub(super) max_turns: u32,
    pub(super) allowed_tools: String,
    pub(super) disallowed_tools: String,
    pub(super) permission_mode: String,
    pub(super) resume: String,
    pub(super) continue_session: bool,
    pub(super) version: bool,
    pub(super) help: bool,
    pub(super) verbose: bool,
    pub(super) debug: bool,
}

impl Cli {
    pub(super) fn with_allowed_tools(&self, allowed_tools: String) -> Self {
        let mut cli = self.clone();
        cli.allowed_tools = allowed_tools;
        cli
    }
}

pub(super) fn parse_args(args: Vec<String>) -> Result<Cli, String> {
    let mut cli = Cli {
        output_format: "text".to_string(),
        max_turns: 100,
        ..Cli::default()
    };

    let mut iter = args.into_iter();
    while let Some(arg) = iter.next() {
        match arg.as_str() {
            "--version" | "-v" | "-V" => cli.version = true,
            "--help" | "-h" => cli.help = true,
            "--verbose" => cli.verbose = true,
            "--prompt" | "-p" | "--output-format" | "--model" | "-m" | "--allowed-tools"
            | "--disallowed-tools" | "--max-turns" | "--permission-mode" | "--resume" | "-r" => {
                let value = next_cli_value(&mut iter, &arg)?;
                apply_cli_value_option(&mut cli, &arg, value)?;
            }
            "--continue" | "-c" => cli.continue_session = true,
            "--debug" | "-d" => cli.debug = true,
            option if option.starts_with('-') => {
                return Err(format!("No such option: {option}"));
            }
            command => return Err(no_such_command_message("iac-code", command)),
        }
    }

    Ok(cli)
}

pub(super) fn normalize_cli_args(args: impl IntoIterator<Item = String>) -> Vec<String> {
    let mut normalized = Vec::new();
    for arg in args {
        if let Some((option, value)) = split_long_option_assignment(&arg) {
            normalized.push(option.to_owned());
            normalized.push(value);
        } else if let Some((option, value)) = split_short_option_attached_value(&arg) {
            normalized.push(option.to_owned());
            normalized.push(value);
        } else {
            normalized.push(arg);
        }
    }
    normalized
}

fn split_long_option_assignment(arg: &str) -> Option<(&str, String)> {
    let (option, value) = arg.split_once('=')?;
    (option.starts_with("--") && option.len() > 2).then(|| (option, value.to_owned()))
}

fn split_short_option_attached_value(arg: &str) -> Option<(&str, String)> {
    if arg.starts_with("--") || !arg.starts_with('-') || arg.len() <= 2 {
        return None;
    }
    let option = &arg[..2];
    matches!(option, "-p" | "-m" | "-r").then(|| (option, arg[2..].to_owned()))
}

fn apply_cli_value_option(cli: &mut Cli, option: &str, value: String) -> Result<bool, String> {
    match option {
        "--prompt" | "-p" => cli.prompt = value,
        "--output-format" => cli.output_format = value,
        "--model" | "-m" => cli.model = value,
        "--allowed-tools" => cli.allowed_tools = value,
        "--disallowed-tools" => cli.disallowed_tools = value,
        "--max-turns" => cli.max_turns = parse_max_turns_value(&value)?,
        "--permission-mode" => cli.permission_mode = value,
        "--resume" | "-r" => cli.resume = value,
        _ => return Ok(false),
    }
    Ok(true)
}

fn parse_max_turns_value(value: &str) -> Result<u32, String> {
    let parsed = value.parse::<i64>().map_err(|_| {
        format!("Invalid value for '--max-turns': '{value}' is not a valid integer.")
    })?;
    Ok(if parsed <= 0 {
        0
    } else {
        parsed.min(u32::MAX as i64) as u32
    })
}

fn next_cli_value<I>(iter: &mut I, option: &str) -> Result<String, String>
where
    I: Iterator<Item = String>,
{
    let Some(value) = iter.next() else {
        return Err(format!("Option '{option}' requires an argument."));
    };
    Ok(value)
}

pub(super) fn next_option_value(
    args: &[String],
    index: &mut usize,
    option: &str,
) -> Result<String, String> {
    let value_index = *index + 1;
    let Some(value) = args.get(value_index) else {
        return Err(format!("Missing value for {option}."));
    };
    *index += 2;
    Ok(value.clone())
}

pub(super) fn split_tool_rules(value: &str) -> Vec<String> {
    value
        .split(',')
        .map(str::trim)
        .filter(|item| !item.is_empty())
        .map(ToOwned::to_owned)
        .collect()
}

pub(super) fn read_stdin() -> String {
    let mut buffer = String::new();
    if io::stdin().read_to_string(&mut buffer).is_ok() {
        buffer.trim().to_string()
    } else {
        String::new()
    }
}

pub(super) fn non_empty_str(value: &str) -> Option<&str> {
    if value.is_empty() {
        None
    } else {
        Some(value)
    }
}

pub(super) fn non_empty_string(value: String) -> Option<String> {
    if value.is_empty() {
        None
    } else {
        Some(value)
    }
}
