use std::fs;
use std::net::TcpListener;
use std::sync::Arc;
use std::thread;

use iac_code_config::cloud_credentials::AliyunCredential;
use iac_code_core::SessionStorage;
use iac_code_exec::OutputFormat;
use iac_code_protocol::json;
use iac_code_protocol::message::{AgentMessageContent, Conversation};
use iac_code_providers::ConfiguredProvider;
use iac_code_tools::{
    AgentProgress, SkillManager, SubAgentRequest, SubAgentResult, SubAgentRunner, ToolCallRequest,
    ToolExecutor,
};

use crate::debug_logging::enable_interactive_debug_log;
use crate::headless_executor::{build_headless_tool_executor, HeadlessToolExecutorOptions};
use crate::headless_runner::{run_configured_headless, ConfiguredHeadlessOptions};
use crate::provider_config::load_configured_provider;
use crate::test_support::{
    accept_test_with_timeout, paths_for, read_test_http_request, unique_temp_dir,
    write_test_http_response, EnvVarGuard,
};

#[test]
fn configured_headless_trusts_resume_session_tool_result_directories_like_python() {
    let config_dir = unique_temp_dir("iac-code-rs-configured-trusted-tool-results-config");
    let workspace_dir = unique_temp_dir("iac-code-rs-configured-trusted-tool-results-workspace");
    fs::create_dir_all(&config_dir).expect("config dir should be created");
    fs::create_dir_all(&workspace_dir).expect("workspace dir should be created");
    let cwd = workspace_dir.to_string_lossy().to_string();
    let _env = EnvVarGuard::set(
        "IAC_CODE_CONFIG_DIR",
        config_dir
            .to_str()
            .expect("config dir should be valid unicode"),
    );

    let listener = TcpListener::bind("127.0.0.1:0").expect("bind test server");
    listener
        .set_nonblocking(true)
        .expect("test server should be nonblocking");
    let addr = listener.local_addr().expect("server addr");
    fs::write(
            config_dir.join("settings.yml"),
            format!(
                "activeProvider: openapi_compatible\nproviders:\n  openapi_compatible:\n    apiBase: http://{addr}/v1\n    model: fixture-openapi-model\n"
            ),
        )
        .expect("settings should be written");
    fs::write(
        config_dir.join(".credentials.yml"),
        "openapi_compatible: fixture-openapi-key\n",
    )
    .expect("credentials should be written");

    let session_id = "trusted-results-session";
    let storage =
        SessionStorage::new(config_dir.join("projects")).expect("session storage should init");
    let mut previous = Conversation::default();
    previous.add_user_message(AgentMessageContent::Text("old prompt".into()));
    storage
        .save(&cwd, session_id, &previous.messages, None)
        .expect("existing session should be saved");

    let artifact_path = config_dir
        .join("tool-results")
        .join(session_id)
        .join("artifact.txt");
    fs::create_dir_all(
        artifact_path
            .parent()
            .expect("artifact should have a parent"),
    )
    .expect("artifact dir should be created");
    fs::write(&artifact_path, "trusted artifact\n").expect("trusted artifact should be written");
    let artifact_path_string = artifact_path.to_string_lossy().to_string();

    let server = thread::spawn(move || {
        let (mut first_stream, _) =
            accept_test_with_timeout(listener.try_clone().expect("clone listener"));
        let first_request = read_test_http_request(&mut first_stream);
        assert!(
            first_request.contains("\"content\":\"old prompt\""),
            "missing resumed message in first payload: {first_request}"
        );
        assert!(
            first_request.contains("\"name\":\"read_file\""),
            "missing read_file tool definition in first payload: {first_request}"
        );
        write_test_http_response(
            &mut first_stream,
            &format!(
                r#"{{
                    "id": "chatcmpl_session_tool_results_1",
                    "choices": [{{
                        "finish_reason": "tool_calls",
                        "message": {{
                            "content": null,
                            "tool_calls": [{{
                                "id": "call_read_session_result",
                                "type": "function",
                                "function": {{
                                    "name": "read_file",
                                    "arguments": "{{\"path\":\"{artifact_path_string}\"}}"
                                }}
                            }}]
                        }}
                    }}],
                    "usage": {{"prompt_tokens": 3, "completion_tokens": 4}}
                }}"#
            ),
        );

        let (mut second_stream, _) = accept_test_with_timeout(listener);
        let second_request = read_test_http_request(&mut second_stream);
        assert!(
            second_request.contains("\"tool_call_id\":\"call_read_session_result\""),
            "missing tool result call id in second payload: {second_request}"
        );
        assert!(
            second_request.contains("trusted artifact"),
            "missing trusted session artifact content in second payload: {second_request}"
        );
        write_test_http_response(
            &mut second_stream,
            r#"{
                    "id": "chatcmpl_session_tool_results_2",
                    "choices": [{
                        "finish_reason": "stop",
                        "message": {"content": "after trusted result"}
                    }],
                    "usage": {"prompt_tokens": 5, "completion_tokens": 6}
                }"#,
        );
    });

    let result = run_configured_headless(ConfiguredHeadlessOptions {
        prompt: "read previous tool result",
        prompt_content: None,
        cli_model: "",
        output_format: OutputFormat::Text,
        max_turns: 3,
        allowed_tools: "",
        disallowed_tools: "",
        permission_mode: "",
        resume: session_id,
        continue_session: false,
        verbose: false,
        cwd_override: Some(&cwd),
        initial_conversation: None,
        session_id_override: None,
        persist_session: false,
        shared_task_manager: None,
        auto_approve_permissions: false,
        permission_resolver: None,
        aliyun_credential_override: None,
    })
    .expect("headless run should complete");

    server.join().expect("server thread");
    assert_eq!(result.stdout, "after trusted result\n");

    fs::remove_dir_all(config_dir).ok();
    fs::remove_dir_all(workspace_dir).ok();
}

#[test]
fn debug_log_honors_iac_code_log_dir_like_python() {
    let root = unique_temp_dir("iac-code-rs-log-dir-env");
    let config_dir = root.join("config");
    let log_dir = root.join("custom-logs");
    fs::create_dir_all(&config_dir).expect("config dir should be created");
    let _env = EnvVarGuard::set_many(&[
        (
            "IAC_CODE_CONFIG_DIR",
            config_dir.to_str().expect("config dir should be utf8"),
        ),
        (
            "IAC_CODE_LOG_DIR",
            log_dir.to_str().expect("log dir should be utf8"),
        ),
    ]);

    let log_path = enable_interactive_debug_log("env-session").expect("debug log should open");

    assert!(log_path.starts_with(&log_dir), "{log_path:?}");
    assert_eq!(
        log_path.file_name().and_then(|name| name.to_str()),
        Some("env-session.log")
    );
    assert!(log_path.exists(), "debug log file should exist");
    assert!(
        log_dir.join("latest.log").exists(),
        "latest log link/copy should exist"
    );

    drop(_env);
    fs::remove_dir_all(root).ok();
}

#[test]
fn configured_provider_reads_provider_specific_settings_after_model_inference_like_python() {
    let root = unique_temp_dir("iac-code-rs-provider-inferred-settings");
    let config_dir = root.join("config");
    fs::create_dir_all(&config_dir).expect("config dir should be created");
    let paths = paths_for(&config_dir);
    fs::write(&paths.credentials_path, "openai: sk-test\n").expect("credentials should be written");
    fs::write(
            &paths.settings_path,
            "providers:\n  openai:\n    apiBase: https://openai.invalid/v1\n    effort: medium\n    model: gpt-5.4\n",
        )
        .expect("settings should be written");

    let (provider, model) =
        load_configured_provider(&paths, "gpt-5.5").expect("provider should load");

    assert_eq!(model, "gpt-5.5");
    let ConfiguredProvider::OpenAiCompatible(provider) = provider else {
        panic!("openai model should use openai-compatible provider");
    };
    assert_eq!(
        provider.config().base_url.as_deref(),
        Some("https://openai.invalid/v1")
    );
    assert_eq!(provider.config().effort.as_deref(), Some("medium"));

    fs::remove_dir_all(root).ok();
}

#[test]
fn headless_executor_shares_task_manager_with_background_agent() {
    let root = unique_temp_dir("iac-code-rs-background-agent");
    let config_dir = root.join("config");
    let cwd = root.join("workspace");
    fs::create_dir_all(&config_dir).expect("config dir should be created");
    fs::create_dir_all(&cwd).expect("workspace dir should be created");
    let paths = paths_for(&config_dir);
    let cwd_string = cwd.to_string_lossy().to_string();
    let runner = Arc::new(RecordingRunner::success("background done", 1, 9));
    let executor = build_headless_tool_executor(HeadlessToolExecutorOptions {
        paths: &paths,
        allowed_tools: "agent",
        disallowed_tools: "",
        permission_mode: "",
        cwd: &cwd_string,
        skill_manager: SkillManager::default(),
        sub_agent_runner: Some(runner),
        include_agent_tool: true,
        agent_definition: None,
        auto_approve_permissions: true,
        shared_task_manager: None,
        permission_resolver: None,
        session_id: None,
        aliyun_credential_override: None,
    })
    .expect("executor should build");

    let agent_result = executor.execute(ToolCallRequest {
        tool_use_id: "agent-1".into(),
        tool_name: "agent".into(),
        input: json::object([
            ("prompt", json::string("run in background")),
            ("description", json::string("Background work")),
            ("subagent_type", json::string("general-purpose")),
            ("run_in_background", json::bool_value(true)),
        ]),
    });

    assert!(!agent_result.is_error, "{agent_result:?}");
    assert!(
        agent_result
            .content
            .starts_with("Background agent launched (task_id: "),
        "{agent_result:?}"
    );

    let task_list = executor.execute(ToolCallRequest {
        tool_use_id: "tasks-1".into(),
        tool_name: "task_list".into(),
        input: json::object(Vec::<(&str, iac_code_protocol::json::JsonValue)>::new()),
    });

    assert!(!task_list.is_error, "{task_list:?}");
    assert!(task_list.content.contains("Background work"));
    assert!(task_list.content.contains("general-purpose"));

    fs::remove_dir_all(root).ok();
}

#[test]
fn headless_executor_uses_a2a_aliyun_credential_override_without_local_config() {
    let root = unique_temp_dir("iac-code-rs-a2a-aliyun-override");
    let config_dir = root.join("config");
    let cwd = root.join("workspace");
    fs::create_dir_all(&config_dir).expect("config dir should be created");
    fs::create_dir_all(&cwd).expect("workspace dir should be created");
    let paths = paths_for(&config_dir);
    let cwd_string = cwd.to_string_lossy().to_string();

    let executor = build_headless_tool_executor(HeadlessToolExecutorOptions {
        paths: &paths,
        allowed_tools: "",
        disallowed_tools: "",
        permission_mode: "",
        cwd: &cwd_string,
        skill_manager: SkillManager::default(),
        sub_agent_runner: None,
        include_agent_tool: false,
        agent_definition: None,
        auto_approve_permissions: true,
        shared_task_manager: None,
        permission_resolver: None,
        session_id: None,
        aliyun_credential_override: Some(AliyunCredential {
            mode: "AK".into(),
            access_key_id: "metadata-ak".into(),
            access_key_secret: "metadata-secret".into(),
            region_id: "cn-beijing".into(),
            ..AliyunCredential::default()
        }),
    })
    .expect("executor should build");
    let tool_names = executor
        .tool_definitions()
        .into_iter()
        .map(|tool| tool.name)
        .collect::<Vec<_>>();

    assert!(
        tool_names.contains(&"aliyun_api".to_owned()),
        "{tool_names:?}"
    );
    assert!(
        tool_names.contains(&"ros_stack".to_owned()),
        "{tool_names:?}"
    );
    assert!(
        !paths.cloud_credentials_path.exists(),
        "request-scoped metadata must not persist cloud credentials"
    );

    fs::remove_dir_all(root).ok();
}

#[derive(Clone)]
struct RecordingRunner {
    response: Result<SubAgentResult, String>,
}

impl RecordingRunner {
    fn success(output: &str, tool_use_count: u32, token_count: u32) -> Self {
        Self {
            response: Ok(SubAgentResult {
                output: output.to_owned(),
                progress: AgentProgress {
                    tool_use_count,
                    token_count,
                },
                stream_events: Vec::new(),
            }),
        }
    }
}

impl SubAgentRunner for RecordingRunner {
    fn run(&self, _request: SubAgentRequest) -> Result<SubAgentResult, String> {
        self.response.clone()
    }
}
