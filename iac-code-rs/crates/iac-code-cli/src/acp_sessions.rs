use std::collections::BTreeSet;

use iac_code_acp::convert::history_message_to_updates;
use iac_code_acp::session::{convert_mcp_server_configs, AcpMcpServerConfig};
use iac_code_config::paths::ConfigPaths;
use iac_code_core::{SessionIndex, SessionStorage};
use iac_code_protocol::message::Conversation;
use iac_code_protocol::{json, json::JsonValue};

use crate::acp_payload::{
    acp_available_commands_message, acp_model_state_json, acp_session_update_message,
};
use crate::acp_resume::{
    acp_cross_project_resume_error, acp_invalid_params_field_error,
    acp_resume_ambiguous_name_error, acp_resume_name_candidates, acp_session_not_found_error,
};
use crate::acp_server::AcpServerRuntime;
use crate::json_utils::{json_object_field, json_string_field};
use crate::jsonrpc_payload::{empty_json_object, jsonrpc_error, jsonrpc_result};
use crate::session_utils::{
    current_working_directory, resolve_session_argument, same_project_path,
};

pub(super) fn handle_acp_new_session(
    request_id: JsonValue,
    params: Option<&JsonValue>,
    runtime: &mut AcpServerRuntime,
) -> Vec<JsonValue> {
    let cwd = params
        .and_then(|value| json_string_field(value, "cwd"))
        .map(ToOwned::to_owned)
        .unwrap_or_else(|| current_working_directory().unwrap_or_else(|_| ".".to_owned()));
    let mcp_configs = acp_mcp_configs_param(params);
    let session_id = runtime.create_session_with_mcp(cwd, mcp_configs);
    let mut messages = vec![acp_available_commands_message(&session_id)];
    messages.push(jsonrpc_result(
        request_id,
        json::object([
            ("sessionId", json::string(session_id)),
            ("models", acp_model_state_json()),
        ]),
    ));
    messages
}

fn acp_mcp_configs_param(params: Option<&JsonValue>) -> Vec<AcpMcpServerConfig> {
    let Some(params) = params else {
        return Vec::new();
    };
    let value = json_object_field(params, "mcpServers")
        .or_else(|| json_object_field(params, "mcp_servers"));
    convert_mcp_server_configs(value)
}

pub(super) fn handle_acp_load_session(
    request_id: JsonValue,
    params: Option<&JsonValue>,
    runtime: &mut AcpServerRuntime,
) -> Vec<JsonValue> {
    let Some(params) = params else {
        return vec![jsonrpc_error(request_id, -32602, "Missing params")];
    };
    let Some(cwd) = json_string_field(params, "cwd") else {
        return vec![jsonrpc_error(request_id, -32602, "Missing cwd")];
    };
    let Some(session_id) = json_string_field(params, "sessionId") else {
        return vec![jsonrpc_error(request_id, -32602, "Missing sessionId")];
    };

    if runtime.sessions.contains_key(session_id) {
        return vec![jsonrpc_result(
            request_id,
            json::object([("models", acp_model_state_json())]),
        )];
    }

    let paths = match ConfigPaths::from_env() {
        Ok(paths) => paths,
        Err(error) => return vec![jsonrpc_error(request_id, -32603, &error.to_string())],
    };
    let storage = match SessionStorage::new(paths.subdirs().projects) {
        Ok(storage) => storage,
        Err(error) => return vec![jsonrpc_error(request_id, -32603, &error.to_string())],
    };
    if !storage.exists(cwd, session_id) {
        return vec![acp_invalid_params_field_error(
            request_id,
            "session_id",
            "Session not found",
        )];
    }

    let history = match storage.load(cwd, session_id) {
        Ok(history) => SessionStorage::repair_interrupted(&history),
        Err(error) => return vec![jsonrpc_error(request_id, -32603, &error.to_string())],
    };
    let title = storage
        .read_metadata(cwd, session_id)
        .and_then(|metadata| metadata.name);
    let conversation = Conversation {
        messages: history.clone(),
    };
    let mcp_configs = acp_mcp_configs_param(Some(params));
    runtime.insert_session_with_mcp(
        session_id.to_owned(),
        cwd.to_owned(),
        conversation,
        title,
        mcp_configs,
    );

    let mut messages = history
        .iter()
        .flat_map(history_message_to_updates)
        .map(|update| acp_session_update_message(session_id, &update))
        .collect::<Vec<_>>();
    messages.push(acp_available_commands_message(session_id));
    messages.push(jsonrpc_result(
        request_id,
        json::object([("models", acp_model_state_json())]),
    ));
    messages
}

pub(super) fn handle_acp_fork_session(
    request_id: JsonValue,
    params: Option<&JsonValue>,
    runtime: &mut AcpServerRuntime,
) -> Vec<JsonValue> {
    let Some(params) = params else {
        return vec![jsonrpc_error(request_id, -32602, "Missing params")];
    };
    let Some(source_session_id) = json_string_field(params, "sessionId") else {
        return vec![jsonrpc_error(request_id, -32602, "Missing sessionId")];
    };

    let (cwd, history, title) = if let Some(source) = runtime.sessions.get(source_session_id) {
        (
            json_string_field(params, "cwd")
                .unwrap_or(&source.agent().cwd)
                .to_owned(),
            source.agent().conversation.messages.clone(),
            source.agent().title.clone(),
        )
    } else {
        let Some(cwd) = json_string_field(params, "cwd") else {
            return vec![jsonrpc_error(request_id, -32602, "Missing cwd")];
        };
        let paths = match ConfigPaths::from_env() {
            Ok(paths) => paths,
            Err(error) => return vec![jsonrpc_error(request_id, -32603, &error.to_string())],
        };
        let storage = match SessionStorage::new(paths.subdirs().projects) {
            Ok(storage) => storage,
            Err(error) => return vec![jsonrpc_error(request_id, -32603, &error.to_string())],
        };
        if !storage.exists(cwd, source_session_id) {
            return vec![acp_invalid_params_field_error(
                request_id,
                "session_id",
                "Source session not found",
            )];
        }
        let history = match storage.load(cwd, source_session_id) {
            Ok(history) => SessionStorage::repair_interrupted(&history),
            Err(error) => return vec![jsonrpc_error(request_id, -32603, &error.to_string())],
        };
        let title = storage
            .read_metadata(cwd, source_session_id)
            .and_then(|metadata| metadata.name);
        (cwd.to_owned(), history, title)
    };

    let mcp_configs = acp_mcp_configs_param(Some(params));
    let fork_session_id = runtime.create_session_with_state_and_mcp(
        cwd,
        Conversation {
            messages: history.clone(),
        },
        title,
        mcp_configs,
    );
    let mut messages = history
        .iter()
        .flat_map(history_message_to_updates)
        .map(|update| acp_session_update_message(&fork_session_id, &update))
        .collect::<Vec<_>>();
    messages.push(acp_available_commands_message(&fork_session_id));
    messages.push(jsonrpc_result(
        request_id,
        json::object([
            ("sessionId", json::string(&fork_session_id)),
            ("models", acp_model_state_json()),
        ]),
    ));
    messages
}

pub(super) fn handle_acp_resume_session(
    request_id: JsonValue,
    params: Option<&JsonValue>,
    runtime: &mut AcpServerRuntime,
) -> Vec<JsonValue> {
    let Some(params) = params else {
        return vec![jsonrpc_error(request_id, -32602, "Missing params")];
    };
    let Some(session_id) = json_string_field(params, "sessionId") else {
        return vec![jsonrpc_error(request_id, -32602, "Missing sessionId")];
    };
    let Some(cwd) = json_string_field(params, "cwd") else {
        return vec![jsonrpc_error(request_id, -32602, "Missing cwd")];
    };

    if let Some(session) = runtime.sessions.get(session_id) {
        if !same_project_path(&session.agent().cwd, cwd) {
            return vec![acp_cross_project_resume_error(
                request_id,
                session_id,
                session_id,
                &session.agent().cwd,
            )];
        }
        return vec![
            acp_available_commands_message(session_id),
            jsonrpc_result(request_id, empty_json_object()),
        ];
    }

    let paths = match ConfigPaths::from_env() {
        Ok(paths) => paths,
        Err(error) => return vec![jsonrpc_error(request_id, -32603, &error.to_string())],
    };
    let storage = match SessionStorage::new(paths.subdirs().projects) {
        Ok(storage) => storage,
        Err(error) => return vec![jsonrpc_error(request_id, -32603, &error.to_string())],
    };
    let index = SessionIndex::new(storage.projects_dir());
    let trimmed_session_id = session_id.trim();
    let entry = match resolve_session_argument(&index, cwd, trimmed_session_id) {
        Ok(entry) => entry,
        Err(error) => {
            if error == format!("Multiple sessions match: {trimmed_session_id}") {
                let candidates = match acp_resume_name_candidates(&index, trimmed_session_id) {
                    Ok(candidates) => candidates,
                    Err(error) => {
                        return vec![jsonrpc_error(request_id, -32603, &error.to_string())];
                    }
                };
                return vec![acp_resume_ambiguous_name_error(
                    request_id,
                    trimmed_session_id,
                    &candidates,
                )];
            }
            if error == format!("Session not found: {trimmed_session_id}") {
                return vec![acp_session_not_found_error(request_id, session_id)];
            }
            return vec![jsonrpc_error(request_id, -32602, &error)];
        }
    };
    if !entry.cwd.is_empty() && !same_project_path(&entry.cwd, cwd) {
        return vec![acp_cross_project_resume_error(
            request_id,
            session_id,
            &entry.session_id,
            &entry.cwd,
        )];
    }
    let resolved_session_id = entry.session_id;
    let storage_cwd = if entry.cwd.is_empty() {
        cwd.to_owned()
    } else {
        entry.cwd
    };

    if let Some(session) = runtime.sessions.get(&resolved_session_id) {
        if !same_project_path(&session.agent().cwd, cwd) {
            return vec![acp_cross_project_resume_error(
                request_id,
                session_id,
                &resolved_session_id,
                &session.agent().cwd,
            )];
        }
        return vec![
            acp_available_commands_message(&resolved_session_id),
            jsonrpc_result(request_id, empty_json_object()),
        ];
    }

    if !storage.exists(&storage_cwd, &resolved_session_id) {
        return vec![acp_session_not_found_error(request_id, session_id)];
    }

    let history = match storage.load(&storage_cwd, &resolved_session_id) {
        Ok(history) => SessionStorage::repair_interrupted(&history),
        Err(error) => return vec![jsonrpc_error(request_id, -32603, &error.to_string())],
    };
    let title = storage
        .read_metadata(&storage_cwd, &resolved_session_id)
        .and_then(|metadata| metadata.name);
    let mcp_configs = acp_mcp_configs_param(Some(params));
    runtime.insert_session_with_mcp(
        resolved_session_id.clone(),
        storage_cwd,
        Conversation { messages: history },
        title,
        mcp_configs,
    );

    vec![
        acp_available_commands_message(&resolved_session_id),
        jsonrpc_result(request_id, empty_json_object()),
    ]
}

pub(super) fn acp_session_list_response(
    runtime: &AcpServerRuntime,
    params: Option<&JsonValue>,
) -> JsonValue {
    let requested_cwd = params.and_then(|value| json_string_field(value, "cwd"));
    let mut seen_session_ids = BTreeSet::new();
    let mut sessions = Vec::new();

    if let Ok(paths) = ConfigPaths::from_env() {
        let index = SessionIndex::new(paths.subdirs().projects);
        let entries = match requested_cwd {
            Some(cwd) => index.list_for_cwd(cwd),
            None => index.list_all_projects(),
        };
        if let Ok(entries) = entries {
            for entry in entries {
                seen_session_ids.insert(entry.session_id.clone());
                sessions.push(json::object([
                    ("sessionId", json::string(entry.session_id)),
                    ("cwd", json::string(entry.cwd)),
                    ("title", json::string(entry.title)),
                ]));
            }
        }
    }

    for (session_id, session) in &runtime.sessions {
        if seen_session_ids.contains(session_id) {
            continue;
        }
        if requested_cwd.is_some_and(|cwd| cwd != session.agent().cwd) {
            continue;
        }
        let mut fields = vec![
            ("sessionId", json::string(session_id)),
            ("cwd", json::string(&session.agent().cwd)),
        ];
        if let Some(title) = &session.agent().title {
            fields.push(("title", json::string(title)));
        }
        sessions.push(json::object(fields));
    }

    json::object([
        ("sessions", json::array(sessions)),
        ("nextCursor", json::null()),
    ])
}
