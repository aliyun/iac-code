use std::collections::BTreeMap;

use iac_code_protocol::json::{self, JsonValue};

use crate::artifacts::{A2AArtifactMetadata, A2AArtifactStore};

use super::{A2AArtifact, A2AEvent, A2APart, TaskArtifactUpdate};

pub(super) fn artifact_update_event(
    task_id: &str,
    context_id: &str,
    metadata: &A2AArtifactMetadata,
) -> A2AEvent {
    let artifact_metadata = artifact_update_metadata_json(metadata);
    A2AEvent::TaskArtifactUpdate(TaskArtifactUpdate {
        task_id: task_id.to_owned(),
        context_id: context_id.to_owned(),
        artifact: A2AArtifact {
            artifact_id: metadata.artifact_id.clone(),
            name: metadata.filename.clone(),
            parts: vec![A2APart {
                url: metadata.uri.clone(),
                filename: metadata.filename.clone(),
                media_type: metadata.media_type.clone(),
                metadata: artifact_metadata.clone(),
            }],
            metadata: artifact_metadata,
        },
        append: false,
        last_chunk: true,
    })
}

pub(super) fn extract_artifact_metadata(
    result: &str,
    artifact_store: Option<&A2AArtifactStore>,
) -> Option<A2AArtifactMetadata> {
    let artifact_store = artifact_store?;
    let JsonValue::Object(result) = json::parse(result).ok()? else {
        return None;
    };
    let artifact = json_object_field_map(&result, "artifact")?;
    let filename = json_string_field_map(artifact, "filename")?;
    let media_type = json_string_field_map(artifact, "mediaType")
        .or_else(|| json_string_field_map(artifact, "media_type"))
        .unwrap_or("application/octet-stream");

    if let Some(content) = json_string_field_map(artifact, "content") {
        return artifact_store.save_text(filename, content, media_type).ok();
    }
    if let Some(content) = json_string_field_map(artifact, "bytes")
        .or_else(|| json_string_field_map(artifact, "base64"))
    {
        return artifact_store
            .save_base64(filename, content, media_type)
            .ok();
    }
    if let Some(source_path) = json_string_field_map(artifact, "path") {
        let content = std::fs::read(source_path).ok()?;
        return artifact_store
            .save_bytes(filename, &content, media_type)
            .ok();
    }
    None
}

pub(super) fn artifact_metadata_json(metadata: &A2AArtifactMetadata) -> JsonValue {
    json::object([
        ("artifactId", json::string(&metadata.artifact_id)),
        ("filename", json::string(&metadata.filename)),
        ("mediaType", json::string(&metadata.media_type)),
        ("byteSize", json::number(metadata.byte_size)),
        ("sha256", json::string(&metadata.sha256)),
        ("uri", json::string(&metadata.uri)),
    ])
}

fn artifact_update_metadata_json(metadata: &A2AArtifactMetadata) -> JsonValue {
    json::object([
        ("uri", json::string(&metadata.uri)),
        ("mediaType", json::string(&metadata.media_type)),
        ("byteSize", json::number(metadata.byte_size)),
        ("sha256", json::string(&metadata.sha256)),
    ])
}

fn json_object_field_map<'a>(
    value: &'a BTreeMap<String, JsonValue>,
    key: &str,
) -> Option<&'a BTreeMap<String, JsonValue>> {
    match value.get(key) {
        Some(JsonValue::Object(value)) => Some(value),
        _ => None,
    }
}

fn json_string_field_map<'a>(value: &'a BTreeMap<String, JsonValue>, key: &str) -> Option<&'a str> {
    match value.get(key) {
        Some(JsonValue::String(value)) => Some(value),
        _ => None,
    }
}
