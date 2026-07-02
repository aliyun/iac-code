use std::fmt;
use std::time::{SystemTime, UNIX_EPOCH};

use iac_code_protocol::json::JsonValue;
use ring::rand;
use ring::rand::SecureRandom;

use crate::metrics::{A2AMetrics, NoOpA2AMetrics};
use crate::push_config_store::A2APushConfigStore;
use crate::push_endpoint::{validate_push_callback_url, InvalidPushNotificationConfigError};
use crate::push_queue::{
    A2APushJob, LocalFileA2APushQueue, PushQueueError, RedisPushStore, RedisStreamsA2APushQueue,
};
use crate::types::validate_protocol_id;

pub trait A2APushQueueSink {
    fn enqueue(&mut self, job: A2APushJob) -> Result<(), PushQueueError>;
}

impl A2APushQueueSink for LocalFileA2APushQueue {
    fn enqueue(&mut self, job: A2APushJob) -> Result<(), PushQueueError> {
        Self::enqueue(self, job)
    }
}

impl<S> A2APushQueueSink for RedisStreamsA2APushQueue<S>
where
    S: RedisPushStore,
{
    fn enqueue(&mut self, job: A2APushJob) -> Result<(), PushQueueError> {
        Self::enqueue(self, job)
    }
}

#[derive(Debug, PartialEq, Eq)]
pub enum A2APushSenderError {
    InvalidConfig,
    Queue(String),
}

impl fmt::Display for A2APushSenderError {
    fn fmt(&self, formatter: &mut fmt::Formatter<'_>) -> fmt::Result {
        match self {
            Self::InvalidConfig => formatter.write_str("Invalid push notification config"),
            Self::Queue(message) => formatter.write_str(message),
        }
    }
}

impl std::error::Error for A2APushSenderError {}

impl From<InvalidPushNotificationConfigError> for A2APushSenderError {
    fn from(_: InvalidPushNotificationConfigError) -> Self {
        Self::InvalidConfig
    }
}

impl From<PushQueueError> for A2APushSenderError {
    fn from(error: PushQueueError) -> Self {
        Self::Queue(format!("{error:?}"))
    }
}

static NOOP_A2A_METRICS: NoOpA2AMetrics = NoOpA2AMetrics;

pub struct A2APushSender<'a, Q>
where
    Q: A2APushQueueSink,
{
    config_store: &'a A2APushConfigStore,
    queue: &'a mut Q,
    metrics: &'a dyn A2AMetrics,
}

impl<'a, Q> A2APushSender<'a, Q>
where
    Q: A2APushQueueSink,
{
    pub fn new(config_store: &'a A2APushConfigStore, queue: &'a mut Q) -> Self {
        Self {
            config_store,
            queue,
            metrics: &NOOP_A2A_METRICS,
        }
    }

    pub fn with_metrics(mut self, metrics: &'a dyn A2AMetrics) -> Self {
        self.metrics = metrics;
        self
    }

    pub fn send_notification(
        &mut self,
        task_id: &str,
        payload: JsonValue,
    ) -> Result<usize, A2APushSenderError> {
        let task_id =
            validate_protocol_id(task_id).map_err(|_| A2APushSenderError::InvalidConfig)?;
        let configs = self.config_store.get_info_for_dispatch(&task_id)?;
        let mut count = 0;
        for config in configs {
            let url = validate_push_callback_url(&config.url)?;
            let config_id = if config.id.is_empty() {
                task_id.clone()
            } else {
                config.id
            };
            self.queue.enqueue(A2APushJob::new(
                new_push_job_id(),
                &task_id,
                config_id,
                url,
                payload.clone(),
            ))?;
            self.metrics.record_push_enqueued();
            count += 1;
        }
        Ok(count)
    }
}

fn new_push_job_id() -> String {
    let mut bytes = [0_u8; 16];
    if rand::SystemRandom::new().fill(&mut bytes).is_ok() {
        return hex_lower(&bytes);
    }
    format!("job-{}", current_time_nanos())
}

fn hex_lower(bytes: &[u8]) -> String {
    let mut output = String::with_capacity(bytes.len() * 2);
    for byte in bytes {
        use std::fmt::Write;
        write!(output, "{byte:02x}").expect("writing to String should not fail");
    }
    output
}

fn current_time_nanos() -> u128 {
    SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .map_or(0, |duration| duration.as_nanos())
}
