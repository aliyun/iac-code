use std::fs;
use std::path::{Path, PathBuf};
use std::time::{SystemTime, UNIX_EPOCH};

use iac_code_protocol::json;

use super::json_value::{object_fields, object_string};
use super::{is_conversation_session_file, SESSION_JSONL_FILENAME};

pub(super) fn find_session_anywhere(
    projects_dir: &Path,
    session_id: &str,
) -> Option<(String, PathBuf)> {
    let entries = fs::read_dir(projects_dir).ok()?;
    for entry in entries.flatten() {
        let project_dir = entry.path();
        if !project_dir.is_dir() {
            continue;
        }
        let directory_path = project_dir.join(session_id).join(SESSION_JSONL_FILENAME);
        if directory_path.exists() {
            let cwd = read_cwd_from_file(&directory_path).unwrap_or_default();
            return Some((cwd, directory_path));
        }
        let legacy_path = project_dir.join(format!("{session_id}.jsonl"));
        if legacy_path.exists() && is_conversation_session_file(&legacy_path) {
            let cwd = read_cwd_from_file(&legacy_path).unwrap_or_default();
            return Some((cwd, legacy_path));
        }
    }
    None
}

pub(super) fn get_latest_session_anywhere(projects_dir: &Path) -> Option<(String, String)> {
    let mut latest: Option<(SystemTime, PathBuf)> = None;
    let entries = fs::read_dir(projects_dir).ok()?;
    for entry in entries.flatten() {
        let project_dir = entry.path();
        if !project_dir.is_dir() {
            continue;
        }
        for child in fs::read_dir(&project_dir).ok()?.flatten() {
            let path = child.path();
            if path.is_dir() {
                let jsonl = path.join(SESSION_JSONL_FILENAME);
                if jsonl.exists() {
                    update_latest(&mut latest, &jsonl);
                }
                continue;
            }
            if is_conversation_session_file(&path) {
                update_latest(&mut latest, &path);
            }
        }
    }
    let (_mtime, path) = latest?;
    let cwd = read_cwd_from_file(&path).unwrap_or_default();
    let session_id = session_id_from_path(&path)?;
    Some((cwd, session_id))
}

fn read_cwd_from_file(path: &Path) -> Option<String> {
    let text = fs::read_to_string(path).ok()?;
    for line in text.lines().map(str::trim).filter(|line| !line.is_empty()) {
        let Ok(value) = json::parse(line) else {
            continue;
        };
        let Some(fields) = object_fields(&value) else {
            continue;
        };
        if let Some(cwd) = object_string(fields, "cwd") {
            return Some(cwd.to_owned());
        }
    }
    None
}

fn update_latest(latest: &mut Option<(SystemTime, PathBuf)>, path: &Path) {
    let Ok(metadata) = fs::metadata(path) else {
        return;
    };
    let mtime = metadata.modified().unwrap_or(UNIX_EPOCH);
    if latest
        .as_ref()
        .is_none_or(|(latest_mtime, _)| mtime > *latest_mtime)
    {
        *latest = Some((mtime, path.to_path_buf()));
    }
}

fn session_id_from_path(path: &Path) -> Option<String> {
    if path
        .file_name()
        .and_then(|name| name.to_str())
        .is_some_and(|name| name == SESSION_JSONL_FILENAME)
    {
        return path
            .parent()
            .and_then(|parent| parent.file_name())
            .and_then(|name| name.to_str())
            .map(str::to_owned);
    }
    path.file_stem()
        .and_then(|stem| stem.to_str())
        .map(str::to_owned)
}
