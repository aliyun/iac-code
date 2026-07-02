use std::env;
use std::io::{self, BufRead, IsTerminal};
#[cfg(unix)]
use std::os::fd::AsRawFd;
use std::path::PathBuf;
use std::time::{Duration, Instant};

use iac_code_config::paths::ConfigPaths;
use iac_code_core::SessionStorage;
use iac_code_exec::{OutputFormat, EXIT_OK};
use iac_code_tools::TaskManager;
use iac_code_tui::InputHistory;

use crate::cli_args::Cli;
use crate::cli_i18n::tr;
use crate::debug_logging::{enable_interactive_debug_log, enable_startup_debug_log};
use crate::interactive_banner::{
    print_interactive_startup_banner, should_print_interactive_startup_banner,
};
use crate::interactive_prompt_handler::{
    handle_interactive_prompt, handle_interactive_prompt_input,
};
use crate::interactive_session::{raw_prompt_action_context, InteractiveSessionState};
#[cfg(unix)]
use crate::raw_prompt_input::{
    read_raw_interactive_prompt_input_with_context, RawPromptPastedImage,
};
#[cfg(unix)]
use crate::raw_suggestions::raw_interactive_skill_catalog;
use crate::session_utils::{current_git_branch, current_working_directory, new_session_id};

pub(super) const INTERACTIVE_CTRL_C_EXIT_WINDOW: Duration = Duration::from_millis(1500);

#[cfg(unix)]
pub(super) fn should_use_raw_interactive_input(
    stdin_is_terminal: bool,
    stdout_is_terminal: bool,
) -> bool {
    stdin_is_terminal && stdout_is_terminal
}

#[cfg(not(unix))]
pub(super) fn should_use_raw_interactive_input(
    _stdin_is_terminal: bool,
    _stdout_is_terminal: bool,
) -> bool {
    false
}

pub(super) fn interactive_ctrl_c_warning_line() -> String {
    tr("Press Ctrl+C again to exit.")
}

pub(super) fn interactive_exit_text_lines(current_session_id: Option<&str>) -> Vec<String> {
    let mut lines = vec![tr("Goodbye!")];
    if let Some(session_id) = current_session_id.filter(|session_id| !session_id.trim().is_empty())
    {
        lines.push(tr("Resume this session with:"));
        lines.push(format!("iac-code --resume {session_id}"));
    }
    lines
}

fn print_interactive_dim_line(line: &str) {
    println!("\x1b[2m{line}\x1b[0m");
}

fn print_interactive_exit_text(current_session_id: Option<&str>) {
    for line in interactive_exit_text_lines(current_session_id) {
        print_interactive_dim_line(&line);
    }
}

pub(super) fn interactive_ctrl_c_exit_requested(
    last_ctrl_c_at: Option<Instant>,
    now: Instant,
) -> bool {
    last_ctrl_c_at
        .map(|last| now.duration_since(last) <= INTERACTIVE_CTRL_C_EXIT_WINDOW)
        .unwrap_or(false)
}

pub(super) fn initialize_interactive_startup_session(
    cli: &Cli,
    stdin_is_terminal: bool,
    stdout_is_terminal: bool,
    debug_log_path: &mut Option<PathBuf>,
) -> Option<String> {
    if !should_use_raw_interactive_input(stdin_is_terminal, stdout_is_terminal) {
        return None;
    }
    if !cli.resume.trim().is_empty() || cli.continue_session {
        return None;
    }
    let paths = ConfigPaths::from_env().ok()?;
    let cwd = current_working_directory().ok()?;
    let storage = SessionStorage::new(paths.subdirs().projects).ok()?;
    let session_id = new_session_id();
    storage
        .save(&cwd, &session_id, &[], current_git_branch(&cwd).as_deref())
        .ok()?;
    if cli.debug {
        *debug_log_path = enable_interactive_debug_log(&session_id).ok();
    }
    Some(session_id)
}

fn load_interactive_input_history() -> Option<InputHistory> {
    ConfigPaths::from_env()
        .ok()
        .map(|paths| InputHistory::new(paths.history_path))
}

pub(super) fn run_interactive_cli(cli: &Cli, output_format: OutputFormat) -> i32 {
    let stdin = io::stdin();
    let stdin_is_terminal = stdin.is_terminal();
    let stdout_is_terminal = io::stdout().is_terminal();
    let mut debug_log_path: Option<PathBuf> = None;
    let startup_session_id = initialize_interactive_startup_session(
        cli,
        stdin_is_terminal,
        stdout_is_terminal,
        &mut debug_log_path,
    );
    if cli.debug && debug_log_path.is_none() {
        debug_log_path = enable_startup_debug_log("interactive").ok();
    }
    let current_session_id = if !cli.resume.trim().is_empty() || cli.continue_session {
        None
    } else {
        startup_session_id
    };
    let mut state = InteractiveSessionState {
        resume: cli.resume.clone(),
        continue_session: cli.continue_session,
        exit_code: EXIT_OK,
        turn_count: 0,
        token_count: 0,
        debug_enabled: cli.debug,
        debug_log_path,
        current_session_id,
        task_manager: TaskManager::new(),
        input_history: load_interactive_input_history(),
        transcript_lines: Vec::new(),
    };
    if should_print_interactive_startup_banner(stdin_is_terminal, stdout_is_terminal) {
        let startup_resume = state
            .current_session_id
            .as_deref()
            .or_else(|| (!state.resume.trim().is_empty()).then_some(state.resume.trim()));
        print_interactive_startup_banner(
            cli,
            startup_resume,
            None,
            state.debug_log_path.as_deref(),
        );
    }

    #[cfg(unix)]
    if should_use_raw_interactive_input(stdin_is_terminal, stdout_is_terminal) {
        let suggestion_root = env::current_dir().unwrap_or_else(|_| PathBuf::from("."));
        let suggestion_root_text = suggestion_root.to_string_lossy().into_owned();
        let raw_paths = ConfigPaths::from_env().ok();
        let skill_catalog = raw_interactive_skill_catalog(&suggestion_root);
        let mut last_ctrl_c_at: Option<Instant> = None;
        // Persist pasted images for the lifetime of the session so a recalled
        // `[Image #N]` (Up-arrow) stays clickable and re-attaches on submit.
        let mut session_pasted_images: Vec<RawPromptPastedImage> = Vec::new();
        loop {
            let action_context =
                raw_prompt_action_context(raw_paths.as_ref(), &suggestion_root_text, &state);
            match read_raw_interactive_prompt_input_with_context(
                stdin.as_raw_fd(),
                state.input_history.as_mut(),
                &suggestion_root,
                skill_catalog.clone(),
                &action_context,
                &mut session_pasted_images,
            ) {
                Ok(Some(input)) => {
                    last_ctrl_c_at = None;
                    if input.prehandled {
                        state.transcript_lines.extend(input.transcript_lines);
                        continue;
                    }
                    if let Some(code) = handle_interactive_prompt_input(
                        cli,
                        output_format,
                        &mut state,
                        &input.text,
                        input.prompt_content,
                    ) {
                        return code;
                    }
                }
                Ok(None) => {
                    let now = Instant::now();
                    if interactive_ctrl_c_exit_requested(last_ctrl_c_at, now) {
                        print_interactive_exit_text(state.current_session_id.as_deref());
                        return state.exit_code;
                    }
                    last_ctrl_c_at = Some(now);
                    print_interactive_dim_line(&interactive_ctrl_c_warning_line());
                }
                Err(error) => {
                    eprintln!("Error reading stdin: {error}");
                    return 1;
                }
            }
        }
    }

    for line in stdin.lock().lines() {
        let line = match line {
            Ok(line) => line,
            Err(error) => {
                eprintln!("Error reading stdin: {error}");
                return 1;
            }
        };
        if let Some(code) = handle_interactive_prompt(cli, output_format, &mut state, &line) {
            return code;
        }
    }
    state.exit_code
}
