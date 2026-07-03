use std::collections::BTreeMap;
use std::fs;
use std::io;
use std::path::{Path, PathBuf};
use std::time::{SystemTime, UNIX_EPOCH};

#[derive(Clone, Debug, PartialEq, Eq)]
pub struct AutoDetectCache {
    path: PathBuf,
    data: BTreeMap<String, BTreeMap<String, bool>>,
    dirty: bool,
}

impl AutoDetectCache {
    pub fn new(path: impl Into<PathBuf>) -> Self {
        let path = path.into();
        Self {
            data: load_auto_detect_cache(&path),
            path,
            dirty: false,
        }
    }

    pub fn get(&self, base_url: &str, model: &str) -> Option<bool> {
        self.data
            .get(base_url)
            .and_then(|models| models.get(model))
            .copied()
    }

    pub fn set(&mut self, base_url: &str, model: &str, value: bool) {
        self.data
            .entry(base_url.to_owned())
            .or_default()
            .insert(model.to_owned(), value);
        self.dirty = true;
    }

    pub fn flush(&mut self) -> io::Result<()> {
        if !self.dirty {
            return Ok(());
        }

        if let Some(parent) = self.path.parent() {
            fs::create_dir_all(parent)?;
        }
        let content = serde_yaml::to_string(&self.data).map_err(io::Error::other)?;
        let tmp_path = self.temporary_path();
        if let Err(error) =
            fs::write(&tmp_path, content).and_then(|_| fs::rename(&tmp_path, &self.path))
        {
            let _ = fs::remove_file(&tmp_path);
            return Err(error);
        }
        self.dirty = false;
        Ok(())
    }

    fn temporary_path(&self) -> PathBuf {
        let nonce = SystemTime::now()
            .duration_since(UNIX_EPOCH)
            .map(|duration| duration.as_nanos())
            .unwrap_or(0);
        self.path.with_file_name(format!(
            ".multimodal-cache.{}.{nonce}.tmp",
            std::process::id()
        ))
    }
}

fn load_auto_detect_cache(path: &Path) -> BTreeMap<String, BTreeMap<String, bool>> {
    let Ok(content) = fs::read_to_string(path) else {
        return BTreeMap::new();
    };
    serde_yaml::from_str(&content).unwrap_or_default()
}
