use std::collections::HashMap;

use base64::engine::general_purpose::STANDARD;
use base64::Engine;
use iac_code_a2a::proto::a2a as a2a_proto;
use iac_code_protocol::json::JsonValue;
use prost_types::{
    value as proto_value, ListValue as ProtoListValue, Struct as ProtoStruct, Value as ProtoValue,
};

use crate::a2a_grpc_convert::{box_status, BoxedStatusResult};
use crate::json_utils::{
    json_bool_field, json_number_i32_field, json_object_field, json_string_field,
    json_string_or_empty, json_string_value,
};

pub(super) fn a2a_proto_send_message_response_from_result(
    result: &JsonValue,
) -> BoxedStatusResult<a2a_proto::SendMessageResponse> {
    if let Some(task) = a2a_proto_task_result(result) {
        return Ok(a2a_proto::SendMessageResponse {
            payload: Some(a2a_proto::send_message_response::Payload::Task(
                a2a_proto_task_from_json(task),
            )),
        });
    }
    if let Some(message) = a2a_proto_message_result(result) {
        return Ok(a2a_proto::SendMessageResponse {
            payload: Some(a2a_proto::send_message_response::Payload::Message(
                a2a_proto_message_from_json(message),
            )),
        });
    }
    Err(box_status(tonic::Status::internal(
        "A2A SendMessage result did not contain a task or message",
    )))
}

pub(super) fn a2a_proto_stream_response_from_result(
    result: &JsonValue,
) -> BoxedStatusResult<a2a_proto::StreamResponse> {
    if let Some(task) = a2a_proto_task_result(result) {
        return Ok(a2a_proto::StreamResponse {
            payload: Some(a2a_proto::stream_response::Payload::Task(
                a2a_proto_task_from_json(task),
            )),
        });
    }
    if let Some(message) = a2a_proto_message_result(result) {
        return Ok(a2a_proto::StreamResponse {
            payload: Some(a2a_proto::stream_response::Payload::Message(
                a2a_proto_message_from_json(message),
            )),
        });
    }
    Err(box_status(tonic::Status::internal(
        "A2A stream result did not contain a task or message",
    )))
}

fn a2a_proto_task_result(result: &JsonValue) -> Option<&JsonValue> {
    json_object_field(result, "task").or_else(|| json_object_field(result, "id").map(|_| result))
}

fn a2a_proto_message_result(result: &JsonValue) -> Option<&JsonValue> {
    json_object_field(result, "message")
        .or_else(|| json_object_field(result, "messageId").map(|_| result))
}

pub(super) fn a2a_proto_task_from_json(value: &JsonValue) -> a2a_proto::Task {
    a2a_proto::Task {
        id: json_string_or_empty(value, "id").to_owned(),
        context_id: json_string_or_empty(value, "contextId").to_owned(),
        status: json_object_field(value, "status").map(a2a_proto_task_status_from_json),
        artifacts: json_array_field(value, "artifacts")
            .iter()
            .map(|artifact| a2a_proto_artifact_from_json(artifact))
            .collect(),
        history: json_array_field(value, "history")
            .iter()
            .map(|message| a2a_proto_message_from_json(message))
            .collect(),
        metadata: json_object_field(value, "metadata").map(a2a_proto_struct_from_json),
    }
}

fn a2a_proto_task_status_from_json(value: &JsonValue) -> a2a_proto::TaskStatus {
    a2a_proto::TaskStatus {
        state: json_string_field(value, "state")
            .and_then(a2a_proto::TaskState::from_str_name)
            .unwrap_or(a2a_proto::TaskState::Unspecified) as i32,
        message: json_object_field(value, "message").map(a2a_proto_message_from_json),
        timestamp: json_string_field(value, "timestamp")
            .and_then(|timestamp| timestamp.parse::<i64>().ok())
            .map(|seconds| prost_types::Timestamp { seconds, nanos: 0 }),
    }
}

fn a2a_proto_message_from_json(value: &JsonValue) -> a2a_proto::Message {
    a2a_proto::Message {
        message_id: json_string_or_empty(value, "messageId").to_owned(),
        context_id: json_string_or_empty(value, "contextId").to_owned(),
        task_id: json_string_or_empty(value, "taskId").to_owned(),
        role: json_string_field(value, "role")
            .and_then(a2a_proto::Role::from_str_name)
            .unwrap_or(a2a_proto::Role::Unspecified) as i32,
        parts: json_array_field(value, "parts")
            .iter()
            .map(|part| a2a_proto_part_from_json(part))
            .collect(),
        metadata: json_object_field(value, "metadata").map(a2a_proto_struct_from_json),
        extensions: json_string_array_field(value, "extensions"),
        reference_task_ids: json_string_array_field(value, "referenceTaskIds"),
    }
}

fn a2a_proto_part_from_json(value: &JsonValue) -> a2a_proto::Part {
    let content = if let Some(text) = json_string_field(value, "text") {
        Some(a2a_proto::part::Content::Text(text.to_owned()))
    } else if let Some(raw) = json_string_field(value, "bytes")
        .or_else(|| json_string_field(value, "base64"))
        .or_else(|| json_string_field(value, "raw"))
    {
        Some(a2a_proto::part::Content::Raw(
            STANDARD
                .decode(raw)
                .unwrap_or_else(|_| raw.as_bytes().to_vec()),
        ))
    } else if let Some(url) = json_string_field(value, "url") {
        Some(a2a_proto::part::Content::Url(url.to_owned()))
    } else {
        json_object_field(value, "data")
            .or_else(|| json_object_field(value, "json"))
            .map(|data| a2a_proto::part::Content::Data(a2a_proto_value_from_json(data)))
    };
    a2a_proto::Part {
        content,
        metadata: json_object_field(value, "metadata").map(a2a_proto_struct_from_json),
        filename: json_string_or_empty(value, "filename").to_owned(),
        media_type: json_string_or_empty(value, "mediaType").to_owned(),
    }
}

fn a2a_proto_artifact_from_json(value: &JsonValue) -> a2a_proto::Artifact {
    a2a_proto::Artifact {
        artifact_id: json_string_or_empty(value, "artifactId").to_owned(),
        name: json_string_or_empty(value, "name").to_owned(),
        description: json_string_or_empty(value, "description").to_owned(),
        parts: json_array_field(value, "parts")
            .iter()
            .map(|part| a2a_proto_part_from_json(part))
            .collect(),
        metadata: json_object_field(value, "metadata").map(a2a_proto_struct_from_json),
        extensions: json_string_array_field(value, "extensions"),
    }
}

pub(super) fn a2a_proto_list_tasks_response_from_result(
    result: &JsonValue,
) -> a2a_proto::ListTasksResponse {
    a2a_proto::ListTasksResponse {
        tasks: json_array_field(result, "tasks")
            .iter()
            .map(|task| a2a_proto_task_from_json(task))
            .collect(),
        next_page_token: json_string_or_empty(result, "nextPageToken").to_owned(),
        page_size: json_number_i32_field(result, "pageSize").unwrap_or_default(),
        total_size: json_number_i32_field(result, "totalSize").unwrap_or_default(),
    }
}

pub(super) fn a2a_proto_push_config_from_json(
    value: &JsonValue,
) -> a2a_proto::TaskPushNotificationConfig {
    a2a_proto::TaskPushNotificationConfig {
        tenant: json_string_or_empty(value, "tenant").to_owned(),
        id: json_string_or_empty(value, "id").to_owned(),
        task_id: json_string_or_empty(value, "taskId").to_owned(),
        url: json_string_or_empty(value, "url").to_owned(),
        token: json_string_or_empty(value, "token").to_owned(),
        authentication: json_object_field(value, "authentication").map(|authentication| {
            a2a_proto::AuthenticationInfo {
                scheme: json_string_or_empty(authentication, "scheme").to_owned(),
                credentials: json_string_or_empty(authentication, "credentials").to_owned(),
            }
        }),
    }
}

pub(super) fn a2a_proto_list_push_configs_response_from_result(
    result: &JsonValue,
) -> a2a_proto::ListTaskPushNotificationConfigsResponse {
    a2a_proto::ListTaskPushNotificationConfigsResponse {
        configs: json_array_field(result, "configs")
            .iter()
            .map(|config| a2a_proto_push_config_from_json(config))
            .collect(),
        next_page_token: json_string_or_empty(result, "nextPageToken").to_owned(),
    }
}

pub(super) fn a2a_proto_agent_card_from_json(value: &JsonValue) -> a2a_proto::AgentCard {
    a2a_proto::AgentCard {
        name: json_string_or_empty(value, "name").to_owned(),
        description: json_string_or_empty(value, "description").to_owned(),
        supported_interfaces: json_array_field(value, "supportedInterfaces")
            .iter()
            .map(|interface| a2a_proto::AgentInterface {
                url: json_string_or_empty(interface, "url").to_owned(),
                protocol_binding: json_string_or_empty(interface, "protocolBinding").to_owned(),
                tenant: json_string_or_empty(interface, "tenant").to_owned(),
                protocol_version: json_string_or_empty(interface, "protocolVersion").to_owned(),
            })
            .collect(),
        provider: json_object_field(value, "provider").map(|provider| a2a_proto::AgentProvider {
            url: json_string_or_empty(provider, "url").to_owned(),
            organization: json_string_or_empty(provider, "organization").to_owned(),
        }),
        version: json_string_or_empty(value, "version").to_owned(),
        documentation_url: optional_json_string_field(value, "documentationUrl"),
        capabilities: json_object_field(value, "capabilities")
            .map(a2a_proto_capabilities_from_json),
        security_schemes: a2a_proto_security_schemes_from_json(value),
        security_requirements: a2a_proto_security_requirements_from_json(
            json_object_field(value, "securityRequirements").unwrap_or(&JsonValue::Null),
        ),
        default_input_modes: json_string_array_field(value, "defaultInputModes"),
        default_output_modes: json_string_array_field(value, "defaultOutputModes"),
        skills: json_array_field(value, "skills")
            .iter()
            .map(|skill| a2a_proto::AgentSkill {
                id: json_string_or_empty(skill, "id").to_owned(),
                name: json_string_or_empty(skill, "name").to_owned(),
                description: json_string_or_empty(skill, "description").to_owned(),
                tags: json_string_array_field(skill, "tags"),
                examples: json_string_array_field(skill, "examples"),
                input_modes: json_string_array_field(skill, "inputModes"),
                output_modes: json_string_array_field(skill, "outputModes"),
                security_requirements: a2a_proto_security_requirements_from_json(
                    json_object_field(skill, "securityRequirements").unwrap_or(&JsonValue::Null),
                ),
            })
            .collect(),
        signatures: json_array_field(value, "signatures")
            .iter()
            .map(|signature| a2a_proto::AgentCardSignature {
                protected: json_string_or_empty(signature, "protected").to_owned(),
                signature: json_string_or_empty(signature, "signature").to_owned(),
                header: json_object_field(signature, "header").map(a2a_proto_struct_from_json),
            })
            .collect(),
        icon_url: optional_json_string_field(value, "iconUrl"),
    }
}

fn a2a_proto_capabilities_from_json(value: &JsonValue) -> a2a_proto::AgentCapabilities {
    a2a_proto::AgentCapabilities {
        streaming: json_bool_field(value, "streaming"),
        push_notifications: json_bool_field(value, "pushNotifications"),
        extensions: json_array_field(value, "extensions")
            .iter()
            .map(|extension| a2a_proto::AgentExtension {
                uri: json_string_or_empty(extension, "uri").to_owned(),
                description: json_string_or_empty(extension, "description").to_owned(),
                required: json_bool_field(extension, "required").unwrap_or(false),
                params: json_object_field(extension, "params").map(a2a_proto_struct_from_json),
            })
            .collect(),
        extended_agent_card: json_bool_field(value, "extendedAgentCard"),
    }
}

fn a2a_proto_security_schemes_from_json(
    value: &JsonValue,
) -> HashMap<String, a2a_proto::SecurityScheme> {
    let Some(JsonValue::Object(schemes)) = json_object_field(value, "securitySchemes") else {
        return HashMap::new();
    };
    schemes
        .iter()
        .map(|(name, scheme)| (name.clone(), a2a_proto_security_scheme_from_json(scheme)))
        .collect()
}

fn a2a_proto_security_scheme_from_json(value: &JsonValue) -> a2a_proto::SecurityScheme {
    let scheme = if let Some(api_key) = json_object_field(value, "apiKeySecurityScheme") {
        Some(a2a_proto::security_scheme::Scheme::ApiKeySecurityScheme(
            a2a_proto::ApiKeySecurityScheme {
                description: json_string_or_empty(api_key, "description").to_owned(),
                location: json_string_or_empty(api_key, "location").to_owned(),
                name: json_string_or_empty(api_key, "name").to_owned(),
            },
        ))
    } else if let Some(http_auth) = json_object_field(value, "httpAuthSecurityScheme") {
        Some(a2a_proto::security_scheme::Scheme::HttpAuthSecurityScheme(
            a2a_proto::HttpAuthSecurityScheme {
                description: json_string_or_empty(http_auth, "description").to_owned(),
                scheme: json_string_or_empty(http_auth, "scheme").to_owned(),
                bearer_format: json_string_or_empty(http_auth, "bearerFormat").to_owned(),
            },
        ))
    } else if let Some(open_id) = json_object_field(value, "openIdConnectSecurityScheme") {
        Some(
            a2a_proto::security_scheme::Scheme::OpenIdConnectSecurityScheme(
                a2a_proto::OpenIdConnectSecurityScheme {
                    description: json_string_or_empty(open_id, "description").to_owned(),
                    open_id_connect_url: json_string_or_empty(open_id, "openIdConnectUrl")
                        .to_owned(),
                },
            ),
        )
    } else if let Some(mtls) = json_object_field(value, "mtlsSecurityScheme") {
        Some(a2a_proto::security_scheme::Scheme::MtlsSecurityScheme(
            a2a_proto::MutualTlsSecurityScheme {
                description: json_string_or_empty(mtls, "description").to_owned(),
            },
        ))
    } else {
        json_object_field(value, "oauth2SecurityScheme").map(|oauth2| {
            a2a_proto::security_scheme::Scheme::Oauth2SecurityScheme(
                a2a_proto::OAuth2SecurityScheme {
                    description: json_string_or_empty(oauth2, "description").to_owned(),
                    flows: None,
                    oauth2_metadata_url: json_string_or_empty(oauth2, "oauth2MetadataUrl")
                        .to_owned(),
                },
            )
        })
    };
    a2a_proto::SecurityScheme { scheme }
}

fn a2a_proto_security_requirements_from_json(
    value: &JsonValue,
) -> Vec<a2a_proto::SecurityRequirement> {
    let JsonValue::Array(requirements) = value else {
        return Vec::new();
    };
    requirements
        .iter()
        .filter_map(|requirement| {
            let JsonValue::Object(fields) = requirement else {
                return None;
            };
            let Some(JsonValue::Object(schemes)) = fields.get("schemes") else {
                return None;
            };
            Some(a2a_proto::SecurityRequirement {
                schemes: schemes
                    .iter()
                    .map(|(name, value)| {
                        (
                            name.clone(),
                            a2a_proto::StringList {
                                list: json_string_array_field(value, "list"),
                            },
                        )
                    })
                    .collect(),
            })
        })
        .collect()
}

fn a2a_proto_struct_from_json(value: &JsonValue) -> ProtoStruct {
    let JsonValue::Object(fields) = value else {
        return ProtoStruct::default();
    };
    ProtoStruct {
        fields: fields
            .iter()
            .map(|(key, value)| (key.clone(), a2a_proto_value_from_json(value)))
            .collect(),
    }
}

fn a2a_proto_value_from_json(value: &JsonValue) -> ProtoValue {
    let kind = match value {
        JsonValue::Null => proto_value::Kind::NullValue(0),
        JsonValue::Bool(value) => proto_value::Kind::BoolValue(*value),
        JsonValue::Number(value) => proto_value::Kind::NumberValue(value.parse().unwrap_or(0.0)),
        JsonValue::String(value) => proto_value::Kind::StringValue(value.clone()),
        JsonValue::Array(values) => proto_value::Kind::ListValue(ProtoListValue {
            values: values.iter().map(a2a_proto_value_from_json).collect(),
        }),
        JsonValue::Object(_) => proto_value::Kind::StructValue(a2a_proto_struct_from_json(value)),
    };
    ProtoValue { kind: Some(kind) }
}

fn json_array_field<'a>(value: &'a JsonValue, key: &str) -> Vec<&'a JsonValue> {
    match json_object_field(value, key) {
        Some(JsonValue::Array(values)) => values.iter().collect(),
        _ => Vec::new(),
    }
}

fn json_string_array_field(value: &JsonValue, key: &str) -> Vec<String> {
    match json_object_field(value, key) {
        Some(JsonValue::Array(values)) => values
            .iter()
            .filter_map(json_string_value)
            .map(ToOwned::to_owned)
            .collect(),
        _ => Vec::new(),
    }
}

fn optional_json_string_field(value: &JsonValue, key: &str) -> Option<String> {
    json_string_field(value, key)
        .filter(|value| !value.is_empty())
        .map(ToOwned::to_owned)
}

pub(super) fn a2a_proto_task_state_name(value: i32) -> Option<&'static str> {
    a2a_proto::TaskState::try_from(value)
        .ok()
        .map(|value| value.as_str_name())
}
