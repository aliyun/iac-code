use iac_code_a2a::dispatcher::{
    decode_dispatch_response_json, decode_dispatch_sse_line, list_push_notification_configs,
    validate_cancel_task_request, validate_subscribe_task_request,
    ListPushNotificationConfigsRequest,
};
use iac_code_a2a::push::TaskPushNotificationConfig;
use iac_code_a2a::task_store::{A2ATaskStore, SdkTask};
use iac_code_protocol::json::{self, JsonValue};

#[test]
fn dispatcher_unary_response_must_be_json_object_like_python() {
    let payload = json::object([("result", json::object([("ok", json::bool_value(true))]))]);

    assert_eq!(
        decode_dispatch_response_json(payload.clone()).expect("object"),
        payload
    );
    assert_eq!(
        decode_dispatch_response_json(json::array([json::number(1)]))
            .unwrap_err()
            .to_string(),
        "A2A dispatcher response must be a JSON object"
    );
}

#[test]
fn dispatcher_stream_decodes_only_sse_data_lines() {
    assert!(decode_dispatch_sse_line("").expect("blank").is_none());
    assert!(decode_dispatch_sse_line("event: message")
        .expect("event")
        .is_none());

    let event = decode_dispatch_sse_line("data: {\"result\":{\"final\":true}}")
        .expect("decode")
        .expect("data");

    assert!(bool_at(&event, &["result", "final"]));
}

#[test]
fn dispatcher_stream_allows_non_object_json_events_like_python_json_loads() {
    assert_eq!(
        decode_dispatch_sse_line("data: [1,2]")
            .expect("decode")
            .expect("data")
            .to_compact_json(),
        "[1,2]"
    );
}

#[test]
fn dispatcher_stream_reports_invalid_data_line_json() {
    let error = decode_dispatch_sse_line("data: {broken").expect_err("invalid");

    assert!(error
        .to_string()
        .starts_with("Invalid A2A dispatcher stream event: "));
}

#[test]
fn request_handler_lists_push_notification_configs_sorted_and_paginated_like_python() {
    let configs = vec![
        push_config("cfg-c"),
        push_config("cfg-a"),
        push_config("cfg-b"),
    ];

    let first = list_push_notification_configs(
        configs.clone(),
        ListPushNotificationConfigsRequest {
            task_id: "task-1".to_owned(),
            page_size: Some(2),
            page_token: None,
        },
    )
    .expect("first page");

    assert_eq!(config_ids(&first.configs), vec!["cfg-a", "cfg-b"]);
    assert_eq!(first.next_page_token, "Y2ZnLWM=");

    let second = list_push_notification_configs(
        configs.clone(),
        ListPushNotificationConfigsRequest {
            task_id: "task-1".to_owned(),
            page_size: Some(2),
            page_token: Some(first.next_page_token),
        },
    )
    .expect("second page");

    assert_eq!(config_ids(&second.configs), vec!["cfg-c"]);
    assert_eq!(second.next_page_token, "");

    let error = list_push_notification_configs(
        configs,
        ListPushNotificationConfigsRequest {
            task_id: "task-1".to_owned(),
            page_size: Some(2),
            page_token: Some("bWlzc2luZw==".to_owned()),
        },
    )
    .expect_err("missing page token");

    assert_eq!(error.to_string(), "Invalid page token: bWlzc2luZw==");
}

#[test]
fn request_handler_validates_cancel_task_state_like_python() {
    let active = task_store_with_task("task-1", true);
    validate_cancel_task_request(&active, "", "task-1").expect("active task is cancelable");

    assert_eq!(
        validate_cancel_task_request(&active, "", "missing")
            .expect_err("missing")
            .to_string(),
        "Task missing not found"
    );

    let inactive = task_store_with_task("task-1", false);
    assert_eq!(
        validate_cancel_task_request(&inactive, "", "task-1")
            .expect_err("inactive")
            .to_string(),
        "Task cannot be canceled"
    );
}

#[test]
fn request_handler_validates_subscribe_task_state_like_python() {
    let active = task_store_with_task("task-1", true);
    validate_subscribe_task_request(&active, "", "task-1").expect("active task can be subscribed");

    assert_eq!(
        validate_subscribe_task_request(&active, "", "missing")
            .expect_err("missing")
            .to_string(),
        "Task missing not found"
    );

    let inactive = task_store_with_task("task-1", false);
    assert_eq!(
        validate_subscribe_task_request(&inactive, "", "task-1")
            .expect_err("inactive")
            .to_string(),
        "Task task-1 is not active"
    );
}

fn bool_at(value: &JsonValue, path: &[&str]) -> bool {
    match at(value, path) {
        JsonValue::Bool(value) => *value,
        other => panic!("expected bool at {path:?}, got {other:?}"),
    }
}

fn push_config(id: &str) -> TaskPushNotificationConfig {
    TaskPushNotificationConfig {
        task_id: "task-1".to_owned(),
        id: id.to_owned(),
        url: format!("https://{id}.example/a2a"),
        token: String::new(),
        authentication: None,
    }
}

fn config_ids(configs: &[TaskPushNotificationConfig]) -> Vec<&str> {
    configs.iter().map(|config| config.id.as_str()).collect()
}

fn task_store_with_task(task_id: &str, active: bool) -> A2ATaskStore {
    let mut store = A2ATaskStore::new();
    store
        .get_or_create_context("ctx-1", "/workspace")
        .expect("context");
    store
        .get_or_create_task(Some(task_id), "ctx-1")
        .expect("task");
    store.save_sdk_task(SdkTask::new(task_id, "ctx-1", "working", 1), "");
    store
        .set_task_active(task_id, active)
        .expect("set active flag");
    store
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
