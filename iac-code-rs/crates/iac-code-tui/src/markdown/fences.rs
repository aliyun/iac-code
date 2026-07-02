use std::borrow::Cow;

pub(super) fn unwrap_markdown_fences<'a>(markdown_source: &'a str) -> Cow<'a, str> {
    if !markdown_source.contains("```") && !markdown_source.contains("~~~") {
        return Cow::Borrowed(markdown_source);
    }

    #[derive(Clone, Copy)]
    struct Fence {
        marker: char,
        len: usize,
        is_markdown: bool,
        is_blockquoted: bool,
    }

    struct ActiveFence {
        fence: Fence,
        opening: String,
        content: String,
    }

    fn strip_line_indent(line: &str) -> Option<&str> {
        let without_newline = line.strip_suffix('\n').unwrap_or(line);
        let mut byte_index = 0usize;
        let mut column = 0usize;
        for byte in without_newline.as_bytes() {
            match byte {
                b' ' => {
                    byte_index += 1;
                    column += 1;
                }
                b'\t' => {
                    byte_index += 1;
                    column += 4;
                }
                _ => break,
            }
            if column >= 4 {
                return None;
            }
        }
        Some(&without_newline[byte_index..])
    }

    fn parse_open_fence(line: &str) -> Option<Fence> {
        let trimmed = strip_line_indent(line)?;
        let is_blockquoted = trimmed.trim_start().starts_with('>');
        let fence_scan_text = strip_blockquote_prefix(trimmed);
        let (marker, len) = parse_fence_marker(fence_scan_text)?;
        Some(Fence {
            marker,
            len,
            is_markdown: is_markdown_fence_info(fence_scan_text, len),
            is_blockquoted,
        })
    }

    fn is_close_fence(line: &str, fence: Fence) -> bool {
        let Some(trimmed) = strip_line_indent(line) else {
            return false;
        };
        let fence_scan_text = if fence.is_blockquoted {
            if !trimmed.trim_start().starts_with('>') {
                return false;
            }
            strip_blockquote_prefix(trimmed)
        } else {
            trimmed
        };
        if let Some((marker, len)) = parse_fence_marker(fence_scan_text) {
            marker == fence.marker && len >= fence.len && fence_scan_text[len..].trim().is_empty()
        } else {
            false
        }
    }

    let mut output = String::with_capacity(markdown_source.len());
    let mut active_fence: Option<ActiveFence> = None;
    for line in markdown_source.split_inclusive('\n') {
        if let Some(mut active) = active_fence.take() {
            if is_close_fence(line, active.fence) {
                if active.fence.is_markdown
                    && markdown_fence_contains_table(&active.content, active.fence.is_blockquoted)
                {
                    output.push_str(&active.content);
                } else {
                    output.push_str(&active.opening);
                    output.push_str(&active.content);
                    output.push_str(line);
                }
            } else {
                active.content.push_str(line);
                active_fence = Some(active);
            }
            continue;
        }

        if let Some(fence) = parse_open_fence(line) {
            active_fence = Some(ActiveFence {
                fence,
                opening: line.to_string(),
                content: String::new(),
            });
            continue;
        }

        output.push_str(line);
    }

    if let Some(active) = active_fence {
        output.push_str(&active.opening);
        output.push_str(&active.content);
    }

    Cow::Owned(output)
}

fn markdown_fence_contains_table(content: &str, is_blockquoted_fence: bool) -> bool {
    let mut previous_line: Option<&str> = None;
    for line in content.lines() {
        let text = if is_blockquoted_fence {
            strip_blockquote_prefix(line)
        } else {
            line
        };
        let trimmed = text.trim();
        if trimmed.is_empty() {
            previous_line = None;
            continue;
        }

        if let Some(previous) = previous_line {
            if is_table_header_line(previous)
                && !is_table_delimiter_line(previous)
                && is_table_delimiter_line(trimmed)
            {
                return true;
            }
        }

        previous_line = Some(trimmed);
    }
    false
}

fn parse_table_segments(line: &str) -> Option<Vec<&str>> {
    let trimmed = line.trim();
    if trimmed.is_empty() {
        return None;
    }

    let has_outer_pipe = trimmed.starts_with('|') || trimmed.ends_with('|');
    let content = trimmed.strip_prefix('|').unwrap_or(trimmed);
    let content = content.strip_suffix('|').unwrap_or(content);
    let raw_segments = split_unescaped_pipe(content);
    if !has_outer_pipe && raw_segments.len() <= 1 {
        return None;
    }

    let segments = raw_segments.into_iter().map(str::trim).collect::<Vec<_>>();
    (!segments.is_empty()).then_some(segments)
}

fn split_unescaped_pipe(content: &str) -> Vec<&str> {
    let mut segments = Vec::with_capacity(8);
    let mut start = 0;
    let bytes = content.as_bytes();
    let mut index = 0;
    while index < bytes.len() {
        if bytes[index] == b'\\' {
            index += 2;
        } else if bytes[index] == b'|' {
            segments.push(&content[start..index]);
            start = index + 1;
            index += 1;
        } else {
            index += 1;
        }
    }
    segments.push(&content[start..]);
    segments
}

fn is_table_header_line(line: &str) -> bool {
    parse_table_segments(line)
        .is_some_and(|segments| segments.iter().any(|segment| !segment.is_empty()))
}

fn is_table_delimiter_line(line: &str) -> bool {
    parse_table_segments(line)
        .is_some_and(|segments| segments.into_iter().all(is_table_delimiter_segment))
}

fn is_table_delimiter_segment(segment: &str) -> bool {
    let trimmed = segment.trim();
    if trimmed.is_empty() {
        return false;
    }
    let without_leading = trimmed.strip_prefix(':').unwrap_or(trimmed);
    let without_ends = without_leading.strip_suffix(':').unwrap_or(without_leading);
    without_ends.len() >= 3 && without_ends.chars().all(|character| character == '-')
}

fn parse_fence_marker(line: &str) -> Option<(char, usize)> {
    let first = line.as_bytes().first().copied()?;
    if first != b'`' && first != b'~' {
        return None;
    }
    let len = line.bytes().take_while(|byte| *byte == first).count();
    if len < 3 {
        return None;
    }
    Some((first as char, len))
}

fn is_markdown_fence_info(trimmed_line: &str, marker_len: usize) -> bool {
    let info = trimmed_line[marker_len..]
        .split_whitespace()
        .next()
        .unwrap_or_default();
    info.eq_ignore_ascii_case("md") || info.eq_ignore_ascii_case("markdown")
}

fn strip_blockquote_prefix(line: &str) -> &str {
    let mut rest = line.trim_start();
    loop {
        let Some(stripped) = rest.strip_prefix('>') else {
            return rest;
        };
        rest = stripped.strip_prefix(' ').unwrap_or(stripped).trim_start();
    }
}
