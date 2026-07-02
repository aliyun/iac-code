use std::fs;
use std::path::{Path, PathBuf};
use std::time::SystemTime;

use iac_code_protocol::json::JsonValue;
use iac_code_protocol::permission::{PermissionResult, ToolPermissionContext};

use super::common::{ask_with_reason, resolve_path, string_field, string_property, tool_schema};
use super::glob_match::glob_segment_matches;
use crate::path_safety::{check_read_path, PathDecision};
use crate::{Tool, ToolContext, ToolResult};

#[derive(Clone, Copy, Debug, Default, PartialEq, Eq)]
pub struct GlobTool;

impl GlobTool {
    pub fn new() -> Self {
        Self
    }
}

impl Tool for GlobTool {
    fn name(&self) -> &str {
        "glob"
    }

    fn description(&self) -> &str {
        "Fast file pattern matching using glob patterns. Searches for files matching the given pattern and returns matching file paths sorted by modification time."
    }

    fn input_schema(&self) -> JsonValue {
        tool_schema(
            &["pattern"],
            vec![
                (
                    "pattern",
                    string_property(
                        "The glob pattern to match files against, e.g. '**/*.py' or 'src/**/*.ts'.",
                    ),
                ),
                (
                    "path",
                    string_property(
                        "The directory to search in. Defaults to current working directory.",
                    ),
                ),
            ],
        )
    }

    fn validate_input(&self, input: &JsonValue) -> Result<(), String> {
        if string_field(input, "pattern").is_none() {
            return Err("missing required field 'pattern'".into());
        }
        Ok(())
    }

    fn execute(&self, input: &JsonValue, context: &ToolContext) -> ToolResult {
        let Some(pattern) = string_field(input, "pattern") else {
            return ToolResult::error("Invalid input for tool 'glob': missing required field 'pattern'. Please provide all required parameters as defined in the tool schema.");
        };
        let requested_path = string_field(input, "path").unwrap_or(&context.cwd);
        let search_root = resolve_path(requested_path, &context.cwd);

        if !search_root.exists() {
            return ToolResult::error(format!("Path not found: {}", requested_path));
        }
        if !search_root.is_dir() {
            return ToolResult::error(format!("Not a directory: {}", requested_path));
        }

        let mut matches = match collect_glob_matches(&search_root, pattern) {
            Ok(matches) => matches,
            Err(error) => {
                return ToolResult::error(format!("Error during glob: {}", error));
            }
        };

        if matches.is_empty() {
            return ToolResult::success("No files found");
        }

        matches.sort_by(|left, right| {
            modified_time(right)
                .cmp(&modified_time(left))
                .then_with(|| left.cmp(right))
        });

        let canonical_root = fs::canonicalize(&search_root).unwrap_or_else(|_| search_root.clone());
        let relative_paths = matches
            .iter()
            .map(|path| relative_display_path(path, &search_root, &canonical_root))
            .collect::<Vec<String>>();
        ToolResult::success(relative_paths.join("\n"))
    }

    fn is_read_only(&self, _input: &JsonValue) -> bool {
        true
    }

    fn user_facing_name(&self, _input: &JsonValue) -> String {
        "Search".into()
    }

    fn check_permissions(
        &self,
        input: &JsonValue,
        context: &ToolPermissionContext,
    ) -> PermissionResult {
        let path = string_field(input, "path").unwrap_or(&context.cwd);
        let decision = check_read_path(
            path,
            &context.cwd,
            &context.additional_directories,
            &context.trusted_read_directories,
        );
        if decision != PathDecision::Allow {
            return decision.to_permission_result();
        }

        let pattern = string_field(input, "pattern").unwrap_or("");
        if glob_pattern_may_escape_root(pattern) {
            return ask_with_reason(
                "path_constraint",
                "glob pattern outside allowed directories",
            );
        }

        let search_root = resolve_path(path, &context.cwd);
        let matches = match collect_glob_matches(&search_root, pattern) {
            Ok(matches) => matches,
            Err(_) => {
                return ask_with_reason(
                    "path_constraint",
                    "glob pattern outside allowed directories",
                );
            }
        };
        for matched_path in matches {
            let decision = check_read_path(
                &matched_path.to_string_lossy(),
                &context.cwd,
                &context.additional_directories,
                &context.trusted_read_directories,
            );
            if decision != PathDecision::Allow {
                return decision.to_permission_result();
            }
        }

        PermissionResult::allow()
    }
}

fn glob_pattern_may_escape_root(pattern: &str) -> bool {
    let normalized = pattern.replace('\\', "/");
    Path::new(&normalized).is_absolute() || normalized.split('/').any(|part| part == "..")
}

fn collect_glob_matches(search_root: &Path, pattern: &str) -> Result<Vec<PathBuf>, String> {
    let mut matches = Vec::new();
    let normalized = pattern.replace('\\', "/");
    if Path::new(&normalized).is_absolute() {
        return Err("Non-relative patterns are unsupported".into());
    }
    let segments = normalized
        .split('/')
        .filter(|segment| !segment.is_empty())
        .collect::<Vec<&str>>();
    collect_glob_segments(search_root, &segments, &mut matches)?;
    Ok(matches)
}

fn collect_glob_segments(
    root: &Path,
    segments: &[&str],
    matches: &mut Vec<PathBuf>,
) -> Result<(), String> {
    if segments.is_empty() {
        if root.is_file() {
            matches.push(root.to_path_buf());
        }
        return Ok(());
    }

    if segments[0] == "**" {
        collect_glob_segments(root, &segments[1..], matches)?;
        let entries = fs::read_dir(root).map_err(|error| error.to_string())?;
        for entry in entries {
            let entry = entry.map_err(|error| error.to_string())?;
            let file_type = entry.file_type().map_err(|error| error.to_string())?;
            if file_type.is_dir() {
                collect_glob_segments(&entry.path(), segments, matches)?;
            }
        }
        return Ok(());
    }

    let entries = fs::read_dir(root).map_err(|error| error.to_string())?;
    for entry in entries {
        let entry = entry.map_err(|error| error.to_string())?;
        let name = entry.file_name().to_string_lossy().into_owned();
        if !glob_segment_matches(&name, segments[0]) {
            continue;
        }

        let path = entry.path();
        if segments.len() == 1 {
            if path.is_file() {
                matches.push(path);
            }
        } else if path.is_dir() {
            collect_glob_segments(&path, &segments[1..], matches)?;
        }
    }
    Ok(())
}

fn modified_time(path: &Path) -> SystemTime {
    path.metadata()
        .and_then(|metadata| metadata.modified())
        .unwrap_or(SystemTime::UNIX_EPOCH)
}

fn relative_display_path(path: &Path, search_root: &Path, canonical_root: &Path) -> String {
    path.strip_prefix(search_root)
        .or_else(|_| path.strip_prefix(canonical_root))
        .unwrap_or(path)
        .to_string_lossy()
        .replace('\\', "/")
}
