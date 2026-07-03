use std::io::{self, BufRead, Write};

use iac_code_protocol::{json, json::JsonValue};

use super::acp_payload::{acp_prompt_blocks, acp_prompt_response_json};
use super::acp_resume::acp_invalid_params_field_error;
use super::acp_server::{acp_jsonrpc_method, handle_acp_jsonrpc, AcpServerRuntime};
use super::acp_server_args::AcpServerArgs;
use super::acp_stdio_client::LiveStdioAcpClient;
use super::json_utils::json_string_field;
use super::jsonrpc_payload::{jsonrpc_error, jsonrpc_result};

pub(super) fn run_acp_stdio_server(_args: AcpServerArgs) -> Result<(), String> {
    let stdin = io::stdin();
    let stdout = io::stdout();
    let mut stdin = stdin.lock();
    let mut stdout = stdout.lock();
    let mut runtime = AcpServerRuntime::new();
    serve_acp_jsonrpc_frames(&mut stdin, &mut stdout, &mut runtime)
}

pub(super) fn serve_acp_jsonrpc_frames<R: BufRead, W: Write>(
    reader: &mut R,
    writer: &mut W,
    runtime: &mut AcpServerRuntime,
) -> Result<(), String> {
    let mut line = String::new();
    loop {
        line.clear();
        let bytes_read = reader
            .read_line(&mut line)
            .map_err(|error| error.to_string())?;
        if bytes_read == 0 {
            break;
        }
        if line.trim().is_empty() {
            continue;
        }
        for message in handle_acp_jsonrpc_stdio(line.trim(), runtime, reader, writer)? {
            write_json_line(writer, &message)?;
        }
    }
    Ok(())
}

fn handle_acp_jsonrpc_stdio<R: BufRead, W: Write>(
    body: &str,
    runtime: &mut AcpServerRuntime,
    reader: &mut R,
    writer: &mut W,
) -> Result<Vec<JsonValue>, String> {
    if acp_jsonrpc_method(body).as_deref() == Some("session/prompt") {
        return handle_acp_prompt_body_stdio(body, runtime, reader, writer);
    }
    Ok(handle_acp_jsonrpc(body, runtime))
}

fn handle_acp_prompt_body_stdio<R: BufRead, W: Write>(
    body: &str,
    runtime: &mut AcpServerRuntime,
    reader: &mut R,
    writer: &mut W,
) -> Result<Vec<JsonValue>, String> {
    let request = match json::parse(body) {
        Ok(JsonValue::Object(request)) => request,
        Ok(_) | Err(_) => return Ok(vec![jsonrpc_error(JsonValue::Null, -32700, "Parse error")]),
    };
    let request_id = request.get("id").cloned().unwrap_or(JsonValue::Null);
    Ok(handle_acp_prompt_stdio(
        request_id,
        request.get("params"),
        runtime,
        reader,
        writer,
    ))
}

fn handle_acp_prompt_stdio<R: BufRead, W: Write>(
    request_id: JsonValue,
    params: Option<&JsonValue>,
    runtime: &mut AcpServerRuntime,
    reader: &mut R,
    writer: &mut W,
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
    let mut client = LiveStdioAcpClient::new(reader, writer);
    let response = session.prompt(prompt, &mut client);
    if let Some(error) = client.into_error() {
        return vec![jsonrpc_error(request_id, -32603, &error)];
    }
    vec![match response {
        Ok(response) => jsonrpc_result(request_id, acp_prompt_response_json(&response)),
        Err(error) => jsonrpc_error(request_id, -32603, &error.to_string()),
    }]
}

pub(super) fn write_json_line<W: Write>(writer: &mut W, value: &JsonValue) -> Result<(), String> {
    writer
        .write_all(value.to_compact_json().as_bytes())
        .map_err(|error| error.to_string())?;
    writer.write_all(b"\n").map_err(|error| error.to_string())?;
    writer.flush().map_err(|error| error.to_string())
}
