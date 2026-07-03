pub trait A2AMetrics: Send + Sync {
    fn record_task_created(&self) {}

    fn record_turn_completed(&self) {}

    fn record_task_canceled(&self) {}

    fn record_task_failed(&self) {}

    fn record_context_evicted(&self) {}

    fn record_executor_error(&self) {}

    fn record_push_enqueued(&self) {}

    fn record_push_delivered(&self, _duration_ms: f64) {}

    fn record_push_retry_scheduled(&self) {}

    fn record_push_dead_lettered(&self) {}

    fn record_push_permanent_failure(&self) {}

    fn record_push_transient_failure(&self) {}

    fn record_push_queue_depth(&self, _depth: usize) {}
}

#[derive(Clone, Copy, Debug, Default, PartialEq, Eq)]
pub struct NoOpA2AMetrics;

impl A2AMetrics for NoOpA2AMetrics {}
