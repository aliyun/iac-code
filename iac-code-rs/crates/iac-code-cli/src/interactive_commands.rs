use std::path::PathBuf;

use iac_code_exec::OutputFormat;
use iac_code_tui::CommandCatalog;

use crate::cli_args::Cli;
use crate::cli_i18n::{tr, tr_dynamic};
use crate::debug_logging::enable_interactive_debug_log;
use crate::interactive_compact_command::print_interactive_compact;
use crate::interactive_debug_command::print_interactive_debug;
use crate::interactive_memory_commands::{
    print_interactive_memory, print_interactive_memory_folder,
};
use crate::interactive_provider_commands::{
    print_interactive_auth, print_interactive_effort, print_interactive_model,
};
use crate::interactive_rename_command::print_interactive_rename;
use crate::interactive_resume_command::{
    interactive_resume_message, latest_session_id_for_current_cwd,
};
use crate::interactive_skills::interactive_skills_message;
use crate::interactive_status::print_interactive_status;

#[cfg(test)]
pub(super) fn interactive_compact_status() -> String {
    crate::interactive_compact_command::interactive_compact_status()
}

pub(super) fn refresh_current_session_debug(
    current_session_id: &mut Option<String>,
    debug_enabled: bool,
    debug_log_path: &mut Option<PathBuf>,
) {
    if let Ok(Some(session_id)) = latest_session_id_for_current_cwd() {
        let should_refresh_log = current_session_id.as_deref() != Some(session_id.as_str())
            || (debug_enabled && debug_log_path.is_none());
        *current_session_id = Some(session_id.clone());
        if debug_enabled && should_refresh_log {
            *debug_log_path = enable_interactive_debug_log(&session_id).ok();
        }
    }
}

pub(super) enum InteractiveCommandResult {
    Continue,
    Clear,
    Resume(String),
    Exit(i32),
    Unsupported(String),
}

pub(super) struct InteractiveCommandContext<'a> {
    pub(super) exit_code: i32,
    pub(super) turn_count: u32,
    pub(super) token_count: u64,
    pub(super) debug_enabled: &'a mut bool,
    pub(super) debug_log_path: &'a mut Option<PathBuf>,
    pub(super) current_session_id: Option<&'a str>,
    pub(super) output_format: OutputFormat,
}

pub(super) fn handle_interactive_command(
    cli: &Cli,
    prompt: &str,
    context: InteractiveCommandContext<'_>,
) -> InteractiveCommandResult {
    let command_text = prompt.strip_prefix('/').unwrap_or(prompt).trim();
    let mut parts = command_text.splitn(2, char::is_whitespace);
    let command = parts.next().unwrap_or_default().to_ascii_lowercase();
    let args = parts.next().unwrap_or_default().trim();

    match command.as_str() {
        "exit" | "quit" | "q" => InteractiveCommandResult::Exit(context.exit_code),
        "clear" => InteractiveCommandResult::Clear,
        "debug" => {
            print_interactive_debug(
                args,
                context.debug_enabled,
                context.debug_log_path,
                context.current_session_id,
            );
            InteractiveCommandResult::Continue
        }
        "memory" => {
            print_interactive_memory(args);
            InteractiveCommandResult::Continue
        }
        "memory-folder" => {
            print_interactive_memory_folder(args);
            InteractiveCommandResult::Continue
        }
        "auth" | "login" => {
            print_interactive_auth(args);
            InteractiveCommandResult::Continue
        }
        "compact" => {
            print_interactive_compact(cli, context.current_session_id, context.output_format);
            InteractiveCommandResult::Continue
        }
        "rename" => {
            print_interactive_rename(args, context.current_session_id);
            InteractiveCommandResult::Continue
        }
        "resume" => match interactive_resume_message(args) {
            Ok((message, Some(session_id))) => {
                print_interactive_command_result(&message);
                InteractiveCommandResult::Resume(session_id)
            }
            Ok((message, None)) => {
                print_interactive_command_result(&message);
                InteractiveCommandResult::Continue
            }
            Err(error) => {
                print_interactive_command_result(&error);
                InteractiveCommandResult::Continue
            }
        },
        "model" => {
            print_interactive_model(args);
            InteractiveCommandResult::Continue
        }
        "effort" => {
            print_interactive_effort(args);
            InteractiveCommandResult::Continue
        }
        "skills" => {
            print_interactive_skills(args);
            InteractiveCommandResult::Continue
        }
        "help" | "?" => {
            print_interactive_help();
            InteractiveCommandResult::Continue
        }
        "status" => {
            print_interactive_status(
                cli,
                context.turn_count,
                context.current_session_id,
                context.token_count,
                *context.debug_enabled,
            );
            InteractiveCommandResult::Continue
        }
        _ => InteractiveCommandResult::Unsupported(command),
    }
}

fn print_interactive_skills(args: &str) {
    let message = match interactive_skills_message(args) {
        Ok(message) => message,
        Err(error) => error,
    };
    print_interactive_command_result(&message);
}

fn print_interactive_help() {
    print_interactive_command_result(&interactive_help_message());
}

fn interactive_help_message() -> String {
    let mut output = format!(
        "iac-code - {}\n\n{}",
        tr("AI-powered infrastructure orchestration tool"),
        tr("Commands:")
    );
    for command in CommandCatalog::default_commands().get_all() {
        output.push_str(&format!(
            "\n  /{:<12}  {}",
            command.name,
            tr_dynamic(&command.description)
        ));
    }
    output.push_str(&format!("\n\n{}", tr("Shortcuts:")));
    output.push_str(&format!("\n  {:<14}  {}", "Enter", tr("Send message")));
    output.push_str(&format!("\n  {:<14}  {}", "Esc+Enter", tr("New line")));
    output.push_str(&format!(
        "\n  {:<14}  {}",
        "/",
        tr("Show command suggestions")
    ));
    output.push_str(&format!("\n  {:<14}  {}", "Ctrl+C", tr("Exit")));
    output
}

pub(super) fn format_interactive_command_result(message: &str) -> String {
    let mut lines = message.lines();
    let Some(first) = lines.next() else {
        return String::new();
    };
    let mut output = format!("  └ {first}");
    for line in lines {
        output.push('\n');
        output.push_str(line);
    }
    output
}

pub(super) fn print_interactive_command_result(message: &str) {
    let output = format_interactive_command_result(message);
    if !output.is_empty() {
        println!("{output}");
    }
}
