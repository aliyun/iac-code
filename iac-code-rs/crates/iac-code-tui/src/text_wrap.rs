use crate::width::{
    take_first_grapheme, take_prefix_by_display_width, terminal_display_width,
    truncate_to_display_width, usable_content_width,
};

mod url;

use url::{text_contains_url_like, wrap_url_aware_line};

pub fn wrap_transcript_lines(lines: &[String], width: usize) -> Vec<String> {
    if width == 0 {
        return Vec::new();
    }

    let mut wrapped = Vec::new();
    for line in lines {
        wrap_transcript_line(line, width, &mut wrapped);
    }
    wrapped
}

pub fn wrap_transcript_lines_tail(lines: &[String], width: usize, max_lines: usize) -> Vec<String> {
    if width == 0 || max_lines == 0 {
        return Vec::new();
    }

    let mut tail_reversed = Vec::new();
    for line in lines.iter().rev() {
        let mut wrapped_line = Vec::new();
        wrap_transcript_line(line, width, &mut wrapped_line);

        for wrapped in wrapped_line.into_iter().rev() {
            tail_reversed.push(wrapped);
            if tail_reversed.len() == max_lines {
                tail_reversed.reverse();
                return tail_reversed;
            }
        }
    }

    tail_reversed.reverse();
    tail_reversed
}

pub(crate) fn wrap_plain_line(line: &str, width: usize, lines: &mut Vec<String>) {
    if width == 0 {
        return;
    }
    if line.is_empty() {
        lines.push(String::new());
        return;
    }
    if text_contains_url_like(line) {
        lines.extend(wrap_url_aware_line(line, width));
        return;
    }

    wrap_word_aware_line(line, width, lines);
}

pub(crate) fn wrap_prefixed_text(
    initial_prefix: &str,
    continuation_prefix: &str,
    text: &str,
    width: usize,
    lines: &mut Vec<String>,
) {
    if width == 0 {
        return;
    }

    if text.is_empty() {
        push_prefixed_logical_line(initial_prefix, continuation_prefix, "", width, lines);
        return;
    }

    let mut first = true;
    for logical_line in text.lines() {
        let prefix = if first {
            initial_prefix
        } else {
            continuation_prefix
        };
        push_prefixed_logical_line(prefix, continuation_prefix, logical_line, width, lines);
        first = false;
    }
}

fn wrap_transcript_line(line: &str, width: usize, lines: &mut Vec<String>) {
    if let Some(text) = line.strip_prefix("❯ ") {
        wrap_prefixed_text("❯ ", "  ", text, width, lines);
    } else if let Some(text) = line.strip_prefix("     ") {
        wrap_prefixed_text("     ", "     ", text, width, lines);
    } else {
        wrap_plain_line(line, width, lines);
    }
}

fn wrap_word_aware_line(line: &str, width: usize, lines: &mut Vec<String>) {
    let mut remaining = line;
    while !remaining.is_empty() {
        let (chunk, rest) = take_prefix_by_display_width(remaining, width);
        if chunk.is_empty() {
            let (fallback, fallback_rest) = take_first_grapheme(remaining);
            lines.push(fallback.to_owned());
            remaining = fallback_rest;
            continue;
        }

        if rest.is_empty() {
            lines.push(chunk.to_owned());
            return;
        }

        if let Some((line_end, next_start)) = last_word_wrap_break(chunk) {
            let head = remaining[..line_end].trim_end_matches(char::is_whitespace);
            if !head.is_empty() {
                lines.push(head.to_owned());
                remaining = remaining[next_start..].trim_start_matches(char::is_whitespace);
                continue;
            }
        }

        lines.push(chunk.to_owned());
        remaining = rest;
    }
}

fn last_word_wrap_break(chunk: &str) -> Option<(usize, usize)> {
    let mut last_break = None;
    for (index, ch) in chunk.char_indices() {
        if ch.is_whitespace() {
            last_break = Some((index, index + ch.len_utf8()));
        }
    }
    last_break
}

fn push_prefixed_logical_line(
    initial_prefix: &str,
    continuation_prefix: &str,
    line: &str,
    width: usize,
    lines: &mut Vec<String>,
) {
    let mut prefix = initial_prefix;
    let mut remaining = line;

    loop {
        let Some(content_width) = usable_content_width(width, terminal_display_width(prefix))
        else {
            lines.push(truncate_to_display_width(prefix, width));
            return;
        };

        if remaining.is_empty() {
            lines.push(prefix.to_owned());
            return;
        }

        if text_contains_url_like(remaining) {
            for chunk in wrap_url_aware_line(remaining, content_width) {
                lines.push(format!("{prefix}{chunk}"));
                prefix = continuation_prefix;
            }
            return;
        }

        let (chunk, rest) = take_prefix_by_display_width(remaining, content_width);
        if chunk.is_empty() {
            lines.push(prefix.to_owned());
            wrap_plain_line(remaining, width, lines);
            return;
        }

        if !rest.is_empty() {
            if let Some((line_end, next_start)) = last_word_wrap_break(chunk) {
                let head = remaining[..line_end].trim_end_matches(char::is_whitespace);
                if !head.is_empty() {
                    lines.push(format!("{prefix}{head}"));
                    remaining = remaining[next_start..].trim_start_matches(char::is_whitespace);
                    prefix = continuation_prefix;
                    continue;
                }
            }
        }

        lines.push(format!("{prefix}{chunk}"));
        if rest.is_empty() {
            return;
        }
        remaining = rest;
        prefix = continuation_prefix;
    }
}
