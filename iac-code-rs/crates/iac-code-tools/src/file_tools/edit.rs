use std::fs;

use iac_code_protocol::json::JsonValue;
use iac_code_protocol::permission::{PermissionResult, ToolPermissionContext};

use super::common::{
    ask_with_reason, path_field, resolve_path, string_field, string_property, tool_schema,
};
use crate::path_safety::{check_write_path, PathDecision};
use crate::{Tool, ToolContext, ToolResult};

#[derive(Clone, Copy, Debug, Default, PartialEq, Eq)]
pub struct EditFileTool;

impl EditFileTool {
    pub fn new() -> Self {
        Self
    }
}

impl Tool for EditFileTool {
    fn name(&self) -> &str {
        "edit_file"
    }

    fn description(&self) -> &str {
        "Make targeted edits to a file using search and replace. The old_string must match exactly one location in the file."
    }

    fn input_schema(&self) -> JsonValue {
        tool_schema(
            &["path", "old_string", "new_string"],
            vec![
                (
                    "path",
                    string_property(
                        "The path to the file to edit. Always emit this field FIRST in the JSON arguments, before 'old_string' and 'new_string'.",
                    ),
                ),
                (
                    "old_string",
                    string_property(
                        "The exact string to search for in the file. Must match exactly one location.",
                    ),
                ),
                (
                    "new_string",
                    string_property("The string to replace old_string with."),
                ),
            ],
        )
    }

    fn validate_input(&self, input: &JsonValue) -> Result<(), String> {
        if path_field(input).is_none() {
            return Err("missing required field 'path'".into());
        }
        if string_field(input, "old_string").is_none() {
            return Err("missing required field 'old_string'".into());
        }
        if string_field(input, "new_string").is_none() {
            return Err("missing required field 'new_string'".into());
        }
        Ok(())
    }

    fn execute(&self, input: &JsonValue, context: &ToolContext) -> ToolResult {
        let Some(path) = path_field(input) else {
            return ToolResult::error("Invalid input for tool 'edit_file': missing required field 'path'. Please provide all required parameters as defined in the tool schema.");
        };
        let Some(old_string) = string_field(input, "old_string") else {
            return ToolResult::error("Invalid input for tool 'edit_file': missing required field 'old_string'. Please provide all required parameters as defined in the tool schema.");
        };
        let Some(new_string) = string_field(input, "new_string") else {
            return ToolResult::error("Invalid input for tool 'edit_file': missing required field 'new_string'. Please provide all required parameters as defined in the tool schema.");
        };

        let path = resolve_path(path, &context.cwd);
        if !path.exists() {
            return ToolResult::error(format!("File not found: {}", path.display()));
        }

        let content = match fs::read_to_string(&path) {
            Ok(content) => content,
            Err(error) => {
                return ToolResult::error(format!("Error reading file: {}", error));
            }
        };

        let count = count_python_string_matches(&content, old_string);
        if count == 0 {
            return ToolResult::error(format!(
                "old_string not found in {}. Make sure the string matches exactly, including whitespace and indentation.",
                path.display()
            ));
        }
        if count > 1 {
            return ToolResult::error(format!(
                "old_string found {} times in {}. It must match exactly once. Include more surrounding context to make the match unique.",
                count,
                path.display()
            ));
        }

        let new_content = replace_once_python_style(&content, old_string, new_string);
        if let Err(error) = fs::write(&path, new_content) {
            return ToolResult::error(format!("Error writing file: {}", error));
        }

        ToolResult::success(format!("Successfully edited {}", path.display()))
    }

    fn is_read_only(&self, _input: &JsonValue) -> bool {
        false
    }

    fn user_facing_name(&self, input: &JsonValue) -> String {
        match string_field(input, "old_string") {
            None => "Edit".into(),
            Some("") => "Create".into(),
            Some(_) => "Update".into(),
        }
    }

    fn check_permissions(
        &self,
        input: &JsonValue,
        context: &ToolPermissionContext,
    ) -> PermissionResult {
        let Some(path) = path_field(input) else {
            return ask_with_reason("path_constraint", "edit file path is required");
        };
        if path.is_empty() {
            return ask_with_reason("path_constraint", "edit file path is required");
        }

        match check_write_path(path, &context.cwd, &context.additional_directories) {
            PathDecision::Allow => {
                PermissionResult::ask(format!("Allow {}?", self.user_facing_name(input)))
            }
            decision => decision.to_permission_result(),
        }
    }
}

fn count_python_string_matches(content: &str, needle: &str) -> usize {
    if needle.is_empty() {
        content.chars().count() + 1
    } else {
        content.matches(needle).count()
    }
}

fn replace_once_python_style(content: &str, old_string: &str, new_string: &str) -> String {
    if old_string.is_empty() {
        format!("{new_string}{content}")
    } else {
        content.replacen(old_string, new_string, 1)
    }
}
