use iac_code_protocol::json::{self, JsonValue};

use super::definitions::builtin_agent_definitions;

pub fn agent_tool_description() -> String {
    let agent_list = builtin_agent_definitions()
        .into_iter()
        .map(|definition| format!("  - {}: {}", definition.agent_type, definition.when_to_use))
        .collect::<Vec<_>>()
        .join("\n");
    format!("Launch a sub-agent to handle complex tasks.\n\nAvailable agent types:\n{agent_list}")
}

pub fn agent_input_schema() -> JsonValue {
    json::object([
        ("type", json::string("object")),
        (
            "properties",
            json::object([
                (
                    "prompt",
                    json::object([
                        ("type", json::string("string")),
                        (
                            "description",
                            json::string("The task for the sub-agent to perform."),
                        ),
                    ]),
                ),
                (
                    "description",
                    json::object([
                        ("type", json::string("string")),
                        (
                            "description",
                            json::string("Short (3-5 word) description of the task."),
                        ),
                    ]),
                ),
                (
                    "subagent_type",
                    json::object([
                        ("type", json::string("string")),
                        (
                            "enum",
                            json::array(
                                builtin_agent_definitions()
                                    .into_iter()
                                    .map(|definition| json::string(&definition.agent_type))
                                    .collect::<Vec<_>>(),
                            ),
                        ),
                        (
                            "description",
                            json::string("The type of specialized agent to use."),
                        ),
                    ]),
                ),
                (
                    "run_in_background",
                    json::object([
                        ("type", json::string("boolean")),
                        ("description", json::string("Run agent in background.")),
                    ]),
                ),
            ]),
        ),
        (
            "required",
            json::array([json::string("prompt"), json::string("description")]),
        ),
    ])
}

pub fn string_field<'a>(value: &'a JsonValue, field: &str) -> Option<&'a str> {
    let JsonValue::Object(fields) = value else {
        return None;
    };
    let Some(JsonValue::String(value)) = fields.get(field) else {
        return None;
    };
    Some(value)
}

pub fn bool_field(value: &JsonValue, field: &str) -> Option<bool> {
    let JsonValue::Object(fields) = value else {
        return None;
    };
    let Some(JsonValue::Bool(value)) = fields.get(field) else {
        return None;
    };
    Some(*value)
}
