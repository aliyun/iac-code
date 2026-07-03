use iac_code_protocol::json::JsonValue;

use super::{iac_metadata, status_update, A2AExposureType, PublishOptions, PublishResult};

pub(super) fn tool_trace_event(
    task_id: &str,
    context_id: &str,
    options: &PublishOptions,
    tool_metadata: JsonValue,
) -> PublishResult {
    if !options.exposure_types.contains(&A2AExposureType::ToolTrace) {
        return PublishResult::default();
    }
    PublishResult {
        events: vec![status_update(
            task_id,
            context_id,
            "TASK_STATE_WORKING",
            None,
            iac_metadata("tool", tool_metadata),
        )],
        text: None,
        permission_decision: None,
    }
}
