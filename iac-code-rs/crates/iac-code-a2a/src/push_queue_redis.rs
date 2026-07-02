use std::cell::RefCell;
use std::collections::BTreeMap;
use std::time::{SystemTime, UNIX_EPOCH};

use crate::push_queue_job::{
    current_time_seconds, deserialize_push_job, serialize_push_job, A2APushJob, PushQueueError,
};
use crate::push_secrets::A2APushSecretKeyring;

#[derive(Clone, Debug, PartialEq, Eq)]
pub struct RedisStreamEntry {
    pub entry_id: String,
    pub fields: BTreeMap<String, String>,
}

pub trait RedisPushStore {
    fn xgroup_create(
        &mut self,
        stream: &str,
        group: &str,
        id: &str,
        mkstream: bool,
    ) -> Result<(), PushQueueError>;

    fn xadd(
        &mut self,
        stream: &str,
        fields: BTreeMap<String, String>,
    ) -> Result<String, PushQueueError>;

    fn xreadgroup(
        &mut self,
        group: &str,
        consumer: &str,
        stream: &str,
        count: usize,
        block_ms: u64,
    ) -> Result<Vec<RedisStreamEntry>, PushQueueError>;

    fn xautoclaim(
        &mut self,
        stream: &str,
        group: &str,
        consumer: &str,
        min_idle_time_ms: u64,
        start_id: &str,
        count: usize,
    ) -> Result<Vec<RedisStreamEntry>, PushQueueError>;

    fn xack(&mut self, stream: &str, group: &str, ids: &[String]) -> Result<usize, PushQueueError>;

    fn zadd(&mut self, key: &str, member: String, score: f64) -> Result<usize, PushQueueError>;

    fn zrangebyscore(
        &mut self,
        key: &str,
        min: f64,
        max: f64,
        start: usize,
        count: usize,
    ) -> Result<Vec<String>, PushQueueError>;

    fn zrem(&mut self, key: &str, members: &[String]) -> Result<usize, PushQueueError>;

    fn close(&mut self) -> Result<(), PushQueueError> {
        Ok(())
    }
}

#[derive(Clone, Debug)]
pub struct RedisStreamsA2APushQueue<S>
where
    S: RedisPushStore,
{
    redis: S,
    stream: String,
    retry_key: String,
    dead_stream: String,
    consumer_group: String,
    consumer_name: String,
    lease_timeout_ms: u64,
    owns_redis: bool,
    secret_keyring: Option<RefCell<A2APushSecretKeyring>>,
    group_ready: bool,
    claimed_entries: BTreeMap<String, String>,
}

impl<S> RedisStreamsA2APushQueue<S>
where
    S: RedisPushStore,
{
    pub fn new(
        redis: S,
        stream: impl Into<String>,
        retry_key: impl Into<String>,
        dead_stream: impl Into<String>,
        consumer_group: impl Into<String>,
        consumer_name: impl Into<String>,
    ) -> Self {
        let consumer_name = consumer_name.into();
        Self {
            redis,
            stream: stream.into(),
            retry_key: retry_key.into(),
            dead_stream: dead_stream.into(),
            consumer_group: consumer_group.into(),
            consumer_name: if consumer_name.is_empty() {
                default_redis_push_consumer_name()
            } else {
                consumer_name
            },
            lease_timeout_ms: 300_000,
            owns_redis: false,
            secret_keyring: None,
            group_ready: false,
            claimed_entries: BTreeMap::new(),
        }
    }

    pub fn with_lease_timeout_ms(mut self, lease_timeout_ms: u64) -> Self {
        self.lease_timeout_ms = lease_timeout_ms;
        self
    }

    pub fn with_owns_redis(mut self, owns_redis: bool) -> Self {
        self.owns_redis = owns_redis;
        self
    }

    pub fn with_secret_keyring(mut self, secret_keyring: A2APushSecretKeyring) -> Self {
        self.secret_keyring = Some(RefCell::new(secret_keyring));
        self
    }

    pub fn enqueue(&mut self, job: A2APushJob) -> Result<(), PushQueueError> {
        self.ensure_group()?;
        let mut fields = BTreeMap::new();
        fields.insert("job".to_owned(), self.serialize(&job)?);
        self.redis.xadd(&self.stream, fields)?;
        Ok(())
    }

    pub fn claim(&mut self, now: Option<f64>) -> Result<Option<A2APushJob>, PushQueueError> {
        self.ensure_group()?;
        let current = now.unwrap_or_else(current_time_seconds);
        self.promote_due_retries(current)?;
        if let Some(job) = self.claim_expired()? {
            return Ok(Some(job));
        }
        let entries = self.redis.xreadgroup(
            &self.consumer_group,
            &self.consumer_name,
            &self.stream,
            1,
            0,
        )?;
        self.job_from_entries(entries)
    }

    pub fn ack(&mut self, job_id: &str) -> Result<(), PushQueueError> {
        let Some(entry_id) = self.claimed_entries.remove(job_id) else {
            return Ok(());
        };
        self.redis
            .xack(&self.stream, &self.consumer_group, &[entry_id])?;
        Ok(())
    }

    pub fn retry(&mut self, job: A2APushJob) -> Result<(), PushQueueError> {
        let job_id = job.job_id.clone();
        let next_attempt_at = job.next_attempt_at;
        let encoded = self.serialize(&job)?;
        self.redis.zadd(&self.retry_key, encoded, next_attempt_at)?;
        self.ack(&job_id)
    }

    pub fn dead_letter(&mut self, job: A2APushJob) -> Result<(), PushQueueError> {
        let job_id = job.job_id.clone();
        let mut fields = BTreeMap::new();
        fields.insert("job".to_owned(), self.serialize(&job)?);
        self.redis.xadd(&self.dead_stream, fields)?;
        self.ack(&job_id)
    }

    pub fn aclose(&mut self) -> Result<(), PushQueueError> {
        if self.owns_redis {
            self.redis.close()?;
        }
        Ok(())
    }

    fn ensure_group(&mut self) -> Result<(), PushQueueError> {
        if self.group_ready {
            return Ok(());
        }
        match self
            .redis
            .xgroup_create(&self.stream, &self.consumer_group, "0-0", true)
        {
            Ok(()) => {}
            Err(PushQueueError::Redis(message)) if message.contains("BUSYGROUP") => {}
            Err(error) => return Err(error),
        }
        self.group_ready = true;
        Ok(())
    }

    fn promote_due_retries(&mut self, now: f64) -> Result<(), PushQueueError> {
        let members = self
            .redis
            .zrangebyscore(&self.retry_key, f64::NEG_INFINITY, now, 0, 10)?;
        for member in members {
            let mut fields = BTreeMap::new();
            fields.insert("job".to_owned(), member.clone());
            self.redis.xadd(&self.stream, fields)?;
            self.redis.zrem(&self.retry_key, &[member])?;
        }
        Ok(())
    }

    fn claim_expired(&mut self) -> Result<Option<A2APushJob>, PushQueueError> {
        let entries = self.redis.xautoclaim(
            &self.stream,
            &self.consumer_group,
            &self.consumer_name,
            self.lease_timeout_ms,
            "0-0",
            1,
        )?;
        self.job_from_entries(entries)
    }

    fn job_from_entries(
        &mut self,
        entries: Vec<RedisStreamEntry>,
    ) -> Result<Option<A2APushJob>, PushQueueError> {
        for entry in entries {
            let Some(encoded) = entry.fields.get("job") else {
                continue;
            };
            let job = deserialize_push_job(encoded, self.secret_keyring.as_ref())?;
            self.claimed_entries
                .insert(job.job_id.clone(), entry.entry_id);
            return Ok(Some(job));
        }
        Ok(None)
    }

    fn serialize(&self, job: &A2APushJob) -> Result<String, PushQueueError> {
        serialize_push_job(job, self.secret_keyring.as_ref())
    }
}

fn default_redis_push_consumer_name() -> String {
    format!(
        "localhost-{}-{}",
        std::process::id(),
        SystemTime::now()
            .duration_since(UNIX_EPOCH)
            .map_or(0, |duration| duration.as_nanos())
    )
}
