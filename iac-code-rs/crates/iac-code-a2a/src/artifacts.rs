use std::path::{Path, PathBuf};
use std::time::{SystemTime, UNIX_EPOCH};

use ring::digest;
use ring::rand;
use ring::rand::SecureRandom;

#[derive(Clone, Debug, PartialEq, Eq)]
pub struct UnsafeArtifactNameError;

#[derive(Clone, Debug, PartialEq, Eq)]
pub struct A2AArtifactMetadata {
    pub artifact_id: String,
    pub filename: String,
    pub media_type: String,
    pub byte_size: usize,
    pub sha256: String,
    pub uri: String,
}

#[derive(Clone, Debug, PartialEq, Eq)]
pub struct A2AArtifactStore {
    root: PathBuf,
}

impl A2AArtifactStore {
    pub fn new(root: impl AsRef<Path>) -> Self {
        Self {
            root: root.as_ref().to_path_buf(),
        }
    }

    pub fn root(&self) -> &Path {
        &self.root
    }

    pub fn save_text(
        &self,
        filename: &str,
        content: &str,
        media_type: &str,
    ) -> Result<A2AArtifactMetadata, UnsafeArtifactNameError> {
        self.save_bytes(filename, content.as_bytes(), media_type)
    }

    pub fn save_base64(
        &self,
        filename: &str,
        content: &str,
        media_type: &str,
    ) -> Result<A2AArtifactMetadata, UnsafeArtifactNameError> {
        let decoded = decode_base64(content).ok_or(UnsafeArtifactNameError)?;
        self.save_bytes(filename, &decoded, media_type)
    }

    pub fn save_bytes(
        &self,
        filename: &str,
        content: &[u8],
        media_type: &str,
    ) -> Result<A2AArtifactMetadata, UnsafeArtifactNameError> {
        let safe_name = safe_filename(filename)?;
        let artifact_id = next_artifact_id();
        let artifact_dir = self.root.join(&artifact_id);
        std::fs::create_dir_all(&artifact_dir).map_err(|_| UnsafeArtifactNameError)?;
        let path = artifact_dir.join(&safe_name);
        std::fs::write(&path, content).map_err(|_| UnsafeArtifactNameError)?;
        let resolved = path.canonicalize().map_err(|_| UnsafeArtifactNameError)?;

        Ok(A2AArtifactMetadata {
            artifact_id,
            filename: safe_name,
            media_type: media_type.to_owned(),
            byte_size: content.len(),
            sha256: sha256_hex(content),
            uri: file_uri(&resolved),
        })
    }

    pub fn path_for(&self, artifact_id: &str) -> std::io::Result<PathBuf> {
        let artifact_dir = self.root.join(artifact_id);
        let mut entries = std::fs::read_dir(artifact_dir)?;
        entries
            .next()
            .transpose()?
            .map(|entry| entry.path())
            .ok_or_else(|| std::io::Error::new(std::io::ErrorKind::NotFound, artifact_id))
    }
}

fn safe_filename(filename: &str) -> Result<String, UnsafeArtifactNameError> {
    if filename.is_empty() || filename == "." || filename == ".." || has_path_separator(filename) {
        return Err(UnsafeArtifactNameError);
    }
    Ok(filename.to_owned())
}

#[cfg(windows)]
fn has_path_separator(filename: &str) -> bool {
    filename.contains('/') || filename.contains('\\')
}

#[cfg(not(windows))]
fn has_path_separator(filename: &str) -> bool {
    filename.contains('/')
}

fn next_artifact_id() -> String {
    new_uuid_v4()
}

fn new_uuid_v4() -> String {
    let mut bytes = [0_u8; 16];
    if rand::SystemRandom::new().fill(&mut bytes).is_err() {
        let nanos = SystemTime::now()
            .duration_since(UNIX_EPOCH)
            .unwrap_or_default()
            .as_nanos();
        let pid = std::process::id() as u128;
        bytes.copy_from_slice(&(nanos ^ (pid << 64)).to_le_bytes());
    }
    bytes[6] = (bytes[6] & 0x0f) | 0x40;
    bytes[8] = (bytes[8] & 0x3f) | 0x80;
    format!(
        "{:02x}{:02x}{:02x}{:02x}-{:02x}{:02x}-{:02x}{:02x}-{:02x}{:02x}-{:02x}{:02x}{:02x}{:02x}{:02x}{:02x}",
        bytes[0],
        bytes[1],
        bytes[2],
        bytes[3],
        bytes[4],
        bytes[5],
        bytes[6],
        bytes[7],
        bytes[8],
        bytes[9],
        bytes[10],
        bytes[11],
        bytes[12],
        bytes[13],
        bytes[14],
        bytes[15]
    )
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

fn file_uri(path: &Path) -> String {
    format!("file://{}", percent_encode_path(&path.to_string_lossy()))
}

fn percent_encode_path(path: &str) -> String {
    let mut output = String::new();
    for byte in path.as_bytes() {
        match *byte {
            b'A'..=b'Z' | b'a'..=b'z' | b'0'..=b'9' | b'-' | b'.' | b'_' | b'~' | b'/' => {
                output.push(*byte as char)
            }
            other => {
                use std::fmt::Write;
                write!(output, "%{other:02X}").expect("writing to String should not fail");
            }
        }
    }
    output
}

fn decode_base64(input: &str) -> Option<Vec<u8>> {
    if !input.len().is_multiple_of(4) {
        return None;
    }
    let mut output = Vec::new();
    for chunk in input.as_bytes().chunks(4) {
        let mut values = [0u8; 4];
        let mut padding = 0;
        for (index, byte) in chunk.iter().enumerate() {
            if *byte == b'=' {
                values[index] = 0;
                padding += 1;
            } else {
                values[index] = decode_base64_byte(*byte)?;
            }
        }
        let combined = ((values[0] as u32) << 18)
            | ((values[1] as u32) << 12)
            | ((values[2] as u32) << 6)
            | values[3] as u32;
        output.push(((combined >> 16) & 0xff) as u8);
        if padding < 2 {
            output.push(((combined >> 8) & 0xff) as u8);
        }
        if padding < 1 {
            output.push((combined & 0xff) as u8);
        }
    }
    Some(output)
}

fn decode_base64_byte(byte: u8) -> Option<u8> {
    match byte {
        b'A'..=b'Z' => Some(byte - b'A'),
        b'a'..=b'z' => Some(byte - b'a' + 26),
        b'0'..=b'9' => Some(byte - b'0' + 52),
        b'+' => Some(62),
        b'/' => Some(63),
        _ => None,
    }
}
