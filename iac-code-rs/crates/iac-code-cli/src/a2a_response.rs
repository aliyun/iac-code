use iac_code_protocol::{json, json::JsonValue};

use crate::json_utils::{json_object_field, json_string_value};

pub(super) enum A2AJsonRpcResponse {
    Json(JsonValue),
    Sse(Vec<JsonValue>),
}

pub(super) fn is_a2a_streaming_request(payload: &JsonValue) -> bool {
    matches!(
        json_object_field(payload, "method").and_then(json_string_value),
        Some("message/stream" | "StreamMessage" | "SendStreamingMessage")
    )
}

pub(super) fn a2a_final_jsonrpc_payload(request_payload: &JsonValue) -> JsonValue {
    json::object([
        ("jsonrpc", json::string("2.0")),
        (
            "id",
            json_object_field(request_payload, "id")
                .cloned()
                .unwrap_or(JsonValue::Null),
        ),
    ])
}
