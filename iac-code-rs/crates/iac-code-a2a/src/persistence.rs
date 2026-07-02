use std::fs;
use std::path::{Path, PathBuf};

use crate::types::{validate_protocol_id, TaskStoreError};

mod json_io;
mod model;

use json_io::{read_json, write_json};
use model::{
    context_from_json, context_to_json, current_time_seconds, routes_from_json, routes_to_json,
    task_from_json, task_to_json,
};
pub use model::{A2AContextSnapshot, A2ARouteSnapshot, A2ATaskSnapshot};

const INTERRUPTED_RESTORE_STATES: &[&str] = &["submitted", "working", "auth-required"];
const INTERRUPTED_STATUS_MESSAGE: &str =
    "Task was interrupted by process exit and cannot be revived automatically.";

#[derive(Clone, Debug)]
pub struct A2APersistenceStore {
    root: PathBuf,
    tasks_dir: PathBuf,
    contexts_dir: PathBuf,
    routes_path: PathBuf,
}

impl A2APersistenceStore {
    pub fn new(root: impl AsRef<Path>) -> Self {
        let root = root.as_ref().to_path_buf();
        Self {
            tasks_dir: root.join("tasks"),
            contexts_dir: root.join("contexts"),
            routes_path: root.join("routes.json"),
            root,
        }
    }

    pub fn save_task(&self, snapshot: A2ATaskSnapshot) -> Result<(), TaskStoreError> {
        let task_id = validate_protocol_id(&snapshot.task_id)?;
        fs::create_dir_all(&self.tasks_dir).map_err(io_error)?;
        write_json(
            &self.tasks_dir.join(format!("{task_id}.json")),
            &task_to_json(&snapshot),
        )
    }

    pub fn load_task(&self, task_id: &str) -> Result<Option<A2ATaskSnapshot>, TaskStoreError> {
        let task_id = validate_protocol_id(task_id)?;
        let Some(data) = read_json(&self.tasks_dir.join(format!("{task_id}.json"))) else {
            return Ok(None);
        };
        Ok(task_from_json(&data))
    }

    pub fn restore_task(&self, task_id: &str) -> Result<Option<A2ATaskSnapshot>, TaskStoreError> {
        let Some(snapshot) = self.load_task(task_id)? else {
            return Ok(None);
        };
        if INTERRUPTED_RESTORE_STATES.contains(&snapshot.state.as_str()) {
            let interrupted = A2ATaskSnapshot {
                task_id: snapshot.task_id,
                context_id: snapshot.context_id,
                state: "interrupted".to_owned(),
                output_text: snapshot.output_text,
                status_message: INTERRUPTED_STATUS_MESSAGE.to_owned(),
                updated_at: current_time_seconds(),
            };
            self.save_task(interrupted.clone())?;
            return Ok(Some(interrupted));
        }
        Ok(Some(snapshot))
    }

    pub fn list_tasks(&self) -> Result<Vec<A2ATaskSnapshot>, TaskStoreError> {
        let Ok(entries) = fs::read_dir(&self.tasks_dir) else {
            return Ok(Vec::new());
        };
        let mut paths = Vec::new();
        for entry in entries {
            let entry = entry.map_err(io_error)?;
            let path = entry.path();
            if path
                .extension()
                .is_some_and(|extension| extension == "json")
            {
                paths.push(path);
            }
        }
        paths.sort();

        let mut snapshots = Vec::new();
        for path in paths {
            let Some(data) = read_json(&path) else {
                continue;
            };
            if let Some(snapshot) = task_from_json(&data) {
                snapshots.push(snapshot);
            }
        }
        Ok(snapshots)
    }

    pub fn save_context(&self, snapshot: A2AContextSnapshot) -> Result<(), TaskStoreError> {
        let context_id = validate_protocol_id(&snapshot.context_id)?;
        fs::create_dir_all(&self.contexts_dir).map_err(io_error)?;
        write_json(
            &self.contexts_dir.join(format!("{context_id}.json")),
            &context_to_json(&snapshot),
        )
    }

    pub fn load_context(
        &self,
        context_id: &str,
    ) -> Result<Option<A2AContextSnapshot>, TaskStoreError> {
        let context_id = validate_protocol_id(context_id)?;
        let Some(data) = read_json(&self.contexts_dir.join(format!("{context_id}.json"))) else {
            return Ok(None);
        };
        Ok(context_from_json(&data))
    }

    pub fn save_routes(&self, routes: Vec<A2ARouteSnapshot>) -> Result<(), TaskStoreError> {
        fs::create_dir_all(&self.root).map_err(io_error)?;
        write_json(&self.routes_path, &routes_to_json(&routes))
    }

    pub fn load_routes(&self) -> Result<Vec<A2ARouteSnapshot>, TaskStoreError> {
        Ok(read_json(&self.routes_path)
            .as_ref()
            .map_or_else(Vec::new, routes_from_json))
    }
}

fn io_error(error: std::io::Error) -> TaskStoreError {
    TaskStoreError::InvalidState(error.to_string())
}
