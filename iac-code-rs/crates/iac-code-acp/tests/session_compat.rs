use iac_code_acp::convert::{AcpContentBlock, SessionUpdate};
use iac_code_acp::permissions::{
    PermissionOption, PermissionOutcome, PermissionResponse, PermissionToolCall,
    OPTION_ALLOW_ALWAYS, OPTION_ALLOW_ONCE, OPTION_REJECT_ALWAYS, OPTION_REJECT_ONCE,
    PREFIX_ALLOW_RULE, PREFIX_DENY_RULE,
};
use iac_code_acp::session::{
    convert_mcp_server_configs, AcpAgent, AcpClient, AcpMcpServerConfig, AcpSession, CompactResult,
    CompactStatus, MemoryEntry, PermissionDecision, RenameOutcome,
};
use iac_code_protocol::json::{self, JsonValue};
use iac_code_protocol::message::{AgentContentBlock, AgentMessageContent, ImageBlock, TextBlock};
use iac_code_protocol::permission::ToolPermissionContext;
use iac_code_protocol::{
    MessageEndEvent, PermissionRequestEvent, StreamEvent, TextDeltaEvent, Usage,
};
use std::collections::BTreeMap;
use std::fs;
use std::path::PathBuf;
use std::sync::{Arc, Mutex, MutexGuard};
use std::time::{SystemTime, UNIX_EPOCH};

static ENV_LOCK: Mutex<()> = Mutex::new(());

#[derive(Default)]
struct RecordingClient {
    permission_outcomes: Vec<PermissionResponse>,
    permission_requests: Vec<RecordedPermissionRequest>,
    updates: Vec<(String, SessionUpdate)>,
}

#[derive(Clone, Debug)]
struct RecordedPermissionRequest {
    session_id: String,
    options: Vec<PermissionOption>,
    tool_call: PermissionToolCall,
}

impl RecordingClient {
    fn new(outcomes: Vec<PermissionResponse>) -> Self {
        Self {
            permission_outcomes: outcomes,
            permission_requests: Vec::new(),
            updates: Vec::new(),
        }
    }
}

impl AcpClient for RecordingClient {
    fn session_update(&mut self, session_id: &str, update: SessionUpdate) {
        self.updates.push((session_id.to_owned(), update));
    }

    fn request_permission(
        &mut self,
        session_id: &str,
        options: Vec<PermissionOption>,
        tool_call: PermissionToolCall,
    ) -> PermissionResponse {
        self.permission_requests.push(RecordedPermissionRequest {
            session_id: session_id.to_owned(),
            options,
            tool_call,
        });
        self.permission_outcomes.remove(0)
    }
}

struct EchoAgent;

impl AcpAgent for EchoAgent {
    fn run_streaming(
        &mut self,
        prompt: &str,
        _request_permission: &mut dyn FnMut(PermissionRequestEvent) -> PermissionDecision,
    ) -> Vec<StreamEvent> {
        vec![
            StreamEvent::TextDelta(TextDeltaEvent {
                text: format!("echo: {prompt}"),
            }),
            StreamEvent::MessageEnd(MessageEndEvent {
                stop_reason: "stop".into(),
                usage: Usage {
                    input_tokens: 3,
                    output_tokens: 5,
                    cache_creation_input_tokens: 0,
                    cache_read_input_tokens: 0,
                },
            }),
        ]
    }
}

struct ContentRecordingAgent {
    seen: Arc<Mutex<Option<AgentMessageContent>>>,
}

impl AcpAgent for ContentRecordingAgent {
    fn run_streaming(
        &mut self,
        _prompt: &str,
        _request_permission: &mut dyn FnMut(PermissionRequestEvent) -> PermissionDecision,
    ) -> Vec<StreamEvent> {
        panic!("expected structured content path")
    }

    fn run_streaming_content(
        &mut self,
        content: AgentMessageContent,
        _prompt_text: &str,
        _request_permission: &mut dyn FnMut(PermissionRequestEvent) -> PermissionDecision,
    ) -> Vec<StreamEvent> {
        *self.seen.lock().expect("seen lock") = Some(content);
        vec![StreamEvent::MessageEnd(MessageEndEvent {
            stop_reason: "stop".into(),
            usage: Usage::default(),
        })]
    }
}

#[test]
fn session_dynamic_config_merges_and_returns_independent_snapshot() {
    let mut session = AcpSession::new("config-session", EchoAgent);

    session.update_config(BTreeMap::from([(
        "temperature".to_owned(),
        json::string("0.5"),
    )]));
    session.update_config(BTreeMap::from([
        ("max_tokens".to_owned(), json::number(2048)),
        ("temperature".to_owned(), json::string("0.9")),
    ]));

    let mut snapshot = session.config();
    assert_eq!(snapshot.get("temperature"), Some(&json::string("0.9")));
    assert_eq!(snapshot.get("max_tokens"), Some(&json::number(2048)));

    snapshot.insert("extra".to_owned(), json::string("value"));
    assert!(!session.config().contains_key("extra"));
}

#[test]
fn session_prompt_streams_updates_and_tracks_turns() {
    let mut session = AcpSession::new("s1", EchoAgent);
    let mut client = RecordingClient::default();

    let first = session
        .prompt(
            vec![AcpContentBlock::Text {
                text: "hello".into(),
            }],
            &mut client,
        )
        .expect("prompt succeeds");
    let first_turn = session.current_turn_id().expect("turn id").to_owned();

    assert_eq!(first.stop_reason, "end_turn");
    let timing = first
        .field_meta
        .get("timing")
        .expect("prompt response should include timing metadata");
    assert!(
        matches!(
            json_field(timing, "elapsed_ms"),
            Some(iac_code_protocol::json::JsonValue::Number(_))
        ),
        "timing metadata should include elapsed_ms: {timing:?}"
    );
    assert_eq!(first.field_meta.get("usage"), Some(&usage_meta(3, 5, 8)));
    assert_eq!(client.updates[0].0, "s1");
    assert_eq!(
        client.updates[0].1,
        SessionUpdate::agent_message("echo: hello")
    );

    session
        .prompt(
            vec![AcpContentBlock::Text {
                text: "again".into(),
            }],
            &mut client,
        )
        .expect("prompt succeeds");
    assert_ne!(session.current_turn_id().expect("turn id"), first_turn);
}

#[test]
fn session_prompt_passes_image_blocks_to_agent_content() {
    let seen = Arc::new(Mutex::new(None));
    let agent = ContentRecordingAgent { seen: seen.clone() };
    let mut session = AcpSession::new("s1", agent);
    let mut client = RecordingClient::default();

    session
        .prompt(
            vec![
                AcpContentBlock::Text {
                    text: "describe".into(),
                },
                AcpContentBlock::Image {
                    mime_type: "image/png".into(),
                    data: "base64-image".into(),
                },
            ],
            &mut client,
        )
        .expect("prompt succeeds");

    assert_eq!(
        seen.lock().expect("seen lock").clone(),
        Some(AgentMessageContent::Blocks(vec![
            AgentContentBlock::Text(TextBlock {
                text: "describe".into(),
            }),
            AgentContentBlock::Image(ImageBlock {
                media_type: "image/png".into(),
                data: "base64-image".into(),
            }),
        ]))
    );
}

#[test]
fn session_stores_acp_mcp_server_configs_like_python() {
    let session = AcpSession::new("s1", EchoAgent);
    assert!(session.mcp_configs().is_empty());

    let configs = vec![AcpMcpServerConfig::Stdio {
        name: "local-tools".into(),
        command: "uvx".into(),
        args: vec!["mcp-server".into()],
        env: [("TOKEN".to_owned(), "secret".to_owned())].into(),
    }];
    let session = AcpSession::new("s2", EchoAgent).with_mcp_configs(configs.clone());

    assert_eq!(session.mcp_configs(), configs.as_slice());
}

#[test]
fn acp_mcp_server_conversion_matches_python_internal_dict_shape() {
    let payload = json::array([
        json::object([
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
        ]),
        json::object([
            ("type", json::string("http")),
            ("name", json::string("remote-http")),
            ("url", json::string("https://example.com/mcp")),
            (
                "headers",
                json::array([json::object([
                    ("name", json::string("Authorization")),
                    ("value", json::string("Bearer token")),
                ])]),
            ),
        ]),
        json::object([
            ("type", json::string("sse")),
            ("name", json::string("remote-sse")),
            ("url", json::string("https://example.com/sse")),
        ]),
        json::object([("type", json::string("unsupported"))]),
    ]);

    let configs = convert_mcp_server_configs(Some(&payload));

    assert_eq!(
        configs,
        vec![
            AcpMcpServerConfig::Stdio {
                name: "local-tools".into(),
                command: "uvx".into(),
                args: vec!["mcp-server".into()],
                env: [("TOKEN".to_owned(), "secret".to_owned())].into(),
            },
            AcpMcpServerConfig::Http {
                name: "remote-http".into(),
                type_name: "http".into(),
                url: "https://example.com/mcp".into(),
                headers: [("Authorization".to_owned(), "Bearer token".to_owned())].into(),
            },
            AcpMcpServerConfig::Http {
                name: "remote-sse".into(),
                type_name: "sse".into(),
                url: "https://example.com/sse".into(),
                headers: Default::default(),
            },
        ]
    );
}

#[test]
fn session_intercepts_unsupported_slash_commands_without_running_agent() {
    let _env = english_locale_env();

    #[derive(Default)]
    struct CountingAgent {
        calls: usize,
    }

    impl AcpAgent for CountingAgent {
        fn run_streaming(
            &mut self,
            _prompt: &str,
            _request_permission: &mut dyn FnMut(PermissionRequestEvent) -> PermissionDecision,
        ) -> Vec<StreamEvent> {
            self.calls += 1;
            Vec::new()
        }
    }

    let mut session = AcpSession::new("slash-session", CountingAgent::default());
    let mut client = RecordingClient::default();

    let response = session
        .prompt(
            vec![AcpContentBlock::Text {
                text: "/help".into(),
            }],
            &mut client,
        )
        .expect("slash command succeeds");

    assert_eq!(response.stop_reason, "end_turn");
    assert_eq!(session.agent().calls, 0);
    assert_eq!(client.updates.len(), 1);
    let text = client.updates[0].1.content_text().expect("slash response");
    assert!(text.contains("Command '/help' is not supported over ACP"));
    assert!(text.contains("/clear"));
    assert!(text.contains("/compact"));
}

#[test]
fn session_unsupported_slash_command_uses_chinese_locale_like_python_acp() {
    let _env = EnvGuard::set_many(&[
        ("LANGUAGE", Some("zh")),
        ("LC_ALL", Some("zh_CN.UTF-8")),
        ("LANG", Some("zh_CN.UTF-8")),
    ]);

    #[derive(Default)]
    struct CountingAgent {
        calls: usize,
    }

    impl AcpAgent for CountingAgent {
        fn run_streaming(
            &mut self,
            _prompt: &str,
            _request_permission: &mut dyn FnMut(PermissionRequestEvent) -> PermissionDecision,
        ) -> Vec<StreamEvent> {
            self.calls += 1;
            Vec::new()
        }
    }

    let mut session = AcpSession::new("slash-session", CountingAgent::default());
    let mut client = RecordingClient::default();

    let response = session
        .prompt(
            vec![AcpContentBlock::Text {
                text: "/help".into(),
            }],
            &mut client,
        )
        .expect("slash command succeeds");

    assert_eq!(response.stop_reason, "end_turn");
    assert_eq!(session.agent().calls, 0);
    assert_eq!(client.updates.len(), 1);
    let text = client.updates[0].1.content_text().expect("slash response");
    assert!(
        text.contains("命令 '/help' 不支持通过 ACP 使用。支持的命令："),
        "{text}"
    );
    assert!(text.contains("/clear"), "{text}");
    assert!(!text.contains("Command '/help' is not supported"), "{text}");
}

#[test]
fn session_debug_and_compact_slash_use_chinese_locale_like_python_acp() {
    let _env = EnvGuard::set_many(&[
        ("LANGUAGE", Some("zh")),
        ("LC_ALL", Some("zh_CN.UTF-8")),
        ("LANG", Some("zh_CN.UTF-8")),
    ]);

    struct CompactAgent;

    impl AcpAgent for CompactAgent {
        fn run_streaming(
            &mut self,
            _prompt: &str,
            _request_permission: &mut dyn FnMut(PermissionRequestEvent) -> PermissionDecision,
        ) -> Vec<StreamEvent> {
            Vec::new()
        }

        fn compact(&mut self) -> Result<CompactResult, String> {
            Ok(CompactResult {
                status: CompactStatus::Empty,
                original_tokens: 0,
                compacted_tokens: 0,
                preserve_recent_turns: 2,
            })
        }
    }

    let mut session = AcpSession::new("slash-session", CompactAgent);
    let mut client = RecordingClient::default();
    for text in ["/debug off", "/debug", "/debug invalid", "/compact"] {
        session
            .prompt(
                vec![AcpContentBlock::Text { text: text.into() }],
                &mut client,
            )
            .expect("slash command succeeds");
    }

    let messages = client
        .updates
        .iter()
        .map(|(_, update)| update.content_text().expect("slash response").to_owned())
        .collect::<Vec<_>>();
    assert_eq!(
        messages,
        vec![
            "调试日志已关闭。",
            "调试日志已关闭。",
            "用法：/debug [on|off]",
            "无内容可压缩：对话为空。",
        ]
    );
}

#[test]
fn session_memory_clear_and_rename_slash_use_chinese_locale_like_python_acp() {
    let _env = EnvGuard::set_many(&[
        ("LANGUAGE", Some("zh")),
        ("LC_ALL", Some("zh_CN.UTF-8")),
        ("LANG", Some("zh_CN.UTF-8")),
    ]);

    struct SlashI18nAgent;

    impl AcpAgent for SlashI18nAgent {
        fn run_streaming(
            &mut self,
            _prompt: &str,
            _request_permission: &mut dyn FnMut(PermissionRequestEvent) -> PermissionDecision,
        ) -> Vec<StreamEvent> {
            Vec::new()
        }

        fn reset(&mut self) -> Result<(), String> {
            Ok(())
        }

        fn memory_entries(&self) -> Option<Vec<MemoryEntry>> {
            Some(Vec::new())
        }

        fn delete_memory(&mut self, _name: &str) -> Result<bool, String> {
            Ok(false)
        }

        fn rename_session(&mut self, name: &str) -> Result<RenameOutcome, String> {
            Ok(if name == "same" {
                RenameOutcome::Unchanged
            } else {
                RenameOutcome::Renamed
            })
        }
    }

    let mut session = AcpSession::new("slash-session", SlashI18nAgent);
    let mut client = RecordingClient::default();
    for text in [
        "/clear",
        "/memory",
        "/memory search none",
        "/memory missing",
        "/memory delete missing",
        "/memory help",
        "/rename",
        "/rename same",
        "/rename next",
    ] {
        session
            .prompt(
                vec![AcpContentBlock::Text { text: text.into() }],
                &mut client,
            )
            .expect("slash command succeeds");
    }

    let messages = client
        .updates
        .iter()
        .map(|(_, update)| update.content_text().expect("slash response").to_owned())
        .collect::<Vec<_>>();
    assert_eq!(
        messages,
        vec![
            "对话历史已清除。",
            "尚未保存任何记忆。",
            "没有匹配的记忆。",
            "未找到记忆 'missing'。",
            "未找到记忆 'missing'。",
            "用法：/memory [<名称>|search <查询>|delete <名称>|help]",
            "用法：/rename <名称>",
            "会话已命名为 same",
            "已将会话重命名为 next",
        ]
    );
}

#[test]
fn session_default_agent_slash_errors_use_chinese_locale_like_python_acp() {
    let _env = EnvGuard::set_many(&[
        ("LANGUAGE", Some("zh")),
        ("LC_ALL", Some("zh_CN.UTF-8")),
        ("LANG", Some("zh_CN.UTF-8")),
    ]);

    struct DefaultSlashAgent;

    impl AcpAgent for DefaultSlashAgent {
        fn run_streaming(
            &mut self,
            _prompt: &str,
            _request_permission: &mut dyn FnMut(PermissionRequestEvent) -> PermissionDecision,
        ) -> Vec<StreamEvent> {
            Vec::new()
        }
    }

    let mut session = AcpSession::new("slash-session", DefaultSlashAgent);
    let mut client = RecordingClient::default();
    for text in [
        "/memory search none",
        "/memory missing",
        "/memory delete missing",
        "/rename renamed",
    ] {
        session
            .prompt(
                vec![AcpContentBlock::Text { text: text.into() }],
                &mut client,
            )
            .expect("slash command succeeds");
    }

    let messages = client
        .updates
        .iter()
        .map(|(_, update)| update.content_text().expect("slash response").to_owned())
        .collect::<Vec<_>>();
    assert_eq!(
        messages,
        vec![
            "记忆管理器不可用。",
            "记忆管理器不可用。",
            "记忆管理器不可用。",
            "创建会话后才能重命名。",
        ]
    );
}

#[test]
fn session_executes_supported_slash_commands() {
    let _env = english_locale_env();

    #[derive(Default)]
    struct SlashAgent {
        streaming_calls: usize,
        reset_calls: usize,
    }

    impl AcpAgent for SlashAgent {
        fn run_streaming(
            &mut self,
            _prompt: &str,
            _request_permission: &mut dyn FnMut(PermissionRequestEvent) -> PermissionDecision,
        ) -> Vec<StreamEvent> {
            self.streaming_calls += 1;
            Vec::new()
        }

        fn reset(&mut self) -> Result<(), String> {
            self.reset_calls += 1;
            Ok(())
        }

        fn memory_entries(&self) -> Option<Vec<iac_code_acp::session::MemoryEntry>> {
            Some(vec![iac_code_acp::session::MemoryEntry {
                name: "user-role".into(),
                memory_type: "user".into(),
                description: "Role".into(),
                content: "Senior engineer".into(),
            }])
        }
    }

    let mut session = AcpSession::new("slash-session", SlashAgent::default());
    let mut client = RecordingClient::default();

    session
        .prompt(
            vec![AcpContentBlock::Text {
                text: "/clear".into(),
            }],
            &mut client,
        )
        .expect("clear succeeds");
    session
        .prompt(
            vec![AcpContentBlock::Text {
                text: "/memory".into(),
            }],
            &mut client,
        )
        .expect("memory succeeds");

    assert_eq!(session.agent().streaming_calls, 0);
    assert_eq!(session.agent().reset_calls, 1);
    assert_eq!(
        client.updates[0].1.content_text(),
        Some("Conversation history cleared.")
    );
    assert_eq!(
        client.updates[1].1.content_text(),
        Some("Saved memories:\n  - user-role - Role")
    );
}

#[test]
fn acp_memory_slash_command_matches_python_list_view_search_delete_and_help() {
    let _env = english_locale_env();

    #[derive(Default)]
    struct MemorySlashAgent {
        memories: Vec<MemoryEntry>,
    }

    impl AcpAgent for MemorySlashAgent {
        fn run_streaming(
            &mut self,
            _prompt: &str,
            _request_permission: &mut dyn FnMut(PermissionRequestEvent) -> PermissionDecision,
        ) -> Vec<StreamEvent> {
            Vec::new()
        }

        fn memory_entries(&self) -> Option<Vec<MemoryEntry>> {
            Some(self.memories.clone())
        }

        fn delete_memory(&mut self, name: &str) -> Result<bool, String> {
            let Some(index) = self.memories.iter().position(|memory| memory.name == name) else {
                return Ok(false);
            };
            self.memories.remove(index);
            Ok(true)
        }
    }

    let mut session = AcpSession::new(
        "memory-session",
        MemorySlashAgent {
            memories: vec![
                MemoryEntry {
                    name: "user-role".into(),
                    memory_type: "user".into(),
                    description: "Role".into(),
                    content: "Senior engineer".into(),
                },
                MemoryEntry {
                    name: "feedback-testing".into(),
                    memory_type: "feedback".into(),
                    description: "Testing".into(),
                    content: "Prefer integration tests.".into(),
                },
            ],
        },
    );
    let mut client = RecordingClient::default();

    for prompt in [
        "/memory",
        "/memory user-role",
        "/memory search integration",
        "/memory delete user-role",
        "/memory user-role",
        "/memory help",
        "/memory remove user-role",
        "/memory search",
    ] {
        session
            .prompt(
                vec![AcpContentBlock::Text {
                    text: prompt.into(),
                }],
                &mut client,
            )
            .expect("memory slash command succeeds");
    }

    let messages = client
        .updates
        .iter()
        .map(|(_, update)| update.content_text().expect("agent message"))
        .collect::<Vec<_>>();

    assert_eq!(
        messages,
        vec![
            "Saved memories:\n  - feedback-testing - Testing\n  - user-role - Role",
            "[user] Role\n\nSenior engineer",
            "Matching memories:\n  - feedback-testing - Testing",
            "Memory 'user-role' deleted.",
            "Memory 'user-role' not found.",
            "Usage: /memory [<name>|search <query>|delete <name>|help]",
            "Usage: /memory [<name>|search <query>|delete <name>|help]",
            "Usage: /memory [<name>|search <query>|delete <name>|help]",
        ]
    );
}

#[test]
fn session_debug_slash_command_uses_config_log_file() {
    #[derive(Default)]
    struct SlashAgent;

    impl AcpAgent for SlashAgent {
        fn run_streaming(
            &mut self,
            _prompt: &str,
            _request_permission: &mut dyn FnMut(PermissionRequestEvent) -> PermissionDecision,
        ) -> Vec<StreamEvent> {
            Vec::new()
        }
    }

    let config_dir = unique_temp_dir("iac-code-rs-acp-debug");
    fs::create_dir_all(&config_dir).expect("config dir should be created");
    let config_dir_text = config_dir.to_string_lossy().into_owned();
    let _env = EnvGuard::set_many(&[
        ("IAC_CODE_CONFIG_DIR", Some(config_dir_text.as_str())),
        ("LANGUAGE", Some("en_US.UTF-8")),
        ("LC_ALL", Some("en_US.UTF-8")),
        ("LC_MESSAGES", Some("en_US.UTF-8")),
        ("LANG", Some("en_US.UTF-8")),
    ]);

    let mut session = AcpSession::new("slash-session", SlashAgent);
    let mut client = RecordingClient::default();

    session
        .prompt(
            vec![AcpContentBlock::Text {
                text: "/debug on".into(),
            }],
            &mut client,
        )
        .expect("debug on succeeds");
    session
        .prompt(
            vec![AcpContentBlock::Text {
                text: "/debug".into(),
            }],
            &mut client,
        )
        .expect("debug status succeeds");
    session
        .prompt(
            vec![AcpContentBlock::Text {
                text: "/debug off".into(),
            }],
            &mut client,
        )
        .expect("debug off succeeds");

    let enabled_path = client.updates[0]
        .1
        .content_text()
        .and_then(|text| text.strip_prefix("Debug logging enabled. Log file: "))
        .map(PathBuf::from)
        .expect("enabled debug path");
    let status_path = client.updates[1]
        .1
        .content_text()
        .and_then(|text| text.strip_prefix("Debug logging is on. Log file: "))
        .map(PathBuf::from)
        .expect("status debug path");
    let expected_log_dir = config_dir
        .join("logs")
        .canonicalize()
        .expect("logs dir should canonicalize");

    assert_eq!(enabled_path, status_path);
    assert!(enabled_path.starts_with(expected_log_dir));
    assert_eq!(
        enabled_path.file_name().and_then(|name| name.to_str()),
        Some("acp.log")
    );
    assert!(enabled_path.exists(), "debug log file should exist");
    assert_eq!(
        client.updates[2].1.content_text(),
        Some("Debug logging disabled.")
    );

    fs::remove_dir_all(&config_dir).ok();
}

struct PermissionAgent {
    requests: Vec<PermissionRequestEvent>,
}

fn unique_temp_dir(prefix: &str) -> PathBuf {
    let nanos = SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .unwrap_or_default()
        .as_nanos();
    std::env::temp_dir().join(format!("{prefix}-{}-{nanos}", std::process::id()))
}

impl PermissionAgent {
    fn new(requests: Vec<PermissionRequestEvent>) -> Self {
        Self { requests }
    }
}

impl AcpAgent for PermissionAgent {
    fn run_streaming(
        &mut self,
        _prompt: &str,
        request_permission: &mut dyn FnMut(PermissionRequestEvent) -> PermissionDecision,
    ) -> Vec<StreamEvent> {
        let mut events = Vec::new();
        for request in self.requests.clone() {
            let allowed = matches!(request_permission(request), PermissionDecision::Allow);
            events.push(StreamEvent::TextDelta(TextDeltaEvent {
                text: if allowed {
                    "executed".into()
                } else {
                    "denied".into()
                },
            }));
        }
        events
    }
}

#[test]
fn permission_request_options_include_rule_suggestions_and_apply_allow_rule() {
    let request = PermissionRequestEvent {
        tool_name: "bash".into(),
        tool_input: json::object([("command", json::string("git status"))]),
        tool_use_id: "tool-1".into(),
        permission_result: Some(permission_result_with_suggestions([("bash", "git:*")])),
    };
    let mut session = AcpSession::new("perm-session", PermissionAgent::new(vec![request]));
    session.set_permission_context(Some(ToolPermissionContext::default()));
    let mut client = RecordingClient::new(vec![PermissionResponse {
        outcome: PermissionOutcome::Allowed {
            option_id: Some(format!("{PREFIX_ALLOW_RULE}git:*")),
        },
        field_meta: Default::default(),
    }]);

    session
        .prompt(
            vec![AcpContentBlock::Text { text: "go".into() }],
            &mut client,
        )
        .expect("prompt succeeds");

    assert_eq!(client.permission_requests.len(), 1);
    let recorded = &client.permission_requests[0];
    assert_eq!(recorded.session_id, "perm-session");
    assert_eq!(recorded.tool_call.tool_call_id, "permission/tool-1");
    assert_eq!(recorded.tool_call.title, "bash");
    assert!(recorded.tool_call.content[0].contains("Suggested rule: git:*"));

    let option_ids: Vec<&str> = recorded
        .options
        .iter()
        .map(|option| option.option_id.as_str())
        .collect();
    assert_eq!(
        option_ids,
        vec![
            OPTION_ALLOW_ONCE,
            "allow_rule:git:*",
            OPTION_REJECT_ONCE,
            "deny_rule:git:*",
            OPTION_REJECT_ALWAYS,
        ]
    );

    let ctx = session.permission_context().expect("permission context");
    assert_eq!(
        ctx.allow_rules.get("session").expect("session allow rules"),
        &vec!["bash(git:*)".to_owned()]
    );
}

#[test]
fn permission_request_applies_deny_rule_to_session_context() {
    let request = PermissionRequestEvent {
        tool_name: "bash".into(),
        tool_input: json::object([("command", json::string("curl http://example.com"))]),
        tool_use_id: "tool-1".into(),
        permission_result: Some(permission_result_with_suggestions([("bash", "curl:*")])),
    };
    let mut session = AcpSession::new("perm-session", PermissionAgent::new(vec![request]));
    session.set_permission_context(Some(ToolPermissionContext::default()));
    let mut client = RecordingClient::new(vec![PermissionResponse {
        outcome: PermissionOutcome::Denied {
            option_id: Some(format!("{PREFIX_DENY_RULE}curl:*")),
        },
        field_meta: Default::default(),
    }]);

    session
        .prompt(
            vec![AcpContentBlock::Text { text: "go".into() }],
            &mut client,
        )
        .expect("prompt succeeds");

    let ctx = session.permission_context().expect("permission context");
    assert_eq!(
        ctx.deny_rules.get("session").expect("session deny rules"),
        &vec!["bash(curl:*)".to_owned()]
    );
}

#[test]
fn permission_cache_is_per_session_lru_and_skips_repeated_requests() {
    let first = permission_request("write_file", "tool-1");
    let second = permission_request("write_file", "tool-2");
    let mut session = AcpSession::new("s1", PermissionAgent::new(vec![first, second]));
    session.set_permission_cache_max_size(2);
    let mut client = RecordingClient::new(vec![PermissionResponse {
        outcome: PermissionOutcome::Allowed {
            option_id: Some(OPTION_ALLOW_ALWAYS.into()),
        },
        field_meta: Default::default(),
    }]);

    session
        .prompt(
            vec![AcpContentBlock::Text { text: "go".into() }],
            &mut client,
        )
        .expect("prompt succeeds");

    assert_eq!(client.permission_requests.len(), 1);
    assert_eq!(
        session.permission_cache_snapshot(),
        vec![("write_file".to_owned(), "always_allow".to_owned())]
    );

    let other = AcpSession::new("s2", PermissionAgent::new(Vec::new()));
    assert!(other.permission_cache_snapshot().is_empty());

    session.cache_permission("tool_a", "always_allow");
    session.cache_permission("tool_b", "always_deny");
    session.cache_permission("tool_a", "always_allow");
    session.cache_permission("tool_c", "always_allow");
    assert_eq!(
        session.permission_cache_snapshot(),
        vec![
            ("tool_a".to_owned(), "always_allow".to_owned()),
            ("tool_c".to_owned(), "always_allow".to_owned()),
        ]
    );
}

#[test]
fn permission_request_omits_blanket_allow_when_tool_disables_it() {
    let mut session = AcpSession::new(
        "s1",
        PermissionAgent::new(vec![permission_request("read_file", "tool-1")]),
    );
    session.disable_blanket_allow("read_file");
    let mut client = RecordingClient::new(vec![PermissionResponse {
        outcome: PermissionOutcome::Denied {
            option_id: Some(OPTION_REJECT_ONCE.into()),
        },
        field_meta: Default::default(),
    }]);

    session
        .prompt(
            vec![AcpContentBlock::Text { text: "go".into() }],
            &mut client,
        )
        .expect("prompt succeeds");

    let option_ids: Vec<&str> = client.permission_requests[0]
        .options
        .iter()
        .map(|option| option.option_id.as_str())
        .collect();
    assert_eq!(
        option_ids,
        vec![OPTION_ALLOW_ONCE, OPTION_REJECT_ONCE, OPTION_REJECT_ALWAYS]
    );
}

#[test]
fn closed_session_rejects_future_prompts_and_clears_state() {
    let mut session = AcpSession::new("s1", EchoAgent);
    session.cache_permission("bash", "always_allow");
    session.close();
    session.close();

    assert!(session.is_closed());
    assert!(session.permission_cache_snapshot().is_empty());

    let mut client = RecordingClient::default();
    let error = session
        .prompt(
            vec![AcpContentBlock::Text {
                text: "hello".into(),
            }],
            &mut client,
        )
        .expect_err("closed session rejects prompts");
    assert_eq!(error.to_string(), "Session is closed");
}

fn permission_request(tool_name: &str, tool_use_id: &str) -> PermissionRequestEvent {
    PermissionRequestEvent {
        tool_name: tool_name.into(),
        tool_input: json::object([("path", json::string("main.tf"))]),
        tool_use_id: tool_use_id.into(),
        permission_result: None,
    }
}

fn permission_result_with_suggestions<const N: usize>(items: [(&str, &str); N]) -> JsonValue {
    json::object([(
        "suggestions",
        json::array(items.into_iter().map(|(tool_name, rule_content)| {
            json::object([
                ("tool_name", json::string(tool_name)),
                ("rule_content", json::string(rule_content)),
            ])
        })),
    )])
}

fn usage_meta(input_tokens: u64, output_tokens: u64, total_tokens: u64) -> JsonValue {
    json::object([
        ("input_tokens", json::number(input_tokens)),
        ("output_tokens", json::number(output_tokens)),
        ("total_tokens", json::number(total_tokens)),
    ])
}

fn json_field<'a>(value: &'a JsonValue, key: &str) -> Option<&'a JsonValue> {
    match value {
        JsonValue::Object(fields) => fields.get(key),
        _ => None,
    }
}

struct EnvGuard {
    _guard: MutexGuard<'static, ()>,
    previous: Vec<(&'static str, Option<String>)>,
}

impl EnvGuard {
    fn set_many(entries: &[(&'static str, Option<&str>)]) -> Self {
        let guard = ENV_LOCK.lock().expect("env lock should not be poisoned");
        let previous = entries
            .iter()
            .map(|(key, _)| (*key, std::env::var(key).ok()))
            .collect::<Vec<_>>();
        for (key, value) in entries {
            match value {
                Some(value) => std::env::set_var(key, value),
                None => std::env::remove_var(key),
            }
        }
        Self {
            _guard: guard,
            previous,
        }
    }
}

impl Drop for EnvGuard {
    fn drop(&mut self) {
        for (key, value) in self.previous.iter().rev() {
            match value {
                Some(value) => std::env::set_var(key, value),
                None => std::env::remove_var(key),
            }
        }
    }
}

fn english_locale_env() -> EnvGuard {
    EnvGuard::set_many(&[
        ("LANGUAGE", Some("en_US.UTF-8")),
        ("LC_ALL", Some("en_US.UTF-8")),
        ("LC_MESSAGES", Some("en_US.UTF-8")),
        ("LANG", Some("en_US.UTF-8")),
    ])
}
