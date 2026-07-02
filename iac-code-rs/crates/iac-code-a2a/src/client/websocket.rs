use std::time::Duration;

use futures_util::{SinkExt, StreamExt};
use iac_code_protocol::json::{self, JsonValue};
use tokio_tungstenite::tungstenite::client::IntoClientRequest;
use tokio_tungstenite::tungstenite::http::{HeaderName, HeaderValue};
use tokio_tungstenite::tungstenite::Message;

use crate::transport::{headers_for_auth, A2AAuthConfig};

use super::{json_bool_field, object_field};

pub(super) fn send_websocket_jsonrpc_payload(
    url: &str,
    payload: &JsonValue,
    auth: Option<&A2AAuthConfig>,
    timeout_seconds: Option<f64>,
) -> Result<JsonValue, String> {
    websocket_runtime()?.block_on(async {
        let mut stream = connect_websocket_async(url, auth, timeout_seconds).await?;
        websocket_wait(
            timeout_seconds,
            stream.send(Message::Text(payload.to_compact_json().into())),
            "A2A WebSocket send timed out.",
        )
        .await?
        .map_err(|error| error.to_string())?;
        let frame = read_websocket_text_message(&mut stream, timeout_seconds)
            .await?
            .ok_or_else(|| "A2A WebSocket transport closed without a response".to_owned())?;
        websocket_payload_from_frame(json::parse(&frame)?)
    })
}

pub(super) fn stream_websocket_jsonrpc_payload(
    url: &str,
    payload: &JsonValue,
    auth: Option<&A2AAuthConfig>,
    timeout_seconds: Option<f64>,
) -> Result<Vec<JsonValue>, String> {
    websocket_runtime()?.block_on(async {
        let mut stream = connect_websocket_async(url, auth, timeout_seconds).await?;
        websocket_wait(
            timeout_seconds,
            stream.send(Message::Text(payload.to_compact_json().into())),
            "A2A WebSocket send timed out.",
        )
        .await?
        .map_err(|error| error.to_string())?;
        let mut events = Vec::new();
        while let Some(frame_text) =
            read_websocket_text_message(&mut stream, timeout_seconds).await?
        {
            let frame = json::parse(&frame_text)?;
            let frame_final = json_bool_field(&frame, "final").unwrap_or(false);
            let payload = websocket_payload_from_frame(frame)?;
            let payload_final = json_bool_field(&payload, "final")
                .or_else(|| {
                    object_field(&payload, "result")
                        .and_then(|result| json_bool_field(result, "final"))
                })
                .unwrap_or(false);
            events.push(payload);
            if frame_final || payload_final {
                break;
            }
        }
        Ok(events)
    })
}

fn websocket_runtime() -> Result<tokio::runtime::Runtime, String> {
    tokio::runtime::Builder::new_current_thread()
        .enable_all()
        .build()
        .map_err(|error| error.to_string())
}

async fn connect_websocket_async(
    url: &str,
    auth: Option<&A2AAuthConfig>,
    timeout_seconds: Option<f64>,
) -> Result<
    tokio_tungstenite::WebSocketStream<tokio_tungstenite::MaybeTlsStream<tokio::net::TcpStream>>,
    String,
> {
    let mut request = url
        .into_client_request()
        .map_err(|error| format!("Invalid WebSocket A2A URL: {error}"))?;
    for (key, value) in headers_for_auth(auth) {
        let name = HeaderName::from_bytes(key.as_bytes()).map_err(|error| error.to_string())?;
        let value = HeaderValue::from_str(&value).map_err(|error| error.to_string())?;
        request.headers_mut().insert(name, value);
    }
    let (stream, _) = websocket_wait(
        timeout_seconds,
        tokio_tungstenite::connect_async(request),
        "A2A WebSocket connect timed out.",
    )
    .await?
    .map_err(|error| error.to_string())?;
    Ok(stream)
}

async fn read_websocket_text_message(
    stream: &mut tokio_tungstenite::WebSocketStream<
        tokio_tungstenite::MaybeTlsStream<tokio::net::TcpStream>,
    >,
    timeout_seconds: Option<f64>,
) -> Result<Option<String>, String> {
    loop {
        let Some(message) = websocket_wait(
            timeout_seconds,
            stream.next(),
            "A2A WebSocket receive timed out.",
        )
        .await?
        else {
            return Ok(None);
        };
        match message.map_err(|error| error.to_string())? {
            Message::Text(text) => return Ok(Some(text.to_string())),
            Message::Close(_) => return Ok(None),
            Message::Ping(_) | Message::Pong(_) => {}
            other => {
                return Err(format!(
                    "Unsupported WebSocket message type: {}",
                    websocket_message_kind(&other)
                ));
            }
        }
    }
}

async fn websocket_wait<T>(
    timeout_seconds: Option<f64>,
    future: impl std::future::Future<Output = T>,
    timeout_message: &str,
) -> Result<T, String> {
    if let Some(timeout) = timeout_seconds {
        return tokio::time::timeout(Duration::from_secs_f64(timeout.max(0.0)), future)
            .await
            .map_err(|_| timeout_message.to_owned());
    }
    Ok(future.await)
}

fn websocket_message_kind(message: &Message) -> &'static str {
    match message {
        Message::Text(_) => "text",
        Message::Binary(_) => "binary",
        Message::Ping(_) => "ping",
        Message::Pong(_) => "pong",
        Message::Close(_) => "close",
        Message::Frame(_) => "frame",
    }
}

fn websocket_payload_from_frame(frame: JsonValue) -> Result<JsonValue, String> {
    let Some(payload) = object_field(&frame, "payload") else {
        return Err("A2A WebSocket frame is missing payload.".to_owned());
    };
    if !matches!(payload, JsonValue::Object(_)) {
        return Err("A2A WebSocket response payload must be a JSON object.".to_owned());
    }
    Ok(payload.clone())
}
