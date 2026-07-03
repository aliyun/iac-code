use std::collections::BTreeMap;

use iac_code_protocol::json::{self, JsonValue};

use crate::exposure::A2AExposureType;

#[derive(Clone, Debug, PartialEq, Eq)]
pub struct AgentInterfaceConfig {
    pub url: String,
    pub protocol_binding: String,
    pub protocol_version: String,
}

impl AgentInterfaceConfig {
    pub fn new(
        url: impl Into<String>,
        protocol_binding: impl Into<String>,
        protocol_version: impl Into<String>,
    ) -> Self {
        Self {
            url: url.into(),
            protocol_binding: protocol_binding.into(),
            protocol_version: protocol_version.into(),
        }
    }

    pub(super) fn to_json(&self) -> JsonValue {
        json::object([
            ("protocolBinding", json::string(&self.protocol_binding)),
            ("protocolVersion", json::string(&self.protocol_version)),
            ("url", json::string(&self.url)),
        ])
    }
}

#[derive(Clone, Debug, PartialEq, Eq)]
pub struct AgentExtensionConfig {
    pub uri: String,
    pub description: String,
    pub required: bool,
    pub params: BTreeMap<String, JsonValue>,
}

impl AgentExtensionConfig {
    pub(super) fn to_json(&self) -> JsonValue {
        let mut object = BTreeMap::new();
        object.insert("description".to_owned(), json::string(&self.description));
        if !self.params.is_empty() {
            object.insert("params".to_owned(), JsonValue::Object(self.params.clone()));
        }
        object.insert("required".to_owned(), json::bool_value(self.required));
        object.insert("uri".to_owned(), json::string(&self.uri));
        JsonValue::Object(object)
    }
}

#[derive(Clone, Debug, PartialEq, Eq)]
pub struct AgentCardOptions {
    pub host: String,
    pub port: u16,
    pub token_enabled: bool,
    pub basic_enabled: bool,
    pub api_key_enabled: bool,
    pub api_key_header: String,
    pub signing_secret: Option<String>,
    pub signing_key_id: String,
    pub push_notifications: bool,
    pub supported_interfaces: Vec<AgentInterfaceConfig>,
    pub agent_extensions: Vec<AgentExtensionConfig>,
    pub thinking_exposure_types: Vec<A2AExposureType>,
}

impl AgentCardOptions {
    pub fn new(host: impl Into<String>, port: u16, token_enabled: bool) -> Self {
        Self {
            host: host.into(),
            port,
            token_enabled,
            basic_enabled: false,
            api_key_enabled: false,
            api_key_header: "X-API-Key".to_owned(),
            signing_secret: None,
            signing_key_id: "default".to_owned(),
            push_notifications: false,
            supported_interfaces: Vec::new(),
            agent_extensions: Vec::new(),
            thinking_exposure_types: Vec::new(),
        }
    }
}

impl Default for AgentCardOptions {
    fn default() -> Self {
        Self::new("127.0.0.1", 41242, false)
    }
}
