use std::env;
use std::ffi::OsString;
use std::fs;
use std::path::{Path, PathBuf};

use iac_code_protocol::permission::{PermissionDecisionReason, PermissionResult};

const BASE_SENSITIVE_PATHS: &[&str] = &[
    ".git/",
    ".git",
    ".iac-code/",
    ".iac-code",
    ".iac-code/.credentials.yml",
    ".iac-code/.cloud-credentials.yml",
    ".bashrc",
    ".zshrc",
    ".profile",
    ".bash_profile",
    ".ssh/",
    ".ssh",
    ".env",
    ".aliyun/",
    ".aliyun",
    ".alibabacloud/",
    ".alibabacloud",
    ".aws/credentials",
];

#[cfg(windows)]
const WINDOWS_SENSITIVE_PATHS: &[&str] = &[
    "AppData/Roaming/Microsoft/Windows/PowerShell",
    "AppData/Local/Microsoft/Credentials",
    "ntuser.dat",
];

#[derive(Clone, Debug, PartialEq, Eq)]
pub enum PathDecision {
    Allow,
    Ask { reason_type: String, detail: String },
}

impl PathDecision {
    pub fn to_permission_result(&self) -> PermissionResult {
        match self {
            Self::Allow => PermissionResult::passthrough(),
            Self::Ask {
                reason_type,
                detail,
            } => PermissionResult {
                behavior: "ask".into(),
                message: detail.clone(),
                reason: Some(PermissionDecisionReason {
                    type_name: reason_type.clone(),
                    detail: detail.clone(),
                }),
                suggestions: None,
            },
        }
    }
}

pub fn check_read_path(
    path: &str,
    cwd: &str,
    additional_directories: &[String],
    trusted_read_directories: &[String],
) -> PathDecision {
    let resolved = resolve_candidate(path, cwd);

    if is_in_allowed_roots(&resolved, trusted_read_directories) {
        return PathDecision::Allow;
    }

    if path_hits_sensitive(&resolved) {
        return ask("safety_check", "read touches a sensitive path");
    }

    let allowed_roots = std::iter::once(cwd)
        .chain(additional_directories.iter().map(String::as_str))
        .chain(std::iter::once(iac_code_application_root()))
        .filter(|root| !root.is_empty())
        .map(ToOwned::to_owned)
        .collect::<Vec<String>>();
    if is_in_allowed_roots(&resolved, &allowed_roots) {
        return PathDecision::Allow;
    }

    ask("path_constraint", "path outside allowed directories")
}

pub fn check_write_path(path: &str, cwd: &str, additional_directories: &[String]) -> PathDecision {
    let resolved = resolve_candidate(path, cwd);

    if path_hits_sensitive(&resolved) {
        return ask("safety_check", "write touches a sensitive path");
    }

    let allowed_roots = std::iter::once(cwd)
        .chain(additional_directories.iter().map(String::as_str))
        .filter(|root| !root.is_empty())
        .map(ToOwned::to_owned)
        .collect::<Vec<String>>();
    if is_in_allowed_roots(&resolved, &allowed_roots) {
        return PathDecision::Allow;
    }

    ask("path_constraint", "path outside allowed directories")
}

pub fn resolve_candidate(path: &str, cwd: &str) -> PathBuf {
    let expanded = expand_user(path);
    let candidate = Path::new(&expanded);
    let joined = if candidate.is_absolute() {
        candidate.to_path_buf()
    } else {
        Path::new(cwd).join(candidate)
    };
    realpath_compat(&joined)
}

fn ask(reason_type: &str, detail: &str) -> PathDecision {
    PathDecision::Ask {
        reason_type: reason_type.into(),
        detail: detail.into(),
    }
}

fn is_in_allowed_roots(path: &Path, roots: &[String]) -> bool {
    roots
        .iter()
        .filter(|root| !root.is_empty())
        .any(|root| path_is_under(path, Path::new(root)))
}

fn path_is_under(path: &Path, root: &Path) -> bool {
    let path_r = normalize_for_platform_path(&realpath_compat(path), cfg!(windows));
    let root_r = normalize_for_platform_path(&realpath_compat(root), cfg!(windows));
    if path_r == root_r {
        return true;
    }

    let root_prefix = root_r.trim_end_matches('/');
    path_r.starts_with(&format!("{root_prefix}/"))
}

fn path_hits_sensitive(path: &Path) -> bool {
    let normalized = normalize_for_platform_path(
        &realpath_compat(path),
        cfg!(any(windows, target_os = "macos")),
    );
    let (single, multi) = sensitive_lookups(cfg!(any(windows, target_os = "macos")));
    let parts = normalized.split('/').collect::<Vec<&str>>();

    parts
        .iter()
        .any(|part| single.iter().any(|entry| entry == part))
        || multi.iter().any(|entry| normalized.contains(entry))
}

fn sensitive_lookups(case_insensitive: bool) -> (Vec<String>, Vec<String>) {
    let mut single = Vec::new();
    let mut multi = Vec::new();
    for entry in sensitive_paths() {
        let cleaned = normalize_for_platform_str(entry.trim_end_matches('/'), case_insensitive);
        if cleaned.is_empty() {
            continue;
        }
        if cleaned.contains('/') {
            multi.push(cleaned);
        } else {
            single.push(cleaned);
        }
    }
    (single, multi)
}

fn sensitive_paths() -> Vec<&'static str> {
    #[cfg(windows)]
    {
        let mut paths = BASE_SENSITIVE_PATHS.to_vec();
        paths.extend_from_slice(WINDOWS_SENSITIVE_PATHS);
        paths
    }

    #[cfg(not(windows))]
    {
        BASE_SENSITIVE_PATHS.to_vec()
    }
}

fn normalize_for_platform_path(path: &Path, case_insensitive: bool) -> String {
    normalize_for_platform_str(&path.to_string_lossy(), case_insensitive)
}

fn normalize_for_platform_str(path: &str, case_insensitive: bool) -> String {
    let normalized = path.replace('\\', "/");
    if case_insensitive {
        normalized.to_lowercase()
    } else {
        normalized
    }
}

fn expand_user(path: &str) -> String {
    if path == "~" {
        home_dir()
            .map(|home| home.to_string_lossy().into_owned())
            .unwrap_or_else(|| path.into())
    } else if let Some(rest) = path.strip_prefix("~/") {
        home_dir()
            .map(|home| home.join(rest).to_string_lossy().into_owned())
            .unwrap_or_else(|| path.into())
    } else {
        path.into()
    }
}

fn home_dir() -> Option<PathBuf> {
    env::var_os("HOME")
        .map(PathBuf::from)
        .or_else(|| env::var_os("USERPROFILE").map(PathBuf::from))
}

fn realpath_compat(path: &Path) -> PathBuf {
    if let Ok(canonical) = fs::canonicalize(path) {
        return canonical;
    }

    let mut current = path;
    let mut tail: Vec<OsString> = Vec::new();
    while !current.exists() {
        let Some(name) = current.file_name() else {
            return path.to_path_buf();
        };
        tail.push(name.to_os_string());
        let Some(parent) = current.parent() else {
            return path.to_path_buf();
        };
        current = parent;
    }

    let mut resolved = fs::canonicalize(current).unwrap_or_else(|_| current.to_path_buf());
    for component in tail.iter().rev() {
        resolved.push(component);
    }
    resolved
}

fn iac_code_application_root() -> &'static str {
    env!("CARGO_MANIFEST_DIR")
}
