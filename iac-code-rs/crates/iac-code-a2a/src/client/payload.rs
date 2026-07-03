use std::collections::BTreeMap;

use iac_code_protocol::json::{self, JsonValue};

use super::A2AClient;

#[derive(Clone, Debug, PartialEq)]
pub struct PushConfigRequest<'a> {
    pub task_id: &'a str,
    pub config_id: &'a str,
    pub url: &'a str,
    pub token: Option<&'a str>,
    pub authentication: Option<JsonValue>,
}

impl A2AClient {
    pub fn jsonrpc_payload(method: &str, params: JsonValue, request_id: &str) -> JsonValue {
        json::object([
            ("jsonrpc", json::string("2.0")),
            ("id", json::string(request_id)),
            ("method", json::string(method)),
            ("params", params),
        ])
    }

    pub fn message_payload(
        method: &str,
        prompt: &str,
        cwd: &str,
        context_id: Option<&str>,
        model: Option<&str>,
        message_id: &str,
        request_id: &str,
    ) -> JsonValue {
        let mut iac_code_metadata = BTreeMap::from([("cwd".to_owned(), json::string(cwd))]);
        if let Some(model) = model.map(str::trim).filter(|value| !value.is_empty()) {
            iac_code_metadata.insert("iac_code_model".to_owned(), json::string(model));
        }

        let mut message = BTreeMap::new();
        message.insert("messageId".to_owned(), json::string(message_id));
        message.insert("role".to_owned(), json::string("ROLE_USER"));
        message.insert(
            "parts".to_owned(),
            json::array([json::object([("text", json::string(prompt))])]),
        );
        message.insert(
            "metadata".to_owned(),
            json::object([("iac_code", JsonValue::Object(iac_code_metadata))]),
        );
        if let Some(context_id) = context_id.filter(|value| !value.is_empty()) {
            message.insert("contextId".to_owned(), json::string(context_id));
        }

        Self::jsonrpc_payload(
            method,
            json::object([
                ("message", JsonValue::Object(message)),
                (
                    "configuration",
                    json::object([(
                        "acceptedOutputModes",
                        json::array([json::string("text/plain")]),
                    )]),
                ),
            ]),
            request_id,
        )
    }

    pub fn get_task_payload(
        task_id: &str,
        history_length: Option<u64>,
        request_id: &str,
    ) -> JsonValue {
        let mut params = BTreeMap::new();
        params.insert("id".to_owned(), json::string(task_id));
        if let Some(history_length) = history_length {
            params.insert("historyLength".to_owned(), json::number(history_length));
        }
        Self::jsonrpc_payload("GetTask", JsonValue::Object(params), request_id)
    }

    pub fn list_tasks_payload(
        context_id: Option<&str>,
        status: Option<&str>,
        page_size: Option<u64>,
        page_token: Option<&str>,
        include_artifacts: Option<bool>,
        request_id: &str,
    ) -> JsonValue {
        let mut params = BTreeMap::new();
        insert_optional_string(&mut params, "contextId", context_id);
        insert_optional_string(&mut params, "status", status);
        if let Some(page_size) = page_size {
            params.insert("pageSize".to_owned(), json::number(page_size));
        }
        insert_optional_string(&mut params, "pageToken", page_token);
        if let Some(include_artifacts) = include_artifacts {
            params.insert(
                "includeArtifacts".to_owned(),
                json::bool_value(include_artifacts),
            );
        }
        Self::jsonrpc_payload("ListTasks", JsonValue::Object(params), request_id)
    }

    pub fn cancel_task_payload(task_id: &str, request_id: &str) -> JsonValue {
        Self::jsonrpc_payload(
            "CancelTask",
            json::object([("id", json::string(task_id))]),
            request_id,
        )
    }

    pub fn subscribe_task_payload(task_id: &str, request_id: &str) -> JsonValue {
        Self::jsonrpc_payload(
            "SubscribeToTask",
            json::object([("id", json::string(task_id))]),
            request_id,
        )
    }

    pub fn create_push_notification_config_payload(
        request: PushConfigRequest<'_>,
        request_id: &str,
    ) -> JsonValue {
        let mut params = BTreeMap::new();
        params.insert("taskId".to_owned(), json::string(request.task_id));
        params.insert("id".to_owned(), json::string(request.config_id));
        params.insert("url".to_owned(), json::string(request.url));
        insert_optional_string(&mut params, "token", request.token);
        if let Some(authentication) = request.authentication {
            params.insert("authentication".to_owned(), authentication);
        }
        Self::jsonrpc_payload(
            "CreateTaskPushNotificationConfig",
            JsonValue::Object(params),
            request_id,
        )
    }

    pub fn get_push_notification_config_payload(
        task_id: &str,
        config_id: &str,
        request_id: &str,
    ) -> JsonValue {
        Self::jsonrpc_payload(
            "GetTaskPushNotificationConfig",
            json::object([
                ("taskId", json::string(task_id)),
                ("id", json::string(config_id)),
            ]),
            request_id,
        )
    }

    pub fn list_push_notification_configs_payload(
        task_id: &str,
        page_size: Option<u64>,
        page_token: Option<&str>,
        request_id: &str,
    ) -> JsonValue {
        let mut params = BTreeMap::new();
        params.insert("taskId".to_owned(), json::string(task_id));
        if let Some(page_size) = page_size {
            params.insert("pageSize".to_owned(), json::number(page_size));
        }
        insert_optional_string(&mut params, "pageToken", page_token);
        Self::jsonrpc_payload(
            "ListTaskPushNotificationConfigs",
            JsonValue::Object(params),
            request_id,
        )
    }

    pub fn delete_push_notification_config_payload(
        task_id: &str,
        config_id: &str,
        request_id: &str,
    ) -> JsonValue {
        Self::jsonrpc_payload(
            "DeleteTaskPushNotificationConfig",
            json::object([
                ("taskId", json::string(task_id)),
                ("id", json::string(config_id)),
            ]),
            request_id,
        )
    }

    pub fn get_extended_agent_card_payload(request_id: &str) -> JsonValue {
        Self::jsonrpc_payload(
            "GetExtendedAgentCard",
            JsonValue::Object(BTreeMap::new()),
            request_id,
        )
    }
}

fn insert_optional_string(
    params: &mut BTreeMap<String, JsonValue>,
    key: &str,
    value: Option<&str>,
) {
    if let Some(value) = value {
        params.insert(key.to_owned(), json::string(value));
    }
}
