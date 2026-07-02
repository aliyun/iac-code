use iac_code_a2a::transports::stdio::{
    decode_frame, encode_frame, error_response, is_streaming_request,
};
use iac_code_protocol::json::{self, JsonValue};

#[test]
fn stdio_frame_encodes_compact_json_with_newline_and_decodes_bytes_or_text() {
    let payload = json::object([
        ("jsonrpc", json::string("2.0")),
        ("id", json::string("1")),
        ("result", json::object([("ok", json::bool_value(true))])),
    ]);

    let frame = encode_frame(&payload);

    assert_eq!(
        String::from_utf8(frame.clone()).expect("utf8"),
        "{\"id\":\"1\",\"jsonrpc\":\"2.0\",\"result\":{\"ok\":true}}\n"
    );
    assert_eq!(decode_frame(&frame).expect("decoded"), payload);
    assert_eq!(decode_frame(frame.as_slice()).expect("decoded"), payload);
    assert_eq!(
        decode_frame("{\"jsonrpc\":\"2.0\",\"id\":\"1\",\"result\":{\"ok\":true}}\n")
            .expect("decoded"),
        payload
    );
}

#[test]
fn stdio_frame_reports_malformed_json_and_non_object_payloads_like_python() {
    let malformed = decode_frame("{broken").expect_err("malformed json");
    assert!(malformed
        .to_string()
        .starts_with("Invalid JSON-RPC frame: "));

    let non_object = decode_frame("[1,2,3]").expect_err("non object");
    assert_eq!(
        non_object.to_string(),
        "A2A frame must decode to a JSON object"
    );
}

#[test]
fn stdio_frame_detects_python_streaming_method_names() {
    assert!(is_streaming_request(&json::object([(
        "method",
        json::string("message/stream")
    )])));
    assert!(is_streaming_request(&json::object([(
        "method",
        json::string("StreamMessage")
    )])));
    assert!(is_streaming_request(&json::object([(
        "method",
        json::string("SendStreamingMessage")
    )])));
    assert!(!is_streaming_request(&json::object([(
        "method",
        json::string("message/send")
    )])));
    assert!(!is_streaming_request(
        &JsonValue::Object(Default::default())
    ));
}

#[test]
fn stdio_frame_error_response_matches_jsonrpc_shape() {
    assert_eq!(
        error_response(Some(json::string("req-1")), "boom").to_compact_json(),
        r#"{"error":{"code":-32603,"message":"boom"},"id":"req-1","jsonrpc":"2.0"}"#
    );
    assert_eq!(
        error_response(None, "boom").to_compact_json(),
        r#"{"error":{"code":-32603,"message":"boom"},"id":null,"jsonrpc":"2.0"}"#
    );
}
