use std::collections::BTreeMap;
use std::sync::atomic::{AtomicU64, Ordering};
use std::time::Instant;

use crate::convert::{
    acp_blocks_to_agent_message_content, acp_blocks_to_prompt_text, AcpContentBlock,
};
use crate::state::TurnState;

use super::model::{AcpAgent, AcpClient, AcpError, PromptResponse};
use super::permission::PermissionRequester;
use super::slash::dispatch_slash_command;
use super::state::AcpSession;
use super::updates::{emit_stream_updates, response_meta};

static TURN_COUNTER: AtomicU64 = AtomicU64::new(1);

impl<A> AcpSession<A>
where
    A: AcpAgent,
{
    pub fn prompt(
        &mut self,
        prompt: Vec<AcpContentBlock>,
        client: &mut dyn AcpClient,
    ) -> Result<PromptResponse, AcpError> {
        if !self.is_open() {
            return Err(AcpError::internal("Session is closed"));
        }
        self.touch();

        let prompt_text = acp_blocks_to_prompt_text(&prompt);
        if dispatch_slash_command(&self.id, &prompt_text, &mut self.agent, client) {
            return Ok(PromptResponse::end_turn(BTreeMap::new()));
        }

        let prompt_start = Instant::now();
        let turn_id = next_turn_id();
        self.current_turn = Some(TurnState::new(&turn_id));

        let session_id = self.id.clone();
        let events = {
            let mut permission_requester = PermissionRequester {
                session_id: &session_id,
                client,
                cache: &mut self.permission_cache,
                permission_context: &mut self.permission_context,
                blanket_allow_disabled_tools: &self.blanket_allow_disabled_tools,
            };

            let prompt_content = acp_blocks_to_agent_message_content(&prompt);
            self.agent
                .run_streaming_content(prompt_content, &prompt_text, &mut |event| {
                    permission_requester.request(event)
                })
        };

        let summary = emit_stream_updates(
            &self.id,
            &turn_id,
            events,
            self.terminal_tool_names.clone(),
            client,
        );

        self.touch();
        if summary.stop_reason.as_deref() == Some("cancelled") {
            return Ok(PromptResponse::cancelled());
        }
        Ok(PromptResponse::end_turn(response_meta(
            summary.usage.as_ref(),
            prompt_start.elapsed().as_millis(),
        )))
    }
}

fn next_turn_id() -> String {
    format!("turn-{}", TURN_COUNTER.fetch_add(1, Ordering::Relaxed))
}
