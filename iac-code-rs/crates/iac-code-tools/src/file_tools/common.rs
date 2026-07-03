use std::path::{Path, PathBuf};

use iac_code_protocol::json::{self, JsonValue};
use iac_code_protocol::permission::{PermissionDecisionReason, PermissionResult};

pub(super) fn string_field<'a>(input: &'a JsonValue, field: &str) -> Option<&'a str> {
    match input {
        JsonValue::Object(fields) => match fields.get(field) {
            Some(JsonValue::String(value)) => Some(value.as_str()),
            _ => None,
        },
        _ => None,
    }
}

pub(super) fn path_field(input: &JsonValue) -> Option<&str> {
    string_field(input, "path").or_else(|| string_field(input, "file_path"))
}

pub(super) fn tool_schema(required: &[&str], properties: Vec<(&str, JsonValue)>) -> JsonValue {
    json::object([
        ("type", json::string("object")),
        ("properties", json::object(properties)),
        (
            "required",
            json::array(required.iter().copied().map(json::string)),
        ),
    ])
}

pub(super) fn string_property(description: &str) -> JsonValue {
    typed_property("string", description)
}

pub(super) fn integer_property(description: &str) -> JsonValue {
    typed_property("integer", description)
}

pub(super) fn boolean_property(description: &str) -> JsonValue {
    typed_property("boolean", description)
}

fn typed_property(type_name: &str, description: &str) -> JsonValue {
    json::object([
        ("type", json::string(type_name)),
        ("description", json::string(description)),
    ])
}

pub(super) fn string_enum_property(values: &[&str], description: &str) -> JsonValue {
    json::object([
        ("type", json::string("string")),
        (
            "enum",
            json::array(values.iter().copied().map(json::string)),
        ),
        ("description", json::string(description)),
    ])
}

pub(super) fn ask_with_reason(reason_type: &str, detail: &str) -> PermissionResult {
    PermissionResult {
        behavior: "ask".into(),
        message: detail.into(),
        reason: Some(PermissionDecisionReason {
            type_name: reason_type.into(),
            detail: detail.into(),
        }),
        suggestions: None,
    }
}

pub(super) fn integer_field(input: &JsonValue, field: &str) -> Option<i64> {
    match input {
        JsonValue::Object(fields) => match fields.get(field) {
            Some(JsonValue::Number(value)) => value.parse().ok(),
            _ => None,
        },
        _ => None,
    }
}

pub(super) fn bool_field(input: &JsonValue, field: &str) -> Option<bool> {
    match input {
        JsonValue::Object(fields) => match fields.get(field) {
            Some(JsonValue::Bool(value)) => Some(*value),
            _ => None,
        },
        _ => None,
    }
}

pub(super) fn resolve_path(path: &str, cwd: &str) -> PathBuf {
    let path = Path::new(path);
    if path.is_absolute() {
        path.to_path_buf()
    } else {
        Path::new(cwd).join(path)
    }
}
