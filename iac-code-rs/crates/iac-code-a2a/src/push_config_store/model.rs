use std::cell::RefCell;
use std::collections::BTreeMap;

use base64::engine::general_purpose::STANDARD;
use base64::Engine;
use iac_code_protocol::json::{self, JsonValue};

use crate::push_config::{A2APushAuthentication, TaskPushNotificationConfig};
use crate::push_endpoint::InvalidPushNotificationConfigError;
use crate::push_secrets::{A2APushSecretEnvelope, A2APushSecretKeyring};

impl A2APushAuthentication {
    fn to_json(&self, include_credentials: bool) -> JsonValue {
        let mut object = BTreeMap::new();
        object.insert("scheme".to_owned(), json::string(&self.scheme));
        if include_credentials {
            object.insert("credentials".to_owned(), json::string(&self.credentials));
        }
        JsonValue::Object(object)
    }

    fn from_json(value: &JsonValue) -> Option<Self> {
        let JsonValue::Object(object) = value else {
            return None;
        };
        Some(Self {
            scheme: json_string(object, "scheme").unwrap_or_default(),
            credentials: json_string(object, "credentials").unwrap_or_default(),
        })
    }
}

impl TaskPushNotificationConfig {
    pub(super) fn to_storage_json(
        &self,
        secret_keyring: &RefCell<A2APushSecretKeyring>,
    ) -> Result<JsonValue, InvalidPushNotificationConfigError> {
        let mut object = BTreeMap::new();
        let mut encrypted_fields = BTreeMap::new();

        object.insert("id".to_owned(), json::string(&self.id));
        object.insert("taskId".to_owned(), json::string(&self.task_id));
        object.insert("url".to_owned(), json::string(&self.url));
        if !self.token.is_empty() {
            let envelope = secret_keyring
                .borrow_mut()
                .encrypt(&self.token)
                .map_err(|_| InvalidPushNotificationConfigError)?;
            encrypted_fields.insert("token".to_owned(), envelope.to_json());
        }
        if let Some(authentication) = &self.authentication {
            object.insert(
                "authentication".to_owned(),
                authentication.to_json(authentication.credentials.is_empty()),
            );
            if !authentication.credentials.is_empty() {
                let envelope = secret_keyring
                    .borrow_mut()
                    .encrypt(&authentication.credentials)
                    .map_err(|_| InvalidPushNotificationConfigError)?;
                encrypted_fields
                    .insert("authentication.credentials".to_owned(), envelope.to_json());
            }
        }

        if !encrypted_fields.is_empty() {
            object.insert(
                "iacCodeEncryptedFields".to_owned(),
                json::object([
                    ("fields", JsonValue::Object(encrypted_fields)),
                    ("version", json::number(1)),
                ]),
            );
        }
        Ok(JsonValue::Object(object))
    }

    fn from_json(value: &JsonValue) -> Option<Self> {
        let JsonValue::Object(object) = value else {
            return None;
        };
        Some(Self {
            id: json_string(object, "id")?,
            task_id: json_string(object, "taskId")?,
            url: json_string(object, "url")?,
            token: json_string(object, "token").unwrap_or_default(),
            authentication: object
                .get("authentication")
                .and_then(A2APushAuthentication::from_json),
        })
    }

    pub(super) fn from_storage_json(
        value: &JsonValue,
        secret_keyring: &RefCell<A2APushSecretKeyring>,
    ) -> Option<Self> {
        let JsonValue::Object(object) = value else {
            return None;
        };
        let mut config = Self::from_json(value)?;
        let Some(JsonValue::Object(encrypted)) = object.get("iacCodeEncryptedFields") else {
            return Some(config);
        };
        let Some(JsonValue::Object(fields)) = encrypted.get("fields") else {
            return Some(config);
        };

        if let Some(envelope) = fields
            .get("token")
            .and_then(A2APushSecretEnvelope::from_json)
        {
            config.token = secret_keyring.borrow_mut().decrypt(&envelope).ok()?;
        }
        if let Some(envelope) = fields
            .get("authentication.credentials")
            .and_then(A2APushSecretEnvelope::from_json)
        {
            let credentials = secret_keyring.borrow_mut().decrypt(&envelope).ok()?;
            let authentication = config
                .authentication
                .get_or_insert_with(|| A2APushAuthentication::new("", ""));
            authentication.credentials = credentials;
        }
        Some(config)
    }
}

pub(super) fn notification_headers(
    config: &TaskPushNotificationConfig,
) -> BTreeMap<String, String> {
    let mut headers = BTreeMap::new();
    if !config.token.is_empty() {
        headers.insert("X-A2A-Notification-Token".to_owned(), config.token.clone());
    }
    let Some(authentication) = &config.authentication else {
        return headers;
    };
    if authentication.scheme.is_empty() || authentication.credentials.is_empty() {
        return headers;
    }

    let scheme = authentication.scheme.to_ascii_lowercase();
    let authorization = if scheme == "bearer" {
        format!("Bearer {}", authentication.credentials)
    } else if scheme == "basic" {
        format!(
            "Basic {}",
            STANDARD.encode(authentication.credentials.as_bytes())
        )
    } else {
        format!("{} {}", authentication.scheme, authentication.credentials)
    };
    headers.insert("Authorization".to_owned(), authorization);
    headers
}

fn json_string(object: &BTreeMap<String, JsonValue>, key: &str) -> Option<String> {
    match object.get(key) {
        Some(JsonValue::String(value)) => Some(value.clone()),
        _ => None,
    }
}
