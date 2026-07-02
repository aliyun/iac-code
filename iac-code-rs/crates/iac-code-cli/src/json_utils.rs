use std::collections::BTreeMap;

use iac_code_protocol::json::JsonValue;

pub(super) fn json_string_or_empty<'a>(value: &'a JsonValue, key: &str) -> &'a str {
    json_string_field(value, key).unwrap_or_default()
}

pub(super) fn json_object_field<'a>(value: &'a JsonValue, key: &str) -> Option<&'a JsonValue> {
    let JsonValue::Object(object) = value else {
        return None;
    };
    object.get(key)
}

pub(super) fn json_object_field_map<'a>(
    value: &'a BTreeMap<String, JsonValue>,
    key: &str,
) -> Option<&'a BTreeMap<String, JsonValue>> {
    match value.get(key) {
        Some(JsonValue::Object(value)) => Some(value),
        _ => None,
    }
}

pub(super) fn json_string_field<'a>(value: &'a JsonValue, key: &str) -> Option<&'a str> {
    match json_object_field(value, key) {
        Some(JsonValue::String(value)) => Some(value),
        _ => None,
    }
}

pub(super) fn json_string_field_map<'a>(
    value: &'a BTreeMap<String, JsonValue>,
    key: &str,
) -> Option<&'a str> {
    match value.get(key) {
        Some(JsonValue::String(value)) => Some(value),
        _ => None,
    }
}

pub(super) fn json_string_value(value: &JsonValue) -> Option<&str> {
    match value {
        JsonValue::String(value) => Some(value.as_str()),
        _ => None,
    }
}

pub(super) fn json_bool_field(value: &JsonValue, key: &str) -> Option<bool> {
    match json_object_field(value, key) {
        Some(JsonValue::Bool(value)) => Some(*value),
        _ => None,
    }
}

pub(super) fn json_number_i32_field(value: &JsonValue, key: &str) -> Option<i32> {
    json_number_i64_field(value, key).and_then(|value| i32::try_from(value).ok())
}

pub(super) fn json_number_i64_field(value: &JsonValue, key: &str) -> Option<i64> {
    match json_object_field(value, key) {
        Some(JsonValue::Number(value)) => value.parse().ok(),
        _ => None,
    }
}

pub(super) fn json_number_usize_field(value: &JsonValue, key: &str) -> Option<usize> {
    match json_object_field(value, key) {
        Some(JsonValue::Number(value)) => value.parse().ok(),
        _ => None,
    }
}

pub(super) fn format_pretty_json(value: &JsonValue) -> String {
    let mut output = String::new();
    write_pretty_json(value, 0, &mut output);
    output
}

fn write_pretty_json(value: &JsonValue, indent: usize, output: &mut String) {
    match value {
        JsonValue::Array(values) => write_pretty_json_array(values, indent, output),
        JsonValue::Object(values) => write_pretty_json_object(values, indent, output),
        _ => output.push_str(&value.to_compact_json()),
    }
}

fn write_pretty_json_array(values: &[JsonValue], indent: usize, output: &mut String) {
    if values.is_empty() {
        output.push_str("[]");
        return;
    }
    output.push('[');
    output.push('\n');
    for (index, value) in values.iter().enumerate() {
        if index > 0 {
            output.push_str(",\n");
        }
        output.push_str(&" ".repeat(indent + 2));
        write_pretty_json(value, indent + 2, output);
    }
    output.push('\n');
    output.push_str(&" ".repeat(indent));
    output.push(']');
}

fn write_pretty_json_object(
    values: &BTreeMap<String, JsonValue>,
    indent: usize,
    output: &mut String,
) {
    if values.is_empty() {
        output.push_str("{}");
        return;
    }
    output.push('{');
    output.push('\n');
    for (index, (key, value)) in values.iter().enumerate() {
        if index > 0 {
            output.push_str(",\n");
        }
        output.push_str(&" ".repeat(indent + 2));
        output.push_str(&json_string(key));
        output.push_str(": ");
        write_pretty_json(value, indent + 2, output);
    }
    output.push('\n');
    output.push_str(&" ".repeat(indent));
    output.push('}');
}

pub(super) fn json_string(value: &str) -> String {
    JsonValue::String(value.to_owned()).to_compact_json()
}
