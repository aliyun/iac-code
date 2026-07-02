use super::a2a_server_args::{parse_a2a_server_args, validate_a2a_server_startup_options_for_cli};
use crate::a2a_server_grpc::{run_a2a_grpc_jsonrpc_server, run_a2a_grpc_server};
use crate::a2a_server_http::run_a2a_http_server;
use crate::a2a_server_redis_streams::run_a2a_redis_streams_server;
#[cfg(test)]
pub(super) use crate::a2a_server_runtime::{
    build_a2a_server_runtime, write_a2a_server_log_to_stdout,
};
use crate::a2a_server_stdio::run_a2a_stdio_server;
use crate::a2a_server_unix::run_a2a_unix_server;
use crate::a2a_server_websocket::run_a2a_websocket_server;

pub(super) fn handle_a2a_server_command(args: &[String]) -> Option<i32> {
    if args.first().map(String::as_str) != Some("a2a") {
        return None;
    }
    match run_a2a_server(&args[1..]) {
        Ok(()) => Some(0),
        Err(error) => {
            eprintln!("{error}");
            Some(1)
        }
    }
}

pub(super) fn run_a2a_server(args: &[String]) -> Result<(), String> {
    let args = parse_a2a_server_args(args)?;
    let transport = validate_a2a_server_startup_options_for_cli(&args)?;
    match transport.as_str() {
        "http" => run_a2a_http_server(args),
        "stdio" => run_a2a_stdio_server(args),
        "unix" => run_a2a_unix_server(args),
        "websocket" => run_a2a_websocket_server(args),
        "grpc-jsonrpc" => run_a2a_grpc_jsonrpc_server(args),
        "grpc" => run_a2a_grpc_server(args),
        "redis-streams" => run_a2a_redis_streams_server(args),
        _ => Err(format!("Unsupported A2A server transport '{transport}'.")),
    }
}
