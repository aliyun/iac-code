use super::style::{BOLD, CYAN, ITALIC, RESET, STRIKETHROUGH, UNDERLINE};

#[derive(Debug, Default)]
pub(super) struct InlineState {
    emphasis: usize,
    strong: usize,
    strikethrough: usize,
    link: usize,
    heading: usize,
}

impl InlineState {
    pub(super) fn enter_emphasis(&mut self) {
        self.emphasis += 1;
    }

    pub(super) fn exit_emphasis(&mut self) {
        self.emphasis = self.emphasis.saturating_sub(1);
    }

    pub(super) fn enter_strong(&mut self) {
        self.strong += 1;
    }

    pub(super) fn exit_strong(&mut self) {
        self.strong = self.strong.saturating_sub(1);
    }

    pub(super) fn enter_strikethrough(&mut self) {
        self.strikethrough += 1;
    }

    pub(super) fn exit_strikethrough(&mut self) {
        self.strikethrough = self.strikethrough.saturating_sub(1);
    }

    pub(super) fn enter_link(&mut self) {
        self.link += 1;
    }

    pub(super) fn exit_link(&mut self) {
        self.link = self.link.saturating_sub(1);
    }

    pub(super) fn enter_heading(&mut self) {
        self.heading += 1;
    }

    pub(super) fn exit_heading(&mut self) {
        self.heading = self.heading.saturating_sub(1);
    }

    pub(super) fn styled_text(&self, text: &str) -> String {
        if text.is_empty() {
            return String::new();
        }
        let mut prefix = String::new();
        if self.heading > 0 || self.strong > 0 {
            prefix.push_str(BOLD);
        }
        if self.emphasis > 0 {
            prefix.push_str(ITALIC);
        }
        if self.strikethrough > 0 {
            prefix.push_str(STRIKETHROUGH);
        }
        if self.link > 0 {
            prefix.push_str(CYAN);
            prefix.push_str(UNDERLINE);
        }
        if prefix.is_empty() {
            return text.to_string();
        }
        format!("{prefix}{text}{RESET}")
    }
}
