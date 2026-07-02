use std::fs;

use iac_code_protocol::json::JsonValue;
use iac_code_protocol::permission::{PermissionResult, ToolPermissionContext};

use super::common::{resolve_path, string_field, string_property, tool_schema};
use crate::path_safety::{check_read_path, PathDecision};
use crate::{Tool, ToolContext, ToolResult};

#[derive(Clone, Copy, Debug, Default, PartialEq, Eq)]
pub struct ListFilesTool;

impl ListFilesTool {
    pub fn new() -> Self {
        Self
    }
}

impl Tool for ListFilesTool {
    fn name(&self) -> &str {
        "list_files"
    }

    fn description(&self) -> &str {
        "List the contents of a directory. Returns file and directory names with indicators for type."
    }

    fn input_schema(&self) -> JsonValue {
        tool_schema(
            &[],
            vec![(
                "path",
                string_property(
                    "The directory path to list. Defaults to current working directory.",
                ),
            )],
        )
    }

    fn execute(&self, input: &JsonValue, context: &ToolContext) -> ToolResult {
        let requested_path = string_field(input, "path").unwrap_or(&context.cwd);
        let path = resolve_path(requested_path, &context.cwd);

        if !path.exists() {
            return ToolResult::error(format!("Path not found: {}", path.display()));
        }
        if !path.is_dir() {
            return ToolResult::error(format!("Not a directory: {}", path.display()));
        }

        let mut entries = match fs::read_dir(&path) {
            Ok(entries) => entries
                .filter_map(Result::ok)
                .collect::<Vec<fs::DirEntry>>(),
            Err(error) if error.kind() == std::io::ErrorKind::PermissionDenied => {
                return ToolResult::error(format!("Permission denied: {}", path.display()));
            }
            Err(error) => {
                return ToolResult::error(format!("Error listing directory: {}", error));
            }
        };
        entries.sort_by_key(|entry| entry.file_name());

        if entries.is_empty() {
            return ToolResult::success(format!("Directory {} is empty.", path.display()));
        }

        let mut lines = Vec::new();
        for entry in entries {
            let name = entry.file_name().to_string_lossy().into_owned();
            let Ok(metadata) = entry.metadata() else {
                continue;
            };
            if metadata.is_dir() {
                lines.push(format!("  {}/", name));
            } else {
                lines.push(format!("  {} ({})", name, format_size(metadata.len())));
            }
        }

        ToolResult::success(format!(
            "Directory: {}\n\n{}",
            path.display(),
            lines.join("\n")
        ))
    }

    fn is_read_only(&self, _input: &JsonValue) -> bool {
        true
    }

    fn user_facing_name(&self, _input: &JsonValue) -> String {
        "List".into()
    }

    fn check_permissions(
        &self,
        input: &JsonValue,
        context: &ToolPermissionContext,
    ) -> PermissionResult {
        let path = string_field(input, "path").unwrap_or(&context.cwd);
        match check_read_path(
            path,
            &context.cwd,
            &context.additional_directories,
            &context.trusted_read_directories,
        ) {
            PathDecision::Allow => PermissionResult::allow(),
            decision => decision.to_permission_result(),
        }
    }
}

fn format_size(size: u64) -> String {
    let mut size = size as f64;
    let units = ["B", "KB", "MB", "GB"];
    for unit in units {
        if size < 1024.0 {
            return if unit == "B" {
                format!("{:.0}B", size)
            } else {
                format!("{:.1}{}", size, unit)
            };
        }
        size /= 1024.0;
    }
    format!("{:.1}TB", size)
}
