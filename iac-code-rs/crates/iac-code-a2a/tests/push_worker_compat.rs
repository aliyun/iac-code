use std::collections::BTreeMap;
use std::sync::{Arc, Mutex};

use iac_code_a2a::push::InvalidPushNotificationConfigError;
use iac_code_a2a::push_queue::{
    A2APushJob, A2APushRetryPolicy, LocalFileA2APushQueue, PushQueueError,
};
use iac_code_a2a::push_worker::{
    pinned_callback_request, validate_resolved_callback_addresses, A2APushCallbackConnector,
    A2APushCallbackResponse, A2APushDeliveryError, A2APushDeliveryWorker, A2APushQueueBackend,
};
use iac_code_protocol::json::{self, JsonValue};

#[test]
fn push_worker_uses_injected_connector_for_delivery_like_python() {
    let root = temp_root("delivery");
    let mut queue = LocalFileA2APushQueue::new(&root);
    queue
        .enqueue(push_job(
            "job-1",
            json::object([("ok", json::bool_value(true))]),
        ))
        .expect("enqueue");
    let connector = RecordingConnector::new(204);
    let posts = connector.posts.clone();
    let mut worker = A2APushDeliveryWorker::new(queue, connector);

    assert!(worker.run_once().expect("run"));

    let posts = posts.lock().expect("posts");
    assert_eq!(posts.len(), 1);
    assert_eq!(posts[0].url, "https://callback.example/a2a");
    assert_eq!(posts[0].timeout, 5.0);
    assert!(!root.join("inflight").join("job-1.json").exists());
}

#[test]
fn push_worker_rejects_invalid_queued_url_before_injected_connector_delivery() {
    let root = temp_root("invalid-url");
    let mut queue = LocalFileA2APushQueue::new(&root);
    queue
        .enqueue(A2APushJob::new(
            "job-1",
            "task-1",
            "cfg-1",
            "http://localhost/a2a",
            json::object([("ok", json::bool_value(true))]),
        ))
        .expect("enqueue");
    let connector = RecordingConnector::new(204);
    let posts = connector.posts.clone();
    let mut worker = A2APushDeliveryWorker::new(queue, connector);

    assert!(!worker.run_once().expect("run"));

    assert!(posts.lock().expect("posts").is_empty());
    assert!(root.join("dead").join("job-1.json").exists());
}

#[test]
fn push_worker_does_not_retry_or_dead_letter_when_ack_fails_after_success_like_python() {
    let queue = AckFailingQueue::new(push_job(
        "job-1",
        json::object([("ok", json::bool_value(true))]),
    ));
    let connector = RecordingConnector::new(204);
    let posts = connector.posts.clone();
    let mut worker = A2APushDeliveryWorker::new(queue, connector);

    assert!(!worker.run_once().expect("run"));

    assert_eq!(posts.lock().expect("posts").len(), 1);
    let queue = worker.queue();
    assert_eq!(queue.acked, vec!["job-1"]);
    assert!(queue.retried.is_empty());
    assert!(queue.dead.is_empty());
}

#[test]
fn push_worker_retries_transient_failure_with_backoff_like_python() {
    let root = temp_root("retry");
    let mut queue = LocalFileA2APushQueue::new(&root);
    queue
        .enqueue(push_job(
            "job-1",
            json::object([("ok", json::bool_value(true))]),
        ))
        .expect("enqueue");
    let connector = RecordingConnector::new(503);
    let mut worker = A2APushDeliveryWorker::new(queue, connector)
        .with_retry_policy(A2APushRetryPolicy {
            initial_delay_seconds: 2.0,
            max_delay_seconds: 10.0,
            jitter_ratio: 0.0,
            max_attempts: 5,
        })
        .with_clock(|| 100.0);

    assert!(!worker.run_once().expect("run"));
    assert!(worker
        .queue_mut()
        .claim(Some(101.0))
        .expect("claim")
        .is_none());
    let retried = worker
        .queue_mut()
        .claim(Some(102.0))
        .expect("claim")
        .expect("retried job");
    assert_eq!(retried.attempt, 1);
}

#[test]
fn push_worker_dead_letters_permanent_failure_like_python() {
    let root = temp_root("dead");
    let mut queue = LocalFileA2APushQueue::new(&root);
    queue
        .enqueue(push_job(
            "job-1",
            json::object([("ok", json::bool_value(true))]),
        ))
        .expect("enqueue");
    let connector = RecordingConnector::new(400);
    let mut worker = A2APushDeliveryWorker::new(queue, connector);

    assert!(!worker.run_once().expect("run"));

    assert!(root.join("dead").join("job-1.json").exists());
}

#[test]
fn resolved_callback_addresses_reject_private_dns_like_python() {
    assert_eq!(
        validate_resolved_callback_addresses(["10.0.0.1"]),
        Err(InvalidPushNotificationConfigError)
    );
}

#[test]
fn pinned_callback_request_pins_dns_resolution_and_preserves_original_host_sni() {
    let pinned = pinned_callback_request(
        "https://callback.example:8443/a2a",
        "93.184.216.34",
        BTreeMap::from([("X-Trace".to_owned(), "trace-1".to_owned())]),
    )
    .expect("pinned request");

    assert_eq!(pinned.url, "https://callback.example:8443/a2a");
    assert_eq!(
        pinned.headers,
        BTreeMap::from([
            ("Host".to_owned(), "callback.example:8443".to_owned()),
            ("X-Trace".to_owned(), "trace-1".to_owned()),
        ])
    );
    assert_eq!(pinned.sni_hostname, "callback.example");
    assert_eq!(
        pinned
            .resolved_addresses
            .iter()
            .map(ToString::to_string)
            .collect::<Vec<_>>(),
        vec!["93.184.216.34:8443"]
    );
}

#[test]
fn pinned_callback_request_uses_scheme_default_ports_like_python() {
    let http = pinned_callback_request(
        "http://callback.example/a2a",
        "93.184.216.34",
        BTreeMap::new(),
    )
    .expect("http pinned request");
    assert_eq!(
        http.resolved_addresses
            .iter()
            .map(ToString::to_string)
            .collect::<Vec<_>>(),
        vec!["93.184.216.34:80"]
    );

    let https = pinned_callback_request(
        "https://callback.example/a2a",
        "93.184.216.34",
        BTreeMap::new(),
    )
    .expect("https pinned request");
    assert_eq!(
        https
            .resolved_addresses
            .iter()
            .map(ToString::to_string)
            .collect::<Vec<_>>(),
        vec!["93.184.216.34:443"]
    );
}

#[derive(Clone, Debug, PartialEq)]
struct PostRecord {
    url: String,
    payload: JsonValue,
    headers: BTreeMap<String, String>,
    timeout: f64,
}

#[derive(Clone, Debug)]
struct RecordingConnector {
    status_code: u16,
    posts: Arc<Mutex<Vec<PostRecord>>>,
}

impl RecordingConnector {
    fn new(status_code: u16) -> Self {
        Self {
            status_code,
            posts: Arc::new(Mutex::new(Vec::new())),
        }
    }
}

impl A2APushCallbackConnector for RecordingConnector {
    fn post(
        &mut self,
        url: &str,
        payload: &JsonValue,
        headers: &BTreeMap<String, String>,
        timeout_seconds: f64,
    ) -> Result<A2APushCallbackResponse, A2APushDeliveryError> {
        self.posts.lock().expect("posts").push(PostRecord {
            url: url.to_owned(),
            payload: payload.clone(),
            headers: headers.clone(),
            timeout: timeout_seconds,
        });
        Ok(A2APushCallbackResponse {
            status_code: self.status_code,
        })
    }
}

#[derive(Clone, Debug)]
struct AckFailingQueue {
    job: Option<A2APushJob>,
    acked: Vec<String>,
    retried: Vec<A2APushJob>,
    dead: Vec<A2APushJob>,
}

impl AckFailingQueue {
    fn new(job: A2APushJob) -> Self {
        Self {
            job: Some(job),
            acked: Vec::new(),
            retried: Vec::new(),
            dead: Vec::new(),
        }
    }
}

impl A2APushQueueBackend for AckFailingQueue {
    fn claim(&mut self, _now: Option<f64>) -> Result<Option<A2APushJob>, PushQueueError> {
        Ok(self.job.clone())
    }

    fn ack(&mut self, job_id: &str) -> Result<(), PushQueueError> {
        self.acked.push(job_id.to_owned());
        Err(PushQueueError::InvalidJob)
    }

    fn retry(&mut self, job: A2APushJob) -> Result<(), PushQueueError> {
        self.retried.push(job);
        Ok(())
    }

    fn dead_letter(&mut self, job: A2APushJob) -> Result<(), PushQueueError> {
        self.dead.push(job);
        Ok(())
    }
}

fn push_job(job_id: &str, payload: JsonValue) -> A2APushJob {
    A2APushJob::new(
        job_id,
        "task-1",
        "cfg-1",
        "https://callback.example/a2a",
        payload,
    )
}

fn temp_root(name: &str) -> std::path::PathBuf {
    let root = std::env::temp_dir().join(format!(
        "iac-code-a2a-push-worker-{name}-{}",
        std::process::id()
    ));
    let _ = std::fs::remove_dir_all(&root);
    root
}
