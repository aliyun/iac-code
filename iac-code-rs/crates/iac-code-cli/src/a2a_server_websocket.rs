use std::net::{TcpListener, TcpStream};

use iac_code_a2a::transports::websocket::{decode_websocket_request, websocket_event_frame};

use crate::a2a_response::{
    a2a_final_jsonrpc_payload, is_a2a_streaming_request, A2AJsonRpcResponse,
};
use crate::a2a_server_args::A2AServerArgs;
use crate::a2a_server_dispatch::dispatch_a2a_jsonrpc_body;
use crate::a2a_server_runtime::{build_a2a_server_runtime, log_a2a_server_error, A2AServerRuntime};
use crate::wire::{
    read_http_request_message, read_websocket_text_frame, write_websocket_handshake_response,
    write_websocket_text_frame,
};

pub(super) fn run_a2a_websocket_server(args: A2AServerArgs) -> Result<(), String> {
    let listener =
        TcpListener::bind((args.host.as_str(), args.port)).map_err(|error| error.to_string())?;
    let mut runtime = build_a2a_server_runtime(&args, "websocket")?;

    for stream in listener.incoming() {
        let mut stream = stream.map_err(|error| error.to_string())?;
        if let Err(error) = handle_a2a_websocket_connection(&mut stream, &mut runtime) {
            log_a2a_server_error(runtime.log_to_stdout, &error);
        }
    }
    Ok(())
}

fn handle_a2a_websocket_connection(
    stream: &mut TcpStream,
    runtime: &mut A2AServerRuntime,
) -> Result<(), String> {
    let request = read_http_request_message(stream)?;
    write_websocket_handshake_response(stream, &request)?;
    while let Some(text) = read_websocket_text_frame(stream)? {
        let payload = match decode_websocket_request(&text) {
            Ok(payload) => payload,
            Err(error) => {
                write_websocket_text_frame(stream, &error.to_error_frame(None).to_compact_json())?;
                continue;
            }
        };
        let body = payload.to_compact_json();
        let response = dispatch_a2a_jsonrpc_body(&body, runtime);
        match response {
            A2AJsonRpcResponse::Json(body) => {
                let frame = websocket_event_frame(body, true);
                write_websocket_text_frame(stream, &frame.to_compact_json())?;
            }
            A2AJsonRpcResponse::Sse(events) => {
                let streaming = is_a2a_streaming_request(&payload);
                for event in events {
                    let frame = websocket_event_frame(event, !streaming);
                    write_websocket_text_frame(stream, &frame.to_compact_json())?;
                }
                if streaming {
                    let frame = websocket_event_frame(a2a_final_jsonrpc_payload(&payload), true);
                    write_websocket_text_frame(stream, &frame.to_compact_json())?;
                }
            }
        }
    }
    Ok(())
}
