use crate::{InputHistory, KeybindingManager};

use super::{PromptEditOutcome, PromptEditor, PromptKeyEvent};

impl PromptEditor {
    pub(super) fn handle_key_with_history_internal(
        &mut self,
        event: PromptKeyEvent,
        history: &mut InputHistory,
        bindings: Option<&KeybindingManager>,
    ) -> PromptEditOutcome {
        let key = event.key.as_str();

        if let Some(outcome) = self.handle_pre_binding_key(&event) {
            return outcome;
        }

        if let Some(outcome) = self.resolve_binding_after_submit_keys(&event, bindings) {
            return outcome;
        }

        if !history.is_navigating() && !self.suggestions.suggestions().is_empty() {
            if key == "up" || (event.ctrl && key == "p") {
                self.move_selection(-1);
                return PromptEditOutcome::Continue;
            }
            if key == "down" || (event.ctrl && key == "n") {
                self.move_selection(1);
                return PromptEditOutcome::Continue;
            }
        }

        if key == "up" {
            if let Some(entry) = history.navigate(-1, self.buffer.text()) {
                self.set_text(entry);
            }
            return PromptEditOutcome::Continue;
        }

        if key == "down" {
            if !history.is_navigating() {
                return PromptEditOutcome::Continue;
            }
            if let Some(entry) = history.navigate(1, self.buffer.text()) {
                self.set_text(entry);
            } else {
                self.set_text(history.saved_input().to_owned());
            }
            return PromptEditOutcome::Continue;
        }

        self.handle_key_with_bindings_internal(event, bindings)
    }
}
