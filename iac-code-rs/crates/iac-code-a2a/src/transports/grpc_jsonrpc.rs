use std::fmt;

use iac_code_protocol::json::{self, JsonValue};

#[derive(Clone, Debug, PartialEq, Eq)]
pub struct JsonRpcEnvelope {
    pub payload: Vec<u8>,
    pub r#final: bool,
}

impl JsonRpcEnvelope {
    pub fn new(payload: Vec<u8>, final_event: bool) -> Self {
        Self {
            payload,
            r#final: final_event,
        }
    }
}

#[derive(Clone, Debug, PartialEq, Eq)]
pub struct A2AGrpcJsonRpcError {
    message: String,
}

impl A2AGrpcJsonRpcError {
    pub fn new(message: impl Into<String>) -> Self {
        Self {
            message: message.into(),
        }
    }
}

impl fmt::Display for A2AGrpcJsonRpcError {
    fn fmt(&self, formatter: &mut fmt::Formatter<'_>) -> fmt::Result {
        formatter.write_str(&self.message)
    }
}

impl std::error::Error for A2AGrpcJsonRpcError {}

pub fn to_grpc_jsonrpc_envelope(payload: &JsonValue) -> JsonRpcEnvelope {
    JsonRpcEnvelope::new(payload.to_compact_json().into_bytes(), false)
}

pub fn from_grpc_jsonrpc_envelope(
    envelope: &JsonRpcEnvelope,
) -> Result<JsonValue, A2AGrpcJsonRpcError> {
    let text = std::str::from_utf8(&envelope.payload)
        .map_err(|error| A2AGrpcJsonRpcError::new(error.to_string()))?;
    let payload = json::parse(text).map_err(A2AGrpcJsonRpcError::new)?;
    match payload {
        JsonValue::Object(_) => Ok(payload),
        _ => Err(A2AGrpcJsonRpcError::new(
            "gRPC A2A envelope must contain a JSON object",
        )),
    }
}

pub fn stream_payload_from_grpc_jsonrpc_envelope(
    envelope: &JsonRpcEnvelope,
) -> Result<JsonValue, A2AGrpcJsonRpcError> {
    let mut payload = from_grpc_jsonrpc_envelope(envelope)?;
    if envelope.r#final {
        let JsonValue::Object(object) = &mut payload else {
            unreachable!("from_grpc_jsonrpc_envelope only returns objects");
        };
        object.insert("final".to_owned(), json::bool_value(true));
    }
    Ok(payload)
}
