use std::fs::{self, File};
use std::io::{self, Read, Write};
use std::path::{Path, PathBuf};
use std::time::{SystemTime, UNIX_EPOCH};

use iac_code_protocol::json::{self, JsonValue};

const FAILED_PREFIX: &str = "failed_events.";
const FAILED_SUFFIX: &str = ".jsonl";

#[derive(Clone, Debug)]
pub struct FallbackStore {
    base_dir: PathBuf,
}

impl FallbackStore {
    pub fn new(base_dir: impl AsRef<Path>) -> Self {
        Self {
            base_dir: base_dir.as_ref().to_path_buf(),
        }
    }

    pub fn write(&self, session_id: &str, events: &[JsonValue]) -> io::Result<PathBuf> {
        ensure_private_dir(&self.base_dir)?;
        let path = self.base_dir.join(format!(
            "{FAILED_PREFIX}{session_id}.{}{FAILED_SUFFIX}",
            short_batch_id()
        ));
        let mut file = File::create(&path)?;
        for event in events {
            writeln!(file, "{}", event.to_compact_json())?;
        }
        ensure_private_file(&path)?;
        Ok(path)
    }

    pub fn list_pending(&self) -> io::Result<Vec<PathBuf>> {
        if !self.base_dir.exists() {
            return Ok(Vec::new());
        }
        let mut paths = Vec::new();
        for entry in fs::read_dir(&self.base_dir)? {
            let path = entry?.path();
            let Some(name) = path.file_name().and_then(|name| name.to_str()) else {
                continue;
            };
            if path.is_file() && name.starts_with(FAILED_PREFIX) && name.ends_with(FAILED_SUFFIX) {
                paths.push(path);
            }
        }
        Ok(paths)
    }

    pub fn remove(&self, path: &Path) -> io::Result<()> {
        match fs::remove_file(path) {
            Ok(()) => Ok(()),
            Err(error) if error.kind() == io::ErrorKind::NotFound => Ok(()),
            Err(error) => Err(error),
        }
    }

    pub fn read(&self, path: &Path) -> io::Result<Vec<JsonValue>> {
        let text = fs::read_to_string(path)?;
        let mut events = Vec::new();
        for line in text.lines().map(str::trim).filter(|line| !line.is_empty()) {
            if let Ok(value) = json::parse(line) {
                events.push(value);
            }
        }
        Ok(events)
    }
}

fn short_batch_id() -> String {
    let mut bytes = [0_u8; 6];
    if read_random(&mut bytes).is_err() {
        let nanos = SystemTime::now()
            .duration_since(UNIX_EPOCH)
            .unwrap_or_default()
            .as_nanos();
        bytes.copy_from_slice(&nanos.to_le_bytes()[..6]);
    }
    hex_lower(&bytes)
}

fn read_random(bytes: &mut [u8]) -> io::Result<()> {
    File::open("/dev/urandom")?.read_exact(bytes)
}

fn hex_lower(bytes: &[u8]) -> String {
    const DIGITS: &[u8; 16] = b"0123456789abcdef";
    let mut output = String::with_capacity(bytes.len() * 2);
    for byte in bytes {
        output.push(DIGITS[(byte >> 4) as usize] as char);
        output.push(DIGITS[(byte & 0x0f) as usize] as char);
    }
    output
}

fn ensure_private_dir(path: &Path) -> io::Result<()> {
    fs::create_dir_all(path)?;
    restrict_dir_permissions(path)
}

fn ensure_private_file(path: &Path) -> io::Result<()> {
    restrict_file_permissions(path)
}

#[cfg(unix)]
fn restrict_dir_permissions(path: &Path) -> io::Result<()> {
    use std::os::unix::fs::PermissionsExt;

    let mut permissions = fs::metadata(path)?.permissions();
    permissions.set_mode(0o700);
    fs::set_permissions(path, permissions)
}

#[cfg(not(unix))]
fn restrict_dir_permissions(_path: &Path) -> io::Result<()> {
    Ok(())
}

#[cfg(unix)]
fn restrict_file_permissions(path: &Path) -> io::Result<()> {
    use std::os::unix::fs::PermissionsExt;

    let mut permissions = fs::metadata(path)?.permissions();
    permissions.set_mode(0o600);
    fs::set_permissions(path, permissions)
}

#[cfg(not(unix))]
fn restrict_file_permissions(_path: &Path) -> io::Result<()> {
    Ok(())
}
