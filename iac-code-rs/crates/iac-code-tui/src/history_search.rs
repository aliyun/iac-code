use std::cmp::Ordering;

use crate::{fuzzy_match, selection_window::SelectionWindow};

#[derive(Clone, Debug, Eq, PartialEq)]
pub struct HistoryMessage {
    pub role: String,
    pub content: HistoryMessageContent,
}

impl HistoryMessage {
    pub fn new(role: impl Into<String>, content: HistoryMessageContent) -> Self {
        Self {
            role: role.into(),
            content,
        }
    }

    pub fn text(role: impl Into<String>, content: impl Into<String>) -> Self {
        Self::new(role, HistoryMessageContent::Text(content.into()))
    }
}

#[derive(Clone, Debug, Eq, PartialEq)]
pub enum HistoryMessageContent {
    Text(String),
    Blocks(Vec<HistoryContentBlock>),
}

#[derive(Clone, Debug, Eq, PartialEq)]
pub enum HistoryContentBlock {
    Text(String),
    Other(String),
    Empty,
}

impl HistoryContentBlock {
    pub fn text(text: impl Into<String>) -> Self {
        Self::Text(text.into())
    }

    pub fn other(value: impl Into<String>) -> Self {
        Self::Other(value.into())
    }

    pub fn empty() -> Self {
        Self::Empty
    }

    fn as_content_text(&self) -> &str {
        match self {
            Self::Text(text) | Self::Other(text) => text,
            Self::Empty => "",
        }
    }
}

#[derive(Clone, Debug, Eq, PartialEq)]
pub struct HistorySearchItem {
    pub key: String,
    pub display: String,
    pub content: String,
    pub filter_text: String,
}

pub struct HistorySearchState {
    items: Vec<HistorySearchItem>,
    filtered: Vec<HistorySearchItem>,
    window: SelectionWindow,
    done: bool,
    result: Option<String>,
}

impl HistorySearchState {
    pub fn new(messages: Vec<HistoryMessage>, visible_count: usize) -> Self {
        let mut state = Self {
            items: build_history_search_items(&messages),
            filtered: Vec::new(),
            window: SelectionWindow::new(visible_count.max(1)),
            done: false,
            result: None,
        };
        state.update_query("");
        state
    }

    pub fn filtered_items(&self) -> &[HistorySearchItem] {
        &self.filtered
    }

    pub fn visible_items(&self) -> &[HistorySearchItem] {
        self.window.visible_slice(&self.filtered)
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

    pub fn result(&self) -> Option<&String> {
        self.result.as_ref()
    }

    pub fn update_query(&mut self, query: &str) {
        if query.is_empty() {
            self.filtered = self.items.clone();
        } else {
            let mut scored = self
                .items
                .iter()
                .filter_map(|item| {
                    fuzzy_match(query, &item.filter_text).map(|score| (score, item.clone()))
                })
                .collect::<Vec<_>>();
            scored.sort_by(|left, right| right.0.partial_cmp(&left.0).unwrap_or(Ordering::Equal));
            self.filtered = scored.into_iter().map(|(_, item)| item).collect();
        }
        self.window.reset();
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

    pub fn select_focused(&mut self) -> Option<HistorySearchItem> {
        let item = self.window.focused(&self.filtered)?.clone();
        self.done = true;
        self.result = Some(item.content.clone());
        Some(item)
    }

    pub fn cancel(&mut self) {
        self.done = true;
        self.result = None;
    }
}

pub fn build_history_search_items(messages: &[HistoryMessage]) -> Vec<HistorySearchItem> {
    messages
        .iter()
        .filter(|message| message.role == "user")
        .rev()
        .enumerate()
        .map(|(index, message)| {
            let content = history_content_text(&message.content);
            HistorySearchItem {
                key: format!("history-{index}"),
                display: first_chars(&content, 80),
                filter_text: content.clone(),
                content,
            }
        })
        .collect()
}

fn history_content_text(content: &HistoryMessageContent) -> String {
    match content {
        HistoryMessageContent::Text(text) => text.clone(),
        HistoryMessageContent::Blocks(blocks) => blocks
            .iter()
            .map(HistoryContentBlock::as_content_text)
            .collect::<Vec<_>>()
            .join(" "),
    }
}

fn first_chars(text: &str, limit: usize) -> String {
    text.chars().take(limit).collect()
}
