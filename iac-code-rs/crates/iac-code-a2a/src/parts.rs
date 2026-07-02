use std::fmt;
use std::path::{Path, PathBuf};

use iac_code_protocol::json::JsonValue;

#[path = "parts_impl/mime.rs"]
mod mime;
#[path = "parts_impl/multimodal.rs"]
mod multimodal;

use mime::{ensure_text_like, is_multimodal};
use multimodal::{binary_data_part_to_manifest, file_url_part_to_manifest, multimodal_manifest};

pub const MAX_INLINE_BYTES: usize = 1024 * 1024;
pub const MAX_FILE_BYTES: u64 = 1024 * 1024;
pub const MAX_BINARY_INLINE_BYTES: usize = 5 * 1024 * 1024;
pub const MAX_BINARY_FILE_BYTES: u64 = 25 * 1024 * 1024;

#[derive(Clone, Debug, PartialEq, Eq)]
pub struct A2APartError {
    message: String,
}

impl A2APartError {
    fn new(message: impl Into<String>) -> Self {
        Self {
            message: message.into(),
        }
    }
}

impl fmt::Display for A2APartError {
    fn fmt(&self, formatter: &mut fmt::Formatter<'_>) -> fmt::Result {
        formatter.write_str(&self.message)
    }
}

impl std::error::Error for A2APartError {}

#[derive(Clone, Debug, PartialEq, Eq)]
pub struct A2APart {
    content: A2APartContent,
    media_type: String,
    filename: String,
}

#[derive(Clone, Debug, PartialEq, Eq)]
enum A2APartContent {
    Text(String),
    Data(JsonValue),
    Raw(Vec<u8>),
    Url(String),
}

impl A2APart {
    pub fn text(value: impl Into<String>) -> Self {
        Self::text_with_media_type(value, "text/plain")
    }

    pub fn text_with_media_type(value: impl Into<String>, media_type: impl Into<String>) -> Self {
        Self {
            content: A2APartContent::Text(value.into()),
            media_type: media_type.into(),
            filename: String::new(),
        }
    }

    pub fn data(value: JsonValue) -> Self {
        Self::data_with_media_type(value, "application/json")
    }

    pub fn data_with_media_type(value: JsonValue, media_type: impl Into<String>) -> Self {
        Self {
            content: A2APartContent::Data(value),
            media_type: media_type.into(),
            filename: String::new(),
        }
    }

    pub fn raw(value: Vec<u8>, media_type: impl Into<String>) -> Self {
        Self::raw_with_filename(value, media_type, "")
    }

    pub fn raw_with_filename(
        value: Vec<u8>,
        media_type: impl Into<String>,
        filename: impl Into<String>,
    ) -> Self {
        Self {
            content: A2APartContent::Raw(value),
            media_type: media_type.into(),
            filename: filename.into(),
        }
    }

    pub fn url(url: impl Into<String>, media_type: impl Into<String>) -> Self {
        Self {
            content: A2APartContent::Url(url.into()),
            media_type: media_type.into(),
            filename: String::new(),
        }
    }
}

pub fn supported_input_mime_types() -> Vec<String> {
    mime::supported_input_mime_types()
}

pub fn parts_to_prompt(parts: &[A2APart], cwd: &Path) -> Result<String, A2APartError> {
    let mut values = Vec::new();
    for part in parts {
        let value = part_to_prompt(part, cwd)?;
        if !value.is_empty() {
            values.push(value);
        }
    }
    Ok(values.join("\n"))
}

pub fn part_to_prompt(part: &A2APart, cwd: &Path) -> Result<String, A2APartError> {
    let media_type = media_type(part);
    match &part.content {
        A2APartContent::Text(value) => {
            ensure_text_like(&media_type)?;
            Ok(value.clone())
        }
        A2APartContent::Data(value) => {
            if is_multimodal(&media_type) {
                return binary_data_part_to_manifest(part, value, &media_type);
            }
            if media_type != "application/json" {
                return Err(A2APartError::new(
                    "A2A data parts must use application/json media type.",
                ));
            }
            let serialized = value.to_compact_json();
            ensure_size(serialized.len(), MAX_INLINE_BYTES, "A2A data part")?;
            Ok(serialized)
        }
        A2APartContent::Raw(value) => {
            if is_multimodal(&media_type) {
                ensure_size(value.len(), MAX_BINARY_INLINE_BYTES, "A2A binary raw part")?;
                return Ok(multimodal_manifest(
                    filename(part)
                        .unwrap_or_else(|| "inline".to_owned())
                        .as_str(),
                    &media_type,
                    value,
                    "inline",
                ));
            }
            ensure_text_like(&media_type)?;
            ensure_size(value.len(), MAX_INLINE_BYTES, "A2A raw part")?;
            String::from_utf8(value.clone())
                .map_err(|_| A2APartError::new("A2A raw parts must contain valid UTF-8."))
        }
        A2APartContent::Url(url) => {
            if is_multimodal(&media_type) {
                return file_url_part_to_manifest(url, &media_type, cwd);
            }
            ensure_text_like(&media_type)?;
            read_file_url_part(url, cwd)
        }
    }
}

fn read_file_url_part(url: &str, cwd: &Path) -> Result<String, A2APartError> {
    let path = safe_file_url_path(url, cwd)?;
    if path
        .metadata()
        .map_err(|_| A2APartError::new("A2A file URL part must reference an existing file."))?
        .len()
        > MAX_FILE_BYTES
    {
        return Err(A2APartError::new("A2A file URL part content is too large."));
    }
    std::fs::read_to_string(path)
        .map_err(|_| A2APartError::new("A2A file URL parts must contain valid UTF-8."))
}

fn safe_file_url_path(url: &str, cwd: &Path) -> Result<PathBuf, A2APartError> {
    let Some(path) = url.strip_prefix("file://") else {
        return Err(A2APartError::new(
            "A2A file URL parts must use local file:// URLs.",
        ));
    };
    if !path.starts_with('/') {
        return Err(A2APartError::new(
            "A2A file URL parts must use local file:// URLs.",
        ));
    }
    let path = PathBuf::from(percent_decode(path)?);
    let path = path
        .canonicalize()
        .map_err(|_| A2APartError::new("A2A file URL part must reference an existing file."))?;
    let cwd = cwd
        .canonicalize()
        .map_err(|_| A2APartError::new("A2A file URL part is outside the allowed workspace."))?;
    if !path.starts_with(&cwd)
        || !allowed_cwd_roots()
            .iter()
            .any(|root| path.starts_with(root))
    {
        return Err(A2APartError::new(
            "A2A file URL part is outside the allowed workspace.",
        ));
    }
    if !path.is_file() {
        return Err(A2APartError::new(
            "A2A file URL part must reference an existing file.",
        ));
    }
    Ok(path)
}

fn allowed_cwd_roots() -> Vec<PathBuf> {
    let roots = if let Some(raw) = std::env::var_os("IACCODE_A2A_ALLOWED_CWDS") {
        std::env::split_paths(&raw).collect::<Vec<_>>()
    } else {
        let mut values = Vec::new();
        if let Ok(cwd) = std::env::current_dir() {
            values.push(cwd);
        }
        values.push(std::env::temp_dir());
        values
    };
    roots
        .into_iter()
        .filter(|path| path.exists() && path.is_dir())
        .filter_map(|path| path.canonicalize().ok())
        .collect()
}

fn media_type(part: &A2APart) -> String {
    let media_type = part.media_type.trim();
    if media_type.is_empty() {
        "text/plain".to_owned()
    } else {
        media_type.to_ascii_lowercase()
    }
}

fn ensure_size(size: usize, limit: usize, label: &str) -> Result<(), A2APartError> {
    if size > limit {
        Err(A2APartError::new(format!("{label} content is too large.")))
    } else {
        Ok(())
    }
}

fn filename(part: &A2APart) -> Option<String> {
    let filename = part.filename.trim();
    (!filename.is_empty()).then(|| {
        Path::new(filename)
            .file_name()
            .and_then(|value| value.to_str())
            .unwrap_or(filename)
            .to_owned()
    })
}

fn percent_decode(value: &str) -> Result<String, A2APartError> {
    let mut output = Vec::with_capacity(value.len());
    let bytes = value.as_bytes();
    let mut index = 0;
    while index < bytes.len() {
        if bytes[index] == b'%' {
            if index + 2 >= bytes.len() {
                return Err(A2APartError::new(
                    "A2A file URL parts must use local file:// URLs.",
                ));
            }
            let high = decode_hex(bytes[index + 1])?;
            let low = decode_hex(bytes[index + 2])?;
            output.push((high << 4) | low);
            index += 3;
        } else {
            output.push(bytes[index]);
            index += 1;
        }
    }
    String::from_utf8(output)
        .map_err(|_| A2APartError::new("A2A file URL parts must use local file:// URLs."))
}

fn decode_hex(value: u8) -> Result<u8, A2APartError> {
    match value {
        b'0'..=b'9' => Ok(value - b'0'),
        b'a'..=b'f' => Ok(value - b'a' + 10),
        b'A'..=b'F' => Ok(value - b'A' + 10),
        _ => Err(A2APartError::new(
            "A2A file URL parts must use local file:// URLs.",
        )),
    }
}
