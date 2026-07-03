use std::collections::BTreeMap;

use super::A2APartError;

const DEFAULT_TEXT_LIKE_MIME_TYPES: &[&str] = &[
    "text/plain",
    "application/json",
    "text/markdown",
    "text/yaml",
    "application/yaml",
    "application/x-yaml",
];
const DEFAULT_MULTIMODAL_MIME_TYPES: &[&str] = &[
    "image/png",
    "image/jpeg",
    "image/webp",
    "image/gif",
    "audio/mpeg",
    "audio/wav",
    "audio/ogg",
    "application/octet-stream",
];

pub(super) fn supported_input_mime_types() -> Vec<String> {
    let mut values = DEFAULT_TEXT_LIKE_MIME_TYPES
        .iter()
        .chain(DEFAULT_MULTIMODAL_MIME_TYPES)
        .map(|value| (*value).to_owned())
        .collect::<Vec<_>>();
    values.extend(extra_mime_types("IACCODE_A2A_TEXT_MIME_TYPES"));
    values.extend(extra_mime_types("IACCODE_A2A_MULTIMODAL_MIME_TYPES"));
    dedupe_preserving_order(values)
}

pub(super) fn ensure_text_like(media_type: &str) -> Result<(), A2APartError> {
    if text_like_mime_types().contains(&media_type.to_owned()) {
        Ok(())
    } else {
        Err(A2APartError::new("A2A part has unsupported media type."))
    }
}

pub(super) fn is_multimodal(media_type: &str) -> bool {
    multimodal_mime_types().contains(&media_type.to_owned())
}

fn text_like_mime_types() -> Vec<String> {
    DEFAULT_TEXT_LIKE_MIME_TYPES
        .iter()
        .map(|value| (*value).to_owned())
        .chain(extra_mime_types("IACCODE_A2A_TEXT_MIME_TYPES"))
        .collect()
}

fn multimodal_mime_types() -> Vec<String> {
    DEFAULT_MULTIMODAL_MIME_TYPES
        .iter()
        .map(|value| (*value).to_owned())
        .chain(extra_mime_types("IACCODE_A2A_MULTIMODAL_MIME_TYPES"))
        .collect()
}

fn extra_mime_types(env_name: &str) -> Vec<String> {
    let mut values = std::env::var(env_name)
        .unwrap_or_default()
        .replace(';', ",")
        .split(',')
        .filter_map(|item| {
            let item = item.trim().to_ascii_lowercase();
            (!item.is_empty()).then_some(item)
        })
        .collect::<Vec<_>>();
    values.sort();
    values.dedup();
    values
}

fn dedupe_preserving_order(values: Vec<String>) -> Vec<String> {
    let mut seen = BTreeMap::new();
    let mut result = Vec::new();
    for value in values {
        if seen.insert(value.clone(), ()).is_none() {
            result.push(value);
        }
    }
    result
}
