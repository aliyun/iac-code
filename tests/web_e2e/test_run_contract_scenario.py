from __future__ import annotations

from types import SimpleNamespace

from scripts.repl.e2e.deterministic_openai_server import FINAL_RESPONSE, RELOAD_RESPONSE
from scripts.web.e2e.run_contract_scenario import (
    _contains_text,
    _records_for_request_spans,
    _session_path,
    _wait_for_provider_count_and_transcript,
    parse_args,
)


def test_web_contract_runner_defaults_to_browser_verification() -> None:
    args = parse_args([])

    assert args.skip_browser is False
    assert args.timeout > 0


def test_web_contract_runner_recurses_through_public_payloads() -> None:
    payload = {"messages": [{"blocks": [{"text": "E2E_RESPONSE_OK"}]}]}

    assert _contains_text(payload, "E2E_RESPONSE_OK") is True
    assert _contains_text(payload, "aliyun_http") is False


def test_web_contract_runner_quotes_composite_session_id() -> None:
    assert _session_path("ws~abc/def", "/messages") == "/api/sessions/ws~abc%2Fdef/messages"


def test_web_contract_runner_separates_title_and_wiki_preparation_from_normal_spans() -> None:
    records = [
        {"kind": "span", "span_id": "title", "name": "chat fixture", "attributes": {}},
        {"kind": "span", "span_id": "wiki", "name": "chat fixture", "attributes": {}},
        {
            "kind": "span",
            "span_id": "normal",
            "name": "chat fixture",
            "attributes": {"iac_code.mode": "normal"},
        },
        {"kind": "log", "span_id": "title", "name": "iac.api.request.succeeded"},
        {"kind": "log", "span_id": "wiki", "name": "iac.api.request.succeeded"},
        {"kind": "log", "span_id": "normal", "name": "iac.api.request.succeeded"},
        {"kind": "metric", "name": "iac.api.request.count"},
    ]

    preparation = _records_for_request_spans(records, required_attributes={"iac_code.mode": None})
    normal = _records_for_request_spans(records, required_attributes={"iac_code.mode": "normal"})

    assert {record.get("span_id") for record in preparation if record.get("span_id")} == {"title", "wiki"}
    assert {record.get("span_id") for record in normal if record.get("span_id")} == {"normal"}


def test_web_reload_waits_for_current_requests_and_a_new_reply(monkeypatch) -> None:
    historical = {"messages": [{"text": FINAL_RESPONSE}]}
    completed = {"messages": [{"text": FINAL_RESPONSE}, {"text": RELOAD_RESPONSE}]}
    snapshots = iter([historical, historical, completed])
    request_counts = iter([4, 6, 6])
    calls = []

    def transcript(*args):
        calls.append(args)
        return next(snapshots)

    monkeypatch.setattr("scripts.web.e2e.run_contract_scenario._json_request", transcript)
    monkeypatch.setattr("scripts.web.e2e.run_contract_scenario.time.sleep", lambda _seconds: None)
    result = _wait_for_provider_count_and_transcript(
        "http://fixture",
        "session",
        SimpleNamespace(requests=lambda: [{}] * next(request_counts)),
        expected_requests=6,
        expected_text=RELOAD_RESPONSE,
        timeout=1,
    )

    assert result is completed
    assert len(calls) == 3
