from __future__ import annotations

import asyncio
import gzip
import threading
import time
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path
from types import MappingProxyType, SimpleNamespace
from typing import Any

import aiohttp
import httpx
import pytest
from alibabacloud_tea_openapi import exceptions as open_api_exceptions
from alibabacloud_tea_openapi import models as open_api_models
from alibabacloud_tea_openapi.utils import Utils, get_canonical_query_string
from aliyunsdkcore.acs_exception.exceptions import ServerException as Acs1ServerException
from darabonba.exceptions import RetryError, UnretryableException
from darabonba.policy.retry import RetryPolicyContext
from darabonba.runtime import RuntimeOptions
from Tea.request import TeaRequest

from iac_code.services.providers.aliyun import AliyunCredential
from iac_code.tools.base import ToolContext
from iac_code.tools.cloud.aliyun.acs3_transport import (
    Acs1Transport,
    Acs3StreamingTransport,
    NormalizedApiResponse,
    ResponseTooLarge,
    TeaTransportAdapter,
    TransportRouter,
    _attach_mns_gateway,
    _attach_oss_gateway,
    _attach_pop_v4_gateway,
    _close_response,
    _StreamingOpenApiClient,
    _uses_mns_gateway,
    _uses_oss_gateway,
    _uses_pop_v4_gateway,
    filter_response_headers,
)
from iac_code.tools.cloud.aliyun.api_contract import (
    ApiCallShape,
    ApiContractResolver,
    BuiltApiRequest,
    CanonicalWireContract,
    RequestBuilder,
    ResponseBodyPolicy,
)
from iac_code.tools.cloud.aliyun.endpoint_resolver import EndpointResolution
from iac_code.tools.cloud.aliyun.openmeta import MetadataFetch, ProductMetadata, normalize_api_metadata
from iac_code.tools.cloud.aliyun.result_contract import serialize_business_result
from iac_code.tools.cloud.aliyun.retry_policy import RetryBudget, RetryExhausted, RetryReason, TransportFailure
from iac_code.tools.cloud.aliyun.runtime import create_aliyun_runtime_services

_UPSTREAM_OPENAPI_UTCNOW_WARNING = pytest.mark.filterwarnings(
    "ignore:datetime\\.datetime\\.utcnow\\(\\) is deprecated and scheduled for removal in a future version\\.:"
    "DeprecationWarning:alibabacloud_tea_openapi\\.utils"
)


def contract(**changes: Any) -> CanonicalWireContract:
    value = CanonicalWireContract(
        metadata_source="openmeta",
        product="Ecs",
        version="2014-05-26",
        action="DescribeInstances",
        style="ROA",
        method="GET",
        pathname="/instances",
        operation_type="read",
        auth_type="AK",
        signature_scheme="acs3",
        transport="acs3_streaming",
        executable=True,
        unsupported_reasons=(),
        parameters=(),
        consumes=(),
        produces=("application/json",),
        policy_digest="digest",
        request_body_type="none",
        response_body_type="json",
    )
    return replace(value, **changes)


def built_request(*, mode: str = "json", body: bytes | None = None, max_bytes: int = 8 * 1024**2) -> BuiltApiRequest:
    return BuiltApiRequest(
        method="GET",
        raw_path=b"/instances/a%2Fb",
        canonical_query=(("Name", "a/b"), ("Page", "1")),
        headers=MappingProxyType({"accept": "application/json"}),
        body=body,
        response_policy=ResponseBodyPolicy(mode=mode, max_bytes=max_bytes, declared_headers=("x-result-token",)),
    )


def endpoint() -> EndpointResolution:
    return EndpointResolution("ecs.cn-hangzhou.aliyuncs.com", "catalog_region", None)


def credential(*, token: str = "") -> AliyunCredential:
    return AliyunCredential(
        mode="StsToken" if token else "AK",
        access_key_id="test-ak",
        access_key_secret="test-secret",
        sts_token=token,
    )


def ram_role_credential() -> AliyunCredential:
    return AliyunCredential(
        mode="RamRoleArn",
        access_key_id="source-ak",
        access_key_secret="source-secret",
        ram_role_arn="acs:ram::123456789012:role/restricted",
        ram_session_name="iac-code-test",
    )


@pytest.mark.asyncio
async def test_acs1_transport_preserves_rpc_form_fields_and_normalizes_success() -> None:
    calls: list[Any] = []

    class FakeClient:
        def do_action_with_exception(self, request: Any) -> bytes:
            calls.append(request)
            return b'{"Regions":["cn-hangzhou"]}'

    transport = Acs1Transport(client_factory=lambda _credential, _region: FakeClient())
    request = replace(
        built_request(),
        method="POST",
        raw_path=b"/",
        canonical_query=(),
        headers=MappingProxyType({"content-type": "application/x-www-form-urlencoded"}),
        body=b"AcceptLanguage=zh-CN",
    )
    response = await transport.execute(
        contract=contract(
            product="fnf",
            version="2019-03-15",
            action="DescribeRegions",
            style="RPC",
            method="POST",
            pathname="/",
            signature_scheme="acs1",
            transport="acs1",
            request_body_type="formData",
        ),
        request=request,
        endpoint=replace(endpoint(), endpoint="cn-hangzhou.fnf.aliyuncs.com", region_id="cn-hangzhou"),
        credential=credential(token="sts-token"),
        context=ToolContext(),
        budget=budget(),
    )

    assert response.status == 200
    assert response.body == {"Regions": ["cn-hangzhou"]}
    assert calls[0].get_body_params() == {"AcceptLanguage": "zh-CN"}
    assert calls[0].get_uri_pattern() is None


@pytest.mark.asyncio
async def test_acs1_transport_normalizes_server_error_without_request_id() -> None:
    class FakeClient:
        def do_action_with_exception(self, request: Any) -> bytes:
            del request
            raise Acs1ServerException("QueueNotExist", "missing", 404, "request-id-must-not-leak")

    response = await Acs1Transport(client_factory=lambda _credential, _region: FakeClient()).execute(
        contract=contract(signature_scheme="acs1", transport="acs1"),
        request=built_request(),
        endpoint=endpoint(),
        credential=credential(),
        context=ToolContext(),
        budget=budget(),
    )

    assert response.status == 404
    assert response.body == {"Code": "QueueNotExist", "Message": "missing"}


@pytest.mark.asyncio
async def test_acs1_transport_waits_for_owned_worker_before_propagating_cancel() -> None:
    started = threading.Event()
    release = threading.Event()

    class FakeClient:
        def do_action_with_exception(self, request: Any) -> bytes:
            del request
            started.set()
            assert release.wait(timeout=2)
            return b"{}"

    call = asyncio.create_task(
        Acs1Transport(client_factory=lambda _credential, _region: FakeClient()).execute(
            contract=contract(signature_scheme="acs1", transport="acs1"),
            request=built_request(),
            endpoint=endpoint(),
            credential=credential(),
            context=ToolContext(),
            budget=budget(),
        )
    )
    assert await asyncio.to_thread(started.wait, 1)
    call.cancel()
    await asyncio.sleep(0.01)
    assert not call.done()
    release.set()
    with pytest.raises(asyncio.CancelledError):
        await call


def test_mns_gateway_is_scoped_to_exact_smqproxy_version() -> None:
    assert _uses_mns_gateway(contract(product="SMQProxy", version="2026-04-09"))
    assert not _uses_mns_gateway(contract(product="SMQProxy", version="2026-04-10"))
    assert not _uses_mns_gateway(contract(product="Mns", version="2026-04-09"))


def test_pop_v4_gateway_is_scoped_to_exact_searchplat_version() -> None:
    assert _uses_pop_v4_gateway(contract(product="Searchplat", version="2024-05-29"))
    assert not _uses_pop_v4_gateway(contract(product="Searchplat", version="2024-04-01"))
    assert not _uses_pop_v4_gateway(contract(product="Other", version="2024-05-29"))


def test_oss_gateway_is_scoped_to_exact_hcs_mgw_version() -> None:
    assert _uses_oss_gateway(contract(product="hcs-mgw", version="2024-06-26"))
    assert not _uses_oss_gateway(contract(product="hcs-mgw", version="2024-06-27"))
    assert not _uses_oss_gateway(contract(product="Oss", version="2024-06-26"))


def test_oss_gateway_attaches_official_spi() -> None:
    client = SimpleNamespace(_spi=None, _product_id=None, _endpoint_rule=None)

    _attach_oss_gateway(client)

    assert client._spi.__class__.__module__ == "alibabacloud_gateway_oss.client"
    assert client._product_id == "hcs-mgw"
    assert client._endpoint_rule == ""


def test_pop_v4_gateway_attaches_official_spi() -> None:
    client = SimpleNamespace(_spi=None, _product_id=None, _endpoint_rule=None)

    _attach_pop_v4_gateway(client, product_id="Searchplat")

    assert client._spi.__class__.__module__ == "alibabacloud_gateway_pop.client"
    assert client._product_id == "Searchplat"
    assert client._endpoint_rule == ""


@pytest.mark.asyncio
async def test_mns_gateway_normalizes_namespaced_xml_error() -> None:
    from Tea.exceptions import TeaException

    client = SimpleNamespace(_spi=None)
    _attach_mns_gateway(client)
    context = SimpleNamespace(
        response=SimpleNamespace(
            status_code=404,
            headers={"content-type": "text/xml;charset=UTF-8"},
            body=(
                b'<Error xmlns="http://mns.aliyuncs.com/doc/v1/">'
                b"<Code>QueueNotExist</Code><Message>missing</Message><RequestId>private-id</RequestId></Error>"
            ),
        )
    )

    with pytest.raises(TeaException) as raised:
        await client._spi.modify_response_async(context, SimpleNamespace())

    assert raised.value.code == "QueueNotExist"
    assert raised.value.statusCode == 404
    assert raised.value.data["Code"] == "QueueNotExist"


def budget() -> RetryBudget:
    return RetryBudget(deadline=100.0, clock=lambda: 1.0, random=lambda: 0.0)


def test_tea_client_keeps_ram_role_credential_provider() -> None:
    from iac_code.tools.cloud.aliyun import acs3_transport as transport_module

    client = transport_module._tea_client(endpoint(), ram_role_credential())

    provider = client._credential.cloud_credential.provider
    assert type(provider).__name__ == "RamRoleArnCredentialsProvider"


@pytest.mark.asyncio
async def test_transport_credential_resolver_assumes_ram_role_asynchronously() -> None:
    from iac_code.tools.cloud.aliyun import acs3_transport as transport_module

    configs: list[Any] = []

    class FakeCredentialClient:
        async def get_credential_async(self) -> Any:
            return SimpleNamespace(
                access_key_id="role-ak",
                access_key_secret="role-secret",
                security_token="role-sts",
            )

    def client_factory(config: Any) -> FakeCredentialClient:
        configs.append(config)
        return FakeCredentialClient()

    resolved = await transport_module.resolve_signing_credential(
        ram_role_credential(),
        client_factory=client_factory,
    )

    assert configs[0].type == "ram_role_arn"
    assert configs[0].role_arn == "acs:ram::123456789012:role/restricted"
    assert configs[0].role_session_name == "iac-code-test"
    assert resolved.mode == "StsToken"
    assert (resolved.access_key_id, resolved.access_key_secret, resolved.sts_token) == (
        "role-ak",
        "role-secret",
        "role-sts",
    )


@pytest.mark.asyncio
async def test_acs3_transport_signs_with_assumed_role_credential(monkeypatch: pytest.MonkeyPatch) -> None:
    from iac_code.tools.cloud.aliyun import acs3_transport as transport_module

    sent: list[httpx.Request] = []
    role_credential = AliyunCredential(
        mode="StsToken",
        access_key_id="role-ak",
        access_key_secret="role-secret",
        sts_token="role-sts",
    )

    async def resolve(_credential: AliyunCredential) -> AliyunCredential:
        return role_credential

    async def handler(request: httpx.Request) -> httpx.Response:
        sent.append(request)
        return httpx.Response(200, headers={"content-type": "application/json"}, json={"ok": True})

    monkeypatch.setattr(transport_module, "resolve_signing_credential", resolve, raising=False)
    client = httpx.AsyncClient(transport=httpx.MockTransport(handler), follow_redirects=False)
    try:
        await Acs3StreamingTransport(client=client).execute(
            contract=contract(),
            request=built_request(),
            endpoint=endpoint(),
            credential=ram_role_credential(),
            context=ToolContext(),
            budget=budget(),
        )
    finally:
        await client.aclose()

    authorization = sent[0].headers["authorization"]
    assert "Credential=role-ak" in authorization
    assert "source-ak" not in authorization
    assert sent[0].headers["x-acs-security-token"] == "role-sts"


@pytest.mark.asyncio
async def test_acs3_signing_matches_official_tea_utils_and_preserves_raw_url() -> None:
    transport = Acs3StreamingTransport(
        clock=lambda: datetime(2026, 7, 12, 1, 2, 3, tzinfo=timezone.utc),
        nonce=lambda: "fixed-nonce",
    )

    prepared = transport.prepare_request(contract(), built_request(), endpoint(), credential(token="sts-token"))

    query = dict(built_request().canonical_query)
    official_request = SimpleNamespace(
        method="GET",
        pathname="/instances/a%2Fb",
        query=query,
        headers=dict(prepared.request.headers),
    )
    official_request.headers.pop("authorization")
    expected_auth = Utils.get_authorization(
        official_request, "ACS3-HMAC-SHA256", prepared.payload_hash, "test-ak", "test-secret"
    )
    expected_query = get_canonical_query_string(query).encode("ascii")

    assert prepared.authorization == expected_auth
    assert prepared.request.headers["authorization"] == expected_auth
    assert prepared.request.headers["x-acs-security-token"] == "sts-token"
    assert "x-acs-security-token" in prepared.signed_headers
    assert prepared.request.url.raw_path == b"/instances/a%2Fb?" + expected_query
    assert prepared.request.url.query == expected_query
    assert prepared.payload_hash == Utils.hash(b"", "ACS3-HMAC-SHA256").hex()


def test_anonymous_streaming_transport_preserves_declared_business_authorization() -> None:
    request = replace(
        built_request(),
        headers=MappingProxyType({"accept": "application/json", "authorization": "xx"}),
    )

    prepared = Acs3StreamingTransport().prepare_request(
        contract(auth_type="Anonymous"),
        request,
        endpoint(),
        None,
    )

    assert prepared.request.headers["authorization"] == "xx"
    assert prepared.authorization == ""
    assert prepared.signed_headers == ()


def test_shared_header_filter_is_case_insensitive_and_denies_secrets_after_allowlist() -> None:
    headers = httpx.Headers(
        [
            ("X-Acs-Request-Id", "request-1"),
            ("X-Log-Request-Id", "request-log-1"),
            ("X-Oss-Request-Id", "request-oss-1"),
            ("X-Acs-Error-Code", "Throttling.Api"),
            ("Content-Type", "application/json"),
            ("X-Result-Token", "visible"),
            ("Set-Cookie", "secret"),
            ("Authorization", "secret"),
            ("X-Acs-Security-Token", "secret"),
            ("X-Acs-Signature", "secret"),
            ("X-Credential-Value", "secret"),
            ("X-AK-Secret", "secret"),
            ("X-Auth-Token", "secret"),
            ("X-Authorization-Token", "secret"),
            ("WWW-Authenticate", "secret"),
            ("X-Cookie-Token", "secret"),
            ("X-AuthenticationToken", "secret"),
            ("X-AuthorizationToken", "secret"),
            ("X-SecurityToken", "secret"),
            ("X-AccessKeyId", "secret"),
            ("X-SignatureValue", "secret"),
            ("X-CredentialValue", "secret"),
        ]
    )

    filtered = filter_response_headers(
        headers,
        declared_headers=(
            "X-Result-Token",
            "Set-Cookie",
            "Authorization",
            "X-Acs-Security-Token",
            "X-Acs-Signature",
            "X-Credential-Value",
            "X-AK-Secret",
            "X-Auth-Token",
            "X-Authorization-Token",
            "WWW-Authenticate",
            "X-Cookie-Token",
            "X-AuthenticationToken",
            "X-AuthorizationToken",
            "X-SecurityToken",
            "X-AccessKeyId",
            "X-SignatureValue",
            "X-CredentialValue",
        ),
    )

    assert filtered == {
        "x-acs-request-id": "request-1",
        "x-log-request-id": "request-log-1",
        "x-oss-request-id": "request-oss-1",
        "x-acs-error-code": "Throttling.Api",
        "content-type": "application/json",
        "x-result-token": "visible",
    }


class TrackingStream(httpx.AsyncByteStream):
    def __init__(self, chunks: list[bytes], failure: Exception | None = None) -> None:
        self.chunks = chunks
        self.failure = failure
        self.closed = False

    async def __aiter__(self):
        for chunk in self.chunks:
            yield chunk
        if self.failure is not None:
            raise self.failure

    async def aclose(self) -> None:
        self.closed = True


def client_for(responses: list[tuple[int, list[tuple[str, str]], TrackingStream]]) -> httpx.AsyncClient:
    index = 0

    async def handler(request: httpx.Request) -> httpx.Response:
        nonlocal index
        status, headers, stream = responses[index]
        index += 1
        return httpx.Response(status, headers=headers, stream=stream, request=request)

    return httpx.AsyncClient(transport=httpx.MockTransport(handler), follow_redirects=False)


@pytest.mark.asyncio
async def test_acs3_transport_enforces_retry_budget_deadline_during_send() -> None:
    started = asyncio.Event()
    cleaned = asyncio.Event()

    async def handler(_request: httpx.Request) -> httpx.Response:
        started.set()
        try:
            await asyncio.Event().wait()
        finally:
            cleaned.set()

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler), follow_redirects=False)

    with pytest.raises(RetryExhausted) as raised:
        await asyncio.wait_for(
            Acs3StreamingTransport(client=client).execute(
                contract=contract(),
                request=built_request(),
                endpoint=endpoint(),
                credential=credential(),
                context=ToolContext(),
                budget=RetryBudget(deadline=time.monotonic() + 1.0),
            ),
            timeout=3.0,
        )

    assert started.is_set()
    assert cleaned.is_set()
    assert raised.value.outcome == "read_timeout"
    await client.aclose()


@pytest.mark.asyncio
async def test_acs3_transport_enforces_retry_budget_deadline_during_response_stream() -> None:
    started = asyncio.Event()
    cancelled = asyncio.Event()
    closed = asyncio.Event()

    class BlockingStream(httpx.AsyncByteStream):
        async def __aiter__(self):
            started.set()
            try:
                await asyncio.Event().wait()
                yield b"unreachable"
            finally:
                cancelled.set()

        async def aclose(self) -> None:
            closed.set()

    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            headers={"Content-Type": "application/json"},
            stream=BlockingStream(),
            request=request,
        )

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler), follow_redirects=False)

    with pytest.raises(RetryExhausted) as raised:
        await asyncio.wait_for(
            Acs3StreamingTransport(client=client).execute(
                contract=contract(),
                request=built_request(),
                endpoint=endpoint(),
                credential=credential(),
                context=ToolContext(),
                budget=RetryBudget(deadline=time.monotonic() + 1.0),
            ),
            timeout=3.0,
        )

    assert started.is_set()
    assert cancelled.is_set()
    assert closed.is_set()
    assert raised.value.outcome == "read_timeout"
    await client.aclose()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("mode", "content_type", "payload", "expected"),
    [
        ("json", "application/json", b'{"ok":true}', {"ok": True}),
        (
            "xml",
            "application/xml; charset=iso-8859-1",
            "<Name>caf\xe9</Name>".encode("latin-1"),
            "<Name>caf\xe9</Name>",
        ),
        ("text", "text/plain; charset=utf-8", "hello".encode(), "hello"),
    ],
)
async def test_normalizes_json_xml_and_text_and_closes_response(
    mode: str, content_type: str, payload: bytes, expected: Any
) -> None:
    stream = TrackingStream([payload])
    client = client_for([(200, [("Content-Type", content_type)], stream)])
    transport = Acs3StreamingTransport(client=client)

    result = await transport.execute(
        contract=contract(response_body_type="string" if mode != "json" else "json"),
        request=built_request(mode=mode),
        endpoint=endpoint(),
        credential=credential(),
        context=ToolContext(),
        budget=budget(),
    )

    assert result.body == expected
    assert result.size == len(payload)
    assert stream.closed
    await client.aclose()


@pytest.mark.asyncio
async def test_json_policy_normalizes_success_xml_when_service_ignores_accept_header() -> None:
    payload = (
        b"<?xml version='1.0' encoding='UTF-8'?>"
        b"<DescribeRegionsResponse>"
        b"<RequestId>req-1</RequestId>"
        b"<Regions>"
        b"<Region><RegionId>cn-hangzhou</RegionId><RegionEndpoint>ecd.cn-hangzhou.aliyuncs.com</RegionEndpoint></Region>"
        b"<Region><RegionId>cn-shanghai</RegionId><RegionEndpoint>ecd.cn-shanghai.aliyuncs.com</RegionEndpoint></Region>"
        b"</Regions>"
        b"</DescribeRegionsResponse>"
    )
    stream = TrackingStream([payload])
    client = client_for([(200, [("Content-Type", "text/xml;charset=utf-8")], stream)])
    transport = Acs3StreamingTransport(client=client)

    result = await transport.execute(
        contract=contract(response_body_type="json"),
        request=built_request(mode="json"),
        endpoint=endpoint(),
        credential=credential(),
        context=ToolContext(),
        budget=budget(),
    )

    assert result.body == {
        "RequestId": "req-1",
        "Regions": {
            "Region": [
                {"RegionId": "cn-hangzhou", "RegionEndpoint": "ecd.cn-hangzhou.aliyuncs.com"},
                {"RegionId": "cn-shanghai", "RegionEndpoint": "ecd.cn-shanghai.aliyuncs.com"},
            ]
        },
    }
    assert result.size == len(payload)
    assert stream.closed
    await client.aclose()


@pytest.mark.asyncio
async def test_repeated_cancellation_waits_for_response_close_after_normalization() -> None:
    close_started = asyncio.Event()
    release_close = asyncio.Event()

    class BlockingCloseStream(TrackingStream):
        async def aclose(self) -> None:
            close_started.set()
            await release_close.wait()
            self.closed = True

    stream = BlockingCloseStream([])
    client = client_for([(200, [("Content-Type", "application/json")], stream)])
    propagated_cancellations: list[asyncio.CancelledError] = []

    async def execute() -> NormalizedApiResponse:
        try:
            return await Acs3StreamingTransport(client=client).execute(
                contract=contract(),
                request=built_request(mode="headers_only"),
                endpoint=endpoint(),
                credential=credential(),
                context=ToolContext(),
                budget=budget(),
            )
        except asyncio.CancelledError as error:
            propagated_cancellations.append(error)
            raise

    task = asyncio.create_task(execute())

    try:
        await asyncio.wait_for(close_started.wait(), timeout=1)
        task.cancel("first cancellation")
        await asyncio.sleep(0)
        assert not task.done()
        task.cancel("second cancellation")
        await asyncio.sleep(0)
        assert not task.done()

        release_close.set()
        with pytest.raises(asyncio.CancelledError):
            await task

        assert propagated_cancellations[0].args == ("first cancellation",)
        assert stream.closed is True
    finally:
        release_close.set()
        await asyncio.gather(task, return_exceptions=True)
        await client.aclose()


@pytest.mark.asyncio
async def test_cancellation_chains_late_response_close_error() -> None:
    send_started = asyncio.Event()
    close_error = OSError("response close failed")
    propagated: list[asyncio.CancelledError] = []

    class FailingCloseStream(TrackingStream):
        async def aclose(self) -> None:
            self.closed = True
            raise close_error

    stream = FailingCloseStream([])

    async def handler(request: httpx.Request) -> httpx.Response:
        send_started.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            return httpx.Response(200, stream=stream, request=request)

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler), follow_redirects=False)

    async def execute() -> NormalizedApiResponse:
        try:
            return await Acs3StreamingTransport(client=client).execute(
                contract=contract(),
                request=built_request(),
                endpoint=endpoint(),
                credential=credential(),
                context=ToolContext(),
                budget=budget(),
            )
        except asyncio.CancelledError as error:
            propagated.append(error)
            raise

    task = asyncio.create_task(execute())
    await send_started.wait()
    task.cancel("caller cancellation")

    try:
        with pytest.raises(asyncio.CancelledError):
            await task

        assert propagated[0].args == ("caller cancellation",)
        assert propagated[0].__cause__ is close_error
        assert stream.closed is True
    finally:
        await asyncio.gather(task, return_exceptions=True)
        await client.aclose()


@pytest.mark.asyncio
async def test_normalize_cancellation_remains_primary_when_response_close_is_cancelled_and_fails() -> None:
    normalization_started = asyncio.Event()
    close_started = asyncio.Event()
    release_close = asyncio.Event()
    close_error = OSError("response close failed")

    class BlockingFailingCloseStream(TrackingStream):
        async def aclose(self) -> None:
            close_started.set()
            await release_close.wait()
            self.closed = True
            raise close_error

    stream = BlockingFailingCloseStream([])
    client = client_for([(200, [("Content-Type", "application/json")], stream)])
    transport = Acs3StreamingTransport(client=client)

    async def blocking_normalize(*_args: Any) -> NormalizedApiResponse:
        normalization_started.set()
        await asyncio.Event().wait()
        raise AssertionError("unreachable")

    transport._normalize = blocking_normalize  # ty: ignore[invalid-assignment]
    shared_budget = budget()
    original_run_attempt = shared_budget.run_attempt
    run_attempt_cancellations: list[asyncio.CancelledError] = []

    async def recording_run_attempt(*args: Any, **kwargs: Any) -> Any:
        try:
            return await original_run_attempt(*args, **kwargs)
        except asyncio.CancelledError as error:
            run_attempt_cancellations.append(error)
            raise

    shared_budget.run_attempt = recording_run_attempt  # ty: ignore[invalid-assignment]
    propagated_cancellations: list[asyncio.CancelledError] = []

    async def execute() -> NormalizedApiResponse:
        try:
            return await transport.execute(
                contract=contract(),
                request=built_request(),
                endpoint=endpoint(),
                credential=credential(),
                context=ToolContext(),
                budget=shared_budget,
            )
        except asyncio.CancelledError as error:
            propagated_cancellations.append(error)
            raise

    task = asyncio.create_task(execute())

    try:
        await asyncio.wait_for(normalization_started.wait(), timeout=1)
        task.cancel("normalize cancellation")
        await asyncio.wait_for(close_started.wait(), timeout=1)
        task.cancel("first close cancellation")
        await asyncio.sleep(0)
        task.cancel("second close cancellation")
        await asyncio.sleep(0)
        release_close.set()

        with pytest.raises(asyncio.CancelledError) as caught:
            await task

        assert caught.type is asyncio.CancelledError
        assert len(run_attempt_cancellations) == 1
        assert len(propagated_cancellations) == 1
        propagated = propagated_cancellations[0]
        assert propagated is run_attempt_cancellations[0]
        assert propagated.args == ("normalize cancellation",)
        assert propagated.__cause__ is close_error
        first_close_cancellation = close_error.__cause__
        assert isinstance(first_close_cancellation, asyncio.CancelledError)
        assert first_close_cancellation.args == ("first close cancellation",)
        second_close_cancellation = first_close_cancellation.__cause__
        assert isinstance(second_close_cancellation, asyncio.CancelledError)
        assert second_close_cancellation.args == ("second close cancellation",)
        assert stream.closed is True
    finally:
        release_close.set()
        await asyncio.gather(task, return_exceptions=True)
        await client.aclose()


@pytest.mark.asyncio
async def test_close_error_precedes_retry_wait_and_close_cancellations(monkeypatch: pytest.MonkeyPatch) -> None:
    from iac_code.tools.cloud.aliyun import acs3_transport as transport_module

    normalization_started = asyncio.Event()
    normalization_cancelled = asyncio.Event()
    release_normalization = asyncio.Event()
    close_started = asyncio.Event()
    release_close = asyncio.Event()
    close_error = OSError("response close failed")

    class BlockingFailingCloseStream(TrackingStream):
        async def aclose(self) -> None:
            close_started.set()
            await release_close.wait()
            self.closed = True
            raise close_error

    stream = BlockingFailingCloseStream([])
    client = client_for([(200, [("Content-Type", "application/json")], stream)])
    transport = Acs3StreamingTransport(client=client)

    async def blocking_normalize(*_args: Any) -> NormalizedApiResponse:
        normalization_started.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            normalization_cancelled.set()
            await release_normalization.wait()
            raise
        raise AssertionError("unreachable")

    transport._normalize = blocking_normalize  # ty: ignore[invalid-assignment]
    shared_budget = budget()
    original_run_attempt = shared_budget.run_attempt
    run_attempt_cancellations: list[asyncio.CancelledError] = []
    escaped_retry_tracebacks: list[Any] = []
    post_cleanup_tracebacks: list[Any] = []

    original_close_response = transport_module._close_response

    async def recording_close_response(
        response: httpx.Response,
        *,
        primary: BaseException | None = None,
    ) -> None:
        if primary is not None:
            escaped_retry_tracebacks.append(primary.__traceback__)
        try:
            await original_close_response(response, primary=primary)
        finally:
            if primary is not None:
                post_cleanup_tracebacks.append(primary.__traceback__)

    monkeypatch.setattr(transport_module, "_close_response", recording_close_response)

    async def recording_run_attempt(*args: Any, **kwargs: Any) -> Any:
        try:
            return await original_run_attempt(*args, **kwargs)
        except asyncio.CancelledError as error:
            run_attempt_cancellations.append(error)
            raise

    shared_budget.run_attempt = recording_run_attempt  # ty: ignore[invalid-assignment]
    propagated_cancellations: list[asyncio.CancelledError] = []

    async def execute() -> NormalizedApiResponse:
        try:
            return await transport.execute(
                contract=contract(),
                request=built_request(),
                endpoint=endpoint(),
                credential=credential(),
                context=ToolContext(),
                budget=shared_budget,
            )
        except asyncio.CancelledError as error:
            propagated_cancellations.append(error)
            raise

    task = asyncio.create_task(execute())

    try:
        await asyncio.wait_for(normalization_started.wait(), timeout=1)
        task.cancel("normalize cancellation")
        await asyncio.wait_for(normalization_cancelled.wait(), timeout=1)
        task.cancel("retry wait cancellation")
        await asyncio.sleep(0)
        assert not task.done()
        release_normalization.set()
        await asyncio.wait_for(close_started.wait(), timeout=1)
        task.cancel("close cancellation")
        await asyncio.sleep(0)
        assert not task.done()
        release_close.set()

        with pytest.raises(asyncio.CancelledError) as caught:
            await task

        assert caught.type is asyncio.CancelledError
        assert len(run_attempt_cancellations) == 1
        assert len(propagated_cancellations) == 1
        propagated = propagated_cancellations[0]
        assert propagated is run_attempt_cancellations[0]
        assert propagated.args == ("normalize cancellation",)
        assert propagated.__cause__ is close_error
        retry_wait_cancellation = close_error.__cause__
        assert isinstance(retry_wait_cancellation, asyncio.CancelledError)
        assert retry_wait_cancellation.args == ("retry wait cancellation",)
        close_cancellation = retry_wait_cancellation.__cause__
        assert isinstance(close_cancellation, asyncio.CancelledError)
        assert close_cancellation.args == ("close cancellation",)
        assert close_cancellation.__cause__ is None

        assert len(escaped_retry_tracebacks) == 1
        assert len(post_cleanup_tracebacks) == 1
        assert escaped_retry_tracebacks[0] is not None
        assert post_cleanup_tracebacks[0] is escaped_retry_tracebacks[0]

        cause: BaseException | None = propagated
        cause_ids: set[int] = set()
        while cause is not None:
            assert id(cause) not in cause_ids
            cause_ids.add(id(cause))
            cause = cause.__cause__
        assert stream.closed is True
    finally:
        release_normalization.set()
        release_close.set()
        await asyncio.gather(task, return_exceptions=True)
        await client.aclose()


@pytest.mark.asyncio
async def test_close_response_keeps_its_first_cancellation_primary_and_chains_late_close_error() -> None:
    close_started = asyncio.Event()
    release_close = asyncio.Event()
    close_error = OSError("response close failed")

    class BlockingFailingCloseStream(TrackingStream):
        async def aclose(self) -> None:
            close_started.set()
            await release_close.wait()
            self.closed = True
            raise close_error

    stream = BlockingFailingCloseStream([])
    response = httpx.Response(200, stream=stream, request=httpx.Request("GET", "https://example.com"))
    propagated_cancellations: list[asyncio.CancelledError] = []

    async def close_response() -> None:
        try:
            await _close_response(response)
        except asyncio.CancelledError as error:
            propagated_cancellations.append(error)
            raise

    task = asyncio.create_task(close_response())

    try:
        await asyncio.wait_for(close_started.wait(), timeout=1)
        task.cancel("first close cancellation")
        await asyncio.sleep(0)
        task.cancel("second close cancellation")
        await asyncio.sleep(0)
        release_close.set()

        with pytest.raises(asyncio.CancelledError) as caught:
            await task

        assert caught.type is asyncio.CancelledError
        assert len(propagated_cancellations) == 1
        propagated = propagated_cancellations[0]
        assert propagated.args == ("first close cancellation",)
        assert propagated.__cause__ is close_error
        second_close_cancellation = close_error.__cause__
        assert isinstance(second_close_cancellation, asyncio.CancelledError)
        assert second_close_cancellation.args == ("second close cancellation",)
        assert stream.closed is True
    finally:
        release_close.set()
        await asyncio.gather(task, return_exceptions=True)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("mode", "content_type", "payload", "expected"),
    [
        ("json", "application/json", b'{"ok":true}', {"ok": True}),
        (
            "text",
            "text/plain; charset=utf-8",
            "compressed caf\N{LATIN SMALL LETTER E WITH ACUTE}".encode(),
            "compressed caf\N{LATIN SMALL LETTER E WITH ACUTE}",
        ),
    ],
)
async def test_normalizes_compressed_json_and_text(mode: str, content_type: str, payload: bytes, expected: Any) -> None:
    compressed = gzip.compress(payload)
    stream = TrackingStream([compressed[:10], compressed[10:]])
    client = client_for([(200, [("Content-Type", content_type), ("Content-Encoding", "gzip")], stream)])

    result = await Acs3StreamingTransport(client=client).execute(
        contract=contract(response_body_type="json" if mode == "json" else "string"),
        request=built_request(mode=mode),
        endpoint=endpoint(),
        credential=credential(),
        context=ToolContext(),
        budget=budget(),
    )

    assert result.body == expected
    assert result.size == len(payload)
    assert result.content_encoding == "gzip"
    assert stream.closed
    await client.aclose()


@pytest.mark.asyncio
async def test_acs3_invalid_success_json_is_a_typed_target_response_failure() -> None:
    stream = TrackingStream([b"{not-json"])
    client = client_for([(200, [("Content-Type", "application/json")], stream)])

    with pytest.raises(RuntimeError, match="^invalid_response$"):
        await Acs3StreamingTransport(client=client).execute(
            contract=contract(),
            request=built_request(),
            endpoint=endpoint(),
            credential=credential(),
            context=ToolContext(),
            budget=budget(),
        )

    assert stream.closed
    await client.aclose()


@pytest.mark.asyncio
async def test_acs3_success_xml_security_failure_is_a_typed_target_response_failure() -> None:
    payload = b"<!DOCTYPE foo [<!ENTITY xxe SYSTEM 'file:///etc/passwd'>]><foo>&xxe;</foo>"
    stream = TrackingStream([payload])
    client = client_for([(200, [("Content-Type", "text/xml")], stream)])

    with pytest.raises(RuntimeError, match="^invalid_response$"):
        await Acs3StreamingTransport(client=client).execute(
            contract=contract(response_body_type="json"),
            request=built_request(mode="json"),
            endpoint=endpoint(),
            credential=credential(),
            context=ToolContext(),
            budget=budget(),
        )

    assert stream.closed
    await client.aclose()


def test_tea_invalid_success_json_is_a_typed_target_response_failure() -> None:
    from iac_code.tools.cloud.aliyun.acs3_transport import _parse_tea_success

    with pytest.raises(RuntimeError, match="^invalid_response$"):
        _parse_tea_success(bytearray(b"{not-json"), "json")


def test_tea_success_xml_security_failure_is_a_typed_target_response_failure() -> None:
    from iac_code.tools.cloud.aliyun.acs3_transport import _parse_tea_success

    payload = bytearray(b"<!DOCTYPE foo [<!ENTITY xxe SYSTEM 'file:///etc/passwd'>]><foo>&xxe;</foo>")

    with pytest.raises(RuntimeError, match="^invalid_response$"):
        _parse_tea_success(payload, "json", "text/xml")


@pytest.mark.asyncio
@_UPSTREAM_OPENAPI_UTCNOW_WARNING
async def test_tea_json_policy_normalizes_success_xml_when_service_ignores_accept_header() -> None:
    payload = (
        b"<?xml version='1.0' encoding='UTF-8'?>"
        b"<DescribeRegionsResponse>"
        b"<RequestId>req-1</RequestId>"
        b"<Regions>"
        b"<Region><RegionId>cn-hangzhou</RegionId><RegionEndpoint>ecd.cn-hangzhou.aliyuncs.com</RegionEndpoint></Region>"
        b"</Regions>"
        b"</DescribeRegionsResponse>"
    )
    content = TrackingTeaContent([payload])
    response = FakeTeaStreamingResponse(200, {"Content-Type": "text/xml;charset=utf-8"}, content)
    session = FakeTeaStreamingSession(response)

    result = await TeaTransportAdapter(session_factory=lambda **_kwargs: session).execute(
        contract=replace(contract(), transport="tea", response_body_type="json"),
        request=built_request(mode="json"),
        endpoint=endpoint(),
        credential=credential(),
        context=ToolContext(),
        budget=budget(),
    )

    assert result.body == {
        "RequestId": "req-1",
        "Regions": {"Region": {"RegionId": "cn-hangzhou", "RegionEndpoint": "ecd.cn-hangzhou.aliyuncs.com"}},
    }
    assert result.content_type == "text/xml;charset=utf-8"
    assert result.size == len(payload)
    assert response.closed
    assert session.closed


@pytest.mark.asyncio
async def test_acs3_invalid_success_text_is_a_typed_target_response_failure() -> None:
    stream = TrackingStream([b"\xff"])
    client = client_for([(200, [("Content-Type", "text/plain; charset=utf-8")], stream)])

    with pytest.raises(RuntimeError, match="^invalid_response$"):
        await Acs3StreamingTransport(client=client).execute(
            contract=contract(response_body_type="string"),
            request=built_request(mode="text"),
            endpoint=endpoint(),
            credential=credential(),
            context=ToolContext(),
            budget=budget(),
        )

    assert stream.closed
    await client.aclose()


@pytest.mark.asyncio
async def test_invalid_text_error_body_preserves_http_status_with_sanitized_body() -> None:
    stream = TrackingStream([b"\xffCUSTOMER_SECRET"])
    client = client_for([(400, [("Content-Type", "text/plain; charset=utf-8")], stream)])

    result = await Acs3StreamingTransport(client=client).execute(
        contract=contract(),
        request=built_request(),
        endpoint=endpoint(),
        credential=credential(),
        context=ToolContext(),
        budget=budget(),
    )

    assert result.status == 400
    assert result.body == {"error": "invalid_text_error_response"}
    assert "CUSTOMER_SECRET" not in str(result.body)
    assert stream.closed
    await client.aclose()


@pytest.mark.asyncio
@pytest.mark.parametrize(("method", "status"), [("HEAD", 200), ("GET", 204)])
async def test_head_and_204_never_parse_body(method: str, status: int) -> None:
    stream = TrackingStream([b"not-json"])
    client = client_for([(status, [("Content-Type", "application/json")], stream)])
    transport = Acs3StreamingTransport(client=client)

    result = await transport.execute(
        contract=contract(method=method),
        request=replace(built_request(), method=method),
        endpoint=endpoint(),
        credential=credential(),
        context=ToolContext(),
        budget=budget(),
    )

    assert result.body is None
    assert result.size == 0
    assert stream.closed
    await client.aclose()


@pytest.mark.asyncio
@pytest.mark.parametrize("characters", [48_000, 48_001])
async def test_text_stays_in_memory_across_the_previous_externalization_boundary(characters: int) -> None:
    payload = ("x" * characters).encode()
    stream = TrackingStream([payload[:20_000], payload[20_000:]])
    client = client_for([(200, [("Content-Type", "text/plain; charset=utf-8")], stream)])

    result = await Acs3StreamingTransport(client=client).execute(
        contract=contract(response_body_type="string"),
        request=built_request(mode="text"),
        endpoint=endpoint(),
        credential=credential(),
        context=ToolContext(),
        budget=budget(),
    )

    assert result.body == "x" * characters
    assert result.size == len(payload)
    assert stream.closed
    await client.aclose()


@pytest.mark.asyncio
async def test_non_utf8_text_is_decoded_in_memory() -> None:
    text = "\N{LATIN SMALL LETTER E WITH ACUTE}" * 48_001
    payload = text.encode("iso-8859-1")
    stream = TrackingStream([payload[:20_000], payload[20_000:]])
    client = client_for([(200, [("Content-Type", "text/plain; charset=iso-8859-1")], stream)])

    result = await Acs3StreamingTransport(client=client).execute(
        contract=contract(response_body_type="string"),
        request=built_request(mode="text"),
        endpoint=endpoint(),
        credential=credential(),
        context=ToolContext(),
        budget=budget(),
    )

    assert result.body == text
    assert result.size == len(payload)
    assert stream.closed
    await client.aclose()


@pytest.mark.asyncio
async def test_compressed_text_is_decoded_in_memory() -> None:
    payload = ("x" * 48_001).encode()
    compressed = gzip.compress(payload)
    stream = TrackingStream([compressed[:10], compressed[10:]])
    client = client_for([(200, [("Content-Type", "text/plain; charset=utf-8"), ("Content-Encoding", "gzip")], stream)])

    result = await Acs3StreamingTransport(client=client).execute(
        contract=contract(response_body_type="string"),
        request=built_request(mode="text"),
        endpoint=endpoint(),
        credential=credential(),
        context=ToolContext(),
        budget=budget(),
    )

    assert result.body == payload.decode()
    assert result.content_encoding == "gzip"
    assert result.size == len(payload)
    await client.aclose()


@pytest.mark.asyncio
async def test_decoded_limit_stops_compressed_text_before_tail() -> None:
    class NoTailReadStream(TrackingStream):
        def __init__(self) -> None:
            super().__init__([])
            self.tail_requested = False

        async def __aiter__(self):
            yield gzip.compress(b"x" * 65_536)
            self.tail_requested = True
            raise AssertionError("decoded overflow consumed the tail")

    stream = NoTailReadStream()
    client = client_for([(200, [("Content-Type", "text/plain; charset=utf-8"), ("Content-Encoding", "gzip")], stream)])

    with pytest.raises(ResponseTooLarge, match="response_too_large"):
        await Acs3StreamingTransport(client=client).execute(
            contract=contract(response_body_type="string"),
            request=built_request(mode="text", max_bytes=40_000),
            endpoint=endpoint(),
            credential=credential(),
            context=ToolContext(),
            budget=budget(),
        )

    assert not stream.tail_requested
    assert stream.closed
    await client.aclose()


@pytest.mark.asyncio
async def test_binary_returns_bounded_base64_body() -> None:
    payload = b"raw-binary-bytes"
    stream = TrackingStream([payload[:4], payload[4:]])
    client = client_for([(200, [("Content-Type", "application/octet-stream")], stream)])

    result = await Acs3StreamingTransport(client=client).execute(
        contract=contract(response_body_type="binary"),
        request=built_request(mode="binary"),
        endpoint=endpoint(),
        credential=credential(),
        context=ToolContext(),
        budget=budget(),
    )

    assert result.body == {"encoding": "base64", "data": "cmF3LWJpbmFyeS1ieXRlcw=="}
    assert result.size == len(payload)
    assert stream.closed
    await client.aclose()


@pytest.mark.asyncio
async def test_zero_length_binary_returns_empty_base64_body() -> None:
    stream = TrackingStream([])
    client = client_for([(200, [("Content-Type", "application/octet-stream"), ("Content-Length", "0")], stream)])

    result = await Acs3StreamingTransport(client=client).execute(
        contract=contract(response_body_type="binary"),
        request=built_request(mode="binary"),
        endpoint=endpoint(),
        credential=credential(),
        context=ToolContext(),
        budget=budget(),
    )

    assert result.size == 0
    assert result.body == {"encoding": "base64", "data": ""}
    assert stream.closed
    await client.aclose()


@pytest.mark.asyncio
@pytest.mark.parametrize("content_length", ["104857601", "not-a-number", "10, 11"])
async def test_content_length_preflight_trusts_only_valid_non_conflicting_decimal(
    content_length: str,
) -> None:
    stream = TrackingStream([b"small"])
    client = client_for(
        [(200, [("Content-Type", "application/octet-stream"), ("Content-Length", content_length)], stream)]
    )
    transport = Acs3StreamingTransport(client=client)

    if content_length.isdecimal():
        with pytest.raises(ResponseTooLarge):
            await transport.execute(
                contract=contract(response_body_type="binary"),
                request=built_request(mode="binary"),
                endpoint=endpoint(),
                credential=credential(),
                context=ToolContext(),
                budget=budget(),
            )
    else:
        result = await transport.execute(
            contract=contract(response_body_type="binary"),
            request=built_request(mode="binary"),
            endpoint=endpoint(),
            credential=credential(),
            context=ToolContext(),
            budget=budget(),
        )
        assert result.size == 5
    assert stream.closed
    await client.aclose()


@pytest.mark.asyncio
@pytest.mark.parametrize("max_bytes", [10, 16 * 1024**2])
async def test_actual_stream_limit_honors_custom_and_hard_max(max_bytes: int) -> None:
    payload = b"x" * 11 if max_bytes == 10 else b"ok"
    stream = TrackingStream([payload])
    client = client_for([(200, [("Content-Type", "application/octet-stream")], stream)])

    if max_bytes == 10:
        with pytest.raises(ResponseTooLarge):
            await Acs3StreamingTransport(client=client).execute(
                contract=contract(response_body_type="binary"),
                request=built_request(mode="binary", max_bytes=max_bytes),
                endpoint=endpoint(),
                credential=credential(),
                context=ToolContext(),
                budget=budget(),
            )
    else:
        result = await Acs3StreamingTransport(client=client).execute(
            contract=contract(response_body_type="binary"),
            request=built_request(mode="binary", max_bytes=max_bytes),
            endpoint=endpoint(),
            credential=credential(),
            context=ToolContext(),
            budget=budget(),
        )
        assert result.size == 2
    await client.aclose()


@pytest.mark.asyncio
async def test_error_body_has_one_mib_hard_limit_and_closes() -> None:
    stream = TrackingStream([b"x" * (1024**2 + 1)])
    client = client_for([(500, [("Content-Type", "text/plain")], stream)])

    with pytest.raises(ResponseTooLarge, match="error_response_too_large"):
        await Acs3StreamingTransport(client=client).execute(
            contract=contract(),
            request=built_request(),
            endpoint=endpoint(),
            credential=credential(),
            context=ToolContext(),
            budget=budget(),
        )
    assert stream.closed
    await client.aclose()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("error_type", "expected_outcome", "expected_reason"),
    [
        (httpx.PoolTimeout, "pre_connect_failure", RetryReason.POOL_UNAVAILABLE),
        (httpx.ReadError, "unknown_after_transport_error", RetryReason.READ_ERROR),
        (httpx.WriteError, "unknown_after_transport_error", None),
    ],
)
async def test_acs3_execute_surfaces_write_transport_outcome(
    error_type: type[httpx.TransportError], expected_outcome: str, expected_reason: RetryReason | None
) -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        raise error_type("transport failed", request=request)

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler), follow_redirects=False)
    shared_budget = budget()

    with pytest.raises(TransportFailure) as caught:
        await Acs3StreamingTransport(client=client).execute(
            contract=contract(operation_type="write", method="POST", request_body_type="json"),
            request=replace(built_request(body=b"{}"), method="POST"),
            endpoint=endpoint(),
            credential=credential(),
            context=ToolContext(),
            budget=shared_budget,
        )

    assert caught.value.outcome == expected_outcome
    assert caught.value.reason is expected_reason
    assert type(caught.value.__cause__) is error_type
    assert shared_budget.attempts == 1
    await client.aclose()


@pytest.mark.asyncio
async def test_retry_reuses_budget_and_returns_only_final_complete_body() -> None:
    failed = TrackingStream([b"partial"], httpx.ReadError("stream failed"))
    complete = TrackingStream([b"complete"])
    client = client_for(
        [
            (200, [("Content-Type", "application/octet-stream")], failed),
            (200, [("Content-Type", "application/octet-stream")], complete),
        ]
    )
    shared_budget = budget()
    sleeps: list[float] = []

    result = await Acs3StreamingTransport(client=client, sleep=lambda delay: _record(sleeps, delay)).execute(
        contract=contract(response_body_type="binary"),
        request=built_request(mode="binary"),
        endpoint=endpoint(),
        credential=credential(),
        context=ToolContext(),
        budget=shared_budget,
    )

    assert shared_budget.attempts == 2
    assert sleeps == [0.2]
    assert result.body == {"encoding": "base64", "data": "Y29tcGxldGU="}
    assert failed.closed and complete.closed
    await client.aclose()


@pytest.mark.asyncio
async def test_retryable_stream_failure_closes_response_before_blocking_backoff() -> None:
    failed = TrackingStream([b"partial"], httpx.ReadError("stream failed"))
    complete = TrackingStream([b"complete"])
    client = client_for(
        [
            (200, [("Content-Type", "application/octet-stream")], failed),
            (200, [("Content-Type", "application/octet-stream")], complete),
        ]
    )
    backoff_started = asyncio.Event()
    release_backoff = asyncio.Event()

    async def blocking_sleep(_delay: float) -> None:
        backoff_started.set()
        await release_backoff.wait()

    task = asyncio.create_task(
        Acs3StreamingTransport(client=client, sleep=blocking_sleep).execute(
            contract=contract(response_body_type="binary"),
            request=built_request(mode="binary"),
            endpoint=endpoint(),
            credential=credential(),
            context=ToolContext(),
            budget=budget(),
        )
    )

    await backoff_started.wait()
    assert failed.closed
    assert not task.done()
    release_backoff.set()
    result = await task

    assert result.body == {"encoding": "base64", "data": "Y29tcGxldGU="}
    assert complete.closed
    await client.aclose()


@pytest.mark.asyncio
async def test_connect_then_stream_failure_share_budget_and_return_final_body() -> None:
    calls = 0
    failed = TrackingStream([b"partial"], httpx.ReadError("stream failed"))
    complete = TrackingStream([b"complete"])

    async def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        if calls == 1:
            raise httpx.ConnectError("connect failed", request=request)
        stream = failed if calls == 2 else complete
        return httpx.Response(
            200,
            headers={"Content-Type": "application/octet-stream"},
            stream=stream,
            request=request,
        )

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler), follow_redirects=False)
    shared_budget = budget()
    sleeps: list[float] = []

    result = await Acs3StreamingTransport(client=client, sleep=lambda delay: _record(sleeps, delay)).execute(
        contract=contract(response_body_type="binary"),
        request=built_request(mode="binary"),
        endpoint=endpoint(),
        credential=credential(),
        context=ToolContext(),
        budget=shared_budget,
    )

    assert shared_budget.attempts == 3
    assert sleeps == [0.2, 0.8]
    assert result.body == {"encoding": "base64", "data": "Y29tcGxldGU="}
    await client.aclose()


async def _record(values: list[float], value: float) -> None:
    values.append(value)


@pytest.mark.asyncio
async def test_cancellation_closes_response() -> None:
    gate = asyncio.Event()
    started = asyncio.Event()

    class BlockingStream(TrackingStream):
        async def __aiter__(self):
            started.set()
            yield b"partial"
            await gate.wait()

    stream = BlockingStream([])
    client = client_for([(200, [("Content-Type", "application/octet-stream")], stream)])
    task = asyncio.create_task(
        Acs3StreamingTransport(client=client).execute(
            contract=contract(response_body_type="binary"),
            request=built_request(mode="binary"),
            endpoint=endpoint(),
            credential=credential(),
            context=ToolContext(),
            budget=budget(),
        )
    )
    await asyncio.wait_for(started.wait(), timeout=1)
    task.cancel()

    with pytest.raises(asyncio.CancelledError):
        await task
    assert stream.closed
    await client.aclose()


class TrackingTeaContent:
    def __init__(self, chunks: list[bytes], *, raw_sizes: list[int] | None = None) -> None:
        self.chunks = chunks
        self.raw_sizes = raw_sizes or [len(chunk) for chunk in chunks]
        self.total_raw_bytes = 0
        self.iterations = 0

    async def iter_chunked(self, _chunk_size: int):
        self.iterations += 1
        for chunk, raw_size in zip(self.chunks, self.raw_sizes, strict=True):
            self.total_raw_bytes += raw_size
            yield chunk


class FakeTeaStreamingResponse:
    def __init__(self, status: int, headers: dict[str, str], content: TrackingTeaContent) -> None:
        self.status = status
        self.headers = headers
        self.content = content
        self.reason = "test response"
        self.closed = False

    async def __aenter__(self) -> FakeTeaStreamingResponse:
        return self

    async def __aexit__(self, *_args: Any) -> None:
        self.closed = True


class FakeTeaStreamingSession:
    def __init__(self, response: FakeTeaStreamingResponse) -> None:
        self.response = response
        self.closed = False
        self.requested = False
        self.requests: list[tuple[tuple[Any, ...], dict[str, Any]]] = []

    async def __aenter__(self) -> FakeTeaStreamingSession:
        return self

    async def __aexit__(self, *_args: Any) -> None:
        self.closed = True

    def request(self, *args: Any, **kwargs: Any) -> FakeTeaStreamingResponse:
        self.requested = True
        self.requests.append((args, kwargs))
        return self.response


async def _execute_shared_error_normalization(
    transport_name: str,
    *,
    content_type: str,
    payload: bytes,
) -> NormalizedApiResponse:
    if transport_name == "acs3":
        stream = TrackingStream([payload])
        client = client_for([(400, [("Content-Type", content_type)], stream)])
        try:
            result = await Acs3StreamingTransport(client=client).execute(
                contract=contract(),
                request=built_request(),
                endpoint=endpoint(),
                credential=credential(),
                context=ToolContext(),
                budget=budget(),
            )
        finally:
            await client.aclose()
        assert stream.closed
        return result

    content = TrackingTeaContent([payload])
    response = FakeTeaStreamingResponse(400, {"Content-Type": content_type}, content)
    session = FakeTeaStreamingSession(response)
    result = await TeaTransportAdapter(session_factory=lambda **_kwargs: session).execute(
        contract=replace(contract(), transport="tea"),
        request=built_request(),
        endpoint=endpoint(),
        credential=credential(),
        context=ToolContext(),
        budget=budget(),
    )
    assert response.closed
    assert session.closed
    return result


@pytest.mark.asyncio
@_UPSTREAM_OPENAPI_UTCNOW_WARNING
@pytest.mark.parametrize("transport_name", ["acs3", "tea"])
@pytest.mark.parametrize(
    ("content_type", "payload", "expected_marker", "forbidden_marker"),
    [
        (
            "application/json; bad",
            b'{"secret":"json-customer-secret"}',
            "unrecognized_error_response",
            "json-customer-secret",
        ),
        (
            'text/plain; note="x; charset=utf-16"',
            b"plain-text-marker",
            "plain-text-marker",
            None,
        ),
        (
            "text/plain; charset=not-a-real-codec",
            b"charset-customer-secret",
            "invalid_text_error_response",
            "charset-customer-secret",
        ),
    ],
)
async def test_acs3_and_tea_share_strict_error_content_type_normalization(
    transport_name: str,
    content_type: str,
    payload: bytes,
    expected_marker: str,
    forbidden_marker: str | None,
) -> None:
    result = await _execute_shared_error_normalization(
        transport_name,
        content_type=content_type,
        payload=payload,
    )

    serialized_body = str(result.body)
    assert result.status == 400
    assert expected_marker in serialized_body
    if forbidden_marker is not None:
        assert forbidden_marker not in serialized_body


class _DeclaredHeaderOpenMeta:
    def __init__(self, raw: dict[str, Any]) -> None:
        self.raw = raw

    async def get_product(self, product: str) -> MetadataFetch[Any]:
        return MetadataFetch(
            value=ProductMetadata(product, "2014-05-26", ("2014-05-26",), None),
            source="fresh",
            error=None,
        )

    async def get_api(self, product: str, version: str, action: str) -> MetadataFetch[Any]:
        del product, version, action
        return MetadataFetch(value=normalize_api_metadata(self.raw), source="fresh", error=None)


async def _resolved_declared_header_request(*, streaming: bool) -> tuple[CanonicalWireContract, BuiltApiRequest]:
    action = "GetText" if streaming else "DescribeInstances"
    produces = ["text/plain"] if streaming else ["application/json"]
    raw = {
        "product": "Ecs",
        "version": "2014-05-26",
        "action": action,
        "style": "ROA" if streaming else "RPC",
        "methods": ["GET" if streaming else "POST"],
        "path": "/text" if streaming else "/",
        "schemes": ["HTTPS"],
        "produces": produces,
        "security": [{"AK": []}],
        "parameters": [],
        "responses": {
            "200": {
                "headers": {
                    "X-Contract-Value": {"schema": {"type": "string"}},
                    "Authorization": {"schema": {"type": "string"}},
                }
            }
        },
    }
    resolved = await ApiContractResolver(_DeclaredHeaderOpenMeta(raw)).resolve(
        ApiCallShape(
            product="Ecs",
            version="2014-05-26",
            action=action,
            region_id="cn-hangzhou",
            explicit_overrides=(),
            parameter_names_by_location=MappingProxyType({}),
            body_source="none",
        ),
        allow_fallback=False,
    )
    return resolved, await RequestBuilder().build(resolved, {})


@pytest.mark.asyncio
@_UPSTREAM_OPENAPI_UTCNOW_WARNING
async def test_resolved_declared_headers_flow_through_tea_and_sensitive_denylist() -> None:
    resolved, request = await _resolved_declared_header_request(streaming=False)
    content = TrackingTeaContent([b'{"ok":true}'])
    response = FakeTeaStreamingResponse(
        200,
        {
            "Content-Type": "application/json",
            "X-Contract-Value": "visible",
            "Authorization": "secret",
        },
        content,
    )
    session = FakeTeaStreamingSession(response)

    result = await TeaTransportAdapter(session_factory=lambda **_kwargs: session).execute(
        contract=resolved,
        request=request,
        endpoint=endpoint(),
        credential=credential(),
        context=ToolContext(),
        budget=budget(),
    )

    assert resolved.transport == "tea"
    assert request.response_policy.declared_headers == ("authorization", "x-contract-value")
    assert result.headers["x-contract-value"] == "visible"
    assert "authorization" not in result.headers


@pytest.mark.asyncio
async def test_resolved_declared_headers_flow_through_acs3_and_sensitive_denylist() -> None:
    resolved, request = await _resolved_declared_header_request(streaming=True)
    stream = TrackingStream([b"hello"])
    client = client_for(
        [
            (
                200,
                [
                    ("Content-Type", "text/plain"),
                    ("X-Contract-Value", "visible"),
                    ("Authorization", "secret"),
                ],
                stream,
            )
        ]
    )

    result = await Acs3StreamingTransport(client=client).execute(
        contract=resolved,
        request=request,
        endpoint=endpoint(),
        credential=credential(),
        context=ToolContext(),
        budget=budget(),
    )

    assert resolved.transport == "acs3_streaming"
    assert request.response_policy.declared_headers == ("authorization", "x-contract-value")
    assert result.headers["x-contract-value"] == "visible"
    assert "authorization" not in result.headers
    await client.aclose()


@pytest.mark.asyncio
@_UPSTREAM_OPENAPI_UTCNOW_WARNING
@pytest.mark.parametrize(
    "content_type",
    ["application/vnd.api+json; charset=utf-8", "application/merge-patch+json"],
)
async def test_tea_preserves_approved_vendor_json_content_type_on_signed_wire(
    content_type: str,
) -> None:
    content = TrackingTeaContent([b'{"ok":true}'])
    response = FakeTeaStreamingResponse(200, {"Content-Type": "application/json"}, content)
    session = FakeTeaStreamingSession(response)
    request = replace(
        built_request(body=b'{"name":"demo"}'),
        method="POST",
        headers=MappingProxyType({"accept": "application/json", "content-type": content_type}),
    )

    result = await TeaTransportAdapter(session_factory=lambda **_kwargs: session).execute(
        contract=replace(
            contract(),
            method="POST",
            transport="tea",
            request_body_type="json",
        ),
        request=request,
        endpoint=endpoint(),
        credential=credential(),
        context=ToolContext(),
        budget=budget(),
    )

    assert result.status == 200
    _args, kwargs = session.requests[0]
    assert kwargs["headers"]["content-type"] == content_type
    authorization = next(value for name, value in kwargs["headers"].items() if name.casefold() == "authorization")
    assert "content-type" in authorization


@pytest.mark.asyncio
@_UPSTREAM_OPENAPI_UTCNOW_WARNING
@pytest.mark.parametrize(
    ("status", "content_length", "expected_message"),
    [
        (200, 11, "response_too_large"),
        (302, 1024**2 + 1, "error_response_too_large"),
        (500, 1024**2 + 1, "error_response_too_large"),
    ],
)
async def test_tea_streaming_preflights_content_length_before_body_consumption(
    status: int, content_length: int, expected_message: str
) -> None:
    content = TrackingTeaContent([b'{"ok":true}'])
    response = FakeTeaStreamingResponse(
        status,
        {"Content-Type": "application/json", "Content-Length": str(content_length)},
        content,
    )
    session = FakeTeaStreamingSession(response)

    with pytest.raises(ResponseTooLarge, match=expected_message):
        await TeaTransportAdapter(session_factory=lambda **_kwargs: session).execute(
            contract=replace(contract(), transport="tea"),
            request=built_request(max_bytes=10),
            endpoint=endpoint(),
            credential=credential(),
            context=ToolContext(),
            budget=budget(),
        )

    assert session.requested
    assert content.iterations == 0
    assert response.closed
    assert session.closed


@pytest.mark.asyncio
async def test_acs3_redirect_response_uses_error_limit_and_returns_error_body() -> None:
    payload = b'{"Code":"Redirect","Message":"redirects are disabled"}'
    stream = TrackingStream([payload])
    client = client_for([(302, [("Content-Type", "application/json")], stream)])

    result = await Acs3StreamingTransport(client=client).execute(
        contract=contract(response_body_type="binary"),
        request=built_request(mode="binary"),
        endpoint=endpoint(),
        credential=credential(),
        context=ToolContext(),
        budget=budget(),
    )

    assert result.status == 302
    assert result.body == {"Code": "Redirect", "Message": "redirects are disabled"}
    await client.aclose()


@pytest.mark.asyncio
@_UPSTREAM_OPENAPI_UTCNOW_WARNING
@pytest.mark.parametrize("status", [200, 500])
@pytest.mark.parametrize("counter", ["raw", "decoded"])
async def test_tea_streaming_enforces_actual_raw_and_decoded_limits_while_consuming(status: int, counter: str) -> None:
    limit = 10 if status == 200 else 1024**2
    chunks = [b"{}"] if counter == "raw" else [b"x" * (limit + 1)]
    raw_sizes = [limit + 1] if counter == "raw" else [1]
    content = TrackingTeaContent(chunks, raw_sizes=raw_sizes)
    response = FakeTeaStreamingResponse(status, {"Content-Type": "application/json"}, content)
    session = FakeTeaStreamingSession(response)
    expected_message = "error_response_too_large" if status >= 400 else "response_too_large"

    with pytest.raises(ResponseTooLarge, match=expected_message):
        await TeaTransportAdapter(session_factory=lambda **_kwargs: session).execute(
            contract=replace(contract(), transport="tea"),
            request=built_request(max_bytes=10),
            endpoint=endpoint(),
            credential=credential(),
            context=ToolContext(),
            budget=budget(),
        )

    assert content.iterations == 1
    assert response.closed
    assert session.closed


@pytest.mark.asyncio
@_UPSTREAM_OPENAPI_UTCNOW_WARNING
@pytest.mark.parametrize(
    ("status", "payload", "expected_body"),
    [
        (200, b'{  "ok" : true }\n', {"ok": True}),
        (
            400,
            b'{  "Code" : "InvalidParameter", "Message" : "bad" }\n',
            {"Code": "InvalidParameter", "Message": "bad", "statusCode": 400},
        ),
    ],
)
async def test_tea_streaming_reports_consumed_payload_size_without_json_reserialization(
    status: int, payload: bytes, expected_body: dict[str, Any]
) -> None:
    content = TrackingTeaContent([payload])
    response = FakeTeaStreamingResponse(status, {"Content-Type": "application/json"}, content)
    session = FakeTeaStreamingSession(response)

    result = await TeaTransportAdapter(session_factory=lambda **_kwargs: session).execute(
        contract=replace(contract(), transport="tea"),
        request=built_request(),
        endpoint=endpoint(),
        credential=credential(),
        context=ToolContext(),
        budget=budget(),
    )

    assert result.status == status
    assert result.body == expected_body
    assert result.size == len(payload)
    assert content.iterations == 1
    assert response.closed
    assert session.closed


@pytest.mark.asyncio
async def test_tea_adapter_rejects_eager_sdk_client_before_acquiring_budget() -> None:
    class EagerOnlyTeaClient:
        def __init__(self) -> None:
            self.called = False

        async def call_api_async(self, params: Any, request: Any, runtime: Any) -> dict[str, Any]:
            self.called = True
            return {
                "statusCode": 200,
                "headers": {},
                "body": {"ok": True},
                "_iac_response_body_size": 11,
            }

    fake = EagerOnlyTeaClient()
    shared_budget = budget()

    with pytest.raises(RuntimeError, match="tea_streaming_client_required"):
        await TeaTransportAdapter(client_factory=lambda _endpoint, _credential: fake).execute(
            contract=replace(contract(), transport="tea"),
            request=built_request(),
            endpoint=endpoint(),
            credential=credential(),
            context=ToolContext(),
            budget=shared_budget,
        )

    assert not fake.called
    assert shared_budget.attempts == 0


@pytest.mark.asyncio
async def test_tea_adapter_disables_nested_retry() -> None:
    class FakeTeaClient:
        def __init__(self) -> None:
            self.runtime: Any = None

        async def call_api_streaming_async(
            self, params: Any, request: Any, runtime: Any, **_kwargs: Any
        ) -> dict[str, Any]:
            self.runtime = runtime
            return {
                "statusCode": 200,
                "headers": {"Content-Type": "application/json", "Set-Cookie": "secret"},
                "body": {"ok": True},
                "_iac_response_body_size": 11,
            }

    fake = FakeTeaClient()
    adapter = TeaTransportAdapter(client_factory=lambda _endpoint, _credential: fake)
    shared_budget = budget()

    result = await adapter.execute(
        contract=replace(contract(), transport="tea"),
        request=built_request(),
        endpoint=endpoint(),
        credential=credential(),
        context=ToolContext(),
        budget=shared_budget,
    )

    assert result.body == {"ok": True}
    assert result.headers == {"content-type": "application/json"}
    assert shared_budget.attempts == 1
    assert fake.runtime.autoretry is False
    assert fake.runtime.max_attempts == 1


@pytest.mark.asyncio
async def test_tea_adapter_uses_sls_gateway_execute_and_host_map_for_sls() -> None:
    class FakeSlsClient:
        def __init__(self) -> None:
            self.params: Any = None
            self.request: Any = None
            self.runtime: Any = None

        async def execute_async(self, params: Any, request: Any, runtime: Any) -> dict[str, Any]:
            self.params = params
            self.request = request
            self.runtime = runtime
            return {
                "statusCode": 200,
                "headers": {"Content-Type": "application/json"},
                "body": {"ok": True},
                "_iac_response_body_size": 11,
            }

    fake = FakeSlsClient()
    adapter = TeaTransportAdapter(client_factory=lambda _endpoint, _credential: fake)
    sls_request = BuiltApiRequest(
        method="GET",
        raw_path=b"/logstores",
        canonical_query=(("offset", "0"),),
        headers=MappingProxyType({"accept": "application/json"}),
        body=None,
        response_policy=ResponseBodyPolicy(mode="json", max_bytes=8 * 1024**2, declared_headers=()),
        host_values=MappingProxyType({"project": "demo-project"}),
    )

    result = await adapter.execute(
        contract=replace(
            contract(),
            product="Sls",
            version="2020-12-30",
            action="ListLogStores",
            transport="tea",
        ),
        request=sls_request,
        endpoint=EndpointResolution(
            endpoint="cn-hangzhou.log.aliyuncs.com",
            source="location",
            host_template="{project}.{endpoint}",
            expected_host="demo-project.cn-hangzhou.log.aliyuncs.com",
            region_id="cn-hangzhou",
        ),
        credential=credential(),
        context=ToolContext(),
        budget=budget(),
    )

    assert result.body == {"ok": True}
    assert fake.params.action == "ListLogStores"
    assert fake.request.host_map == {"project": "demo-project"}
    assert fake.request.query == {"offset": "0"}
    assert fake.request.headers == {"accept": "application/json"}
    assert fake.runtime.autoretry is False


@pytest.mark.asyncio
async def test_tea_adapter_uses_oss_gateway_xml_contract_for_hcs_mgw() -> None:
    class FakeHcsClient:
        def __init__(self) -> None:
            self._spi = SimpleNamespace()
            self.params: Any = None
            self.request: Any = None

        async def execute_async(self, params: Any, request: Any, runtime: Any) -> dict[str, Any]:
            del runtime
            self.params = params
            self.request = request
            return {
                "statusCode": 200,
                "headers": {"Content-Type": "application/xml"},
                "body": {"Jobs": []},
                "_iac_response_body_size": 8,
            }

    fake = FakeHcsClient()
    adapter = TeaTransportAdapter(client_factory=lambda _endpoint, _credential: fake)
    request = replace(
        built_request(),
        method="GET",
        raw_path=b"/joblist",
        canonical_query=(("count", "1"),),
        host_values=MappingProxyType({"userid": "xx"}),
    )

    result = await adapter.execute(
        contract=replace(
            contract(),
            product="hcs-mgw",
            version="2024-06-26",
            action="ListJob",
            transport="tea",
        ),
        request=request,
        endpoint=EndpointResolution(
            endpoint="cn-hangzhou.mgw.aliyuncs.com",
            source="catalog_region",
            host_template="{userid}.{endpoint}",
            expected_host="xx.cn-hangzhou.mgw.aliyuncs.com",
            region_id="cn-hangzhou",
        ),
        credential=credential(token="sts-token"),
        context=ToolContext(),
        budget=budget(),
    )

    assert result.body == {"Jobs": []}
    assert fake.params.req_body_type == "xml"
    assert fake.params.body_type == "xml"
    assert fake.request.host_map == {"userid": "xx"}


@pytest.mark.asyncio
async def test_tea_adapter_estimates_sls_gateway_response_size_when_sdk_omits_it() -> None:
    class FakeSlsClient:
        async def execute_async(self, params: Any, request: Any, runtime: Any) -> dict[str, Any]:
            del params, request, runtime
            return {
                "statusCode": 200,
                "headers": {"Content-Type": "application/json"},
                "body": {"projects": []},
            }

    result = await TeaTransportAdapter(client_factory=lambda _endpoint, _credential: FakeSlsClient()).execute(
        contract=replace(
            contract(),
            product="Sls",
            version="2020-12-30",
            action="ListProject",
            transport="tea",
        ),
        request=built_request(),
        endpoint=EndpointResolution(
            endpoint="cn-hangzhou.log.aliyuncs.com",
            source="location",
            host_template=None,
            expected_host="cn-hangzhou.log.aliyuncs.com",
            region_id="cn-hangzhou",
        ),
        credential=credential(),
        context=ToolContext(),
        budget=budget(),
    )

    assert result.status == 200
    assert result.body == {"projects": []}
    assert result.size == len(b'{"projects":[]}')


@pytest.mark.asyncio
async def test_tea_adapter_normalizes_sls_gateway_http_error() -> None:
    from Tea.exceptions import TeaException

    class FakeSlsClient:
        async def execute_async(self, params: Any, request: Any, runtime: Any) -> dict[str, Any]:
            del params, request, runtime
            raise UnretryableException(
                RetryPolicyContext(
                    exception=TeaException(
                        {
                            "code": "ProjectNotExist",
                            "message": "The Project does not exist.",
                            "data": {
                                "httpCode": 404,
                                "requestId": "request-1",
                                "statusCode": 404,
                            },
                        }
                    )
                )
            )

    result = await TeaTransportAdapter(client_factory=lambda _endpoint, _credential: FakeSlsClient()).execute(
        contract=replace(
            contract(),
            product="Sls",
            version="2020-12-30",
            action="GetLogsV2",
            transport="tea",
        ),
        request=built_request(),
        endpoint=EndpointResolution(
            endpoint="cn-hangzhou.log.aliyuncs.com",
            source="location",
            host_template="{project}.{endpoint}",
            expected_host="demo-project.cn-hangzhou.log.aliyuncs.com",
            region_id="cn-hangzhou",
        ),
        credential=credential(),
        context=ToolContext(),
        budget=budget(),
    )

    assert result.status == 404
    assert result.body == {
        "Code": "ProjectNotExist",
        "Message": "The Project does not exist.",
        "RequestId": "request-1",
    }


@pytest.mark.asyncio
async def test_tea_adapter_enforces_retry_budget_deadline_during_streaming_call() -> None:
    started = asyncio.Event()
    cleaned = asyncio.Event()

    class FakeTeaClient:
        async def call_api_streaming_async(
            self, params: Any, request: Any, runtime: Any, **_kwargs: Any
        ) -> dict[str, Any]:
            started.set()
            try:
                await asyncio.Event().wait()
            finally:
                cleaned.set()

    adapter = TeaTransportAdapter(client_factory=lambda _endpoint, _credential: FakeTeaClient())

    with pytest.raises(RetryExhausted) as raised:
        await asyncio.wait_for(
            adapter.execute(
                contract=replace(contract(), transport="tea"),
                request=built_request(),
                endpoint=endpoint(),
                credential=credential(),
                context=ToolContext(),
                budget=RetryBudget(deadline=time.monotonic() + 0.05),
            ),
            timeout=0.5,
        )

    assert started.is_set()
    assert cleaned.is_set()
    assert raised.value.outcome == "read_timeout"


@pytest.mark.asyncio
async def test_tea_adapter_preserves_header_only_204_result() -> None:
    class FakeTeaClient:
        async def call_api_streaming_async(
            self, params: Any, request: Any, runtime: Any, **_kwargs: Any
        ) -> dict[str, Any]:
            return {"headers": {"X-Acs-Request-Id": "request-204"}}

    result = await TeaTransportAdapter(client_factory=lambda _endpoint, _credential: FakeTeaClient()).execute(
        contract=replace(contract(), transport="tea"),
        request=built_request(),
        endpoint=endpoint(),
        credential=credential(),
        context=ToolContext(),
        budget=budget(),
    )

    assert result.status == 204
    assert result.headers == {"x-acs-request-id": "request-204"}
    assert result.body is None
    assert result.size == 0


@pytest.mark.asyncio
async def test_tea_adapter_requests_header_safe_sdk_body_type_for_head() -> None:
    class FakeTeaClient:
        def __init__(self) -> None:
            self.body_type: str | None = None

        async def call_api_streaming_async(
            self, params: Any, request: Any, runtime: Any, **_kwargs: Any
        ) -> dict[str, Any]:
            self.body_type = params.body_type
            return {"statusCode": 200, "headers": {}, "body": ""}

    fake = FakeTeaClient()
    result = await TeaTransportAdapter(client_factory=lambda _endpoint, _credential: fake).execute(
        contract=replace(contract(method="HEAD"), transport="tea"),
        request=replace(built_request(), method="HEAD"),
        endpoint=endpoint(),
        credential=credential(),
        context=ToolContext(),
        budget=budget(),
    )

    assert fake.body_type == "string"
    assert result.status == 200
    assert result.body is None
    assert result.size == 0


@pytest.mark.asyncio
async def test_bounded_tea_wire_request_does_not_invent_accept_for_head() -> None:
    client = _StreamingOpenApiClient(
        open_api_models.Config(endpoint="ecs.cn-hangzhou.aliyuncs.com", region_id="cn-hangzhou"),
        session_factory=lambda **_kwargs: None,
    )
    params = open_api_models.Params(
        action="HeadResource",
        version="2014-05-26",
        protocol="HTTPS",
        pathname="/resource",
        method="HEAD",
        auth_type="Anonymous",
        style="ROA",
        req_body_type="json",
        body_type="string",
    )

    request = await client._prepare_streaming_request(
        params,
        open_api_models.OpenApiRequest(headers={}),
        RuntimeOptions(),
    )

    assert "accept" not in {str(name).casefold() for name in request.headers}


@pytest.mark.asyncio
async def test_bounded_tea_http_wire_does_not_invent_accept_for_head(monkeypatch: pytest.MonkeyPatch) -> None:
    received_headers: asyncio.Future[set[bytes]] = asyncio.get_running_loop().create_future()

    async def handle(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        request_head = await reader.readuntil(b"\r\n\r\n")
        header_names = {
            line.split(b":", 1)[0].strip().lower() for line in request_head.split(b"\r\n")[1:] if b":" in line
        }
        received_headers.set_result(header_names)
        writer.write(b"HTTP/1.1 200 OK\r\nContent-Length: 0\r\nConnection: close\r\n\r\n")
        await writer.drain()
        writer.close()
        await writer.wait_closed()

    for variable in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "http_proxy", "https_proxy", "all_proxy"):
        monkeypatch.delenv(variable, raising=False)
    server = await asyncio.start_server(handle, "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]
    client = _StreamingOpenApiClient(
        open_api_models.Config(endpoint=f"127.0.0.1:{port}", region_id="cn-hangzhou"),
        session_factory=aiohttp.ClientSession,
    )
    params = open_api_models.Params(
        action="HeadResource",
        version="2014-05-26",
        protocol="HTTP",
        pathname="/resource",
        method="HEAD",
        auth_type="Anonymous",
        style="ROA",
        req_body_type="json",
        body_type="string",
    )

    try:
        request = await client._prepare_streaming_request(
            params,
            open_api_models.OpenApiRequest(headers={}),
            RuntimeOptions(),
        )
        async with server:
            await client._send_streaming_request(request, RuntimeOptions(), success_limit=1)
        assert b"accept" not in await asyncio.wait_for(received_headers, timeout=1)
    finally:
        server.close()
        await server.wait_closed()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("exception_type", "status"),
    [
        (open_api_exceptions.ThrottlingException, 429),
        (open_api_exceptions.ServerException, 502),
        (open_api_exceptions.ServerException, 503),
        (open_api_exceptions.ServerException, 504),
    ],
)
async def test_tea_adapter_retries_sdk_status_exceptions_with_same_budget(
    exception_type: type[Exception], status: int
) -> None:
    error = exception_type(
        status_code=status,
        code="Retryable",
        message="retry later",
        data={"Code": "Retryable", "Message": "retry later", "RequestId": "request-retry"},
        request_id="request-retry",
    )

    class FakeTeaClient:
        def __init__(self) -> None:
            self.calls = 0

        async def call_api_streaming_async(
            self, params: Any, request: Any, runtime: Any, **_kwargs: Any
        ) -> dict[str, Any]:
            self.calls += 1
            if self.calls == 1:
                raise error
            return {
                "statusCode": 200,
                "headers": {},
                "body": {"ok": True},
                "_iac_response_body_size": 11,
            }

    fake = FakeTeaClient()
    sleeps: list[float] = []
    shared_budget = budget()
    result = await TeaTransportAdapter(
        client_factory=lambda _endpoint, _credential: fake,
        sleep=lambda delay: _record(sleeps, delay),
    ).execute(
        contract=replace(contract(), transport="tea"),
        request=built_request(),
        endpoint=endpoint(),
        credential=credential(),
        context=ToolContext(),
        budget=shared_budget,
    )

    assert result.body == {"ok": True}
    assert shared_budget.attempts == 2
    assert sleeps == [0.2]


@pytest.mark.asyncio
async def test_tea_adapter_uses_sdk_retry_after_milliseconds_at_two_second_boundary() -> None:
    error = open_api_exceptions.ThrottlingException(
        status_code=429,
        code="Throttling",
        message="retry later",
        retry_after=2_000,
        data={"Code": "Throttling", "Message": "retry later"},
    )

    class FakeTeaClient:
        def __init__(self) -> None:
            self.calls = 0

        async def call_api_streaming_async(
            self, params: Any, request: Any, runtime: Any, **_kwargs: Any
        ) -> dict[str, Any]:
            self.calls += 1
            if self.calls == 1:
                raise error
            return {
                "statusCode": 200,
                "headers": {},
                "body": {"ok": True},
                "_iac_response_body_size": 11,
            }

    fake = FakeTeaClient()
    sleeps: list[float] = []
    result = await TeaTransportAdapter(
        client_factory=lambda _endpoint, _credential: fake,
        sleep=lambda delay: _record(sleeps, delay),
    ).execute(
        contract=replace(contract(), transport="tea"),
        request=built_request(),
        endpoint=endpoint(),
        credential=credential(),
        context=ToolContext(),
        budget=budget(),
    )

    assert result.body == {"ok": True}
    assert fake.calls == 2
    assert sleeps == [2.0]


@pytest.mark.asyncio
async def test_tea_adapter_returns_sdk_error_when_retry_after_reaches_deadline() -> None:
    error = open_api_exceptions.ThrottlingException(
        status_code=429,
        code="Throttling",
        message="retry later",
        retry_after=2_000,
        data={"Code": "Throttling", "Message": "retry later"},
    )
    setattr(error, "_iac_response_body_size", len(b'{"Code":"Throttling","Message":"retry later"}'))

    class FakeTeaClient:
        def __init__(self) -> None:
            self.calls = 0

        async def call_api_streaming_async(
            self, params: Any, request: Any, runtime: Any, **_kwargs: Any
        ) -> dict[str, Any]:
            self.calls += 1
            raise error

    fake = FakeTeaClient()
    shared_budget = RetryBudget(deadline=3.0, clock=lambda: 1.0, random=lambda: 0.0)
    result = await TeaTransportAdapter(client_factory=lambda _endpoint, _credential: fake).execute(
        contract=replace(contract(), transport="tea"),
        request=built_request(),
        endpoint=endpoint(),
        credential=credential(),
        context=ToolContext(),
        budget=shared_budget,
    )

    assert result.status == 429
    assert result.body["Code"] == "Throttling"
    assert fake.calls == 1
    assert shared_budget.attempts == 1


@pytest.mark.asyncio
async def test_tea_adapter_returns_result_error_when_retry_after_reaches_deadline() -> None:
    class FakeTeaClient:
        async def call_api_streaming_async(
            self, params: Any, request: Any, runtime: Any, **_kwargs: Any
        ) -> dict[str, Any]:
            return {
                "statusCode": 503,
                "headers": {"retry-after": "2", "content-type": "application/json"},
                "body": {"Code": "ServiceUnavailable"},
                "_iac_response_body_size": 29,
            }

    result = await TeaTransportAdapter(client_factory=lambda _endpoint, _credential: FakeTeaClient()).execute(
        contract=replace(contract(), transport="tea"),
        request=built_request(),
        endpoint=endpoint(),
        credential=credential(),
        context=ToolContext(),
        budget=RetryBudget(deadline=3.0, clock=lambda: 1.0, random=lambda: 0.0),
    )

    assert result.status == 503
    assert result.body == {"Code": "ServiceUnavailable"}


@pytest.mark.asyncio
async def test_tea_adapter_preserves_retry_reason_when_sleep_crosses_deadline() -> None:
    clock = [1.0]
    error = open_api_exceptions.ThrottlingException(
        status_code=429,
        code="Throttling",
        message="retry later",
        data={"Code": "Throttling", "Message": "retry later"},
    )

    class FakeTeaClient:
        async def call_api_streaming_async(
            self, params: Any, request: Any, runtime: Any, **_kwargs: Any
        ) -> dict[str, Any]:
            raise error

    async def cross_deadline(_delay: float) -> None:
        clock[0] = 3.0

    with pytest.raises(RetryExhausted) as raised:
        await TeaTransportAdapter(
            client_factory=lambda _endpoint, _credential: FakeTeaClient(),
            sleep=cross_deadline,
        ).execute(
            contract=replace(contract(), transport="tea"),
            request=built_request(),
            endpoint=endpoint(),
            credential=credential(),
            context=ToolContext(),
            budget=RetryBudget(deadline=3.0, clock=lambda: clock[0], random=lambda: 0.0),
        )

    assert raised.value.outcome == "retryable_status"
    assert raised.value.reason is RetryReason.RETRYABLE_STATUS


@pytest.mark.asyncio
async def test_acs3_returns_http_error_when_retry_delay_reaches_deadline() -> None:
    stream = TrackingStream([b'{"Code":"ServiceUnavailable"}'])
    client = client_for([(503, [("content-type", "application/json"), ("retry-after", "2")], stream)])

    result = await Acs3StreamingTransport(client=client).execute(
        contract=contract(),
        request=built_request(),
        endpoint=endpoint(),
        credential=credential(),
        context=ToolContext(),
        budget=RetryBudget(deadline=3.0, clock=lambda: 1.0, random=lambda: 0.0),
    )

    assert result.status == 503
    assert result.body == {"Code": "ServiceUnavailable"}
    assert stream.closed
    await client.aclose()


@pytest.mark.asyncio
async def test_acs3_preserves_retry_reason_when_sleep_crosses_deadline() -> None:
    clock = [1.0]
    first = TrackingStream([b'{"Code":"ServiceUnavailable"}'])
    client = client_for([(503, [("content-type", "application/json")], first)])

    async def cross_deadline(_delay: float) -> None:
        clock[0] = 3.0

    with pytest.raises(RetryExhausted) as raised:
        await Acs3StreamingTransport(client=client, sleep=cross_deadline).execute(
            contract=contract(),
            request=built_request(),
            endpoint=endpoint(),
            credential=credential(),
            context=ToolContext(),
            budget=RetryBudget(deadline=3.0, clock=lambda: clock[0], random=lambda: 0.0),
        )

    assert raised.value.outcome == "retryable_status"
    assert raised.value.reason is RetryReason.RETRYABLE_STATUS
    assert first.closed
    await client.aclose()


@pytest.mark.asyncio
async def test_tea_adapter_normalizes_sdk_status_exception() -> None:
    raw_payload = b'{  "Code" : "InvalidParameter", "Message" : "invalid parameter", "RequestId" : "request-400" }\n'
    error = open_api_exceptions.ClientException(
        status_code=400,
        code="InvalidParameter",
        message="invalid parameter",
        data={"Code": "InvalidParameter", "Message": "invalid parameter", "RequestId": "request-400"},
        request_id="request-400",
    )
    setattr(error, "_iac_response_body_size", len(raw_payload))

    class FakeTeaClient:
        async def call_api_streaming_async(
            self, params: Any, request: Any, runtime: Any, **_kwargs: Any
        ) -> dict[str, Any]:
            raise error

    result = await TeaTransportAdapter(client_factory=lambda _endpoint, _credential: FakeTeaClient()).execute(
        contract=replace(contract(), transport="tea"),
        request=built_request(),
        endpoint=endpoint(),
        credential=credential(),
        context=ToolContext(),
        budget=budget(),
    )

    assert result.status == 400
    assert result.headers == {}
    assert result.body == error.data
    assert result.content_type is None
    assert result.size == len(raw_payload)


@pytest.mark.asyncio
async def test_tea_adapter_enforces_one_mib_sdk_error_limit() -> None:
    error = open_api_exceptions.ServerException(
        status_code=500,
        code="LargeError",
        message="large error",
        data={"Message": "x" * (1024**2 + 1)},
    )
    setattr(error, "_iac_response_body_size", 1024**2 + 1)

    class FakeTeaClient:
        async def call_api_streaming_async(
            self, params: Any, request: Any, runtime: Any, **_kwargs: Any
        ) -> dict[str, Any]:
            raise error

    with pytest.raises(ResponseTooLarge, match="error_response_too_large"):
        await TeaTransportAdapter(client_factory=lambda _endpoint, _credential: FakeTeaClient()).execute(
            contract=replace(contract(), transport="tea"),
            request=built_request(),
            endpoint=endpoint(),
            credential=credential(),
            context=ToolContext(),
            budget=budget(),
        )


@pytest.mark.asyncio
async def test_tea_adapter_enforces_success_body_limit() -> None:
    class FakeTeaClient:
        async def call_api_streaming_async(
            self, params: Any, request: Any, runtime: Any, **_kwargs: Any
        ) -> dict[str, Any]:
            return {
                "statusCode": 200,
                "headers": {},
                "body": {"value": "too large"},
                "_iac_response_body_size": 21,
            }

    with pytest.raises(ResponseTooLarge, match="response_too_large"):
        await TeaTransportAdapter(client_factory=lambda _endpoint, _credential: FakeTeaClient()).execute(
            contract=replace(contract(), transport="tea"),
            request=built_request(max_bytes=10),
            endpoint=endpoint(),
            credential=credential(),
            context=ToolContext(),
            budget=budget(),
        )


@pytest.mark.asyncio
@pytest.mark.parametrize("raw_body", [b"raw-binary", bytearray(b"raw-binary")])
async def test_tea_adapter_binary_body_projects_to_stable_base64_json(raw_body: bytes | bytearray) -> None:
    class FakeTeaClient:
        async def call_api_streaming_async(
            self, params: Any, request: Any, runtime: Any, **_kwargs: Any
        ) -> dict[str, Any]:
            return {
                "statusCode": 200,
                "headers": {"Content-Type": "application/octet-stream"},
                "body": raw_body,
                "_iac_response_body_size": len(raw_body),
            }

    binary_contract = replace(contract(response_body_type="binary"), transport="tea")
    binary_request = built_request(mode="binary")
    response = await TeaTransportAdapter(client_factory=lambda _endpoint, _credential: FakeTeaClient()).execute(
        contract=binary_contract,
        request=binary_request,
        endpoint=endpoint(),
        credential=credential(),
        context=ToolContext(),
        budget=budget(),
    )

    content, body_format = serialize_business_result(response, binary_request, binary_contract)

    assert content == '{\n  "data": "cmF3LWJpbmFyeQ==",\n  "encoding": "base64"\n}'
    assert body_format == "binary_base64_json"


@pytest.mark.asyncio
async def test_tea_adapter_preflights_trusted_content_length() -> None:
    class FakeTeaClient:
        async def call_api_streaming_async(
            self, params: Any, request: Any, runtime: Any, **_kwargs: Any
        ) -> dict[str, Any]:
            return {"statusCode": 200, "headers": {"Content-Length": "11"}, "body": "ok"}

    with pytest.raises(ResponseTooLarge, match="response_too_large"):
        await TeaTransportAdapter(client_factory=lambda _endpoint, _credential: FakeTeaClient()).execute(
            contract=replace(contract(response_body_type="string"), transport="tea"),
            request=built_request(mode="text", max_bytes=10),
            endpoint=endpoint(),
            credential=credential(),
            context=ToolContext(),
            budget=budget(),
        )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("inner", "expected_outcome", "expected_reason"),
    [
        (
            aiohttp.ClientConnectorError(
                SimpleNamespace(ssl=False, host="example.com", port=443),
                OSError("connect"),
            ),
            "pre_connect_failure",
            RetryReason.CONNECT_ERROR,
        ),
        (
            aiohttp.ClientPayloadError("partial response"),
            "unknown_after_transport_error",
            RetryReason.STREAM_READ_ERROR,
        ),
        (
            aiohttp.ClientOSError(1, "write failed"),
            "unknown_after_transport_error",
            None,
        ),
    ],
)
async def test_tea_execute_surfaces_write_transport_outcome(
    inner: Exception, expected_outcome: str, expected_reason: RetryReason | None
) -> None:
    wrapped_inner = inner
    if type(inner) is aiohttp.ClientConnectorError:
        wrapped_inner = RetryError(str(inner))
        wrapped_inner.__context__ = inner
    wrapper = UnretryableException(RetryPolicyContext(http_request=TeaRequest(), exception=wrapped_inner))

    class FakeTeaClient:
        async def call_api_streaming_async(
            self, params: Any, request: Any, runtime: Any, **_kwargs: Any
        ) -> dict[str, Any]:
            raise wrapper

    shared_budget = budget()
    with pytest.raises(TransportFailure) as caught:
        await TeaTransportAdapter(client_factory=lambda _endpoint, _credential: FakeTeaClient()).execute(
            contract=replace(
                contract(operation_type="write", method="POST", request_body_type="json"),
                transport="tea",
            ),
            request=replace(built_request(body=b"{}"), method="POST"),
            endpoint=endpoint(),
            credential=credential(),
            context=ToolContext(),
            budget=shared_budget,
        )

    assert caught.value.outcome == expected_outcome
    assert caught.value.reason is expected_reason
    assert caught.value.__cause__ is wrapper
    assert shared_budget.attempts == 1


@pytest.mark.asyncio
async def test_tea_adapter_retries_known_wrapper_with_same_budget() -> None:
    class FakeTeaClient:
        def __init__(self) -> None:
            self.calls = 0

        async def call_api_streaming_async(
            self, params: Any, request: Any, runtime: Any, **_kwargs: Any
        ) -> dict[str, Any]:
            self.calls += 1
            if self.calls == 1:
                inner = aiohttp.ClientConnectorError(
                    SimpleNamespace(ssl=False, host="example.com", port=443),
                    OSError("connect"),
                )
                core_error = RetryError(str(inner))
                core_error.__context__ = inner
                raise UnretryableException(
                    RetryPolicyContext(
                        http_request=TeaRequest(),
                        exception=core_error,
                    )
                )
            return {
                "statusCode": 200,
                "headers": {},
                "body": {"ok": True},
                "_iac_response_body_size": 11,
            }

    fake = FakeTeaClient()
    sleeps: list[float] = []
    adapter = TeaTransportAdapter(
        client_factory=lambda _endpoint, _credential: fake,
        sleep=lambda delay: _record(sleeps, delay),
    )
    shared_budget = budget()

    result = await adapter.execute(
        contract=replace(contract(), transport="tea"),
        request=built_request(),
        endpoint=endpoint(),
        credential=credential(),
        context=ToolContext(),
        budget=shared_budget,
    )

    assert result.body == {"ok": True}
    assert shared_budget.attempts == 2
    assert sleeps == [0.2]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("selected", "action", "request_body_type", "response_body_type", "consumes", "produces", "mode", "body"),
    [
        ("tea", "DownloadBinary", "none", "binary", (), ("application/octet-stream",), "binary", None),
        ("acs3_streaming", "DescribeInstances", "none", "json", (), ("application/json",), "json", None),
        (
            "tea",
            "UploadBytes",
            "byte",
            "json",
            ("application/octet-stream",),
            ("application/json",),
            "json",
            b"raw-body",
        ),
        (
            "acs3_streaming",
            "CreateJsonResource",
            "json",
            "string",
            ("application/json",),
            ("text/plain",),
            "text",
            b"{}",
        ),
    ],
)
async def test_router_dispatches_only_by_contract_transport(
    selected: str,
    action: str,
    request_body_type: str,
    response_body_type: str,
    consumes: tuple[str, ...],
    produces: tuple[str, ...],
    mode: str,
    body: bytes | None,
) -> None:
    results = {
        "tea": NormalizedApiResponse(201, {}, "tea", None, None, 3),
        "acs3_streaming": NormalizedApiResponse(202, {}, "acs3", None, None, 4),
    }

    class SpyTransport:
        def __init__(self, name: str) -> None:
            self.name = name
            self.calls: list[dict[str, Any]] = []

        async def execute(self, **kwargs: Any) -> NormalizedApiResponse:
            self.calls.append(kwargs)
            return results[self.name]

    spies = {name: SpyTransport(name) for name in results}
    router = TransportRouter(spies)
    shared_budget = budget()
    selected_contract = replace(
        contract(
            action=action,
            request_body_type=request_body_type,
            response_body_type=response_body_type,
            consumes=consumes,
            produces=produces,
        ),
        transport=selected,
    )
    selected_request = built_request(mode=mode, body=body)

    result = await router.execute(
        contract=selected_contract,
        request=selected_request,
        endpoint=endpoint(),
        credential=credential(),
        context=ToolContext(),
        budget=shared_budget,
    )

    assert result is results[selected]
    assert len(spies[selected].calls) == 1
    assert spies[selected].calls[0]["contract"] is selected_contract
    assert spies[selected].calls[0]["request"] is selected_request
    assert spies[selected].calls[0]["budget"] is shared_budget
    assert not spies["acs3_streaming" if selected == "tea" else "tea"].calls


@pytest.mark.asyncio
async def test_runtime_services_expose_and_close_single_transport_router(tmp_path: Path) -> None:
    services = create_aliyun_runtime_services(cache_dir=tmp_path)

    assert isinstance(services.transport_router, TransportRouter)
    assert services.transport_router is services.transport_router

    await services.aclose()


@pytest.mark.asyncio
async def test_runtime_services_close_openmeta_when_transport_router_close_fails(tmp_path: Path) -> None:
    services = create_aliyun_runtime_services(cache_dir=tmp_path)
    router_error = RuntimeError("router close failed")
    openmeta_closed = False

    async def fail_router_close() -> None:
        raise router_error

    async def close_openmeta() -> None:
        nonlocal openmeta_closed
        openmeta_closed = True

    services.transport_router.aclose = fail_router_close  # type: ignore[method-assign]
    services.openmeta.aclose = close_openmeta  # type: ignore[method-assign]

    with pytest.raises(RuntimeError, match="router close failed") as caught:
        await services.aclose()

    assert caught.value is router_error
    assert openmeta_closed
