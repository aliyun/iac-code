use std::collections::BTreeMap;
use std::fs;
use std::path::PathBuf;

use iac_code_protocol::json::{self, JsonValue};
use iac_code_protocol::message::{
    AgentContentBlock, AgentMessage, AgentMessageContent, Conversation, ImageBlock, TextBlock,
    ThinkingBlock, ToolResultBlock, ToolUseBlock,
};
use iac_code_protocol::permission::{
    PermissionDecisionReason, PermissionMode, PermissionResult, PermissionRule,
    PermissionRuleSource, PermissionRuleValue, ToolPermissionContext,
};
use iac_code_protocol::provider::{
    NonStreamingResponse, ProviderContentBlock, ProviderMessage, ToolDefinition,
};
use iac_code_protocol::tool::ToolResult;
use iac_code_protocol::{ToJsonValue, Usage};

#[test]
fn protocol_core_values_match_python_fixtures() {
    let fixtures = fixture_lines();
    assert_eq!(
        fixtures.len(),
        21,
        "fixture should cover protocol-core values"
    );

    let text_block = AgentContentBlock::Text(TextBlock {
        text: "hello".into(),
    });
    let tool_use_block = AgentContentBlock::ToolUse(ToolUseBlock {
        id: "toolu_1".into(),
        name: "read_file".into(),
        input: json::object([("path", json::string("README.md"))]),
    });
    let tool_result_block = AgentContentBlock::ToolResult(ToolResultBlock {
        tool_use_id: "toolu_1".into(),
        content: "ok".into(),
        is_error: false,
    });
    let thinking_block = AgentContentBlock::Thinking(ThinkingBlock {
        thinking: "reasoning".into(),
    });
    let image_block = AgentContentBlock::Image(ImageBlock {
        media_type: "image/png".into(),
        data: "ZmFrZQ==".into(),
    });

    let structured_message = AgentMessage {
        role: "assistant".into(),
        content: AgentMessageContent::Blocks(vec![
            text_block.clone(),
            tool_use_block.clone(),
            tool_result_block.clone(),
            thinking_block.clone(),
            image_block.clone(),
        ]),
        token_count: 12,
        elapsed_seconds: 1.5,
    };

    let mut conversation = Conversation::default();
    conversation.add_user_message(AgentMessageContent::Text("hello".into()));
    conversation.add_assistant_message(AgentMessageContent::Blocks(vec![AgentContentBlock::Text(
        TextBlock {
            text: "world".into(),
        },
    )]));

    let mut allow_rules = BTreeMap::new();
    allow_rules.insert("bash".to_owned(), vec!["ls *".to_owned()]);
    let mut deny_rules = BTreeMap::new();
    deny_rules.insert("bash".to_owned(), vec!["rm *".to_owned()]);
    let mut ask_rules = BTreeMap::new();
    ask_rules.insert("write_file".to_owned(), vec!["*".to_owned()]);

    let actual = vec![
        named("agent_text_block", text_block.to_json_value()),
        named("agent_tool_use_block", tool_use_block.to_json_value()),
        named("agent_tool_result_block", tool_result_block.to_json_value()),
        named("agent_thinking_block", thinking_block.to_json_value()),
        named("agent_image_block", image_block.to_json_value()),
        named(
            "agent_string_message",
            AgentMessage {
                role: "user".into(),
                content: AgentMessageContent::Text("hello".into()),
                token_count: 0,
                elapsed_seconds: 0.0,
            }
            .to_json_value(),
        ),
        named(
            "agent_structured_message",
            structured_message.to_json_value(),
        ),
        named(
            "agent_structured_message_api",
            structured_message.to_api_json_value(),
        ),
        named(
            "agent_conversation_api",
            json::object([("messages", conversation.to_api_json_value())]),
        ),
        named(
            "provider_tool_definition",
            ToolDefinition {
                name: "read_file".into(),
                description: "Read a file".into(),
                input_schema: json::object([
                    ("type", json::string("object")),
                    (
                        "properties",
                        json::object([("path", json::object([("type", json::string("string"))]))]),
                    ),
                    ("required", json::array([json::string("path")])),
                ]),
            }
            .to_json_value(),
        ),
        named(
            "provider_content_block_text",
            ProviderContentBlock::text("hello").to_json_value(),
        ),
        named(
            "provider_user_message",
            ProviderMessage::user("hello").to_json_value(),
        ),
        named(
            "provider_assistant_tool_use",
            ProviderMessage::assistant_tool_use(
                "toolu_1",
                "read_file",
                json::object([("path", json::string("README.md"))]),
            )
            .to_json_value(),
        ),
        named(
            "provider_tool_result_message",
            ProviderMessage::tool_result("toolu_1", "ok", false).to_json_value(),
        ),
        named(
            "provider_non_streaming_response",
            NonStreamingResponse {
                message_id: "msg_1".into(),
                text: "hello".into(),
                tool_uses: vec![json::object([
                    ("id", json::string("toolu_1")),
                    ("name", json::string("read_file")),
                    ("input", json::object([("path", json::string("README.md"))])),
                ])],
                stop_reason: "end_turn".into(),
                usage: Usage {
                    input_tokens: 1,
                    output_tokens: 2,
                    cache_creation_input_tokens: 3,
                    cache_read_input_tokens: 4,
                },
                thinking: "reasoning".into(),
            }
            .to_json_value(),
        ),
        named(
            "permission_rule_value",
            PermissionRuleValue {
                tool_name: "bash".into(),
                rule_content: "ls *".into(),
            }
            .to_json_value(),
        ),
        named(
            "permission_rule",
            PermissionRule {
                source: PermissionRuleSource::CliArg,
                behavior: "allow".into(),
                value: PermissionRuleValue {
                    tool_name: "bash".into(),
                    rule_content: "ls *".into(),
                },
            }
            .to_json_value(),
        ),
        named(
            "permission_result",
            PermissionResult {
                behavior: "ask".into(),
                message: "Allow Bash?".into(),
                reason: Some(PermissionDecisionReason {
                    type_name: "rule".into(),
                    detail: "matched ask rule".into(),
                }),
                suggestions: Some(vec![PermissionRuleValue {
                    tool_name: "bash".into(),
                    rule_content: "ls *".into(),
                }]),
            }
            .to_json_value(),
        ),
        named(
            "permission_context",
            ToolPermissionContext {
                mode: PermissionMode::AcceptEdits,
                cwd: "/workspace".into(),
                allow_rules,
                deny_rules,
                ask_rules,
                additional_directories: vec!["/tmp/project".into()],
                trusted_read_directories: vec!["/tmp/project/read".into()],
            }
            .to_json_value(),
        ),
        named(
            "tool_result_success",
            ToolResult::success("ok").to_json_value(),
        ),
        named(
            "tool_result_with_new_messages",
            ToolResult {
                content: "updated".into(),
                is_error: false,
                cancelled: false,
                new_messages: vec![json::object([
                    ("role", json::string("user")),
                    ("content", json::string("extra")),
                ])],
                context_modifier: None,
                stream_events: Vec::new(),
            }
            .to_json_value(),
        ),
    ];

    assert_eq!(actual, fixtures);
}

fn named(name: &str, value: JsonValue) -> String {
    json::object([("name", json::string(name)), ("value", value)]).to_compact_json()
}

fn fixture_lines() -> Vec<String> {
    let mut path = PathBuf::from(env!("CARGO_MANIFEST_DIR"));
    path.push("../../fixtures/compatibility/protocol_core/values.jsonl");
    fs::read_to_string(path)
        .expect("protocol-core fixture should be readable")
        .lines()
        .map(str::to_owned)
        .collect()
}
