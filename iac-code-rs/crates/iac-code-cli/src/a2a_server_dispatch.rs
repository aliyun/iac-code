use iac_code_a2a::artifacts::A2AArtifactStore;
use iac_code_a2a::push::A2APushConfigStore;
use iac_code_a2a::task_store::A2ATaskStore;
use iac_code_protocol::{json, json::JsonValue};

use crate::a2a_messages::{handle_a2a_send_message, A2ASendMessageContext};
use crate::a2a_push::{
    handle_a2a_create_push_config, handle_a2a_delete_push_config, handle_a2a_get_push_config,
    handle_a2a_list_push_configs, A2APushQueueRuntime,
};
use crate::a2a_response::A2AJsonRpcResponse;
use crate::a2a_server_runtime::A2AServerRuntime;
use crate::a2a_tasks::{
    handle_a2a_cancel_task, handle_a2a_get_task, handle_a2a_list_tasks, handle_a2a_subscribe_task,
};
use crate::json_utils::{json_object_field, json_string_value};
use crate::jsonrpc_payload::{jsonrpc_error, jsonrpc_result};

struct A2ADispatchContext<'a> {
    card: &'a JsonValue,
    task_store: &'a mut A2ATaskStore,
    push_config_store: &'a mut A2APushConfigStore,
    push_queue: Option<&'a mut A2APushQueueRuntime>,
    artifact_store: &'a A2AArtifactStore,
    auto_approve_permissions: bool,
    log_to_stdout: bool,
}

pub(super) fn dispatch_a2a_rest_message_send(
    body: &str,
    runtime: &mut A2AServerRuntime,
) -> JsonValue {
    let mut context = a2a_dispatch_context(runtime);
    handle_a2a_rest_message_send(body, &mut context)
}

fn handle_a2a_rest_message_send(body: &str, context: &mut A2ADispatchContext<'_>) -> JsonValue {
    let params = match json::parse(body) {
        Ok(JsonValue::Object(params)) => JsonValue::Object(params),
        Ok(_) | Err(_) => return json::object([("error", json::string("Parse error"))]),
    };
    let response = handle_a2a_send_message(
        Some(&params),
        JsonValue::Null,
        a2a_send_message_context(context),
    );
    json_object_field(&response, "result")
        .cloned()
        .or_else(|| json_object_field(&response, "error").cloned())
        .unwrap_or(response)
}

fn handle_a2a_jsonrpc(body: &str, context: &mut A2ADispatchContext<'_>) -> A2AJsonRpcResponse {
    let request = match json::parse(body) {
        Ok(JsonValue::Object(request)) => request,
        Ok(_) | Err(_) => {
            return A2AJsonRpcResponse::Json(jsonrpc_error(JsonValue::Null, -32700, "Parse error"));
        }
    };
    let request_id = request.get("id").cloned().unwrap_or(JsonValue::Null);
    let method = request
        .get("method")
        .and_then(json_string_value)
        .unwrap_or_default();
    let response = match method {
        "GetExtendedAgentCard" | "agent/getAuthenticatedExtendedCard" => {
            jsonrpc_result(request_id, context.card.clone())
        }
        "SendMessage" | "message/send" => handle_a2a_send_message(
            request.get("params"),
            request_id,
            a2a_send_message_context(context),
        ),
        "SendStreamingMessage" | "StreamMessage" | "message/stream" => {
            return A2AJsonRpcResponse::Sse(vec![handle_a2a_send_message(
                request.get("params"),
                request_id,
                a2a_send_message_context(context),
            )]);
        }
        "GetTask" | "tasks/get" => {
            handle_a2a_get_task(request.get("params"), request_id, context.task_store)
        }
        "ListTasks" => handle_a2a_list_tasks(request.get("params"), request_id, context.task_store),
        "CancelTask" | "tasks/cancel" => handle_a2a_cancel_task(
            request.get("params"),
            request_id,
            context.task_store,
            context.push_config_store,
            context.push_queue.as_deref_mut(),
            context.log_to_stdout,
        ),
        "SubscribeToTask" | "tasks/resubscribe" => {
            return handle_a2a_subscribe_task(
                request.get("params"),
                request_id,
                context.task_store,
            );
        }
        "CreateTaskPushNotificationConfig" | "tasks/pushNotificationConfig/set" => {
            handle_a2a_create_push_config(
                request.get("params"),
                request_id,
                context.task_store,
                context.push_config_store,
            )
        }
        "GetTaskPushNotificationConfig" | "tasks/pushNotificationConfig/get" => {
            handle_a2a_get_push_config(
                request.get("params"),
                request_id,
                context.task_store,
                context.push_config_store,
            )
        }
        "ListTaskPushNotificationConfigs" | "tasks/pushNotificationConfig/list" => {
            handle_a2a_list_push_configs(
                request.get("params"),
                request_id,
                context.task_store,
                context.push_config_store,
            )
        }
        "DeleteTaskPushNotificationConfig" | "tasks/pushNotificationConfig/delete" => {
            handle_a2a_delete_push_config(
                request.get("params"),
                request_id,
                context.task_store,
                context.push_config_store,
            )
        }
        _ => jsonrpc_error(request_id, -32601, "Method not found"),
    };
    A2AJsonRpcResponse::Json(response)
}

pub(super) fn dispatch_a2a_jsonrpc_value(
    payload: &JsonValue,
    runtime: &mut A2AServerRuntime,
) -> A2AJsonRpcResponse {
    dispatch_a2a_jsonrpc_body(&payload.to_compact_json(), runtime)
}

pub(super) fn dispatch_a2a_jsonrpc_body(
    body: &str,
    runtime: &mut A2AServerRuntime,
) -> A2AJsonRpcResponse {
    let mut context = a2a_dispatch_context(runtime);
    handle_a2a_jsonrpc(body, &mut context)
}

fn a2a_dispatch_context(runtime: &mut A2AServerRuntime) -> A2ADispatchContext<'_> {
    A2ADispatchContext {
        card: &runtime.card,
        task_store: &mut runtime.task_store,
        push_config_store: &mut runtime.push_config_store,
        push_queue: runtime.push_queue.as_mut(),
        artifact_store: &runtime.artifact_store,
        auto_approve_permissions: runtime.auto_approve_permissions,
        log_to_stdout: runtime.log_to_stdout,
    }
}

fn a2a_send_message_context<'a>(
    context: &'a mut A2ADispatchContext<'_>,
) -> A2ASendMessageContext<'a> {
    A2ASendMessageContext {
        task_store: context.task_store,
        push_config_store: context.push_config_store,
        push_queue: context.push_queue.as_deref_mut(),
        artifact_store: context.artifact_store,
        auto_approve_permissions: context.auto_approve_permissions,
        log_to_stdout: context.log_to_stdout,
    }
}
