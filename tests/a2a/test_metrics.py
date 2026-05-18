from iac_code.a2a.metrics import A2AMetrics, NoOpA2AMetrics


def test_noop_metrics_records_events() -> None:
    metrics: A2AMetrics = NoOpA2AMetrics()

    metrics.record_task_created()
    metrics.record_turn_completed()
    metrics.record_task_canceled()
    metrics.record_task_failed()
    metrics.record_context_evicted()
    metrics.record_executor_error()


def test_noop_metrics_support_push_delivery_hooks() -> None:
    metrics: A2AMetrics = NoOpA2AMetrics()

    metrics.record_push_enqueued()
    metrics.record_push_delivered(duration_ms=12.5)
    metrics.record_push_retry_scheduled()
    metrics.record_push_dead_lettered()
    metrics.record_push_permanent_failure()
    metrics.record_push_transient_failure()
    metrics.record_push_queue_depth(3)
