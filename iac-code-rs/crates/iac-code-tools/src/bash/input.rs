use iac_code_protocol::json::{self, JsonValue};

const DEFAULT_TIMEOUT_SECONDS: u64 = 120;

pub(super) fn input_schema() -> JsonValue {
    json::object([
        ("type", json::string("object")),
        (
            "properties",
            json::object([
                (
                    "command",
                    json::object([
                        ("type", json::string("string")),
                        ("description", json::string("The shell command to execute.")),
                    ]),
                ),
                (
                    "timeout",
                    json::object([
                        ("type", json::string("integer")),
                        (
                            "description",
                            json::string("Timeout in seconds. Defaults to 120."),
                        ),
                    ]),
                ),
            ]),
        ),
        ("required", json::array([json::string("command")])),
    ])
}

pub(super) fn string_field<'a>(input: &'a JsonValue, field: &str) -> Option<&'a str> {
    match json_field(input, field) {
        Some(JsonValue::String(value)) => Some(value.as_str()),
        _ => None,
    }
}

pub(super) fn timeout_seconds(input: &JsonValue) -> Result<u64, String> {
    match json_field(input, "timeout") {
        Some(value) => parse_timeout_seconds(value),
        None => Ok(DEFAULT_TIMEOUT_SECONDS),
    }
}

pub(super) fn parse_timeout_seconds(value: &JsonValue) -> Result<u64, String> {
    match value {
        JsonValue::Number(raw) => raw
            .parse::<u64>()
            .map_err(|_| "field 'timeout' must be a non-negative integer".to_owned()),
        _ => Err("field 'timeout' must be a non-negative integer".to_owned()),
    }
}

pub(super) fn json_field<'a>(input: &'a JsonValue, field: &str) -> Option<&'a JsonValue> {
    match input {
        JsonValue::Object(fields) => fields.get(field),
        _ => None,
    }
}
