import pytest

from scripts.web.e2e.resource_link_workflow import delays
from scripts.web.e2e.resource_link_workflow.files import WorkflowError


@pytest.mark.parametrize("seconds", [1, 30])
def test_worker_wait_has_no_resource_deadline_or_cloud_operation(monkeypatch, seconds):
    slept = []
    ticks = iter([50.0, 50.0 + seconds])
    monkeypatch.setattr(delays.time, "sleep", slept.append)
    monkeypatch.setattr(delays.time, "monotonic", lambda: next(ticks))
    assert delays.worker_wait(seconds) == {
        "waited_seconds": seconds,
        "cloud_calls": 0,
        "resource_deadline": None,
    }
    assert slept == [seconds]


@pytest.mark.parametrize("seconds", [0, -1, 31, True, False, "30", 1.5, None, float("nan")])
def test_worker_wait_rejects_malformed_single_transport_delay(monkeypatch, seconds):
    monkeypatch.setattr(delays.time, "sleep", lambda _: pytest.fail("invalid wait must not sleep"))
    with pytest.raises(WorkflowError, match="no resource deadline"):
        delays.worker_wait(seconds)


def test_worker_wait_can_repeat_without_aggregate_deadline(monkeypatch):
    slept = []
    monkeypatch.setattr(delays.time, "sleep", slept.append)
    monkeypatch.setattr(delays.time, "monotonic", lambda: 0)
    for _ in range(121):
        assert delays.worker_wait()["resource_deadline"] is None
    assert slept == [30] * 121
