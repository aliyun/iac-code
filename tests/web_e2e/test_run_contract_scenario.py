from __future__ import annotations

from scripts.web.e2e.run_contract_scenario import (
    _contains_text,
    _records_for_request_spans,
    _session_path,
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


def test_web_contract_runner_filters_title_and_normal_request_spans() -> None:
    records = [
        {"kind": "span", "span_id": "title", "name": "chat fixture", "attributes": {}},
        {
            "kind": "span",
            "span_id": "normal",
            "name": "chat fixture",
            "attributes": {"iac_code.mode": "normal"},
        },
        {"kind": "log", "span_id": "title", "name": "iac.api.request.succeeded"},
        {"kind": "log", "span_id": "normal", "name": "iac.api.request.succeeded"},
        {"kind": "metric", "name": "iac.api.request.count"},
    ]

    title = _records_for_request_spans(records, required_attributes={"iac_code.mode": None})
    normal = _records_for_request_spans(records, required_attributes={"iac_code.mode": "normal"})

    assert {record.get("span_id") for record in title if record.get("span_id")} == {"title"}
    assert {record.get("span_id") for record in normal if record.get("span_id")} == {"normal"}
