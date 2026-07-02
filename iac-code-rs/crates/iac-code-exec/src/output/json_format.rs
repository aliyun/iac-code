use std::collections::BTreeMap;
use std::fmt::Write;

use iac_code_protocol::json::JsonValue;
use iac_code_protocol::{
    ErrorEvent, MessageEndEvent, StreamEvent, ToJsonValue, ToolResultEvent, ToolUseEndEvent,
    ToolUseStartEvent, Usage,
};

pub(super) fn stream_event_json(event: &StreamEvent) -> String {
    match event {
        StreamEvent::MessageStart(event) => object_json(&[
            ("message_id", json_string(&event.message_id)),
            ("type", json_string("message_start")),
        ]),
        StreamEvent::TextDelta(event) => object_json(&[
            ("text", json_string(&event.text)),
            ("type", json_string("text_delta")),
        ]),
        StreamEvent::ToolUseStart(ToolUseStartEvent { tool_use_id, name }) => object_json(&[
            ("tool_use_id", json_string(tool_use_id)),
            ("name", json_string(name)),
            ("type", json_string("tool_use_start")),
        ]),
        StreamEvent::ToolUseEnd(ToolUseEndEvent {
            tool_use_id,
            name,
            input,
        }) => object_json(&[
            ("tool_use_id", json_string(tool_use_id)),
            ("name", json_string(name)),
            ("input", json_value_python(input)),
            ("type", json_string("tool_use_end")),
        ]),
        StreamEvent::ToolResult(ToolResultEvent {
            tool_use_id,
            tool_name,
            result,
            is_error,
        }) => object_json(&[
            ("tool_use_id", json_string(tool_use_id)),
            ("tool_name", json_string(tool_name)),
            ("result", json_string(result)),
            ("is_error", bool_json(*is_error)),
            ("type", json_string("tool_result")),
        ]),
        StreamEvent::MessageEnd(MessageEndEvent { stop_reason, usage }) => object_json(&[
            ("stop_reason", json_string(stop_reason)),
            ("usage", usage_json(usage)),
            ("type", json_string("message_end")),
        ]),
        StreamEvent::Error(ErrorEvent {
            error,
            is_retryable,
        }) => object_json(&[
            ("error", json_string(error)),
            ("is_retryable", bool_json(*is_retryable)),
            ("type", json_string("error")),
        ]),
        _ => event.to_compact_json(),
    }
}

pub(super) fn usage_json(usage: &Usage) -> String {
    object_json(&[
        ("input_tokens", usage.input_tokens.to_string()),
        ("output_tokens", usage.output_tokens.to_string()),
        (
            "cache_creation_input_tokens",
            usage.cache_creation_input_tokens.to_string(),
        ),
        (
            "cache_read_input_tokens",
            usage.cache_read_input_tokens.to_string(),
        ),
    ])
}

pub(super) fn json_value_python(value: &JsonValue) -> String {
    match value {
        JsonValue::Null => "null".to_owned(),
        JsonValue::Bool(value) => bool_json(*value),
        JsonValue::Number(value) => value.clone(),
        JsonValue::String(value) => json_string(value),
        JsonValue::Array(values) => {
            array_json(&values.iter().map(json_value_python).collect::<Vec<_>>())
        }
        JsonValue::Object(values) => ordered_object_json(values),
    }
}

fn ordered_object_json(values: &BTreeMap<String, JsonValue>) -> String {
    let fields = values
        .iter()
        .map(|(key, value)| (key.as_str(), json_value_python(value)))
        .collect::<Vec<_>>();
    object_json(&fields)
}

pub(super) fn object_json(fields: &[(&str, String)]) -> String {
    let body = fields
        .iter()
        .map(|(key, value)| format!("{}: {}", json_string(key), value))
        .collect::<Vec<_>>()
        .join(", ");
    format!("{{{body}}}")
}

pub(super) fn array_json(values: &[String]) -> String {
    format!("[{}]", values.join(", "))
}

pub(super) fn bool_json(value: bool) -> String {
    (if value { "true" } else { "false" }).to_owned()
}

pub(super) fn json_string(value: &str) -> String {
    let mut output = String::new();
    output.push('"');
    for character in value.chars() {
        match character {
            '"' => output.push_str("\\\""),
            '\\' => output.push_str("\\\\"),
            '\u{08}' => output.push_str("\\b"),
            '\u{0c}' => output.push_str("\\f"),
            '\n' => output.push_str("\\n"),
            '\r' => output.push_str("\\r"),
            '\t' => output.push_str("\\t"),
            character if character <= '\u{1f}' => {
                write!(output, "\\u{:04x}", character as u32)
                    .expect("writing to String should not fail");
            }
            character => output.push(character),
        }
    }
    output.push('"');
    output
}
