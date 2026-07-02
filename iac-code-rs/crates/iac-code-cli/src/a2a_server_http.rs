use std::collections::BTreeMap;
use std::net::{TcpListener, TcpStream};

use iac_code_a2a::app::{agent_card_response, health_response};
use iac_code_protocol::{json, json::JsonValue};

use crate::a2a_response::A2AJsonRpcResponse;
use crate::a2a_server_args::A2AServerArgs;
use crate::a2a_server_dispatch::{dispatch_a2a_jsonrpc_body, dispatch_a2a_rest_message_send};
use crate::a2a_server_runtime::{build_a2a_server_runtime, log_a2a_server_error, A2AServerRuntime};
use crate::wire::{
    a2a_http_authorized, http_header_value, http_last_modified, http_request_body,
    http_request_method, http_request_path, read_http_request_message, write_json_http_response,
    write_sse_http_response,
};

pub(super) fn run_a2a_http_server(args: A2AServerArgs) -> Result<(), String> {
    let listener =
        TcpListener::bind((args.host.as_str(), args.port)).map_err(|error| error.to_string())?;
    let mut runtime = build_a2a_server_runtime(&args, "http")?;

    for stream in listener.incoming() {
        let mut stream = stream.map_err(|error| error.to_string())?;
        if let Err(error) = handle_a2a_http_connection(&mut stream, &mut runtime) {
            log_a2a_server_error(runtime.log_to_stdout, &error);
        }
    }
    Ok(())
}

fn handle_a2a_http_connection(
    stream: &mut TcpStream,
    runtime: &mut A2AServerRuntime,
) -> Result<(), String> {
    let request = read_http_request_message(stream)?;
    if !a2a_http_authorized(&runtime.auth, &request) {
        return write_json_http_response(
            stream,
            401,
            &[],
            &json::object([("error", json::string("Unauthorized"))]),
        );
    }
    let method = http_request_method(&request).unwrap_or("");
    let path = http_request_path(&request).unwrap_or("/");
    match (method, path) {
        ("GET", "/health") => write_json_http_response(
            stream,
            health_response().status_code,
            &[],
            &health_response().body,
        ),
        ("GET", "/.well-known/agent-card.json") => {
            let response = agent_card_response(
                &runtime.card,
                http_header_value(&request, "if-none-match"),
                http_last_modified(),
            );
            let headers = response
                .headers
                .iter()
                .map(|(key, value)| (key.as_str(), value.as_str()))
                .collect::<Vec<_>>();
            write_json_http_response(stream, response.status_code, &headers, &response.body)
        }
        ("GET", "/extendedAgentCard") => write_json_http_response(stream, 200, &[], &runtime.card),
        ("POST", "/message:send") => {
            let body = dispatch_a2a_rest_message_send(http_request_body(&request), runtime);
            write_json_http_response(stream, 200, &[], &body)
        }
        ("POST", _) => {
            let response = dispatch_a2a_jsonrpc_body(http_request_body(&request), runtime);
            match response {
                A2AJsonRpcResponse::Json(body) => write_json_http_response(stream, 200, &[], &body),
                A2AJsonRpcResponse::Sse(events) => write_sse_http_response(stream, &events),
            }
        }
        _ => write_json_http_response(
            stream,
            404,
            &[],
            &JsonValue::Object(BTreeMap::from([(
                "error".to_owned(),
                JsonValue::String("not found".to_owned()),
            )])),
        ),
    }
}
