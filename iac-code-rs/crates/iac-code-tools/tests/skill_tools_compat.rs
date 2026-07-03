use std::fs;
use std::path::{Path, PathBuf};
use std::sync::{Arc, Mutex};
use std::time::{SystemTime, UNIX_EPOCH};

use iac_code_protocol::json::{self, JsonValue};
use iac_code_protocol::message::AgentMessageContent;
use iac_code_protocol::{StreamEvent, SubAgentToolEvent};

use iac_code_tools::{
    register_skill_tools, AgentProgress, RegistryToolExecutor, SkillManager, SkillTool,
    SubAgentRequest, SubAgentResult, SubAgentRunner, ToolCallRequest, ToolContext, ToolExecutor,
    ToolRegistry, ToolResult,
};

#[test]
fn skill_manager_discovers_project_skills_and_builds_listing_like_python() {
    let workspace = TestWorkspace::new("skill-discovery");
    write_skill(
        &workspace.path().join("skills").join("demo"),
        "Demo skill",
        "Use it for demos.",
        "Say hello",
    );

    let manager = SkillManager::discover(workspace.path().join("user-skills"), workspace.path())
        .expect("skills should discover");
    let skill = manager.get("demo").expect("demo skill should exist");

    assert_eq!(skill.name, "demo");
    assert_eq!(skill.description, "Demo skill");
    let listing = manager.build_listing();
    assert!(
        listing.starts_with("The following skills are available for use with the Skill tool:\n")
    );
    assert!(
        listing.contains("- demo: Demo skill\nUse it for demos."),
        "missing demo skill in listing: {listing}"
    );
}

#[test]
fn skill_manager_uses_localized_frontmatter_description_for_detected_language() {
    let _env = EnvGuard::set_many(&[
        ("LANGUAGE", Some("zh_CN.UTF-8")),
        ("LC_ALL", None),
        ("LC_MESSAGES", None),
        ("LANG", None),
    ]);
    let workspace = TestWorkspace::new("skill-localized-description");
    write_skill_with_frontmatter(
        &workspace.path().join("skills").join("localized"),
        "description: English description\ndescriptions:\n  zh: 中文描述\n  es: Descripción localizada\nwhen_to_use: Use localized metadata.",
        "Localized body",
    );

    let manager = SkillManager::discover(workspace.path().join("user-skills"), workspace.path())
        .expect("skills should discover");
    let skill = manager
        .get("localized")
        .expect("localized skill should exist");

    assert_eq!(skill.description, "中文描述");
    let listing = manager.build_listing();
    assert!(
        listing.contains("- localized: 中文描述\nUse localized metadata."),
        "listing should use localized description: {listing}"
    );
    assert!(
        !listing.contains("English description"),
        "listing should not include fallback description when localized description exists: {listing}"
    );
}

#[cfg(unix)]
#[test]
fn skill_manager_deduplicates_symlinked_skill_dirs_like_python() {
    let workspace = TestWorkspace::new("skill-symlink-dedup");
    let skills_dir = workspace.path().join("skills");
    fs::create_dir_all(&skills_dir).expect("skills dir should be created");
    let real_skill = workspace.path().join("actual").join("real");
    write_skill(&real_skill, "Real skill", "Use the real skill.", "Body");
    std::os::unix::fs::symlink(&real_skill, skills_dir.join("alias"))
        .expect("skill symlink should be created");

    let manager = SkillManager::discover(workspace.path().join("user-skills"), workspace.path())
        .expect("skills should discover");
    let project_skills = manager
        .skills()
        .iter()
        .filter(|skill| skill.source == iac_code_tools::SkillSource::Project)
        .collect::<Vec<_>>();

    assert_eq!(
        project_skills.len(),
        1,
        "same skill directory should only be discovered once: {project_skills:?}"
    );
    assert_eq!(project_skills[0].name, "alias");
}

#[test]
fn skill_tool_loads_inline_skill_with_arguments_as_new_message() {
    let workspace = TestWorkspace::new("skill-tool");
    let skill_root = workspace
        .path()
        .join(".iac-code")
        .join("skills")
        .join("demo");
    write_skill(
        &skill_root,
        "Demo skill",
        "",
        "Skill root: ${SKILL_DIR}\nUse $ARGUMENTS",
    );
    let expected_skill_root = skill_root
        .canonicalize()
        .expect("skill root should canonicalize");
    let manager = SkillManager::discover(workspace.path().join("user-skills"), workspace.path())
        .expect("skills should discover");
    let mut registry = ToolRegistry::new();
    register_skill_tools(&mut registry, manager);
    let executor = RegistryToolExecutor::new(registry).with_context(ToolContext {
        cwd: workspace.path().to_string_lossy().into_owned(),
    });

    let result = executor.execute(ToolCallRequest {
        tool_use_id: "toolu_skill".into(),
        tool_name: "skill".into(),
        input: json::object([
            ("skill", json::string("/Demo")),
            ("args", json::string("ros stack")),
        ]),
    });

    assert!(!result.is_error);
    assert_eq!(result.content, "Skill 'demo' loaded (inline).");
    assert_eq!(result.new_messages.len(), 1);
    assert_eq!(
        json_string_field(&result.new_messages[0], "role"),
        Some("user")
    );
    let content = json_string_field(&result.new_messages[0], "content")
        .expect("skill message content should exist");
    assert!(content.starts_with("<skill-name>demo</skill-name>\n\n"));
    assert!(
        content.contains(&format!(
            "Base directory for this skill: {}",
            expected_skill_root.display()
        )),
        "missing skill root prelude: {content}"
    );
    assert!(
        content.contains(&format!("Skill root: {}", expected_skill_root.display())),
        "missing SKILL_DIR replacement: {content}"
    );
    assert!(content.contains("Use ros stack"));
}

#[test]
fn skill_tool_applies_model_and_effort_frontmatter_overrides_like_python() {
    let workspace = TestWorkspace::new("skill-model-effort");
    let skill_root = workspace.path().join("skills").join("tuned");
    write_skill_with_frontmatter(
        &skill_root,
        "description: Tuned skill\nmodel: claude-sonnet-4-6\neffort: high",
        "Use tuned model",
    );
    let manager = SkillManager::discover(workspace.path().join("user-skills"), workspace.path())
        .expect("skills should discover");
    let mut registry = ToolRegistry::new();
    register_skill_tools(&mut registry, manager);
    let executor = RegistryToolExecutor::new(registry).with_context(ToolContext {
        cwd: workspace.path().to_string_lossy().into_owned(),
    });

    let result = executor.execute(ToolCallRequest {
        tool_use_id: "toolu_skill".into(),
        tool_name: "skill".into(),
        input: json::object([("skill", json::string("tuned"))]),
    });

    assert!(!result.is_error);
    let modifier = result
        .context_modifier
        .expect("skill should expose context modifier");
    assert_eq!(
        modifier.model_override.as_deref(),
        Some("claude-sonnet-4-6")
    );
    assert_eq!(modifier.effort_override.as_deref(), Some("high"));
    assert!(modifier.allowed_tool_rules.is_empty());
}

#[test]
fn skill_tool_renders_inline_and_block_shell_segments_like_python() {
    let workspace = TestWorkspace::new("skill-shell");
    let skill_root = workspace.path().join("skills").join("shell-demo");
    write_skill(
        &skill_root,
        "Shell skill",
        "",
        "Inline !`printf inline`\n```!\nprintf block\n```\nDone",
    );
    let manager = SkillManager::discover(workspace.path().join("user-skills"), workspace.path())
        .expect("skills should discover");
    let mut registry = ToolRegistry::new();
    register_skill_tools(&mut registry, manager);
    let executor = RegistryToolExecutor::new(registry).with_context(ToolContext {
        cwd: workspace.path().to_string_lossy().into_owned(),
    });

    let result = executor.execute(ToolCallRequest {
        tool_use_id: "toolu_skill".into(),
        tool_name: "skill".into(),
        input: json::object([("skill", json::string("shell-demo"))]),
    });

    assert!(!result.is_error);
    let content = json_string_field(&result.new_messages[0], "content")
        .expect("skill message content should exist");
    assert!(
        content.contains("Inline inline"),
        "missing inline shell output: {content}"
    );
    assert!(
        content.contains("block\n\nDone") || content.contains("block\nDone"),
        "missing block shell output: {content}"
    );
    assert!(!content.contains("printf inline"));
    assert!(!content.contains("printf block"));
}

#[test]
fn skill_tool_keeps_argument_inline_shell_syntax_as_text_like_python() {
    let workspace = TestWorkspace::new("skill-arg-inline-shell");
    let skill_root = workspace.path().join("skills").join("arg-demo");
    write_skill(&skill_root, "Argument skill", "", "User input: $ARGUMENTS");
    let manager = SkillManager::discover(workspace.path().join("user-skills"), workspace.path())
        .expect("skills should discover");
    let mut registry = ToolRegistry::new();
    register_skill_tools(&mut registry, manager);
    let executor = RegistryToolExecutor::new(registry).with_context(ToolContext {
        cwd: workspace.path().to_string_lossy().into_owned(),
    });

    let result = executor.execute(ToolCallRequest {
        tool_use_id: "toolu_skill".into(),
        tool_name: "skill".into(),
        input: json::object([
            ("skill", json::string("arg-demo")),
            ("args", json::string("!`printf pwned`")),
        ]),
    });

    assert!(!result.is_error);
    let content = json_string_field(&result.new_messages[0], "content")
        .expect("skill message content should exist");
    assert!(
        content.contains("User input: !`printf pwned`"),
        "argument shell syntax should stay text: {content}"
    );
    assert!(
        !content.contains("User input: pwned"),
        "argument inline shell syntax was executed: {content}"
    );
}

#[test]
fn skill_tool_keeps_argument_block_shell_syntax_as_text_like_python() {
    let workspace = TestWorkspace::new("skill-arg-block-shell");
    let skill_root = workspace.path().join("skills").join("arg-demo");
    write_skill(&skill_root, "Argument skill", "", "User input:\n$ARGUMENTS");
    let manager = SkillManager::discover(workspace.path().join("user-skills"), workspace.path())
        .expect("skills should discover");
    let mut registry = ToolRegistry::new();
    register_skill_tools(&mut registry, manager);
    let executor = RegistryToolExecutor::new(registry).with_context(ToolContext {
        cwd: workspace.path().to_string_lossy().into_owned(),
    });

    let result = executor.execute(ToolCallRequest {
        tool_use_id: "toolu_skill".into(),
        tool_name: "skill".into(),
        input: json::object([
            ("skill", json::string("arg-demo")),
            ("args", json::string("```!\nprintf pwned\n```")),
        ]),
    });

    assert!(!result.is_error);
    let content = json_string_field(&result.new_messages[0], "content")
        .expect("skill message content should exist");
    assert!(
        content.contains("User input:\n```!\nprintf pwned\n```"),
        "argument shell block should stay text: {content}"
    );
    assert!(
        !content.contains("User input:\npwned"),
        "argument block shell syntax was executed: {content}"
    );
}

#[test]
fn skill_tool_substitutes_named_and_indexed_arguments_like_python() {
    let workspace = TestWorkspace::new("skill-named-args");
    let skill_root = workspace.path().join("skills").join("arg-demo");
    write_skill_with_frontmatter(
        &skill_root,
        "description: Argument skill\narguments:\n  - target\n  - branch",
        "Named: $target / $branch\nIndexed: $0 / $ARGUMENTS[1]\nFull: $ARGUMENTS",
    );
    let manager = SkillManager::discover(workspace.path().join("user-skills"), workspace.path())
        .expect("skills should discover");
    let mut registry = ToolRegistry::new();
    register_skill_tools(&mut registry, manager);
    let executor = RegistryToolExecutor::new(registry).with_context(ToolContext {
        cwd: workspace.path().to_string_lossy().into_owned(),
    });

    let result = executor.execute(ToolCallRequest {
        tool_use_id: "toolu_skill".into(),
        tool_name: "skill".into(),
        input: json::object([
            ("skill", json::string("arg-demo")),
            ("args", json::string("\"prod stack\" main")),
        ]),
    });

    assert!(!result.is_error);
    let content = json_string_field(&result.new_messages[0], "content")
        .expect("skill message content should exist");
    assert!(
        content.contains("Named: prod stack / main"),
        "missing named arguments: {content}"
    );
    assert!(
        content.contains("Indexed: prod stack / main"),
        "missing indexed arguments: {content}"
    );
    assert!(
        content.contains("Full: \"prod stack\" main"),
        "missing full arguments: {content}"
    );
}

#[test]
fn skill_tool_runs_forked_skill_through_sub_agent_runner() {
    let workspace = TestWorkspace::new("skill-fork");
    let skill_root = workspace.path().join("skills").join("fork-demo");
    write_skill_with_frontmatter(
        &skill_root,
        "description: Fork skill\ncontext: fork\nagent: explore",
        "Investigate $ARGUMENTS",
    );
    let manager = SkillManager::discover(workspace.path().join("user-skills"), workspace.path())
        .expect("skills should discover");
    let child_event = SubAgentToolEvent {
        parent_tool_use_id: String::new(),
        child_tool_name: "grep".into(),
        child_tool_input: json::object([("pattern", json::string("stack drift"))]),
        is_done: true,
        is_error: false,
    };
    let runner = Arc::new(RecordingRunner::success_with_events(
        "fork answer",
        3,
        55,
        vec![child_event.clone()],
    ));
    let mut registry = ToolRegistry::new();
    registry.register(Box::new(
        SkillTool::new(manager).with_sub_agent_runner(runner.clone()),
    ));
    let executor = RegistryToolExecutor::new(registry).with_context(ToolContext {
        cwd: workspace.path().to_string_lossy().into_owned(),
    });

    let result = executor.execute(ToolCallRequest {
        tool_use_id: "toolu_skill".into(),
        tool_name: "skill".into(),
        input: json::object([
            ("skill", json::string("fork-demo")),
            ("args", json::string("stack drift")),
        ]),
    });

    assert_eq!(
        result,
        ToolResult::success(
            "fork answer\n\n[Skill 'fork-demo' completed: 3 tool calls, 55 tokens]"
        )
        .with_stream_events(vec![StreamEvent::SubAgentTool(child_event)])
    );
    assert!(result.new_messages.is_empty());
    let requests = runner.requests();
    assert_eq!(requests.len(), 1);
    assert_eq!(requests[0].agent_type, "explore");
    assert_eq!(requests[0].cwd, workspace.path().to_string_lossy());
    assert!(requests[0].prompt.contains("Investigate stack drift"));
    assert!(!requests[0]
        .prompt
        .contains("<skill-name>fork-demo</skill-name>"));
}

#[test]
fn bundled_iac_aliyun_skill_materializes_referenced_resources() {
    let workspace = TestWorkspace::new("skill-bundled-resources");
    let user_skills_dir = workspace.path().join("user-skills");

    let manager =
        SkillManager::discover(&user_skills_dir, workspace.path()).expect("skills should discover");
    let skill = manager
        .get("iac-aliyun")
        .expect("bundled iac-aliyun skill should exist");
    let skill_root = PathBuf::from(&skill.skill_root);

    assert!(
        skill_root.starts_with(workspace.path().join("bundled-skills").join("iac-aliyun")),
        "bundled skill root should be a self-contained materialized resource tree: {}",
        skill_root.display()
    );
    for relative_path in [
        "SKILL.md",
        "auto_trigger.py",
        "references/template-parameters.md",
        "references/template-parameter-recommendation.md",
        "references/ros-template.md",
        "references/terraform-template.md",
        "references/cloud-products/ecs.md",
        "references/cloud-products/oss.md",
        "references/cloud-products/rds.md",
        "references/cloud-products/redis.md",
        "references/cloud-products/slb.md",
        "references/cloud-products/vpc.md",
        "scripts/tf2ros.py",
    ] {
        assert!(
            skill_root.join(relative_path).is_file(),
            "missing bundled iac-aliyun resource: {relative_path}"
        );
    }

    let ros_reference = fs::read_to_string(skill_root.join("references/ros-template.md"))
        .expect("ROS reference should be readable from materialized bundle");
    assert!(
        ros_reference.contains("RunCommand"),
        "materialized ROS reference should contain bundled content"
    );

    let auto_messages = manager.auto_triggered_messages(
        "请帮我生成一个阿里云 ECS ROS 模板",
        &ToolContext {
            cwd: workspace.path().to_string_lossy().into_owned(),
        },
        &[],
    );
    assert_eq!(auto_messages.len(), 1);
    let AgentMessageContent::Text(content) = &auto_messages[0].content else {
        panic!("auto-triggered skill message should be text");
    };
    assert!(
        content.contains(&format!(
            "Base directory for this skill: {}",
            skill_root.display()
        )),
        "auto-triggered bundled skill should render materialized skill root: {content}"
    );
}

fn write_skill(skill_root: &Path, description: &str, when_to_use: &str, content: &str) {
    write_skill_with_frontmatter(
        skill_root,
        &format!("description: {description}\nwhen_to_use: {when_to_use}"),
        content,
    );
}

fn write_skill_with_frontmatter(skill_root: &Path, frontmatter: &str, content: &str) {
    fs::create_dir_all(skill_root).expect("skill root should be created");
    fs::write(
        skill_root.join("SKILL.md"),
        format!("---\n{frontmatter}\n---\n\n{content}\n"),
    )
    .expect("skill should be written");
}

fn json_string_field<'a>(value: &'a JsonValue, field: &str) -> Option<&'a str> {
    let JsonValue::Object(fields) = value else {
        return None;
    };
    let Some(JsonValue::String(value)) = fields.get(field) else {
        return None;
    };
    Some(value)
}

struct TestWorkspace {
    root: PathBuf,
}

impl TestWorkspace {
    fn new(prefix: &str) -> Self {
        let root = unique_temp_dir(prefix);
        fs::create_dir_all(&root).expect("workspace should be created");
        Self { root }
    }

    fn path(&self) -> &Path {
        &self.root
    }
}

impl Drop for TestWorkspace {
    fn drop(&mut self) {
        fs::remove_dir_all(&self.root).ok();
    }
}

fn unique_temp_dir(prefix: &str) -> PathBuf {
    let nanos = SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .expect("system clock should be after unix epoch")
        .as_nanos();
    std::env::temp_dir().join(format!("{prefix}-{}-{nanos}", std::process::id()))
}

struct EnvGuard {
    previous: Vec<(&'static str, Option<String>)>,
}

impl EnvGuard {
    fn set_many(values: &[(&'static str, Option<&str>)]) -> Self {
        let previous = values
            .iter()
            .map(|(key, _)| (*key, std::env::var(key).ok()))
            .collect::<Vec<_>>();
        for (key, value) in values {
            match value {
                Some(value) => std::env::set_var(key, value),
                None => std::env::remove_var(key),
            }
        }
        Self { previous }
    }
}

impl Drop for EnvGuard {
    fn drop(&mut self) {
        for (key, value) in self.previous.drain(..) {
            match value {
                Some(value) => std::env::set_var(key, value),
                None => std::env::remove_var(key),
            }
        }
    }
}

#[derive(Clone)]
struct RecordingRunner {
    response: Result<SubAgentResult, String>,
    requests: Arc<Mutex<Vec<SubAgentRequest>>>,
}

impl RecordingRunner {
    fn success_with_events(
        output: &str,
        tool_use_count: u32,
        token_count: u32,
        stream_events: Vec<SubAgentToolEvent>,
    ) -> Self {
        Self {
            response: Ok(SubAgentResult {
                output: output.to_owned(),
                progress: AgentProgress {
                    tool_use_count,
                    token_count,
                },
                stream_events,
            }),
            requests: Arc::new(Mutex::new(Vec::new())),
        }
    }

    fn requests(&self) -> Vec<SubAgentRequest> {
        self.requests.lock().expect("requests").clone()
    }
}

impl SubAgentRunner for RecordingRunner {
    fn run(&self, request: SubAgentRequest) -> Result<SubAgentResult, String> {
        self.requests.lock().expect("requests").push(request);
        self.response.clone()
    }
}
