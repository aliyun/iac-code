use std::cmp::Ordering;

pub const OVERLAY_MAX_ITEMS: usize = 5;

#[derive(Clone, Debug, PartialEq, Eq)]
pub struct CompletionToken {
    pub text: String,
    pub start: usize,
    pub end: usize,
    pub trigger: String,
}

#[derive(Clone, Debug, PartialEq)]
pub struct SuggestionItem {
    pub id: String,
    pub display_text: String,
    pub completion: String,
    pub description: Option<String>,
    pub icon: Option<String>,
    pub source: String,
    pub score: f64,
    pub arg_hint: Option<String>,
}

pub trait SuggestionProvider {
    fn trigger(&self) -> &str;
    fn provide(&self, token: &CompletionToken) -> Vec<SuggestionItem>;
}

#[derive(Default, Debug)]
pub struct TokenExtractor;

impl TokenExtractor {
    pub fn new() -> Self {
        Self
    }

    pub fn extract(&self, text: &str, cursor_pos: usize) -> Option<CompletionToken> {
        if text.is_empty() || cursor_pos == 0 {
            return None;
        }

        let end = clamp_to_char_boundary(text, cursor_pos.min(text.len()));
        if end == 0 {
            return None;
        }

        if let Some(token) = extract_slash_command(text, end) {
            return Some(token);
        }

        let mut start = end;
        while let Some((previous_start, ch)) = previous_char(text, start) {
            if !is_token_char(ch) {
                break;
            }
            start = previous_start;
        }

        if start == end {
            return None;
        }

        let token_text = &text[start..end];
        let trigger = token_text.chars().next()?;
        let is_valid = match trigger {
            '/' | '$' => is_start_or_after_prompt_separator(text, start),
            '@' => true,
            '!' => is_line_start(text, start),
            _ => false,
        };

        is_valid.then(|| CompletionToken {
            text: token_text.to_owned(),
            start,
            end,
            trigger: trigger.to_string(),
        })
    }
}

pub struct SuggestionAggregator {
    providers: Vec<Box<dyn SuggestionProvider>>,
    extractor: TokenExtractor,
    suggestions: Vec<SuggestionItem>,
    selected_index: usize,
    token_text: String,
    token_start: usize,
    token_end: usize,
    active: bool,
}

impl SuggestionAggregator {
    pub fn new(providers: Vec<Box<dyn SuggestionProvider>>) -> Self {
        Self {
            providers,
            extractor: TokenExtractor::new(),
            suggestions: Vec::new(),
            selected_index: 0,
            token_text: String::new(),
            token_start: 0,
            token_end: 0,
            active: false,
        }
    }

    pub fn update(&mut self, text: &str, cursor_pos: usize) {
        let Some(token) = self.extractor.extract(text, cursor_pos) else {
            self.dismiss();
            return;
        };

        let mut matching_provider_count = 0;
        let mut suggestions = Vec::new();
        for provider in &self.providers {
            if provider.trigger() == token.trigger {
                matching_provider_count += 1;
                suggestions.extend(provider.provide(&token));
            }
        }

        if matching_provider_count == 0 {
            self.dismiss();
            return;
        }

        suggestions.sort_by(|left, right| {
            right
                .score
                .partial_cmp(&left.score)
                .unwrap_or(Ordering::Equal)
        });

        self.suggestions = suggestions;
        self.selected_index = 0;
        self.token_text = token.text;
        self.token_start = token.start;
        self.token_end = token.end;
        self.active = true;
    }

    pub fn suggestions(&self) -> &[SuggestionItem] {
        &self.suggestions
    }

    pub fn visible_suggestions(&self) -> &[SuggestionItem] {
        let start = self.visible_start();
        let end = (start + OVERLAY_MAX_ITEMS).min(self.suggestions.len());
        &self.suggestions[start..end]
    }

    pub fn selected_index(&self) -> usize {
        self.selected_index
    }

    pub fn visible_selected_index(&self) -> usize {
        self.selected_index.saturating_sub(self.visible_start())
    }

    pub fn has_more_above(&self) -> bool {
        self.visible_start() > 0
    }

    pub fn has_more_below(&self) -> bool {
        self.visible_start() + OVERLAY_MAX_ITEMS < self.suggestions.len()
    }

    pub fn ghost_text(&self) -> String {
        let Some(selected) = self.suggestions.get(self.selected_index) else {
            return String::new();
        };

        if !starts_with_ignore_ascii_case(&selected.completion, &self.token_text) {
            return String::new();
        }

        let mut suffix = selected.completion[self.token_text.len()..].to_owned();
        if let Some(arg_hint) = &selected.arg_hint {
            if self
                .token_text
                .eq_ignore_ascii_case(selected.completion.trim_end())
            {
                suffix.push_str(arg_hint);
            }
        }
        suffix
    }

    pub fn move_selection(&mut self, delta: isize) {
        if self.suggestions.is_empty() {
            return;
        }

        let len = self.suggestions.len() as isize;
        self.selected_index = (self.selected_index as isize + delta).rem_euclid(len) as usize;
    }

    pub fn accept_selected(&mut self) -> Option<(String, usize, usize)> {
        if !self.active || self.suggestions.is_empty() {
            return None;
        }

        let completion = self.suggestions[self.selected_index].completion.clone();
        let start = self.token_start;
        let end = self.token_end;
        self.dismiss();
        Some((completion, start, end))
    }

    pub fn accept_ghost_text(&mut self) -> Option<(String, usize, usize)> {
        if self.ghost_text().is_empty() {
            return None;
        }
        self.accept_selected()
    }

    pub fn dismiss(&mut self) {
        self.suggestions.clear();
        self.selected_index = 0;
        self.token_text.clear();
        self.token_start = 0;
        self.token_end = 0;
        self.active = false;
    }

    fn visible_start(&self) -> usize {
        let len = self.suggestions.len();
        if len <= OVERLAY_MAX_ITEMS {
            return 0;
        }
        let start = self.selected_index.saturating_sub(OVERLAY_MAX_ITEMS - 1);
        start.min(len - OVERLAY_MAX_ITEMS)
    }
}

fn extract_slash_command(text: &str, end: usize) -> Option<CompletionToken> {
    let line_start = text[..end].rfind('\n').map_or(0, |index| index + 1);
    for (offset, ch) in text[line_start..end].char_indices() {
        if ch != '/' {
            continue;
        }

        let start = line_start + offset;
        if start == line_start
            || previous_char(text, start).is_some_and(|(_, ch)| ch.is_whitespace())
        {
            return Some(CompletionToken {
                text: text[start..end].to_owned(),
                start,
                end,
                trigger: "/".to_owned(),
            });
        }
    }
    None
}

fn clamp_to_char_boundary(text: &str, mut index: usize) -> usize {
    while index > 0 && !text.is_char_boundary(index) {
        index -= 1;
    }
    index
}

fn previous_char(text: &str, end: usize) -> Option<(usize, char)> {
    text.get(..end)?.char_indices().next_back()
}

fn is_token_char(ch: char) -> bool {
    ch.is_alphanumeric()
        || matches!(
            ch,
            '_' | '.' | '-' | '/' | '\\' | '~' | '@' | '#' | '!' | '$'
        )
}

fn is_start_or_after_prompt_separator(text: &str, start: usize) -> bool {
    start == 0 || previous_char(text, start).is_some_and(|(_, ch)| matches!(ch, ' ' | '\t' | '\n'))
}

fn is_line_start(text: &str, start: usize) -> bool {
    start == 0 || previous_char(text, start).is_some_and(|(_, ch)| ch == '\n')
}

fn starts_with_ignore_ascii_case(text: &str, prefix: &str) -> bool {
    text.get(..prefix.len())
        .is_some_and(|candidate| candidate.eq_ignore_ascii_case(prefix))
}
