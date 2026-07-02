use std::io::{self, Read, Write};
use std::net::{TcpListener, TcpStream};
use std::time::{Duration, Instant};

use iac_code_tui::RawInputCapture;

use crate::cli_i18n::tr;
use crate::raw_auth_oauth_utils::raw_auth_query_param;

const RAW_AUTH_ALIYUN_OAUTH_CALLBACK_HOST: &str = "127.0.0.1";
const RAW_AUTH_ALIYUN_OAUTH_CALLBACK_PATH: &str = "/cli/callback";
const RAW_AUTH_ALIYUN_OAUTH_CALLBACK_TIMEOUT: Duration = Duration::from_secs(300);

pub(super) struct RawAuthAliyunOAuthCallback {
    listener: TcpListener,
    pub(super) redirect_uri: String,
}

impl RawAuthAliyunOAuthCallback {
    pub(super) fn start() -> Result<Self, String> {
        let mut last_error = None;
        for port in 12345..=12349 {
            match TcpListener::bind((RAW_AUTH_ALIYUN_OAUTH_CALLBACK_HOST, port)) {
                Ok(listener) => {
                    listener.set_nonblocking(true).map_err(|error| {
                        format!("Failed to prepare OAuth callback listener: {error}")
                    })?;
                    return Ok(Self {
                        listener,
                        redirect_uri: format!(
                            "http://{}:{}{}",
                            RAW_AUTH_ALIYUN_OAUTH_CALLBACK_HOST,
                            port,
                            RAW_AUTH_ALIYUN_OAUTH_CALLBACK_PATH
                        ),
                    });
                }
                Err(error) => last_error = Some(error),
            }
        }
        Err(format!(
            "No available callback port in range 12345-12349{}",
            last_error
                .map(|error| format!(": {error}"))
                .unwrap_or_default()
        ))
    }

    pub(super) fn wait_for_code(
        &self,
        capture: &RawInputCapture,
        expected_state: &str,
    ) -> Result<String, String> {
        let deadline = Instant::now() + RAW_AUTH_ALIYUN_OAUTH_CALLBACK_TIMEOUT;
        loop {
            if Instant::now() >= deadline {
                return Err(tr(
                    "Timed out waiting for OAuth callback. Close the old authorization page and run /auth to choose OAuth Login (Browser) again.",
                ));
            }

            if let Some(event) = capture
                .read_key(Some(Duration::from_millis(100)))
                .map_err(|error| error.to_string())?
            {
                if event.key == "escape" || (event.ctrl && event.key == "c") {
                    return Err(tr("OAuth login cancelled."));
                }
            }

            match self.listener.accept() {
                Ok((mut stream, _)) => {
                    let request = raw_auth_read_http_request(&mut stream)?;
                    let result = raw_auth_parse_oauth_callback_code(&request, expected_state);
                    match result {
                        Ok(code) => {
                            let _ = raw_auth_write_http_response(
                                &mut stream,
                                200,
                                "OK",
                                &tr("Authorization successful. You can close this window."),
                            );
                            return Ok(code);
                        }
                        Err(error) => {
                            let _ = raw_auth_write_http_response(
                                &mut stream,
                                400,
                                "Bad Request",
                                &error,
                            );
                            return Err(error);
                        }
                    }
                }
                Err(error) if error.kind() == io::ErrorKind::WouldBlock => continue,
                Err(error) => return Err(format!("OAuth callback listener failed: {error}")),
            }
        }
    }
}

fn raw_auth_read_http_request(stream: &mut TcpStream) -> Result<String, String> {
    stream
        .set_read_timeout(Some(Duration::from_secs(2)))
        .map_err(|error| format!("Failed to read OAuth callback: {error}"))?;
    let mut bytes = Vec::new();
    loop {
        let mut chunk = [0_u8; 2048];
        let count = stream
            .read(&mut chunk)
            .map_err(|error| format!("Failed to read OAuth callback: {error}"))?;
        if count == 0 {
            break;
        }
        bytes.extend_from_slice(&chunk[..count]);
        if bytes.windows(4).any(|window| window == b"\r\n\r\n") || bytes.len() > 16 * 1024 {
            break;
        }
    }
    String::from_utf8(bytes).map_err(|_| "OAuth callback was not valid UTF-8".to_owned())
}

fn raw_auth_parse_oauth_callback_code(
    request: &str,
    expected_state: &str,
) -> Result<String, String> {
    let request_line = request.lines().next().unwrap_or_default();
    let target = request_line.split_whitespace().nth(1).unwrap_or_default();
    let (path, query) = target.split_once('?').unwrap_or((target, ""));
    if path != RAW_AUTH_ALIYUN_OAUTH_CALLBACK_PATH {
        return Err(tr("Not found"));
    }
    let state = raw_auth_query_param(query, "state").unwrap_or_default();
    if state != expected_state {
        return Err(tr("Invalid state"));
    }
    let code = raw_auth_query_param(query, "code").unwrap_or_default();
    if code.is_empty() {
        return Err(tr("Authorization code not found"));
    }
    Ok(code)
}

fn raw_auth_write_http_response(
    stream: &mut TcpStream,
    status: u16,
    reason: &str,
    body: &str,
) -> io::Result<()> {
    let body = body.as_bytes();
    write!(
        stream,
        "HTTP/1.1 {status} {reason}\r\nContent-Type: text/plain; charset=utf-8\r\nContent-Length: {}\r\nConnection: close\r\n\r\n",
        body.len()
    )?;
    stream.write_all(body)
}
