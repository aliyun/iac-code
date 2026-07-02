use std::collections::BTreeMap;
use std::io::{BufRead, BufReader, Read};

use iac_code_protocol::{
    MessageEndEvent, MessageStartEvent, StreamEvent, TextDeltaEvent, ThinkingDeltaEvent,
    TombstoneEvent, ToolInputDeltaEvent, ToolUseStartEvent, Usage,
};

use super::response::{parse_non_streaming_response, stream_events_from_response};
use super::usage::json_u64;
use super::StreamChatError;
use crate::tool_input_parser::parse_tool_input_events;

#[derive(Clone, Debug, Default, PartialEq, Eq)]
struct StreamingToolCall {
    id: String,
    name: String,
    arguments: String,
}

pub(super) fn stream_response_events_with_sink(
    response: reqwest::blocking::Response,
    sink: &mut dyn FnMut(&StreamEvent),
) -> Result<Vec<StreamEvent>, StreamChatError> {
    let mut reader = BufReader::new(response);
    let mut line = String::new();
    let mut data_lines = Vec::<String>::new();
    let mut parser = AnthropicSseEventParser::default();

    loop {
        line.clear();
        let bytes = reader
            .read_line(&mut line)
            .map_err(|error| StreamChatError::new(error.to_string(), parser.events.clone()))?;
        if bytes == 0 {
            break;
        }
        if parser.is_empty() && data_lines.is_empty() {
            let trimmed = line.trim_start();
            if trimmed.starts_with('{') {
                let mut text = line.clone();
                reader
                    .read_to_string(&mut text)
                    .map_err(|error| StreamChatError::new(error.to_string(), Vec::new()))?;
                let events = parse_non_streaming_response(&text)
                    .map(stream_events_from_response)
                    .map_err(StreamChatError::without_partial)?;
                for event in &events {
                    sink(event);
                }
                return Ok(events);
            }
        }

        let trimmed_end = line.trim_end_matches(['\r', '\n']);
        if trimmed_end.is_empty() {
            if !data_lines.is_empty() {
                let data = data_lines.join("\n");
                data_lines.clear();
                parser.push_data(&data, sink)?;
            }
            continue;
        }
        if let Some(data) = trimmed_end.strip_prefix("data:") {
            data_lines.push(data.trim_start().to_owned());
        }
    }

    if !data_lines.is_empty() {
        let data = data_lines.join("\n");
        parser.push_data(&data, sink)?;
    }

    parser.finish(sink)
}

pub(super) fn parse_sse_stream_events(text: &str) -> Result<Vec<StreamEvent>, StreamChatError> {
    let mut parser = AnthropicSseEventParser::default();
    let mut sink = ignore_stream_event;
    for data in sse_data_blocks(text) {
        parser.push_data(&data, &mut sink)?;
    }
    parser.finish(&mut sink)
}

pub(super) fn tombstone_events_for_orphaned_messages(events: &[StreamEvent]) -> Vec<StreamEvent> {
    let mut open_message_ids = Vec::<String>::new();
    for event in events {
        match event {
            StreamEvent::MessageStart(event) => open_message_ids.push(event.message_id.clone()),
            StreamEvent::MessageEnd(_) => open_message_ids.clear(),
            _ => {}
        }
    }
    open_message_ids
        .into_iter()
        .map(|message_id| StreamEvent::Tombstone(TombstoneEvent { message_id }))
        .collect()
}

fn ignore_stream_event(_: &StreamEvent) {}

#[derive(Default)]
struct AnthropicSseEventParser {
    events: Vec<StreamEvent>,
    tool_calls: BTreeMap<u64, StreamingToolCall>,
    stop_reason: String,
    usage: Usage,
    saw_data: bool,
}

impl AnthropicSseEventParser {
    fn is_empty(&self) -> bool {
        !self.saw_data && self.events.is_empty()
    }

    fn push_data(
        &mut self,
        data: &str,
        sink: &mut dyn FnMut(&StreamEvent),
    ) -> Result<(), StreamChatError> {
        let chunk: serde_json::Value = match serde_json::from_str(data) {
            Ok(chunk) => chunk,
            Err(error) => return Err(StreamChatError::new(error.to_string(), self.events.clone())),
        };
        self.saw_data = true;
        match chunk
            .get("type")
            .and_then(serde_json::Value::as_str)
            .unwrap_or_default()
        {
            "message_start" => {
                let message = chunk.get("message").unwrap_or(&serde_json::Value::Null);
                self.push_event(
                    StreamEvent::MessageStart(MessageStartEvent {
                        message_id: message
                            .get("id")
                            .and_then(serde_json::Value::as_str)
                            .unwrap_or("msg_stream")
                            .to_owned(),
                    }),
                    sink,
                );
                if let Some(start_usage) = message.get("usage") {
                    self.usage.input_tokens = json_u64(start_usage, "input_tokens");
                    self.usage.cache_creation_input_tokens =
                        json_u64(start_usage, "cache_creation_input_tokens");
                    self.usage.cache_read_input_tokens =
                        json_u64(start_usage, "cache_read_input_tokens");
                }
            }
            "content_block_start" => {
                let index = chunk
                    .get("index")
                    .and_then(serde_json::Value::as_u64)
                    .unwrap_or(0);
                let block = chunk
                    .get("content_block")
                    .unwrap_or(&serde_json::Value::Null);
                if block.get("type").and_then(serde_json::Value::as_str) == Some("tool_use") {
                    let tool_use_id = block
                        .get("id")
                        .and_then(serde_json::Value::as_str)
                        .unwrap_or_default()
                        .to_owned();
                    let name = block
                        .get("name")
                        .and_then(serde_json::Value::as_str)
                        .unwrap_or_default()
                        .to_owned();
                    self.tool_calls.insert(
                        index,
                        StreamingToolCall {
                            id: tool_use_id.clone(),
                            name: name.clone(),
                            arguments: String::new(),
                        },
                    );
                    self.push_event(
                        StreamEvent::ToolUseStart(ToolUseStartEvent { tool_use_id, name }),
                        sink,
                    );
                }
            }
            "content_block_delta" => {
                let index = chunk
                    .get("index")
                    .and_then(serde_json::Value::as_u64)
                    .unwrap_or(0);
                let delta = chunk.get("delta").unwrap_or(&serde_json::Value::Null);
                match delta
                    .get("type")
                    .and_then(serde_json::Value::as_str)
                    .unwrap_or_default()
                {
                    "text_delta" => {
                        if let Some(text) = delta.get("text").and_then(serde_json::Value::as_str) {
                            self.push_event(
                                StreamEvent::TextDelta(TextDeltaEvent {
                                    text: text.to_owned(),
                                }),
                                sink,
                            );
                        }
                    }
                    "thinking_delta" => {
                        if let Some(thinking) =
                            delta.get("thinking").and_then(serde_json::Value::as_str)
                        {
                            self.push_event(
                                StreamEvent::ThinkingDelta(ThinkingDeltaEvent {
                                    text: thinking.to_owned(),
                                }),
                                sink,
                            );
                        }
                    }
                    "input_json_delta" => {
                        if let Some(partial_json) = delta
                            .get("partial_json")
                            .and_then(serde_json::Value::as_str)
                        {
                            if let Some(tool_call) = self.tool_calls.get_mut(&index) {
                                tool_call.arguments.push_str(partial_json);
                                let event = StreamEvent::ToolInputDelta(ToolInputDeltaEvent {
                                    tool_use_id: tool_call.id.clone(),
                                    partial_json: partial_json.to_owned(),
                                });
                                sink(&event);
                                self.events.push(event);
                            }
                        }
                    }
                    _ => {}
                }
            }
            "content_block_stop" => {
                let index = chunk
                    .get("index")
                    .and_then(serde_json::Value::as_u64)
                    .unwrap_or(0);
                if let Some(tool_call) = self.tool_calls.remove(&index) {
                    for event in parse_tool_input_events(
                        &tool_call.id,
                        &tool_call.name,
                        &tool_call.arguments,
                    ) {
                        self.push_event(event, sink);
                    }
                }
            }
            "message_delta" => {
                if let Some(reason) = chunk
                    .get("delta")
                    .and_then(|delta| delta.get("stop_reason"))
                    .and_then(serde_json::Value::as_str)
                {
                    self.stop_reason = reason.to_owned();
                }
                if let Some(delta_usage) = chunk.get("usage") {
                    self.usage.output_tokens = json_u64(delta_usage, "output_tokens");
                }
            }
            "message_stop" => {}
            _ => {}
        }
        Ok(())
    }

    fn finish(
        mut self,
        sink: &mut dyn FnMut(&StreamEvent),
    ) -> Result<Vec<StreamEvent>, StreamChatError> {
        if !self.saw_data {
            return Err(StreamChatError::without_partial(
                "API returned no data. Please check that your API Base URL is correct.",
            ));
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
