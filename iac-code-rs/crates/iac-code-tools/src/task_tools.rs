use iac_code_protocol::json::{self, JsonValue};

use crate::{Tool, ToolContext, ToolRegistry, ToolResult};

mod formatting;
mod model;
mod schema;

use formatting::truncate_chars;
pub use model::{TaskInfo, TaskManager, TaskStatus};
use schema::{string_field, task_id_schema};

#[derive(Clone, Debug)]
pub struct TaskListTool {
    manager: TaskManager,
}

impl TaskListTool {
    pub fn new(manager: TaskManager) -> Self {
        Self { manager }
    }
}

impl Tool for TaskListTool {
    fn name(&self) -> &str {
        "task_list"
    }

    fn description(&self) -> &str {
        "List all background tasks with their status."
    }

    fn input_schema(&self) -> JsonValue {
        json::object([
            ("type", json::string("object")),
            ("properties", json::object(Vec::<(&str, JsonValue)>::new())),
        ])
    }

    fn execute(&self, _input: &JsonValue, _context: &ToolContext) -> ToolResult {
        let tasks = self.manager.list_all();
        if tasks.is_empty() {
            return ToolResult::success("No background tasks.");
        }

        let mut lines = Vec::new();
        for task in tasks {
            lines.push(format!(
                "- [{}] {} | [{}] {}",
                task.id,
                task.status.as_str(),
                task.agent_type,
                task.description
            ));
            if let Some(result) = task.result.filter(|result| !result.is_empty()) {
                lines.push(format!("  Result: {}", truncate_chars(&result, 200)));
            }
            if let Some(error) = task.error.filter(|error| !error.is_empty()) {
                lines.push(format!("  Error: {}", truncate_chars(&error, 200)));
            }
        }
        ToolResult::success(lines.join("\n"))
    }

    fn is_read_only(&self, _input: &JsonValue) -> bool {
        true
    }
}

#[derive(Clone, Debug)]
pub struct TaskGetTool {
    manager: TaskManager,
}

impl TaskGetTool {
    pub fn new(manager: TaskManager) -> Self {
        Self { manager }
    }
}

impl Tool for TaskGetTool {
    fn name(&self) -> &str {
        "task_get"
    }

    fn description(&self) -> &str {
        "Get details of a specific background task by ID."
    }

    fn input_schema(&self) -> JsonValue {
        task_id_schema()
    }

    fn validate_input(&self, input: &JsonValue) -> Result<(), String> {
        if string_field(input, "task_id").is_none() {
            return Err("missing required field 'task_id'".into());
        }
        Ok(())
    }

    fn execute(&self, input: &JsonValue, _context: &ToolContext) -> ToolResult {
        let task_id = string_field(input, "task_id").unwrap_or("");
        let Some(task) = self.manager.get(task_id) else {
            return ToolResult::error(format!("Task '{task_id}' not found."));
        };

        let mut lines = vec![
            format!("ID: {}", task.id),
            format!("Description: {}", task.description),
            format!("Status: {}", task.status.as_str()),
            format!("Agent type: {}", task.agent_type),
            format!("Tool uses: {}", task.tool_use_count),
            format!("Tokens: {}", task.token_count),
        ];
        if let Some(result) = task.result.filter(|result| !result.is_empty()) {
            lines.push(format!("Result: {result}"));
        }
        if let Some(error) = task.error.filter(|error| !error.is_empty()) {
            lines.push(format!("Error: {error}"));
        }
        ToolResult::success(lines.join("\n"))
    }

    fn is_read_only(&self, _input: &JsonValue) -> bool {
        true
    }
}

#[derive(Clone, Debug)]
pub struct TaskStopTool {
    manager: TaskManager,
}

impl TaskStopTool {
    pub fn new(manager: TaskManager) -> Self {
        Self { manager }
    }
}

impl Tool for TaskStopTool {
    fn name(&self) -> &str {
        "task_stop"
    }

    fn description(&self) -> &str {
        "Stop a running background task."
    }

    fn input_schema(&self) -> JsonValue {
        task_id_schema()
    }

    fn validate_input(&self, input: &JsonValue) -> Result<(), String> {
        if string_field(input, "task_id").is_none() {
            return Err("missing required field 'task_id'".into());
        }
        Ok(())
    }

    fn execute(&self, input: &JsonValue, _context: &ToolContext) -> ToolResult {
        let task_id = string_field(input, "task_id").unwrap_or("");
        let Some(task) = self.manager.get(task_id) else {
            return ToolResult::error(format!("Task '{task_id}' not found."));
        };
        if !self.manager.stop(task_id) {
            return ToolResult::success(format!(
                "Task '{task_id}' already {}.",
                task.status.as_str()
            ));
        }
        ToolResult::success(format!("Task '{task_id}' stopped."))
    }

    fn is_read_only(&self, _input: &JsonValue) -> bool {
        false
    }
}

pub fn register_task_tools(registry: &mut ToolRegistry, manager: TaskManager) {
    registry.register(Box::new(TaskListTool::new(manager.clone())));
    registry.register(Box::new(TaskGetTool::new(manager.clone())));
    registry.register(Box::new(TaskStopTool::new(manager)));
}
