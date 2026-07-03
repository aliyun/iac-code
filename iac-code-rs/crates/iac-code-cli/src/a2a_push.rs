use std::path::Path;

use iac_code_a2a::dispatcher::{
    list_push_notification_configs, ListPushNotificationConfigsRequest,
};
use iac_code_a2a::push::{
    A2APushAuthentication, A2APushConfigStore, A2APushQueueSink, A2APushSender,
    TaskPushNotificationConfig,
};
use iac_code_a2a::push_queue::{LocalFileA2APushQueue, PushQueueError, RedisStreamsA2APushQueue};
use iac_code_a2a::push_secrets::A2APushSecretKeyring;
use iac_code_a2a::task_store::{A2ATaskStore, SdkTask};
use iac_code_protocol::{json, json::JsonValue};

use crate::a2a_payload::{a2a_push_config_json, new_a2a_server_id};
use crate::a2a_redis::RedisConnectionPushStore;
use crate::a2a_server_args::A2AServerArgs;
use crate::a2a_server_runtime::log_a2a_server_error;
use crate::json_utils::{json_number_usize_field, json_object_field, json_string_field};
use crate::jsonrpc_payload::{jsonrpc_error, jsonrpc_result};

pub(super) enum A2APushQueueRuntime {
    LocalFile(LocalFileA2APushQueue),
    Redis(Box<RedisStreamsA2APushQueue<RedisConnectionPushStore>>),
}

impl A2APushQueueSink for A2APushQueueRuntime {
    fn enqueue(&mut self, job: iac_code_a2a::push_queue::A2APushJob) -> Result<(), PushQueueError> {
        match self {
            Self::LocalFile(queue) => queue.enqueue(job),
            Self::Redis(queue) => queue.enqueue(job),
        }
    }
}

pub(super) fn build_a2a_push_queue(
    args: &A2AServerArgs,
    persistence_root: &Path,
) -> Result<Option<A2APushQueueRuntime>, String> {
    if !args.push_notifications {
        return Ok(None);
    }
    let secret_keyring = A2APushSecretKeyring::new(persistence_root.join("push_keys.json"));
    match args.push_queue.as_str() {
        "local-file" => Ok(Some(A2APushQueueRuntime::LocalFile(
            LocalFileA2APushQueue::new(persistence_root.join("push_queue"))
                .with_secret_keyring(secret_keyring),
        ))),
        "redis-streams" => {
            let store = RedisConnectionPushStore::from_url(&args.push_redis_url)?;
            let consumer_name = if args.push_consumer_name.is_empty() {
                format!("consumer-{}", new_a2a_server_id("push"))
            } else {
                args.push_consumer_name.clone()
            };
            Ok(Some(A2APushQueueRuntime::Redis(Box::new(
                RedisStreamsA2APushQueue::new(
                    store,
                    &args.push_stream,
                    &args.push_retry_key,
                    &args.push_dead_stream,
                    &args.push_consumer_group,
                    consumer_name,
                )
                .with_lease_timeout_ms(args.push_lease_timeout_ms)
                .with_owns_redis(true)
                .with_secret_keyring(secret_keyring),
            ))))
        }
        _ => Err("--push-queue must be local-file or redis-streams.".to_owned()),
    }
}

pub(super) fn enqueue_a2a_task_status_push(
    push_config_store: &A2APushConfigStore,
    push_queue: Option<&mut A2APushQueueRuntime>,
    task: &SdkTask,
    log_to_stdout: bool,
) {
    let Some(push_queue) = push_queue else {
        return;
    };
    let payload = a2a_task_status_push_payload(task);
    let mut sender = A2APushSender::new(push_config_store, push_queue);
    if let Err(error) = sender.send_notification(&task.id, payload) {
        log_a2a_server_error(
            log_to_stdout,
            &format!("A2A push notification failed: {error}"),
        );
    }
}

fn a2a_task_status_push_payload(task: &SdkTask) -> JsonValue {
    json::object([(
        "statusUpdate",
        json::object([
            ("taskId", json::string(&task.id)),
            ("contextId", json::string(&task.context_id)),
            (
                "status",
                json::object([
                    ("state", json::string(&task.status)),
                    (
                        "timestamp",
                        task.status_timestamp.map_or_else(json::null, json::number),
                    ),
                ]),
            ),
            (
                "final",
                json::bool_value(matches!(
                    task.status.as_str(),
                    "TASK_STATE_COMPLETED"
                        | "TASK_STATE_CANCELED"
                        | "TASK_STATE_FAILED"
                        | "TASK_STATE_INPUT_REQUIRED"
                )),
            ),
        ]),
    )])
}

pub(super) fn handle_a2a_create_push_config(
    params: Option<&JsonValue>,
    request_id: JsonValue,
    task_store: &A2ATaskStore,
    push_config_store: &mut A2APushConfigStore,
) -> JsonValue {
    let params = params.unwrap_or(&JsonValue::Null);
    let Some(task_id) =
        json_string_field(params, "taskId").or_else(|| json_string_field(params, "task_id"))
    else {
        return jsonrpc_error(request_id, -32602, "Missing task id");
    };
    if let Some(error) = missing_a2a_task_error(task_store, task_id) {
        return jsonrpc_error(request_id, -32000, &error);
    }
    let Some(callback_url) = json_string_field(params, "url") else {
        return jsonrpc_error(request_id, -32602, "Missing callback URL");
    };
    let config_id = json_string_field(params, "id").unwrap_or_default();
    let mut config = TaskPushNotificationConfig::new(config_id, callback_url);
    config.token = json_string_field(params, "token")
        .unwrap_or_default()
        .to_owned();
    config.authentication = json_object_field(params, "authentication").map(|authentication| {
        A2APushAuthentication::new(
            json_string_field(authentication, "scheme").unwrap_or_default(),
            json_string_field(authentication, "credentials").unwrap_or_default(),
        )
    });
    if push_config_store.set_info("", task_id, config).is_err() {
        return jsonrpc_error(request_id, -32602, "Invalid push notification config");
    }
    let Some(config) = find_push_config(push_config_store, task_id, config_id) else {
        return jsonrpc_error(request_id, -32000, "Push notification config not found");
    };
    jsonrpc_result(request_id, a2a_push_config_json(&config))
}

pub(super) fn handle_a2a_get_push_config(
    params: Option<&JsonValue>,
    request_id: JsonValue,
    task_store: &A2ATaskStore,
    push_config_store: &A2APushConfigStore,
) -> JsonValue {
    let params = params.unwrap_or(&JsonValue::Null);
    let Some(task_id) =
        json_string_field(params, "taskId").or_else(|| json_string_field(params, "task_id"))
    else {
        return jsonrpc_error(request_id, -32602, "Missing task id");
    };
    if let Some(error) = missing_a2a_task_error(task_store, task_id) {
        return jsonrpc_error(request_id, -32000, &error);
    }
    let Some(config_id) = json_string_field(params, "id") else {
        return jsonrpc_error(request_id, -32602, "Missing config id");
    };
    let Some(config) = find_push_config(push_config_store, task_id, config_id) else {
        return jsonrpc_error(request_id, -32000, "Push notification config not found");
    };
    jsonrpc_result(request_id, a2a_push_config_json(&config))
}

pub(super) fn handle_a2a_list_push_configs(
    params: Option<&JsonValue>,
    request_id: JsonValue,
    task_store: &A2ATaskStore,
    push_config_store: &A2APushConfigStore,
) -> JsonValue {
    let params = params.unwrap_or(&JsonValue::Null);
    let Some(task_id) =
        json_string_field(params, "taskId").or_else(|| json_string_field(params, "task_id"))
    else {
        return jsonrpc_error(request_id, -32602, "Missing task id");
    };
    if let Some(error) = missing_a2a_task_error(task_store, task_id) {
        return jsonrpc_error(request_id, -32000, &error);
    }
    let configs = match push_config_store.get_info("", task_id) {
        Ok(configs) => configs,
        Err(_) => return jsonrpc_error(request_id, -32602, "Invalid push notification config"),
    };
    let page = match list_push_notification_configs(
        configs,
        ListPushNotificationConfigsRequest {
            task_id: task_id.to_owned(),
            page_size: json_number_usize_field(params, "pageSize")
                .or_else(|| json_number_usize_field(params, "page_size")),
            page_token: json_string_field(params, "pageToken")
                .or_else(|| json_string_field(params, "page_token"))
                .map(ToOwned::to_owned),
        },
    ) {
        Ok(page) => page,
        Err(error) => return jsonrpc_error(request_id, -32602, &error.to_string()),
    };
    jsonrpc_result(
        request_id,
        json::object([
            (
                "configs",
                json::array(page.configs.iter().map(a2a_push_config_json)),
            ),
            ("nextPageToken", json::string(page.next_page_token)),
        ]),
    )
}

pub(super) fn handle_a2a_delete_push_config(
    params: Option<&JsonValue>,
    request_id: JsonValue,
    task_store: &A2ATaskStore,
    push_config_store: &mut A2APushConfigStore,
) -> JsonValue {
    let params = params.unwrap_or(&JsonValue::Null);
    let Some(task_id) =
        json_string_field(params, "taskId").or_else(|| json_string_field(params, "task_id"))
    else {
        return jsonrpc_error(request_id, -32602, "Missing task id");
    };
    if let Some(error) = missing_a2a_task_error(task_store, task_id) {
        return jsonrpc_error(request_id, -32000, &error);
    }
    let Some(config_id) = json_string_field(params, "id") else {
        return jsonrpc_error(request_id, -32602, "Missing config id");
    };
    if push_config_store
        .delete_info("", task_id, Some(config_id))
        .is_err()
    {
        return jsonrpc_error(request_id, -32602, "Invalid push notification config");
    }
    jsonrpc_result(request_id, JsonValue::Null)
}

fn missing_a2a_task_error(task_store: &A2ATaskStore, task_id: &str) -> Option<String> {
    task_store
        .get_sdk_task(task_id, "")
        .is_none()
        .then(|| format!("Task {task_id} not found"))
}

fn find_push_config(
    push_config_store: &A2APushConfigStore,
    task_id: &str,
    config_id: &str,
) -> Option<TaskPushNotificationConfig> {
    push_config_store
        .get_info("", task_id)
        .ok()?
        .into_iter()
        .find(|config| config.id == config_id)
}
