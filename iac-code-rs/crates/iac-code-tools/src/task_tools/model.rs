use std::sync::atomic::{AtomicU64, Ordering};
use std::sync::{Arc, Mutex};

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum TaskStatus {
    Running,
    Completed,
    Failed,
    Stopped,
}

impl TaskStatus {
    pub fn as_str(self) -> &'static str {
        match self {
            TaskStatus::Running => "running",
            TaskStatus::Completed => "completed",
            TaskStatus::Failed => "failed",
            TaskStatus::Stopped => "stopped",
        }
    }
}

#[derive(Clone, Debug, PartialEq, Eq)]
pub struct TaskInfo {
    pub id: String,
    pub description: String,
    pub agent_type: String,
    pub status: TaskStatus,
    pub result: Option<String>,
    pub error: Option<String>,
    pub tool_use_count: u32,
    pub token_count: u32,
}

#[derive(Clone, Debug, Default)]
pub struct TaskManager {
    tasks: Arc<Mutex<Vec<TaskInfo>>>,
}

impl TaskManager {
    pub fn new() -> Self {
        Self::default()
    }

    pub fn register(&self, description: &str, agent_type: &str) -> String {
        static NEXT_ID: AtomicU64 = AtomicU64::new(1);
        let task_id = format!("{:08x}", NEXT_ID.fetch_add(1, Ordering::SeqCst));
        self.tasks.lock().expect("task lock").push(TaskInfo {
            id: task_id.clone(),
            description: description.to_owned(),
            agent_type: agent_type.to_owned(),
            status: TaskStatus::Running,
            result: None,
            error: None,
            tool_use_count: 0,
            token_count: 0,
        });
        task_id
    }

    pub fn get(&self, task_id: &str) -> Option<TaskInfo> {
        self.tasks
            .lock()
            .expect("task lock")
            .iter()
            .find(|task| task.id == task_id)
            .cloned()
    }

    pub fn complete(&self, task_id: &str, result: &str) {
        self.update_task(task_id, |task| {
            if task.status != TaskStatus::Stopped {
                task.status = TaskStatus::Completed;
                task.result = Some(result.to_owned());
            }
        });
    }

    pub fn fail(&self, task_id: &str, error: &str) {
        self.update_task(task_id, |task| {
            if task.status != TaskStatus::Stopped {
                task.status = TaskStatus::Failed;
                task.error = Some(error.to_owned());
            }
        });
    }

    pub fn stop(&self, task_id: &str) -> bool {
        let mut tasks = self.tasks.lock().expect("task lock");
        let Some(task) = tasks.iter_mut().find(|task| task.id == task_id) else {
            return false;
        };
        if task.status == TaskStatus::Running {
            task.status = TaskStatus::Stopped;
            true
        } else {
            false
        }
    }

    pub fn update_progress(&self, task_id: &str, tool_use_count: u32, token_count: u32) {
        self.update_task(task_id, |task| {
            task.tool_use_count = tool_use_count;
            task.token_count = token_count;
        });
    }

    pub fn list_all(&self) -> Vec<TaskInfo> {
        self.tasks.lock().expect("task lock").clone()
    }

    fn update_task(&self, task_id: &str, update: impl FnOnce(&mut TaskInfo)) {
        if let Some(task) = self
            .tasks
            .lock()
            .expect("task lock")
            .iter_mut()
            .find(|task| task.id == task_id)
        {
            update(task);
        }
    }
}
