use std::cell::RefCell;
use std::rc::Rc;

use iac_code_protocol::json::{self, JsonValue};
use iac_code_protocol::provider::ToolDefinition;
use iac_code_tools::{
    RegistryToolExecutor, Tool, ToolCallRequest, ToolContext, ToolRegistry, ToolResult,
};

#[test]
fn registry_preserves_registration_order_and_exports_tool_definitions() {
    let mut registry = ToolRegistry::new();

    registry.register(Box::new(StaticTool::new(
        "read",
        "Read files",
        json::object([("type", json::string("object"))]),
        true,
        None,
    )));
    registry.register(Box::new(StaticTool::new(
        "write",
        "Write files",
        json::object([("required", json::array([json::string("path")]))]),
        false,
        None,
    )));
    registry.register(Box::new(StaticTool::new(
        "read",
        "Read replacement",
        json::object([("type", json::string("object"))]),
        true,
        None,
    )));

    assert_eq!(registry.list_tool_names(), vec!["read", "write"]);
    assert_eq!(
        registry.to_tool_definitions(),
        vec![
            ToolDefinition {
                name: "read".into(),
                description: "Read replacement".into(),
                input_schema: json::object([("type", json::string("object"))]),
            },
            ToolDefinition {
                name: "write".into(),
                description: "Write files".into(),
                input_schema: json::object([("required", json::array([json::string("path")]))]),
            },
        ]
    );

    registry.unregister("read");
    assert_eq!(registry.list_tool_names(), vec!["write"]);
    assert!(registry.get("read").is_none());
}

#[test]
fn registry_executor_partitions_calls_and_returns_results_in_request_order() {
    let calls = Rc::new(RefCell::new(Vec::new()));
    let mut registry = ToolRegistry::new();
    registry.register(Box::new(StaticTool::new(
        "read",
        "Read",
        json::object([("type", json::string("object"))]),
        true,
        Some(calls.clone()),
    )));
    registry.register(Box::new(StaticTool::new(
        "write",
        "Write",
        json::object([("type", json::string("object"))]),
        false,
        Some(calls.clone()),
    )));

    let executor = RegistryToolExecutor::new(registry).with_context(ToolContext {
        cwd: "/tmp/iac-code-rs-test".into(),
    });
    let requests = vec![
        request(
            "write-1",
            "write",
            json::object([("path", json::string("a.txt"))]),
        ),
        request(
            "read-1",
            "read",
            json::object([("path", json::string("a.txt"))]),
        ),
        request("missing-1", "missing", empty_object()),
        request("invalid-1", "read", empty_object()),
    ];

    let partition = executor.partition(&requests);
    assert_eq!(
        tool_use_ids(&partition.concurrent),
        vec!["read-1", "invalid-1"]
    );
    assert_eq!(
        tool_use_ids(&partition.serial),
        vec!["write-1", "missing-1"]
    );

    let results = executor.execute_batch(&requests);

    assert_eq!(
        results,
        vec![
            ToolResult::success("write /tmp/iac-code-rs-test a.txt"),
            ToolResult::success("read /tmp/iac-code-rs-test a.txt"),
            ToolResult::error("Unknown tool: missing"),
            ToolResult::error(
                "Invalid input for tool 'read': missing required field 'path'. Please provide all required parameters as defined in the tool schema."
            ),
        ]
    );
    assert_eq!(
        calls.borrow().as_slice(),
        &["write:a.txt".to_string(), "read:a.txt".to_string()]
    );
}

fn request(tool_use_id: &str, tool_name: &str, input: JsonValue) -> ToolCallRequest {
    ToolCallRequest {
        tool_use_id: tool_use_id.into(),
        tool_name: tool_name.into(),
        input,
    }
}

fn empty_object() -> JsonValue {
    json::object(Vec::<(&str, JsonValue)>::new())
}

fn tool_use_ids(requests: &[ToolCallRequest]) -> Vec<&str> {
    requests
        .iter()
        .map(|request| request.tool_use_id.as_str())
        .collect()
}

struct StaticTool {
    name: String,
    description: String,
    input_schema: JsonValue,
    read_only: bool,
    calls: Option<Rc<RefCell<Vec<String>>>>,
}

impl StaticTool {
    fn new(
        name: &str,
        description: &str,
        input_schema: JsonValue,
        read_only: bool,
        calls: Option<Rc<RefCell<Vec<String>>>>,
    ) -> Self {
        Self {
            name: name.into(),
            description: description.into(),
            input_schema,
            read_only,
            calls,
        }
    }
}

impl Tool for StaticTool {
    fn name(&self) -> &str {
        &self.name
    }

    fn description(&self) -> &str {
        &self.description
    }

    fn input_schema(&self) -> JsonValue {
        self.input_schema.clone()
    }

    fn validate_input(&self, input: &JsonValue) -> Result<(), String> {
        path(input)
            .map(|_| ())
            .ok_or_else(|| "missing required field 'path'".into())
    }

    fn execute(&self, input: &JsonValue, context: &ToolContext) -> ToolResult {
        let path = path(input).expect("input was validated before execution");
        if let Some(calls) = &self.calls {
            calls.borrow_mut().push(format!("{}:{}", self.name, path));
        }
        ToolResult::success(format!("{} {} {}", self.name, context.cwd, path))
    }

    fn is_read_only(&self, _input: &JsonValue) -> bool {
        self.read_only
    }
}

fn path(input: &JsonValue) -> Option<&str> {
    match input {
        JsonValue::Object(entries) => match entries.get("path") {
            Some(JsonValue::String(path)) => Some(path.as_str()),
            _ => None,
        },
        _ => None,
    }
}
