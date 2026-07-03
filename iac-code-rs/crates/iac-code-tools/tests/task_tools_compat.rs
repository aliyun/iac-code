use iac_code_protocol::json::{self, JsonValue};
use iac_code_protocol::permission::{PermissionMode, PermissionResult, ToolPermissionContext};
use iac_code_tools::{
    check_tool_permission, TaskGetTool, TaskListTool, TaskManager, TaskStatus, TaskStopTool, Tool,
    ToolResult,
};

#[test]
fn task_list_reports_empty_and_task_summaries_like_python() {
    let manager = TaskManager::new();
    let tool = TaskListTool::new(manager.clone());

    assert_eq!(
        tool.execute(
            &json::object(Vec::<(&str, JsonValue)>::new()),
            &Default::default()
        ),
        ToolResult::success("No background tasks.")
    );

    let running = manager.register("Explore repo", "general-purpose");
    let completed = manager.register("Summarize docs", "research");
    manager.complete(&completed, &"x".repeat(250));

    assert_eq!(
        tool.execute(&json::object(Vec::<(&str, JsonValue)>::new()), &Default::default()),
        ToolResult::success(format!(
            "- [{running}] running | [general-purpose] Explore repo\n- [{completed}] completed | [research] Summarize docs\n  Result: {}",
            "x".repeat(200)
        ))
    );
}

#[test]
fn task_get_reports_existing_error_and_missing_like_python() {
    let manager = TaskManager::new();
    let task_id = manager.register("Run tests", "qa");
    manager.update_progress(&task_id, 3, 42);
    manager.fail(&task_id, "failed hard");
    let tool = TaskGetTool::new(manager);

    assert_eq!(
        tool.execute(
            &json::object([("task_id", json::string(&task_id))]),
            &Default::default(),
        ),
        ToolResult::success(format!(
            "ID: {task_id}\nDescription: Run tests\nStatus: failed\nAgent type: qa\nTool uses: 3\nTokens: 42\nError: failed hard"
        ))
    );
    assert_eq!(
        tool.execute(
            &json::object([("task_id", json::string("missing"))]),
            &Default::default(),
        ),
        ToolResult::error("Task 'missing' not found.")
    );
}

#[test]
fn task_stop_updates_running_tasks_and_reports_terminal_status_like_python() {
    let manager = TaskManager::new();
    let running = manager.register("Long task", "general-purpose");
    let completed = manager.register("Done task", "general-purpose");
    manager.complete(&completed, "finished");
    let tool = TaskStopTool::new(manager.clone());

    assert_eq!(
        tool.execute(
            &json::object([("task_id", json::string(&running))]),
            &Default::default(),
        ),
        ToolResult::success(format!("Task '{running}' stopped."))
    );
    assert_eq!(
        manager.get(&running).expect("running task").status,
        TaskStatus::Stopped
    );
    assert_eq!(
        tool.execute(
            &json::object([("task_id", json::string(&completed))]),
            &Default::default(),
        ),
        ToolResult::success(format!("Task '{completed}' already completed."))
    );
    assert_eq!(
        tool.execute(
            &json::object([("task_id", json::string("missing"))]),
            &Default::default(),
        ),
        ToolResult::error("Task 'missing' not found.")
    );
}

#[test]
fn task_status_overwrites_keep_prior_result_and_error_like_python() {
    let manager = TaskManager::new();
    let failed_then_completed = manager.register("Recover task", "qa");
    manager.fail(&failed_then_completed, "first failure");
    manager.complete(&failed_then_completed, "recovered");
    let recovered = manager
        .get(&failed_then_completed)
        .expect("recovered task should exist");
    assert_eq!(recovered.status, TaskStatus::Completed);
    assert_eq!(recovered.result.as_deref(), Some("recovered"));
    assert_eq!(recovered.error.as_deref(), Some("first failure"));

    let completed_then_failed = manager.register("Regress task", "qa");
    manager.complete(&completed_then_failed, "initial result");
    manager.fail(&completed_then_failed, "later failure");
    let regressed = manager
        .get(&completed_then_failed)
        .expect("regressed task should exist");
    assert_eq!(regressed.status, TaskStatus::Failed);
    assert_eq!(regressed.result.as_deref(), Some("initial result"));
    assert_eq!(regressed.error.as_deref(), Some("later failure"));
}

#[test]
fn task_tool_permissions_match_python_read_write_defaults() {
    let manager = TaskManager::new();
    let list_tool = TaskListTool::new(manager.clone());
    let get_tool = TaskGetTool::new(manager.clone());
    let stop_tool = TaskStopTool::new(manager);
    let context = ToolPermissionContext {
        mode: PermissionMode::Default,
        cwd: ".".into(),
        allow_rules: Default::default(),
        deny_rules: Default::default(),
        ask_rules: Default::default(),
        additional_directories: Vec::new(),
        trusted_read_directories: Vec::new(),
    };

    assert_eq!(
        check_tool_permission(
            &list_tool,
            &json::object(Vec::<(&str, JsonValue)>::new()),
            &context,
        ),
        PermissionResult::allow()
    );
    assert_eq!(
        check_tool_permission(
            &get_tool,
            &json::object([("task_id", json::string("t1"))]),
            &context,
        ),
        PermissionResult::allow()
    );
    assert_eq!(
        check_tool_permission(
            &stop_tool,
            &json::object([("task_id", json::string("t1"))]),
            &context,
        ),
        PermissionResult::ask("Allow task_stop?")
    );
}
