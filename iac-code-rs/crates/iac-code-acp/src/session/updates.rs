use std::collections::{BTreeMap, BTreeSet};

use iac_code_protocol::json::{self, JsonValue};
use iac_code_protocol::{MessageEndEvent, StreamEvent, Usage};

use crate::convert::{AcpEventConverter, SessionUpdate};

use super::model::AcpClient;

#[derive(Default)]
pub(super) struct PromptStreamSummary {
    pub(super) usage: Option<Usage>,
    pub(super) stop_reason: Option<String>,
}

pub(super) fn emit_stream_updates(
    session_id: &str,
    turn_id: &str,
    events: Vec<StreamEvent>,
    terminal_tool_names: BTreeSet<String>,
    client: &mut dyn AcpClient,
) -> PromptStreamSummary {
    let mut converter = AcpEventConverter::new(turn_id).with_terminal_tools(terminal_tool_names);
    let mut summary = PromptStreamSummary::default();

    for event in events {
        if let StreamEvent::MessageEnd(MessageEndEvent { stop_reason, usage }) = &event {
            summary.usage = Some(usage.clone());
            summary.stop_reason = Some(stop_reason.clone());
        }
        for update in converter.event_to_updates(&event) {
            send_session_update(client, session_id, update);
        }
    }

    summary
}

pub(super) fn response_meta(
    usage: Option<&Usage>,
    elapsed_ms: u128,
) -> BTreeMap<String, JsonValue> {
    let mut meta = BTreeMap::new();
    meta.insert(
        "timing".to_owned(),
        json::object([("elapsed_ms", json::number(elapsed_ms as u64))]),
    );
    if let Some(usage) = usage {
        meta.insert(
            "usage".to_owned(),
            json::object([
                ("input_tokens", json::number(usage.input_tokens)),
                ("output_tokens", json::number(usage.output_tokens)),
                ("total_tokens", json::number(usage.total_tokens())),
            ]),
        );
    }
    meta
}

fn send_session_update(client: &mut dyn AcpClient, session_id: &str, update: SessionUpdate) {
    client.session_update(session_id, update);
}
