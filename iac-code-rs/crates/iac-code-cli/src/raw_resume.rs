use std::io;
#[cfg(unix)]
use std::os::fd::RawFd;
use std::time::UNIX_EPOCH;

use iac_code_core::SessionEntry;
use iac_code_tui::{RawInputCapture, ResumePickerState, ResumeSessionEntry};

use super::raw_picker::RawPickerSearchQuery;
use super::raw_prompt_context::RawPromptActionContext;
use super::raw_resume_preview::read_raw_resume_preview;

#[cfg(unix)]
mod render;

#[cfg(unix)]
use render::clear_raw_resume_picker;
#[cfg(all(unix, test))]
pub(super) use render::raw_resume_picker_clear_sequence;
#[cfg(unix)]
pub(super) use render::render_raw_resume_picker;

#[cfg(unix)]
#[derive(Clone, Debug, PartialEq)]
pub(super) struct RawResumeSessionEntry {
    session_id: String,
    cwd: String,
    project_name: String,
    git_branch: Option<String>,
    title: String,
    modified_at_epoch_seconds: u64,
    size_bytes: u64,
    name: Option<String>,
    auto_title: Option<String>,
    is_legacy: bool,
}

#[cfg(all(unix, test))]
impl RawResumeSessionEntry {
    pub(super) fn new(
        session_id: impl Into<String>,
        cwd: impl Into<String>,
        project_name: impl Into<String>,
        title: impl Into<String>,
    ) -> Self {
        Self {
            session_id: session_id.into(),
            cwd: cwd.into(),
            project_name: project_name.into(),
            git_branch: None,
            title: title.into(),
            modified_at_epoch_seconds: 0,
            size_bytes: 0,
            name: None,
            auto_title: None,
            is_legacy: false,
        }
    }

    pub(super) fn with_name(mut self, name: impl Into<String>) -> Self {
        self.name = Some(name.into());
        self
    }
}

#[cfg(unix)]
impl From<&RawResumeSessionEntry> for ResumeSessionEntry {
    fn from(entry: &RawResumeSessionEntry) -> Self {
        let mut converted = ResumeSessionEntry::new(
            &entry.session_id,
            &entry.cwd,
            &entry.project_name,
            &entry.title,
            entry.modified_at_epoch_seconds,
            entry.size_bytes,
        )
        .with_legacy(entry.is_legacy);
        if let Some(branch) = &entry.git_branch {
            converted = converted.with_git_branch(branch);
        }
        if let Some(name) = &entry.name {
            converted = converted.with_name(name);
        }
        if let Some(auto_title) = &entry.auto_title {
            converted = converted.with_auto_title(auto_title);
        }
        converted
    }
}

#[cfg(unix)]
pub(super) fn raw_resume_entries_from_session_entries(
    entries: &[SessionEntry],
) -> Vec<RawResumeSessionEntry> {
    entries
        .iter()
        .map(|entry| RawResumeSessionEntry {
            session_id: entry.session_id.clone(),
            cwd: entry.cwd.clone(),
            project_name: entry.project_name.clone(),
            git_branch: entry.git_branch.clone(),
            title: entry.title.clone(),
            modified_at_epoch_seconds: entry
                .mtime
                .duration_since(UNIX_EPOCH)
                .unwrap_or_default()
                .as_secs(),
            size_bytes: entry.size_bytes,
            name: entry.name.clone(),
            auto_title: entry.auto_title.clone(),
            is_legacy: entry.is_legacy,
        })
        .collect()
}

#[cfg(unix)]
pub(super) fn read_raw_resume_picker(
    fd: RawFd,
    capture: &RawInputCapture,
    context: &RawPromptActionContext,
) -> io::Result<Option<String>> {
    let current_project_entries = context
        .resume_current_project_entries
        .iter()
        .map(ResumeSessionEntry::from)
        .collect::<Vec<_>>();
    let all_project_entries = if context.resume_all_project_entries.is_empty() {
        current_project_entries.clone()
    } else {
        context
            .resume_all_project_entries
            .iter()
            .map(ResumeSessionEntry::from)
            .collect::<Vec<_>>()
    };
    let mut state = ResumePickerState::new(
        current_project_entries,
        all_project_entries,
        context.current_session_id.as_deref(),
        context.current_branch.as_deref(),
        5,
    );
    if state.filtered_entries().is_empty() {
        return Ok(None);
    }

    let mut query = RawPickerSearchQuery::new();
    let mut rendered_lines = 0usize;
    rendered_lines = render_raw_resume_picker(fd, rendered_lines, &query, &state)?;

    loop {
        let Some(event) = capture.read_key(None)? else {
            continue;
        };
        let key = event.key.as_str();
        if key == "enter" {
            let selected = state.select_focused().map(|entry| entry.session_id.clone());
            clear_raw_resume_picker(fd, rendered_lines)?;
            return Ok(selected);
        }
        if key == "escape" || (event.ctrl && key == "c") {
            clear_raw_resume_picker(fd, rendered_lines)?;
            return Ok(None);
        }
        if key == "up" || (event.ctrl && key == "p") {
            state.move_focus(-1);
            rendered_lines = render_raw_resume_picker(fd, rendered_lines, &query, &state)?;
            continue;
        }
        if key == "down" || (event.ctrl && key == "n") {
            state.move_focus(1);
            rendered_lines = render_raw_resume_picker(fd, rendered_lines, &query, &state)?;
            continue;
        }
        if key == "pageup" {
            state.page_up();
            rendered_lines = render_raw_resume_picker(fd, rendered_lines, &query, &state)?;
            continue;
        }
        if key == "pagedown" {
            state.page_down();
            rendered_lines = render_raw_resume_picker(fd, rendered_lines, &query, &state)?;
            continue;
        }
        if event.ctrl && key == "a" {
            state.toggle_show_all_projects();
            rendered_lines = render_raw_resume_picker(fd, rendered_lines, &query, &state)?;
            continue;
        }
        if event.ctrl && key == "b" {
            state.toggle_only_current_branch();
            rendered_lines = render_raw_resume_picker(fd, rendered_lines, &query, &state)?;
            continue;
        }
        if key == " " && state.enter_preview() {
            clear_raw_resume_picker(fd, rendered_lines)?;
            rendered_lines = 0;
            if let Some(session_id) = read_raw_resume_preview(fd, capture, context, &mut state)? {
                return Ok(Some(session_id));
            }
            rendered_lines = render_raw_resume_picker(fd, rendered_lines, &query, &state)?;
            continue;
        }
        if query.handle_key(&event) {
            state.update_query(query.text());
            rendered_lines = render_raw_resume_picker(fd, rendered_lines, &query, &state)?;
            continue;
        }
    }
}
