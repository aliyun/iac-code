use std::collections::{BTreeMap, BTreeSet};

mod listing;
mod model;

pub use crate::types::TaskStoreError;
use crate::types::{validate_protocol_id, A2AContextRecord, A2ATaskRecord};
pub use model::{Artifact, ListTasksRequest, ListTasksResponse, SdkTask};

#[derive(Clone, Debug)]
pub struct A2ATaskStore {
    sdk_tasks: BTreeMap<String, BTreeMap<String, SdkTask>>,
    sdk_tasks_by_context: BTreeMap<String, BTreeMap<String, BTreeSet<String>>>,
    tasks: BTreeMap<String, A2ATaskRecord>,
    contexts: BTreeMap<String, A2AContextRecord>,
    expired_task_tombstones: BTreeMap<String, f64>,
    idle_timeout_seconds: f64,
    cleanup_interval_seconds: f64,
    next_session_number: u64,
    next_task_number: u64,
    now: f64,
}

impl Default for A2ATaskStore {
    fn default() -> Self {
        Self::new()
    }
}

impl A2ATaskStore {
    pub fn new() -> Self {
        Self {
            sdk_tasks: BTreeMap::new(),
            sdk_tasks_by_context: BTreeMap::new(),
            tasks: BTreeMap::new(),
            contexts: BTreeMap::new(),
            expired_task_tombstones: BTreeMap::new(),
            idle_timeout_seconds: 3600.0,
            cleanup_interval_seconds: 300.0,
            next_session_number: 1,
            next_task_number: 1,
            now: 0.0,
        }
    }

    pub fn with_idle_timeout_seconds(mut self, seconds: f64) -> Self {
        self.idle_timeout_seconds = seconds;
        self
    }

    pub fn with_cleanup_interval_seconds(mut self, seconds: f64) -> Self {
        self.cleanup_interval_seconds = seconds;
        self
    }

    pub fn get_or_create_context(
        &mut self,
        context_id: &str,
        cwd: &str,
    ) -> Result<&A2AContextRecord, TaskStoreError> {
        let context_id = validate_protocol_id(context_id)?;
        if self.contexts.contains_key(&context_id) {
            let record = self.contexts.get_mut(&context_id).expect("context exists");
            if record.expired {
                return Err(TaskStoreError::InvalidState(
                    "A2A context expired".to_owned(),
                ));
            }
            if record.cwd != cwd {
                return Err(TaskStoreError::InvalidState(
                    "A2A context belongs to a different workspace".to_owned(),
                ));
            }
            record.touch(self.now);
            return Ok(self.contexts.get(&context_id).expect("context exists"));
        }

        let session_id = self.next_session_id();
        self.contexts.insert(
            context_id.clone(),
            A2AContextRecord::new(context_id.clone(), session_id, cwd.to_owned(), self.now),
        );
        Ok(self.contexts.get(&context_id).expect("inserted context"))
    }

    pub fn get_or_create_task(
        &mut self,
        task_id: Option<&str>,
        context_id: &str,
    ) -> Result<&A2ATaskRecord, TaskStoreError> {
        let context_id = validate_protocol_id(context_id)?;
        let task_id = match task_id {
            Some(task_id) => validate_protocol_id(task_id)?,
            None => self.next_task_id(),
        };

        if self.expired_task_tombstones.contains_key(&task_id) {
            return Err(TaskStoreError::InvalidState("A2A task expired".to_owned()));
        }

        if self.tasks.contains_key(&task_id) {
            let record = self.tasks.get_mut(&task_id).expect("task exists");
            if record.context_id != context_id {
                return Err(TaskStoreError::InvalidState(
                    "Task belongs to a different context".to_owned(),
                ));
            }
            record.touch(self.now);
            return Ok(self.tasks.get(&task_id).expect("task exists"));
        }

        self.tasks.insert(
            task_id.clone(),
            A2ATaskRecord::new(task_id.clone(), context_id, self.now),
        );
        Ok(self.tasks.get(&task_id).expect("inserted task"))
    }

    pub fn ensure_task_not_expired(&self, task_id: &str) -> Result<(), TaskStoreError> {
        let task_id = validate_protocol_id(task_id)?;
        if self.expired_task_tombstones.contains_key(&task_id) {
            return Err(TaskStoreError::InvalidState("A2A task expired".to_owned()));
        }
        Ok(())
    }

    pub fn set_task_active(&mut self, task_id: &str, active: bool) -> Result<(), TaskStoreError> {
        let task_id = validate_protocol_id(task_id)?;
        let Some(record) = self.tasks.get_mut(&task_id) else {
            return Err(TaskStoreError::InvalidState(
                "A2A task not found".to_owned(),
            ));
        };
        record.active = active;
        Ok(())
    }

    pub fn cancel_task(&mut self, task_id: &str) -> bool {
        let Ok(task_id) = validate_protocol_id(task_id) else {
            return false;
        };
        let Some(record) = self.tasks.get_mut(&task_id) else {
            return false;
        };
        if !record.active {
            return false;
        }
        record.active = false;
        true
    }

    pub fn is_task_active(&self, task_id: &str) -> bool {
        let Ok(task_id) = validate_protocol_id(task_id) else {
            return false;
        };
        self.tasks.get(&task_id).is_some_and(|record| record.active)
    }

    pub fn save_sdk_task(&mut self, task: SdkTask, owner: &str) {
        let owner = owner.to_owned();
        let task_id = task.id.clone();
        let context_id = task.context_id.clone();
        let existing = self
            .sdk_tasks
            .entry(owner.clone())
            .or_default()
            .insert(task_id.clone(), task);
        if let Some(existing) = existing {
            self.remove_sdk_task_from_index(&owner, &task_id, &existing.context_id);
        }
        self.sdk_tasks_by_context
            .entry(owner)
            .or_default()
            .entry(context_id)
            .or_default()
            .insert(task_id);
    }

    pub fn get_sdk_task(&self, task_id: &str, owner: &str) -> Option<SdkTask> {
        let task_id = validate_protocol_id(task_id).ok()?;
        self.sdk_tasks
            .get(owner)
            .and_then(|tasks| tasks.get(&task_id))
            .cloned()
    }

    pub fn delete_task(&mut self, task_id: &str, owner: &str) -> Result<(), TaskStoreError> {
        let task_id = validate_protocol_id(task_id)?;
        let mut removed_context_id = None;
        let mut owner_is_empty = false;
        if let Some(owner_tasks) = self.sdk_tasks.get_mut(owner) {
            removed_context_id = owner_tasks
                .remove(&task_id)
                .map(|existing| existing.context_id);
            owner_is_empty = owner_tasks.is_empty();
        }
        if let Some(context_id) = removed_context_id {
            self.remove_sdk_task_from_index(owner, &task_id, &context_id);
        }
        if owner_is_empty {
            self.sdk_tasks.remove(owner);
        }
        self.tasks.remove(&task_id);
        self.expired_task_tombstones.remove(&task_id);
        Ok(())
    }

    pub fn cleanup_once(&mut self, now_offset_seconds: f64) {
        let now = self.now + now_offset_seconds;
        let expired_context_ids = self
            .contexts
            .iter()
            .filter(|(_, context)| {
                context.active_task_id.is_none()
                    && now - context.last_active > self.idle_timeout_seconds
            })
            .map(|(context_id, _)| context_id.clone())
            .collect::<Vec<_>>();

        for context_id in expired_context_ids {
            self.contexts.remove(&context_id);
            for task in self.tasks.values_mut() {
                if task.context_id == context_id {
                    task.expired = true;
                    self.expired_task_tombstones
                        .insert(task.task_id.clone(), now);
                }
            }
        }

        let expired_task_ids = self
            .expired_task_tombstones
            .iter()
            .filter(|(_, expired_at)| now - **expired_at > self.cleanup_interval_seconds)
            .map(|(task_id, _)| task_id.clone())
            .collect::<Vec<_>>();

        for task_id in expired_task_ids {
            self.expired_task_tombstones.remove(&task_id);
            self.tasks.remove(&task_id);
            self.remove_sdk_task_from_all_owners(&task_id);
        }
    }

    fn next_session_id(&mut self) -> String {
        let id = format!("session-{}", self.next_session_number);
        self.next_session_number += 1;
        id
    }

    fn next_task_id(&mut self) -> String {
        let id = format!("task-{}", self.next_task_number);
        self.next_task_number += 1;
        id
    }

    fn remove_sdk_task_from_all_owners(&mut self, task_id: &str) {
        let owners = self.sdk_tasks.keys().cloned().collect::<Vec<_>>();
        for owner in owners {
            let existing = self
                .sdk_tasks
                .get_mut(&owner)
                .and_then(|tasks| tasks.remove(task_id));
            if let Some(existing) = existing {
                self.remove_sdk_task_from_index(&owner, task_id, &existing.context_id);
            }
            if self.sdk_tasks.get(&owner).is_some_and(BTreeMap::is_empty) {
                self.sdk_tasks.remove(&owner);
            }
        }
    }

    fn remove_sdk_task_from_index(&mut self, owner: &str, task_id: &str, context_id: &str) {
        let Some(owner_contexts) = self.sdk_tasks_by_context.get_mut(owner) else {
            return;
        };
        let Some(task_ids) = owner_contexts.get_mut(context_id) else {
            return;
        };
        task_ids.remove(task_id);
        if task_ids.is_empty() {
            owner_contexts.remove(context_id);
        }
        if owner_contexts.is_empty() {
            self.sdk_tasks_by_context.remove(owner);
        }
    }
}
