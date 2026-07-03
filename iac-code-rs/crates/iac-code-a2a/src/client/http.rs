use std::time::Duration;

use iac_code_protocol::json::{self, JsonValue};

use crate::transport::{headers_for_auth, A2AAuthConfig};
use crate::transports::http::decode_sse_data_line;

pub(super) fn blocking_http_client(
    timeout_seconds: Option<f64>,
) -> Result<reqwest::blocking::Client, String> {
    let mut builder = reqwest::blocking::Client::builder();
    if let Some(timeout) = timeout_seconds {
        builder = builder.timeout(Duration::from_secs_f64(timeout.max(0.0)));
    }
    builder.build().map_err(|error| error.to_string())
}

pub(super) fn fetch_json(
    client: &reqwest::blocking::Client,
    url: &str,
    auth: Option<&A2AAuthConfig>,
) -> Result<JsonValue, String> {
    let mut request = client.get(url);
    for (key, value) in headers_for_auth(auth) {
        request = request.header(key, value);
    }
    let response = request
        .send()
        .and_then(|response| response.error_for_status())
        .map_err(|error| error.to_string())?;
    let text = response.text().map_err(|error| error.to_string())?;
    json::parse(&text)
}

pub(super) fn post_json(
    client: &reqwest::blocking::Client,
    url: &str,
    payload: &JsonValue,
    auth: Option<&A2AAuthConfig>,
) -> Result<JsonValue, String> {
    let mut request = client
        .post(url)
        .header("A2A-Version", "1.0")
        .header("Content-Type", "application/json")
        .body(payload.to_compact_json());
    for (key, value) in headers_for_auth(auth) {
        request = request.header(key, value);
    }
    let response = request
        .send()
        .and_then(|response| response.error_for_status())
        .map_err(|error| error.to_string())?;
    let text = response.text().map_err(|error| error.to_string())?;
    let payload = json::parse(&text)?;
    if !matches!(payload, JsonValue::Object(_)) {
        return Err("A2A HTTP response must be a JSON object".to_owned());
    }
    Ok(payload)
}

pub(super) fn post_sse_json(
    client: &reqwest::blocking::Client,
    url: &str,
    payload: &JsonValue,
    auth: Option<&A2AAuthConfig>,
) -> Result<Vec<JsonValue>, String> {
    let mut request = client
        .post(url)
        .header("A2A-Version", "1.0")
        .header("Content-Type", "application/json")
        .body(payload.to_compact_json());
    for (key, value) in headers_for_auth(auth) {
        request = request.header(key, value);
    }
    let response = request
        .send()
        .and_then(|response| response.error_for_status())
        .map_err(|error| error.to_string())?;
    let text = response.text().map_err(|error| error.to_string())?;
    let mut events = Vec::new();
    for line in text.lines() {
        if let Some(event) = decode_sse_data_line(line).map_err(|error| error.to_string())? {
            events.push(event);
        }
    }
    Ok(events)
}
