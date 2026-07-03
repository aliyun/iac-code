use iac_code_protocol::json::JsonValue;

use crate::transport::{binding_from_url, is_runnable_binding, A2ATransportBinding};

use super::{object_field, string_field, A2AClient};

impl A2AClient {
    pub fn select_endpoint_url(card: &JsonValue, fallback_url: &str) -> String {
        if let Some(JsonValue::Array(interfaces)) = object_field(card, "supportedInterfaces") {
            for item in interfaces {
                let Some(url) = string_field(item, "url") else {
                    continue;
                };
                if !url.is_empty() && is_runnable_binding(&interface_transport_binding(item, url)) {
                    return url.to_owned();
                }
            }
        }
        string_field(card, "url")
            .filter(|url| !url.is_empty())
            .unwrap_or(fallback_url)
            .to_owned()
    }
}

fn interface_transport_binding(interface: &JsonValue, url: &str) -> A2ATransportBinding {
    let protocol_version = string_field(interface, "protocolVersion");
    match string_field(interface, "protocolBinding") {
        Some(protocol_binding) if !protocol_binding.trim().is_empty() => {
            A2ATransportBinding::new(url, protocol_binding, protocol_version)
        }
        _ => binding_from_url(url, protocol_version),
    }
}
