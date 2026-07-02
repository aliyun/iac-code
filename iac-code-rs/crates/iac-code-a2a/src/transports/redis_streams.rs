use std::collections::BTreeMap;
use std::fmt;

use iac_code_protocol::json::{self, JsonValue};

pub type RedisFields = BTreeMap<Vec<u8>, Vec<u8>>;

#[derive(Clone, Debug, PartialEq, Eq)]
pub struct A2ARedisStreamsError {
    message: String,
}

impl A2ARedisStreamsError {
    pub fn new(message: impl Into<String>) -> Self {
        Self {
            message: message.into(),
        }
    }
}

impl fmt::Display for A2ARedisStreamsError {
    fn fmt(&self, formatter: &mut fmt::Formatter<'_>) -> fmt::Result {
        formatter.write_str(&self.message)
    }
}

impl std::error::Error for A2ARedisStreamsError {}

#[derive(Clone, Debug, PartialEq, Eq)]
pub struct RedisStreamsMessage {
    pub entry_id: String,
    pub correlation_id: String,
    pub payload: JsonValue,
    pub r#final: bool,
}

pub fn parse_redis_entry(
    entry_id: impl Into<String>,
    fields: &RedisFields,
) -> Result<RedisStreamsMessage, A2ARedisStreamsError> {
    let payload_text = decode_field(field_value(fields, "payload"))?;
    let payload = json::parse(&payload_text).map_err(A2ARedisStreamsError::new)?;
    if !matches!(payload, JsonValue::Object(_)) {
        return Err(A2ARedisStreamsError::new(
            "Redis Streams A2A payload must be a JSON object",
        ));
    }

    Ok(RedisStreamsMessage {
        entry_id: entry_id.into(),
        correlation_id: decode_field(field_value(fields, "correlation_id"))?,
        payload,
        r#final: redis_truthy(field_value(fields, "final")),
    })
}

pub fn redis_request_fields(
    correlation_id: &str,
    reply_stream: &str,
    payload: &JsonValue,
) -> RedisFields {
    fields_from([
        ("correlation_id", correlation_id.to_owned()),
        ("reply_stream", reply_stream.to_owned()),
        ("payload", payload.to_compact_json()),
    ])
}

pub fn redis_response_fields(
    correlation_id: &str,
    payload: &JsonValue,
    final_event: bool,
) -> RedisFields {
    fields_from([
        ("correlation_id", correlation_id.to_owned()),
        ("payload", payload.to_compact_json()),
        (
            "final",
            if final_event { "true" } else { "false" }.to_owned(),
        ),
    ])
}

pub fn redis_reply_stream(fields: &RedisFields, default_response_stream: &str) -> String {
    let Some(value) = field_value(fields, "reply_stream") else {
        return default_response_stream.to_owned();
    };
    decode_field(Some(value)).unwrap_or_else(|_| default_response_stream.to_owned())
}

fn fields_from(entries: impl IntoIterator<Item = (&'static str, String)>) -> RedisFields {
    entries
        .into_iter()
        .map(|(key, value)| (key.as_bytes().to_vec(), value.into_bytes()))
        .collect()
}

fn field_value<'a>(fields: &'a RedisFields, name: &str) -> Option<&'a [u8]> {
    fields.get(name.as_bytes()).map(Vec::as_slice)
}

fn decode_field(value: Option<&[u8]>) -> Result<String, A2ARedisStreamsError> {
    let Some(value) = value else {
        return Ok("None".to_owned());
    };
    String::from_utf8(value.to_vec()).map_err(|error| A2ARedisStreamsError::new(error.to_string()))
}

fn redis_truthy(value: Option<&[u8]>) -> bool {
    matches!(value, Some(b"true" | b"1"))
}
