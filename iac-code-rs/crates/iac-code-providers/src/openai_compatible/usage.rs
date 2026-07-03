use iac_code_protocol::Usage;

pub(super) fn usage_from_value(usage: &serde_json::Value) -> Usage {
    let details = usage
        .get("prompt_tokens_details")
        .unwrap_or(&serde_json::Value::Null);
    Usage {
        input_tokens: usage
            .get("prompt_tokens")
            .and_then(serde_json::Value::as_u64)
            .unwrap_or(0),
        output_tokens: usage
            .get("completion_tokens")
            .and_then(serde_json::Value::as_u64)
            .unwrap_or(0),
        cache_creation_input_tokens: details
            .get("cache_creation_input_tokens")
            .and_then(serde_json::Value::as_u64)
            .unwrap_or(0),
        cache_read_input_tokens: details
            .get("cached_tokens")
            .and_then(serde_json::Value::as_u64)
            .unwrap_or(0),
    }
}
