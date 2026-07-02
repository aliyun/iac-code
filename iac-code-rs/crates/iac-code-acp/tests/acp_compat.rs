use iac_code_acp::convert::{
    acp_blocks_to_agent_message_content, acp_blocks_to_multimodal, acp_blocks_to_prompt_text,
    history_message_to_updates, tool_kind, AcpContentBlock, AcpEventConverter, MultimodalPart,
    SessionUpdate, ToolStatus,
};
use iac_code_acp::state::{extract_key_argument, TurnState};
use iac_code_protocol::json::{self, JsonValue};
use iac_code_protocol::message::{
    AgentContentBlock, AgentMessage, AgentMessageContent, ImageBlock, TextBlock, ThinkingBlock,
    ToolResultBlock, ToolUseBlock,
};
use iac_code_protocol::{
    CompactionEvent, ErrorEvent, MessageEndEvent, PlanEvent, PlanStep, StreamEvent, TextDeltaEvent,
    ThinkingDeltaEvent, ToolInputDeltaEvent, ToolResultEvent, ToolUseEndEvent, ToolUseStartEvent,
    Usage,
};

#[test]
fn acp_content_blocks_convert_to_prompt_text_and_multimodal_parts() {
    let blocks = vec![
        AcpContentBlock::Text {
            text: "hello".into(),
        },
        AcpContentBlock::EmbeddedTextResource {
            uri: "file:///main.tf".into(),
            text: "resource".into(),
        },
        AcpContentBlock::ResourceLink {
            uri: "file:///vars.tf".into(),
            name: "vars.tf".into(),
        },
        AcpContentBlock::Image {
            mime_type: "image/png".into(),
            data: "base64-image".into(),
        },
        AcpContentBlock::Audio {
            mime_type: "audio/wav".into(),
            data: "base64-audio".into(),
        },
    ];

    assert_eq!(
        acp_blocks_to_prompt_text(&blocks),
        "hello\n\n<resource uri='file:///main.tf'>\nresource\n</resource>\n\n<resource_link uri='file:///vars.tf' name='vars.tf' />\n\n[image: image/png]\n\n[audio: audio/wav]"
    );
    assert_eq!(
        acp_blocks_to_multimodal(&blocks),
        vec![
            MultimodalPart::Text {
                text: "hello".into(),
            },
            MultimodalPart::Text {
                text: "<resource uri='file:///main.tf'>\nresource\n</resource>".into(),
            },
            MultimodalPart::Text {
                text: "<resource_link uri='file:///vars.tf' name='vars.tf' />".into(),
            },
            MultimodalPart::Image {
                mime_type: "image/png".into(),
                data: "base64-image".into(),
            },
            MultimodalPart::Audio {
                mime_type: "audio/wav".into(),
                data: "base64-audio".into(),
            },
        ]
    );
}

#[test]
fn acp_content_blocks_convert_to_agent_message_content() {
    assert_eq!(
        acp_blocks_to_agent_message_content(&[AcpContentBlock::Text {
            text: "hello".into(),
        }]),
        AgentMessageContent::Text("hello".into())
    );

    assert_eq!(
        acp_blocks_to_agent_message_content(&[
            AcpContentBlock::Text {
                text: "describe".into(),
            },
            AcpContentBlock::Image {
                mime_type: "image/png".into(),
                data: "base64-image".into(),
            },
            AcpContentBlock::Audio {
                mime_type: "audio/wav".into(),
                data: "base64-audio".into(),
            },
        ]),
        AgentMessageContent::Blocks(vec![
            AgentContentBlock::Text(TextBlock {
                text: "describe".into(),
            }),
            AgentContentBlock::Image(ImageBlock {
                media_type: "image/png".into(),
                data: "base64-image".into(),
            }),
            AgentContentBlock::Text(TextBlock {
                text: "[audio: audio/wav]".into(),
            }),
        ])
    );
}

#[test]
fn acp_resource_attrs_use_python_repr_for_quotes_and_escapes() {
    let blocks = vec![AcpContentBlock::ResourceLink {
        uri: "file:///team's.tf".into(),
        name: "vars\nmain.tf".into(),
    }];
    let expected = r#"<resource_link uri="file:///team's.tf" name='vars\nmain.tf' />"#;

    assert_eq!(acp_blocks_to_prompt_text(&blocks), expected);
    assert_eq!(
        acp_blocks_to_multimodal(&blocks),
        vec![MultimodalPart::Text {
            text: expected.into(),
        }]
    );
}

#[test]
fn turn_state_extracts_titles_from_complete_and_partial_json() {
    assert_eq!(
        extract_key_argument("bash", r#"{"command":"ls -la"}"#),
        "ls -la"
    );
    assert_eq!(
        extract_key_argument("bash", r#"{"command": "echo hello"#),
        "echo hello"
    );
    assert_eq!(extract_key_argument("bash", r#"{"command": 42"#), "");
    assert_eq!(extract_key_argument("unknown", r#"{"command":"ls"}"#), "");

    let mut turn = TurnState::new("turn-1");
    turn.start_tool_call("tool-1", "bash");
    turn.get_tool_call_mut("tool-1")
        .expect("tool state")
        .update_input(r#"{"command": "terraform plan"#);
    assert_eq!(
        turn.get_tool_call("tool-1").expect("tool state").title,
        "bash: terraform plan"
    );
}

#[test]
fn tool_kind_matches_python_mapping_and_suffix_heuristics() {
    assert_eq!(tool_kind("read_file"), "read");
    assert_eq!(tool_kind("write_file"), "edit");
    assert_eq!(tool_kind("grep"), "search");
    assert_eq!(tool_kind("bash"), "execute");
    assert_eq!(tool_kind("aliyun_doc_search"), "fetch");
    assert_eq!(tool_kind("ecs_api"), "execute");
    assert_eq!(tool_kind("unknown"), "other");
}

#[test]
fn event_converter_maps_core_stream_events_to_acp_updates() {
    let mut converter = AcpEventConverter::new("turn-1");

    assert_eq!(
        converter.event_to_updates(&StreamEvent::TextDelta(TextDeltaEvent {
            text: "hello".into()
        })),
        vec![SessionUpdate::agent_message("hello")]
    );
    assert_eq!(
        converter.event_to_updates(&StreamEvent::ThinkingDelta(ThinkingDeltaEvent {
            text: "thinking".into()
        })),
        vec![SessionUpdate::agent_thought("thinking")]
    );

    let start = converter.event_to_updates(&StreamEvent::ToolUseStart(ToolUseStartEvent {
        tool_use_id: "t1".into(),
        name: "bash".into(),
    }));
    assert_eq!(
        start,
        vec![SessionUpdate::tool_call_start(
            "turn-1/t1",
            "bash",
            "execute",
            ToolStatus::Pending,
        )]
    );

    let first_input =
        converter.event_to_updates(&StreamEvent::ToolInputDelta(ToolInputDeltaEvent {
            tool_use_id: "t1".into(),
            partial_json: "{\"command\":\"".into(),
        }));
    let second_input =
        converter.event_to_updates(&StreamEvent::ToolInputDelta(ToolInputDeltaEvent {
            tool_use_id: "t1".into(),
            partial_json: r#"ls"}"#.into(),
        }));
    assert_eq!(first_input[0].content_text(), Some("{\"command\":\""));
    assert_eq!(second_input[0].content_text(), Some(r#"{"command":"ls"}"#));

    assert_eq!(
        converter.event_to_updates(&StreamEvent::ToolUseEnd(ToolUseEndEvent {
            tool_use_id: "t1".into(),
            name: "bash".into(),
            input: json::object([("command", json::string("ls"))]),
        })),
        vec![SessionUpdate::tool_call_progress(
            "turn-1/t1",
            Some("bash"),
            ToolStatus::InProgress,
            Some(r#"{"command":"ls"}"#),
        )]
    );

    let result = converter.event_to_updates(&StreamEvent::ToolResult(ToolResultEvent {
        tool_use_id: "t1".into(),
        tool_name: "bash".into(),
        result: "done".into(),
        is_error: false,
    }));
    assert_eq!(result.len(), 2);
    assert_eq!(result[0].status, Some(ToolStatus::InProgress));
    assert_eq!(result[0].content_text(), Some("done"));
    assert_eq!(result[1].status, Some(ToolStatus::Completed));

    assert_eq!(
        converter.event_to_updates(&StreamEvent::Compaction(CompactionEvent {
            original_tokens: 5000,
            compacted_tokens: 2000,
        })),
        vec![SessionUpdate::agent_message(
            "[Context compacted: 5000 -> 2000 tokens]"
        )]
    );
    assert_eq!(
        converter.event_to_updates(&StreamEvent::Error(ErrorEvent {
            error: "Rate limit".into(),
            is_retryable: true,
        })),
        vec![SessionUpdate::agent_message("[Error] Rate limit")]
    );
    assert_eq!(
        converter.event_to_updates(&StreamEvent::Plan(PlanEvent {
            steps: vec![PlanStep {
                content: "Do it".into(),
                status: "pending".into(),
                priority: "high".into(),
            }],
        })),
        vec![SessionUpdate::plan(vec![("Do it", "pending", "high")])]
    );
    assert_eq!(
        converter
            .with_context_snapshot(|| (1234, 200_000))
            .event_to_updates(&StreamEvent::MessageEnd(MessageEndEvent {
                stop_reason: "end_turn".into(),
                usage: Usage::default(),
            })),
        vec![SessionUpdate::usage(1234, 200_000)]
    );
}

#[test]
fn terminal_tool_results_are_marked_already_displayed() {
    let mut converter = AcpEventConverter::new("turn-1").with_terminal_tools(["bash"]);
    converter.event_to_updates(&StreamEvent::ToolUseStart(ToolUseStartEvent {
        tool_use_id: "t1".into(),
        name: "bash".into(),
    }));

    let updates = converter.event_to_updates(&StreamEvent::ToolResult(ToolResultEvent {
        tool_use_id: "t1".into(),
        tool_name: "bash".into(),
        result: "output".into(),
        is_error: false,
    }));

    assert_eq!(
        updates[0]
            .field_meta
            .as_ref()
            .and_then(|meta| meta.get("already_displayed")),
        Some(&json::bool_value(true))
    );
}

#[test]
fn tool_result_progress_includes_elapsed_timing_meta_like_python() {
    let mut converter = AcpEventConverter::new("turn-1").with_terminal_tools(["bash"]);
    converter.event_to_updates(&StreamEvent::ToolUseStart(ToolUseStartEvent {
        tool_use_id: "t1".into(),
        name: "read_file".into(),
    }));

    let updates = converter.event_to_updates(&StreamEvent::ToolResult(ToolResultEvent {
        tool_use_id: "t1".into(),
        tool_name: "read_file".into(),
        result: "content".into(),
        is_error: false,
    }));

    let meta = updates[0]
        .field_meta
        .as_ref()
        .expect("tool result progress should include field meta");
    let timing = match meta.get("timing") {
        Some(JsonValue::Object(fields)) => fields,
        other => panic!("expected timing object, got {other:?}"),
    };
    assert!(
        matches!(timing.get("elapsed_ms"), Some(JsonValue::Number(_))),
        "timing metadata should include elapsed_ms: {timing:?}"
    );
    assert!(!meta.contains_key("already_displayed"), "{meta:?}");
}

#[test]
fn history_messages_convert_to_acp_replay_updates() {
    let user = AgentMessage {
        role: "user".into(),
        content: AgentMessageContent::Text("hello".into()),
        token_count: 0,
        elapsed_seconds: 0.0,
    };
    assert_eq!(
        history_message_to_updates(&user),
        vec![SessionUpdate::user_message("hello")]
    );

    let assistant = AgentMessage {
        role: "assistant".into(),
        content: AgentMessageContent::Blocks(vec![
            AgentContentBlock::Thinking(ThinkingBlock {
                thinking: "thought".into(),
            }),
            AgentContentBlock::Text(TextBlock {
                text: "answer".into(),
            }),
            AgentContentBlock::ToolUse(ToolUseBlock {
                id: "t1".into(),
                name: "read_file".into(),
                input: json::object([("path", json::string("main.tf"))]),
            }),
        ]),
        token_count: 0,
        elapsed_seconds: 0.0,
    };
    let updates = history_message_to_updates(&assistant);
    assert_eq!(updates[0], SessionUpdate::agent_thought("thought"));
    assert_eq!(updates[1], SessionUpdate::agent_message("answer"));
    assert_eq!(updates[2].session_update, "tool_call");
    assert_eq!(updates[2].status, Some(ToolStatus::Completed));
    assert_eq!(updates[3].content_text(), Some(r#"{"path":"main.tf"}"#));

    let tool_result = AgentMessage {
        role: "user".into(),
        content: AgentMessageContent::Blocks(vec![AgentContentBlock::ToolResult(
            ToolResultBlock {
                tool_use_id: "t1".into(),
                content: "file content".into(),
                is_error: false,
            },
        )]),
        token_count: 0,
        elapsed_seconds: 0.0,
    };
    let updates = history_message_to_updates(&tool_result);
    assert_eq!(updates[0].status, Some(ToolStatus::Completed));
    assert_eq!(updates[0].content_text(), Some("file content"));
}
