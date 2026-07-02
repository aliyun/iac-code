use std::fs;
use std::path::{Path, PathBuf};
use std::sync::atomic::{AtomicU64, Ordering};

use iac_code_protocol::json::{self, JsonValue};
use iac_code_protocol::permission::{
    PermissionDecisionReason, PermissionMode, PermissionResult, ToolPermissionContext,
};
use iac_code_tools::{
    check_tool_permission, register_file_tools, EditFileTool, GlobTool, GrepTool, ListFilesTool,
    ReadFileTool, RegistryToolExecutor, Tool, ToolCallRequest, ToolContext, ToolRegistry,
    ToolResult, WriteFileTool,
};

#[test]
fn read_file_reads_relative_path_with_python_style_line_numbers() {
    let workspace = TestWorkspace::new("read-line-range");
    workspace.write_file("main.tf", "first\nsecond\nthird\n");

    let tool = ReadFileTool::new();
    let result = tool.execute(
        &json::object([
            ("path", json::string("main.tf")),
            ("start_line", json::number(2)),
            ("end_line", json::number(3)),
        ]),
        &ToolContext {
            cwd: workspace.path_string(),
        },
    );

    assert_eq!(
        result,
        ToolResult::success(format!(
            "File: {} (lines 2-3 of 3)\n\n     2\tsecond\n     3\tthird\n",
            workspace.path().join("main.tf").display()
        ))
    );
    assert!(tool.is_read_only(&json::object([("path", json::string("main.tf"))])));
    assert!(!tool.supports_blanket_allow());
    assert_eq!(tool.user_facing_name(&empty_object()), "Read");
}

#[test]
fn read_file_preserves_missing_final_newline_like_python() {
    let workspace = TestWorkspace::new("read-no-final-newline");
    workspace.write_file("main.tf", "resource no_newline");

    let result = ReadFileTool::new().execute(
        &json::object([("path", json::string("main.tf"))]),
        &ToolContext {
            cwd: workspace.path_string(),
        },
    );

    assert_eq!(
        result,
        ToolResult::success(format!(
            "File: {} (1 lines)\n\n     1\tresource no_newline",
            workspace.path().join("main.tf").display()
        ))
    );
}

#[test]
fn read_file_reports_missing_file_and_missing_required_path() {
    let workspace = TestWorkspace::new("read-errors");
    let tool = ReadFileTool::new();

    assert_eq!(
        tool.validate_input(&empty_object()),
        Err("missing required field 'path'".into())
    );
    assert_eq!(
        tool.execute(
            &json::object([("path", json::string("missing.txt"))]),
            &ToolContext {
                cwd: workspace.path_string(),
            },
        ),
        ToolResult::error(format!(
            "File not found: {}",
            workspace.path().join("missing.txt").display()
        ))
    );
}

#[test]
fn file_tools_accept_file_path_alias_like_python_normalize_input() {
    let workspace = TestWorkspace::new("file-path-alias");
    workspace.write_file("main.tf", "resource old\n");
    let context = ToolContext {
        cwd: workspace.path_string(),
    };

    let read_input = json::object([("file_path", json::string("main.tf"))]);
    let read_tool = ReadFileTool::new();
    assert_eq!(read_tool.validate_input(&read_input), Ok(()));
    let read = read_tool.execute(&read_input, &context);
    assert!(read.content.contains("resource old"), "{read:?}");

    let write_input = json::object([
        ("file_path", json::string("nested/out.tf")),
        ("content", json::string("output {}\n")),
    ]);
    let write_tool = WriteFileTool::new();
    assert_eq!(write_tool.validate_input(&write_input), Ok(()));
    let write = write_tool.execute(&write_input, &context);
    assert_eq!(
        write,
        ToolResult::success(format!(
            "Successfully wrote 1 lines to {}",
            workspace.path().join("nested/out.tf").display()
        ))
    );
    assert_eq!(
        fs::read_to_string(workspace.path().join("nested/out.tf")).expect("read alias write"),
        "output {}\n"
    );

    let edit_input = json::object([
        ("file_path", json::string("main.tf")),
        ("old_string", json::string("old")),
        ("new_string", json::string("new")),
    ]);
    let edit_tool = EditFileTool::new();
    assert_eq!(edit_tool.validate_input(&edit_input), Ok(()));
    let edit = edit_tool.execute(&edit_input, &context);
    assert_eq!(
        edit,
        ToolResult::success(format!(
            "Successfully edited {}",
            workspace.path().join("main.tf").display()
        ))
    );
    assert_eq!(
        fs::read_to_string(workspace.path().join("main.tf")).expect("read alias edit"),
        "resource new\n"
    );
}

#[test]
fn file_tool_schemas_include_python_properties_for_provider_guidance() {
    let read_schema = ReadFileTool::new().input_schema();
    assert_schema_required(&read_schema, &["path"]);
    assert_property_type(&read_schema, "path", "string");
    assert_property_description_contains(
        &read_schema,
        "path",
        "absolute or relative to working directory",
    );
    assert_property_type(&read_schema, "start_line", "integer");
    assert_property_type(&read_schema, "end_line", "integer");

    let write_schema = WriteFileTool::new().input_schema();
    assert_schema_required(&write_schema, &["path", "content"]);
    assert_property_type(&write_schema, "path", "string");
    assert_property_description_contains(&write_schema, "path", "Always emit this field FIRST");
    assert_property_type(&write_schema, "content", "string");

    let edit_schema = EditFileTool::new().input_schema();
    assert_schema_required(&edit_schema, &["path", "old_string", "new_string"]);
    assert_property_type(&edit_schema, "path", "string");
    assert_property_type(&edit_schema, "old_string", "string");
    assert_property_description_contains(
        &edit_schema,
        "old_string",
        "Must match exactly one location",
    );
    assert_property_type(&edit_schema, "new_string", "string");

    let list_schema = ListFilesTool::new().input_schema();
    assert_schema_required(&list_schema, &[]);
    assert_property_type(&list_schema, "path", "string");
    assert_property_description_contains(
        &list_schema,
        "path",
        "Defaults to current working directory",
    );

    let glob_schema = GlobTool::new().input_schema();
    assert_schema_required(&glob_schema, &["pattern"]);
    assert_property_type(&glob_schema, "pattern", "string");
    assert_property_description_contains(&glob_schema, "pattern", "**/*.py");
    assert_property_type(&glob_schema, "path", "string");

    let grep_schema = GrepTool::new().input_schema();
    assert_schema_required(&grep_schema, &["pattern"]);
    assert_property_type(&grep_schema, "pattern", "string");
    assert_property_type(&grep_schema, "path", "string");
    assert_property_type(&grep_schema, "glob", "string");
    assert_property_type(&grep_schema, "case_insensitive", "boolean");
    assert_property_type(&grep_schema, "output_mode", "string");
    assert_property_enum(
        &grep_schema,
        "output_mode",
        &["files_with_matches", "content"],
    );
    assert_property_type(&grep_schema, "max_results", "integer");
}

#[test]
fn read_file_limits_large_files_like_python() {
    let workspace = TestWorkspace::new("read-large-file");
    let mut content = String::new();
    for line_number in 1..=50_001 {
        content.push_str(&format!("line {line_number}\n"));
    }
    workspace.write_file("large.txt", &content);

    let tool = ReadFileTool::new();
    let result = tool.execute(
        &json::object([("path", json::string("large.txt"))]),
        &ToolContext {
            cwd: workspace.path_string(),
        },
    );

    assert!(result.content.starts_with(&format!(
        "File: {} (50000 lines, truncated)",
        workspace.path().join("large.txt").display()
    )));
    assert!(
        result.content.contains(" 50000\tline 50000"),
        "missing final retained line"
    );
    assert!(
        !result.content.contains("line 50001"),
        "truncated read should not include line 50001"
    );
}

#[test]
fn list_files_lists_sorted_entries_with_directory_markers_and_sizes() {
    let workspace = TestWorkspace::new("list-files");
    workspace.write_file("a.txt", "hello");
    workspace.write_file("b.bin", &"x".repeat(1536));
    fs::create_dir(workspace.path().join("nested")).expect("create nested dir");

    let tool = ListFilesTool::new();
    let result = tool.execute(
        &json::object([("path", json::string("."))]),
        &ToolContext {
            cwd: workspace.path_string(),
        },
    );

    assert_eq!(
        result,
        ToolResult::success(format!(
            "Directory: {}\n\n  a.txt (5B)\n  b.bin (1.5KB)\n  nested/",
            workspace.path().join(".").display()
        ))
    );
    assert!(tool.is_read_only(&empty_object()));
    assert_eq!(tool.user_facing_name(&empty_object()), "List");
}

#[test]
fn glob_finds_files_with_relative_paths_and_no_match_message() {
    let workspace = TestWorkspace::new("glob-files");
    workspace.write_file("a.py", "");
    workspace.write_file("b.py", "");
    workspace.write_file("c.txt", "");
    fs::create_dir_all(workspace.path().join("nested")).expect("create nested dir");
    workspace.write_file("nested/d.py", "");

    let tool = GlobTool::new();
    let result = tool.execute(
        &json::object([
            ("pattern", json::string("**/*.py")),
            ("path", json::string(".")),
        ]),
        &ToolContext {
            cwd: workspace.path_string(),
        },
    );

    assert!(!result.is_error);
    let mut lines = result.content.lines().collect::<Vec<&str>>();
    lines.sort_unstable();
    assert_eq!(lines, vec!["a.py", "b.py", "nested/d.py"]);
    assert!(tool.is_read_only(&empty_object()));
    assert_eq!(tool.user_facing_name(&empty_object()), "Search");

    assert_eq!(
        tool.execute(
            &json::object([
                ("pattern", json::string("*.nomatch")),
                ("path", json::string(".")),
            ]),
            &ToolContext {
                cwd: workspace.path_string(),
            },
        ),
        ToolResult::success("No files found")
    );
}

#[test]
fn glob_reports_path_errors_and_validates_pattern() {
    let workspace = TestWorkspace::new("glob-errors");
    workspace.write_file("file.txt", "x");
    let tool = GlobTool::new();

    assert_eq!(
        tool.validate_input(&empty_object()),
        Err("missing required field 'pattern'".into())
    );
    assert_eq!(
        tool.execute(
            &json::object([
                ("pattern", json::string("*.py")),
                ("path", json::string("missing")),
            ]),
            &ToolContext {
                cwd: workspace.path_string(),
            },
        ),
        ToolResult::error("Path not found: missing")
    );
    assert_eq!(
        tool.execute(
            &json::object([
                ("pattern", json::string("*.py")),
                ("path", json::string("file.txt")),
            ]),
            &ToolContext {
                cwd: workspace.path_string(),
            },
        ),
        ToolResult::error("Not a directory: file.txt")
    );
}

#[test]
fn grep_finds_matching_files_content_lines_and_no_match_message() {
    let workspace = TestWorkspace::new("grep-basic");
    workspace.write_file("a.txt", "hello world\n");
    workspace.write_file("b.txt", "nothing\n");
    workspace.write_file("nested/c.txt", "hello nested\n");
    let tool = GrepTool::new();

    let result = tool.execute(
        &json::object([
            ("pattern", json::string("hello")),
            ("path", path_string(workspace.path())),
        ]),
        &ToolContext {
            cwd: workspace.path_string(),
        },
    );

    assert!(!result.is_error);
    let mut lines = result
        .content
        .lines()
        .map(str::to_owned)
        .collect::<Vec<String>>();
    lines.sort_unstable();
    assert_eq!(
        lines,
        vec![
            workspace
                .path()
                .join("a.txt")
                .to_string_lossy()
                .into_owned(),
            workspace
                .path()
                .join("nested/c.txt")
                .to_string_lossy()
                .into_owned(),
        ]
    );
    assert!(tool.is_read_only(&empty_object()));
    assert_eq!(tool.user_facing_name(&empty_object()), "Search");

    let content = tool.execute(
        &json::object([
            ("pattern", json::string("hello")),
            ("path", path_string(workspace.path())),
            ("output_mode", json::string("content")),
        ]),
        &ToolContext {
            cwd: workspace.path_string(),
        },
    );
    assert!(content.content.contains(&format!(
        "{}:1:hello world",
        workspace.path().join("a.txt").display()
    )));
    assert!(content.content.contains(":1:hello nested"));

    assert_eq!(
        tool.execute(
            &json::object([
                ("pattern", json::string("missing")),
                ("path", path_string(workspace.path())),
            ]),
            &ToolContext {
                cwd: workspace.path_string(),
            },
        ),
        ToolResult::success("No matches")
    );
}

#[test]
fn grep_treats_pattern_as_regex_like_python() {
    let workspace = TestWorkspace::new("grep-regex");
    workspace.write_file("a.txt", "hello world\n");
    workspace.write_file("b.txt", "hallo world\n");
    workspace.write_file("c.txt", "h.llo literal\n");
    let tool = GrepTool::new();
    let context = ToolContext {
        cwd: workspace.path_string(),
    };

    let result = tool.execute(
        &json::object([
            ("pattern", json::string("h.llo")),
            ("path", path_string(workspace.path())),
        ]),
        &context,
    );

    assert!(!result.is_error, "{result:?}");
    let mut lines = result.content.lines().collect::<Vec<&str>>();
    lines.sort_unstable();
    assert_eq!(
        lines,
        vec![
            workspace.path().join("a.txt").to_str().expect("utf-8 path"),
            workspace.path().join("b.txt").to_str().expect("utf-8 path"),
            workspace.path().join("c.txt").to_str().expect("utf-8 path"),
        ]
    );
}

#[test]
fn grep_supports_case_insensitive_glob_filter_max_results_and_pattern_errors() {
    let workspace = TestWorkspace::new("grep-options");
    workspace.write_file("src/app.py", "HIT\n");
    workspace.write_file("src/pkg/nested.py", "hit\n");
    workspace.write_file("app.py", "hit\n");
    workspace.write_file("notes.txt", "hit\n");
    let tool = GrepTool::new();
    let context = ToolContext {
        cwd: workspace.path_string(),
    };

    let filtered = tool.execute(
        &json::object([
            ("pattern", json::string("hit")),
            ("path", path_string(workspace.path())),
            ("glob", json::string("src/**/*.py")),
            ("case_insensitive", json::bool_value(true)),
        ]),
        &context,
    );
    assert!(!filtered.is_error);
    assert!(filtered.content.contains("src/app.py"));
    assert!(filtered.content.contains("src/pkg/nested.py"));
    assert!(!filtered.content.contains("notes.txt"));
    assert!(!filtered
        .content
        .contains(&format!("{}", workspace.path().join("app.py").display())));

    let limited = tool.execute(
        &json::object([
            ("pattern", json::string("hit")),
            ("path", path_string(workspace.path())),
            ("max_results", json::number(2)),
        ]),
        &context,
    );
    assert_eq!(limited.content.lines().count(), 2);

    assert_eq!(
        tool.execute(
            &json::object([
                ("pattern", json::string("[unclosed")),
                ("path", path_string(workspace.path())),
            ]),
            &context,
        ),
        ToolResult::success("Invalid pattern: unterminated character set")
    );
    assert_eq!(
        tool.validate_input(&empty_object()),
        Err("missing required field 'pattern'".into())
    );
}

#[test]
fn grep_reports_missing_path_and_checks_read_permissions() {
    let workspace = TestWorkspace::new("grep-permissions");
    let project = workspace.path().join("project");
    let outside = workspace.path().join("outside");
    fs::create_dir_all(&project).expect("create project");
    fs::create_dir_all(&outside).expect("create outside");
    fs::write(outside.join("secret.txt"), "secret").expect("write outside file");

    let tool = GrepTool::new();

    assert_eq!(
        tool.execute(
            &json::object([
                ("pattern", json::string("x")),
                ("path", json::string("missing")),
            ]),
            &ToolContext {
                cwd: project.to_string_lossy().into_owned(),
            },
        ),
        ToolResult::error(format!(
            "Path not found: {}",
            project.join("missing").display()
        ))
    );
    assert_permission_ask(
        tool.check_permissions(
            &json::object([
                ("pattern", json::string("secret")),
                ("path", path_string(&outside)),
            ]),
            &permission_context(&project),
        ),
        "path_constraint",
        "path outside allowed directories",
    );
}

#[test]
fn write_file_creates_parent_directories_and_reports_python_style_line_count() {
    let workspace = TestWorkspace::new("write-file");
    let tool = WriteFileTool::new();

    let result = tool.execute(
        &json::object([
            ("path", json::string("nested/main.tf")),
            ("content", json::string("resource {}\noutput {}\n")),
        ]),
        &ToolContext {
            cwd: workspace.path_string(),
        },
    );

    let path = workspace.path().join("nested/main.tf");
    assert_eq!(
        result,
        ToolResult::success(format!("Successfully wrote 2 lines to {}", path.display()))
    );
    assert_eq!(
        fs::read_to_string(&path).expect("read written file"),
        "resource {}\noutput {}\n"
    );
    assert!(!tool.is_read_only(&json::object([("path", json::string("nested/main.tf"))])));
    assert_eq!(tool.user_facing_name(&empty_object()), "Write");

    let overwrite = tool.execute(
        &json::object([
            ("path", json::string("nested/main.tf")),
            ("content", json::string("single line")),
        ]),
        &ToolContext {
            cwd: workspace.path_string(),
        },
    );
    assert_eq!(
        overwrite,
        ToolResult::success(format!("Successfully wrote 1 lines to {}", path.display()))
    );
    assert_eq!(
        fs::read_to_string(&path).expect("read overwritten file"),
        "single line"
    );
}

#[test]
fn write_file_validates_required_path_and_content() {
    let tool = WriteFileTool::new();

    assert_eq!(
        tool.validate_input(&empty_object()),
        Err("missing required field 'path'".into())
    );
    assert_eq!(
        tool.validate_input(&json::object([("path", json::string("main.tf"))])),
        Err("missing required field 'content'".into())
    );
}

#[test]
fn edit_file_replaces_exactly_one_match_and_reports_success() {
    let workspace = TestWorkspace::new("edit-file");
    workspace.write_file("main.tf", "resource old\noutput old\n");
    let tool = EditFileTool::new();

    let result = tool.execute(
        &json::object([
            ("path", json::string("main.tf")),
            ("old_string", json::string("resource old")),
            ("new_string", json::string("resource new")),
        ]),
        &ToolContext {
            cwd: workspace.path_string(),
        },
    );

    let path = workspace.path().join("main.tf");
    assert_eq!(
        result,
        ToolResult::success(format!("Successfully edited {}", path.display()))
    );
    assert_eq!(
        fs::read_to_string(&path).expect("read edited file"),
        "resource new\noutput old\n"
    );
    assert!(!tool.is_read_only(&empty_object()));
    assert_eq!(tool.user_facing_name(&empty_object()), "Edit");
    assert_eq!(
        tool.user_facing_name(&json::object([("old_string", json::string(""))])),
        "Create"
    );
    assert_eq!(
        tool.user_facing_name(&json::object([("old_string", json::string("old"))])),
        "Update"
    );
}

#[test]
fn edit_file_reports_not_found_and_multiple_match_errors_without_writing() {
    let workspace = TestWorkspace::new("edit-errors");
    workspace.write_file("main.tf", "hello hello hello");
    let tool = EditFileTool::new();
    let context = ToolContext {
        cwd: workspace.path_string(),
    };

    let missing = tool.execute(
        &json::object([
            ("path", json::string("main.tf")),
            ("old_string", json::string("not here")),
            ("new_string", json::string("replacement")),
        ]),
        &context,
    );
    assert_eq!(
        missing,
        ToolResult::error(format!(
            "old_string not found in {}. Make sure the string matches exactly, including whitespace and indentation.",
            workspace.path().join("main.tf").display()
        ))
    );

    let multiple = tool.execute(
        &json::object([
            ("path", json::string("main.tf")),
            ("old_string", json::string("hello")),
            ("new_string", json::string("world")),
        ]),
        &context,
    );
    assert_eq!(
        multiple,
        ToolResult::error(format!(
            "old_string found 3 times in {}. It must match exactly once. Include more surrounding context to make the match unique.",
            workspace.path().join("main.tf").display()
        ))
    );
    assert_eq!(
        fs::read_to_string(workspace.path().join("main.tf")).expect("read unchanged file"),
        "hello hello hello"
    );
}

#[test]
fn edit_file_validates_required_fields_and_missing_file() {
    let workspace = TestWorkspace::new("edit-validate");
    let tool = EditFileTool::new();

    assert_eq!(
        tool.validate_input(&empty_object()),
        Err("missing required field 'path'".into())
    );
    assert_eq!(
        tool.validate_input(&json::object([("path", json::string("main.tf"))])),
        Err("missing required field 'old_string'".into())
    );
    assert_eq!(
        tool.validate_input(&json::object([
            ("path", json::string("main.tf")),
            ("old_string", json::string("old")),
        ])),
        Err("missing required field 'new_string'".into())
    );
    assert_eq!(
        tool.execute(
            &json::object([
                ("path", json::string("missing.txt")),
                ("old_string", json::string("old")),
                ("new_string", json::string("new")),
            ]),
            &ToolContext {
                cwd: workspace.path_string(),
            },
        ),
        ToolResult::error(format!(
            "File not found: {}",
            workspace.path().join("missing.txt").display()
        ))
    );
}

#[test]
fn read_file_permissions_apply_path_safety() {
    let workspace = TestWorkspace::new("read-permissions");
    let project = workspace.path().join("project");
    let outside = workspace.path().join("outside");
    let trusted = workspace.path().join(".iac-code/tool-results/session-1");
    fs::create_dir_all(&project).expect("create project");
    fs::create_dir_all(&outside).expect("create outside");
    fs::create_dir_all(&trusted).expect("create trusted");
    fs::write(project.join("main.tf"), "resource x").expect("write project file");
    fs::write(project.join(".env"), "TOKEN=fake").expect("write sensitive file");
    fs::write(outside.join("secret.txt"), "secret").expect("write outside file");
    fs::write(trusted.join(".env"), "TOKEN=fake").expect("write trusted file");

    let tool = ReadFileTool::new();
    let context = permission_context(&project);

    assert_eq!(
        tool.check_permissions(
            &json::object([("path", path_string(project.join("main.tf")))]),
            &context,
        ),
        PermissionResult::allow()
    );
    assert_permission_ask(
        tool.check_permissions(
            &json::object([("path", path_string(outside.join("secret.txt")))]),
            &context,
        ),
        "path_constraint",
        "path outside allowed directories",
    );
    assert_permission_ask(
        tool.check_permissions(
            &json::object([("path", path_string(project.join(".env")))]),
            &context,
        ),
        "safety_check",
        "read touches a sensitive path",
    );

    let trusted_context = ToolPermissionContext {
        trusted_read_directories: vec![trusted.to_string_lossy().into_owned()],
        ..permission_context(&project)
    };
    assert_eq!(
        tool.check_permissions(
            &json::object([("path", path_string(trusted.join(".env")))]),
            &trusted_context,
        ),
        PermissionResult::allow()
    );
    assert_permission_ask(
        tool.check_permissions(&json::object([("path", json::string(""))]), &context),
        "path_constraint",
        "Read file path is required.",
    );
}

#[test]
fn list_files_permissions_apply_path_safety() {
    let workspace = TestWorkspace::new("list-permissions");
    let project = workspace.path().join("project");
    let outside = workspace.path().join("outside");
    fs::create_dir_all(&project).expect("create project");
    fs::create_dir_all(&outside).expect("create outside");

    let tool = ListFilesTool::new();
    let context = permission_context(&project);

    assert_eq!(
        tool.check_permissions(&empty_object(), &context),
        PermissionResult::allow()
    );
    assert_permission_ask(
        tool.check_permissions(&json::object([("path", path_string(&outside))]), &context),
        "path_constraint",
        "path outside allowed directories",
    );
}

#[test]
fn glob_permissions_apply_path_safety_to_root_pattern_and_matches() {
    let workspace = TestWorkspace::new("glob-permissions");
    let project = workspace.path().join("project");
    let outside = workspace.path().join("outside");
    fs::create_dir_all(&project).expect("create project");
    fs::create_dir_all(&outside).expect("create outside");
    fs::write(outside.join("secret.txt"), "secret").expect("write outside file");
    std::os::unix::fs::symlink(&outside, project.join("link-outside")).expect("create symlink");

    let tool = GlobTool::new();
    let context = permission_context(&project);

    assert_eq!(
        tool.check_permissions(&json::object([("pattern", json::string("**/*"))]), &context,),
        PermissionResult::allow()
    );
    assert_permission_ask(
        tool.check_permissions(
            &json::object([
                ("pattern", json::string("**/*")),
                ("path", path_string(&outside)),
            ]),
            &context,
        ),
        "path_constraint",
        "path outside allowed directories",
    );
    assert_permission_ask(
        tool.check_permissions(
            &json::object([
                ("pattern", json::string("../outside/*")),
                ("path", path_string(&project)),
            ]),
            &context,
        ),
        "path_constraint",
        "glob pattern outside allowed directories",
    );
    assert_permission_ask(
        tool.check_permissions(
            &json::object([
                ("pattern", json::string("link-outside/*")),
                ("path", path_string(&project)),
            ]),
            &context,
        ),
        "path_constraint",
        "path outside allowed directories",
    );

    let additional_context = ToolPermissionContext {
        additional_directories: vec![outside.to_string_lossy().into_owned()],
        ..permission_context(&project)
    };
    assert_eq!(
        tool.check_permissions(
            &json::object([
                ("pattern", json::string("link-outside/*")),
                ("path", path_string(&project)),
            ]),
            &additional_context,
        ),
        PermissionResult::allow()
    );
}

#[test]
fn write_file_permissions_apply_path_safety_before_default_prompt() {
    let workspace = TestWorkspace::new("write-permissions");
    let project = workspace.path().join("project");
    let outside = workspace.path().join("outside");
    fs::create_dir_all(&project).expect("create project");
    fs::create_dir_all(&outside).expect("create outside");

    let tool = WriteFileTool::new();
    let context = permission_context(&project);

    assert_eq!(
        tool.check_permissions(
            &json::object([
                ("path", path_string(project.join("main.tf"))),
                ("content", json::string("resource x")),
            ]),
            &context,
        ),
        PermissionResult::ask("Allow Write?")
    );
    assert_permission_ask(
        tool.check_permissions(
            &json::object([
                ("path", path_string(outside.join("secret.txt"))),
                ("content", json::string("secret")),
            ]),
            &context,
        ),
        "path_constraint",
        "path outside allowed directories",
    );
    assert_permission_ask(
        tool.check_permissions(
            &json::object([
                ("path", path_string(project.join(".env"))),
                ("content", json::string("TOKEN=fake")),
            ]),
            &context,
        ),
        "safety_check",
        "write touches a sensitive path",
    );
    assert_permission_ask(
        tool.check_permissions(&json::object([("content", json::string("x"))]), &context),
        "path_constraint",
        "write file path is required",
    );
}

#[test]
fn edit_file_permissions_apply_path_safety_before_default_prompt() {
    let workspace = TestWorkspace::new("edit-permissions");
    let project = workspace.path().join("project");
    let outside = workspace.path().join("outside");
    fs::create_dir_all(&project).expect("create project");
    fs::create_dir_all(&outside).expect("create outside");

    let tool = EditFileTool::new();
    let context = permission_context(&project);

    assert_eq!(
        tool.check_permissions(
            &json::object([
                ("path", path_string(project.join("main.tf"))),
                ("old_string", json::string("old")),
                ("new_string", json::string("new")),
            ]),
            &context,
        ),
        PermissionResult::ask("Allow Update?")
    );
    assert_permission_ask(
        tool.check_permissions(
            &json::object([
                ("path", path_string(outside.join("secret.txt"))),
                ("old_string", json::string("old")),
                ("new_string", json::string("new")),
            ]),
            &context,
        ),
        "path_constraint",
        "path outside allowed directories",
    );
    assert_permission_ask(
        tool.check_permissions(
            &json::object([
                ("path", path_string(project.join(".env"))),
                ("old_string", json::string("old")),
                ("new_string", json::string("new")),
            ]),
            &context,
        ),
        "safety_check",
        "write touches a sensitive path",
    );
    assert_permission_ask(
        tool.check_permissions(
            &json::object([
                ("old_string", json::string("old")),
                ("new_string", json::string("new")),
            ]),
            &context,
        ),
        "path_constraint",
        "edit file path is required",
    );
}

#[test]
fn bare_allow_rule_does_not_override_file_path_constraints() {
    let workspace = TestWorkspace::new("allow-path-constraint");
    let project = workspace.path().join("project");
    let outside = workspace.path().join("outside");
    fs::create_dir_all(&project).expect("create project");
    fs::create_dir_all(&outside).expect("create outside");
    fs::write(outside.join("secret.txt"), "secret").expect("write outside file");

    let read_context = ToolPermissionContext {
        allow_rules: grouped_rules(&[("user_settings", "read_file")]),
        ..permission_context(&project)
    };
    let read_result = check_tool_permission(
        &ReadFileTool::new(),
        &json::object([("path", path_string(outside.join("secret.txt")))]),
        &read_context,
    );
    assert_permission_ask(
        read_result,
        "path_constraint",
        "path outside allowed directories",
    );

    let write_context = ToolPermissionContext {
        allow_rules: grouped_rules(&[("user_settings", "write_file")]),
        ..permission_context(&project)
    };
    let write_result = check_tool_permission(
        &WriteFileTool::new(),
        &json::object([
            ("path", path_string(outside.join("secret.txt"))),
            ("content", json::string("secret")),
        ]),
        &write_context,
    );
    assert_permission_ask(
        write_result,
        "path_constraint",
        "path outside allowed directories",
    );

    let edit_context = ToolPermissionContext {
        allow_rules: grouped_rules(&[("user_settings", "edit_file")]),
        ..permission_context(&project)
    };
    let edit_result = check_tool_permission(
        &EditFileTool::new(),
        &json::object([
            ("path", path_string(outside.join("secret.txt"))),
            ("old_string", json::string("before")),
            ("new_string", json::string("after")),
        ]),
        &edit_context,
    );
    assert_permission_ask(
        edit_result,
        "path_constraint",
        "path outside allowed directories",
    );
}

#[test]
fn register_file_tools_makes_read_tools_executable_through_registry() {
    let workspace = TestWorkspace::new("registry");
    workspace.write_file("main.tf", "resource {}\n");
    let mut registry = ToolRegistry::new();
    register_file_tools(&mut registry);

    assert_eq!(
        registry.list_tool_names(),
        vec![
            "read_file",
            "write_file",
            "edit_file",
            "bash",
            "list_files",
            "glob",
            "grep",
            "web_fetch"
        ]
    );

    let executor = RegistryToolExecutor::new(registry).with_context(ToolContext {
        cwd: workspace.path_string(),
    });
    let results = executor.execute_batch(&[
        ToolCallRequest {
            tool_use_id: "toolu_read".into(),
            tool_name: "read_file".into(),
            input: json::object([("path", json::string("main.tf"))]),
        },
        ToolCallRequest {
            tool_use_id: "toolu_list".into(),
            tool_name: "list_files".into(),
            input: empty_object(),
        },
    ]);

    assert_eq!(results.len(), 2);
    assert!(results[0].content.starts_with(&format!(
        "File: {} (1 lines)",
        workspace.path().join("main.tf").display()
    )));
    assert!(results[1].content.contains("  main.tf (12B)"));
}

fn empty_object() -> JsonValue {
    json::object(Vec::<(&str, JsonValue)>::new())
}

fn path_string(path: impl AsRef<Path>) -> JsonValue {
    json::string(path.as_ref().to_string_lossy().into_owned())
}

fn assert_schema_required(schema: &JsonValue, expected: &[&str]) {
    assert_eq!(
        array_strings(object_field(schema, "required").expect("schema required")),
        expected,
        "schema required mismatch: {schema:?}"
    );
}

fn assert_property_type(schema: &JsonValue, property: &str, expected_type: &str) {
    let property_schema = schema_property(schema, property);
    assert_eq!(
        object_string_field(property_schema, "type"),
        Some(expected_type),
        "{property} property type mismatch: {property_schema:?}"
    );
}

fn assert_property_description_contains(schema: &JsonValue, property: &str, expected: &str) {
    let property_schema = schema_property(schema, property);
    let description =
        object_string_field(property_schema, "description").expect("property description");
    assert!(
        description.contains(expected),
        "{property} description should contain {expected:?}, got {description:?}"
    );
}

fn assert_property_enum(schema: &JsonValue, property: &str, expected: &[&str]) {
    let property_schema = schema_property(schema, property);
    assert_eq!(
        array_strings(object_field(property_schema, "enum").expect("property enum")),
        expected,
        "{property} enum mismatch: {property_schema:?}"
    );
}

fn schema_property<'a>(schema: &'a JsonValue, property: &str) -> &'a JsonValue {
    let properties = object_field(schema, "properties").expect("schema properties");
    object_field(properties, property).unwrap_or_else(|| panic!("missing property {property}"))
}

fn object_field<'a>(value: &'a JsonValue, field: &str) -> Option<&'a JsonValue> {
    match value {
        JsonValue::Object(fields) => fields.get(field),
        _ => None,
    }
}

fn object_string_field<'a>(value: &'a JsonValue, field: &str) -> Option<&'a str> {
    match object_field(value, field) {
        Some(JsonValue::String(value)) => Some(value.as_str()),
        _ => None,
    }
}

fn array_strings(value: &JsonValue) -> Vec<&str> {
    match value {
        JsonValue::Array(items) => items
            .iter()
            .map(|item| match item {
                JsonValue::String(value) => value.as_str(),
                _ => panic!("expected string array item, got {item:?}"),
            })
            .collect(),
        _ => panic!("expected array, got {value:?}"),
    }
}

fn permission_context(cwd: &Path) -> ToolPermissionContext {
    ToolPermissionContext {
        mode: PermissionMode::Default,
        cwd: cwd.to_string_lossy().into_owned(),
        allow_rules: Default::default(),
        deny_rules: Default::default(),
        ask_rules: Default::default(),
        additional_directories: Vec::new(),
        trusted_read_directories: Vec::new(),
    }
}

fn grouped_rules(entries: &[(&str, &str)]) -> std::collections::BTreeMap<String, Vec<String>> {
    let mut grouped = std::collections::BTreeMap::new();
    for (source, rule) in entries {
        grouped
            .entry((*source).to_owned())
            .or_insert_with(Vec::new)
            .push((*rule).to_owned());
    }
    grouped
}

fn assert_permission_ask(result: PermissionResult, reason_type: &str, detail: &str) {
    assert_eq!(
        result,
        PermissionResult {
            behavior: "ask".into(),
            message: detail.into(),
            reason: Some(PermissionDecisionReason {
                type_name: reason_type.into(),
                detail: detail.into(),
            }),
            suggestions: None,
        }
    );
}

struct TestWorkspace {
    root: PathBuf,
}

impl TestWorkspace {
    fn new(name: &str) -> Self {
        static NEXT_ID: AtomicU64 = AtomicU64::new(1);
        let root = std::env::temp_dir().join(format!(
            "iac-code-rs-{}-{}",
            name,
            NEXT_ID.fetch_add(1, Ordering::Relaxed)
        ));
        if root.exists() {
            fs::remove_dir_all(&root).expect("remove stale test workspace");
        }
        fs::create_dir_all(&root).expect("create test workspace");
        Self { root }
    }

    fn path(&self) -> &Path {
        &self.root
    }

    fn path_string(&self) -> String {
        self.root.to_string_lossy().into_owned()
    }

    fn write_file(&self, relative_path: &str, content: &str) {
        let path = self.root.join(relative_path);
        if let Some(parent) = path.parent() {
            fs::create_dir_all(parent).expect("create parent dirs");
        }
        fs::write(path, content).expect("write test file");
    }
}

impl Drop for TestWorkspace {
    fn drop(&mut self) {
        let _ = fs::remove_dir_all(&self.root);
    }
}
