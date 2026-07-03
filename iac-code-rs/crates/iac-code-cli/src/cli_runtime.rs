use iac_code_config::paths::ConfigPaths;
use iac_code_config::settings::load_saved_model;
use iac_code_exec::OutputFormat;

use crate::a2a_client_commands::{
    handle_a2a_client_call, handle_a2a_client_command, handle_a2a_client_discover,
    handle_a2a_client_push_command, handle_a2a_client_route_preview,
    handle_a2a_client_task_command,
};
use crate::a2a_server::handle_a2a_server_command;
use crate::acp_server_args::handle_acp_server_command;
use crate::cli_args::{normalize_cli_args, parse_args, read_stdin, Cli};
use crate::cli_completion::{handle_completion_command, handle_shell_completion_protocol};
use crate::cli_help::{handle_unknown_top_level_command, print_help};
use crate::cli_i18n::{tr, tr_permission_mode_error, tr_two_values};
use crate::cli_protocol_help::{handle_protocol_command_help, handle_update_command};
use crate::debug_logging::enable_startup_debug_log;
use crate::headless_runner::{run_prompt_from_cli, write_headless_result};
use crate::interactive_runtime::run_interactive_cli;

pub(super) const VERSION: &str = "0.4.1";

const VALID_OUTPUT_FORMATS: &[&str] = &["text", "json", "stream-json"];
const VALID_PERMISSION_MODES: &[&str] =
    &["default", "accept_edits", "bypass_permissions", "dont_ask"];

pub(super) fn run_cli(args: impl IntoIterator<Item = String>) -> i32 {
    let raw_args = normalize_cli_args(args);
    if let Some(exit_code) = handle_shell_completion_protocol() {
        return exit_code;
    }
    if handle_protocol_command_help(&raw_args) {
        return 0;
    }
    if let Some(exit_code) = handle_completion_command(&raw_args) {
        return exit_code;
    }
    if let Some(exit_code) = handle_update_command(&raw_args) {
        return exit_code;
    }
    if let Some(exit_code) = handle_acp_server_command(&raw_args) {
        return exit_code;
    }
    if let Some(exit_code) = handle_a2a_client_command(&raw_args) {
        return exit_code;
    }
    if let Some(exit_code) = handle_a2a_server_command(&raw_args) {
        return exit_code;
    }
    if let Some(exit_code) = handle_a2a_client_call(&raw_args) {
        return exit_code;
    }
    if let Some(exit_code) = handle_a2a_client_task_command(&raw_args) {
        return exit_code;
    }
    if let Some(exit_code) = handle_a2a_client_push_command(&raw_args) {
        return exit_code;
    }
    if let Some(exit_code) = handle_a2a_client_discover(&raw_args) {
        return exit_code;
    }
    if let Some(exit_code) = handle_a2a_client_route_preview(&raw_args) {
        return exit_code;
    }
    if let Some(exit_code) = handle_unknown_top_level_command(&raw_args) {
        return exit_code;
    }

    let mut cli = match parse_args(raw_args) {
        Ok(cli) => cli,
        Err(error) => {
            eprintln!("{error}");
            return 2;
        }
    };

    if cli.help {
        print_help();
        return 0;
    }

    if cli.version {
        println!("iac-code v{VERSION}");
        return 0;
    }

    if !cli.resume.is_empty() && cli.continue_session {
        eprintln!(
            "{}",
            tr("Error: --resume and --continue cannot be used together.")
        );
        return 1;
    }

    let parsed_output_format = if !cli.prompt.is_empty() {
        match parse_cli_output_format(&cli) {
            Ok(output_format) => output_format,
            Err(error) => {
                eprintln!("{error}");
                return 1;
            }
        }
    } else {
        OutputFormat::Text
    };
    if let Err(error) = preflight_saved_model_like_python(&cli) {
        eprintln!("{error}");
        return 1;
    }
    if cli.prompt == "-" {
        cli.prompt = read_stdin();
    }
    if !cli.prompt.is_empty() {
        if let Err(error) = validate_cli_permission_mode(&cli) {
            eprintln!("{error}");
            return 1;
        }
    }

    if !cli.prompt.is_empty() {
        if cli.debug {
            if let Err(error) = enable_startup_debug_log("headless") {
                eprintln!("{error}");
                return 1;
            }
        }
        let result = match run_prompt_from_cli(
            &cli,
            &cli.prompt,
            parsed_output_format,
            &cli.resume,
            cli.continue_session,
            None,
        ) {
            Ok(result) => result,
            Err(error) => {
                eprintln!("{error}");
                return 1;
            }
        };
        write_headless_result(&result);
        return result.exit_code;
    }

    run_interactive_cli(&cli, parsed_output_format)
}

fn parse_cli_output_format(cli: &Cli) -> Result<OutputFormat, String> {
    let output_format = if cli.output_format.is_empty() {
        "text".to_owned()
    } else {
        cli.output_format.trim().to_ascii_lowercase()
    };
    let Some(parsed_output_format) = OutputFormat::parse(&output_format) else {
        return Err(tr_two_values(
            "Invalid --output-format '{}'. Valid values: {}",
            &cli.output_format,
            &VALID_OUTPUT_FORMATS.join(", "),
        ));
    };
    if !VALID_OUTPUT_FORMATS.contains(&output_format.as_str()) {
        return Err(tr_two_values(
            "Invalid --output-format '{}'. Valid values: {}",
            &cli.output_format,
            &VALID_OUTPUT_FORMATS.join(", "),
        ));
    }
    Ok(parsed_output_format)
}

fn preflight_saved_model_like_python(cli: &Cli) -> Result<(), String> {
    if cli.model.trim().is_empty() {
        let paths = ConfigPaths::from_env().map_err(|error| error.to_string())?;
        let _ = load_saved_model(&paths).map_err(|error| error.to_string())?;
    }
    Ok(())
}

fn validate_cli_permission_mode(cli: &Cli) -> Result<(), String> {
    if !cli.permission_mode.is_empty()
        && !VALID_PERMISSION_MODES.contains(&cli.permission_mode.as_str())
    {
        return Err(tr_permission_mode_error(
            &cli.permission_mode,
            &VALID_PERMISSION_MODES.join(", "),
        ));
    }
    Ok(())
}
