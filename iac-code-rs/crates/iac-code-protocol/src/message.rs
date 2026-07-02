use crate::json::{self, JsonValue};
use crate::ToJsonValue;

#[derive(Clone, Debug, PartialEq)]
pub struct TextBlock {
    pub text: String,
}

impl ToJsonValue for TextBlock {
    fn to_json_value(&self) -> JsonValue {
        json::object([
            ("type", json::string("text")),
            ("text", json::string(&self.text)),
        ])
    }
}

#[derive(Clone, Debug, PartialEq)]
pub struct ToolUseBlock {
    pub id: String,
    pub name: String,
    pub input: JsonValue,
}

impl ToJsonValue for ToolUseBlock {
    fn to_json_value(&self) -> JsonValue {
        json::object([
            ("type", json::string("tool_use")),
            ("id", json::string(&self.id)),
            ("name", json::string(&self.name)),
            ("input", self.input.clone()),
        ])
    }
}

#[derive(Clone, Debug, PartialEq)]
pub struct ToolResultBlock {
    pub tool_use_id: String,
    pub content: String,
    pub is_error: bool,
}

impl ToJsonValue for ToolResultBlock {
    fn to_json_value(&self) -> JsonValue {
        json::object([
            ("type", json::string("tool_result")),
            ("tool_use_id", json::string(&self.tool_use_id)),
            ("content", json::string(&self.content)),
            ("is_error", json::bool_value(self.is_error)),
        ])
    }
}

#[derive(Clone, Debug, PartialEq)]
pub struct ThinkingBlock {
    pub thinking: String,
}

impl ToJsonValue for ThinkingBlock {
    fn to_json_value(&self) -> JsonValue {
        json::object([
            ("type", json::string("thinking")),
            ("thinking", json::string(&self.thinking)),
        ])
    }
}

#[derive(Clone, Debug, PartialEq)]
pub struct ImageBlock {
    pub media_type: String,
    pub data: String,
}

impl ToJsonValue for ImageBlock {
    fn to_json_value(&self) -> JsonValue {
        json::object([
            ("type", json::string("image")),
            ("media_type", json::string(&self.media_type)),
            ("data", json::string(&self.data)),
        ])
    }
}

#[derive(Clone, Debug, PartialEq)]
pub enum AgentContentBlock {
    Text(TextBlock),
    ToolUse(ToolUseBlock),
    ToolResult(ToolResultBlock),
    Thinking(ThinkingBlock),
    Image(ImageBlock),
}

impl ToJsonValue for AgentContentBlock {
    fn to_json_value(&self) -> JsonValue {
        match self {
            AgentContentBlock::Text(block) => block.to_json_value(),
            AgentContentBlock::ToolUse(block) => block.to_json_value(),
            AgentContentBlock::ToolResult(block) => block.to_json_value(),
            AgentContentBlock::Thinking(block) => block.to_json_value(),
            AgentContentBlock::Image(block) => block.to_json_value(),
        }
    }
}

#[derive(Clone, Debug, PartialEq)]
pub enum AgentMessageContent {
    Text(String),
    Blocks(Vec<AgentContentBlock>),
}

impl AgentMessageContent {
    fn to_json_value(&self) -> JsonValue {
        match self {
            AgentMessageContent::Text(text) => json::string(text),
            AgentMessageContent::Blocks(blocks) => {
                json::array(blocks.iter().map(|block| block.to_json_value()))
            }
        }
    }
}

#[derive(Clone, Debug, PartialEq)]
pub struct AgentMessage {
    pub role: String,
    pub content: AgentMessageContent,
    pub token_count: u64,
    pub elapsed_seconds: f64,
}

impl AgentMessage {
    pub fn to_api_json_value(&self) -> JsonValue {
        json::object([
            ("role", json::string(&self.role)),
            ("content", self.content.to_json_value()),
        ])
    }

    pub fn get_text(&self) -> String {
        match &self.content {
            AgentMessageContent::Text(text) => text.clone(),
            AgentMessageContent::Blocks(blocks) => blocks
                .iter()
                .filter_map(|block| match block {
                    AgentContentBlock::Text(text) => Some(text.text.as_str()),
                    _ => None,
                })
                .collect::<Vec<_>>()
                .join("\n"),
        }
    }

    pub fn get_tool_use_blocks(&self) -> Vec<&ToolUseBlock> {
        let AgentMessageContent::Blocks(blocks) = &self.content else {
            return Vec::new();
        };
        blocks
            .iter()
            .filter_map(|block| match block {
                AgentContentBlock::ToolUse(tool_use) => Some(tool_use),
                _ => None,
            })
            .collect()
    }

    pub fn get_tool_result_blocks(&self) -> Vec<&ToolResultBlock> {
        let AgentMessageContent::Blocks(blocks) = &self.content else {
            return Vec::new();
        };
        blocks
            .iter()
            .filter_map(|block| match block {
                AgentContentBlock::ToolResult(tool_result) => Some(tool_result),
                _ => None,
            })
            .collect()
    }
}

impl ToJsonValue for AgentMessage {
    fn to_json_value(&self) -> JsonValue {
        json::object([
            ("role", json::string(&self.role)),
            ("content", self.content.to_json_value()),
            ("token_count", json::number(self.token_count)),
            ("elapsed_seconds", json::float(self.elapsed_seconds)),
        ])
    }
}

#[derive(Clone, Debug, Default, PartialEq)]
pub struct Conversation {
    pub messages: Vec<AgentMessage>,
}

impl Conversation {
    pub fn add_user_message(&mut self, content: AgentMessageContent) -> &AgentMessage {
        self.messages.push(AgentMessage {
            role: "user".into(),
            content,
            token_count: 0,
            elapsed_seconds: 0.0,
        });
        self.messages.last().expect("message was just pushed")
    }

    pub fn add_assistant_message(&mut self, content: AgentMessageContent) -> &AgentMessage {
        self.messages.push(AgentMessage {
            role: "assistant".into(),
            content,
            token_count: 0,
            elapsed_seconds: 0.0,
        });
        self.messages.last().expect("message was just pushed")
    }

    pub fn to_api_json_value(&self) -> JsonValue {
        json::array(self.messages.iter().map(AgentMessage::to_api_json_value))
    }
}
