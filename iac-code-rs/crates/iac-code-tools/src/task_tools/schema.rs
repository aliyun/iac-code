use iac_code_protocol::json::{self, JsonValue};

pub fn task_id_schema() -> JsonValue {
    json::object([
        ("type", json::string("object")),
        (
            "properties",
            json::object([("task_id", json::object([("type", json::string("string"))]))]),
        ),
        ("required", json::array([json::string("task_id")])),
    ])
}

pub fn string_field<'a>(input: &'a JsonValue, field: &str) -> Option<&'a str> {
    match input {
        JsonValue::Object(fields) => match fields.get(field) {
            Some(JsonValue::String(value)) => Some(value.as_str()),
            _ => None,
        },
        _ => None,
    }
}
