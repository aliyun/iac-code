use std::collections::BTreeMap;

use iac_code_protocol::json::JsonValue;

pub(super) fn object_string_map(
    input: Option<&BTreeMap<String, JsonValue>>,
) -> BTreeMap<String, String> {
    input
        .map(|fields| {
            fields
                .iter()
                .map(|(key, value)| (key.clone(), json_value_to_param(value)))
                .collect()
        })
        .unwrap_or_default()
}

pub(super) fn json_value_to_param(value: &JsonValue) -> String {
    match value {
        JsonValue::String(value) | JsonValue::Number(value) => value.clone(),
        JsonValue::Bool(value) => {
            if *value {
                "true".into()
            } else {
                "false".into()
            }
        }
        JsonValue::Null => "null".into(),
        JsonValue::Array(_) | JsonValue::Object(_) => value.to_compact_json(),
    }
}

pub(super) fn pretty_json_or_raw(body: &str) -> String {
    serde_json::from_str::<serde_json::Value>(body)
        .and_then(|value| serde_json::to_string_pretty(&value))
        .unwrap_or_else(|_| body.to_owned())
}

pub(super) fn clean_error_message(message: &str) -> String {
    message
        .find(" Response: {")
        .map(|index| message[..index].trim().to_owned())
        .unwrap_or_else(|| message.trim().to_owned())
}

pub(super) fn string_field<'a>(input: &'a JsonValue, field: &str) -> Option<&'a str> {
    let JsonValue::Object(fields) = input else {
        return None;
    };
    match fields.get(field) {
        Some(JsonValue::String(value)) => Some(value.as_str()),
        _ => None,
    }
}

pub(super) fn json_field<'a>(input: &'a JsonValue, field: &str) -> Option<&'a JsonValue> {
    let JsonValue::Object(fields) = input else {
        return None;
    };
    fields.get(field)
}

pub(super) fn object_field<'a>(
    input: &'a JsonValue,
    field: &str,
) -> Option<&'a BTreeMap<String, JsonValue>> {
    let JsonValue::Object(fields) = input else {
        return None;
    };
    match fields.get(field) {
        Some(JsonValue::Object(value)) => Some(value),
        _ => None,
    }
}
