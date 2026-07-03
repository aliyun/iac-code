use std::fmt;

use iac_code_protocol::json::{self, JsonValue};

#[derive(Clone, Debug, PartialEq, Eq)]
pub struct A2AWebSocketFrameError {
    pub code: i64,
    pub message: String,
}

impl A2AWebSocketFrameError {
    fn new(code: i64, message: impl Into<String>) -> Self {
        Self {
            code,
            message: message.into(),
        }
    }

    pub fn to_error_frame(&self, request_id: Option<JsonValue>) -> JsonValue {
        websocket_error_frame(request_id, self.code, &self.message)
    }
}

impl fmt::Display for A2AWebSocketFrameError {
    fn fmt(&self, formatter: &mut fmt::Formatter<'_>) -> fmt::Result {
        formatter.write_str(&self.message)
    }
}

impl std::error::Error for A2AWebSocketFrameError {}

pub fn websocket_event_frame(payload: JsonValue, final_event: bool) -> JsonValue {
    let request_id = payload_id(&payload).unwrap_or(JsonValue::Null);
    json::object([
        ("id", request_id),
        ("payload", payload),
        ("final", json::bool_value(final_event)),
    ])
}

pub fn websocket_error_frame(request_id: Option<JsonValue>, code: i64, message: &str) -> JsonValue {
    let payload = json::object([
        ("jsonrpc", json::string("2.0")),
        ("id", request_id.unwrap_or(JsonValue::Null)),
        (
            "error",
            json::object([
                ("code", json::number(code)),
                ("message", json::string(message)),
            ]),
        ),
    ]);
    websocket_event_frame(payload, true)
}

pub fn decode_websocket_request(text: &str) -> Result<JsonValue, A2AWebSocketFrameError> {
    let payload =
        json::parse(text).map_err(|_| A2AWebSocketFrameError::new(-32700, "Parse error"))?;
    match payload {
        JsonValue::Object(_) => Ok(payload),
        _ => Err(A2AWebSocketFrameError::new(-32600, "Invalid Request")),
    }
}

fn payload_id(payload: &JsonValue) -> Option<JsonValue> {
    let JsonValue::Object(object) = payload else {
        return None;
    };
    object.get("id").cloned()
}
