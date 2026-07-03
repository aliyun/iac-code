use std::fmt;

pub const A2A_ID_MAX_LENGTH: usize = 128;
pub const TASK_STATE_CANCELED: &str = "canceled";
pub const TASK_STATE_FAILED: &str = "failed";
pub const TASK_STATE_INPUT_REQUIRED: &str = "input-required";
pub const TASK_STATE_SUBMITTED: &str = "submitted";
pub const TASK_STATE_WORKING: &str = "working";

#[derive(Clone, Debug, PartialEq, Eq)]
pub enum TaskStoreError {
    InvalidA2AId,
    InvalidParams(String),
    InvalidState(String),
}

impl fmt::Display for TaskStoreError {
    fn fmt(&self, formatter: &mut fmt::Formatter<'_>) -> fmt::Result {
        match self {
            TaskStoreError::InvalidA2AId => formatter.write_str("Invalid A2A id"),
            TaskStoreError::InvalidParams(message) | TaskStoreError::InvalidState(message) => {
                formatter.write_str(message)
            }
        }
    }
}

impl std::error::Error for TaskStoreError {}

pub fn validate_protocol_id(value: &str) -> Result<String, TaskStoreError> {
    if value.is_empty() || value.len() > A2A_ID_MAX_LENGTH {
        return Err(TaskStoreError::InvalidA2AId);
    }
    if !value
        .bytes()
        .all(|byte| byte.is_ascii_alphanumeric() || matches!(byte, b'_' | b'.' | b':' | b'-'))
    {
        return Err(TaskStoreError::InvalidA2AId);
    }
    Ok(value.to_owned())
}

#[derive(Clone, Debug, PartialEq)]
pub struct A2ATaskRecord {
    pub task_id: String,
    pub context_id: String,
    pub state: String,
    pub output_text: Vec<String>,
    pub active: bool,
    pub expired: bool,
    pub created_at: f64,
    pub last_active: f64,
}

impl A2ATaskRecord {
    pub fn new(task_id: String, context_id: String, now: f64) -> Self {
        Self {
            task_id,
            context_id,
            state: TASK_STATE_SUBMITTED.to_owned(),
            output_text: Vec::new(),
            active: false,
            expired: false,
            created_at: now,
            last_active: now,
        }
    }

    pub fn touch(&mut self, now: f64) {
        self.last_active = now;
    }
}

#[derive(Clone, Debug, PartialEq)]
pub struct A2AContextRecord {
    pub context_id: String,
    pub session_id: String,
    pub cwd: String,
    pub active_task_id: Option<String>,
    pub expired: bool,
    pub created_at: f64,
    pub last_active: f64,
}

impl A2AContextRecord {
    pub fn new(context_id: String, session_id: String, cwd: String, now: f64) -> Self {
        Self {
            context_id,
            session_id,
            cwd,
            active_task_id: None,
            expired: false,
            created_at: now,
            last_active: now,
        }
    }

    pub fn touch(&mut self, now: f64) {
        self.last_active = now;
    }
}
