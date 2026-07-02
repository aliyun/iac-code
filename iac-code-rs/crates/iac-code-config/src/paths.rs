use std::env;
use std::io;
use std::path::{Component, Path, PathBuf};

use crate::file_security::ensure_private_dir;

const CONFIG_DIR_NAME: &str = ".iac-code";
const CONFIG_DIR_ENV_VAR: &str = "IAC_CODE_CONFIG_DIR";
const CREDENTIALS_FILE: &str = ".credentials.yml";
const SETTINGS_FILE: &str = "settings.yml";
const CLOUD_CREDENTIALS_FILE: &str = ".cloud-credentials.yml";
const HISTORY_FILE: &str = ".input_history";

#[derive(Clone, Debug, PartialEq, Eq)]
pub struct ConfigPaths {
    pub config_dir: PathBuf,
    pub credentials_path: PathBuf,
    pub settings_path: PathBuf,
    pub cloud_credentials_path: PathBuf,
    pub history_path: PathBuf,
}

impl ConfigPaths {
    pub fn from_env() -> io::Result<Self> {
        let config_dir = resolve_config_dir()?;
        ensure_private_dir(&config_dir)?;
        Ok(Self {
            credentials_path: config_dir.join(CREDENTIALS_FILE),
            settings_path: config_dir.join(SETTINGS_FILE),
            cloud_credentials_path: config_dir.join(CLOUD_CREDENTIALS_FILE),
            history_path: config_dir.join(HISTORY_FILE),
            config_dir,
        })
    }

    pub fn subdirs(&self) -> ConfigSubdirs {
        ConfigSubdirs {
            projects: self.config_dir.join("projects"),
            image_cache: self.config_dir.join("image-cache"),
            tool_results: self.config_dir.join("tool-results"),
            logs: self.config_dir.join("logs"),
            memory: self.config_dir.join("memory"),
            a2a: self.config_dir.join("a2a"),
            telemetry: self.config_dir.join("telemetry"),
            skills: self.config_dir.join("skills"),
        }
    }
}

#[derive(Clone, Debug, PartialEq, Eq)]
pub struct ConfigSubdirs {
    pub projects: PathBuf,
    pub image_cache: PathBuf,
    pub tool_results: PathBuf,
    pub logs: PathBuf,
    pub memory: PathBuf,
    pub a2a: PathBuf,
    pub telemetry: PathBuf,
    pub skills: PathBuf,
}

fn resolve_config_dir() -> io::Result<PathBuf> {
    let raw = env::var(CONFIG_DIR_ENV_VAR).unwrap_or_default();
    let trimmed = raw.trim();
    if trimmed.is_empty() {
        return Ok(home_dir().join(CONFIG_DIR_NAME));
    }

    let expanded_user = expand_user(trimmed);
    let expanded_vars = expand_vars(&expanded_user);
    absolutize(&expanded_vars)
}

fn expand_user(value: &str) -> String {
    if value == "~" {
        return home_dir().to_string_lossy().into_owned();
    }
    if let Some(rest) = value.strip_prefix("~/") {
        return home_dir().join(rest).to_string_lossy().into_owned();
    }
    value.to_owned()
}

fn expand_vars(value: &str) -> String {
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

fn absolutize(value: &str) -> io::Result<PathBuf> {
    let path = PathBuf::from(value);
    let absolute = if path.is_absolute() {
        path
    } else {
        env::current_dir()?.join(path)
    };
    Ok(resolve_existing_ancestor(&absolute))
}

fn normalize_path(path: &Path) -> PathBuf {
    let mut normalized = PathBuf::new();
    for component in path.components() {
        match component {
            Component::CurDir => {}
            Component::ParentDir => {
                normalized.pop();
            }
            Component::Normal(value) => normalized.push(value),
            Component::RootDir | Component::Prefix(_) => normalized.push(component.as_os_str()),
        }
    }
    normalized
}

fn resolve_existing_ancestor(path: &Path) -> PathBuf {
    let normalized = normalize_path(path);
    if let Ok(canonical) = normalized.canonicalize() {
        return canonical;
    }

    let mut missing_components = Vec::new();
    let mut cursor = normalized.as_path();
    while !cursor.exists() {
        let Some(file_name) = cursor.file_name() else {
            return normalized;
        };
        missing_components.push(file_name.to_os_string());
        let Some(parent) = cursor.parent() else {
            return normalized;
        };
        cursor = parent;
    }

    let Ok(mut resolved) = cursor.canonicalize() else {
        return normalized;
    };
    for component in missing_components.iter().rev() {
        resolved.push(component);
    }
    normalize_path(&resolved)
}

fn home_dir() -> PathBuf {
    env::var_os("HOME")
        .map(PathBuf::from)
        .unwrap_or_else(|| PathBuf::from("."))
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn expand_vars_keeps_unknown_variables_like_python() {
        env::remove_var("IAC_CODE_RS_MISSING_VAR");

        assert_eq!(
            expand_vars("$IAC_CODE_RS_MISSING_VAR/config"),
            "$IAC_CODE_RS_MISSING_VAR/config"
        );
        assert_eq!(
            expand_vars("${IAC_CODE_RS_MISSING_VAR}/config"),
            "${IAC_CODE_RS_MISSING_VAR}/config"
        );
    }
}
