use std::collections::BTreeMap;

use iac_code_a2a::agent_card::{build_agent_card, AgentCardOptions, AgentExtensionConfig};
use iac_code_a2a::server::{
    missing_required_extensions, required_extension_uris, validate_required_extensions,
    validate_server_startup_options, ServerStartupOptions,
};
use iac_code_protocol::json;

#[test]
fn server_extracts_required_extension_uris_sorted_like_python_handler() {
    let card = build_agent_card(AgentCardOptions {
        agent_extensions: vec![
            AgentExtensionConfig {
                uri: "urn:iac-code:z-required".to_owned(),
                description: "required z".to_owned(),
                required: true,
                params: BTreeMap::new(),
            },
            AgentExtensionConfig {
                uri: "urn:iac-code:optional".to_owned(),
                description: "optional".to_owned(),
                required: false,
                params: BTreeMap::new(),
            },
            AgentExtensionConfig {
                uri: "urn:iac-code:a-required".to_owned(),
                description: "required a".to_owned(),
                required: true,
                params: BTreeMap::new(),
            },
        ],
        ..AgentCardOptions::new("127.0.0.1", 41242, false)
    });

    assert_eq!(
        required_extension_uris(&card),
        vec!["urn:iac-code:a-required", "urn:iac-code:z-required"]
    );
}

#[test]
fn server_reports_missing_required_extensions_with_python_message() {
    let card = build_agent_card(AgentCardOptions {
        agent_extensions: vec![
            AgentExtensionConfig {
                uri: "urn:iac-code:z-required".to_owned(),
                description: "required z".to_owned(),
                required: true,
                params: BTreeMap::new(),
            },
            AgentExtensionConfig {
                uri: "urn:iac-code:a-required".to_owned(),
                description: "required a".to_owned(),
                required: true,
                params: BTreeMap::new(),
            },
        ],
        ..AgentCardOptions::new("127.0.0.1", 41242, false)
    });

    assert_eq!(
        missing_required_extensions(&card, ["urn:iac-code:z-required"]),
        vec!["urn:iac-code:a-required"]
    );
    assert_eq!(
        validate_required_extensions(&card, ["urn:iac-code:z-required"])
            .unwrap_err()
            .to_string(),
        "Required A2A extensions were not requested: urn:iac-code:a-required"
    );
}

#[test]
fn server_accepts_when_all_required_extensions_are_requested() {
    let card = build_agent_card(AgentCardOptions {
        agent_extensions: vec![AgentExtensionConfig {
            uri: "urn:iac-code:required".to_owned(),
            description: "required".to_owned(),
            required: true,
            params: BTreeMap::new(),
        }],
        ..AgentCardOptions::new("127.0.0.1", 41242, false)
    });

    validate_required_extensions(&card, ["urn:iac-code:required"]).expect("all requested");
}

#[test]
fn server_ignores_malformed_extension_entries_like_python_attribute_filtering() {
    let malformed_card = json::object([(
        "capabilities",
        json::object([(
            "extensions",
            json::array([
                json::object([
                    ("uri", json::string("urn:iac-code:required")),
                    ("required", json::bool_value(true)),
                ]),
                json::object([("required", json::bool_value(true))]),
                json::object([
                    ("uri", json::string("urn:iac-code:not-required")),
                    ("required", json::bool_value(false)),
                ]),
            ]),
        )]),
    )]);

    assert_eq!(
        required_extension_uris(&malformed_card),
        vec!["urn:iac-code:required"]
    );
}

#[test]
fn server_startup_options_require_transport_specific_runtime_config() {
    assert_eq!(
        validate_server_startup_options(ServerStartupOptions::new("unix", "linux"))
            .unwrap_err()
            .to_string(),
        "--socket-path is required for --transport unix."
    );

    assert_eq!(
        validate_server_startup_options(ServerStartupOptions::new("redis-streams", "linux"))
            .unwrap_err()
            .to_string(),
        "--redis-url is required for --transport redis-streams."
    );

    assert_eq!(
        validate_server_startup_options(ServerStartupOptions {
            push_queue: "redis-streams",
            ..ServerStartupOptions::new("http", "linux")
        })
        .unwrap_err()
        .to_string(),
        "--push-redis-url is required for --push-queue redis-streams."
    );
}

#[test]
fn server_startup_options_normalize_transport_aliases_and_check_platform_after_required_config() {
    assert_eq!(
        validate_server_startup_options(ServerStartupOptions {
            redis_url: Some("redis://127.0.0.1/0"),
            ..ServerStartupOptions::new("redis", "linux")
        })
        .expect("valid redis alias"),
        "redis-streams"
    );

    assert_eq!(
        validate_server_startup_options(ServerStartupOptions {
            socket_path: Some("/tmp/iac-code.sock"),
            ..ServerStartupOptions::new("unix", "win32")
        })
        .unwrap_err()
        .to_string(),
        "Unix domain socket transport is not supported on Windows. Use --transport http or --transport stdio instead."
    );

    assert_eq!(
        validate_server_startup_options(ServerStartupOptions::new("unix", "win32"))
            .unwrap_err()
            .to_string(),
        "--socket-path is required for --transport unix."
    );
}
