use std::fmt;

use iac_code_protocol::json::JsonValue;

mod auth;
mod binding;

pub use auth::headers_for_auth;
pub use binding::{
    binding_from_url, ensure_supported_transport, is_runnable_binding, normalize_protocol_binding,
    normalize_transport_name, select_binding, validate_transport_for_platform,
    validate_transport_supported,
};

#[derive(Clone, Debug, PartialEq, Eq)]
pub struct UnsupportedA2ATransportError {
    message: String,
}

impl UnsupportedA2ATransportError {
    fn new(message: impl Into<String>) -> Self {
        Self {
            message: message.into(),
        }
    }
}

impl fmt::Display for UnsupportedA2ATransportError {
    fn fmt(&self, formatter: &mut fmt::Formatter<'_>) -> fmt::Result {
        formatter.write_str(&self.message)
    }
}

impl std::error::Error for UnsupportedA2ATransportError {}

#[derive(Clone, Debug, PartialEq, Eq)]
pub struct A2ATransportConfigError {
    message: String,
}

impl A2ATransportConfigError {
    pub fn new(message: impl Into<String>) -> Self {
        Self {
            message: message.into(),
        }
    }
}

impl fmt::Display for A2ATransportConfigError {
    fn fmt(&self, formatter: &mut fmt::Formatter<'_>) -> fmt::Result {
        formatter.write_str(&self.message)
    }
}

impl std::error::Error for A2ATransportConfigError {}

#[derive(Clone, Debug, PartialEq, Eq)]
pub struct A2ATransportDependencyError {
    message: String,
}

impl A2ATransportDependencyError {
    pub fn new(message: impl Into<String>) -> Self {
        Self {
            message: message.into(),
        }
    }
}

impl fmt::Display for A2ATransportDependencyError {
    fn fmt(&self, formatter: &mut fmt::Formatter<'_>) -> fmt::Result {
        formatter.write_str(&self.message)
    }
}

impl std::error::Error for A2ATransportDependencyError {}

#[derive(Clone, Debug, PartialEq, Eq)]
pub struct A2ATransportBinding {
    pub url: String,
    pub protocol_binding: String,
    pub protocol_version: Option<String>,
}

impl A2ATransportBinding {
    pub fn new(
        url: impl Into<String>,
        protocol_binding: impl Into<String>,
        protocol_version: Option<&str>,
    ) -> Self {
        Self {
            url: url.into(),
            protocol_binding: protocol_binding.into(),
            protocol_version: protocol_version.map(ToOwned::to_owned),
        }
    }

    pub fn transport(&self) -> String {
        binding::normalize_transport_name(Some(&self.protocol_binding))
    }
}

#[derive(Clone, Debug, Default, PartialEq, Eq)]
pub struct A2AAuthConfig {
    pub bearer_token: Option<String>,
    pub api_key: Option<String>,
    pub api_key_header: String,
    pub basic_username: Option<String>,
    pub basic_password: Option<String>,
}

#[derive(Clone, Debug, PartialEq, Eq)]
pub struct TransportStreamEvent {
    pub request_id: Option<JsonValue>,
    pub payload: JsonValue,
    pub r#final: bool,
}

impl TransportStreamEvent {
    pub fn new(request_id: Option<JsonValue>, payload: JsonValue, final_event: bool) -> Self {
        Self {
            request_id,
            payload,
            r#final: final_event,
        }
    }
}

#[derive(Clone, Debug, PartialEq, Eq)]
pub struct TransportServerOptions {
    pub transport: String,
    pub model: String,
    pub host: String,
    pub port: u16,
    pub token: Option<String>,
    pub basic_username: Option<String>,
    pub basic_password: Option<String>,
    pub api_key: Option<String>,
    pub api_key_header: String,
    pub persistence_dir: Option<String>,
    pub artifact_dir: Option<String>,
    pub signing_secret: Option<String>,
    pub signing_key_id: String,
    pub push_notifications: bool,
    pub socket_path: Option<String>,
    pub ws_path: String,
    pub grpc_host: Option<String>,
    pub grpc_port: Option<u16>,
    pub redis_url: Option<String>,
    pub request_stream: String,
    pub response_stream: String,
    pub consumer_group: String,
}

impl TransportServerOptions {
    pub fn new(transport: impl Into<String>, model: impl Into<String>) -> Self {
        Self {
            transport: transport.into(),
            model: model.into(),
            host: "127.0.0.1".to_owned(),
            port: 41242,
            token: None,
            basic_username: None,
            basic_password: None,
            api_key: None,
            api_key_header: "X-API-Key".to_owned(),
            persistence_dir: None,
            artifact_dir: None,
            signing_secret: None,
            signing_key_id: "default".to_owned(),
            push_notifications: false,
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

#[derive(Clone, Debug, PartialEq)]
pub struct TransportClientOptions {
    pub binding: A2ATransportBinding,
    pub token: Option<String>,
    pub basic_username: Option<String>,
    pub basic_password: Option<String>,
    pub api_key: Option<String>,
    pub api_key_header: String,
    pub command: Option<Vec<String>>,
    pub redis_url: Option<String>,
    pub request_stream: String,
    pub response_stream: String,
    pub timeout_seconds: f64,
}

impl TransportClientOptions {
    pub fn new(binding: A2ATransportBinding) -> Self {
        Self {
            binding,
            token: None,
            basic_username: None,
            basic_password: None,
            api_key: None,
            api_key_header: "X-API-Key".to_owned(),
            command: None,
            redis_url: None,
            request_stream: "iac-code:a2a:requests".to_owned(),
            response_stream: "iac-code:a2a:responses".to_owned(),
            timeout_seconds: 30.0,
        }
    }
}
