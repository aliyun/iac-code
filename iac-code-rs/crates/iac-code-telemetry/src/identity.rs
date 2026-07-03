use std::cell::RefCell;
use std::fs::{self, File};
use std::io::{self, Read};
use std::path::{Path, PathBuf};
use std::time::{SystemTime, UNIX_EPOCH};

pub const USER_ID_PREFIX: &str = "iac_user_";
pub const SESSION_ID_PREFIX: &str = "iac_sess_";
pub const TENANT_ID_PREFIX: &str = "iac_tenant_";

const USER_ID_KEY: &str = "userID";
const TENANT_ENV_VAR: &str = "IAC_CODE_TENANT_ID";

thread_local! {
    static SESSION_ID_OVERRIDES: RefCell<Vec<String>> = const { RefCell::new(Vec::new()) };
}

#[derive(Clone, Debug)]
pub struct Identity {
    settings_path: PathBuf,
    user_id: Option<String>,
    session_id: Option<String>,
    was_first_run: bool,
}

#[derive(Debug)]
pub struct SessionIdOverrideGuard;

pub fn use_session_id(session_id: &str) -> Result<SessionIdOverrideGuard, String> {
    if session_id.is_empty() {
        return Err("session_id must be a non-empty string".to_owned());
    }
    let session_id = prefix_session_id(session_id);
    SESSION_ID_OVERRIDES.with(|overrides| {
        overrides.borrow_mut().push(session_id);
    });
    Ok(SessionIdOverrideGuard)
}

impl Drop for SessionIdOverrideGuard {
    fn drop(&mut self) {
        SESSION_ID_OVERRIDES.with(|overrides| {
            overrides.borrow_mut().pop();
        });
    }
}

impl Identity {
    pub fn new(settings_path: impl AsRef<Path>, session_id: Option<&str>) -> Self {
        Self {
            settings_path: settings_path.as_ref().to_path_buf(),
            user_id: None,
            session_id: session_id.map(prefix_session_id),
            was_first_run: false,
        }
    }

    pub fn get_user_id(&mut self) -> io::Result<String> {
        if let Some(user_id) = &self.user_id {
            return Ok(user_id.clone());
        }
        if let Some(existing) = read_user_id(&self.settings_path) {
            self.user_id = Some(existing.clone());
            return Ok(existing);
        }
        let user_id = format!("{USER_ID_PREFIX}{}", new_uuid_v4());
        write_user_id(&self.settings_path, &user_id)?;
        self.user_id = Some(user_id.clone());
        self.was_first_run = true;
        Ok(user_id)
    }

    pub fn get_session_id(&mut self) -> String {
        if let Some(session_id) = current_session_id_override() {
            return session_id;
        }
        if let Some(session_id) = &self.session_id {
            return session_id.clone();
        }
        let session_id = format!("{SESSION_ID_PREFIX}{}", new_uuid_v4());
        self.session_id = Some(session_id.clone());
        session_id
    }

    pub fn get_tenant_id(&self) -> Option<String> {
        let raw = std::env::var(TENANT_ENV_VAR).unwrap_or_default();
        let trimmed = raw.trim();
        if trimmed.is_empty() {
            return None;
        }
        if trimmed.starts_with(TENANT_ID_PREFIX) {
            Some(trimmed.to_owned())
        } else {
            Some(format!("{TENANT_ID_PREFIX}{trimmed}"))
        }
    }

    pub fn was_first_run(&self) -> bool {
        self.was_first_run
    }
}

fn current_session_id_override() -> Option<String> {
    SESSION_ID_OVERRIDES.with(|overrides| overrides.borrow().last().cloned())
}

fn prefix_session_id(session_id: &str) -> String {
    if session_id.starts_with(SESSION_ID_PREFIX) {
        session_id.to_owned()
    } else {
        format!("{SESSION_ID_PREFIX}{session_id}")
    }
}

fn read_user_id(path: &Path) -> Option<String> {
    let text = fs::read_to_string(path).ok()?;
    for line in text.lines() {
        let trimmed = line.trim();
        let Some((key, value)) = trimmed.split_once(':') else {
            continue;
        };
        if key.trim() != USER_ID_KEY {
            continue;
        }
        let value = value.trim().trim_matches('"').trim_matches('\'');
        if value.starts_with(USER_ID_PREFIX) {
            return Some(value.to_owned());
        }
    }
    None
}

fn write_user_id(path: &Path, user_id: &str) -> io::Result<()> {
    if let Some(parent) = path.parent() {
        fs::create_dir_all(parent)?;
        restrict_dir_permissions(parent)?;
    }
    let content = match fs::read_to_string(path) {
        Ok(content) => content,
        Err(error) if error.kind() == io::ErrorKind::NotFound => String::new(),
        Err(error) => return Err(error),
    };
    let updated = upsert_top_level_user_id(&content, user_id);
    fs::write(path, updated)?;
    restrict_file_permissions(path)
}

fn upsert_top_level_user_id(content: &str, user_id: &str) -> String {
    if content.is_empty() {
        return format!("{USER_ID_KEY}: {user_id}\n");
    }

    let mut replaced = false;
    let mut lines = Vec::new();
    for line in content.lines() {
        let is_top_level = line == line.trim_start();
        let is_user_id = line
            .split_once(':')
            .map(|(key, _)| key.trim() == USER_ID_KEY)
            .unwrap_or(false);
        if is_top_level && is_user_id {
            lines.push(format!("{USER_ID_KEY}: {user_id}"));
            replaced = true;
        } else {
            lines.push(line.to_owned());
        }
    }
    if !replaced {
        lines.push(format!("{USER_ID_KEY}: {user_id}"));
    }
    format!("{}\n", lines.join("\n"))
}

fn new_uuid_v4() -> String {
    let mut bytes = [0_u8; 16];
    if read_random(&mut bytes).is_err() {
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

fn read_random(bytes: &mut [u8]) -> io::Result<()> {
    File::open("/dev/urandom")?.read_exact(bytes)
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
