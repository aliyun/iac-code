"""Focused tests for the generated OSS async-operation policy and V4 adapter."""

from __future__ import annotations

import asyncio
import contextvars
import gc
import hashlib
import importlib
import importlib.metadata
import inspect
import json
import re
import subprocess
import sys
import threading
from datetime import datetime, timezone
from pathlib import Path
from types import MappingProxyType, SimpleNamespace
from typing import Any
from urllib.parse import urlsplit

try:
    import tomllib
except ModuleNotFoundError:  # pragma: no cover - Python 3.10 compatibility
    import tomli as tomllib

import aiohttp
import httpx
import pytest
from alibabacloud_oss_v2 import Credentials
from alibabacloud_oss_v2 import exceptions as oss_exceptions
from alibabacloud_oss_v2.aio.client import AsyncClient
from alibabacloud_oss_v2.signer.v4 import SignerV4
from alibabacloud_oss_v2.types import HttpRequest, SigningContext
from multidict import CIMultiDict
from packaging.specifiers import SpecifierSet

from iac_code.services.providers.aliyun import AliyunCredential
from iac_code.tools.base import ToolContext
from iac_code.tools.cloud.aliyun.acs3_transport import NormalizedApiResponse
from iac_code.tools.cloud.aliyun.aliyun_api import AliyunApi
from iac_code.tools.cloud.aliyun.api_contract import (
    ApiCallShape,
    ApiContractResolver,
    BuiltApiRequest,
    CanonicalWireContract,
    ResponseBodyPolicy,
)
from iac_code.tools.cloud.aliyun.contract_store import canonical_input_sha256
from iac_code.tools.cloud.aliyun.endpoint_resolver import EndpointResolution, HostBindingResolver
from iac_code.tools.cloud.aliyun.openmeta import MetadataFetch, ParameterMetadata, normalize_api_metadata
from iac_code.tools.cloud.aliyun.retry_policy import RetryBudget, TransportFailure
from iac_code.tools.cloud.aliyun.runtime import TransportRouter, create_aliyun_runtime_services
from iac_code.types.permissions import InvocationBinding, ToolPermissionContext
from tests.tools.cloud.aliyun._ecs_ram_role_fakes import FakeEcsRuntime

ROOT = Path(__file__).resolve().parents[4]
CATALOG_PATH = ROOT / "src/iac_code/tools/cloud/aliyun/data/oss/operation_catalog.json"
FIXTURE_PATH = ROOT / "tests/tools/cloud/aliyun/fixtures/oss/openmeta_operations.json"
GENERATOR_PATH = ROOT / "scripts/aliyun/generate_oss_operations.py"
SHA256 = re.compile(r"^[0-9a-f]{64}$")


def _public_async_methods() -> tuple[str, ...]:
    return tuple(
        name
        for name, method in inspect.getmembers(AsyncClient, inspect.isfunction)
        if not name.startswith("_") and inspect.iscoroutinefunction(method)
    )


def _catalog_document() -> dict[str, object]:
    return json.loads(CATALOG_PATH.read_text(encoding="ascii"))


def _adapter_module() -> Any:
    return importlib.import_module("iac_code.tools.cloud.aliyun.oss_v4_adapter")


def _project_oss_requirement() -> str:
    project = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))["project"]
    dependencies = project["dependencies"]
    matches = [item for item in dependencies if item.casefold().startswith("alibabacloud-oss-v2")]
    assert len(matches) == 1
    return matches[0].removeprefix("alibabacloud-oss-v2")


def test_openmeta_fixture_has_authentic_complete_sdk_operation_provenance() -> None:
    fixture_bytes = FIXTURE_PATH.read_bytes()
    fixture = json.loads(fixture_bytes)
    assert fixture["_meta"] == {
        "product": "Oss",
        "schema_version": 1,
        "source": "Alibaba Cloud OpenMeta",
        "source_url_template": (
            "https://api.aliyun.com/meta/v1/products/Oss/versions/2019-05-17/apis/{action}/api.json?language=ZH_CN"
        ),
        "version": "2019-05-17",
    }
    operations = fixture["operations"]
    request_models = (
        inspect.signature(method).parameters["request"].annotation
        for name, method in inspect.getmembers(AsyncClient, inspect.isfunction)
        if name in _public_async_methods() and "request" in inspect.signature(method).parameters
    )
    request_actions = {
        model.__name__.removesuffix("Request")
        for model in request_models
        if inspect.isclass(model) and model.__name__.endswith("Request")
    }
    assert len(operations) == 48
    assert {row["action"] for row in operations} == request_actions
    assert [row["action"] for row in operations] == sorted(request_actions)
    assert all(SHA256.fullmatch(row["document_sha256"]) for row in operations)
    assert SHA256.fullmatch(hashlib.sha256(fixture_bytes).hexdigest())


def test_catalog_has_exactly_one_deterministic_row_per_public_async_method() -> None:
    document = _catalog_document()
    rows = document["operations"]
    methods = _public_async_methods()
    assert len(rows) == len(methods) == 53
    assert [row["sdk_method"] for row in rows] == sorted(methods)
    assert len({row["sdk_method"] for row in rows}) == len(rows)
    required_keys = {
        "action",
        "body_type",
        "field_mapping",
        "method",
        "request_model",
        "response_mode",
        "sdk_method",
        "supported",
        "unsupported_reasons",
    }
    for row in rows:
        assert set(row) == required_keys
        assert isinstance(row["supported"], bool)
        assert row["unsupported_reasons"] == sorted(set(row["unsupported_reasons"]))
        assert row["field_mapping"] == sorted(
            row["field_mapping"],
            key=lambda item: (item["location"], item["openmeta_name"].casefold(), item["sdk_field"]),
        )
        if row["supported"]:
            assert row["unsupported_reasons"] == []
            assert row["response_mode"] in {"stream", "headers_only", "buffered"}
            assert row["body_type"] in {"none", "byte"}
            assert isinstance(row["action"], str) and row["action"]
            assert isinstance(row["method"], str) and row["method"]
            assert isinstance(row["request_model"], str) and row["request_model"]
        else:
            assert row["unsupported_reasons"]
            assert row["response_mode"] == "unsupported"


def test_supported_catalog_rows_map_every_openmeta_parameter_once() -> None:
    fixture = json.loads(FIXTURE_PATH.read_text(encoding="ascii"))
    metadata_by_action = {row["action"]: row for row in fixture["operations"]}
    rows = _catalog_document()["operations"]
    supported = [row for row in rows if row["supported"]]
    assert {row["action"] for row in supported} >= {"GetObject", "PutObject", "HeadObject", "ListBuckets"}
    for row in supported:
        metadata = metadata_by_action[row["action"]]
        expected = sorted(parameter["name"] for parameter in metadata["parameters"])
        actual = sorted(mapping["openmeta_name"] for mapping in row["field_mapping"])
        assert actual == expected
        for mapping in row["field_mapping"]:
            assert set(mapping) == {
                "location",
                "openmeta_name",
                "required",
                "sdk_field",
                "sdk_type",
                "wire_name",
            }


def test_catalog_provenance_matches_project_lock_sdk_and_fixture() -> None:
    document = _catalog_document()
    meta = document["_meta"]
    installed = importlib.metadata.version("alibabacloud-oss-v2")
    requirement = _project_oss_requirement()
    assert installed in SpecifierSet(requirement)
    wheel_hash = subprocess.run(
        [sys.executable, str(GENERATOR_PATH), "wheel-hash", "--lockfile", str(ROOT / "uv.lock")],
        cwd=ROOT,
        check=True,
        capture_output=True,
        text=True,
    ).stdout
    assert wheel_hash == wheel_hash.strip() + "\n"
    assert SHA256.fullmatch(wheel_hash.strip())
    assert meta == {
        "generated_by": "scripts/aliyun/generate_oss_operations.py",
        "openmeta_fixture_schema_version": 1,
        "openmeta_fixture_sha256": hashlib.sha256(FIXTURE_PATH.read_bytes()).hexdigest(),
        "schema_version": 1,
        "sdk_version": installed,
        "sdk_wheel_sha256": wheel_hash.strip(),
    }


def test_generator_reproduces_catalog_byte_for_byte(tmp_path: Path) -> None:
    output = tmp_path / "catalog.json"
    wheel_hash = subprocess.run(
        [sys.executable, str(GENERATOR_PATH), "wheel-hash", "--lockfile", str(ROOT / "uv.lock")],
        cwd=ROOT,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    subprocess.run(
        [
            sys.executable,
            str(GENERATOR_PATH),
            "generate",
            "--sdk-version",
            importlib.metadata.version("alibabacloud-oss-v2"),
            "--sdk-wheel-sha256",
            wheel_hash,
            "--openmeta-fixture",
            str(FIXTURE_PATH),
            "--output",
            str(output),
        ],
        cwd=ROOT,
        check=True,
    )
    assert output.read_bytes() == CATALOG_PATH.read_bytes()


@pytest.mark.parametrize(
    "wheels",
    [
        [],
        [
            {
                "url": "https://example.invalid/alibabacloud_oss_v2-1.3.2-1-py3-none-any.whl",
                "hash": "sha256:" + "1" * 64,
            },
            {
                "url": "https://example.invalid/alibabacloud_oss_v2-1.3.2-2-py3-none-any.whl",
                "hash": "sha256:" + "2" * 64,
            },
        ],
    ],
)
def test_wheel_hash_fails_when_no_unique_installed_platform_wheel(tmp_path: Path, wheels: list[dict[str, str]]) -> None:
    lockfile = tmp_path / "uv.lock"
    wheel_lines = "\n".join('    {{ url = "{}", hash = "{}" }},'.format(item["url"], item["hash"]) for item in wheels)
    lockfile.write_text(
        'version = 1\n\n[[package]]\nname = "alibabacloud-oss-v2"\nversion = "1.3.2"\nwheels = [\n'
        + wheel_lines
        + "\n]\n",
        encoding="ascii",
    )
    result = subprocess.run(
        [sys.executable, str(GENERATOR_PATH), "wheel-hash", "--lockfile", str(lockfile)],
        cwd=ROOT,
        capture_output=True,
        text=True,
    )
    assert result.returncode != 0
    assert result.stdout == ""


def test_catalog_model_loads_all_rows_and_has_stable_digest() -> None:
    module = _adapter_module()
    catalog = module.OssOperationCatalog.load(CATALOG_PATH)
    assert len(catalog.operations) == 53
    assert catalog.require("GetObject").sdk_method == "get_object"
    assert catalog.require("GetObject").response_mode == "stream"
    assert catalog.require("CompleteMultipartUpload").supported is False
    assert catalog.policy_digest == hashlib.sha256(CATALOG_PATH.read_bytes()).hexdigest()


def _signing_context(
    *,
    host: str = "demo-bucket.oss-cn-hangzhou.aliyuncs.com",
    include_host_header: bool = True,
) -> SigningContext:
    return SigningContext(
        product="oss",
        region="cn-hangzhou",
        bucket="demo-bucket",
        key="folder/demo.txt",
        request=HttpRequest(
            "GET",
            f"https://{host}/folder/demo.txt?versionId=v1",
            headers={"Host": host} if include_host_header else {},
        ),
        credentials=Credentials("test-ak", "test-secret", "sts-token"),
        signing_time=datetime(2026, 7, 12, 1, 2, 3, tzinfo=timezone.utc),
    )


def test_identity_signer_matches_official_v4_golden_and_signs_sts_and_identity() -> None:
    module = _adapter_module()
    actual = _signing_context()
    module.OssIdentitySigner().sign(actual)

    expected = _signing_context()
    expected.request.headers["Accept-Encoding"] = "identity"
    expected.additional_headers = {"accept-encoding", "host"}
    SignerV4().sign(expected)

    assert actual.request.headers == expected.request.headers
    assert actual.string_to_sign == expected.string_to_sign
    authorization = actual.request.headers["Authorization"]
    assert "Credential=test-ak/20260712/cn-hangzhou/oss/aliyun_v4_request" in authorization
    assert "AdditionalHeaders=accept-encoding;host" in authorization
    assert actual.request.headers["x-oss-security-token"] == "sts-token"
    assert actual.request.headers["Accept-Encoding"] == "identity"


def test_identity_signer_binds_host_value_into_official_v4_signature() -> None:
    module = _adapter_module()
    original = _signing_context()
    changed = _signing_context()
    changed.request.headers["Host"] = "other-bucket.oss-cn-hangzhou.aliyuncs.com"

    module.OssIdentitySigner().sign(original)
    module.OssIdentitySigner().sign(changed)

    assert "AdditionalHeaders=accept-encoding;host" in original.request.headers["Authorization"]
    assert original.request.headers["Authorization"] != changed.request.headers["Authorization"]


def test_identity_signer_binds_if_match_value_into_official_v4_signature() -> None:
    module = _adapter_module()
    original = _signing_context()
    changed = _signing_context()
    original.request.headers["If-Match"] = '"etag-1"'
    changed.request.headers["If-Match"] = '"etag-2"'

    module.OssIdentitySigner().sign(original)
    module.OssIdentitySigner().sign(changed)

    assert "AdditionalHeaders=accept-encoding;host;if-match" in original.request.headers["Authorization"]
    assert original.request.headers["Authorization"] != changed.request.headers["Authorization"]


def test_identity_signer_materializes_url_host_for_locked_sdk_request() -> None:
    module = _adapter_module()
    context = _signing_context(include_host_header=False)

    module.OssIdentitySigner().sign(context)

    assert context.request.headers["Host"] == "demo-bucket.oss-cn-hangzhou.aliyuncs.com"


class FakeContent:
    def __init__(self, chunks: list[bytes], *, gate: asyncio.Event | None = None) -> None:
        self.chunks = chunks
        self.gate = gate
        self.read_calls = 0
        self.iter_calls = 0

    async def read(self) -> bytes:
        self.read_calls += 1
        if self.gate is not None:
            await self.gate.wait()
        return b"".join(self.chunks)

    async def iter_chunked(self, _size: int) -> Any:
        self.iter_calls += 1
        if self.gate is not None:
            await self.gate.wait()
        for chunk in self.chunks:
            yield chunk


class FakeAioResponse:
    def __init__(
        self,
        chunks: list[bytes],
        *,
        status: int = 200,
        headers: list[tuple[str, str]] | None = None,
        gate: asyncio.Event | None = None,
    ) -> None:
        self.status = status
        self.reason = "OK" if status < 400 else "Error"
        self.headers = CIMultiDict(headers or [])
        self.content = FakeContent(chunks, gate=gate)
        self.closed = False

    def close(self) -> None:
        self.closed = True


class FakeSession:
    def __init__(self, response: FakeAioResponse) -> None:
        self.response = response
        self.requests: list[dict[str, Any]] = []
        self.closed = False

    async def request(self, method: str, url: str, **kwargs: Any) -> FakeAioResponse:
        self.requests.append({"method": method, "url": url, **kwargs})
        return self.response

    async def close(self) -> None:
        self.closed = True


class SequenceSession(FakeSession):
    def __init__(self, responses: list[FakeAioResponse]) -> None:
        super().__init__(responses[0])
        self.responses = responses

    async def request(self, method: str, url: str, **kwargs: Any) -> FakeAioResponse:
        self.requests.append({"method": method, "url": url, **kwargs})
        return self.responses.pop(0)


class RaisingSession(FakeSession):
    def __init__(self, error: BaseException) -> None:
        super().__init__(FakeAioResponse([]))
        self.error = error

    async def request(self, method: str, url: str, **kwargs: Any) -> FakeAioResponse:
        self.requests.append({"method": method, "url": url, **kwargs})
        raise self.error


def _signed_http_request(
    *,
    host: str = "demo-bucket.oss-cn-hangzhou.aliyuncs.com",
    signed_host: str | None = None,
    scheme: str = "https",
    accept_encoding: str = "identity",
) -> HttpRequest:
    return HttpRequest(
        "GET",
        f"{scheme}://{host}/folder/demo.txt",
        headers={
            "Accept-Encoding": accept_encoding,
            "Authorization": ("OSS4-HMAC-SHA256 Credential=x,AdditionalHeaders=accept-encoding;host,Signature=y"),
            "Host": signed_host or host,
        },
    )


def _policy(module: Any, *, mode: str, max_bytes: int = 32 * 1024**2) -> Any:
    return module.OssResponsePolicy(
        mode=mode,
        expected_host="demo-bucket.oss-cn-hangzhou.aliyuncs.com",
        max_response_bytes=max_bytes,
        declared_headers=("x-result-token",),
    )


async def _send_with_policy(client: Any, module: Any, policy: Any, request: HttpRequest | None = None) -> Any:
    token = module._OSS_RESPONSE_POLICY.set(policy)
    try:
        return await client.send(request or _signed_http_request())
    finally:
        module._OSS_RESPONSE_POLICY.reset(token)


@pytest.mark.asyncio
async def test_stream_mode_is_lazy_snapshots_policy_and_closes_connection() -> None:
    module = _adapter_module()
    gate = asyncio.Event()
    raw = FakeAioResponse([b"x" * 1024], headers=[("Content-Encoding", "gzip")], gate=gate)
    session = FakeSession(raw)
    session_kwargs: dict[str, Any] = {}

    def session_factory(**kwargs: Any) -> FakeSession:
        session_kwargs.update(kwargs)
        return session

    client = module.OssStreamingHttpClient(session_factory=session_factory)
    response = await asyncio.wait_for(_send_with_policy(client, module, _policy(module, mode="stream")), 0.5)

    assert session_kwargs["auto_decompress"] is False
    assert session_kwargs["skip_auto_headers"] == {"Accept-Encoding"}
    assert raw.content.read_calls == raw.content.iter_calls == 0
    assert response.policy == _policy(module, mode="stream")
    assert module._OSS_RESPONSE_POLICY.get() is None
    await response.close()
    assert raw.closed is True
    await client.close()
    assert session.closed is True


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("mode", "status", "chunk_count", "message"),
    [
        ("buffered", 200, 17, "response_too_large"),
        ("buffered", 400, 2, "error_response_too_large"),
    ],
)
async def test_buffered_and_error_modes_enforce_incremental_hard_limits(
    mode: str, status: int, chunk_count: int, message: str
) -> None:
    module = _adapter_module()
    chunks = [b"x" * 1024**2] * chunk_count
    raw = FakeAioResponse(chunks, status=status)
    client = module.OssStreamingHttpClient(session_factory=lambda **_kwargs: FakeSession(raw))
    with pytest.raises(module.ResponseTooLarge, match=message):
        await _send_with_policy(client, module, _policy(module, mode=mode))
    assert raw.content.read_calls == 0
    assert raw.content.iter_calls == 1
    assert raw.closed is True
    await client.close()


@pytest.mark.asyncio
async def test_headers_only_and_exact_buffer_boundaries_close_without_overread() -> None:
    module = _adapter_module()
    headers_raw = FakeAioResponse([b"must-not-read"])
    headers_client = module.OssStreamingHttpClient(session_factory=lambda **_kwargs: FakeSession(headers_raw))
    headers_response = await _send_with_policy(headers_client, module, _policy(module, mode="headers_only"))
    assert headers_response.content == b""
    assert headers_raw.content.read_calls == headers_raw.content.iter_calls == 0
    assert headers_raw.closed is True
    await headers_client.close()

    buffered_raw = FakeAioResponse([b"x" * 1024**2] * 16)
    buffered_client = module.OssStreamingHttpClient(session_factory=lambda **_kwargs: FakeSession(buffered_raw))
    buffered_response = await _send_with_policy(buffered_client, module, _policy(module, mode="buffered"))
    assert len(buffered_response.content) == 16 * 1024**2
    assert buffered_raw.closed is True
    await buffered_client.close()

    error_raw = FakeAioResponse([b"x" * 1024**2], status=400)
    error_client = module.OssStreamingHttpClient(session_factory=lambda **_kwargs: FakeSession(error_raw))
    error_response = await _send_with_policy(error_client, module, _policy(module, mode="buffered"))
    assert len(error_response.content) == 1024**2
    assert error_raw.closed is True
    await error_client.close()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("http_request", "error"),
    [
        (_signed_http_request(scheme="http"), "oss_https_required"),
        (_signed_http_request(host="other.oss-cn-hangzhou.aliyuncs.com"), "oss_final_host_mismatch"),
        (_signed_http_request(accept_encoding="gzip"), "oss_accept_encoding_not_identity"),
    ],
)
async def test_http_client_rejects_http_host_mismatch_and_caller_encoding(
    http_request: HttpRequest, error: str
) -> None:
    module = _adapter_module()
    raw = FakeAioResponse([])
    session = FakeSession(raw)
    client = module.OssStreamingHttpClient(session_factory=lambda **_kwargs: session)
    with pytest.raises(ValueError, match=error):
        await _send_with_policy(client, module, _policy(module, mode="stream"), http_request)
    assert session.requests == []
    await client.close()


@pytest.mark.asyncio
async def test_http_client_rejects_signed_host_mismatch_before_network() -> None:
    module = _adapter_module()
    raw = FakeAioResponse([])
    session = FakeSession(raw)
    client = module.OssStreamingHttpClient(session_factory=lambda **_kwargs: session)

    with pytest.raises(ValueError, match="oss_final_host_mismatch"):
        await _send_with_policy(
            client,
            module,
            _policy(module, mode="stream"),
            request=_signed_http_request(signed_host="other.oss-cn-hangzhou.aliyuncs.com"),
        )

    assert session.requests == []
    await client.close()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("transport_error", "sdk_error"),
    [
        (aiohttp.ClientConnectionError("disconnect"), oss_exceptions.RequestError),
        (aiohttp.ServerTimeoutError("timeout"), oss_exceptions.ResponseError),
    ],
)
async def test_http_client_wraps_aiohttp_failures_in_sdk_error_envelopes(
    transport_error: BaseException, sdk_error: type[BaseException]
) -> None:
    module = _adapter_module()
    client = module.OssStreamingHttpClient(session_factory=lambda **_kwargs: RaisingSession(transport_error))
    with pytest.raises(sdk_error) as raised:
        await _send_with_policy(client, module, _policy(module, mode="stream"))
    assert raised.value.unwrap() is transport_error
    await client.close()


def test_identity_signer_rejects_preexisting_accept_encoding() -> None:
    module = _adapter_module()
    context = _signing_context()
    context.request.headers["Accept-Encoding"] = "gzip"
    with pytest.raises(ValueError, match="caller_accept_encoding_forbidden"):
        module.OssIdentitySigner().sign(context)


def test_signed_http_request_has_expected_virtual_host() -> None:
    request = _signed_http_request()
    assert urlsplit(request.url).hostname == "demo-bucket.oss-cn-hangzhou.aliyuncs.com"


def _parameter(name: str, location: str, *, required: bool = False, schema_type: str = "string") -> ParameterMetadata:
    return ParameterMetadata(
        name=name,
        location=location,
        required=required,
        style=None,
        path_encoding="preserve_slashes" if location == "path" else None,
        schema=MappingProxyType({"type": schema_type}),
        description=None,
        example=None,
    )


def _oss_contract(
    *,
    action: str = "GetObject",
    method: str = "GET",
    operation_type: str = "read",
    parameters: tuple[ParameterMetadata, ...] | None = None,
    request_body_type: str = "none",
    response_body_type: str = "binary",
) -> CanonicalWireContract:
    if parameters is None:
        parameters = (_parameter("bucket", "host", required=True), _parameter("key", "path", required=True))
    return CanonicalWireContract(
        metadata_source="fresh",
        product="Oss",
        version="2019-05-17",
        action=action,
        style="ROA",
        method=method,
        pathname="/{key}" if any(item.location == "path" for item in parameters) else "/",
        operation_type=operation_type,
        auth_type="AK",
        signature_scheme="oss_v4",
        transport="oss_v4_sdk",
        executable=True,
        unsupported_reasons=(),
        parameters=parameters,
        consumes=("application/octet-stream",) if request_body_type == "byte" else (),
        produces=("application/octet-stream",) if response_body_type == "binary" else ("application/xml",),
        policy_digest="contract-policy",
        request_body_type=request_body_type,  # type: ignore[arg-type]
        response_body_type=response_body_type,  # type: ignore[arg-type]
    )


def _oss_request(
    *,
    method: str = "GET",
    raw_path: bytes = b"/folder/demo.txt",
    query: tuple[tuple[str, str], ...] = (),
    headers: MappingProxyType[str, str] | None = None,
    body: bytes | None = None,
    max_bytes: int = 32 * 1024**2,
    host_values: MappingProxyType[str, str] | None = None,
) -> BuiltApiRequest:
    return BuiltApiRequest(
        method=method,
        raw_path=raw_path,
        canonical_query=query,
        headers=headers or MappingProxyType({}),
        body=body,
        response_policy=ResponseBodyPolicy(
            mode="binary",
            max_bytes=max_bytes,
            declared_headers=("x-result-token", "x-oss-security-token"),
        ),
        host_values=(MappingProxyType({"bucket": "demo-bucket"}) if host_values is None else host_values),
    )


def _oss_endpoint(*, bucket: bool = True) -> EndpointResolution:
    return EndpointResolution(
        "oss-cn-hangzhou.aliyuncs.com",
        "catalog_region",
        "{bucket}.{endpoint}" if bucket else None,
        "demo-bucket.oss-cn-hangzhou.aliyuncs.com" if bucket else "oss-cn-hangzhou.aliyuncs.com",
        "cn-hangzhou",
    )


def _credential() -> AliyunCredential:
    return AliyunCredential(
        mode="StsToken",
        access_key_id="test-ak",
        access_key_secret="test-secret",
        sts_token="sts-token",
        region_id="cn-hangzhou",
    )


def _budget() -> RetryBudget:
    return RetryBudget(deadline=100.0, clock=lambda: 1.0, random=lambda: 0.0)


def _runtime_openmeta_document(action: str) -> dict[str, Any]:
    operations = json.loads(FIXTURE_PATH.read_text(encoding="ascii"))["operations"]
    row = next(item for item in operations if item["action"] == action)
    parameters = []
    for parameter in row["parameters"]:
        schema = {"type": parameter["type"]}
        if parameter["format"] is not None:
            schema["format"] = parameter["format"]
        parameters.append(
            {
                "name": parameter["name"],
                "in": parameter["location"],
                "required": parameter["required"],
                "schema": schema,
            }
        )
    return {
        "product": "Oss",
        "version": "2019-05-17",
        "action": action,
        "style": "ROA",
        "methods": [row["method"]],
        "path": row["pathname"],
        "schemes": ["HTTPS"],
        "consumes": row["consumes"],
        "produces": row["produces"],
        "operationType": row["operation_type"],
        "security": [{"AK": []}],
        "parameters": parameters,
        "responses": {
            "200": {
                "headers": {
                    "X-Runtime-Declared": {"schema": {"type": "string"}},
                }
            }
        },
    }


class _RuntimeOssEndpointResolver:
    def __init__(self, host_binding_resolver: HostBindingResolver) -> None:
        self.host_binding_resolver = host_binding_resolver
        self.regions: list[str] = []

    async def resolve(self, contract, region_id, credential, *, host_values, explicit_endpoint=None):
        del contract, credential, host_values, explicit_endpoint
        self.regions.append(region_id)
        return EndpointResolution(
            endpoint=f"oss-{region_id}.aliyuncs.com",
            source="catalog_region",
            host_template="{bucket}.{endpoint}",
        )


async def _execute_real_runtime_oss(
    tmp_path: Path,
    *,
    action: str,
    request_region: str,
    credential_region: str,
) -> tuple[Any, FakeSession]:
    async def openmeta_handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/products.json"):
            return httpx.Response(
                200,
                json={
                    "products": [
                        {
                            "product": "Oss",
                            "defaultVersion": "2019-05-17",
                            "versions": ["2019-05-17"],
                        }
                    ]
                },
            )
        return httpx.Response(200, json=_runtime_openmeta_document(action))

    payload = b"object" if action == "GetObject" else b""
    response_headers = [
        ("Content-Length", str(len(payload))),
        ("Content-Type", "application/octet-stream"),
        ("ETag", '"etag-1"'),
        ("X-Oss-Request-Id", "request-1"),
        ("X-Runtime-Declared", "visible"),
    ]
    session = FakeSession(FakeAioResponse([payload] if payload else [], headers=response_headers))
    http_client = _adapter_module().OssStreamingHttpClient(session_factory=lambda **_kwargs: session)
    services = create_aliyun_runtime_services(
        cache_dir=tmp_path / "openmeta",
        openmeta_transport=httpx.MockTransport(openmeta_handler),
    )
    host_binding = HostBindingResolver(("aliyuncs.com",))
    endpoint_resolver = _RuntimeOssEndpointResolver(host_binding)
    adapter = _adapter_module().OssV4Adapter(
        catalog=services.oss_operation_catalog,
        http_client=http_client,
        host_binding_resolver=host_binding,
    )
    services.endpoint_resolver = endpoint_resolver
    services.host_binding_resolver = host_binding
    services.oss_http_client = http_client
    services.transport_router = TransportRouter({"oss_v4_sdk": adapter})
    services.credential_provider = lambda: AliyunCredential(
        access_key_id="test-ak",
        access_key_secret="test-secret",
        region_id=credential_region,
    )
    tool = AliyunApi(services=services)
    tool_input: dict[str, Any] = {
        "product": "Oss",
        "action": action,
        "region_id": request_region,
        "params": {"bucket": "demo-bucket", "key": "folder/demo.txt"},
    }
    if action == "PutObject":
        body_file = tmp_path / "object.bin"
        body_file.write_bytes(b"object")
        tool_input["body_file"] = str(body_file)
    binding = InvocationBinding(
        runtime_nonce="runtime",
        session_id="session",
        tool_use_id="call-1",
        tool_name="aliyun_api",
        canonical_input_sha256=canonical_input_sha256(tool_input),
    )
    permission = await tool.check_permissions(
        tool_input,
        ToolPermissionContext(invocation_binding=binding),
    )
    assert permission.behavior in {"allow", "ask"}
    result = await tool.execute(
        tool_input=tool_input,
        context=ToolContext(
            tool_use_id="call-1",
            invocation_binding=binding,
            snapshot_id=permission.snapshot_id,
            security_digest=permission.security_digest,
            execution_class=permission.execution_class,
        ),
    )
    await services.aclose()
    return result, session


@pytest.mark.asyncio
@pytest.mark.parametrize("action", ["GetObject", "PutObject", "HeadObject"])
async def test_runtime_real_oss_adapter_binds_bucket_exactly_once(
    tmp_path: Path,
    action: str,
) -> None:
    result, session = await _execute_real_runtime_oss(
        tmp_path,
        action=action,
        request_region="cn-hangzhou",
        credential_region="cn-hangzhou",
    )

    assert result.is_error is False
    assert len(session.requests) == 1
    request = session.requests[0]
    assert urlsplit(request["url"]).hostname == "demo-bucket.oss-cn-hangzhou.aliyuncs.com"
    assert request["headers"]["Host"] == "demo-bucket.oss-cn-hangzhou.aliyuncs.com"
    content = json.loads(result.content)
    aliyun_http = result.metadata["aliyun_http"]
    if action == "GetObject":
        assert content == {"data": "b2JqZWN0", "encoding": "base64"}
        assert aliyun_http["body_format"] == "binary_base64_json"
        assert aliyun_http["response_mode"] == "binary"
    else:
        assert content["x-runtime-declared"] == "visible"
        assert aliyun_http["body_format"] == "headers_only_json"
        assert aliyun_http["response_mode"] == "headers_only"
    assert aliyun_http["headers_nonempty"] is True
    assert aliyun_http["header_count"] == 5
    assert "x-runtime-declared" not in aliyun_http


@pytest.mark.asyncio
async def test_runtime_real_oss_adapter_uses_request_region_for_v4_scope(tmp_path: Path) -> None:
    result, session = await _execute_real_runtime_oss(
        tmp_path,
        action="HeadObject",
        request_region="cn-shanghai",
        credential_region="cn-beijing",
    )

    assert result.is_error is False
    request = session.requests[0]
    assert "/cn-shanghai/oss/aliyun_v4_request" in request["headers"]["Authorization"]
    assert "/cn-beijing/oss/aliyun_v4_request" not in request["headers"]["Authorization"]
    assert urlsplit(request["url"]).hostname == "demo-bucket.oss-cn-shanghai.aliyuncs.com"


class FakeStreamBody:
    def __init__(self, events: list[Any]) -> None:
        self.events = events
        self.closed = False
        self.started = asyncio.Event()

    async def iter_bytes(self, **_kwargs: Any) -> Any:
        return self._iterate()

    async def _iterate(self) -> Any:
        self.started.set()
        for event in self.events:
            if isinstance(event, asyncio.Event):
                await event.wait()
            elif isinstance(event, BaseException):
                raise event
            else:
                yield event

    async def close(self) -> None:
        self.closed = True


def _sdk_result(
    *,
    body: FakeStreamBody | None = None,
    status: int = 200,
    headers: list[tuple[str, str]] | None = None,
    **values: Any,
) -> Any:
    raw_headers = CIMultiDict(headers or [])
    return SimpleNamespace(
        status="OK",
        status_code=status,
        request_id=raw_headers.get("x-oss-request-id", ""),
        headers=raw_headers,
        body=body,
        **values,
    )


class FakeSdkClient:
    def __init__(self, module: Any, outcomes: list[Any]) -> None:
        self.module = module
        self.outcomes = list(outcomes)
        self.calls: list[tuple[str, Any, Any]] = []

    def __getattr__(self, name: str) -> Any:
        async def call(request: Any) -> Any:
            self.calls.append((name, request, self.module._OSS_RESPONSE_POLICY.get()))
            if not self.outcomes:
                raise AssertionError(f"unexpected SDK call: {name}")
            outcome = self.outcomes.pop(0)
            if callable(outcome):
                outcome = outcome(request)
            if inspect.isawaitable(outcome):
                outcome = await outcome
            if isinstance(outcome, BaseException):
                raise outcome
            return outcome

        return call


class FakeClientFactory:
    def __init__(self, client: FakeSdkClient) -> None:
        self.client = client
        self.calls: list[tuple[Any, Any]] = []

    def __call__(self, config: Any, signer: Any) -> FakeSdkClient:
        self.calls.append((config, signer))
        return self.client


async def _no_sleep(_delay: float) -> None:
    return None


def _oss_adapter(module: Any, client: FakeSdkClient, *, sleep: Any = _no_sleep) -> tuple[Any, FakeClientFactory]:
    factory = FakeClientFactory(client)
    adapter = module.OssV4Adapter(
        catalog=module.OssOperationCatalog.load(CATALOG_PATH),
        host_binding_resolver=HostBindingResolver(("aliyuncs.com",)),
        client_factory=factory,
        sleep=sleep,
    )
    return adapter, factory


@pytest.mark.asyncio
async def test_late_sdk_result_body_close_finishes_and_chains_error_under_repeated_cancellation() -> None:
    module = _adapter_module()
    sdk_started = asyncio.Event()
    close_started = asyncio.Event()
    release_close = asyncio.Event()
    close_error = OSError("body close failed")
    propagated: list[asyncio.CancelledError] = []

    class FailingCloseBody(FakeStreamBody):
        async def close(self) -> None:
            self.close_started = True
            close_started.set()
            await release_close.wait()
            self.closed = True
            raise close_error

    body = FailingCloseBody([])

    async def late_result(_request: Any) -> Any:
        sdk_started.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            return _sdk_result(body=body, headers=[])

    sdk = FakeSdkClient(module, [late_result])
    adapter, _factory = _oss_adapter(module, sdk)

    async def execute() -> NormalizedApiResponse:
        try:
            return await adapter.execute(
                contract=_oss_contract(),
                request=_oss_request(),
                endpoint=_oss_endpoint(),
                credential=_credential(),
                context=ToolContext(tool_use_id="call-1"),
                budget=_budget(),
            )
        except asyncio.CancelledError as error:
            propagated.append(error)
            raise

    task = asyncio.create_task(execute())
    await sdk_started.wait()

    try:
        task.cancel("first cancellation")
        await close_started.wait()
        task.cancel("second cancellation")
        await asyncio.sleep(0)

        assert not task.done()
        release_close.set()
        with pytest.raises(asyncio.CancelledError):
            await task

        assert propagated[0].args == ("first cancellation",)
        assert propagated[0].__cause__ is close_error
        assert isinstance(close_error.__cause__, asyncio.CancelledError)
        assert close_error.__cause__.args == ("second cancellation",)
        assert body.closed is True
    finally:
        release_close.set()
        await asyncio.gather(task, return_exceptions=True)
        await adapter.aclose()


@pytest.mark.asyncio
async def test_oss_adapter_uses_assumed_role_credential_for_sdk_client(monkeypatch: pytest.MonkeyPatch) -> None:
    module = _adapter_module()
    client = FakeSdkClient(module, [_sdk_result(status=200)])
    adapter, factory = _oss_adapter(module, client)
    source = AliyunCredential(
        mode="RamRoleArn",
        access_key_id="source-ak",
        access_key_secret="source-secret",
        ram_role_arn="acs:ram::123456789012:role/restricted",
        ram_session_name="iac-code-test",
        region_id="cn-hangzhou",
    )
    resolved = AliyunCredential(
        mode="StsToken",
        access_key_id="role-ak",
        access_key_secret="role-secret",
        sts_token="role-sts",
        region_id="cn-hangzhou",
    )

    async def resolve(credential: AliyunCredential) -> AliyunCredential:
        assert credential is source
        return resolved

    monkeypatch.setattr(module, "resolve_signing_credential", resolve, raising=False)

    await adapter.execute(
        contract=_oss_contract(action="HeadObject", method="HEAD", response_body_type="none"),
        request=_oss_request(method="HEAD"),
        endpoint=_oss_endpoint(),
        credential=source,
        context=ToolContext(),
        budget=_budget(),
    )

    credentials = factory.calls[0][0].credentials_provider.get_credentials()
    assert (credentials.access_key_id, credentials.access_key_secret, credentials.security_token) == (
        "role-ak",
        "role-secret",
        "role-sts",
    )


@pytest.mark.asyncio
async def test_oss_adapter_uses_ecs_metadata_credential_for_sdk_client(fake_ecs_runtime: FakeEcsRuntime) -> None:
    module = _adapter_module()
    client = FakeSdkClient(module, [_sdk_result(status=200)])
    adapter, factory = _oss_adapter(module, client)
    loop_thread = threading.get_ident()

    await adapter.execute(
        contract=_oss_contract(action="HeadObject", method="HEAD", response_body_type="none"),
        request=_oss_request(method="HEAD"),
        endpoint=_oss_endpoint(),
        credential=fake_ecs_runtime.credential(region_id="cn-hangzhou"),
        context=ToolContext(),
        budget=_budget(),
    )

    credentials = factory.calls[0][0].credentials_provider.get_credentials()
    assert (credentials.access_key_id, credentials.access_key_secret, credentials.security_token) == (
        "STS.fake-ecs-ak",
        "fake-ecs-secret",
        "fake-ecs-sts",
    )
    # The V4 signer needs the requested region, which must survive the credential swap.
    assert factory.calls[0][0].region == "cn-hangzhou"
    provider = fake_ecs_runtime.providers[0]
    assert provider.call_threads and all(ident != loop_thread for ident in provider.call_threads)
    assert provider.async_calls == 0


@pytest.mark.asyncio
async def test_oss_adapter_fails_before_building_the_sdk_client_when_metadata_is_disabled(
    fake_ecs_runtime: FakeEcsRuntime, monkeypatch: pytest.MonkeyPatch
) -> None:
    from iac_code.services.providers.aliyun_credentials_runtime import ECS_METADATA_DISABLED

    module = _adapter_module()
    client = FakeSdkClient(module, [_sdk_result(status=200)])
    adapter, factory = _oss_adapter(module, client)
    monkeypatch.setenv("ALIBABA_CLOUD_ECS_METADATA_DISABLED", "true")

    with pytest.raises(ValueError) as raised:
        await adapter.execute(
            contract=_oss_contract(action="HeadObject", method="HEAD", response_body_type="none"),
            request=_oss_request(method="HEAD"),
            endpoint=_oss_endpoint(),
            credential=fake_ecs_runtime.credential(),
            context=ToolContext(),
            budget=_budget(),
        )

    assert str(raised.value) == ECS_METADATA_DISABLED
    assert factory.calls == []
    assert client.calls == []


@pytest.mark.asyncio
async def test_locked_sdk_get_object_returns_lazy_stream_before_body_read() -> None:
    module = _adapter_module()
    gate = asyncio.Event()
    raw = FakeAioResponse(
        [b"payload"],
        headers=[("Content-Length", "7"), ("ETag", '"etag-1"')],
        gate=gate,
    )
    session = FakeSession(raw)
    adapter = module.OssV4Adapter(
        catalog=module.OssOperationCatalog.load(CATALOG_PATH),
        http_client=module.OssStreamingHttpClient(session_factory=lambda **_kwargs: session),
        host_binding_resolver=HostBindingResolver(("aliyuncs.com",)),
    )
    operation = adapter.catalog.require("GetObject")
    model_module, _, model_name = operation.request_model.rpartition(".")
    request_model = getattr(importlib.import_module(model_module), model_name)
    sdk_request = request_model(bucket="demo-bucket", key="folder/demo.txt")
    client = adapter._client(endpoint=_oss_endpoint(), credential=_credential())

    result = await asyncio.wait_for(
        adapter._invoke(client, operation, sdk_request, _policy(module, mode="stream")),
        timeout=0.5,
    )

    assert raw.content.read_calls == raw.content.iter_calls == 0
    assert result.body is not None
    await result.body.close()
    assert raw.closed is True
    await adapter.aclose()


@pytest.mark.asyncio
async def test_adapter_uses_locked_virtual_host_config_maps_raw_put_and_filters_headers() -> None:
    module = _adapter_module()
    sdk = FakeSdkClient(
        module,
        [
            _sdk_result(
                headers=[
                    ("X-Oss-Request-Id", "request-1"),
                    ("Content-Length", "0"),
                    ("X-Result-Token", "visible"),
                    ("Authorization", "secret"),
                    ("X-Oss-Security-Token", "secret"),
                ],
                etag="etag-1",
            )
        ],
    )
    adapter, factory = _oss_adapter(module, sdk)
    contract = _oss_contract(
        action="PutObject",
        method="PUT",
        operation_type="write",
        request_body_type="byte",
        response_body_type="none",
        parameters=(
            _parameter("bucket", "host", required=True),
            _parameter("key", "path", required=True),
            _parameter("x-oss-meta-*", "header", schema_type="object"),
            _parameter("body", "body"),
        ),
    )
    request = _oss_request(
        method="PUT",
        headers=MappingProxyType(
            {
                "content-type": "application/octet-stream",
                "x-oss-meta-owner": "alice",
            }
        ),
        body=b"\x00raw-body",
    )
    result = await adapter.execute(
        contract=contract,
        request=request,
        endpoint=_oss_endpoint(),
        credential=_credential(),
        context=ToolContext(tool_use_id="call-1"),
        budget=_budget(),
    )

    assert isinstance(result, NormalizedApiResponse)
    assert result.status == 200
    assert dict(result.headers) == {
        "x-oss-request-id": "request-1",
        "content-length": "0",
        "x-result-token": "visible",
    }
    assert dict(result.body) == {"etag": "etag-1"}
    assert len(sdk.calls) == 1
    method_name, sdk_request, policy = sdk.calls[0]
    assert method_name == "put_object"
    assert sdk_request.bucket == "demo-bucket"
    assert sdk_request.key == "folder/demo.txt"
    assert sdk_request.body == b"\x00raw-body"
    assert sdk_request.content_type == "application/octet-stream"
    assert dict(sdk_request.metadata) == {"owner": "alice"}
    assert policy.mode == "buffered"
    assert policy.expected_host == "demo-bucket.oss-cn-hangzhou.aliyuncs.com"
    config, signer = factory.calls[0]
    assert config.region == "cn-hangzhou"
    assert config.endpoint == "oss-cn-hangzhou.aliyuncs.com"
    assert config.signature_version == "v4"
    assert config.retry_max_attempts == 1
    assert config.use_cname is False
    assert config.use_accelerate_endpoint is False
    assert config.use_path_style is False
    assert config.disable_ssl is False
    assert config.enabled_redirect is False
    assert config.additional_headers == ["accept-encoding", "host"]
    assert isinstance(signer, module.OssIdentitySigner)
    await adapter.aclose()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("phase", "inner", "expected_outcome"),
    [
        (
            "connector",
            aiohttp.ClientConnectorError(
                SimpleNamespace(ssl=False, host="example.com", port=443),
                OSError("connect"),
            ),
            "pre_connect_failure",
        ),
        ("headers", aiohttp.ClientConnectionError("headers failed"), "unknown_after_transport_error"),
        ("body", aiohttp.ClientOSError(1, "body write failed"), "unknown_after_transport_error"),
        ("read", aiohttp.ClientPayloadError("response read failed"), "unknown_after_transport_error"),
    ],
)
async def test_put_object_classifies_transport_phase_without_retry(
    phase: str,
    inner: BaseException,
    expected_outcome: str,
) -> None:
    del phase
    module = _adapter_module()
    wrapped = oss_exceptions.RequestError(error=inner)
    sdk = FakeSdkClient(module, [wrapped])
    adapter, _factory = _oss_adapter(module, sdk)
    shared_budget = _budget()

    with pytest.raises(TransportFailure) as caught:
        await adapter.execute(
            contract=_oss_contract(
                action="PutObject",
                method="PUT",
                operation_type="write",
                request_body_type="byte",
                response_body_type="none",
                parameters=(
                    _parameter("bucket", "host", required=True),
                    _parameter("key", "path", required=True),
                    _parameter("body", "body"),
                ),
            ),
            request=_oss_request(
                method="PUT",
                headers=MappingProxyType({"content-type": "application/octet-stream"}),
                body=b"object",
            ),
            endpoint=_oss_endpoint(),
            credential=_credential(),
            context=ToolContext(tool_use_id="call-1"),
            budget=shared_budget,
        )

    assert caught.value.outcome == expected_outcome
    assert caught.value.__cause__ is wrapped
    assert shared_budget.attempts == 1
    assert len(sdk.calls) == 1
    await adapter.aclose()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "setting",
    ["use_cname", "use_accelerate_endpoint", "use_path_style", "disable_ssl", "enabled_redirect"],
)
async def test_locked_sdk_config_rejects_unsafe_addressing_and_transport_modes(setting: str) -> None:
    module = _adapter_module()
    adapter, factory = _oss_adapter(module, FakeSdkClient(module, []))
    adapter._client(endpoint=_oss_endpoint(), credential=_credential())
    config, _signer = factory.calls[0]
    setattr(config, setting, True)

    with pytest.raises(ValueError, match="unsafe_oss_sdk_config"):
        module._validate_locked_config(config)

    await adapter.aclose()


@pytest.mark.asyncio
async def test_get_object_retry_returns_complete_base64_body() -> None:
    module = _adapter_module()
    first_body = FakeStreamBody([b"part", aiohttp.ClientPayloadError("broken")])
    second_body = FakeStreamBody([b"complete"])
    headers = [
        ("ETag", '"etag-1"'),
        ("Content-Length", "8"),
        ("Content-Type", "application/octet-stream"),
        ("Content-Encoding", "gzip"),
        ("X-Oss-Request-Id", "request-1"),
        ("X-Result-Token", "visible"),
    ]
    sdk = FakeSdkClient(
        module,
        [
            _sdk_result(body=first_body, headers=headers, etag='"etag-1"', content_length=8),
            _sdk_result(body=second_body, headers=headers, etag='"etag-1"', content_length=8),
        ],
    )
    adapter, _factory = _oss_adapter(module, sdk)
    retry_budget = _budget()

    result = await adapter.execute(
        contract=_oss_contract(),
        request=_oss_request(),
        endpoint=_oss_endpoint(),
        credential=_credential(),
        context=ToolContext(),
        budget=retry_budget,
    )

    assert retry_budget.attempts == 2
    assert sdk.calls[0][1].if_match is None
    assert sdk.calls[1][1].if_match == '"etag-1"'
    assert first_body.closed is second_body.closed is True
    assert result.body == {"encoding": "base64", "data": "Y29tcGxldGU="}
    assert result.size == 8
    assert result.content_encoding == "gzip"
    assert dict(result.headers)["x-result-token"] == "visible"
    await adapter.aclose()


@pytest.mark.asyncio
async def test_get_object_short_read_without_etag_is_terminal() -> None:
    module = _adapter_module()
    body = FakeStreamBody([b"part"])
    sdk = FakeSdkClient(
        module,
        [_sdk_result(body=body, headers=[("Content-Length", "8")], etag=None, content_length=8)],
    )
    adapter, _factory = _oss_adapter(module, sdk)

    with pytest.raises(TransportFailure):
        await adapter.execute(
            contract=_oss_contract(),
            request=_oss_request(),
            endpoint=_oss_endpoint(),
            credential=_credential(),
            context=ToolContext(),
            budget=_budget(),
        )

    assert len(sdk.calls) == 1
    assert body.closed is True
    await adapter.aclose()


@pytest.mark.asyncio
async def test_get_object_zero_length_returns_empty_base64_body() -> None:
    module = _adapter_module()
    body = FakeStreamBody([])
    sdk = FakeSdkClient(
        module,
        [_sdk_result(body=body, headers=[("Content-Length", "0")], content_length=0)],
    )
    adapter, _factory = _oss_adapter(module, sdk)

    result = await adapter.execute(
        contract=_oss_contract(),
        request=_oss_request(),
        endpoint=_oss_endpoint(),
        credential=_credential(),
        context=ToolContext(),
        budget=_budget(),
    )

    assert result.body == {"encoding": "base64", "data": ""}
    assert result.size == 0
    assert body.closed is True
    await adapter.aclose()


@pytest.mark.asyncio
async def test_get_object_enforces_bounded_in_memory_limit() -> None:
    module = _adapter_module()
    body = FakeStreamBody([b"12345"])
    sdk = FakeSdkClient(module, [_sdk_result(body=body, headers=[])])
    adapter, _factory = _oss_adapter(module, sdk)

    with pytest.raises(Exception, match="response_too_large"):
        await adapter.execute(
            contract=_oss_contract(),
            request=_oss_request(max_bytes=4),
            endpoint=_oss_endpoint(),
            credential=_credential(),
            context=ToolContext(),
            budget=_budget(),
        )

    assert body.closed is True
    await adapter.aclose()


@pytest.mark.asyncio
async def test_get_object_cancellation_closes_body_and_resets_policy() -> None:
    module = _adapter_module()
    gate = asyncio.Event()
    body = FakeStreamBody([gate])
    sdk = FakeSdkClient(module, [_sdk_result(body=body, headers=[("ETag", '"one"')], etag='"one"')])
    adapter, _factory = _oss_adapter(module, sdk)
    task = asyncio.create_task(
        adapter.execute(
            contract=_oss_contract(),
            request=_oss_request(),
            endpoint=_oss_endpoint(),
            credential=_credential(),
            context=ToolContext(),
            budget=_budget(),
        )
    )

    await body.started.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert body.closed is True
    assert module._OSS_RESPONSE_POLICY.get() is None
    await adapter.aclose()


@pytest.mark.asyncio
async def test_unsupported_catalog_action_fails_before_sdk_client_or_network() -> None:
    module = _adapter_module()
    sdk = FakeSdkClient(module, [])
    adapter, factory = _oss_adapter(module, sdk)
    with pytest.raises(ValueError, match="oss_operation_unsupported"):
        await adapter.execute(
            contract=_oss_contract(action="CompleteMultipartUpload", method="POST", operation_type="write"),
            request=_oss_request(method="POST"),
            endpoint=_oss_endpoint(),
            credential=_credential(),
            context=ToolContext(tool_use_id="call-1"),
            budget=_budget(),
        )
    assert factory.calls == []
    assert sdk.calls == []
    await adapter.aclose()


@pytest.mark.asyncio
async def test_adapter_rejects_caller_accept_encoding_before_sdk() -> None:
    module = _adapter_module()
    sdk = FakeSdkClient(module, [])
    adapter, factory = _oss_adapter(module, sdk)
    with pytest.raises(ValueError, match="caller_accept_encoding_forbidden"):
        await adapter.execute(
            contract=_oss_contract(),
            request=_oss_request(headers=MappingProxyType({"Accept-Encoding": "identity"})),
            endpoint=_oss_endpoint(),
            credential=_credential(),
            context=ToolContext(tool_use_id="call-1"),
            budget=_budget(),
        )
    assert factory.calls == []
    await adapter.aclose()


@pytest.mark.asyncio
async def test_adapter_rejects_explicit_accelerate_endpoint_before_sdk() -> None:
    module = _adapter_module()
    sdk = FakeSdkClient(module, [])
    adapter, factory = _oss_adapter(module, sdk)
    endpoint = EndpointResolution("oss-accelerate.aliyuncs.com", "catalog_global", "{bucket}.{endpoint}")
    with pytest.raises(ValueError, match="oss_accelerate_forbidden"):
        await adapter.execute(
            contract=_oss_contract(),
            request=_oss_request(),
            endpoint=endpoint,
            credential=_credential(),
            context=ToolContext(tool_use_id="call-1"),
            budget=_budget(),
        )
    assert factory.calls == []
    await adapter.aclose()


class TrackingPolicyVar:
    def __init__(self) -> None:
        self.inner: contextvars.ContextVar[Any | None] = contextvars.ContextVar("tracking_oss_policy", default=None)
        self.set_tokens: list[Any] = []
        self.reset_tokens: list[Any] = []

    def set(self, value: Any) -> Any:
        token = self.inner.set(value)
        self.set_tokens.append(token)
        return token

    def reset(self, token: Any) -> None:
        self.reset_tokens.append(token)
        self.inner.reset(token)

    def get(self) -> Any:
        return self.inner.get()


@pytest.mark.asyncio
async def test_policy_token_resets_exactly_once_on_sdk_success_exception_and_cancellation(monkeypatch: Any) -> None:
    module = _adapter_module()
    tracker = TrackingPolicyVar()
    monkeypatch.setattr(module, "_OSS_RESPONSE_POLICY", tracker)
    operation = module.OssOperationCatalog.load(CATALOG_PATH).require("ListBuckets")
    policy = _policy(module, mode="buffered")

    success_client = FakeSdkClient(module, [_sdk_result(headers=[], buckets=[])])
    adapter, _factory = _oss_adapter(module, success_client)
    await adapter._invoke(success_client, operation, object(), policy)
    assert tracker.get() is None

    error_client = FakeSdkClient(module, [RuntimeError("sdk failed")])
    with pytest.raises(RuntimeError, match="sdk failed"):
        await adapter._invoke(error_client, operation, object(), policy)
    assert tracker.get() is None

    started = asyncio.Event()
    release = asyncio.Event()

    async def blocked(_request: Any) -> Any:
        started.set()
        await release.wait()

    cancel_client = FakeSdkClient(module, [blocked])
    task = asyncio.create_task(adapter._invoke(cancel_client, operation, object(), policy))
    await started.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert tracker.get() is None
    assert len(tracker.set_tokens) == len(tracker.reset_tokens) == 3
    assert tracker.reset_tokens == tracker.set_tokens
    await adapter.aclose()


async def _collect_request_body(body: Any) -> bytes:
    if body is None:
        return b""
    if isinstance(body, str):
        return body.encode()
    if isinstance(body, bytes):
        return body
    if hasattr(body, "__aiter__"):
        chunks = []
        async for chunk in body:
            chunks.append(bytes(chunk))
        return b"".join(chunks)
    return b"".join(bytes(chunk) for chunk in body)


@pytest.mark.asyncio
async def test_real_sdk_uses_virtual_host_and_signs_identity_content_type_metadata_and_sts() -> None:
    module = _adapter_module()
    raw = FakeAioResponse(
        [],
        headers=[("Content-Length", "0"), ("X-Oss-Request-Id", "request-1")],
    )
    session = FakeSession(raw)
    http_client = module.OssStreamingHttpClient(session_factory=lambda **_kwargs: session)
    adapter = module.OssV4Adapter(
        catalog=module.OssOperationCatalog.load(CATALOG_PATH),
        http_client=http_client,
        host_binding_resolver=HostBindingResolver(("aliyuncs.com",)),
    )
    contract = _oss_contract(
        action="PutObject",
        method="PUT",
        operation_type="write",
        request_body_type="byte",
        response_body_type="none",
        parameters=(
            _parameter("bucket", "host", required=True),
            _parameter("key", "path", required=True),
            _parameter("x-oss-meta-*", "header", schema_type="object"),
            _parameter("body", "body"),
        ),
    )
    result = await adapter.execute(
        contract=contract,
        request=_oss_request(
            method="PUT",
            headers=MappingProxyType({"content-type": "application/octet-stream", "x-oss-meta-owner": "alice"}),
            body=b"raw-body",
        ),
        endpoint=_oss_endpoint(),
        credential=_credential(),
        context=ToolContext(tool_use_id="call-1"),
        budget=_budget(),
    )
    sent = session.requests[0]
    assert sent["url"] == "https://demo-bucket.oss-cn-hangzhou.aliyuncs.com/folder/demo.txt"
    assert sent["headers"]["Accept-Encoding"] == "identity"
    assert sent["headers"]["Content-Type"] == "application/octet-stream"
    assert sent["headers"]["x-oss-meta-owner"] == "alice"
    assert sent["headers"]["x-oss-security-token"] == "sts-token"
    assert "AdditionalHeaders=accept-encoding;host" in sent["headers"]["Authorization"]
    assert "Credential=test-ak/" in sent["headers"]["Authorization"]
    assert await _collect_request_body(sent["data"]) == b"raw-body"
    assert result.status == 200
    assert module._OSS_RESPONSE_POLICY.get() is None
    await adapter.aclose()


@pytest.mark.asyncio
async def test_real_sdk_get_object_retry_signs_if_match_only_when_present() -> None:
    module = _adapter_module()
    first = FakeAioResponse(
        [b"part"],
        headers=[("Content-Length", "8"), ("ETag", '"etag-1"')],
    )
    second = FakeAioResponse(
        [b"complete"],
        headers=[("Content-Length", "8"), ("ETag", '"etag-1"')],
    )
    session = SequenceSession([first, second])
    adapter = module.OssV4Adapter(
        catalog=module.OssOperationCatalog.load(CATALOG_PATH),
        http_client=module.OssStreamingHttpClient(session_factory=lambda **_kwargs: session),
        host_binding_resolver=HostBindingResolver(("aliyuncs.com",)),
        sleep=_no_sleep,
    )
    result = await adapter.execute(
        contract=_oss_contract(),
        request=_oss_request(),
        endpoint=_oss_endpoint(),
        credential=_credential(),
        context=ToolContext(),
        budget=_budget(),
    )

    first_request, retry_request = session.requests
    assert "If-Match" not in first_request["headers"]
    assert "AdditionalHeaders=accept-encoding;host" in first_request["headers"]["Authorization"]
    assert retry_request["headers"]["If-Match"] == '"etag-1"'
    assert "AdditionalHeaders=accept-encoding;host;if-match" in retry_request["headers"]["Authorization"]
    assert first_request["headers"]["Authorization"] != retry_request["headers"]["Authorization"]
    assert result.body == {"encoding": "base64", "data": "Y29tcGxldGU="}
    await adapter.aclose()


@pytest.mark.asyncio
async def test_real_sdk_xml_error_uses_one_mib_error_mode_and_shared_header_filter() -> None:
    module = _adapter_module()
    payload = b"<Error><Code>NoSuchKey</Code><Message>missing</Message><RequestId>request-1</RequestId></Error>"
    raw = FakeAioResponse(
        [payload],
        status=404,
        headers=[
            ("Date", "Sun, 12 Jul 2026 01:02:03 GMT"),
            ("Content-Type", "application/xml"),
            ("Content-Length", str(len(payload))),
            ("X-Oss-Request-Id", "request-1"),
            ("Authorization", "secret"),
        ],
    )
    session = FakeSession(raw)
    adapter = module.OssV4Adapter(
        catalog=module.OssOperationCatalog.load(CATALOG_PATH),
        http_client=module.OssStreamingHttpClient(session_factory=lambda **_kwargs: session),
        host_binding_resolver=HostBindingResolver(("aliyuncs.com",)),
    )
    result = await adapter.execute(
        contract=_oss_contract(),
        request=_oss_request(),
        endpoint=_oss_endpoint(),
        credential=_credential(),
        context=ToolContext(),
        budget=_budget(),
    )
    assert result.status == 404
    assert result.body["Code"] == "NoSuchKey"
    assert result.body["Message"] == "missing"
    assert dict(result.headers) == {
        "content-type": "application/xml",
        "content-length": str(len(payload)),
        "x-oss-request-id": "request-1",
    }
    assert raw.closed is True
    await adapter.aclose()


@pytest.mark.asyncio
async def test_cancelled_raw_http_stream_closes_underlying_connection() -> None:
    module = _adapter_module()
    gate = asyncio.Event()
    raw = FakeAioResponse([b"data"], gate=gate)
    client = module.OssStreamingHttpClient(session_factory=lambda **_kwargs: FakeSession(raw))
    response = await _send_with_policy(client, module, _policy(module, mode="stream"))

    async def consume() -> None:
        async for _chunk in response.iter_bytes():
            pass

    task = asyncio.create_task(consume())
    while raw.content.iter_calls == 0:
        await asyncio.sleep(0)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert raw.closed is True
    await client.close()


@pytest.mark.asyncio
@pytest.mark.skipif(sys.platform == "win32", reason="resource.getrusage is unavailable on Windows")
async def test_unread_stream_rss_does_not_scale_with_declared_payload_size() -> None:
    import resource

    module = _adapter_module()
    gc.collect()
    before = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    raw = FakeAioResponse([], headers=[("Content-Length", str(256 * 1024**2))])
    client = module.OssStreamingHttpClient(session_factory=lambda **_kwargs: FakeSession(raw))
    response = await _send_with_policy(
        client,
        module,
        _policy(module, mode="stream", max_bytes=300 * 1024**2),
    )
    after = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    scale = 1 if sys.platform == "darwin" else 1024
    assert (after - before) * scale < 8 * 1024**2
    assert raw.content.read_calls == raw.content.iter_calls == 0
    await response.close()
    await client.close()


def _field_value(field: Any) -> str:
    if "bool" in field.sdk_type.casefold():
        return "true"
    if "int" in field.sdk_type.casefold():
        return "1"
    return "value"


@pytest.mark.asyncio
async def test_every_supported_catalog_row_reaches_its_declared_adapter_branch() -> None:
    module = _adapter_module()
    catalog = module.OssOperationCatalog.load(CATALOG_PATH)
    supported = [operation for operation in catalog.operations if operation.supported]
    assert len(supported) == 33
    for operation in supported:
        host_fields = [field for field in operation.field_mapping if field.location == "host"]
        path_fields = [field for field in operation.field_mapping if field.location == "path"]
        parameters = tuple(
            _parameter(
                field.openmeta_name,
                field.location,
                required=field.required,
                schema_type=("integer" if "int" in field.sdk_type.casefold() else "string"),
            )
            for field in operation.field_mapping
        )
        query = tuple(
            (field.wire_name, _field_value(field))
            for field in operation.field_mapping
            if field.location == "query" and field.required
        )
        headers = {
            field.wire_name: _field_value(field)
            for field in operation.field_mapping
            if field.location == "header" and field.required and field.openmeta_name.casefold() != "accept-encoding"
        }
        host_values = MappingProxyType({field.openmeta_name: "demo-bucket" for field in host_fields})
        body = b"body" if operation.body_type == "byte" else None
        if operation.response_mode == "stream":
            sdk_result = _sdk_result(
                body=FakeStreamBody([]),
                headers=[("ETag", '"etag"'), ("Content-Length", "0")],
                etag='"etag"',
                content_length=0,
            )
        else:
            sdk_result = _sdk_result(headers=[("Content-Length", "0")], value="ok")
        sdk = FakeSdkClient(module, [sdk_result])
        adapter, _factory = _oss_adapter(module, sdk)
        result = await adapter.execute(
            contract=_oss_contract(
                action=operation.action,
                method=operation.method,
                operation_type="read" if operation.method in {"GET", "HEAD", "OPTIONS"} else "write",
                parameters=parameters,
                request_body_type="byte" if body is not None else "none",
                response_body_type="binary" if operation.response_mode == "stream" else "json",
            ),
            request=_oss_request(
                method=operation.method,
                raw_path=b"/demo.txt" if path_fields else b"/",
                query=query,
                headers=MappingProxyType(headers),
                body=body,
                host_values=host_values,
            ),
            endpoint=_oss_endpoint(bucket=bool(host_fields)),
            credential=_credential(),
            context=ToolContext(),
            budget=_budget(),
        )
        assert sdk.calls[0][0] == operation.sdk_method
        assert sdk.calls[0][2].mode == operation.response_mode
        assert result.status == 200
        await adapter.aclose()


class FakeOssOpenMeta:
    def __init__(self, raw: dict[str, Any]) -> None:
        self.raw = raw

    async def get_product(self, _product: str) -> MetadataFetch[Any]:
        return MetadataFetch(value=None, source=None, error="temporarily_unavailable")

    async def get_api(self, _product: str, _version: str, _action: str) -> MetadataFetch[Any]:
        return MetadataFetch(value=normalize_api_metadata(self.raw), source="fresh", error=None)


class UnavailableOssOpenMeta:
    async def get_product(self, _product: str) -> MetadataFetch[Any]:
        return MetadataFetch(value=None, source=None, error="temporarily_unavailable")

    async def get_api(self, _product: str, _version: str, _action: str) -> MetadataFetch[Any]:
        return MetadataFetch(value=None, source=None, error="temporarily_unavailable")


def _oss_raw_api(action: str, method: str = "GET") -> dict[str, Any]:
    return {
        "product": "Oss",
        "version": "2019-05-17",
        "action": action,
        "style": "ROA",
        "methods": [method],
        "path": "/{key}",
        "schemes": ["HTTPS"],
        "security": [{"AK": []}],
        "operationType": "read" if method in {"GET", "HEAD"} else "write",
        "parameters": [
            {"name": "bucket", "in": "host", "required": True, "schema": {"type": "string"}},
            {"name": "key", "in": "path", "required": True, "schema": {"type": "string"}},
        ],
        "produces": ["application/octet-stream"],
        "responses": {"200": {"schema": {"type": "string", "format": "binary"}}},
    }


def _oss_shape(action: str) -> ApiCallShape:
    return ApiCallShape(
        product="Oss",
        version="2019-05-17",
        action=action,
        region_id="cn-hangzhou",
        explicit_overrides=(),
        parameter_names_by_location=MappingProxyType({"host": ("bucket",), "path": ("key",)}),
        body_source="none",
    )


@pytest.mark.asyncio
async def test_contract_resolver_merges_oss_catalog_support_and_digest_before_execution() -> None:
    module = _adapter_module()
    catalog = module.OssOperationCatalog.load(CATALOG_PATH)
    supported = await ApiContractResolver(
        FakeOssOpenMeta(_oss_raw_api("GetObject")),
        oss_catalog=catalog,
    ).resolve(_oss_shape("GetObject"), allow_fallback=False)
    assert supported.executable is True
    assert supported.unsupported_reasons == ()
    assert (supported.transport, supported.signature_scheme) == ("oss_v4_sdk", "oss_v4")
    assert supported.oss_catalog_schema_version == 1
    assert supported.oss_catalog_digest == catalog.policy_digest
    assert supported.oss_sdk_version == "1.3.2"

    unsupported = await ApiContractResolver(
        FakeOssOpenMeta(_oss_raw_api("CompleteMultipartUpload", "POST")),
        oss_catalog=catalog,
    ).resolve(_oss_shape("CompleteMultipartUpload"), allow_fallback=False)
    assert unsupported.executable is False
    assert unsupported.unsupported_reasons == catalog.require("CompleteMultipartUpload").unsupported_reasons

    unknown = await ApiContractResolver(
        FakeOssOpenMeta(_oss_raw_api("UnknownOperation")),
        oss_catalog=catalog,
    ).resolve(_oss_shape("UnknownOperation"), allow_fallback=False)
    assert unknown.executable is False
    assert unknown.unsupported_reasons == ("oss_operation_not_cataloged",)


@pytest.mark.asyncio
async def test_openmeta_unavailable_oss_fallback_stops_before_request_builder() -> None:
    module = _adapter_module()
    catalog = module.OssOperationCatalog.load(CATALOG_PATH)
    shape = ApiCallShape(
        product="Oss",
        version="2019-05-17",
        action="GetObject",
        region_id="cn-hangzhou",
        explicit_overrides=("style", "method", "pathname"),
        parameter_names_by_location=MappingProxyType({"host": ("bucket",), "path": ("key",)}),
        body_source="none",
        style="ROA",
        method="GET",
        pathname="/{key}",
    )

    resolved = await ApiContractResolver(
        UnavailableOssOpenMeta(),
        oss_catalog=catalog,
    ).resolve(shape, allow_fallback=True)

    assert resolved.executable is False
    assert "oss_openmeta_required_for_complete_request" in resolved.unsupported_reasons


@pytest.mark.asyncio
async def test_single_runtime_owns_catalog_router_adapter_and_http_lifecycle(tmp_path: Path) -> None:
    module = _adapter_module()
    runtime = create_aliyun_runtime_services(cache_dir=tmp_path)
    adapter = runtime.transport_router._transports["oss_v4_sdk"]
    assert isinstance(adapter, module.OssV4Adapter)
    assert runtime.oss_operation_catalog is adapter.catalog
    assert runtime.contract_resolver._oss_catalog is runtime.oss_operation_catalog
    assert runtime.oss_http_client is adapter.http_client
    await runtime.aclose()
    assert adapter._closed is True


def test_ci_tracks_and_regenerates_oss_catalog() -> None:
    workflow = (ROOT / ".github/workflows/test.yml").read_text(encoding="utf-8")
    assert workflow.count('"scripts/aliyun/**"') == 2
    assert "scripts/aliyun/generate_oss_operations.py wheel-hash --lockfile uv.lock" in workflow
    assert "--openmeta-fixture tests/tools/cloud/aliyun/fixtures/oss/openmeta_operations.json" in workflow
    assert 'cmp "$oss_catalog" src/iac_code/tools/cloud/aliyun/data/oss/operation_catalog.json' in workflow
