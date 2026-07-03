use std::collections::BTreeMap;
use std::env;
use std::io::{self, BufRead, Read, Write};
use std::net::{TcpListener, TcpStream};
use std::sync::mpsc::{self, Receiver, Sender};
use std::sync::{Arc, Mutex};
use std::thread;
use std::time::Duration;

use iac_code_a2a::app::health_response;
use iac_code_protocol::{json, json::JsonValue};

use super::acp_server::AcpServerRuntime;
use super::acp_server_args::AcpServerArgs;
use super::acp_stdio::serve_acp_jsonrpc_frames;
use super::json_utils::json_string_value;
use super::wire::{
    http_header_value, http_request_body, http_request_method, http_request_path,
    read_http_request_message, write_acp_sse_http_response, write_empty_http_response,
    write_json_http_response,
};

type AcpHttpOutputReceiver = Arc<Mutex<Receiver<JsonValue>>>;
type AcpHttpNextEventId = Arc<Mutex<u64>>;
pub(super) type AcpHttpOutputStream = (AcpHttpOutputReceiver, AcpHttpNextEventId);

pub(super) struct AcpHttpServerRuntime {
    connections: BTreeMap<String, AcpHttpConnection>,
    next_connection_id: u64,
}

pub(super) struct AcpHttpConnection {
    input_sender: Sender<String>,
    output_receiver: AcpHttpOutputReceiver,
    next_event_id: AcpHttpNextEventId,
}

impl AcpHttpConnection {
    pub(super) fn new(input_sender: Sender<String>, output_receiver: Receiver<JsonValue>) -> Self {
        Self {
            input_sender,
            output_receiver: Arc::new(Mutex::new(output_receiver)),
            next_event_id: Arc::new(Mutex::new(1)),
        }
    }
}

pub(super) fn run_acp_http_server(args: AcpServerArgs) -> Result<(), String> {
    let listener =
        TcpListener::bind((args.host.as_str(), args.port)).map_err(|error| error.to_string())?;
    let runtime = Arc::new(Mutex::new(AcpHttpServerRuntime::new()));

    for stream in listener.incoming() {
        let mut stream = stream.map_err(|error| error.to_string())?;
        let runtime = Arc::clone(&runtime);
        thread::spawn(move || {
            if let Err(error) = handle_acp_http_connection(&mut stream, &runtime) {
                eprintln!("{error}");
            }
        });
    }
    Ok(())
}

fn handle_acp_http_connection(
    stream: &mut TcpStream,
    runtime: &Arc<Mutex<AcpHttpServerRuntime>>,
) -> Result<(), String> {
    let request = read_http_request_message(stream)?;
    if !acp_http_authorized(&request) {
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
        ("POST", "/acp") => handle_acp_http_post(stream, &request, runtime),
        ("GET", "/acp") => handle_acp_http_get(stream, &request, runtime),
        ("DELETE", "/acp") => {
            if let Some(connection_id) = http_header_value(&request, "Acp-Connection-Id") {
                runtime
                    .lock()
                    .map_err(|_| "ACP HTTP runtime lock poisoned".to_owned())?
                    .remove_connection(connection_id);
            }
            write_empty_http_response(stream, 200)
        }
        _ => write_json_http_response(
            stream,
            404,
            &[],
            &json::object([("error", json::string("not found"))]),
        ),
    }
}

fn handle_acp_http_post(
    stream: &mut TcpStream,
    request: &str,
    runtime: &Arc<Mutex<AcpHttpServerRuntime>>,
) -> Result<(), String> {
    let body = http_request_body(request);
    let parsed = match json::parse(body) {
        Ok(JsonValue::Object(request)) => request,
        Ok(_) | Err(_) => {
            return write_json_http_response(
                stream,
                400,
                &[],
                &json::object([("error", json::string("Invalid JSON"))]),
            );
        }
    };
    let method = parsed
        .get("method")
        .and_then(json_string_value)
        .unwrap_or_default();
    if method == "initialize" {
        let (connection, response) = match spawn_acp_http_connection(body) {
            Ok(connection) => connection,
            Err(error) => {
                return write_json_http_response(
                    stream,
                    502,
                    &[],
                    &json::object([("error", json::string(error))]),
                );
            }
        };
        let connection_id = runtime
            .lock()
            .map_err(|_| "ACP HTTP runtime lock poisoned".to_owned())?
            .create_connection(connection);
        return write_json_http_response(
            stream,
            200,
            &[("Acp-Connection-Id", &connection_id)],
            &response,
        );
    }

    let Some(connection_id) = http_header_value(request, "Acp-Connection-Id") else {
        return write_acp_http_missing_connection(stream);
    };
    let input_sender = {
        let runtime = runtime
            .lock()
            .map_err(|_| "ACP HTTP runtime lock poisoned".to_owned())?;
        let Some(input_sender) = runtime.input_sender(connection_id) else {
            return write_acp_http_missing_connection(stream);
        };
        input_sender
    };
    send_acp_http_message(&input_sender, body)?;
    write_empty_http_response(stream, 202)
}

fn spawn_acp_http_connection(body: &str) -> Result<(AcpHttpConnection, JsonValue), String> {
    let (input_sender, input_receiver) = mpsc::channel::<String>();
    let (output_sender, output_receiver) = mpsc::channel::<JsonValue>();
    let mut reader = AcpHttpChannelReader::new(input_receiver);
    let mut writer = AcpHttpChannelWriter::new(output_sender);
    let _worker = thread::spawn(move || {
        let mut runtime = AcpServerRuntime::new();
        if let Err(error) = serve_acp_jsonrpc_frames(&mut reader, &mut writer, &mut runtime) {
            eprintln!("{error}");
        }
    });

    send_acp_http_message(&input_sender, body)?;
    let response = output_receiver
        .recv_timeout(Duration::from_secs(30))
        .map_err(|_| "Initialize timeout".to_owned())?;
    Ok((
        AcpHttpConnection::new(input_sender, output_receiver),
        response,
    ))
}

fn handle_acp_http_get(
    stream: &mut TcpStream,
    request: &str,
    runtime: &Arc<Mutex<AcpHttpServerRuntime>>,
) -> Result<(), String> {
    let Some(connection_id) = http_header_value(request, "Acp-Connection-Id") else {
        return write_acp_http_missing_connection(stream);
    };
    let (output_receiver, next_event_id) = {
        let runtime = runtime
            .lock()
            .map_err(|_| "ACP HTTP runtime lock poisoned".to_owned())?;
        let Some(output_stream) = runtime.output_stream(connection_id) else {
            return write_acp_http_missing_connection(stream);
        };
        output_stream
    };
    write_acp_sse_http_response(stream, output_receiver, next_event_id)
}

impl AcpHttpServerRuntime {
    pub(super) fn new() -> Self {
        Self {
            connections: BTreeMap::new(),
            next_connection_id: 1,
        }
    }

    pub(super) fn create_connection(&mut self, connection: AcpHttpConnection) -> String {
        loop {
            let connection_id = format!("acp-http-{}", self.next_connection_id);
            self.next_connection_id = self.next_connection_id.saturating_add(1);
            if !self.connections.contains_key(&connection_id) {
                self.connections.insert(connection_id.clone(), connection);
                return connection_id;
            }
        }
    }

    pub(super) fn remove_connection(&mut self, connection_id: &str) {
        self.connections.remove(connection_id);
    }

    pub(super) fn input_sender(&self, connection_id: &str) -> Option<Sender<String>> {
        self.connections
            .get(connection_id)
            .map(|connection| connection.input_sender.clone())
    }

    pub(super) fn output_stream(&self, connection_id: &str) -> Option<AcpHttpOutputStream> {
        let connection = self.connections.get(connection_id)?;
        Some((
            Arc::clone(&connection.output_receiver),
            Arc::clone(&connection.next_event_id),
        ))
    }
}

pub(super) fn send_acp_http_message(sender: &Sender<String>, body: &str) -> Result<(), String> {
    sender
        .send(format!("{}\n", body.trim()))
        .map_err(|_| "ACP HTTP connection is closed.".to_owned())
}

pub(super) struct AcpHttpChannelReader {
    receiver: Receiver<String>,
    buffer: Vec<u8>,
    closed: bool,
}

impl AcpHttpChannelReader {
    pub(super) fn new(receiver: Receiver<String>) -> Self {
        Self {
            receiver,
            buffer: Vec::new(),
            closed: false,
        }
    }
}

impl Read for AcpHttpChannelReader {
    fn read(&mut self, output: &mut [u8]) -> io::Result<usize> {
        let count = {
            let available = self.fill_buf()?;
            if available.is_empty() {
                return Ok(0);
            }
            let count = available.len().min(output.len());
            output[..count].copy_from_slice(&available[..count]);
            count
        };
        self.consume(count);
        Ok(count)
    }
}

impl BufRead for AcpHttpChannelReader {
    fn fill_buf(&mut self) -> io::Result<&[u8]> {
        while self.buffer.is_empty() && !self.closed {
            match self.receiver.recv() {
                Ok(message) => self.buffer = message.into_bytes(),
                Err(_) => self.closed = true,
            }
        }
        Ok(&self.buffer)
    }

    fn consume(&mut self, amount: usize) {
        let amount = amount.min(self.buffer.len());
        self.buffer.drain(..amount);
    }
}

pub(super) struct AcpHttpChannelWriter {
    sender: Sender<JsonValue>,
    buffer: String,
}

impl AcpHttpChannelWriter {
    pub(super) fn new(sender: Sender<JsonValue>) -> Self {
        Self {
            sender,
            buffer: String::new(),
        }
    }

    fn flush_lines(&mut self) -> io::Result<()> {
        while let Some(index) = self.buffer.find('\n') {
            let line = self.buffer[..index].trim().to_owned();
            self.buffer.drain(..=index);
            if line.is_empty() {
                continue;
            }
            let value = json::parse(&line).map_err(|error| {
                io::Error::new(
                    io::ErrorKind::InvalidData,
                    format!("Invalid ACP HTTP worker output: {error}"),
                )
            })?;
            self.sender
                .send(value)
                .map_err(|_| io::Error::new(io::ErrorKind::BrokenPipe, "ACP HTTP client closed"))?;
        }
        Ok(())
    }
}

impl Write for AcpHttpChannelWriter {
    fn write(&mut self, input: &[u8]) -> io::Result<usize> {
        let text = std::str::from_utf8(input).map_err(|error| {
            io::Error::new(
                io::ErrorKind::InvalidData,
                format!("Invalid ACP HTTP worker output bytes: {error}"),
            )
        })?;
        self.buffer.push_str(text);
        self.flush_lines()?;
        Ok(input.len())
    }

    fn flush(&mut self) -> io::Result<()> {
        self.flush_lines()
    }
}

pub(super) fn acp_http_authorized(request: &str) -> bool {
    let Ok(token) = env::var("IACCODE_ACP_HTTP_TOKEN") else {
        return true;
    };
    if token.is_empty() {
        return true;
    }
    http_header_value(request, "authorization")
        .and_then(|value| value.strip_prefix("Bearer "))
        .is_some_and(|value| value == token)
}

pub(super) fn write_acp_http_missing_connection(stream: &mut TcpStream) -> Result<(), String> {
    write_json_http_response(
        stream,
        400,
        &[],
        &json::object([(
            "error",
            json::string(
                "Connection not found. Send 'initialize' first or provide a valid Acp-Connection-Id header.",
            ),
        )]),
    )
}
