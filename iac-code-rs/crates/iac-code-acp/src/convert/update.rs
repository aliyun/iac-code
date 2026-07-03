use std::collections::BTreeMap;

use iac_code_protocol::json::JsonValue;

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum ToolStatus {
    Pending,
    InProgress,
    Completed,
    Failed,
}

impl ToolStatus {
    pub fn as_str(self) -> &'static str {
        match self {
            ToolStatus::Pending => "pending",
            ToolStatus::InProgress => "in_progress",
            ToolStatus::Completed => "completed",
            ToolStatus::Failed => "failed",
        }
    }
}

#[derive(Clone, Debug, PartialEq, Eq)]
pub struct PlanEntry {
    pub content: String,
    pub status: String,
    pub priority: String,
}

#[derive(Clone, Debug, PartialEq, Eq)]
pub struct AvailableCommand {
    pub name: String,
    pub description: String,
    pub input_hint: Option<String>,
}

impl AvailableCommand {
    pub fn new(
        name: impl Into<String>,
        description: impl Into<String>,
        input_hint: Option<String>,
    ) -> Self {
        Self {
            name: name.into(),
            description: description.into(),
            input_hint,
        }
    }
}

#[derive(Clone, Debug, PartialEq)]
pub struct SessionUpdate {
    pub session_update: String,
    pub text: Option<String>,
    pub tool_call_id: Option<String>,
    pub title: Option<String>,
    pub kind: Option<String>,
    pub status: Option<ToolStatus>,
    pub contents: Vec<String>,
    pub used: Option<i64>,
    pub size: Option<i64>,
    pub entries: Vec<PlanEntry>,
    pub available_commands: Vec<AvailableCommand>,
    pub field_meta: Option<BTreeMap<String, JsonValue>>,
}

impl SessionUpdate {
    pub fn user_message(text: impl Into<String>) -> Self {
        Self::text_update("user_message_chunk", text)
    }

    pub fn agent_message(text: impl Into<String>) -> Self {
        Self::text_update("agent_message_chunk", text)
    }

    pub fn agent_thought(text: impl Into<String>) -> Self {
        Self::text_update("agent_thought_chunk", text)
    }

    pub fn tool_call_start(
        tool_call_id: impl Into<String>,
        title: impl Into<String>,
        kind: impl Into<String>,
        status: ToolStatus,
    ) -> Self {
        Self {
            session_update: "tool_call".to_owned(),
            text: None,
            tool_call_id: Some(tool_call_id.into()),
            title: Some(title.into()),
            kind: Some(kind.into()),
            status: Some(status),
            contents: Vec::new(),
            used: None,
            size: None,
            entries: Vec::new(),
            available_commands: Vec::new(),
            field_meta: None,
        }
    }

    pub fn tool_call_progress(
        tool_call_id: impl Into<String>,
        title: Option<&str>,
        status: ToolStatus,
        content: Option<&str>,
    ) -> Self {
        Self {
            session_update: "tool_call_update".to_owned(),
            text: None,
            tool_call_id: Some(tool_call_id.into()),
            title: title.map(str::to_owned),
            kind: None,
            status: Some(status),
            contents: content.into_iter().map(str::to_owned).collect(),
            used: None,
            size: None,
            entries: Vec::new(),
            available_commands: Vec::new(),
            field_meta: None,
        }
    }

    pub fn plan(entries: Vec<(&str, &str, &str)>) -> Self {
        Self {
            session_update: "plan".to_owned(),
            text: None,
            tool_call_id: None,
            title: None,
            kind: None,
            status: None,
            contents: Vec::new(),
            used: None,
            size: None,
            entries: entries
                .into_iter()
                .map(|(content, status, priority)| PlanEntry {
                    content: content.to_owned(),
                    status: status.to_owned(),
                    priority: priority.to_owned(),
                })
                .collect(),
            available_commands: Vec::new(),
            field_meta: None,
        }
    }

    pub fn usage(used: i64, size: i64) -> Self {
        Self {
            session_update: "usage_update".to_owned(),
            text: None,
            tool_call_id: None,
            title: None,
            kind: None,
            status: None,
            contents: Vec::new(),
            used: Some(used),
            size: Some(size),
            entries: Vec::new(),
            available_commands: Vec::new(),
            field_meta: None,
        }
    }

    pub fn available_commands(commands: Vec<AvailableCommand>) -> Self {
        Self {
            session_update: "available_commands_update".to_owned(),
            text: None,
            tool_call_id: None,
            title: None,
            kind: None,
            status: None,
            contents: Vec::new(),
            used: None,
            size: None,
            entries: Vec::new(),
            available_commands: commands,
            field_meta: None,
        }
    }

    pub fn content_text(&self) -> Option<&str> {
        self.contents
            .first()
            .map(String::as_str)
            .or(self.text.as_deref())
    }

    fn text_update(kind: &str, text: impl Into<String>) -> Self {
        Self {
            session_update: kind.to_owned(),
            text: Some(text.into()),
            tool_call_id: None,
            title: None,
            kind: None,
            status: None,
            contents: Vec::new(),
            used: None,
            size: None,
            entries: Vec::new(),
            available_commands: Vec::new(),
            field_meta: None,
        }
    }
}
