use std::path::{Path, PathBuf};

use iac_code_protocol::json::{self, JsonValue};
use ring::digest;

pub(super) fn owner_hash(owner: &str) -> String {
    let digest = digest::digest(&digest::SHA256, owner.as_bytes());
    let mut output = String::with_capacity(digest.as_ref().len() * 2);
    for byte in digest.as_ref() {
        use std::fmt::Write;
        write!(output, "{byte:02x}").expect("writing to String should not fail");
    }
    output
}

pub(super) fn sorted_owner_dirs(root: &Path) -> Vec<PathBuf> {
    let Ok(entries) = std::fs::read_dir(root) else {
        return Vec::new();
    };
    let mut paths = entries
        .filter_map(|entry| entry.ok().map(|entry| entry.path()))
        .filter(|path| path.is_dir())
        .collect::<Vec<_>>();
    paths.sort();
    paths
}

pub(super) fn sorted_json_files(dir: &Path) -> Vec<PathBuf> {
    let Ok(entries) = std::fs::read_dir(dir) else {
        return Vec::new();
    };
    let mut paths = entries
        .filter_map(|entry| entry.ok().map(|entry| entry.path()))
        .filter(|path| is_json_file(path))
        .collect::<Vec<_>>();
    paths.sort();
    paths
}

pub(super) fn is_json_file(path: &Path) -> bool {
    path.extension()
        .is_some_and(|extension| extension == "json")
}

pub(super) fn read_json_file(path: &Path) -> Option<JsonValue> {
    let value = std::fs::read_to_string(path).ok()?;
    json::parse(&value).ok()
}

pub(super) fn remove_file_if_exists(path: &Path) -> std::io::Result<()> {
    match std::fs::remove_file(path) {
        Ok(()) => Ok(()),
        Err(error) if error.kind() == std::io::ErrorKind::NotFound => Ok(()),
        Err(error) => Err(error),
    }
}
