use std::collections::BTreeMap;

use iac_code_protocol::{
    MessageEndEvent, MessageStartEvent, StreamEvent, TextDeltaEvent, ThinkingDeltaEvent,
    ToolInputDeltaEvent, ToolUseStartEvent, Usage,
};

use crate::tool_input_parser::parse_tool_input_events;

use super::errors::StreamChatError;
use super::usage::usage_from_value;

#[derive(Clone, Debug, Default, PartialEq, Eq)]
struct StreamingToolCall {
    id: String,
    name: String,
    arguments: String,
    started: bool,
}

pub(super) fn parse_sse_stream_events(text: &str) -> Result<Vec<StreamEvent>, StreamChatError> {
    let mut parser = OpenAiSseEventParser::default();
    let mut sink = ignore_stream_event;
    for data in sse_data_blocks(text) {
        if parser.push_data(&data, &mut sink)? {
            break;
        }
    }
    parser.finish(&mut sink)
}

fn ignore_stream_event(_: &StreamEvent) {}

#[derive(Default)]
pub(super) struct OpenAiSseEventParser {
    pub(super) events: Vec<StreamEvent>,
    message_started: bool,
    message_id: String,
    tool_calls: BTreeMap<u64, StreamingToolCall>,
    stop_reason: String,
    usage: Usage,
    saw_data: bool,
}

impl OpenAiSseEventParser {
    pub(super) fn is_empty(&self) -> bool {
        !self.saw_data && self.events.is_empty()
    }

    pub(super) fn push_data(
        &mut self,
        data: &str,
        sink: &mut dyn FnMut(&StreamEvent),
    ) -> Result<bool, StreamChatError> {
        if data.trim() == "[DONE]" {
            return Ok(true);
        }
        let chunk: serde_json::Value = match serde_json::from_str(data) {
            Ok(chunk) => chunk,
            Err(error) => return Err(StreamChatError::new(error.to_string(), self.events.clone())),
        };
        self.saw_data = true;

        if self.message_id.is_empty() {
            self.message_id = chunk
                .get("id")
                .and_then(serde_json::Value::as_str)
                .unwrap_or("msg_stream")
                .to_owned();
        }
        if !self.message_started {
            self.push_event(
                StreamEvent::MessageStart(MessageStartEvent {
                    message_id: self.message_id.clone(),
                }),
                sink,
            );
            self.message_started = true;
        }

        if let Some(chunk_usage) = chunk.get("usage") {
            self.usage = usage_from_value(chunk_usage);
        }

        let Some(choice) = chunk
            .get("choices")
            .and_then(serde_json::Value::as_array)
            .and_then(|choices| choices.first())
        else {
            return Ok(false);
        };

        if let Some(reason) = choice
            .get("finish_reason")
            .and_then(serde_json::Value::as_str)
        {
            self.stop_reason = match reason {
                "tool_calls" => "tool_use".into(),
                "length" => "max_tokens".into(),
                _ => "end_turn".into(),
            };
        }

        let Some(delta) = choice.get("delta") else {
            return Ok(false);
        };
        if let Some(reasoning) = delta
            .get("reasoning_content")
            .and_then(serde_json::Value::as_str)
            .filter(|value| !value.is_empty())
        {
            self.push_event(
                StreamEvent::ThinkingDelta(ThinkingDeltaEvent {
                    text: reasoning.to_owned(),
                }),
                sink,
            );
        }
        if let Some(content) = delta
            .get("content")
            .and_then(serde_json::Value::as_str)
            .filter(|value| !value.is_empty())
        {
            self.push_event(
                StreamEvent::TextDelta(TextDeltaEvent {
                    text: content.to_owned(),
                }),
                sink,
            );
        }

        if let Some(deltas) = delta
            .get("tool_calls")
            .and_then(serde_json::Value::as_array)
        {
            for tool_delta in deltas {
                let index = tool_delta
                    .get("index")
                    .and_then(serde_json::Value::as_u64)
                    .unwrap_or(0);
                let entry = self.tool_calls.entry(index).or_default();
                if let Some(id) = tool_delta
                    .get("id")
                    .and_then(serde_json::Value::as_str)
                    .filter(|value| !value.is_empty())
                {
                    entry.id = id.to_owned();
                }
                let function = tool_delta
                    .get("function")
                    .unwrap_or(&serde_json::Value::Null);
                if let Some(name) = function
                    .get("name")
                    .and_then(serde_json::Value::as_str)
                    .filter(|value| !value.is_empty())
                {
                    entry.name = name.to_owned();
                    if !entry.started {
                        let event = StreamEvent::ToolUseStart(ToolUseStartEvent {
                            tool_use_id: entry.id.clone(),
                            name: entry.name.clone(),
                        });
                        sink(&event);
                        self.events.push(event);
                        entry.started = true;
                    }
                }
                if let Some(arguments) = function
                    .get("arguments")
                    .and_then(serde_json::Value::as_str)
                    .filter(|value| !value.is_empty())
                {
                    entry.arguments.push_str(arguments);
                    let event = StreamEvent::ToolInputDelta(ToolInputDeltaEvent {
                        tool_use_id: entry.id.clone(),
                        partial_json: arguments.to_owned(),
                    });
                    sink(&event);
                    self.events.push(event);
                }
            }
        }
        Ok(false)
    }

    pub(super) fn finish(
        mut self,
        sink: &mut dyn FnMut(&StreamEvent),
    ) -> Result<Vec<StreamEvent>, StreamChatError> {
        if !self.saw_data {
            return Err(StreamChatError::without_partial(
                "API returned no data. Please check that your API Base URL is correct.",
            ));
        }

        let tool_calls = std::mem::take(&mut self.tool_calls);
        for (_index, tool_call) in tool_calls {
            for event in
                parse_tool_input_events(&tool_call.id, &tool_call.name, &tool_call.arguments)
            {
                self.push_event(event, sink);
            }
        }
        let stop_reason = if self.stop_reason.is_empty() {
            "end_turn".to_owned()
        } else {
            std::mem::take(&mut self.stop_reason)
        };
        let usage = std::mem::take(&mut self.usage);
        self.push_event(
            StreamEvent::MessageEnd(MessageEndEvent { stop_reason, usage }),
            sink,
        );
        Ok(self.events)
    }

    fn push_event(&mut self, event: StreamEvent, sink: &mut dyn FnMut(&StreamEvent)) {
        sink(&event);
        self.events.push(event);
    }
}

fn sse_data_blocks(text: &str) -> Vec<String> {
    text.split("\n\n")
        .filter_map(|block| {
            let lines = block
                .lines()
                .filter_map(|line| line.strip_prefix("data:"))
                .map(str::trim_start)
                .collect::<Vec<_>>();
            (!lines.is_empty()).then(|| lines.join("\n"))
        })
        .collect()
}
