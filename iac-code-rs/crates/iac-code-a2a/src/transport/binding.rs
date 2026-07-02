use super::{A2ATransportBinding, UnsupportedA2ATransportError};

const SUPPORTED_TRANSPORTS: &[&str] = &[
    "grpc",
    "grpc-jsonrpc",
    "http",
    "redis-streams",
    "stdio",
    "unix",
    "websocket",
];

pub fn normalize_transport_name(value: Option<&str>) -> String {
    let normalized = value
        .unwrap_or("http")
        .trim()
        .to_ascii_lowercase()
        .replace('_', "-");
    match normalized.as_str() {
        "jsonrpc" | "json-rpc" | "http+jsonrpc" | "http-jsonrpc" | "https" => "http".to_owned(),
        "grpcs" => "grpc".to_owned(),
        "grpc+jsonrpc" => "grpc-jsonrpc".to_owned(),
        "ws" | "wss" => "websocket".to_owned(),
        "redis" | "redis-stream" => "redis-streams".to_owned(),
        _ => normalized,
    }
}

pub fn normalize_protocol_binding(value: Option<&str>) -> String {
    let normalized = normalize_transport_name(value);
    if normalized == "http" {
        "jsonrpc".to_owned()
    } else {
        normalized
    }
}

pub fn binding_from_url(url: &str, protocol_version: Option<&str>) -> A2ATransportBinding {
    let scheme = url.split_once("://").map_or("http", |(scheme, _)| scheme);
    let transport = normalize_transport_name(Some(scheme));
    A2ATransportBinding::new(url, transport, protocol_version)
}

pub fn is_runnable_binding(binding: &A2ATransportBinding) -> bool {
    match normalize_transport_name(Some(&binding.protocol_binding)).as_str() {
        "http" => binding.url.starts_with("http://") || binding.url.starts_with("https://"),
        "websocket" => binding.url.starts_with("ws://") || binding.url.starts_with("wss://"),
        "grpc" => binding.url.starts_with("grpc://") || binding.url.starts_with("grpcs://"),
        "grpc-jsonrpc" => {
            binding.url.starts_with("grpc-jsonrpc://") || binding.url.starts_with("grpc+jsonrpc://")
        }
        "redis-streams" => binding.url.starts_with("redis-streams://"),
        "unix" => binding.url.starts_with("unix://"),
        "stdio" => binding.url.starts_with("stdio://"),
        _ => false,
    }
}

pub fn select_binding(
    bindings: &[A2ATransportBinding],
) -> Result<A2ATransportBinding, UnsupportedA2ATransportError> {
    for binding in bindings {
        if is_runnable_binding(binding) {
            return Ok(A2ATransportBinding {
                url: binding.url.clone(),
                protocol_binding: normalize_transport_name(Some(&binding.protocol_binding)),
                protocol_version: binding.protocol_version.clone(),
            });
        }
    }
    let names = if bindings.is_empty() {
        "none".to_owned()
    } else {
        bindings
            .iter()
            .map(|binding| binding.protocol_binding.as_str())
            .collect::<Vec<_>>()
            .join(", ")
    };
    Err(UnsupportedA2ATransportError::new(format!(
        "No runnable A2A transport found. Candidate bindings: {names}"
    )))
}

pub fn ensure_supported_transport(
    binding: A2ATransportBinding,
) -> Result<A2ATransportBinding, UnsupportedA2ATransportError> {
    if is_runnable_binding(&binding) {
        Ok(binding)
    } else {
        Err(UnsupportedA2ATransportError::new(format!(
            "A2A protocol binding {:?} at {:?} is not runnable.",
            binding.protocol_binding, binding.url
        )))
    }
}

pub fn validate_transport_supported(transport: &str) -> Result<(), UnsupportedA2ATransportError> {
    if SUPPORTED_TRANSPORTS.contains(&transport) {
        Ok(())
    } else {
        Err(UnsupportedA2ATransportError::new(format!(
            "Unsupported transport '{transport}'. Supported values: {}",
            SUPPORTED_TRANSPORTS.join(", ")
        )))
    }
}

pub fn validate_transport_for_platform(
    transport: &str,
    platform: &str,
) -> Result<(), UnsupportedA2ATransportError> {
    validate_transport_supported(transport)?;
    if transport == "unix" && platform == "win32" {
        Err(UnsupportedA2ATransportError::new(
            "Unix domain socket transport is not supported on Windows. Use --transport http or --transport stdio instead.",
        ))
    } else {
        Ok(())
    }
}
