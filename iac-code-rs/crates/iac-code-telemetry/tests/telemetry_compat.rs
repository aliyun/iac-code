use std::fs;
use std::path::PathBuf;
use std::sync::Mutex;
use std::time::{SystemTime, UNIX_EPOCH};

use iac_code_protocol::json;
use iac_code_protocol::message::{
    AgentContentBlock, AgentMessage, AgentMessageContent, TextBlock, ToolResultBlock, ToolUseBlock,
};
use iac_code_protocol::provider::ToolDefinition;
use iac_code_telemetry::config::{
    get_content_capture_mode, get_privacy_level, is_essential_traffic_only,
    is_telemetry_disabled_for_release_date, should_capture_content_on_span, ContentCaptureMode,
    PrivacyLevel,
};
use iac_code_telemetry::content::{
    serialize_input_messages, serialize_output_messages, serialize_system_instructions,
    serialize_tool_arguments_json, serialize_tool_arguments_text, serialize_tool_definitions,
    serialize_tool_result_text, serialize_user_input,
};
use iac_code_telemetry::fallback::FallbackStore;
use iac_code_telemetry::identity::{
    use_session_id, Identity, SESSION_ID_PREFIX, TENANT_ID_PREFIX, USER_ID_PREFIX,
};
use iac_code_telemetry::names::{
    events, gen_ai_attr, gen_ai_operation_name, gen_ai_span_kind, metrics, spans,
};
use iac_code_telemetry::sanitize::{
    bucket_resource_count, sanitize_error_message, sanitize_model_name, sanitize_resource_type,
    sanitize_skill_name, sanitize_terraform_provider, sanitize_tool_name, ResourceKind,
};

static ENV_LOCK: Mutex<()> = Mutex::new(());

#[test]
fn telemetry_name_constants_match_python_contract() {
    assert_eq!(gen_ai_span_kind::ENTRY, "ENTRY");
    assert_eq!(gen_ai_span_kind::LLM, "LLM");
    assert_eq!(gen_ai_operation_name::ENTER, "enter");
    assert_eq!(gen_ai_operation_name::CHAT, "chat");
    assert_eq!(gen_ai_operation_name::EXECUTE_TOOL, "execute_tool");
    assert_eq!(gen_ai_attr::SPAN_KIND, "gen_ai.span.kind");
    assert_eq!(gen_ai_attr::SESSION_ID, "gen_ai.session.id");
    assert_eq!(gen_ai_attr::TOOL_CALL_RESULT, "gen_ai.tool.call.result");

    let expected_events = [
        "iac.init",
        "iac.session.started",
        "iac.session.exited",
        "iac.session.cancelled",
        "iac.auth.configured",
        "iac.api.request.started",
        "iac.api.request.succeeded",
        "iac.api.request.failed",
        "iac.api.request.retried",
        "iac.model.fallback.triggered",
        "iac.tool.use.succeeded",
        "iac.tool.use.failed",
        "iac.tool.use.granted_in_prompt",
        "iac.tool.use.rejected_in_prompt",
        "iac.template.generated",
        "iac.template.validated",
        "iac.deployment.started",
        "iac.deployment.succeeded",
        "iac.deployment.failed",
        "iac.deployment.cancelled",
        "iac.doc.searched",
        "iac.skill.invoked",
        "iac.skill.completed",
        "iac.aliyun.api.called",
        "iac.memory.compact.succeeded",
        "iac.memory.compact.failed",
        "iac.exception.uncaught",
        "iac.exception.unhandled",
        "iac.query.failed",
    ];
    assert_eq!(events::ALL, expected_events);
    assert!(events::ALL.iter().all(|name| name.starts_with("iac.")));

    assert_eq!(metrics::SESSION_COUNT, "iac.session.count");
    assert_eq!(metrics::TOKEN_USAGE, "iac.token.usage");
    assert!(metrics::ALL.iter().all(|name| name.starts_with("iac.")));

    assert_eq!(spans::ENTRY, "enter_ai_application_system");
    assert_eq!(spans::LLM_CHAT, "chat");
    assert_eq!(spans::TOOL_EXECUTE, "execute_tool");
    assert_eq!(spans::REACT_STEP, "react step");
}

#[test]
fn privacy_and_content_capture_modes_match_python_env_rules() {
    let _guard = ENV_LOCK.lock().expect("env lock");
    clear_telemetry_env();

    assert_eq!(get_privacy_level(), PrivacyLevel::Default);
    assert!(!is_essential_traffic_only());
    assert!(!is_telemetry_disabled_for_release_date("2026-01-01"));
    assert!(is_telemetry_disabled_for_release_date(""));

    std::env::set_var("DISABLE_TELEMETRY", "1");
    assert_eq!(get_privacy_level(), PrivacyLevel::NoTelemetry);
    assert!(is_telemetry_disabled_for_release_date("2026-01-01"));
    assert!(!is_essential_traffic_only());

    std::env::set_var("IAC_CODE_DISABLE_NONESSENTIAL_TRAFFIC", "yes");
    assert_eq!(get_privacy_level(), PrivacyLevel::EssentialTraffic);
    assert!(is_essential_traffic_only());

    clear_telemetry_env();
    assert_eq!(get_content_capture_mode(), ContentCaptureMode::NoContent);
    assert!(!should_capture_content_on_span(false));
    assert!(should_capture_content_on_span(true));

    std::env::set_var(
        "OTEL_INSTRUMENTATION_GENAI_CAPTURE_MESSAGE_CONTENT",
        "span_only",
    );
    assert_eq!(get_content_capture_mode(), ContentCaptureMode::SpanOnly);
    assert!(should_capture_content_on_span(false));

    std::env::set_var(
        "OTEL_INSTRUMENTATION_GENAI_CAPTURE_MESSAGE_CONTENT",
        "EVENT_ONLY",
    );
    assert_eq!(get_content_capture_mode(), ContentCaptureMode::EventOnly);
    assert!(!should_capture_content_on_span(false));

    clear_telemetry_env();
}

#[test]
fn sanitizers_match_python_placeholders_and_buckets() {
    let _guard = ENV_LOCK.lock().expect("env lock");
    clear_telemetry_env();

    assert_eq!(sanitize_error_message(None), None);
    assert_eq!(
        sanitize_error_message(Some("line1\nline2\rline3\tend")).as_deref(),
        Some("line1 line2 line3 end")
    );
    let truncated = sanitize_error_message(Some(&"x".repeat(1000))).expect("truncated");
    assert!(truncated.ends_with("... (truncated)"));
    assert!(truncated.len() <= 512);
    std::env::set_var("IAC_CODE_DISABLE_NONESSENTIAL_TRAFFIC", "1");
    assert_eq!(sanitize_error_message(Some("rate limit")), None);
    clear_telemetry_env();

    assert_eq!(
        sanitize_skill_name(Some("iac_aliyun")).as_deref(),
        Some("iac_aliyun")
    );
    assert_eq!(
        sanitize_skill_name(Some("custom_skill")).as_deref(),
        Some("custom")
    );
    assert_eq!(sanitize_skill_name(None), None);

    assert_eq!(
        sanitize_resource_type("ALIYUN::ECS::Instance", ResourceKind::Ros),
        "ALIYUN::ECS::Instance"
    );
    assert_eq!(
        sanitize_resource_type("Custom::Acme::Thing", ResourceKind::Ros),
        "Custom::Other"
    );
    assert_eq!(
        sanitize_resource_type("aws_s3_bucket", ResourceKind::Terraform),
        "aws_s3_bucket"
    );
    assert_eq!(
        sanitize_resource_type("acme_internal", ResourceKind::Terraform),
        "custom_provider::other"
    );
    assert_eq!(sanitize_terraform_provider("alicloud"), "alicloud");
    assert_eq!(sanitize_terraform_provider("acme"), "other");
    assert_eq!(
        sanitize_model_name("claude-opus-4-7-20260101"),
        "claude-opus-4-7"
    );
    assert_eq!(sanitize_model_name("private-model"), "other");
    assert_eq!(sanitize_tool_name("Bash"), "Bash");
    assert_eq!(sanitize_tool_name("mcp__acme__query"), "mcp_tool");
    assert_eq!(bucket_resource_count(5), "1-5");
    assert_eq!(bucket_resource_count(20), "6-20");
    assert_eq!(bucket_resource_count(50), "21-50");
    assert_eq!(bucket_resource_count(51), "50+");
}

#[test]
fn content_serializers_match_python_json_shapes() {
    assert_eq!(
        serialize_user_input("Hello"),
        r#"[{"parts":[{"content":"Hello","type":"text"}],"role":"user"}]"#
    );

    let messages = vec![
        AgentMessage {
            role: "assistant".into(),
            content: AgentMessageContent::Blocks(vec![
                AgentContentBlock::Text(TextBlock { text: "Hi".into() }),
                AgentContentBlock::ToolUse(ToolUseBlock {
                    id: "t1".into(),
                    name: "bash".into(),
                    input: json::object([("command", json::string("ls"))]),
                }),
            ]),
            token_count: 0,
            elapsed_seconds: 0.0,
        },
        AgentMessage {
            role: "user".into(),
            content: AgentMessageContent::Blocks(vec![AgentContentBlock::ToolResult(
                ToolResultBlock {
                    tool_use_id: "t1".into(),
                    content: "result output".into(),
                    is_error: false,
                },
            )]),
            token_count: 0,
            elapsed_seconds: 0.0,
        },
    ];
    let serialized = serialize_input_messages(&messages);
    assert!(serialized.contains(r#""type":"tool_call""#));
    assert!(serialized.contains(r#""name":"bash""#));
    assert!(serialized.contains(r#""type":"tool_call_response""#));
    assert!(serialized.contains(r#""response":"result output""#));

    assert_eq!(
        serialize_output_messages("Done!", "end_turn"),
        r#"[{"finish_reason":"end_turn","parts":[{"content":"Done!","type":"text"}],"role":"assistant"}]"#
    );
    assert_eq!(
        serialize_system_instructions("You are helpful."),
        r#"[{"content":"You are helpful.","type":"text"}]"#
    );
    assert_eq!(serialize_tool_definitions(&[]), "[]");
    assert!(serialize_tool_definitions(&[ToolDefinition {
        name: "bash".into(),
        description: "Run a command".into(),
        input_schema: json::object(Vec::<(&str, iac_code_protocol::json::JsonValue)>::new()),
    }])
    .contains(r#""description":"Run a command""#));
    assert_eq!(
        serialize_tool_arguments_json(&json::object([("cmd", json::string("ls"))])),
        r#"{"cmd":"ls"}"#
    );
    assert_eq!(serialize_tool_arguments_text("raw args"), "raw args");
    assert_eq!(serialize_tool_result_text("output"), "output");
    assert!(serialize_tool_arguments_text(&"x".repeat(10000)).ends_with("...[truncated]"));
}

#[test]
fn fallback_store_writes_lists_reads_and_removes_jsonl_batches() {
    let root = unique_temp_dir("iac-code-rs-telemetry-fallback");
    let store = FallbackStore::new(root.join("telemetry"));

    let first = store
        .write(
            "iac_sess_abc",
            &[json::object([
                ("event.name", json::string("iac.test")),
                ("k", json::number(1)),
            ])],
        )
        .expect("fallback batch should be written");
    assert!(first.exists());
    assert!(first
        .file_name()
        .and_then(|name| name.to_str())
        .unwrap()
        .contains("iac_sess_abc"));
    assert_eq!(
        first.extension().and_then(|ext| ext.to_str()),
        Some("jsonl")
    );

    let second = store
        .write(
            "iac_sess_def",
            &[json::object([("event.name", json::string("iac.next"))])],
        )
        .expect("second fallback batch should be written");
    fs::write(root.join("telemetry").join("noise.txt"), "noise").expect("noise should be written");

    let pending = store.list_pending().expect("pending files should list");
    assert!(pending.contains(&first));
    assert!(pending.contains(&second));
    assert_eq!(pending.len(), 2);

    let events = store.read(&first).expect("events should read");
    assert_eq!(events.len(), 1);
    assert_eq!(
        events[0],
        json::object([
            ("event.name", json::string("iac.test")),
            ("k", json::number(1))
        ])
    );

    store.remove(&first).expect("batch should be removed");
    assert!(!first.exists());
    store
        .remove(&first)
        .expect("missing batch removal should be tolerated");

    fs::remove_dir_all(root).ok();
}

#[test]
fn identity_generates_persists_and_prefixes_ids_like_python() {
    let _guard = ENV_LOCK.lock().expect("env lock");
    std::env::remove_var("IAC_CODE_TENANT_ID");
    let root = unique_temp_dir("iac-code-rs-telemetry-identity");
    let settings_path = root.join("settings.yml");

    let mut identity = Identity::new(&settings_path, None);
    let user_id = identity.get_user_id().expect("user id should generate");
    assert!(user_id.starts_with(USER_ID_PREFIX));
    assert!(identity.was_first_run());
    assert_eq!(
        Identity::new(&settings_path, None)
            .get_user_id()
            .expect("user id should load"),
        user_id
    );
    assert!(!Identity::new(&settings_path, None).was_first_run());

    let mut injected = Identity::new(&settings_path, Some("process-level"));
    assert_eq!(
        injected.get_session_id(),
        format!("{SESSION_ID_PREFIX}process-level")
    );

    std::env::set_var("IAC_CODE_TENANT_ID", "  acme  ");
    let tenant = format!("{TENANT_ID_PREFIX}acme");
    assert_eq!(injected.get_tenant_id().as_deref(), Some(tenant.as_str()));
    std::env::set_var("IAC_CODE_TENANT_ID", "iac_tenant_acme");
    assert_eq!(injected.get_tenant_id().as_deref(), Some("iac_tenant_acme"));
    std::env::remove_var("IAC_CODE_TENANT_ID");

    fs::remove_dir_all(root).ok();
}

#[test]
fn identity_persists_user_id_without_clobbering_settings_like_python() {
    let root = unique_temp_dir("iac-code-rs-telemetry-identity-preserve-settings");
    fs::create_dir_all(&root).expect("settings dir should be created");
    let settings_path = root.join("settings.yml");
    fs::write(
        &settings_path,
        "activeProvider: openapi_compatible\nproviders:\n  openapi_compatible:\n    model: fixture-model\n",
    )
    .expect("settings should be written");

    let mut identity = Identity::new(&settings_path, None);
    let user_id = identity.get_user_id().expect("user id should generate");
    let settings = fs::read_to_string(&settings_path).expect("settings should be readable");

    assert!(settings.contains("activeProvider: openapi_compatible"));
    assert!(settings.contains("providers:"));
    assert!(settings.contains("openapi_compatible:"));
    assert!(settings.contains("model: fixture-model"));
    assert!(settings.contains(&format!("userID: {user_id}")));

    fs::remove_dir_all(root).ok();
}

#[test]
fn identity_session_override_scopes_like_python_use_session_id() {
    let root = unique_temp_dir("iac-code-rs-telemetry-session-override");
    let settings_path = root.join("settings.yml");
    let mut identity = Identity::new(&settings_path, Some("process-level"));

    assert_eq!(
        identity.get_session_id(),
        format!("{SESSION_ID_PREFIX}process-level")
    );
    {
        let _override = use_session_id("request-level").expect("override should be accepted");
        assert_eq!(
            identity.get_session_id(),
            format!("{SESSION_ID_PREFIX}request-level")
        );
        {
            let _nested =
                use_session_id("iac_sess_nested").expect("prefixed override should be accepted");
            assert_eq!(identity.get_session_id(), "iac_sess_nested");
        }
        assert_eq!(
            identity.get_session_id(),
            format!("{SESSION_ID_PREFIX}request-level")
        );
    }
    assert_eq!(
        identity.get_session_id(),
        format!("{SESSION_ID_PREFIX}process-level")
    );
    assert!(use_session_id("").is_err());

    fs::remove_dir_all(root).ok();
}

fn clear_telemetry_env() {
    std::env::remove_var("DISABLE_TELEMETRY");
    std::env::remove_var("IAC_CODE_DISABLE_NONESSENTIAL_TRAFFIC");
    std::env::remove_var("OTEL_INSTRUMENTATION_GENAI_CAPTURE_MESSAGE_CONTENT");
}

fn unique_temp_dir(prefix: &str) -> PathBuf {
    let nanos = SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .expect("system clock should be after unix epoch")
        .as_nanos();
    std::env::temp_dir().join(format!("{prefix}-{}-{nanos}", std::process::id()))
}
