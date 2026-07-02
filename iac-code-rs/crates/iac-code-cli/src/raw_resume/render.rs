use std::io;
use std::os::fd::RawFd;
use std::time::{SystemTime, UNIX_EPOCH};

use iac_code_config::i18n::detect_language;
use iac_code_tui::{
    format_resume_session_size, short_resume_session_id, ResumePickerState, ResumeSessionEntry,
};

use crate::cli_i18n::tr;
use crate::raw_picker::{
    raw_picker_clear_sequence, raw_picker_fit_line_to_width, raw_picker_push_line,
    raw_picker_push_styled_line, raw_picker_query_prompt_line_with_styled_prompt_and_cursor_save,
    raw_picker_terminal_width, write_raw_interactive_fd_all, RawPickerSearchQuery,
};

pub(super) fn clear_raw_resume_picker(fd: RawFd, previous_lines: usize) -> io::Result<()> {
    write_raw_interactive_fd_all(
        fd,
        raw_resume_picker_clear_sequence(previous_lines).as_bytes(),
    )
}

/// Like [`raw_picker_clear_sequence`], but first moves the cursor back down to
/// the bottom of the block. `render_raw_resume_picker` ends by restoring the
/// cursor to the search box (the 3rd rendered line: title, blank, search) via
/// `\x1b[u`, so the plain clear sequence - which assumes a bottom-anchored
/// cursor - would clear the wrong rows and leave duplicated copies behind.
pub(crate) fn raw_resume_picker_clear_sequence(previous_lines: usize) -> String {
    const SEARCH_LINE_NUMBER: usize = 3;
    let mut output = String::new();
    if previous_lines > SEARCH_LINE_NUMBER {
        output.push_str(&format!(
            "\x1b[{}B",
            previous_lines.saturating_sub(SEARCH_LINE_NUMBER)
        ));
    }
    output.push_str(&raw_picker_clear_sequence(previous_lines));
    output
}

pub(crate) fn render_raw_resume_picker(
    fd: RawFd,
    previous_lines: usize,
    query: &RawPickerSearchQuery,
    state: &ResumePickerState,
) -> io::Result<usize> {
    let visible = state.visible_entries();
    let total = state.filtered_entries().len();
    let focus_pos = if total == 0 {
        0
    } else {
        state.focused_index() + 1
    };
    let mut item_line_count = if visible.is_empty() {
        1
    } else {
        visible.len() * 2
    };
    if state.visible_from() > 0 {
        item_line_count += 1;
    }
    let mut last_project = if state.visible_from() > 0 {
        state
            .filtered_entries()
            .get(state.visible_from() - 1)
            .map(|entry| entry.project_name.as_str())
    } else {
        None
    };
    for entry in visible {
        if !entry.project_name.is_empty() && Some(entry.project_name.as_str()) != last_project {
            item_line_count += 1;
            last_project = Some(entry.project_name.as_str());
        }
    }
    if state.visible_from() + visible.len() < total {
        item_line_count += 1;
    }
    let line_count = 6 + item_line_count;
    let width = raw_picker_terminal_width(fd);
    let mut output = raw_resume_picker_clear_sequence(previous_lines);
    let mut title = tr("Resume Session");
    if total > 0 {
        title.push_str(&format!(" ({focus_pos} of {total})"));
    }
    raw_picker_push_line(&mut output, &title, width);
    raw_picker_push_line(&mut output, "", width);
    output.push_str(&raw_resume_search_line(query, width));
    raw_picker_push_line(&mut output, "", width);
    if visible.is_empty() {
        raw_picker_push_dim_line(&mut output, &tr("No sessions found"), width);
    } else {
        if state.visible_from() > 0 {
            raw_picker_push_dim_line(&mut output, "↑", width);
        }
        let mut last_project = if state.visible_from() > 0 {
            state
                .filtered_entries()
                .get(state.visible_from() - 1)
                .map(|entry| entry.project_name.as_str())
        } else {
            None
        };
        for (index, entry) in visible.iter().enumerate() {
            let absolute_index = state.visible_from() + index;
            if !entry.project_name.is_empty() && Some(entry.project_name.as_str()) != last_project {
                raw_picker_push_dim_line(&mut output, &entry.project_name, width);
                last_project = Some(entry.project_name.as_str());
            }
            if absolute_index == state.focused_index() {
                let plain = format!("❯ {}", entry.title);
                let styled = format!("\x1b[1m\x1b[36m❯ \x1b[0m\x1b[1m{}\x1b[0m", entry.title);
                raw_picker_push_styled_line(&mut output, &plain, &styled, width);
            } else {
                raw_picker_push_line(&mut output, &format!("  {}", entry.title), width);
            }
            raw_picker_push_dim_line(
                &mut output,
                &format!("  {}", raw_resume_entry_metadata(entry)),
                width,
            );
        }
        if state.visible_from() + visible.len() < total {
            raw_picker_push_dim_line(&mut output, "↓", width);
        }
    }
    raw_picker_push_line(&mut output, "", width);
    output.push_str("\r\n\x1b[2K");
    raw_resume_push_footer_hint(&mut output, &raw_resume_picker_hints(), width);
    // Restore the cursor to the search box (saved in raw_resume_search_line) so
    // the caret sits where the user types, matching the skills picker.
    output.push_str("\x1b[u");
    write_raw_interactive_fd_all(fd, output.as_bytes())?;
    Ok(line_count)
}

fn raw_picker_push_dim_line(output: &mut String, line: &str, width: usize) {
    output.push_str("\r\n\x1b[2K\x1b[2m");
    output.push_str(&raw_picker_fit_line_to_width(line, width));
    output.push_str("\x1b[0m");
}

fn raw_resume_push_footer_hint(output: &mut String, line: &str, width: usize) {
    output.push_str("  \x1b[38;2;128;128;128m");
    output.push_str(&raw_picker_fit_line_to_width(line, width.saturating_sub(2)));
    output.push_str("\x1b[0m");
}

fn raw_resume_search_line(query: &RawPickerSearchQuery, width: usize) -> String {
    if query.text().is_empty() {
        let mut output = String::new();
        let placeholder = tr("Search...");
        let plain = format!("> {placeholder}");
        let styled = format!("\x1b[1m\x1b[36m> \x1b[0m\x1b[s\x1b[2m{placeholder}\x1b[0m");
        raw_picker_push_styled_line(&mut output, &plain, &styled, width);
        return output;
    }
    format!(
        "\r\n\x1b[2K{}",
        raw_picker_query_prompt_line_with_styled_prompt_and_cursor_save(
            "> ",
            "\x1b[1m\x1b[36m> \x1b[0m",
            query,
            width
        )
    )
}

fn raw_resume_entry_metadata(entry: &ResumeSessionEntry) -> String {
    let mut parts = vec![format_resume_modified_ago(entry.modified_at_epoch_seconds)];
    if entry.name.is_some() {
        parts.push(short_resume_session_id(&entry.session_id));
    }
    if let Some(branch) = entry
        .git_branch
        .as_deref()
        .filter(|branch| !branch.is_empty())
    {
        parts.push(branch.to_owned());
    }
    parts.push(format_resume_session_size(entry.size_bytes));
    parts.join(" · ")
}

fn format_resume_modified_ago(modified_at_epoch_seconds: u64) -> String {
    let now = SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .map(|duration| duration.as_secs())
        .unwrap_or(modified_at_epoch_seconds);
    let elapsed = now.saturating_sub(modified_at_epoch_seconds);
    let language = detect_language();
    let minute = 60;
    let hour = minute * 60;
    let day = hour * 24;
    if language == "zh" {
        if elapsed >= day {
            return format!("{} 天前", elapsed / day);
        }
        if elapsed >= hour {
            return format!("{} 小时前", elapsed / hour);
        }
        if elapsed >= minute {
            return format!("{} 分钟前", elapsed / minute);
        }
        return tr("just now");
    }
    if elapsed >= day {
        return format!("{}d ago", elapsed / day);
    }
    if elapsed >= hour {
        return format!("{}h ago", elapsed / hour);
    }
    if elapsed >= minute {
        return format!("{}m ago", elapsed / minute);
    }
    tr("just now")
}

fn raw_resume_picker_hints() -> String {
    format!(
        "Ctrl+A {} · Ctrl+B {} · Space {} · {} · Esc {}",
        tr("show all projects"),
        tr("current branch only"),
        tr("preview"),
        tr("type to search"),
        tr("cancel")
    )
}
