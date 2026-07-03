use iac_code_protocol::json::{self, JsonValue};
use iac_code_protocol::message::{AgentContentBlock, AgentMessage, AgentMessageContent};
use iac_code_protocol::provider::ToolDefinition;

pub(super) fn convert_message(message: &AgentMessage) -> JsonValue {
    match &message.content {
        AgentMessageContent::Text(text) => json::object([
            ("role", json::string(&message.role)),
            ("content", json::string(text)),
        ]),
        AgentMessageContent::Blocks(blocks) => json::object([
            ("role", json::string(&message.role)),
            ("content", json::array(blocks.iter().map(convert_block))),
        ]),
    }
}

fn convert_block(block: &AgentContentBlock) -> JsonValue {
    match block {
        AgentContentBlock::Text(text) => json::object([
            ("type", json::string("text")),
            ("text", json::string(&text.text)),
        ]),
        AgentContentBlock::Thinking(thinking) => json::object([
            ("type", json::string("thinking")),
            ("thinking", json::string(&thinking.thinking)),
        ]),
        AgentContentBlock::ToolUse(tool_use) => json::object([
            ("type", json::string("tool_use")),
            ("id", json::string(&tool_use.id)),
            ("name", json::string(&tool_use.name)),
            ("input", tool_use.input.clone()),
        ]),
        AgentContentBlock::ToolResult(tool_result) => {
            let mut entries = vec![
                ("type", json::string("tool_result")),
                ("tool_use_id", json::string(&tool_result.tool_use_id)),
                ("content", json::string(&tool_result.content)),
            ];
            if tool_result.is_error {
                entries.push(("is_error", json::bool_value(true)));
            }
            json::object(entries)
        }
        AgentContentBlock::Image(image) => json::object([
            ("type", json::string("image")),
            (
                "source",
                json::object([
                    ("type", json::string("base64")),
                    ("media_type", json::string(&image.media_type)),
                    ("data", json::string(&image.data)),
                ]),
            ),
        ]),
    }
}

pub(super) fn convert_tools(tools: &[ToolDefinition]) -> JsonValue {
    json::array(tools.iter().map(|tool| {
        json::object([
            ("name", json::string(&tool.name)),
            ("description", json::string(&tool.description)),
            ("input_schema", tool.input_schema.clone()),
        ])
    }))
}

pub(super) fn anthropic_model_alias(model: &str) -> (&str, Option<&'static str>) {
    match model {
        "claude-sonnet-4-6-1m" => ("claude-sonnet-4-6", Some("context-1m-2025-08-07")),
        _ => (model, None),
    }
}

pub(super) fn anthropic_thinking_budget(
    provider_key: &str,
    model: &str,
    effort: Option<&str>,
) -> Option<u32> {
    if provider_key != "anthropic" {
        return None;
    }
    if !matches!(
        model,
        "claude-opus-4-7"
            | "claude-opus-4-6"
            | "claude-sonnet-4-6"
            | "claude-sonnet-4-6-1m"
            | "claude-haiku-4-5-20251001"
    ) {
        return None;
    }
    match effort
        .map(str::trim)
        .map(str::to_ascii_lowercase)
        .as_deref()
    {
        Some("low") => Some(1024),
        Some("medium") => Some(4096),
        Some("high") => Some(16384),
        Some("xhigh") => Some(32000),
        Some("max") => Some(64000),
        _ => None,
    }
}
