use crate::json::{self, JsonValue};
use crate::stream_event::StreamEvent;
use crate::ToJsonValue;

#[derive(Clone, Debug, Default, PartialEq, Eq)]
pub struct ToolContextModifier {
    pub allowed_tool_rules: Vec<String>,
    pub model_override: Option<String>,
    pub effort_override: Option<String>,
}

impl ToJsonValue for ToolContextModifier {
    fn to_json_value(&self) -> JsonValue {
        let mut fields = vec![(
            "allowed_tool_rules",
            json::array(self.allowed_tool_rules.iter().map(json::string)),
        )];
        if let Some(model_override) = &self.model_override {
            fields.push(("model_override", json::string(model_override)));
        }
        if let Some(effort_override) = &self.effort_override {
            fields.push(("effort_override", json::string(effort_override)));
        }
        json::object(fields)
    }
}

#[derive(Clone, Debug, PartialEq)]
pub struct ToolResult {
    pub content: String,
    pub is_error: bool,
    pub cancelled: bool,
    pub new_messages: Vec<JsonValue>,
    pub context_modifier: Option<ToolContextModifier>,
    pub stream_events: Vec<StreamEvent>,
}

impl ToolResult {
    pub fn success(content: impl Into<String>) -> Self {
        Self {
            content: content.into(),
            is_error: false,
            cancelled: false,
            new_messages: Vec::new(),
            context_modifier: None,
            stream_events: Vec::new(),
        }
    }

    pub fn error(message: impl Into<String>) -> Self {
        Self {
            content: message.into(),
            is_error: true,
            cancelled: false,
            new_messages: Vec::new(),
            context_modifier: None,
            stream_events: Vec::new(),
        }
    }

    pub fn cancelled(message: impl Into<String>) -> Self {
        Self {
            content: message.into(),
            is_error: true,
            cancelled: true,
            new_messages: Vec::new(),
            context_modifier: None,
            stream_events: Vec::new(),
        }
    }

    pub fn with_context_modifier(mut self, context_modifier: ToolContextModifier) -> Self {
        self.context_modifier = Some(context_modifier);
        self
    }

    pub fn with_new_messages(mut self, new_messages: Vec<JsonValue>) -> Self {
        self.new_messages = new_messages;
        self
    }

    pub fn with_stream_events(mut self, stream_events: Vec<StreamEvent>) -> Self {
        self.stream_events = stream_events;
        self
    }
}

impl ToJsonValue for ToolResult {
    fn to_json_value(&self) -> JsonValue {
        let context_modifier = self
            .context_modifier
            .as_ref()
            .map(ToJsonValue::to_json_value)
            .unwrap_or_else(json::null);
        json::object([
            ("content", json::string(&self.content)),
            ("is_error", json::bool_value(self.is_error)),
            ("new_messages", json::array(self.new_messages.clone())),
            ("context_modifier", context_modifier),
        ])
    }
}
