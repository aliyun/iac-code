use std::collections::BTreeMap;

use iac_code_protocol::json::JsonValue;

#[derive(Clone, Debug, PartialEq, Eq)]
pub enum AcpMcpServerConfig {
    Stdio {
        name: String,
        command: String,
        args: Vec<String>,
        env: BTreeMap<String, String>,
    },
    Http {
        name: String,
        type_name: String,
        url: String,
        headers: BTreeMap<String, String>,
    },
}

pub fn convert_mcp_server_configs(value: Option<&JsonValue>) -> Vec<AcpMcpServerConfig> {
    let Some(JsonValue::Array(servers)) = value else {
        return Vec::new();
    };

    servers
        .iter()
        .filter_map(convert_single_mcp_server)
        .collect()
}

fn convert_single_mcp_server(server: &JsonValue) -> Option<AcpMcpServerConfig> {
    let type_name = json_string_field(server, "type").unwrap_or("sse");
    if type_name == "stdio" {
        let command = json_string_field(server, "command")?.to_owned();
        return Some(AcpMcpServerConfig::Stdio {
            name: json_string_field(server, "name")
                .unwrap_or_default()
                .to_owned(),
            command,
            args: string_array_field(server, "args"),
            env: named_string_map_field(server, "env"),
        });
    }

    if matches!(type_name, "http" | "sse") {
        let url = json_string_field(server, "url")?.to_owned();
        return Some(AcpMcpServerConfig::Http {
            name: json_string_field(server, "name")
                .unwrap_or_default()
                .to_owned(),
            type_name: type_name.to_owned(),
            url,
            headers: named_string_map_field(server, "headers"),
        });
    }

    None
}

fn string_array_field(value: &JsonValue, key: &str) -> Vec<String> {
    let Some(JsonValue::Array(values)) = json_object_field(value, key) else {
        return Vec::new();
    };
    values
        .iter()
        .filter_map(|value| match value {
            JsonValue::String(value) => Some(value.clone()),
            _ => None,
        })
        .collect()
}

fn named_string_map_field(value: &JsonValue, key: &str) -> BTreeMap<String, String> {
    match json_object_field(value, key) {
        Some(JsonValue::Object(entries)) => entries
            .iter()
            .filter_map(|(key, value)| match value {
                JsonValue::String(value) => Some((key.clone(), value.clone())),
                _ => None,
            })
            .collect(),
        Some(JsonValue::Array(entries)) => entries
            .iter()
            .filter_map(|entry| {
                let name = json_string_field(entry, "name")?;
                let value = json_string_field(entry, "value")?;
                Some((name.to_owned(), value.to_owned()))
            })
            .collect(),
        _ => BTreeMap::new(),
    }
}

fn json_object_field<'a>(value: &'a JsonValue, key: &str) -> Option<&'a JsonValue> {
    let JsonValue::Object(object) = value else {
        return None;
    };
    object.get(key)
}

fn json_string_field<'a>(value: &'a JsonValue, key: &str) -> Option<&'a str> {
    match json_object_field(value, key) {
        Some(JsonValue::String(value)) => Some(value),
        _ => None,
    }
}
