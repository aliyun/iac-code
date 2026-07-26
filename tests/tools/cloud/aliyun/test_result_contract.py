"""Focused tests for the reviewed Aliyun business-result contract."""

from __future__ import annotations

import json
from types import MappingProxyType

import pytest

from iac_code.tools.cloud.aliyun.acs3_transport import NormalizedApiResponse, filter_response_headers
from iac_code.tools.cloud.aliyun.api_contract import (
    ApiContractError,
    BuiltApiRequest,
    CanonicalWireContract,
    ResponseBodyPolicy,
)
from iac_code.tools.cloud.aliyun.result_contract import (
    ALIYUN_BODY_CONTRACT_VERSION,
    build_aliyun_http_metadata,
    render_aliyun_result,
    sanitize_aliyun_http_metadata,
    serialize_business_result,
    with_aliyun_content_state,
)


def _contract(*, transport: str = "tea") -> CanonicalWireContract:
    return CanonicalWireContract(
        metadata_source="fresh",
        product="ecs",
        version="2014-05-26",
        action="DescribeInstances",
        style="RPC",
        method="POST",
        pathname="/",
        operation_type="read",
        auth_type="AK",
        signature_scheme="acs3" if transport != "oss_v4_sdk" else "oss_v4",
        transport=transport,
        executable=True,
        unsupported_reasons=(),
        parameters=(),
        consumes=(),
        produces=("application/json",),
        policy_digest="digest",
    )


def _request(mode: str = "json", *, method: str = "POST") -> BuiltApiRequest:
    return BuiltApiRequest(
        method=method,
        raw_path=b"/",
        canonical_query=(),
        headers=MappingProxyType({}),
        body=None,
        response_policy=ResponseBodyPolicy(mode=mode, max_bytes=1024, declared_headers=()),
    )


def _response(
    body: object,
    *,
    status: int = 200,
    headers: MappingProxyType[str, str] | None = None,
    content_encoding: str | None = None,
) -> NormalizedApiResponse:
    return NormalizedApiResponse(
        status=status,
        headers=headers or MappingProxyType({}),
        body=body,
        content_type="application/json",
        content_encoding=content_encoding,
        size=0,
    )


@pytest.mark.parametrize(
    ("body", "expected"),
    [
        ({"Name": "中文"}, '{\n  "Name": "中文"\n}'),
        ([1, True], "[\n  1,\n  true\n]"),
        ("value", '"value"'),
        (3, "3"),
        (False, "false"),
        (None, "null"),
    ],
)
def test_json_business_result_matches_legacy_visible_format(body: object, expected: str) -> None:
    content, body_format = serialize_business_result(_response(body), _request(), _contract())

    assert content == expected
    assert body_format == "json"


@pytest.mark.parametrize("mode", ["text", "xml"])
def test_text_modes_preserve_raw_body(mode: str) -> None:
    content, body_format = serialize_business_result(_response("<ok>yes</ok>"), _request(mode), _contract())

    assert content == "<ok>yes</ok>"
    assert body_format == mode


def test_oss_buffered_structured_xml_result_is_stable_json() -> None:
    content, body_format = serialize_business_result(
        _response(MappingProxyType({"Buckets": [{"Name": "a"}]})),
        _request("xml"),
        _contract(transport="oss_v4_sdk"),
    )

    assert json.loads(content) == {"Buckets": [{"Name": "a"}]}
    assert body_format == "json"


def test_204_and_json_null_have_same_content_but_distinct_formats() -> None:
    empty_content, empty_format = serialize_business_result(_response(None, status=204), _request("text"), _contract())
    null_content, null_format = serialize_business_result(_response(None), _request("json"), _contract())

    assert (empty_content, empty_format) == ("null", "empty")
    assert (null_content, null_format) == ("null", "json")


@pytest.mark.parametrize("body", [b"\x00\xff", bytearray(b"\x00\xff"), {"encoding": "base64", "data": "AP8="}])
def test_binary_result_is_stable_base64_json(body: object) -> None:
    content, body_format = serialize_business_result(_response(body), _request("binary"), _contract())

    assert json.loads(content) == {"encoding": "base64", "data": "AP8="}
    assert body_format == "binary_base64_json"


@pytest.mark.parametrize(
    "body",
    ["raw", {"encoding": "hex", "data": "00"}, {"encoding": "base64", "data": "not base64!"}],
)
def test_invalid_binary_result_is_rejected(body: object) -> None:
    with pytest.raises(ApiContractError, match="aliyun_response_body_invalid"):
        serialize_business_result(_response(body), _request("binary"), _contract())


def test_head_takes_headers_only_priority_over_204() -> None:
    headers = filter_response_headers(
        {
            "RequestId": "req-1",
            "ETag": "etag-1",
            "Last-Modified": "today",
            "Content-Length": "12",
            "X-Result-Token": "business-token",
            "Authorization": "secret",
        },
        declared_headers=("X-Result-Token",),
    )
    assert isinstance(headers, MappingProxyType)

    content, body_format = serialize_business_result(
        _response(None, status=204, headers=headers),
        _request("json", method="HEAD"),
        _contract(),
    )

    assert json.loads(content) == {
        "content-length": "12",
        "etag": "etag-1",
        "last-modified": "today",
        "requestid": "req-1",
        "x-result-token": "business-token",
    }
    assert "secret" not in content
    assert body_format == "headers_only_json"


def test_headers_only_accepts_exact_count_and_rejects_one_more() -> None:
    exact = MappingProxyType({f"x-{index}": "v" for index in range(64)})
    content, body_format = serialize_business_result(
        _response(None, headers=exact),
        _request("headers_only"),
        _contract(),
    )

    assert len(json.loads(content)) == 64
    assert body_format == "headers_only_json"
    with pytest.raises(ApiContractError, match="aliyun_response_headers_too_large"):
        serialize_business_result(
            _response(None, headers=MappingProxyType({**dict(exact), "x-over": "v"})),
            _request("headers_only"),
            _contract(),
        )


def test_headers_only_uses_utf8_byte_limit_for_each_value() -> None:
    exact = "界" * 2730 + "ab"
    assert len(exact.encode("utf-8")) == 8192
    serialize_business_result(
        _response(None, headers=MappingProxyType({"x-value": exact})),
        _request("headers_only"),
        _contract(),
    )

    with pytest.raises(ApiContractError, match="aliyun_response_headers_too_large"):
        serialize_business_result(
            _response(None, headers=MappingProxyType({"x-value": exact + "c"})),
            _request("headers_only"),
            _contract(),
        )


def test_headers_only_uses_exact_stable_json_byte_limit() -> None:
    empty_values = {f"x-{index}": "" for index in range(4)}
    overhead = len(json.dumps(empty_values, ensure_ascii=False, indent=2, sort_keys=True).encode("utf-8"))
    payload_bytes = 32768 - overhead
    base, remainder = divmod(payload_bytes, 4)
    exact = {key: "v" * (base + (1 if index < remainder else 0)) for index, key in enumerate(empty_values)}
    assert all(len(value.encode("utf-8")) <= 8192 for value in exact.values())
    expected = json.dumps(exact, ensure_ascii=False, indent=2, sort_keys=True)
    assert len(expected.encode("utf-8")) == 32768

    content, _ = serialize_business_result(
        _response(None, headers=MappingProxyType(exact)), _request("headers_only"), _contract()
    )
    assert content == expected

    over = dict(exact)
    over["x-0"] += "v"
    with pytest.raises(ApiContractError, match="aliyun_response_headers_too_large"):
        serialize_business_result(
            _response(None, headers=MappingProxyType(over)), _request("headers_only"), _contract()
        )


def test_metadata_is_derived_without_header_values_and_sanitized() -> None:
    response = _response(
        {"RequestId": "req-1"},
        headers=MappingProxyType({"requestid": "secret-value"}),
        content_encoding="gzip",
    )
    metadata = build_aliyun_http_metadata(response, _request(), _contract(), body_format="json")
    metadata["unknown"] = "drop-me"
    metadata["headers"] = {"requestid": "secret-value"}

    sanitized = sanitize_aliyun_http_metadata(metadata)

    assert sanitized == {
        "contract_version": ALIYUN_BODY_CONTRACT_VERSION,
        "product": "ecs",
        "version": "2014-05-26",
        "action": "DescribeInstances",
        "status": 200,
        "status_class": "2xx",
        "response_mode": "json",
        "body_format": "json",
        "headers_present": True,
        "body_present": True,
        "content_type_present": True,
        "size_present": True,
        "content_encoding_present": True,
        "headers_nonempty": True,
        "header_count": 1,
    }
    assert "secret-value" not in json.dumps(sanitized)


def test_content_state_is_added_only_to_marked_metadata_copy() -> None:
    original = {"keep": "value", "aliyun_http": {"contract_version": ALIYUN_BODY_CONTRACT_VERSION}}

    updated = with_aliyun_content_state(original, externalized=True)

    assert updated == {
        "keep": "value",
        "aliyun_http": {
            "contract_version": ALIYUN_BODY_CONTRACT_VERSION,
            "content_state": "externalized_preview",
        },
    }
    assert original["aliyun_http"] == {"contract_version": ALIYUN_BODY_CONTRACT_VERSION}


def test_renderer_handles_null_request_id_text_and_externalized_preview() -> None:
    marker = {
        "contract_version": ALIYUN_BODY_CONTRACT_VERSION,
        "body_format": "json",
        "content_state": "inline_final",
    }

    assert render_aliyun_result({}, "null", is_error=False, aliyun_http=marker, verbose=False) == "Call succeeded"
    assert (
        render_aliyun_result(
            {},
            '{"RequestId":"req-1"}',
            is_error=False,
            aliyun_http=marker,
            verbose=False,
        )
        == "Call succeeded (RequestId: req-1)"
    )
    assert render_aliyun_result({}, "body", is_error=False, aliyun_http=marker, verbose=True) == "body"
    assert (
        render_aliyun_result(
            {},
            "first\nsecond",
            is_error=False,
            aliyun_http={**marker, "content_state": "externalized_preview"},
            verbose=False,
        )
        == "Received response (2 lines)"
    )


def test_renderer_keeps_diagnostics_composite_on_generic_line_summary() -> None:
    marker = {
        "contract_version": ALIYUN_BODY_CONTRACT_VERSION,
        "body_format": "json",
        "content_state": "inline_final",
    }

    summary = render_aliyun_result(
        {},
        '{"RequestId":"req-1"}\n\nROS validation diagnostics',
        is_error=False,
        aliyun_http=marker,
        verbose=False,
    )

    assert summary == "Received response (3 lines)"
