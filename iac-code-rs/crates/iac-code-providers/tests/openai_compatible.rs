use std::collections::BTreeMap;
use std::io::{Read, Write};
use std::net::TcpListener;
use std::sync::mpsc;
use std::thread;

use iac_code_protocol::json::{self, JsonValue};
use iac_code_protocol::message::{
    AgentContentBlock, AgentMessageContent, Conversation, TextBlock, ThinkingBlock,
    ToolResultBlock, ToolUseBlock,
};
use iac_code_protocol::provider::ToolDefinition;
use iac_code_protocol::{
    MessageEndEvent, MessageStartEvent, StreamEvent, TextDeltaEvent, ThinkingDeltaEvent,
    TombstoneEvent, ToolInputDeltaEvent, ToolUseEndEvent, ToolUseStartEvent, Usage,
};
use iac_code_providers::{
    create_provider_config, provider_descriptor, provider_keys, qwenpaw_provider_mappings,
    EventProvider, OpenAiCompatibleProvider, ProviderConfig,
};

#[test]
fn provider_registry_and_selection_match_python_basics() {
    let dashscope = provider_descriptor("dashscope").expect("dashscope descriptor");
    assert_eq!(dashscope.default_model(), "qwen3.7-max");
    assert_eq!(
        dashscope.base_url.as_deref(),
        Some("https://dashscope.aliyuncs.com/compatible-mode/v1")
    );
    assert!(dashscope.require_api_key);

    let ollama = provider_descriptor("ollama").expect("ollama descriptor");
    assert_eq!(
        ollama.base_url.as_deref(),
        Some("http://localhost:11434/v1")
    );
    assert!(!ollama.require_api_key);
    assert!(ollama.is_local);

    let mut credentials = BTreeMap::new();
    credentials.insert("dashscope".to_string(), "fake-key".to_string());
    let selected =
        create_provider_config("qwen3.7-max", &credentials, None, None, None).expect("config");
    assert_eq!(selected.provider_key, "dashscope");
    assert_eq!(selected.api_key.as_deref(), Some("fake-key"));
    assert_eq!(
        selected.base_url.as_deref(),
        Some("https://dashscope.aliyuncs.com/compatible-mode/v1")
    );

    assert_eq!(
        create_provider_config("gpt-5.5", &BTreeMap::new(), Some("openai"), None, None)
            .expect_err("missing key"),
        "No API key configured for provider 'OpenAI' (model: gpt-5.5). Run /auth to configure."
    );
}

#[test]
fn provider_registry_exposes_all_python_provider_keys() {
    assert_eq!(
        provider_keys(),
        &[
            "dashscope",
            "dashscope_token_plan",
            "openai",
            "anthropic",
            "deepseek",
            "openapi_compatible",
            "anthropic_compatible",
            "gemini",
            "kimi_cn",
            "kimi_intl",
            "minimax_cn",
            "minimax_intl",
            "zhipu_cn",
            "zhipu_intl",
            "volcengine_cn",
            "siliconflow_cn",
            "siliconflow_intl",
            "ollama",
            "lmstudio",
            "openrouter",
            "azure_openai",
            "modelscope",
            "aliyun_codingplan",
            "aliyun_codingplan_intl",
            "zhipu_cn_codingplan",
            "zhipu_intl_codingplan",
            "volcengine_cn_codingplan",
        ]
    );
}

#[test]
fn qwenpaw_provider_mappings_match_python_registry() {
    assert_eq!(
        qwenpaw_provider_mappings(),
        &[
            ("dashscope", "dashscope"),
            ("aliyun-tokenplan", "dashscope_token_plan"),
            ("openai", "openai"),
            ("anthropic", "anthropic"),
            ("deepseek", "deepseek"),
            ("gemini", "gemini"),
            ("kimi-cn", "kimi_cn"),
            ("kimi-intl", "kimi_intl"),
            ("minimax-cn", "minimax_cn"),
            ("minimax", "minimax_intl"),
            ("zhipu-cn", "zhipu_cn"),
            ("zhipu-intl", "zhipu_intl"),
            ("volcengine-cn", "volcengine_cn"),
            ("siliconflow-cn", "siliconflow_cn"),
            ("siliconflow-intl", "siliconflow_intl"),
            ("ollama", "ollama"),
            ("lmstudio", "lmstudio"),
            ("openrouter", "openrouter"),
            ("azure-openai", "azure_openai"),
            ("modelscope", "modelscope"),
            ("aliyun-codingplan", "aliyun_codingplan"),
            ("aliyun-codingplan-intl", "aliyun_codingplan_intl"),
            ("zhipu-cn-codingplan", "zhipu_cn_codingplan"),
            ("zhipu-intl-codingplan", "zhipu_intl_codingplan"),
            ("volcengine-cn-codingplan", "volcengine_cn_codingplan"),
        ]
    );
}

#[test]
fn local_openai_compatible_providers_use_python_dummy_api_keys() {
    let ollama = create_provider_config("llama3", &BTreeMap::new(), Some("ollama"), None, None)
        .expect("ollama config");
    assert_eq!(ollama.api_key.as_deref(), Some("ollama"));
    assert_eq!(
        ollama.base_url.as_deref(),
        Some("http://localhost:11434/v1")
    );

    let lmstudio = create_provider_config(
        "local-model",
        &BTreeMap::new(),
        Some("lmstudio"),
        None,
        None,
    )
    .expect("lmstudio config");
    assert_eq!(lmstudio.api_key.as_deref(), Some("lm-studio"));
    assert_eq!(
        lmstudio.base_url.as_deref(),
        Some("http://localhost:1234/v1")
    );

    let mut credentials = BTreeMap::new();
    credentials.insert("ollama".to_owned(), "custom-local-key".to_owned());
    let custom = create_provider_config("llama3", &credentials, Some("ollama"), None, None)
        .expect("custom ollama config");
    assert_eq!(custom.api_key.as_deref(), Some("custom-local-key"));
}

#[test]
fn openai_compatible_payload_matches_python_message_and_tool_shape() {
    let mut conversation = Conversation::default();
    conversation.add_user_message(AgentMessageContent::Text("hello".into()));
    conversation.add_assistant_message(AgentMessageContent::Blocks(vec![
        AgentContentBlock::Thinking(ThinkingBlock {
            thinking: "reason".into(),
        }),
        AgentContentBlock::Text(TextBlock { text: "ok".into() }),
        AgentContentBlock::ToolUse(ToolUseBlock {
            id: "call_1".into(),
            name: "read_file".into(),
            input: json::object([("path", json::string("README.md"))]),
        }),
    ]));
    conversation.add_user_message(AgentMessageContent::Blocks(vec![
        AgentContentBlock::ToolResult(ToolResultBlock {
            tool_use_id: "call_1".into(),
            content: "file content".into(),
            is_error: false,
        }),
    ]));

    let provider = OpenAiCompatibleProvider::new(ProviderConfig {
        provider_key: "openai".into(),
        model: "gpt-5.5".into(),
        api_key: Some("fake-key".into()),
        base_url: None,
        effort: None,
        supports_stream_options: false,
    });
    let payload = provider.build_chat_payload(
        &conversation,
        "system prompt",
        &[ToolDefinition {
            name: "read_file".into(),
            description: "Read a file".into(),
            input_schema: json::object([
                ("type", json::string("object")),
                (
                    "properties",
                    json::object([("path", json::object([("type", json::string("string"))]))]),
                ),
            ]),
        }],
        8192,
        true,
    );

    assert_eq!(
        payload,
        json::object([
            ("max_tokens", json::number(8192)),
            (
                "messages",
                json::array([
                    json::object([
                        ("content", json::string("system prompt")),
                        ("role", json::string("system")),
                    ]),
                    json::object([
                        ("content", json::string("hello")),
                        ("role", json::string("user")),
                    ]),
                    json::object([
                        ("content", json::string("ok")),
                        ("reasoning_content", json::string("reason")),
                        ("role", json::string("assistant")),
                        (
                            "tool_calls",
                            json::array([json::object([
                                (
                                    "function",
                                    json::object([
                                        ("arguments", json::string("{\"path\": \"README.md\"}")),
                                        ("name", json::string("read_file")),
                                    ]),
                                ),
                                ("id", json::string("call_1")),
                                ("type", json::string("function")),
                            ])]),
                        ),
                    ]),
                    json::object([
                        ("content", json::string("file content")),
                        ("role", json::string("tool")),
                        ("tool_call_id", json::string("call_1")),
                    ]),
                ]),
            ),
            ("model", json::string("gpt-5.5")),
            ("stream", json::bool_value(true)),
            (
                "tools",
                json::array([json::object([
                    (
                        "function",
                        json::object([
                            ("description", json::string("Read a file")),
                            ("name", json::string("read_file")),
                            (
                                "parameters",
                                json::object([
                                    (
                                        "properties",
                                        json::object([(
                                            "path",
                                            json::object([("type", json::string("string"))]),
                                        )]),
                                    ),
                                    ("type", json::string("object")),
                                ]),
                            ),
                        ]),
                    ),
                    ("type", json::string("function")),
                ])]),
            ),
        ])
    );
}

#[test]
fn dashscope_payload_adds_stream_options_like_python_provider() {
    let mut credentials = BTreeMap::new();
    credentials.insert("dashscope".to_string(), "fake-key".to_string());
    let config = create_provider_config("qwen3.7-max", &credentials, Some("dashscope"), None, None)
        .expect("dashscope config");
    let provider = OpenAiCompatibleProvider::new(config);
    let mut conversation = Conversation::default();
    conversation.add_user_message(AgentMessageContent::Text("hello".into()));

    let payload = provider.build_chat_payload(&conversation, "", &[], 1024, true);

    assert_eq!(
        object_field(&payload, "stream_options"),
        Some(&json::object([("include_usage", json::bool_value(true),)]))
    );
    assert_eq!(
        object_field(&payload, "extra_body"),
        Some(&json::object([
            ("enable_thinking", json::bool_value(true),)
        ]))
    );
}

#[test]
fn deepseek_and_gemini_payloads_add_stream_options_like_python_providers() {
    for (provider_key, model) in [
        ("deepseek", "deepseek-v4-pro"),
        ("gemini", "gemini-3.5-flash"),
    ] {
        let mut credentials = BTreeMap::new();
        credentials.insert(provider_key.to_string(), "fake-key".to_string());
        let descriptor = provider_descriptor(provider_key).expect("provider descriptor");
        assert!(
            descriptor.supports_stream_options,
            "{provider_key} should support stream options like the Python provider"
        );
        let config = create_provider_config(model, &credentials, Some(provider_key), None, None)
            .expect("provider config");
        let provider = OpenAiCompatibleProvider::new(config);
        let mut conversation = Conversation::default();
        conversation.add_user_message(AgentMessageContent::Text("hello".into()));

        let payload = provider.build_chat_payload(&conversation, "", &[], 1024, true);

        assert_eq!(
            object_field(&payload, "stream_options"),
            Some(&json::object([("include_usage", json::bool_value(true),)])),
            "{provider_key} payload should include stream usage options"
        );
    }
}

#[test]
fn dashscope_explicit_cache_payload_matches_python_provider() {
    let provider = OpenAiCompatibleProvider::new(ProviderConfig {
        provider_key: "dashscope".into(),
        model: "qwen3.5-plus".into(),
        api_key: Some("fake-key".into()),
        base_url: Some("https://dashscope.aliyuncs.com/compatible-mode/v1".into()),
        effort: None,
        supports_stream_options: true,
    });
    let mut conversation = Conversation::default();
    conversation.add_user_message(AgentMessageContent::Text("first".into()));
    conversation.add_assistant_message(AgentMessageContent::Text("reply".into()));
    conversation.add_user_message(AgentMessageContent::Text("second".into()));

    let payload = provider.build_chat_payload(
        &conversation,
        "STATIC\n\n--- DYNAMIC_BOUNDARY ---\n\nDYNAMIC",
        &[],
        1024,
        false,
    );

    assert_eq!(
        object_field(&payload, "messages"),
        Some(&json::array([
            json::object([
                (
                    "content",
                    json::array([
                        json::object([
                            (
                                "cache_control",
                                json::object([("type", json::string("ephemeral"))]),
                            ),
                            ("text", json::string("STATIC")),
                            ("type", json::string("text")),
                        ]),
                        json::object([
                            ("text", json::string("DYNAMIC")),
                            ("type", json::string("text")),
                        ]),
                    ]),
                ),
                ("role", json::string("system")),
            ]),
            json::object([
                ("content", json::string("first")),
                ("role", json::string("user")),
            ]),
            json::object([
                ("content", json::string("reply")),
                ("role", json::string("assistant")),
            ]),
            json::object([
                (
                    "content",
                    json::array([json::object([
                        (
                            "cache_control",
                            json::object([("type", json::string("ephemeral"))]),
                        ),
                        ("text", json::string("second")),
                        ("type", json::string("text")),
                    ])]),
                ),
                ("role", json::string("user")),
            ]),
        ]))
    );
}

#[test]
fn openai_payload_adds_reasoning_effort_like_python_provider() {
    let provider = OpenAiCompatibleProvider::new(ProviderConfig {
        provider_key: "openai".into(),
        model: "gpt-5.5".into(),
        api_key: Some("fake-key".into()),
        base_url: None,
        effort: Some("medium".into()),
        supports_stream_options: false,
    });
    let mut conversation = Conversation::default();
    conversation.add_user_message(AgentMessageContent::Text("hello".into()));

    let payload = provider.build_chat_payload(&conversation, "", &[], 1024, true);

    assert_eq!(
        object_field(&payload, "reasoning_effort"),
        Some(&json::string("medium"))
    );
    assert_eq!(
        object_field(&payload, "extra_body"),
        Some(&json::object([(
            "thinking",
            json::object([("type", json::string("enabled"))]),
        )]))
    );
}

#[test]
fn gemini_payload_adds_reasoning_effort_without_extra_body_like_python_provider() {
    let provider = OpenAiCompatibleProvider::new(ProviderConfig {
        provider_key: "gemini".into(),
        model: "gemini-3.5-flash".into(),
        api_key: Some("fake-key".into()),
        base_url: Some("https://generativelanguage.googleapis.com/v1beta/openai".into()),
        effort: Some("high".into()),
        supports_stream_options: false,
    });
    let mut conversation = Conversation::default();
    conversation.add_user_message(AgentMessageContent::Text("hello".into()));

    let payload = provider.build_chat_payload(&conversation, "", &[], 1024, true);

    assert_eq!(
        object_field(&payload, "reasoning_effort"),
        Some(&json::string("high"))
    );
    assert_eq!(object_field(&payload, "extra_body"), None);
}

#[test]
fn openai_compatible_complete_posts_payload_and_parses_response() {
    let listener = TcpListener::bind("127.0.0.1:0").expect("bind test server");
    let addr = listener.local_addr().expect("server addr");
    let server = thread::spawn(move || {
        let (mut stream, _) = listener.accept().expect("accept request");
        let request = read_http_request(&mut stream);
        assert!(request.starts_with("POST /v1/chat/completions HTTP/1.1"));
        assert!(request.contains("authorization: Bearer fake-key"));
        assert!(request.contains("\"model\":\"gpt-5.5\""));
        assert!(!request.contains("\"stream\""));
        assert!(request.contains("\"hello\""));

        let body = r#"{
            "id": "chatcmpl_1",
            "choices": [{
                "finish_reason": "tool_calls",
                "message": {
                    "content": "Use the tool",
                    "reasoning_content": "thinking",
                    "tool_calls": [{
                        "id": "call_1",
                        "function": {
                            "name": "read_file",
                            "arguments": "{\"path\":\"README.md\"}"
                        }
                    }]
                }
            }],
            "usage": {
                "prompt_tokens": 11,
                "completion_tokens": 7,
                "prompt_tokens_details": {
                    "cached_tokens": 3,
                    "cache_creation_input_tokens": 5
                }
            }
        }"#;
        write!(
            stream,
            "HTTP/1.1 200 OK\r\ncontent-type: application/json\r\ncontent-length: {}\r\n\r\n{}",
            body.len(),
            body
        )
        .expect("write response");
    });

    let provider = OpenAiCompatibleProvider::new(ProviderConfig {
        provider_key: "openai".into(),
        model: "gpt-5.5".into(),
        api_key: Some("fake-key".into()),
        base_url: Some(format!("http://{addr}/v1")),
        effort: None,
        supports_stream_options: false,
    });
    let mut conversation = Conversation::default();
    conversation.add_user_message(AgentMessageContent::Text("hello".into()));

    let response = provider
        .complete_chat(&conversation, "", &[], 2048)
        .expect("complete response");
    server.join().expect("server thread");

    assert_eq!(response.message_id, "chatcmpl_1");
    assert_eq!(response.text, "Use the tool");
    assert_eq!(response.thinking, "thinking");
    assert_eq!(response.stop_reason, "tool_use");
    assert_eq!(response.usage.input_tokens, 11);
    assert_eq!(response.usage.output_tokens, 7);
    assert_eq!(response.usage.cache_read_input_tokens, 3);
    assert_eq!(response.usage.cache_creation_input_tokens, 5);
    assert_eq!(
        response.tool_uses,
        vec![json::object([
            ("id", json::string("call_1")),
            ("name", json::string("read_file")),
            ("input", json::object([("path", json::string("README.md"))]),),
        ])]
    );
}

#[test]
fn openai_compatible_complete_recovers_concatenated_tool_inputs_like_python() {
    let listener = TcpListener::bind("127.0.0.1:0").expect("bind test server");
    let addr = listener.local_addr().expect("server addr");
    let server = thread::spawn(move || {
        let (mut stream, _) = listener.accept().expect("accept request");
        let _request = read_http_request(&mut stream);
        let body = r#"{
            "id": "chatcmpl_concat",
            "choices": [{
                "finish_reason": "tool_calls",
                "message": {
                    "content": null,
                    "tool_calls": [{
                        "id": "call_1",
                        "function": {
                            "name": "read_file",
                            "arguments": "{\"path\":\"a.txt\"}{\"path\":\"b.txt\"}"
                        }
                    }]
                }
            }]
        }"#;
        write!(
            stream,
            "HTTP/1.1 200 OK\r\ncontent-type: application/json\r\ncontent-length: {}\r\n\r\n{}",
            body.len(),
            body
        )
        .expect("write response");
    });

    let provider = OpenAiCompatibleProvider::new(ProviderConfig {
        provider_key: "openai".into(),
        model: "gpt-5.5".into(),
        api_key: Some("fake-key".into()),
        base_url: Some(format!("http://{addr}/v1")),
        effort: None,
        supports_stream_options: false,
    });
    let mut conversation = Conversation::default();
    conversation.add_user_message(AgentMessageContent::Text("hello".into()));

    let response = provider
        .complete_chat(&conversation, "", &[], 2048)
        .expect("complete response");
    server.join().expect("server thread");

    assert_eq!(response.tool_uses.len(), 2);
    assert_eq!(
        response.tool_uses[0],
        json::object([
            ("id", json::string("call_1")),
            ("name", json::string("read_file")),
            ("input", json::object([("path", json::string("a.txt"))])),
        ])
    );
    assert_synthetic_tool_use(
        &response.tool_uses[1],
        "read_file",
        json::object([("path", json::string("b.txt"))]),
    );
}

#[test]
fn openrouter_complete_sends_python_default_headers() {
    let listener = TcpListener::bind("127.0.0.1:0").expect("bind test server");
    let addr = listener.local_addr().expect("server addr");
    let (request_tx, request_rx) = mpsc::channel();
    let server = thread::spawn(move || {
        let (mut stream, _) = listener.accept().expect("accept request");
        let request = read_http_request(&mut stream);
        request_tx.send(request).expect("send request");
        let body = r#"{
            "id": "chatcmpl_openrouter",
            "choices": [{
                "finish_reason": "stop",
                "message": {"content": "ok"}
            }]
        }"#;
        write!(
            stream,
            "HTTP/1.1 200 OK\r\ncontent-type: application/json\r\ncontent-length: {}\r\n\r\n{}",
            body.len(),
            body
        )
        .expect("write response");
    });

    let provider = OpenAiCompatibleProvider::new(ProviderConfig {
        provider_key: "openrouter".into(),
        model: "anthropic/claude-sonnet-4.6".into(),
        api_key: Some("fake-key".into()),
        base_url: Some(format!("http://{addr}/v1")),
        effort: None,
        supports_stream_options: false,
    });
    let mut conversation = Conversation::default();
    conversation.add_user_message(AgentMessageContent::Text("hello".into()));

    provider
        .complete_chat(&conversation, "", &[], 2048)
        .expect("complete response");
    server.join().expect("server thread");
    let request = request_rx.recv().expect("captured request");
    let request_lower = request.to_ascii_lowercase();

    assert!(
        request_lower.contains("http-referer: https://github.com/aliyun/iac-code"),
        "missing OpenRouter HTTP-Referer header: {request}"
    );
    assert!(
        request_lower.contains("x-title: iac-code"),
        "missing OpenRouter X-Title header: {request}"
    );
}

#[test]
fn openai_compatible_provider_emits_non_streaming_response_events() {
    let listener = TcpListener::bind("127.0.0.1:0").expect("bind test server");
    let addr = listener.local_addr().expect("server addr");
    let server = thread::spawn(move || {
        let (mut stream, _) = listener.accept().expect("accept request");
        let _request = read_http_request(&mut stream);
        let body = r#"{
            "id": "chatcmpl_2",
            "choices": [{
                "finish_reason": "stop",
                "message": {"content": "done"}
            }],
            "usage": {"prompt_tokens": 2, "completion_tokens": 4}
        }"#;
        write!(
            stream,
            "HTTP/1.1 200 OK\r\ncontent-type: application/json\r\ncontent-length: {}\r\n\r\n{}",
            body.len(),
            body
        )
        .expect("write response");
    });
    let provider = OpenAiCompatibleProvider::new(ProviderConfig {
        provider_key: "openai".into(),
        model: "gpt-5.5".into(),
        api_key: Some("fake-key".into()),
        base_url: Some(format!("http://{addr}/v1")),
        effort: None,
        supports_stream_options: false,
    });
    let mut conversation = Conversation::default();
    conversation.add_user_message(AgentMessageContent::Text("hello".into()));

    let events = provider.stream_events(&conversation, "", &[], 100);
    server.join().expect("server thread");

    assert_eq!(
        events,
        vec![
            StreamEvent::MessageStart(MessageStartEvent {
                message_id: "chatcmpl_2".into(),
            }),
            StreamEvent::TextDelta(TextDeltaEvent {
                text: "done".into(),
            }),
            StreamEvent::MessageEnd(MessageEndEvent {
                stop_reason: "end_turn".into(),
                usage: Usage {
                    input_tokens: 2,
                    output_tokens: 4,
                    cache_creation_input_tokens: 0,
                    cache_read_input_tokens: 0,
                },
            }),
        ]
    );
}

#[test]
fn openai_compatible_provider_streams_sse_text_reasoning_tools_and_usage() {
    let listener = TcpListener::bind("127.0.0.1:0").expect("bind test server");
    let addr = listener.local_addr().expect("server addr");
    let server = thread::spawn(move || {
        let (mut stream, _) = listener.accept().expect("accept request");
        let request = read_http_request(&mut stream);
        assert!(request.starts_with("POST /v1/chat/completions HTTP/1.1"));
        assert!(
            request.contains("\"stream\":true"),
            "missing stream flag: {request}"
        );
        assert!(request.contains("\"model\":\"gpt-5.5\""));

        let body = concat!(
            "data: {\"id\":\"chatcmpl_stream\",\"choices\":[{\"delta\":{\"reasoning_content\":\"think \"}}]}\n\n",
            "data: {\"id\":\"chatcmpl_stream\",\"choices\":[{\"delta\":{\"content\":\"hel\"}}]}\n\n",
            "data: {\"id\":\"chatcmpl_stream\",\"choices\":[{\"delta\":{\"content\":\"lo\"}}]}\n\n",
            "data: {\"id\":\"chatcmpl_stream\",\"choices\":[{\"delta\":{\"tool_calls\":[{\"index\":0,\"id\":\"call_1\",\"type\":\"function\",\"function\":{\"name\":\"read_file\",\"arguments\":\"{\\\"path\\\"\"}}]}}]}\n\n",
            "data: {\"id\":\"chatcmpl_stream\",\"choices\":[{\"delta\":{\"tool_calls\":[{\"index\":0,\"function\":{\"arguments\":\":\\\"README.md\\\"}\"}}]}}]}\n\n",
            "data: {\"id\":\"chatcmpl_stream\",\"choices\":[{\"finish_reason\":\"tool_calls\",\"delta\":{}}]}\n\n",
            "data: {\"id\":\"chatcmpl_stream\",\"choices\":[],\"usage\":{\"prompt_tokens\":5,\"completion_tokens\":7,\"prompt_tokens_details\":{\"cached_tokens\":2,\"cache_creation_input_tokens\":3}}}\n\n",
            "data: [DONE]\n\n",
        );
        write!(
            stream,
            "HTTP/1.1 200 OK\r\ncontent-type: text/event-stream\r\ncontent-length: {}\r\n\r\n{}",
            body.len(),
            body
        )
        .expect("write response");
    });
    let provider = OpenAiCompatibleProvider::new(ProviderConfig {
        provider_key: "openai".into(),
        model: "gpt-5.5".into(),
        api_key: Some("fake-key".into()),
        base_url: Some(format!("http://{addr}/v1")),
        effort: None,
        supports_stream_options: false,
    });
    let mut conversation = Conversation::default();
    conversation.add_user_message(AgentMessageContent::Text("hello".into()));

    let events = provider.stream_events(&conversation, "", &[], 100);
    server.join().expect("server thread");

    assert_eq!(
        events,
        vec![
            StreamEvent::MessageStart(MessageStartEvent {
                message_id: "chatcmpl_stream".into(),
            }),
            StreamEvent::ThinkingDelta(ThinkingDeltaEvent {
                text: "think ".into(),
            }),
            StreamEvent::TextDelta(TextDeltaEvent { text: "hel".into() }),
            StreamEvent::TextDelta(TextDeltaEvent { text: "lo".into() }),
            StreamEvent::ToolUseStart(ToolUseStartEvent {
                tool_use_id: "call_1".into(),
                name: "read_file".into(),
            }),
            StreamEvent::ToolInputDelta(ToolInputDeltaEvent {
                tool_use_id: "call_1".into(),
                partial_json: "{\"path\"".into(),
            }),
            StreamEvent::ToolInputDelta(ToolInputDeltaEvent {
                tool_use_id: "call_1".into(),
                partial_json: ":\"README.md\"}".into(),
            }),
            StreamEvent::ToolUseEnd(ToolUseEndEvent {
                tool_use_id: "call_1".into(),
                name: "read_file".into(),
                input: json::object([("path", json::string("README.md"))]),
            }),
            StreamEvent::MessageEnd(MessageEndEvent {
                stop_reason: "tool_use".into(),
                usage: Usage {
                    input_tokens: 5,
                    output_tokens: 7,
                    cache_creation_input_tokens: 3,
                    cache_read_input_tokens: 2,
                },
            }),
        ]
    );
}

#[test]
fn openai_compatible_stream_recovers_concatenated_tool_inputs_like_python() {
    let listener = TcpListener::bind("127.0.0.1:0").expect("bind test server");
    let addr = listener.local_addr().expect("server addr");
    let server = thread::spawn(move || {
        let (mut stream, _) = listener.accept().expect("accept request");
        let _request = read_http_request(&mut stream);
        let body = concat!(
            "data: {\"id\":\"chatcmpl_concat_stream\",\"choices\":[{\"delta\":{\"tool_calls\":[{\"index\":0,\"id\":\"call_1\",\"type\":\"function\",\"function\":{\"name\":\"read_file\",\"arguments\":\"{\\\"path\\\":\\\"a.txt\\\"}{\\\"path\\\":\\\"b.txt\\\"}\"}}]}}]}\n\n",
            "data: {\"id\":\"chatcmpl_concat_stream\",\"choices\":[{\"finish_reason\":\"tool_calls\",\"delta\":{}}]}\n\n",
            "data: [DONE]\n\n",
        );
        write!(
            stream,
            "HTTP/1.1 200 OK\r\ncontent-type: text/event-stream\r\ncontent-length: {}\r\n\r\n{}",
            body.len(),
            body
        )
        .expect("write response");
    });
    let provider = OpenAiCompatibleProvider::new(ProviderConfig {
        provider_key: "openai".into(),
        model: "gpt-5.5".into(),
        api_key: Some("fake-key".into()),
        base_url: Some(format!("http://{addr}/v1")),
        effort: None,
        supports_stream_options: false,
    });
    let mut conversation = Conversation::default();
    conversation.add_user_message(AgentMessageContent::Text("hello".into()));

    let events = provider.stream_events(&conversation, "", &[], 100);
    server.join().expect("server thread");

    assert_eq!(
        &events[..4],
        &[
            StreamEvent::MessageStart(MessageStartEvent {
                message_id: "chatcmpl_concat_stream".into(),
            }),
            StreamEvent::ToolUseStart(ToolUseStartEvent {
                tool_use_id: "call_1".into(),
                name: "read_file".into(),
            }),
            StreamEvent::ToolInputDelta(ToolInputDeltaEvent {
                tool_use_id: "call_1".into(),
                partial_json: "{\"path\":\"a.txt\"}{\"path\":\"b.txt\"}".into(),
            }),
            StreamEvent::ToolUseEnd(ToolUseEndEvent {
                tool_use_id: "call_1".into(),
                name: "read_file".into(),
                input: json::object([("path", json::string("a.txt"))]),
            }),
        ]
    );
    assert_synthetic_tool_events(
        &events[4..6],
        "read_file",
        json::object([("path", json::string("b.txt"))]),
    );
    assert_eq!(
        events.last(),
        Some(&StreamEvent::MessageEnd(MessageEndEvent {
            stop_reason: "tool_use".into(),
            usage: Usage::default(),
        }))
    );
}

#[test]
fn openai_compatible_provider_falls_back_to_non_streaming_when_stream_request_fails() {
    let listener = TcpListener::bind("127.0.0.1:0").expect("bind test server");
    listener.set_nonblocking(true).expect("set nonblocking");
    let addr = listener.local_addr().expect("server addr");
    let server = thread::spawn(move || {
        let mut stream =
            accept_tcp_with_timeout(&listener, "stream request").expect("accept stream request");
        let stream_request = read_http_request(&mut stream);
        assert!(
            stream_request.contains("\"stream\":true"),
            "first request should be streaming: {stream_request}"
        );
        let stream_body = r#"{"error":"stream failed"}"#;
        write!(
            stream,
            "HTTP/1.1 500 Internal Server Error\r\ncontent-type: application/json\r\ncontent-length: {}\r\n\r\n{}",
            stream_body.len(),
            stream_body
        )
        .expect("write stream error response");

        let Some(mut stream) = accept_tcp_with_timeout(&listener, "fallback request") else {
            return false;
        };
        let fallback_request = read_http_request(&mut stream);
        assert!(
            !fallback_request.contains("\"stream\""),
            "fallback request should be non-streaming: {fallback_request}"
        );
        let fallback_body = r#"{
            "id": "chatcmpl_fallback",
            "choices": [{
                "finish_reason": "stop",
                "message": {"content": "fallback text"}
            }],
            "usage": {"prompt_tokens": 9, "completion_tokens": 4}
        }"#;
        write!(
            stream,
            "HTTP/1.1 200 OK\r\ncontent-type: application/json\r\ncontent-length: {}\r\n\r\n{}",
            fallback_body.len(),
            fallback_body
        )
        .expect("write fallback response");
        true
    });
    let provider = OpenAiCompatibleProvider::new(ProviderConfig {
        provider_key: "openai".into(),
        model: "gpt-5.5".into(),
        api_key: Some("fake-key".into()),
        base_url: Some(format!("http://{addr}/v1")),
        effort: None,
        supports_stream_options: false,
    });
    let mut conversation = Conversation::default();
    conversation.add_user_message(AgentMessageContent::Text("hello".into()));

    let events = provider.stream_events(&conversation, "", &[], 100);
    let saw_fallback_request = server.join().expect("server thread");

    assert!(
        saw_fallback_request,
        "stream failure should trigger a non-streaming fallback request; events: {events:?}"
    );

    assert_eq!(
        events,
        vec![
            StreamEvent::MessageStart(MessageStartEvent {
                message_id: "chatcmpl_fallback".into(),
            }),
            StreamEvent::TextDelta(TextDeltaEvent {
                text: "fallback text".into(),
            }),
            StreamEvent::MessageEnd(MessageEndEvent {
                stop_reason: "end_turn".into(),
                usage: Usage {
                    input_tokens: 9,
                    output_tokens: 4,
                    cache_creation_input_tokens: 0,
                    cache_read_input_tokens: 0,
                },
            }),
        ]
    );
}

#[test]
fn openai_compatible_provider_retries_retryable_non_streaming_fallback_like_python() {
    let listener = TcpListener::bind("127.0.0.1:0").expect("bind test server");
    listener.set_nonblocking(true).expect("set nonblocking");
    let addr = listener.local_addr().expect("server addr");
    let server = thread::spawn(move || {
        let mut stream =
            accept_tcp_with_timeout(&listener, "stream request").expect("accept stream request");
        let stream_request = read_http_request(&mut stream);
        assert!(
            stream_request.contains("\"stream\":true"),
            "first request should be streaming: {stream_request}"
        );
        let stream_body = r#"{"error":"stream failed"}"#;
        write!(
            stream,
            "HTTP/1.1 500 Internal Server Error\r\ncontent-type: application/json\r\ncontent-length: {}\r\n\r\n{}",
            stream_body.len(),
            stream_body
        )
        .expect("write stream error response");

        let mut stream = accept_tcp_with_timeout(&listener, "retryable fallback request")
            .expect("accept retryable fallback request");
        let fallback_request = read_http_request(&mut stream);
        assert!(
            !fallback_request.contains("\"stream\""),
            "fallback request should be non-streaming: {fallback_request}"
        );
        let retryable_body = r#"{"error":"temporary"}"#;
        write!(
            stream,
            "HTTP/1.1 503 Service Unavailable\r\ncontent-type: application/json\r\ncontent-length: {}\r\n\r\n{}",
            retryable_body.len(),
            retryable_body
        )
        .expect("write retryable fallback error response");

        let Some(mut stream) = accept_tcp_with_timeout(&listener, "retried fallback request")
        else {
            return false;
        };
        let retried_request = read_http_request(&mut stream);
        assert!(
            !retried_request.contains("\"stream\""),
            "retried fallback request should be non-streaming: {retried_request}"
        );
        let fallback_body = r#"{
            "id": "chatcmpl_retried_fallback",
            "choices": [{
                "finish_reason": "stop",
                "message": {"content": "retried fallback text"}
            }],
            "usage": {"prompt_tokens": 6, "completion_tokens": 2}
        }"#;
        write!(
            stream,
            "HTTP/1.1 200 OK\r\ncontent-type: application/json\r\ncontent-length: {}\r\n\r\n{}",
            fallback_body.len(),
            fallback_body
        )
        .expect("write retried fallback response");
        true
    });
    let provider = OpenAiCompatibleProvider::new(ProviderConfig {
        provider_key: "openapi_compatible".into(),
        model: "custom-model".into(),
        api_key: Some("fake-key".into()),
        base_url: Some(format!("http://{addr}/v1")),
        effort: None,
        supports_stream_options: false,
    });
    let mut conversation = Conversation::default();
    conversation.add_user_message(AgentMessageContent::Text("hello".into()));

    let events = provider.stream_events(&conversation, "", &[], 100);
    let saw_retried_fallback = server.join().expect("server thread");

    assert!(
        saw_retried_fallback,
        "retryable fallback completion should be retried; events: {events:?}"
    );
    assert_eq!(
        events,
        vec![
            StreamEvent::MessageStart(MessageStartEvent {
                message_id: "chatcmpl_retried_fallback".into(),
            }),
            StreamEvent::TextDelta(TextDeltaEvent {
                text: "retried fallback text".into(),
            }),
            StreamEvent::MessageEnd(MessageEndEvent {
                stop_reason: "end_turn".into(),
                usage: Usage {
                    input_tokens: 6,
                    output_tokens: 2,
                    cache_creation_input_tokens: 0,
                    cache_read_input_tokens: 0,
                },
            }),
        ]
    );
}

#[test]
fn openai_compatible_provider_tombstones_partial_stream_before_fallback() {
    let listener = TcpListener::bind("127.0.0.1:0").expect("bind test server");
    listener.set_nonblocking(true).expect("set nonblocking");
    let addr = listener.local_addr().expect("server addr");
    let server = thread::spawn(move || {
        let mut stream =
            accept_tcp_with_timeout(&listener, "stream request").expect("accept stream request");
        let stream_request = read_http_request(&mut stream);
        assert!(
            stream_request.contains("\"stream\":true"),
            "first request should be streaming: {stream_request}"
        );
        let stream_body = concat!(
            "data: {\"id\":\"chatcmpl_partial\",\"choices\":[{\"delta\":{\"content\":\"partial\"}}]}\n\n",
            "data: {not-json\n\n",
        );
        write!(
            stream,
            "HTTP/1.1 200 OK\r\ncontent-type: text/event-stream\r\ncontent-length: {}\r\n\r\n{}",
            stream_body.len(),
            stream_body
        )
        .expect("write partial stream response");

        let mut stream = accept_tcp_with_timeout(&listener, "fallback request")
            .expect("accept fallback request");
        let fallback_request = read_http_request(&mut stream);
        assert!(
            !fallback_request.contains("\"stream\""),
            "fallback request should be non-streaming: {fallback_request}"
        );
        let fallback_body = r#"{
            "id": "chatcmpl_fallback_after_partial",
            "choices": [{
                "finish_reason": "stop",
                "message": {"content": "fallback text"}
            }],
            "usage": {"prompt_tokens": 9, "completion_tokens": 4}
        }"#;
        write!(
            stream,
            "HTTP/1.1 200 OK\r\ncontent-type: application/json\r\ncontent-length: {}\r\n\r\n{}",
            fallback_body.len(),
            fallback_body
        )
        .expect("write fallback response");
    });
    let provider = OpenAiCompatibleProvider::new(ProviderConfig {
        provider_key: "openai".into(),
        model: "gpt-5.5".into(),
        api_key: Some("fake-key".into()),
        base_url: Some(format!("http://{addr}/v1")),
        effort: None,
        supports_stream_options: false,
    });
    let mut conversation = Conversation::default();
    conversation.add_user_message(AgentMessageContent::Text("hello".into()));

    let events = provider.stream_events(&conversation, "", &[], 100);
    server.join().expect("server thread");

    assert_eq!(
        events,
        vec![
            StreamEvent::MessageStart(MessageStartEvent {
                message_id: "chatcmpl_partial".into(),
            }),
            StreamEvent::TextDelta(TextDeltaEvent {
                text: "partial".into(),
            }),
            StreamEvent::Tombstone(TombstoneEvent {
                message_id: "chatcmpl_partial".into(),
            }),
            StreamEvent::MessageStart(MessageStartEvent {
                message_id: "chatcmpl_fallback_after_partial".into(),
            }),
            StreamEvent::TextDelta(TextDeltaEvent {
                text: "fallback text".into(),
            }),
            StreamEvent::MessageEnd(MessageEndEvent {
                stop_reason: "end_turn".into(),
                usage: Usage {
                    input_tokens: 9,
                    output_tokens: 4,
                    cache_creation_input_tokens: 0,
                    cache_read_input_tokens: 0,
                },
            }),
        ]
    );
}

#[test]
fn openai_compatible_provider_degrades_model_when_stream_and_primary_complete_fail() {
    let listener = TcpListener::bind("127.0.0.1:0").expect("bind test server");
    listener.set_nonblocking(true).expect("set nonblocking");
    let addr = listener.local_addr().expect("server addr");
    let server = thread::spawn(move || {
        let mut stream =
            accept_tcp_with_timeout(&listener, "stream request").expect("accept stream request");
        let stream_request = read_http_request(&mut stream);
        assert!(
            stream_request.contains("\"stream\":true"),
            "first request should be streaming: {stream_request}"
        );
        assert!(
            stream_request.contains("\"model\":\"gpt-5.5\""),
            "stream request should use primary model: {stream_request}"
        );
        let stream_body = r#"{"error":"stream failed"}"#;
        write!(
            stream,
            "HTTP/1.1 500 Internal Server Error\r\ncontent-type: application/json\r\ncontent-length: {}\r\n\r\n{}",
            stream_body.len(),
            stream_body
        )
        .expect("write stream error response");

        for attempt in 0..=5 {
            let mut stream = accept_tcp_with_timeout(&listener, "primary complete request")
                .expect("accept primary complete request");
            let primary_complete_request = read_http_request(&mut stream);
            assert!(
                !primary_complete_request.contains("\"stream\""),
                "primary complete request should be non-streaming: {primary_complete_request}"
            );
            assert!(
                primary_complete_request.contains("\"model\":\"gpt-5.5\""),
                "primary complete request should use primary model: {primary_complete_request}"
            );
            let complete_body = format!(r#"{{"error":"primary complete failed {attempt}"}}"#);
            write!(
                stream,
                "HTTP/1.1 500 Internal Server Error\r\ncontent-type: application/json\r\ncontent-length: {}\r\n\r\n{}",
                complete_body.len(),
                complete_body
            )
            .expect("write primary complete error response");
        }

        let Some(mut stream) = accept_tcp_with_timeout(&listener, "fallback model request") else {
            return false;
        };
        let fallback_request = read_http_request(&mut stream);
        assert!(
            !fallback_request.contains("\"stream\""),
            "fallback model request should be non-streaming: {fallback_request}"
        );
        assert!(
            fallback_request.contains("\"model\":\"gpt-5.4\""),
            "fallback model request should use degraded model: {fallback_request}"
        );
        let fallback_body = r#"{
            "id": "chatcmpl_model_fallback",
            "choices": [{
                "finish_reason": "stop",
                "message": {"content": "fallback model text"}
            }],
            "usage": {"prompt_tokens": 7, "completion_tokens": 3}
        }"#;
        write!(
            stream,
            "HTTP/1.1 200 OK\r\ncontent-type: application/json\r\ncontent-length: {}\r\n\r\n{}",
            fallback_body.len(),
            fallback_body
        )
        .expect("write fallback model response");
        true
    });
    let provider = OpenAiCompatibleProvider::new(ProviderConfig {
        provider_key: "openai".into(),
        model: "gpt-5.5".into(),
        api_key: Some("fake-key".into()),
        base_url: Some(format!("http://{addr}/v1")),
        effort: None,
        supports_stream_options: false,
    });
    let mut conversation = Conversation::default();
    conversation.add_user_message(AgentMessageContent::Text("hello".into()));

    let events = provider.stream_events(&conversation, "", &[], 100);
    let saw_fallback_model_request = server.join().expect("server thread");

    assert!(
        saw_fallback_model_request,
        "primary completion failure should trigger degraded-model completion; events: {events:?}"
    );
    assert_eq!(
        events,
        vec![
            StreamEvent::MessageStart(MessageStartEvent {
                message_id: "chatcmpl_model_fallback".into(),
            }),
            StreamEvent::TextDelta(TextDeltaEvent {
                text: "fallback model text".into(),
            }),
            StreamEvent::MessageEnd(MessageEndEvent {
                stop_reason: "end_turn".into(),
                usage: Usage {
                    input_tokens: 7,
                    output_tokens: 3,
                    cache_creation_input_tokens: 0,
                    cache_read_input_tokens: 0,
                },
            }),
        ]
    );
}

#[test]
fn openai_compatible_provider_reports_base_url_hint_when_stream_and_complete_have_no_data() {
    let listener = TcpListener::bind("127.0.0.1:0").expect("bind test server");
    listener.set_nonblocking(true).expect("set nonblocking");
    let addr = listener.local_addr().expect("server addr");
    let base_url = format!("http://{addr}/compatible");
    let expected_base_url = base_url.clone();
    let server = thread::spawn(move || {
        let mut stream =
            accept_tcp_with_timeout(&listener, "stream request").expect("accept stream request");
        let stream_request = read_http_request(&mut stream);
        assert!(
            stream_request.contains("\"stream\":true"),
            "first request should be streaming: {stream_request}"
        );
        write!(
            stream,
            "HTTP/1.1 200 OK\r\ncontent-type: text/event-stream\r\ncontent-length: 0\r\n\r\n"
        )
        .expect("write empty stream response");

        let mut stream = accept_tcp_with_timeout(&listener, "primary complete request")
            .expect("accept primary complete request");
        let primary_complete_request = read_http_request(&mut stream);
        assert!(
            !primary_complete_request.contains("\"stream\""),
            "primary complete request should be non-streaming: {primary_complete_request}"
        );
        let complete_body = r#"{"error":"missing v1 suffix"}"#;
        write!(
            stream,
            "HTTP/1.1 200 OK\r\ncontent-type: application/json\r\ncontent-length: {}\r\n\r\n{}",
            complete_body.len(),
            complete_body
        )
        .expect("write invalid complete response");
    });
    let provider = OpenAiCompatibleProvider::new(ProviderConfig {
        provider_key: "openai".into(),
        model: "gpt-test".into(),
        api_key: Some("fake-key".into()),
        base_url: Some(base_url.clone()),
        effort: None,
        supports_stream_options: false,
    });
    let mut conversation = Conversation::default();
    conversation.add_user_message(AgentMessageContent::Text("hello".into()));

    let events = provider.stream_events(&conversation, "", &[], 100);
    server.join().expect("server thread");

    let StreamEvent::Error(error) = events.last().expect("error event") else {
        panic!("expected final error event, got {events:?}");
    };
    assert!(
        error
            .error
            .contains(&format!("current: {expected_base_url}")),
        "missing current base URL hint: {}",
        error.error
    );
    assert!(
        error.error.contains(&format!(
            "Many OpenAI-compatible endpoints require a /v1 suffix (e.g. {expected_base_url}/v1)."
        )),
        "missing /v1 suffix hint: {}",
        error.error
    );
}

fn object_field<'a>(value: &'a JsonValue, key: &str) -> Option<&'a JsonValue> {
    match value {
        JsonValue::Object(fields) => fields.get(key),
        _ => None,
    }
}

fn assert_synthetic_tool_use(value: &JsonValue, name: &str, input: JsonValue) {
    assert!(
        matches!(object_field(value, "id"), Some(JsonValue::String(id)) if id.starts_with("toolu_")),
        "synthetic tool use should have a toolu_ id: {value:?}"
    );
    assert_eq!(object_field(value, "name"), Some(&json::string(name)));
    assert_eq!(object_field(value, "input"), Some(&input));
}

fn assert_synthetic_tool_events(events: &[StreamEvent], name: &str, input: JsonValue) {
    let [StreamEvent::ToolUseStart(start), StreamEvent::ToolUseEnd(end)] = events else {
        panic!("expected synthetic start/end events, got {events:?}");
    };
    assert!(
        start.tool_use_id.starts_with("toolu_"),
        "synthetic tool use should have a toolu_ id: {start:?}"
    );
    assert_eq!(start.name, name);
    assert_eq!(end.tool_use_id, start.tool_use_id);
    assert_eq!(end.name, name);
    assert_eq!(end.input, input);
}

fn accept_tcp_with_timeout(listener: &TcpListener, label: &str) -> Option<std::net::TcpStream> {
    let deadline = std::time::Instant::now() + std::time::Duration::from_secs(3);
    loop {
        match listener.accept() {
            Ok((stream, _)) => {
                stream.set_nonblocking(false).expect("set stream blocking");
                return Some(stream);
            }
            Err(error) if error.kind() == std::io::ErrorKind::WouldBlock => {
                if std::time::Instant::now() >= deadline {
                    return None;
                }
                thread::sleep(std::time::Duration::from_millis(10));
            }
            Err(error) => panic!("accept {label}: {error}"),
        }
    }
}

fn read_http_request(stream: &mut std::net::TcpStream) -> String {
    let mut buffer = Vec::new();
    let mut temp = [0_u8; 4096];
    loop {
        let read = stream.read(&mut temp).expect("read request");
        if read == 0 {
            break;
        }
        buffer.extend_from_slice(&temp[..read]);
        let text = String::from_utf8_lossy(&buffer);
        let Some((headers, body)) = text.split_once("\r\n\r\n") else {
            continue;
        };
        let content_length = headers
            .lines()
            .find_map(|line| {
                let (name, value) = line.split_once(':')?;
                name.eq_ignore_ascii_case("content-length")
                    .then(|| value.trim().parse::<usize>().ok())
                    .flatten()
            })
            .unwrap_or(0);
        if body.len() >= content_length {
            return text.to_string();
        }
    }
    String::from_utf8_lossy(&buffer).into_owned()
}
