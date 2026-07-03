use std::fs;
use std::path::{Path, PathBuf};

use crate::path_provider::{absolute_path, path_to_slash, should_exclude_dir};

#[derive(Clone, Debug, Eq, PartialEq)]
pub struct QuickOpenItem {
    pub key: String,
    pub display: String,
    pub file_path: PathBuf,
    pub filter_text: String,
}

impl QuickOpenItem {
    pub fn new(display: impl Into<String>, file_path: impl Into<PathBuf>) -> Self {
        let display = display.into();
        Self {
            key: format!("file:{display}"),
            filter_text: display.clone(),
            display,
            file_path: file_path.into(),
        }
    }

    pub fn selection_insert_text(&self) -> String {
        format!("@{}", self.display)
    }
}

#[derive(Clone, Debug, Eq, PartialEq)]
pub struct QuickOpenPreview {
    pub title: String,
    pub language: String,
    pub content: String,
}

pub fn build_quick_open_items(root_dir: impl AsRef<Path>) -> Vec<QuickOpenItem> {
    let root_dir = absolute_path(root_dir.as_ref());
    let mut items = Vec::new();
    collect_quick_open_items(&root_dir, &root_dir, &mut items);
    items
}

pub fn build_quick_open_preview(item: &QuickOpenItem) -> QuickOpenPreview {
    QuickOpenPreview {
        title: item.display.clone(),
        language: item
            .file_path
            .extension()
            .and_then(|extension| extension.to_str())
            .filter(|extension| !extension.is_empty())
            .unwrap_or("text")
            .to_owned(),
        content: read_first_lines(&item.file_path, 20),
    }
}

fn collect_quick_open_items(root: &Path, dir: &Path, items: &mut Vec<QuickOpenItem>) {
    let Ok(entries) = fs::read_dir(dir) else {
        return;
    };

    for entry in entries.filter_map(Result::ok) {
        let path = entry.path();
        let Ok(file_type) = entry.file_type() else {
            continue;
        };
        if file_type.is_dir() {
            let name = entry.file_name().to_string_lossy().into_owned();
            if !should_exclude_dir(&name) {
                collect_quick_open_items(root, &path, items);
            }
            continue;
        }
        if file_type.is_file() {
            if let Ok(relative) = path.strip_prefix(root) {
                items.push(QuickOpenItem::new(path_to_slash(relative), path));
            }
        }
    }
}

fn read_first_lines(file_path: &Path, max_lines: usize) -> String {
    let Ok(bytes) = fs::read(file_path) else {
        return String::new();
    };
    let content = String::from_utf8_lossy(&bytes);
    content.split_inclusive('\n').take(max_lines).collect()
}
