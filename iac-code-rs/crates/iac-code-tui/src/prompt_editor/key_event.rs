#[derive(Clone, Debug, Eq, PartialEq)]
pub struct PromptKeyEvent {
    pub key: String,
    pub char_text: String,
    pub ctrl: bool,
    pub alt: bool,
    pub shift: bool,
}

impl PromptKeyEvent {
    pub fn new(key: impl Into<String>, char_text: impl Into<String>) -> Self {
        Self {
            key: key.into(),
            char_text: char_text.into(),
            ctrl: false,
            alt: false,
            shift: false,
        }
    }

    pub fn text(ch: char) -> Self {
        let text = ch.to_string();
        Self::new(text.clone(), text)
    }

    pub fn with_ctrl(mut self, ctrl: bool) -> Self {
        self.ctrl = ctrl;
        self
    }

    pub fn with_alt(mut self, alt: bool) -> Self {
        self.alt = alt;
        self
    }

    pub fn with_shift(mut self, shift: bool) -> Self {
        self.shift = shift;
        self
    }

    pub fn key_id(&self) -> String {
        let mut parts = Vec::new();
        if self.ctrl {
            parts.push("ctrl");
        }
        if self.alt {
            parts.push("alt");
        }
        parts.push(self.key.as_str());
        parts.join("+")
    }
}

pub(super) fn is_printable_text(text: &str) -> bool {
    !text.is_empty() && text.chars().all(|ch| !ch.is_control())
}
