use iac_code_a2a::transports::grpc_jsonrpc::{
    from_grpc_jsonrpc_envelope, stream_payload_from_grpc_jsonrpc_envelope,
    to_grpc_jsonrpc_envelope, JsonRpcEnvelope,
};
use iac_code_protocol::json::{self, JsonValue};

#[test]
fn grpc_jsonrpc_envelope_serializes_payload_as_compact_json_bytes() {
    let payload = json::object([
        ("jsonrpc", json::string("2.0")),
        ("id", json::number(4)),
        ("params", json::object([("message", json::string("hello"))])),
    ]);

    let envelope = to_grpc_jsonrpc_envelope(&payload);

    assert_eq!(
        envelope.payload,
        br#"{"id":4,"jsonrpc":"2.0","params":{"message":"hello"}}"#.to_vec()
    );
    assert!(!envelope.r#final);
}

#[test]
fn grpc_jsonrpc_envelope_decodes_only_json_objects() {
    let envelope = JsonRpcEnvelope::new(br#"{"id":"req-1","result":{"ok":true}}"#.to_vec(), false);

    let payload = from_grpc_jsonrpc_envelope(&envelope).expect("payload");

    assert_eq!(string_at(&payload, &["id"]), "req-1");
    assert!(bool_at(&payload, &["result", "ok"]));

    let invalid = JsonRpcEnvelope::new(br#"[1]"#.to_vec(), false);
    assert_eq!(
        from_grpc_jsonrpc_envelope(&invalid)
            .unwrap_err()
            .to_string(),
        "gRPC A2A envelope must contain a JSON object"
    );
}

#[test]
fn grpc_jsonrpc_stream_payload_injects_final_flag_like_python_client() {
    let envelope = JsonRpcEnvelope::new(br#"{"jsonrpc":"2.0","id":"req-2"}"#.to_vec(), true);

    let payload = stream_payload_from_grpc_jsonrpc_envelope(&envelope).expect("payload");

    assert_eq!(string_at(&payload, &["id"]), "req-2");
    assert!(bool_at(&payload, &["final"]));
}

#[test]
fn grpc_jsonrpc_stream_payload_keeps_non_final_payload_unchanged() {
    let envelope = JsonRpcEnvelope::new(
        br#"{"jsonrpc":"2.0","id":"req-3","final":false}"#.to_vec(),
        false,
    );

    let payload = stream_payload_from_grpc_jsonrpc_envelope(&envelope).expect("payload");

    assert!(!bool_at(&payload, &["final"]));
}

fn bool_at(value: &JsonValue, path: &[&str]) -> bool {
    match at(value, path) {
        JsonValue::Bool(value) => *value,
        other => panic!("expected bool at {path:?}, got {other:?}"),
    }
}

fn string_at(value: &JsonValue, path: &[&str]) -> String {
    match at(value, path) {
        JsonValue::String(value) => value.clone(),
        other => panic!("expected string at {path:?}, got {other:?}"),
    }
}

fn at<'a>(mut value: &'a JsonValue, path: &[&str]) -> &'a JsonValue {
    for segment in path {
        value = match value {
            JsonValue::Object(object) => object.get(*segment).unwrap_or_else(|| {
                panic!("missing object key {segment:?} in path {path:?}");
            }),
            JsonValue::Array(values) => values
                .get(segment.parse::<usize>().expect("array index"))
                .unwrap_or_else(|| panic!("missing array index {segment:?} in path {path:?}")),
            other => panic!("cannot descend into {other:?} at {segment:?}"),
        };
    }
    value
}
