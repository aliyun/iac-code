use iac_code_protocol::json::{self, JsonValue};
use iac_code_protocol::provider::ToolDefinition;

pub(super) fn convert_tools(tools: &[ToolDefinition]) -> JsonValue {
    json::array(tools.iter().map(|tool| {
        json::object([
            ("type", json::string("function")),
            (
                "function",
                json::object([
                    ("name", json::string(&tool.name)),
                    ("description", json::string(&tool.description)),
                    ("parameters", tool.input_schema.clone()),
                ]),
            ),
        ])
    }))
}
