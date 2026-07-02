use std::collections::BTreeSet;
use std::fmt;

use iac_code_protocol::json::JsonValue;

use crate::transport::{normalize_transport_name, validate_transport_for_platform};

#[derive(Clone, Debug, PartialEq, Eq)]
pub struct A2AExtensionSupportError {
    message: String,
}

impl A2AExtensionSupportError {
    fn new(message: impl Into<String>) -> Self {
        Self {
            message: message.into(),
        }
    }
}

impl fmt::Display for A2AExtensionSupportError {
    fn fmt(&self, formatter: &mut fmt::Formatter<'_>) -> fmt::Result {
        formatter.write_str(&self.message)
    }
}

impl std::error::Error for A2AExtensionSupportError {}

#[derive(Clone, Debug, PartialEq, Eq)]
pub struct A2AServerStartupError {
    message: String,
}

impl A2AServerStartupError {
    fn new(message: impl Into<String>) -> Self {
        Self {
            message: message.into(),
        }
    }
}

impl fmt::Display for A2AServerStartupError {
    fn fmt(&self, formatter: &mut fmt::Formatter<'_>) -> fmt::Result {
        formatter.write_str(&self.message)
    }
}

impl std::error::Error for A2AServerStartupError {}

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub struct ServerStartupOptions<'a> {
    pub transport: &'a str,
    pub socket_path: Option<&'a str>,
    pub redis_url: Option<&'a str>,
    pub push_queue: &'a str,
    pub push_redis_url: Option<&'a str>,
    pub platform: &'a str,
}

impl<'a> ServerStartupOptions<'a> {
    pub fn new(transport: &'a str, platform: &'a str) -> Self {
        Self {
            transport,
            socket_path: None,
            redis_url: None,
            push_queue: "local-file",
            push_redis_url: None,
            platform,
        }
    }
}

pub fn validate_server_startup_options(
    options: ServerStartupOptions<'_>,
) -> Result<String, A2AServerStartupError> {
    let normalized_transport = normalize_transport_name(Some(options.transport));
    if normalized_transport == "unix" && blank(options.socket_path) {
        return Err(A2AServerStartupError::new(
            "--socket-path is required for --transport unix.",
        ));
    }
    if normalized_transport == "redis-streams" && blank(options.redis_url) {
        return Err(A2AServerStartupError::new(
            "--redis-url is required for --transport redis-streams.",
        ));
    }
    if options.push_queue == "redis-streams" && blank(options.push_redis_url) {
        return Err(A2AServerStartupError::new(
            "--push-redis-url is required for --push-queue redis-streams.",
        ));
    }
    validate_transport_for_platform(&normalized_transport, options.platform)
        .map_err(|error| A2AServerStartupError::new(error.to_string()))?;
    Ok(normalized_transport)
}

pub fn required_extension_uris(card: &JsonValue) -> Vec<String> {
    let Some(JsonValue::Array(extensions)) = object_path(card, &["capabilities", "extensions"])
    else {
        return Vec::new();
    };
    let mut uris = extensions
        .iter()
        .filter(|extension| bool_field(extension, "required") == Some(true))
        .filter_map(|extension| string_field(extension, "uri").map(ToOwned::to_owned))
        .collect::<Vec<_>>();
    uris.sort();
    uris
}

pub fn missing_required_extensions<I, S>(card: &JsonValue, requested: I) -> Vec<String>
where
    I: IntoIterator<Item = S>,
    S: AsRef<str>,
{
    let requested = requested
        .into_iter()
        .map(|value| value.as_ref().to_owned())
        .collect::<BTreeSet<_>>();
    required_extension_uris(card)
        .into_iter()
        .filter(|uri| !requested.contains(uri))
        .collect()
}

pub fn validate_required_extensions<I, S>(
    card: &JsonValue,
    requested: I,
) -> Result<(), A2AExtensionSupportError>
where
    I: IntoIterator<Item = S>,
    S: AsRef<str>,
{
    let missing = missing_required_extensions(card, requested);
    if missing.is_empty() {
        Ok(())
    } else {
        Err(A2AExtensionSupportError::new(format!(
            "Required A2A extensions were not requested: {}",
            missing.join(", ")
        )))
    }
}

fn blank(value: Option<&str>) -> bool {
    value.is_none_or(str::is_empty)
}

fn object_path<'a>(mut value: &'a JsonValue, path: &[&str]) -> Option<&'a JsonValue> {
    for segment in path {
        value = object_field(value, segment)?;
    }
    Some(value)
}

fn object_field<'a>(value: &'a JsonValue, key: &str) -> Option<&'a JsonValue> {
    let JsonValue::Object(object) = value else {
        return None;
    };
    object.get(key)
}

fn string_field<'a>(value: &'a JsonValue, key: &str) -> Option<&'a str> {
    match object_field(value, key) {
        Some(JsonValue::String(value)) => Some(value.as_str()),
        _ => None,
    }
}

fn bool_field(value: &JsonValue, key: &str) -> Option<bool> {
    match object_field(value, key) {
        Some(JsonValue::Bool(value)) => Some(*value),
        _ => None,
    }
}
