use std::cell::RefCell;
use std::fs;
use std::path::{Path, PathBuf};
use std::time::{Duration, Instant};

use crate::{CompletionToken, SuggestionItem, SuggestionProvider};

const EXCLUDE_DIRS: &[&str] = &[
    ".git",
    ".svn",
    ".hg",
    ".bzr",
    ".jj",
    ".sl",
    ".vscode",
    ".idea",
    ".claude",
    "__pycache__",
    ".venv",
    "venv",
    ".tox",
    ".mypy_cache",
    ".ruff_cache",
    ".pytest_cache",
    ".eggs",
    ".nox",
    "node_modules",
    ".next",
    ".nuxt",
    "bower_components",
    "dist",
    "build",
    "_build",
    ".build",
    "target",
    ".cache",
    ".npm",
    ".yarn",
];
const MAX_INDEX_FILES: usize = 10_000;
const INDEX_STALE_SECONDS: Duration = Duration::from_secs(30);

pub fn fuzzy_match(query: &str, text: &str) -> Option<f64> {
    if query.is_empty() {
        return Some(0.0);
    }

    let query_lower = query.to_lowercase();
    let text_lower = text.to_lowercase();
    if query_lower
        .chars()
        .any(|query_ch| !text_lower.contains(query_ch))
    {
        return None;
    }

    let query_chars = query_lower.chars().collect::<Vec<_>>();
    let text_chars = text_lower.chars().collect::<Vec<_>>();
    let mut score = 0.0;
    let mut text_index = 0;
    let mut query_index = 0;
    let mut consecutive = 0.0;
    let mut last_text_index: Option<usize> = None;
    let mut prefix_bonus_given = false;

    while query_index < query_chars.len() && text_index < text_chars.len() {
        if text_chars[text_index] == query_chars[query_index] {
            score += 1.0;

            if text_index == 0 && query_index == 0 && !prefix_bonus_given {
                score += 2.0;
                prefix_bonus_given = true;
            }

            if text_index == 0 || matches!(text_chars[text_index - 1], ' ' | '_' | '-' | '/' | '.')
            {
                score += 1.5;
            }

            if last_text_index == Some(text_index.saturating_sub(1)) {
                consecutive += 1.0;
                score += 0.5 * consecutive;
            } else {
                consecutive = 0.0;
            }

            last_text_index = Some(text_index);
            query_index += 1;
        }
        text_index += 1;
    }

    (query_index == query_chars.len()).then_some(score)
}

pub struct FileSuggestionProvider {
    root_dir: PathBuf,
    cache: RefCell<FileIndexCache>,
}

#[derive(Clone, Debug, Default)]
struct FileIndexCache {
    index: Vec<String>,
    index_time: Option<Instant>,
}

impl FileSuggestionProvider {
    pub fn new(root_dir: impl Into<PathBuf>) -> Self {
        let root_dir = root_dir.into();
        Self {
            root_dir: absolute_path(&root_dir),
            cache: RefCell::new(FileIndexCache::default()),
        }
    }

    fn cache_needs_refresh(cache: &FileIndexCache) -> bool {
        cache
            .index_time
            .is_none_or(|time| time.elapsed() > INDEX_STALE_SECONDS)
    }
}

impl SuggestionProvider for FileSuggestionProvider {
    fn trigger(&self) -> &str {
        "@"
    }

    fn provide(&self, token: &CompletionToken) -> Vec<SuggestionItem> {
        {
            let mut cache = self.cache.borrow_mut();
            if Self::cache_needs_refresh(&cache) {
                let mut files = Vec::new();
                collect_files(&self.root_dir, &self.root_dir, &mut files);
                cache.index = files;
                cache.index_time = Some(Instant::now());
            }
        }
        let query = token.text.strip_prefix('@').unwrap_or(&token.text);
        let cache = self.cache.borrow();
        let mut scored = cache
            .index
            .iter()
            .filter_map(|path| fuzzy_match(query, path).map(|score| (score, path.as_str())))
            .collect::<Vec<_>>();
        scored.sort_by(|left, right| {
            right
                .0
                .partial_cmp(&left.0)
                .unwrap_or(std::cmp::Ordering::Equal)
        });

        scored
            .into_iter()
            .map(|(score, rel_path)| SuggestionItem {
                id: format!("file:{rel_path}"),
                display_text: rel_path.to_owned(),
                completion: format!("@{rel_path}"),
                description: Some(String::new()),
                icon: Some("+".to_owned()),
                source: "file".to_owned(),
                score,
                arg_hint: None,
            })
            .collect()
    }
}

#[derive(Clone, Debug)]
pub struct DirectorySuggestionProvider {
    root_dir: PathBuf,
}

impl DirectorySuggestionProvider {
    pub fn new(root_dir: impl Into<PathBuf>) -> Self {
        let root_dir = root_dir.into();
        Self {
            root_dir: absolute_path(&root_dir),
        }
    }

    fn list_entries(&self, dir_path: &Path) -> Vec<(String, bool)> {
        let Ok(entries) = fs::read_dir(dir_path) else {
            return Vec::new();
        };
        let mut entries = entries
            .filter_map(Result::ok)
            .filter_map(|entry| {
                let name = entry.file_name().to_string_lossy().into_owned();
                let is_dir = entry.file_type().ok()?.is_dir();
                if name.starts_with('.') || (is_dir && should_exclude_dir(&name)) {
                    return None;
                }
                Some((name, is_dir))
            })
            .collect::<Vec<_>>();
        entries.sort_by(|left, right| {
            (!left.1)
                .cmp(&(!right.1))
                .then_with(|| left.0.to_lowercase().cmp(&right.0.to_lowercase()))
        });
        entries
    }
}

impl SuggestionProvider for DirectorySuggestionProvider {
    fn trigger(&self) -> &str {
        "@"
    }

    fn provide(&self, token: &CompletionToken) -> Vec<SuggestionItem> {
        let query = token.text.strip_prefix('@').unwrap_or(&token.text);
        let (dir_prefix, fragment) = split_dir_query(query);
        let list_dir = if dir_prefix.is_empty() {
            self.root_dir.clone()
        } else {
            self.root_dir.join(&dir_prefix)
        };

        self.list_entries(&list_dir)
            .into_iter()
            .filter_map(|(name, is_dir)| {
                let score = if fragment.is_empty() {
                    0.0
                } else {
                    fuzzy_match(fragment, &name)?
                };
                let rel_path = if dir_prefix.is_empty() {
                    name
                } else {
                    format!("{dir_prefix}/{name}")
                };
                let suffix = if is_dir { "/" } else { "" };
                Some(SuggestionItem {
                    id: format!("dir:{rel_path}"),
                    display_text: format!("{rel_path}{suffix}"),
                    completion: format!("@{rel_path}{suffix}"),
                    description: Some(if is_dir {
                        "directory".to_owned()
                    } else {
                        String::new()
                    }),
                    icon: Some("\u{25c7}".to_owned()),
                    source: "directory".to_owned(),
                    score,
                    arg_hint: None,
                })
            })
            .collect()
    }
}

fn collect_files(root: &Path, dir: &Path, files: &mut Vec<String>) {
    if files.len() >= MAX_INDEX_FILES {
        return;
    }
    let Ok(entries) = fs::read_dir(dir) else {
        return;
    };

    for entry in entries.filter_map(Result::ok) {
        if files.len() >= MAX_INDEX_FILES {
            return;
        }
        let path = entry.path();
        let Ok(file_type) = entry.file_type() else {
            continue;
        };
        if file_type.is_dir() {
            let name = entry.file_name().to_string_lossy().into_owned();
            if !should_exclude_dir(&name) {
                collect_files(root, &path, files);
            }
            continue;
        }
        if file_type.is_file() {
            if let Ok(relative) = path.strip_prefix(root) {
                files.push(path_to_slash(relative));
            }
        }
    }
}

pub(crate) fn should_exclude_dir(name: &str) -> bool {
    EXCLUDE_DIRS.contains(&name) || name.ends_with(".egg-info")
}

fn split_dir_query(query: &str) -> (String, &str) {
    if let Some(last_slash) = query.rfind('/') {
        (query[..last_slash].to_owned(), &query[last_slash + 1..])
    } else {
        (String::new(), query)
    }
}

pub(crate) fn path_to_slash(path: &Path) -> String {
    path.components()
        .map(|component| component.as_os_str().to_string_lossy())
        .collect::<Vec<_>>()
        .join("/")
}

pub(crate) fn absolute_path(path: &Path) -> PathBuf {
    if path.is_absolute() {
        path.to_path_buf()
    } else {
        std::env::current_dir()
            .map(|cwd| cwd.join(path))
            .unwrap_or_else(|_| path.to_path_buf())
    }
}
