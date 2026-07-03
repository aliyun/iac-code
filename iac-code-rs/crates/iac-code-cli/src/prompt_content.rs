use base64::engine::general_purpose::STANDARD;
use base64::Engine;
use std::fs;
use std::path::PathBuf;
use std::time::Duration;

use iac_code_config::paths::ConfigPaths;
use iac_code_protocol::message::{AgentContentBlock, AgentMessageContent, ImageBlock};
use iac_code_providers::{is_model_multimodal_with_probe, MultimodalProbeOptions};

const API_IMAGE_MAX_BASE64_SIZE: usize = 5 * 1024 * 1024;
pub(super) const IMAGE_TARGET_RAW_SIZE: usize = (API_IMAGE_MAX_BASE64_SIZE * 3) / 4;

pub(super) fn local_image_path_prompt_content(
    prompt: &str,
) -> Result<Option<AgentMessageContent>, String> {
    let Some(path) = local_image_path_from_prompt(prompt) else {
        return Ok(None);
    };
    let data = fs::read(&path)
        .map_err(|error| format!("Failed to read image '{}': {error}", path.display()))?;
    if data.is_empty() {
        return Err(format!("Image file is empty: {}", path.display()));
    }
    if data.len() > IMAGE_TARGET_RAW_SIZE {
        return Err(format!(
            "Image '{}' is too large for Rust local image input without resizing ({} bytes > {} bytes).",
            path.display(),
            data.len(),
            IMAGE_TARGET_RAW_SIZE
        ));
    }
    Ok(Some(AgentMessageContent::Blocks(vec![
        AgentContentBlock::Image(ImageBlock {
            media_type: detect_image_media_type(&data).to_owned(),
            data: STANDARD.encode(data),
        }),
    ])))
}

pub(super) fn ensure_prompt_content_supported(
    content: &AgentMessageContent,
    paths: &ConfigPaths,
    provider_config: &iac_code_providers::ProviderConfig,
    model: &str,
) -> Result<(), String> {
    if !agent_message_content_has_image(content) {
        return Ok(());
    }
    let cache_path = paths.config_dir.join(".multimodal-cache.yml");
    let supports_images = is_model_multimodal_with_probe(
        model,
        MultimodalProbeOptions {
            settings_path: Some(&paths.settings_path),
            cache_path: Some(&cache_path),
            provider_key: Some(&provider_config.provider_key),
            base_url: provider_config.base_url.as_deref(),
            api_key: provider_config.api_key.as_deref(),
            timeout: Duration::from_secs(5),
        },
    );
    if supports_images {
        return Ok(());
    }
    Err(format!(
        "Current model {model} does not support image input. Use /model to switch to a vision-capable model."
    ))
}

fn agent_message_content_has_image(content: &AgentMessageContent) -> bool {
    match content {
        AgentMessageContent::Text(_) => false,
        AgentMessageContent::Blocks(blocks) => blocks
            .iter()
            .any(|block| matches!(block, AgentContentBlock::Image(_))),
    }
}

fn local_image_path_from_prompt(prompt: &str) -> Option<PathBuf> {
    let raw = prompt.trim();
    if raw.is_empty() || raw.contains('\n') {
        return None;
    }
    let unquoted = strip_matching_quotes(raw);
    let path_text = unquoted
        .strip_prefix("file://")
        .map(percent_decode)
        .unwrap_or_else(|| unquoted.to_owned());
    if !has_image_extension(&path_text) {
        return None;
    }
    let path = PathBuf::from(path_text);
    path.is_file().then_some(path)
}

fn strip_matching_quotes(value: &str) -> &str {
    let bytes = value.as_bytes();
    if bytes.len() >= 2
        && ((bytes[0] == b'\'' && bytes[bytes.len() - 1] == b'\'')
            || (bytes[0] == b'"' && bytes[bytes.len() - 1] == b'"'))
    {
        return &value[1..value.len() - 1];
    }
    value
}

fn has_image_extension(path: &str) -> bool {
    let lower = path.to_ascii_lowercase();
    [".png", ".jpg", ".jpeg", ".gif", ".webp"]
        .iter()
        .any(|suffix| lower.ends_with(suffix))
}

pub(super) fn detect_image_media_type(data: &[u8]) -> &'static str {
    if data.len() >= 8 && &data[..8] == b"\x89PNG\r\n\x1a\n" {
        return "image/png";
    }
    if data.len() >= 3 && &data[..3] == b"\xff\xd8\xff" {
        return "image/jpeg";
    }
    if data.len() >= 3 && &data[..3] == b"GIF" {
        return "image/gif";
    }
    if data.len() >= 12 && &data[..4] == b"RIFF" && &data[8..12] == b"WEBP" {
        return "image/webp";
    }
    "image/png"
}

fn percent_decode(value: &str) -> String {
    let bytes = value.as_bytes();
    let mut out = Vec::with_capacity(bytes.len());
    let mut index = 0;
    while index < bytes.len() {
        if bytes[index] == b'%' && index + 2 < bytes.len() {
            if let Some(decoded) = hex_pair(bytes[index + 1], bytes[index + 2]) {
                out.push(decoded);
                index += 3;
                continue;
            }
        }
        out.push(bytes[index]);
        index += 1;
    }
    String::from_utf8_lossy(&out).into_owned()
}

fn hex_pair(high: u8, low: u8) -> Option<u8> {
    Some(hex_digit(high)? * 16 + hex_digit(low)?)
}

fn hex_digit(value: u8) -> Option<u8> {
    match value {
        b'0'..=b'9' => Some(value - b'0'),
        b'a'..=b'f' => Some(value - b'a' + 10),
        b'A'..=b'F' => Some(value - b'A' + 10),
        _ => None,
    }
}
