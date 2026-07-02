use std::cell::RefCell;
use std::collections::BTreeMap;
use std::fmt;
use std::time::{SystemTime, UNIX_EPOCH};

use iac_code_protocol::json::{self, JsonValue};

use crate::push_secrets::{A2APushSecretEnvelope, A2APushSecretError, A2APushSecretKeyring};

const REDACTED_HEADERS: &[&str] = &[
    "authorization",
    "x-a2a-notification-token",
    "x-api-key",
    "api-key",
];
const ENCRYPTED_JOB_FIELD: &str = "iacCodeEncryptedPushJob";

#[derive(Clone, Debug, PartialEq)]
pub struct A2APushJob {
    pub task_id: String,
    pub config_id: String,
    pub url: String,
    pub payload: JsonValue,
    pub headers: BTreeMap<String, String>,
    pub job_id: String,
    pub attempt: u32,
    pub next_attempt_at: f64,
    pub last_error: String,
}

impl A2APushJob {
    pub fn new(
        job_id: impl Into<String>,
        task_id: impl Into<String>,
        config_id: impl Into<String>,
        url: impl Into<String>,
        payload: JsonValue,
    ) -> Self {
        Self {
            task_id: task_id.into(),
            config_id: config_id.into(),
            url: url.into(),
            payload,
            headers: BTreeMap::new(),
            job_id: job_id.into(),
            attempt: 0,
            next_attempt_at: 0.0,
            last_error: String::new(),
        }
    }

    pub fn with_headers<const N: usize>(mut self, headers: [(&str, &str); N]) -> Self {
        self.headers = headers
            .into_iter()
            .map(|(key, value)| (key.to_owned(), value.to_owned()))
            .collect();
        self
    }

    pub fn with_attempt(
        mut self,
        attempt: u32,
        next_attempt_at: Option<f64>,
        last_error: impl Into<String>,
    ) -> Self {
        self.attempt = attempt;
        if let Some(next_attempt_at) = next_attempt_at {
            self.next_attempt_at = next_attempt_at;
        }
        self.last_error = last_error.into();
        self
    }

    fn to_json(&self) -> JsonValue {
        json::object([
            ("attempt", json::number(self.attempt)),
            ("configId", json::string(&self.config_id)),
            ("jobId", json::string(&self.job_id)),
            ("lastError", json::string(&self.last_error)),
            ("nextAttemptAt", json::float(self.next_attempt_at)),
            ("payload", self.payload.clone()),
            ("taskId", json::string(&self.task_id)),
            ("url", json::string(&self.url)),
        ])
    }

    fn from_json(value: &JsonValue) -> Result<Self, PushQueueError> {
        let JsonValue::Object(object) = value else {
            return Err(PushQueueError::InvalidJob);
        };
        Ok(Self {
            job_id: string_field(object, "jobId")?,
            task_id: string_field(object, "taskId")?,
            config_id: string_field(object, "configId")?,
            url: string_field(object, "url")?,
            payload: object
                .get("payload")
                .cloned()
                .ok_or(PushQueueError::InvalidJob)?,
            headers: headers_field(object.get("headers")),
            attempt: number_field(object.get("attempt")).unwrap_or(0.0) as u32,
            next_attempt_at: number_field(object.get("nextAttemptAt")).unwrap_or(0.0),
            last_error: string_field_optional(object, "lastError").unwrap_or_default(),
        })
    }
}

#[derive(Clone, Copy, Debug, PartialEq)]
pub struct A2APushRetryPolicy {
    pub initial_delay_seconds: f64,
    pub max_delay_seconds: f64,
    pub jitter_ratio: f64,
    pub max_attempts: u32,
}

impl Default for A2APushRetryPolicy {
    fn default() -> Self {
        Self {
            initial_delay_seconds: 1.0,
            max_delay_seconds: 60.0,
            jitter_ratio: 0.2,
            max_attempts: 5,
        }
    }
}

impl A2APushRetryPolicy {
    pub fn delay_for_attempt(&self, attempt: u32) -> f64 {
        let exponent = attempt.saturating_sub(1);
        let base = self.initial_delay_seconds * 2f64.powi(exponent as i32);
        base.min(self.max_delay_seconds)
    }
}

#[derive(Debug)]
pub enum PushQueueError {
    Io(std::io::Error),
    InvalidJson(String),
    InvalidJob,
    Redis(String),
    Secret(String),
}

impl fmt::Display for PushQueueError {
    fn fmt(&self, formatter: &mut fmt::Formatter<'_>) -> fmt::Result {
        match self {
            Self::Io(error) => write!(formatter, "{error}"),
            Self::InvalidJson(message) => write!(formatter, "invalid JSON: {message}"),
            Self::InvalidJob => formatter.write_str("invalid A2A push job"),
            Self::Redis(message) | Self::Secret(message) => formatter.write_str(message),
        }
    }
}

impl std::error::Error for PushQueueError {}

impl From<std::io::Error> for PushQueueError {
    fn from(error: std::io::Error) -> Self {
        Self::Io(error)
    }
}

impl From<A2APushSecretError> for PushQueueError {
    fn from(error: A2APushSecretError) -> Self {
        Self::Secret(error.to_string())
    }
}

pub fn redact_push_headers(headers: BTreeMap<String, String>) -> BTreeMap<String, String> {
    headers
        .into_iter()
        .map(|(key, value)| {
            if REDACTED_HEADERS.contains(&key.to_ascii_lowercase().as_str()) {
                (key, "[redacted]".to_owned())
            } else {
                (key, value)
            }
        })
        .collect()
}

pub(crate) fn serialize_push_job(
    job: &A2APushJob,
    secret_keyring: Option<&RefCell<A2APushSecretKeyring>>,
) -> Result<String, PushQueueError> {
    let payload = job.to_json().to_compact_json();
    let Some(secret_keyring) = secret_keyring else {
        return Ok(payload);
    };
    let envelope = secret_keyring.borrow_mut().encrypt(&payload)?;
    let mut encrypted = match envelope.to_json() {
        JsonValue::Object(object) => object,
        _ => return Err(PushQueueError::InvalidJob),
    };
    encrypted.insert("version".to_owned(), json::number(1));
    Ok(json::object([(ENCRYPTED_JOB_FIELD, JsonValue::Object(encrypted))]).to_compact_json())
}

pub(crate) fn deserialize_push_job(
    value: &str,
    secret_keyring: Option<&RefCell<A2APushSecretKeyring>>,
) -> Result<A2APushJob, PushQueueError> {
    let value = json::parse(value).map_err(PushQueueError::InvalidJson)?;
    if let JsonValue::Object(object) = &value {
        if let Some(encrypted) = object.get(ENCRYPTED_JOB_FIELD) {
            let Some(secret_keyring) = secret_keyring else {
                return Err(PushQueueError::InvalidJob);
            };
            let envelope =
                A2APushSecretEnvelope::from_json(encrypted).ok_or(PushQueueError::InvalidJob)?;
            let decrypted = secret_keyring.borrow_mut().decrypt(&envelope)?;
            let decrypted = json::parse(&decrypted).map_err(PushQueueError::InvalidJson)?;
            return A2APushJob::from_json(&decrypted);
        }
    }
    A2APushJob::from_json(&value)
}

pub(crate) fn current_time_seconds() -> f64 {
    SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .map_or(0.0, |duration| duration.as_secs_f64())
}

fn string_field(object: &BTreeMap<String, JsonValue>, key: &str) -> Result<String, PushQueueError> {
    string_field_optional(object, key).ok_or(PushQueueError::InvalidJob)
}

fn string_field_optional(object: &BTreeMap<String, JsonValue>, key: &str) -> Option<String> {
    match object.get(key) {
        Some(JsonValue::String(value)) => Some(value.clone()),
        _ => None,
    }
}

fn headers_field(value: Option<&JsonValue>) -> BTreeMap<String, String> {
    let Some(JsonValue::Object(headers)) = value else {
        return BTreeMap::new();
    };
    headers
        .iter()
        .filter_map(|(key, value)| match value {
            JsonValue::String(value) => Some((key.clone(), value.clone())),
            _ => None,
        })
        .collect()
}

fn number_field(value: Option<&JsonValue>) -> Option<f64> {
    match value {
        Some(JsonValue::Number(value)) => value.parse().ok(),
        _ => None,
    }
}
