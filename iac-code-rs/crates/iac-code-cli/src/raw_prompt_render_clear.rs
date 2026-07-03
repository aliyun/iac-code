use std::os::fd::RawFd;

use iac_code_tui::terminal_dimensions;

use crate::raw_prompt_render_state::RawPromptRenderState;
use crate::raw_prompt_text::raw_prompt_visual_line_count;

pub(super) fn raw_prompt_terminal_width(fd: RawFd) -> usize {
    terminal_dimensions(fd)
        .ok()
        .flatten()
        .map(|size| size.columns)
        .unwrap_or(80)
        .max(1)
}

pub(super) fn raw_prompt_repaint_clear_state(
    previous_state: RawPromptRenderState,
    text: &str,
    ghost_text: &str,
    width: usize,
) -> RawPromptRenderState {
    if previous_state.line_count == 0 {
        return RawPromptRenderState::empty();
    }
    let ghost_text = raw_prompt_effective_ghost_text(text, ghost_text);
    let current_line_count = raw_prompt_visual_line_count(text, ghost_text, width);
    let previous_line_count = previous_state.rendered_line_count_at_width(width);
    let line_count = previous_state
        .line_count
        .max(previous_line_count)
        .max(current_line_count);
    RawPromptRenderState {
        line_count,
        cursor_row: previous_state
            .rendered_cursor_row_at_width(width)
            .min(line_count.saturating_sub(1)),
        rendered: None,
    }
}

pub(super) fn raw_prompt_effective_ghost_text<'a>(text: &str, ghost_text: &'a str) -> &'a str {
    if text.contains('\n') {
        ""
    } else {
        ghost_text
    }
}

#[cfg(test)]
pub(super) fn raw_prompt_repaint_clear_lines(
    previous_lines: usize,
    text: &str,
    ghost_text: &str,
    width: usize,
) -> usize {
    raw_prompt_repaint_clear_state(
        RawPromptRenderState::from_line_count_at_bottom(previous_lines),
        text,
        ghost_text,
        width,
    )
    .line_count
}

#[cfg(test)]
pub(super) fn raw_prompt_clear_sequence(previous_lines: usize) -> String {
    raw_prompt_clear_sequence_from_state(RawPromptRenderState::from_line_count_at_bottom(
        previous_lines,
    ))
}

pub(super) fn raw_prompt_clear_sequence_from_state(previous_state: RawPromptRenderState) -> String {
    if previous_state.line_count <= 1 {
        "\r\x1b[2K".to_owned()
    } else {
        let mut output = String::new();
        let cursor_row = previous_state
            .cursor_row
            .min(previous_state.line_count.saturating_sub(1));
        if cursor_row > 0 {
            output.push_str(&format!("\x1b[{cursor_row}A"));
        }
        output.push_str("\r\x1b[2K");
        for _ in 1..previous_state.line_count {
            output.push_str("\r\n\x1b[2K");
        }
        if previous_state.line_count > 1 {
            output.push_str(&format!("\x1b[{}A", previous_state.line_count - 1));
        }
        output.push('\r');
        output
    }
}
