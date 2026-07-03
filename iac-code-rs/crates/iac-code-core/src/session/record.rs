use std::collections::BTreeMap;
use std::fs::{self, OpenOptions};
use std::io::{self, Write};
use std::path::Path;

use iac_code_protocol::json::{self, JsonValue};
use iac_code_protocol::message::{
    AgentContentBlock, AgentMessage, AgentMessageContent, ImageBlock, TextBlock, ThinkingBlock,
    ToolResultBlock, ToolUseBlock,
};
use iac_code_protocol::ToJsonValue;

use super::json_value::{
    empty_json_object, object_bool, object_f64, object_fields, object_string, object_u64,
};
use super::{ensure_private_dir, ensure_private_file, path_parent};

pub(super) fn load_session_file(path: &Path) -> io::Result<Vec<AgentMessage>> {
    if !path.exists() {
        return Ok(Vec::new());
    }
    let text = fs::read_to_string(path)?;
    let mut messages = Vec::new();
    for line in text.lines().map(str::trim).filter(|line| !line.is_empty()) {
        let Ok(value) = json::parse(line) else {
            continue;
        };
        let Some(fields) = object_fields(&value) else {
            continue;
        };
        if !fields.contains_key("role") {
            continue;
        }
        if let Some(message) = agent_message_from_json_value(&value) {
            messages.push(message);
        }
    }
    Ok(messages)
}

fn agent_message_from_json_value(value: &JsonValue) -> Option<AgentMessage> {
    let fields = object_fields(value)?;
    let role = object_string(fields, "role")?;
    if role != "user" && role != "assistant" {
        return None;
    }
    let content = match fields.get("content")? {
        JsonValue::String(text) => AgentMessageContent::Text(text.clone()),
        JsonValue::Array(values) => AgentMessageContent::Blocks(
            values
                .iter()
                .filter_map(content_block_from_json_value)
                .collect(),
        ),
        _ => return None,
    };
    Some(AgentMessage {
        role: role.to_owned(),
        content,
        token_count: object_u64(fields, "token_count").unwrap_or(0),
        elapsed_seconds: object_f64(fields, "elapsed_seconds").unwrap_or(0.0),
    })
}

fn content_block_from_json_value(value: &JsonValue) -> Option<AgentContentBlock> {
    let fields = object_fields(value)?;
    match object_string(fields, "type")? {
        "text" => Some(AgentContentBlock::Text(TextBlock {
            text: object_string(fields, "text")?.to_owned(),
        })),
        "tool_use" => Some(AgentContentBlock::ToolUse(ToolUseBlock {
            id: object_string(fields, "id")?.to_owned(),
            name: object_string(fields, "name")?.to_owned(),
            input: fields
                .get("input")
                .cloned()
                .unwrap_or_else(empty_json_object),
        })),
        "tool_result" => Some(AgentContentBlock::ToolResult(ToolResultBlock {
            tool_use_id: object_string(fields, "tool_use_id")?.to_owned(),
            content: object_string(fields, "content")?.to_owned(),
            is_error: object_bool(fields, "is_error").unwrap_or(false),
        })),
        "thinking" => Some(AgentContentBlock::Thinking(ThinkingBlock {
            thinking: object_string(fields, "thinking")?.to_owned(),
        })),
        "image" => Some(AgentContentBlock::Image(ImageBlock {
            media_type: object_string(fields, "media_type")?.to_owned(),
            data: object_string(fields, "data")?.to_owned(),
        })),
        _ => None,
    }
}

pub(super) fn stamp_message(
    message: &AgentMessage,
    cwd: &str,
    session_id: &str,
    git_branch: Option<&str>,
) -> JsonValue {
    let mut fields = match message.to_json_value() {
        JsonValue::Object(fields) => fields,
        _ => BTreeMap::new(),
    };
    fields.insert("session_id".to_owned(), json::string(session_id));
    fields.insert("cwd".to_owned(), json::string(cwd));
    if let Some(git_branch) = git_branch {
        fields.insert("git_branch".to_owned(), json::string(git_branch));
    }
    fields.insert(
        "version".to_owned(),
        json::string(env!("CARGO_PKG_VERSION")),
    );
    JsonValue::Object(fields)
}

pub(super) fn append_jsonl_row(path: &Path, row: &JsonValue) -> io::Result<()> {
    ensure_private_dir(path_parent(path)?)?;
    let mut file = OpenOptions::new().append(true).create(true).open(path)?;
    writeln!(file, "{}", row.to_compact_json())?;
    ensure_private_file(path)
}

pub(super) fn message_tool_uses(message: &AgentMessage) -> Vec<&ToolUseBlock> {
    match &message.content {
        AgentMessageContent::Text(_) => Vec::new(),
        AgentMessageContent::Blocks(blocks) => blocks
            .iter()
            .filter_map(|block| match block {
                AgentContentBlock::ToolUse(tool_use) => Some(tool_use),
                _ => None,
            })
            .collect(),
    }
}
