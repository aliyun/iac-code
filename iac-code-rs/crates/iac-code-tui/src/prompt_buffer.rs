use std::ops::Range;

use unicode_segmentation::UnicodeSegmentation;

#[derive(Clone, Debug, Default, Eq, PartialEq)]
pub struct PromptBuffer {
    text: String,
    cursor: usize,
}

impl PromptBuffer {
    pub fn new() -> Self {
        Self::default()
    }

    pub fn from_text(text: impl Into<String>) -> Self {
        let text = text.into();
        let cursor = text.len();
        Self { text, cursor }
    }

    pub fn text(&self) -> &str {
        &self.text
    }

    pub fn cursor(&self) -> usize {
        self.cursor
    }

    pub fn set_cursor(&mut self, cursor: usize) {
        self.cursor = self.clamp_to_grapheme_boundary(cursor);
    }

    pub fn insert_text(&mut self, text: &str) {
        self.text.insert_str(self.cursor, text);
        self.cursor += text.len();
    }

    pub fn backspace(&mut self) {
        if let Some(range) = image_placeholder_range_before_cursor(&self.text, self.cursor) {
            self.text.replace_range(range.clone(), "");
            self.cursor = range.start;
            return;
        }
        let Some(previous) = self.previous_grapheme_boundary(self.cursor) else {
            return;
        };
        self.text.replace_range(previous..self.cursor, "");
        self.cursor = previous;
    }

    pub fn delete(&mut self) {
        if let Some(range) = image_placeholder_range_at_cursor(&self.text, self.cursor) {
            self.text.replace_range(range, "");
            return;
        }
        let Some(next) = self.next_grapheme_boundary(self.cursor) else {
            return;
        };
        self.text.replace_range(self.cursor..next, "");
    }

    pub fn move_left(&mut self) {
        if let Some(previous) = self.previous_grapheme_boundary(self.cursor) {
            self.cursor = previous;
        }
    }

    pub fn move_right(&mut self) {
        if let Some(next) = self.next_grapheme_boundary(self.cursor) {
            self.cursor = next;
        }
    }

    pub fn move_home(&mut self) {
        self.cursor = 0;
    }

    pub fn move_end(&mut self) {
        self.cursor = self.text.len();
    }

    pub fn kill_to_end(&mut self) {
        self.text.truncate(self.cursor);
    }

    pub fn kill_to_start(&mut self) {
        self.text.replace_range(..self.cursor, "");
        self.cursor = 0;
    }

    pub fn delete_previous_word(&mut self) {
        if self.cursor == 0 {
            return;
        }

        let mut start = self.cursor;
        if self.previous_char_is_space(start) {
            while self.previous_char_is_space(start) {
                start = self.previous_grapheme_boundary(start).unwrap_or(0);
            }
        } else {
            while start > 0 && !self.previous_char_is_space(start) {
                start = self.previous_grapheme_boundary(start).unwrap_or(0);
            }
        }

        self.text.replace_range(start..self.cursor, "");
        self.cursor = start;
    }

    pub fn replace_range(&mut self, range: Range<usize>, replacement: &str) {
        let start = self.clamp_to_char_boundary(range.start);
        let end = self.clamp_to_char_boundary(range.end);
        let (start, end) = if start <= end {
            (start, end)
        } else {
            (end, start)
        };
        self.text.replace_range(start..end, replacement);
        self.cursor = start + replacement.len();
    }

    fn previous_grapheme_boundary(&self, cursor: usize) -> Option<usize> {
        let cursor = self.clamp_to_char_boundary(cursor);
        if cursor == 0 {
            return None;
        }
        self.text
            .grapheme_indices(true)
            .take_while(|(index, _)| *index < cursor)
            .last()
            .map(|(index, _)| index)
    }

    fn next_grapheme_boundary(&self, cursor: usize) -> Option<usize> {
        let cursor = self.clamp_to_char_boundary(cursor);
        if cursor >= self.text.len() {
            return None;
        }
        self.text
            .grapheme_indices(true)
            .find_map(|(index, _)| (index > cursor).then_some(index))
            .or(Some(self.text.len()))
    }

    fn previous_char_is_space(&self, cursor: usize) -> bool {
        self.text[..cursor]
            .chars()
            .next_back()
            .is_some_and(|value| value == ' ')
    }

    fn clamp_to_char_boundary(&self, cursor: usize) -> usize {
        let mut cursor = cursor.min(self.text.len());
        while !self.text.is_char_boundary(cursor) {
            cursor -= 1;
        }
        cursor
    }

    fn clamp_to_grapheme_boundary(&self, cursor: usize) -> usize {
        let cursor = self.clamp_to_char_boundary(cursor);
        if cursor == self.text.len() || self.text.is_empty() {
            return cursor;
        }
        self.text
            .grapheme_indices(true)
            .take_while(|(index, _)| *index <= cursor)
            .last()
            .map(|(index, _)| index)
            .unwrap_or(0)
    }
}

fn image_placeholder_range_before_cursor(text: &str, cursor: usize) -> Option<Range<usize>> {
    let cursor = cursor.min(text.len());
    image_placeholder_ranges(text)
        .into_iter()
        .find(|range| range.start < cursor && cursor <= range.end)
}

fn image_placeholder_range_at_cursor(text: &str, cursor: usize) -> Option<Range<usize>> {
    let cursor = cursor.min(text.len());
    image_placeholder_ranges(text)
        .into_iter()
        .find(|range| range.start <= cursor && cursor < range.end)
}

fn image_placeholder_ranges(text: &str) -> Vec<Range<usize>> {
    const PREFIX: &str = "[Image #";
    let mut ranges = Vec::new();
    let mut search_start = 0usize;
    while let Some(relative_start) = text[search_start..].find(PREFIX) {
        let start = search_start + relative_start;
        let digits_start = start + PREFIX.len();
        let bytes = text.as_bytes();
        let mut digits_end = digits_start;
        while digits_end < bytes.len() && bytes[digits_end].is_ascii_digit() {
            digits_end += 1;
        }
        if digits_end == digits_start || digits_end >= bytes.len() || bytes[digits_end] != b']' {
            search_start = digits_start;
            continue;
        }
        let end = digits_end + 1;
        ranges.push(start..end);
        search_start = end;
    }
    ranges
}
