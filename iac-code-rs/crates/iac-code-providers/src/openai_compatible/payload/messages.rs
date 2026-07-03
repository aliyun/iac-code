use iac_code_protocol::json::{self, JsonValue};
use iac_code_protocol::message::{AgentContentBlock, AgentMessage, AgentMessageContent};

use super::arguments::python_json_string;

pub(super) fn convert_message(message: &AgentMessage) -> Vec<JsonValue> {
    match &message.content {
        AgentMessageContent::Text(text) => vec![json::object([
            ("role", json::string(&message.role)),
            ("content", json::string(text)),
        ])],
        AgentMessageContent::Blocks(blocks) => convert_blocks(&message.role, blocks),
    }
}

fn convert_blocks(role: &str, blocks: &[AgentContentBlock]) -> Vec<JsonValue> {
    let mut messages = Vec::new();
    let text = blocks
        .iter()
        .filter_map(|block| match block {
            AgentContentBlock::Text(text) => Some(text.text.as_str()),
            _ => None,
        })
        .collect::<Vec<_>>()
        .join("");
    let thinking = blocks
        .iter()
        .filter_map(|block| match block {
            AgentContentBlock::Thinking(thinking) => Some(thinking.thinking.as_str()),
            _ => None,
        })
        .collect::<Vec<_>>()
        .join("");
    let tool_uses = blocks
        .iter()
        .filter_map(|block| match block {
            AgentContentBlock::ToolUse(tool_use) => Some(tool_use),
            _ => None,
        })
        .collect::<Vec<_>>();

    if role == "assistant" && (!text.is_empty() || !thinking.is_empty() || !tool_uses.is_empty()) {
        let mut entries = vec![("role", json::string("assistant"))];
        entries.push(if text.is_empty() {
            ("content", json::null())
        } else {
            ("content", json::string(text))
        });
        if !thinking.is_empty() {
            entries.push(("reasoning_content", json::string(thinking)));
        }
        if !tool_uses.is_empty() {
            entries.push((
                "tool_calls",
                json::array(tool_uses.into_iter().map(|tool_use| {
                    json::object([
                        ("id", json::string(&tool_use.id)),
                        ("type", json::string("function")),
                        (
                            "function",
                            json::object([
                                ("name", json::string(&tool_use.name)),
                                (
                                    "arguments",
                                    json::string(python_json_string(&tool_use.input)),
                                ),
                            ]),
                        ),
                    ])
                })),
            ));
        }
        messages.push(json::object(entries));
    }

    if role == "user" {
        let mut user_parts = Vec::new();
        for block in blocks {
            match block {
                AgentContentBlock::Text(text) => user_parts.push(json::object([
                    ("type", json::string("text")),
                    ("text", json::string(&text.text)),
                ])),
                AgentContentBlock::Image(image) => user_parts.push(json::object([
                    ("type", json::string("image_url")),
                    (
                        "image_url",
                        json::object([(
                            "url",
                            json::string(format!(
                                "data:{};base64,{}",
                                image.media_type, image.data
                            )),
                        )]),
                    ),
                ])),
                _ => {}
            }
        }
        if !user_parts.is_empty() {
            messages.push(json::object([
                ("role", json::string("user")),
                ("content", json::array(user_parts)),
            ]));
        }
    }

    for block in blocks {
        if let AgentContentBlock::ToolResult(tool_result) = block {
            messages.push(json::object([
                ("role", json::string("tool")),
                ("tool_call_id", json::string(&tool_result.tool_use_id)),
                ("content", json::string(&tool_result.content)),
            ]));
        }
    }

    messages
}
