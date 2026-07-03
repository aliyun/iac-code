use super::AgentLoop;
use iac_code_protocol::message::{AgentMessageContent, ToolResultBlock};
use iac_code_protocol::{
    json::JsonValue, StreamEvent, SubAgentToolEvent, ToolResultEvent, ToolUseEndEvent,
};
use iac_code_providers::EventProvider;
use iac_code_tools::{ToolCallRequest, ToolExecutor, ToolResult};

#[derive(Clone, Debug, Default, PartialEq)]
pub(super) struct ToolExecution {
    pub(super) events: Vec<StreamEvent>,
    pub(super) cancelled: bool,
}

impl<P, T> AgentLoop<P, T>
where
    P: EventProvider,
    T: ToolExecutor,
{
    pub(super) fn execute_tool_uses(
        &mut self,
        completed_tool_uses: &[ToolUseEndEvent],
    ) -> ToolExecution {
        let mut events = Vec::with_capacity(completed_tool_uses.len());
        let mut result_blocks = Vec::with_capacity(completed_tool_uses.len());
        let mut new_messages = Vec::new();
        let mut context_modifiers = Vec::new();
        let mut cancelled = false;

        let requests = completed_tool_uses
            .iter()
            .map(|tool_use| ToolCallRequest {
                tool_use_id: tool_use.tool_use_id.clone(),
                tool_name: tool_use.name.clone(),
                input: tool_use.input.clone(),
            })
            .collect::<Vec<_>>();
        let mut results = self.tool_executor.execute_batch(&requests).into_iter();

        for tool_use in completed_tool_uses {
            let result = results.next().unwrap_or_else(|| {
                ToolResult::error(format!(
                    "Tool executor did not return a result for '{}'",
                    tool_use.name
                ))
            });
            events.extend(
                result
                    .stream_events
                    .iter()
                    .map(|event| parent_scoped_tool_stream_event(event, &tool_use.tool_use_id)),
            );
            let processed_content =
                self.process_tool_result_content(&tool_use.tool_use_id, &result.content);
            events.push(StreamEvent::ToolResult(ToolResultEvent {
                tool_use_id: tool_use.tool_use_id.clone(),
                tool_name: tool_use.name.clone(),
                result: processed_content.clone(),
                is_error: result.is_error,
            }));
            cancelled |= result.cancelled;
            result_blocks.push(ToolResultBlock {
                tool_use_id: tool_use.tool_use_id.clone(),
                content: processed_content,
                is_error: result.is_error,
            });
            new_messages.extend(result.new_messages);
            if let Some(modifier) = result.context_modifier {
                context_modifiers.push(modifier);
            }
        }

        if !result_blocks.is_empty() {
            self.context_manager.add_tool_results(result_blocks);
        }
        self.inject_new_messages(new_messages);
        for modifier in &context_modifiers {
            self.tool_executor.apply_context_modifier(modifier);
        }

        ToolExecution { events, cancelled }
    }

    fn process_tool_result_content(&self, tool_use_id: &str, content: &str) -> String {
        self.result_storage.as_ref().map_or_else(
            || content.to_owned(),
            |storage| storage.process(tool_use_id, content),
        )
    }

    fn inject_new_messages(&mut self, new_messages: Vec<JsonValue>) {
        for message in new_messages {
            let Some((role, content)) = text_message_from_json(&message) else {
                continue;
            };
            match role {
                "user" => {
                    self.context_manager
                        .add_user_message(AgentMessageContent::Text(content.to_owned()));
                }
                "assistant" => {
                    self.context_manager
                        .add_assistant_message(AgentMessageContent::Text(content.to_owned()));
                }
                _ => {}
            }
        }
    }
}

fn parent_scoped_tool_stream_event(event: &StreamEvent, parent_tool_use_id: &str) -> StreamEvent {
    match event {
        StreamEvent::SubAgentTool(SubAgentToolEvent {
            child_tool_name,
            child_tool_input,
            is_done,
            is_error,
            ..
        }) => StreamEvent::SubAgentTool(SubAgentToolEvent {
            parent_tool_use_id: parent_tool_use_id.to_owned(),
            child_tool_name: child_tool_name.clone(),
            child_tool_input: child_tool_input.clone(),
            is_done: *is_done,
            is_error: *is_error,
        }),
        _ => event.clone(),
    }
}

fn text_message_from_json(message: &JsonValue) -> Option<(&str, &str)> {
    let JsonValue::Object(fields) = message else {
        return None;
    };
    let Some(JsonValue::String(role)) = fields.get("role") else {
        return None;
    };
    let Some(JsonValue::String(content)) = fields.get("content") else {
        return None;
    };
    Some((role.as_str(), content.as_str()))
}
