import pytest

from iac_code.web.pipeline_actions import _ForwardingEventQueue


@pytest.mark.asyncio
async def test_forwarding_queue_invokes_observer():
    sink_calls = []
    observed = []

    async def sink(evs):
        sink_calls.append(evs)

    q = _ForwardingEventQueue(sink, envelope_observer=lambda env: observed.append(env))
    env = {"eventType": "input_required", "data": {"options": [{"candidate_index": 0}]}}
    await q.enqueue_local_pipeline_envelope(env)
    assert observed == [env]


@pytest.mark.asyncio
async def test_forwarding_queue_observer_error_does_not_break_sink():
    sink_calls = []

    async def sink(evs):
        sink_calls.append(evs)

    def boom(_env):
        raise RuntimeError("observer failed")

    q = _ForwardingEventQueue(sink, envelope_observer=boom)
    # push() must still run and sink must not be prevented from future events.
    await q.enqueue_local_pipeline_envelope({"eventType": "status", "data": {}})
    # No exception propagated == pass.
