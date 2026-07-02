use std::collections::BTreeMap;

use iac_code_protocol::{json, json::JsonValue};

pub(super) fn empty_json_object() -> JsonValue {
    JsonValue::Object(BTreeMap::new())
}

pub(super) fn jsonrpc_result(id: JsonValue, result: JsonValue) -> JsonValue {
    json::object([
        ("id", id),
        ("jsonrpc", json::string("2.0")),
        ("result", result),
    ])
}

pub(super) fn jsonrpc_error(id: JsonValue, code: i64, message: &str) -> JsonValue {
    json::object([
        ("id", id),
        ("jsonrpc", json::string("2.0")),
        (
            "error",
            json::object([
                ("code", json::number(code)),
                ("message", json::string(message)),
            ]),
        ),
    ])
}

pub(super) fn jsonrpc_error_with_data(
    id: JsonValue,
    code: i64,
    message: &str,
    data: JsonValue,
) -> JsonValue {
    json::object([
        ("id", id),
        ("jsonrpc", json::string("2.0")),
        (
            "error",
            json::object([
                ("code", json::number(code)),
                ("message", json::string(message)),
                ("data", data),
            ]),
        ),
    ])
}
