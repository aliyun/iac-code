use std::collections::BTreeMap;
use std::time::{SystemTime, UNIX_EPOCH};

use iac_code_a2a::push::TaskPushNotificationConfig;
use iac_code_a2a::task_store::{Artifact, SdkTask};
use iac_code_protocol::{json, json::JsonValue};

pub(super) fn a2a_sdk_task_json(task: &SdkTask, include_artifacts: bool) -> JsonValue {
    let mut status = BTreeMap::from([
        ("state".to_owned(), json::string(&task.status)),
        (
            "timestamp".to_owned(),
            json::string(
                task.status_timestamp
                    .map(|timestamp| timestamp.to_string())
                    .unwrap_or_default(),
            ),
        ),
    ]);
    if !task.status_message.is_empty() {
        status.insert(
            "message".to_owned(),
            a2a_text_message_json(
                &format!("{}-{}", task.id, task.status),
                &task.id,
                &task.context_id,
                &task.status_message,
            ),
        );
    }
    let mut fields = BTreeMap::from([
        ("id".to_owned(), json::string(&task.id)),
        ("contextId".to_owned(), json::string(&task.context_id)),
        ("status".to_owned(), JsonValue::Object(status)),
    ]);
    if include_artifacts && !task.artifacts.is_empty() {
        fields.insert(
            "artifacts".to_owned(),
            json::array(task.artifacts.iter().map(a2a_artifact_json)),
        );
    }
    JsonValue::Object(fields)
}

fn a2a_artifact_json(artifact: &Artifact) -> JsonValue {
    let metadata = a2a_artifact_metadata_json(artifact);
    json::object([
        ("artifactId", json::string(&artifact.artifact_id)),
        ("name", json::string(&artifact.filename)),
        ("metadata", metadata.clone()),
        (
            "parts",
            json::array([json::object([
                ("url", json::string(&artifact.uri)),
                ("filename", json::string(&artifact.filename)),
                ("mediaType", json::string(&artifact.media_type)),
                ("metadata", metadata),
            ])]),
        ),
    ])
}

fn a2a_artifact_metadata_json(artifact: &Artifact) -> JsonValue {
    json::object([
        ("uri", json::string(&artifact.uri)),
        ("mediaType", json::string(&artifact.media_type)),
        ("byteSize", json::number(artifact.byte_size)),
        ("sha256", json::string(&artifact.sha256)),
    ])
}

pub(super) fn a2a_text_message_json(
    message_id: &str,
    task_id: &str,
    context_id: &str,
    text: &str,
) -> JsonValue {
    json::object([
        ("messageId", json::string(message_id)),
        ("taskId", json::string(task_id)),
        ("contextId", json::string(context_id)),
        ("role", json::string("ROLE_AGENT")),
        (
            "parts",
            json::array([json::object([("text", json::string(text))])]),
        ),
    ])
}

pub(super) fn a2a_push_config_json(config: &TaskPushNotificationConfig) -> JsonValue {
    let mut fields = BTreeMap::from([
        ("id".to_owned(), json::string(&config.id)),
        ("taskId".to_owned(), json::string(&config.task_id)),
        ("url".to_owned(), json::string(&config.url)),
    ]);
    if let Some(authentication) = &config.authentication {
        fields.insert(
            "authentication".to_owned(),
            json::object([("scheme", json::string(&authentication.scheme))]),
        );
    }
    JsonValue::Object(fields)
}

pub(super) fn current_unix_seconds() -> i64 {
    SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .unwrap_or_default()
        .as_secs() as i64
}

pub(super) fn new_a2a_server_id(prefix: &str) -> String {
    let nanos = SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .unwrap_or_default()
        .as_nanos();
    format!("{prefix}-{nanos:x}")
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn a2a_task_json_includes_artifact_metadata_when_requested() {
        let mut task = SdkTask::new("task-artifact", "ctx-artifact", "TASK_STATE_WORKING", 123);
        task.artifacts.push(Artifact {
            artifact_id: "artifact-compat".into(),
            filename: "template.yaml".into(),
            media_type: "application/yaml".into(),
            byte_size: 42,
            sha256: "abc123".into(),
            uri: "file:///tmp/template.yaml".into(),
        });

        assert_eq!(
            a2a_sdk_task_json(&task, true),
            json::object([
                ("id", json::string("task-artifact")),
                ("contextId", json::string("ctx-artifact")),
                (
                    "status",
                    json::object([
                        ("state", json::string("TASK_STATE_WORKING")),
                        ("timestamp", json::string("123")),
                    ]),
                ),
                (
                    "artifacts",
                    json::array([json::object([
                        ("artifactId", json::string("artifact-compat")),
                        ("name", json::string("template.yaml")),
                        (
                            "metadata",
                            json::object([
                                ("uri", json::string("file:///tmp/template.yaml")),
                                ("mediaType", json::string("application/yaml")),
                                ("byteSize", json::number(42)),
                                ("sha256", json::string("abc123")),
                            ]),
                        ),
                        (
                            "parts",
                            json::array([json::object([
                                ("url", json::string("file:///tmp/template.yaml")),
                                ("filename", json::string("template.yaml")),
                                ("mediaType", json::string("application/yaml")),
                                (
                                    "metadata",
                                    json::object([
                                        ("uri", json::string("file:///tmp/template.yaml")),
                                        ("mediaType", json::string("application/yaml")),
                                        ("byteSize", json::number(42)),
                                        ("sha256", json::string("abc123")),
                                    ]),
                                ),
                            ])]),
                        ),
                    ])]),
                ),
            ])
        );
        assert_eq!(
            a2a_sdk_task_json(&task, false),
            json::object([
                ("id", json::string("task-artifact")),
                ("contextId", json::string("ctx-artifact")),
                (
                    "status",
                    json::object([
                        ("state", json::string("TASK_STATE_WORKING")),
                        ("timestamp", json::string("123")),
                    ]),
                ),
            ])
        );
    }
}
