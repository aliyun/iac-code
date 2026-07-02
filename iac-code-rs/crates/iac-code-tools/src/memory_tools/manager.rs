use std::fs;
use std::io;
use std::path::{Path, PathBuf};

use super::model::Memory;
use super::parse::parse_memory_file;
use super::private_fs::{ensure_private_dir, ensure_private_file, is_symlink, path_name};
use super::validate::validate_name;
use super::{INDEX_FILE, MAX_INDEX_LINES, MEMORY_TYPES};

#[derive(Clone, Debug)]
pub struct MemoryManager {
    memory_dir: PathBuf,
    memory_root: PathBuf,
}

impl MemoryManager {
    pub fn new(memory_dir: impl Into<PathBuf>) -> io::Result<Self> {
        let memory_dir = memory_dir.into();
        ensure_private_dir(&memory_dir)?;
        let memory_root = memory_dir.canonicalize()?;
        Ok(Self {
            memory_dir,
            memory_root,
        })
    }

    pub fn save(
        &self,
        name: &str,
        content: &str,
        memory_type: &str,
        description: &str,
    ) -> Result<(), String> {
        if !MEMORY_TYPES.contains(&memory_type) {
            return Err(format!("Invalid memory type: {memory_type}"));
        }

        let path = self.memory_path(name)?;
        let index_path = self.index_path();
        self.ensure_writable_path(&path)?;
        self.ensure_writable_path(&index_path)?;

        let file_content =
            format!("---\nname: {name}\ndescription: {description}\ntype: {memory_type}\n---\n\n{content}\n");
        fs::write(&path, file_content).map_err(|error| error.to_string())?;
        ensure_private_file(&path).map_err(|error| error.to_string())?;
        self.update_index()
    }

    pub fn load(&self, name: &str) -> Result<Option<Memory>, String> {
        let path = self.memory_path(name)?;
        let Some(safe_path) = self.safe_existing_file(&path) else {
            return Ok(None);
        };
        self.load_memory_file(&safe_path).map(Some)
    }

    pub fn delete(&self, name: &str) -> Result<(), String> {
        let path = self.memory_path(name)?;
        let index_path = self.index_path();
        self.ensure_writable_path(&index_path)?;
        if is_symlink(&path) {
            return Err(format!("Invalid memory path: {}", path_name(&path)));
        }
        if path.exists() {
            self.ensure_writable_path(&path)?;
            fs::remove_file(&path).map_err(|error| error.to_string())?;
        }
        self.update_index()
    }

    pub fn list_memories(&self) -> Result<Vec<Memory>, String> {
        let mut paths = self.iter_memory_files()?;
        paths.sort_by(|left, right| left.file_name().cmp(&right.file_name()));
        paths
            .into_iter()
            .map(|path| self.load_memory_file(&path))
            .collect()
    }

    pub fn search(&self, query: &str) -> Result<Vec<Memory>, String> {
        let needle = query.trim().to_ascii_lowercase();
        if needle.is_empty() {
            return Ok(Vec::new());
        }
        Ok(self
            .list_memories()?
            .into_iter()
            .filter(|memory| {
                [
                    memory.name.as_str(),
                    memory.description.as_str(),
                    memory.memory_type.as_str(),
                    memory.content.as_str(),
                ]
                .join("\n")
                .to_ascii_lowercase()
                .contains(&needle)
            })
            .collect())
    }

    pub fn get_index_content(&self) -> String {
        let path = self.index_path();
        let Some(safe_path) = self.safe_existing_file(&path) else {
            return String::new();
        };
        fs::read_to_string(safe_path).unwrap_or_default()
    }

    pub fn get_prompt_content(&self) -> String {
        let mut paths = match self.iter_memory_files() {
            Ok(paths) => paths,
            Err(_) => return String::new(),
        };
        paths.sort_by(|left, right| left.file_name().cmp(&right.file_name()));
        paths
            .into_iter()
            .filter_map(|path| self.load_memory_file(&path).ok())
            .map(|memory| format!("[{}] {}", memory.memory_type, memory.content))
            .collect::<Vec<_>>()
            .join("\n\n")
    }

    fn update_index(&self) -> Result<(), String> {
        let mut entries = Vec::new();
        let mut paths = self.iter_memory_files()?;
        paths.sort_by(|left, right| left.file_name().cmp(&right.file_name()));

        for path in paths {
            let memory = self.load_memory_file(&path)?;
            let stem = path
                .file_stem()
                .map(|value| value.to_string_lossy().into_owned())
                .unwrap_or_default();
            let file_name = path
                .file_name()
                .map(|value| value.to_string_lossy().into_owned())
                .unwrap_or_default();
            entries.push(format!("- [{stem}]({file_name}) — {}", memory.description));
        }

        let mut content = entries
            .into_iter()
            .take(MAX_INDEX_LINES)
            .collect::<Vec<_>>()
            .join("\n");
        content.push('\n');
        let index_path = self.index_path();
        self.ensure_writable_path(&index_path)?;
        fs::write(&index_path, content).map_err(|error| error.to_string())?;
        ensure_private_file(&index_path).map_err(|error| error.to_string())
    }

    fn iter_memory_files(&self) -> Result<Vec<PathBuf>, String> {
        let mut paths = Vec::new();
        let entries = match fs::read_dir(&self.memory_dir) {
            Ok(entries) => entries,
            Err(error) => return Err(error.to_string()),
        };

        for entry in entries {
            let path = entry.map_err(|error| error.to_string())?.path();
            if path.extension().and_then(|value| value.to_str()) != Some("md") {
                continue;
            }
            if path
                .file_name()
                .and_then(|value| value.to_str())
                .is_some_and(|name| name.eq_ignore_ascii_case(INDEX_FILE))
            {
                continue;
            }
            if let Some(safe_path) = self.safe_existing_file(&path) {
                paths.push(safe_path);
            }
        }
        Ok(paths)
    }

    fn load_memory_file(&self, path: &Path) -> Result<Memory, String> {
        let text = fs::read_to_string(path).map_err(|error| error.to_string())?;
        Ok(parse_memory_file(&text))
    }

    fn memory_path(&self, name: &str) -> Result<PathBuf, String> {
        Ok(self.memory_dir.join(format!("{}.md", validate_name(name)?)))
    }

    fn index_path(&self) -> PathBuf {
        self.memory_dir.join(INDEX_FILE)
    }

    fn safe_existing_file(&self, path: &Path) -> Option<PathBuf> {
        if is_symlink(path) {
            return None;
        }
        let resolved = path.canonicalize().ok()?;
        if !resolved.starts_with(&self.memory_root) || !path.is_file() {
            return None;
        }
        Some(path.to_path_buf())
    }

    fn ensure_writable_path(&self, path: &Path) -> Result<(), String> {
        if is_symlink(path) {
            return Err(format!("Invalid memory path: {}", path_name(path)));
        }
        let parent = path
            .parent()
            .ok_or_else(|| format!("Invalid memory path: {}", path_name(path)))?
            .canonicalize()
            .map_err(|_| format!("Invalid memory path: {}", path_name(path)))?;
        if parent != self.memory_root {
            return Err(format!("Invalid memory path: {}", path_name(path)));
        }
        if !path.exists() {
            return Ok(());
        }
        let resolved = path
            .canonicalize()
            .map_err(|_| format!("Invalid memory path: {}", path_name(path)))?;
        if !resolved.starts_with(&self.memory_root) || !path.is_file() {
            return Err(format!("Invalid memory path: {}", path_name(path)));
        }
        Ok(())
    }
}
