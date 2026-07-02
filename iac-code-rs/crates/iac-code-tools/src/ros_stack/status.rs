pub(crate) const CREATE_TERMINAL_STATUSES: &[&str] = &[
    "CREATE_COMPLETE",
    "CREATE_FAILED",
    "CREATE_ROLLBACK_COMPLETE",
    "CREATE_ROLLBACK_FAILED",
    "IMPORT_CREATE_COMPLETE",
    "IMPORT_CREATE_FAILED",
    "IMPORT_CREATE_ROLLBACK_COMPLETE",
    "IMPORT_CREATE_ROLLBACK_FAILED",
];

pub(crate) const UPDATE_TERMINAL_STATUSES: &[&str] = &[
    "UPDATE_COMPLETE",
    "UPDATE_FAILED",
    "ROLLBACK_COMPLETE",
    "ROLLBACK_FAILED",
    "IMPORT_UPDATE_COMPLETE",
    "IMPORT_UPDATE_FAILED",
    "IMPORT_UPDATE_ROLLBACK_COMPLETE",
    "IMPORT_UPDATE_ROLLBACK_FAILED",
];

pub(crate) const DELETE_TERMINAL_STATUSES: &[&str] = &["DELETE_COMPLETE", "DELETE_FAILED"];

#[derive(Clone, Debug)]
pub(crate) struct StackStatus {
    pub(crate) stack_id: String,
    pub(crate) stack_name: String,
    pub(crate) status: String,
    pub(crate) status_reason: String,
    pub(crate) progress_percentage: f64,
}

pub(crate) fn is_action_terminal(action: &str, status: &str) -> bool {
    match action {
        "CreateStack" | "ContinueCreateStack" => CREATE_TERMINAL_STATUSES.contains(&status),
        "UpdateStack" => UPDATE_TERMINAL_STATUSES.contains(&status),
        "DeleteStack" => DELETE_TERMINAL_STATUSES.contains(&status),
        _ => status.ends_with("_COMPLETE") || status.ends_with("_FAILED"),
    }
}

pub(crate) fn is_action_success(action: &str, status: &str) -> bool {
    if action == "DeleteStack" {
        return status == "DELETE_COMPLETE";
    }
    status.ends_with("_COMPLETE") && !status.contains("ROLLBACK")
}

pub(crate) fn json_string(value: &serde_json::Value, field: &str) -> Option<String> {
    value
        .get(field)
        .and_then(serde_json::Value::as_str)
        .map(str::to_owned)
}

pub(crate) fn json_f64(value: &serde_json::Value, field: &str) -> Option<f64> {
    value.get(field).and_then(|value| {
        value
            .as_f64()
            .or_else(|| value.as_str().and_then(|text| text.parse::<f64>().ok()))
    })
}
