use std::fs;

use iac_code_acp::session::{AcpAgent, AcpMcpServerConfig, PermissionDecision};
use iac_code_core::SessionStorage;
use iac_code_protocol::json;
use iac_code_protocol::message::{
    AgentContentBlock, AgentMessageContent, Conversation, ImageBlock, TextBlock,
};
use iac_code_protocol::StreamEvent;

use crate::acp_agent::AcpHeadlessAgent;
use crate::acp_payload::acp_model_state_json;
use crate::acp_server::{handle_acp_jsonrpc, AcpServerRuntime};
use crate::acp_sessions::handle_acp_new_session;
use crate::json_utils::json_string_field;
use crate::jsonrpc_payload::jsonrpc_result;
use crate::test_support::{
    english_locale_config_dir_guard, english_locale_guard, unique_temp_dir, EnvVarGuard,
};

#[test]
fn acp_headless_agent_preserves_structured_image_prompt() {
    let _env = EnvVarGuard::set("IAC_CODE_RS_FAKE_PROVIDER", "1");
    let mut agent = AcpHeadlessAgent::new(
        "acp-session-structured-image".into(),
        "/tmp/iac-code-rs-acp".into(),
        Conversation::default(),
        None,
    );
    let content = AgentMessageContent::Blocks(vec![
        AgentContentBlock::Text(TextBlock {
            text: "describe".into(),
        }),
        AgentContentBlock::Image(ImageBlock {
            media_type: "image/png".into(),
            data: "base64-image".into(),
        }),
    ]);
    let mut permission = |_event| PermissionDecision::Allow;

    let events = agent.run_streaming_content(content.clone(), "describe", &mut permission);

    assert_eq!(agent.conversation.messages[0].content, content);
    assert!(events.iter().any(|event| matches!(
        event,
        StreamEvent::TextDelta(delta) if delta.text == "fixture response: describe"
    )));
}

#[test]
fn acp_new_session_stores_mcp_servers_from_params_like_python() {
    let config_dir = unique_temp_dir("iac-code-rs-acp-new-session-config");
    fs::create_dir_all(&config_dir).expect("config dir should be created");
    fs::write(
        config_dir.join("settings.yml"),
        "activeProvider: openapi_compatible\nproviders:\n  openapi_compatible:\n    model: fixture-openapi-model\n",
    )
    .expect("settings should be written");
    let _env = EnvVarGuard::set(
        "IAC_CODE_CONFIG_DIR",
        config_dir
            .to_str()
            .expect("config dir should be valid unicode"),
    );
    let expected_models = acp_model_state_json();
    let mut runtime = AcpServerRuntime::new();
    let params = json::object([
        ("cwd", json::string("/tmp/iac-code-rs-acp")),
        (
            "mcpServers",
            json::array([json::object([
                ("type", json::string("stdio")),
                ("name", json::string("local-tools")),
                ("command", json::string("uvx")),
                ("args", json::array([json::string("mcp-server")])),
                (
                    "env",
                    json::array([json::object([
                        ("name", json::string("TOKEN")),
                        ("value", json::string("secret")),
                    ])]),
                ),
            ])]),
        ),
    ]);

    let messages = handle_acp_new_session(json::number(1), Some(&params), &mut runtime);

    assert_eq!(messages.len(), 2);
    assert_eq!(
        json_string_field(&messages[0], "method"),
        Some("session/update")
    );
    assert_eq!(
        messages[1],
        json::object([
            ("jsonrpc", json::string("2.0")),
            ("id", json::number(1)),
            (
                "result",
                json::object([
                    ("sessionId", json::string("acp-session-1")),
                    ("models", expected_models),
                ]),
            ),
        ])
    );
    let session = runtime
        .sessions
        .get("acp-session-1")
        .expect("session should be created");
    assert_eq!(
        session.mcp_configs(),
        &[AcpMcpServerConfig::Stdio {
            name: "local-tools".into(),
            command: "uvx".into(),
            args: vec!["mcp-server".into()],
            env: [("TOKEN".to_owned(), "secret".to_owned())].into(),
        }]
    );
    fs::remove_dir_all(config_dir).ok();
}

#[test]
fn acp_load_and_fork_not_found_return_field_errors_like_python() {
    let config_dir = unique_temp_dir("iac-code-rs-acp-load-fork-missing-config");
    let _env = EnvVarGuard::set(
        "IAC_CODE_CONFIG_DIR",
        config_dir
            .to_str()
            .expect("config dir should be valid unicode"),
    );
    fs::create_dir_all(&config_dir).expect("config dir should be created");

    let mut runtime = AcpServerRuntime::new();
    let load = handle_acp_jsonrpc(
        r#"{"jsonrpc":"2.0","id":"load-missing","method":"session/load","params":{"cwd":"/tmp","sessionId":"missing-load"}}"#,
        &mut runtime,
    );
    let fork = handle_acp_jsonrpc(
        r#"{"jsonrpc":"2.0","id":"fork-missing","method":"session/fork","params":{"cwd":"/tmp","sessionId":"missing-fork"}}"#,
        &mut runtime,
    );

    let load_body = load
        .last()
        .expect("load response should be emitted")
        .to_compact_json();
    assert!(load_body.contains("\"code\":-32602"), "{load_body}");
    assert!(
        load_body.contains("\"message\":\"Invalid params\""),
        "{load_body}"
    );
    assert!(
        load_body.contains("\"session_id\":\"Session not found\""),
        "{load_body}"
    );

    let fork_body = fork
        .last()
        .expect("fork response should be emitted")
        .to_compact_json();
    assert!(fork_body.contains("\"code\":-32602"), "{fork_body}");
    assert!(
        fork_body.contains("\"message\":\"Invalid params\""),
        "{fork_body}"
    );
    assert!(
        fork_body.contains("\"session_id\":\"Source session not found\""),
        "{fork_body}"
    );

    fs::remove_dir_all(config_dir).ok();
}

#[test]
fn acp_prompt_cancel_and_config_not_found_return_field_errors_like_python() {
    let mut runtime = AcpServerRuntime::new();
    let cases = [
        (
            "prompt-missing",
            r#"{"jsonrpc":"2.0","id":"prompt-missing","method":"session/prompt","params":{"sessionId":"missing-prompt","prompt":[{"type":"text","text":"hello"}]}}"#,
        ),
        (
            "cancel-missing",
            r#"{"jsonrpc":"2.0","id":"cancel-missing","method":"session/cancel","params":{"sessionId":"missing-cancel"}}"#,
        ),
        (
            "config-missing",
            r#"{"jsonrpc":"2.0","id":"config-missing","method":"session/set_config_option","params":{"sessionId":"missing-config","configId":"temperature","value":"0.5"}}"#,
        ),
    ];

    for (id, body) in cases {
        let response = handle_acp_jsonrpc(body, &mut runtime);
        let response_body = response
            .last()
            .expect("response should be emitted")
            .to_compact_json();
        assert!(
            response_body.contains(&format!("\"id\":\"{id}\"")),
            "{response_body}"
        );
        assert!(response_body.contains("\"code\":-32602"), "{response_body}");
        assert!(
            response_body.contains("\"message\":\"Invalid params\""),
            "{response_body}"
        );
        assert!(
            response_body.contains("\"session_id\":\"Session not found\""),
            "{response_body}"
        );
    }
}

#[test]
fn acp_set_config_option_updates_session_config_like_python() {
    let mut runtime = AcpServerRuntime::new();
    let session_id =
        runtime.create_session_with_mcp("/tmp/iac-code-rs-acp-config".to_owned(), Vec::new());

    let first = handle_acp_jsonrpc(
        &format!(
            r#"{{"jsonrpc":"2.0","id":"set-config-1","method":"session/set_config_option","params":{{"sessionId":"{session_id}","configId":"temperature","value":"0.5"}}}}"#
        ),
        &mut runtime,
    );
    let second = handle_acp_jsonrpc(
        &format!(
            r#"{{"jsonrpc":"2.0","id":"set-config-2","method":"session/set_config_option","params":{{"sessionId":"{session_id}","configId":"max_tokens","value":2048}}}}"#
        ),
        &mut runtime,
    );
    let third = handle_acp_jsonrpc(
        &format!(
            r#"{{"jsonrpc":"2.0","id":"set-config-3","method":"session/set_config_option","params":{{"sessionId":"{session_id}","configId":"temperature","value":"0.9"}}}}"#
        ),
        &mut runtime,
    );

    assert_eq!(
        first,
        vec![jsonrpc_result(json::string("set-config-1"), json::null())]
    );
    assert_eq!(
        second,
        vec![jsonrpc_result(json::string("set-config-2"), json::null())]
    );
    assert_eq!(
        third,
        vec![jsonrpc_result(json::string("set-config-3"), json::null())]
    );

    let session = runtime
        .sessions
        .get(&session_id)
        .expect("session should remain active");
    let config = session.config();
    assert_eq!(config.get("temperature"), Some(&json::string("0.9")));
    assert_eq!(config.get("max_tokens"), Some(&json::number(2048)));
}

#[test]
fn acp_resume_active_session_rejects_other_project_like_python() {
    let _locale_env = english_locale_guard();
    let mut runtime = AcpServerRuntime::new();
    let session_id =
        runtime.create_session_with_mcp("/source project;unsafe".to_owned(), Vec::new());

    let response = handle_acp_jsonrpc(
        &format!(
            r#"{{"jsonrpc":"2.0","id":"resume-other-project","method":"session/resume","params":{{"cwd":"/other","sessionId":"{session_id}"}}}}"#
        ),
        &mut runtime,
    );

    let body = response
        .last()
        .expect("resume response should be emitted")
        .to_compact_json();
    assert!(body.contains("\"code\":-32602"), "{body}");
    assert!(
        body.contains("Session belongs to another project"),
        "{body}"
    );
    assert!(body.contains("\"session_id\":\"acp-session-1\""), "{body}");
    assert!(
        body.contains("\"resolved_session_id\":\"acp-session-1\""),
        "{body}"
    );
    assert!(
        body.contains("\"cwd\":\"/source project;unsafe\""),
        "{body}"
    );
    assert!(body.contains("/source project;unsafe"), "{body}");
    assert!(body.contains("iac-code --resume acp-session-1"), "{body}");
}

#[test]
fn acp_resume_cross_project_name_returns_hint_data_like_python() {
    let config_dir = unique_temp_dir("iac-code-rs-acp-resume-cross-project-config");
    let config_dir_text = config_dir
        .to_str()
        .expect("config dir should be valid unicode")
        .to_owned();
    let _env = EnvVarGuard::set_many(&[
        ("LANGUAGE", "en"),
        ("LC_ALL", "en_US.UTF-8"),
        ("LC_MESSAGES", "en_US.UTF-8"),
        ("LANG", "en_US.UTF-8"),
        ("IAC_CODE_CONFIG_DIR", &config_dir_text),
    ]);
    let storage =
        SessionStorage::new(config_dir.join("projects")).expect("session storage should init");
    let mut previous = Conversation::default();
    previous.add_user_message(AgentMessageContent::Text("hello".into()));
    storage
        .save(
            "/other project;unsafe",
            "foreign-session-123",
            &previous.messages,
            None,
        )
        .expect("foreign session should be saved");
    storage
        .rename_session(
            "/other project;unsafe",
            "foreign-session-123",
            "foreign-deploy",
            None,
        )
        .expect("foreign session should be named");

    let mut runtime = AcpServerRuntime::new();
    let response = handle_acp_jsonrpc(
        r#"{"jsonrpc":"2.0","id":"resume-cross-project","method":"session/resume","params":{"cwd":"/tmp","sessionId":"foreign-deploy"}}"#,
        &mut runtime,
    );

    let body = response
        .last()
        .expect("resume response should be emitted")
        .to_compact_json();
    assert!(body.contains("\"code\":-32602"), "{body}");
    assert!(
        body.contains("Session belongs to another project"),
        "{body}"
    );
    assert!(body.contains("\"session_id\":\"foreign-deploy\""), "{body}");
    assert!(
        body.contains("\"resolved_session_id\":\"foreign-session-123\""),
        "{body}"
    );
    assert!(body.contains("\"cwd\":\"/other project;unsafe\""), "{body}");
    assert!(
        body.contains("cd '/other project;unsafe' && iac-code --resume foreign-session-123"),
        "{body}"
    );

    fs::remove_dir_all(config_dir).ok();
}

#[test]
fn acp_resume_not_found_returns_session_id_data_like_python() {
    let config_dir = unique_temp_dir("iac-code-rs-acp-resume-not-found-config");
    let _env = english_locale_config_dir_guard(&config_dir);
    fs::create_dir_all(&config_dir).expect("config dir should be created");

    let mut runtime = AcpServerRuntime::new();
    let response = handle_acp_jsonrpc(
        r#"{"jsonrpc":"2.0","id":"resume-missing","method":"session/resume","params":{"cwd":"/tmp","sessionId":"nonexistent-id"}}"#,
        &mut runtime,
    );

    let body = response
        .last()
        .expect("resume response should be emitted")
        .to_compact_json();
    assert!(body.contains("\"code\":-32602"), "{body}");
    assert!(body.contains("\"message\":\"Session not found\""), "{body}");
    assert!(body.contains("\"session_id\":\"nonexistent-id\""), "{body}");
    assert!(!body.contains("Session not found:"), "{body}");

    fs::remove_dir_all(config_dir).ok();
}

#[test]
fn acp_resume_ambiguous_name_returns_candidates_like_python() {
    let config_dir = unique_temp_dir("iac-code-rs-acp-resume-ambiguous-config");
    let _env = EnvVarGuard::set(
        "IAC_CODE_CONFIG_DIR",
        config_dir
            .to_str()
            .expect("config dir should be valid unicode"),
    );
    let storage =
        SessionStorage::new(config_dir.join("projects")).expect("session storage should init");
    let mut previous = Conversation::default();
    previous.add_user_message(AgentMessageContent::Text("hello".into()));
    storage
        .save("/project a;bad", "candidate-a", &previous.messages, None)
        .expect("first session should be saved");
    storage
        .rename_session("/project a;bad", "candidate-a", "deploy-prod", None)
        .expect("first session should be named");
    storage
        .save("/project-b", "candidate-b", &previous.messages, None)
        .expect("second session should be saved");
    storage
        .rename_session("/project-b", "candidate-b", "deploy-prod", None)
        .expect("second session should be named");

    let mut runtime = AcpServerRuntime::new();
    let response = handle_acp_jsonrpc(
        r#"{"jsonrpc":"2.0","id":"resume-ambiguous","method":"session/resume","params":{"cwd":"/current","sessionId":"deploy-prod"}}"#,
        &mut runtime,
    );

    let body = response
        .last()
        .expect("resume response should be emitted")
        .to_compact_json();
    assert!(body.contains("\"code\":-32602"), "{body}");
    assert!(body.contains("Session name is ambiguous"), "{body}");
    assert!(body.contains("\"candidates\""), "{body}");
    assert!(body.contains("\"session_id\":\"candidate-a\""), "{body}");
    assert!(body.contains("\"session_id\":\"candidate-b\""), "{body}");
    assert!(
        body.contains("cd '/project a;bad' && iac-code --resume candidate-a"),
        "{body}"
    );
    assert!(
        body.contains("cd /project-b && iac-code --resume candidate-b"),
        "{body}"
    );

    fs::remove_dir_all(config_dir).ok();
}
