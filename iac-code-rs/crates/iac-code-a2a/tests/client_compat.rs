use iac_code_a2a::client::{
    merge_jwks, send_jsonrpc_payload, A2AClient, A2AClientResponse, PushConfigRequest,
};
use iac_code_protocol::json::{self, JsonValue};

#[test]
fn response_text_extracts_python_priority_order() {
    assert_eq!(
        A2AClientResponse {
            payload: json::object([("result", json::object([("text", json::string("done"))]))])
        }
        .text(),
        "done"
    );
    assert_eq!(
        A2AClientResponse {
            payload: json::object([(
                "result",
                json::object([("status", message_status("status ", "text"),)])
            )])
        }
        .text(),
        "status text"
    );
    assert_eq!(
        A2AClientResponse {
            payload: json::object([(
                "result",
                json::object([("message", message("ROLE_AGENT", &["message ", "parts"]))])
            )])
        }
        .text(),
        "message parts"
    );
    assert_eq!(
        A2AClientResponse {
            payload: json::object([(
                "result",
                json::object([(
                    "task",
                    json::object([("status", message_status("task ", "status"))])
                )])
            )])
        }
        .text(),
        "task status"
    );
    assert_eq!(
        A2AClientResponse {
            payload: json::object([(
                "result",
                json::object([(
                    "task",
                    json::object([(
                        "history",
                        json::array([
                            message("ROLE_USER", &["hello"]),
                            message("ROLE_AGENT", &["first ", "answer"]),
                            message("ROLE_USER", &["follow up"]),
                            message("ROLE_AGENT", &["final answer"]),
                        ])
                    ),])
                )])
            )])
        }
        .text(),
        "final answer"
    );
    assert_eq!(
        A2AClientResponse {
            payload: json::object([("result", json::object([("text", json::string(""))]))])
        }
        .text(),
        ""
    );
    assert_eq!(
        A2AClientResponse {
            payload: JsonValue::Object(Default::default())
        }
        .text(),
        ""
    );
}

#[test]
fn client_selects_endpoint_url_like_python() {
    let card = json::object([
        ("url", json::string("http://fallback.example/rpc")),
        (
            "supportedInterfaces",
            json::array([json::object([
                ("url", json::string("http://card.example/a2a")),
                ("protocolBinding", json::string("JSONRPC")),
                ("protocolVersion", json::string("1.0")),
            ])]),
        ),
    ]);

    assert_eq!(
        A2AClient::select_endpoint_url(&card, "http://input.example/"),
        "http://card.example/a2a"
    );

    let card = json::object([
        ("url", json::string("http://fallback.example/rpc")),
        (
            "supportedInterfaces",
            json::array([
                json::object([
                    ("url", json::string("nats://broker/iac-code")),
                    ("protocolBinding", json::string("nats")),
                ]),
                json::object([
                    ("url", json::string("http://card.example/runnable")),
                    ("protocolBinding", json::string("JSONRPC")),
                ]),
            ]),
        ),
    ]);
    assert_eq!(
        A2AClient::select_endpoint_url(&card, "http://input.example/"),
        "http://card.example/runnable"
    );

    assert_eq!(
        A2AClient::select_endpoint_url(
            &json::object([("url", json::string("http://card.example/rpc"))]),
            "http://input.example/",
        ),
        "http://card.example/rpc"
    );
    assert_eq!(
        A2AClient::select_endpoint_url(
            &json::object([("name", json::string("remote"))]),
            "http://input.example/"
        ),
        "http://input.example/"
    );
}

#[test]
fn message_payload_matches_a2a_jsonrpc_shape() {
    let payload = A2AClient::message_payload(
        "SendMessage",
        "hello",
        "/tmp/work",
        Some("ctx-1"),
        Some(" qwen3.7-max "),
        "message-1",
        "rpc-1",
    );

    assert_eq!(string_at(&payload, &["jsonrpc"]), "2.0");
    assert_eq!(string_at(&payload, &["id"]), "rpc-1");
    assert_eq!(string_at(&payload, &["method"]), "SendMessage");
    assert_eq!(
        string_at(&payload, &["params", "message", "messageId"]),
        "message-1"
    );
    assert_eq!(
        string_at(&payload, &["params", "message", "role"]),
        "ROLE_USER"
    );
    assert_eq!(
        string_at(&payload, &["params", "message", "parts", "0", "text"]),
        "hello"
    );
    assert_eq!(
        string_at(
            &payload,
            &["params", "message", "metadata", "iac_code", "cwd"]
        ),
        "/tmp/work"
    );
    assert_eq!(
        string_at(
            &payload,
            &[
                "params",
                "message",
                "metadata",
                "iac_code",
                "iac_code_model"
            ]
        ),
        "qwen3.7-max"
    );
    assert_eq!(
        string_at(&payload, &["params", "message", "contextId"]),
        "ctx-1"
    );
    assert_eq!(
        strings_at(
            &payload,
            &["params", "configuration", "acceptedOutputModes"]
        ),
        vec!["text/plain"]
    );
}

#[test]
fn task_and_push_payload_helpers_drop_none_values() {
    assert_eq!(
        A2AClient::get_task_payload("task-1", Some(2), "rpc-1").to_compact_json(),
        r#"{"id":"rpc-1","jsonrpc":"2.0","method":"GetTask","params":{"historyLength":2,"id":"task-1"}}"#
    );
    assert_eq!(
        A2AClient::list_tasks_payload(
            Some("ctx-1"),
            Some("TASK_STATE_WORKING"),
            Some(10),
            None,
            Some(false),
            "rpc-2",
        )
        .to_compact_json(),
        r#"{"id":"rpc-2","jsonrpc":"2.0","method":"ListTasks","params":{"contextId":"ctx-1","includeArtifacts":false,"pageSize":10,"status":"TASK_STATE_WORKING"}}"#
    );

    let payload = A2AClient::create_push_notification_config_payload(
        PushConfigRequest {
            task_id: "task-1",
            config_id: "cfg-1",
            url: "https://callback.example/a2a",
            token: Some("token-1"),
            authentication: Some(json::object([
                ("scheme", json::string("bearer")),
                ("credentials", json::string("secret")),
            ])),
        },
        "rpc-3",
    );

    assert_eq!(
        string_at(&payload, &["params", "authentication", "scheme"]),
        "bearer"
    );
    assert_eq!(string_at(&payload, &["params", "id"]), "cfg-1");
}

#[test]
fn task_and_push_payload_helpers_match_jsonrpc_methods() {
    assert_eq!(
        A2AClient::jsonrpc_payload("Ping", json::object([("x", json::number(1))]), "rpc-0")
            .to_compact_json(),
        r#"{"id":"rpc-0","jsonrpc":"2.0","method":"Ping","params":{"x":1}}"#
    );
    assert_eq!(
        A2AClient::cancel_task_payload("task-1", "rpc-4").to_compact_json(),
        r#"{"id":"rpc-4","jsonrpc":"2.0","method":"CancelTask","params":{"id":"task-1"}}"#
    );
    assert_eq!(
        A2AClient::subscribe_task_payload("task-1", "rpc-5").to_compact_json(),
        r#"{"id":"rpc-5","jsonrpc":"2.0","method":"SubscribeToTask","params":{"id":"task-1"}}"#
    );
    assert_eq!(
        A2AClient::get_push_notification_config_payload("task-1", "cfg-1", "rpc-6")
            .to_compact_json(),
        r#"{"id":"rpc-6","jsonrpc":"2.0","method":"GetTaskPushNotificationConfig","params":{"id":"cfg-1","taskId":"task-1"}}"#
    );
    assert_eq!(
        A2AClient::list_push_notification_configs_payload(
            "task-1",
            Some(20),
            Some("page-2"),
            "rpc-7",
        )
        .to_compact_json(),
        r#"{"id":"rpc-7","jsonrpc":"2.0","method":"ListTaskPushNotificationConfigs","params":{"pageSize":20,"pageToken":"page-2","taskId":"task-1"}}"#
    );
    assert_eq!(
        A2AClient::delete_push_notification_config_payload("task-1", "cfg-1", "rpc-8")
            .to_compact_json(),
        r#"{"id":"rpc-8","jsonrpc":"2.0","method":"DeleteTaskPushNotificationConfig","params":{"id":"cfg-1","taskId":"task-1"}}"#
    );
    assert_eq!(
        A2AClient::get_extended_agent_card_payload("rpc-9").to_compact_json(),
        r#"{"id":"rpc-9","jsonrpc":"2.0","method":"GetExtendedAgentCard","params":{}}"#
    );
}

#[test]
fn websocket_client_accepts_wss_scheme_like_python_binding_selection() {
    let payload = A2AClient::get_extended_agent_card_payload("rpc-wss");

    let error = send_jsonrpc_payload("wss://127.0.0.1:9/a2a", &payload, None, Some(0.01))
        .expect_err("closed port should fail after scheme acceptance");

    assert!(
        !error.contains("wss A2A WebSocket URLs are not supported"),
        "{error}"
    );
}

#[test]
fn merge_jwks_combines_only_key_arrays() {
    let remote = json::object([(
        "keys",
        json::array([json::object([("kid", json::string("remote"))])]),
    )]);
    let local = json::object([(
        "keys",
        json::array([json::object([("kid", json::string("local"))])]),
    )]);
    let ignored = json::object([("keys", json::string("bad"))]);

    assert_eq!(
        merge_jwks(&[Some(&remote), Some(&ignored), Some(&local)])
            .expect("merged")
            .to_compact_json(),
        r#"{"keys":[{"kid":"remote"},{"kid":"local"}]}"#
    );
    assert!(merge_jwks(&[Some(&ignored), None]).is_none());
}

fn message_status(left: &str, right: &str) -> JsonValue {
    json::object([("message", message("ROLE_AGENT", &[left, right]))])
}

fn message(role: &str, parts: &[&str]) -> JsonValue {
    json::object([
        ("role", json::string(role)),
        (
            "parts",
            json::array(
                parts
                    .iter()
                    .map(|part| json::object([("text", json::string(*part))])),
            ),
        ),
    ])
}

fn string_at(value: &JsonValue, path: &[&str]) -> String {
    match at(value, path) {
        JsonValue::String(value) => value.clone(),
        other => panic!("expected string at {path:?}, got {other:?}"),
    }
}

fn strings_at(value: &JsonValue, path: &[&str]) -> Vec<String> {
    match at(value, path) {
        JsonValue::Array(values) => values.iter().map(|value| string_at(value, &[])).collect(),
        other => panic!("expected array at {path:?}, got {other:?}"),
    }
}

fn at<'a>(mut value: &'a JsonValue, path: &[&str]) -> &'a JsonValue {
    for segment in path {
        value = match value {
            JsonValue::Object(object) => object.get(*segment).unwrap_or_else(|| {
                panic!("missing object key {segment:?} in path {path:?}");
            }),
            JsonValue::Array(values) => values
                .get(segment.parse::<usize>().expect("array index"))
                .unwrap_or_else(|| panic!("missing array index {segment:?} in path {path:?}")),
            other => panic!("cannot descend into {other:?} at {segment:?}"),
        };
    }
    value
}
