use iac_code_protocol::json::JsonValue;

#[derive(Clone, Debug, PartialEq)]
pub struct A2AClientResponse {
    pub payload: JsonValue,
}

impl A2AClientResponse {
    pub fn text(&self) -> String {
        extract_response_text(&self.payload)
    }
}

pub fn extract_response_text(payload: &JsonValue) -> String {
    let Some(result) = object_field(payload, "result") else {
        return String::new();
    };
    if let Some(text) = string_field(result, "text").filter(|text| !text.is_empty()) {
        return text.to_owned();
    }
    if let Some(status) = object_field(result, "status") {
        let extracted = object_field(status, "message")
            .map(extract_parts_text)
            .unwrap_or_default();
        if !extracted.is_empty() {
            return extracted;
        }
    }
    let extracted = object_field(result, "message")
        .map(extract_parts_text)
        .unwrap_or_default();
    if !extracted.is_empty() {
        return extracted;
    }
    if let Some(task) = object_field(result, "task") {
        if let Some(task_status) = object_field(task, "status") {
            let extracted = object_field(task_status, "message")
                .map(extract_parts_text)
                .unwrap_or_default();
            if !extracted.is_empty() {
                return extracted;
            }
        }
        if let Some(JsonValue::Array(history)) = object_field(task, "history") {
            for entry in history.iter().rev() {
                let extracted = extract_agent_entry_text(entry);
                if !extracted.is_empty() {
                    return extracted;
                }
            }
        }
    }
    String::new()
}

fn extract_agent_entry_text(entry: &JsonValue) -> String {
    if string_field(entry, "role") != Some("ROLE_AGENT") {
        return String::new();
    }
    extract_parts_text(entry)
}

fn extract_parts_text(message: &JsonValue) -> String {
    let Some(JsonValue::Array(parts)) = object_field(message, "parts") else {
        return String::new();
    };
    parts
        .iter()
        .filter_map(|part| string_field(part, "text"))
        .collect::<Vec<_>>()
        .join("")
}

fn object_field<'a>(value: &'a JsonValue, key: &str) -> Option<&'a JsonValue> {
    let JsonValue::Object(object) = value else {
        return None;
    };
    object.get(key)
}

fn string_field<'a>(value: &'a JsonValue, key: &str) -> Option<&'a str> {
    match object_field(value, key) {
        Some(JsonValue::String(value)) => Some(value.as_str()),
        _ => None,
    }
}
