use std::net::SocketAddr;

use crate::a2a_server_args::A2AServerArgs;
pub(super) use crate::a2a_server_grpc_jsonrpc::run_a2a_grpc_jsonrpc_server;
pub(super) use crate::a2a_server_grpc_official::run_a2a_grpc_server;
use crate::cli_args::non_empty_str;

pub(crate) fn a2a_grpc_socket_addr(args: &A2AServerArgs) -> Result<SocketAddr, String> {
    let host = non_empty_str(&args.grpc_host).unwrap_or(&args.host);
    let port = args.grpc_port.unwrap_or(args.port);
    format!("{host}:{port}")
        .parse::<SocketAddr>()
        .map_err(|error| error.to_string())
}

pub(crate) fn a2a_tokio_runtime() -> Result<tokio::runtime::Runtime, String> {
    tokio::runtime::Builder::new_multi_thread()
        .enable_all()
        .build()
        .map_err(|error| error.to_string())
}
