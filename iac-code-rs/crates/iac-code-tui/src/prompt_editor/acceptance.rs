use super::PromptEditor;

impl PromptEditor {
    pub(super) fn accept_suggestion(&mut self, ghost_only: bool) -> bool {
        let replacement = if ghost_only {
            self.suggestions.accept_ghost_text()
        } else {
            self.suggestions.accept_selected()
        };

        let Some((completion, start, end)) = replacement else {
            return false;
        };
        self.buffer.replace_range(start..end, &completion);
        self.suggestions.dismiss();
        true
    }
}
