use std::fs::{self, File};
use std::io::{self, Write};
use std::path::{Path, PathBuf};
use std::time::{SystemTime, UNIX_EPOCH};

use iac_code_protocol::json::{self, JsonValue};
use iac_code_protocol::message::{
    AgentContentBlock, AgentMessage, AgentMessageContent, ToolResultBlock,
};

mod discovery;
mod hash;
mod index;
mod json_value;
mod metadata;
mod name;
mod path;
mod record;

pub use index::{SessionEntry, SessionIndex};
pub use metadata::{read_session_metadata, write_session_metadata, SessionMetadata};
pub use name::{normalize_session_name, validate_session_name};
pub use path::sanitize_path;

use discovery::{find_session_anywhere, get_latest_session_anywhere};
use record::{append_jsonl_row, load_session_file, message_tool_uses, stamp_message};

pub const SESSION_JSONL_FILENAME: &str = "session.jsonl";
pub const SESSION_METADATA_FILENAME: &str = "metadata.json";
pub const SESSION_NAME_PATTERN_TEXT: &str = r"^[A-Za-z0-9][A-Za-z0-9._-]{0,199}$";
pub const SESSION_METADATA_SCHEMA_VERSION: u64 = 1;

const INTERRUPTED_TOOL_MESSAGE: &str = "Session interrupted before tool execution completed.";

#[derive(Clone, Debug)]
pub struct SessionStorage {
    projects_dir: PathBuf,
}

impl SessionStorage {
    pub fn new(projects_dir: impl AsRef<Path>) -> io::Result<Self> {
        let projects_dir = projects_dir.as_ref().to_path_buf();
        ensure_private_dir(&projects_dir)?;
        Ok(Self { projects_dir })
    }

    pub fn session_path(&self, cwd: &str, session_id: &str) -> PathBuf {
        let directory_path = self.directory_session_path(cwd, session_id);
        if directory_path.exists() {
            return directory_path;
        }
        let legacy_path = self.legacy_session_path(cwd, session_id);
        if legacy_path.exists() {
            return legacy_path;
        }
        directory_path
    }

    pub fn projects_dir(&self) -> &Path {
        &self.projects_dir
    }

    pub fn legacy_session_path(&self, cwd: &str, session_id: &str) -> PathBuf {
        self.project_dir_for(cwd)
            .join(format!("{session_id}.jsonl"))
    }

    pub fn session_dir(&self, cwd: &str, session_id: &str) -> PathBuf {
        self.project_dir_for(cwd).join(session_id)
    }

    pub fn read_metadata(&self, cwd: &str, session_id: &str) -> Option<SessionMetadata> {
        read_session_metadata(&self.session_dir(cwd, session_id))
    }

    pub fn append(
        &self,
        cwd: &str,
        session_id: &str,
        message: &AgentMessage,
        git_branch: Option<&str>,
    ) -> io::Result<()> {
        let path = self.session_path(cwd, session_id);
        let row = stamp_message(message, cwd, session_id, git_branch);
        append_jsonl_row(&path, &row)
    }

    pub fn append_meta(
        &self,
        cwd: &str,
        session_id: &str,
        meta_entry: JsonValue,
    ) -> io::Result<()> {
        let mut fields = match meta_entry {
            JsonValue::Object(fields) => fields,
            _ => {
                return Err(invalid_input(
                    "meta_entry must be a JSON object with a 'type' field",
                ));
            }
        };
        if !fields.contains_key("type") {
            return Err(invalid_input("meta_entry must include a 'type' field"));
        }
        fields.insert("session_id".to_owned(), json::string(session_id));
        append_jsonl_row(
            &self.session_path(cwd, session_id),
            &JsonValue::Object(fields),
        )
    }

    pub fn save(
        &self,
        cwd: &str,
        session_id: &str,
        messages: &[AgentMessage],
        git_branch: Option<&str>,
    ) -> io::Result<()> {
        let path = self.session_path(cwd, session_id);
        ensure_private_dir(path_parent(&path)?)?;
        let mut file = File::create(&path)?;
        for message in messages {
            let row = stamp_message(message, cwd, session_id, git_branch);
            writeln!(file, "{}", row.to_compact_json())?;
        }
        ensure_private_file(&path)
    }

    pub fn load(&self, cwd: &str, session_id: &str) -> io::Result<Vec<AgentMessage>> {
        load_session_file(&self.session_path(cwd, session_id))
    }

    pub fn exists(&self, cwd: &str, session_id: &str) -> bool {
        self.session_path(cwd, session_id).exists()
    }

    pub fn rename_session(
        &self,
        cwd: &str,
        session_id: &str,
        name: &str,
        git_branch: Option<&str>,
    ) -> io::Result<String> {
        let normalized = normalize_session_name(name).map_err(invalid_input)?;
        if let Some(current) = self.read_metadata(cwd, session_id) {
            if current.name.as_deref() == Some(normalized.as_str()) {
                return Ok("unchanged".to_owned());
            }
        }

        if let Some(owner) = self.name_owner_in_project(cwd, &normalized)? {
            if owner != session_id {
                return Err(invalid_input(format!(
                    "Session name already exists in this project: {normalized}"
                )));
            }
        }

        let current = self.read_metadata(cwd, session_id);
        let session_dir = self.ensure_directory_format(cwd, session_id)?;
        let now = utc_now();
        let metadata = SessionMetadata {
            session_id: session_id.to_owned(),
            name: Some(normalized),
            cwd: Some(cwd.to_owned()),
            git_branch: git_branch.map(str::to_owned),
            created_at: current
                .as_ref()
                .and_then(|metadata| metadata.created_at.clone())
                .or_else(|| Some(now.clone())),
            updated_at: Some(now),
            schema_version: SESSION_METADATA_SCHEMA_VERSION,
        };
        write_session_metadata(&session_dir, &metadata)?;
        Ok("renamed".to_owned())
    }

    pub fn find_session_anywhere(&self, session_id: &str) -> Option<(String, PathBuf)> {
        find_session_anywhere(&self.projects_dir, session_id)
    }

    pub fn get_latest_session_anywhere(&self) -> Option<(String, String)> {
        get_latest_session_anywhere(&self.projects_dir)
    }

    pub fn detect_interruption(messages: &[AgentMessage]) -> bool {
        let Some(last) = messages.last() else {
            return false;
        };
        last.role == "assistant" && !message_tool_uses(last).is_empty()
    }

    pub fn repair_interrupted(messages: &[AgentMessage]) -> Vec<AgentMessage> {
        if !Self::detect_interruption(messages) {
            return messages.to_vec();
        }
        let mut repaired = messages.to_vec();
        let results = messages
            .last()
            .into_iter()
            .flat_map(message_tool_uses)
            .map(|tool_use| {
                AgentContentBlock::ToolResult(ToolResultBlock {
                    tool_use_id: tool_use.id.clone(),
                    content: INTERRUPTED_TOOL_MESSAGE.to_owned(),
                    is_error: true,
                })
            })
            .collect::<Vec<_>>();
        repaired.push(AgentMessage {
            role: "user".to_owned(),
            content: AgentMessageContent::Blocks(results),
            token_count: 0,
            elapsed_seconds: 0.0,
        });
        repaired
    }

    fn project_dir_for(&self, cwd: &str) -> PathBuf {
        self.projects_dir.join(sanitize_path(cwd))
    }

    fn directory_session_path(&self, cwd: &str, session_id: &str) -> PathBuf {
        self.session_dir(cwd, session_id)
            .join(SESSION_JSONL_FILENAME)
    }

    fn ensure_directory_format(&self, cwd: &str, session_id: &str) -> io::Result<PathBuf> {
        let session_dir = self.session_dir(cwd, session_id);
        let directory_path = session_dir.join(SESSION_JSONL_FILENAME);
        if directory_path.exists() {
            return Ok(session_dir);
        }

        let legacy_path = self.legacy_session_path(cwd, session_id);
        ensure_private_dir(&session_dir)?;
        if legacy_path.exists() {
            fs::rename(&legacy_path, &directory_path)?;
        } else {
            File::create(&directory_path)?;
        }
        ensure_private_file(&directory_path)?;
        Ok(session_dir)
    }

    fn project_session_dirs(&self, cwd: &str) -> io::Result<Vec<PathBuf>> {
        let project_dir = self.project_dir_for(cwd);
        if !project_dir.exists() {
            return Ok(Vec::new());
        }
        let mut dirs = Vec::new();
        for entry in fs::read_dir(project_dir)? {
            let path = entry?.path();
            if path.is_dir() && path.join(SESSION_JSONL_FILENAME).exists() {
                dirs.push(path);
            }
        }
        Ok(dirs)
    }

    fn name_owner_in_project(&self, cwd: &str, name: &str) -> io::Result<Option<String>> {
        for session_dir in self.project_session_dirs(cwd)? {
            if let Some(metadata) = read_session_metadata(&session_dir) {
                if metadata.name.as_deref() == Some(name) {
                    return Ok(Some(metadata.session_id));
                }
            }
        }
        Ok(None)
    }
}

pub(super) fn is_conversation_session_file(path: &Path) -> bool {
    let Some(name) = path.file_name().and_then(|name| name.to_str()) else {
        return false;
    };
    name.ends_with(".jsonl") && !name.ends_with(".usage.jsonl")
}

pub(super) fn path_parent(path: &Path) -> io::Result<&Path> {
    path.parent()
        .ok_or_else(|| invalid_input(format!("path has no parent: {}", path.display())))
}

fn invalid_input(message: impl Into<String>) -> io::Error {
    io::Error::new(io::ErrorKind::InvalidInput, message.into())
}

fn utc_now() -> String {
    let seconds = SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .unwrap_or_default()
        .as_secs() as i64;
    format_unix_utc(seconds)
}

fn format_unix_utc(seconds: i64) -> String {
    let days = seconds.div_euclid(86_400);
    let seconds_of_day = seconds.rem_euclid(86_400);
    let (year, month, day) = civil_from_days(days);
    let hour = seconds_of_day / 3_600;
    let minute = seconds_of_day % 3_600 / 60;
    let second = seconds_of_day % 60;
    format!("{year:04}-{month:02}-{day:02}T{hour:02}:{minute:02}:{second:02}Z")
}

fn civil_from_days(days_since_epoch: i64) -> (i64, i64, i64) {
    let z = days_since_epoch + 719_468;
    let era = if z >= 0 { z } else { z - 146_096 } / 146_097;
    let doe = z - era * 146_097;
    let yoe = (doe - doe / 1_460 + doe / 36_524 - doe / 146_096) / 365;
    let y = yoe + era * 400;
    let doy = doe - (365 * yoe + yoe / 4 - yoe / 100);
    let mp = (5 * doy + 2) / 153;
    let day = doy - (153 * mp + 2) / 5 + 1;
    let month = mp + if mp < 10 { 3 } else { -9 };
    let year = y + if month <= 2 { 1 } else { 0 };
    (year, month, day)
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
