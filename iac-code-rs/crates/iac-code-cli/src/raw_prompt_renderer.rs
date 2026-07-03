use std::collections::BTreeMap;
use std::io;
use std::os::fd::RawFd;

use iac_code_tui::terminal_display_width;

use crate::cli_i18n::tr;
use crate::raw_picker::write_raw_interactive_fd_all;
use crate::raw_prompt_input::RawPromptPastedImage;
pub(super) use crate::raw_prompt_render_clear::raw_prompt_clear_sequence_from_state;
#[cfg(test)]
pub(super) use crate::raw_prompt_render_clear::{
    raw_prompt_clear_sequence, raw_prompt_repaint_clear_lines,
};
use crate::raw_prompt_render_clear::{
    raw_prompt_effective_ghost_text, raw_prompt_repaint_clear_state, raw_prompt_terminal_width,
};
pub(super) use crate::raw_prompt_render_state::RawPromptRenderState;
use crate::raw_prompt_render_state::RawPromptRenderedState;
use crate::raw_prompt_render_suggestions::raw_prompt_suggestion_overlay_lines;
pub(super) use crate::raw_prompt_render_suggestions::{
    raw_prompt_suggestion_overlay, RawPromptSuggestionOverlay,
};
use crate::raw_prompt_text::{
    raw_prompt_clamp_cursor, raw_prompt_cursor_position,
    raw_prompt_cursor_reposition_sequence_with_extra_lines, raw_prompt_image_links,
    raw_prompt_push_rendered_text, raw_prompt_push_rendered_text_with_image_links,
    raw_prompt_strip_ansi_sequences, raw_prompt_visual_line_count, RAW_PROMPT_PREFIX,
    RAW_PROMPT_PREFIX_STYLED,
};

#[cfg(unix)]
pub(super) struct RawPromptRenderParams<'a> {
    pub(super) previous_state: RawPromptRenderState,
    pub(super) text: &'a str,
    pub(super) cursor: usize,
    pub(super) ghost_text: &'a str,
    pub(super) suggestions: RawPromptSuggestionOverlay<'a>,
    pub(super) images: &'a [RawPromptPastedImage],
    pub(super) clipboard_has_image: bool,
}

#[cfg(unix)]
struct RawPromptRenderOutputParams<'a> {
    previous_state: RawPromptRenderState,
    text: &'a str,
    cursor: usize,
    ghost_text: &'a str,
    width: usize,
    suggestions: RawPromptSuggestionOverlay<'a>,
    image_links: &'a BTreeMap<usize, String>,
    clipboard_hint: Option<&'a str>,
}

#[cfg(unix)]
pub(super) fn clear_raw_interactive_prompt(
    fd: RawFd,
    previous_state: RawPromptRenderState,
) -> io::Result<()> {
    write_raw_interactive_fd_all(
        fd,
        raw_prompt_clear_sequence_from_state(previous_state).as_bytes(),
    )
}

#[cfg(unix)]
pub(super) fn clear_raw_interactive_prompt_current(
    fd: RawFd,
    previous_state: RawPromptRenderState,
    text: &str,
    ghost_text: &str,
) -> io::Result<()> {
    let width = raw_prompt_terminal_width(fd);
    let clear_state = raw_prompt_repaint_clear_state(previous_state, text, ghost_text, width);
    clear_raw_interactive_prompt(fd, clear_state)
}

#[cfg(unix)]
pub(super) fn render_raw_interactive_prompt_with_clipboard_hint(
    fd: RawFd,
    previous_state: RawPromptRenderState,
    text: &str,
    cursor: usize,
    ghost_text: &str,
    images: &[RawPromptPastedImage],
    clipboard_has_image: bool,
) -> io::Result<RawPromptRenderState> {
    render_raw_interactive_prompt_with_overlay_and_clipboard_hint(
        fd,
        RawPromptRenderParams {
            previous_state,
            text,
            cursor,
            ghost_text,
            suggestions: RawPromptSuggestionOverlay::empty(),
            images,
            clipboard_has_image,
        },
    )
}

#[cfg(unix)]
pub(super) fn render_raw_interactive_prompt_with_overlay_and_clipboard_hint(
    fd: RawFd,
    params: RawPromptRenderParams<'_>,
) -> io::Result<RawPromptRenderState> {
    let RawPromptRenderParams {
        previous_state,
        text,
        cursor,
        ghost_text,
        suggestions,
        images,
        clipboard_has_image,
    } = params;
    let width = raw_prompt_terminal_width(fd);
    let clipboard_hint = clipboard_has_image.then(|| tr("Image in clipboard · ctrl+v to paste"));
    let image_links = raw_prompt_image_links(images);
    let (output, state) = raw_prompt_render_output_with_params(RawPromptRenderOutputParams {
        previous_state,
        text,
        cursor,
        ghost_text,
        width,
        suggestions,
        image_links: &image_links,
        clipboard_hint: clipboard_hint.as_deref(),
    });
    write_raw_interactive_fd_all(fd, output.as_bytes()).map(|()| state)
}

#[cfg(all(unix, test))]
pub(super) fn raw_prompt_render_output(
    previous_lines: usize,
    text: &str,
    cursor: usize,
    ghost_text: &str,
    width: usize,
) -> (String, usize) {
    let (output, state) = raw_prompt_render_output_with_state(
        RawPromptRenderState::from_line_count_at_bottom(previous_lines),
        text,
        cursor,
        ghost_text,
        width,
    );
    (output, state.line_count)
}

#[cfg(all(unix, test))]
pub(super) fn raw_prompt_render_output_with_state(
    previous_state: RawPromptRenderState,
    text: &str,
    cursor: usize,
    ghost_text: &str,
    width: usize,
) -> (String, RawPromptRenderState) {
    raw_prompt_render_output_with_overlay(
        previous_state,
        text,
        cursor,
        ghost_text,
        width,
        RawPromptSuggestionOverlay::empty(),
    )
}

#[cfg(all(unix, test))]
pub(super) fn raw_prompt_render_output_with_image_links(
    previous_lines: usize,
    text: &str,
    cursor: usize,
    ghost_text: &str,
    width: usize,
    image_links: &BTreeMap<usize, String>,
) -> (String, usize) {
    let (output, state) = raw_prompt_render_output_with_params(RawPromptRenderOutputParams {
        previous_state: RawPromptRenderState::from_line_count_at_bottom(previous_lines),
        text,
        cursor,
        ghost_text,
        width,
        suggestions: RawPromptSuggestionOverlay::empty(),
        image_links,
        clipboard_hint: None,
    });
    (output, state.line_count)
}

#[cfg(all(unix, test))]
pub(super) fn raw_prompt_render_output_with_overlay(
    previous_state: RawPromptRenderState,
    text: &str,
    cursor: usize,
    ghost_text: &str,
    width: usize,
    suggestions: RawPromptSuggestionOverlay<'_>,
) -> (String, RawPromptRenderState) {
    let image_links = BTreeMap::new();
    raw_prompt_render_output_with_params(RawPromptRenderOutputParams {
        previous_state,
        text,
        cursor,
        ghost_text,
        width,
        suggestions,
        image_links: &image_links,
        clipboard_hint: None,
    })
}

#[cfg(unix)]
fn raw_prompt_render_output_with_params(
    params: RawPromptRenderOutputParams<'_>,
) -> (String, RawPromptRenderState) {
    let RawPromptRenderOutputParams {
        previous_state,
        text,
        cursor,
        ghost_text,
        width,
        suggestions,
        image_links,
        clipboard_hint,
    } = params;
    let ghost_text = raw_prompt_effective_ghost_text(text, ghost_text);
    let suggestion_lines = raw_prompt_suggestion_overlay_lines(suggestions, width);
    let content_line_count = raw_prompt_visual_line_count(text, ghost_text, width);
    let line_count = content_line_count + suggestion_lines.len();
    let clear_state = raw_prompt_repaint_clear_state(previous_state, text, ghost_text, width);
    let mut output = raw_prompt_clear_sequence_from_state(clear_state);
    output.push_str(RAW_PROMPT_PREFIX_STYLED);
    raw_prompt_push_rendered_text_with_image_links(&mut output, text, image_links);
    if !ghost_text.is_empty() {
        output.push_str("\x1b[2m");
        raw_prompt_push_rendered_text(&mut output, ghost_text);
        output.push_str("\x1b[0m");
    }
    raw_prompt_push_clipboard_hint(
        &mut output,
        clipboard_hint,
        text,
        ghost_text,
        width,
        content_line_count,
    );
    for line in &suggestion_lines {
        output.push_str("\r\n");
        output.push_str(line);
    }
    output.push_str(&raw_prompt_cursor_reposition_sequence_with_extra_lines(
        text,
        cursor,
        ghost_text,
        width,
        suggestion_lines.len(),
    ));
    let cursor_row = raw_prompt_cursor_position(text, cursor, width).line;
    (
        output,
        RawPromptRenderState {
            line_count,
            cursor_row,
            rendered: Some(RawPromptRenderedState {
                text: text.to_owned(),
                cursor: raw_prompt_clamp_cursor(text, cursor),
                ghost_text: ghost_text.to_owned(),
                suggestion_lines: suggestion_lines
                    .iter()
                    .map(|line| raw_prompt_strip_ansi_sequences(line))
                    .collect(),
            }),
        },
    )
}

#[cfg(unix)]
fn raw_prompt_push_clipboard_hint(
    output: &mut String,
    clipboard_hint: Option<&str>,
    text: &str,
    ghost_text: &str,
    width: usize,
    content_line_count: usize,
) {
    if content_line_count != 1 {
        return;
    }
    let Some(clipboard_hint) = clipboard_hint else {
        return;
    };
    let hint_width = terminal_display_width(clipboard_hint);
    let prompt_width = terminal_display_width(RAW_PROMPT_PREFIX)
        + terminal_display_width(text)
        + terminal_display_width(ghost_text);
    let gap_width = 2;
    if width < prompt_width + gap_width + hint_width {
        return;
    }

    output.push_str(&format!(
        "\x1b[s\x1b[{}G\x1b[2m",
        width.saturating_sub(hint_width).saturating_add(1)
    ));
    raw_prompt_push_rendered_text(output, clipboard_hint);
    output.push_str("\x1b[0m\x1b[u");
}

#[cfg(unix)]
pub(super) fn write_raw_interactive_prompt_newline(fd: RawFd) -> io::Result<()> {
    write_raw_interactive_fd_all(fd, b"\r\n")
}

#[cfg(unix)]
pub(super) fn write_raw_interactive_prompt_submit_newline(
    fd: RawFd,
    previous_state: RawPromptRenderState,
    prompt: &str,
    images: &[RawPromptPastedImage],
) -> io::Result<()> {
    let needs_final_render = previous_state.rendered.as_ref().is_some_and(|rendered| {
        rendered.text != prompt
            || !rendered.ghost_text.is_empty()
            || !rendered.suggestion_lines.is_empty()
    });
    if needs_final_render {
        render_raw_interactive_prompt_with_overlay_and_clipboard_hint(
            fd,
            RawPromptRenderParams {
                previous_state,
                text: prompt,
                cursor: prompt.len(),
                ghost_text: "",
                suggestions: RawPromptSuggestionOverlay::empty(),
                images,
                clipboard_has_image: false,
            },
        )?;
    }
    write_raw_interactive_prompt_newline(fd)
}
