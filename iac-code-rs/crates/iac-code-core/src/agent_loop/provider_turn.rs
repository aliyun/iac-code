use super::AgentLoop;
use iac_code_protocol::{json::JsonValue, StreamEvent, ToolUseEndEvent};
use iac_code_providers::EventProvider;
use iac_code_tools::ToolExecutor;

#[derive(Default)]
pub(super) struct ProviderTurn {
    pub(super) events: Vec<StreamEvent>,
    pub(super) text_chunks: Vec<String>,
    pub(super) thinking_chunks: Vec<String>,
    pub(super) completed_tool_uses: Vec<ToolUseEndEvent>,
    pub(super) message_ended: bool,
}

#[derive(Default)]
struct PendingToolUse {
    tool_use_id: String,
    name: Option<String>,
    input: Option<JsonValue>,
}

impl<P, T> AgentLoop<P, T>
where
    P: EventProvider,
    T: ToolExecutor,
{
    pub(super) fn run_provider_turn(&mut self, sink: &mut dyn FnMut(&StreamEvent)) -> ProviderTurn {
        let mut turn = ProviderTurn::default();
        let mut pending_tool_uses = Vec::<PendingToolUse>::new();
        let tool_definitions = self.tool_executor.tool_definitions();
        self.context_manager
            .set_tool_definitions(tool_definitions.clone());

        for event in self.provider.stream_events_with_sink(
            self.context_manager.conversation(),
            &self.system_prompt,
            &tool_definitions,
            self.max_turns,
            sink,
        ) {
            match &event {
                StreamEvent::TextDelta(text) => turn.text_chunks.push(text.text.clone()),
                StreamEvent::ThinkingDelta(thinking) => {
                    turn.thinking_chunks.push(thinking.text.clone());
                }
                StreamEvent::ToolUseStart(tool_use) => {
                    let pending =
                        pending_tool_use_mut(&mut pending_tool_uses, &tool_use.tool_use_id);
                    pending.name = Some(tool_use.name.clone());
                }
                StreamEvent::ToolUseEnd(tool_use) => {
                    let pending =
                        pending_tool_use_mut(&mut pending_tool_uses, &tool_use.tool_use_id);
                    pending.input = Some(tool_use.input.clone());
                }
                StreamEvent::Tombstone(_) => {
                    turn.text_chunks.clear();
                    turn.thinking_chunks.clear();
                    pending_tool_uses.clear();
                }
                StreamEvent::MessageEnd(_) => turn.message_ended = true,
                _ => {}
            }

            turn.events.push(event);
        }

        turn.completed_tool_uses = pending_tool_uses
            .into_iter()
            .filter_map(|pending| {
                Some(ToolUseEndEvent {
                    tool_use_id: pending.tool_use_id,
                    name: pending.name?,
                    input: pending.input?,
                })
            })
            .collect();

        turn
    }
}

fn pending_tool_use_mut<'a>(
    pending_tool_uses: &'a mut Vec<PendingToolUse>,
    tool_use_id: &str,
) -> &'a mut PendingToolUse {
    if let Some(index) = pending_tool_uses
        .iter()
        .position(|pending| pending.tool_use_id == tool_use_id)
    {
        return &mut pending_tool_uses[index];
    }
    pending_tool_uses.push(PendingToolUse {
        tool_use_id: tool_use_id.to_owned(),
        ..PendingToolUse::default()
    });
    pending_tool_uses
        .last_mut()
        .expect("pending tool use was just pushed")
}
