use std::io::{Read, Write};
use std::net::TcpListener;
use std::thread;

use iac_code_protocol::json::{self, JsonValue};
use iac_code_protocol::message::{AgentMessageContent, Conversation};
use iac_code_protocol::provider::ToolDefinition;
use iac_code_protocol::{
    MessageEndEvent, MessageStartEvent, StreamEvent, TextDeltaEvent, ThinkingDeltaEvent,
    TombstoneEvent, ToolInputDeltaEvent, ToolUseEndEvent, ToolUseStartEvent, Usage,
};
use iac_code_providers::{AnthropicProvider, EventProvider, ProviderConfig};

#[test]
fn anthropic_context_window_stop_uses_the_internal_truncation_reason() {
    for streaming in [false, true] {
        let listener = TcpListener::bind("127.0.0.1:0").expect("bind test server");
        let addr = listener.local_addr().expect("server addr");
        let server = thread::spawn(move || {
            let (mut stream, _) = listener.accept().expect("messages request");
            let _request = read_http_request(&mut stream);
            let body = if streaming {
                concat!(
                    "data: {\"type\":\"message_start\",\"message\":{\"id\":\"context_stop\"}}\n\n",
                    "data: {\"type\":\"message_delta\",\"delta\":{\"stop_reason\":\"model_context_window_exceeded\"}}\n\n",
                    "data: {\"type\":\"message_stop\"}\n\n",
                )
            } else {
                r#"{"id":"context_stop","content":[],"stop_reason":"model_context_window_exceeded"}"#
            };
            write!(
                stream,
                "HTTP/1.1 200 OK\r\ncontent-length: {}\r\n\r\n{}",
                body.len(),
                body
            )
            .expect("write context-window stop");
        });
        let provider = AnthropicProvider::new(ProviderConfig {
            provider_key: "anthropic".into(),
            model: "claude-opus-4-7".into(),
            api_key: Some("fixture-key".into()),
            base_url: Some(format!("http://{addr}")),
            effort: None,
            supports_stream_options: false,
        });
        let conversation = Conversation::default();
        let reason = if streaming {
            let events = provider
                .stream_chat(&conversation, "", &[])
                .expect("stream response");
            match events.last().expect("message end") {
                StreamEvent::MessageEnd(end) => end.stop_reason.clone(),
                event => panic!("expected message end, got {event:?}"),
            }
        } else {
            provider
                .complete_chat(&conversation, "", &[])
                .expect("complete response")
                .stop_reason
        };
        server.join().expect("server thread");
        assert_eq!(reason, "max_tokens", "streaming: {streaming}");
    }
}

#[test]
fn anthropic_unknown_model_uses_provider_metadata_maximum() {
    for (provider_key, model) in [
        ("anthropic", "custom-model"),
        ("anthropic_compatible", "custom-model"),
    ] {
        let listener = TcpListener::bind("127.0.0.1:0").expect("bind test server");
        listener.set_nonblocking(true).expect("set nonblocking");
        let addr = listener.local_addr().expect("server addr");
        let server = thread::spawn(move || {
            let Some(mut stream) = accept_tcp_with_timeout(&listener, "model metadata") else {
                return false;
            };
            let request = read_http_request(&mut stream);
            assert!(
                request.starts_with(&format!("GET /v1/models/{model} HTTP/1.1")),
                "{request}"
            );
            assert!(request.contains("x-api-key: fixture-key"));
            let body = r#"{"id":"custom-model","max_tokens":196608}"#;
            write!(
                stream,
                "HTTP/1.1 200 OK\r\ncontent-type: application/json\r\ncontent-length: {}\r\n\r\n{}",
                body.len(),
                body
            )
            .expect("write metadata response");
            true
        });
        let provider = AnthropicProvider::new(ProviderConfig {
            provider_key: provider_key.into(),
            model: model.into(),
            api_key: Some("fixture-key".into()),
            base_url: Some(format!("http://{addr}/v1")),
            effort: None,
            supports_stream_options: false,
        });
        let payload = provider
            .build_messages_payload(&Conversation::default(), "", &[])
            .expect("model capability");
        let payload: serde_json::Value =
            serde_json::from_str(&payload.to_compact_json()).expect("request JSON");
        assert!(
            server.join().expect("server thread"),
            "metadata request was missing"
        );
        assert_eq!(payload["max_tokens"], 196608);
    }
}

#[test]
fn minimax_messages_omit_optional_output_limits_without_model_lookup() {
    for provider_key in ["minimax_cn", "minimax_intl"] {
        for model in [
            "MiniMax-M3",
            "MiniMax-M3.1-Flash",
            "MiniMax-M2.7",
            "MiniMax-M2.7-highspeed",
            "MiniMax-M2.5",
            "MiniMax-M2.5-highspeed",
            "custom-minimax-model",
        ] {
            let provider = AnthropicProvider::new(ProviderConfig {
                provider_key: provider_key.into(),
                model: model.into(),
                api_key: Some("fixture-key".into()),
                base_url: Some("http://127.0.0.1:9/anthropic".into()),
                effort: None,
                supports_stream_options: false,
            });
            let payload = provider
                .build_messages_payload(&Conversation::default(), "", &[])
                .expect("MiniMax capacity");
            let payload: serde_json::Value =
                serde_json::from_str(&payload.to_compact_json()).unwrap();
            for field in ["max_tokens", "max_completion_tokens", "max_output_tokens"] {
                assert!(
                    payload.get(field).is_none(),
                    "{provider_key}: {model}: {field}"
                );
            }
        }
    }
}

#[test]
fn minimax_complete_stream_and_sink_requests_omit_output_limits() {
    for provider_key in ["minimax_cn", "minimax_intl"] {
        for mode in 0..3 {
            let listener = TcpListener::bind("127.0.0.1:0").expect("bind test server");
            let addr = listener.local_addr().expect("server addr");
            let server = thread::spawn(move || {
                let (mut stream, _) = listener.accept().expect("messages request");
                let request = read_http_request(&mut stream);
                assert!(
                    request.starts_with("POST /anthropic/v1/messages HTTP/1.1"),
                    "{request}"
                );
                let (_, body) = request.split_once("\r\n\r\n").expect("request body");
                let payload: serde_json::Value = serde_json::from_str(body).expect("request JSON");
                assert_eq!(payload["model"], "MiniMax-M2.7");
                assert_eq!(
                    payload
                        .get("stream")
                        .and_then(serde_json::Value::as_bool)
                        .unwrap_or(false),
                    mode != 0
                );
                for field in ["max_tokens", "max_completion_tokens", "max_output_tokens"] {
                    assert!(
                        payload.get(field).is_none(),
                        "unexpected output limit: {field}"
                    );
                }
                let body = r#"{"id":"minimax-message","content":[],"stop_reason":"end_turn"}"#;
                write!(
                    stream,
                    "HTTP/1.1 200 OK\r\ncontent-length: {}\r\n\r\n{}",
                    body.len(),
                    body
                )
                .expect("write messages response");
            });
            let provider = AnthropicProvider::new(ProviderConfig {
                provider_key: provider_key.into(),
                model: "MiniMax-M2.7".into(),
                api_key: Some("fixture-key".into()),
                base_url: Some(format!("http://{addr}/anthropic")),
                effort: None,
                supports_stream_options: false,
            });
            let conversation = Conversation::default();
            match mode {
                0 => {
                    provider
                        .complete_chat(&conversation, "", &[])
                        .expect("complete response");
                }
                1 => {
                    provider
                        .stream_chat(&conversation, "", &[])
                        .expect("stream response");
                }
                _ => {
                    provider
                        .stream_chat_with_sink(&conversation, "", &[], &mut |_| {})
                        .expect("sink response");
                }
            }
            server.join().expect("server thread");
        }
    }
}

#[test]
fn anthropic_metadata_without_a_positive_integer_capacity_is_rejected() {
    for capacity in ["null", "0", "-1", "1.5", "true", "\"131072\""] {
        let listener = TcpListener::bind("127.0.0.1:0").expect("bind test server");
        let addr = listener.local_addr().expect("server addr");
        let body = format!(r#"{{"max_tokens":{capacity}}}"#);
        let server = thread::spawn(move || {
            let (mut stream, _) = listener.accept().expect("metadata request");
            let _request = read_http_request(&mut stream);
            write!(
                stream,
                "HTTP/1.1 200 OK\r\ncontent-length: {}\r\n\r\n{}",
                body.len(),
                body
            )
            .expect("write invalid metadata");
        });
        let provider = AnthropicProvider::new(ProviderConfig {
            provider_key: "anthropic_compatible".into(),
            model: "unknown-model".into(),
            api_key: Some("fixture-key".into()),
            base_url: Some(format!("http://{addr}")),
            effort: None,
            supports_stream_options: false,
        });
        let error = provider
            .build_messages_payload(&Conversation::default(), "", &[])
            .expect_err("invalid capability");
        server.join().expect("server thread");
        assert!(
            error.contains("positive max_tokens capability"),
            "{capacity}: {error}"
        );
    }
}

#[test]
fn anthropic_metadata_http_error_does_not_expose_url_credentials() {
    let listener = TcpListener::bind("127.0.0.1:0").expect("bind test server");
    let addr = listener.local_addr().expect("server addr");
    let server = thread::spawn(move || {
        let (mut stream, _) = listener.accept().expect("metadata request");
        let _request = read_http_request(&mut stream);
        write!(
            stream,
            "HTTP/1.1 403 Forbidden\r\ncontent-length: 0\r\n\r\n"
        )
        .expect("write metadata error");
    });
    let provider = AnthropicProvider::new(ProviderConfig {
        provider_key: "anthropic_compatible".into(),
        model: "unknown-model".into(),
        api_key: Some("fixture-key".into()),
        base_url: Some(format!("http://{addr}/v1?access_token=fixture-secret")),
        effort: None,
        supports_stream_options: false,
    });
    let error = provider
        .build_messages_payload(&Conversation::default(), "", &[])
        .expect_err("HTTP error");
    server.join().expect("server thread");
    assert!(error.contains("403"), "{error}");
    assert!(
        !error.contains("fixture-secret"),
        "URL credentials leaked: {error}"
    );
}

#[test]
fn anthropic_compatible_model_alias_uses_gateway_metadata() {
    let listener = TcpListener::bind("127.0.0.1:0").expect("bind test server");
    listener.set_nonblocking(true).expect("set nonblocking");
    let addr = listener.local_addr().expect("server addr");
    let server = thread::spawn(move || {
        let Some(mut stream) = accept_tcp_with_timeout(&listener, "gateway metadata") else {
            return false;
        };
        let request = read_http_request(&mut stream);
        assert!(
            request.contains("content-type: application/json"),
            "{request}"
        );
        assert!(
            request.contains("anthropic-version: 2023-06-01"),
            "{request}"
        );
        assert!(request.contains("x-api-key: fixture-key"), "{request}");
        assert!(
            request.starts_with("GET /v1/models/claude-sonnet-4-6 HTTP/1.1"),
            "{request}"
        );
        let body = r#"{"id":"claude-sonnet-4-6","max_tokens":262144}"#;
        write!(
            stream,
            "HTTP/1.1 200 OK\r\ncontent-length: {}\r\n\r\n{}",
            body.len(),
            body
        )
        .expect("write gateway capability");
        true
    });
    let provider = AnthropicProvider::new(ProviderConfig {
        provider_key: "anthropic_compatible".into(),
        model: "claude-sonnet-4-6".into(),
        api_key: Some("fixture-key".into()),
        base_url: Some(format!("http://{addr}")),
        effort: Some("high".into()),
        supports_stream_options: false,
    });
    let payload = provider
        .build_messages_payload(&Conversation::default(), "", &[])
        .expect("gateway capability");
    assert!(
        server.join().expect("server thread"),
        "metadata request was missing"
    );
    let payload: serde_json::Value = serde_json::from_str(&payload.to_compact_json()).unwrap();
    assert_eq!(payload["max_tokens"], 262144);
    assert!(payload.get("thinking").is_none());
}

#[test]
fn anthropic_thinking_budget_cannot_exceed_model_capability() {
    let provider = AnthropicProvider::new(ProviderConfig {
        provider_key: "anthropic".into(),
        model: "claude-haiku-4-5-20251001".into(),
        api_key: Some("fixture-key".into()),
        base_url: None,
        effort: Some("max".into()),
        supports_stream_options: false,
    });
    assert!(provider
        .build_messages_payload(&Conversation::default(), "", &[])
        .is_err());
}

#[test]
fn anthropic_messages_use_model_maximum_instead_of_caller_budget() {
    for provider_key in ["anthropic"] {
        for (model, expected) in [
            ("claude-opus-4-7", 128_000),
            ("claude-opus-4-6", 128_000),
            ("claude-sonnet-4-6", 128_000),
            ("claude-sonnet-4-6-1m", 128_000),
            ("claude-haiku-4-5-20251001", 64_000),
        ] {
            let provider = AnthropicProvider::new(ProviderConfig {
                provider_key: provider_key.into(),
                model: model.into(),
                api_key: Some("fixture-key".into()),
                base_url: None,
                effort: None,
                supports_stream_options: false,
            });
            let payload = provider
                .build_messages_payload(&Conversation::default(), "", &[])
                .expect("model capability");
            let payload: serde_json::Value =
                serde_json::from_str(&payload.to_compact_json()).expect("request JSON");
            assert_eq!(payload["max_tokens"], expected, "{provider_key}: {model}");
        }
    }
}

#[test]
fn anthropic_provider_streams_sse_text_thinking_tools_and_usage() {
    let listener = TcpListener::bind("127.0.0.1:0").expect("bind test server");
    let addr = listener.local_addr().expect("server addr");
    let server = thread::spawn(move || {
        let (mut stream, _) = listener.accept().expect("accept request");
        let request = read_http_request(&mut stream);
        assert!(request.starts_with("POST /v1/messages HTTP/1.1"));
        assert!(
            request.contains("x-api-key: fixture-key")
                || request.contains("X-Api-Key: fixture-key"),
            "missing api key header: {request}"
        );
        assert!(
            request.contains("anthropic-version: 2023-06-01"),
            "missing anthropic version header: {request}"
        );
        assert!(
            request.contains("\"stream\":true"),
            "missing stream flag: {request}"
        );
        assert!(
            request.contains("\"input_schema\"") && !request.contains("\"function\""),
            "tools should use anthropic schema shape: {request}"
        );

        let body = concat!(
            "data: {\"type\":\"message_start\",\"message\":{\"id\":\"msg_stream\",\"usage\":{\"input_tokens\":5,\"output_tokens\":0}}}\n\n",
            "data: {\"type\":\"content_block_start\",\"index\":0,\"content_block\":{\"type\":\"text\",\"text\":\"\"}}\n\n",
            "data: {\"type\":\"content_block_delta\",\"index\":0,\"delta\":{\"type\":\"text_delta\",\"text\":\"hel\"}}\n\n",
            "data: {\"type\":\"content_block_delta\",\"index\":0,\"delta\":{\"type\":\"text_delta\",\"text\":\"lo\"}}\n\n",
            "data: {\"type\":\"content_block_delta\",\"index\":0,\"delta\":{\"type\":\"thinking_delta\",\"thinking\":\"think \"}}\n\n",
            "data: {\"type\":\"content_block_start\",\"index\":1,\"content_block\":{\"type\":\"tool_use\",\"id\":\"tool_1\",\"name\":\"read_file\",\"input\":{}}}\n\n",
            "data: {\"type\":\"content_block_delta\",\"index\":1,\"delta\":{\"type\":\"input_json_delta\",\"partial_json\":\"{\\\"path\\\"\"}}\n\n",
            "data: {\"type\":\"content_block_delta\",\"index\":1,\"delta\":{\"type\":\"input_json_delta\",\"partial_json\":\":\\\"README.md\\\"}\"}}\n\n",
            "data: {\"type\":\"content_block_stop\",\"index\":1}\n\n",
            "data: {\"type\":\"message_delta\",\"delta\":{\"stop_reason\":\"tool_use\"},\"usage\":{\"output_tokens\":7}}\n\n",
            "data: {\"type\":\"message_stop\"}\n\n",
        );
        write!(
            stream,
            "HTTP/1.1 200 OK\r\ncontent-type: text/event-stream\r\ncontent-length: {}\r\n\r\n{}",
            body.len(),
            body
        )
        .expect("write response");
    });

    let provider = AnthropicProvider::new(ProviderConfig {
        provider_key: "anthropic".into(),
        model: "claude-opus-4-7".into(),
        api_key: Some("fixture-key".into()),
        base_url: Some(format!("http://{addr}")),
        effort: None,
        supports_stream_options: false,
    });
    let mut conversation = Conversation::default();
    conversation.add_user_message(AgentMessageContent::Text("hello".into()));

    let events = provider.stream_events(
        &conversation,
        "system prompt",
        &[ToolDefinition {
            name: "read_file".into(),
            description: "Read a file".into(),
            input_schema: json::object([("type", json::string("object"))]),
        }],
        100,
    );
    server.join().expect("server thread");

    assert_eq!(
        events,
        vec![
            StreamEvent::MessageStart(MessageStartEvent {
                message_id: "msg_stream".into(),
            }),
            StreamEvent::TextDelta(TextDeltaEvent { text: "hel".into() }),
            StreamEvent::TextDelta(TextDeltaEvent { text: "lo".into() }),
            StreamEvent::ThinkingDelta(ThinkingDeltaEvent {
                text: "think ".into(),
            }),
            StreamEvent::ToolUseStart(ToolUseStartEvent {
                tool_use_id: "tool_1".into(),
                name: "read_file".into(),
            }),
            StreamEvent::ToolInputDelta(ToolInputDeltaEvent {
                tool_use_id: "tool_1".into(),
                partial_json: "{\"path\"".into(),
            }),
            StreamEvent::ToolInputDelta(ToolInputDeltaEvent {
                tool_use_id: "tool_1".into(),
                partial_json: ":\"README.md\"}".into(),
            }),
            StreamEvent::ToolUseEnd(ToolUseEndEvent {
                tool_use_id: "tool_1".into(),
                name: "read_file".into(),
                input: json::object([("path", json::string("README.md"))]),
            }),
            StreamEvent::MessageEnd(MessageEndEvent {
                stop_reason: "tool_use".into(),
                usage: Usage {
                    input_tokens: 5,
                    output_tokens: 7,
                    cache_creation_input_tokens: 0,
                    cache_read_input_tokens: 0,
                },
            }),
        ]
    );
}

#[test]
fn anthropic_provider_recovers_concatenated_tool_inputs_like_python() {
    let listener = TcpListener::bind("127.0.0.1:0").expect("bind test server");
    let addr = listener.local_addr().expect("server addr");
    let server = thread::spawn(move || {
        let (mut stream, _) = listener.accept().expect("accept request");
        let _request = read_http_request(&mut stream);
        let body = concat!(
            "data: {\"type\":\"message_start\",\"message\":{\"id\":\"msg_concat\",\"usage\":{\"input_tokens\":5,\"output_tokens\":0}}}\n\n",
            "data: {\"type\":\"content_block_start\",\"index\":0,\"content_block\":{\"type\":\"tool_use\",\"id\":\"tool_1\",\"name\":\"read_file\",\"input\":{}}}\n\n",
            "data: {\"type\":\"content_block_delta\",\"index\":0,\"delta\":{\"type\":\"input_json_delta\",\"partial_json\":\"{\\\"path\\\":\\\"a.txt\\\"}{\\\"path\\\":\\\"b.txt\\\"}\"}}\n\n",
            "data: {\"type\":\"content_block_stop\",\"index\":0}\n\n",
            "data: {\"type\":\"message_delta\",\"delta\":{\"stop_reason\":\"tool_use\"},\"usage\":{\"output_tokens\":7}}\n\n",
            "data: {\"type\":\"message_stop\"}\n\n",
        );
        write!(
            stream,
            "HTTP/1.1 200 OK\r\ncontent-type: text/event-stream\r\ncontent-length: {}\r\n\r\n{}",
            body.len(),
            body
        )
        .expect("write response");
    });

    let provider = AnthropicProvider::new(ProviderConfig {
        provider_key: "anthropic".into(),
        model: "claude-opus-4-7".into(),
        api_key: Some("fixture-key".into()),
        base_url: Some(format!("http://{addr}")),
        effort: None,
        supports_stream_options: false,
    });
    let mut conversation = Conversation::default();
    conversation.add_user_message(AgentMessageContent::Text("hello".into()));

    let events = provider.stream_events(&conversation, "system prompt", &[], 100);
    server.join().expect("server thread");

    assert_eq!(
        &events[..4],
        &[
            StreamEvent::MessageStart(MessageStartEvent {
                message_id: "msg_concat".into(),
            }),
            StreamEvent::ToolUseStart(ToolUseStartEvent {
                tool_use_id: "tool_1".into(),
                name: "read_file".into(),
            }),
            StreamEvent::ToolInputDelta(ToolInputDeltaEvent {
                tool_use_id: "tool_1".into(),
                partial_json: "{\"path\":\"a.txt\"}{\"path\":\"b.txt\"}".into(),
            }),
            StreamEvent::ToolUseEnd(ToolUseEndEvent {
                tool_use_id: "tool_1".into(),
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
            usage: Usage {
                input_tokens: 5,
                output_tokens: 7,
                cache_creation_input_tokens: 0,
                cache_read_input_tokens: 0,
            },
        }))
    );
}

#[test]
fn anthropic_without_effort_does_not_emit_thinking_payload() {
    let provider = AnthropicProvider::new(ProviderConfig {
        provider_key: "anthropic".into(),
        model: "claude-sonnet-4-6".into(),
        api_key: Some("fixture-key".into()),
        base_url: None,
        effort: None,
        supports_stream_options: false,
    });
    let mut conversation = Conversation::default();
    conversation.add_user_message(AgentMessageContent::Text("hello".into()));

    let payload = provider
        .build_messages_payload(&conversation, "system prompt", &[])
        .expect("model capability");
    let request_body = payload.to_compact_json();

    assert!(
        request_body.contains("\"max_tokens\":128000"),
        "anthropic should use the model maximum: {request_body}"
    );
    assert!(
        !request_body.contains("\"thinking\"") && !request_body.contains("\"budget_tokens\""),
        "anthropic should not emit Anthropic thinking payload: {request_body}"
    );
}

#[test]
fn anthropic_provider_falls_back_to_non_streaming_when_stream_request_fails() {
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
            "HTTP/1.1 503 Service Unavailable\r\ncontent-type: application/json\r\ncontent-length: {}\r\n\r\n{}",
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
            "id": "msg_fallback",
            "content": [{"type": "text", "text": "fallback anthropic"}],
            "stop_reason": "end_turn",
            "usage": {"input_tokens": 8, "output_tokens": 3}
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

    let provider = AnthropicProvider::new(ProviderConfig {
        provider_key: "anthropic".into(),
        model: "claude-opus-4-7".into(),
        api_key: Some("fixture-key".into()),
        base_url: Some(format!("http://{addr}")),
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
                message_id: "msg_fallback".into(),
            }),
            StreamEvent::TextDelta(TextDeltaEvent {
                text: "fallback anthropic".into(),
            }),
            StreamEvent::MessageEnd(MessageEndEvent {
                stop_reason: "end_turn".into(),
                usage: Usage {
                    input_tokens: 8,
                    output_tokens: 3,
                    cache_creation_input_tokens: 0,
                    cache_read_input_tokens: 0,
                },
            }),
        ]
    );
}

#[test]
fn anthropic_provider_retries_retryable_non_streaming_fallback_like_python() {
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
            "HTTP/1.1 503 Service Unavailable\r\ncontent-type: application/json\r\ncontent-length: {}\r\n\r\n{}",
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
            "id": "msg_retried_fallback",
            "content": [{"type": "text", "text": "retried anthropic"}],
            "stop_reason": "end_turn",
            "usage": {"input_tokens": 7, "output_tokens": 2}
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

    let provider = AnthropicProvider::new(ProviderConfig {
        provider_key: "anthropic".into(),
        model: "claude-haiku-4-5-20251001".into(),
        api_key: Some("fixture-key".into()),
        base_url: Some(format!("http://{addr}")),
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
                message_id: "msg_retried_fallback".into(),
            }),
            StreamEvent::TextDelta(TextDeltaEvent {
                text: "retried anthropic".into(),
            }),
            StreamEvent::MessageEnd(MessageEndEvent {
                stop_reason: "end_turn".into(),
                usage: Usage {
                    input_tokens: 7,
                    output_tokens: 2,
                    cache_creation_input_tokens: 0,
                    cache_read_input_tokens: 0,
                },
            }),
        ]
    );
}

#[test]
fn anthropic_provider_tombstones_partial_stream_before_fallback() {
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
            "data: {\"type\":\"message_start\",\"message\":{\"id\":\"msg_partial\",\"usage\":{\"input_tokens\":1}}}\n\n",
            "data: {\"type\":\"content_block_delta\",\"index\":0,\"delta\":{\"type\":\"text_delta\",\"text\":\"partial\"}}\n\n",
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
            "id": "msg_fallback_after_partial",
            "content": [{"type": "text", "text": "fallback anthropic"}],
            "stop_reason": "end_turn",
            "usage": {"input_tokens": 8, "output_tokens": 3}
        }"#;
        write!(
            stream,
            "HTTP/1.1 200 OK\r\ncontent-type: application/json\r\ncontent-length: {}\r\n\r\n{}",
            fallback_body.len(),
            fallback_body
        )
        .expect("write fallback response");
    });

    let provider = AnthropicProvider::new(ProviderConfig {
        provider_key: "anthropic".into(),
        model: "claude-opus-4-7".into(),
        api_key: Some("fixture-key".into()),
        base_url: Some(format!("http://{addr}")),
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
                message_id: "msg_partial".into(),
            }),
            StreamEvent::TextDelta(TextDeltaEvent {
                text: "partial".into(),
            }),
            StreamEvent::Tombstone(TombstoneEvent {
                message_id: "msg_partial".into(),
            }),
            StreamEvent::MessageStart(MessageStartEvent {
                message_id: "msg_fallback_after_partial".into(),
            }),
            StreamEvent::TextDelta(TextDeltaEvent {
                text: "fallback anthropic".into(),
            }),
            StreamEvent::MessageEnd(MessageEndEvent {
                stop_reason: "end_turn".into(),
                usage: Usage {
                    input_tokens: 8,
                    output_tokens: 3,
                    cache_creation_input_tokens: 0,
                    cache_read_input_tokens: 0,
                },
            }),
        ]
    );
}

#[test]
fn anthropic_provider_degrades_model_when_stream_and_primary_complete_fail() {
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
            stream_request.contains("\"model\":\"claude-opus-4-7\""),
            "stream request should use primary model: {stream_request}"
        );
        let stream_body = r#"{"error":"stream failed"}"#;
        write!(
            stream,
            "HTTP/1.1 503 Service Unavailable\r\ncontent-type: application/json\r\ncontent-length: {}\r\n\r\n{}",
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
                primary_complete_request.contains("\"model\":\"claude-opus-4-7\""),
                "primary complete request should use primary model: {primary_complete_request}"
            );
            let complete_body = format!(r#"{{"error":"primary complete failed {attempt}"}}"#);
            write!(
                stream,
                "HTTP/1.1 503 Service Unavailable\r\ncontent-type: application/json\r\ncontent-length: {}\r\n\r\n{}",
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
            fallback_request.contains("\"model\":\"claude-haiku-4-5-20251001\""),
            "fallback model request should use degraded model: {fallback_request}"
        );
        let fallback_body = r#"{
            "id": "msg_model_fallback",
            "content": [{"type": "text", "text": "fallback haiku"}],
            "stop_reason": "end_turn",
            "usage": {"input_tokens": 6, "output_tokens": 2}
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

    let provider = AnthropicProvider::new(ProviderConfig {
        provider_key: "anthropic".into(),
        model: "claude-opus-4-7".into(),
        api_key: Some("fixture-key".into()),
        base_url: Some(format!("http://{addr}")),
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
                message_id: "msg_model_fallback".into(),
            }),
            StreamEvent::TextDelta(TextDeltaEvent {
                text: "fallback haiku".into(),
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
