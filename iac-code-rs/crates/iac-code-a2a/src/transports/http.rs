use std::collections::BTreeMap;
use std::fmt;

use iac_code_protocol::json::{self, JsonValue};

use crate::transport::{headers_for_auth, A2AAuthConfig};

#[derive(Clone, Debug, PartialEq, Eq)]
pub struct A2AHttpTransportError {
    message: String,
}

impl A2AHttpTransportError {
    fn new(message: impl Into<String>) -> Self {
        Self {
            message: message.into(),
        }
    }
}

impl fmt::Display for A2AHttpTransportError {
    fn fmt(&self, formatter: &mut fmt::Formatter<'_>) -> fmt::Result {
        formatter.write_str(&self.message)
    }
}

impl std::error::Error for A2AHttpTransportError {}

pub trait SseLineInput {
    fn line_text(&self) -> Result<String, A2AHttpTransportError>;
}

impl SseLineInput for &str {
    fn line_text(&self) -> Result<String, A2AHttpTransportError> {
        Ok((*self).to_owned())
    }
}

impl SseLineInput for &[u8] {
    fn line_text(&self) -> Result<String, A2AHttpTransportError> {
        String::from_utf8((*self).to_vec()).map_err(|error| {
            A2AHttpTransportError::new(format!("Invalid A2A HTTP stream event: {error}"))
        })
    }
}

pub fn http_headers(auth: Option<&A2AAuthConfig>) -> BTreeMap<String, String> {
    let mut headers = BTreeMap::from([("A2A-Version".to_owned(), "1.0".to_owned())]);
    headers.extend(headers_for_auth(auth));
    headers
}

pub fn decode_http_response_json(value: JsonValue) -> Result<JsonValue, A2AHttpTransportError> {
    match value {
        JsonValue::Object(_) => Ok(value),
        _ => Err(A2AHttpTransportError::new(
            "A2A HTTP response must be a JSON object",
        )),
    }
}

pub fn decode_sse_data_line(
    input: impl SseLineInput,
) -> Result<Option<JsonValue>, A2AHttpTransportError> {
    let line = input.line_text()?;
    let Some(data) = line.strip_prefix("data:") else {
        return Ok(None);
    };
    let payload = json::parse(data.trim()).map_err(|error| {
        A2AHttpTransportError::new(format!("Invalid A2A HTTP stream event: {error}"))
    })?;
    Ok(Some(payload))
}
