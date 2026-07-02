use std::collections::BTreeMap;
use std::fmt;
use std::net::ToSocketAddrs;
use std::time::{SystemTime, UNIX_EPOCH};

use iac_code_protocol::json::JsonValue;

mod delivery;
mod request;

use crate::metrics::{A2AMetrics, NoOpA2AMetrics};
use crate::push::{validate_push_callback_url, InvalidPushNotificationConfigError};
use crate::push_endpoint::{parse_callback_endpoint, validate_resolved_callback_address_iter};
pub use crate::push_endpoint::{
    pinned_callback_request, validate_resolved_callback_addresses, PinnedCallbackRequest,
};
use crate::push_queue::{
    redact_push_headers, A2APushJob, A2APushRetryPolicy, LocalFileA2APushQueue, PushQueueError,
    RedisPushStore, RedisStreamsA2APushQueue,
};

pub trait A2APushQueueBackend {
    fn claim(&mut self, now: Option<f64>) -> Result<Option<A2APushJob>, PushQueueError>;

    fn ack(&mut self, job_id: &str) -> Result<(), PushQueueError>;

    fn retry(&mut self, job: A2APushJob) -> Result<(), PushQueueError>;

    fn dead_letter(&mut self, job: A2APushJob) -> Result<(), PushQueueError>;
}

impl A2APushQueueBackend for LocalFileA2APushQueue {
    fn claim(&mut self, now: Option<f64>) -> Result<Option<A2APushJob>, PushQueueError> {
        Self::claim(self, now)
    }

    fn ack(&mut self, job_id: &str) -> Result<(), PushQueueError> {
        Self::ack(self, job_id)
    }

    fn retry(&mut self, job: A2APushJob) -> Result<(), PushQueueError> {
        Self::retry(self, job)
    }

    fn dead_letter(&mut self, job: A2APushJob) -> Result<(), PushQueueError> {
        Self::dead_letter(self, job)
    }
}

impl<S> A2APushQueueBackend for RedisStreamsA2APushQueue<S>
where
    S: RedisPushStore,
{
    fn claim(&mut self, now: Option<f64>) -> Result<Option<A2APushJob>, PushQueueError> {
        Self::claim(self, now)
    }

    fn ack(&mut self, job_id: &str) -> Result<(), PushQueueError> {
        Self::ack(self, job_id)
    }

    fn retry(&mut self, job: A2APushJob) -> Result<(), PushQueueError> {
        Self::retry(self, job)
    }

    fn dead_letter(&mut self, job: A2APushJob) -> Result<(), PushQueueError> {
        Self::dead_letter(self, job)
    }
}

#[derive(Clone, Debug, PartialEq, Eq)]
pub struct A2APushCallbackResponse {
    pub status_code: u16,
}

pub trait A2APushCallbackConnector {
    fn post(
        &mut self,
        url: &str,
        payload: &JsonValue,
        headers: &BTreeMap<String, String>,
        timeout_seconds: f64,
    ) -> Result<A2APushCallbackResponse, A2APushDeliveryError>;
}

#[derive(Debug, PartialEq, Eq)]
pub enum A2APushDeliveryError {
    InvalidConfig,
    HttpStatus(u16),
    Timeout(String),
    Transport(String),
    Queue(String),
}

impl A2APushDeliveryError {
    pub fn timeout(message: impl Into<String>) -> Self {
        Self::Timeout(message.into())
    }

    pub fn transport(message: impl Into<String>) -> Self {
        Self::Transport(message.into())
    }
}

impl fmt::Display for A2APushDeliveryError {
    fn fmt(&self, formatter: &mut fmt::Formatter<'_>) -> fmt::Result {
        match self {
            Self::InvalidConfig => write!(formatter, "invalid A2A push callback configuration"),
            Self::HttpStatus(status) => write!(formatter, "HTTP {status}"),
            Self::Timeout(message) | Self::Transport(message) | Self::Queue(message) => {
                formatter.write_str(message)
            }
        }
    }
}

impl std::error::Error for A2APushDeliveryError {}

impl From<InvalidPushNotificationConfigError> for A2APushDeliveryError {
    fn from(_: InvalidPushNotificationConfigError) -> Self {
        Self::InvalidConfig
    }
}

impl From<PushQueueError> for A2APushDeliveryError {
    fn from(error: PushQueueError) -> Self {
        Self::Queue(format!("{error:?}"))
    }
}

pub trait A2APushAlertSink {
    fn dead_lettered(&self, job: &A2APushJob);
}

#[derive(Clone, Copy, Debug, Default, PartialEq, Eq)]
pub struct LoggingA2APushAlertSink;

impl A2APushAlertSink for LoggingA2APushAlertSink {
    fn dead_lettered(&self, job: &A2APushJob) {
        let _ = redact_push_headers(job.headers.clone());
    }
}

pub type A2APushHeaderResolver = dyn Fn(&str, &str) -> BTreeMap<String, String>;

pub struct A2APushDeliveryWorker<Q, C>
where
    Q: A2APushQueueBackend,
    C: A2APushCallbackConnector,
{
    queue: Q,
    connector: C,
    metrics: Box<dyn A2AMetrics>,
    retry_policy: A2APushRetryPolicy,
    alert_sink: Box<dyn A2APushAlertSink>,
    header_resolver: Option<Box<A2APushHeaderResolver>>,
    clock: Box<dyn Fn() -> f64>,
    timeout_seconds: f64,
}

impl<Q, C> A2APushDeliveryWorker<Q, C>
where
    Q: A2APushQueueBackend,
    C: A2APushCallbackConnector,
{
    pub fn new(queue: Q, connector: C) -> Self {
        Self {
            queue,
            connector,
            metrics: Box::new(NoOpA2AMetrics),
            retry_policy: A2APushRetryPolicy::default(),
            alert_sink: Box::new(LoggingA2APushAlertSink),
            header_resolver: None,
            clock: Box::new(current_time_seconds),
            timeout_seconds: 5.0,
        }
    }

    pub fn with_metrics(mut self, metrics: impl A2AMetrics + 'static) -> Self {
        self.metrics = Box::new(metrics);
        self
    }

    pub fn with_retry_policy(mut self, retry_policy: A2APushRetryPolicy) -> Self {
        self.retry_policy = retry_policy;
        self
    }

    pub fn with_alert_sink(mut self, alert_sink: impl A2APushAlertSink + 'static) -> Self {
        self.alert_sink = Box::new(alert_sink);
        self
    }

    pub fn with_header_resolver(
        mut self,
        resolver: impl Fn(&str, &str) -> BTreeMap<String, String> + 'static,
    ) -> Self {
        self.header_resolver = Some(Box::new(resolver));
        self
    }

    pub fn with_clock(mut self, clock: impl Fn() -> f64 + 'static) -> Self {
        self.clock = Box::new(clock);
        self
    }

    pub fn with_timeout_seconds(mut self, timeout_seconds: f64) -> Self {
        self.timeout_seconds = timeout_seconds;
        self
    }

    pub fn queue(&self) -> &Q {
        &self.queue
    }

    pub fn queue_mut(&mut self) -> &mut Q {
        &mut self.queue
    }

    pub fn run_once(&mut self) -> Result<bool, A2APushDeliveryError> {
        let now = (self.clock)();
        let Some(job) = self.queue.claim(Some(now))? else {
            return Ok(false);
        };
        let started = (self.clock)();
        if let Err(error) = validate_push_callback_url(&job.url) {
            self.handle_failure(job, A2APushDeliveryError::from(error))?;
            return Ok(false);
        }
        let headers = self.resolve_headers(&job);

        match self
            .connector
            .post(&job.url, &job.payload, &headers, self.timeout_seconds)
        {
            Ok(response) if (200..300).contains(&response.status_code) => {
                if self.queue.ack(&job.job_id).is_err() {
                    return Ok(false);
                }
                self.metrics
                    .record_push_delivered(((self.clock)() - started) * 1000.0);
                Ok(true)
            }
            Ok(response) => {
                self.handle_failure(job, A2APushDeliveryError::HttpStatus(response.status_code))?;
                Ok(false)
            }
            Err(error) => {
                self.handle_failure(job, error)?;
                Ok(false)
            }
        }
    }

    fn handle_failure(
        &mut self,
        job: A2APushJob,
        error: A2APushDeliveryError,
    ) -> Result<(), A2APushDeliveryError> {
        let next_attempt = job.attempt + 1;
        let failure_class = delivery::classify_failure(&error);
        if failure_class.is_transient() {
            self.metrics.record_push_transient_failure();
        } else {
            self.metrics.record_push_permanent_failure();
        }

        if delivery::should_retry(failure_class, next_attempt, &self.retry_policy) {
            let delay = self.retry_policy.delay_for_attempt(next_attempt);
            self.queue.retry(job.with_attempt(
                next_attempt,
                Some((self.clock)() + delay),
                error.to_string(),
            ))?;
            self.metrics.record_push_retry_scheduled();
            return Ok(());
        }

        let dead = job.with_attempt(next_attempt, None, error.to_string());
        self.queue.dead_letter(dead.clone())?;
        self.metrics.record_push_dead_lettered();
        self.alert_sink.dead_lettered(&dead);
        Ok(())
    }

    fn resolve_headers(&self, job: &A2APushJob) -> BTreeMap<String, String> {
        match &self.header_resolver {
            Some(resolver) => resolver(&job.task_id, &job.config_id),
            None => job.headers.clone(),
        }
    }
}

pub struct DefaultA2APushCallbackConnector;

impl DefaultA2APushCallbackConnector {
    pub fn new() -> Self {
        Self
    }
}

impl Default for DefaultA2APushCallbackConnector {
    fn default() -> Self {
        Self::new()
    }
}

impl A2APushCallbackConnector for DefaultA2APushCallbackConnector {
    fn post(
        &mut self,
        url: &str,
        payload: &JsonValue,
        headers: &BTreeMap<String, String>,
        timeout_seconds: f64,
    ) -> Result<A2APushCallbackResponse, A2APushDeliveryError> {
        validate_push_callback_url(url)?;
        let addresses = resolve_callback_addresses(url)?;
        let address = addresses
            .first()
            .ok_or(InvalidPushNotificationConfigError)?
            .clone();
        let pinned = pinned_callback_request(url, &address, headers.clone())?;
        let client = request::build_callback_client(&pinned)?;
        let response = request::build_json_request(&client, pinned, payload, timeout_seconds)
            .send()
            .map_err(request::classify_send_error)?;
        Ok(A2APushCallbackResponse {
            status_code: response.status().as_u16(),
        })
    }
}

fn resolve_callback_addresses(url: &str) -> Result<Vec<String>, A2APushDeliveryError> {
    let endpoint = parse_callback_endpoint(url)?;
    let addresses = (endpoint.host(), endpoint.port_or_default()?)
        .to_socket_addrs()
        .map_err(|error| A2APushDeliveryError::transport(error.to_string()))?
        .map(|socket| socket.ip().to_string())
        .collect::<Vec<_>>();
    validate_resolved_callback_address_iter(addresses.iter().map(String::as_str))
        .map_err(A2APushDeliveryError::from)
}

fn current_time_seconds() -> f64 {
    SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .map_or(0.0, |duration| duration.as_secs_f64())
}
