use std::cell::RefCell;
use std::env;
use std::fs;
use std::path::{Path, PathBuf};
use std::time::SystemTime;

use crate::{CompletionToken, SuggestionItem, SuggestionProvider};

pub const MAX_HISTORY_SUGGESTIONS: usize = 100;

type HistoryCacheKey = (PathBuf, Option<SystemTime>, u64);

#[derive(Debug)]
pub struct ShellHistoryProvider {
    history_path: Option<PathBuf>,
    cache_key: RefCell<Option<HistoryCacheKey>>,
    cache_entries: RefCell<Vec<String>>,
    max_suggestions: usize,
}

impl ShellHistoryProvider {
    pub fn new() -> Self {
        Self::with_history_path(detect_shell_history_path(), MAX_HISTORY_SUGGESTIONS)
    }

    pub fn with_history_path(history_path: Option<PathBuf>, max_suggestions: usize) -> Self {
        Self {
            history_path,
            cache_key: RefCell::new(None),
            cache_entries: RefCell::new(Vec::new()),
            max_suggestions,
        }
    }

    fn current_cache_key(&self) -> Option<HistoryCacheKey> {
        let path = self.history_path.as_ref()?;
        let metadata = fs::metadata(path).ok()?;
        Some((path.clone(), metadata.modified().ok(), metadata.len()))
    }

    fn entries(&self) -> Vec<String> {
        let Some(cache_key) = self.current_cache_key() else {
            *self.cache_key.borrow_mut() = None;
            self.cache_entries.borrow_mut().clear();
            return Vec::new();
        };

        if self.cache_key.borrow().as_ref() != Some(&cache_key) {
            *self.cache_key.borrow_mut() = Some(cache_key.clone());
            *self.cache_entries.borrow_mut() = read_shell_history(&cache_key.0);
        }
        self.cache_entries.borrow().clone()
    }
}

impl Default for ShellHistoryProvider {
    fn default() -> Self {
        Self::new()
    }
}

impl SuggestionProvider for ShellHistoryProvider {
    fn trigger(&self) -> &str {
        "!"
    }

    fn provide(&self, token: &CompletionToken) -> Vec<SuggestionItem> {
        if self.history_path.is_none() {
            return Vec::new();
        }

        let query = token.text.strip_prefix('!').unwrap_or(&token.text);
        let query_lower = query.to_ascii_lowercase();
        let mut seen = Vec::<String>::new();
        let mut matched = Vec::<String>::new();

        for entry in self.entries().into_iter().rev() {
            if matched.len() >= self.max_suggestions {
                break;
            }
            if seen.iter().any(|seen_entry| seen_entry == &entry) {
                continue;
            }
            if entry.to_ascii_lowercase().contains(&query_lower) {
                seen.push(entry.clone());
                matched.push(entry);
            }
        }

        let total = matched.len();
        matched
            .into_iter()
            .enumerate()
            .map(|(index, entry)| SuggestionItem {
                id: format!("shell:{index}"),
                display_text: entry.clone(),
                completion: format!("!{entry}"),
                description: Some(String::new()),
                icon: Some("↑".to_owned()),
                source: "shell".to_owned(),
                score: (total - index) as f64,
                arg_hint: None,
            })
            .collect()
    }
}

pub fn detect_shell_history_path() -> Option<PathBuf> {
    let shell = env::var("SHELL").unwrap_or_default();
    let home = home_dir()?;

    #[cfg(windows)]
    if shell.is_empty() {
        if let Some(path) = env::var_os("USERPROFILE")
            .map(PathBuf::from)
            .map(|path| path.join(".bash_history"))
            .filter(|path| path.exists())
        {
            return Some(path);
        }
        if let Some(path) = env::var_os("APPDATA")
            .map(PathBuf::from)
            .map(|path| {
                path.join("Microsoft")
                    .join("Windows")
                    .join("PowerShell")
                    .join("PSReadLine")
                    .join("ConsoleHost_history.txt")
            })
            .filter(|path| path.exists())
        {
            return Some(path);
        }
        return None;
    }

    if shell.contains("zsh") {
        return existing_history_path(home.join(".zsh_history"));
    }
    if shell.contains("bash") {
        return existing_history_path(home.join(".bash_history"));
    }

    let zsh = home.join(".zsh_history");
    if zsh.exists() {
        return Some(zsh);
    }
    let bash = home.join(".bash_history");
    if bash.exists() {
        return Some(bash);
    }
    None
}

pub fn read_shell_history(path: impl AsRef<Path>) -> Vec<String> {
    let Ok(raw) = fs::read(path) else {
        return Vec::new();
    };
    let text = String::from_utf8_lossy(&raw);
    let mut entries = Vec::new();

    for line in text.lines() {
        if line.starts_with(": ") && line.contains(';') {
            if let Some((_, command)) = line.split_once(';') {
                let command = command.trim();
                if !command.is_empty() {
                    entries.push(command.to_owned());
                }
            }
            continue;
        }

        let line = line.trim();
        if !line.is_empty() {
            entries.push(line.to_owned());
        }
    }
    entries
}

fn existing_history_path(path: PathBuf) -> Option<PathBuf> {
    path.exists().then_some(path)
}

fn home_dir() -> Option<PathBuf> {
    env::var_os("HOME").map(PathBuf::from)
}
