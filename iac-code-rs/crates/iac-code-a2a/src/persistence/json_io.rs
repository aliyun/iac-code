use std::fmt::Write;
use std::fs;
use std::path::Path;

use iac_code_protocol::json::{self, JsonValue};

use crate::types::TaskStoreError;

pub(super) fn write_json(path: &Path, value: &JsonValue) -> Result<(), TaskStoreError> {
    let tmp = path.with_extension("json.tmp");
    fs::write(&tmp, to_python_json(value)).map_err(io_error)?;
    fs::rename(tmp, path).map_err(io_error)
}

pub(super) fn read_json(path: &Path) -> Option<JsonValue> {
    let text = fs::read_to_string(path).ok()?;
    json::parse(&text).ok()
}

fn io_error(error: std::io::Error) -> TaskStoreError {
    TaskStoreError::InvalidState(error.to_string())
}

fn to_python_json(value: &JsonValue) -> String {
    let mut output = String::new();
    write_python_json(value, &mut output);
    output
}

fn write_python_json(value: &JsonValue, output: &mut String) {
    match value {
        JsonValue::Null => output.push_str("null"),
        JsonValue::Bool(value) => output.push_str(if *value { "true" } else { "false" }),
        JsonValue::Number(value) => output.push_str(value),
        JsonValue::String(value) => write_json_string(value, output),
        JsonValue::Array(values) => {
            output.push('[');
            for (index, value) in values.iter().enumerate() {
                if index > 0 {
                    output.push_str(", ");
                }
                write_python_json(value, output);
            }
            output.push(']');
        }
        JsonValue::Object(values) => {
            output.push('{');
            for (index, (key, value)) in values.iter().enumerate() {
                if index > 0 {
                    output.push_str(", ");
                }
                write_json_string(key, output);
                output.push_str(": ");
                write_python_json(value, output);
            }
            output.push('}');
        }
    }
}

fn write_json_string(value: &str, output: &mut String) {
    output.push('"');
    for character in value.chars() {
        match character {
            '"' => output.push_str("\\\""),
            '\\' => output.push_str("\\\\"),
            '\u{08}' => output.push_str("\\b"),
            '\u{0c}' => output.push_str("\\f"),
            '\n' => output.push_str("\\n"),
            '\r' => output.push_str("\\r"),
            '\t' => output.push_str("\\t"),
            character if character <= '\u{1f}' => {
                write!(output, "\\u{:04x}", character as u32)
                    .expect("writing to String should not fail");
            }
            character => output.push(character),
        }
    }
    output.push('"');
}
