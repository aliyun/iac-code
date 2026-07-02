use std::collections::BTreeMap;

use iac_code_a2a::transport::{
    binding_from_url, ensure_supported_transport, headers_for_auth, normalize_protocol_binding,
    normalize_transport_name, select_binding, validate_transport_for_platform,
    validate_transport_supported, A2AAuthConfig, A2ATransportBinding, A2ATransportConfigError,
    A2ATransportDependencyError, TransportClientOptions, TransportServerOptions,
    TransportStreamEvent,
};
use iac_code_protocol::json;

#[test]
fn normalize_transport_name_accepts_all_runnable_bindings() {
    assert_eq!(normalize_transport_name(Some("HTTP+JSONRPC")), "http");
    assert_eq!(normalize_transport_name(Some("JSONRPC")), "http");
    assert_eq!(normalize_transport_name(Some("stdio")), "stdio");
    assert_eq!(normalize_transport_name(Some("unix")), "unix");
    assert_eq!(normalize_transport_name(Some("websocket")), "websocket");
    assert_eq!(normalize_transport_name(Some("ws")), "websocket");
    assert_eq!(normalize_transport_name(Some("grpcs")), "grpc");
    assert_eq!(
        normalize_transport_name(Some("grpc+jsonrpc")),
        "grpc-jsonrpc"
    );
    assert_eq!(normalize_transport_name(Some("redis")), "redis-streams");
    assert_eq!(normalize_protocol_binding(Some("https")), "jsonrpc");
}

#[test]
fn binding_from_url_derives_transport_name() {
    assert_eq!(
        binding_from_url("https://127.0.0.1:41242/", None).transport(),
        "http"
    );
    assert_eq!(
        binding_from_url("stdio://iac-code", None).transport(),
        "stdio"
    );
    assert_eq!(
        binding_from_url("unix:///tmp/iac-code.sock", None).transport(),
        "unix"
    );
    assert_eq!(
        binding_from_url("wss://agent.example/a2a", None).transport(),
        "websocket"
    );
    assert_eq!(
        binding_from_url("grpc://127.0.0.1:50051", None).transport(),
        "grpc"
    );
    assert_eq!(
        binding_from_url("grpc-jsonrpc://127.0.0.1:50052", None).transport(),
        "grpc-jsonrpc"
    );
    assert_eq!(
        binding_from_url("redis-streams://localhost/0/iac-code", None).transport(),
        "redis-streams"
    );
}

#[test]
fn select_binding_prefers_first_supported_binding_and_fails_when_none() {
    let selected = select_binding(&[
        A2ATransportBinding::new("nats://broker/iac-code", "nats", None),
        A2ATransportBinding::new("unix:///tmp/iac-code.sock", "unix", None),
    ])
    .expect("selected");

    assert_eq!(selected.url, "unix:///tmp/iac-code.sock");
    assert_eq!(selected.protocol_binding, "unix");

    let error = select_binding(&[A2ATransportBinding::new(
        "nats://broker/iac-code",
        "nats",
        None,
    )])
    .unwrap_err()
    .to_string();
    assert!(error.contains("No runnable A2A transport"));
}

#[test]
fn ensure_supported_transport_accepts_non_http_runtimes() {
    let binding = A2ATransportBinding::new("ws://127.0.0.1:41243/a2a", "websocket", None);

    assert_eq!(
        ensure_supported_transport(binding.clone()).unwrap(),
        binding
    );
}

#[test]
fn headers_for_auth_prefers_bearer_or_basic_and_adds_api_key() {
    assert_eq!(
        headers_for_auth(Some(&A2AAuthConfig {
            bearer_token: Some("token".to_owned()),
            basic_username: Some("iac".to_owned()),
            basic_password: Some("secret".to_owned()),
            api_key: Some("api-secret".to_owned()),
            api_key_header: "X-Custom-Key".to_owned(),
        })),
        BTreeMap::from([
            ("Authorization".to_owned(), "Bearer token".to_owned()),
            ("X-Custom-Key".to_owned(), "api-secret".to_owned()),
        ])
    );
    assert_eq!(
        headers_for_auth(Some(&A2AAuthConfig {
            bearer_token: None,
            basic_username: Some("iac".to_owned()),
            basic_password: Some("secret".to_owned()),
            api_key: None,
            api_key_header: "X-API-Key".to_owned(),
        }))
        .get("Authorization")
        .unwrap(),
        "Basic aWFjOnNlY3JldA=="
    );
}

#[test]
fn validate_transport_reports_supported_values_and_platform_errors() {
    validate_transport_supported("grpc-jsonrpc").unwrap();
    validate_transport_for_platform("unix", "linux").unwrap();
    validate_transport_for_platform("http", "win32").unwrap();

    let error = validate_transport_supported("invalid-transport")
        .unwrap_err()
        .to_string();
    assert_eq!(
        error,
        "Unsupported transport 'invalid-transport'. Supported values: grpc, grpc-jsonrpc, http, redis-streams, stdio, unix, websocket"
    );
    assert!(validate_transport_for_platform("unix", "win32")
        .unwrap_err()
        .to_string()
        .contains("Unix domain socket transport is not supported on Windows"));
}

#[test]
fn transport_server_options_match_python_defaults() {
    let options = TransportServerOptions::new("stdio", "qwen-max");

    assert_eq!(options.transport, "stdio");
    assert_eq!(options.model, "qwen-max");
    assert_eq!(options.host, "127.0.0.1");
    assert_eq!(options.port, 41242);
    assert_eq!(options.token, None);
    assert_eq!(options.basic_username, None);
    assert_eq!(options.basic_password, None);
    assert_eq!(options.api_key, None);
    assert_eq!(options.api_key_header, "X-API-Key");
    assert_eq!(options.persistence_dir, None);
    assert_eq!(options.artifact_dir, None);
    assert_eq!(options.signing_secret, None);
    assert_eq!(options.signing_key_id, "default");
    assert!(!options.push_notifications);
    assert_eq!(options.socket_path, None);
    assert_eq!(options.ws_path, "/a2a");
    assert_eq!(options.grpc_host, None);
    assert_eq!(options.grpc_port, None);
    assert_eq!(options.redis_url, None);
    assert_eq!(options.request_stream, "iac-code:a2a:requests");
    assert_eq!(options.response_stream, "iac-code:a2a:responses");
    assert_eq!(options.consumer_group, "iac-code");
}

#[test]
fn transport_client_options_match_python_defaults() {
    let binding = binding_from_url("redis-streams://localhost/0/iac-code", Some("1.0"));
    let options = TransportClientOptions::new(binding.clone());

    assert_eq!(options.binding, binding);
    assert_eq!(options.token, None);
    assert_eq!(options.basic_username, None);
    assert_eq!(options.basic_password, None);
    assert_eq!(options.api_key, None);
    assert_eq!(options.api_key_header, "X-API-Key");
    assert_eq!(options.command, None);
    assert_eq!(options.redis_url, None);
    assert_eq!(options.request_stream, "iac-code:a2a:requests");
    assert_eq!(options.response_stream, "iac-code:a2a:responses");
    assert_eq!(options.timeout_seconds, 30.0);
}

#[test]
fn transport_stream_event_keeps_request_id_payload_and_final_flag() {
    let event = TransportStreamEvent::new(
        Some(json::number(7)),
        json::object([("result", json::object([("final", json::bool_value(true))]))]),
        true,
    );

    assert_eq!(event.request_id, Some(json::number(7)));
    assert_eq!(
        event.payload.to_compact_json(),
        r#"{"result":{"final":true}}"#
    );
    assert!(event.r#final);
}

#[test]
fn transport_error_types_preserve_python_error_messages() {
    assert_eq!(
        A2ATransportConfigError::new("missing socket path").to_string(),
        "missing socket path"
    );
    assert_eq!(
        A2ATransportDependencyError::new("optional dependency is not installed").to_string(),
        "optional dependency is not installed"
    );
}
