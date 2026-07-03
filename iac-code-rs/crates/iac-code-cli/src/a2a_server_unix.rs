use std::fs;
use std::io;

use crate::a2a_server_args::A2AServerArgs;
use crate::a2a_server_runtime::{build_a2a_server_runtime, log_a2a_server_error};
use crate::a2a_server_stdio::serve_a2a_jsonrpc_frames;

#[cfg(unix)]
use iac_code_a2a::transports::unix::validate_socket_path;

#[cfg(unix)]
pub(super) fn run_a2a_unix_server(args: A2AServerArgs) -> Result<(), String> {
    let socket_path = validate_socket_path(&args.socket_path).map_err(|error| error.to_string())?;
    if socket_path.exists() {
        fs::remove_file(&socket_path).map_err(|error| error.to_string())?;
    }
    let listener =
        std::os::unix::net::UnixListener::bind(&socket_path).map_err(|error| error.to_string())?;
    let mut runtime = build_a2a_server_runtime(&args, "unix")?;

    for stream in listener.incoming() {
        let stream = stream.map_err(|error| error.to_string())?;
        let mut reader = io::BufReader::new(stream.try_clone().map_err(|error| error.to_string())?);
        let mut writer = stream;
        if let Err(error) = serve_a2a_jsonrpc_frames(&mut reader, &mut writer, &mut runtime) {
            log_a2a_server_error(runtime.log_to_stdout, &error);
        }
    }
    Ok(())
}

#[cfg(not(unix))]
pub(super) fn run_a2a_unix_server(_args: A2AServerArgs) -> Result<(), String> {
    Err("Unix domain socket transport is not supported on this platform.".to_owned())
}
