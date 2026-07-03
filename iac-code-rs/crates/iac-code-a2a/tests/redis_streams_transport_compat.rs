use std::collections::BTreeMap;

use iac_code_a2a::transports::redis_streams::{
    parse_redis_entry, redis_reply_stream, redis_request_fields, redis_response_fields,
};
use iac_code_protocol::json::{self, JsonValue};

#[test]
fn redis_streams_parse_entry_decodes_python_field_shape() {
    let fields = redis_fields([
        ("payload", r#"{"id":"req-1","method":"message/send"}"#),
        ("correlation_id", "corr-1"),
        ("final", "true"),
    ]);

    let message = parse_redis_entry("1700000000000-0", &fields).expect("message");

    assert_eq!(message.entry_id, "1700000000000-0");
    assert_eq!(message.correlation_id, "corr-1");
    assert_eq!(string_at(&message.payload, &["method"]), "message/send");
    assert!(message.r#final);
}

#[test]
fn redis_streams_final_flag_accepts_python_truthy_values() {
    for value in ["true", "1"] {
        let fields = redis_fields([
            ("payload", r#"{"jsonrpc":"2.0"}"#),
            ("correlation_id", "corr-1"),
            ("final", value),
        ]);

        assert!(parse_redis_entry("1-0", &fields).expect("message").r#final);
    }

    let fields = redis_fields([
        ("payload", r#"{"jsonrpc":"2.0"}"#),
        ("correlation_id", "corr-1"),
        ("final", "false"),
    ]);
    assert!(!parse_redis_entry("1-0", &fields).expect("message").r#final);
}

#[test]
fn redis_streams_parse_entry_requires_json_object_payload() {
    let fields = redis_fields([
        ("payload", r#"[1]"#),
        ("correlation_id", "corr-1"),
        ("final", "false"),
    ]);

    assert_eq!(
        parse_redis_entry("1-0", &fields).unwrap_err().to_string(),
        "Redis Streams A2A payload must be a JSON object"
    );
}

#[test]
fn redis_streams_request_and_response_fields_match_python_compact_json() {
    let payload = json::object([
        ("jsonrpc", json::string("2.0")),
        ("id", json::number(3)),
        ("result", json::object([("ok", json::bool_value(true))])),
    ]);

    let request = redis_request_fields("corr-2", "iac-code:a2a:responses", &payload);
    assert_eq!(field_text(&request, "correlation_id"), "corr-2");
    assert_eq!(
        field_text(&request, "reply_stream"),
        "iac-code:a2a:responses"
    );
    assert_eq!(
        field_text(&request, "payload"),
        r#"{"id":3,"jsonrpc":"2.0","result":{"ok":true}}"#
    );

    let response = redis_response_fields("corr-2", &payload, true);
    assert_eq!(field_text(&response, "correlation_id"), "corr-2");
    assert_eq!(
        field_text(&response, "payload"),
        r#"{"id":3,"jsonrpc":"2.0","result":{"ok":true}}"#
    );
    assert_eq!(field_text(&response, "final"), "true");
}

#[test]
fn redis_streams_reply_stream_uses_field_or_default() {
    let with_reply = redis_fields([("reply_stream", "custom:responses")]);
    assert_eq!(
        redis_reply_stream(&with_reply, "iac-code:a2a:responses"),
        "custom:responses"
    );

    assert_eq!(
        redis_reply_stream(&BTreeMap::new(), "iac-code:a2a:responses"),
        "iac-code:a2a:responses"
    );
}

fn redis_fields(
    entries: impl IntoIterator<Item = (&'static str, &'static str)>,
) -> BTreeMap<Vec<u8>, Vec<u8>> {
    entries
        .into_iter()
        .map(|(key, value)| (key.as_bytes().to_vec(), value.as_bytes().to_vec()))
        .collect()
}

fn field_text(fields: &BTreeMap<Vec<u8>, Vec<u8>>, name: &str) -> String {
    String::from_utf8(fields.get(name.as_bytes()).expect("field").clone()).expect("utf-8")
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
