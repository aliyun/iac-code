use iac_code_config::paths::ConfigPaths;
use iac_code_core::{normalize_session_name, SessionStorage};

use crate::cli_i18n::{tr, tr_name};
use crate::interactive_commands::print_interactive_command_result;
use crate::session_utils::{current_git_branch, current_working_directory};

pub(super) fn print_interactive_rename(args: &str, current_session_id: Option<&str>) {
    let message = match interactive_rename_message(args, current_session_id) {
        Ok(message) => message,
        Err(error) => error,
    };
    print_interactive_command_result(&message);
}

fn interactive_rename_message(
    args: &str,
    current_session_id: Option<&str>,
) -> Result<String, String> {
    let parts = args.split_whitespace().collect::<Vec<_>>();
    if parts.is_empty() {
        return Ok(format!(
            "{}\n  {}",
            tr("Session name:"),
            tr("Rename cancelled")
        ));
    }
    if parts.len() != 1 {
        return Ok(tr("Usage: /rename <name>"));
    }
    let Some(session_id) = current_session_id else {
        return Ok("No active session to rename.".to_owned());
    };
    let name = normalize_session_name(parts[0])?;
    let paths = ConfigPaths::from_env().map_err(|error| error.to_string())?;
    let cwd = current_working_directory()?;
    let storage =
        SessionStorage::new(paths.subdirs().projects).map_err(|error| error.to_string())?;
    let git_branch = current_git_branch(&cwd);
    match storage.rename_session(&cwd, session_id, &name, git_branch.as_deref()) {
        Ok(result) if result == "unchanged" => {
            Ok(tr_name("Session is already named {name}", &name))
        }
        Ok(_) => Ok(tr_name("Renamed session to {name}", &name)),
        Err(error) => Ok(error.to_string()),
    }
}
