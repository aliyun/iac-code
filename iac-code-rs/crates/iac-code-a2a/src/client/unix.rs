use std::io::{BufRead, BufReader, Write};

use iac_code_protocol::json::JsonValue;

use crate::transports::stdio::{decode_frame, encode_frame};

use super::{json_bool_field, object_field};

#[cfg(unix)]
use std::os::unix::net::UnixStream;

#[cfg(unix)]
pub(super) fn send_unix_jsonrpc_payload(
    url: &str,
    payload: &JsonValue,
) -> Result<JsonValue, String> {
    let socket_path = unix_socket_path_from_url(url)?;
    let mut stream = UnixStream::connect(socket_path).map_err(|error| error.to_string())?;
    stream
        .write_all(&encode_frame(payload))
        .map_err(|error| error.to_string())?;
    stream.flush().map_err(|error| error.to_string())?;

    let mut reader = BufReader::new(stream);
    let mut line = String::new();
    let bytes_read = reader
        .read_line(&mut line)
        .map_err(|error| error.to_string())?;
    if bytes_read == 0 {
        return Err("A2A Unix transport closed without a response".to_owned());
    }
    decode_frame(line.as_str()).map_err(|error| error.to_string())
}

#[cfg(unix)]
pub(super) fn stream_unix_jsonrpc_payload(
    url: &str,
    payload: &JsonValue,
) -> Result<Vec<JsonValue>, String> {
    let socket_path = unix_socket_path_from_url(url)?;
    let mut stream = UnixStream::connect(socket_path).map_err(|error| error.to_string())?;
    stream
        .write_all(&encode_frame(payload))
        .map_err(|error| error.to_string())?;
    stream.flush().map_err(|error| error.to_string())?;

    let mut events = Vec::new();
    let mut reader = BufReader::new(stream);
    let mut line = String::new();
    loop {
        line.clear();
        let bytes_read = reader
            .read_line(&mut line)
            .map_err(|error| error.to_string())?;
        if bytes_read == 0 {
            break;
        }
        let event = decode_frame(line.as_str()).map_err(|error| error.to_string())?;
        let is_final = json_bool_field(&event, "final")
            .or_else(|| {
                object_field(&event, "result").and_then(|result| json_bool_field(result, "final"))
            })
            .unwrap_or(false);
        events.push(event);
        if is_final {
            break;
        }
    }
    Ok(events)
}

#[cfg(not(unix))]
pub(super) fn send_unix_jsonrpc_payload(
    _url: &str,
    _payload: &JsonValue,
) -> Result<JsonValue, String> {
    Err("Unix domain socket transport is not supported on this platform.".to_owned())
}

#[cfg(not(unix))]
pub(super) fn stream_unix_jsonrpc_payload(
    _url: &str,
    _payload: &JsonValue,
) -> Result<Vec<JsonValue>, String> {
    Err("Unix domain socket transport is not supported on this platform.".to_owned())
}

fn unix_socket_path_from_url(url: &str) -> Result<&str, String> {
    let Some(path) = url.strip_prefix("unix://") else {
        return Err(format!("Invalid Unix A2A URL: {url}"));
    };
    if path.is_empty() {
        return Err("Unix A2A URL requires a socket path.".to_owned());
    }
    Ok(path)
}
