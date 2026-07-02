use std::cmp::Ordering;

use crate::fuzzy_match;
use crate::selection_window::SelectionWindow;

pub const WHEEL_LINES: usize = 3;

const PREVIEW_START_OFFSET: usize = 1 << 30;
const SESSION_ID_PREFIX_SCORE: f64 = 1_000_000.0;

#[derive(Clone, Debug, Eq, PartialEq)]
pub struct ResumeSessionEntry {
    pub session_id: String,
    pub cwd: String,
    pub project_name: String,
    pub git_branch: Option<String>,
    pub title: String,
    pub modified_at_epoch_seconds: u64,
    pub size_bytes: u64,
    pub name: Option<String>,
    pub auto_title: Option<String>,
    pub is_legacy: bool,
}

impl ResumeSessionEntry {
    pub fn new(
        session_id: impl Into<String>,
        cwd: impl Into<String>,
        project_name: impl Into<String>,
        title: impl Into<String>,
        modified_at_epoch_seconds: u64,
        size_bytes: u64,
    ) -> Self {
        Self {
            session_id: session_id.into(),
            cwd: cwd.into(),
            project_name: project_name.into(),
            git_branch: None,
            title: title.into(),
            modified_at_epoch_seconds,
            size_bytes,
            name: None,
            auto_title: None,
            is_legacy: false,
        }
    }

    pub fn with_project_name(mut self, project_name: impl Into<String>) -> Self {
        self.project_name = project_name.into();
        self
    }

    pub fn with_git_branch(mut self, git_branch: impl Into<String>) -> Self {
        self.git_branch = Some(git_branch.into());
        self
    }

    pub fn without_git_branch(mut self) -> Self {
        self.git_branch = None;
        self
    }

    pub fn with_name(mut self, name: impl Into<String>) -> Self {
        self.name = Some(name.into());
        self
    }

    pub fn with_auto_title(mut self, auto_title: impl Into<String>) -> Self {
        self.auto_title = Some(auto_title.into());
        self
    }

    pub fn with_legacy(mut self, is_legacy: bool) -> Self {
        self.is_legacy = is_legacy;
        self
    }
}

pub struct ResumePickerState {
    current_project_entries: Vec<ResumeSessionEntry>,
    all_project_entries: Vec<ResumeSessionEntry>,
    filtered: Vec<ResumeSessionEntry>,
    current_session_id: Option<String>,
    current_branch: Option<String>,
    query: String,
    show_all_projects: bool,
    only_current_branch: bool,
    window: SelectionWindow,
    show_preview: bool,
    preview_scroll_offset: usize,
    preview_body_height_last: usize,
    done: bool,
    result: Option<ResumeSessionEntry>,
}

impl ResumePickerState {
    pub fn new(
        current_project_entries: Vec<ResumeSessionEntry>,
        all_project_entries: Vec<ResumeSessionEntry>,
        current_session_id: Option<&str>,
        current_branch: Option<&str>,
        visible_count: usize,
    ) -> Self {
        let mut state = Self {
            current_project_entries,
            all_project_entries,
            filtered: Vec::new(),
            current_session_id: current_session_id.map(str::to_owned),
            current_branch: current_branch.map(str::to_owned),
            query: String::new(),
            show_all_projects: false,
            only_current_branch: false,
            window: SelectionWindow::new(visible_count.max(1)),
            show_preview: false,
            preview_scroll_offset: 0,
            preview_body_height_last: 0,
            done: false,
            result: None,
        };
        state.apply_filter();
        state
    }

    pub fn filtered_entries(&self) -> &[ResumeSessionEntry] {
        &self.filtered
    }

    pub fn visible_entries(&self) -> &[ResumeSessionEntry] {
        self.window.visible_slice(&self.filtered)
    }

    pub fn focused_entry(&self) -> Option<&ResumeSessionEntry> {
        self.window.focused(&self.filtered)
    }

    pub fn focused_index(&self) -> usize {
        self.window.focused_index()
    }

    pub fn visible_from(&self) -> usize {
        self.window.visible_from()
    }

    pub fn show_all_projects(&self) -> bool {
        self.show_all_projects
    }

    pub fn only_current_branch(&self) -> bool {
        self.only_current_branch
    }

    pub fn is_previewing(&self) -> bool {
        self.show_preview
    }

    pub fn preview_scroll_offset(&self) -> usize {
        self.preview_scroll_offset
    }

    pub fn is_done(&self) -> bool {
        self.done
    }

    pub fn result(&self) -> Option<&ResumeSessionEntry> {
        self.result.as_ref()
    }

    pub fn update_query(&mut self, query: &str) {
        self.query = query.to_owned();
        self.apply_filter();
    }

    pub fn toggle_show_all_projects(&mut self) {
        self.show_all_projects = !self.show_all_projects;
        self.apply_filter();
    }

    pub fn toggle_only_current_branch(&mut self) {
        self.only_current_branch = !self.only_current_branch;
        self.apply_filter();
    }

    pub fn move_focus(&mut self, delta: isize) {
        self.window.move_focus(self.filtered.len(), delta);
    }

    pub fn page_up(&mut self) {
        self.window.page_up(self.filtered.len());
    }

    pub fn page_down(&mut self) {
        self.window.page_down(self.filtered.len());
    }

    pub fn enter_preview(&mut self) -> bool {
        if !self.query.is_empty() || self.filtered.is_empty() {
            return false;
        }
        self.show_preview = true;
        self.preview_scroll_offset = 0;
        true
    }

    pub fn exit_preview(&mut self) {
        self.show_preview = false;
    }

    pub fn set_preview_body_height(&mut self, height: usize) {
        self.preview_body_height_last = height;
    }

    pub fn scroll_preview(&mut self, delta: isize) {
        if delta >= 0 {
            self.preview_scroll_offset = self.preview_scroll_offset.saturating_add(delta as usize);
        } else {
            self.preview_scroll_offset = self
                .preview_scroll_offset
                .saturating_sub(delta.unsigned_abs());
        }
    }

    pub fn wheel_preview_up(&mut self) {
        self.scroll_preview(WHEEL_LINES as isize);
    }

    pub fn wheel_preview_down(&mut self) {
        self.scroll_preview(-(WHEEL_LINES as isize));
    }

    pub fn page_preview_up(&mut self) {
        let delta = self.preview_body_height_last.saturating_sub(1).max(1);
        self.scroll_preview(delta as isize);
    }

    pub fn page_preview_down(&mut self) {
        let delta = self.preview_body_height_last.saturating_sub(1).max(1);
        self.scroll_preview(-(delta as isize));
    }

    pub fn jump_preview_start(&mut self) {
        self.preview_scroll_offset = PREVIEW_START_OFFSET;
    }

    pub fn jump_preview_end(&mut self) {
        self.preview_scroll_offset = 0;
    }

    pub fn select_focused(&mut self) -> Option<ResumeSessionEntry> {
        let entry = self.window.focused(&self.filtered)?.clone();
        self.done = true;
        self.result = Some(entry.clone());
        Some(entry)
    }

    pub fn cancel(&mut self) {
        self.done = true;
        self.result = None;
    }

    fn apply_filter(&mut self) {
        let query = self.query.trim();
        let current_session_id = self.current_session_id.as_deref();
        let current_branch = self.current_branch.as_deref();
        let source = if self.show_all_projects {
            &self.all_project_entries
        } else {
            &self.current_project_entries
        };
        let candidates = source.iter().filter(|entry| {
            current_session_id != Some(entry.session_id.as_str())
                && (!self.only_current_branch
                    || current_branch.is_none()
                    || entry.git_branch.as_deref() == current_branch)
        });

        if query.is_empty() {
            self.filtered = candidates.cloned().collect();
        } else {
            let mut scored = candidates
                .filter_map(|entry| score_entry(entry, query).map(|score| (score, entry.clone())))
                .collect::<Vec<_>>();
            scored.sort_by(|left, right| right.0.partial_cmp(&left.0).unwrap_or(Ordering::Equal));
            self.filtered = scored.into_iter().map(|(_, entry)| entry).collect();
        }

        self.window.reset();
    }
}

pub fn short_resume_session_id(session_id: &str) -> String {
    if session_id.len() <= 8 {
        session_id.to_owned()
    } else {
        session_id[..8].to_owned()
    }
}

pub fn format_resume_session_size(size_bytes: u64) -> String {
    if size_bytes < 1024 {
        return format!("{size_bytes}B");
    }
    let kb = size_bytes as f64 / 1024.0;
    if kb < 1024.0 {
        return format!("{kb:.1}KB");
    }
    let mb = kb / 1024.0;
    if mb < 1024.0 {
        return format!("{mb:.1}MB");
    }
    let gb = mb / 1024.0;
    format!("{gb:.1}GB")
}

fn score_entry(entry: &ResumeSessionEntry, query: &str) -> Option<f64> {
    if entry.session_id.starts_with(query) {
        return Some(SESSION_ID_PREFIX_SCORE);
    }
    fuzzy_match(query, &entry_haystack(entry))
}

fn entry_haystack(entry: &ResumeSessionEntry) -> String {
    [
        entry.name.as_deref(),
        Some(entry.session_id.as_str()),
        Some(entry.title.as_str()),
        entry.auto_title.as_deref(),
        Some(entry.project_name.as_str()),
        entry.git_branch.as_deref(),
    ]
    .into_iter()
    .flatten()
    .filter(|part| !part.is_empty())
    .collect::<Vec<_>>()
    .join(" ")
}
