use std::collections::BTreeSet;
use std::io;
#[cfg(unix)]
use std::os::fd::RawFd;

use iac_code_config::paths::ConfigPaths;
use iac_code_config::settings::{normalize_skill_name, save_disabled_skills};
use iac_code_tui::{
    terminal_display_width, RawInputCapture, SkillManagementItem, SkillManagementSource,
    SkillsPickerState, SkillsSortMode,
};

use super::cli_i18n::{tr, tr_dynamic};
use super::raw_picker::{
    raw_picker_clear_sequence, raw_picker_fit_line_to_width, raw_picker_push_line,
    raw_picker_push_styled_line, raw_picker_query_prompt_line_with_styled_prompt_and_cursor_save,
    raw_picker_terminal_width, write_raw_interactive_fd_all, RawPickerSearchQuery,
};
use super::raw_prompt_context::RawPromptActionContext;
use super::skills_management::{format_skill_token_estimate, load_skill_management_state};

#[cfg(unix)]
#[derive(Clone, Debug, PartialEq, Eq)]
pub(super) enum RawSkillsPickerOutcome {
    Saved(BTreeSet<String>),
    Cancelled,
}

#[cfg(unix)]
pub(super) fn read_raw_skills_picker(
    fd: RawFd,
    capture: &RawInputCapture,
    context: &RawPromptActionContext,
) -> io::Result<RawSkillsPickerOutcome> {
    let mut state = SkillsPickerState::new(context.skill_management_items.clone(), 10);
    let mut query = RawPickerSearchQuery::new();
    let mut rendered_lines = 0usize;
    rendered_lines = render_raw_skills_picker(fd, rendered_lines, &query, &state)?;

    loop {
        let Some(event) = capture.read_key(None)? else {
            continue;
        };
        let key = event.key.as_str();
        if key == "enter" {
            let disabled = state.save();
            if let Some(paths) = &context.config_paths {
                save_disabled_skills(
                    paths,
                    disabled.iter().map(String::as_str),
                    context.skill_locked_names.iter().map(String::as_str),
                )
                .map_err(|error| io::Error::other(error.to_string()))?;
            }
            clear_raw_skills_picker(fd, rendered_lines)?;
            return Ok(RawSkillsPickerOutcome::Saved(disabled));
        }
        if key == "escape" || (event.ctrl && key == "c") {
            state.cancel();
            clear_raw_skills_picker(fd, rendered_lines)?;
            return Ok(RawSkillsPickerOutcome::Cancelled);
        }
        if key == "up" || (event.ctrl && key == "p") {
            state.move_focus(-1);
            rendered_lines = render_raw_skills_picker(fd, rendered_lines, &query, &state)?;
            continue;
        }
        if key == "down" || (event.ctrl && key == "n") {
            state.move_focus(1);
            rendered_lines = render_raw_skills_picker(fd, rendered_lines, &query, &state)?;
            continue;
        }
        if key == "pageup" {
            state.page_up();
            rendered_lines = render_raw_skills_picker(fd, rendered_lines, &query, &state)?;
            continue;
        }
        if key == "pagedown" {
            state.page_down();
            rendered_lines = render_raw_skills_picker(fd, rendered_lines, &query, &state)?;
            continue;
        }
        if key == " " {
            state.toggle_focused();
            rendered_lines = render_raw_skills_picker(fd, rendered_lines, &query, &state)?;
            continue;
        }
        if key == "tab" {
            state.cycle_sort();
            rendered_lines = render_raw_skills_picker(fd, rendered_lines, &query, &state)?;
            continue;
        }
        if query.handle_key(&event) {
            state.update_query(query.text());
            rendered_lines = render_raw_skills_picker(fd, rendered_lines, &query, &state)?;
            continue;
        }
    }
}

#[cfg(unix)]
pub(super) fn render_raw_skills_picker(
    fd: RawFd,
    previous_lines: usize,
    query: &RawPickerSearchQuery,
    state: &SkillsPickerState,
) -> io::Result<usize> {
    let filtered_total = state.filtered_items().len();
    let focus_pos = if filtered_total == 0 {
        0
    } else {
        state.focused_index() + 1
    };
    let visible = state.visible_items();
    let selected_index = state.focused_index().saturating_sub(state.visible_from());
    let disabled = state.disabled_skill_names();
    let mut line_count = 6 + if visible.is_empty() { 1 } else { visible.len() };
    if !state.status_message().is_empty() {
        line_count += 1;
    }

    let width = raw_picker_terminal_width(fd);
    let mut output = raw_skills_picker_clear_sequence(previous_lines);
    let title = tr("Skills");
    let title_meta = if filtered_total == 0 {
        String::new()
    } else {
        format!(
            " ({})",
            tr("{current} of {total}")
                .replace("{current}", &focus_pos.to_string())
                .replace("{total}", &filtered_total.to_string())
        )
    };
    raw_skills_push_header_line(&mut output, &title, &title_meta, width);
    raw_skills_push_dim_line(
        &mut output,
        &tr("{count} skills - Space to toggle, Enter to save, Tab to sort, Esc to cancel")
            .replace("{count}", &state.total_items().to_string()),
        width,
    );
    raw_skills_push_dim_line(
        &mut output,
        &tr("Sort: {mode}").replace("{mode}", &tr(skills_sort_mode_label(state.sort_mode()))),
        width,
    );
    output.push_str(&raw_skills_search_line(query, width));
    raw_picker_push_line(&mut output, "", width);
    if visible.is_empty() {
        raw_skills_push_dim_line(&mut output, &tr("No skills found"), width);
    } else {
        for (index, item) in visible.iter().enumerate() {
            let marker = if index == selected_index { ">" } else { " " };
            let normalized = normalize_skill_name(&item.name);
            let enabled = item.locked || !disabled.contains(&normalized);
            let state_marker = format!(
                "{} {}",
                if enabled { "-" } else { "x" },
                if enabled { tr("on") } else { tr("off") }
            );
            let mut details = vec![
                translated_skill_management_source_label(&item.source),
                format_skill_token_estimate(item.content_length),
            ];
            if item.locked {
                details.insert(1, tr("locked"));
            }
            if state.description_matched_names().contains(&item.name) {
                details.push(tr("matched description"));
            }
            if !matches!(item.source, SkillManagementSource::Bundled) && !item.path.is_empty() {
                details.push(item.path.clone());
            }
            raw_skills_push_item_line(
                &mut output,
                RawSkillsItemLine {
                    marker,
                    state_marker: &state_marker,
                    name: &item.name,
                    details: &details,
                    is_focused: index == selected_index,
                    enabled,
                },
                width,
            );
        }
    }
    raw_picker_push_line(&mut output, "", width);
    if !state.status_message().is_empty() {
        raw_skills_push_status_line(&mut output, &tr_dynamic(state.status_message()), width);
    }
    output.push_str("\x1b[u");
    write_raw_interactive_fd_all(fd, output.as_bytes())?;
    Ok(line_count)
}

#[cfg(unix)]
fn clear_raw_skills_picker(fd: RawFd, previous_lines: usize) -> io::Result<()> {
    write_raw_interactive_fd_all(
        fd,
        raw_skills_picker_clear_sequence(previous_lines).as_bytes(),
    )
}

#[cfg(unix)]
pub(super) fn raw_skills_picker_clear_sequence(previous_lines: usize) -> String {
    const SEARCH_LINE_NUMBER: usize = 4;
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

#[cfg(unix)]
fn raw_skills_push_header_line(output: &mut String, title: &str, meta: &str, width: usize) {
    let plain = format!("{title}{meta}");
    let styled = format!("\x1b[1m\x1b[36m{title}\x1b[0m\x1b[2m{meta}\x1b[0m");
    raw_picker_push_styled_line(output, &plain, &styled, width);
}

#[cfg(unix)]
fn raw_skills_push_dim_line(output: &mut String, line: &str, width: usize) {
    let fitted = raw_picker_fit_line_to_width(line, width);
    raw_picker_push_styled_line(output, line, &format!("\x1b[2m{fitted}\x1b[0m"), width);
}

#[cfg(unix)]
fn raw_skills_push_status_line(output: &mut String, line: &str, width: usize) {
    let fitted = raw_picker_fit_line_to_width(line, width);
    raw_picker_push_styled_line(output, line, &format!("\x1b[33m{fitted}\x1b[0m"), width);
}

#[cfg(unix)]
struct RawSkillsItemLine<'a> {
    marker: &'a str,
    state_marker: &'a str,
    name: &'a str,
    details: &'a [String],
    is_focused: bool,
    enabled: bool,
}

#[cfg(unix)]
fn raw_skills_push_item_line(output: &mut String, item: RawSkillsItemLine<'_>, width: usize) {
    let RawSkillsItemLine {
        marker,
        state_marker,
        name,
        details,
        is_focused,
        enabled,
    } = item;
    let details = details.join(" - ");
    let plain = format!("{marker} {state_marker} {name:<18} - {details}");
    if terminal_display_width(&plain) > width {
        raw_picker_push_line(output, &plain, width);
        return;
    }

    let marker_style = if is_focused {
        format!("\x1b[1m\x1b[36m{marker} \x1b[0m")
    } else {
        format!("{marker} ")
    };
    let state_style = if enabled { "\x1b[32m" } else { "\x1b[31m" };
    let name_part = format!("{name:<18}");
    let styled_name = if is_focused {
        format!("\x1b[1m{name_part}\x1b[0m")
    } else {
        name_part
    };
    let styled = format!(
        "{marker_style}{state_style}{state_marker}\x1b[0m {styled_name}\x1b[2m - {details}\x1b[0m"
    );
    raw_picker_push_styled_line(output, &plain, &styled, width);
}

#[cfg(unix)]
fn raw_skills_search_line(query: &RawPickerSearchQuery, width: usize) -> String {
    if query.text().is_empty() {
        let mut output = String::new();
        let placeholder = tr("Search skills...");
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

#[cfg(unix)]
pub(super) fn raw_skills_picker_context(
    paths: &ConfigPaths,
    cwd: &str,
) -> (Vec<SkillManagementItem>, BTreeSet<String>) {
    load_skill_management_state(paths, cwd)
        .map(|state| (state.items, state.locked))
        .unwrap_or_default()
}

#[cfg(unix)]
fn skills_sort_mode_label(mode: SkillsSortMode) -> &'static str {
    match mode {
        SkillsSortMode::Name => "name",
        SkillsSortMode::Source => "source",
        SkillsSortMode::Size => "size",
    }
}

#[cfg(unix)]
fn translated_skill_management_source_label(source: &SkillManagementSource) -> String {
    match source {
        SkillManagementSource::Bundled => tr("bundled"),
        SkillManagementSource::Project => tr("project"),
        SkillManagementSource::User => tr("user"),
        SkillManagementSource::Other(value) => value.clone(),
    }
}
