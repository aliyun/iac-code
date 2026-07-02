use iac_code_protocol::json::JsonValue;
use iac_code_protocol::permission::{PermissionResult, ToolPermissionContext};
use iac_code_protocol::provider::ToolDefinition;

use crate::ToolResult;

#[derive(Clone, Debug, PartialEq, Eq)]
pub struct ToolContext {
    pub cwd: String,
}

impl Default for ToolContext {
    fn default() -> Self {
        Self { cwd: ".".into() }
    }
}

pub trait Tool {
    fn name(&self) -> &str;

    fn description(&self) -> &str;

    fn input_schema(&self) -> JsonValue;

    fn validate_input(&self, _input: &JsonValue) -> Result<(), String> {
        Ok(())
    }

    fn execute(&self, input: &JsonValue, context: &ToolContext) -> ToolResult;

    fn is_read_only(&self, _input: &JsonValue) -> bool {
        false
    }

    fn is_concurrency_safe(&self, input: &JsonValue) -> bool {
        self.is_read_only(input)
    }

    fn supports_blanket_allow(&self) -> bool {
        true
    }

    fn user_facing_name(&self, _input: &JsonValue) -> String {
        self.name().to_owned()
    }

    fn check_permissions(
        &self,
        input: &JsonValue,
        _context: &ToolPermissionContext,
    ) -> PermissionResult {
        if self.is_read_only(input) {
            PermissionResult::allow()
        } else {
            PermissionResult::ask(format!("Allow {}?", self.user_facing_name(input)))
        }
    }
}

#[derive(Default)]
pub struct ToolRegistry {
    tools: Vec<Box<dyn Tool>>,
}

impl ToolRegistry {
    pub fn new() -> Self {
        Self::default()
    }

    pub fn register(&mut self, tool: Box<dyn Tool>) {
        let name = tool.name().to_owned();
        if let Some(existing) = self
            .tools
            .iter_mut()
            .find(|existing| existing.name() == name)
        {
            *existing = tool;
            return;
        }

        self.tools.push(tool);
    }

    pub fn unregister(&mut self, name: &str) {
        self.tools.retain(|tool| tool.name() != name);
    }

    pub fn get(&self, name: &str) -> Option<&dyn Tool> {
        self.tools
            .iter()
            .find(|tool| tool.name() == name)
            .map(Box::as_ref)
    }

    pub fn list_tool_names(&self) -> Vec<String> {
        self.tools
            .iter()
            .map(|tool| tool.name().to_owned())
            .collect()
    }

    pub fn to_tool_definitions(&self) -> Vec<ToolDefinition> {
        self.tools
            .iter()
            .map(|tool| ToolDefinition {
                name: tool.name().to_owned(),
                description: tool.description().to_owned(),
                input_schema: tool.input_schema(),
            })
            .collect()
    }
}
