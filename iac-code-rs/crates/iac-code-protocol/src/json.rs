use std::collections::BTreeMap;
use std::fmt::Write;

#[derive(Clone, Debug, PartialEq, Eq)]
pub enum JsonValue {
    Null,
    Bool(bool),
    Number(String),
    String(String),
    Array(Vec<JsonValue>),
    Object(BTreeMap<String, JsonValue>),
}

pub fn parse(text: &str) -> Result<JsonValue, String> {
    serde_json::from_str::<serde_json::Value>(text)
        .map(from_serde)
        .map_err(|error| error.to_string())
}

pub fn from_serde(value: serde_json::Value) -> JsonValue {
    match value {
        serde_json::Value::Null => JsonValue::Null,
        serde_json::Value::Bool(value) => JsonValue::Bool(value),
        serde_json::Value::Number(value) => JsonValue::Number(value.to_string()),
        serde_json::Value::String(value) => JsonValue::String(value),
        serde_json::Value::Array(values) => {
            JsonValue::Array(values.into_iter().map(from_serde).collect())
        }
        serde_json::Value::Object(values) => JsonValue::Object(
            values
                .into_iter()
                .map(|(key, value)| (key, from_serde(value)))
                .collect(),
        ),
    }
}

impl JsonValue {
    pub fn to_compact_json(&self) -> String {
        let mut output = String::new();
        self.write_json(&mut output);
        output
    }

    fn write_json(&self, output: &mut String) {
        match self {
            JsonValue::Null => output.push_str("null"),
            JsonValue::Bool(value) => output.push_str(if *value { "true" } else { "false" }),
            JsonValue::Number(value) => output.push_str(value),
            JsonValue::String(value) => write_string(value, output),
            JsonValue::Array(values) => {
                output.push('[');
                for (index, value) in values.iter().enumerate() {
                    if index > 0 {
                        output.push(',');
                    }
                    value.write_json(output);
                }
                output.push(']');
            }
            JsonValue::Object(values) => {
                output.push('{');
                for (index, (key, value)) in values.iter().enumerate() {
                    if index > 0 {
                        output.push(',');
                    }
                    write_string(key, output);
                    output.push(':');
                    value.write_json(output);
                }
                output.push('}');
            }
        }
    }
}

pub fn null() -> JsonValue {
    JsonValue::Null
}

pub fn bool_value(value: bool) -> JsonValue {
    JsonValue::Bool(value)
}

pub fn number(value: impl ToString) -> JsonValue {
    JsonValue::Number(value.to_string())
}

pub fn float(value: f64) -> JsonValue {
    let mut output = value.to_string();
    if value.is_finite() && !output.contains('.') && !output.contains('e') && !output.contains('E')
    {
        output.push_str(".0");
    }
    JsonValue::Number(output)
}

pub fn string(value: impl Into<String>) -> JsonValue {
    JsonValue::String(value.into())
}

pub fn array(values: impl IntoIterator<Item = JsonValue>) -> JsonValue {
    JsonValue::Array(values.into_iter().collect())
}

pub fn object<K>(entries: impl IntoIterator<Item = (K, JsonValue)>) -> JsonValue
where
    K: Into<String>,
{
    JsonValue::Object(
        entries
            .into_iter()
            .map(|(key, value)| (key.into(), value))
            .collect(),
    )
}

fn write_string(value: &str, output: &mut String) {
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
