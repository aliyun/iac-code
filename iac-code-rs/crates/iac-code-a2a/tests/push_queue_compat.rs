use std::cell::RefCell;
use std::collections::BTreeMap;
use std::collections::BTreeSet;
use std::rc::Rc;

use iac_code_a2a::push_queue::{
    redact_push_headers, A2APushJob, A2APushRetryPolicy, LocalFileA2APushQueue, PushQueueError,
    RedisPushStore, RedisStreamEntry, RedisStreamsA2APushQueue,
};
use iac_code_a2a::push_secrets::A2APushSecretKeyring;
use iac_code_a2a::push_worker::{
    A2APushCallbackConnector, A2APushCallbackResponse, A2APushDeliveryError, A2APushDeliveryWorker,
};
use iac_code_protocol::json::{self, JsonValue};

#[test]
fn local_file_push_queue_enqueues_claims_acks_and_persists() {
    let root = temp_root("claim");
    let mut queue = LocalFileA2APushQueue::new(&root);
    let job = push_job("job-1", json::object([("statusUpdate", task_payload())]))
        .with_headers([("Authorization", "Bearer secret")]);

    queue.enqueue(job).expect("enqueue");
    let claimed = queue.claim(Some(100.0)).expect("claim").expect("job");

    assert_eq!(claimed.job_id, "job-1");
    assert!((root.join("inflight").join("job-1.json")).exists());

    queue.ack("job-1").expect("ack");
    assert!(!(root.join("inflight").join("job-1.json")).exists());
}

#[test]
fn local_file_push_queue_retries_and_dead_letters() {
    let root = temp_root("retry");
    let mut queue = LocalFileA2APushQueue::new(&root);
    queue
        .enqueue(push_job(
            "job-1",
            json::object([("ok", json::bool_value(true))]),
        ))
        .expect("enqueue");

    let claimed = queue.claim(Some(100.0)).expect("claim").expect("job");
    queue
        .retry(claimed.with_attempt(1, Some(123.0), "timeout"))
        .expect("retry");

    let raw =
        std::fs::read_to_string(root.join("pending").join("job-1.json")).expect("read pending");
    assert!(raw.contains(r#""attempt":1"#));
    assert!(raw.contains(r#""nextAttemptAt":123.0"#));

    assert!(queue.claim(Some(122.0)).expect("claim").is_none());
    let claimed_again = queue.claim(Some(124.0)).expect("claim").expect("job");
    queue
        .dead_letter(claimed_again.with_attempt(3, None, "HTTP 400"))
        .expect("dead letter");
    assert!((root.join("dead").join("job-1.json")).exists());
}

#[test]
fn local_file_push_queue_recovers_expired_inflight_jobs() {
    let root = temp_root("recover");
    let mut queue = LocalFileA2APushQueue::new(&root).with_inflight_timeout_seconds(10.0);
    queue
        .enqueue(push_job(
            "job-1",
            json::object([("ok", json::bool_value(true))]),
        ))
        .expect("enqueue");
    let claimed = queue.claim(Some(100.0)).expect("claim").expect("job");
    assert_eq!(claimed.next_attempt_at, 110.0);

    let mut restarted = LocalFileA2APushQueue::new(&root).with_inflight_timeout_seconds(10.0);
    assert!(restarted.claim(Some(109.0)).expect("claim").is_none());

    let recovered = restarted.claim(Some(111.0)).expect("claim").expect("job");
    assert_eq!(recovered.job_id, "job-1");
    assert_eq!(recovered.last_error, "Delivery lease expired.");
    assert!((root.join("inflight").join("job-1.json")).exists());
}

#[test]
fn local_file_push_queue_does_not_persist_sensitive_headers() {
    let root = temp_root("headers");
    let mut queue = LocalFileA2APushQueue::new(&root);
    let job = push_job("job-1", json::object([("ok", json::bool_value(true))])).with_headers([
        ("Authorization", "Bearer secret"),
        ("X-A2A-Notification-Token", "token"),
    ]);

    queue.enqueue(job).expect("enqueue");

    let raw =
        std::fs::read_to_string(root.join("pending").join("job-1.json")).expect("read pending");
    assert!(!raw.contains("secret"));
    assert!(!raw.contains("token"));
    assert!(!raw.contains("headers"));
}

#[cfg(unix)]
#[test]
fn local_file_push_queue_writes_private_files_like_python() {
    use std::os::unix::fs::PermissionsExt;

    let root = temp_root("private-perms");
    let mut queue = LocalFileA2APushQueue::new(&root);

    for name in ["pending", "inflight", "dead"] {
        assert_eq!(
            std::fs::metadata(root.join(name))
                .expect("queue directory metadata")
                .permissions()
                .mode()
                & 0o777,
            0o700
        );
    }

    queue
        .enqueue(push_job(
            "job-1",
            json::object([("ok", json::bool_value(true))]),
        ))
        .expect("enqueue");
    assert_eq!(
        std::fs::metadata(root.join("pending").join("job-1.json"))
            .expect("pending job metadata")
            .permissions()
            .mode()
            & 0o777,
        0o600
    );

    queue.claim(Some(100.0)).expect("claim").expect("job");
    assert_eq!(
        std::fs::metadata(root.join("inflight").join("job-1.json"))
            .expect("inflight job metadata")
            .permissions()
            .mode()
            & 0o777,
        0o600
    );
}

#[test]
fn local_file_push_queue_encrypts_jobs_when_keyring_is_configured() {
    let root = temp_root("encrypted");
    let mut queue = LocalFileA2APushQueue::new(root.join("queue"))
        .with_secret_keyring(A2APushSecretKeyring::new(root.join("keys.json")));
    let job = push_job(
        "job-1",
        json::object([("message", json::string("private task payload"))]),
    );

    queue.enqueue(job).expect("enqueue");

    let raw = std::fs::read_to_string(root.join("queue").join("pending").join("job-1.json"))
        .expect("read pending");
    assert!(!raw.contains("private task payload"));
    assert!(!raw.contains("callback.example"));
    assert!(raw.contains("iacCodeEncryptedPushJob"));

    let claimed = queue.claim(Some(100.0)).expect("claim").expect("job");
    assert_eq!(claimed.url, "https://callback.example/a2a");
    assert_eq!(
        claimed.payload,
        json::object([("message", json::string("private task payload"))])
    );
}

#[test]
fn retry_policy_uses_exponential_backoff_with_cap() {
    let policy = A2APushRetryPolicy {
        initial_delay_seconds: 1.0,
        max_delay_seconds: 10.0,
        jitter_ratio: 0.0,
        max_attempts: 5,
    };

    assert_eq!(policy.delay_for_attempt(1), 1.0);
    assert_eq!(policy.delay_for_attempt(2), 2.0);
    assert_eq!(policy.delay_for_attempt(5), 10.0);
}

#[test]
fn redact_push_headers_removes_credentials() {
    assert_eq!(
        redact_push_headers(BTreeMap::from([
            ("Authorization".to_owned(), "Bearer secret".to_owned()),
            ("X-A2A-Notification-Token".to_owned(), "token".to_owned()),
            ("X-Trace".to_owned(), "trace-1".to_owned()),
        ])),
        BTreeMap::from([
            ("Authorization".to_owned(), "[redacted]".to_owned()),
            (
                "X-A2A-Notification-Token".to_owned(),
                "[redacted]".to_owned()
            ),
            ("X-Trace".to_owned(), "trace-1".to_owned()),
        ])
    );
}

#[test]
fn redis_push_queue_enqueues_claims_and_acks_like_python() {
    let redis = SharedFakeRedis::default();
    let mut queue = redis_queue(redis.clone(), "worker-1");
    queue
        .enqueue(
            push_job("job-1", json::object([("ok", json::bool_value(true))]))
                .with_headers([("Authorization", "Bearer secret")]),
        )
        .expect("enqueue");

    let claimed = queue.claim(Some(100.0)).expect("claim").expect("job");

    assert_eq!(claimed.job_id, "job-1");
    assert!(!redis.debug_streams().contains("secret"));
    queue.ack("job-1").expect("ack");
    assert_eq!(
        redis.acked(),
        vec![("push".to_owned(), "workers".to_owned(), "1-0".to_owned())]
    );
}

#[test]
fn redis_push_queue_encrypts_jobs_when_keyring_is_configured_like_python() {
    let root = temp_root("redis-encrypted");
    let redis = SharedFakeRedis::default();
    let mut queue = redis_queue(redis.clone(), "worker-1")
        .with_secret_keyring(A2APushSecretKeyring::new(root.join("keys.json")));
    queue
        .enqueue(push_job(
            "job-1",
            json::object([("message", json::string("private task payload"))]),
        ))
        .expect("enqueue");

    assert!(!redis.debug_streams().contains("private task payload"));
    assert!(!redis.debug_streams().contains("callback.example"));

    let claimed = queue.claim(Some(100.0)).expect("claim").expect("job");
    assert_eq!(
        claimed.payload,
        json::object([("message", json::string("private task payload"))])
    );
    assert_eq!(claimed.url, "https://callback.example/a2a");
}

#[test]
fn redis_push_queue_claims_new_jobs_only_once_per_group_like_python() {
    let redis = SharedFakeRedis::default();
    let mut worker_1 = redis_queue(redis.clone(), "worker-1").with_lease_timeout_ms(1_000);
    let mut worker_2 = redis_queue(redis.clone(), "worker-2").with_lease_timeout_ms(1_000);
    worker_1
        .enqueue(push_job(
            "job-1",
            json::object([("ok", json::bool_value(true))]),
        ))
        .expect("enqueue");

    let claimed = worker_1.claim(Some(100.0)).expect("claim");
    let duplicate = worker_2.claim(Some(100.0)).expect("claim duplicate");

    assert_eq!(claimed.expect("job").job_id, "job-1");
    assert!(duplicate.is_none());
}

#[test]
fn redis_push_queue_reclaims_pending_jobs_only_after_idle_timeout_like_python() {
    let redis = SharedFakeRedis::default();
    let mut worker_1 = redis_queue(redis.clone(), "worker-1").with_lease_timeout_ms(1_000);
    let mut worker_2 = redis_queue(redis.clone(), "worker-2").with_lease_timeout_ms(1_000);
    worker_1
        .enqueue(push_job(
            "job-1",
            json::object([("ok", json::bool_value(true))]),
        ))
        .expect("enqueue");

    redis.set_now_ms(0);
    assert!(worker_1.claim(Some(100.0)).expect("claim").is_some());
    redis.set_now_ms(999);
    assert!(worker_2
        .claim(Some(101.0))
        .expect("before timeout")
        .is_none());
    redis.set_now_ms(1_000);

    let reclaimed = worker_2
        .claim(Some(102.0))
        .expect("after timeout")
        .expect("job");
    assert_eq!(reclaimed.job_id, "job-1");
}

#[test]
fn redis_push_queue_retries_via_sorted_set_and_promotes_due_jobs_like_python() {
    let redis = SharedFakeRedis::default();
    let mut queue = redis_queue(redis.clone(), "worker-1");
    queue
        .enqueue(push_job(
            "job-1",
            json::object([("ok", json::bool_value(true))]),
        ))
        .expect("enqueue");
    let claimed = queue.claim(Some(100.0)).expect("claim").expect("job");

    queue
        .retry(claimed.with_attempt(1, Some(125.0), "timeout"))
        .expect("retry");
    assert_eq!(redis.zset_len("push:retry"), 1);
    assert!(queue
        .claim(Some(124.0))
        .expect("claim before due")
        .is_none());

    let promoted = queue
        .claim(Some(125.0))
        .expect("claim after due")
        .expect("job");
    assert_eq!(promoted.attempt, 1);
    assert_eq!(promoted.last_error, "timeout");
}

#[test]
fn redis_push_queue_dead_letters_claimed_job_like_python() {
    let redis = SharedFakeRedis::default();
    let mut queue = redis_queue(redis.clone(), "worker-1");
    queue
        .enqueue(push_job(
            "job-1",
            json::object([("ok", json::bool_value(true))]),
        ))
        .expect("enqueue");
    let claimed = queue.claim(Some(100.0)).expect("claim").expect("job");

    queue
        .dead_letter(claimed.with_attempt(3, None, "HTTP 400"))
        .expect("dead letter");

    assert_eq!(redis.stream_len("push:dead"), 1);
    assert_eq!(
        redis.acked(),
        vec![("push".to_owned(), "workers".to_owned(), "1-0".to_owned())]
    );
}

#[test]
fn redis_push_queue_closes_owned_redis_client_like_python() {
    let redis = SharedFakeRedis::default();
    let mut queue = redis_queue(redis.clone(), "worker-1").with_owns_redis(true);

    queue.aclose().expect("close");

    assert!(redis.closed());
}

#[test]
fn redis_push_queue_delivers_claimed_job_through_worker_like_python() {
    let redis = SharedFakeRedis::default();
    let mut queue = redis_queue(redis.clone(), "worker-1");
    queue
        .enqueue(push_job(
            "job-1",
            json::object([("ok", json::bool_value(true))]),
        ))
        .expect("enqueue");
    let connector = RecordingPushConnector::default();
    let posts = connector.posts.clone();
    let mut worker = A2APushDeliveryWorker::new(queue, connector);

    assert!(worker.run_once().expect("run"));

    assert_eq!(posts.borrow().as_slice(), ["https://callback.example/a2a"]);
    assert_eq!(
        redis.acked(),
        vec![("push".to_owned(), "workers".to_owned(), "1-0".to_owned())]
    );
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

fn task_payload() -> JsonValue {
    json::object([("taskId", json::string("task-1"))])
}

fn temp_root(name: &str) -> std::path::PathBuf {
    let root = std::env::temp_dir().join(format!(
        "iac-code-a2a-push-queue-{name}-{}",
        std::process::id()
    ));
    let _ = std::fs::remove_dir_all(&root);
    root
}

fn redis_queue(
    redis: SharedFakeRedis,
    consumer_name: &str,
) -> RedisStreamsA2APushQueue<SharedFakeRedis> {
    RedisStreamsA2APushQueue::new(
        redis,
        "push",
        "push:retry",
        "push:dead",
        "workers",
        consumer_name,
    )
}

#[derive(Clone, Default)]
struct RecordingPushConnector {
    posts: Rc<RefCell<Vec<String>>>,
}

impl A2APushCallbackConnector for RecordingPushConnector {
    fn post(
        &mut self,
        url: &str,
        _payload: &JsonValue,
        _headers: &BTreeMap<String, String>,
        _timeout_seconds: f64,
    ) -> Result<A2APushCallbackResponse, A2APushDeliveryError> {
        self.posts.borrow_mut().push(url.to_owned());
        Ok(A2APushCallbackResponse { status_code: 204 })
    }
}

#[derive(Clone, Default)]
struct SharedFakeRedis(Rc<RefCell<FakeRedisPushStore>>);

impl SharedFakeRedis {
    fn set_now_ms(&self, now_ms: u64) {
        self.0.borrow_mut().now_ms = now_ms;
    }

    fn debug_streams(&self) -> String {
        format!("{:?}", self.0.borrow().streams)
    }

    fn stream_len(&self, name: &str) -> usize {
        self.0
            .borrow()
            .streams
            .get(name)
            .map_or(0, std::vec::Vec::len)
    }

    fn zset_len(&self, name: &str) -> usize {
        self.0.borrow().zsets.get(name).map_or(0, BTreeMap::len)
    }

    fn acked(&self) -> Vec<(String, String, String)> {
        self.0.borrow().acked.clone()
    }

    fn closed(&self) -> bool {
        self.0.borrow().closed
    }
}

#[derive(Debug, Default)]
struct FakeRedisPushStore {
    streams: BTreeMap<String, FakeRedisStreamEntries>,
    groups: BTreeSet<(String, String)>,
    group_positions: BTreeMap<(String, String), usize>,
    pending: BTreeMap<(String, String), BTreeMap<String, FakeRedisPendingEntry>>,
    zsets: BTreeMap<String, BTreeMap<String, f64>>,
    acked: Vec<(String, String, String)>,
    closed: bool,
    now_ms: u64,
    next_id: u64,
}

type FakeRedisStreamEntries = Vec<(String, BTreeMap<String, String>)>;

#[derive(Clone, Debug)]
struct FakeRedisPendingEntry {
    fields: BTreeMap<String, String>,
    consumer: String,
    last_delivered_ms: u64,
}

impl RedisPushStore for SharedFakeRedis {
    fn xgroup_create(
        &mut self,
        stream: &str,
        group: &str,
        id: &str,
        mkstream: bool,
    ) -> Result<(), PushQueueError> {
        let mut redis = self.0.borrow_mut();
        let key = (stream.to_owned(), group.to_owned());
        if redis.groups.contains(&key) {
            return Err(PushQueueError::Redis(
                "BUSYGROUP Consumer Group name already exists".to_owned(),
            ));
        }
        redis.groups.insert(key.clone());
        let stream_len = if mkstream {
            redis.streams.entry(stream.to_owned()).or_default().len()
        } else {
            redis.streams.get(stream).map_or(0, std::vec::Vec::len)
        };
        redis
            .group_positions
            .insert(key, if id == "$" { stream_len } else { 0 });
        Ok(())
    }

    fn xadd(
        &mut self,
        stream: &str,
        fields: BTreeMap<String, String>,
    ) -> Result<String, PushQueueError> {
        let mut redis = self.0.borrow_mut();
        redis.next_id += 1;
        let entry_id = format!("{}-0", redis.next_id);
        redis
            .streams
            .entry(stream.to_owned())
            .or_default()
            .push((entry_id.clone(), fields));
        Ok(entry_id)
    }

    fn xreadgroup(
        &mut self,
        group: &str,
        consumer: &str,
        stream: &str,
        count: usize,
        _block_ms: u64,
    ) -> Result<Vec<RedisStreamEntry>, PushQueueError> {
        let mut redis = self.0.borrow_mut();
        let group_key = (stream.to_owned(), group.to_owned());
        let position = redis
            .group_positions
            .get(&group_key)
            .copied()
            .unwrap_or_default();
        let available = redis.streams.entry(stream.to_owned()).or_default();
        if position >= available.len() {
            return Ok(Vec::new());
        }
        let entries = available
            .iter()
            .skip(position)
            .take(count)
            .map(|(entry_id, fields)| RedisStreamEntry {
                entry_id: entry_id.clone(),
                fields: fields.clone(),
            })
            .collect::<Vec<_>>();
        redis
            .group_positions
            .insert(group_key.clone(), position + entries.len());
        let now_ms = redis.now_ms;
        let pending = redis.pending.entry(group_key).or_default();
        for entry in &entries {
            pending.insert(
                entry.entry_id.clone(),
                FakeRedisPendingEntry {
                    fields: entry.fields.clone(),
                    consumer: consumer.to_owned(),
                    last_delivered_ms: now_ms,
                },
            );
        }
        Ok(entries)
    }

    fn xautoclaim(
        &mut self,
        stream: &str,
        group: &str,
        consumer: &str,
        min_idle_time_ms: u64,
        _start_id: &str,
        count: usize,
    ) -> Result<Vec<RedisStreamEntry>, PushQueueError> {
        let mut redis = self.0.borrow_mut();
        let now_ms = redis.now_ms;
        let Some(pending) = redis
            .pending
            .get_mut(&(stream.to_owned(), group.to_owned()))
        else {
            return Ok(Vec::new());
        };
        let mut entries = Vec::new();
        for (entry_id, entry) in pending.iter_mut() {
            if now_ms.saturating_sub(entry.last_delivered_ms) < min_idle_time_ms {
                continue;
            }
            entry.consumer = consumer.to_owned();
            entry.last_delivered_ms = now_ms;
            entries.push(RedisStreamEntry {
                entry_id: entry_id.clone(),
                fields: entry.fields.clone(),
            });
            if entries.len() >= count {
                break;
            }
        }
        Ok(entries)
    }

    fn xack(&mut self, stream: &str, group: &str, ids: &[String]) -> Result<usize, PushQueueError> {
        let mut redis = self.0.borrow_mut();
        let group_key = (stream.to_owned(), group.to_owned());
        let mut removed = 0;
        let pending = redis.pending.entry(group_key).or_default();
        for entry_id in ids {
            if pending.remove(entry_id).is_some() {
                removed += 1;
            }
        }
        redis.acked.extend(
            ids.iter()
                .map(|entry_id| (stream.to_owned(), group.to_owned(), entry_id.clone())),
        );
        Ok(removed)
    }

    fn zadd(&mut self, key: &str, member: String, score: f64) -> Result<usize, PushQueueError> {
        self.0
            .borrow_mut()
            .zsets
            .entry(key.to_owned())
            .or_default()
            .insert(member, score);
        Ok(1)
    }

    fn zrangebyscore(
        &mut self,
        key: &str,
        min: f64,
        max: f64,
        start: usize,
        count: usize,
    ) -> Result<Vec<String>, PushQueueError> {
        let redis = self.0.borrow();
        let mut members = redis
            .zsets
            .get(key)
            .into_iter()
            .flat_map(BTreeMap::iter)
            .filter(|(_, score)| **score >= min && **score <= max)
            .map(|(member, score)| (member.clone(), *score))
            .collect::<Vec<_>>();
        members.sort_by(|left, right| left.1.total_cmp(&right.1).then(left.0.cmp(&right.0)));
        Ok(members
            .into_iter()
            .skip(start)
            .take(count)
            .map(|(member, _)| member)
            .collect())
    }

    fn zrem(&mut self, key: &str, members: &[String]) -> Result<usize, PushQueueError> {
        let mut redis = self.0.borrow_mut();
        let zset = redis.zsets.entry(key.to_owned()).or_default();
        let mut removed = 0;
        for member in members {
            if zset.remove(member).is_some() {
                removed += 1;
            }
        }
        Ok(removed)
    }

    fn close(&mut self) -> Result<(), PushQueueError> {
        self.0.borrow_mut().closed = true;
        Ok(())
    }
}
