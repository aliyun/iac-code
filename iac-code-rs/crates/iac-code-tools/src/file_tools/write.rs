use std::fs;
use std::path::Path;

use iac_code_protocol::json::JsonValue;
use iac_code_protocol::permission::{PermissionResult, ToolPermissionContext};

use super::common::{
    ask_with_reason, path_field, resolve_path, string_field, string_property, tool_schema,
};
use crate::path_safety::{check_write_path, PathDecision};
use crate::{Tool, ToolContext, ToolResult};

#[derive(Clone, Copy, Debug, Default, PartialEq, Eq)]
pub struct WriteFileTool;

impl WriteFileTool {
    pub fn new() -> Self {
        Self
    }
}

impl Tool for WriteFileTool {
    fn name(&self) -> &str {
        "write_file"
    }

    fn description(&self) -> &str {
        "Write content to a file. Creates the file if it doesn't exist, or overwrites it if it does."
    }

    fn input_schema(&self) -> JsonValue {
        tool_schema(
            &["path", "content"],
            vec![
                (
                    "path",
                    string_property(
                        "The path to write the file to. Always emit this field FIRST in the JSON arguments, before 'content'.",
                    ),
                ),
                ("content", string_property("The content to write to the file.")),
            ],
        )
    }

    fn validate_input(&self, input: &JsonValue) -> Result<(), String> {
        if path_field(input).is_none() {
            return Err("missing required field 'path'".into());
        }
        if string_field(input, "content").is_none() {
            return Err("missing required field 'content'".into());
        }
        Ok(())
    }

    fn execute(&self, input: &JsonValue, context: &ToolContext) -> ToolResult {
        let Some(path) = path_field(input) else {
            return ToolResult::error("Invalid input for tool 'write_file': missing required field 'path'. Please provide all required parameters as defined in the tool schema.");
        };
        let Some(content) = string_field(input, "content") else {
            return ToolResult::error("Invalid input for tool 'write_file': missing required field 'content'. Please provide all required parameters as defined in the tool schema.");
        };

        let path = resolve_path(path, &context.cwd);
        if let Some(parent) = path.parent() {
            if let Err(error) = fs::create_dir_all(parent) {
                return write_error(&path, error);
            }
        }

        if let Err(error) = fs::write(&path, content) {
            return write_error(&path, error);
        }

        let lines = count_python_write_lines(content);
        ToolResult::success(format!(
            "Successfully wrote {} lines to {}",
            lines,
            path.display()
        ))
    }

    fn is_read_only(&self, _input: &JsonValue) -> bool {
        false
    }

    fn user_facing_name(&self, _input: &JsonValue) -> String {
        "Write".into()
    }

    fn check_permissions(
        &self,
        input: &JsonValue,
        context: &ToolPermissionContext,
    ) -> PermissionResult {
        let Some(path) = path_field(input) else {
            return ask_with_reason("path_constraint", "write file path is required");
        };
        if path.is_empty() {
            return ask_with_reason("path_constraint", "write file path is required");
        }

        match check_write_path(path, &context.cwd, &context.additional_directories) {
            PathDecision::Allow => {
                PermissionResult::ask(format!("Allow {}?", self.user_facing_name(input)))
            }
            decision => decision.to_permission_result(),
        }
    }
}

fn count_python_write_lines(content: &str) -> usize {
    content.matches('\n').count() + usize::from(!content.is_empty() && !content.ends_with('\n'))
}

fn write_error(path: &Path, error: std::io::Error) -> ToolResult {
    if error.kind() == std::io::ErrorKind::PermissionDenied {
        ToolResult::error(format!("Permission denied: {}", path.display()))
    } else {
        ToolResult::error(format!("Error writing file: {}", error))
    }
}
