use std::collections::BTreeMap;

use iac_code_protocol::json::{self, JsonValue};
use iac_code_protocol::{
    ErrorEvent, MessageEndEvent, PermissionRequestEvent, StreamEvent, TextDeltaEvent,
    ThinkingDeltaEvent, ToolInputDeltaEvent, ToolResultEvent, ToolUseEndEvent, ToolUseStartEvent,
};

pub use crate::exposure::A2AExposureType;

mod artifact_helpers;
mod metadata;
mod options;
mod trace;

use artifact_helpers::{artifact_metadata_json, artifact_update_event, extract_artifact_metadata};
pub use metadata::truncate_metadata;
use metadata::truncate_string;
pub use options::PublishOptions;
use trace::tool_trace_event;

pub const METADATA_MAX_CHARS: usize = 4000;
pub const ERROR_TEXT_MAX_CHARS: usize = 1000;

#[derive(Clone, Debug, PartialEq)]
pub enum A2AEvent {
    TaskStatusUpdate(TaskStatusUpdate),
    TaskArtifactUpdate(TaskArtifactUpdate),
}

#[derive(Clone, Debug, PartialEq)]
pub struct TaskStatusUpdate {
    pub task_id: String,
    pub context_id: String,
    pub state: String,
    pub message: Option<A2AMessage>,
    pub metadata: JsonValue,
}

#[derive(Clone, Debug, PartialEq)]
pub struct TaskArtifactUpdate {
    pub task_id: String,
    pub context_id: String,
    pub artifact: A2AArtifact,
    pub append: bool,
    pub last_chunk: bool,
}

#[derive(Clone, Debug, PartialEq)]
pub struct A2AArtifact {
    pub artifact_id: String,
    pub name: String,
    pub parts: Vec<A2APart>,
    pub metadata: JsonValue,
}

#[derive(Clone, Debug, PartialEq)]
pub struct A2APart {
    pub url: String,
    pub filename: String,
    pub media_type: String,
    pub metadata: JsonValue,
}

#[derive(Clone, Debug, PartialEq, Eq)]
pub struct A2AMessage {
    pub role: String,
    pub text: String,
}

#[derive(Clone, Debug, Default, PartialEq)]
pub struct PublishResult {
    pub events: Vec<A2AEvent>,
    pub text: Option<String>,
    pub permission_decision: Option<bool>,
}

pub fn publish_stream_event(
    task_id: &str,
    context_id: &str,
    event: &StreamEvent,
    options: PublishOptions,
    mut permission_resolver: Option<&mut dyn FnMut(&PermissionRequestEvent) -> bool>,
) -> PublishResult {
    match event {
        StreamEvent::TextDelta(TextDeltaEvent { text }) => {
            if text.is_empty() {
                return PublishResult::default();
            }
            PublishResult {
                events: vec![status_update(
                    task_id,
                    context_id,
                    "TASK_STATE_WORKING",
                    Some(agent_text_message(text)),
                    json::object([] as [(&str, JsonValue); 0]),
                )],
                text: Some(text.clone()),
                permission_decision: None,
            }
        }
        StreamEvent::ThinkingDelta(ThinkingDeltaEvent { text }) => {
            if !options
                .exposure_types
                .contains(&A2AExposureType::RawThinking)
            {
                return PublishResult::default();
            }
            PublishResult {
                events: vec![status_update(
                    task_id,
                    context_id,
                    "TASK_STATE_WORKING",
                    None,
                    iac_metadata(
                        "thinking",
                        json::object([
                            ("type", json::string("raw_thinking")),
                            ("text", truncate_metadata(&json::string(text))),
                        ]),
                    ),
                )],
                text: None,
                permission_decision: None,
            }
        }
        StreamEvent::ToolUseStart(ToolUseStartEvent { tool_use_id, name }) => tool_trace_event(
            task_id,
            context_id,
            &options,
            json::object([
                ("status", json::string("started")),
                ("toolUseId", json::string(tool_use_id)),
                ("name", json::string(name)),
            ]),
        ),
        StreamEvent::ToolInputDelta(ToolInputDeltaEvent {
            tool_use_id,
            partial_json,
        }) => tool_trace_event(
            task_id,
            context_id,
            &options,
            json::object([
                ("status", json::string("input_delta")),
                ("toolUseId", json::string(tool_use_id)),
                (
                    "partialJson",
                    truncate_metadata(&json::string(partial_json)),
                ),
            ]),
        ),
        StreamEvent::ToolUseEnd(ToolUseEndEvent {
            tool_use_id,
            name,
            input,
        }) => tool_trace_event(
            task_id,
            context_id,
            &options,
            json::object([
                ("status", json::string("input_complete")),
                ("toolUseId", json::string(tool_use_id)),
                ("name", json::string(name)),
                ("input", truncate_metadata(input)),
            ]),
        ),
        StreamEvent::ToolResult(ToolResultEvent {
            tool_use_id,
            tool_name,
            result,
            is_error,
        }) => {
            let artifact_metadata =
                extract_artifact_metadata(result, options.artifact_store.as_ref());
            let mut events = Vec::new();
            if let Some(metadata) = &artifact_metadata {
                events.push(artifact_update_event(task_id, context_id, metadata));
            }
            if !options.exposure_types.contains(&A2AExposureType::ToolTrace) {
                return PublishResult {
                    events,
                    text: None,
                    permission_decision: None,
                };
            }

            let mut tool_metadata = BTreeMap::from([
                (
                    "status".to_owned(),
                    json::string(if *is_error { "failed" } else { "completed" }),
                ),
                ("toolUseId".to_owned(), json::string(tool_use_id)),
                ("name".to_owned(), json::string(tool_name)),
                ("result".to_owned(), tool_result_metadata(result)),
            ]);
            if let Some(metadata) = &artifact_metadata {
                tool_metadata.insert("artifact".to_owned(), artifact_metadata_json(metadata));
            }
            events.push(status_update(
                task_id,
                context_id,
                "TASK_STATE_WORKING",
                None,
                iac_metadata("tool", JsonValue::Object(tool_metadata)),
            ));
            PublishResult {
                events,
                text: None,
                permission_decision: None,
            }
        }
        StreamEvent::PermissionRequest(event) => {
            let approved = permission_resolver
                .as_mut()
                .map_or(options.auto_approve_permissions, |resolver| resolver(event));
            if !options.exposure_types.contains(&A2AExposureType::ToolTrace) {
                return PublishResult {
                    events: Vec::new(),
                    text: None,
                    permission_decision: Some(approved),
                };
            }
            PublishResult {
                events: vec![status_update(
                    task_id,
                    context_id,
                    "TASK_STATE_WORKING",
                    None,
                    iac_metadata(
                        "permission",
                        json::object([
                            ("autoApproved", json::bool_value(approved)),
                            ("toolName", json::string(&event.tool_name)),
                            ("toolUseId", json::string(&event.tool_use_id)),
                            ("toolInput", truncate_metadata(&event.tool_input)),
                        ]),
                    ),
                )],
                text: None,
                permission_decision: Some(approved),
            }
        }
        StreamEvent::MessageEnd(MessageEndEvent { usage, .. }) => PublishResult {
            events: vec![status_update(
                task_id,
                context_id,
                "TASK_STATE_WORKING",
                None,
                iac_metadata(
                    "usage",
                    json::object([
                        ("inputTokens", json::number(usage.input_tokens)),
                        ("outputTokens", json::number(usage.output_tokens)),
                        ("totalTokens", json::number(usage.total_tokens())),
                    ]),
                ),
            )],
            text: None,
            permission_decision: None,
        },
        StreamEvent::Error(ErrorEvent {
            error,
            is_retryable,
        }) => {
            let (state, text) = if *is_retryable {
                (
                    "TASK_STATE_INPUT_REQUIRED",
                    "A temporary error occurred. Please retry.".to_owned(),
                )
            } else {
                (
                    "TASK_STATE_FAILED",
                    truncate_string(
                        if error.is_empty() {
                            "Unknown error"
                        } else {
                            error.as_str()
                        },
                        ERROR_TEXT_MAX_CHARS,
                    ),
                )
            };
            PublishResult {
                events: vec![status_update(
                    task_id,
                    context_id,
                    state,
                    Some(agent_text_message(&text)),
                    json::object([] as [(&str, JsonValue); 0]),
                )],
                text: None,
                permission_decision: None,
            }
        }
        _ => PublishResult::default(),
    }
}

fn status_update(
    task_id: &str,
    context_id: &str,
    state: &str,
    message: Option<A2AMessage>,
    metadata: JsonValue,
) -> A2AEvent {
    A2AEvent::TaskStatusUpdate(TaskStatusUpdate {
        task_id: task_id.to_owned(),
        context_id: context_id.to_owned(),
        state: state.to_owned(),
        message,
        metadata,
    })
}

fn agent_text_message(text: &str) -> A2AMessage {
    A2AMessage {
        role: "ROLE_AGENT".to_owned(),
        text: text.to_owned(),
    }
}

fn iac_metadata(key: &str, value: JsonValue) -> JsonValue {
    json::object([("iac_code", json::object([(key, value)]))])
}

fn tool_result_metadata(result: &str) -> JsonValue {
    let Ok(JsonValue::Object(mut result)) = json::parse(result) else {
        return truncate_metadata(&json::string(result));
    };
    if let Some(JsonValue::Object(artifact)) = result.get_mut("artifact") {
        for key in ["content", "bytes", "base64", "raw", "path"] {
            artifact.remove(key);
        }
    }
    truncate_metadata(&JsonValue::Object(result))
}
