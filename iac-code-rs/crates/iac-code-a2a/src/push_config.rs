use crate::push_endpoint::{validate_push_callback_url, InvalidPushNotificationConfigError};

#[derive(Clone, Debug, PartialEq, Eq)]
pub struct A2APushConfig {
    pub task_id: String,
    pub callback_url: String,
}

impl A2APushConfig {
    pub fn new(
        task_id: impl Into<String>,
        callback_url: impl Into<String>,
    ) -> Result<Self, InvalidPushNotificationConfigError> {
        let raw_callback_url = callback_url.into();
        let callback_url = validate_push_callback_url(&raw_callback_url)?;
        Ok(Self {
            task_id: task_id.into(),
            callback_url,
        })
    }
}

#[derive(Clone, Debug, PartialEq, Eq)]
pub struct A2APushAuthentication {
    pub scheme: String,
    pub credentials: String,
}

impl A2APushAuthentication {
    pub fn new(scheme: impl Into<String>, credentials: impl Into<String>) -> Self {
        Self {
            scheme: scheme.into(),
            credentials: credentials.into(),
        }
    }
}

#[derive(Clone, Debug, PartialEq, Eq)]
pub struct TaskPushNotificationConfig {
    pub task_id: String,
    pub id: String,
    pub url: String,
    pub token: String,
    pub authentication: Option<A2APushAuthentication>,
}

impl TaskPushNotificationConfig {
    pub fn new(id: impl Into<String>, url: impl Into<String>) -> Self {
        Self {
            task_id: String::new(),
            id: id.into(),
            url: url.into(),
            token: String::new(),
            authentication: None,
        }
    }
}
