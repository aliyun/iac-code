use std::collections::BTreeMap;

use iac_code_protocol::json::JsonValue;

pub(crate) fn params_with_region(input: &JsonValue, region: &str) -> BTreeMap<String, String> {
    let mut params = object_string_map(object_field(input, "params"));
    if !region.is_empty() {
        params
            .entry("RegionId".into())
            .or_insert_with(|| region.to_owned());
    }
    params
}

fn object_string_map(input: Option<&BTreeMap<String, JsonValue>>) -> BTreeMap<String, String> {
    input
        .map(|fields| {
            fields
                .iter()
                .map(|(key, value)| (key.clone(), json_value_to_param(value)))
                .collect()
        })
        .unwrap_or_default()
}

fn json_value_to_param(value: &JsonValue) -> String {
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

pub(crate) fn string_field<'a>(input: &'a JsonValue, field: &str) -> Option<&'a str> {
    let JsonValue::Object(fields) = input else {
        return None;
    };
    match fields.get(field) {
        Some(JsonValue::String(value)) => Some(value.as_str()),
        _ => None,
    }
}

fn object_field<'a>(input: &'a JsonValue, field: &str) -> Option<&'a BTreeMap<String, JsonValue>> {
    let JsonValue::Object(fields) = input else {
        return None;
    };
    match fields.get(field) {
        Some(JsonValue::Object(value)) => Some(value),
        _ => None,
    }
}
