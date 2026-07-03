use iac_code_protocol::json::{self, JsonValue};
use iac_code_protocol::provider::NonStreamingResponse;
use iac_code_protocol::{
    MessageEndEvent, MessageStartEvent, StreamEvent, TextDeltaEvent, ToolUseEndEvent,
    ToolUseStartEvent,
};

use super::usage::usage_from_value;

pub(super) fn parse_non_streaming_response(text: &str) -> Result<NonStreamingResponse, String> {
    let value: serde_json::Value = serde_json::from_str(text).map_err(|error| error.to_string())?;
    let content = value
        .get("content")
        .and_then(serde_json::Value::as_array)
        .cloned()
        .unwrap_or_default();
    let text_parts = content
        .iter()
        .filter(|block| block.get("type").and_then(serde_json::Value::as_str) == Some("text"))
        .filter_map(|block| block.get("text").and_then(serde_json::Value::as_str))
        .collect::<Vec<_>>()
        .join("");
    let tool_uses = content
        .iter()
        .filter(|block| block.get("type").and_then(serde_json::Value::as_str) == Some("tool_use"))
        .map(|block| {
            json::object([
                (
                    "id",
                    json::string(
                        block
                            .get("id")
                            .and_then(serde_json::Value::as_str)
                            .unwrap_or_default(),
                    ),
                ),
                (
                    "name",
                    json::string(
                        block
                            .get("name")
                            .and_then(serde_json::Value::as_str)
                            .unwrap_or_default(),
                    ),
                ),
                (
                    "input",
                    block
                        .get("input")
                        .cloned()
                        .map(json::from_serde)
                        .unwrap_or(JsonValue::Null),
                ),
            ])
        })
        .collect();

    Ok(NonStreamingResponse {
        message_id: value
            .get("id")
            .and_then(serde_json::Value::as_str)
            .unwrap_or("msg_anthropic")
            .to_owned(),
        text: text_parts,
        tool_uses,
        stop_reason: value
            .get("stop_reason")
            .and_then(serde_json::Value::as_str)
            .unwrap_or("end_turn")
            .to_owned(),
        usage: usage_from_value(value.get("usage").unwrap_or(&serde_json::Value::Null)),
        thinking: String::new(),
    })
}

pub(super) fn stream_events_from_response(response: NonStreamingResponse) -> Vec<StreamEvent> {
    let mut events = Vec::new();
    events.push(StreamEvent::MessageStart(MessageStartEvent {
        message_id: response.message_id,
    }));
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
