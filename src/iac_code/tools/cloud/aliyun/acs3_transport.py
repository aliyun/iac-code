"""Tea and ACS3 streaming transports with shared response normalization."""

from __future__ import annotations

import asyncio
import base64
import codecs
import json
import sys
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from types import MappingProxyType
from typing import Any, Protocol
from urllib.parse import parse_qsl

import aiohttp
import httpx
from alibabacloud_credentials.client import Client as CredentialClient
from alibabacloud_tea_openapi import exceptions as open_api_exceptions
from alibabacloud_tea_openapi import models as open_api_models
from alibabacloud_tea_openapi.client import Client as OpenApiClient
from alibabacloud_tea_openapi.utils import Utils, get_canonical_query_string
from aliyunsdkcore.acs_exception.exceptions import ClientException as Acs1ClientException
from aliyunsdkcore.acs_exception.exceptions import ServerException as Acs1ServerException
from aliyunsdkcore.auth.credentials import StsTokenCredential
from aliyunsdkcore.client import AcsClient
from aliyunsdkcore.request import CommonRequest
from darabonba.core import DaraCore
from darabonba.exceptions import RetryError, UnretryableException
from darabonba.policy.retry import RetryOptions, RetryPolicyContext
from darabonba.request import DaraRequest
from darabonba.runtime import RuntimeOptions
from defusedxml import ElementTree
from defusedxml.common import DefusedXmlException
from yarl import URL

from iac_code.services.providers.aliyun import AliyunCredential
from iac_code.services.providers.aliyun_credentials_runtime import aliyun_credential_runtime
from iac_code.tools.base import ToolContext
from iac_code.tools.cloud.aliyun.api_contract import (
    ApiContractError,
    BuiltApiRequest,
    CanonicalWireContract,
    ParsedContentType,
    parse_content_type,
)
from iac_code.tools.cloud.aliyun.ecs_credential_errors import ecs_credential_error_code
from iac_code.tools.cloud.aliyun.endpoint_resolver import EndpointResolution
from iac_code.tools.cloud.aliyun.retry_policy import (
    RetryBudget,
    RetryExhausted,
    RetryReason,
    TransportFailure,
    classify_transport_failure,
    is_transport_failure,
    map_httpx_retry_reason,
    map_retryable_status,
    map_tea_retry_reason,
    retry_delay,
    retry_eligible,
)
from iac_code.utils.async_lifecycle import await_task_to_completion

_SIGNATURE_ALGORITHM = "ACS3-HMAC-SHA256"
_STREAM_CHUNK_BYTES = 16 * 1024
_ERROR_RESPONSE_BYTES = 1024**2
_MAX_RESPONSE_BYTES = 16 * 1024**2
_TEA_RESPONSE_SIZE_KEY = "_iac_response_body_size"
_TEA_RESPONSE_HEADERS_ATTRIBUTE = "_iac_response_headers"
_TEA_RESPONSE_SIZE_ATTRIBUTE = "_iac_response_body_size"
_BASE_RESPONSE_HEADERS = frozenset(
    {
        "requestid",
        "request-id",
        "x-request-id",
        "x-acs-request-id",
        "x-aliyun-request-id",
        "x-log-request-id",
        "x-log-requestid",
        "x-oss-request-id",
        "x-mns-request-id",
        "errorcode",
        "error-code",
        "x-error-code",
        "x-acs-error-code",
        "x-aliyun-error-code",
        "x-log-error-code",
        "x-oss-error-code",
        "x-mns-error-code",
        "content-type",
        "content-length",
        "etag",
        "last-modified",
    }
)


class ResponseTooLarge(ValueError):  # noqa: N818 - stable public transport error
    """A response exceeded its reviewed body limit."""


@dataclass(frozen=True)
class TransportRequest:
    request: httpx.Request
    payload_hash: str
    authorization: str
    signed_headers: tuple[str, ...]


@dataclass(frozen=True)
class NormalizedApiResponse:
    status: int
    headers: Mapping[str, str]
    body: Any | None
    content_type: str | None
    content_encoding: str | None
    size: int


class AliyunTransport(Protocol):
    async def execute(
        self,
        *,
        contract: CanonicalWireContract,
        request: BuiltApiRequest,
        endpoint: EndpointResolution,
        credential: AliyunCredential | None,
        context: ToolContext,
        budget: RetryBudget,
    ) -> NormalizedApiResponse: ...


@dataclass(frozen=True, repr=False)
class PreparedTransportCall:
    transport: AliyunTransport
    contract: CanonicalWireContract
    request: BuiltApiRequest
    endpoint: EndpointResolution
    credential: AliyunCredential | None
    context: ToolContext

    async def execute(self, *, budget: RetryBudget) -> NormalizedApiResponse:
        return await self.transport.execute(
            contract=self.contract,
            request=self.request,
            endpoint=self.endpoint,
            credential=self.credential,
            context=self.context,
            budget=budget,
        )


def dynamic_credential_client(credential: AliyunCredential | None) -> Any | None:
    """Return the shared Credentials-SDK client for dynamic modes, else `None`.

    `RamRoleArn` keeps its existing per-call AssumeRole client; `EcsRamRole` gets the
    process-wide IMDS provider adapter so no client construction creates a provider.
    """
    return aliyun_credential_runtime().sdk_client(credential)


async def resolve_signing_credential(
    credential: AliyunCredential,
    *,
    client_factory: Callable[[Any], Any] = CredentialClient,
) -> AliyunCredential:
    """Resolve a dynamic credential into a signable AK/STS triple.

    `RamRoleArn` keeps its existing `get_credential_async()` path; `EcsRamRole` goes
    through the runtime adapter, which offloads the blocking IMDS call with
    `asyncio.to_thread()` instead of touching the raw provider's async interface.
    """
    return await aliyun_credential_runtime().resolve(credential, client_factory=client_factory)


def filter_response_headers(
    headers: Mapping[str, str] | httpx.Headers, *, declared_headers: tuple[str, ...] = ()
) -> Mapping[str, str]:
    allowed = _BASE_RESPONSE_HEADERS | {name.casefold() for name in declared_headers}
    filtered: dict[str, str] = {}
    for name, value in headers.items():
        lowered = name.casefold()
        if lowered in allowed and not _sensitive_header(lowered):
            filtered[lowered] = value
    return MappingProxyType(filtered)


def _sensitive_header(name: str) -> bool:
    normalized = name.casefold().replace("_", "-")
    compact = "".join(character for character in normalized if character.isalnum())
    sensitive_stems = (
        "cookie",
        "authenticat",
        "authorization",
        "securitytoken",
        "accesskey",
        "signature",
        "credential",
    )
    tokens = set(normalized.split("-"))
    return any(stem in compact for stem in sensitive_stems) or bool(tokens & {"ak", "auth"})


class TransportRouter:
    def __init__(self, transports: Mapping[str, AliyunTransport] | None = None) -> None:
        self._transports = dict(
            transports
            or {
                "tea": TeaTransportAdapter(),
                "acs1": Acs1Transport(),
                "acs3_streaming": Acs3StreamingTransport(),
            }
        )

    def prepare(
        self,
        *,
        contract: CanonicalWireContract,
        request: BuiltApiRequest,
        endpoint: EndpointResolution,
        credential: AliyunCredential | None,
        context: ToolContext,
    ) -> PreparedTransportCall:
        transport = self._transports.get(contract.transport)
        if transport is None:
            raise ValueError("unsupported_transport")
        return PreparedTransportCall(
            transport=transport,
            contract=contract,
            request=request,
            endpoint=endpoint,
            credential=credential,
            context=context,
        )

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
        prepared = self.prepare(
            contract=contract,
            request=request,
            endpoint=endpoint,
            credential=credential,
            context=context,
        )
        return await prepared.execute(budget=budget)

    async def aclose(self) -> None:
        for transport in self._transports.values():
            close = getattr(transport, "aclose", None)
            if close is not None:
                await close()


class Acs1Transport:
    """Compatibility transport for exact product versions that still require ACS1 signing."""

    def __init__(
        self,
        *,
        client_factory: Callable[[AliyunCredential, str], Any] | None = None,
    ) -> None:
        self._client_factory = client_factory or _acs1_client

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
        del context
        if contract.signature_scheme != "acs1":
            raise ValueError("unsupported_signature_scheme")
        if credential is None:
            raise ValueError("aliyun_credentials_required")
        signing_credential = await resolve_signing_credential(credential)
        region_id = endpoint.region_id or signing_credential.region_id
        client = self._client_factory(signing_credential, region_id)
        common_request = _acs1_request(contract, request, endpoint)
        await budget.acquire()
        try:
            payload = await budget.run_attempt(
                lambda: _run_acs1_call(client, common_request),
                retryable_call=False,
            )
        except Acs1ServerException as error:
            return _normalize_acs1_server_error(error, request)
        except Acs1ClientException as error:
            raise TransportFailure(outcome="target_transport_failure", reason=None) from error
        if not isinstance(payload, (bytes, bytearray)):
            payload = str(payload).encode("utf-8")
        return _normalize_acs1_success(bytes(payload), request)


async def _run_acs1_call(client: Any, request: CommonRequest) -> Any:
    worker = asyncio.create_task(asyncio.to_thread(client.do_action_with_exception, request))
    try:
        return await asyncio.shield(worker)
    except asyncio.CancelledError as cancellation:
        try:
            await await_task_to_completion(worker)
        except BaseException as cleanup_error:
            raise cancellation from cleanup_error
        raise


def _acs1_client(credential: AliyunCredential, region_id: str) -> AcsClient:
    kwargs: dict[str, Any] = {
        "region_id": region_id,
        "auto_retry": False,
        "connect_timeout": 10,
        "timeout": 60,
    }
    if credential.sts_token:
        kwargs["credential"] = StsTokenCredential(
            credential.access_key_id,
            credential.access_key_secret,
            credential.sts_token,
        )
    else:
        kwargs["ak"] = credential.access_key_id
        kwargs["secret"] = credential.access_key_secret
    return AcsClient(**kwargs)


def _acs1_request(
    contract: CanonicalWireContract,
    request: BuiltApiRequest,
    endpoint: EndpointResolution,
) -> CommonRequest:
    uri_pattern = request.raw_path.decode("ascii") if contract.style == "ROA" else None
    value = CommonRequest(
        domain=endpoint.wire_endpoint,
        version=contract.version,
        action_name=contract.action,
        uri_pattern=uri_pattern,
        product=contract.product,
    )
    value.set_protocol_type("https")
    value.set_method(request.method)
    value.set_accept_format("JSON")
    for name, item in request.canonical_query:
        value.add_query_param(name, item)
    for name, item in request.headers.items():
        if name.casefold() not in {"authorization", "content-length", "host"}:
            value.add_header(name, item)
    if request.body:
        content_type = _mapping_header(request.headers, "content-type") or ""
        if content_type.casefold().startswith("application/x-www-form-urlencoded"):
            for name, item in parse_qsl(request.body.decode("utf-8"), keep_blank_values=True):
                value.add_body_params(name, item)
        else:
            value.set_content(request.body)
    return value


def _normalize_acs1_success(payload: bytes, request: BuiltApiRequest) -> NormalizedApiResponse:
    limit = _response_limit(request, 200)
    if len(payload) > limit:
        raise ResponseTooLarge("response_too_large")
    stripped = payload.lstrip()
    content_type = "application/xml" if stripped.startswith(b"<") else "application/json"
    if request.response_policy.mode == "headers_only" or not payload:
        body: Any | None = None
    elif request.response_policy.mode == "binary":
        body = _base64_body(payload)
        content_type = "application/octet-stream"
    elif request.response_policy.mode == "string":
        body = _decode_text(payload, content_type)
    else:
        body = _normalize_json_success(payload, content_type)
    return NormalizedApiResponse(
        status=200,
        headers=MappingProxyType({"content-type": content_type}),
        body=body,
        content_type=content_type,
        content_encoding=None,
        size=len(payload),
    )


def _normalize_acs1_server_error(
    error: Acs1ServerException,
    request: BuiltApiRequest,
) -> NormalizedApiResponse:
    status = int(error.get_http_status() or 500)
    body = {"Code": str(error.get_error_code() or "ServerError"), "Message": str(error.get_error_msg() or "")}
    encoded = json.dumps(body, ensure_ascii=True, separators=(",", ":")).encode("ascii")
    if len(encoded) > _response_limit(request, status):
        raise ResponseTooLarge("error_response_too_large")
    content_type = "application/json"
    return NormalizedApiResponse(
        status=status,
        headers=MappingProxyType({"content-type": content_type}),
        body=body,
        content_type=content_type,
        content_encoding=None,
        size=len(encoded),
    )


class TeaTransportAdapter:
    def __init__(
        self,
        *,
        client_factory: Callable[[EndpointResolution, AliyunCredential | None], Any] | None = None,
        session_factory: Callable[..., Any] = aiohttp.ClientSession,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        self._client_factory = client_factory or (
            lambda endpoint, credential: _tea_client(
                endpoint,
                credential,
                session_factory=session_factory,
            )
        )
        self._sleep = sleep

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
        del context
        sls_gateway = _uses_sls_gateway(contract)
        mns_gateway = _uses_mns_gateway(contract)
        oss_gateway = _uses_oss_gateway(contract)
        pop_v4_gateway = _uses_pop_v4_gateway(contract)
        spi_gateway = sls_gateway or mns_gateway or oss_gateway or pop_v4_gateway
        client_endpoint = replace(endpoint, expected_host=None) if sls_gateway else endpoint
        client = self._client_factory(client_endpoint, credential)
        params = open_api_models.Params(
            action=contract.action,
            version=contract.version,
            protocol=contract.protocol,
            pathname=request.raw_path.decode("ascii"),
            method=request.method,
            auth_type=contract.auth_type,
            style=contract.style,
            body_type=_tea_response_type(contract, method=request.method),
            req_body_type=_tea_request_type(contract),
        )
        api_request = open_api_models.OpenApiRequest(
            headers=dict(request.headers),
            query=dict(request.canonical_query),
            body=_tea_body(contract, request.body),
            host_map=dict(request.host_values),
        )
        if spi_gateway:
            if sls_gateway:
                _attach_sls_gateway(client)
            elif mns_gateway:
                _attach_mns_gateway(client)
            elif oss_gateway:
                _attach_oss_gateway(client)
            else:
                _attach_pop_v4_gateway(client, product_id=contract.product)
            sdk_call = getattr(client, "execute_async", None)
            if not callable(sdk_call):
                raise RuntimeError("tea_spi_gateway_client_required")
        else:
            sdk_call = getattr(client, "call_api_streaming_async", None)
            if not callable(sdk_call):
                raise RuntimeError("tea_streaming_client_required")
        runtime = RuntimeOptions(autoretry=False, max_attempts=1)
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
                    lambda: (
                        sdk_call(params, api_request, runtime)
                        if spi_gateway
                        else sdk_call(
                            params,
                            api_request,
                            runtime,
                            success_limit=_response_limit(request, 200),
                        )
                    ),
                    retryable_call=eligible,
                )
            except (
                open_api_exceptions.ClientException,
                open_api_exceptions.ServerException,
                open_api_exceptions.ThrottlingException,
            ) as error:
                status = error.status_code
                if status is None:
                    raise
                reason = map_retryable_status(status)
                if reason is not None and eligible and attempt < budget.max_attempts:
                    try:
                        delay = retry_delay(
                            budget,
                            failed_attempt=attempt,
                            retry_after=_tea_exception_retry_after(error),
                            reason=reason,
                        )
                    except RetryExhausted:
                        pass
                    else:
                        previous_reason = reason
                        await self._sleep(delay)
                        continue
                return _normalize_tea_error(
                    error,
                    declared_headers=request.response_policy.declared_headers,
                )
            except BaseException as error:
                if (credential_code := ecs_credential_error_code(error)) is not None:
                    # The SPI gateway signs inside the SDK, so the credential runtime's
                    # failure arrives wrapped in the SDK envelope. Nothing was sent, so it
                    # must not become a transport outcome: keep the stable code the same
                    # way the streaming path already surfaces it.
                    raise ValueError(credential_code) from error
                if (
                    spi_gateway
                    and (
                        gateway_error := _normalize_spi_gateway_error(
                            error,
                            declared_headers=request.response_policy.declared_headers,
                        )
                    )
                    is not None
                ):
                    reason = map_retryable_status(gateway_error.status)
                    if reason is not None and eligible and attempt < budget.max_attempts:
                        try:
                            delay = retry_delay(budget, failed_attempt=attempt, reason=reason)
                        except RetryExhausted:
                            pass
                        else:
                            previous_reason = reason
                            await self._sleep(delay)
                            continue
                    return gateway_error
                reason = map_tea_retry_reason(error)
                if reason is not None and eligible and attempt < budget.max_attempts:
                    delay = retry_delay(budget, failed_attempt=attempt, reason=reason)
                    previous_reason = reason
                    await self._sleep(delay)
                    continue
                if is_transport_failure(error):
                    raise TransportFailure(
                        outcome=classify_transport_failure(error, retryable_call=eligible),
                        reason=reason,
                    ) from error
                raise
            status = _tea_result_status(result)
            raw_headers = result.get("headers", {})
            reason = map_retryable_status(status)
            if reason is not None and eligible and attempt < budget.max_attempts:
                try:
                    delay = retry_delay(
                        budget,
                        failed_attempt=attempt,
                        retry_after=_mapping_header(raw_headers, "retry-after"),
                        reason=reason,
                    )
                except RetryExhausted:
                    pass
                else:
                    previous_reason = reason
                    await self._sleep(delay)
                    continue
            headers = filter_response_headers(raw_headers, declared_headers=request.response_policy.declared_headers)
            content_type = _mapping_header(raw_headers, "content-type")
            content_encoding = _mapping_header(raw_headers, "content-encoding")
            body = (
                None
                if request.method == "HEAD" or status == 204 or request.response_policy.mode == "headers_only"
                else result.get("body")
            )
            if spi_gateway and _TEA_RESPONSE_SIZE_KEY not in result:
                result = dict(result)
                result[_TEA_RESPONSE_SIZE_KEY] = _estimated_tea_body_size(body)
            size = _tea_result_size(result, body)
            limit = _response_limit(request, status)
            expected = _trusted_content_length(raw_headers)
            if expected is not None and expected > limit:
                message = "error_response_too_large" if not _is_success_status(status) else "response_too_large"
                raise ResponseTooLarge(message)
            if size > limit:
                message = "error_response_too_large" if not _is_success_status(status) else "response_too_large"
                raise ResponseTooLarge(message)
            return NormalizedApiResponse(
                status=status,
                headers=headers,
                body=body,
                content_type=content_type,
                content_encoding=content_encoding,
                size=size,
            )


class _StreamingOpenApiClient(OpenApiClient):
    """OpenAPI client request/signing path with bounded response consumption."""

    def __init__(self, config: open_api_models.Config, *, session_factory: Callable[..., Any]) -> None:
        super().__init__(config)
        self._session_factory = session_factory

    async def call_api_streaming_async(
        self,
        params: open_api_models.Params,
        request: open_api_models.OpenApiRequest,
        runtime: RuntimeOptions,
        *,
        success_limit: int,
    ) -> dict[str, Any]:
        tea_request = await self._prepare_streaming_request(params, request, runtime)
        status, headers, payload = await self._send_streaming_request(
            tea_request,
            runtime,
            success_limit=success_limit,
        )
        size = len(payload)
        if not _is_success_status(status):
            raise _tea_status_exception(status, headers, payload, size=size)
        body = (
            None
            if params.method == "HEAD" or status == 204
            else _parse_tea_success(
                payload,
                _required_tea_string(params.body_type, "missing_tea_body_type"),
                _mapping_header(headers, "content-type"),
            )
        )
        return {
            "statusCode": status,
            "headers": headers,
            "body": body,
            _TEA_RESPONSE_SIZE_KEY: size,
        }

    async def _prepare_streaming_request(
        self,
        params: open_api_models.Params,
        request: open_api_models.OpenApiRequest,
        runtime: RuntimeOptions,
    ) -> DaraRequest:
        tea_request = DaraRequest()
        tea_request.protocol = self._protocol or _required_tea_string(params.protocol, "missing_tea_protocol")
        tea_request.method = _required_tea_string(params.method, "missing_tea_method")
        tea_request.pathname = _required_tea_string(params.pathname, "missing_tea_pathname")
        global_queries: Mapping[str, Any] = {}
        global_headers: Mapping[str, str] = {}
        if self._global_parameters is not None:
            global_queries = self._global_parameters.queries or {}
            global_headers = self._global_parameters.headers or {}
        extends_queries: Mapping[str, Any] = {}
        extends_headers: Mapping[str, str] = {}
        if runtime.extends_parameters is not None:
            extends_queries = runtime.extends_parameters.queries or {}
            extends_headers = runtime.extends_parameters.headers or {}
        tea_request.query = DaraCore.merge({}, global_queries, extends_queries, request.query or {})
        tea_request.headers = DaraCore.merge(
            {
                "host": self._endpoint,
                "x-acs-version": params.version,
                "x-acs-action": params.action,
                "user-agent": Utils.get_user_agent(self._user_agent),
                "x-acs-date": Utils.get_timestamp(),
                "x-acs-signature-nonce": Utils.get_nonce(),
            },
            global_headers,
            extends_headers,
            request.headers or {},
        )
        if params.style == "RPC":
            rpc_headers = self.get_rpc_headers()
            if rpc_headers is not None:
                tea_request.headers = DaraCore.merge({}, tea_request.headers, rpc_headers)

        signature_algorithm = self._signature_algorithm or _SIGNATURE_ALGORITHM
        request_body = request.body
        if request.stream is not None:
            raise ValueError("tea_stream_request_unsupported")
        if request_body is None:
            wire_body = b""
        elif params.req_body_type == "byte":
            wire_body = bytes(request_body)
        elif params.req_body_type == "json":
            wire_body = DaraCore.to_json_string(request_body).encode("utf-8")
            if _mapping_header(tea_request.headers, "content-type") is None:
                tea_request.headers["content-type"] = "application/json; charset=utf-8"
        else:
            wire_body = Utils.to_form(request_body).encode("utf-8")
            if _mapping_header(tea_request.headers, "content-type") is None:
                tea_request.headers["content-type"] = "application/x-www-form-urlencoded"
        tea_request.body = wire_body
        payload_hash = Utils.hash(wire_body, signature_algorithm).hex()
        tea_request.headers["x-acs-content-sha256"] = payload_hash

        if params.auth_type != "Anonymous":
            if self._credential is None:
                raise open_api_exceptions.ClientException(
                    code="InvalidCredentials",
                    message="Please set up the credentials correctly.",
                )
            credential_model = await self._credential.get_credential_async()
            if credential_model.provider_name:
                tea_request.headers["x-acs-credentials-provider"] = credential_model.provider_name
            if credential_model.type == "bearer":
                tea_request.headers["x-acs-bearer-token"] = credential_model.bearer_token
                if params.style == "RPC":
                    tea_request.query["SignatureType"] = "BEARERTOKEN"
                else:
                    tea_request.headers["x-acs-signature-type"] = "BEARERTOKEN"
            elif credential_model.type == "id_token":
                tea_request.headers["x-acs-zero-trust-idtoken"] = credential_model.security_token
            else:
                access_key_id = credential_model.access_key_id
                security_token = credential_model.security_token
                if security_token:
                    tea_request.headers["x-acs-accesskey-id"] = access_key_id
                    tea_request.headers["x-acs-security-token"] = security_token
                tea_request.headers["Authorization"] = Utils.get_authorization(
                    tea_request,
                    signature_algorithm,
                    payload_hash,
                    access_key_id,
                    credential_model.access_key_secret,
                )
        return tea_request

    async def _send_streaming_request(
        self,
        request: DaraRequest,
        runtime: RuntimeOptions,
        *,
        success_limit: int,
    ) -> tuple[int, dict[str, str], bytearray]:
        query = get_canonical_query_string(request.query or {})
        suffix = f"?{query}" if query else ""
        url = URL(
            f"{request.protocol.casefold()}://{request.headers['host']}{request.pathname}{suffix}",
            encoded=True,
        )
        connect_timeout = runtime.connect_timeout or self._connect_timeout
        read_timeout = runtime.read_timeout or self._read_timeout
        timeout = aiohttp.ClientTimeout(
            sock_connect=_milliseconds_to_seconds(connect_timeout),
            sock_read=_milliseconds_to_seconds(read_timeout),
        )
        proxy = self._http_proxy if request.protocol.upper() == "HTTP" else self._https_proxy
        request_options: dict[str, Any] = {
            "data": request.body or b"",
            "headers": request.headers,
            "allow_redirects": False,
            "timeout": timeout,
        }
        if proxy:
            request_options["proxy"] = proxy
        if runtime.ignore_ssl:
            request_options["ssl"] = False

        try:
            async with self._session_factory(
                auto_decompress=True,
                trust_env=True,
                skip_auto_headers={"Accept"},
            ) as session:
                async with session.request(request.method, url, **request_options) as response:
                    status = int(response.status)
                    headers = {str(name): str(value) for name, value in response.headers.items()}
                    if request.method == "HEAD" or status == 204:
                        return status, headers, bytearray()
                    limit = _ERROR_RESPONSE_BYTES if not _is_success_status(status) else success_limit
                    expected = _trusted_content_length(response.headers)
                    if expected is not None and expected > limit:
                        message = "error_response_too_large" if not _is_success_status(status) else "response_too_large"
                        raise ResponseTooLarge(message)
                    payload = await _read_aiohttp_limited(response, limit, error=not _is_success_status(status))
                    return status, headers, payload
        except ResponseTooLarge:
            raise
        except (aiohttp.ClientError, OSError) as error:
            raise _tea_transport_wrapper(request, error) from error


def _tea_client(
    endpoint: EndpointResolution,
    credential: AliyunCredential | None,
    *,
    session_factory: Callable[..., Any] = aiohttp.ClientSession,
) -> OpenApiClient:
    config_values: dict[str, Any] = {
        "endpoint": endpoint.wire_endpoint,
        "retry_options": RetryOptions({"retryable": False}),
    }
    if endpoint.region_id:
        config_values["region_id"] = endpoint.region_id
    if credential is not None:
        dynamic_client = dynamic_credential_client(credential)
        if dynamic_client is not None:
            config_values["credential"] = dynamic_client
        else:
            config_values.update(
                access_key_id=credential.access_key_id,
                access_key_secret=credential.access_key_secret,
                security_token=credential.sts_token,
            )
    config = open_api_models.Config(**config_values)
    return _StreamingOpenApiClient(config, session_factory=session_factory)


def _uses_sls_gateway(contract: CanonicalWireContract) -> bool:
    return contract.product.casefold() == "sls"


def _uses_mns_gateway(contract: CanonicalWireContract) -> bool:
    return contract.product.casefold() == "smqproxy" and contract.version == "2026-04-09"


def _uses_oss_gateway(contract: CanonicalWireContract) -> bool:
    return contract.product.casefold() == "hcs-mgw" and contract.version == "2024-06-26"


def _uses_pop_v4_gateway(contract: CanonicalWireContract) -> bool:
    return contract.product.casefold() == "searchplat" and contract.version == "2024-05-29"


def _attach_sls_gateway(client: Any) -> None:
    if not hasattr(client, "_spi") or getattr(client, "_spi", None) is not None:
        return
    from alibabacloud_gateway_sls.client import Client as SlsGatewayClient

    client._spi = SlsGatewayClient()


def _attach_mns_gateway(client: Any) -> None:
    if not hasattr(client, "_spi") or getattr(client, "_spi", None) is not None:
        return
    from alibabacloud_gateway_mns.client import Client as MnsGatewayClient
    from alibabacloud_gateway_spi.client import Client as GatewaySpiClient
    from alibabacloud_tea_util.client import Client as TeaUtilClient
    from Tea.exceptions import TeaException

    class MnsGatewayAdapter(MnsGatewayClient):
        def __init__(self) -> None:
            GatewaySpiClient.__init__(self)
            self._sign_prefix = "aliyun_v4"
            self._sign_suffix = "aliyun_v4_request"
            self._auth_prefix = "MNS4-HMAC-SHA256"

        async def modify_request_async(self, context: Any, attribute_map: Any) -> None:
            context.request.version = "2015-06-06"
            context.request.headers.setdefault("content-type", "text/xml;charset=UTF-8")
            await super().modify_request_async(context, attribute_map)

        async def modify_response_async(self, context: Any, attribute_map: Any) -> None:
            response = context.response
            content_type = str(response.headers.get("content-type", ""))
            if "xml" not in content_type.casefold():
                await super().modify_response_async(context, attribute_map)
                return
            payload = await TeaUtilClient.read_as_string_async(response.body)
            try:
                body = _xml_element_value(ElementTree.fromstring(payload))
            except (ElementTree.ParseError, DefusedXmlException) as error:
                raise TeaException({"code": "InvalidXmlResponse", "message": "invalid MNS XML response"}) from error
            if 400 <= int(response.status_code) < 600:
                data = dict(body) if isinstance(body, Mapping) else {}
                data["statusCode"] = int(response.status_code)
                raise TeaException(
                    {
                        "code": str(data.get("Code") or "MnsError"),
                        "message": str(data.get("Message") or "MNS request failed"),
                        "data": data,
                    }
                )
            response.deserialized_body = body

    client._spi = MnsGatewayAdapter()


def _attach_oss_gateway(client: Any) -> None:
    if not hasattr(client, "_spi") or getattr(client, "_spi", None) is not None:
        return
    from alibabacloud_gateway_oss.client import Client as OssGatewayClient

    client._product_id = "hcs-mgw"
    client._endpoint_rule = ""
    client._spi = OssGatewayClient()


def _attach_pop_v4_gateway(client: Any, *, product_id: str) -> None:
    if not hasattr(client, "_spi") or getattr(client, "_spi", None) is not None:
        return
    from alibabacloud_gateway_pop.client import Client as PopGatewayClient

    client._product_id = product_id
    client._endpoint_rule = ""
    client._spi = PopGatewayClient()


def _tea_request_type(contract: CanonicalWireContract) -> str:
    if _uses_oss_gateway(contract):
        return "xml"
    return "json" if contract.request_body_type == "none" else contract.request_body_type


def _tea_response_type(contract: CanonicalWireContract, *, method: str) -> str:
    if _uses_oss_gateway(contract):
        return "xml"
    if method == "HEAD":
        return "string"
    return {"json": "json", "string": "string", "binary": "binary", "none": "string"}[contract.response_body_type]


def _tea_body(contract: CanonicalWireContract, body: bytes | None) -> Any:
    if body is None:
        return None
    if contract.request_body_type == "json":
        return json.loads(body)
    if contract.request_body_type == "formData":
        return dict(parse_qsl(body.decode("ascii"), keep_blank_values=True))
    return body


class Acs3StreamingTransport:
    def __init__(
        self,
        *,
        client: httpx.AsyncClient | None = None,
        clock: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
        nonce: Callable[[], str] = lambda: Utils.get_nonce(),
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        self._client = client or httpx.AsyncClient(follow_redirects=False)
        self._clock = clock
        self._nonce = nonce
        self._sleep = sleep

    def prepare_request(
        self,
        contract: CanonicalWireContract,
        request: BuiltApiRequest,
        endpoint: EndpointResolution,
        credential: AliyunCredential | None,
    ) -> TransportRequest:
        if contract.signature_scheme != "acs3":
            raise ValueError("unsupported_signature_scheme")
        query = dict(request.canonical_query)
        canonical_query = get_canonical_query_string(query).encode("ascii")
        raw_path = request.raw_path + (b"?" + canonical_query if canonical_query else b"")
        headers = {name.casefold(): value for name, value in request.headers.items()}
        headers.update(
            {
                "host": endpoint.wire_endpoint,
                "x-acs-action": contract.action,
                "x-acs-version": contract.version,
                "x-acs-date": self._clock().astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
                "x-acs-signature-nonce": self._nonce(),
            }
        )
        payload_hash = Utils.hash(request.body or b"", _SIGNATURE_ALGORITHM).hex()
        headers["x-acs-content-sha256"] = payload_hash
        if contract.auth_type != "Anonymous":
            if credential is None:
                raise ValueError("credential_required")
            if credential.sts_token:
                headers["x-acs-accesskey-id"] = credential.access_key_id
                headers["x-acs-security-token"] = credential.sts_token
        url = httpx.URL(scheme="https", host=endpoint.wire_endpoint, raw_path=raw_path)
        authorization = ""
        signed_headers: tuple[str, ...] = ()
        if contract.auth_type != "Anonymous":
            assert credential is not None
            signing_request = _SigningRequest(request.method, request.raw_path.decode("ascii"), query, headers)
            authorization = Utils.get_authorization(
                signing_request,
                _SIGNATURE_ALGORITHM,
                payload_hash,
                credential.access_key_id,
                credential.access_key_secret,
            )
            headers["authorization"] = authorization
            signed_headers = tuple(_signed_headers(authorization))
        http_request = httpx.Request(request.method, url, headers=headers, content=request.body)
        return TransportRequest(http_request, payload_hash, authorization, signed_headers)

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
        if contract.auth_type == "Anonymous":
            signing_credential = None
        else:
            if credential is None:
                raise ValueError("credential_required")
            signing_credential = await budget.run_attempt(
                lambda: resolve_signing_credential(credential),
                retryable_call=False,
            )
        eligible = retry_eligible(
            operation_type=contract.operation_type,
            method=request.method,
            has_body=request.body is not None,
        )
        previous_reason: RetryReason | None = None
        while True:
            attempt = await budget.acquire(reason=previous_reason)
            previous_reason = None
            prepared = self.prepare_request(contract, request, endpoint, signing_credential)
            response: httpx.Response | None = None
            try:
                received_response = await budget.run_attempt(
                    lambda: self._client.send(prepared.request, stream=True, follow_redirects=False),
                    retryable_call=eligible,
                    abandon_result=lambda abandoned: abandoned.aclose(),
                )
                response = received_response
                reason = map_retryable_status(received_response.status_code)
                if reason is not None and eligible and attempt < budget.max_attempts:
                    retry_after = received_response.headers.get("retry-after")
                    try:
                        delay = retry_delay(
                            budget,
                            failed_attempt=attempt,
                            retry_after=retry_after,
                            reason=reason,
                        )
                    except RetryExhausted:
                        return await budget.run_attempt(
                            lambda: self._normalize(received_response, contract, request, context),
                            retryable_call=eligible,
                        )
                    previous_reason = reason
                    await _close_response(response)
                    response = None
                    await self._sleep(delay)
                    continue
                return await budget.run_attempt(
                    lambda: self._normalize(received_response, contract, request, context),
                    retryable_call=eligible,
                )
            except BaseException as error:
                reason = map_httpx_retry_reason(error)
                if reason is not None and eligible and attempt < budget.max_attempts:
                    if response is not None:
                        await _close_response(response)
                        response = None
                    delay = retry_delay(budget, failed_attempt=attempt, reason=reason)
                    previous_reason = reason
                    await self._sleep(delay)
                    continue
                if is_transport_failure(error):
                    raise TransportFailure(
                        outcome=classify_transport_failure(error, retryable_call=eligible),
                        reason=reason,
                    ) from error
                raise
            finally:
                if response is not None:
                    primary = sys.exc_info()[1]
                    await _close_response(response, primary=primary)

    async def _normalize(
        self,
        response: httpx.Response,
        contract: CanonicalWireContract,
        request: BuiltApiRequest,
        context: ToolContext,
    ) -> NormalizedApiResponse:
        headers = filter_response_headers(response.headers, declared_headers=request.response_policy.declared_headers)
        content_type = response.headers.get("content-type")
        content_encoding = response.headers.get("content-encoding")
        if request.method == "HEAD" or response.status_code == 204 or request.response_policy.mode == "headers_only":
            return NormalizedApiResponse(response.status_code, headers, None, content_type, content_encoding, 0)
        limit = _response_limit(request, response.status_code)
        expected = _trusted_content_length(response.headers)
        if expected is not None and expected > limit:
            message = (
                "error_response_too_large" if not _is_success_status(response.status_code) else "response_too_large"
            )
            raise ResponseTooLarge(message)
        if not _is_success_status(response.status_code):
            payload = await _read_limited(response, limit, error=True)
            body = _normalize_error(payload, content_type)
            return NormalizedApiResponse(
                response.status_code, headers, body, content_type, content_encoding, len(payload)
            )
        if request.response_policy.mode == "binary":
            payload = await _read_limited(response, limit)
            return NormalizedApiResponse(
                response.status_code,
                headers,
                _base64_body(payload),
                content_type,
                content_encoding,
                len(payload),
            )
        if request.response_policy.mode == "json":
            payload = await _read_limited(response, limit)
            try:
                body = _normalize_json_success(payload, content_type) if payload else None
            except (UnicodeDecodeError, json.JSONDecodeError, ElementTree.ParseError, DefusedXmlException) as error:
                raise RuntimeError("invalid_response") from error
            return NormalizedApiResponse(
                response.status_code, headers, body, content_type, content_encoding, len(payload)
            )
        try:
            payload = await _read_limited(response, limit)
            body = _decode_text(payload, content_type)
        except (LookupError, UnicodeError) as error:
            raise RuntimeError("invalid_response") from error
        return NormalizedApiResponse(response.status_code, headers, body, content_type, content_encoding, len(payload))

    async def aclose(self) -> None:
        await self._client.aclose()


@dataclass
class _SigningRequest:
    method: str
    pathname: str
    query: Mapping[str, str]
    headers: Mapping[str, str]


def _signed_headers(authorization: str) -> list[str]:
    marker = "SignedHeaders="
    value = authorization.split(marker, 1)[1].split(",", 1)[0]
    return value.split(";")


def _required_tea_string(value: Any, error: str) -> str:
    if not isinstance(value, str) or not value:
        raise ValueError(error)
    return value


def _milliseconds_to_seconds(value: int | None) -> float | None:
    return None if value is None else value / 1000


async def _read_aiohttp_limited(response: Any, limit: int, *, error: bool) -> bytearray:
    payload = bytearray()
    decoded_size = 0
    async for chunk in response.content.iter_chunked(_STREAM_CHUNK_BYTES):
        decoded_size += len(chunk)
        raw_size = getattr(response.content, "total_raw_bytes", decoded_size)
        if raw_size > limit or decoded_size > limit:
            message = "error_response_too_large" if error else "response_too_large"
            raise ResponseTooLarge(message)
        payload.extend(chunk)
    raw_size = getattr(response.content, "total_raw_bytes", decoded_size)
    if raw_size > limit:
        message = "error_response_too_large" if error else "response_too_large"
        raise ResponseTooLarge(message)
    return payload


def _tea_transport_wrapper(request: DaraRequest, error: BaseException) -> UnretryableException:
    inner: BaseException = error
    if isinstance(error, OSError):
        retry_error = RetryError(str(error))
        retry_error.__context__ = error
        inner = retry_error
    context = RetryPolicyContext(retries_attempted=1, http_request=request, exception=inner)
    return UnretryableException(context)


def _parse_tea_success(payload: bytearray, body_type: str, content_type: str | None = None) -> Any:
    try:
        if body_type in {"json", "array"}:
            return _normalize_json_success(payload, content_type) if payload else None
        if body_type in {"binary", "byte"}:
            return bytes(payload)
        return payload.decode("utf-8")
    except (UnicodeDecodeError, json.JSONDecodeError, ElementTree.ParseError, DefusedXmlException) as error:
        raise RuntimeError("invalid_response") from error


def _tea_status_exception(
    status: int,
    headers: Mapping[str, str],
    payload: bytearray,
    *,
    size: int,
) -> open_api_exceptions.AlibabaCloudException:
    content_type = _mapping_header(headers, "content-type")
    normalized = _normalize_error(payload, content_type)
    data = dict(normalized) if isinstance(normalized, Mapping) else {"Message": normalized}
    code = str(data.get("Code") or data.get("code") or "")
    request_id = str(data.get("RequestId") or data.get("requestId") or "")
    response_message = str(data.get("Message") or data.get("message") or "")
    description = str(data.get("Description") or data.get("description") or "")
    message = f"code: {status}, {response_message} request id: {request_id}"
    lowered_headers = {name.casefold(): value for name, value in headers.items()}
    has_rate_limit = bool({"x-ratelimit-user-api", "x-ratelimit-user"} & lowered_headers.keys())
    retry_after = Utils.get_throttling_time_left(lowered_headers) if has_rate_limit else None
    if code in {"Throttling", "Throttling.User", "Throttling.Api"}:
        exception_kwargs: dict[str, Any] = {
            "status_code": status,
            "code": code,
            "message": message,
            "description": description,
            "data": data,
            "request_id": request_id,
        }
        if retry_after is not None:
            exception_kwargs["retry_after"] = retry_after
        exception: open_api_exceptions.AlibabaCloudException = open_api_exceptions.ThrottlingException(
            **exception_kwargs
        )
    elif status < 500:
        access_denied_detail = data.get("AccessDeniedDetail") or data.get("accessDeniedDetail")
        if not isinstance(access_denied_detail, dict):
            access_denied_detail = {}
        exception = open_api_exceptions.ClientException(
            status_code=status,
            code=code,
            message=message,
            description=description,
            data=data,
            access_denied_detail=access_denied_detail,
            request_id=request_id,
        )
    else:
        exception = open_api_exceptions.ServerException(
            status_code=status,
            code=code,
            message=message,
            description=description,
            data=data,
            request_id=request_id,
        )
    setattr(exception, _TEA_RESPONSE_SIZE_ATTRIBUTE, size)
    setattr(exception, _TEA_RESPONSE_HEADERS_ATTRIBUTE, dict(headers))
    return exception


def _tea_exception_retry_after(error: open_api_exceptions.AlibabaCloudException) -> str | None:
    response_headers = getattr(error, _TEA_RESPONSE_HEADERS_ATTRIBUTE, {})
    if isinstance(response_headers, Mapping):
        retry_after = _mapping_header(response_headers, "retry-after")
        if retry_after is not None:
            return retry_after
    milliseconds = error.retry_after
    if type(milliseconds) is not int or milliseconds < 0:
        return None
    return str(milliseconds / 1000)


async def _read_limited(response: httpx.Response, limit: int, *, error: bool = False) -> bytearray:
    payload = bytearray()
    size = 0
    async for chunk in response.aiter_bytes(chunk_size=_STREAM_CHUNK_BYTES):
        size += len(chunk)
        if response.num_bytes_downloaded > limit or size > limit:
            message = "error_response_too_large" if error else "response_too_large"
            raise ResponseTooLarge(message)
        payload.extend(chunk)
    if response.num_bytes_downloaded > limit:
        message = "error_response_too_large" if error else "response_too_large"
        raise ResponseTooLarge(message)
    return payload


async def _capture_response_close_error(response: httpx.Response) -> BaseException | None:
    try:
        await response.aclose()
    except BaseException as error:
        return error
    return None


async def _close_response(response: httpx.Response, *, primary: BaseException | None = None) -> None:
    close_task = asyncio.create_task(_capture_response_close_error(response))
    cancellations: list[asyncio.CancelledError] = []
    while not close_task.done():
        try:
            await asyncio.shield(close_task)
        except asyncio.CancelledError as error:
            cancellations.append(error)
    close_error = close_task.result()
    if primary is not None:
        if close_error is not None or cancellations:
            _splice_stable_causes(primary, tuple(cancellations), direct_cause=close_error)
        return
    if cancellations:
        primary_cancellation, *later_cancellations = cancellations
        if close_error is not None or later_cancellations:
            _raise_with_stable_causes(
                primary_cancellation,
                tuple(later_cancellations),
                direct_cause=close_error,
            )
        raise primary_cancellation
    if close_error is not None:
        raise close_error


def _raise_with_stable_causes(
    primary: BaseException,
    causes: tuple[BaseException, ...],
    *,
    direct_cause: BaseException | None = None,
) -> None:
    _splice_stable_causes(primary, causes, direct_cause=direct_cause)
    raise primary


def _splice_stable_causes(
    primary: BaseException,
    causes: tuple[BaseException, ...],
    *,
    direct_cause: BaseException | None = None,
) -> None:
    ordered = tuple(
        cause for cause in (direct_cause, primary.__cause__, *causes) if cause is not None and cause is not primary
    )
    cause = _stable_cause_chain(ordered)
    if cause is not None:
        primary.__cause__ = cause
        primary.__suppress_context__ = True


def _stable_cause_chain(errors: tuple[BaseException, ...]) -> BaseException | None:
    unique: list[BaseException] = []
    seen: set[int] = set()
    for error in errors:
        if id(error) not in seen:
            unique.append(error)
            seen.add(id(error))
    for current, following in zip(unique, unique[1:]):
        tail = current
        chain_ids = {id(tail)}
        while tail.__cause__ is not None and id(tail.__cause__) not in chain_ids:
            tail = tail.__cause__
            chain_ids.add(id(tail))
        if id(following) not in chain_ids:
            tail.__cause__ = following
            tail.__suppress_context__ = True
    return unique[0] if unique else None


def _response_limit(request: BuiltApiRequest, status: int) -> int:
    limit = _ERROR_RESPONSE_BYTES if not _is_success_status(status) else request.response_policy.max_bytes
    if limit <= 0 or limit > _MAX_RESPONSE_BYTES:
        raise ValueError("invalid_response_limit")
    return limit


def _is_success_status(status: int) -> bool:
    return 200 <= status < 300


def _trusted_content_length(headers: Mapping[str, Any] | httpx.Headers) -> int | None:
    if isinstance(headers, httpx.Headers):
        values = headers.get_list("content-length")
    elif callable(getall := getattr(headers, "getall", None)):
        values = [str(value) for value in getall("content-length", [])]
    else:
        value = _mapping_header(headers, "content-length")
        values = [] if value is None else [value]
    if not values or any(not value.isdecimal() for value in values):
        return None
    lengths = {int(value) for value in values}
    return lengths.pop() if len(lengths) == 1 else None


def _parsed_content_type(content_type: str | None) -> ParsedContentType | None:
    if content_type is None:
        return None
    try:
        return parse_content_type(content_type)
    except ApiContractError:
        return None


def _media_type(content_type: str | None) -> str | None:
    parsed = _parsed_content_type(content_type)
    return parsed.media_type if parsed is not None else None


def _charset(content_type: str | None) -> str:
    parsed = _parsed_content_type(content_type)
    return parsed.parameters.get("charset", "utf-8") if parsed is not None else "utf-8"


def _decode_text(payload: bytes | bytearray, content_type: str | None) -> str:
    decoder = codecs.getincrementaldecoder(_charset(content_type))(errors="strict")
    return decoder.decode(payload, final=True)


def _base64_body(payload: bytes | bytearray) -> dict[str, str]:
    return {"encoding": "base64", "data": base64.b64encode(payload).decode("ascii")}


def _normalize_error(payload: bytes | bytearray, content_type: str | None) -> Any:
    media_type = _media_type(content_type)
    if media_type and (media_type == "application/json" or media_type.endswith("+json")):
        try:
            return json.loads(payload)
        except (UnicodeDecodeError, json.JSONDecodeError):
            return {"error": "invalid_json_error_response"}
    if media_type and (media_type in {"application/xml", "text/xml"} or media_type.endswith("+xml")):
        try:
            root = ElementTree.fromstring(payload)
        except ElementTree.ParseError:
            return {"error": "invalid_xml_error_response"}
        values = {}
        for name in ("Code", "Message", "RequestId", "Description"):
            element = root.find(f".//{name}")
            if element is not None and element.text is not None:
                values[name] = element.text
        return values
    if media_type and (media_type.startswith("text/") or media_type == "application/xml"):
        try:
            return _decode_text(payload, content_type)
        except (LookupError, UnicodeError):
            return {"error": "invalid_text_error_response"}
    return {"error": "unrecognized_error_response", "size": len(payload)}


def _normalize_json_success(payload: bytes | bytearray, content_type: str | None) -> Any:
    media_type = _media_type(content_type)
    if media_type and (media_type in {"application/xml", "text/xml"} or media_type.endswith("+xml")):
        return _xml_element_value(ElementTree.fromstring(payload))
    return json.loads(payload)


def _xml_element_value(element: Any) -> Any:
    children = list(element)
    if not children:
        return element.text or ""
    values: dict[str, Any] = {}
    for child in children:
        name = _xml_local_name(child.tag)
        value = _xml_element_value(child)
        if name in values:
            existing = values[name]
            if isinstance(existing, list):
                existing.append(value)
            else:
                values[name] = [existing, value]
        else:
            values[name] = value
    return values


def _xml_local_name(tag: Any) -> str:
    value = str(tag)
    return value.rsplit("}", 1)[-1]


def _mapping_header(headers: Mapping[str, Any], name: str) -> str | None:
    for key, value in headers.items():
        if str(key).casefold() == name:
            return str(value)
    return None


def _tea_result_status(result: Mapping[str, Any]) -> int:
    for name in ("statusCode", "status", "http_status"):
        value = result.get(name)
        if value is not None:
            return int(value)
    return 204 if "headers" in result and "body" not in result else 200


def _normalize_tea_error(
    error: open_api_exceptions.AlibabaCloudException,
    *,
    declared_headers: tuple[str, ...],
) -> NormalizedApiResponse:
    if error.status_code is None:
        raise ValueError("missing_tea_response_status")
    status = error.status_code
    raw_headers = getattr(error, _TEA_RESPONSE_HEADERS_ATTRIBUTE, {})
    if not isinstance(raw_headers, Mapping):
        raise ValueError("invalid_tea_response_headers")
    body = error.data
    if body is None:
        body = {
            name: value
            for name, value in (
                ("Code", error.code),
                ("Message", error.message),
                ("RequestId", error.request_id),
                ("Description", error.description),
            )
            if value is not None
        }
    size = getattr(error, _TEA_RESPONSE_SIZE_ATTRIBUTE, None)
    if type(size) is not int or size < 0:
        raise ValueError("missing_tea_error_response_size")
    if size > _ERROR_RESPONSE_BYTES:
        raise ResponseTooLarge("error_response_too_large")
    headers = filter_response_headers(raw_headers, declared_headers=declared_headers)
    content_type = _mapping_header(raw_headers, "content-type")
    content_encoding = _mapping_header(raw_headers, "content-encoding")
    return NormalizedApiResponse(
        status=status,
        headers=headers,
        body=body,
        content_type=content_type,
        content_encoding=content_encoding,
        size=size,
    )


def _normalize_spi_gateway_error(
    error: BaseException,
    *,
    declared_headers: tuple[str, ...],
) -> NormalizedApiResponse | None:
    tea_error = _spi_gateway_tea_exception(error)
    if tea_error is None:
        return None
    data = getattr(tea_error, "data", None)
    if not isinstance(data, Mapping):
        return None
    status_value = data.get("statusCode", data.get("httpCode", getattr(tea_error, "statusCode", None)))
    if status_value is None:
        return None
    status = int(status_value)
    body = {
        name: value
        for name, value in (
            ("Code", getattr(tea_error, "code", None)),
            ("Message", getattr(tea_error, "message", None)),
            ("RequestId", data.get("requestId")),
        )
        if value is not None
    }
    size = _estimated_tea_body_size(body)
    if size > _ERROR_RESPONSE_BYTES:
        raise ResponseTooLarge("error_response_too_large")
    return NormalizedApiResponse(
        status=status,
        headers=filter_response_headers({}, declared_headers=declared_headers),
        body=body,
        content_type=None,
        content_encoding=None,
        size=size,
    )


def _spi_gateway_tea_exception(error: BaseException) -> BaseException | None:
    if type(error) is not UnretryableException:
        return None
    inner = error.inner_exception
    if type(inner) is RetryError:
        nested = inner.__cause__ or inner.__context__
        inner = nested if isinstance(nested, BaseException) else inner
    try:
        from Tea.exceptions import TeaException
    except ImportError:
        return None
    return inner if isinstance(inner, TeaException) else None


def _tea_result_size(result: Mapping[str, Any], body: Any) -> int:
    size = result.get(_TEA_RESPONSE_SIZE_KEY)
    if type(size) is int and size >= 0:
        return size
    if body is None:
        return 0
    if isinstance(body, (bytes, bytearray)):
        return len(body)
    if isinstance(body, str):
        return len(body.encode("utf-8"))
    raise ValueError("missing_tea_response_size")


def _estimated_tea_body_size(body: Any) -> int:
    if body is None:
        return 0
    if isinstance(body, (bytes, bytearray)):
        return len(body)
    if isinstance(body, str):
        return len(body.encode("utf-8"))
    return len(json.dumps(body, ensure_ascii=False, separators=(",", ":")).encode("utf-8"))
