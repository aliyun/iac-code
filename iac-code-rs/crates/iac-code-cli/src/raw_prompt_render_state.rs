use crate::raw_prompt_text::{
    raw_prompt_cursor_position, raw_prompt_line_visual_line_count, raw_prompt_visual_line_count,
};

#[derive(Clone, Debug, Eq, PartialEq)]
pub(super) struct RawPromptRenderState {
    pub(super) line_count: usize,
    pub(super) cursor_row: usize,
    pub(super) rendered: Option<RawPromptRenderedState>,
}

#[derive(Clone, Debug, Eq, PartialEq)]
pub(super) struct RawPromptRenderedState {
    pub(super) text: String,
    pub(super) cursor: usize,
    pub(super) ghost_text: String,
    pub(super) suggestion_lines: Vec<String>,
}

impl RawPromptRenderState {
    pub(super) fn empty() -> Self {
        Self {
            line_count: 0,
            cursor_row: 0,
            rendered: None,
        }
    }

    #[cfg(test)]
    pub(super) fn from_line_count_at_bottom(line_count: usize) -> Self {
        Self {
            line_count,
            cursor_row: line_count.saturating_sub(1),
            rendered: None,
        }
    }

    pub(super) fn rendered_line_count_at_width(&self, width: usize) -> usize {
        let Some(rendered) = &self.rendered else {
            return self.line_count;
        };
        raw_prompt_visual_line_count(&rendered.text, &rendered.ghost_text, width)
            + rendered
                .suggestion_lines
                .iter()
                .map(|line| raw_prompt_line_visual_line_count(line, width))
                .sum::<usize>()
    }

    pub(super) fn rendered_cursor_row_at_width(&self, width: usize) -> usize {
        self.rendered
            .as_ref()
            .map(|rendered| raw_prompt_cursor_position(&rendered.text, rendered.cursor, width).line)
            .unwrap_or(self.cursor_row)
    }
}
