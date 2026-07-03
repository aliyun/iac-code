use crate::text_wrap::{wrap_plain_line, wrap_prefixed_text, wrap_transcript_lines_tail};

#[derive(Clone, Debug, Eq, PartialEq)]
pub enum TranscriptTurn {
    User { text: String },
    Assistant { segments: Vec<TranscriptSegment> },
}

impl TranscriptTurn {
    pub fn user(text: impl Into<String>) -> Self {
        Self::User { text: text.into() }
    }

    pub fn assistant(segments: Vec<TranscriptSegment>) -> Self {
        Self::Assistant { segments }
    }

    fn is_user(&self) -> bool {
        matches!(self, Self::User { .. })
    }
}

#[derive(Clone, Debug, Eq, PartialEq)]
pub enum TranscriptSegment {
    Text(String),
    Tool {
        header_lines: Vec<String>,
        prompt: Option<String>,
        result_line: Option<String>,
    },
}

impl TranscriptSegment {
    pub fn text(text: impl Into<String>) -> Self {
        Self::Text(text.into())
    }

    pub fn tool(
        header_lines: Vec<String>,
        prompt: Option<&str>,
        result_line: Option<&str>,
    ) -> Self {
        Self::Tool {
            header_lines,
            prompt: prompt.map(str::to_owned),
            result_line: result_line.map(str::to_owned),
        }
    }
}

#[derive(Clone, Debug, Eq, PartialEq)]
pub struct TranscriptViewState {
    turns: Vec<TranscriptTurn>,
    current_segments: Vec<TranscriptSegment>,
}

impl TranscriptViewState {
    pub fn new(turns: Vec<TranscriptTurn>) -> Self {
        Self {
            turns,
            current_segments: Vec::new(),
        }
    }

    pub fn with_current_segments(mut self, current_segments: Vec<TranscriptSegment>) -> Self {
        self.current_segments = current_segments;
        self
    }

    pub fn render_lines(&self) -> Vec<String> {
        let mut lines = Vec::new();
        let mut first = true;

        for turn in &self.turns {
            if !first {
                lines.push(String::new());
            }
            first = false;
            render_turn(turn, &mut lines);
        }

        if !self.current_segments.is_empty() {
            if !first && self.turns.last().is_none_or(TranscriptTurn::is_user) {
                lines.push(String::new());
            }
            render_assistant_segments(&self.current_segments, &mut lines);
        }

        lines
    }

    pub fn render_wrapped_lines(&self, width: usize) -> Vec<String> {
        if width == 0 {
            return Vec::new();
        }

        let mut lines = Vec::new();
        let mut first = true;

        for turn in &self.turns {
            if !first {
                lines.push(String::new());
            }
            first = false;
            render_turn_wrapped(turn, width, &mut lines);
        }

        if !self.current_segments.is_empty() {
            if !first && self.turns.last().is_none_or(TranscriptTurn::is_user) {
                lines.push(String::new());
            }
            render_assistant_segments_wrapped(&self.current_segments, width, &mut lines);
        }

        lines
    }
}

#[derive(Clone, Debug, Eq, PartialEq)]
pub struct DrawnTranscriptView {
    pub visible_lines: Vec<String>,
    pub footer: String,
}

pub fn draw_transcript_view(lines: &[String], rows: usize) -> DrawnTranscriptView {
    let content_rows = rows.saturating_sub(2).max(1);
    let visible_lines = if lines.len() > content_rows {
        lines[lines.len() - content_rows..].to_vec()
    } else {
        lines.to_vec()
    };
    DrawnTranscriptView {
        visible_lines,
        footer: "Showing transcript · ctrl+o to toggle".to_owned(),
    }
}

pub fn draw_transcript_view_wrapped(
    lines: &[String],
    rows: usize,
    width: usize,
) -> DrawnTranscriptView {
    let content_rows = rows.saturating_sub(2).max(1);
    DrawnTranscriptView {
        visible_lines: wrap_transcript_lines_tail(lines, width, content_rows),
        footer: "Showing transcript · ctrl+o to toggle".to_owned(),
    }
}

pub fn transcript_should_exit(key: &str, ctrl: bool) -> bool {
    (ctrl && matches!(key, "o" | "c")) || key == "escape"
}

fn render_turn(turn: &TranscriptTurn, lines: &mut Vec<String>) {
    match turn {
        TranscriptTurn::User { text } => lines.push(format!("❯ {text}")),
        TranscriptTurn::Assistant { segments } => render_assistant_segments(segments, lines),
    }
}

fn render_turn_wrapped(turn: &TranscriptTurn, width: usize, lines: &mut Vec<String>) {
    match turn {
        TranscriptTurn::User { text } => {
            wrap_prefixed_text("❯ ", "  ", text, width, lines);
        }
        TranscriptTurn::Assistant { segments } => {
            render_assistant_segments_wrapped(segments, width, lines);
        }
    }
}

fn render_assistant_segments(segments: &[TranscriptSegment], lines: &mut Vec<String>) {
    let mut has_content = false;
    for segment in segments {
        match segment {
            TranscriptSegment::Text(text) if !text.is_empty() => {
                if has_content {
                    lines.push(String::new());
                }
                lines.extend(text.lines().map(str::to_owned));
                has_content = true;
            }
            TranscriptSegment::Text(_) => {}
            TranscriptSegment::Tool {
                header_lines,
                prompt,
                result_line,
            } => {
                if has_content {
                    lines.push(String::new());
                }
                render_tool(
                    header_lines,
                    prompt.as_deref(),
                    result_line.as_deref(),
                    lines,
                );
                has_content = true;
            }
        }
    }
}

fn render_assistant_segments_wrapped(
    segments: &[TranscriptSegment],
    width: usize,
    lines: &mut Vec<String>,
) {
    let mut has_content = false;
    for segment in segments {
        match segment {
            TranscriptSegment::Text(text) if !text.is_empty() => {
                if has_content {
                    lines.push(String::new());
                }
                for text_line in text.lines() {
                    wrap_plain_line(text_line, width, lines);
                }
                has_content = true;
            }
            TranscriptSegment::Text(_) => {}
            TranscriptSegment::Tool {
                header_lines,
                prompt,
                result_line,
            } => {
                if has_content {
                    lines.push(String::new());
                }
                render_tool_wrapped(
                    header_lines,
                    prompt.as_deref(),
                    result_line.as_deref(),
                    width,
                    lines,
                );
                has_content = true;
            }
        }
    }
}

fn render_tool(
    header_lines: &[String],
    prompt: Option<&str>,
    result_line: Option<&str>,
    lines: &mut Vec<String>,
) {
    if let Some(first_header) = header_lines.first() {
        lines.push(first_header.clone());
    }
    if let Some(prompt) = prompt.map(str::trim).filter(|prompt| !prompt.is_empty()) {
        lines.push("  ⎿  Prompt:".to_owned());
        for prompt_line in prompt.lines() {
            lines.push(format!("     {prompt_line}"));
        }
    }
    for header_line in header_lines.iter().skip(1) {
        lines.push(header_line.clone());
    }
    if let Some(result_line) = result_line.filter(|line| !line.is_empty()) {
        lines.push(result_line.to_owned());
    }
}

fn render_tool_wrapped(
    header_lines: &[String],
    prompt: Option<&str>,
    result_line: Option<&str>,
    width: usize,
    lines: &mut Vec<String>,
) {
    if let Some(first_header) = header_lines.first() {
        wrap_plain_line(first_header, width, lines);
    }
    if let Some(prompt) = prompt.map(str::trim).filter(|prompt| !prompt.is_empty()) {
        wrap_plain_line("  ⎿  Prompt:", width, lines);
        wrap_prefixed_text("     ", "     ", prompt, width, lines);
    }
    for header_line in header_lines.iter().skip(1) {
        wrap_plain_line(header_line, width, lines);
    }
    if let Some(result_line) = result_line.filter(|line| !line.is_empty()) {
        wrap_plain_line(result_line, width, lines);
    }
}
