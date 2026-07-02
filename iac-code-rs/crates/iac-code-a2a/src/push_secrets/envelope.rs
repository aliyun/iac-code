use iac_code_protocol::json::{self, JsonValue};

use super::json_string;

#[derive(Clone, Debug, PartialEq, Eq)]
pub struct A2APushSecretEnvelope {
    key_id: String,
    ciphertext: String,
}

impl A2APushSecretEnvelope {
    pub fn new(key_id: impl Into<String>, ciphertext: impl Into<String>) -> Self {
        Self {
            key_id: key_id.into(),
            ciphertext: ciphertext.into(),
        }
    }

    pub fn key_id(&self) -> &str {
        &self.key_id
    }

    pub fn ciphertext(&self) -> &str {
        &self.ciphertext
    }

    pub fn to_json(&self) -> JsonValue {
        json::object([
            ("keyId", json::string(&self.key_id)),
            ("ciphertext", json::string(&self.ciphertext)),
        ])
    }

    pub fn from_json(value: &JsonValue) -> Option<Self> {
        let JsonValue::Object(object) = value else {
            return None;
        };
        Some(Self {
            key_id: json_string(object, "keyId")?,
            ciphertext: json_string(object, "ciphertext")?,
        })
    }
}
