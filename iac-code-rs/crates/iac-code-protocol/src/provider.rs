use crate::json::{self, JsonValue};
use crate::{ToJsonValue, Usage};

#[derive(Clone, Debug, PartialEq)]
pub struct ToolDefinition {
    pub name: String,
    pub description: String,
    pub input_schema: JsonValue,
}

impl ToJsonValue for ToolDefinition {
    fn to_json_value(&self) -> JsonValue {
        json::object([
            ("name", json::string(&self.name)),
            ("description", json::string(&self.description)),
            ("input_schema", self.input_schema.clone()),
        ])
    }
}

#[derive(Clone, Debug, PartialEq)]
pub struct ProviderContentBlock {
    pub type_name: String,
    pub text: Option<String>,
    pub tool_use_id: Option<String>,
    pub name: Option<String>,
    pub input: Option<JsonValue>,
    pub content: Option<String>,
    pub is_error: bool,
    pub media_type: Option<String>,
    pub data: Option<String>,
}

impl ProviderContentBlock {
    pub fn text(text: impl Into<String>) -> Self {
        Self {
            type_name: "text".into(),
            text: Some(text.into()),
            tool_use_id: None,
            name: None,
            input: None,
            content: None,
            is_error: false,
            media_type: None,
            data: None,
        }
    }

    pub fn tool_use(
        tool_use_id: impl Into<String>,
        name: impl Into<String>,
        input: JsonValue,
    ) -> Self {
        Self {
            type_name: "tool_use".into(),
            text: None,
            tool_use_id: Some(tool_use_id.into()),
            name: Some(name.into()),
            input: Some(input),
            content: None,
            is_error: false,
            media_type: None,
            data: None,
        }
    }

    pub fn tool_result(
        tool_use_id: impl Into<String>,
        content: impl Into<String>,
        is_error: bool,
    ) -> Self {
        Self {
            type_name: "tool_result".into(),
            text: None,
            tool_use_id: Some(tool_use_id.into()),
            name: None,
            input: None,
            content: Some(content.into()),
            is_error,
            media_type: None,
            data: None,
        }
    }
}

impl ToJsonValue for ProviderContentBlock {
    fn to_json_value(&self) -> JsonValue {
        json::object([
            ("type", json::string(&self.type_name)),
            ("text", optional_string(&self.text)),
            ("tool_use_id", optional_string(&self.tool_use_id)),
            ("name", optional_string(&self.name)),
            ("input", optional_json(&self.input)),
            ("content", optional_string(&self.content)),
            ("is_error", json::bool_value(self.is_error)),
            ("media_type", optional_string(&self.media_type)),
            ("data", optional_string(&self.data)),
        ])
    }
}

#[derive(Clone, Debug, PartialEq)]
pub enum ProviderMessageContent {
    Text(String),
    Blocks(Vec<ProviderContentBlock>),
}

impl ProviderMessageContent {
    fn to_json_value(&self) -> JsonValue {
        match self {
            ProviderMessageContent::Text(text) => json::string(text),
            ProviderMessageContent::Blocks(blocks) => {
                json::array(blocks.iter().map(|block| block.to_json_value()))
            }
        }
    }
}

#[derive(Clone, Debug, PartialEq)]
pub struct ProviderMessage {
    pub role: String,
    pub content: ProviderMessageContent,
}

impl ProviderMessage {
    pub fn user(text: impl Into<String>) -> Self {
        Self {
            role: "user".into(),
            content: ProviderMessageContent::Text(text.into()),
        }
    }

    pub fn assistant_tool_use(
        tool_use_id: impl Into<String>,
        name: impl Into<String>,
        input: JsonValue,
    ) -> Self {
        Self {
            role: "assistant".into(),
            content: ProviderMessageContent::Blocks(vec![ProviderContentBlock::tool_use(
                tool_use_id,
                name,
                input,
            )]),
        }
    }

    pub fn tool_result(
        tool_use_id: impl Into<String>,
        content: impl Into<String>,
        is_error: bool,
    ) -> Self {
        Self {
            role: "user".into(),
            content: ProviderMessageContent::Blocks(vec![ProviderContentBlock::tool_result(
                tool_use_id,
                content,
                is_error,
            )]),
        }
    }
}

impl ToJsonValue for ProviderMessage {
    fn to_json_value(&self) -> JsonValue {
        json::object([
            ("role", json::string(&self.role)),
            ("content", self.content.to_json_value()),
        ])
    }
}

#[derive(Clone, Debug, PartialEq)]
pub struct NonStreamingResponse {
    pub message_id: String,
    pub text: String,
    pub tool_uses: Vec<JsonValue>,
    pub stop_reason: String,
    pub usage: Usage,
    pub thinking: String,
}

impl ToJsonValue for NonStreamingResponse {
    fn to_json_value(&self) -> JsonValue {
        json::object([
            ("message_id", json::string(&self.message_id)),
            ("text", json::string(&self.text)),
            ("tool_uses", json::array(self.tool_uses.clone())),
            ("stop_reason", json::string(&self.stop_reason)),
            ("usage", self.usage.to_json_value()),
            ("thinking", json::string(&self.thinking)),
        ])
    }
}

fn optional_string(value: &Option<String>) -> JsonValue {
    value.as_ref().map_or_else(json::null, json::string)
}

fn optional_json(value: &Option<JsonValue>) -> JsonValue {
    value.clone().unwrap_or(JsonValue::Null)
}
