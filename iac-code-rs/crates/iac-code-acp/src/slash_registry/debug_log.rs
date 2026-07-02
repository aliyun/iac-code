use std::fs;
use std::io;
use std::path::{Path, PathBuf};
use std::sync::Mutex;

use iac_code_config::paths::ConfigPaths;

static DEBUG_LOG_PATH: Mutex<Option<PathBuf>> = Mutex::new(None);

pub(super) fn current_debug_log_path() -> Option<PathBuf> {
    DEBUG_LOG_PATH.lock().ok().and_then(|path| path.clone())
}

pub(super) fn enable_acp_debug_log() -> Result<PathBuf, String> {
    let paths = ConfigPaths::from_env().map_err(|error| error.to_string())?;
    let log_dir = paths.subdirs().logs;
    ensure_private_dir(&log_dir).map_err(|error| error.to_string())?;
    let log_file = log_dir.join("acp.log");
    fs::OpenOptions::new()
        .create(true)
        .append(true)
        .open(&log_file)
        .map_err(|error| error.to_string())?;
    ensure_private_file(&log_file).map_err(|error| error.to_string())?;
    refresh_latest_log_link(&log_dir, &log_file);
    if let Ok(mut current) = DEBUG_LOG_PATH.lock() {
        *current = Some(log_file.clone());
    }
    Ok(log_file)
}

pub(super) fn disable_acp_debug_log() {
    if let Ok(mut current) = DEBUG_LOG_PATH.lock() {
        *current = None;
    }
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

#[cfg(unix)]
fn refresh_latest_log_link(log_dir: &Path, log_file: &Path) {
    let latest = log_dir.join("latest.log");
    let _ = fs::remove_file(&latest);
    let _ = std::os::unix::fs::symlink(log_file, latest);
}

#[cfg(not(unix))]
fn refresh_latest_log_link(_log_dir: &Path, _log_file: &Path) {}
