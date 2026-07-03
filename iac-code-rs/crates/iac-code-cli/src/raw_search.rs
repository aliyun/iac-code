use std::fs;
use std::io;
#[cfg(unix)]
use std::os::fd::RawFd;
use std::path::Path;

#[cfg(unix)]
use iac_code_tui::{
    build_quick_open_items, FuzzyPickerState, GlobalSearchItem, HistoryMessage, HistorySearchState,
    InputHistory, PickerItem, RawInputCapture,
};

#[cfg(unix)]
use super::raw_picker::{
    clear_raw_picker, raw_picker_clear_sequence, raw_picker_push_line,
    raw_picker_query_prompt_line, raw_picker_terminal_width, write_raw_interactive_fd_all,
    RawPickerSearchQuery,
};

#[cfg(unix)]
pub(super) fn read_raw_history_search(
    fd: RawFd,
    capture: &RawInputCapture,
    input_history: Option<&InputHistory>,
) -> io::Result<Option<String>> {
    let messages = raw_history_search_messages(input_history);
    let mut state = HistorySearchState::new(messages, 5);
    let mut query = RawPickerSearchQuery::new();
    let mut rendered_lines = 0usize;
    rendered_lines = render_raw_history_search(fd, rendered_lines, &query, &state)?;

    loop {
        let Some(event) = capture.read_key(None)? else {
            continue;
        };
        let key = event.key.as_str();
        if key == "enter" {
            let selected = state.select_focused().map(|item| item.content);
            clear_raw_history_search(fd, rendered_lines)?;
            return Ok(selected);
        }
        if key == "escape" || (event.ctrl && key == "c") {
            clear_raw_history_search(fd, rendered_lines)?;
            return Ok(None);
        }
        if key == "up" || (event.ctrl && key == "p") {
            state.move_focus(-1);
            rendered_lines = render_raw_history_search(fd, rendered_lines, &query, &state)?;
            continue;
        }
        if key == "down" || (event.ctrl && key == "n") {
            state.move_focus(1);
            rendered_lines = render_raw_history_search(fd, rendered_lines, &query, &state)?;
            continue;
        }
        if query.handle_key(&event) {
            state.update_query(query.text());
            rendered_lines = render_raw_history_search(fd, rendered_lines, &query, &state)?;
            continue;
        }
    }
}

#[cfg(unix)]
fn raw_history_search_messages(input_history: Option<&InputHistory>) -> Vec<HistoryMessage> {
    input_history
        .map(InputHistory::entries)
        .unwrap_or_default()
        .into_iter()
        .map(|entry| HistoryMessage::text("user", entry))
        .collect()
}

pub(super) fn read_raw_quick_open(
    fd: RawFd,
    capture: &RawInputCapture,
    root: &Path,
) -> io::Result<Option<String>> {
    let mut state = FuzzyPickerState::new_static(raw_quick_open_picker_items(root), 5);
    let mut query = RawPickerSearchQuery::new();
    let mut rendered_lines = 0usize;
    rendered_lines = render_raw_quick_open(fd, rendered_lines, &query, &state)?;

    loop {
        let Some(event) = capture.read_key(None)? else {
            continue;
        };
        let key = event.key.as_str();
        if key == "enter" {
            let selected = state
                .select_focused()
                .map(|item| format!("@{}", item.display));
            clear_raw_picker(fd, rendered_lines)?;
            return Ok(selected);
        }
        if key == "escape" || (event.ctrl && key == "c") {
            clear_raw_picker(fd, rendered_lines)?;
            return Ok(None);
        }
        if key == "up" || (event.ctrl && key == "p") {
            state.move_focus(-1);
            rendered_lines = render_raw_quick_open(fd, rendered_lines, &query, &state)?;
            continue;
        }
        if key == "down" || (event.ctrl && key == "n") {
            state.move_focus(1);
            rendered_lines = render_raw_quick_open(fd, rendered_lines, &query, &state)?;
            continue;
        }
        if query.handle_key(&event) {
            state.update_query(query.text());
            rendered_lines = render_raw_quick_open(fd, rendered_lines, &query, &state)?;
            continue;
        }
    }
}

#[cfg(unix)]
fn raw_quick_open_picker_items(root: &Path) -> Vec<PickerItem> {
    build_quick_open_items(root)
        .into_iter()
        .map(|item| PickerItem::new(item.key, item.display).with_filter_text(item.filter_text))
        .collect()
}

#[cfg(unix)]
fn render_raw_quick_open(
    fd: RawFd,
    previous_lines: usize,
    query: &RawPickerSearchQuery,
    state: &FuzzyPickerState,
) -> io::Result<usize> {
    let visible = state.visible_items();
    let line_count = visible.len() + 2;
    let selected_index = state.focused_index().saturating_sub(state.visible_from());
    let width = raw_picker_terminal_width(fd);
    let mut output = raw_picker_clear_sequence(previous_lines);
    output.push_str(&raw_picker_query_prompt_line("quick> ", query, width));
    for (index, item) in visible.iter().enumerate() {
        let marker = if index == selected_index { ">" } else { " " };
        raw_picker_push_line(&mut output, &format!("{marker} {}", item.display), width);
    }
    raw_picker_push_line(&mut output, "Enter select  Esc cancel", width);
    write_raw_interactive_fd_all(fd, output.as_bytes())?;
    Ok(line_count)
}

#[cfg(unix)]
pub(super) fn read_raw_global_search(
    fd: RawFd,
    capture: &RawInputCapture,
    root: &Path,
) -> io::Result<Option<String>> {
    let root = root.to_path_buf();
    let mut state =
        FuzzyPickerState::new_dynamic(move |query| raw_global_search_picker_items(&root, query), 5);
    let mut query = RawPickerSearchQuery::new();
    let mut rendered_lines = 0usize;
    rendered_lines = render_raw_global_search(fd, rendered_lines, &query, &state)?;

    loop {
        let Some(event) = capture.read_key(None)? else {
            continue;
        };
        let key = event.key.as_str();
        if key == "enter" {
            let selected = state
                .select_focused()
                .map(|item| format!("@{}", item.display));
            clear_raw_picker(fd, rendered_lines)?;
            return Ok(selected);
        }
        if key == "escape" || (event.ctrl && key == "c") {
            clear_raw_picker(fd, rendered_lines)?;
            return Ok(None);
        }
        if key == "up" || (event.ctrl && key == "p") {
            state.move_focus(-1);
            rendered_lines = render_raw_global_search(fd, rendered_lines, &query, &state)?;
            continue;
        }
        if key == "down" || (event.ctrl && key == "n") {
            state.move_focus(1);
            rendered_lines = render_raw_global_search(fd, rendered_lines, &query, &state)?;
            continue;
        }
        if query.handle_key(&event) {
            state.update_query(query.text());
            rendered_lines = render_raw_global_search(fd, rendered_lines, &query, &state)?;
            continue;
        }
    }
}

#[cfg(unix)]
fn raw_global_search_picker_items(root: &Path, query: &str) -> Vec<PickerItem> {
    let query = query.trim();
    if query.is_empty() {
        return Vec::new();
    }

    let mut items = Vec::new();
    for file in build_quick_open_items(root) {
        if items.len() >= 100 {
            break;
        }
        let Ok(bytes) = fs::read(&file.file_path) else {
            continue;
        };
        let content = String::from_utf8_lossy(&bytes);
        for (line_index, line) in content.lines().enumerate() {
            if !line.contains(query) {
                continue;
            }
            let item = GlobalSearchItem::new(&file.file_path, root, line_index + 1, line);
            items.push(PickerItem::new(item.key, item.display).with_filter_text(item.filter_text));
            if items.len() >= 100 {
                break;
            }
        }
    }
    items
}

#[cfg(unix)]
fn render_raw_global_search(
    fd: RawFd,
    previous_lines: usize,
    query: &RawPickerSearchQuery,
    state: &FuzzyPickerState,
) -> io::Result<usize> {
    let visible = state.visible_items();
    let line_count = visible.len() + 2;
    let selected_index = state.focused_index().saturating_sub(state.visible_from());
    let width = raw_picker_terminal_width(fd);
    let mut output = raw_picker_clear_sequence(previous_lines);
    output.push_str(&raw_picker_query_prompt_line("search> ", query, width));
    for (index, item) in visible.iter().enumerate() {
        let marker = if index == selected_index { ">" } else { " " };
        raw_picker_push_line(&mut output, &format!("{marker} {}", item.display), width);
    }
    raw_picker_push_line(&mut output, "Enter select  Esc cancel", width);
    write_raw_interactive_fd_all(fd, output.as_bytes())?;
    Ok(line_count)
}

#[cfg(unix)]
pub(super) fn render_raw_history_search(
    fd: RawFd,
    previous_lines: usize,
    query: &RawPickerSearchQuery,
    state: &HistorySearchState,
) -> io::Result<usize> {
    let visible = state.visible_items();
    let line_count = visible.len() + 2;
    let selected_index = state.focused_index().saturating_sub(state.visible_from());
    let width = raw_picker_terminal_width(fd);
    let mut output = raw_picker_clear_sequence(previous_lines);
    output.push_str(&raw_picker_query_prompt_line("history> ", query, width));
    for (index, item) in visible.iter().enumerate() {
        let marker = if index == selected_index { ">" } else { " " };
        raw_picker_push_line(&mut output, &format!("{marker} {}", item.display), width);
    }
    raw_picker_push_line(&mut output, "Enter select  Esc cancel", width);
    write_raw_interactive_fd_all(fd, output.as_bytes())?;
    Ok(line_count)
}

#[cfg(unix)]
fn clear_raw_history_search(fd: RawFd, previous_lines: usize) -> io::Result<()> {
    clear_raw_picker(fd, previous_lines)
}
