use std::cell::RefCell;
use std::path::{Path, PathBuf};

use crate::private_fs::{ensure_private_dir, write_private_file};
use crate::push_queue_job::{
    current_time_seconds, deserialize_push_job, serialize_push_job, A2APushJob, PushQueueError,
};
use crate::push_secrets::A2APushSecretKeyring;

#[derive(Clone, Debug)]
pub struct LocalFileA2APushQueue {
    root: PathBuf,
    inflight_timeout_seconds: f64,
    secret_keyring: Option<RefCell<A2APushSecretKeyring>>,
}

impl LocalFileA2APushQueue {
    pub fn new(root: impl AsRef<Path>) -> Self {
        let queue = Self {
            root: root.as_ref().to_path_buf(),
            inflight_timeout_seconds: 300.0,
            secret_keyring: None,
        };
        queue
            .ensure_dirs()
            .expect("creating local push queue directories should not fail");
        queue
    }

    pub fn with_inflight_timeout_seconds(mut self, seconds: f64) -> Self {
        self.inflight_timeout_seconds = seconds;
        self
    }

    pub fn with_secret_keyring(mut self, secret_keyring: A2APushSecretKeyring) -> Self {
        self.secret_keyring = Some(RefCell::new(secret_keyring));
        self
    }

    pub fn enqueue(&mut self, job: A2APushJob) -> Result<(), PushQueueError> {
        self.write_job(
            &self.pending_dir().join(format!("{}.json", job.job_id)),
            &job,
        )
    }

    pub fn claim(&mut self, now: Option<f64>) -> Result<Option<A2APushJob>, PushQueueError> {
        let current = now.unwrap_or_else(current_time_seconds);
        self.recover_expired_inflight(current)?;

        for path in sorted_json_files(&self.pending_dir())? {
            let job = self.read_job(&path)?;
            if job.next_attempt_at > current {
                continue;
            }
            let target = self.inflight_dir().join(
                path.file_name()
                    .ok_or(PushQueueError::InvalidJob)?
                    .to_string_lossy()
                    .as_ref(),
            );
            let attempt = job.attempt;
            let last_error = job.last_error.clone();
            let leased = job.with_attempt(
                attempt,
                Some(current + self.inflight_timeout_seconds),
                last_error,
            );
            std::fs::rename(&path, &target)?;
            self.write_job(&target, &leased)?;
            return self.read_job(&target).map(Some);
        }

        Ok(None)
    }

    pub fn ack(&mut self, job_id: &str) -> Result<(), PushQueueError> {
        remove_file_if_exists(self.inflight_dir().join(format!("{job_id}.json")))
    }

    pub fn retry(&mut self, job: A2APushJob) -> Result<(), PushQueueError> {
        remove_file_if_exists(self.inflight_dir().join(format!("{}.json", job.job_id)))?;
        self.write_job(
            &self.pending_dir().join(format!("{}.json", job.job_id)),
            &job,
        )
    }

    pub fn dead_letter(&mut self, job: A2APushJob) -> Result<(), PushQueueError> {
        remove_file_if_exists(self.inflight_dir().join(format!("{}.json", job.job_id)))?;
        self.write_job(&self.dead_dir().join(format!("{}.json", job.job_id)), &job)
    }

    fn ensure_dirs(&self) -> Result<(), PushQueueError> {
        for path in [self.pending_dir(), self.inflight_dir(), self.dead_dir()] {
            ensure_private_dir(&path)?;
        }
        Ok(())
    }

    fn recover_expired_inflight(&self, now: f64) -> Result<(), PushQueueError> {
        for path in sorted_json_files(&self.inflight_dir())? {
            let job = self.read_job(&path)?;
            if job.next_attempt_at > now {
                continue;
            }
            let attempt = job.attempt;
            let recovered = job.with_attempt(attempt, Some(now), "Delivery lease expired.");
            let target = self.pending_dir().join(
                path.file_name()
                    .ok_or(PushQueueError::InvalidJob)?
                    .to_string_lossy()
                    .as_ref(),
            );
            std::fs::rename(&path, &target)?;
            self.write_job(&target, &recovered)?;
        }
        Ok(())
    }

    fn write_job(&self, path: &Path, job: &A2APushJob) -> Result<(), PushQueueError> {
        write_private_file(path, serialize_push_job(job, self.secret_keyring.as_ref())?)?;
        Ok(())
    }

    fn read_job(&self, path: &Path) -> Result<A2APushJob, PushQueueError> {
        deserialize_push_job(
            &std::fs::read_to_string(path)?,
            self.secret_keyring.as_ref(),
        )
    }

    fn pending_dir(&self) -> PathBuf {
        self.root.join("pending")
    }

    fn inflight_dir(&self) -> PathBuf {
        self.root.join("inflight")
    }

    fn dead_dir(&self) -> PathBuf {
        self.root.join("dead")
    }
}

fn sorted_json_files(dir: &Path) -> Result<Vec<PathBuf>, PushQueueError> {
    let mut paths = std::fs::read_dir(dir)?
        .filter_map(|entry| entry.ok().map(|entry| entry.path()))
        .filter(|path| path.extension().is_some_and(|ext| ext == "json"))
        .collect::<Vec<_>>();
    paths.sort();
    Ok(paths)
}

fn remove_file_if_exists(path: PathBuf) -> Result<(), PushQueueError> {
    match std::fs::remove_file(path) {
        Ok(()) => Ok(()),
        Err(error) if error.kind() == std::io::ErrorKind::NotFound => Ok(()),
        Err(error) => Err(PushQueueError::Io(error)),
    }
}
