use iac_code_a2a::metrics::{A2AMetrics, NoOpA2AMetrics};

#[test]
fn noop_metrics_records_task_lifecycle_hooks_like_python() {
    let metrics: Box<dyn A2AMetrics> = Box::new(NoOpA2AMetrics);

    metrics.record_task_created();
    metrics.record_turn_completed();
    metrics.record_task_canceled();
    metrics.record_task_failed();
    metrics.record_context_evicted();
    metrics.record_executor_error();
}

#[test]
fn noop_metrics_records_push_delivery_hooks_like_python() {
    let metrics: Box<dyn A2AMetrics> = Box::new(NoOpA2AMetrics);

    metrics.record_push_enqueued();
    metrics.record_push_delivered(12.5);
    metrics.record_push_retry_scheduled();
    metrics.record_push_dead_lettered();
    metrics.record_push_permanent_failure();
    metrics.record_push_transient_failure();
    metrics.record_push_queue_depth(3);
}
