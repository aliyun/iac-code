use std::fs;
use std::path::{Path, PathBuf};

use iac_code_protocol::json::JsonValue;
use iac_code_protocol::permission::{PermissionResult, ToolPermissionContext};
use regex::{Regex, RegexBuilder};

use super::common::{
    bool_field, boolean_property, integer_field, integer_property, resolve_path,
    string_enum_property, string_field, string_property, tool_schema,
};
use super::glob_match::matches_path_glob;
use crate::path_safety::{check_read_path, PathDecision};
use crate::{Tool, ToolContext, ToolResult};

#[derive(Clone, Copy, Debug, Default, PartialEq, Eq)]
pub struct GrepTool;

impl GrepTool {
    pub fn new() -> Self {
        Self
    }
}

impl Tool for GrepTool {
    fn name(&self) -> &str {
        "grep"
    }

    fn description(&self) -> &str {
        "Search for a regex pattern across file contents. Returns matching file paths or matching lines depending on output_mode."
    }

    fn input_schema(&self) -> JsonValue {
        tool_schema(
            &["pattern"],
            vec![
                (
                    "pattern",
                    string_property("The regular expression pattern to search for."),
                ),
                (
                    "path",
                    string_property("The directory to search in. Defaults to current working directory."),
                ),
                (
                    "glob",
                    string_property(
                        "Glob pattern to filter files, e.g. '*.py'. Only files whose names match this pattern will be searched.",
                    ),
                ),
                (
                    "case_insensitive",
                    boolean_property("Perform a case-insensitive search. Defaults to false."),
                ),
                (
                    "output_mode",
                    string_enum_property(
                        &["files_with_matches", "content"],
                        "Controls output format. 'files_with_matches' (default) returns only the paths of matching files. 'content' returns each matching line with its file path and line number.",
                    ),
                ),
                (
                    "max_results",
                    integer_property("Maximum number of results to return. Defaults to 100."),
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
            return ToolResult::error("Invalid input for tool 'grep': missing required field 'pattern'. Please provide all required parameters as defined in the tool schema.");
        };
        let requested_path = string_field(input, "path").unwrap_or(&context.cwd);
        let path = resolve_path(requested_path, &context.cwd);
        let glob_filter = string_field(input, "glob");
        let case_insensitive = bool_field(input, "case_insensitive").unwrap_or(false);
        let output_mode = string_field(input, "output_mode").unwrap_or("files_with_matches");
        let max_results = integer_field(input, "max_results").unwrap_or(100).max(0) as usize;

        if !path.exists() {
            return ToolResult::error(format!("Path not found: {}", path.display()));
        }

        let mut output = python_like_grep(
            pattern,
            &path,
            glob_filter,
            case_insensitive,
            output_mode,
            max_results,
        );
        output = output.trim().to_owned();

        if output.is_empty() {
            return ToolResult::success("No matches");
        }

        let lines = output.lines().collect::<Vec<&str>>();
        if lines.len() > max_results {
            output = lines[..max_results].join("\n");
        }

        ToolResult::success(output)
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

fn python_like_grep(
    pattern: &str,
    path: &Path,
    glob_filter: Option<&str>,
    case_insensitive: bool,
    output_mode: &str,
    max_results: usize,
) -> String {
    if let Err(error) = validate_grep_pattern(pattern) {
        return format!("Invalid pattern: {}", error);
    }

    let matcher = match build_grep_regex(pattern, case_insensitive) {
        Ok(matcher) => matcher,
        Err(error) => {
            return format!("Invalid pattern: {}", error);
        }
    };

    let mut files = Vec::new();
    if collect_grep_files(path, &mut files).is_err() {
        return String::new();
    }

    let search_root = fs::canonicalize(path).unwrap_or_else(|_| path.to_path_buf());
    let mut results = Vec::new();
    let mut files_matched = 0usize;

    for file in files {
        if output_mode != "content" && files_matched >= max_results {
            break;
        }
        if !path_is_under_real_root(&file, &search_root) {
            continue;
        }

        let relative_path = file
            .strip_prefix(path)
            .unwrap_or(&file)
            .to_string_lossy()
            .replace('\\', "/");
        if glob_filter.is_some_and(|glob| !matches_path_glob(&relative_path, glob)) {
            continue;
        }

        let Ok(bytes) = fs::read(&file) else {
            continue;
        };
        let content = String::from_utf8_lossy(&bytes);

        for (line_index, line) in content.lines().enumerate() {
            if matcher.is_match(line) {
                if output_mode == "content" {
                    results.push(format!(
                        "{}:{}:{}",
                        file.display(),
                        line_index + 1,
                        line.trim_end()
                    ));
                } else {
                    results.push(file.to_string_lossy().into_owned());
                    files_matched += 1;
                    break;
                }
            }
        }
    }

    results.join("\n")
}

fn build_grep_regex(pattern: &str, case_insensitive: bool) -> Result<Regex, String> {
    RegexBuilder::new(pattern)
        .case_insensitive(case_insensitive)
        .build()
        .map_err(|error| error.to_string())
}

fn validate_grep_pattern(pattern: &str) -> Result<(), &'static str> {
    let mut escaped = false;
    let mut in_class = false;
    for character in pattern.chars() {
        if escaped {
            escaped = false;
            continue;
        }
        match character {
            '\\' => escaped = true,
            '[' if !in_class => in_class = true,
            ']' if in_class => in_class = false,
            _ => {}
        }
    }
    if in_class {
        Err("unterminated character set")
    } else if escaped {
        Err("bad escape at end of pattern")
    } else {
        Ok(())
    }
}

fn collect_grep_files(root: &Path, files: &mut Vec<PathBuf>) -> Result<(), String> {
    if root.is_file() {
        files.push(root.to_path_buf());
        return Ok(());
    }
    if !root.is_dir() {
        return Ok(());
    }

    let mut entries = fs::read_dir(root)
        .map_err(|error| error.to_string())?
        .filter_map(Result::ok)
        .collect::<Vec<fs::DirEntry>>();
    entries.sort_by_key(|entry| entry.file_name());

    for entry in entries {
        let path = entry.path();
        let Ok(file_type) = entry.file_type() else {
            continue;
        };
        if file_type.is_dir() {
            collect_grep_files(&path, files)?;
        } else if path.is_file() {
            files.push(path);
        }
    }
    Ok(())
}

fn path_is_under_real_root(path: &Path, root: &Path) -> bool {
    let Ok(path) = fs::canonicalize(path) else {
        return false;
    };
    let path = normalize_real_path(&path);
    let root = normalize_real_path(root);
    if path == root {
        return true;
    }
    path.starts_with(&format!("{}/", root.trim_end_matches('/')))
}

fn normalize_real_path(path: &Path) -> String {
    let normalized = path.to_string_lossy().replace('\\', "/");
    if cfg!(any(windows, target_os = "macos")) {
        normalized.to_lowercase()
    } else {
        normalized
    }
}
