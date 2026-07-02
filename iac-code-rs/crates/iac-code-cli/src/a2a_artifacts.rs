use std::fs;

use iac_code_a2a::artifacts::{A2AArtifactMetadata, A2AArtifactStore};
use iac_code_a2a::task_store::Artifact;
use iac_code_protocol::{json, json::JsonValue, StreamEvent};

use crate::json_utils::{json_object_field_map, json_string_field_map};

pub(super) fn a2a_artifacts_from_events(
    events: &[StreamEvent],
    artifact_store: &A2AArtifactStore,
) -> Vec<Artifact> {
    events
        .iter()
        .filter_map(|event| match event {
            StreamEvent::ToolResult(tool_result) => {
                a2a_artifact_from_tool_result(&tool_result.result, artifact_store)
            }
            _ => None,
        })
        .collect()
}

fn a2a_artifact_from_tool_result(
    result: &str,
    artifact_store: &A2AArtifactStore,
) -> Option<Artifact> {
    let JsonValue::Object(result) = json::parse(result).ok()? else {
        return None;
    };
    let artifact = json_object_field_map(&result, "artifact")?;
    let filename = json_string_field_map(artifact, "filename")?;
    let media_type = json_string_field_map(artifact, "mediaType")
        .or_else(|| json_string_field_map(artifact, "media_type"))
        .unwrap_or("application/octet-stream");
    let metadata = if let Some(content) = json_string_field_map(artifact, "content") {
        artifact_store
            .save_text(filename, content, media_type)
            .ok()?
    } else if let Some(content) = json_string_field_map(artifact, "bytes")
        .or_else(|| json_string_field_map(artifact, "base64"))
    {
        artifact_store
            .save_base64(filename, content, media_type)
            .ok()?
    } else if let Some(source_path) = json_string_field_map(artifact, "path") {
        let content = fs::read(source_path).ok()?;
        artifact_store
            .save_bytes(filename, &content, media_type)
            .ok()?
    } else {
        return None;
    };
    Some(artifact_from_metadata(metadata))
}

fn artifact_from_metadata(metadata: A2AArtifactMetadata) -> Artifact {
    Artifact {
        artifact_id: metadata.artifact_id,
        filename: metadata.filename,
        media_type: metadata.media_type,
        byte_size: metadata.byte_size,
        sha256: metadata.sha256,
        uri: metadata.uri,
    }
}
