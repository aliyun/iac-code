use std::collections::BTreeSet;

use crate::{fuzzy_match, selection_window::SelectionWindow};

const LOCKED_STATUS_MESSAGE: &str = "Bundled skills cannot be disabled.";

#[derive(Clone, Debug, Eq, PartialEq)]
pub enum SkillManagementSource {
    Bundled,
    Project,
    User,
    Other(String),
}

#[derive(Clone, Debug, Eq, PartialEq)]
pub struct SkillManagementItem {
    pub name: String,
    pub description: String,
    pub source: SkillManagementSource,
    pub content_length: usize,
    pub path: String,
    pub enabled: bool,
    pub locked: bool,
}

impl SkillManagementItem {
    pub fn new(
        name: impl Into<String>,
        description: impl Into<String>,
        source: SkillManagementSource,
        content_length: usize,
        path: impl Into<String>,
        enabled: bool,
        locked: bool,
    ) -> Self {
        Self {
            name: name.into(),
            description: description.into(),
            source,
            content_length,
            path: path.into(),
            enabled,
            locked,
        }
    }
}

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub enum SkillsSortMode {
    Name,
    Source,
    Size,
}

impl SkillsSortMode {
    fn next(self) -> Self {
        match self {
            Self::Name => Self::Source,
            Self::Source => Self::Size,
            Self::Size => Self::Name,
        }
    }
}

pub struct SkillsPickerState {
    all_items: Vec<SkillManagementItem>,
    filtered: Vec<SkillManagementItem>,
    disabled: BTreeSet<String>,
    description_matched_names: BTreeSet<String>,
    query: String,
    sort_mode: SkillsSortMode,
    window: SelectionWindow,
    done: bool,
    result: Option<BTreeSet<String>>,
    status_message: String,
}

impl SkillsPickerState {
    pub fn new(items: Vec<SkillManagementItem>, visible_count: usize) -> Self {
        let disabled = items
            .iter()
            .filter(|item| !item.enabled && !item.locked)
            .map(|item| normalize_skill_name(&item.name))
            .filter(|name| !name.is_empty())
            .collect::<BTreeSet<_>>();
        let mut state = Self {
            all_items: items,
            filtered: Vec::new(),
            disabled,
            description_matched_names: BTreeSet::new(),
            query: String::new(),
            sort_mode: SkillsSortMode::Name,
            window: SelectionWindow::new(visible_count),
            done: false,
            result: None,
            status_message: String::new(),
        };
        state.apply_filter(None);
        state
    }

    pub fn disabled_skill_names(&self) -> BTreeSet<String> {
        self.disabled.clone()
    }

    pub fn filtered_items(&self) -> &[SkillManagementItem] {
        &self.filtered
    }

    pub fn total_items(&self) -> usize {
        self.all_items.len()
    }

    pub fn visible_items(&self) -> &[SkillManagementItem] {
        self.window.visible_slice(&self.filtered)
    }

    pub fn focused_item(&self) -> Option<&SkillManagementItem> {
        self.window.focused(&self.filtered)
    }

    pub fn description_matched_names(&self) -> &BTreeSet<String> {
        &self.description_matched_names
    }

    pub fn focused_index(&self) -> usize {
        self.window.focused_index()
    }

    pub fn visible_from(&self) -> usize {
        self.window.visible_from()
    }

    pub fn sort_mode(&self) -> SkillsSortMode {
        self.sort_mode
    }

    pub fn status_message(&self) -> &str {
        &self.status_message
    }

    pub fn is_done(&self) -> bool {
        self.done
    }

    pub fn result(&self) -> Option<&BTreeSet<String>> {
        self.result.as_ref()
    }

    pub fn update_query(&mut self, query: &str) {
        self.query = query.trim().to_owned();
        self.status_message.clear();
        self.apply_filter(None);
    }

    pub fn cycle_sort(&mut self) {
        self.sort_mode = self.sort_mode.next();
        let focused = self.focused_item().map(|item| item.name.clone());
        self.apply_filter(focused.as_deref());
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

    pub fn toggle_focused(&mut self) {
        let Some(item) = self.focused_item().cloned() else {
            return;
        };
        let name = normalize_skill_name(&item.name);
        if item.locked {
            self.status_message = LOCKED_STATUS_MESSAGE.to_owned();
            return;
        }
        if self.disabled.contains(&name) {
            self.disabled.remove(&name);
        } else {
            self.disabled.insert(name);
        }
        self.status_message.clear();
        self.apply_filter(Some(&item.name));
    }

    pub fn save(&mut self) -> BTreeSet<String> {
        self.done = true;
        self.result = Some(self.disabled.clone());
        self.disabled.clone()
    }

    pub fn cancel(&mut self) {
        self.done = true;
        self.result = None;
    }

    fn apply_filter(&mut self, keep_focus_name: Option<&str>) {
        self.description_matched_names.clear();
        let mut candidates = if self.query.is_empty() {
            self.all_items.clone()
        } else {
            let mut scored = Vec::new();
            for item in &self.all_items {
                let name_score = fuzzy_match(&self.query, &item.name);
                let description_score = skill_description_query_match_score(&self.query, item);
                if let Some(score) = name_score.or(description_score) {
                    if name_score.is_none() {
                        self.description_matched_names.insert(item.name.clone());
                    }
                    scored.push((score, item.clone()));
                }
            }
            scored.sort_by(|left, right| {
                right
                    .0
                    .partial_cmp(&left.0)
                    .unwrap_or(std::cmp::Ordering::Equal)
            });
            scored.into_iter().map(|(_, item)| item).collect::<Vec<_>>()
        };

        sort_items(&mut candidates, self.sort_mode);
        self.filtered = candidates;
        let focused_index = keep_focus_name
            .and_then(|name| self.filtered.iter().position(|item| item.name == name))
            .unwrap_or(0);
        self.window
            .reset_with_focus(self.filtered.len(), focused_index);
    }
}

fn skill_description_query_match_score(query: &str, item: &SkillManagementItem) -> Option<f64> {
    let query = query.trim();
    if query.is_empty() {
        return Some(0.0);
    }
    if !item
        .description
        .to_lowercase()
        .contains(&query.to_lowercase())
    {
        return None;
    }
    fuzzy_match(query, &item.description)
}

fn sort_items(items: &mut [SkillManagementItem], sort_mode: SkillsSortMode) {
    match sort_mode {
        SkillsSortMode::Name => items.sort_by(|left, right| left.name.cmp(&right.name)),
        SkillsSortMode::Source => items.sort_by(|left, right| {
            source_order(&left.source)
                .cmp(&source_order(&right.source))
                .then_with(|| left.name.cmp(&right.name))
        }),
        SkillsSortMode::Size => items.sort_by(|left, right| {
            left.content_length
                .cmp(&right.content_length)
                .then_with(|| left.name.cmp(&right.name))
        }),
    }
}

fn source_order(source: &SkillManagementSource) -> u8 {
    match source {
        SkillManagementSource::Bundled => 0,
        SkillManagementSource::Project => 1,
        SkillManagementSource::User => 2,
        SkillManagementSource::Other(_) => 99,
    }
}

fn normalize_skill_name(name: &str) -> String {
    name.trim_start_matches(['/', '$'])
        .trim()
        .to_ascii_lowercase()
}
