use std::sync::Arc;

use iac_code_protocol::json::JsonValue;

use crate::{TaskManager, Tool, ToolContext, ToolRegistry, ToolResult};

mod definitions;
mod model;
mod runner;
mod schema;

pub use definitions::{builtin_agent_definitions, get_agent_definition};
pub use model::{AgentDefinition, AgentProgress, SubAgentRequest, SubAgentResult, SubAgentRunner};
use runner::{run_in_background, run_in_foreground};
use schema::{agent_input_schema, agent_tool_description, bool_field, string_field};

#[derive(Clone)]
pub struct AgentTool {
    runner: Arc<dyn SubAgentRunner>,
    task_manager: Option<TaskManager>,
    description: String,
}

impl AgentTool {
    pub fn new(runner: Arc<dyn SubAgentRunner>) -> Self {
        Self {
            runner,
            task_manager: None,
            description: agent_tool_description(),
        }
    }

    pub fn with_task_manager(mut self, task_manager: TaskManager) -> Self {
        self.task_manager = Some(task_manager);
        self
    }
}

impl Tool for AgentTool {
    fn name(&self) -> &str {
        "agent"
    }

    fn description(&self) -> &str {
        &self.description
    }

    fn input_schema(&self) -> JsonValue {
        agent_input_schema()
    }

    fn validate_input(&self, input: &JsonValue) -> Result<(), String> {
        if string_field(input, "prompt").is_none() {
            return Err("missing required field 'prompt'".into());
        }
        if string_field(input, "description").is_none() {
            return Err("missing required field 'description'".into());
        }
        Ok(())
    }

    fn execute(&self, input: &JsonValue, context: &ToolContext) -> ToolResult {
        let prompt = string_field(input, "prompt").unwrap_or_default().to_owned();
        let description = string_field(input, "description")
            .unwrap_or("Sub-agent task")
            .to_owned();
        let agent_type = string_field(input, "subagent_type")
            .or_else(|| string_field(input, "agent_type"))
            .unwrap_or("general-purpose")
            .to_owned();

        if get_agent_definition(&agent_type).is_none() {
            return ToolResult::error(format!("Unknown agent type: '{agent_type}'"));
        }

        let request = SubAgentRequest {
            prompt,
            agent_type: agent_type.clone(),
            cwd: context.cwd.clone(),
        };
        if bool_field(input, "run_in_background") == Some(true) {
            if let Some(task_manager) = &self.task_manager {
                return run_in_background(
                    Arc::clone(&self.runner),
                    task_manager,
                    request,
                    &description,
                    &agent_type,
                );
            }
        }

        run_in_foreground(self.runner.as_ref(), request)
    }

    fn user_facing_name(&self, input: &JsonValue) -> String {
        match string_field(input, "subagent_type").or_else(|| string_field(input, "agent_type")) {
            Some("explore") => "Explore".into(),
            Some("plan") => "Plan".into(),
            _ => "Agent".into(),
        }
    }
}

pub fn register_agent_tools(registry: &mut ToolRegistry, runner: Arc<dyn SubAgentRunner>) {
    registry.register(Box::new(AgentTool::new(runner)));
}
