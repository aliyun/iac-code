use std::collections::BTreeMap;
use std::path::Path;

use iac_code_tui::terminal_display_width;
use unicode_segmentation::UnicodeSegmentation;

use crate::raw_prompt_input::{raw_prompt_image_refs, RawPromptPastedImage};

pub(super) const RAW_PROMPT_PREFIX: &str = "❯ ";
pub(super) const RAW_PROMPT_PREFIX_STYLED: &str = "\x1b[1m\x1b[36m❯ \x1b[0m";

pub(super) fn raw_prompt_visual_line_count(text: &str, ghost_text: &str, width: usize) -> usize {
    let width = width.max(1);
    let mut state = RawPromptVisualPosition::new(width);
    state.push_text(RAW_PROMPT_PREFIX);
    state.push_text(text);
    state.push_text(ghost_text);
    state.line_count()
}

pub(super) fn raw_prompt_line_visual_line_count(text: &str, width: usize) -> usize {
    let width = width.max(1);
    let mut state = RawPromptVisualPosition::new(width);
    state.push_text(text);
    state.line_count()
}

#[derive(Clone, Copy)]
pub(super) struct RawPromptVisualPosition {
    pub(super) line: usize,
    pub(super) column: usize,
    width: usize,
}

impl RawPromptVisualPosition {
    fn new(width: usize) -> Self {
        Self {
            line: 0,
            column: 0,
            width: width.max(1),
        }
    }

    fn line_count(&self) -> usize {
        self.line + 1
    }

    pub(super) fn push_text(&mut self, text: &str) {
        for grapheme in text.graphemes(true) {
            if grapheme == "\n" {
                self.line += 1;
                self.column = 0;
                continue;
            }
            let ch_width = terminal_display_width(grapheme);
            if ch_width == 0 {
                continue;
            }
            if self.column > 0 && self.column.saturating_add(ch_width) > self.width {
                self.line += 1;
                self.column = 0;
            }
            self.column = self.column.saturating_add(ch_width).min(self.width);
        }
    }
}

pub(super) fn raw_prompt_cursor_reposition_sequence_with_extra_lines(
    text: &str,
    cursor: usize,
    ghost_text: &str,
    width: usize,
    extra_lines_after_content: usize,
) -> String {
    let cursor_position = raw_prompt_cursor_position(text, cursor, width);
    let cursor = raw_prompt_clamp_cursor(text, cursor);

    let mut final_position = cursor_position;
    final_position.push_text(&text[cursor..]);
    final_position.push_text(ghost_text);
    final_position.line = final_position
        .line
        .saturating_add(extra_lines_after_content);

    raw_prompt_relative_cursor_sequence(final_position, cursor_position)
}

pub(super) fn raw_prompt_cursor_position(
    text: &str,
    cursor: usize,
    width: usize,
) -> RawPromptVisualPosition {
    let cursor = raw_prompt_clamp_cursor(text, cursor);
    let mut cursor_position = RawPromptVisualPosition::new(width);
    cursor_position.push_text(RAW_PROMPT_PREFIX);
    cursor_position.push_text(&text[..cursor]);
    cursor_position
}

fn raw_prompt_relative_cursor_sequence(
    final_position: RawPromptVisualPosition,
    cursor_position: RawPromptVisualPosition,
) -> String {
    let mut output = String::new();
    if final_position.line > cursor_position.line {
        output.push_str(&format!(
            "\x1b[{}A\r",
            final_position.line - cursor_position.line
        ));
        if cursor_position.column > 0 {
            output.push_str(&format!("\x1b[{}C", cursor_position.column));
        }
    } else if final_position.column > cursor_position.column {
        output.push_str(&format!(
            "\x1b[{}D",
            final_position.column - cursor_position.column
        ));
    } else if final_position.column < cursor_position.column {
        output.push_str(&format!(
            "\x1b[{}C",
            cursor_position.column - final_position.column
        ));
    }
    output
}

pub(super) fn raw_prompt_clamp_cursor(text: &str, cursor: usize) -> usize {
    let mut cursor = cursor.min(text.len());
    while !text.is_char_boundary(cursor) {
        cursor -= 1;
    }
    cursor
}

pub(super) fn raw_prompt_push_rendered_text(output: &mut String, text: &str) {
    for ch in text.chars() {
        if ch == '\n' {
            output.push_str("\r\n");
        } else {
            output.push(ch);
        }
    }
}

pub(super) fn raw_prompt_push_rendered_text_with_image_links(
    output: &mut String,
    text: &str,
    image_links: &BTreeMap<usize, String>,
) {
    let refs = raw_prompt_image_refs(text);
    if refs.is_empty() {
        raw_prompt_push_rendered_text(output, text);
        return;
    }

    let mut cursor = 0usize;
    for image_ref in refs {
        if image_ref.start > cursor {
            raw_prompt_push_rendered_text(output, &text[cursor..image_ref.start]);
        }
        let label = &text[image_ref.start..image_ref.end];
        let link = image_links.get(&image_ref.id);
        if let Some(link) = link {
            output.push_str("\x1b]8;;");
            output.push_str(link);
            output.push_str("\x1b\\");
        }
        output.push_str("\x1b[1m\x1b[96m");
        raw_prompt_push_rendered_text(output, label);
        output.push_str("\x1b[0m");
        if link.is_some() {
            output.push_str("\x1b]8;;\x1b\\");
        }
        cursor = image_ref.end;
    }
    if cursor < text.len() {
        raw_prompt_push_rendered_text(output, &text[cursor..]);
    }
}

pub(super) fn raw_prompt_image_links(images: &[RawPromptPastedImage]) -> BTreeMap<usize, String> {
    images
        .iter()
        .enumerate()
        .filter_map(|(index, image)| {
            image
                .source_path
                .as_ref()
                .map(|path| (index + 1, raw_prompt_file_uri(path)))
        })
        .collect()
}

fn raw_prompt_file_uri(path: &Path) -> String {
    format!(
        "file://{}",
        raw_prompt_uri_path_encode(&path.to_string_lossy())
    )
}

fn raw_prompt_uri_path_encode(value: &str) -> String {
    const HEX: &[u8; 16] = b"0123456789ABCDEF";
    let mut output = String::new();
    for byte in value.as_bytes() {
        match *byte {
            b'A'..=b'Z' | b'a'..=b'z' | b'0'..=b'9' | b'/' | b'.' | b'-' | b'_' | b'~' | b':' => {
                output.push(*byte as char)
            }
            other => {
                output.push('%');
                output.push(HEX[(other >> 4) as usize] as char);
                output.push(HEX[(other & 0x0F) as usize] as char);
            }
        }
    }
    output
}

pub(super) fn raw_prompt_strip_ansi_sequences(input: &str) -> String {
    let mut output = String::new();
    let mut chars = input.chars().peekable();
    while let Some(ch) = chars.next() {
        if ch == '\x1b' && chars.peek() == Some(&'[') {
            chars.next();
            for sequence_ch in chars.by_ref() {
                if ('@'..='~').contains(&sequence_ch) {
                    break;
                }
            }
            continue;
        }
        if ch == '\x1b' && chars.peek() == Some(&']') {
            chars.next();
            let mut saw_escape = false;
            for sequence_ch in chars.by_ref() {
                if saw_escape && sequence_ch == '\\' {
                    break;
                }
                if sequence_ch == '\u{7}' {
                    break;
                }
                saw_escape = sequence_ch == '\x1b';
            }
            continue;
        }
        output.push(ch);
    }
    output
}
