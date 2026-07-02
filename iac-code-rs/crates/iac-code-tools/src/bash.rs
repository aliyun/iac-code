use iac_code_protocol::json::JsonValue;
use iac_code_protocol::permission::{PermissionResult, ToolPermissionContext};

use crate::{Tool, ToolContext, ToolResult};

mod execution;
mod input;
mod parser;
mod path_args;
mod pipeline;
mod readonly;
mod result;
mod rules;
mod safety;
mod sed;
mod subcommands;
mod suggestions;

use execution::{execute_shell_command, ShellExecution};
use input::{input_schema, json_field, parse_timeout_seconds, string_field, timeout_seconds};
use pipeline::bash_tool_has_permission;

#[derive(Clone, Copy, Debug, Default, PartialEq, Eq)]
pub struct BashTool;

impl BashTool {
    pub fn new() -> Self {
        Self
    }
}

impl Tool for BashTool {
    fn name(&self) -> &str {
        "bash"
    }

    fn description(&self) -> &str {
        "Execute a shell command in the system's default shell. Use this for running programs, installing packages, searching code, running tests, git operations, and other system tasks. Commands are executed with a timeout of 120 seconds by default."
    }

    fn input_schema(&self) -> JsonValue {
        input_schema()
    }

    fn validate_input(&self, input: &JsonValue) -> Result<(), String> {
        if string_field(input, "command").is_none() {
            return Err("missing required field 'command'".into());
        }
        if let Some(value) = json_field(input, "timeout") {
            parse_timeout_seconds(value)?;
        }
        Ok(())
    }

    fn execute(&self, input: &JsonValue, context: &ToolContext) -> ToolResult {
        let command = string_field(input, "command").unwrap_or("");
        let timeout = match timeout_seconds(input) {
            Ok(timeout) => timeout,
            Err(error) => return ToolResult::error(format!("Error executing command: {error}")),
        };

        match execute_shell_command(command, timeout, &context.cwd) {
            Ok(ShellExecution::Completed(output)) => output.into_tool_result(),
            Ok(ShellExecution::TimedOut) => ToolResult::error(format!(
                "Command timed out after {timeout} seconds: {command}"
            )),
            Err(error) => ToolResult::error(format!("Error executing command: {error}")),
        }
    }

    fn supports_blanket_allow(&self) -> bool {
        false
    }

    fn is_read_only(&self, _input: &JsonValue) -> bool {
        false
    }

    fn user_facing_name(&self, _input: &JsonValue) -> String {
        "Bash".into()
    }

    fn check_permissions(
        &self,
        input: &JsonValue,
        context: &ToolPermissionContext,
    ) -> PermissionResult {
        let command = string_field(input, "command").unwrap_or("");
        if command.is_empty() {
            return PermissionResult::allow();
        }
        bash_tool_has_permission(command, context)
    }
}
