use std::collections::BTreeMap;

use base64::engine::general_purpose::STANDARD;
use base64::Engine;
use iac_code_a2a::proto::a2a as a2a_proto;
use iac_code_protocol::{json, json::JsonValue};
use prost_types::{value as proto_value, Struct as ProtoStruct, Value as ProtoValue};

pub(super) fn a2a_json_send_message_params(request: a2a_proto::SendMessageRequest) -> JsonValue {
    let mut params = BTreeMap::new();
    if let Some(message) = request.message {
        params.insert("message".to_owned(), a2a_json_message_from_proto(message));
    }
    if let Some(configuration) = request.configuration {
        params.insert(
            "configuration".to_owned(),
            a2a_json_send_message_configuration_from_proto(configuration),
        );
    }
    if let Some(metadata) = request.metadata {
        params.insert("metadata".to_owned(), a2a_json_struct_from_proto(metadata));
    }
    JsonValue::Object(params)
}

fn a2a_json_send_message_configuration_from_proto(
    configuration: a2a_proto::SendMessageConfiguration,
) -> JsonValue {
    let mut fields = BTreeMap::new();
    if !configuration.accepted_output_modes.is_empty() {
        fields.insert(
            "acceptedOutputModes".to_owned(),
            json::array(
                configuration
                    .accepted_output_modes
                    .into_iter()
                    .map(json::string),
            ),
        );
    }
    if let Some(history_length) = configuration.history_length {
        fields.insert("historyLength".to_owned(), json::number(history_length));
    }
    fields.insert(
        "returnImmediately".to_owned(),
        json::bool_value(configuration.return_immediately),
    );
    if let Some(config) = configuration.task_push_notification_config {
        fields.insert(
            "taskPushNotificationConfig".to_owned(),
            a2a_json_push_config_params(config),
        );
    }
    JsonValue::Object(fields)
}

fn a2a_json_message_from_proto(message: a2a_proto::Message) -> JsonValue {
    let mut fields = BTreeMap::new();
    fields.insert("messageId".to_owned(), json::string(message.message_id));
    if !message.context_id.is_empty() {
        fields.insert("contextId".to_owned(), json::string(message.context_id));
    }
    if !message.task_id.is_empty() {
        fields.insert("taskId".to_owned(), json::string(message.task_id));
    }
    if let Some(role) = a2a_proto_role_name(message.role) {
        fields.insert("role".to_owned(), json::string(role));
    }
    fields.insert(
        "parts".to_owned(),
        json::array(message.parts.into_iter().map(a2a_json_part_from_proto)),
    );
    if let Some(metadata) = message.metadata {
        fields.insert("metadata".to_owned(), a2a_json_struct_from_proto(metadata));
    }
    if !message.extensions.is_empty() {
        fields.insert(
            "extensions".to_owned(),
            json::array(message.extensions.into_iter().map(json::string)),
        );
    }
    if !message.reference_task_ids.is_empty() {
        fields.insert(
            "referenceTaskIds".to_owned(),
            json::array(message.reference_task_ids.into_iter().map(json::string)),
        );
    }
    JsonValue::Object(fields)
}

fn a2a_json_part_from_proto(part: a2a_proto::Part) -> JsonValue {
    let mut fields = BTreeMap::new();
    match part.content {
        Some(a2a_proto::part::Content::Text(text)) => {
            fields.insert("text".to_owned(), json::string(text));
        }
        Some(a2a_proto::part::Content::Raw(raw)) => {
            fields.insert("bytes".to_owned(), json::string(STANDARD.encode(raw)));
        }
        Some(a2a_proto::part::Content::Url(url)) => {
            fields.insert("url".to_owned(), json::string(url));
        }
        Some(a2a_proto::part::Content::Data(data)) => {
            fields.insert("data".to_owned(), a2a_json_value_from_proto(data));
        }
        None => {}
    }
    if let Some(metadata) = part.metadata {
        fields.insert("metadata".to_owned(), a2a_json_struct_from_proto(metadata));
    }
    if !part.filename.is_empty() {
        fields.insert("filename".to_owned(), json::string(part.filename));
    }
    if !part.media_type.is_empty() {
        fields.insert("mediaType".to_owned(), json::string(part.media_type));
    }
    JsonValue::Object(fields)
}

pub(super) fn a2a_json_push_config_params(
    config: a2a_proto::TaskPushNotificationConfig,
) -> JsonValue {
    let mut fields = BTreeMap::from([
        ("taskId".to_owned(), json::string(config.task_id)),
        ("id".to_owned(), json::string(config.id)),
        ("url".to_owned(), json::string(config.url)),
    ]);
    if !config.token.is_empty() {
        fields.insert("token".to_owned(), json::string(config.token));
    }
    if let Some(authentication) = config.authentication {
        fields.insert(
            "authentication".to_owned(),
            json::object([
                ("scheme", json::string(authentication.scheme)),
                ("credentials", json::string(authentication.credentials)),
            ]),
        );
    }
    JsonValue::Object(fields)
}

fn a2a_json_struct_from_proto(value: ProtoStruct) -> JsonValue {
    JsonValue::Object(
        value
            .fields
            .into_iter()
            .map(|(key, value)| (key, a2a_json_value_from_proto(value)))
            .collect(),
    )
}

fn a2a_json_value_from_proto(value: ProtoValue) -> JsonValue {
    match value.kind {
        Some(proto_value::Kind::NullValue(_)) | None => JsonValue::Null,
        Some(proto_value::Kind::NumberValue(value)) => json::float(value),
        Some(proto_value::Kind::StringValue(value)) => json::string(value),
        Some(proto_value::Kind::BoolValue(value)) => json::bool_value(value),
        Some(proto_value::Kind::StructValue(value)) => a2a_json_struct_from_proto(value),
        Some(proto_value::Kind::ListValue(value)) => {
            json::array(value.values.into_iter().map(a2a_json_value_from_proto))
        }
    }
}

fn a2a_proto_role_name(value: i32) -> Option<&'static str> {
    a2a_proto::Role::try_from(value)
        .ok()
        .map(|value| value.as_str_name())
}
