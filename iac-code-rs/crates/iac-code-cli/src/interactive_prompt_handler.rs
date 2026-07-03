use std::io::{self, Write};
use std::time::{Duration, Instant};

use iac_code_exec::{OutputFormat, EXIT_OK};
use iac_code_protocol::message::AgentMessageContent;
use iac_code_protocol::StreamEvent;
use iac_code_tui::{CommandCatalog, InputHistory};

use crate::cli_args::Cli;
use crate::cli_i18n::tr_name;
use crate::debug_logging::{enable_interactive_debug_log, enable_startup_debug_log};
use crate::headless_runner::{
    run_prompt_from_cli_with_content, run_prompt_from_cli_with_content_and_sink,
    RunPromptWithSinkOptions,
};
use crate::interactive_banner::clear_interactive_screen_and_print_banner;
use crate::interactive_commands::{
    handle_interactive_command, refresh_current_session_debug, InteractiveCommandContext,
    InteractiveCommandResult,
};
use crate::interactive_renderer::{
    write_interactive_agent_result, InteractiveEventRenderer,
    INTERACTIVE_LIVE_THINKING_MIN_INTERVAL,
};
use crate::interactive_session::{append_interactive_transcript, InteractiveSessionState};
use crate::interactive_shell_escape::handle_interactive_shell_escape;
use crate::interactive_skill_invocation::{
    parse_interactive_skill_invocation, run_interactive_skill_from_cli,
};
use crate::interactive_working::start_interactive_working_indicator;

#[derive(Copy, Clone, Debug, Eq, PartialEq)]
pub(super) enum InteractiveInputHistoryMode {
    Persist,
    Session,
    None,
}

pub(super) fn append_interactive_input_history(
    history: &mut Option<InputHistory>,
    prompt: &str,
    mode: InteractiveInputHistoryMode,
) {
    let Some(history) = history else {
        return;
    };
    match mode {
        InteractiveInputHistoryMode::Persist => {
            let _ = history.append(prompt);
        }
        InteractiveInputHistoryMode::Session => {
            let _ = history.append_session_only(prompt);
        }
        InteractiveInputHistoryMode::None => {
            history.reset_navigation();
        }
    }
}

pub(super) fn interactive_slash_command_history_mode(
    prompt: &str,
) -> Option<InteractiveInputHistoryMode> {
    let command_text = prompt.strip_prefix('/').unwrap_or(prompt).trim();
    let command = command_text
        .split_whitespace()
        .next()
        .unwrap_or_default()
        .to_ascii_lowercase();
    match CommandCatalog::default_commands().find(&command) {
        Some(command) if command.name == "exit" => Some(InteractiveInputHistoryMode::None),
        Some(_) => Some(InteractiveInputHistoryMode::Session),
        None => None,
    }
}

pub(super) fn handle_interactive_prompt(
    cli: &Cli,
    output_format: OutputFormat,
    state: &mut InteractiveSessionState,
    line: &str,
) -> Option<i32> {
    handle_interactive_prompt_input(cli, output_format, state, line, None)
}

pub(super) fn handle_interactive_prompt_input(
    cli: &Cli,
    output_format: OutputFormat,
    state: &mut InteractiveSessionState,
    line: &str,
    prompt_content: Option<AgentMessageContent>,
) -> Option<i32> {
    let prompt = line.trim();
    if prompt.is_empty() {
        return None;
    }
    if prompt.starts_with('!') {
        if let Err(error) = handle_interactive_shell_escape(cli, prompt) {
            eprintln!("{error}");
        }
        return None;
    }
    if prompt.starts_with('$') {
        let result = match run_interactive_skill_from_cli(
            cli,
            prompt,
            output_format,
            &state.resume,
            state.continue_session,
            state.task_manager.clone(),
        ) {
            Ok(Some(result)) => result,
            Ok(None) => {
                let name = parse_interactive_skill_invocation(prompt)
                    .map(|invocation| invocation.name)
                    .unwrap_or_default();
                eprintln!(
                    "{}",
                    tr_name(
                        "Unknown skill: ${name}. Type / to list commands and skills.",
                        &name
                    )
                );
                return None;
            }
            Err(error) => {
                eprintln!("{error}");
                return None;
            }
        };
        let elapsed = Duration::ZERO;
        append_interactive_input_history(
            &mut state.input_history,
            prompt,
            InteractiveInputHistoryMode::Persist,
        );
        append_interactive_transcript(state, prompt, &result);
        write_interactive_agent_result(&result, output_format, elapsed);
        state.exit_code = result.exit_code;
        if state.exit_code != EXIT_OK {
            return Some(state.exit_code);
        }
        state.turn_count = state.turn_count.saturating_add(1);
        state.token_count = state.token_count.saturating_add(result.token_count);
        refresh_current_session_debug(
            &mut state.current_session_id,
            state.debug_enabled,
            &mut state.debug_log_path,
        );
        state.resume.clear();
        state.continue_session = true;
        return None;
    }
    if prompt.starts_with('/') {
        if let Some(history_mode) = interactive_slash_command_history_mode(prompt) {
            append_interactive_input_history(&mut state.input_history, prompt, history_mode);
        }
        let command_context = InteractiveCommandContext {
            exit_code: state.exit_code,
            turn_count: state.turn_count,
            token_count: state.token_count,
            debug_enabled: &mut state.debug_enabled,
            debug_log_path: &mut state.debug_log_path,
            current_session_id: state.current_session_id.as_deref(),
            output_format,
        };
        match handle_interactive_command(cli, prompt, command_context) {
            InteractiveCommandResult::Continue => {}
            InteractiveCommandResult::Clear => {
                state.resume.clear();
                state.continue_session = false;
                state.exit_code = EXIT_OK;
                state.turn_count = 0;
                state.token_count = 0;
                state.current_session_id = None;
                state.transcript_lines.clear();
                // Re-establish a fresh debug log so the re-printed banner shows
                // debug mode just like startup (a new session begins next turn).
                state.debug_log_path = if state.debug_enabled {
                    enable_startup_debug_log("interactive").ok()
                } else {
                    None
                };
                clear_interactive_screen_and_print_banner(cli, state);
            }
            InteractiveCommandResult::Resume(session_id) => {
                state.resume = session_id.clone();
                state.continue_session = false;
                state.exit_code = EXIT_OK;
                state.turn_count = 0;
                state.token_count = 0;
                state.current_session_id = Some(session_id);
                if state.debug_enabled {
                    state.debug_log_path = state
                        .current_session_id
                        .as_deref()
                        .and_then(|session_id| enable_interactive_debug_log(session_id).ok());
                }
            }
            InteractiveCommandResult::Exit(code) => return Some(code),
            InteractiveCommandResult::Unsupported(command) => {
                match run_interactive_skill_from_cli(
                    cli,
                    prompt,
                    output_format,
                    &state.resume,
                    state.continue_session,
                    state.task_manager.clone(),
                ) {
                    Ok(Some(result)) => {
                        let elapsed = Duration::ZERO;
                        append_interactive_input_history(
                            &mut state.input_history,
                            prompt,
                            InteractiveInputHistoryMode::Persist,
                        );
                        append_interactive_transcript(state, prompt, &result);
                        write_interactive_agent_result(&result, output_format, elapsed);
                        state.exit_code = result.exit_code;
                        if state.exit_code != EXIT_OK {
                            return Some(state.exit_code);
                        }
                        state.turn_count = state.turn_count.saturating_add(1);
                        state.token_count = state.token_count.saturating_add(result.token_count);
                        refresh_current_session_debug(
                            &mut state.current_session_id,
                            state.debug_enabled,
                            &mut state.debug_log_path,
                        );
                        state.resume.clear();
                        state.continue_session = true;
                        return None;
                    }
                    Ok(None) => {}
                    Err(error) => {
                        eprintln!("{error}");
                        return None;
                    }
                }
                eprintln!(
                    "{}",
                    tr_name(
                        "Unknown command: /{name}. Type /help for available commands.",
                        &command
                    )
                );
            }
        }
        return None;
    }
    append_interactive_input_history(
        &mut state.input_history,
        prompt,
        InteractiveInputHistoryMode::Persist,
    );
    let started_at = Instant::now();
    let mut working_indicator = start_interactive_working_indicator(output_format);
    let mut stream_renderer =
        InteractiveEventRenderer::streaming_with_live_updates(working_indicator.is_some());
    // Throttle the live-thinking repaint so the spinner isn't paused/redrawn on
    // every reasoning delta (which makes it flicker); tests leave this at zero.
    stream_renderer.live_thinking_min_interval = INTERACTIVE_LIVE_THINKING_MIN_INTERVAL;
    let result = if output_format == OutputFormat::Text {
        let indicator = working_indicator.as_ref();
        let mut event_sink = |event: &StreamEvent| {
            stream_renderer.push_event(event);
            let output = stream_renderer.take_output();
            if !output.is_empty() {
                if let Some(indicator) = indicator {
                    indicator.pause_and_clear();
                }
                print!("{output}");
                let _ = io::stdout().flush();
                if let Some(indicator) = indicator {
                    indicator.resume();
                }
            }
        };
        run_prompt_from_cli_with_content_and_sink(RunPromptWithSinkOptions {
            cli,
            prompt,
            prompt_content,
            output_format,
            resume: &state.resume,
            continue_session: state.continue_session,
            shared_task_manager: Some(state.task_manager.clone()),
            event_sink: Some(&mut event_sink),
        })
    } else {
        run_prompt_from_cli_with_content(
            cli,
            prompt,
            prompt_content,
            output_format,
            &state.resume,
            state.continue_session,
            Some(state.task_manager.clone()),
        )
    };
    let result = match result {
        Ok(result) => result,
        Err(error) => {
            if let Some(indicator) = working_indicator.as_mut() {
                indicator.stop_and_clear();
            }
            eprintln!("{error}");
            return Some(1);
        }
    };
    let elapsed = started_at.elapsed();
    append_interactive_transcript(state, prompt, &result);
    if output_format == OutputFormat::Text {
        if let Some(indicator) = working_indicator.as_mut() {
            indicator.stop_and_clear();
        }
        print!("{}", stream_renderer.finish_with_elapsed(elapsed));
        eprint!("{}", result.stderr);
    } else {
        write_interactive_agent_result(&result, output_format, elapsed);
    }
    state.exit_code = result.exit_code;
    if state.exit_code != EXIT_OK {
        return Some(state.exit_code);
    }
    state.turn_count = state.turn_count.saturating_add(1);
    state.token_count = state.token_count.saturating_add(result.token_count);
    refresh_current_session_debug(
        &mut state.current_session_id,
        state.debug_enabled,
        &mut state.debug_log_path,
    );
    state.resume.clear();
    state.continue_session = true;
    None
}
