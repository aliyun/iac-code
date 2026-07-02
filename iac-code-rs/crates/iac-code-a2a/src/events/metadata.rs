use std::collections::BTreeMap;

use iac_code_protocol::json::{self, JsonValue};

use super::METADATA_MAX_CHARS;

const METADATA_MAX_DEPTH: usize = 32;

pub fn truncate_metadata(value: &JsonValue) -> JsonValue {
    truncate_metadata_at(value, 0)
}

fn truncate_metadata_at(value: &JsonValue, depth: usize) -> JsonValue {
    if depth >= METADATA_MAX_DEPTH {
        return json::string("[truncated-depth]");
    }

    match value {
        JsonValue::String(value) => json::string(truncate_string(value, METADATA_MAX_CHARS)),
        JsonValue::Array(values) => json::array(
            values
                .iter()
                .map(|value| truncate_metadata_at(value, depth + 1)),
        ),
        JsonValue::Object(values) => JsonValue::Object(
            values
                .iter()
                .map(|(key, value)| (key.clone(), truncate_metadata_at(value, depth + 1)))
                .collect::<BTreeMap<_, _>>(),
        ),
        _ => value.clone(),
    }
}

pub(super) fn truncate_string(value: &str, max_chars: usize) -> String {
    value.chars().take(max_chars).collect()
}
