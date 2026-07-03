use iac_code_a2a::push::A2APushConfigStore;
use iac_code_a2a::task_store::{A2ATaskStore, ListTasksRequest, SdkTask};
use iac_code_protocol::{json, json::JsonValue};

use crate::a2a_payload::{a2a_sdk_task_json, current_unix_seconds};
use crate::a2a_push::{enqueue_a2a_task_status_push, A2APushQueueRuntime};
use crate::a2a_response::A2AJsonRpcResponse;
use crate::json_utils::{json_bool_field, json_number_usize_field, json_string_field};
use crate::jsonrpc_payload::{jsonrpc_error, jsonrpc_result};

pub(super) fn handle_a2a_get_task(
    params: Option<&JsonValue>,
    request_id: JsonValue,
    task_store: &A2ATaskStore,
) -> JsonValue {
    let params = params.unwrap_or(&JsonValue::Null);
    let Some(task_id) = json_string_field(params, "id") else {
        return jsonrpc_error(request_id, -32602, "Missing task id");
    };
    let Some(task) = task_store.get_sdk_task(task_id, "") else {
        return jsonrpc_error(request_id, -32000, &format!("Task {task_id} not found"));
    };
    jsonrpc_result(request_id, a2a_sdk_task_json(&task, true))
}

pub(super) fn handle_a2a_list_tasks(
    params: Option<&JsonValue>,
    request_id: JsonValue,
    task_store: &A2ATaskStore,
) -> JsonValue {
    let params = params.unwrap_or(&JsonValue::Null);
    let request = ListTasksRequest {
        context_id: json_string_field(params, "contextId")
            .or_else(|| json_string_field(params, "context_id"))
            .map(ToOwned::to_owned),
        status: json_string_field(params, "status").map(ToOwned::to_owned),
        page_size: json_number_usize_field(params, "pageSize")
            .or_else(|| json_number_usize_field(params, "page_size")),
        page_token: json_string_field(params, "pageToken")
            .or_else(|| json_string_field(params, "page_token"))
            .map(ToOwned::to_owned),
        include_artifacts: json_bool_field(params, "includeArtifacts")
            .or_else(|| json_bool_field(params, "include_artifacts"))
            .unwrap_or(false),
    };
    let response = match task_store.list_sdk_tasks(request, "") {
        Ok(response) => response,
        Err(error) => return jsonrpc_error(request_id, -32602, &error.to_string()),
    };
    jsonrpc_result(
        request_id,
        json::object([
            (
                "tasks",
                json::array(
                    response
                        .tasks
                        .iter()
                        .map(|task| a2a_sdk_task_json(task, true)),
                ),
            ),
            ("nextPageToken", json::string(response.next_page_token)),
            ("pageSize", json::number(response.page_size)),
            ("totalSize", json::number(response.total_size)),
        ]),
    )
}

pub(super) fn handle_a2a_cancel_task(
    params: Option<&JsonValue>,
    request_id: JsonValue,
    task_store: &mut A2ATaskStore,
    push_config_store: &A2APushConfigStore,
    push_queue: Option<&mut A2APushQueueRuntime>,
    log_to_stdout: bool,
) -> JsonValue {
    let params = params.unwrap_or(&JsonValue::Null);
    let Some(task_id) = json_string_field(params, "id") else {
        return jsonrpc_error(request_id, -32602, "Missing task id");
    };
    let Some(existing) = task_store.get_sdk_task(task_id, "") else {
        return jsonrpc_error(request_id, -32000, &format!("Task {task_id} not found"));
    };
    if !task_store.cancel_task(task_id) {
        return jsonrpc_error(request_id, -32000, "Task cannot be canceled");
    }
    let task = SdkTask::new(
        existing.id,
        existing.context_id,
        "TASK_STATE_CANCELED",
        current_unix_seconds(),
    );
    task_store.save_sdk_task(task.clone(), "");
    enqueue_a2a_task_status_push(push_config_store, push_queue, &task, log_to_stdout);
    jsonrpc_result(request_id, a2a_sdk_task_json(&task, true))
}

pub(super) fn handle_a2a_subscribe_task(
    params: Option<&JsonValue>,
    request_id: JsonValue,
    task_store: &A2ATaskStore,
) -> A2AJsonRpcResponse {
    let params = params.unwrap_or(&JsonValue::Null);
    let Some(task_id) = json_string_field(params, "id") else {
        return A2AJsonRpcResponse::Json(jsonrpc_error(request_id, -32602, "Missing task id"));
    };
    let Some(task) = task_store.get_sdk_task(task_id, "") else {
        return A2AJsonRpcResponse::Json(jsonrpc_error(
            request_id,
            -32000,
            &format!("Task {task_id} not found"),
        ));
    };
    if !task_store.is_task_active(task_id) {
        return A2AJsonRpcResponse::Json(jsonrpc_error(
            request_id,
            -32000,
            &format!("Task {task_id} is not active"),
        ));
    }
    let mut result = a2a_sdk_task_json(&task, true);
    if let JsonValue::Object(fields) = &mut result {
        fields.insert("final".to_owned(), json::bool_value(true));
    }
    A2AJsonRpcResponse::Sse(vec![jsonrpc_result(request_id, result)])
}
