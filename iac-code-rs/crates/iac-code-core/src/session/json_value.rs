use std::collections::BTreeMap;

use iac_code_protocol::json::JsonValue;

pub(super) fn object_fields(value: &JsonValue) -> Option<&BTreeMap<String, JsonValue>> {
    match value {
        JsonValue::Object(fields) => Some(fields),
        _ => None,
    }
}

pub(super) fn object_string<'a>(
    fields: &'a BTreeMap<String, JsonValue>,
    key: &str,
) -> Option<&'a str> {
    match fields.get(key) {
        Some(JsonValue::String(value)) => Some(value.as_str()),
        _ => None,
    }
}

pub(super) fn object_bool(fields: &BTreeMap<String, JsonValue>, key: &str) -> Option<bool> {
    match fields.get(key) {
        Some(JsonValue::Bool(value)) => Some(*value),
        _ => None,
    }
}

pub(super) fn object_u64(fields: &BTreeMap<String, JsonValue>, key: &str) -> Option<u64> {
    match fields.get(key) {
        Some(JsonValue::Number(value)) => value.parse().ok(),
        _ => None,
    }
}

pub(super) fn object_f64(fields: &BTreeMap<String, JsonValue>, key: &str) -> Option<f64> {
    match fields.get(key) {
        Some(JsonValue::Number(value)) => value.parse().ok(),
        _ => None,
    }
}

pub(super) fn empty_json_object() -> JsonValue {
    JsonValue::Object(BTreeMap::new())
}
