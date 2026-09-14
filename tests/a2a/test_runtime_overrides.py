from google.protobuf.struct_pb2 import Struct

from iac_code.a2a.runtime_overrides import a2a_request_context, resolve_a2a_llm_headers
from iac_code.providers.request_headers import get_provider_request_headers


def test_resolve_a2a_llm_headers_accepts_mapping_and_protobuf_metadata() -> None:
    expected = {"X-Request-Source": "a2a", "Authorization": "Bearer request-token"}
    metadata = {"iac_code": {"llm_headers": expected}}

    assert resolve_a2a_llm_headers(metadata) == expected

    protobuf_metadata = Struct()
    protobuf_metadata.update(metadata)
    assert resolve_a2a_llm_headers(protobuf_metadata) == expected


def test_resolve_a2a_llm_headers_filters_invalid_entries_case_insensitively() -> None:
    metadata = {
        "iac_code": {
            "llm_headers": {
                " X-Trace ": "first",
                "x-trace": "last",
                "bad header": "ignored",
                "X-Newline": "ignored\r\nInjected: yes",
                "X-Control": "ignored\x00",
                "X-Unicode": "忽略",
                "X-Number": 123,
            }
        }
    }

    assert resolve_a2a_llm_headers(metadata) == {"x-trace": "last"}


def test_resolve_a2a_llm_headers_distinguishes_omission_from_explicit_clear() -> None:
    assert resolve_a2a_llm_headers(None) is None
    assert resolve_a2a_llm_headers({"iac_code": {}}) is None
    assert resolve_a2a_llm_headers({"iac_code": {"llm_headers": None}}) is None
    assert resolve_a2a_llm_headers({"iac_code": {"llm_headers": {}}}) == {}


def test_a2a_request_context_scopes_and_restores_llm_headers() -> None:
    assert get_provider_request_headers() == {}

    with a2a_request_context(llm_headers={"X-Session": "one"}):
        assert get_provider_request_headers() == {"X-Session": "one"}
        with a2a_request_context(preferred_language="en"):
            assert get_provider_request_headers() == {"X-Session": "one"}

    assert get_provider_request_headers() == {}
