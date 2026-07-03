use iac_code_protocol::{
    ErrorEvent, StackInstancesProgressEvent, StackProgressEvent, StreamEvent, SubAgentToolEvent,
    TextDeltaEvent, ToolResultEvent, ToolUseStartEvent, Usage,
};

use super::events::{OutputCapture, OutputFormat};
use super::json_format::{json_string, stream_event_json, usage_json};
use super::tools::ToolUseTracker;

pub(super) fn write_events(output_format: OutputFormat, events: &[StreamEvent]) -> OutputCapture {
    match output_format {
        OutputFormat::Text => TextWriter::default().write(events),
        OutputFormat::Json => JsonWriter::default().write(events),
        OutputFormat::StreamJson => StreamJsonWriter.write(events),
    }
}

pub(super) fn write_progress(events: &[StreamEvent]) -> String {
    let mut stderr = String::new();
    for event in events {
        let line = match event {
            StreamEvent::ToolUseStart(ToolUseStartEvent { name, .. }) => {
                Some(format!("Tool started: {name}"))
            }
            StreamEvent::ToolResult(ToolResultEvent {
                tool_name,
                is_error,
                ..
            }) => Some(if *is_error {
                format!("Tool failed: {tool_name}")
            } else {
                format!("Tool finished: {tool_name}")
            }),
            StreamEvent::SubAgentTool(SubAgentToolEvent {
                child_tool_name,
                is_done,
                is_error,
                ..
            }) => Some(match (*is_done, *is_error) {
                (true, true) => format!("Child tool failed: {child_tool_name}"),
                (true, false) => format!("Child tool finished: {child_tool_name}"),
                (false, _) => format!("Child tool started: {child_tool_name}"),
            }),
            StreamEvent::StackProgress(StackProgressEvent {
                stack_name,
                status,
                progress_percentage,
                ..
            }) => Some(format!(
                "Stack {stack_name}: {status} ({progress_percentage:.1}%)"
            )),
            StreamEvent::StackInstancesProgress(StackInstancesProgressEvent {
                stack_group_name,
                status,
                progress_percentage,
                ..
            }) => Some(format!(
                "Stack group {stack_group_name}: {status} ({progress_percentage}%)"
            )),
            _ => None,
        };
        if let Some(line) = line {
            stderr.push_str(&line);
            stderr.push('\n');
        }
    }
    stderr
}

#[derive(Default)]
struct TextWriter {
    stdout: String,
    stderr: String,
    has_output: bool,
}

impl TextWriter {
    fn write(mut self, events: &[StreamEvent]) -> OutputCapture {
        for event in events {
            match event {
                StreamEvent::TextDelta(TextDeltaEvent { text }) => {
                    self.stdout.push_str(text);
                    self.has_output = true;
                }
                StreamEvent::Error(ErrorEvent { error, .. }) => {
                    self.stderr.push_str("Error: ");
                    self.stderr.push_str(error);
                    self.stderr.push('\n');
                }
                _ => {}
            }
        }
        if self.has_output {
            self.stdout.push('\n');
        }
        OutputCapture {
            stdout: self.stdout,
            stderr: self.stderr,
        }
    }
}

#[derive(Default)]
struct JsonWriter {
    text: String,
    tool_uses: ToolUseTracker,
    usage: Option<Usage>,
    error: Option<String>,
}

impl JsonWriter {
    fn write(mut self, events: &[StreamEvent]) -> OutputCapture {
        for event in events {
            match event {
                StreamEvent::TextDelta(TextDeltaEvent { text }) => self.text.push_str(text),
                StreamEvent::ToolUseStart(event) => {
                    self.tool_uses
                        .record_start(&event.tool_use_id, event.name.clone());
                }
                StreamEvent::ToolUseEnd(event) => {
                    self.tool_uses
                        .record_input(&event.tool_use_id, event.input.clone());
                }
                StreamEvent::ToolResult(event) => {
                    self.tool_uses.record_result(
                        &event.tool_use_id,
                        event.result.clone(),
                        event.is_error,
                    );
                }
                StreamEvent::MessageEnd(event) => {
                    let is_empty_synthetic_max_turns = event.stop_reason == "max_turns"
                        && self.usage.is_some()
                        && event.usage.total_tokens() == 0;
                    if !is_empty_synthetic_max_turns {
                        self.usage = Some(event.usage.clone());
                    }
                }
                StreamEvent::Error(event) => {
                    self.error = Some(event.error.clone());
                }
                _ => {}
            }
        }

        let mut fields = vec![
            ("text", json_string(&self.text)),
            ("tool_uses", self.tool_uses.to_json()),
            (
                "usage",
                self.usage
                    .as_ref()
                    .map_or_else(|| "null".to_owned(), usage_json),
            ),
        ];
        if let Some(error) = &self.error {
            fields.push(("error", json_string(error)));
        }

        let mut stdout = super::json_format::object_json(&fields);
        stdout.push('\n');
        OutputCapture {
            stdout,
            stderr: String::new(),
        }
    }
}

struct StreamJsonWriter;

impl StreamJsonWriter {
    fn write(self, events: &[StreamEvent]) -> OutputCapture {
        let mut stdout = String::new();
        for event in events {
            stdout.push_str(&stream_event_json(event));
            stdout.push('\n');
        }
        OutputCapture {
            stdout,
            stderr: String::new(),
        }
    }
}
