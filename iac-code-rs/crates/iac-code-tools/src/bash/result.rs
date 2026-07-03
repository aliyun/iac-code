use iac_code_protocol::permission::{PermissionDecisionReason, PermissionResult};

pub(super) fn result_with_reason(
    behavior: impl Into<String>,
    reason_type: impl Into<String>,
    detail: String,
) -> PermissionResult {
    PermissionResult {
        behavior: behavior.into(),
        message: detail.clone(),
        reason: Some(PermissionDecisionReason {
            type_name: reason_type.into(),
            detail,
        }),
        suggestions: None,
    }
}

pub(super) fn ask_with_reason(reason_type: &str, detail: &str) -> PermissionResult {
    result_with_reason("ask", reason_type, detail.into())
}
