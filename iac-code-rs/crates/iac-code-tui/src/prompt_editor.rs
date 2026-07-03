use crate::{
    InputHistory, KeybindingManager, PromptBuffer, SuggestionAggregator, SuggestionItem,
    SuggestionProvider,
};

mod acceptance;
mod history;
mod key_event;
mod keybinding;

pub use key_event::PromptKeyEvent;

#[derive(Clone, Debug, Eq, PartialEq)]
pub enum PromptEditOutcome {
    Continue,
    Submit(String),
    Cancel,
    Action(String),
}

pub struct PromptEditor {
    buffer: PromptBuffer,
    suggestions: SuggestionAggregator,
    escape_pending: bool,
}

impl PromptEditor {
    pub fn new(providers: Vec<Box<dyn SuggestionProvider>>) -> Self {
        Self {
            buffer: PromptBuffer::new(),
            suggestions: SuggestionAggregator::new(providers),
            escape_pending: false,
        }
    }

    pub fn text(&self) -> &str {
        self.buffer.text()
    }

    pub fn cursor(&self) -> usize {
        self.buffer.cursor()
    }

    pub fn suggestions(&self) -> &[SuggestionItem] {
        self.suggestions.suggestions()
    }

    pub fn visible_suggestions(&self) -> &[SuggestionItem] {
        self.suggestions.visible_suggestions()
    }

    pub fn visible_selected_index(&self) -> usize {
        self.suggestions.visible_selected_index()
    }

    pub fn has_more_suggestions_above(&self) -> bool {
        self.suggestions.has_more_above()
    }

    pub fn has_more_suggestions_below(&self) -> bool {
        self.suggestions.has_more_below()
    }

    pub fn ghost_text(&self) -> String {
        self.suggestions.ghost_text()
    }

    pub fn insert_text(&mut self, text: &str) {
        self.buffer.insert_text(text);
        self.refresh_suggestions();
    }

    pub fn set_text(&mut self, text: impl Into<String>) {
        self.buffer = PromptBuffer::from_text(text);
        self.refresh_suggestions();
    }

    pub fn backspace(&mut self) {
        self.buffer.backspace();
        self.refresh_suggestions();
    }

    pub fn delete(&mut self) {
        self.buffer.delete();
        self.refresh_suggestions();
    }

    pub fn move_left(&mut self) {
        self.buffer.move_left();
        self.refresh_suggestions();
    }

    pub fn move_right(&mut self) {
        self.buffer.move_right();
        self.refresh_suggestions();
    }

    pub fn move_home(&mut self) {
        self.buffer.move_home();
        self.refresh_suggestions();
    }

    pub fn move_end(&mut self) {
        self.buffer.move_end();
        self.refresh_suggestions();
    }

    pub fn kill_to_end(&mut self) {
        self.buffer.kill_to_end();
        self.refresh_suggestions();
    }

    pub fn kill_to_start(&mut self) {
        self.buffer.kill_to_start();
        self.refresh_suggestions();
    }

    pub fn delete_previous_word(&mut self) {
        self.buffer.delete_previous_word();
        self.refresh_suggestions();
    }

    pub fn move_selection(&mut self, delta: isize) {
        self.suggestions.move_selection(delta);
    }

    pub fn accept_selected_suggestion(&mut self) -> bool {
        self.accept_suggestion(false)
    }

    pub fn accept_ghost_text(&mut self) -> bool {
        self.accept_suggestion(true)
    }

    pub fn handle_key(&mut self, event: PromptKeyEvent) -> PromptEditOutcome {
        self.handle_key_with_bindings_internal(event, None)
    }

    pub fn handle_key_with_history(
        &mut self,
        event: PromptKeyEvent,
        history: &mut InputHistory,
    ) -> PromptEditOutcome {
        self.handle_key_with_history_internal(event, history, None)
    }

    pub fn handle_key_with_bindings(
        &mut self,
        event: PromptKeyEvent,
        bindings: &KeybindingManager,
    ) -> PromptEditOutcome {
        self.handle_key_with_bindings_internal(event, Some(bindings))
    }

    pub fn handle_key_with_history_and_bindings(
        &mut self,
        event: PromptKeyEvent,
        history: &mut InputHistory,
        bindings: &KeybindingManager,
    ) -> PromptEditOutcome {
        self.handle_key_with_history_internal(event, history, Some(bindings))
    }

    fn clear(&mut self) {
        self.buffer = PromptBuffer::new();
        self.suggestions.dismiss();
    }

    fn refresh_suggestions(&mut self) {
        self.suggestions
            .update(self.buffer.text(), self.buffer.cursor());
    }
}
