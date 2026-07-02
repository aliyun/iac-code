use std::collections::BTreeMap;
use std::sync::{Mutex, OnceLock};

use base64::engine::general_purpose::STANDARD;
use base64::Engine;
use iac_code_a2a::agent_card::{build_agent_card, AgentCardOptions};
use iac_code_a2a::app::{
    agent_card_etag, agent_card_response, health_response, is_v03_jsonrpc_method, resolve_api_key,
    resolve_api_key_header, resolve_basic_credentials, resolve_token, supported_interfaces,
    A2AAuthConfig, SupportedInterfacesOptions,
};

#[test]
fn app_resolves_auth_config_from_cli_or_environment() {
    let _guard = env_guard();
    std::env::set_var("IACCODE_A2A_HTTP_TOKEN", "env-token");
    std::env::set_var("IACCODE_A2A_BASIC_USERNAME", "env-user");
    std::env::set_var("IACCODE_A2A_BASIC_PASSWORD", "env-pass");
    std::env::set_var("IACCODE_A2A_API_KEY", "env-key");
    std::env::set_var("IACCODE_A2A_API_KEY_HEADER", "X-Env-Key");

    assert_eq!(
        resolve_token(Some("cli-token")),
        Some("cli-token".to_owned())
    );
    assert_eq!(resolve_token(None), Some("env-token".to_owned()));
    assert_eq!(
        resolve_basic_credentials(Some("cli-user"), Some("cli-pass")),
        Some(("cli-user".to_owned(), "cli-pass".to_owned()))
    );
    assert_eq!(
        resolve_basic_credentials(None, None),
        Some(("env-user".to_owned(), "env-pass".to_owned()))
    );
    assert_eq!(resolve_api_key(Some("cli-key")), Some("cli-key".to_owned()));
    assert_eq!(resolve_api_key(None), Some("env-key".to_owned()));
    assert_eq!(
        resolve_api_key_header(Some("X-Cli-Key")),
        "X-Cli-Key".to_owned()
    );
    assert_eq!(resolve_api_key_header(None), "X-Env-Key".to_owned());

    std::env::remove_var("IACCODE_A2A_HTTP_TOKEN");
    std::env::remove_var("IACCODE_A2A_BASIC_USERNAME");
    std::env::remove_var("IACCODE_A2A_BASIC_PASSWORD");
    std::env::remove_var("IACCODE_A2A_API_KEY");
    std::env::remove_var("IACCODE_A2A_API_KEY_HEADER");
}

#[test]
fn app_requires_basic_auth_username_password_pair() {
    let _guard = env_guard();
    std::env::set_var("IACCODE_A2A_BASIC_USERNAME", "env-user");
    std::env::remove_var("IACCODE_A2A_BASIC_PASSWORD");

    assert_eq!(resolve_basic_credentials(None, None), None);

    std::env::remove_var("IACCODE_A2A_BASIC_USERNAME");
}

#[test]
fn auth_config_allows_any_configured_scheme_and_rejects_bad_values() {
    let config = A2AAuthConfig::new(
        Some("bearer-secret"),
        Some("iac"),
        Some("basic-secret"),
        Some("api-secret"),
        "X-Custom-Key",
    );

    assert_eq!(
        config.authorized_principal(&headers([("Authorization", "Bearer bearer-secret")])),
        Some("bearer".to_owned())
    );
    assert_eq!(
        config.authorized_principal(&headers([(
            "Authorization",
            &format!("Basic {}", STANDARD.encode("iac:basic-secret")),
        )])),
        Some("basic:iac".to_owned())
    );
    assert_eq!(
        config.authorized_principal(&headers([("X-Custom-Key", "api-secret")])),
        Some("api-key:X-Custom-Key".to_owned())
    );
    assert_eq!(
        config.authorized_principal(&headers([("Authorization", "Bearer wrong")])),
        None
    );
    assert_eq!(
        config.authorized_principal(&headers([(
            "Authorization",
            &format!("Basic {}", STANDARD.encode(":basic-secret")),
        )])),
        None
    );
    assert!(config.auth_enabled());
}

#[test]
fn app_agent_card_etag_is_stable_sha256_header_value() {
    let card = build_agent_card(AgentCardOptions::new("127.0.0.1", 41242, false));
    let etag = agent_card_etag(&card);

    assert!(etag.starts_with("\"sha256-"));
    assert!(etag.ends_with('"'));
    assert_eq!(etag, agent_card_etag(&card));
}

#[test]
fn app_health_response_matches_python_shape() {
    let response = health_response();

    assert_eq!(response.status_code, 200);
    assert_eq!(response.body.to_compact_json(), r#"{"status":"healthy"}"#);
    assert!(response.headers.is_empty());
}

#[test]
fn app_agent_card_response_sets_python_cache_headers_and_body() {
    let card = build_agent_card(AgentCardOptions::new("127.0.0.1", 41242, false));
    let last_modified = "Fri, 05 Jun 2026 00:00:00 GMT";

    let response = agent_card_response(&card, None, last_modified);

    assert_eq!(response.status_code, 200);
    assert_eq!(response.body, card);
    assert_eq!(
        response.headers.get("Cache-Control").map(String::as_str),
        Some("public, max-age=60")
    );
    assert_eq!(
        response.headers.get("Last-Modified").map(String::as_str),
        Some(last_modified)
    );
    assert_eq!(
        response.headers.get("ETag").map(String::as_str),
        Some(agent_card_etag(&card).as_str())
    );
}

#[test]
fn app_agent_card_response_returns_304_for_matching_if_none_match() {
    let card = build_agent_card(AgentCardOptions::new("127.0.0.1", 41242, false));
    let etag = agent_card_etag(&card);

    let response = agent_card_response(&card, Some(&etag), "Fri, 05 Jun 2026 00:00:00 GMT");

    assert_eq!(response.status_code, 304);
    assert_eq!(response.body, iac_code_protocol::json::null());
    assert_eq!(
        response.headers.get("ETag").map(String::as_str),
        Some(etag.as_str())
    );
}

#[test]
fn app_recognizes_v03_jsonrpc_methods() {
    assert!(is_v03_jsonrpc_method("message/send"));
    assert!(is_v03_jsonrpc_method("tasks/pushNotificationConfig/set"));
    assert!(!is_v03_jsonrpc_method("SendMessage"));
}

#[test]
fn supported_interfaces_match_python_transport_bindings() {
    assert_eq!(
        supported_interfaces(SupportedInterfacesOptions {
            transport: "http".to_owned(),
            ..SupportedInterfacesOptions::default()
        })
        .unwrap()
        .into_iter()
        .map(|item| (item.url, item.protocol_binding, item.protocol_version))
        .collect::<Vec<_>>(),
        vec![
            (
                "http://127.0.0.1:41242/".to_owned(),
                "JSONRPC".to_owned(),
                "1.0".to_owned()
            ),
            (
                "http://127.0.0.1:41242".to_owned(),
                "HTTP+JSON".to_owned(),
                "1.0".to_owned()
            ),
        ]
    );
    assert_eq!(
        supported_interfaces(SupportedInterfacesOptions {
            transport: "grpc".to_owned(),
            grpc_port: Some(0),
            ..SupportedInterfacesOptions::default()
        })
        .unwrap()[0]
            .url,
        "grpc://127.0.0.1:0"
    );
    assert_eq!(
        supported_interfaces(SupportedInterfacesOptions {
            transport: "redis-streams".to_owned(),
            redis_url: Some("redis://127.0.0.1:6379/0".to_owned()),
            request_stream: "requests".to_owned(),
            response_stream: "responses".to_owned(),
            consumer_group: "iac-code".to_owned(),
            ..SupportedInterfacesOptions::default()
        })
        .unwrap()[0]
            .url,
        "redis-streams://redis://127.0.0.1:6379/0/requests/responses/iac-code"
    );
}

fn headers<const N: usize>(items: [(&str, &str); N]) -> BTreeMap<String, String> {
    items
        .into_iter()
        .map(|(key, value)| (key.to_owned(), value.to_owned()))
        .collect()
}

fn env_guard() -> std::sync::MutexGuard<'static, ()> {
    static LOCK: OnceLock<Mutex<()>> = OnceLock::new();
    LOCK.get_or_init(|| Mutex::new(())).lock().unwrap()
}
