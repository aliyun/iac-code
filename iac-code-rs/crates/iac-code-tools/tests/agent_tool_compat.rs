use std::sync::{Arc, Mutex};
use std::time::{Duration, Instant};

use iac_code_protocol::{json, StreamEvent, SubAgentToolEvent};
use iac_code_tools::{
    AgentProgress, AgentTool, SubAgentRequest, SubAgentResult, SubAgentRunner, TaskManager,
    TaskStatus, Tool, ToolContext, ToolResult,
};

#[test]
fn agent_tool_description_lists_builtin_agent_types_like_python() {
    let tool = AgentTool::new(Arc::new(RecordingRunner::success("unused", 0, 0)));

    let description = tool.description();

    assert!(description.contains("Available agent types:"));
    assert!(description.contains("general-purpose"));
    assert!(description.contains("explore"));
    assert!(description.contains("plan"));
    assert!(description.contains("Use to quickly find files"));
}

#[test]
fn agent_tool_user_facing_name_matches_python_agent_type_labels() {
    let tool = AgentTool::new(Arc::new(RecordingRunner::success("unused", 0, 0)));

    assert_eq!(
        tool.user_facing_name(&json::object([("subagent_type", json::string("explore"))])),
        "Explore"
    );
    assert_eq!(
        tool.user_facing_name(&json::object([("subagent_type", json::string("plan"))])),
        "Plan"
    );
    assert_eq!(
        tool.user_facing_name(&json::object([(
            "subagent_type",
            json::string("general-purpose")
        )])),
        "Agent"
    );
    assert_eq!(
        tool.user_facing_name(&json::object([("agent_type", json::string("explore"))])),
        "Explore"
    );
    assert_eq!(
        tool.user_facing_name(&json::object(Vec::<(&str, json::JsonValue)>::new())),
        "Agent"
    );
}

#[test]
fn agent_tool_runs_foreground_sub_agent_and_reports_python_stats_suffix() {
    let runner = Arc::new(RecordingRunner::success("Sub-agent answer", 2, 37));
    let tool = AgentTool::new(runner.clone());

    let result = tool.execute(
        &json::object([
            ("prompt", json::string("inspect the repo")),
            ("description", json::string("Inspect repo")),
            ("subagent_type", json::string("explore")),
        ]),
        &ToolContext {
            cwd: "/workspace".into(),
        },
    );

    assert_eq!(
        result,
        ToolResult::success("Sub-agent answer\n\n[Agent stats: 2 tool calls, 37 tokens]")
    );
    assert_eq!(
        runner.requests(),
        vec![SubAgentRequest {
            prompt: "inspect the repo".into(),
            agent_type: "explore".into(),
            cwd: "/workspace".into(),
        }]
    );
}

#[test]
fn agent_tool_forwards_sub_agent_tool_progress_events() {
    let child_event = SubAgentToolEvent {
        parent_tool_use_id: String::new(),
        child_tool_name: "read_file".into(),
        child_tool_input: json::object([("path", json::string("src/main.rs"))]),
        is_done: true,
        is_error: false,
    };
    let runner = Arc::new(RecordingRunner::success_with_events(
        "Sub-agent answer",
        1,
        11,
        vec![child_event.clone()],
    ));
    let tool = AgentTool::new(runner);

    let result = tool.execute(
        &json::object([
            ("prompt", json::string("inspect the repo")),
            ("description", json::string("Inspect repo")),
            ("subagent_type", json::string("explore")),
        ]),
        &ToolContext {
            cwd: "/workspace".into(),
        },
    );

    assert_eq!(
        result,
        ToolResult::success("Sub-agent answer\n\n[Agent stats: 1 tool calls, 11 tokens]")
            .with_stream_events(vec![StreamEvent::SubAgentTool(child_event)])
    );
}

#[test]
fn agent_tool_rejects_unknown_agent_type_like_python() {
    let tool = AgentTool::new(Arc::new(RecordingRunner::success("unused", 0, 0)));

    assert_eq!(
        tool.execute(
            &json::object([
                ("prompt", json::string("do it")),
                ("description", json::string("Do it")),
                ("subagent_type", json::string("missing")),
            ]),
            &Default::default(),
        ),
        ToolResult::error("Unknown agent type: 'missing'")
    );
}

#[test]
fn agent_tool_launches_background_task_and_persists_result() {
    let manager = TaskManager::new();
    let tool = AgentTool::new(Arc::new(RecordingRunner::success("background done", 1, 9)))
        .with_task_manager(manager.clone());

    let result = tool.execute(
        &json::object([
            ("prompt", json::string("run in background")),
            ("description", json::string("Background work")),
            ("subagent_type", json::string("general-purpose")),
            ("run_in_background", json::bool_value(true)),
        ]),
        &Default::default(),
    );

    assert!(!result.is_error, "{result:?}");
    assert!(result
        .content
        .starts_with("Background agent launched (task_id: "));
    assert!(result.content.ends_with(", type: general-purpose)"));

    let task = wait_for_completed_task(&manager);
    assert_eq!(task.description, "Background work");
    assert_eq!(task.agent_type, "general-purpose");
    assert_eq!(task.result.as_deref(), Some("background done"));
    assert_eq!(task.tool_use_count, 1);
    assert_eq!(task.token_count, 9);
}

#[derive(Clone)]
struct RecordingRunner {
    response: Result<SubAgentResult, String>,
    requests: Arc<Mutex<Vec<SubAgentRequest>>>,
}

impl RecordingRunner {
    fn success(output: &str, tool_use_count: u32, token_count: u32) -> Self {
        Self::success_with_events(output, tool_use_count, token_count, Vec::new())
    }

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

fn wait_for_completed_task(manager: &TaskManager) -> iac_code_tools::TaskInfo {
    let deadline = Instant::now() + Duration::from_secs(2);
    loop {
        let tasks = manager.list_all();
        if let Some(task) = tasks
            .iter()
            .find(|task| task.status == TaskStatus::Completed)
            .cloned()
        {
            return task;
        }
        assert!(
            Instant::now() < deadline,
            "background task did not complete"
        );
        std::thread::sleep(Duration::from_millis(10));
    }
}
