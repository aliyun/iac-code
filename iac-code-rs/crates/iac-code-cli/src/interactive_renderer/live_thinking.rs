use std::time::{Duration, Instant};

use iac_code_tui::terminal_display_width;

pub(super) fn push_thinking_delta(thinking: &mut String, text: &str) {
    if text.is_empty() {
        return;
    }
    if thinking.is_empty() {
        thinking.push_str(text);
        return;
    }
    if text == thinking {
        return;
    }
    if text.starts_with(thinking.as_str()) {
        thinking.push_str(&text[thinking.len()..]);
    } else {
        thinking.push_str(text);
    }
}

pub(super) fn render_due(
    last_live_thinking_render: &mut Option<Instant>,
    live_thinking_min_interval: Duration,
) -> bool {
    let now = Instant::now();
    let due = match last_live_thinking_render {
        Some(previous) => now.duration_since(*previous) >= live_thinking_min_interval,
        None => true,
    };
    if due {
        *last_live_thinking_render = Some(now);
    }
    due
}

pub(super) fn visible_trailing_lines(
    thinking: &str,
    terminal_width: usize,
    max_rows: usize,
) -> (Vec<&str>, usize) {
    // Show only the trailing rows so the transient region stays bounded as
    // the reasoning grows -- an ever-taller block makes the redraw flicker.
    let lines: Vec<&str> = thinking.lines().collect();
    let mut visible: Vec<&str> = Vec::new();
    let mut line_count = 0usize;
    for line in lines.iter().rev() {
        let rows = terminal_rows_for_text(&format!("▌ {line}"), terminal_width);
        if !visible.is_empty() && line_count + rows > max_rows {
            break;
        }
        line_count += rows;
        visible.push(line);
    }
    (visible, line_count)
}

pub(super) fn clear_rows(output: &mut String, rows: usize) {
    for _ in 0..rows {
        output.push_str("\x1b[1A\r\x1b[2K");
    }
}

fn terminal_rows_for_text(text: &str, width: usize) -> usize {
    terminal_display_width(text)
        .saturating_add(width.saturating_sub(1))
        .checked_div(width)
        .unwrap_or(1)
        .max(1)
}
