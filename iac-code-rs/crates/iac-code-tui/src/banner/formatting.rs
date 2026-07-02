use std::path::{Path, PathBuf};

pub(super) fn display_cwd(cwd: &Path, home_dir: Option<&Path>) -> String {
    let cwd = absolutize(cwd);
    if let Some(home) = home_dir.map(absolutize) {
        if let Ok(relative) = cwd.strip_prefix(&home) {
            if relative.as_os_str().is_empty() {
                return "~".to_owned();
            }
            return format!("~/{}", path_to_slash(relative));
        }
    }
    path_to_slash(&cwd)
}

fn absolutize(path: &Path) -> PathBuf {
    if path.is_absolute() {
        path.to_path_buf()
    } else {
        std::env::current_dir()
            .map(|cwd| cwd.join(path))
            .unwrap_or_else(|_| path.to_path_buf())
    }
}

fn path_to_slash(path: &Path) -> String {
    path.components()
        .map(|component| component.as_os_str().to_string_lossy())
        .collect::<Vec<_>>()
        .join("/")
}

pub(super) fn capitalize_username(username: &str) -> String {
    let mut chars = username.chars();
    let Some(first) = chars.next() else {
        return "User".to_owned();
    };
    first.to_uppercase().collect::<String>() + chars.as_str()
}

pub(super) fn shell_quote(part: &str) -> String {
    if part.is_empty() {
        return "''".to_owned();
    }
    if part.chars().all(|ch| {
        ch.is_ascii_alphanumeric()
            || matches!(
                ch,
                '_' | '@' | '%' | '+' | '=' | ':' | ',' | '.' | '/' | '-'
            )
    }) {
        return part.to_owned();
    }
    format!("'{}'", part.replace('\'', "'\"'\"'"))
}
