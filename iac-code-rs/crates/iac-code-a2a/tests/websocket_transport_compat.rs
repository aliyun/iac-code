use iac_code_a2a::transports::websocket::{
    decode_websocket_request, websocket_error_frame, websocket_event_frame,
};
use iac_code_protocol::json::{self, JsonValue};

#[test]
fn websocket_event_frame_wraps_payload_id_and_final_flag() {
    let payload = json::object([
        ("jsonrpc", json::string("2.0")),
        ("id", json::string("task-1")),
        ("result", json::object([("ok", json::bool_value(true))])),
    ]);

    let frame = websocket_event_frame(payload.clone(), true);

    assert_eq!(string_at(&frame, &["id"]), "task-1");
    assert_eq!(at(&frame, &["payload"]), &payload);
    assert!(bool_at(&frame, &["final"]));
}

#[test]
fn websocket_event_frame_uses_null_id_when_payload_has_no_id() {
    let frame = websocket_event_frame(json::object([("jsonrpc", json::string("2.0"))]), false);

    assert_eq!(at(&frame, &["id"]), &JsonValue::Null);
    assert!(!bool_at(&frame, &["final"]));
}

#[test]
fn websocket_error_frame_matches_python_jsonrpc_error_shape() {
    let frame = websocket_error_frame(Some(json::number(9)), -32600, "Invalid Request");

    assert_eq!(at(&frame, &["id"]), &json::number(9));
    assert!(bool_at(&frame, &["final"]));
    assert_eq!(string_at(&frame, &["payload", "jsonrpc"]), "2.0");
    assert_eq!(at(&frame, &["payload", "id"]), &json::number(9));
    assert_eq!(
        at(&frame, &["payload", "error", "code"]),
        &json::number(-32600)
    );
    assert_eq!(
        string_at(&frame, &["payload", "error", "message"]),
        "Invalid Request"
    );
}

#[test]
fn websocket_decode_request_accepts_only_json_objects() {
    let payload =
        decode_websocket_request(r#"{"id":"abc","method":"message/send"}"#).expect("object");
    assert_eq!(string_at(&payload, &["method"]), "message/send");

    let parse_error = decode_websocket_request("{broken").expect_err("parse error");
    assert_eq!(parse_error.code, -32700);
    assert_eq!(parse_error.message, "Parse error");
    assert_eq!(
        parse_error.to_error_frame(None),
        websocket_error_frame(None, -32700, "Parse error")
    );

    let invalid_request = decode_websocket_request("[1]").expect_err("invalid request");
    assert_eq!(invalid_request.code, -32600);
    assert_eq!(invalid_request.message, "Invalid Request");
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
