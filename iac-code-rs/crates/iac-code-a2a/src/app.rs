use std::collections::BTreeMap;

use base64::engine::general_purpose::STANDARD;
use base64::Engine;
use iac_code_protocol::json::{self, JsonValue};
use ring::digest;

use crate::agent_card::AgentInterfaceConfig;

const V03_JSONRPC_METHODS: &[&str] = &[
    "message/send",
    "message/stream",
    "tasks/get",
    "tasks/cancel",
    "tasks/pushNotificationConfig/set",
    "tasks/pushNotificationConfig/get",
    "tasks/pushNotificationConfig/list",
    "tasks/pushNotificationConfig/delete",
    "tasks/resubscribe",
    "agent/getAuthenticatedExtendedCard",
];

#[derive(Clone, Debug, PartialEq, Eq)]
pub struct A2AAuthConfig {
    pub token: Option<String>,
    pub basic_username: Option<String>,
    pub basic_password: Option<String>,
    pub api_key: Option<String>,
    pub api_key_header: String,
}

impl A2AAuthConfig {
    pub fn new(
        token: Option<&str>,
        basic_username: Option<&str>,
        basic_password: Option<&str>,
        api_key: Option<&str>,
        api_key_header: &str,
    ) -> Self {
        Self {
            token: token.map(ToOwned::to_owned),
            basic_username: basic_username.map(ToOwned::to_owned),
            basic_password: basic_password.map(ToOwned::to_owned),
            api_key: api_key.map(ToOwned::to_owned),
            api_key_header: api_key_header.to_owned(),
        }
    }

    pub fn auth_enabled(&self) -> bool {
        self.token.is_some()
            || (self.basic_username.is_some() && self.basic_password.is_some())
            || self.api_key.is_some()
    }

    pub fn authorized_principal(&self, headers: &BTreeMap<String, String>) -> Option<String> {
        let auth = header(headers, "authorization").unwrap_or_default();
        if let Some(token) = &self.token {
            if let Some(value) = auth.strip_prefix("Bearer ") {
                if secure_eq(value, token) {
                    return Some("bearer".to_owned());
                }
            }
        }

        if self.basic_username.is_some()
            && self.basic_password.is_some()
            && self.valid_basic_auth(auth)
        {
            return Some(format!(
                "basic:{}",
                self.basic_username.as_deref().unwrap_or_default()
            ));
        }

        if let Some(expected) = &self.api_key {
            if let Some(value) = header(headers, &self.api_key_header) {
                if secure_eq(value, expected) {
                    return Some(format!("api-key:{}", self.api_key_header));
                }
            }
        }
        None
    }

    pub fn valid_basic_auth(&self, auth: &str) -> bool {
        let Some(encoded) = auth.strip_prefix("Basic ") else {
            return false;
        };
        let Ok(decoded) = STANDARD.decode(encoded.as_bytes()) else {
            return false;
        };
        let Ok(decoded) = String::from_utf8(decoded) else {
            return false;
        };
        let Some((username, password)) = decoded.split_once(':') else {
            return false;
        };
        if username.is_empty() || password.is_empty() {
            return false;
        }
        secure_eq(username, self.basic_username.as_deref().unwrap_or_default())
            && secure_eq(password, self.basic_password.as_deref().unwrap_or_default())
    }
}

#[derive(Clone, Debug, PartialEq, Eq)]
pub struct SupportedInterfacesOptions {
    pub transport: String,
    pub host: String,
    pub port: u16,
    pub socket_path: Option<String>,
    pub ws_path: String,
    pub grpc_host: Option<String>,
    pub grpc_port: Option<u16>,
    pub redis_url: Option<String>,
    pub request_stream: String,
    pub response_stream: String,
    pub consumer_group: String,
}

#[derive(Clone, Debug, PartialEq, Eq)]
pub struct HttpJsonResponse {
    pub status_code: u16,
    pub headers: BTreeMap<String, String>,
    pub body: JsonValue,
}

impl Default for SupportedInterfacesOptions {
    fn default() -> Self {
        Self {
            transport: "http".to_owned(),
            host: "127.0.0.1".to_owned(),
            port: 41242,
            socket_path: None,
            ws_path: "/a2a".to_owned(),
            grpc_host: None,
            grpc_port: None,
            redis_url: None,
            request_stream: "iac-code:a2a:requests".to_owned(),
            response_stream: "iac-code:a2a:responses".to_owned(),
            consumer_group: "iac-code".to_owned(),
        }
    }
}

pub fn resolve_token(cli_token: Option<&str>) -> Option<String> {
    cli_token
        .map(ToOwned::to_owned)
        .or_else(|| std::env::var("IACCODE_A2A_HTTP_TOKEN").ok())
}

pub fn resolve_basic_credentials(
    cli_username: Option<&str>,
    cli_password: Option<&str>,
) -> Option<(String, String)> {
    let username = cli_username
        .map(ToOwned::to_owned)
        .or_else(|| std::env::var("IACCODE_A2A_BASIC_USERNAME").ok());
    let password = cli_password
        .map(ToOwned::to_owned)
        .or_else(|| std::env::var("IACCODE_A2A_BASIC_PASSWORD").ok());
    match (username, password) {
        (Some(username), Some(password)) if !username.is_empty() && !password.is_empty() => {
            Some((username, password))
        }
        _ => None,
    }
}

pub fn resolve_api_key(cli_api_key: Option<&str>) -> Option<String> {
    cli_api_key
        .map(ToOwned::to_owned)
        .or_else(|| std::env::var("IACCODE_A2A_API_KEY").ok())
}

pub fn resolve_api_key_header(cli_api_key_header: Option<&str>) -> String {
    cli_api_key_header
        .map(ToOwned::to_owned)
        .or_else(|| std::env::var("IACCODE_A2A_API_KEY_HEADER").ok())
        .unwrap_or_else(|| "X-API-Key".to_owned())
}

pub fn agent_card_etag(card: &JsonValue) -> String {
    let digest = digest::digest(&digest::SHA256, card.to_compact_json().as_bytes());
    format!("\"sha256-{}\"", hex_lower(digest.as_ref()))
}

pub fn health_response() -> HttpJsonResponse {
    HttpJsonResponse {
        status_code: 200,
        headers: BTreeMap::new(),
        body: json::object([("status", json::string("healthy"))]),
    }
}

pub fn agent_card_response(
    card: &JsonValue,
    if_none_match: Option<&str>,
    last_modified: &str,
) -> HttpJsonResponse {
    let etag = agent_card_etag(card);
    let headers = BTreeMap::from([
        ("Cache-Control".to_owned(), "public, max-age=60".to_owned()),
        ("ETag".to_owned(), etag.clone()),
        ("Last-Modified".to_owned(), last_modified.to_owned()),
    ]);
    if if_none_match == Some(etag.as_str()) {
        HttpJsonResponse {
            status_code: 304,
            headers,
            body: JsonValue::Null,
        }
    } else {
        HttpJsonResponse {
            status_code: 200,
            headers,
            body: card.clone(),
        }
    }
}

pub fn is_v03_jsonrpc_method(method: &str) -> bool {
    V03_JSONRPC_METHODS.contains(&method)
}

pub fn supported_interfaces(
    options: SupportedInterfacesOptions,
) -> Option<Vec<AgentInterfaceConfig>> {
    match options.transport.as_str() {
        "http" => Some(vec![
            AgentInterfaceConfig::new(
                format!("http://{}:{}/", options.host, options.port),
                "JSONRPC",
                "1.0",
            ),
            AgentInterfaceConfig::new(
                format!("http://{}:{}", options.host, options.port),
                "HTTP+JSON",
                "1.0",
            ),
        ]),
        "stdio" => Some(vec![AgentInterfaceConfig::new(
            "stdio://iac-code",
            "stdio",
            "1.0",
        )]),
        "unix" => options.socket_path.map(|socket_path| {
            vec![AgentInterfaceConfig::new(
                format!("unix://{socket_path}"),
                "unix",
                "1.0",
            )]
        }),
        "websocket" => Some(vec![AgentInterfaceConfig::new(
            format!("ws://{}:{}{}", options.host, options.port, options.ws_path),
            "websocket",
            "1.0",
        )]),
        "grpc" => Some(vec![AgentInterfaceConfig::new(
            format!(
                "grpc://{}:{}",
                options.grpc_host.as_deref().unwrap_or(&options.host),
                options.grpc_port.unwrap_or(options.port)
            ),
            "grpc",
            "1.0",
        )]),
        "grpc-jsonrpc" => Some(vec![AgentInterfaceConfig::new(
            format!(
                "grpc-jsonrpc://{}:{}",
                options.grpc_host.as_deref().unwrap_or(&options.host),
                options.grpc_port.unwrap_or(options.port)
            ),
            "grpc-jsonrpc",
            "1.0",
        )]),
        "redis-streams" => options.redis_url.map(|redis_url| {
            vec![AgentInterfaceConfig::new(
                format!(
                    "redis-streams://{}/{}/{}/{}",
                    redis_url,
                    options.request_stream,
                    options.response_stream,
                    options.consumer_group
                ),
                "redis-streams",
                "1.0",
            )]
        }),
        _ => None,
    }
}

fn header<'a>(headers: &'a BTreeMap<String, String>, name: &str) -> Option<&'a str> {
    headers
        .iter()
        .find(|(key, _)| key.eq_ignore_ascii_case(name))
        .map(|(_, value)| value.as_str())
}

fn secure_eq(left: &str, right: &str) -> bool {
    let left = left.as_bytes();
    let right = right.as_bytes();
    if left.len() != right.len() {
        return false;
    }
    let mut diff = 0_u8;
    for (left, right) in left.iter().zip(right) {
        diff |= left ^ right;
    }
    diff == 0
}

fn hex_lower(bytes: &[u8]) -> String {
    let mut output = String::with_capacity(bytes.len() * 2);
    for byte in bytes {
        use std::fmt::Write;
        write!(output, "{byte:02x}").expect("writing to String should not fail");
    }
    output
}
