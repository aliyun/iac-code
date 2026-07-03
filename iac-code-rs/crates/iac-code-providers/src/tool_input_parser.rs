use std::sync::atomic::{AtomicU64, Ordering};

use iac_code_protocol::json::{self, JsonValue};
use iac_code_protocol::{StreamEvent, ToolUseEndEvent, ToolUseStartEvent};

static SYNTHETIC_TOOL_ID: AtomicU64 = AtomicU64::new(1);

pub(crate) fn parse_tool_input_events(
    tool_use_id: &str,
    tool_name: &str,
    raw_json: &str,
) -> Vec<StreamEvent> {
    if let Some(input) = parse_single_object(raw_json) {
        return vec![tool_use_end(tool_use_id, tool_name, input)];
    }

    if !raw_json.is_empty() {
        let parts = parse_concatenated_objects(raw_json);
        if let Some((first, remaining)) = parts.split_first() {
            let mut events = vec![tool_use_end(tool_use_id, tool_name, first.clone())];
            for input in remaining {
                let next_tool_use_id = next_synthetic_tool_use_id();
                events.push(StreamEvent::ToolUseStart(ToolUseStartEvent {
                    tool_use_id: next_tool_use_id.clone(),
                    name: tool_name.to_owned(),
                }));
                events.push(tool_use_end(&next_tool_use_id, tool_name, input.clone()));
            }
            return events;
        }
    }

    vec![tool_use_end(
        tool_use_id,
        tool_name,
        json::object(Vec::<(&str, JsonValue)>::new()),
    )]
}

fn parse_single_object(raw_json: &str) -> Option<JsonValue> {
    match serde_json::from_str::<serde_json::Value>(raw_json).ok()? {
        serde_json::Value::Object(fields) => {
            Some(json::from_serde(serde_json::Value::Object(fields)))
        }
        _ => None,
    }
}

fn parse_concatenated_objects(raw_json: &str) -> Vec<JsonValue> {
    let stream = serde_json::Deserializer::from_str(raw_json).into_iter::<serde_json::Value>();
    let mut values = Vec::new();
    for value in stream {
        match value {
            Ok(serde_json::Value::Object(fields)) => {
                values.push(json::from_serde(serde_json::Value::Object(fields)));
            }
            Ok(_) => {}
            Err(_) => break,
        }
    }
    values
}

fn tool_use_end(tool_use_id: &str, tool_name: &str, input: JsonValue) -> StreamEvent {
    StreamEvent::ToolUseEnd(ToolUseEndEvent {
        tool_use_id: tool_use_id.to_owned(),
        name: tool_name.to_owned(),
        input,
    })
}

fn next_synthetic_tool_use_id() -> String {
    let id = SYNTHETIC_TOOL_ID.fetch_add(1, Ordering::Relaxed);
    format!("toolu_{id:024x}")
}
