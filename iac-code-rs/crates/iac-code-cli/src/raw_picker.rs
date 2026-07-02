use std::io;

#[cfg(unix)]
use std::os::fd::RawFd;

use iac_code_tui::{
    suffix_start_for_display_width, terminal_dimensions, terminal_display_width, PromptBuffer,
    PromptKeyEvent,
};
use unicode_segmentation::UnicodeSegmentation;

use super::raw_prompt_text::raw_prompt_clamp_cursor;

#[cfg(unix)]
#[derive(Default)]
pub(super) struct RawPickerSearchQuery {
    buffer: PromptBuffer,
}

#[cfg(unix)]
impl RawPickerSearchQuery {
    pub(super) fn new() -> Self {
        Self::default()
    }

    #[cfg(test)]
    pub(super) fn from_text(text: &str) -> Self {
        Self {
            buffer: PromptBuffer::from_text(text),
        }
    }

    pub(super) fn text(&self) -> &str {
        self.buffer.text()
    }

    fn cursor(&self) -> usize {
        self.buffer.cursor()
    }

    pub(super) fn handle_key(&mut self, event: &PromptKeyEvent) -> bool {
        let key = event.key.as_str();
        if (event.ctrl && key == "a") || key == "home" {
            self.buffer.move_home();
            return true;
        }
        if (event.ctrl && key == "e") || key == "end" {
            self.buffer.move_end();
            return true;
        }
        if event.ctrl && key == "k" {
            self.buffer.kill_to_end();
            return true;
        }
        if event.ctrl && key == "u" {
            self.buffer.kill_to_start();
            return true;
        }
        if event.ctrl && key == "w" {
            self.buffer.delete_previous_word();
            return true;
        }
        if key == "left" {
            self.buffer.move_left();
            return true;
        }
        if key == "right" {
            self.buffer.move_right();
            return true;
        }
        if key == "backspace" {
            self.buffer.backspace();
            return true;
        }
        if key == "delete" {
            self.buffer.delete();
            return true;
        }
        if raw_picker_insertable_text(event) {
            self.buffer.insert_text(&event.char_text);
            return true;
        }
        false
    }
}

#[cfg(unix)]
pub(super) fn raw_picker_query_prompt_line(
    prompt: &str,
    query: &RawPickerSearchQuery,
    width: usize,
) -> String {
    raw_picker_query_prompt_line_with_styled_prompt(prompt, prompt, query, width)
}

#[cfg(unix)]
pub(super) fn raw_picker_query_prompt_line_with_styled_prompt(
    prompt: &str,
    styled_prompt: &str,
    query: &RawPickerSearchQuery,
    width: usize,
) -> String {
    raw_picker_query_prompt_line_internal(prompt, styled_prompt, query, width, false)
}

#[cfg(unix)]
pub(super) fn raw_picker_query_prompt_line_with_styled_prompt_and_cursor_save(
    prompt: &str,
    styled_prompt: &str,
    query: &RawPickerSearchQuery,
    width: usize,
) -> String {
    raw_picker_query_prompt_line_internal(prompt, styled_prompt, query, width, true)
}

#[cfg(unix)]
fn raw_picker_query_prompt_line_internal(
    prompt: &str,
    styled_prompt: &str,
    query: &RawPickerSearchQuery,
    width: usize,
    save_cursor: bool,
) -> String {
    if width == 0 {
        return String::new();
    }
    let prompt_width = terminal_display_width(prompt);
    if prompt_width >= width {
        return raw_picker_fit_line_to_width(prompt, width);
    }

    let text = query.text();
    let cursor = raw_picker_clamp_grapheme_cursor(text, query.cursor());
    let content_width = width - prompt_width;
    let mut output = styled_prompt.to_owned();
    let mut used = 0usize;
    let mut index = raw_picker_query_visible_start(text, cursor, content_width);
    let mut cursor_saved = false;

    while index < text.len() && used < content_width {
        let Some((grapheme, next_index)) = raw_picker_next_grapheme(text, index) else {
            break;
        };
        let grapheme_width = terminal_display_width(grapheme);
        if grapheme_width > 0 && used.saturating_add(grapheme_width) > content_width {
            break;
        }
        if index == cursor {
            if save_cursor {
                output.push_str("\x1b[s");
                cursor_saved = true;
            }
            output.push_str("\x1b[7m");
            output.push_str(grapheme);
            output.push_str("\x1b[0m");
        } else {
            output.push_str(grapheme);
        }
        used = used.saturating_add(grapheme_width);
        index = next_index;
    }

    if cursor == text.len() && used < content_width {
        if save_cursor {
            output.push_str("\x1b[s");
            cursor_saved = true;
        }
        output.push_str("\x1b[7m \x1b[0m");
    }
    if save_cursor && !cursor_saved {
        output.push_str("\x1b[s");
    }
    output
}

#[cfg(unix)]
fn raw_picker_query_visible_start(text: &str, cursor: usize, content_width: usize) -> usize {
    let cursor = raw_picker_clamp_grapheme_cursor(text, cursor);
    if content_width == 0 {
        return cursor;
    }
    if cursor == text.len() {
        return raw_picker_suffix_start_for_width(text, content_width.saturating_sub(1));
    }
    let Some((cursor_grapheme, _)) = raw_picker_next_grapheme(text, cursor) else {
        return 0;
    };
    let cursor_width = terminal_display_width(cursor_grapheme);
    raw_picker_suffix_start_for_width(&text[..cursor], content_width.saturating_sub(cursor_width))
}

#[cfg(unix)]
fn raw_picker_suffix_start_for_width(text: &str, width: usize) -> usize {
    suffix_start_for_display_width(text, width)
}

#[cfg(unix)]
fn raw_picker_next_grapheme(text: &str, index: usize) -> Option<(&str, usize)> {
    if index > text.len() || !text.is_char_boundary(index) {
        return None;
    }
    let grapheme = text[index..].graphemes(true).next()?;
    Some((grapheme, index + grapheme.len()))
}

#[cfg(unix)]
fn raw_picker_clamp_grapheme_cursor(text: &str, cursor: usize) -> usize {
    let cursor = raw_prompt_clamp_cursor(text, cursor);
    if cursor == text.len() || text.is_empty() {
        return cursor;
    }
    text.grapheme_indices(true)
        .take_while(|(index, _)| *index <= cursor)
        .map(|(index, _)| index)
        .last()
        .unwrap_or(0)
}

#[cfg(unix)]
pub(super) fn raw_picker_insertable_text(event: &PromptKeyEvent) -> bool {
    !event.char_text.is_empty()
        && !event.ctrl
        && !event.alt
        && event.char_text.chars().all(|ch| !ch.is_control())
}

#[cfg(unix)]
pub(super) fn clear_raw_picker(fd: RawFd, previous_lines: usize) -> io::Result<()> {
    write_raw_interactive_fd_all(fd, raw_picker_clear_sequence(previous_lines).as_bytes())
}

#[cfg(unix)]
pub(super) fn raw_picker_clear_sequence(previous_lines: usize) -> String {
    let mut output = String::new();
    if previous_lines > 0 {
        output.push_str(&format!("\x1b[{previous_lines}A"));
    }
    output.push_str("\r\x1b[2K");
    for _ in 0..previous_lines {
        output.push_str("\r\n\x1b[2K");
    }
    if previous_lines > 0 {
        output.push_str(&format!("\x1b[{previous_lines}A"));
    }
    output.push('\r');
    output
}

#[cfg(unix)]
pub(super) fn raw_picker_terminal_width(fd: RawFd) -> usize {
    terminal_dimensions(fd)
        .ok()
        .flatten()
        .map(|size| size.columns)
        .unwrap_or(80)
        .max(1)
}

#[cfg(unix)]
pub(super) fn raw_picker_push_line(output: &mut String, line: &str, width: usize) {
    output.push_str("\r\n\x1b[2K");
    output.push_str(&raw_picker_fit_line_to_width(line, width));
}

#[cfg(unix)]
pub(super) fn raw_picker_push_styled_line(
    output: &mut String,
    plain: &str,
    styled: &str,
    width: usize,
) {
    output.push_str("\r\n\x1b[2K");
    if terminal_display_width(plain) <= width {
        output.push_str(styled);
    } else {
        output.push_str(&raw_picker_fit_line_to_width(plain, width));
    }
}

#[cfg(unix)]
pub(super) fn raw_picker_fit_line_to_width(line: &str, width: usize) -> String {
    if width == 0 {
        return String::new();
    }
    if terminal_display_width(line) <= width {
        return line.to_owned();
    }

    const ELLIPSIS: &str = "...";
    let ellipsis_width = terminal_display_width(ELLIPSIS);
    if width <= ellipsis_width {
        return ".".repeat(width);
    }

    let content_width = width - ellipsis_width;
    let mut fitted = String::new();
    let mut used = 0usize;
    for grapheme in line.graphemes(true) {
        let grapheme_width = terminal_display_width(grapheme);
        if used.saturating_add(grapheme_width) > content_width {
            break;
        }
        fitted.push_str(grapheme);
        used += grapheme_width;
    }
    fitted.push_str(ELLIPSIS);
    fitted
}

#[cfg(unix)]
pub(super) fn write_raw_interactive_fd_all(fd: RawFd, mut bytes: &[u8]) -> io::Result<()> {
    while !bytes.is_empty() {
        let status = unsafe { libc::write(fd, bytes.as_ptr().cast(), bytes.len()) };
        if status < 0 {
            return Err(io::Error::last_os_error());
        }
        if status == 0 {
            return Err(io::Error::new(
                io::ErrorKind::WriteZero,
                "terminal fd write returned zero bytes",
            ));
        }
        bytes = &bytes[status as usize..];
    }
    Ok(())
}
