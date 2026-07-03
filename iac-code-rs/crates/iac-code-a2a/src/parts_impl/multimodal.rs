use std::collections::BTreeMap;
use std::path::Path;

use base64::engine::general_purpose::STANDARD;
use base64::Engine;
use iac_code_protocol::json::JsonValue;
use ring::digest;

use super::{
    ensure_size, filename, safe_file_url_path, A2APart, A2APartError, MAX_BINARY_FILE_BYTES,
    MAX_BINARY_INLINE_BYTES,
};

pub(super) fn binary_data_part_to_manifest(
    part: &A2APart,
    value: &JsonValue,
    media_type: &str,
) -> Result<String, A2APartError> {
    let JsonValue::Object(object) = value else {
        return Err(A2APartError::new(
            "A2A binary data parts must contain an object.",
        ));
    };
    let encoded = json_string(object, "bytes").or_else(|| json_string(object, "base64"));
    let Some(encoded) = encoded else {
        return Err(A2APartError::new(
            "A2A binary data parts must include base64 bytes.",
        ));
    };
    let content = STANDARD
        .decode(encoded.as_bytes())
        .map_err(|_| A2APartError::new("A2A binary data part bytes must be valid base64."))?;
    ensure_size(
        content.len(),
        MAX_BINARY_INLINE_BYTES,
        "A2A binary data part",
    )?;
    let filename = json_string(object, "filename")
        .or_else(|| filename(part))
        .unwrap_or_else(|| "inline".to_owned());
    Ok(multimodal_manifest(&filename, media_type, &content, "data"))
}

pub(super) fn file_url_part_to_manifest(
    url: &str,
    media_type: &str,
    cwd: &Path,
) -> Result<String, A2APartError> {
    let path = safe_file_url_path(url, cwd)?;
    if path
        .metadata()
        .map_err(|_| A2APartError::new("A2A file URL part must reference an existing file."))?
        .len()
        > MAX_BINARY_FILE_BYTES
    {
        return Err(A2APartError::new(
            "A2A binary file URL part content is too large.",
        ));
    }
    let content = std::fs::read(&path)
        .map_err(|_| A2APartError::new("A2A file URL part must reference an existing file."))?;
    let filename = path
        .file_name()
        .and_then(|value| value.to_str())
        .unwrap_or("attachment");
    Ok(multimodal_manifest(
        filename,
        media_type,
        &content,
        &file_url(&path),
    ))
}

pub(super) fn multimodal_manifest(
    filename: &str,
    media_type: &str,
    content: &[u8],
    source: &str,
) -> String {
    let safe_filename =
        if !filename.is_empty() && !filename.contains('/') && !filename.contains('\\') {
            filename
        } else {
            "attachment"
        };
    [
        "A2A multimodal attachment:".to_owned(),
        format!("- filename={safe_filename}"),
        format!("- mediaType={media_type}"),
        format!("- byteSize={}", content.len()),
        format!("- sha256={}", sha256_hex(content)),
        format!("- source={source}"),
    ]
    .join("\n")
}

fn json_string(object: &BTreeMap<String, JsonValue>, key: &str) -> Option<String> {
    match object.get(key) {
        Some(JsonValue::String(value)) => Some(value.clone()),
        _ => None,
    }
}

fn sha256_hex(content: &[u8]) -> String {
    let digest = digest::digest(&digest::SHA256, content);
    let mut output = String::with_capacity(digest.as_ref().len() * 2);
    for byte in digest.as_ref() {
        use std::fmt::Write;
        write!(output, "{byte:02x}").expect("writing to String should not fail");
    }
    output
}

fn file_url(path: &Path) -> String {
    format!("file://{}", path.to_string_lossy())
}
