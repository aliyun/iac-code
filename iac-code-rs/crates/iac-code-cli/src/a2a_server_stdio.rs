use std::io::{self, BufRead, Write};

use iac_code_a2a::transports::stdio::{
    decode_frame as decode_stdio_frame, encode_frame as encode_stdio_frame,
    error_response as stdio_error_response,
};

use crate::a2a_response::A2AJsonRpcResponse;
use crate::a2a_server_args::A2AServerArgs;
use crate::a2a_server_dispatch::dispatch_a2a_jsonrpc_value;
use crate::a2a_server_runtime::{build_a2a_server_runtime, A2AServerRuntime};

pub(super) fn run_a2a_stdio_server(args: A2AServerArgs) -> Result<(), String> {
    let mut runtime = build_a2a_server_runtime(&args, "stdio")?;
    let stdin = io::stdin();
    let stdout = io::stdout();
    let mut stdout = stdout.lock();
    let mut stdin = stdin.lock();

    serve_a2a_jsonrpc_frames(&mut stdin, &mut stdout, &mut runtime)
}

pub(super) fn serve_a2a_jsonrpc_frames<R: BufRead, W: Write>(
    reader: &mut R,
    writer: &mut W,
    runtime: &mut A2AServerRuntime,
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
        let response = match decode_stdio_frame(line.as_str()) {
            Ok(payload) => dispatch_a2a_jsonrpc_value(&payload, runtime),
            Err(error) => A2AJsonRpcResponse::Json(stdio_error_response(None, &error.to_string())),
        };
        write_a2a_stdio_response(writer, response)?;
    }
    Ok(())
}

fn write_a2a_stdio_response<W: Write>(
    writer: &mut W,
    response: A2AJsonRpcResponse,
) -> Result<(), String> {
    match response {
        A2AJsonRpcResponse::Json(body) => {
            writer
                .write_all(&encode_stdio_frame(&body))
                .map_err(|error| error.to_string())?;
        }
        A2AJsonRpcResponse::Sse(events) => {
            for event in events {
                writer
                    .write_all(&encode_stdio_frame(&event))
                    .map_err(|error| error.to_string())?;
            }
        }
    }
    writer.flush().map_err(|error| error.to_string())
}
