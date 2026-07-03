use iac_code_protocol::{StackInstancesProgressEvent, StreamEvent};

use crate::ToolResult;

use super::status::StackInstanceStatus;

pub(crate) fn final_result(
    stack_group_name: String,
    operation_id: String,
    status: String,
    progress_percentage: u64,
    instances: &[StackInstanceStatus],
    elapsed_seconds: u64,
) -> ToolResult {
    let is_success = status == "SUCCEEDED";
    let content = serde_json::json!({
        "stack_group_name": stack_group_name,
        "operation_id": operation_id,
        "status": status,
        "progress_percentage": progress_percentage,
        "elapsed_seconds": elapsed_seconds,
        "is_success": is_success,
    });
    let text = serde_json::to_string_pretty(&content).unwrap_or_else(|_| content.to_string());
    let event = StreamEvent::StackInstancesProgress(StackInstancesProgressEvent {
        stack_group_name,
        operation_id,
        status,
        progress_percentage,
        instances: instances
            .iter()
            .map(StackInstanceStatus::to_json_value)
            .collect(),
        elapsed_seconds,
    });
    if is_success {
        ToolResult::success(text).with_stream_events(vec![event])
    } else {
        ToolResult::error(text).with_stream_events(vec![event])
    }
}

pub(crate) fn clean_error(message: &str) -> String {
    message
        .find(" Response: {")
        .map(|index| message[..index].trim().to_owned())
        .unwrap_or_else(|| message.trim().to_owned())
}
