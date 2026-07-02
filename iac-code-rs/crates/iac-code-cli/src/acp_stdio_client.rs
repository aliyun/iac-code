use std::collections::BTreeMap;
use std::io::{BufRead, Write};

use iac_code_acp::convert::SessionUpdate;
use iac_code_acp::permissions::{
    PermissionOption, PermissionOutcome, PermissionResponse, PermissionToolCall, OPTION_REJECT_ONCE,
};
use iac_code_acp::session::AcpClient;
use iac_code_protocol::{json, json::JsonValue};

use super::acp_payload::{acp_permission_request_message, acp_session_update_message};
use super::acp_stdio::write_json_line;
use super::json_utils::{json_object_field, json_string_field};
use super::jsonrpc_payload::jsonrpc_result;

#[derive(Default)]
pub(super) struct StdioAcpClient {
    messages: Vec<JsonValue>,
    next_request_id: u64,
}

impl StdioAcpClient {
    pub(super) fn take_messages(self) -> Vec<JsonValue> {
        self.messages
    }
}

impl AcpClient for StdioAcpClient {
    fn session_update(&mut self, session_id: &str, update: SessionUpdate) {
        self.messages
            .push(acp_session_update_message(session_id, &update));
    }

    fn request_permission(
        &mut self,
        session_id: &str,
        options: Vec<PermissionOption>,
        tool_call: PermissionToolCall,
    ) -> PermissionResponse {
        let request_id = format!("permission-{}", self.next_request_id);
        self.next_request_id = self.next_request_id.saturating_add(1);
        self.messages.push(acp_permission_request_message(
            &request_id,
            session_id,
            &options,
            &tool_call,
        ));
        PermissionResponse {
            outcome: PermissionOutcome::Denied {
                option_id: Some(OPTION_REJECT_ONCE.to_owned()),
            },
            field_meta: BTreeMap::new(),
        }
    }
}

pub(super) struct LiveStdioAcpClient<'a, R, W> {
    reader: &'a mut R,
    writer: &'a mut W,
    next_request_id: u64,
    io_error: Option<String>,
}

impl<'a, R, W> LiveStdioAcpClient<'a, R, W> {
    pub(super) fn new(reader: &'a mut R, writer: &'a mut W) -> Self {
        Self {
            reader,
            writer,
            next_request_id: 0,
            io_error: None,
        }
    }

    pub(super) fn into_error(self) -> Option<String> {
        self.io_error
    }

    fn record_error(&mut self, error: impl Into<String>) {
        if self.io_error.is_none() {
            self.io_error = Some(error.into());
        }
    }
}

impl<R, W> LiveStdioAcpClient<'_, R, W>
where
    R: BufRead,
    W: Write,
{
    fn write_message(&mut self, message: &JsonValue) {
        if let Err(error) = write_json_line(self.writer, message) {
            self.record_error(error);
        }
    }

    fn read_permission_response(
        &mut self,
        request_id: &str,
        session_id: &str,
        options: &[PermissionOption],
    ) -> PermissionResponse {
        let mut line = String::new();
        loop {
            line.clear();
            let bytes_read = match self.reader.read_line(&mut line) {
                Ok(bytes_read) => bytes_read,
                Err(error) => {
                    self.record_error(error.to_string());
                    return reject_permission_response(None);
                }
            };
            if bytes_read == 0 {
                self.record_error("ACP client closed before permission response");
                return reject_permission_response(None);
            }
            let body = line.trim();
            if body.is_empty() {
                continue;
            }
            let value = match json::parse(body) {
                Ok(value) => value,
                Err(error) => {
                    self.record_error(format!("Invalid permission response: {error}"));
                    return reject_permission_response(None);
                }
            };
            if acp_cancel_session_id(&value) == Some(session_id) {
                if let Some(cancel_id) = jsonrpc_id_value(&value) {
                    self.write_message(&jsonrpc_result(cancel_id, json::null()));
                }
                return PermissionResponse {
                    outcome: PermissionOutcome::Cancelled,
                    field_meta: BTreeMap::new(),
                };
            }
            if !jsonrpc_id_matches(&value, request_id) {
                continue;
            }
            return parse_permission_response(&value, options);
        }
    }
}

impl<R, W> AcpClient for LiveStdioAcpClient<'_, R, W>
where
    R: BufRead,
    W: Write,
{
    fn session_update(&mut self, session_id: &str, update: SessionUpdate) {
        self.write_message(&acp_session_update_message(session_id, &update));
    }

    fn request_permission(
        &mut self,
        session_id: &str,
        options: Vec<PermissionOption>,
        tool_call: PermissionToolCall,
    ) -> PermissionResponse {
        if self.io_error.is_some() {
            return reject_permission_response(None);
        }

        let request_id = format!("permission-{}", self.next_request_id);
        self.next_request_id = self.next_request_id.saturating_add(1);
        self.write_message(&acp_permission_request_message(
            &request_id,
            session_id,
            &options,
            &tool_call,
        ));
        if self.io_error.is_some() {
            return reject_permission_response(None);
        }

        self.read_permission_response(&request_id, session_id, &options)
    }
}

fn parse_permission_response(
    response: &JsonValue,
    options: &[PermissionOption],
) -> PermissionResponse {
    let Some(result) = json_object_field(response, "result") else {
        return reject_permission_response(None);
    };
    let Some(outcome) = json_object_field(result, "outcome") else {
        return reject_permission_response(None);
    };
    match json_string_field(outcome, "outcome") {
        Some("selected") => {
            let option_id = json_string_field(outcome, "optionId")
                .or_else(|| json_string_field(outcome, "option_id"))
                .unwrap_or_default();
            let selected = options.iter().find(|option| option.option_id == option_id);
            let option_id = Some(option_id.to_owned());
            if selected.is_some_and(|option| option.kind.starts_with("allow")) {
                PermissionResponse {
                    outcome: PermissionOutcome::Allowed { option_id },
                    field_meta: BTreeMap::new(),
                }
            } else {
                reject_permission_response(option_id)
            }
        }
        Some("cancelled") => PermissionResponse {
            outcome: PermissionOutcome::Cancelled,
            field_meta: BTreeMap::new(),
        },
        _ => reject_permission_response(None),
    }
}

fn reject_permission_response(option_id: Option<String>) -> PermissionResponse {
    PermissionResponse {
        outcome: PermissionOutcome::Denied { option_id },
        field_meta: BTreeMap::new(),
    }
}

fn jsonrpc_id_matches(value: &JsonValue, expected: &str) -> bool {
    match jsonrpc_id_value(value).as_ref() {
        Some(JsonValue::String(id)) => id == expected,
        Some(JsonValue::Number(id)) => id == expected,
        _ => false,
    }
}

fn jsonrpc_id_value(value: &JsonValue) -> Option<JsonValue> {
    json_object_field(value, "id").cloned()
}

fn acp_cancel_session_id(value: &JsonValue) -> Option<&str> {
    let JsonValue::Object(object) = value else {
        return None;
    };
    let Some(JsonValue::String(method)) = object.get("method") else {
        return None;
    };
    if method != "session/cancel" {
        return None;
    }
    object
        .get("params")
        .and_then(|params| json_string_field(params, "sessionId"))
}
