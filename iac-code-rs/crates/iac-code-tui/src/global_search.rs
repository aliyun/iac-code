use std::collections::HashSet;
use std::fs;
use std::path::{Path, PathBuf};

#[derive(Clone, Debug, Eq, PartialEq)]
pub struct GlobalSearchItem {
    pub key: String,
    pub display: String,
    pub file_path: PathBuf,
    pub line_number: usize,
    pub text: String,
    pub filter_text: String,
}

impl GlobalSearchItem {
    pub fn new(
        file_path: impl Into<PathBuf>,
        root_dir: impl AsRef<Path>,
        line_number: usize,
        text: impl Into<String>,
    ) -> Self {
        let file_path = file_path.into();
        let text = text.into();
        let rel_path = relative_path_display(root_dir.as_ref(), &file_path);
        let display = format!("{rel_path}:{line_number}  {}", text.trim());
        Self {
            key: format!("{}:{line_number}", file_path.display()),
            display: display.clone(),
            file_path,
            line_number,
            text,
            filter_text: display,
        }
    }

    pub fn selection_insert_text(&self) -> String {
        format!("@{}", self.display)
    }
}

#[derive(Clone, Debug, Eq, PartialEq)]
pub struct GlobalSearchPreview {
    pub title: String,
    pub language: String,
    pub start_line: usize,
    pub highlight_line: usize,
    pub content: String,
}

pub fn parse_global_search_results(
    root_dir: impl AsRef<Path>,
    output: &str,
) -> Vec<GlobalSearchItem> {
    let root_dir = root_dir.as_ref();
    let mut items = Vec::new();
    let mut seen = HashSet::new();

    for line in output.lines() {
        let Some((file_path, line_number, matched_text)) = parse_search_line(line) else {
            continue;
        };
        let item = GlobalSearchItem::new(file_path, root_dir, line_number, matched_text);
        if seen.insert(item.key.clone()) {
            items.push(item);
        }
    }

    items
}

pub fn build_global_search_preview(
    root_dir: impl AsRef<Path>,
    item: &GlobalSearchItem,
) -> GlobalSearchPreview {
    let start_line = item.line_number.saturating_sub(5).max(1);
    let end_line = item.line_number.saturating_add(5);
    let content = read_preview_window(&item.file_path, start_line, end_line);
    GlobalSearchPreview {
        title: format!(
            "{}:{}",
            relative_path_display(root_dir.as_ref(), &item.file_path),
            item.line_number
        ),
        language: item
            .file_path
            .extension()
            .and_then(|extension| extension.to_str())
            .filter(|extension| !extension.is_empty())
            .unwrap_or("text")
            .to_owned(),
        start_line,
        highlight_line: item.line_number,
        content,
    }
}

fn parse_search_line(line: &str) -> Option<(&str, usize, &str)> {
    for (colon_index, _) in line.match_indices(':') {
        let rest = &line[colon_index + 1..];
        let Some(next_colon_index) = rest.find(':') else {
            continue;
        };
        let line_number = &rest[..next_colon_index];
        if line_number.is_empty() || !line_number.chars().all(|ch| ch.is_ascii_digit()) {
            continue;
        }
        let file_path = &line[..colon_index];
        if file_path.is_empty() {
            return None;
        }
        let matched_text = &rest[next_colon_index + 1..];
        return Some((file_path, line_number.parse().ok()?, matched_text));
    }
    None
}

fn read_preview_window(file_path: &Path, start_line: usize, end_line: usize) -> String {
    let Ok(bytes) = fs::read(file_path) else {
        return String::new();
    };
    let content = String::from_utf8_lossy(&bytes);
    content
        .split_inclusive('\n')
        .skip(start_line.saturating_sub(1))
        .take(end_line.saturating_sub(start_line).saturating_add(1))
        .collect()
}

fn relative_path_display(root_dir: &Path, file_path: &Path) -> String {
    file_path
        .strip_prefix(root_dir)
        .unwrap_or(file_path)
        .display()
        .to_string()
}
