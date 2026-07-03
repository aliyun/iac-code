use iac_code_protocol::json::{self, JsonValue};
use iac_code_protocol::message::{AgentContentBlock, AgentMessage, AgentMessageContent};
use iac_code_protocol::provider::ToolDefinition;

const MAX_CONTENT_BYTES: usize = 4096;
const TRUNCATED_MARKER: &str = "...[truncated]";

pub fn serialize_user_input(user_input: &str) -> String {
    json::array([json::object([
        (
            "parts",
            json::array([text_part(&truncate_content(user_input))]),
        ),
        ("role", json::string("user")),
    ])])
    .to_compact_json()
}

pub fn serialize_input_messages(messages: &[AgentMessage]) -> String {
    json::array(messages.iter().map(|message| {
        json::object([
            ("parts", json::array(parts_for_message(message))),
            ("role", json::string(message.role.clone())),
        ])
    }))
    .to_compact_json()
}

pub fn serialize_output_messages(text: &str, finish_reason: &str) -> String {
    json::array([json::object([
        ("finish_reason", json::string(finish_reason)),
        ("parts", json::array([text_part(&truncate_content(text))])),
        ("role", json::string("assistant")),
    ])])
    .to_compact_json()
}

pub fn serialize_system_instructions(system: &str) -> String {
    json::array([text_instruction(&truncate_content(system))]).to_compact_json()
}

pub fn serialize_tool_definitions(tools: &[ToolDefinition]) -> String {
    if tools.is_empty() {
        return "[]".to_owned();
    }
    json::array(tools.iter().map(|tool| {
        json::object([
            (
                "description",
                json::string(truncate_content(&tool.description)),
            ),
            ("name", json::string(tool.name.clone())),
            ("type", json::string("function")),
        ])
    }))
    .to_compact_json()
}

pub fn serialize_tool_arguments_json(arguments: &JsonValue) -> String {
    truncate_content(&arguments.to_compact_json())
}

pub fn serialize_tool_arguments_text(arguments: &str) -> String {
    truncate_content(arguments)
}

pub fn serialize_tool_result_text(result: &str) -> String {
    truncate_content(result)
}

fn parts_for_message(message: &AgentMessage) -> Vec<JsonValue> {
    match &message.content {
        AgentMessageContent::Text(text) => vec![text_part(&truncate_content(text))],
        AgentMessageContent::Blocks(blocks) => blocks
            .iter()
            .map(|block| match block {
                AgentContentBlock::Text(text) => text_part(&truncate_content(&text.text)),
                AgentContentBlock::ToolUse(tool_use) => json::object([
                    ("id", json::string(tool_use.id.clone())),
                    ("name", json::string(tool_use.name.clone())),
                    ("type", json::string("tool_call")),
                ]),
                AgentContentBlock::ToolResult(tool_result) => json::object([
                    ("id", json::string(tool_result.tool_use_id.clone())),
                    (
                        "response",
                        json::string(truncate_content(&tool_result.content)),
                    ),
                    ("type", json::string("tool_call_response")),
                ]),
                AgentContentBlock::Thinking(_) => {
                    json::object([("type", json::string("thinking"))])
                }
                AgentContentBlock::Image(_) => json::object([("type", json::string("image"))]),
            })
            .collect(),
    }
}

fn text_part(content: &str) -> JsonValue {
    json::object([
        ("content", json::string(content)),
        ("type", json::string("text")),
    ])
}

fn text_instruction(content: &str) -> JsonValue {
    json::object([
        ("content", json::string(content)),
        ("type", json::string("text")),
    ])
}

fn truncate_content(value: &str) -> String {
    if value.len() <= MAX_CONTENT_BYTES {
        return value.to_owned();
    }
    let mut end = 0;
    for (index, character) in value.char_indices() {
        let next = index + character.len_utf8();
        if next > MAX_CONTENT_BYTES {
            break;
        }
        end = next;
    }
    format!("{}{}", &value[..end], TRUNCATED_MARKER)
}
