use crate::ToolResult;

use super::status::{is_action_success, StackStatus};

pub(crate) fn final_result(
    action: &str,
    status: StackStatus,
    resources: Vec<serde_json::Value>,
    elapsed_seconds: u64,
) -> ToolResult {
    let is_success = is_action_success(action, &status.status);
    let content = serde_json::json!({
        "stack_id": status.stack_id,
        "stack_name": status.stack_name,
        "status": status.status,
        "status_reason": status.status_reason,
        "progress_percentage": status.progress_percentage,
        "elapsed_seconds": elapsed_seconds,
        "is_success": is_success,
        "resources": resources,
    });
    let text = serde_json::to_string_pretty(&content).unwrap_or_else(|_| content.to_string());
    if is_success {
        ToolResult::success(text)
    } else {
        ToolResult::error(text)
    }
}

pub(crate) fn clean_error(message: &str) -> String {
    message
        .find(" Response: {")
        .map(|index| message[..index].trim().to_owned())
        .unwrap_or_else(|| message.trim().to_owned())
}
