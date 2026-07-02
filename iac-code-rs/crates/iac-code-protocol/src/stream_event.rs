use crate::json::{self, JsonValue};

pub trait ToJsonValue {
    fn to_json_value(&self) -> JsonValue;

    fn to_compact_json(&self) -> String {
        self.to_json_value().to_compact_json()
    }
}

#[derive(Clone, Debug, Default, PartialEq)]
pub struct Usage {
    pub input_tokens: u64,
    pub output_tokens: u64,
    pub cache_creation_input_tokens: u64,
    pub cache_read_input_tokens: u64,
}

impl Usage {
    pub fn total_tokens(&self) -> u64 {
        self.input_tokens
            + self.output_tokens
            + self.cache_creation_input_tokens
            + self.cache_read_input_tokens
    }
}

impl ToJsonValue for Usage {
    fn to_json_value(&self) -> JsonValue {
        json::object([
            (
                "cache_creation_input_tokens",
                json::number(self.cache_creation_input_tokens),
            ),
            (
                "cache_read_input_tokens",
                json::number(self.cache_read_input_tokens),
            ),
            ("input_tokens", json::number(self.input_tokens)),
            ("output_tokens", json::number(self.output_tokens)),
        ])
    }
}

#[derive(Clone, Debug, PartialEq)]
pub struct MessageStartEvent {
    pub message_id: String,
}

impl ToJsonValue for MessageStartEvent {
    fn to_json_value(&self) -> JsonValue {
        event_object(
            "message_start",
            [("message_id", json::string(&self.message_id))],
        )
    }
}

#[derive(Clone, Debug, PartialEq)]
pub struct TextDeltaEvent {
    pub text: String,
}

impl ToJsonValue for TextDeltaEvent {
    fn to_json_value(&self) -> JsonValue {
        event_object("text_delta", [("text", json::string(&self.text))])
    }
}

#[derive(Clone, Debug, PartialEq)]
pub struct ThinkingDeltaEvent {
    pub text: String,
}

impl ToJsonValue for ThinkingDeltaEvent {
    fn to_json_value(&self) -> JsonValue {
        event_object("thinking_delta", [("text", json::string(&self.text))])
    }
}

#[derive(Clone, Debug, PartialEq)]
pub struct ToolUseStartEvent {
    pub tool_use_id: String,
    pub name: String,
}

impl ToJsonValue for ToolUseStartEvent {
    fn to_json_value(&self) -> JsonValue {
        event_object(
            "tool_use_start",
            [
                ("tool_use_id", json::string(&self.tool_use_id)),
                ("name", json::string(&self.name)),
            ],
        )
    }
}

#[derive(Clone, Debug, PartialEq)]
pub struct ToolInputDeltaEvent {
    pub tool_use_id: String,
    pub partial_json: String,
}

impl ToJsonValue for ToolInputDeltaEvent {
    fn to_json_value(&self) -> JsonValue {
        event_object(
            "tool_input_delta",
            [
                ("tool_use_id", json::string(&self.tool_use_id)),
                ("partial_json", json::string(&self.partial_json)),
            ],
        )
    }
}

#[derive(Clone, Debug, PartialEq)]
pub struct ToolUseEndEvent {
    pub tool_use_id: String,
    pub name: String,
    pub input: JsonValue,
}

impl ToJsonValue for ToolUseEndEvent {
    fn to_json_value(&self) -> JsonValue {
        event_object(
            "tool_use_end",
            [
                ("tool_use_id", json::string(&self.tool_use_id)),
                ("name", json::string(&self.name)),
                ("input", self.input.clone()),
            ],
        )
    }
}

#[derive(Clone, Debug, PartialEq)]
pub struct MessageEndEvent {
    pub stop_reason: String,
    pub usage: Usage,
}

impl ToJsonValue for MessageEndEvent {
    fn to_json_value(&self) -> JsonValue {
        event_object(
            "message_end",
            [
                ("stop_reason", json::string(&self.stop_reason)),
                ("usage", self.usage.to_json_value()),
            ],
        )
    }
}

#[derive(Clone, Debug, PartialEq)]
pub struct TombstoneEvent {
    pub message_id: String,
}

impl ToJsonValue for TombstoneEvent {
    fn to_json_value(&self) -> JsonValue {
        event_object(
            "tombstone",
            [("message_id", json::string(&self.message_id))],
        )
    }
}

#[derive(Clone, Debug, PartialEq)]
pub struct ErrorEvent {
    pub error: String,
    pub is_retryable: bool,
}

impl ToJsonValue for ErrorEvent {
    fn to_json_value(&self) -> JsonValue {
        event_object(
            "error",
            [
                ("error", json::string(&self.error)),
                ("is_retryable", json::bool_value(self.is_retryable)),
            ],
        )
    }
}

#[derive(Clone, Debug, PartialEq)]
pub struct ToolResultEvent {
    pub tool_use_id: String,
    pub tool_name: String,
    pub result: String,
    pub is_error: bool,
}

impl ToJsonValue for ToolResultEvent {
    fn to_json_value(&self) -> JsonValue {
        event_object(
            "tool_result",
            [
                ("tool_use_id", json::string(&self.tool_use_id)),
                ("tool_name", json::string(&self.tool_name)),
                ("result", json::string(&self.result)),
                ("is_error", json::bool_value(self.is_error)),
            ],
        )
    }
}

#[derive(Clone, Debug, PartialEq)]
pub struct PermissionRequestEvent {
    pub tool_name: String,
    pub tool_input: JsonValue,
    pub tool_use_id: String,
    pub permission_result: Option<JsonValue>,
}

impl ToJsonValue for PermissionRequestEvent {
    fn to_json_value(&self) -> JsonValue {
        event_object(
            "permission_request",
            [
                ("tool_name", json::string(&self.tool_name)),
                ("tool_input", self.tool_input.clone()),
                ("tool_use_id", json::string(&self.tool_use_id)),
                ("response_future", json::null()),
                (
                    "permission_result",
                    self.permission_result.clone().unwrap_or(JsonValue::Null),
                ),
            ],
        )
    }
}

#[derive(Clone, Debug, PartialEq)]
pub struct CompactionEvent {
    pub original_tokens: u64,
    pub compacted_tokens: u64,
}

impl ToJsonValue for CompactionEvent {
    fn to_json_value(&self) -> JsonValue {
        event_object(
            "compaction",
            [
                ("original_tokens", json::number(self.original_tokens)),
                ("compacted_tokens", json::number(self.compacted_tokens)),
            ],
        )
    }
}

#[derive(Clone, Debug, PartialEq)]
pub struct TaskNotificationEvent {
    pub task_id: String,
    pub description: String,
    pub status: String,
    pub result: Option<String>,
    pub error: Option<String>,
}

impl ToJsonValue for TaskNotificationEvent {
    fn to_json_value(&self) -> JsonValue {
        event_object(
            "task_notification",
            [
                ("task_id", json::string(&self.task_id)),
                ("description", json::string(&self.description)),
                ("status", json::string(&self.status)),
                ("result", optional_string(&self.result)),
                ("error", optional_string(&self.error)),
            ],
        )
    }
}

#[derive(Clone, Debug, PartialEq)]
pub struct QueuedInputSubmittedEvent {
    pub text: String,
}

impl ToJsonValue for QueuedInputSubmittedEvent {
    fn to_json_value(&self) -> JsonValue {
        event_object(
            "queued_input_submitted",
            [("text", json::string(&self.text))],
        )
    }
}

#[derive(Clone, Debug, PartialEq)]
pub struct SubAgentToolEvent {
    pub parent_tool_use_id: String,
    pub child_tool_name: String,
    pub child_tool_input: JsonValue,
    pub is_done: bool,
    pub is_error: bool,
}

impl ToJsonValue for SubAgentToolEvent {
    fn to_json_value(&self) -> JsonValue {
        event_object(
            "subagent_tool",
            [
                ("parent_tool_use_id", json::string(&self.parent_tool_use_id)),
                ("child_tool_name", json::string(&self.child_tool_name)),
                ("child_tool_input", self.child_tool_input.clone()),
                ("is_done", json::bool_value(self.is_done)),
                ("is_error", json::bool_value(self.is_error)),
            ],
        )
    }
}

#[derive(Clone, Debug, PartialEq)]
pub struct StackProgressEvent {
    pub stack_id: String,
    pub stack_name: String,
    pub status: String,
    pub progress_percentage: f64,
    pub resources: Vec<JsonValue>,
    pub elapsed_seconds: u64,
}

impl ToJsonValue for StackProgressEvent {
    fn to_json_value(&self) -> JsonValue {
        event_object(
            "stack_progress",
            [
                ("stack_id", json::string(&self.stack_id)),
                ("stack_name", json::string(&self.stack_name)),
                ("status", json::string(&self.status)),
                (
                    "progress_percentage",
                    json::number(self.progress_percentage),
                ),
                ("resources", json::array(self.resources.clone())),
                ("elapsed_seconds", json::number(self.elapsed_seconds)),
            ],
        )
    }
}

#[derive(Clone, Debug, PartialEq)]
pub struct StackInstancesProgressEvent {
    pub stack_group_name: String,
    pub operation_id: String,
    pub status: String,
    pub progress_percentage: u64,
    pub instances: Vec<JsonValue>,
    pub elapsed_seconds: u64,
}

impl ToJsonValue for StackInstancesProgressEvent {
    fn to_json_value(&self) -> JsonValue {
        event_object(
            "stack_instances_progress",
            [
                ("stack_group_name", json::string(&self.stack_group_name)),
                ("operation_id", json::string(&self.operation_id)),
                ("status", json::string(&self.status)),
                (
                    "progress_percentage",
                    json::number(self.progress_percentage),
                ),
                ("instances", json::array(self.instances.clone())),
                ("elapsed_seconds", json::number(self.elapsed_seconds)),
            ],
        )
    }
}

#[derive(Clone, Debug, PartialEq)]
pub struct PlanStep {
    pub content: String,
    pub status: String,
    pub priority: String,
}

impl ToJsonValue for PlanStep {
    fn to_json_value(&self) -> JsonValue {
        json::object([
            ("content", json::string(&self.content)),
            ("status", json::string(&self.status)),
            ("priority", json::string(&self.priority)),
        ])
    }
}

#[derive(Clone, Debug, PartialEq)]
pub struct PlanEvent {
    pub steps: Vec<PlanStep>,
}

impl ToJsonValue for PlanEvent {
    fn to_json_value(&self) -> JsonValue {
        event_object(
            "plan",
            [(
                "steps",
                json::array(self.steps.iter().map(|step| step.to_json_value())),
            )],
        )
    }
}

#[derive(Clone, Debug, PartialEq)]
pub enum StreamEvent {
    MessageStart(MessageStartEvent),
    TextDelta(TextDeltaEvent),
    ThinkingDelta(ThinkingDeltaEvent),
    ToolUseStart(ToolUseStartEvent),
    ToolInputDelta(ToolInputDeltaEvent),
    ToolUseEnd(ToolUseEndEvent),
    MessageEnd(MessageEndEvent),
    Tombstone(TombstoneEvent),
    Error(ErrorEvent),
    ToolResult(ToolResultEvent),
    PermissionRequest(PermissionRequestEvent),
    Compaction(CompactionEvent),
    TaskNotification(TaskNotificationEvent),
    QueuedInputSubmitted(QueuedInputSubmittedEvent),
    SubAgentTool(SubAgentToolEvent),
    StackProgress(StackProgressEvent),
    StackInstancesProgress(StackInstancesProgressEvent),
    Plan(PlanEvent),
}

impl ToJsonValue for StreamEvent {
    fn to_json_value(&self) -> JsonValue {
        match self {
            StreamEvent::MessageStart(event) => event.to_json_value(),
            StreamEvent::TextDelta(event) => event.to_json_value(),
            StreamEvent::ThinkingDelta(event) => event.to_json_value(),
            StreamEvent::ToolUseStart(event) => event.to_json_value(),
            StreamEvent::ToolInputDelta(event) => event.to_json_value(),
            StreamEvent::ToolUseEnd(event) => event.to_json_value(),
            StreamEvent::MessageEnd(event) => event.to_json_value(),
            StreamEvent::Tombstone(event) => event.to_json_value(),
            StreamEvent::Error(event) => event.to_json_value(),
            StreamEvent::ToolResult(event) => event.to_json_value(),
            StreamEvent::PermissionRequest(event) => event.to_json_value(),
            StreamEvent::Compaction(event) => event.to_json_value(),
            StreamEvent::TaskNotification(event) => event.to_json_value(),
            StreamEvent::QueuedInputSubmitted(event) => event.to_json_value(),
            StreamEvent::SubAgentTool(event) => event.to_json_value(),
            StreamEvent::StackProgress(event) => event.to_json_value(),
            StreamEvent::StackInstancesProgress(event) => event.to_json_value(),
            StreamEvent::Plan(event) => event.to_json_value(),
        }
    }
}

fn event_object(
    event_type: &'static str,
    fields: impl IntoIterator<Item = (&'static str, JsonValue)>,
) -> JsonValue {
    let mut entries = Vec::from([("type", json::string(event_type))]);
    entries.extend(fields);
    json::object(entries)
}

fn optional_string(value: &Option<String>) -> JsonValue {
    value.as_ref().map_or_else(json::null, json::string)
}
