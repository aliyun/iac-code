use std::env;
use std::fs;
use std::io;
use std::path::{Path, PathBuf};
use std::time::{SystemTime, UNIX_EPOCH};

use iac_code_config::paths::ConfigPaths;

use crate::session_utils::expand_user;

pub(super) fn interactive_startup_banner_debug_log_display_path(debug_log_path: &Path) -> PathBuf {
    debug_log_path
        .parent()
        .filter(|parent| !parent.as_os_str().is_empty())
        .map(|parent| parent.join("latest.log"))
        .unwrap_or_else(|| debug_log_path.to_path_buf())
}

pub(super) fn enable_startup_debug_log(prefix: &str) -> Result<PathBuf, String> {
    let now = SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .map_err(|error| error.to_string())?
        .as_nanos();
    enable_interactive_debug_log(&format!("{prefix}-{now}"))
}

pub(super) fn enable_interactive_debug_log(session_id: &str) -> Result<PathBuf, String> {
    let paths = ConfigPaths::from_env().map_err(|error| error.to_string())?;
    let log_dir = debug_log_dir(&paths)?;
    ensure_private_dir(&log_dir).map_err(|error| error.to_string())?;
    let log_file = log_dir.join(format!("{session_id}.log"));
    fs::OpenOptions::new()
        .create(true)
        .append(true)
        .open(&log_file)
        .map_err(|error| error.to_string())?;
    ensure_private_file(&log_file).map_err(|error| error.to_string())?;
    refresh_latest_log_link(&log_dir, &log_file);
    Ok(log_file)
}

fn debug_log_dir(paths: &ConfigPaths) -> Result<PathBuf, String> {
    let raw = env::var("IAC_CODE_LOG_DIR").unwrap_or_default();
    let trimmed = raw.trim();
    if trimmed.is_empty() {
        return Ok(paths.subdirs().logs);
    }
    let expanded = expand_env_vars(&expand_user(trimmed));
    let path = PathBuf::from(expanded);
    if path.is_absolute() {
        Ok(path)
    } else {
        env::current_dir()
            .map(|cwd| cwd.join(path))
            .map_err(|error| error.to_string())
    }
}

fn expand_env_vars(value: &str) -> String {
    let mut output = String::new();
    let mut chars = value.chars().peekable();

    while let Some(character) = chars.next() {
        if character != '$' {
            output.push(character);
            continue;
        }

        if chars.peek() == Some(&'{') {
            chars.next();
            let mut name = String::new();
            let mut closed = false;
            for next in chars.by_ref() {
                if next == '}' {
                    closed = true;
                    break;
                }
                name.push(next);
            }
            if closed {
                match env::var(&name) {
                    Ok(value) => output.push_str(&value),
                    Err(_) => {
                        output.push_str("${");
                        output.push_str(&name);
                        output.push('}');
                    }
                }
            } else {
                output.push_str("${");
                output.push_str(&name);
            }
            continue;
        }

        let mut name = String::new();
        while let Some(next) = chars.peek().copied() {
            if next == '_' || next.is_ascii_alphanumeric() {
                name.push(next);
                chars.next();
            } else {
                break;
            }
        }
        if name.is_empty() {
            output.push('$');
        } else {
            match env::var(&name) {
                Ok(value) => output.push_str(&value),
                Err(_) => {
                    output.push('$');
                    output.push_str(&name);
                }
            }
        }
    }

    output
}

pub(super) fn ensure_private_dir(path: &Path) -> io::Result<()> {
    fs::create_dir_all(path)?;
    restrict_dir_permissions(path)
}

pub(super) fn ensure_private_file(path: &Path) -> io::Result<()> {
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

#[cfg(unix)]
fn refresh_latest_log_link(log_dir: &Path, log_file: &Path) {
    let latest = log_dir.join("latest.log");
    let _ = fs::remove_file(&latest);
    let _ = std::os::unix::fs::symlink(log_file, latest);
}

#[cfg(not(unix))]
fn refresh_latest_log_link(_log_dir: &Path, _log_file: &Path) {}
