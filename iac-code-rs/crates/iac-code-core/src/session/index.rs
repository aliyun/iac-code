use std::cmp::Reverse;
use std::collections::BTreeMap;
use std::fs::{self, File};
use std::io::{self, Read, Seek, SeekFrom};
use std::path::{Path, PathBuf};
use std::time::{SystemTime, UNIX_EPOCH};

use iac_code_protocol::json::{self, JsonValue};

use super::json_value::{object_fields, object_string};
use super::{
    is_conversation_session_file, path_parent, read_session_metadata, sanitize_path,
    SESSION_JSONL_FILENAME,
};

const LITE_READ_BUF_SIZE: u64 = 64 * 1024;

#[derive(Clone, Debug, PartialEq)]
pub struct SessionEntry {
    pub session_id: String,
    pub cwd: String,
    pub project_name: String,
    pub git_branch: Option<String>,
    pub title: String,
    pub mtime: SystemTime,
    pub size_bytes: u64,
    pub name: Option<String>,
    pub auto_title: Option<String>,
    pub is_legacy: bool,
}

#[derive(Clone, Debug)]
pub struct SessionIndex {
    projects_dir: PathBuf,
}

impl SessionIndex {
    pub fn new(projects_dir: impl AsRef<Path>) -> Self {
        Self {
            projects_dir: projects_dir.as_ref().to_path_buf(),
        }
    }

    pub fn list_for_cwd(&self, cwd: &str) -> io::Result<Vec<SessionEntry>> {
        let project_dir = self.projects_dir.join(sanitize_path(cwd));
        if !project_dir.exists() {
            return Ok(Vec::new());
        }
        let mut entries = Vec::new();
        for (path, session_id) in iter_session_files(&project_dir)? {
            if let Some(entry) = build_entry(&path, cwd, &session_id)? {
                entries.push(entry);
            }
        }
        entries.sort_by_key(|entry| Reverse(entry.mtime));
        Ok(entries)
    }

    pub fn list_all_projects(&self) -> io::Result<Vec<SessionEntry>> {
        if !self.projects_dir.exists() {
            return Ok(Vec::new());
        }
        let mut entries = Vec::new();
        for entry in fs::read_dir(&self.projects_dir)? {
            let project_dir = entry?.path();
            if !project_dir.is_dir() {
                continue;
            }
            for (path, session_id) in iter_session_files(&project_dir)? {
                if let Some(entry) = build_entry(&path, "", &session_id)? {
                    entries.push(entry);
                }
            }
        }
        entries.sort_by_key(|entry| Reverse(entry.mtime));
        Ok(entries)
    }

    pub fn find_by_id_or_prefix(&self, arg: &str) -> Option<SessionEntry> {
        if arg.is_empty() {
            return None;
        }
        let entries = self.list_all_projects().ok()?;
        if let Some(entry) = entries.iter().find(|entry| entry.session_id == arg) {
            return Some(entry.clone());
        }
        let matches = entries
            .into_iter()
            .filter(|entry| entry.session_id.starts_with(arg))
            .collect::<Vec<_>>();
        (matches.len() == 1).then(|| matches[0].clone())
    }
}

fn iter_session_files(project_dir: &Path) -> io::Result<Vec<(PathBuf, String)>> {
    let mut files_by_session_id = BTreeMap::<String, PathBuf>::new();
    for entry in fs::read_dir(project_dir)? {
        let path = entry?.path();
        if path.is_file() && is_conversation_session_file(&path) {
            if let Some(stem) = path.file_stem().and_then(|value| value.to_str()) {
                files_by_session_id.insert(stem.to_owned(), path);
            }
        }
    }
    for entry in fs::read_dir(project_dir)? {
        let path = entry?.path();
        if !path.is_dir() {
            continue;
        }
        let jsonl = path.join(SESSION_JSONL_FILENAME);
        if jsonl.exists() {
            if let Some(session_id) = path.file_name().and_then(|value| value.to_str()) {
                files_by_session_id.insert(session_id.to_owned(), jsonl);
            }
        }
    }
    Ok(files_by_session_id
        .into_iter()
        .map(|(session_id, path)| (path, session_id))
        .collect())
}

fn build_entry(
    path: &Path,
    fallback_cwd: &str,
    session_id: &str,
) -> io::Result<Option<SessionEntry>> {
    let stat = match fs::metadata(path) {
        Ok(stat) => stat,
        Err(error) if error.kind() == io::ErrorKind::NotFound => return Ok(None),
        Err(error) => return Err(error),
    };
    let lite = read_lite_metadata(path);
    let is_directory_session = path
        .file_name()
        .and_then(|name| name.to_str())
        .is_some_and(|name| name == SESSION_JSONL_FILENAME);
    let directory_metadata = if is_directory_session {
        read_session_metadata(path_parent(path)?)
            .filter(|metadata| metadata.session_id.as_str() == session_id)
    } else {
        None
    };
    let name = directory_metadata
        .as_ref()
        .and_then(|metadata| metadata.name.clone());
    let auto_title = lite
        .last_prompt
        .or(lite.first_prompt)
        .map(|title| trim_title(&title, 200));
    let cwd = directory_metadata
        .as_ref()
        .and_then(|metadata| metadata.cwd.clone())
        .or(lite.cwd)
        .unwrap_or_else(|| fallback_cwd.to_owned());
    let title = name
        .clone()
        .or_else(|| auto_title.clone())
        .unwrap_or_else(|| "(empty)".to_owned());
    Ok(Some(SessionEntry {
        session_id: session_id.to_owned(),
        cwd: cwd.clone(),
        project_name: project_name(&cwd),
        git_branch: directory_metadata
            .and_then(|metadata| metadata.git_branch)
            .or(lite.git_branch),
        title,
        mtime: stat.modified().unwrap_or(UNIX_EPOCH),
        size_bytes: stat.len(),
        name,
        auto_title,
        is_legacy: !is_directory_session,
    }))
}

#[derive(Default)]
struct LiteMetadata {
    cwd: Option<String>,
    git_branch: Option<String>,
    last_prompt: Option<String>,
    first_prompt: Option<String>,
}

fn read_lite_metadata(path: &Path) -> LiteMetadata {
    let Ok((head, tail)) = read_head_and_tail(path) else {
        return LiteMetadata::default();
    };
    LiteMetadata {
        cwd: extract_first_json_string_field(&head, "cwd"),
        git_branch: extract_last_json_string_field(&tail, "git_branch")
            .or_else(|| extract_first_json_string_field(&head, "git_branch")),
        last_prompt: extract_last_json_string_field(&tail, "last_prompt"),
        first_prompt: extract_first_user_text(&head),
    }
}

fn read_head_and_tail(path: &Path) -> io::Result<(String, String)> {
    let size = fs::metadata(path)?.len();
    let mut file = File::open(path)?;
    let head_len = size.min(LITE_READ_BUF_SIZE) as usize;
    let mut head_bytes = vec![0; head_len];
    file.read_exact(&mut head_bytes)?;

    let tail_bytes = if size <= LITE_READ_BUF_SIZE {
        head_bytes.clone()
    } else {
        file.seek(SeekFrom::Start(size - LITE_READ_BUF_SIZE))?;
        let mut bytes = vec![0; LITE_READ_BUF_SIZE as usize];
        file.read_exact(&mut bytes)?;
        bytes
    };

    Ok((
        String::from_utf8_lossy(&head_bytes).into_owned(),
        String::from_utf8_lossy(&tail_bytes).into_owned(),
    ))
}

fn extract_first_json_string_field(chunk: &str, field: &str) -> Option<String> {
    scan_json_string_field(chunk, field, false)
}

fn extract_last_json_string_field(chunk: &str, field: &str) -> Option<String> {
    scan_json_string_field(chunk, field, true)
}

fn scan_json_string_field(chunk: &str, field: &str, last: bool) -> Option<String> {
    let needle = format!("\"{field}\":");
    let position = if last {
        chunk.rfind(&needle)
    } else {
        chunk.find(&needle)
    }?;
    let mut index = position + needle.len();
    let bytes = chunk.as_bytes();
    while index < bytes.len() && matches!(bytes[index], b' ' | b'\t') {
        index += 1;
    }
    if bytes.get(index) != Some(&b'"') {
        return None;
    }
    index += 1;
    let start = index;
    while index < bytes.len() {
        match bytes[index] {
            b'\\' => index += 2,
            b'"' => return Some(decode_json_string_body(&chunk[start..index])),
            _ => index += 1,
        }
    }
    Some(decode_json_string_body(&chunk[start..]))
}

fn decode_json_string_body(raw: &str) -> String {
    let quoted = format!("\"{raw}\"");
    if let Ok(JsonValue::String(value)) = json::parse(&quoted) {
        return value;
    }

    let mut decoded = String::new();
    let mut chars = raw.chars();
    while let Some(character) = chars.next() {
        if character != '\\' {
            decoded.push(character);
            continue;
        }
        let Some(escaped) = chars.next() else {
            decoded.push(character);
            break;
        };
        match escaped {
            'n' => decoded.push('\n'),
            't' => decoded.push('\t'),
            '"' => decoded.push('"'),
            '\\' => decoded.push('\\'),
            other => {
                decoded.push('\\');
                decoded.push(other);
            }
        }
    }
    decoded
}

fn extract_first_user_text(head: &str) -> Option<String> {
    for line in head.lines().map(str::trim).filter(|line| !line.is_empty()) {
        if !line.contains("\"role\"") || !line.contains("\"user\"") {
            continue;
        }
        let Ok(value) = json::parse(line) else {
            continue;
        };
        let Some(fields) = object_fields(&value) else {
            continue;
        };
        if object_string(fields, "role") != Some("user") {
            continue;
        }
        match fields.get("content") {
            Some(JsonValue::String(text)) if !text.trim().is_empty() => {
                return Some(text.to_owned());
            }
            Some(JsonValue::Array(blocks)) => {
                let texts = blocks
                    .iter()
                    .filter_map(|block| {
                        let fields = object_fields(block)?;
                        (object_string(fields, "type") == Some("text"))
                            .then(|| object_string(fields, "text"))
                            .flatten()
                    })
                    .filter(|text| !text.is_empty())
                    .collect::<Vec<_>>();
                if !texts.is_empty() {
                    return Some(texts.join(" "));
                }
            }
            _ => {}
        }
    }
    None
}

fn trim_title(text: &str, max_len: usize) -> String {
    let flat = text.replace('\n', " ").trim().to_owned();
    if flat.chars().count() <= max_len {
        return flat;
    }
    let mut trimmed = flat.chars().take(max_len).collect::<String>();
    while trimmed.ends_with(char::is_whitespace) {
        trimmed.pop();
    }
    trimmed.push('…');
    trimmed
}

fn project_name(cwd: &str) -> String {
    if cwd.is_empty() {
        return "?".to_owned();
    }
    Path::new(cwd)
        .file_name()
        .and_then(|value| value.to_str())
        .filter(|value| !value.is_empty())
        .unwrap_or("?")
        .to_owned()
}
