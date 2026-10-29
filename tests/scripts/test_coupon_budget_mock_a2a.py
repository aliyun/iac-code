from types import SimpleNamespace

import pytest

from scripts.ci import run_coupon_budget_mock_a2a as runner


@pytest.mark.parametrize("text, content_type, passed", [
    ("", "text/event-stream", False),
    ("   ", "text/event-stream", False),
    ("|本次计算费可抵扣金额|0元|\n|计算部分需补现金|0元|", "text/event-stream", False),
    ("|本次计算费可抵扣金额|待确认|\n|计算部分需补现金|待确认|", "application/json", False),
    ("|本次计算费可抵扣金额|待确认|\n|计算部分需补现金|待确认|", "text/event-stream", True),
])
def test_coupon_run_requires_the_original_answer_without_a_followup(
    monkeypatch, tmp_path, text, content_type, passed,
):
    config = tmp_path / "config"
    config.mkdir()
    monkeypatch.setenv("IAC_CODE_CONFIG_DIR", str(config))
    calls = []
    ledger = []
    lifecycle = []

    class Server:
        def __init__(self, **_kwargs):
            pass

        def start(self):
            lifecycle.append("started")

        def terminate(self):
            lifecycle.append("terminated")

    class Capture:
        def __init__(self, _path):
            self.env = {}

        def start(self):
            return self

        def stop(self):
            return [{"attributes": {"gen_ai.request.model": "offline-test-model"}}]

    def stream(**options):
        calls.append(options)
        if options["prompt"] == runner.QUESTION:
            ledger.append({"case": "query_denied", "action": "DescribeCoupon", "mocked": True})
        return SimpleNamespace(
            context_id="context-one", task_id="task-one", normal_handoff_ready=False,
            last_status_state="TASK_STATE_INPUT_REQUIRED", response_content_type=content_type,
        )

    monkeypatch.setattr(runner, "ManagedServer", Server)
    monkeypatch.setattr(runner, "ObserveCapture", Capture)
    monkeypatch.setattr(runner, "_free_port", lambda _host: 12345)
    monkeypatch.setattr(runner, "_write_server_config", lambda *args, **kwargs: tmp_path / "server.json")
    monkeypatch.setattr(runner, "wait_for_server", lambda *args, **kwargs: None)
    monkeypatch.setattr(runner, "stream_message", stream)
    monkeypatch.setattr(runner, "recorded_calls", lambda _path: ledger.copy())
    monkeypatch.setattr(runner, "tool_inputs", lambda _path: [
        {"id": "guide-read", "name": "read_file", "input": {"path": "references/coupons.md"}},
    ])
    monkeypatch.setattr(runner, "visible_pipeline_text", lambda _path: text)
    monkeypatch.setattr(runner, "pipeline_facts", lambda _path: ({"status": "awaiting_selection"}, []))

    result = runner.run(SimpleNamespace(
        run_dir=tmp_path, provider="offline-test-provider", model="offline-test-model",
        scenario=["query_denied"], timeout=10,
    ))

    assert [call["prompt"] for call in calls] == [runner.SETUP, runner.QUESTION]
    assert calls[1]["context_id"] == "context-one" and calls[1]["task_id"] == "task-one"
    assert calls[1]["timeout"] == 10
    assert result["status"] == ("passed" if passed else "failed")
    assert result["cases"][0]["passed"] is passed
    assert result["cases"][0]["checks"]["visible answer returned for the original question"] is bool(text.strip())
    assert lifecycle == ["started", "terminated"]
