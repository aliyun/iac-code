use std::fmt;

use iac_code_protocol::json::{self, JsonValue};

#[derive(Clone, Debug, PartialEq, Eq)]
pub struct A2AFrameError {
    message: String,
}

impl A2AFrameError {
    fn new(message: impl Into<String>) -> Self {
        Self {
            message: message.into(),
        }
    }
}

impl fmt::Display for A2AFrameError {
    fn fmt(&self, formatter: &mut fmt::Formatter<'_>) -> fmt::Result {
        formatter.write_str(&self.message)
    }
}

impl std::error::Error for A2AFrameError {}

pub trait FrameInput {
    fn frame_text(&self) -> Result<String, A2AFrameError>;
}

impl FrameInput for &str {
    fn frame_text(&self) -> Result<String, A2AFrameError> {
        Ok((*self).to_owned())
    }
}

impl FrameInput for &[u8] {
    fn frame_text(&self) -> Result<String, A2AFrameError> {
        String::from_utf8((*self).to_vec())
            .map_err(|error| A2AFrameError::new(format!("Invalid JSON-RPC frame: {error}")))
    }
}

impl FrameInput for &Vec<u8> {
    fn frame_text(&self) -> Result<String, A2AFrameError> {
        self.as_slice().frame_text()
    }
}

pub fn encode_frame(payload: &JsonValue) -> Vec<u8> {
    let mut output = payload.to_compact_json();
    output.push('\n');
    output.into_bytes()
}

pub fn decode_frame(input: impl FrameInput) -> Result<JsonValue, A2AFrameError> {
    let text = input.frame_text()?;
    let payload = json::parse(text.trim_end_matches(['\r', '\n']))
        .map_err(|error| A2AFrameError::new(format!("Invalid JSON-RPC frame: {error}")))?;
    match payload {
        JsonValue::Object(_) => Ok(payload),
        _ => Err(A2AFrameError::new("A2A frame must decode to a JSON object")),
    }
}

pub fn is_streaming_request(payload: &JsonValue) -> bool {
    matches!(
        object_string(payload, "method"),
        Some("message/stream" | "StreamMessage" | "SendStreamingMessage")
    )
}

pub fn error_response(request_id: Option<JsonValue>, message: &str) -> JsonValue {
    json::object([
        ("jsonrpc", json::string("2.0")),
        ("id", request_id.unwrap_or(JsonValue::Null)),
        (
            "error",
            json::object([
                ("code", json::number(-32603)),
                ("message", json::string(message)),
            ]),
        ),
    ])
}

fn object_string<'a>(value: &'a JsonValue, key: &str) -> Option<&'a str> {
    let JsonValue::Object(object) = value else {
        return None;
    };
    match object.get(key) {
        Some(JsonValue::String(value)) => Some(value.as_str()),
        _ => None,
    }
}
