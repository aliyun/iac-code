use std::sync::Arc;
use std::thread;

use iac_code_protocol::StreamEvent;

use super::model::{SubAgentRequest, SubAgentRunner};
use crate::{TaskManager, ToolResult};

pub fn run_in_background(
    runner: Arc<dyn SubAgentRunner>,
    task_manager: &TaskManager,
    request: SubAgentRequest,
    description: &str,
    agent_type: &str,
) -> ToolResult {
    let task_id = task_manager.register(description, agent_type);
    let background_task_id = task_id.clone();
    let background_manager = task_manager.clone();
    thread::spawn(move || match runner.run(request) {
        Ok(result) => {
            background_manager.update_progress(
                &background_task_id,
                result.progress.tool_use_count,
                result.progress.token_count,
            );
            background_manager.complete(&background_task_id, &result.output);
        }
        Err(error) => {
            background_manager.fail(&background_task_id, &error);
        }
    });
    ToolResult::success(format!(
        "Background agent launched (task_id: {task_id}, type: {agent_type})"
    ))
}

pub fn run_in_foreground(runner: &dyn SubAgentRunner, request: SubAgentRequest) -> ToolResult {
    match runner.run(request) {
        Ok(result) => ToolResult::success(format!(
            "{}\n\n[Agent stats: {} tool calls, {} tokens]",
            result.output, result.progress.tool_use_count, result.progress.token_count
        ))
        .with_stream_events(
            result
                .stream_events
                .into_iter()
                .map(StreamEvent::SubAgentTool)
                .collect(),
        ),
        Err(error) => ToolResult::error(format!("Sub-agent failed: {error}")),
    }
}
