use iac_code_protocol::json::{self, JsonValue};
use iac_code_protocol::provider::NonStreamingResponse;
use iac_code_protocol::{
    MessageEndEvent, MessageStartEvent, StreamEvent, TextDeltaEvent, ThinkingDeltaEvent,
    ToolUseEndEvent, ToolUseStartEvent,
};

use crate::tool_input_parser::parse_tool_input_events;

use super::usage::usage_from_value;

pub(super) fn stream_events_from_response(response: NonStreamingResponse) -> Vec<StreamEvent> {
    let mut events = Vec::new();
    events.push(StreamEvent::MessageStart(MessageStartEvent {
        message_id: response.message_id,
    }));
    if !response.thinking.is_empty() {
        events.push(StreamEvent::ThinkingDelta(ThinkingDeltaEvent {
            text: response.thinking,
        }));
    }
    if !response.text.is_empty() {
        events.push(StreamEvent::TextDelta(TextDeltaEvent {
            text: response.text,
        }));
    }
    for tool_use in response.tool_uses {
        let tool_use_id = object_string_field(&tool_use, "id").unwrap_or_default();
        let name = object_string_field(&tool_use, "name").unwrap_or_default();
        events.push(StreamEvent::ToolUseStart(ToolUseStartEvent {
            tool_use_id: tool_use_id.clone(),
            name: name.clone(),
        }));
        events.push(StreamEvent::ToolUseEnd(ToolUseEndEvent {
            tool_use_id,
            name,
            input: object_field(&tool_use, "input")
                .cloned()
                .unwrap_or(JsonValue::Null),
        }));
    }
    events.push(StreamEvent::MessageEnd(MessageEndEvent {
        stop_reason: response.stop_reason,
        usage: response.usage,
    }));
    events
}

pub(super) fn parse_non_streaming_response(text: &str) -> Result<NonStreamingResponse, String> {
    let value: serde_json::Value = serde_json::from_str(text).map_err(|error| error.to_string())?;
    let choices = value
        .get("choices")
        .and_then(serde_json::Value::as_array)
        .ok_or_else(|| "API returned an invalid response: missing choices".to_owned())?;
    let choice = choices.first().ok_or_else(|| {
        "API returned an invalid response: Response choices were empty.".to_owned()
    })?;
    let message = choice
        .get("message")
        .ok_or_else(|| "API returned an invalid response: missing message".to_owned())?;

    let tool_uses = message
        .get("tool_calls")
        .and_then(serde_json::Value::as_array)
        .map(|tool_calls| {
            tool_calls
                .iter()
                .flat_map(parse_tool_call)
                .collect::<Vec<_>>()
        })
        .unwrap_or_default();

    let usage = value.get("usage").unwrap_or(&serde_json::Value::Null);

    Ok(NonStreamingResponse {
        message_id: value
            .get("id")
            .and_then(serde_json::Value::as_str)
            .unwrap_or("")
            .to_owned(),
        text: message
            .get("content")
            .and_then(serde_json::Value::as_str)
            .unwrap_or("")
            .to_owned(),
        tool_uses,
        stop_reason: stop_reason_from_choice(choice),
        usage: usage_from_value(usage),
        thinking: message
            .get("reasoning_content")
            .and_then(serde_json::Value::as_str)
            .unwrap_or("")
            .to_owned(),
    })
}

fn parse_tool_call(tool_call: &serde_json::Value) -> Vec<JsonValue> {
    let Some(id) = tool_call.get("id").and_then(serde_json::Value::as_str) else {
        return Vec::new();
    };
    let Some(function) = tool_call.get("function") else {
        return Vec::new();
    };
    let Some(name) = function.get("name").and_then(serde_json::Value::as_str) else {
        return Vec::new();
    };
    let raw_arguments = function
        .get("arguments")
        .and_then(serde_json::Value::as_str)
        .unwrap_or("");
    parse_tool_input_events(id, name, raw_arguments)
        .into_iter()
        .filter_map(|event| match event {
            StreamEvent::ToolUseEnd(event) => Some(json::object([
                ("id", json::string(event.tool_use_id)),
                ("name", json::string(event.name)),
                ("input", event.input),
            ])),
            _ => None,
        })
        .collect()
}

fn stop_reason_from_choice(choice: &serde_json::Value) -> String {
    match choice
        .get("finish_reason")
        .and_then(serde_json::Value::as_str)
        .unwrap_or("")
    {
        "tool_calls" => "tool_use".into(),
        "length" => "max_tokens".into(),
        _ => "end_turn".into(),
    }
}

fn object_field<'a>(value: &'a JsonValue, key: &str) -> Option<&'a JsonValue> {
    match value {
        JsonValue::Object(fields) => fields.get(key),
        _ => None,
    }
}

fn object_string_field(value: &JsonValue, key: &str) -> Option<String> {
    match object_field(value, key) {
        Some(JsonValue::String(value)) => Some(value.clone()),
        _ => None,
    }
}
