use std::collections::BTreeMap;
use std::fmt;

use iac_code_protocol::json::JsonValue;
use iac_code_protocol::message::AgentMessageContent;
use iac_code_protocol::{PermissionRequestEvent, StreamEvent};

use crate::convert::SessionUpdate;
use crate::permissions::{PermissionOption, PermissionResponse, PermissionToolCall};

pub trait AcpClient {
    fn session_update(&mut self, session_id: &str, update: SessionUpdate);

    fn request_permission(
        &mut self,
        session_id: &str,
        options: Vec<PermissionOption>,
        tool_call: PermissionToolCall,
    ) -> PermissionResponse;
}

pub trait AcpAgent {
    fn run_streaming(
        &mut self,
        prompt: &str,
        request_permission: &mut dyn FnMut(PermissionRequestEvent) -> PermissionDecision,
    ) -> Vec<StreamEvent>;

    fn run_streaming_content(
        &mut self,
        content: AgentMessageContent,
        prompt_text: &str,
        request_permission: &mut dyn FnMut(PermissionRequestEvent) -> PermissionDecision,
    ) -> Vec<StreamEvent> {
        let _ = content;
        self.run_streaming(prompt_text, request_permission)
    }

    fn reset(&mut self) -> Result<(), String> {
        Ok(())
    }

    fn compact(&mut self) -> Result<CompactResult, String> {
        Err("compact is unavailable".to_owned())
    }

    fn context_usage_percent(&self) -> f64 {
        0.0
    }

    fn memory_entries(&self) -> Option<Vec<MemoryEntry>> {
        None
    }

    fn load_memory(&self, name: &str) -> Result<Option<MemoryEntry>, String> {
        let Some(memories) = self.memory_entries() else {
            return Err("Memory manager is unavailable.".to_owned());
        };
        Ok(memories.into_iter().find(|memory| memory.name == name))
    }

    fn search_memories(&self, query: &str) -> Result<Vec<MemoryEntry>, String> {
        let Some(memories) = self.memory_entries() else {
            return Err("Memory manager is unavailable.".to_owned());
        };
        let needle = query.trim().to_ascii_lowercase();
        if needle.is_empty() {
            return Ok(Vec::new());
        }
        Ok(memories
            .into_iter()
            .filter(|memory| {
                [
                    memory.name.as_str(),
                    memory.description.as_str(),
                    memory.memory_type.as_str(),
                    memory.content.as_str(),
                ]
                .join("\n")
                .to_ascii_lowercase()
                .contains(&needle)
            })
            .collect())
    }

    fn delete_memory(&mut self, _name: &str) -> Result<bool, String> {
        if self.memory_entries().is_none() {
            return Err("Memory manager is unavailable.".to_owned());
        }
        Ok(false)
    }

    fn rename_session(&mut self, _name: &str) -> Result<RenameOutcome, String> {
        Err("Rename is only available after a session is created.".to_owned())
    }
}

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum PermissionDecision {
    Allow,
    Deny,
    Cancel,
}

impl From<bool> for PermissionDecision {
    fn from(value: bool) -> Self {
        if value {
            Self::Allow
        } else {
            Self::Deny
        }
    }
}

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum CompactStatus {
    Success,
    Empty,
    TooShort,
    TooSmall,
    Failed,
}

#[derive(Clone, Debug, PartialEq, Eq)]
pub struct CompactResult {
    pub status: CompactStatus,
    pub original_tokens: u64,
    pub compacted_tokens: u64,
    pub preserve_recent_turns: u64,
}

#[derive(Clone, Debug, PartialEq, Eq)]
pub struct MemoryEntry {
    pub name: String,
    pub memory_type: String,
    pub description: String,
    pub content: String,
}

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum RenameOutcome {
    Renamed,
    Unchanged,
}

#[derive(Clone, Debug, PartialEq)]
pub struct PromptResponse {
    pub stop_reason: String,
    pub field_meta: BTreeMap<String, JsonValue>,
}

impl PromptResponse {
    pub(super) fn end_turn(field_meta: BTreeMap<String, JsonValue>) -> Self {
        Self {
            stop_reason: "end_turn".to_owned(),
            field_meta,
        }
    }

    pub(super) fn cancelled() -> Self {
        Self {
            stop_reason: "cancelled".to_owned(),
            field_meta: BTreeMap::new(),
        }
    }
}

#[derive(Clone, Debug, PartialEq, Eq)]
pub struct AcpError {
    message: String,
}

impl AcpError {
    pub(super) fn internal(message: impl Into<String>) -> Self {
        Self {
            message: message.into(),
        }
    }
}

impl fmt::Display for AcpError {
    fn fmt(&self, formatter: &mut fmt::Formatter<'_>) -> fmt::Result {
        self.message.fmt(formatter)
    }
}

impl std::error::Error for AcpError {}
