use iac_code_config::paths::ConfigPaths;
use iac_code_core::{SessionEntry, SessionIndex};

use crate::cli_i18n::tr_value;
use crate::session_utils::{
    cross_project_message, current_working_directory, resolve_session_argument, same_project_path,
};

pub(super) fn interactive_resume_message(args: &str) -> Result<(String, Option<String>), String> {
    let paths = ConfigPaths::from_env().map_err(|error| error.to_string())?;
    let cwd = current_working_directory()?;
    let index = SessionIndex::new(paths.subdirs().projects);
    let arg = args.trim();
    if arg.is_empty() {
        let entries = index
            .list_for_cwd(&cwd)
            .map_err(|error| error.to_string())?;
        return Ok((format_session_list(&entries), None));
    }
    let entry = resolve_session_argument(&index, &cwd, arg)
        .map_err(|error| localize_interactive_resume_error(&error, arg))?;
    if !entry.cwd.is_empty() && !same_project_path(&entry.cwd, &cwd) {
        return Ok((cross_project_message(&entry.cwd, &entry.session_id), None));
    }
    let label = session_display_label(&entry);
    Ok((
        format!("Resuming session: {label} ({})", entry.session_id),
        Some(entry.session_id),
    ))
}

pub(super) fn latest_session_id_for_current_cwd() -> Result<Option<String>, String> {
    let paths = ConfigPaths::from_env().map_err(|error| error.to_string())?;
    let cwd = current_working_directory()?;
    let index = SessionIndex::new(paths.subdirs().projects);
    Ok(index
        .list_for_cwd(&cwd)
        .map_err(|error| error.to_string())?
        .into_iter()
        .next()
        .map(|entry| entry.session_id))
}

fn format_session_list(entries: &[SessionEntry]) -> String {
    if entries.is_empty() {
        return "No saved sessions for this project.".to_owned();
    }
    let mut output = "Sessions:".to_owned();
    for entry in entries.iter().take(20) {
        output.push_str("\n  - ");
        output.push_str(&session_display_label(entry));
        output.push_str(" (");
        output.push_str(&entry.session_id);
        output.push(')');
    }
    output
}

fn localize_interactive_resume_error(error: &str, arg: &str) -> String {
    if error == format!("Session not found: {arg}") {
        return tr_value("Session not found: {arg}", "arg", arg);
    }
    error.to_owned()
}

fn session_display_label(entry: &SessionEntry) -> String {
    entry
        .name
        .clone()
        .or_else(|| entry.auto_title.clone())
        .unwrap_or_else(|| entry.title.clone())
}
