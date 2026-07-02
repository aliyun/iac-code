use iac_code_a2a::artifacts::A2AArtifactStore;
use iac_code_a2a::push::A2APushConfigStore;
use iac_code_a2a::task_store::{A2ATaskStore, SdkTask};
use iac_code_config::cloud_credentials::{AliyunCredential, DEFAULT_REGION};
use iac_code_exec::EXIT_OK;
use iac_code_protocol::{json, json::JsonValue};

use crate::a2a_artifacts::a2a_artifacts_from_events;
use crate::a2a_client_format::extract_a2a_message_text;
use crate::a2a_payload::{a2a_sdk_task_json, current_unix_seconds, new_a2a_server_id};
use crate::a2a_push::{enqueue_a2a_task_status_push, A2APushQueueRuntime};
use crate::headless_runner::{a2a_headless_error_message, run_a2a_server_headless};
use crate::json_utils::{json_bool_field, json_object_field, json_string_field};
use crate::jsonrpc_payload::{jsonrpc_error, jsonrpc_result};

pub(super) struct A2ASendMessageContext<'a> {
    pub(super) task_store: &'a mut A2ATaskStore,
    pub(super) push_config_store: &'a A2APushConfigStore,
    pub(super) push_queue: Option<&'a mut A2APushQueueRuntime>,
    pub(super) artifact_store: &'a A2AArtifactStore,
    pub(super) auto_approve_permissions: bool,
    pub(super) log_to_stdout: bool,
}

pub(super) fn handle_a2a_send_message(
    params: Option<&JsonValue>,
    request_id: JsonValue,
    context: A2ASendMessageContext<'_>,
) -> JsonValue {
    let params = params.unwrap_or(&JsonValue::Null);
    let Some(message) = json_object_field(params, "message") else {
        return jsonrpc_error(request_id, -32602, "Missing message");
    };
    let task_id = json_string_field(message, "taskId")
        .or_else(|| json_string_field(message, "task_id"))
        .map(ToOwned::to_owned);
    let context_id = json_string_field(message, "contextId")
        .or_else(|| json_string_field(message, "context_id"))
        .map(ToOwned::to_owned)
        .unwrap_or_else(|| new_a2a_server_id("ctx"));
    let cwd = a2a_message_iac_metadata_string(message, "cwd").unwrap_or(".");
    let model_override = a2a_message_iac_metadata_string(message, "iac_code_model");
    let aliyun_credential_override = a2a_message_aliyun_credential(message);
    let return_immediately = json_object_field(params, "configuration")
        .and_then(|configuration| {
            json_bool_field(configuration, "returnImmediately")
                .or_else(|| json_bool_field(configuration, "return_immediately"))
        })
        .unwrap_or(false);

    if let Err(error) = context.task_store.get_or_create_context(&context_id, cwd) {
        return jsonrpc_error(request_id, -32602, &error.to_string());
    }
    let task_id = match context
        .task_store
        .get_or_create_task(task_id.as_deref(), &context_id)
    {
        Ok(record) => record.task_id.clone(),
        Err(error) => return jsonrpc_error(request_id, -32602, &error.to_string()),
    };
    if let Err(error) = context
        .task_store
        .set_task_active(&task_id, return_immediately)
    {
        return jsonrpc_error(request_id, -32602, &error.to_string());
    }

    let task = if return_immediately {
        SdkTask::new(
            &task_id,
            &context_id,
            "TASK_STATE_WORKING",
            current_unix_seconds(),
        )
    } else {
        match run_a2a_server_headless(
            &extract_a2a_message_text(message),
            cwd,
            model_override,
            aliyun_credential_override,
            context.auto_approve_permissions,
        ) {
            Ok(result) if result.exit_code == EXIT_OK => {
                let mut task = SdkTask::new(
                    &task_id,
                    &context_id,
                    "TASK_STATE_INPUT_REQUIRED",
                    current_unix_seconds(),
                )
                .with_status_message(result.stdout.trim_end_matches('\n'));
                task.artifacts = a2a_artifacts_from_events(&result.events, context.artifact_store);
                task
            }
            Ok(result) => SdkTask::new(
                &task_id,
                &context_id,
                "TASK_STATE_FAILED",
                current_unix_seconds(),
            )
            .with_status_message(a2a_headless_error_message(&result)),
            Err(error) => SdkTask::new(
                &task_id,
                &context_id,
                "TASK_STATE_FAILED",
                current_unix_seconds(),
            )
            .with_status_message(error),
        }
    };
    context.task_store.save_sdk_task(task.clone(), "");
    enqueue_a2a_task_status_push(
        context.push_config_store,
        context.push_queue,
        &task,
        context.log_to_stdout,
    );
    jsonrpc_result(
        request_id,
        json::object([("task", a2a_sdk_task_json(&task, true))]),
    )
}

pub(super) fn a2a_message_iac_metadata_string<'a>(
    message: &'a JsonValue,
    key: &str,
) -> Option<&'a str> {
    json_object_field(message, "metadata")
        .and_then(|metadata| json_object_field(metadata, "iac_code"))
        .and_then(|metadata| json_string_field(metadata, key))
        .map(str::trim)
        .filter(|value| !value.is_empty())
}

pub(super) fn a2a_message_aliyun_credential(message: &JsonValue) -> Option<AliyunCredential> {
    let access_key_id = a2a_message_iac_metadata_string(message, "alibaba_cloud_access_key_id")?;
    let access_key_secret =
        a2a_message_iac_metadata_string(message, "alibaba_cloud_access_key_secret")?;
    let sts_token =
        a2a_message_iac_metadata_string(message, "alibaba_cloud_security_token").unwrap_or("");
    Some(AliyunCredential {
        mode: if sts_token.is_empty() {
            "AK".into()
        } else {
            "StsToken".into()
        },
        access_key_id: access_key_id.to_owned(),
        access_key_secret: access_key_secret.to_owned(),
        region_id: a2a_message_iac_metadata_string(message, "alibaba_cloud_region_id")
            .unwrap_or(DEFAULT_REGION)
            .to_owned(),
        sts_token: sts_token.to_owned(),
        ..AliyunCredential::default()
    })
}
