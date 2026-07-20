"""Alibaba Cloud OSS V4 SDK adapter with bounded raw async responses."""

from __future__ import annotations

import asyncio
import base64
import contextvars
import hashlib
import importlib
import inspect
import json
import re
from collections.abc import Callable, Mapping
from dataclasses import dataclass, replace
from datetime import datetime
from enum import Enum
from pathlib import Path
from types import MappingProxyType
from typing import Any, Literal, cast
from urllib.parse import unquote, urlsplit

import aiohttp
from alibabacloud_oss_v2 import Config
from alibabacloud_oss_v2 import exceptions as oss_exceptions
from alibabacloud_oss_v2.aio.client import AsyncClient
from alibabacloud_oss_v2.credentials import StaticCredentialsProvider
from alibabacloud_oss_v2.signer.v4 import SignerV4
from alibabacloud_oss_v2.types import AsyncHttpClient, AsyncHttpResponse, HttpRequest, SigningContext
from requests.structures import CaseInsensitiveDict

from iac_code.services.providers.aliyun import AliyunCredential
from iac_code.tools.base import ToolContext
from iac_code.tools.cloud.aliyun.acs3_transport import (
    NormalizedApiResponse,
    ResponseTooLarge,
    filter_response_headers,
    resolve_signing_credential,
)
from iac_code.tools.cloud.aliyun.api_contract import BuiltApiRequest, CanonicalWireContract
from iac_code.tools.cloud.aliyun.endpoint_resolver import EndpointResolution, HostBindingResolver
from iac_code.tools.cloud.aliyun.retry_policy import (
    RetryBudget,
    RetryExhausted,
    RetryReason,
    TransportFailure,
    map_aiohttp_retry_reason,
    map_retryable_status,
    retry_delay,
    retry_eligible,
)

_CATALOG_PATH = Path(__file__).parent / "data" / "oss" / "operation_catalog.json"
_BUFFERED_RESPONSE_BYTES = 16 * 1024**2
_ERROR_RESPONSE_BYTES = 1024**2
_STREAM_CHUNK_BYTES = 16 * 1024
_AUTH_ADDITIONAL_HEADERS = re.compile(r"(?:^|,)AdditionalHeaders=([^,]+)")
_PATH_FIELD = re.compile(r"\{([^{}]+)\}")


@dataclass(frozen=True)
class OssFieldMapping:
    location: str
    openmeta_name: str
    required: bool
    sdk_field: str
    sdk_type: str
    wire_name: str


@dataclass(frozen=True)
class OssOperationSpec:
    action: str | None
    body_type: str
    field_mapping: tuple[OssFieldMapping, ...]
    method: str | None
    request_model: str | None
    response_mode: Literal["stream", "headers_only", "buffered", "unsupported"]
    sdk_method: str
    supported: bool
    unsupported_reasons: tuple[str, ...]


class OssOperationCatalog:
    """Validated immutable view of the generated locked-SDK operation policy."""

    def __init__(self, document: Mapping[str, Any], *, policy_digest: str) -> None:
        meta = document.get("_meta")
        rows = document.get("operations")
        if not isinstance(meta, Mapping) or meta.get("schema_version") != 1 or not isinstance(rows, list):
            raise ValueError("invalid_oss_operation_catalog")
        operations: list[OssOperationSpec] = []
        methods: set[str] = set()
        actions: set[str] = set()
        for raw in rows:
            operation = _operation_spec(raw)
            if operation.sdk_method in methods:
                raise ValueError("duplicate_oss_sdk_method")
            methods.add(operation.sdk_method)
            if operation.action is not None:
                if operation.action in actions:
                    raise ValueError("duplicate_oss_action")
                actions.add(operation.action)
            operations.append(operation)
        if [item.sdk_method for item in operations] != sorted(methods):
            raise ValueError("unsorted_oss_operation_catalog")
        self.meta = MappingProxyType(dict(meta))
        self.operations = tuple(operations)
        self.policy_digest = policy_digest
        self._by_action = MappingProxyType({item.action: item for item in operations if item.action is not None})
        self._by_method = MappingProxyType({item.sdk_method: item for item in operations})

    @classmethod
    def load(cls, path: Path = _CATALOG_PATH) -> OssOperationCatalog:
        raw = path.read_bytes()
        document = json.loads(raw)
        if not isinstance(document, Mapping):
            raise ValueError("invalid_oss_operation_catalog")
        return cls(document, policy_digest=hashlib.sha256(raw).hexdigest())

    def get(self, action: str) -> OssOperationSpec | None:
        return self._by_action.get(action)

    def require(self, action: str) -> OssOperationSpec:
        operation = self.get(action)
        if operation is None:
            raise KeyError(action)
        return operation

    def by_sdk_method(self, method: str) -> OssOperationSpec:
        return self._by_method[method]


def _operation_spec(raw: Any) -> OssOperationSpec:
    if not isinstance(raw, Mapping):
        raise ValueError("invalid_oss_operation_row")
    mappings = raw.get("field_mapping")
    reasons = raw.get("unsupported_reasons")
    if not isinstance(mappings, list) or not isinstance(reasons, list):
        raise ValueError("invalid_oss_operation_row")
    fields = tuple(OssFieldMapping(**dict(item)) for item in mappings if isinstance(item, Mapping))
    if len(fields) != len(mappings):
        raise ValueError("invalid_oss_field_mapping")
    response_mode = str(raw.get("response_mode", "unsupported"))
    if response_mode not in {"stream", "headers_only", "buffered", "unsupported"}:
        raise ValueError("invalid_oss_operation_row")
    operation = OssOperationSpec(
        action=raw.get("action") if isinstance(raw.get("action"), str) else None,
        body_type=str(raw.get("body_type", "")),
        field_mapping=fields,
        method=raw.get("method") if isinstance(raw.get("method"), str) else None,
        request_model=raw.get("request_model") if isinstance(raw.get("request_model"), str) else None,
        response_mode=cast(Literal["stream", "headers_only", "buffered", "unsupported"], response_mode),
        sdk_method=str(raw.get("sdk_method", "")),
        supported=raw.get("supported") is True,
        unsupported_reasons=tuple(str(reason) for reason in reasons),
    )
    if not operation.sdk_method or operation.response_mode not in {
        "stream",
        "headers_only",
        "buffered",
        "unsupported",
    }:
        raise ValueError("invalid_oss_operation_row")
    if operation.supported != (not operation.unsupported_reasons and operation.response_mode != "unsupported"):
        raise ValueError("inconsistent_oss_operation_support")
    return operation


@dataclass(frozen=True)
class OssResponsePolicy:
    mode: Literal["stream", "headers_only", "buffered", "unsupported"]
    expected_host: str
    max_response_bytes: int
    declared_headers: tuple[str, ...]


_OSS_RESPONSE_POLICY: contextvars.ContextVar[OssResponsePolicy | None] = contextvars.ContextVar(
    "iac_code_oss_response_policy", default=None
)


class OssIdentitySigner:
    """Inject identity negotiation before delegating to the official V4 signer."""

    def __init__(self, signer: SignerV4 | None = None) -> None:
        self._signer = signer or SignerV4()

    def sign(self, signing_ctx: SigningContext) -> None:
        if signing_ctx is None or signing_ctx.request is None:
            raise ValueError("oss_signing_context_required")
        if signing_ctx.request.headers.get("Accept-Encoding") is not None:
            raise ValueError("caller_accept_encoding_forbidden")
        if signing_ctx.request.headers.get("Host") is None:
            signing_ctx.request.headers["Host"] = urlsplit(signing_ctx.request.url).netloc
        signing_ctx.request.headers["Accept-Encoding"] = "identity"
        required_headers = {"accept-encoding", "host"}
        if _mapping_header(signing_ctx.request.headers, "if-match") is not None:
            required_headers.add("if-match")
        signing_ctx.additional_headers = set(signing_ctx.additional_headers or ()) | required_headers
        self._signer.sign(signing_ctx)
        authorization = signing_ctx.request.headers.get("Authorization", "")
        match = _AUTH_ADDITIONAL_HEADERS.search(authorization)
        signed = set(match.group(1).split(";")) if match else set()
        if signing_ctx.request.headers.get("Accept-Encoding") != "identity" or "accept-encoding" not in signed:
            raise RuntimeError("oss_accept_encoding_not_signed")
        if "host" not in signed:
            raise RuntimeError("oss_host_not_signed")
        if "if-match" in required_headers and "if-match" not in signed:
            raise RuntimeError("oss_if_match_not_signed")


class _OssHttpResponse(AsyncHttpResponse):
    def __init__(self, request: HttpRequest, response: Any, policy: OssResponsePolicy) -> None:
        self._request = request
        self._response = response
        self.policy = policy
        self._headers = CaseInsensitiveDict(response.headers)
        self._content: bytes | None = None
        self._closed = False
        self._stream_consumed = False

    @property
    def request(self) -> HttpRequest:
        return self._request

    @property
    def status_code(self) -> int:
        return int(self._response.status)

    @property
    def headers(self) -> CaseInsensitiveDict[str]:
        return self._headers

    @property
    def reason(self) -> str:
        return str(self._response.reason or "")

    @property
    def is_closed(self) -> bool:
        return self._closed

    @property
    def is_stream_consumed(self) -> bool:
        return self._stream_consumed

    @property
    def content(self) -> bytes:
        if self._content is None:
            raise oss_exceptions.ResponseNotReadError()
        return self._content

    async def set_content(self, content: bytes) -> None:
        self._content = content
        self._stream_consumed = True
        await self.close()

    async def consume(self, limit: int, message: str) -> bytes:
        if self._content is not None:
            return self._content
        self._stream_consumed = True
        payload = bytearray()
        try:
            async for chunk in self._response.content.iter_chunked(_STREAM_CHUNK_BYTES):
                if len(payload) + len(chunk) > limit:
                    raise ResponseTooLarge(message)
                payload.extend(chunk)
        except BaseException:
            await self.close()
            raise
        self._content = bytes(payload)
        await self.close()
        return self._content

    async def read(self) -> bytes:
        return await self.consume(self.policy.max_response_bytes, "response_too_large")

    async def close(self) -> None:
        if not self._closed:
            self._closed = True
            self._response.close()
            await asyncio.sleep(0)

    async def iter_bytes(self, **kwargs: Any) -> Any:
        block_size = int(kwargs.get("block_size", _STREAM_CHUNK_BYTES))
        if block_size <= 0:
            raise ValueError("invalid_stream_block_size")
        if self._content is not None:
            for offset in range(0, len(self._content), block_size):
                yield self._content[offset : offset + block_size]
            return
        if self._stream_consumed:
            raise oss_exceptions.StreamConsumedError()
        self._stream_consumed = True
        size = 0
        try:
            async for chunk in self._response.content.iter_chunked(block_size):
                size += len(chunk)
                if size > self.policy.max_response_bytes:
                    raise ResponseTooLarge("response_too_large")
                yield bytes(chunk)
        finally:
            await self.close()

    async def __aenter__(self) -> _OssHttpResponse:
        return self

    async def __aexit__(self, *_args: Any) -> None:
        await self.close()


class OssStreamingHttpClient(AsyncHttpClient):
    """Raw aiohttp transport selected by a per-operation response policy."""

    def __init__(
        self,
        *,
        session_factory: Callable[..., Any] = aiohttp.ClientSession,
        connect_timeout: float = 10.0,
        read_timeout: float = 20.0,
    ) -> None:
        self._session_factory = session_factory
        self._connect_timeout = connect_timeout
        self._read_timeout = read_timeout
        self._session: Any | None = None

    async def open(self) -> None:
        if self._session is None:
            self._session = self._session_factory(
                auto_decompress=False,
                skip_auto_headers={"Accept-Encoding"},
            )

    async def close(self) -> None:
        session = self._session
        if session is not None:
            await session.close()
            if self._session is session:
                self._session = None

    async def send(self, request: HttpRequest, **kwargs: Any) -> AsyncHttpResponse:
        del kwargs
        policy = _OSS_RESPONSE_POLICY.get()
        if policy is None:
            raise RuntimeError("oss_response_policy_required")
        snapshot = replace(policy)
        self._validate_request(request, snapshot)
        await self.open()
        session = self._session
        if session is None:
            raise RuntimeError("oss_http_session_unavailable")
        timeout = aiohttp.ClientTimeout(sock_connect=self._connect_timeout, sock_read=self._read_timeout)
        try:
            response = await session.request(
                request.method,
                request.url,
                headers=request.headers,
                data=request.body,
                timeout=timeout,
                allow_redirects=False,
            )
        except (aiohttp.ClientResponseError, asyncio.TimeoutError) as error:
            raise oss_exceptions.ResponseError(error=error) from error
        except aiohttp.ClientError as error:
            raise oss_exceptions.RequestError(error=error) from error
        wrapped = _OssHttpResponse(request, response, snapshot)
        if not 200 <= wrapped.status_code < 300:
            await _precheck_length(response.headers, _ERROR_RESPONSE_BYTES, wrapped, "error_response_too_large")
            await wrapped.consume(_ERROR_RESPONSE_BYTES, "error_response_too_large")
            return wrapped
        if snapshot.mode == "headers_only":
            await wrapped.set_content(b"")
            return wrapped
        if snapshot.mode == "buffered":
            limit = min(snapshot.max_response_bytes, _BUFFERED_RESPONSE_BYTES)
            await _precheck_length(response.headers, limit, wrapped, "response_too_large")
            await wrapped.consume(limit, "response_too_large")
            return wrapped
        if snapshot.mode == "stream":
            await _precheck_length(response.headers, snapshot.max_response_bytes, wrapped, "response_too_large")
            return wrapped
        await wrapped.close()
        raise RuntimeError("unsupported_oss_response_policy")

    @staticmethod
    def _validate_request(request: HttpRequest, policy: OssResponsePolicy) -> None:
        parsed = urlsplit(request.url)
        if parsed.scheme != "https":
            raise ValueError("oss_https_required")
        if parsed.username is not None or parsed.password is not None or parsed.port is not None:
            raise ValueError("oss_final_host_mismatch")
        if (
            parsed.netloc != policy.expected_host
            or parsed.hostname != policy.expected_host
            or _mapping_header(request.headers, "host") != policy.expected_host
        ):
            raise ValueError("oss_final_host_mismatch")
        if request.headers.get("Accept-Encoding") != "identity":
            raise ValueError("oss_accept_encoding_not_identity")
        authorization = request.headers.get("Authorization", "")
        match = _AUTH_ADDITIONAL_HEADERS.search(authorization)
        signed = set(match.group(1).split(";")) if match else set()
        if "accept-encoding" not in signed:
            raise ValueError("oss_accept_encoding_not_signed")
        if "host" not in signed:
            raise ValueError("oss_host_not_signed")
        if _mapping_header(request.headers, "if-match") is not None and "if-match" not in signed:
            raise ValueError("oss_if_match_not_signed")


class OssV4Adapter:
    """Catalog-restricted OSS async SDK adapter implementing the shared transport contract."""

    def __init__(
        self,
        *,
        catalog: OssOperationCatalog | None = None,
        http_client: OssStreamingHttpClient | None = None,
        signer: OssIdentitySigner | None = None,
        host_binding_resolver: HostBindingResolver | None = None,
        client_factory: Callable[[Config, OssIdentitySigner], Any] | None = None,
        sleep: Callable[[float], Any] = asyncio.sleep,
    ) -> None:
        self.catalog = catalog or OssOperationCatalog.load()
        self.http_client = http_client or OssStreamingHttpClient()
        self.signer = signer or OssIdentitySigner()
        self.host_binding_resolver = host_binding_resolver or HostBindingResolver(("aliyuncs.com",))
        self._client_factory = client_factory or (
            lambda config, identity_signer: AsyncClient(config, signer=identity_signer)
        )
        self._sleep = sleep
        self._closed = False

    async def aclose(self) -> None:
        if not self._closed:
            await self.http_client.close()
            self._closed = True

    async def execute(
        self,
        *,
        contract: CanonicalWireContract,
        request: BuiltApiRequest,
        endpoint: EndpointResolution,
        credential: AliyunCredential | None,
        context: ToolContext,
        budget: RetryBudget,
    ) -> NormalizedApiResponse:
        if self._closed:
            raise RuntimeError("oss_adapter_closed")
        if contract.transport != "oss_v4_sdk" or contract.signature_scheme != "oss_v4":
            raise ValueError("invalid_oss_transport_contract")
        operation = self.catalog.get(contract.action)
        if operation is None:
            raise ValueError("oss_operation_not_cataloged")
        if not operation.supported:
            suffix = ",".join(operation.unsupported_reasons)
            raise ValueError(f"oss_operation_unsupported:{suffix}")
        if (
            operation.action != contract.action
            or operation.method != request.method
            or operation.method != contract.method
        ):
            raise ValueError("oss_operation_contract_mismatch")
        if _mapping_header(request.headers, "accept-encoding") is not None:
            raise ValueError("caller_accept_encoding_forbidden")
        _validate_endpoint_mode(endpoint)

        expected_host = endpoint.expected_host
        if expected_host is None or endpoint.region_id is None:
            raise ValueError("oss_target_binding_required")
        policy = OssResponsePolicy(
            mode=operation.response_mode,
            expected_host=expected_host,
            max_response_bytes=request.response_policy.max_bytes,
            declared_headers=request.response_policy.declared_headers,
        )
        sdk_request = _build_sdk_request(operation, contract, request)
        if credential is None:
            raise ValueError("credential_required")
        signing_credential = await budget.run_attempt(
            lambda: resolve_signing_credential(credential),
            retryable_call=False,
        )
        client = self._client(
            endpoint=endpoint,
            credential=signing_credential,
        )
        if operation.response_mode == "stream":
            return await self._execute_get_object(
                client=client,
                operation=operation,
                sdk_request=sdk_request,
                contract=contract,
                request=request,
                policy=policy,
                budget=budget,
            )
        return await self._execute_regular(
            client=client,
            operation=operation,
            sdk_request=sdk_request,
            contract=contract,
            request=request,
            policy=policy,
            budget=budget,
        )

    def _client(self, *, endpoint: EndpointResolution, credential: AliyunCredential) -> Any:
        if endpoint.region_id is None:
            raise ValueError("oss_request_region_required")
        config = Config(
            region=endpoint.region_id,
            endpoint=endpoint.endpoint,
            signature_version="v4",
            credentials_provider=StaticCredentialsProvider(
                credential.access_key_id,
                credential.access_key_secret,
                credential.sts_token or None,
            ),
            retry_max_attempts=1,
            http_client=cast(Any, self.http_client),
            use_cname=False,
            use_accelerate_endpoint=False,
            use_path_style=False,
            disable_ssl=False,
            enabled_redirect=False,
            additional_headers=["accept-encoding", "host"],
        )
        _validate_locked_config(config)
        return self._client_factory(config, self.signer)

    async def _invoke(
        self, client: Any, operation: OssOperationSpec, sdk_request: Any, policy: OssResponsePolicy
    ) -> Any:
        method = getattr(client, operation.sdk_method, None)
        if not callable(method):
            raise RuntimeError("oss_sdk_operation_missing")
        token = _OSS_RESPONSE_POLICY.set(policy)
        try:
            return await method(sdk_request)
        finally:
            _OSS_RESPONSE_POLICY.reset(token)

    async def _execute_regular(
        self,
        *,
        client: Any,
        operation: OssOperationSpec,
        sdk_request: Any,
        contract: CanonicalWireContract,
        request: BuiltApiRequest,
        policy: OssResponsePolicy,
        budget: RetryBudget,
    ) -> NormalizedApiResponse:
        eligible = retry_eligible(
            operation_type=contract.operation_type,
            method=request.method,
            has_body=request.body is not None,
        )
        previous_reason: RetryReason | None = None
        while True:
            attempt = await budget.acquire(reason=previous_reason)
            previous_reason = None
            try:
                result = await budget.run_attempt(
                    lambda: self._invoke(client, operation, sdk_request, policy),
                    retryable_call=eligible,
                )
            except BaseException as error:
                service_error = _service_error(error)
                if service_error is not None:
                    reason = map_retryable_status(service_error.status_code)
                    if reason is not None and eligible and attempt < budget.max_attempts:
                        try:
                            delay = retry_delay(
                                budget,
                                failed_attempt=attempt,
                                retry_after=_mapping_header(service_error.headers or {}, "retry-after"),
                                reason=reason,
                            )
                        except RetryExhausted:
                            pass
                        else:
                            previous_reason = reason
                            await self._sleep(delay)
                            continue
                    return _normalize_service_error(service_error, policy.declared_headers)
                if _is_oss_deserialization_error(error):
                    raise RuntimeError("invalid_response") from error
                reason = _oss_retry_reason(error)
                if reason is not None and eligible and attempt < budget.max_attempts:
                    delay = retry_delay(budget, failed_attempt=attempt, reason=reason)
                    previous_reason = reason
                    await self._sleep(delay)
                    continue
                if _is_oss_transport_failure(error):
                    raise TransportFailure(
                        outcome=_classify_oss_transport_failure(error, retryable_call=eligible),
                        reason=reason,
                    ) from error
                raise
            return _normalize_sdk_result(result, operation.response_mode, policy.declared_headers)

    async def _execute_get_object(
        self,
        *,
        client: Any,
        operation: OssOperationSpec,
        sdk_request: Any,
        contract: CanonicalWireContract,
        request: BuiltApiRequest,
        policy: OssResponsePolicy,
        budget: RetryBudget,
    ) -> NormalizedApiResponse:
        eligible = retry_eligible(
            operation_type=contract.operation_type,
            method=request.method,
            has_body=request.body is not None,
        )
        expected_etag: str | None = None
        expected_length: int | None = None
        expected_content_type: str | None = None
        expected_content_encoding: str | None = None
        previous_reason: RetryReason | None = None

        while True:
            attempt = await budget.acquire(reason=previous_reason)
            previous_reason = None
            current_request = _copy_sdk_request(sdk_request)
            if expected_etag is not None:
                if not hasattr(current_request, "if_match"):
                    raise RuntimeError("oss_if_match_injection_missing")
                current_request.if_match = expected_etag
            try:
                result = await budget.run_attempt(
                    lambda: self._invoke(client, operation, current_request, policy),
                    retryable_call=eligible,
                    abandon_result=lambda abandoned: _close_body(getattr(abandoned, "body", None)),
                )
            except BaseException as error:
                service_error = _service_error(error)
                if service_error is not None:
                    reason = map_retryable_status(service_error.status_code)
                    if reason is not None and eligible and attempt < budget.max_attempts:
                        try:
                            delay = retry_delay(
                                budget,
                                failed_attempt=attempt,
                                retry_after=_mapping_header(service_error.headers or {}, "retry-after"),
                                reason=reason,
                            )
                        except RetryExhausted:
                            pass
                        else:
                            previous_reason = reason
                            await self._sleep(delay)
                            continue
                    return _normalize_service_error(service_error, policy.declared_headers)
                if _is_oss_deserialization_error(error):
                    raise RuntimeError("invalid_response") from error
                reason = _oss_retry_reason(error)
                if reason is not None and eligible and attempt < budget.max_attempts:
                    delay = retry_delay(budget, failed_attempt=attempt, reason=reason)
                    previous_reason = reason
                    await self._sleep(delay)
                    continue
                if reason is not None:
                    raise TransportFailure(
                        outcome=_classify_oss_transport_failure(error, retryable_call=eligible),
                        reason=reason,
                    ) from error
                if _is_oss_transport_failure(error):
                    raise TransportFailure(
                        outcome=_classify_oss_transport_failure(error, retryable_call=eligible),
                        reason=None,
                    ) from error
                raise

            raw_headers = _result_headers(result)
            body = getattr(result, "body", None)
            etag = getattr(result, "etag", None) or _mapping_header(raw_headers, "etag")
            length = _trusted_content_length(raw_headers)
            content_type = _mapping_header(raw_headers, "content-type")
            content_encoding = _mapping_header(raw_headers, "content-encoding")
            if length is not None and length > policy.max_response_bytes:
                await _close_body(body)
                raise ResponseTooLarge("response_too_large")
            if expected_etag is not None and etag != expected_etag:
                await _close_body(body)
                raise ValueError("oss_object_changed_during_retry")
            if expected_length is not None and length is not None and length != expected_length:
                await _close_body(body)
                raise ValueError("oss_object_changed_during_retry")
            if expected_etag is not None and (
                content_type != expected_content_type or content_encoding != expected_content_encoding
            ):
                await _close_body(body)
                raise ValueError("oss_object_metadata_changed_during_retry")
            if expected_etag is None:
                expected_etag = etag if isinstance(etag, str) and etag else None
                expected_length = length
                expected_content_type = content_type
                expected_content_encoding = content_encoding
            trusted_length = length if length is not None else expected_length

            try:
                payload = await budget.run_attempt(
                    lambda: _consume_object(
                        body,
                        policy.max_response_bytes,
                        expected_bytes=trusted_length,
                    ),
                    retryable_call=eligible,
                )
            except BaseException as error:
                reason = _oss_retry_reason(error) or map_aiohttp_retry_reason(error)
                can_retry = (
                    reason is not None and expected_etag is not None and eligible and attempt < budget.max_attempts
                )
                if can_retry:
                    delay = retry_delay(budget, failed_attempt=attempt, reason=reason)
                    previous_reason = reason
                    await self._sleep(delay)
                    continue
                if reason is not None:
                    raise TransportFailure(outcome=reason.value, reason=reason) from error
                raise

            headers = filter_response_headers(raw_headers, declared_headers=policy.declared_headers)
            return NormalizedApiResponse(
                status=int(getattr(result, "status_code", 200)),
                headers=headers,
                body={"encoding": "base64", "data": base64.b64encode(payload).decode("ascii")},
                content_type=content_type,
                content_encoding=content_encoding,
                size=len(payload),
            )


def _validate_locked_config(config: Config) -> None:
    if (
        config.signature_version != "v4"
        or config.retry_max_attempts != 1
        or config.use_cname is not False
        or config.use_accelerate_endpoint is not False
        or config.use_path_style is not False
        or config.disable_ssl is not False
        or config.enabled_redirect is not False
        or config.additional_headers != ["accept-encoding", "host"]
    ):
        raise ValueError("unsafe_oss_sdk_config")


def _validate_endpoint_mode(endpoint: EndpointResolution) -> None:
    labels = endpoint.endpoint.casefold().split(".")
    if any(label.startswith("oss-accelerate") for label in labels):
        raise ValueError("oss_accelerate_forbidden")


def _build_sdk_request(
    operation: OssOperationSpec,
    contract: CanonicalWireContract,
    request: BuiltApiRequest,
) -> Any:
    if operation.request_model is None:
        raise RuntimeError("oss_request_model_missing")
    module_name, separator, class_name = operation.request_model.rpartition(".")
    if not separator:
        raise RuntimeError("invalid_oss_request_model")
    model_class = getattr(importlib.import_module(module_name), class_name, None)
    if not inspect.isclass(model_class):
        raise RuntimeError("oss_request_model_missing")
    query = _casefold_pairs(request.canonical_query)
    headers = {str(name).casefold(): str(value) for name, value in request.headers.items()}
    path_values = _path_values(contract.pathname, request.raw_path)
    values: dict[str, Any] = {}
    for field in operation.field_mapping:
        value: Any | None = None
        if field.location == "host":
            value = request.host_values.get(field.openmeta_name)
        elif field.location == "path":
            value = path_values.get(field.openmeta_name)
        elif field.location == "query":
            value = query.get(field.wire_name.casefold())
        elif field.location == "header":
            if "*" in field.openmeta_name:
                prefix, suffix = field.openmeta_name.casefold().split("*", 1)
                dynamic = {
                    name[len(prefix) : len(name) - len(suffix) if suffix else None]: item
                    for name, item in headers.items()
                    if name.startswith(prefix) and name.endswith(suffix)
                }
                value = dynamic or None
            else:
                value = headers.get(field.wire_name.casefold())
        elif field.location == "body":
            value = request.body
        if value is None:
            if field.required:
                raise ValueError(f"missing_oss_sdk_field:{field.sdk_field}")
            continue
        values[field.sdk_field] = _sdk_scalar(value, field.sdk_type)

    attributes = getattr(model_class, "_attribute_map", {})
    for header_name, sdk_field in (("content-type", "content_type"), ("content-length", "content_length")):
        if sdk_field in attributes and sdk_field not in values and header_name in headers:
            sdk_type = str(attributes[sdk_field].get("type", "str"))
            values[sdk_field] = _sdk_scalar(headers[header_name], sdk_type)
    return model_class(**values)


def _copy_sdk_request(request: Any) -> Any:
    model_class = type(request)
    attributes = getattr(model_class, "_attribute_map", {})
    values = {name: getattr(request, name) for name in attributes if hasattr(request, name)}
    return model_class(**values)


def _path_values(pathname: str, raw_path: bytes) -> Mapping[str, str]:
    template = pathname.partition("?")[0]
    actual = raw_path.decode("ascii").partition("?")[0]
    names = _PATH_FIELD.findall(template)
    if not names:
        return MappingProxyType({})
    pattern = re.escape(template)
    for name in names:
        pattern = pattern.replace(re.escape("{" + name + "}"), f"(?P<{name}>.*)", 1)
    match = re.fullmatch(pattern, actual)
    if match is None:
        raise ValueError("oss_path_mapping_failed")
    return MappingProxyType({name: unquote(value) for name, value in match.groupdict().items()})


def _casefold_pairs(pairs: tuple[tuple[str, str], ...]) -> Mapping[str, str]:
    result: dict[str, str] = {}
    for name, value in pairs:
        lowered = name.casefold()
        if lowered in result:
            raise ValueError("duplicate_oss_query_field")
        result[lowered] = value
    return MappingProxyType(result)


def _sdk_scalar(value: Any, sdk_type: str) -> Any:
    lowered = sdk_type.casefold()
    if "int" in lowered and isinstance(value, str):
        return int(value)
    if "bool" in lowered and isinstance(value, str):
        if value.casefold() not in {"true", "false"}:
            raise ValueError("invalid_oss_boolean")
        return value.casefold() == "true"
    return value


async def _consume_object(
    body: Any,
    limit: int,
    *,
    expected_bytes: int | None,
) -> bytes:
    if body is None or not callable(getattr(body, "iter_bytes", None)):
        raise RuntimeError("oss_stream_body_required")
    payload = bytearray()
    try:
        iterator = body.iter_bytes(block_size=_STREAM_CHUNK_BYTES)
        if inspect.isawaitable(iterator):
            iterator = await iterator
        async for chunk in iterator:
            raw = bytes(chunk)
            if len(payload) + len(raw) > limit:
                raise ResponseTooLarge("response_too_large")
            payload.extend(raw)
        if expected_bytes is not None and len(payload) != expected_bytes:
            raise aiohttp.ClientPayloadError("oss_object_content_length_mismatch")
        return bytes(payload)
    finally:
        await _close_body(body)


async def _close_body(body: Any) -> None:
    close = getattr(body, "close", None)
    if callable(close):
        result = close()
        if inspect.isawaitable(result):
            task = asyncio.ensure_future(result)
            cancellation: asyncio.CancelledError | None = None
            while not task.done():
                try:
                    await asyncio.shield(task)
                except asyncio.CancelledError as error:
                    if cancellation is None:
                        cancellation = error
                except BaseException:
                    if cancellation is None:
                        raise
                    break
            if cancellation is not None:
                try:
                    task.result()
                except BaseException:
                    pass
                raise cancellation
            task.result()


def _normalize_sdk_result(
    result: Any,
    mode: str,
    declared_headers: tuple[str, ...],
) -> NormalizedApiResponse:
    raw_headers = _result_headers(result)
    content_type = _mapping_header(raw_headers, "content-type")
    content_encoding = _mapping_header(raw_headers, "content-encoding")
    body = None if mode == "headers_only" else _sdk_result_body(result)
    size = _trusted_content_length(raw_headers)
    if size is None:
        size = len(json.dumps(body, sort_keys=True, default=str).encode("utf-8")) if body is not None else 0
    return NormalizedApiResponse(
        status=int(getattr(result, "status_code", 200)),
        headers=filter_response_headers(raw_headers, declared_headers=declared_headers),
        body=body,
        content_type=content_type,
        content_encoding=content_encoding,
        size=size,
    )


def _sdk_result_body(result: Any) -> Mapping[str, Any]:
    excluded = {"status", "status_code", "request_id", "headers", "body"}
    values = {
        name: _sdk_value(value)
        for name, value in vars(result).items()
        if name not in excluded and not name.startswith("_") and value is not None
    }
    return values


def _sdk_value(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {str(name): _sdk_value(item) for name, item in value.items()}
    if isinstance(value, list | tuple):
        return [_sdk_value(item) for item in value]
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, Enum):
        return value.value
    if hasattr(value, "__dict__"):
        return {
            name: _sdk_value(item)
            for name, item in vars(value).items()
            if not name.startswith("_") and item is not None
        }
    return value


def _normalize_service_error(
    error: oss_exceptions.ServiceError,
    declared_headers: tuple[str, ...],
) -> NormalizedApiResponse:
    raw_headers = error.headers if isinstance(error.headers, Mapping) else {}
    fields = error.error_fileds if isinstance(error.error_fileds, Mapping) else {}
    body: dict[str, Any] = dict(fields)
    if not body:
        body = {"Code": error.code or "", "Message": error.message or ""}
    snapshot = error.snapshot if isinstance(error.snapshot, bytes) else b""
    return NormalizedApiResponse(
        status=int(error.status_code),
        headers=filter_response_headers(raw_headers, declared_headers=declared_headers),
        body=body,
        content_type=_mapping_header(raw_headers, "content-type"),
        content_encoding=_mapping_header(raw_headers, "content-encoding"),
        size=len(snapshot),
    )


def _service_error(error: BaseException) -> oss_exceptions.ServiceError | None:
    current: BaseException = error
    seen: set[int] = set()
    while id(current) not in seen:
        seen.add(id(current))
        if isinstance(current, oss_exceptions.ServiceError):
            return current
        unwrap = getattr(current, "unwrap", None)
        if not callable(unwrap):
            return None
        nested = unwrap()
        if not isinstance(nested, BaseException):
            return None
        current = nested
    return None


def _oss_retry_reason(error: BaseException) -> RetryReason | None:
    current: BaseException = error
    seen: set[int] = set()
    while id(current) not in seen:
        seen.add(id(current))
        reason = map_aiohttp_retry_reason(current)
        if reason is not None:
            return reason
        unwrap = getattr(current, "unwrap", None)
        if not callable(unwrap):
            return None
        nested = unwrap()
        if not isinstance(nested, BaseException):
            return None
        current = nested
    return None


def _oss_error_chain(error: BaseException) -> tuple[BaseException, ...]:
    chain: list[BaseException] = []
    current: BaseException = error
    seen: set[int] = set()
    while id(current) not in seen:
        seen.add(id(current))
        chain.append(current)
        unwrap = getattr(current, "unwrap", None)
        nested = unwrap() if callable(unwrap) else None
        if not isinstance(nested, BaseException):
            break
        current = nested
    return tuple(chain)


def _is_oss_transport_failure(error: BaseException) -> bool:
    return any(
        isinstance(item, (oss_exceptions.RequestError, oss_exceptions.ResponseError, aiohttp.ClientError))
        or type(item) is asyncio.TimeoutError
        for item in _oss_error_chain(error)
    )


def _is_oss_deserialization_error(error: BaseException) -> bool:
    return any(isinstance(item, oss_exceptions.DeserializationError) for item in _oss_error_chain(error))


def _classify_oss_transport_failure(error: BaseException, *, retryable_call: bool) -> str:
    chain = _oss_error_chain(error)
    reason = next((map_aiohttp_retry_reason(item) for item in chain if map_aiohttp_retry_reason(item)), None)
    if retryable_call and reason is not None:
        return reason.value
    if any(type(item) is aiohttp.ClientConnectorError for item in chain):
        return "pre_connect_failure"
    return "unknown_after_transport_error"


def _result_headers(result: Any) -> Mapping[str, str]:
    headers = getattr(result, "headers", {})
    return headers if isinstance(headers, Mapping) else {}


def _mapping_header(headers: Mapping[str, Any], name: str) -> str | None:
    lowered = name.casefold()
    for candidate, value in headers.items():
        if str(candidate).casefold() == lowered:
            return str(value)
    return None


def _trusted_content_length(headers: Mapping[str, Any]) -> int | None:
    getall = getattr(headers, "getall", None)
    if callable(getall):
        values = [str(value) for value in getall("Content-Length", [])]
    else:
        value = _mapping_header(headers, "content-length")
        values = [] if value is None else [value]
    if not values or any(not value.isdecimal() for value in values):
        return None
    lengths = {int(value) for value in values}
    return next(iter(lengths)) if len(lengths) == 1 else None


async def _precheck_length(headers: Any, limit: int, response: _OssHttpResponse, message: str) -> None:
    values: list[str]
    getall = getattr(headers, "getall", None)
    if callable(getall):
        values = [str(value) for value in getall("Content-Length", [])]
    else:
        value = headers.get("Content-Length") if isinstance(headers, Mapping) else None
        values = [] if value is None else [str(value)]
    if not values or any(not value.isdecimal() for value in values):
        return
    lengths = {int(value) for value in values}
    if len(lengths) == 1 and next(iter(lengths)) > limit:
        await response.close()
        raise ResponseTooLarge(message)
