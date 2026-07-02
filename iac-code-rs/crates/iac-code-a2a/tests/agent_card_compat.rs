use std::collections::BTreeMap;

use iac_code_a2a::agent_card::{
    agent_card_to_client_dict, build_agent_card, A2AExposureType, AgentCardOptions,
    AgentExtensionConfig, AgentInterfaceConfig, IAC_CODE_ARTIFACT_METADATA_EXTENSION_URI,
    IAC_CODE_THINKING_EXPOSURE_EXTENSION_URI,
};
use iac_code_protocol::json::JsonValue;

#[test]
fn agent_card_declares_a2a_1_jsonrpc_interface() {
    let card = build_agent_card(AgentCardOptions::new("127.0.0.1", 41242, false));

    assert_eq!(string_at(&card, &["name"]), "iac-code");
    assert_eq!(
        string_at(&card, &["supportedInterfaces", "0", "protocolVersion"]),
        "1.0"
    );
    assert_eq!(
        string_at(&card, &["supportedInterfaces", "0", "protocolBinding"]),
        "JSONRPC"
    );
    assert_eq!(
        string_at(&card, &["supportedInterfaces", "0", "url"]),
        "http://127.0.0.1:41242/"
    );
    assert!(bool_at(&card, &["capabilities", "streaming"]));
    assert!(!bool_at(&card, &["capabilities", "pushNotifications"]));
    assert!(array_at(&card, &["skills"])
        .iter()
        .any(|skill| string_at(skill, &["id"]) == "iac_generation"));
}

#[test]
fn agent_card_advertises_supported_input_mime_modes() {
    let card = build_agent_card(AgentCardOptions::new("127.0.0.1", 41242, false));
    let modes = strings_at(&card, &["defaultInputModes"]);

    assert_eq!(
        modes,
        vec![
            "text/plain",
            "application/json",
            "text/markdown",
            "text/yaml",
            "application/yaml",
            "application/x-yaml",
            "image/png",
            "image/jpeg",
            "image/webp",
            "image/gif",
            "audio/mpeg",
            "audio/wav",
            "audio/ogg",
            "application/octet-stream",
        ]
    );
    assert!(array_at(&card, &["skills"])
        .iter()
        .all(|skill| strings_at(skill, &["inputModes"]) == modes));
}

#[test]
fn agent_card_advertises_auth_schemes_when_enabled() {
    let card = build_agent_card(AgentCardOptions {
        basic_enabled: true,
        api_key_enabled: true,
        api_key_header: "X-IAC-Code-Key".to_owned(),
        ..AgentCardOptions::new("127.0.0.1", 41242, true)
    });

    assert_eq!(
        string_at(
            &card,
            &[
                "securitySchemes",
                "bearerAuth",
                "httpAuthSecurityScheme",
                "scheme"
            ]
        ),
        "bearer"
    );
    assert_eq!(
        string_at(
            &card,
            &[
                "securitySchemes",
                "basicAuth",
                "httpAuthSecurityScheme",
                "scheme"
            ]
        ),
        "basic"
    );
    assert_eq!(
        string_at(
            &card,
            &[
                "securitySchemes",
                "apiKeyAuth",
                "apiKeySecurityScheme",
                "name"
            ]
        ),
        "X-IAC-Code-Key"
    );
    assert_eq!(array_at(&card, &["securityRequirements"]).len(), 3);
}

#[test]
fn agent_card_advertises_extensions_interfaces_push_and_signature() {
    let card = build_agent_card(AgentCardOptions {
        push_notifications: true,
        signing_secret: Some("s".repeat(32)),
        thinking_exposure_types: vec![A2AExposureType::RawThinking, A2AExposureType::ToolTrace],
        supported_interfaces: vec![
            AgentInterfaceConfig::new("unix:///tmp/iac-code.sock", "unix", "1.0"),
            AgentInterfaceConfig::new("ws://127.0.0.1:41243/a2a", "websocket", "1.0"),
        ],
        agent_extensions: vec![AgentExtensionConfig {
            uri: "urn:iac-code:test-required".to_owned(),
            description: "test required extension".to_owned(),
            required: true,
            params: BTreeMap::new(),
        }],
        ..AgentCardOptions::new("127.0.0.1", 41242, false)
    });

    assert!(bool_at(&card, &["capabilities", "pushNotifications"]));
    assert_eq!(
        string_at(&card, &["capabilities", "extensions", "0", "uri"]),
        IAC_CODE_ARTIFACT_METADATA_EXTENSION_URI
    );
    assert_eq!(
        string_at(&card, &["capabilities", "extensions", "1", "uri"]),
        IAC_CODE_THINKING_EXPOSURE_EXTENSION_URI
    );
    assert_eq!(
        strings_at(
            &card,
            &["capabilities", "extensions", "1", "params", "enabledTypes"]
        ),
        vec!["raw_thinking", "tool_trace"]
    );
    assert_eq!(
        string_at(&card, &["capabilities", "extensions", "2", "uri"]),
        "urn:iac-code:test-required"
    );
    assert_eq!(
        string_at(&card, &["supportedInterfaces", "0", "protocolBinding"]),
        "unix"
    );
    assert!(!string_at(&card, &["signatures", "0", "protected"]).is_empty());
}

#[test]
fn agent_card_client_dict_promotes_primary_interface() {
    let card = build_agent_card(AgentCardOptions {
        supported_interfaces: vec![
            AgentInterfaceConfig::new("unix:///tmp/iac-code.sock", "unix", "1.0"),
            AgentInterfaceConfig::new("ws://127.0.0.1:41243/a2a", "websocket", "1.0"),
        ],
        ..AgentCardOptions::new("127.0.0.1", 41242, false)
    });

    let client = agent_card_to_client_dict(&card);

    assert_eq!(string_at(&client, &["url"]), "unix:///tmp/iac-code.sock");
    assert_eq!(string_at(&client, &["preferredTransport"]), "unix");
    assert_eq!(string_at(&client, &["protocolVersion"]), "1.0");
    assert_eq!(
        string_at(&client, &["additionalInterfaces", "0", "transport"]),
        "websocket"
    );
}

fn string_at(value: &JsonValue, path: &[&str]) -> String {
    match at(value, path) {
        JsonValue::String(value) => value.clone(),
        other => panic!("expected string at {path:?}, got {other:?}"),
    }
}

fn bool_at(value: &JsonValue, path: &[&str]) -> bool {
    match at(value, path) {
        JsonValue::Bool(value) => *value,
        other => panic!("expected bool at {path:?}, got {other:?}"),
    }
}

fn strings_at(value: &JsonValue, path: &[&str]) -> Vec<String> {
    array_at(value, path)
        .iter()
        .map(|value| string_at(value, &[]))
        .collect()
}

fn array_at<'a>(value: &'a JsonValue, path: &[&str]) -> &'a [JsonValue] {
    match at(value, path) {
        JsonValue::Array(values) => values,
        other => panic!("expected array at {path:?}, got {other:?}"),
    }
}

fn at<'a>(mut value: &'a JsonValue, path: &[&str]) -> &'a JsonValue {
    for segment in path {
        value = match value {
            JsonValue::Object(object) => object.get(*segment).unwrap_or_else(|| {
                panic!("missing object key {segment:?} in path {path:?}");
            }),
            JsonValue::Array(values) => values
                .get(segment.parse::<usize>().expect("array index"))
                .unwrap_or_else(|| panic!("missing array index {segment:?} in path {path:?}")),
            other => panic!("cannot descend into {other:?} at {segment:?}"),
        };
    }
    value
}
