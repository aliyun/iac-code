use std::collections::BTreeMap;

use iac_code_acp::session::{AcpMcpServerConfig, AcpSession};
use iac_code_protocol::message::Conversation;
use iac_code_protocol::{json, json::JsonValue};

use super::acp_agent::AcpHeadlessAgent;
use super::acp_payload::{acp_initialize_response, acp_prompt_blocks, acp_prompt_response_json};
use super::acp_resume::acp_invalid_params_field_error;
use super::acp_sessions::{
    acp_session_list_response, handle_acp_fork_session, handle_acp_load_session,
    handle_acp_new_session, handle_acp_resume_session,
};
use super::acp_stdio_client::StdioAcpClient;
use super::json_utils::{json_object_field, json_string_field, json_string_value};
use super::jsonrpc_payload::{empty_json_object, jsonrpc_error, jsonrpc_result};
use crate::cli_runtime::VERSION;

pub(super) struct AcpServerRuntime {
    pub(super) sessions: BTreeMap<String, AcpSession<AcpHeadlessAgent>>,
    next_session_id: u64,
}

impl AcpServerRuntime {
    pub(super) fn new() -> Self {
        Self {
            sessions: BTreeMap::new(),
            next_session_id: 1,
        }
    }

    pub(super) fn create_session_with_mcp(
        &mut self,
        cwd: String,
        mcp_configs: Vec<AcpMcpServerConfig>,
    ) -> String {
        self.create_session_with_state_and_mcp(cwd, Conversation::default(), None, mcp_configs)
    }

    pub(super) fn create_session_with_state_and_mcp(
        &mut self,
        cwd: String,
        conversation: Conversation,
        title: Option<String>,
        mcp_configs: Vec<AcpMcpServerConfig>,
    ) -> String {
        loop {
            let session_id = format!("acp-session-{}", self.next_session_id);
            self.next_session_id = self.next_session_id.saturating_add(1);
            if !self.sessions.contains_key(&session_id) {
                self.insert_session_with_mcp(
                    session_id.clone(),
                    cwd,
                    conversation,
                    title,
                    mcp_configs,
                );
                return session_id;
            }
        }
    }

    pub(super) fn insert_session_with_mcp(
        &mut self,
        session_id: String,
        cwd: String,
        conversation: Conversation,
        title: Option<String>,
        mcp_configs: Vec<AcpMcpServerConfig>,
    ) {
        let agent = AcpHeadlessAgent::new(session_id.clone(), cwd, conversation, title);
        let session = AcpSession::new(session_id.clone(), agent).with_mcp_configs(mcp_configs);
        self.sessions.insert(session_id, session);
    }
}

pub(super) fn acp_jsonrpc_method(body: &str) -> Option<String> {
    let Ok(JsonValue::Object(request)) = json::parse(body) else {
        return None;
    };
    request
        .get("method")
        .and_then(json_string_value)
        .map(str::to_owned)
}

pub(super) fn handle_acp_jsonrpc(body: &str, runtime: &mut AcpServerRuntime) -> Vec<JsonValue> {
    let request = match json::parse(body) {
        Ok(JsonValue::Object(request)) => request,
        Ok(_) | Err(_) => return vec![jsonrpc_error(JsonValue::Null, -32700, "Parse error")],
    };
    let request_id = request.get("id").cloned().unwrap_or(JsonValue::Null);
    let Some(method) = request.get("method").and_then(json_string_value) else {
        return vec![jsonrpc_error(request_id, -32600, "Invalid request")];
    };
    let params = request.get("params");
    let response = match method {
        "initialize" => jsonrpc_result(request_id, acp_initialize_response(VERSION)),
        "authenticate" => jsonrpc_result(request_id, empty_json_object()),
        "session/new" => {
            return handle_acp_new_session(request_id, params, runtime);
        }
        "session/load" => {
            return handle_acp_load_session(request_id, params, runtime);
        }
        "session/fork" => {
            return handle_acp_fork_session(request_id, params, runtime);
        }
        "session/resume" => {
            return handle_acp_resume_session(request_id, params, runtime);
        }
        "session/prompt" => {
            return handle_acp_prompt(request_id, params, runtime);
        }
        "session/close" => handle_acp_close_session(request_id, params, runtime),
        "session/list" => jsonrpc_result(request_id, acp_session_list_response(runtime, params)),
        "session/cancel" => handle_acp_cancel_session(request_id, params, runtime),
        "session/set_config_option" => handle_acp_set_config_option(request_id, params, runtime),
        "session/set_mode" | "session/set_model" => jsonrpc_result(request_id, json::null()),
        _ => jsonrpc_error(request_id, -32601, "Method not found"),
    };
    vec![response]
}

fn handle_acp_prompt(
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
    let Some(session) = runtime.sessions.get_mut(session_id) else {
        return vec![acp_invalid_params_field_error(
            request_id,
            "session_id",
            "Session not found",
        )];
    };
    let prompt = acp_prompt_blocks(params);
    let mut client = StdioAcpClient::default();
    let response = session.prompt(prompt, &mut client);
    let mut messages = client.take_messages();
    messages.push(match response {
        Ok(response) => jsonrpc_result(request_id, acp_prompt_response_json(&response)),
        Err(error) => jsonrpc_error(request_id, -32603, &error.to_string()),
    });
    messages
}

fn handle_acp_close_session(
    request_id: JsonValue,
    params: Option<&JsonValue>,
    runtime: &mut AcpServerRuntime,
) -> JsonValue {
    let session_id = params.and_then(|value| json_string_field(value, "sessionId"));
    if let Some(session_id) = session_id {
        if let Some(mut session) = runtime.sessions.remove(session_id) {
            session.close();
        }
    }
    jsonrpc_result(request_id, empty_json_object())
}

fn handle_acp_cancel_session(
    request_id: JsonValue,
    params: Option<&JsonValue>,
    runtime: &mut AcpServerRuntime,
) -> JsonValue {
    let Some(params) = params else {
        return jsonrpc_error(request_id, -32602, "Missing params");
    };
    let Some(session_id) = json_string_field(params, "sessionId") else {
        return jsonrpc_error(request_id, -32602, "Missing sessionId");
    };
    if !runtime.sessions.contains_key(session_id) {
        return acp_invalid_params_field_error(request_id, "session_id", "Session not found");
    }
    jsonrpc_result(request_id, json::null())
}

fn handle_acp_set_config_option(
    request_id: JsonValue,
    params: Option<&JsonValue>,
    runtime: &mut AcpServerRuntime,
) -> JsonValue {
    let Some(params) = params else {
        return jsonrpc_error(request_id, -32602, "Missing params");
    };
    let Some(session_id) = json_string_field(params, "sessionId") else {
        return jsonrpc_error(request_id, -32602, "Missing sessionId");
    };
    let Some(config_id) =
        json_string_field(params, "configId").or_else(|| json_string_field(params, "config_id"))
    else {
        return jsonrpc_error(request_id, -32602, "Missing configId");
    };
    let Some(value) = json_object_field(params, "value") else {
        return jsonrpc_error(request_id, -32602, "Missing value");
    };
    let Some(session) = runtime.sessions.get_mut(session_id) else {
        return acp_invalid_params_field_error(request_id, "session_id", "Session not found");
    };

    session.update_config(BTreeMap::from([(config_id.to_owned(), value.clone())]));
    jsonrpc_result(request_id, json::null())
}
