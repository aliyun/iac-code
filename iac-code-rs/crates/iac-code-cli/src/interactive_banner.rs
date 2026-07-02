use std::env;
use std::io::{self, IsTerminal, Write};
#[cfg(unix)]
use std::os::fd::AsRawFd;
use std::path::Path;

use iac_code_providers::provider_descriptor;
#[cfg(test)]
use iac_code_tui::render_welcome_banner_lines;
#[cfg(unix)]
use iac_code_tui::terminal_dimensions;
use iac_code_tui::{render_welcome_banner_ansi_lines, WelcomeBannerLabels, WelcomeBannerState};

use super::interactive_status::resolve_interactive_status_provider_model;
use crate::cli_args::Cli;
use crate::cli_i18n::{tr, tr_dynamic};
use crate::cli_runtime::VERSION;
use crate::debug_logging::interactive_startup_banner_debug_log_display_path;
use crate::interactive_session::InteractiveSessionState;
use crate::session_utils::current_working_directory;

pub(super) fn should_print_interactive_startup_banner(
    stdin_is_terminal: bool,
    stdout_is_terminal: bool,
) -> bool {
    stdin_is_terminal && stdout_is_terminal
}

pub(super) fn print_interactive_startup_banner(
    cli: &Cli,
    session_id: Option<&str>,
    session_name: Option<&str>,
    debug_log_path: Option<&Path>,
) {
    let cwd = current_working_directory().unwrap_or_else(|_| ".".to_owned());
    let (provider, model) = resolve_interactive_status_provider_model(cli);
    let username = current_username();
    let debug_log_display_path =
        debug_log_path.map(interactive_startup_banner_debug_log_display_path);
    let lines = interactive_startup_banner_ansi_lines(InteractiveStartupBannerAnsiOptions {
        model: &model,
        cwd: Path::new(&cwd),
        provider_display: interactive_startup_banner_provider_display(&provider).as_deref(),
        username: &username,
        session_id,
        session_name,
        debug_log_path: debug_log_display_path.as_deref(),
        terminal_width: interactive_startup_banner_width(),
    });
    for line in lines {
        println!("{line}");
    }
}

pub(super) fn interactive_clear_screen_sequence() -> &'static str {
    "\x1b[H\x1b[2J\x1b[3J"
}

pub(super) fn clear_interactive_screen_and_print_banner(
    cli: &Cli,
    state: &InteractiveSessionState,
) {
    if !should_print_interactive_startup_banner(
        io::stdin().is_terminal(),
        io::stdout().is_terminal(),
    ) {
        return;
    }
    print!("{}", interactive_clear_screen_sequence());
    let _ = io::stdout().flush();
    let resume = state
        .current_session_id
        .as_deref()
        .or_else(|| (!state.resume.trim().is_empty()).then_some(state.resume.trim()));
    print_interactive_startup_banner(cli, resume, None, state.debug_log_path.as_deref());
}

pub(super) fn interactive_startup_banner_provider_display(provider: &str) -> Option<String> {
    if provider.is_empty() || provider == "not configured" {
        return None;
    }
    provider_descriptor(provider)
        .map(|descriptor| tr_dynamic(&descriptor.display_name))
        .or_else(|| Some(tr_dynamic(provider)))
}

pub(super) fn interactive_startup_banner_width() -> usize {
    #[cfg(unix)]
    {
        if let Ok(Some(dimensions)) = terminal_dimensions(io::stdout().as_raw_fd()) {
            return dimensions.columns;
        }
    }
    env::var("COLUMNS")
        .ok()
        .and_then(|value| value.parse::<usize>().ok())
        .filter(|columns| *columns > 0)
        .unwrap_or(80)
}

fn localized_welcome_banner_labels() -> WelcomeBannerLabels {
    WelcomeBannerLabels {
        welcome_back: tr("Welcome back"),
        description: tr("Your AI-powered Infrastructure as Code assistant"),
        session: tr("Session"),
        debug_mode: tr("Debug mode"),
        log_file: tr("Log file"),
    }
}

fn current_username() -> String {
    env::var("USER")
        .or_else(|_| env::var("USERNAME"))
        .ok()
        .filter(|value| !value.trim().is_empty())
        .unwrap_or_else(|| "User".to_owned())
}

#[cfg(test)]
pub(super) fn interactive_startup_banner_lines(
    model: &str,
    cwd: &Path,
    provider_display: Option<&str>,
    username: &str,
    session_id: Option<&str>,
    session_name: Option<&str>,
    debug_log_path: Option<&Path>,
) -> Vec<String> {
    let mut state =
        WelcomeBannerState::new(model, cwd.to_path_buf(), VERSION).with_username(username);
    if let Some(provider_display) = provider_display {
        state = state.with_provider_display(provider_display);
    }
    if let Some(session_id) = session_id {
        state = state.with_session(session_id, session_name);
    }
    if let Some(debug_log_path) = debug_log_path {
        state = state.with_debug_log_path(debug_log_path.to_path_buf());
    }
    render_welcome_banner_lines(&state)
}

pub(super) struct InteractiveStartupBannerAnsiOptions<'a> {
    pub(super) model: &'a str,
    pub(super) cwd: &'a Path,
    pub(super) provider_display: Option<&'a str>,
    pub(super) username: &'a str,
    pub(super) session_id: Option<&'a str>,
    pub(super) session_name: Option<&'a str>,
    pub(super) debug_log_path: Option<&'a Path>,
    pub(super) terminal_width: usize,
}

pub(super) fn interactive_startup_banner_ansi_lines(
    options: InteractiveStartupBannerAnsiOptions<'_>,
) -> Vec<String> {
    let mut state = WelcomeBannerState::new(options.model, options.cwd.to_path_buf(), VERSION)
        .with_username(options.username)
        .with_labels(localized_welcome_banner_labels());
    if let Some(provider_display) = options.provider_display {
        state = state.with_provider_display(provider_display);
    }
    if let Some(session_id) = options.session_id {
        state = state.with_session(session_id, options.session_name);
    }
    if let Some(debug_log_path) = options.debug_log_path {
        state = state.with_debug_log_path(debug_log_path.to_path_buf());
    }
    render_welcome_banner_ansi_lines(&state, options.terminal_width)
}
