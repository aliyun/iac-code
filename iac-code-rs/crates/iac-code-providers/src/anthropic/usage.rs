use iac_code_protocol::Usage;

pub(super) fn usage_from_value(value: &serde_json::Value) -> Usage {
    Usage {
        input_tokens: json_u64(value, "input_tokens"),
        output_tokens: json_u64(value, "output_tokens"),
        cache_creation_input_tokens: json_u64(value, "cache_creation_input_tokens"),
        cache_read_input_tokens: json_u64(value, "cache_read_input_tokens"),
    }
}

pub(super) fn json_u64(value: &serde_json::Value, key: &str) -> u64 {
    value
        .get(key)
        .and_then(serde_json::Value::as_u64)
        .unwrap_or(0)
}
