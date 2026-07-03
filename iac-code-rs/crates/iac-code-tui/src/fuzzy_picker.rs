use std::cmp::Ordering;

use crate::fuzzy_match;
use crate::selection_window::SelectionWindow;

type DynamicItemFactory = dyn Fn(&str) -> Vec<PickerItem>;

#[derive(Clone, Debug, Eq, PartialEq)]
pub struct PickerItem {
    pub key: String,
    pub display: String,
    pub description: String,
    pub filter_text: String,
}

impl PickerItem {
    pub fn new(key: impl Into<String>, display: impl Into<String>) -> Self {
        let display = display.into();
        Self {
            key: key.into(),
            filter_text: display.clone(),
            display,
            description: String::new(),
        }
    }

    pub fn with_description(mut self, description: impl Into<String>) -> Self {
        self.description = description.into();
        self
    }

    pub fn with_filter_text(mut self, filter_text: impl Into<String>) -> Self {
        let filter_text = filter_text.into();
        self.filter_text = if filter_text.is_empty() {
            self.display.clone()
        } else {
            filter_text
        };
        self
    }
}

pub struct FuzzyPickerState {
    source: PickerSource,
    filtered_items: Vec<PickerItem>,
    window: SelectionWindow,
    done: bool,
    result: Option<PickerItem>,
}

impl FuzzyPickerState {
    pub fn new_static(items: Vec<PickerItem>, visible_count: usize) -> Self {
        let mut state = Self {
            source: PickerSource::Static(items),
            filtered_items: Vec::new(),
            window: SelectionWindow::new(visible_count),
            done: false,
            result: None,
        };
        state.update_query("");
        state
    }

    pub fn new_dynamic<F>(factory: F, visible_count: usize) -> Self
    where
        F: Fn(&str) -> Vec<PickerItem> + 'static,
    {
        let mut state = Self {
            source: PickerSource::Dynamic(Box::new(factory)),
            filtered_items: Vec::new(),
            window: SelectionWindow::new(visible_count),
            done: false,
            result: None,
        };
        state.update_query("");
        state
    }

    pub fn update_query(&mut self, query: &str) {
        self.filtered_items = match &self.source {
            PickerSource::Static(items) => filter_static_items(items, query),
            PickerSource::Dynamic(factory) => factory(query),
        };
        self.window.reset();
    }

    pub fn filtered_items(&self) -> &[PickerItem] {
        &self.filtered_items
    }

    pub fn visible_items(&self) -> &[PickerItem] {
        self.window.visible_slice(&self.filtered_items)
    }

    pub fn focused_index(&self) -> usize {
        self.window.focused_index()
    }

    pub fn visible_from(&self) -> usize {
        self.window.visible_from()
    }

    pub fn is_done(&self) -> bool {
        self.done
    }

    pub fn result(&self) -> Option<&PickerItem> {
        self.result.as_ref()
    }

    pub fn match_count_text(&self) -> String {
        let matched = self.filtered_items.len();
        match &self.source {
            PickerSource::Static(items) => format!("{matched}/{} matches", items.len()),
            PickerSource::Dynamic(_) => format!("{matched} results"),
        }
    }

    pub fn move_focus(&mut self, delta: isize) {
        self.window.move_focus(self.filtered_items.len(), delta);
    }

    pub fn page_up(&mut self) {
        self.window.page_up(self.filtered_items.len());
    }

    pub fn page_down(&mut self) {
        self.window.page_down(self.filtered_items.len());
    }

    pub fn select_focused(&mut self) -> Option<PickerItem> {
        let item = self.window.focused(&self.filtered_items)?.clone();
        self.done = true;
        self.result = Some(item.clone());
        Some(item)
    }

    pub fn cancel(&mut self) {
        self.done = true;
    }
}

enum PickerSource {
    Static(Vec<PickerItem>),
    Dynamic(Box<DynamicItemFactory>),
}

fn filter_static_items(items: &[PickerItem], query: &str) -> Vec<PickerItem> {
    if query.is_empty() {
        return items.to_vec();
    }

    let mut scored = items
        .iter()
        .filter_map(|item| fuzzy_match(query, &item.filter_text).map(|score| (score, item)))
        .collect::<Vec<_>>();
    scored.sort_by(|left, right| right.0.partial_cmp(&left.0).unwrap_or(Ordering::Equal));
    scored
        .into_iter()
        .map(|(_, item)| item.clone())
        .collect::<Vec<_>>()
}
