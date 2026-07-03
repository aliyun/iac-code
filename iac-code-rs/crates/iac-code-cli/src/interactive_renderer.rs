use std::collections::{BTreeMap, BTreeSet};
use std::time::{Duration, Instant};

use iac_code_exec::{HeadlessRunResult, OutputFormat};
use iac_code_protocol::{json::JsonValue, StreamEvent, Usage};

use crate::ansi::{ANSI_DIM, ANSI_RESET};
use crate::cli_i18n::tr;
use crate::interactive_banner::interactive_startup_banner_width;
use crate::interactive_markdown::{
    markdown_source_starts_with_heading, prefix_interactive_markdown_block,
    render_interactive_agent_stdout, streaming_markdown_flush_index, InteractiveMarkdownPrefix,
};
use crate::interactive_tool_renderer::{
    interactive_tool_header_line, interactive_tool_result_has_expandable_detail,
    interactive_tool_result_summary,
};
use crate::interactive_usage::{interactive_completion_status, interactive_usage_parts};

mod live_thinking;

pub(super) fn write_interactive_agent_result(
    result: &HeadlessRunResult,
    output_format: OutputFormat,
    elapsed: Duration,
) {
    if output_format == OutputFormat::Text {
        print!("{}", render_interactive_agent_events(result, elapsed));
    } else {
        print!("{}", result.stdout);
    }
    eprint!("{}", result.stderr);
}

pub(super) fn render_interactive_agent_events(
    result: &HeadlessRunResult,
    elapsed: Duration,
) -> String {
    let mut renderer = InteractiveEventRenderer::new(elapsed);
    for event in &result.events {
        renderer.push_event(event);
    }
    renderer.finish()
}

pub(super) struct InteractiveEventRenderer {
    output: String,
    text: String,
    saw_thinking: bool,
    thinking: String,
    thinking_started_at: Option<Instant>,
    tool_inputs: BTreeMap<String, (String, JsonValue)>,
    rendered_tool_headers: BTreeSet<String>,
    pending_usage: Usage,
    elapsed: Duration,
    streaming: bool,
    live_updates: bool,
    live_thinking_lines: usize,
    live_thinking_separator_active: bool,
    last_live_thinking_render: Option<Instant>,
    pub(super) live_thinking_min_interval: Duration,
    text_continuation: bool,
    markdown_document_text: bool,
    had_content: bool,
}

/// Cap on how many rendered rows the transient live-thinking preview occupies.
/// Mirrors the Python UI, which crops the thinking quote to a handful of rows so
/// the region stays small instead of growing with the whole reasoning trace.
pub(super) const INTERACTIVE_LIVE_THINKING_MAX_ROWS: usize = 6;

/// Minimum wall-clock gap between live-thinking repaints during streaming. The
/// reasoning channel emits many small deltas per second; repainting on every one
/// pauses/redraws the spinner constantly and makes it flicker badly. Throttling
/// to ~12 fps (matching the spinner's own cadence) keeps it readable and steady,
/// mirroring Rich's `refresh_per_second` on the Python side.
pub(super) const INTERACTIVE_LIVE_THINKING_MIN_INTERVAL: Duration = Duration::from_millis(80);

impl InteractiveEventRenderer {
    pub(super) fn new(elapsed: Duration) -> Self {
        Self {
            output: String::new(),
            text: String::new(),
            saw_thinking: false,
            thinking: String::new(),
            thinking_started_at: None,
            tool_inputs: BTreeMap::new(),
            rendered_tool_headers: BTreeSet::new(),
            pending_usage: Usage::default(),
            elapsed,
            streaming: false,
            live_updates: false,
            live_thinking_lines: 0,
            live_thinking_separator_active: false,
            last_live_thinking_render: None,
            live_thinking_min_interval: Duration::ZERO,
            text_continuation: false,
            markdown_document_text: false,
            had_content: false,
        }
    }

    #[cfg(test)]
    pub(super) fn streaming() -> Self {
        Self::streaming_with_live_updates(false)
    }

    pub(super) fn streaming_with_live_updates(live_updates: bool) -> Self {
        Self {
            streaming: true,
            live_updates,
            ..Self::new(Duration::ZERO)
        }
    }

    pub(super) fn push_event(&mut self, event: &StreamEvent) {
        match event {
            StreamEvent::TextDelta(text) => {
                self.flush_pending_usage();
                self.flush_thinking();
                self.text.push_str(&text.text);
                if self.streaming {
                    self.flush_complete_text_lines();
                }
            }
            StreamEvent::ThinkingDelta(thinking) => {
                self.flush_pending_usage();
                if self.thinking_started_at.is_none() {
                    self.thinking_started_at = Some(Instant::now());
                }
                self.saw_thinking = true;
                self.push_thinking_delta(&thinking.text);
                if self.streaming && self.live_updates && self.live_thinking_render_due() {
                    self.render_live_thinking();
                }
            }
            StreamEvent::ToolUseEnd(tool_use) => {
                self.flush_text();
                self.flush_thinking();
                self.tool_inputs.insert(
                    tool_use.tool_use_id.clone(),
                    (tool_use.name.clone(), tool_use.input.clone()),
                );
            }
            StreamEvent::ToolResult(tool_result) => {
                self.flush_text();
                self.flush_thinking();
                if !self
                    .rendered_tool_headers
                    .contains(&tool_result.tool_use_id)
                {
                    let input = self
                        .tool_inputs
                        .get(&tool_result.tool_use_id)
                        .map(|(_, input)| input.clone());
                    self.push_tool_header(
                        &tool_result.tool_use_id,
                        &tool_result.tool_name,
                        input.as_ref(),
                    );
                }
                let summary = interactive_tool_result_summary(
                    &tool_result.tool_name,
                    &tool_result.result,
                    tool_result.is_error,
                );
                self.push_line(&interactive_dim_line(&format!("  ⎿  {summary}")));
                if interactive_tool_result_has_expandable_detail(
                    &tool_result.tool_name,
                    &tool_result.result,
                    tool_result.is_error,
                ) {
                    self.push_line(&interactive_dim_line(&format!(
                        "  {}",
                        tr("(ctrl+o to expand)")
                    )));
                }
            }
            StreamEvent::Error(error) => {
                self.flush_text();
                self.flush_thinking();
                self.push_line(&format!("{}: {}", tr("Error"), error.error));
            }
            StreamEvent::MessageEnd(message_end) => {
                self.pending_usage.input_tokens = self
                    .pending_usage
                    .input_tokens
                    .saturating_add(message_end.usage.input_tokens);
                self.pending_usage.output_tokens = self
                    .pending_usage
                    .output_tokens
                    .saturating_add(message_end.usage.output_tokens);
                self.pending_usage.cache_creation_input_tokens = self
                    .pending_usage
                    .cache_creation_input_tokens
                    .saturating_add(message_end.usage.cache_creation_input_tokens);
                self.pending_usage.cache_read_input_tokens = self
                    .pending_usage
                    .cache_read_input_tokens
                    .saturating_add(message_end.usage.cache_read_input_tokens);
            }
            _ => {}
        }
    }

    pub(super) fn finish(mut self) -> String {
        self.flush_thinking();
        self.flush_text();
        self.flush_pending_usage();
        let status = interactive_completion_status(
            self.had_content || !self.output.trim().is_empty(),
            self.elapsed,
        );
        if !status.is_empty() {
            self.push_block_line(&interactive_dim_line(&status));
            if !self.output.ends_with("\n\n") {
                self.output.push('\n');
            }
        }
        self.output
    }

    pub(super) fn finish_with_elapsed(mut self, elapsed: Duration) -> String {
        self.elapsed = elapsed;
        self.finish()
    }

    pub(super) fn take_output(&mut self) -> String {
        std::mem::take(&mut self.output)
    }

    fn flush_thinking(&mut self) {
        if !self.saw_thinking {
            return;
        }
        let seconds = self
            .thinking_started_at
            .map(|started| started.elapsed().as_secs_f64())
            .unwrap_or_else(|| self.elapsed.as_secs_f64());
        self.thinking.clear();
        self.thinking_started_at = None;
        self.last_live_thinking_render = None;
        self.clear_live_thinking();
        self.text_continuation = false;
        self.markdown_document_text = false;
        let summary = interactive_dim_line(&format!(
            "▌ {}",
            tr("Thought for {seconds:.1f}s").replace("{seconds:.1f}", &format!("{seconds:.1}"))
        ));
        if self.live_thinking_separator_active {
            self.push_line(&summary);
            self.live_thinking_separator_active = false;
        } else {
            self.push_block_line(&summary);
        }
        self.saw_thinking = false;
    }

    fn flush_text(&mut self) {
        if self.text.trim().is_empty() {
            self.text.clear();
            return;
        }
        let text = std::mem::take(&mut self.text);
        self.push_rendered_text(&text);
    }

    fn flush_complete_text_lines(&mut self) {
        let Some(index) = streaming_markdown_flush_index(&self.text) else {
            return;
        };
        let remainder = self.text.split_off(index);
        let complete = std::mem::replace(&mut self.text, remainder);
        if complete.trim().is_empty() {
            return;
        }
        self.push_rendered_text(&complete);
    }

    fn push_rendered_text(&mut self, text: &str) {
        self.had_content = true;
        self.ensure_trailing_blank_line();
        let mut rendered = render_interactive_agent_stdout(text);
        if !rendered.ends_with('\n') {
            rendered.push('\n');
        }
        if !self.text_continuation && markdown_source_starts_with_heading(text) {
            self.markdown_document_text = true;
        }
        let prefix = if self.markdown_document_text {
            InteractiveMarkdownPrefix::None
        } else if self.text_continuation {
            InteractiveMarkdownPrefix::Continuation
        } else {
            InteractiveMarkdownPrefix::Bullet
        };
        self.output
            .push_str(&prefix_interactive_markdown_block(&rendered, prefix));
        self.text_continuation = true;
    }

    fn push_tool_header(&mut self, tool_use_id: &str, tool_name: &str, input: Option<&JsonValue>) {
        if !self.rendered_tool_headers.insert(tool_use_id.to_owned()) {
            return;
        }
        self.text_continuation = false;
        self.markdown_document_text = false;
        self.push_block_line(&interactive_tool_header_line(tool_name, input));
    }

    fn push_line(&mut self, line: &str) {
        if !self.text.is_empty() {
            self.flush_text();
        }
        self.clear_live_thinking();
        self.had_content = true;
        self.output.push_str(line);
        self.output.push('\n');
    }

    fn push_block_line(&mut self, line: &str) {
        self.ensure_trailing_blank_line();
        self.push_line(line);
    }

    fn flush_pending_usage(&mut self) {
        let parts = interactive_usage_parts(&self.pending_usage);
        if parts.is_empty() {
            return;
        }
        self.pending_usage = Usage::default();
        self.push_block_line(&interactive_dim_line(&format!("  {}", parts.join(" · "))));
        self.ensure_trailing_blank_line();
    }

    fn push_thinking_delta(&mut self, text: &str) {
        live_thinking::push_thinking_delta(&mut self.thinking, text);
    }

    /// Whether enough wall-clock time has passed to repaint the transient
    /// live-thinking region again. Returns true immediately for the first delta
    /// of a block (no prior render) and whenever the throttle interval is zero
    /// (the default outside the live interactive loop, e.g. in tests).
    pub(super) fn live_thinking_render_due(&mut self) -> bool {
        live_thinking::render_due(
            &mut self.last_live_thinking_render,
            self.live_thinking_min_interval,
        )
    }

    fn render_live_thinking(&mut self) {
        self.clear_live_thinking();
        let thinking = self.thinking.trim_end().to_owned();
        if thinking.is_empty() {
            return;
        }
        let terminal_width = interactive_startup_banner_width().max(1);
        if !self.live_thinking_separator_active {
            self.ensure_trailing_blank_line();
            self.live_thinking_separator_active = true;
        }
        let (visible, line_count) = live_thinking::visible_trailing_lines(
            &thinking,
            terminal_width,
            INTERACTIVE_LIVE_THINKING_MAX_ROWS,
        );
        for line in visible.iter().rev() {
            self.output
                .push_str(&interactive_dim_line(&format!("▌ {line}")));
            self.output.push('\n');
        }
        self.live_thinking_lines = line_count;
    }

    fn clear_live_thinking(&mut self) {
        if self.live_thinking_lines == 0 {
            return;
        }
        live_thinking::clear_rows(&mut self.output, self.live_thinking_lines);
        self.live_thinking_lines = 0;
    }

    fn ensure_trailing_blank_line(&mut self) {
        if self.output.is_empty() || !self.output.ends_with("\n\n") {
            self.output.push('\n');
        }
    }
}

pub(super) fn interactive_dim_line(line: &str) -> String {
    format!("{ANSI_DIM}{line}{ANSI_RESET}")
}
