use std::io;

use iac_code_core::{SessionEntry, SessionIndex};
use iac_code_protocol::{json, json::JsonValue};

use crate::cli_i18n::{tr, tr_value};
use crate::jsonrpc_payload::jsonrpc_error_with_data;
use crate::session_utils::format_resume_command;

pub(super) fn acp_session_not_found_error(request_id: JsonValue, session_id: &str) -> JsonValue {
    jsonrpc_error_with_data(
        request_id,
        -32602,
        &tr("Session not found"),
        json::object([("session_id", json::string(session_id))]),
    )
}

pub(super) fn acp_invalid_params_field_error(
    request_id: JsonValue,
    field: &str,
    message: &str,
) -> JsonValue {
    jsonrpc_error_with_data(
        request_id,
        -32602,
        "Invalid params",
        json::object([(field, json::string(message))]),
    )
}

pub(super) fn acp_cross_project_resume_error(
    request_id: JsonValue,
    session_id: &str,
    resolved_session_id: &str,
    cwd: &str,
) -> JsonValue {
    let hint = format_resume_command(cwd, resolved_session_id);
    jsonrpc_error_with_data(
        request_id,
        -32602,
        &tr_value(
            "Session belongs to another project. Run: {hint}",
            "hint",
            &hint,
        ),
        json::object([
            ("session_id", json::string(session_id)),
            ("resolved_session_id", json::string(resolved_session_id)),
            ("cwd", json::string(cwd)),
            ("hint", json::string(hint)),
        ]),
    )
}

pub(super) fn acp_resume_name_candidates(
    index: &SessionIndex,
    name: &str,
) -> io::Result<Vec<SessionEntry>> {
    Ok(index
        .list_all_projects()?
        .into_iter()
        .filter(|entry| entry.name.as_deref() == Some(name))
        .collect())
}

pub(super) fn acp_resume_ambiguous_name_error(
    request_id: JsonValue,
    session_id: &str,
    candidates: &[SessionEntry],
) -> JsonValue {
    let candidate_ids = candidates
        .iter()
        .map(|entry| entry.session_id.as_str())
        .collect::<Vec<_>>()
        .join(", ");
    jsonrpc_error_with_data(
        request_id,
        -32602,
        &format!("Session name is ambiguous. Candidates: {candidate_ids}"),
        json::object([
            ("session_id", json::string(session_id)),
            (
                "candidates",
                json::array(candidates.iter().map(acp_resume_candidate_data)),
            ),
        ]),
    )
}

fn acp_resume_candidate_data(entry: &SessionEntry) -> JsonValue {
    json::object([
        ("session_id", json::string(&entry.session_id)),
        (
            "name",
            entry
                .name
                .as_deref()
                .map(json::string)
                .unwrap_or_else(json::null),
        ),
        ("cwd", json::string(&entry.cwd)),
        (
            "command",
            json::string(format_resume_command(&entry.cwd, &entry.session_id)),
        ),
    ])
}
