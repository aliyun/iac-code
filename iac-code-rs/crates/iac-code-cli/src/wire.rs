use std::collections::BTreeMap;
use std::io::{self, Read, Write};
use std::net::TcpStream;
use std::sync::mpsc::{self, Receiver};
use std::sync::{Arc, Mutex};
use std::time::Duration;

use base64::engine::general_purpose::STANDARD;
use base64::Engine;
use iac_code_a2a::app::A2AAuthConfig as A2AServerAuthConfig;
use iac_code_protocol::json::JsonValue;
use ring::digest;

pub(super) fn read_http_request_message(stream: &mut TcpStream) -> Result<String, String> {
    let mut bytes = Vec::new();
    let mut buffer = [0_u8; 1024];
    let mut expected_len = None;
    loop {
        let count = stream
            .read(&mut buffer)
            .map_err(|error| error.to_string())?;
        if count == 0 {
            break;
        }
        bytes.extend_from_slice(&buffer[..count]);
        if expected_len.is_none() {
            if let Some(header_end) = bytes.windows(4).position(|window| window == b"\r\n\r\n") {
                let header_text = String::from_utf8_lossy(&bytes[..header_end]);
                let content_length = header_text
                    .lines()
                    .find_map(|line| {
                        let (name, value) = line.split_once(':')?;
                        name.eq_ignore_ascii_case("content-length")
                            .then(|| value.trim().parse::<usize>().ok())
                            .flatten()
                    })
                    .unwrap_or(0);
                expected_len = Some(header_end + 4 + content_length);
            }
        }
        if expected_len.is_some_and(|length| bytes.len() >= length) {
            break;
        }
        if bytes.len() > 16 * 1024 {
            return Err("HTTP request is too large.".to_owned());
        }
    }
    Ok(String::from_utf8_lossy(&bytes).into_owned())
}

pub(super) fn http_request_method(request: &str) -> Option<&str> {
    request.lines().next()?.split_whitespace().next()
}

pub(super) fn http_request_path(request: &str) -> Option<&str> {
    request.lines().next()?.split_whitespace().nth(1)
}

pub(super) fn http_request_body(request: &str) -> &str {
    request
        .split_once("\r\n\r\n")
        .map(|(_, body)| body)
        .unwrap_or_default()
}

pub(super) fn http_header_value<'a>(request: &'a str, name: &str) -> Option<&'a str> {
    request.lines().find_map(|line| {
        let (key, value) = line.split_once(':')?;
        key.eq_ignore_ascii_case(name).then_some(value.trim())
    })
}

pub(super) fn a2a_http_authorized(auth: &A2AServerAuthConfig, request: &str) -> bool {
    if !auth.auth_enabled() {
        return true;
    }
    auth.authorized_principal(&http_header_map(request))
        .is_some()
}

fn http_header_map(request: &str) -> BTreeMap<String, String> {
    request
        .lines()
        .skip(1)
        .take_while(|line| !line.trim().is_empty())
        .filter_map(|line| {
            let (key, value) = line.split_once(':')?;
            Some((key.trim().to_owned(), value.trim().to_owned()))
        })
        .collect()
}

pub(super) fn write_websocket_handshake_response(
    stream: &mut TcpStream,
    request: &str,
) -> Result<(), String> {
    let key = http_header_value(request, "sec-websocket-key")
        .ok_or_else(|| "Missing Sec-WebSocket-Key header.".to_owned())?;
    let accept_key = websocket_accept_key(key);
    let response = format!(
        "HTTP/1.1 101 Switching Protocols\r\nUpgrade: websocket\r\nConnection: Upgrade\r\nSec-WebSocket-Accept: {accept_key}\r\n\r\n"
    );
    stream
        .write_all(response.as_bytes())
        .map_err(|error| error.to_string())
}

fn websocket_accept_key(key: &str) -> String {
    let mut input = key.as_bytes().to_vec();
    input.extend_from_slice(b"258EAFA5-E914-47DA-95CA-C5AB0DC85B11");
    let digest = digest::digest(&digest::SHA1_FOR_LEGACY_USE_ONLY, &input);
    STANDARD.encode(digest.as_ref())
}

pub(super) fn read_websocket_text_frame(stream: &mut TcpStream) -> Result<Option<String>, String> {
    let mut header = [0_u8; 2];
    match stream.read_exact(&mut header) {
        Ok(()) => {}
        Err(error) if error.kind() == io::ErrorKind::UnexpectedEof => return Ok(None),
        Err(error) => return Err(error.to_string()),
    }
    let opcode = header[0] & 0x0f;
    if opcode == 0x8 {
        return Ok(None);
    }
    if opcode != 0x1 {
        return Err(format!("Unsupported WebSocket opcode: {opcode}"));
    }

    let masked = header[1] & 0x80 != 0;
    let mut length = (header[1] & 0x7f) as usize;
    if length == 126 {
        let mut extended = [0_u8; 2];
        stream
            .read_exact(&mut extended)
            .map_err(|error| error.to_string())?;
        length = u16::from_be_bytes(extended) as usize;
    } else if length == 127 {
        let mut extended = [0_u8; 8];
        stream
            .read_exact(&mut extended)
            .map_err(|error| error.to_string())?;
        length = u64::from_be_bytes(extended) as usize;
    }
    if length > 16 * 1024 * 1024 {
        return Err("WebSocket frame is too large.".to_owned());
    }

    let mut mask = [0_u8; 4];
    if masked {
        stream
            .read_exact(&mut mask)
            .map_err(|error| error.to_string())?;
    }
    let mut payload = vec![0_u8; length];
    stream
        .read_exact(&mut payload)
        .map_err(|error| error.to_string())?;
    if masked {
        for (index, byte) in payload.iter_mut().enumerate() {
            *byte ^= mask[index % mask.len()];
        }
    }
    String::from_utf8(payload)
        .map(Some)
        .map_err(|error| format!("Invalid WebSocket text frame: {error}"))
}

pub(super) fn write_websocket_text_frame(stream: &mut TcpStream, text: &str) -> Result<(), String> {
    let payload = text.as_bytes();
    let mut frame = Vec::with_capacity(payload.len() + 10);
    frame.push(0x81);
    if payload.len() < 126 {
        frame.push(payload.len() as u8);
    } else if u16::try_from(payload.len()).is_ok() {
        frame.push(126);
        frame.extend_from_slice(&(payload.len() as u16).to_be_bytes());
    } else {
        frame.push(127);
        frame.extend_from_slice(&(payload.len() as u64).to_be_bytes());
    }
    frame.extend_from_slice(payload);
    stream
        .write_all(&frame)
        .and_then(|_| stream.flush())
        .map_err(|error| error.to_string())
}

pub(super) fn http_last_modified() -> &'static str {
    "Fri, 05 Jun 2026 00:00:00 GMT"
}

pub(super) fn write_json_http_response(
    stream: &mut TcpStream,
    status_code: u16,
    headers: &[(&str, &str)],
    body: &JsonValue,
) -> Result<(), String> {
    let reason = match status_code {
        200 => "OK",
        202 => "Accepted",
        304 => "Not Modified",
        400 => "Bad Request",
        401 => "Unauthorized",
        404 => "Not Found",
        502 => "Bad Gateway",
        504 => "Gateway Timeout",
        _ => "OK",
    };
    let body_text = if status_code == 304 {
        String::new()
    } else {
        body.to_compact_json()
    };
    let mut response = format!(
        "HTTP/1.1 {status_code} {reason}\r\nContent-Type: application/json\r\nContent-Length: {}\r\nConnection: close\r\n",
        body_text.len()
    );
    for (key, value) in headers {
        response.push_str(key);
        response.push_str(": ");
        response.push_str(value);
        response.push_str("\r\n");
    }
    response.push_str("\r\n");
    response.push_str(&body_text);
    stream
        .write_all(response.as_bytes())
        .map_err(|error| error.to_string())
}

pub(super) fn write_empty_http_response(
    stream: &mut TcpStream,
    status_code: u16,
) -> Result<(), String> {
    let reason = match status_code {
        200 => "OK",
        202 => "Accepted",
        400 => "Bad Request",
        401 => "Unauthorized",
        404 => "Not Found",
        _ => "OK",
    };
    let response = format!(
        "HTTP/1.1 {status_code} {reason}\r\nContent-Length: 0\r\nConnection: close\r\n\r\n"
    );
    stream
        .write_all(response.as_bytes())
        .map_err(|error| error.to_string())
}

pub(super) fn write_acp_sse_http_response(
    stream: &mut TcpStream,
    output_receiver: Arc<Mutex<Receiver<JsonValue>>>,
    next_event_id: Arc<Mutex<u64>>,
) -> Result<(), String> {
    let mut body = String::new();
    let mut messages = Vec::new();
    {
        let receiver = output_receiver
            .lock()
            .map_err(|_| "ACP HTTP output lock poisoned".to_owned())?;
        match receiver.recv_timeout(Duration::from_secs(2)) {
            Ok(message) => messages.push(message),
            Err(mpsc::RecvTimeoutError::Timeout | mpsc::RecvTimeoutError::Disconnected) => {}
        }
        while let Ok(message) = receiver.try_recv() {
            messages.push(message);
        }
    }
    for message in messages {
        let event_id = {
            let mut next_event_id = next_event_id
                .lock()
                .map_err(|_| "ACP HTTP event id lock poisoned".to_owned())?;
            let event_id = *next_event_id;
            *next_event_id = next_event_id.saturating_add(1);
            event_id
        };
        body.push_str("event: message\n");
        body.push_str("data: ");
        body.push_str(&message.to_compact_json());
        body.push('\n');
        body.push_str("id: ");
        body.push_str(&event_id.to_string());
        body.push('\n');
        body.push_str("retry: 5000\n\n");
    }
    let response = format!(
        "HTTP/1.1 200 OK\r\nContent-Type: text/event-stream\r\nCache-Control: no-cache\r\nConnection: close\r\nContent-Length: {}\r\n\r\n{}",
        body.len(),
        body
    );
    stream
        .write_all(response.as_bytes())
        .map_err(|error| error.to_string())
}

pub(super) fn write_sse_http_response(
    stream: &mut TcpStream,
    events: &[JsonValue],
) -> Result<(), String> {
    let mut body = String::new();
    for event in events {
        body.push_str("data: ");
        body.push_str(&event.to_compact_json());
        body.push_str("\n\n");
    }
    let response = format!(
        "HTTP/1.1 200 OK\r\nContent-Type: text/event-stream\r\nCache-Control: no-cache\r\nContent-Length: {}\r\nConnection: close\r\n\r\n{}",
        body.len(),
        body
    );
    stream
        .write_all(response.as_bytes())
        .map_err(|error| error.to_string())
}
