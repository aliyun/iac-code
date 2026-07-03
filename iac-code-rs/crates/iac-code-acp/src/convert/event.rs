use std::collections::{BTreeMap, BTreeSet};
use std::time::Instant;

use iac_code_protocol::json::{self, JsonValue};
use iac_code_protocol::message::{AgentContentBlock, AgentMessage, AgentMessageContent};
use iac_code_protocol::{
    CompactionEvent, ErrorEvent, MessageEndEvent, PlanEvent, StreamEvent, TextDeltaEvent,
    ThinkingDeltaEvent, ToolInputDeltaEvent, ToolResultEvent, ToolUseEndEvent, ToolUseStartEvent,
};

use super::{SessionUpdate, ToolStatus};

pub fn tool_kind(tool_name: &str) -> &'static str {
    match tool_name {
        "read_file" | "list_files" | "read_memory" | "task_list" | "task_get" => "read",
        "write_file" | "edit_file" | "write_memory" => "edit",
        "grep" | "glob" => "search",
        "bash" | "task_stop" | "ros_stack" | "ros_stack_instances" => "execute",
        "web_fetch" | "aliyun_doc_search" => "fetch",
        name if name.ends_with("_api") => "execute",
        name if name.ends_with("_doc_search") => "fetch",
        _ => "other",
    }
}

pub struct AcpEventConverter {
    turn_id: String,
    tool_inputs: BTreeMap<String, String>,
    tool_starts: BTreeMap<String, Instant>,
    terminal_tool_names: BTreeSet<String>,
    context_snapshot: Option<Box<dyn Fn() -> (i64, i64)>>,
}

impl AcpEventConverter {
    pub fn new(turn_id: impl Into<String>) -> Self {
        Self {
            turn_id: turn_id.into(),
            tool_inputs: BTreeMap::new(),
            tool_starts: BTreeMap::new(),
            terminal_tool_names: BTreeSet::new(),
            context_snapshot: None,
        }
    }

    pub fn with_terminal_tools<I, S>(mut self, names: I) -> Self
    where
        I: IntoIterator<Item = S>,
        S: Into<String>,
    {
        self.terminal_tool_names = names.into_iter().map(Into::into).collect();
        self
    }

    pub fn with_context_snapshot<F>(mut self, snapshot: F) -> Self
    where
        F: Fn() -> (i64, i64) + 'static,
    {
        self.context_snapshot = Some(Box::new(snapshot));
        self
    }

    pub fn acp_tool_call_id(&self, tool_use_id: &str) -> String {
        format!("{}/{}", self.turn_id, tool_use_id)
    }

    pub fn event_to_updates(&mut self, event: &StreamEvent) -> Vec<SessionUpdate> {
        match event {
            StreamEvent::TextDelta(TextDeltaEvent { text }) => {
                vec![SessionUpdate::agent_message(text)]
            }
            StreamEvent::ThinkingDelta(ThinkingDeltaEvent { text }) => {
                vec![SessionUpdate::agent_thought(text)]
            }
            StreamEvent::ToolUseStart(ToolUseStartEvent { tool_use_id, name }) => {
                self.tool_starts.insert(tool_use_id.clone(), Instant::now());
                vec![SessionUpdate::tool_call_start(
                    self.acp_tool_call_id(tool_use_id),
                    name,
                    tool_kind(name),
                    ToolStatus::Pending,
                )]
            }
            StreamEvent::ToolInputDelta(ToolInputDeltaEvent {
                tool_use_id,
                partial_json,
            }) => {
                let input = self.tool_inputs.entry(tool_use_id.clone()).or_default();
                input.push_str(partial_json);
                let accumulated = input.clone();
                vec![SessionUpdate::tool_call_progress(
                    self.acp_tool_call_id(tool_use_id),
                    None,
                    ToolStatus::Pending,
                    Some(&accumulated),
                )]
            }
            StreamEvent::ToolUseEnd(ToolUseEndEvent {
                tool_use_id,
                name,
                input,
            }) => vec![SessionUpdate::tool_call_progress(
                self.acp_tool_call_id(tool_use_id),
                Some(name),
                ToolStatus::InProgress,
                Some(&input.to_compact_json()),
            )],
            StreamEvent::ToolResult(ToolResultEvent {
                tool_use_id,
                tool_name,
                result,
                is_error,
            }) => {
                let mut progress = SessionUpdate::tool_call_progress(
                    self.acp_tool_call_id(tool_use_id),
                    None,
                    ToolStatus::InProgress,
                    Some(result),
                );
                let mut field_meta = BTreeMap::new();
                if let Some(started_at) = self.tool_starts.get(tool_use_id) {
                    field_meta.insert(
                        "timing".to_owned(),
                        json::object([(
                            "elapsed_ms",
                            json::number(started_at.elapsed().as_millis() as u64),
                        )]),
                    );
                }
                if self.terminal_tool_names.contains(tool_name) {
                    field_meta.insert("already_displayed".to_owned(), json::bool_value(true));
                }
                if !field_meta.is_empty() {
                    progress.field_meta = Some(field_meta);
                }
                let end_status = if *is_error {
                    ToolStatus::Failed
                } else {
                    ToolStatus::Completed
                };
                let end = SessionUpdate::tool_call_progress(
                    self.acp_tool_call_id(tool_use_id),
                    None,
                    end_status,
                    None,
                );
                vec![progress, end]
            }
            StreamEvent::Compaction(CompactionEvent {
                original_tokens,
                compacted_tokens,
            }) => vec![SessionUpdate::agent_message(format!(
                "[Context compacted: {original_tokens} -> {compacted_tokens} tokens]"
            ))],
            StreamEvent::Error(ErrorEvent { error, .. }) => {
                vec![SessionUpdate::agent_message(format!("[Error] {error}"))]
            }
            StreamEvent::Plan(PlanEvent { steps }) => vec![SessionUpdate::plan(
                steps
                    .iter()
                    .map(|step| {
                        (
                            step.content.as_str(),
                            step.status.as_str(),
                            step.priority.as_str(),
                        )
                    })
                    .collect(),
            )],
            StreamEvent::MessageEnd(MessageEndEvent { .. }) => {
                let Some(snapshot) = &self.context_snapshot else {
                    return Vec::new();
                };
                let (used, size) = snapshot();
                if size <= 0 || used < 0 {
                    return Vec::new();
                }
                vec![SessionUpdate::usage(used, size)]
            }
            _ => Vec::new(),
        }
    }
}

pub fn history_message_to_updates(message: &AgentMessage) -> Vec<SessionUpdate> {
    match (&message.role[..], &message.content) {
        ("user", AgentMessageContent::Text(text)) => vec![SessionUpdate::user_message(text)],
        ("user", AgentMessageContent::Blocks(blocks)) => blocks
            .iter()
            .filter_map(|block| match block {
                AgentContentBlock::ToolResult(result) => Some(SessionUpdate::tool_call_progress(
                    result.tool_use_id.clone(),
                    None,
                    if result.is_error {
                        ToolStatus::Failed
                    } else {
                        ToolStatus::Completed
                    },
                    Some(&result.content),
                )),
                _ => None,
            })
            .collect(),
        ("assistant", AgentMessageContent::Text(text)) => vec![SessionUpdate::agent_message(text)],
        ("assistant", AgentMessageContent::Blocks(blocks)) => {
            let mut updates = Vec::new();
            for block in blocks {
                match block {
                    AgentContentBlock::Text(text) => {
                        updates.push(SessionUpdate::agent_message(&text.text));
                    }
                    AgentContentBlock::Thinking(thinking) => {
                        updates.push(SessionUpdate::agent_thought(&thinking.thinking));
                    }
                    AgentContentBlock::ToolUse(tool_use) => {
                        updates.push(SessionUpdate::tool_call_start(
                            tool_use.id.clone(),
                            tool_use.name.clone(),
                            tool_kind(&tool_use.name),
                            ToolStatus::Completed,
                        ));
                        if !matches!(tool_use.input, JsonValue::Null) {
                            updates.push(SessionUpdate::tool_call_progress(
                                tool_use.id.clone(),
                                None,
                                ToolStatus::Completed,
                                Some(&tool_use.input.to_compact_json()),
                            ));
                        }
                    }
                    _ => {}
                }
            }
            updates
        }
        _ => Vec::new(),
    }
}
