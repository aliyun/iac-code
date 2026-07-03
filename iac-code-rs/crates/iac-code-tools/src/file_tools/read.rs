use std::fs;
use std::io::{BufRead, BufReader};

use iac_code_protocol::json::JsonValue;
use iac_code_protocol::permission::{PermissionResult, ToolPermissionContext};

use super::common::{
    ask_with_reason, integer_field, integer_property, path_field, resolve_path, string_property,
    tool_schema,
};
use crate::path_safety::{check_read_path, PathDecision};
use crate::{Tool, ToolContext, ToolResult};

const MAX_READ_BYTES: usize = 10 * 1024 * 1024;
const MAX_READ_LINES: usize = 50_000;

#[derive(Clone, Copy, Debug, Default, PartialEq, Eq)]
pub struct ReadFileTool;

impl ReadFileTool {
    pub fn new() -> Self {
        Self
    }
}

impl Tool for ReadFileTool {
    fn name(&self) -> &str {
        "read_file"
    }

    fn description(&self) -> &str {
        "Read the contents of a file. You can optionally specify a line range to read only a portion of the file."
    }

    fn input_schema(&self) -> JsonValue {
        tool_schema(
            &["path"],
            vec![
                (
                    "path",
                    string_property(
                        "The path to the file to read. Can be absolute or relative to working directory.",
                    ),
                ),
                (
                    "start_line",
                    integer_property(
                        "The starting line number to read from (1-based, inclusive). Optional.",
                    ),
                ),
                (
                    "end_line",
                    integer_property(
                        "The ending line number to read to (1-based, inclusive). Optional.",
                    ),
                ),
            ],
        )
    }

    fn validate_input(&self, input: &JsonValue) -> Result<(), String> {
        match path_field(input) {
            Some(_) => Ok(()),
            None => Err("missing required field 'path'".into()),
        }
    }

    fn execute(&self, input: &JsonValue, context: &ToolContext) -> ToolResult {
        let Some(path) = path_field(input) else {
            return ToolResult::error("Invalid input for tool 'read_file': missing required field 'path'. Please provide all required parameters as defined in the tool schema.");
        };
        let path = resolve_path(path, &context.cwd);
        let start_line = integer_field(input, "start_line");
        let end_line = integer_field(input, "end_line");

        let file = match fs::File::open(&path) {
            Ok(file) => file,
            Err(error) if error.kind() == std::io::ErrorKind::NotFound => {
                return ToolResult::error(format!("File not found: {}", path.display()));
            }
            Err(error) if error.kind() == std::io::ErrorKind::PermissionDenied => {
                return ToolResult::error(format!("Permission denied: {}", path.display()));
            }
            Err(error) => {
                return ToolResult::error(format!("Error reading file: {}", error));
            }
        };

        let reader = BufReader::new(file);
        let start = start_line.unwrap_or(1).max(1) as usize;
        let (selected, total_lines, truncated) = match read_limited_lines(reader, start, end_line) {
            Ok(value) => value,
            Err(error) if error.kind() == std::io::ErrorKind::InvalidData => {
                return ToolResult::error(format!("Cannot read binary file: {}", path.display()));
            }
            Err(error) => {
                return ToolResult::error(format!("Error reading file: {}", error));
            }
        };
        let suffix = if truncated { ", truncated" } else { "" };

        if start_line.is_some() || end_line.is_some() {
            let end = end_line
                .unwrap_or(total_lines as i64)
                .min(total_lines as i64)
                .max(1);
            let content = numbered_lines(&selected);
            return ToolResult::success(format!(
                "File: {} (lines {}-{} of {}{})\n\n{}",
                path.display(),
                start,
                end,
                total_lines,
                suffix,
                content
            ));
        }

        if total_lines == 0 {
            return ToolResult::success(format!(
                "File: {} (0 lines)\n\n(empty file)",
                path.display()
            ));
        }

        ToolResult::success(format!(
            "File: {} ({} lines{})\n\n{}",
            path.display(),
            total_lines,
            suffix,
            numbered_lines(&selected)
        ))
    }

    fn is_read_only(&self, _input: &JsonValue) -> bool {
        true
    }

    fn supports_blanket_allow(&self) -> bool {
        false
    }

    fn user_facing_name(&self, _input: &JsonValue) -> String {
        "Read".into()
    }

    fn check_permissions(
        &self,
        input: &JsonValue,
        context: &ToolPermissionContext,
    ) -> PermissionResult {
        let Some(path) = path_field(input) else {
            return ask_with_reason("path_constraint", "Read file path is required.");
        };
        if path.is_empty() {
            return ask_with_reason("path_constraint", "Read file path is required.");
        }

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

type NumberedLine = (usize, String);
type LimitedLinesRead = (Vec<NumberedLine>, usize, bool);

fn numbered_lines(lines: &[NumberedLine]) -> String {
    lines
        .iter()
        .map(|(line_number, line)| format!("{:>6}\t{}", line_number, line))
        .collect()
}

fn read_limited_lines<R: BufRead>(
    mut reader: R,
    start_line: usize,
    end_line: Option<i64>,
) -> std::io::Result<LimitedLinesRead> {
    let mut selected = Vec::new();
    let mut total_lines = 0usize;
    let mut bytes_read = 0usize;
    let mut truncated = false;

    loop {
        if total_lines >= MAX_READ_LINES || bytes_read >= MAX_READ_BYTES {
            truncated = reader.fill_buf().map(|buffer| !buffer.is_empty())?;
            break;
        }

        let remaining_bytes = MAX_READ_BYTES - bytes_read;
        let mut line = String::new();
        let bytes = reader.read_line(&mut line)?;
        if bytes == 0 {
            break;
        }

        total_lines += 1;
        if bytes > remaining_bytes {
            truncate_string_to_bytes(&mut line, remaining_bytes);
            bytes_read = MAX_READ_BYTES;
            truncated = true;
        } else {
            bytes_read += bytes;
        }

        let in_range = total_lines >= start_line
            && end_line
                .map(|end| total_lines <= end.max(0) as usize)
                .unwrap_or(true);
        if in_range {
            selected.push((total_lines, line));
        }

        if truncated {
            break;
        }
    }

    Ok((selected, total_lines, truncated))
}

fn truncate_string_to_bytes(value: &mut String, max_bytes: usize) {
    if value.len() <= max_bytes {
        return;
    }
    let mut end = max_bytes;
    while !value.is_char_boundary(end) {
        end -= 1;
    }
    value.truncate(end);
}
