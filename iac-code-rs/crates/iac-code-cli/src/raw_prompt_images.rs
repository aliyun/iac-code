use base64::engine::general_purpose::STANDARD;
use base64::Engine;
use std::fs;
use std::io;
use std::path::PathBuf;
#[cfg(all(unix, target_os = "macos"))]
use std::process;
#[cfg(all(unix, target_os = "macos"))]
use std::time::{SystemTime, UNIX_EPOCH};

use iac_code_protocol::message::{AgentContentBlock, AgentMessageContent, ImageBlock, TextBlock};

use crate::debug_logging::{ensure_private_dir, ensure_private_file};
use crate::permission_settings::validate_session_id_for_trusted_read_directories;
use crate::prompt_content::{detect_image_media_type, IMAGE_TARGET_RAW_SIZE};
use crate::raw_prompt_context::RawPromptActionContext;

#[cfg(unix)]
#[derive(Clone, Debug, PartialEq)]
pub(super) struct RawPromptPastedImage {
    pub(super) media_type: String,
    pub(super) data: String,
    pub(super) source_path: Option<PathBuf>,
}

#[cfg(unix)]
pub(super) trait RawPromptImageSource {
    fn has_image(&mut self) -> io::Result<bool>;
    fn read_image(&mut self) -> io::Result<Option<RawPromptPastedImage>>;
}

#[cfg(unix)]
pub(super) struct SystemRawPromptImageSource;

#[cfg(unix)]
impl RawPromptImageSource for SystemRawPromptImageSource {
    fn has_image(&mut self) -> io::Result<bool> {
        system_clipboard_has_image()
    }

    fn read_image(&mut self) -> io::Result<Option<RawPromptPastedImage>> {
        read_system_clipboard_image()
    }
}

#[cfg(all(unix, target_os = "macos"))]
fn system_clipboard_has_image() -> io::Result<bool> {
    read_macos_clipboard_has_image()
}

#[cfg(all(unix, not(target_os = "macos")))]
fn system_clipboard_has_image() -> io::Result<bool> {
    Ok(false)
}

#[cfg(all(unix, target_os = "macos"))]
fn read_system_clipboard_image() -> io::Result<Option<RawPromptPastedImage>> {
    read_macos_clipboard_image()
}

#[cfg(all(unix, not(target_os = "macos")))]
fn read_system_clipboard_image() -> io::Result<Option<RawPromptPastedImage>> {
    Ok(None)
}

#[cfg(all(unix, target_os = "macos"))]
fn read_macos_clipboard_has_image() -> io::Result<bool> {
    let script = r#"
try
    set img to (the clipboard as «class PNGf»)
    return "1"
on error
end try
try
    set img to (the clipboard as «class JPEG»)
    return "1"
on error
end try
return "0"
"#;
    let output = match process::Command::new("osascript")
        .arg("-e")
        .arg(script)
        .output()
    {
        Ok(output) => output,
        Err(error) if error.kind() == io::ErrorKind::NotFound => return Ok(false),
        Err(error) => return Err(error),
    };
    if !output.status.success() {
        return Ok(false);
    }
    Ok(String::from_utf8_lossy(&output.stdout).trim() == "1")
}

#[cfg(all(unix, target_os = "macos"))]
fn read_macos_clipboard_image() -> io::Result<Option<RawPromptPastedImage>> {
    let path = std::env::temp_dir().join(format!(
        "iac-code-rs-clipboard-{}-{}.img",
        process::id(),
        SystemTime::now()
            .duration_since(UNIX_EPOCH)
            .unwrap_or_default()
            .as_nanos()
    ));
    let path_text = path.to_string_lossy();
    let script = format!(
        r#"
set outPath to POSIX file "{}"
set mediaType to ""
try
    set img to (the clipboard as «class PNGf»)
    set fileRef to open for access outPath with write permission
    set eof fileRef to 0
    write img to fileRef
    close access fileRef
    return "image/png"
on error
    try
        close access fileRef
    end try
end try
try
    set img to (the clipboard as «class JPEG»)
    set fileRef to open for access outPath with write permission
    set eof fileRef to 0
    write img to fileRef
    close access fileRef
    return "image/jpeg"
on error
    try
        close access fileRef
    end try
end try
return ""
"#,
        apple_script_string(&path_text)
    );
    let output = match process::Command::new("osascript")
        .arg("-e")
        .arg(script)
        .output()
    {
        Ok(output) => output,
        Err(error) if error.kind() == io::ErrorKind::NotFound => return Ok(None),
        Err(error) => return Err(error),
    };
    if !output.status.success() {
        let _ = fs::remove_file(&path);
        return Ok(None);
    }
    let media_type = String::from_utf8_lossy(&output.stdout).trim().to_owned();
    if media_type.is_empty() {
        let _ = fs::remove_file(&path);
        return Ok(None);
    }
    let data = fs::read(&path)?;
    let _ = fs::remove_file(&path);
    raw_prompt_image_from_bytes(&data, &media_type)
}

#[cfg(all(unix, target_os = "macos"))]
fn apple_script_string(value: &str) -> String {
    value.replace('\\', "\\\\").replace('"', "\\\"")
}

#[cfg(unix)]
fn raw_prompt_image_from_bytes(
    data: &[u8],
    media_type: &str,
) -> io::Result<Option<RawPromptPastedImage>> {
    if data.is_empty() {
        return Ok(None);
    }
    if data.len() > IMAGE_TARGET_RAW_SIZE {
        return Err(io::Error::new(
            io::ErrorKind::InvalidData,
            format!(
                "Image is too large for Rust interactive image input ({} bytes > {} bytes).",
                data.len(),
                IMAGE_TARGET_RAW_SIZE
            ),
        ));
    }
    let detected_media_type = detect_image_media_type(data);
    let media_type = if media_type.trim().is_empty() {
        detected_media_type
    } else {
        media_type.trim()
    };
    Ok(Some(RawPromptPastedImage {
        media_type: media_type.to_owned(),
        data: STANDARD.encode(data),
        source_path: None,
    }))
}

#[cfg(unix)]
pub(super) fn raw_prompt_persist_pasted_image(
    image: &mut RawPromptPastedImage,
    image_id: usize,
    context: &RawPromptActionContext,
) {
    let Some(paths) = &context.config_paths else {
        return;
    };
    let Some(session_id) = context.current_session_id.as_deref() else {
        return;
    };
    if session_id.is_empty()
        || validate_session_id_for_trusted_read_directories(session_id).is_err()
    {
        return;
    }
    let Ok(bytes) = STANDARD.decode(&image.data) else {
        return;
    };
    let extension = raw_prompt_image_extension(&image.media_type);
    let image_dir = paths.subdirs().image_cache.join(session_id);
    if ensure_private_dir(&image_dir).is_err() {
        return;
    }
    let image_path = image_dir.join(format!("{image_id}.{extension}"));
    if fs::write(&image_path, bytes).is_err() {
        return;
    }
    if ensure_private_file(&image_path).is_err() {
        let _ = fs::remove_file(&image_path);
        return;
    }
    image.source_path = Some(image_path);
}

#[cfg(unix)]
fn raw_prompt_image_extension(media_type: &str) -> &'static str {
    match media_type.trim().to_ascii_lowercase().as_str() {
        "image/jpeg" | "image/jpg" => "jpg",
        "image/gif" => "gif",
        "image/webp" => "webp",
        "image/png" => "png",
        _ => "img",
    }
}

#[cfg(unix)]
#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub(super) struct RawPromptImageRef {
    pub(super) id: usize,
    pub(super) start: usize,
    pub(super) end: usize,
}

#[cfg(unix)]
pub(super) fn raw_prompt_content_from_pasted_images(
    text: &str,
    images: &[RawPromptPastedImage],
) -> Option<AgentMessageContent> {
    let refs = raw_prompt_image_refs(text);
    let refs: Vec<_> = refs
        .into_iter()
        .filter(|image_ref| image_ref.id > 0 && image_ref.id <= images.len())
        .collect();
    if refs.is_empty() {
        return None;
    }

    let mut blocks = Vec::new();
    let mut cursor = 0usize;
    for image_ref in refs {
        if image_ref.start > cursor {
            blocks.push(AgentContentBlock::Text(TextBlock {
                text: text[cursor..image_ref.start].to_owned(),
            }));
        }
        let image = &images[image_ref.id - 1];
        blocks.push(AgentContentBlock::Image(ImageBlock {
            media_type: image.media_type.clone(),
            data: image.data.clone(),
        }));
        cursor = image_ref.end;
    }
    if cursor < text.len() {
        blocks.push(AgentContentBlock::Text(TextBlock {
            text: text[cursor..].to_owned(),
        }));
    }
    Some(AgentMessageContent::Blocks(blocks))
}

#[cfg(unix)]
pub(super) fn raw_prompt_image_refs(text: &str) -> Vec<RawPromptImageRef> {
    const PREFIX: &str = "[Image #";
    let mut refs = Vec::new();
    let mut search_start = 0usize;
    while let Some(relative_start) = text[search_start..].find(PREFIX) {
        let start = search_start + relative_start;
        let digits_start = start + PREFIX.len();
        let bytes = text.as_bytes();
        let mut digits_end = digits_start;
        while digits_end < bytes.len() && bytes[digits_end].is_ascii_digit() {
            digits_end += 1;
        }
        if digits_end == digits_start || digits_end >= bytes.len() || bytes[digits_end] != b']' {
            search_start = digits_start;
            continue;
        }
        let id = text[digits_start..digits_end].parse::<usize>().unwrap_or(0);
        refs.push(RawPromptImageRef {
            id,
            start,
            end: digits_end + 1,
        });
        search_start = digits_end + 1;
    }
    refs
}

#[cfg(all(unix, test))]
pub(super) struct EmptyRawPromptImageSource;

#[cfg(all(unix, test))]
impl RawPromptImageSource for EmptyRawPromptImageSource {
    fn has_image(&mut self) -> io::Result<bool> {
        Ok(false)
    }

    fn read_image(&mut self) -> io::Result<Option<RawPromptPastedImage>> {
        Ok(None)
    }
}
