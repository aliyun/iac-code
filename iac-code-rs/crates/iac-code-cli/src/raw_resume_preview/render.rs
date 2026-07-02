use std::io;
use std::os::fd::RawFd;

use iac_code_tui::{short_resume_session_id, ResumePickerState, ResumeSessionEntry};

use crate::raw_picker::{raw_picker_fit_line_to_width, write_raw_interactive_fd_all};

pub(super) fn render_raw_resume_preview(
    fd: RawFd,
    entry: &ResumeSessionEntry,
    message_count: usize,
    body_lines: &[String],
    rows: usize,
    width: usize,
    state: &mut ResumePickerState,
) -> io::Result<()> {
    let header_lines = raw_resume_preview_header_lines(entry, message_count, width);
    let body_height = rows.saturating_sub(header_lines.len() + 1).max(1);
    state.set_preview_body_height(body_height);
    let total = body_lines.len();
    let mut body_block = Vec::new();

    if total <= body_height {
        body_block.extend(body_lines.iter().cloned());
    } else {
        let inner_height = body_height.saturating_sub(2).max(1);
        let max_offset = total.saturating_sub(inner_height);
        let offset = state.preview_scroll_offset().min(max_offset);
        let end = total.saturating_sub(offset);
        let start = end.saturating_sub(inner_height);
        body_block.push(raw_resume_preview_scroll_marker("up", start, width));
        body_block.extend(body_lines[start..end].iter().cloned());
        body_block.push(raw_resume_preview_scroll_marker(
            "down",
            total.saturating_sub(end),
            width,
        ));
    }

    let mut output = "\x1b[H\x1b[2J".to_owned();
    for line in &header_lines {
        output.push_str(line);
        output.push_str("\r\n");
    }
    for line in &body_block {
        output.push_str(line);
        output.push_str("\r\n");
    }
    let used_rows = header_lines.len() + body_block.len();
    for _ in 0..rows.saturating_sub(used_rows + 1) {
        output.push_str("\r\n");
    }
    output.push_str(&format!(
        "\x1b[{rows};1H\x1b[2K{}",
        raw_picker_fit_line_to_width("Enter resume  Esc back  ↑↓ scroll", width)
    ));
    write_raw_interactive_fd_all(fd, output.as_bytes())
}

fn raw_resume_preview_header_lines(
    entry: &ResumeSessionEntry,
    message_count: usize,
    width: usize,
) -> Vec<String> {
    let title = entry
        .name
        .as_deref()
        .or(entry.auto_title.as_deref())
        .unwrap_or(&entry.title);
    let branch = entry
        .git_branch
        .as_deref()
        .map(|branch| format!(" · {branch}"))
        .unwrap_or_default();
    vec![
        raw_picker_fit_line_to_width(&format!("Resume Session - {title}"), width),
        raw_picker_fit_line_to_width(
            &format!(
                "{} · {} · {} messages{}",
                short_resume_session_id(&entry.session_id),
                entry.project_name,
                message_count,
                branch
            ),
            width,
        ),
        String::new(),
    ]
}

fn raw_resume_preview_scroll_marker(direction: &str, hidden: usize, width: usize) -> String {
    let marker = match direction {
        "up" => format!("↑ {hidden} older lines"),
        _ => format!("↓ {hidden} newer lines"),
    };
    raw_picker_fit_line_to_width(&marker, width)
}
