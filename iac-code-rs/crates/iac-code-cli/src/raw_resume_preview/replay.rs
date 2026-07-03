use std::collections::HashMap;

use iac_code_protocol::message::{AgentContentBlock, AgentMessage, AgentMessageContent};
use iac_code_tui::{render_markdown_ansi, terminal_display_width};
use unicode_segmentation::UnicodeSegmentation;

use crate::ansi::{ANSI_BOLD, ANSI_GREEN, ANSI_RESET};
use crate::cli_i18n::tr;
use crate::interactive_renderer::interactive_dim_line;
use crate::interactive_tool_renderer::{interactive_tool_header, interactive_tool_result_summary};
use crate::raw_picker::raw_picker_fit_line_to_width;

pub(super) fn raw_resume_preview_body_lines(
    messages: &[AgentMessage],
    width: usize,
) -> Vec<String> {
    let width = width.max(1);
    if messages.is_empty() {
        return vec![raw_picker_fit_line_to_width(&tr("(empty session)"), width)];
    }

    // tool_use_id -> (result content, is_error), gathered from every tool-result
    // block so an assistant tool call can render its result inline.
    let mut tool_results: HashMap<&str, (&str, bool)> = HashMap::new();
    for message in messages {
        if let AgentMessageContent::Blocks(blocks) = &message.content {
            for block in blocks {
                if let AgentContentBlock::ToolResult(result) = block {
                    tool_results.insert(
                        result.tool_use_id.as_str(),
                        (result.content.as_str(), result.is_error),
                    );
                }
            }
        }
    }

    let mut lines: Vec<String> = Vec::new();
    let mut first_turn = true;
    for message in messages {
        match message.role.as_str() {
            "user" => {
                // Tool-result-only user turns are rendered inline with the
                // assistant tool call, so skip them here (matches Python).
                if raw_resume_message_is_tool_result_only(message) {
                    continue;
                }
                let text = message.get_text();
                if text.trim().is_empty() {
                    continue;
                }
                if !first_turn {
                    lines.push(String::new());
                }
                first_turn = false;
                raw_resume_preview_push_user_lines(text.trim_end(), width, &mut lines);
                lines.push(String::new());
            }
            "assistant" => {
                raw_resume_preview_push_assistant_lines(message, &tool_results, width, &mut lines);
            }
            _ => {}
        }
    }

    while matches!(lines.last(), Some(line) if line.is_empty()) {
        lines.pop();
    }
    if lines.is_empty() {
        return vec![raw_picker_fit_line_to_width(&tr("(empty session)"), width)];
    }
    lines
}

fn raw_resume_message_is_tool_result_only(message: &AgentMessage) -> bool {
    match &message.content {
        AgentMessageContent::Blocks(blocks) => {
            !blocks.is_empty()
                && blocks
                    .iter()
                    .all(|block| matches!(block, AgentContentBlock::ToolResult(_)))
        }
        AgentMessageContent::Text(_) => false,
    }
}

fn raw_resume_preview_push_user_lines(text: &str, width: usize, lines: &mut Vec<String>) {
    let inner_width = width.saturating_sub(2).max(1);
    let mut first = true;
    for source_line in text.split('\n') {
        for chunk in raw_resume_preview_wrap_plain(source_line, inner_width) {
            if first {
                lines.push(format!("\x1b[1m\x1b[36m❯ \x1b[0m{chunk}"));
                first = false;
            } else {
                lines.push(format!("  {chunk}"));
            }
        }
    }
    if first {
        lines.push("\x1b[1m\x1b[36m❯ \x1b[0m".to_owned());
    }
}

fn raw_resume_preview_push_assistant_lines(
    message: &AgentMessage,
    tool_results: &HashMap<&str, (&str, bool)>,
    width: usize,
    lines: &mut Vec<String>,
) {
    match &message.content {
        AgentMessageContent::Text(text) => {
            if !text.trim().is_empty() {
                raw_resume_preview_ensure_blank(lines);
                raw_resume_preview_push_markdown(text, width, lines);
            }
        }
        AgentMessageContent::Blocks(blocks) => {
            for block in blocks {
                match block {
                    AgentContentBlock::Text(text_block) if !text_block.text.trim().is_empty() => {
                        raw_resume_preview_ensure_blank(lines);
                        raw_resume_preview_push_markdown(&text_block.text, width, lines);
                    }
                    AgentContentBlock::Text(_) => {}
                    AgentContentBlock::ToolUse(tool_use) => {
                        raw_resume_preview_ensure_blank(lines);
                        let header = interactive_tool_header(&tool_use.name, Some(&tool_use.input));
                        let header_fitted =
                            raw_picker_fit_line_to_width(&header, width.saturating_sub(2));
                        lines.push(format!(
                            "{ANSI_GREEN}● {ANSI_RESET}{ANSI_BOLD}{header_fitted}{ANSI_RESET}"
                        ));
                        if let Some((content, is_error)) = tool_results.get(tool_use.id.as_str()) {
                            let summary =
                                interactive_tool_result_summary(&tool_use.name, content, *is_error);
                            let summary_line =
                                raw_picker_fit_line_to_width(&format!("  ⎿  {summary}"), width);
                            lines.push(interactive_dim_line(&summary_line));
                        }
                    }
                    _ => {}
                }
            }
        }
    }
}

fn raw_resume_preview_ensure_blank(lines: &mut Vec<String>) {
    if matches!(lines.last(), Some(line) if !line.is_empty()) {
        lines.push(String::new());
    }
}

fn raw_resume_preview_push_markdown(text: &str, width: usize, lines: &mut Vec<String>) {
    let inner_width = width.saturating_sub(2).max(1);
    let rendered = render_markdown_ansi(text, Some(inner_width));
    let mut bullet_pending = true;
    for line in rendered.lines() {
        if line.trim().is_empty() {
            lines.push(String::new());
            continue;
        }
        let prefix = if bullet_pending { "✦ " } else { "  " };
        bullet_pending = false;
        lines.push(format!("{prefix}{line}"));
    }
}

fn raw_resume_preview_wrap_plain(line: &str, width: usize) -> Vec<String> {
    if width == 0 || line.is_empty() {
        return vec![String::new()];
    }
    let mut wrapped = Vec::new();
    let mut current = String::new();
    let mut used = 0usize;
    for grapheme in line.graphemes(true) {
        let grapheme_width = terminal_display_width(grapheme);
        if used + grapheme_width > width && !current.is_empty() {
            wrapped.push(std::mem::take(&mut current));
            used = 0;
        }
        current.push_str(grapheme);
        used += grapheme_width;
    }
    if !current.is_empty() || wrapped.is_empty() {
        wrapped.push(current);
    }
    wrapped
}
