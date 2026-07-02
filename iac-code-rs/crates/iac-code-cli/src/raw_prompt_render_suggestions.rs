use iac_code_tui::{terminal_display_width, PromptEditor, SuggestionItem};

use crate::cli_i18n::tr;
use crate::raw_picker::raw_picker_fit_line_to_width;

#[derive(Clone, Copy)]
pub(super) struct RawPromptSuggestionOverlay<'a> {
    pub(super) visible: &'a [SuggestionItem],
    pub(super) selected_index: usize,
    pub(super) has_more_above: bool,
    pub(super) has_more_below: bool,
}

impl<'a> RawPromptSuggestionOverlay<'a> {
    pub(super) fn empty() -> Self {
        Self {
            visible: &[],
            selected_index: 0,
            has_more_above: false,
            has_more_below: false,
        }
    }

    fn is_empty(&self) -> bool {
        self.visible.is_empty()
    }
}

pub(super) fn raw_prompt_suggestion_overlay(
    editor: &PromptEditor,
) -> RawPromptSuggestionOverlay<'_> {
    RawPromptSuggestionOverlay {
        visible: editor.visible_suggestions(),
        selected_index: editor.visible_selected_index(),
        has_more_above: editor.has_more_suggestions_above(),
        has_more_below: editor.has_more_suggestions_below(),
    }
}

pub(super) fn raw_prompt_suggestion_overlay_lines(
    suggestions: RawPromptSuggestionOverlay<'_>,
    width: usize,
) -> Vec<String> {
    if suggestions.is_empty() || width == 0 {
        return Vec::new();
    }

    let max_name_width = suggestions
        .visible
        .iter()
        .map(|item| terminal_display_width(&item.display_text))
        .max()
        .unwrap_or(0);
    let name_col_width = (max_name_width + 3).min((width.saturating_mul(2) / 5).max(1));
    let mut lines = Vec::with_capacity(suggestions.visible.len() + 1);

    for (index, item) in suggestions.visible.iter().enumerate() {
        let color = if index == suggestions.selected_index {
            "\x1b[96m"
        } else {
            "\x1b[38;2;128;128;128m"
        };
        let name = raw_prompt_pad_to_width(
            &raw_picker_fit_line_to_width(&item.display_text, name_col_width),
            name_col_width,
        );
        let desc_prefix_width = 2 + terminal_display_width(&name);
        let desc_width = width.saturating_sub(desc_prefix_width);
        let description = item.description.as_deref().unwrap_or_default();
        let description = raw_picker_fit_line_to_width(description, desc_width);
        lines.push(format!("  {color}{name}{description}\x1b[0m"));
    }

    let mut scroll_hint = String::new();
    if suggestions.has_more_above {
        scroll_hint.push('↑');
    }
    if suggestions.has_more_below {
        scroll_hint.push('↓');
    }
    let scroll_hint = if scroll_hint.is_empty() {
        String::new()
    } else {
        format!(" {scroll_hint}")
    };
    let nav = tr("Navigate");
    let confirm = tr("Confirm");
    let fill = tr("Fill");
    let dismiss = tr("Dismiss");
    let hint = format!("  ↑↓ {nav}{scroll_hint}  Enter {confirm}  Tab {fill}  Esc {dismiss}");
    lines.push(format!(
        "\x1b[38;2;128;128;128m{}\x1b[0m",
        raw_picker_fit_line_to_width(&hint, width)
    ));
    lines
}

fn raw_prompt_pad_to_width(text: &str, width: usize) -> String {
    let used = terminal_display_width(text);
    if used >= width {
        text.to_owned()
    } else {
        format!("{text}{}", " ".repeat(width - used))
    }
}
