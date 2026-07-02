use std::time::{SystemTime, UNIX_EPOCH};

use iac_code_protocol::json::JsonValue;

use crate::transport::{binding_from_url, A2AAuthConfig};

mod discovery;
pub use discovery::merge_jwks;
mod endpoint;
mod http;
mod payload;
pub use payload::PushConfigRequest;
mod response;
pub use response::{extract_response_text, A2AClientResponse};
mod unix;
mod websocket;

use discovery::discover_agent_card_with_client;
use http::{blocking_http_client, post_json, post_sse_json};
use unix::{send_unix_jsonrpc_payload, stream_unix_jsonrpc_payload};
use websocket::{send_websocket_jsonrpc_payload, stream_websocket_jsonrpc_payload};

#[derive(Clone, Debug, PartialEq, Eq)]
pub struct A2AClient;

#[derive(Clone, Debug, Default, PartialEq)]
pub struct A2ADiscoverOptions<'a> {
    pub base_url: &'a str,
    pub auth: Option<A2AAuthConfig>,
    pub verification_secret: Option<String>,
    pub verification_jwks_url: Option<String>,
    pub require_card_signature: bool,
    pub timeout_seconds: Option<f64>,
}

#[derive(Clone, Debug, Default, PartialEq)]
pub struct A2ACallOptions<'a> {
    pub base_url: &'a str,
    pub prompt: &'a str,
    pub cwd: &'a str,
    pub context_id: Option<&'a str>,
    pub model: Option<&'a str>,
    pub auth: Option<A2AAuthConfig>,
    pub verification_secret: Option<String>,
    pub verification_jwks_url: Option<String>,
    pub require_card_signature: bool,
    pub timeout_seconds: Option<f64>,
}

pub fn discover_agent_card(options: A2ADiscoverOptions<'_>) -> Result<JsonValue, String> {
    let client = blocking_http_client(options.timeout_seconds)?;
    discover_agent_card_with_client(&client, &options)
}

pub fn call_agent(options: A2ACallOptions<'_>) -> Result<A2AClientResponse, String> {
    let client = blocking_http_client(options.timeout_seconds)?;
    let card = discover_agent_card_with_client(
        &client,
        &A2ADiscoverOptions {
            base_url: options.base_url,
            auth: options.auth.clone(),
            verification_secret: options.verification_secret.clone(),
            verification_jwks_url: options.verification_jwks_url.clone(),
            require_card_signature: options.require_card_signature,
            timeout_seconds: options.timeout_seconds,
        },
    )?;
    let endpoint_url = A2AClient::select_endpoint_url(&card, options.base_url);
    let payload = A2AClient::message_payload(
        "SendMessage",
        options.prompt,
        options.cwd,
        options.context_id,
        options.model,
        &new_client_id(),
        &new_client_id(),
    );
    let payload = send_jsonrpc_payload(
        &endpoint_url,
        &payload,
        options.auth.clone(),
        options.timeout_seconds,
    )?;
    Ok(A2AClientResponse { payload })
}

pub fn stream_agent(options: A2ACallOptions<'_>) -> Result<Vec<JsonValue>, String> {
    let client = blocking_http_client(options.timeout_seconds)?;
    let card = discover_agent_card_with_client(
        &client,
        &A2ADiscoverOptions {
            base_url: options.base_url,
            auth: options.auth.clone(),
            verification_secret: options.verification_secret.clone(),
            verification_jwks_url: options.verification_jwks_url.clone(),
            require_card_signature: options.require_card_signature,
            timeout_seconds: options.timeout_seconds,
        },
    )?;
    let endpoint_url = A2AClient::select_endpoint_url(&card, options.base_url);
    let payload = A2AClient::message_payload(
        "SendStreamingMessage",
        options.prompt,
        options.cwd,
        options.context_id,
        options.model,
        &new_client_id(),
        &new_client_id(),
    );
    stream_jsonrpc_payload(
        &endpoint_url,
        &payload,
        options.auth.clone(),
        options.timeout_seconds,
    )
}

pub fn send_jsonrpc_payload(
    url: &str,
    payload: &JsonValue,
    auth: Option<A2AAuthConfig>,
    timeout_seconds: Option<f64>,
) -> Result<JsonValue, String> {
    match binding_from_url(url, None).transport().as_str() {
        "unix" => return send_unix_jsonrpc_payload(url, payload),
        "websocket" => {
            return send_websocket_jsonrpc_payload(url, payload, auth.as_ref(), timeout_seconds);
        }
        _ => {}
    }
    let client = blocking_http_client(timeout_seconds)?;
    post_json(&client, url, payload, auth.as_ref())
}

pub fn stream_jsonrpc_payload(
    url: &str,
    payload: &JsonValue,
    auth: Option<A2AAuthConfig>,
    timeout_seconds: Option<f64>,
) -> Result<Vec<JsonValue>, String> {
    match binding_from_url(url, None).transport().as_str() {
        "unix" => return stream_unix_jsonrpc_payload(url, payload),
        "websocket" => {
            return stream_websocket_jsonrpc_payload(url, payload, auth.as_ref(), timeout_seconds);
        }
        _ => {}
    }
    let client = blocking_http_client(timeout_seconds)?;
    post_sse_json(&client, url, payload, auth.as_ref())
}

fn new_client_id() -> String {
    let nanos = SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .unwrap_or_default()
        .as_nanos();
    format!("{nanos:x}")
}

pub(crate) fn object_field<'a>(value: &'a JsonValue, key: &str) -> Option<&'a JsonValue> {
    let JsonValue::Object(object) = value else {
        return None;
    };
    object.get(key)
}

pub(crate) fn string_field<'a>(value: &'a JsonValue, key: &str) -> Option<&'a str> {
    match object_field(value, key) {
        Some(JsonValue::String(value)) => Some(value.as_str()),
        _ => None,
    }
}

pub(crate) fn json_bool_field(value: &JsonValue, key: &str) -> Option<bool> {
    match object_field(value, key) {
        Some(JsonValue::Bool(value)) => Some(*value),
        _ => None,
    }
}
