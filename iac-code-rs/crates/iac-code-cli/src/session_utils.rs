use std::env;
use std::fs;
use std::io::Read;
use std::path::{Component, Path, PathBuf};
use std::process;
use std::time::{SystemTime, UNIX_EPOCH};

use iac_code_core::{SessionEntry, SessionIndex, SessionStorage};
use iac_code_protocol::message::AgentMessage;

use crate::cli_i18n::tr_value;

#[derive(Clone, Debug, Default)]
pub(super) struct HeadlessSession {
    pub(super) session_id: String,
    pub(super) storage_cwd: String,
    pub(super) resume_messages: Vec<AgentMessage>,
}

pub(super) fn resolve_headless_session(
    storage: &SessionStorage,
    cwd: &str,
    resume: &str,
    continue_session: bool,
) -> Result<HeadlessSession, String> {
    if continue_session {
        let Some((session_cwd, session_id)) = storage.get_latest_session_anywhere() else {
            return Ok(HeadlessSession {
                session_id: new_session_id(),
                storage_cwd: cwd.to_owned(),
                resume_messages: Vec::new(),
            });
        };
        if !session_cwd.is_empty() && !same_project_path(&session_cwd, cwd) {
            return Err(cross_project_message(&session_cwd, &session_id));
        }
        let storage_cwd = if session_cwd.is_empty() {
            cwd.to_owned()
        } else {
            session_cwd
        };
        let messages = storage
            .load(&storage_cwd, &session_id)
            .map_err(|error| error.to_string())?;
        return Ok(HeadlessSession {
            session_id,
            storage_cwd,
            resume_messages: SessionStorage::repair_interrupted(&messages),
        });
    }

    if !resume.trim().is_empty() {
        let index = SessionIndex::new(storage_projects_dir(storage));
        let entry = resolve_session_argument(&index, cwd, resume.trim())?;
        if !entry.cwd.is_empty() && !same_project_path(&entry.cwd, cwd) {
            return Err(cross_project_message(&entry.cwd, &entry.session_id));
        }
        let storage_cwd = if entry.cwd.is_empty() {
            cwd.to_owned()
        } else {
            entry.cwd.clone()
        };
        let messages = storage
            .load(&storage_cwd, &entry.session_id)
            .map_err(|error| error.to_string())?;
        return Ok(HeadlessSession {
            session_id: entry.session_id,
            storage_cwd,
            resume_messages: SessionStorage::repair_interrupted(&messages),
        });
    }

    Ok(HeadlessSession {
        session_id: new_session_id(),
        storage_cwd: cwd.to_owned(),
        resume_messages: Vec::new(),
    })
}

fn storage_projects_dir(storage: &SessionStorage) -> PathBuf {
    storage.projects_dir().to_path_buf()
}

pub(super) fn resolve_session_argument(
    index: &SessionIndex,
    current_cwd: &str,
    arg: &str,
) -> Result<SessionEntry, String> {
    let current_entries = index
        .list_for_cwd(current_cwd)
        .map_err(|error| error.to_string())?;
    if let Some(entry) = exact_id(&current_entries, arg) {
        return Ok(entry.clone());
    }
    let current_prefix = id_prefix_matches(&current_entries, arg);
    if current_prefix.len() == 1 {
        return Ok(current_prefix[0].clone());
    }
    if current_prefix.len() > 1 {
        return Err(format!("Session not found: {arg}"));
    }
    if let Some(entry) = exact_name(&current_entries, arg) {
        return Ok(entry.clone());
    }

    let all_entries = index
        .list_all_projects()
        .map_err(|error| error.to_string())?;
    if let Some(entry) = exact_id(&all_entries, arg) {
        return Ok(entry.clone());
    }
    let global_prefix = id_prefix_matches(&all_entries, arg);
    if global_prefix.len() == 1 {
        return Ok(global_prefix[0].clone());
    }
    if global_prefix.len() > 1 {
        return Err(format!("Session not found: {arg}"));
    }
    let name_matches = all_entries
        .iter()
        .filter(|entry| entry.name.as_deref() == Some(arg))
        .collect::<Vec<_>>();
    if name_matches.len() == 1 {
        return Ok(name_matches[0].clone());
    }
    if name_matches.len() > 1 {
        return Err(format!("Multiple sessions match: {arg}"));
    }
    Err(format!("Session not found: {arg}"))
}

fn exact_id<'a>(entries: &'a [SessionEntry], arg: &str) -> Option<&'a SessionEntry> {
    entries.iter().find(|entry| entry.session_id == arg)
}

fn id_prefix_matches<'a>(entries: &'a [SessionEntry], arg: &str) -> Vec<&'a SessionEntry> {
    entries
        .iter()
        .filter(|entry| entry.session_id.starts_with(arg))
        .collect()
}

fn exact_name<'a>(entries: &'a [SessionEntry], arg: &str) -> Option<&'a SessionEntry> {
    entries
        .iter()
        .find(|entry| entry.name.as_deref() == Some(arg))
}

pub(super) fn new_session_id() -> String {
    let mut bytes = [0_u8; 16];
    let random_ok = fs::File::open("/dev/urandom")
        .and_then(|mut file| file.read_exact(&mut bytes))
        .is_ok();
    if !random_ok {
        let nanos = SystemTime::now()
            .duration_since(UNIX_EPOCH)
            .unwrap_or_default()
            .as_nanos();
        let pid = process::id() as u128;
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

pub(super) fn same_project_path(left: &str, right: &str) -> bool {
    normalize_project_path(left) == normalize_project_path(right)
}

fn normalize_project_path(value: &str) -> String {
    let path = PathBuf::from(expand_user(value));
    let absolute = if path.is_absolute() {
        path
    } else {
        env::current_dir()
            .unwrap_or_else(|_| PathBuf::from("."))
            .join(path)
    };
    fs::canonicalize(&absolute)
        .unwrap_or(absolute)
        .to_string_lossy()
        .into_owned()
}

pub(super) fn expand_user(value: &str) -> String {
    if value == "~" {
        return env::var("HOME").unwrap_or_else(|_| ".".to_owned());
    }
    if let Some(rest) = value.strip_prefix("~/") {
        return PathBuf::from(env::var("HOME").unwrap_or_else(|_| ".".to_owned()))
            .join(rest)
            .to_string_lossy()
            .into_owned();
    }
    value.to_owned()
}

pub(super) fn cross_project_message(cwd: &str, session_id: &str) -> String {
    let hint = format_resume_command(cwd, session_id);
    tr_value(
        "Session belongs to another project. Run: {hint}",
        "hint",
        &hint,
    )
}

pub(super) fn format_resume_command(cwd: &str, session_id: &str) -> String {
    format!(
        "cd {} && iac-code --resume {}",
        shell_quote(cwd),
        shell_quote(session_id)
    )
}

pub(super) fn shell_quote(value: &str) -> String {
    if value
        .chars()
        .all(|ch| ch.is_ascii_alphanumeric() || "/._-".contains(ch))
    {
        return value.to_owned();
    }
    format!("'{}'", value.replace('\'', "'\"'\"'"))
}

pub(super) fn current_git_branch(cwd: &str) -> Option<String> {
    let head = read_git_head(cwd)?;
    head.strip_prefix("ref: refs/heads/").map(str::to_owned)
}

fn read_git_head(cwd: &str) -> Option<String> {
    let root = find_git_worktree_root(cwd)?;
    let git_path = root.join(".git");
    let git_dir = if git_path.is_dir() {
        git_path
    } else {
        let text = fs::read_to_string(&git_path).ok()?;
        let raw = text.trim().strip_prefix("gitdir: ")?;
        let path = PathBuf::from(raw);
        if path.is_absolute() {
            path
        } else {
            root.join(path)
        }
    };
    fs::read_to_string(git_dir.join("HEAD"))
        .ok()
        .map(|text| text.trim().to_owned())
}

pub(super) fn find_git_worktree_root(cwd: &str) -> Option<PathBuf> {
    let mut current = PathBuf::from(cwd);
    loop {
        let git_path = current.join(".git");
        if git_path.is_dir() || git_path.is_file() {
            return Some(current);
        }
        if !current.pop() {
            return None;
        }
    }
}

pub(super) fn current_working_directory() -> Result<String, String> {
    let physical_cwd = env::current_dir().map_err(|error| error.to_string())?;
    if let Ok(pwd) = env::var("PWD") {
        let logical_cwd = PathBuf::from(&pwd);
        if logical_cwd.is_absolute() {
            let logical_resolved = logical_cwd.canonicalize();
            let physical_resolved = physical_cwd.canonicalize();
            if logical_resolved.is_ok()
                && physical_resolved.is_ok()
                && logical_resolved.ok() == physical_resolved.ok()
            {
                return Ok(normalize_logical_path(&logical_cwd)
                    .to_string_lossy()
                    .into_owned());
            }
        }
    }
    Ok(physical_cwd.to_string_lossy().into_owned())
}

fn normalize_logical_path(path: &Path) -> PathBuf {
    let mut normalized = PathBuf::new();
    for component in path.components() {
        match component {
            Component::CurDir => {}
            Component::ParentDir => {
                normalized.pop();
            }
            Component::Normal(part) => normalized.push(part),
            Component::RootDir | Component::Prefix(_) => normalized.push(component.as_os_str()),
        }
    }
    normalized
}
