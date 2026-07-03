use std::fs;
use std::path::{Path, PathBuf};
use std::sync::atomic::{AtomicU64, Ordering};

use iac_code_protocol::json::{self, JsonValue};
use iac_code_protocol::permission::{PermissionMode, PermissionResult, ToolPermissionContext};
use iac_code_tools::{
    check_tool_permission, MemoryManager, ReadMemoryTool, Tool, ToolResult, WriteMemoryTool,
};

#[test]
fn write_memory_saves_memory_file_and_index_like_python() {
    let workspace = TestWorkspace::new("memory-write");
    let manager = MemoryManager::new(workspace.path()).expect("memory manager");
    let tool = WriteMemoryTool::new(manager);

    assert_eq!(
        tool.execute(
            &json::object([
                ("name", json::string("role")),
                ("content", json::string("User prefers concise answers.")),
                ("memory_type", json::string("user")),
                ("description", json::string("User style preference")),
            ]),
            &Default::default(),
        ),
        ToolResult::success("Memory 'role' saved.")
    );

    assert_eq!(
        fs::read_to_string(workspace.path().join("role.md")).expect("memory file"),
        "---\nname: role\ndescription: User style preference\ntype: user\n---\n\nUser prefers concise answers.\n"
    );
    assert_eq!(
        fs::read_to_string(workspace.path().join("MEMORY.md")).expect("index file"),
        "- [role](role.md) — User style preference\n"
    );
}

#[test]
fn read_memory_lists_index_and_reads_named_memory_like_python() {
    let workspace = TestWorkspace::new("memory-read");
    let manager = MemoryManager::new(workspace.path()).expect("memory manager");
    manager
        .save(
            "project-note",
            "Use fake providers in tests.",
            "project",
            "Testing rule",
        )
        .expect("save memory");
    let tool = ReadMemoryTool::new(manager);

    assert_eq!(
        tool.execute(
            &json::object(Vec::<(&str, JsonValue)>::new()),
            &Default::default()
        ),
        ToolResult::success("- [project-note](project-note.md) — Testing rule\n")
    );
    assert_eq!(
        tool.execute(
            &json::object([("name", json::string("project-note"))]),
            &Default::default(),
        ),
        ToolResult::success("[project] Testing rule\n\nUse fake providers in tests.")
    );
}

#[test]
fn memory_manager_prompt_content_matches_python_format() {
    let workspace = TestWorkspace::new("memory-prompt");
    let manager = MemoryManager::new(workspace.path()).expect("memory manager");
    manager
        .save("user-style", "Answer concisely.", "user", "Style")
        .expect("save user memory");
    manager
        .save(
            "project-rule",
            "Use mocked providers in tests.",
            "project",
            "Testing",
        )
        .expect("save project memory");

    assert_eq!(
        manager.get_prompt_content(),
        "[project] Use mocked providers in tests.\n\n[user] Answer concisely."
    );
}

#[test]
fn read_memory_reports_empty_and_missing_like_python() {
    let workspace = TestWorkspace::new("memory-missing");
    let tool = ReadMemoryTool::new(MemoryManager::new(workspace.path()).expect("memory manager"));

    assert_eq!(
        tool.execute(
            &json::object(Vec::<(&str, JsonValue)>::new()),
            &Default::default()
        ),
        ToolResult::success("No memories saved yet.")
    );
    assert_eq!(
        tool.execute(
            &json::object([("name", json::string("missing"))]),
            &Default::default(),
        ),
        ToolResult::error("Memory 'missing' not found.")
    );
}

#[test]
fn write_memory_rejects_invalid_type_and_name_like_python() {
    let workspace = TestWorkspace::new("memory-invalid");
    let tool = WriteMemoryTool::new(MemoryManager::new(workspace.path()).expect("memory manager"));

    assert_eq!(
        tool.execute(
            &json::object([
                ("name", json::string("../bad")),
                ("content", json::string("x")),
                ("memory_type", json::string("user")),
                ("description", json::string("bad")),
            ]),
            &Default::default(),
        ),
        ToolResult::error("Invalid memory name: \"../bad\"")
    );
    assert_eq!(
        tool.execute(
            &json::object([
                ("name", json::string("bad-type")),
                ("content", json::string("x")),
                ("memory_type", json::string("other")),
                ("description", json::string("bad")),
            ]),
            &Default::default(),
        ),
        ToolResult::error("Invalid memory type: other")
    );
}

#[test]
fn memory_tool_permissions_match_python_read_write_defaults() {
    let workspace = TestWorkspace::new("memory-permissions");
    let manager = MemoryManager::new(workspace.path()).expect("memory manager");
    let read_tool = ReadMemoryTool::new(manager.clone());
    let write_tool = WriteMemoryTool::new(manager);
    let context = ToolPermissionContext {
        mode: PermissionMode::Default,
        cwd: workspace.path().to_string_lossy().into_owned(),
        allow_rules: Default::default(),
        deny_rules: Default::default(),
        ask_rules: Default::default(),
        additional_directories: Vec::new(),
        trusted_read_directories: Vec::new(),
    };

    assert_eq!(
        check_tool_permission(
            &read_tool,
            &json::object(Vec::<(&str, JsonValue)>::new()),
            &context,
        ),
        PermissionResult::allow()
    );
    assert_eq!(
        check_tool_permission(
            &write_tool,
            &json::object([
                ("name", json::string("role")),
                ("content", json::string("x")),
                ("memory_type", json::string("user")),
                ("description", json::string("role")),
            ]),
            &context,
        ),
        PermissionResult::ask("Allow write_memory?")
    );
}

struct TestWorkspace {
    root: PathBuf,
}

impl TestWorkspace {
    fn new(name: &str) -> Self {
        static COUNTER: AtomicU64 = AtomicU64::new(0);
        let root = std::env::temp_dir().join(format!(
            "iac-code-rs-{name}-{}",
            COUNTER.fetch_add(1, Ordering::SeqCst)
        ));
        if root.exists() {
            fs::remove_dir_all(&root).expect("remove stale temp workspace");
        }
        fs::create_dir_all(&root).expect("create temp workspace");
        Self { root }
    }

    fn path(&self) -> &Path {
        &self.root
    }
}

impl Drop for TestWorkspace {
    fn drop(&mut self) {
        let _ = fs::remove_dir_all(&self.root);
    }
}
