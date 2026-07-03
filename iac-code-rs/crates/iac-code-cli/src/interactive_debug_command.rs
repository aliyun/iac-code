use std::path::PathBuf;

use crate::cli_i18n::{tr, tr_value};
use crate::debug_logging::enable_interactive_debug_log;
use crate::interactive_commands::print_interactive_command_result;

pub(super) fn print_interactive_debug(
    args: &str,
    debug_enabled: &mut bool,
    debug_log_path: &mut Option<PathBuf>,
    current_session_id: Option<&str>,
) {
    let message =
        match interactive_debug_message(args, debug_enabled, debug_log_path, current_session_id) {
            Ok(message) => message,
            Err(error) => error,
        };
    print_interactive_command_result(&message);
}

fn interactive_debug_message(
    args: &str,
    debug_enabled: &mut bool,
    debug_log_path: &mut Option<PathBuf>,
    current_session_id: Option<&str>,
) -> Result<String, String> {
    match args.trim().to_ascii_lowercase().as_str() {
        "" | "status" => {
            if *debug_enabled {
                if let Some(log_path) = debug_log_path.as_ref() {
                    Ok(tr_value(
                        "Debug logging is on. Log file: {path}",
                        "path",
                        &log_path.display().to_string(),
                    ))
                } else {
                    Ok(tr("No active session."))
                }
            } else {
                Ok(tr("Debug logging is off."))
            }
        }
        "on" => {
            let Some(session_id) = current_session_id else {
                return Ok(tr("No active session."));
            };
            let log_path = enable_interactive_debug_log(session_id)?;
            *debug_enabled = true;
            *debug_log_path = Some(log_path.clone());
            Ok(tr_value(
                "Debug logging enabled. Log file: {path}",
                "path",
                &log_path.display().to_string(),
            ))
        }
        "off" => {
            *debug_enabled = false;
            *debug_log_path = None;
            Ok(tr("Debug logging disabled."))
        }
        _ => Ok(tr("Usage: /debug [on|off]")),
    }
}
