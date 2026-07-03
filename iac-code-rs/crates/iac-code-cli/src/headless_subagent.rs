use std::collections::BTreeMap;

use iac_code_exec::HeadlessRunResult;
use iac_code_protocol::json::JsonValue;
use iac_code_protocol::message::{AgentContentBlock, AgentMessageContent, Conversation};
use iac_code_protocol::{StreamEvent, SubAgentToolEvent};

pub(super) fn sub_agent_error_detail(result: &HeadlessRunResult) -> Option<String> {
    if matches!(
        result.exit_code,
        iac_code_exec::EXIT_OK | iac_code_exec::EXIT_MAX_TURNS
    ) {
        return None;
    }

    Some(if result.stderr.trim().is_empty() {
        result.stdout.trim().to_owned()
    } else {
        result.stderr.trim().to_owned()
    })
}

pub(super) fn sub_agent_tool_events_from_child_events(
    events: &[StreamEvent],
) -> Vec<SubAgentToolEvent> {
    let mut tool_inputs: BTreeMap<String, JsonValue> = BTreeMap::new();
    let mut sub_agent_events = Vec::new();

    for event in events {
        match event {
            StreamEvent::ToolUseEnd(tool_use) => {
                tool_inputs.insert(tool_use.tool_use_id.clone(), tool_use.input.clone());
                sub_agent_events.push(SubAgentToolEvent {
                    parent_tool_use_id: String::new(),
                    child_tool_name: tool_use.name.clone(),
                    child_tool_input: tool_use.input.clone(),
                    is_done: false,
                    is_error: false,
                });
            }
            StreamEvent::ToolResult(tool_result) => {
                let child_tool_input = tool_inputs
                    .remove(&tool_result.tool_use_id)
                    .unwrap_or_else(|| JsonValue::Object(BTreeMap::new()));
                sub_agent_events.push(SubAgentToolEvent {
                    parent_tool_use_id: String::new(),
                    child_tool_name: tool_result.tool_name.clone(),
                    child_tool_input,
                    is_done: true,
                    is_error: tool_result.is_error,
                });
            }
            _ => {}
        }
    }

    sub_agent_events
}

pub(super) fn truncate_sub_agent_output(output: &str) -> String {
    let words = output.split_whitespace().collect::<Vec<_>>();
    if words.len() <= 500 {
        return output.to_owned();
    }

    format!("{}\n\n[... truncated to 500 words]", words[..500].join(" "))
}

pub(super) fn count_tool_result_blocks(conversation: &Conversation) -> u32 {
    conversation
        .messages
        .iter()
        .map(|message| match &message.content {
            AgentMessageContent::Blocks(blocks) => blocks
                .iter()
                .filter(|block| matches!(block, AgentContentBlock::ToolResult(_)))
                .count() as u32,
            AgentMessageContent::Text(_) => 0,
        })
        .sum()
}
