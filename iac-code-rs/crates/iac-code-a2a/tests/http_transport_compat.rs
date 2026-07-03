use std::collections::BTreeMap;

use iac_code_a2a::transport::A2AAuthConfig;
use iac_code_a2a::transports::http::{
    decode_http_response_json, decode_sse_data_line, http_headers,
};
use iac_code_protocol::json::{self, JsonValue};

#[test]
fn http_transport_headers_add_a2a_version_and_auth_headers() {
    assert_eq!(
        http_headers(Some(&A2AAuthConfig {
            bearer_token: Some("secret".to_owned()),
            api_key: Some("api-secret".to_owned()),
            api_key_header: "X-IAC-Code-Key".to_owned(),
            basic_username: None,
            basic_password: None,
        })),
        BTreeMap::from([
            ("A2A-Version".to_owned(), "1.0".to_owned()),
            ("Authorization".to_owned(), "Bearer secret".to_owned()),
            ("X-IAC-Code-Key".to_owned(), "api-secret".to_owned()),
        ])
    );
    assert_eq!(
        http_headers(None),
        BTreeMap::from([("A2A-Version".to_owned(), "1.0".to_owned())])
    );
}

#[test]
fn http_transport_response_must_be_json_object() {
    let payload = json::object([("result", json::object([("ok", json::bool_value(true))]))]);

    assert_eq!(
        decode_http_response_json(payload.clone()).expect("object"),
        payload
    );
    assert_eq!(
        decode_http_response_json(json::array([json::number(1)]))
            .expect_err("non object")
            .to_string(),
        "A2A HTTP response must be a JSON object"
    );
}

#[test]
fn http_transport_decodes_only_sse_data_lines() {
    assert!(decode_sse_data_line("").expect("blank").is_none());
    assert!(decode_sse_data_line("event: ignored")
        .expect("event")
        .is_none());

    let event = decode_sse_data_line("data: {\"result\": {\"status\": {\"state\": \"working\"}}}")
        .expect("decode")
        .expect("data event");

    assert_eq!(string_at(&event, &["result", "status", "state"]), "working");
}

#[test]
fn http_transport_sse_data_lines_accept_bytes_and_report_invalid_json() {
    let event = decode_sse_data_line(b"data: {\"final\": true}".as_slice())
        .expect("decode")
        .expect("data event");

    assert_eq!(event.to_compact_json(), r#"{"final":true}"#);

    let error = decode_sse_data_line("data: {broken").expect_err("invalid json");
    assert!(error
        .to_string()
        .starts_with("Invalid A2A HTTP stream event: "));
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
