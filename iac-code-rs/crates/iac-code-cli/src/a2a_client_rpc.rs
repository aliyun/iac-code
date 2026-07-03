use std::collections::BTreeMap;
use std::time::{SystemTime, UNIX_EPOCH};

use iac_code_a2a::client::send_jsonrpc_payload;
use iac_code_a2a::transport::A2AAuthConfig;
use iac_code_protocol::json::JsonValue;

use crate::a2a_client_task_args::A2AClientAuthArgs;
use crate::cli_args::non_empty_string;
use crate::json_utils::format_pretty_json;

pub(super) fn post_a2a_jsonrpc(
    url: &str,
    payload: &JsonValue,
    auth: A2AClientAuthArgs,
) -> Result<String, String> {
    let response = send_a2a_jsonrpc(url, payload, auth)?;
    Ok(format_pretty_json(&response))
}

pub(super) fn send_a2a_jsonrpc(
    url: &str,
    payload: &JsonValue,
    auth: A2AClientAuthArgs,
) -> Result<JsonValue, String> {
    send_jsonrpc_payload(url, payload, Some(auth_config_from_args(auth)), None)
}

pub(super) fn push_callback_authentication(scheme: &str, credentials: &str) -> Option<JsonValue> {
    if scheme.is_empty() && credentials.is_empty() {
        return None;
    }
    let mut authentication = BTreeMap::new();
    authentication.insert("scheme".to_owned(), JsonValue::String(scheme.to_owned()));
    authentication.insert(
        "credentials".to_owned(),
        JsonValue::String(credentials.to_owned()),
    );
    Some(JsonValue::Object(authentication))
}

pub(super) fn auth_config_from_args(args: A2AClientAuthArgs) -> A2AAuthConfig {
    A2AAuthConfig {
        bearer_token: non_empty_string(args.token),
        api_key: non_empty_string(args.api_key),
        api_key_header: args.api_key_header,
        basic_username: non_empty_string(args.basic_username),
        basic_password: non_empty_string(args.basic_password),
    }
}

pub(super) fn new_cli_request_id() -> String {
    let nanos = SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .unwrap_or_default()
        .as_nanos();
    format!("{nanos:x}")
}
