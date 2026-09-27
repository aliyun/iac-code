"""Offline checks for the bounded REPL wait diagnosis."""

from __future__ import annotations

import json

from scripts.repl.e2e import wait_diagnosis


def test_diagnosis_uses_bailian_key_and_redacts_known_secrets(tmp_path, monkeypatch) -> None:
    api_key = "sk-fixture-secret-value"
    cloud_key = "LTAIfixture123456"
    (tmp_path / ".credentials.yml").write_text(json.dumps({"dashscope": api_key}), encoding="utf-8")
    (tmp_path / ".cloud-credentials.yml").write_text(
        json.dumps({"aliyun": {"access_key_id": cloud_key}}), encoding="utf-8"
    )
    calls = []

    class Response:
        def raise_for_status(self):
            return None

        def json(self):
            return {"choices": [{"message": {"content": '{"state":"waiting_for_input","confidence":0.93}'}}]}

    def fake_post(url, **kwargs):
        calls.append((url, kwargs))
        return Response()

    monkeypatch.setattr(wait_diagnosis.httpx, "post", fake_post)
    result = wait_diagnosis.diagnose_wait(
        tmp_path,
        expected="pipeline completed",
        transcript=f"Ask user question. API key: {api_key}. Cloud key: {cloud_key}",
    )

    assert result == {"state": "waiting_for_input", "confidence": 0.93}
    assert len(calls) == 1
    assert calls[0][0] == wait_diagnosis.BAILIAN_CHAT_URL
    assert calls[0][1]["timeout"] == 20.0
    assert calls[0][1]["headers"]["Authorization"] == "Bearer " + api_key
    content = json.dumps(calls[0][1]["json"])
    assert calls[0][1]["json"]["enable_thinking"] is False
    assert api_key not in content
    assert cloud_key not in content


def test_diagnosis_skips_missing_credentials_or_terminal_content(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(wait_diagnosis.httpx, "post", lambda *_args, **_kwargs: 1 / 0)
    assert wait_diagnosis.diagnose_wait(tmp_path, expected="prompt", transcript="waiting") is None
    (tmp_path / ".credentials.yml").write_text('{"dashscope":"sk-fixture-secret-value"}', encoding="utf-8")
    assert wait_diagnosis.diagnose_wait(tmp_path, expected="prompt", transcript="") is None


def test_diagnosis_failure_returns_fixed_safe_state(tmp_path, monkeypatch) -> None:
    (tmp_path / ".credentials.yml").write_text('{"dashscope":"sk-fixture-secret-value"}', encoding="utf-8")

    def fake_post(*_args, **_kwargs):
        raise wait_diagnosis.httpx.ConnectError("sensitive server response")

    monkeypatch.setattr(wait_diagnosis.httpx, "post", fake_post)
    assert wait_diagnosis.diagnose_wait(tmp_path, expected="prompt", transcript="waiting") == {
        "state": "unavailable", "confidence": 0.0, "failure": "transport",
    }


def test_diagnosis_accepts_fenced_json_response(tmp_path, monkeypatch) -> None:
    (tmp_path / ".credentials.yml").write_text('{"dashscope":"sk-fixture-secret-value"}', encoding="utf-8")

    class Response:
        def raise_for_status(self):
            return None

        def json(self):
            return {"choices": [{"message": {"content": '```json\n{"state":"unknown","confidence":0.5}\n```'}}]}

    monkeypatch.setattr(wait_diagnosis.httpx, "post", lambda *_args, **_kwargs: Response())
    assert wait_diagnosis.diagnose_wait(tmp_path, expected="prompt", transcript="waiting") == {
        "state": "unknown", "confidence": 0.5,
    }
