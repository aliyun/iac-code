use std::fmt;

use base64::engine::general_purpose::STANDARD;
use base64::Engine;
use iac_code_protocol::json::{self, JsonValue};

use crate::push::TaskPushNotificationConfig;
use crate::task_store::A2ATaskStore;

const DEFAULT_LIST_TASKS_PAGE_SIZE: usize = 100;

#[derive(Clone, Debug, PartialEq, Eq)]
pub struct A2ADispatcherError {
    message: String,
}

#[derive(Clone, Debug, PartialEq, Eq)]
pub struct ListPushNotificationConfigsRequest {
    pub task_id: String,
    pub page_size: Option<usize>,
    pub page_token: Option<String>,
}

#[derive(Clone, Debug, PartialEq, Eq)]
pub struct ListPushNotificationConfigsResponse {
    pub configs: Vec<TaskPushNotificationConfig>,
    pub next_page_token: String,
}

impl A2ADispatcherError {
    fn new(message: impl Into<String>) -> Self {
        Self {
            message: message.into(),
        }
    }
}

impl fmt::Display for A2ADispatcherError {
    fn fmt(&self, formatter: &mut fmt::Formatter<'_>) -> fmt::Result {
        formatter.write_str(&self.message)
    }
}

impl std::error::Error for A2ADispatcherError {}

pub fn decode_dispatch_response_json(value: JsonValue) -> Result<JsonValue, A2ADispatcherError> {
    match value {
        JsonValue::Object(_) => Ok(value),
        _ => Err(A2ADispatcherError::new(
            "A2A dispatcher response must be a JSON object",
        )),
    }
}

pub fn decode_dispatch_sse_line(line: &str) -> Result<Option<JsonValue>, A2ADispatcherError> {
    let Some(data) = line.strip_prefix("data:") else {
        return Ok(None);
    };
    let payload = json::parse(data.trim()).map_err(|error| {
        A2ADispatcherError::new(format!("Invalid A2A dispatcher stream event: {error}"))
    })?;
    Ok(Some(payload))
}

pub fn list_push_notification_configs(
    mut configs: Vec<TaskPushNotificationConfig>,
    request: ListPushNotificationConfigsRequest,
) -> Result<ListPushNotificationConfigsResponse, A2ADispatcherError> {
    configs.retain(|config| config.task_id == request.task_id);
    configs.sort_by(|left, right| left.id.cmp(&right.id));

    let start_idx = if let Some(page_token) = request
        .page_token
        .as_deref()
        .filter(|value| !value.is_empty())
    {
        let start_config_id = decode_page_token(page_token)
            .ok_or_else(|| A2ADispatcherError::new(format!("Invalid page token: {page_token}")))?;
        configs
            .iter()
            .position(|config| config.id == start_config_id)
            .ok_or_else(|| A2ADispatcherError::new(format!("Invalid page token: {page_token}")))?
    } else {
        0
    };

    let page_size = request
        .page_size
        .filter(|page_size| *page_size > 0)
        .unwrap_or(DEFAULT_LIST_TASKS_PAGE_SIZE);
    let end_idx = (start_idx + page_size).min(configs.len());
    let next_page_token = if end_idx < configs.len() {
        encode_page_token(&configs[end_idx].id)
    } else {
        String::new()
    };

    Ok(ListPushNotificationConfigsResponse {
        configs: configs[start_idx..end_idx].to_vec(),
        next_page_token,
    })
}

pub fn validate_cancel_task_request(
    task_store: &A2ATaskStore,
    owner: &str,
    task_id: &str,
) -> Result<(), A2ADispatcherError> {
    ensure_task_exists(task_store, owner, task_id)?;
    if !task_store.is_task_active(task_id) {
        return Err(A2ADispatcherError::new("Task cannot be canceled"));
    }
    Ok(())
}

pub fn validate_subscribe_task_request(
    task_store: &A2ATaskStore,
    owner: &str,
    task_id: &str,
) -> Result<(), A2ADispatcherError> {
    ensure_task_exists(task_store, owner, task_id)?;
    if !task_store.is_task_active(task_id) {
        return Err(A2ADispatcherError::new(format!(
            "Task {task_id} is not active"
        )));
    }
    Ok(())
}

fn encode_page_token(config_id: &str) -> String {
    STANDARD.encode(config_id.as_bytes())
}

fn decode_page_token(page_token: &str) -> Option<String> {
    let bytes = STANDARD.decode(page_token.as_bytes()).ok()?;
    String::from_utf8(bytes).ok()
}

fn ensure_task_exists(
    task_store: &A2ATaskStore,
    owner: &str,
    task_id: &str,
) -> Result<(), A2ADispatcherError> {
    if task_store.get_sdk_task(task_id, owner).is_some() {
        Ok(())
    } else {
        Err(A2ADispatcherError::new(format!("Task {task_id} not found")))
    }
}
