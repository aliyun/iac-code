use std::io;
use std::os::fd::RawFd;

use iac_code_core::SessionStorage;
use iac_code_protocol::message::AgentMessage;
use iac_code_tui::{terminal_dimensions, RawInputCapture, ResumePickerState, ResumeSessionEntry};

use super::raw_prompt_context::RawPromptActionContext;
use super::raw_transcript::{RawAlternateScreenGuard, RawMouseTrackingGuard};

mod render;
mod replay;

use render::render_raw_resume_preview;

#[cfg(unix)]
pub(super) fn read_raw_resume_preview(
    fd: RawFd,
    capture: &RawInputCapture,
    context: &RawPromptActionContext,
    state: &mut ResumePickerState,
) -> io::Result<Option<String>> {
    let mut screen = RawAlternateScreenGuard::enter(fd)?;
    let mut mouse = RawMouseTrackingGuard::enter(fd)?;
    let entry = state.focused_entry().cloned();
    let Some(entry) = entry else {
        state.exit_preview();
        mouse.exit()?;
        screen.exit()?;
        return Ok(None);
    };
    let messages = raw_resume_preview_messages(context, &entry);
    let message_count = messages.len();
    let mut cached_width = 0usize;
    let mut body_lines: Vec<String> = Vec::new();
    loop {
        let dimensions = terminal_dimensions(fd).ok().flatten();
        let rows = dimensions.map(|size| size.rows).unwrap_or(24).max(1);
        let width = dimensions.map(|size| size.columns).unwrap_or(80).max(1);
        if width != cached_width {
            body_lines = raw_resume_preview_body_lines(&messages, width);
            cached_width = width;
        }
        render_raw_resume_preview(fd, &entry, message_count, &body_lines, rows, width, state)?;
        let Some(event) = capture.read_key(None)? else {
            continue;
        };
        let key = event.key.as_str();
        if key == "escape" {
            state.exit_preview();
            mouse.exit()?;
            screen.exit()?;
            return Ok(None);
        }
        if event.ctrl && key == "c" {
            state.cancel();
            mouse.exit()?;
            screen.exit()?;
            return Ok(None);
        }
        if key == "enter" {
            let selected = state.select_focused().map(|entry| entry.session_id.clone());
            state.exit_preview();
            mouse.exit()?;
            screen.exit()?;
            return Ok(selected);
        }
        if key == "up" || (event.ctrl && key == "p") {
            state.scroll_preview(1);
            continue;
        }
        if key == "down" || (event.ctrl && key == "n") {
            state.scroll_preview(-1);
            continue;
        }
        if key == "wheel_up" {
            state.wheel_preview_up();
            continue;
        }
        if key == "wheel_down" {
            state.wheel_preview_down();
            continue;
        }
        if key == "pageup" {
            state.page_preview_up();
            continue;
        }
        if key == "pagedown" {
            state.page_preview_down();
            continue;
        }
        if key == "home" {
            state.jump_preview_start();
            continue;
        }
        if key == "end" {
            state.jump_preview_end();
        }
    }
}

#[cfg(unix)]
fn raw_resume_preview_messages(
    context: &RawPromptActionContext,
    entry: &ResumeSessionEntry,
) -> Vec<AgentMessage> {
    let Some(paths) = &context.config_paths else {
        return Vec::new();
    };
    let Ok(storage) = SessionStorage::new(paths.subdirs().projects) else {
        return Vec::new();
    };
    storage
        .load(&entry.cwd, &entry.session_id)
        .unwrap_or_default()
}

/// Replay stored session messages into preview lines with the same look as a
/// live `--resume` (markdown-rendered assistant text, collapsed tool headers and
/// one-line tool-result summaries). Mirrors the Python `Renderer.replay_history`
/// path used by the resume picker preview. Returned lines may carry ANSI styling
/// and are already fitted to `width`, so callers must not re-truncate them.
#[cfg(unix)]
pub(super) fn raw_resume_preview_body_lines(
    messages: &[AgentMessage],
    width: usize,
) -> Vec<String> {
    replay::raw_resume_preview_body_lines(messages, width)
}
