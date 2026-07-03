use crate::KeybindingManager;

use super::key_event::is_printable_text;
use super::{PromptEditOutcome, PromptEditor, PromptKeyEvent};

impl PromptEditor {
    pub(super) fn handle_key_with_bindings_internal(
        &mut self,
        event: PromptKeyEvent,
        bindings: Option<&KeybindingManager>,
    ) -> PromptEditOutcome {
        if let Some(outcome) = self.handle_pre_binding_key(&event) {
            return outcome;
        }

        if let Some(outcome) = self.resolve_binding_after_submit_keys(&event, bindings) {
            return outcome;
        }

        self.handle_key_after_bindings(event)
    }

    pub(super) fn handle_pre_binding_key(
        &mut self,
        event: &PromptKeyEvent,
    ) -> Option<PromptEditOutcome> {
        let key = event.key.as_str();

        if key == "paste" {
            self.insert_text(&event.char_text);
            return Some(PromptEditOutcome::Continue);
        }

        if matches!(key, "focus_in" | "focus_out") {
            return Some(PromptEditOutcome::Continue);
        }

        if self.escape_pending {
            self.escape_pending = false;
            if key == "enter" {
                self.insert_text("\n");
                return Some(PromptEditOutcome::Continue);
            }
        }

        if key == "escape" && !self.suggestions.suggestions().is_empty() {
            self.escape_pending = true;
            self.suggestions.dismiss();
            return Some(PromptEditOutcome::Continue);
        }

        if event.ctrl && key == "c" {
            if self.buffer.text().is_empty() {
                return Some(PromptEditOutcome::Cancel);
            }
            self.clear();
            return Some(PromptEditOutcome::Continue);
        }

        if key == "enter" && event.shift {
            self.insert_text("\n");
            return Some(PromptEditOutcome::Continue);
        }

        if key == "enter" {
            if !self.suggestions.suggestions().is_empty() {
                self.accept_selected_suggestion();
            }
            return Some(PromptEditOutcome::Submit(self.buffer.text().to_owned()));
        }

        if key == "tab" {
            self.accept_ghost_text();
            return Some(PromptEditOutcome::Continue);
        }

        None
    }

    pub(super) fn resolve_binding_after_submit_keys(
        &mut self,
        event: &PromptKeyEvent,
        bindings: Option<&KeybindingManager>,
    ) -> Option<PromptEditOutcome> {
        if event.key == "escape" {
            self.escape_pending = true;
        }
        bindings
            .and_then(|bindings| bindings.resolve(event))
            .map(PromptEditOutcome::Action)
    }

    fn handle_key_after_bindings(&mut self, event: PromptKeyEvent) -> PromptEditOutcome {
        let key = event.key.as_str();

        if key == "escape" {
            self.escape_pending = true;
            return PromptEditOutcome::Continue;
        }

        if !self.suggestions.suggestions().is_empty() {
            if key == "up" || (event.ctrl && key == "p") {
                self.move_selection(-1);
                return PromptEditOutcome::Continue;
            }
            if key == "down" || (event.ctrl && key == "n") {
                self.move_selection(1);
                return PromptEditOutcome::Continue;
            }
        }

        if (event.ctrl && key == "a") || key == "home" {
            self.move_home();
            return PromptEditOutcome::Continue;
        }
        if (event.ctrl && key == "e") || key == "end" {
            self.move_end();
            return PromptEditOutcome::Continue;
        }
        if event.ctrl && key == "k" {
            self.kill_to_end();
            return PromptEditOutcome::Continue;
        }
        if event.ctrl && key == "u" {
            self.kill_to_start();
            return PromptEditOutcome::Continue;
        }
        if event.ctrl && key == "w" {
            self.delete_previous_word();
            return PromptEditOutcome::Continue;
        }
        if key == "left" {
            self.move_left();
            return PromptEditOutcome::Continue;
        }
        if key == "right" {
            self.move_right();
            return PromptEditOutcome::Continue;
        }
        if key == "backspace" {
            self.backspace();
            return PromptEditOutcome::Continue;
        }
        if key == "delete" {
            self.delete();
            return PromptEditOutcome::Continue;
        }

        if is_printable_text(&event.char_text) {
            self.insert_text(&event.char_text);
        }

        PromptEditOutcome::Continue
    }
}
