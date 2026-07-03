use std::collections::BTreeMap;
use std::fs;
use std::path::{Path, PathBuf};
use std::sync::atomic::{AtomicU64, Ordering};
use std::time::{Duration, Instant};

use iac_code_protocol::json::{self, JsonValue};
use iac_code_protocol::permission::{
    PermissionDecisionReason, PermissionMode, PermissionResult, PermissionRuleValue,
    ToolPermissionContext,
};
use iac_code_tools::{check_tool_permission, BashTool, Tool, ToolContext, ToolResult};

#[test]
fn bash_tool_schema_and_display_match_python_basics() {
    let tool = BashTool::new();

    assert_eq!(tool.name(), "bash");
    assert_eq!(
        tool.validate_input(&empty_object()),
        Err("missing required field 'command'".into())
    );
    assert!(!tool.is_read_only(&empty_object()));
    assert!(!tool.supports_blanket_allow());
    assert_eq!(tool.user_facing_name(&empty_object()), "Bash");
    assert_eq!(
        tool.execute(
            &json::object([("command", json::string("echo ok"))]),
            &Default::default(),
        ),
        ToolResult::success("STDOUT:\nok\n\nExit code: 0")
    );
}

#[test]
fn bash_executes_shell_commands_with_python_output_format() {
    let workspace = TestWorkspace::new("bash-execute");
    let tool = BashTool::new();

    assert_eq!(
        tool.execute(
            &json::object([("command", json::string("printf hello"))]),
            &tool_context(workspace.path()),
        ),
        ToolResult::success("STDOUT:\nhello\nExit code: 0")
    );

    assert_eq!(
        tool.execute(
            &json::object([("command", json::string("printf err >&2; exit 7"))]),
            &tool_context(workspace.path()),
        ),
        ToolResult::error("STDERR:\nerr\nExit code: 7")
    );
}

#[test]
fn bash_executes_in_context_cwd() {
    let workspace = TestWorkspace::new("bash-cwd");
    let tool = BashTool::new();

    assert_eq!(
        tool.execute(
            &json::object([("command", json::string("pwd"))]),
            &tool_context(workspace.path()),
        ),
        ToolResult::success(format!(
            "STDOUT:\n{}\n\nExit code: 0",
            fs::canonicalize(workspace.path())
                .expect("canonical cwd")
                .display()
        ))
    );
}

#[test]
fn bash_execution_timeout_returns_python_style_error() {
    let workspace = TestWorkspace::new("bash-timeout");
    let tool = BashTool::new();
    let started = Instant::now();

    assert_eq!(
        tool.execute(
            &json::object([
                ("command", json::string("sleep 2")),
                ("timeout", json::number(1)),
            ]),
            &tool_context(workspace.path()),
        ),
        ToolResult::error("Command timed out after 1 seconds: sleep 2")
    );
    assert!(started.elapsed() < Duration::from_secs(2));
}

#[test]
fn bash_permissions_allow_readonly_and_prompt_unknown_commands() {
    let workspace = TestWorkspace::new("bash-readonly");
    workspace.write_file("file.txt", "ok");
    let tool = BashTool::new();

    assert_eq!(
        check_tool_permission(
            &tool,
            &json::object([("command", json::string("ls -la"))]),
            &permission_context(workspace.path()),
        ),
        PermissionResult::allow()
    );
    assert_eq!(
        check_tool_permission(
            &tool,
            &json::object([("command", json::string("cat file.txt"))]),
            &permission_context(workspace.path()),
        ),
        PermissionResult::allow()
    );
    assert_eq!(
        check_tool_permission(
            &tool,
            &json::object([("command", json::string("echo ok > out.txt"))]),
            &permission_context(workspace.path()),
        ),
        PermissionResult {
            behavior: "ask".into(),
            message: "Allow Bash?".into(),
            reason: None,
            suggestions: Some(vec![PermissionRuleValue {
                tool_name: "bash".into(),
                rule_content: "echo:*".into(),
            }]),
        }
    );
    assert_eq!(
        check_tool_permission(
            &tool,
            &json::object([("command", json::string("curl https://example.com"))]),
            &permission_context(workspace.path()),
        ),
        PermissionResult {
            behavior: "ask".into(),
            message: "Allow Bash?".into(),
            reason: None,
            suggestions: Some(vec![PermissionRuleValue {
                tool_name: "bash".into(),
                rule_content: "curl:*".into(),
            }]),
        }
    );
}

#[test]
fn bash_pipeline_permissions_analyze_each_subcommand_like_python_tree_sitter() {
    let workspace = TestWorkspace::new("bash-pipeline");
    workspace.write_file("file.txt", "ok\n");
    workspace.write_file("victim.txt", "remove me\n");
    let tool = BashTool::new();

    assert_eq!(
        check_tool_permission(
            &tool,
            &json::object([("command", json::string("cat file.txt | grep ok"))]),
            &permission_context(workspace.path()),
        ),
        PermissionResult::allow()
    );
    assert_eq!(
        check_tool_permission(
            &tool,
            &json::object([("command", json::string("cat file.txt | rm victim.txt"))]),
            &permission_context(workspace.path()),
        ),
        PermissionResult {
            behavior: "ask".into(),
            message: "Allow Bash?".into(),
            reason: None,
            suggestions: Some(vec![PermissionRuleValue {
                tool_name: "bash".into(),
                rule_content: "rm:*".into(),
            }]),
        }
    );
}

#[test]
fn bash_permission_rules_match_full_and_prefix_commands() {
    let workspace = TestWorkspace::new("bash-rules");
    let tool = BashTool::new();

    assert_permission(
        check_tool_permission(
            &tool,
            &json::object([("command", json::string("rm -rf /"))]),
            &ToolPermissionContext {
                deny_rules: grouped_rules(&[("user_settings", "bash(rm -rf /)")]),
                ..permission_context(workspace.path())
            },
        ),
        "deny",
        "rule",
        "matched deny rule(s) on full command: bash(rm -rf /)",
    );
    assert_permission(
        check_tool_permission(
            &tool,
            &json::object([("command", json::string("git push origin main"))]),
            &ToolPermissionContext {
                allow_rules: grouped_rules(&[("user_settings", "bash(git:*)")]),
                ..permission_context(workspace.path())
            },
        ),
        "allow",
        "rule",
        "matched allow rule(s): bash(git:*)",
    );
    assert_permission_with_suggestions(
        check_tool_permission(
            &tool,
            &json::object([("command", json::string("git push origin main"))]),
            &ToolPermissionContext {
                ask_rules: grouped_rules(&[("user_settings", "bash(git push:*)")]),
                ..permission_context(workspace.path())
            },
        ),
        "ask",
        "rule",
        "matched ask rule(s): bash(git push:*)",
        &["git:*"],
    );
}

#[test]
fn bash_permissions_apply_modes_and_path_constraints_before_rules() {
    let workspace = TestWorkspace::new("bash-paths");
    let project = workspace.path().join("project");
    let outside = workspace.path().join("outside");
    fs::create_dir_all(&project).expect("create project");
    fs::create_dir_all(&outside).expect("create outside");
    fs::write(project.join("file.txt"), "ok").expect("write project file");
    fs::write(outside.join("secret.txt"), "secret").expect("write outside file");
    let tool = BashTool::new();

    assert_eq!(
        check_tool_permission(
            &tool,
            &json::object([("command", json::string("mkdir new-dir"))]),
            &ToolPermissionContext {
                mode: PermissionMode::AcceptEdits,
                ..permission_context(&project)
            },
        ),
        PermissionResult::allow()
    );
    assert_permission_with_suggestions(
        check_tool_permission(
            &tool,
            &json::object([("command", json::string("cat /etc/passwd"))]),
            &permission_context(&project),
        ),
        "ask",
        "path_constraint",
        "path outside allowed directories",
        &["cat:*"],
    );
    assert_permission_with_suggestions(
        check_tool_permission(
            &tool,
            &json::object([("command", json::string("rm /etc/passwd"))]),
            &ToolPermissionContext {
                allow_rules: grouped_rules(&[("user_settings", "bash(rm:*)")]),
                ..permission_context(&project)
            },
        ),
        "ask",
        "path_constraint",
        "path outside allowed directories: /etc/passwd",
        &["rm:*"],
    );

    let trusted_context = ToolPermissionContext {
        trusted_read_directories: vec![outside.to_string_lossy().into_owned()],
        ..permission_context(&project)
    };
    assert_eq!(
        check_tool_permission(
            &tool,
            &json::object([(
                "command",
                json::string(format!("cat {}", outside.join("secret.txt").display()))
            )]),
            &trusted_context,
        ),
        PermissionResult::allow()
    );
}

#[test]
fn bash_write_path_option_values_are_checked_like_python() {
    let workspace = TestWorkspace::new("bash-write-option-paths");
    let tool = BashTool::new();

    for command in [
        "cp --target-directory /etc file.txt",
        "cp --target-directory=/etc file.txt",
        "cp -t /etc file.txt",
        "cp -t/etc file.txt",
        "cp -pt/etc file.txt",
        "mv --target-directory /etc file.txt",
        "mv --target-directory=/etc file.txt",
        "mv -t /etc file.txt",
        "mv -t/etc file.txt",
        "mv -vt/etc file.txt",
        "ln --target-directory /etc file.txt",
        "ln --target-directory=/etc file.txt",
        "ln -t /etc file.txt",
        "ln -t/etc file.txt",
        "ln -st/etc file.txt",
        "install --target-directory /etc file.txt",
        "install --target-directory=/etc file.txt",
        "install -t /etc file.txt",
        "install -t/etc file.txt",
        "install -Dt/etc file.txt",
    ] {
        assert_permission_with_suggestions(
            check_tool_permission(
                &tool,
                &json::object([("command", json::string(command))]),
                &ToolPermissionContext {
                    mode: PermissionMode::AcceptEdits,
                    ..permission_context(workspace.path())
                },
            ),
            "ask",
            "path_constraint",
            "path outside allowed directories: /etc",
            &[&format!("{}:*", command.split_whitespace().next().unwrap())],
        );
    }
}

#[test]
fn bash_read_redirects_and_implicit_cd_reads_are_checked_like_python() {
    let workspace = TestWorkspace::new("bash-read-paths");
    let tool = BashTool::new();

    for (command, detail, suggestions) in [
        (
            "cat < /etc/passwd",
            "path outside allowed directories",
            vec!["cat:*"],
        ),
        (
            "cd /etc && rg root",
            "read path after cd requires confirmation: current directory",
            vec!["cd:*", "rg:*"],
        ),
        (
            "cd /etc && fd passwd",
            "read path after cd requires confirmation: current directory",
            vec!["cd:*", "fd:*"],
        ),
        (
            "cd /etc && find",
            "read path after cd requires confirmation: current directory",
            vec!["cd:*", "find:*"],
        ),
    ] {
        assert_permission_with_suggestions(
            check_tool_permission(
                &tool,
                &json::object([("command", json::string(command))]),
                &permission_context(workspace.path()),
            ),
            "ask",
            "path_constraint",
            detail,
            &suggestions,
        );
    }
}

#[test]
fn bash_suggestions_focus_on_blocking_subcommand_like_python() {
    let workspace = TestWorkspace::new("bash-suggestions");
    let tool = BashTool::new();

    assert_permission_with_suggestions(
        check_tool_permission(
            &tool,
            &json::object([("command", json::string("eval ls && mkdir -p out"))]),
            &permission_context(workspace.path()),
        ),
        "ask",
        "complex_command",
        "complex command requires confirmation",
        &["mkdir:*"],
    );
    assert_eq!(
        check_tool_permission(
            &tool,
            &json::object([("command", json::string("ls && mkdir out"))]),
            &permission_context(workspace.path()),
        ),
        PermissionResult {
            behavior: "ask".into(),
            message: "Allow Bash?".into(),
            reason: None,
            suggestions: Some(vec![PermissionRuleValue {
                tool_name: "bash".into(),
                rule_content: "mkdir:*".into(),
            }]),
        },
    );
}

#[test]
fn bash_readonly_variants_match_python_allowlist() {
    let workspace = TestWorkspace::new("bash-readonly-variants");
    workspace.write_file("repo/.gitkeep", "");
    let tool = BashTool::new();

    for command in [
        "git -C repo status",
        "git -c core.quotePath=false status",
        "pip3.11 freeze",
        "uv pip list",
        "cargo metadata",
    ] {
        assert_eq!(
            check_tool_permission(
                &tool,
                &json::object([("command", json::string(command))]),
                &permission_context(workspace.path()),
            ),
            PermissionResult::allow(),
            "{command}"
        );
    }
}

#[test]
fn bash_dangerous_readonly_arguments_require_confirmation() {
    let workspace = TestWorkspace::new("bash-dangerous-readonly");
    let tool = BashTool::new();

    for command in [
        "find . -delete",
        "sed -i 's/a/b/' file.txt",
        "sed -f run.sed file.txt",
        "sed --file run.sed file.txt",
        "sed -frun.sed file.txt",
        "sed '1e echo marker' file.txt",
        "sed '/foo/!e echo marker' file.txt",
        "sed 's/.*/echo marker/e' file.txt",
        "sed 's/a/echo marker/g2e' file.txt",
        "sed '1{e echo marker;}' file.txt",
        "sed '\\%foo%e echo marker' file.txt",
        "sed -nes/a/b/e file.txt",
        "sed 'w /tmp/out' file.txt",
        "sed 's/a/b/w /tmp/out' file.txt",
        "sed 's1foo1bar1w /tmp/out' file.txt",
        "sed -Ees/a/b/w/tmp/out file.txt",
        "rg --pre cat needle .",
    ] {
        let result = check_tool_permission(
            &tool,
            &json::object([("command", json::string(command))]),
            &permission_context(workspace.path()),
        );
        assert_eq!(result.behavior, "ask");
        assert_eq!(
            result
                .reason
                .as_ref()
                .map(|reason| reason.type_name.as_str()),
            Some("dangerous_readonly_argument")
        );
    }
}

#[test]
fn bash_safe_sed_readonly_arguments_remain_allowed_like_python() {
    let workspace = TestWorkspace::new("bash-safe-sed-readonly");
    workspace.write_file("file.txt", "alpha\n");
    let tool = BashTool::new();

    for command in [
        "sed -es/i/j/ file.txt",
        "sed 'a\\\nwarning' file.txt",
        "sed '1{a\\\nwarning\n}' file.txt",
        "sed '# s/a/b/w /tmp/out' file.txt",
        "sed 'p;# s/a/b/w /tmp/out' file.txt",
        "sed -n '\\#s/a/b/w#p' file.txt",
        "sed -n '/s\\/a\\/b\\/w/p' file.txt",
        "sed ':we' file.txt",
        "sed 'b we' file.txt",
        "sed 't we' file.txt",
        "sed 'T we' file.txt",
        "sed 'r wfile' file.txt",
        "sed 'R wfile' file.txt",
    ] {
        let result = check_tool_permission(
            &tool,
            &json::object([("command", json::string(command))]),
            &permission_context(workspace.path()),
        );
        assert_eq!(result.behavior, "allow", "{command}");
    }
}

#[test]
fn bash_sed_script_read_paths_are_checked_like_python() {
    let workspace = TestWorkspace::new("bash-sed-read-paths");
    workspace.write_file("file.txt", "alpha\n");
    let tool = BashTool::new();

    assert_permission_with_suggestions(
        check_tool_permission(
            &tool,
            &json::object([("command", json::string("sed 'r /etc/passwd' file.txt"))]),
            &permission_context(workspace.path()),
        ),
        "ask",
        "path_constraint",
        "path outside allowed directories",
        &["sed:*"],
    );
}

#[test]
fn bash_compound_command_safety_limits_match_python() {
    let workspace = TestWorkspace::new("bash-compound-limits");
    let tool = BashTool::new();
    let too_many = (0..11)
        .map(|index| format!("echo {index}"))
        .collect::<Vec<_>>()
        .join(" && ");

    assert_permission_with_suggestions(
        check_tool_permission(
            &tool,
            &json::object([("command", json::string(too_many))]),
            &permission_context(workspace.path()),
        ),
        "ask",
        "compound_limit",
        "too many subcommands (>10)",
        &["echo:*"],
    );
    assert_permission_with_suggestions(
        check_tool_permission(
            &tool,
            &json::object([("command", json::string("cd a && cd b"))]),
            &permission_context(workspace.path()),
        ),
        "ask",
        "compound_cd",
        "multiple cd commands in compound command",
        &["cd:*"],
    );
    assert_permission_with_suggestions(
        check_tool_permission(
            &tool,
            &json::object([("command", json::string("cd repo && git status"))]),
            &permission_context(workspace.path()),
        ),
        "ask",
        "compound_cd_git",
        "cd combined with git in compound command",
        &["cd:*", "git:*"],
    );
    assert_permission_with_suggestions(
        check_tool_permission(
            &tool,
            &json::object([("command", json::string("cd repo && cat file.txt"))]),
            &permission_context(workspace.path()),
        ),
        "ask",
        "path_constraint",
        "read path after cd requires confirmation: file.txt",
        &["cd:*", "cat:*"],
    );
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

fn empty_object() -> JsonValue {
    json::object(Vec::<(&str, JsonValue)>::new())
}

fn grouped_rules(entries: &[(&str, &str)]) -> BTreeMap<String, Vec<String>> {
    let mut grouped = BTreeMap::new();
    for (source, rule) in entries {
        grouped
            .entry((*source).to_owned())
            .or_insert_with(Vec::new)
            .push((*rule).to_owned());
    }
    grouped
}

fn assert_permission(result: PermissionResult, behavior: &str, reason_type: &str, detail: &str) {
    assert_eq!(
        result,
        PermissionResult {
            behavior: behavior.into(),
            message: detail.into(),
            reason: Some(PermissionDecisionReason {
                type_name: reason_type.into(),
                detail: detail.into(),
            }),
            suggestions: None,
        }
    );
}

fn assert_permission_with_suggestions(
    result: PermissionResult,
    behavior: &str,
    reason_type: &str,
    detail: &str,
    suggestions: &[&str],
) {
    assert_eq!(
        result,
        PermissionResult {
            behavior: behavior.into(),
            message: detail.into(),
            reason: Some(PermissionDecisionReason {
                type_name: reason_type.into(),
                detail: detail.into(),
            }),
            suggestions: Some(
                suggestions
                    .iter()
                    .map(|rule| PermissionRuleValue {
                        tool_name: "bash".into(),
                        rule_content: (*rule).into(),
                    })
                    .collect()
            ),
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

    fn write_file(&self, relative_path: &str, content: &str) {
        let path = self.root.join(relative_path);
        if let Some(parent) = path.parent() {
            fs::create_dir_all(parent).expect("create parent dirs");
        }
        fs::write(path, content).expect("write test file");
    }
}

fn tool_context(cwd: &Path) -> ToolContext {
    ToolContext {
        cwd: cwd.to_string_lossy().into_owned(),
    }
}

impl Drop for TestWorkspace {
    fn drop(&mut self) {
        let _ = fs::remove_dir_all(&self.root);
    }
}
