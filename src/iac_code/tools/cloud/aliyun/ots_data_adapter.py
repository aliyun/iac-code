"""Narrow Tablestore data-plane adapter for reviewed read operations."""

from __future__ import annotations

import json
from collections.abc import Callable
from functools import lru_cache
from types import MappingProxyType
from typing import Any

from iac_code.services.providers.aliyun import AliyunCredential
from iac_code.tools.base import ToolContext
from iac_code.tools.cloud.aliyun.acs3_transport import (
    NormalizedApiResponse,
    ResponseTooLarge,
    _response_limit,
    resolve_signing_credential,
)
from iac_code.tools.cloud.aliyun.api_contract import BuiltApiRequest, CanonicalWireContract
from iac_code.tools.cloud.aliyun.endpoint_resolver import EndpointResolution
from iac_code.tools.cloud.aliyun.openmeta import ApiMetadata, ParameterMetadata
from iac_code.tools.cloud.aliyun.retry_policy import RetryBudget, TransportFailure

OTS_DATA_PRODUCT = "Tablestore"
OTS_DATA_VERSION = "2015-12-31"
OTS_LIST_TABLE_ACTION = "ListTable"
_INSTANCE_PARAMETER = "InstanceName"


def is_ots_list_table(product: str, action: str, version: str | None = None) -> bool:
    return (
        product.casefold() == OTS_DATA_PRODUCT.casefold()
        and action.casefold() == OTS_LIST_TABLE_ACTION.casefold()
        and version in {None, OTS_DATA_VERSION}
    )


@lru_cache(maxsize=1)
def ots_list_table_metadata() -> ApiMetadata:
    parameter = ParameterMetadata(
        name=_INSTANCE_PARAMETER,
        location="host",
        required=True,
        style=None,
        path_encoding=None,
        schema=MappingProxyType({"type": "string"}),
        description="Name of the Tablestore instance whose tables are listed.",
        example="my-instance",
    )
    return ApiMetadata(
        product=OTS_DATA_PRODUCT,
        version=OTS_DATA_VERSION,
        action=OTS_LIST_TABLE_ACTION,
        title="List tables",
        summary="List the names of tables in one Tablestore instance.",
        style="ROA",
        methods=("POST",),
        method="POST",
        pathname="/ListTable",
        schemes=("HTTPS",),
        consumes=(),
        produces=("application/json",),
        request_body_type="none",
        response_body_type="json",
        operation_type="read",
        parameters=(parameter,),
        document_parameters=(parameter,),
        security_declared=True,
        security_requirements=(),
        document_components=MappingProxyType({}),
        validation_components=MappingProxyType({}),
        responses=MappingProxyType(
            {
                "200": {
                    "description": "A normalized list of table names.",
                    "schema": {
                        "type": "object",
                        "properties": {
                            "Tables": {
                                "type": "array",
                                "items": {
                                    "type": "object",
                                    "properties": {"TableName": {"type": "string"}},
                                },
                            }
                        },
                    },
                }
            }
        ),
        declared_response_headers=(),
        response_header_metadata_valid=True,
        response_schema_references_valid=True,
        deprecated=False,
        error_codes=MappingProxyType({}),
        change_set=(),
        static_info=MappingProxyType({}),
    )


class OtsDataAdapter:
    """Execute the fixed ListTable operation through Alibaba Cloud's SDK."""

    def __init__(self, *, client_factory: Callable[..., Any] | None = None) -> None:
        self._client_factory = client_factory or _ots_client

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
        if (
            contract.transport != "ots_sdk"
            or contract.signature_scheme != "ots_v4"
            or not is_ots_list_table(contract.product, contract.action, contract.version)
            or contract.method != "POST"
            or request.method != "POST"
        ):
            raise ValueError("invalid_ots_transport_contract")
        if credential is None:
            raise ValueError("aliyun_credentials_required")
        if endpoint.region_id is None or endpoint.expected_host is None:
            raise ValueError("ots_target_binding_required")
        instance_name = request.host_values.get(_INSTANCE_PARAMETER)
        if not instance_name:
            raise ValueError("ots_instance_name_required")

        signing_credential = await budget.run_attempt(
            lambda: resolve_signing_credential(credential),
            retryable_call=False,
        )
        client = self._client_factory(
            endpoint="https://" + endpoint.expected_host,
            credential=signing_credential,
            instance_name=instance_name,
            region_id=endpoint.region_id,
        )
        await budget.acquire()
        try:
            table_names = await budget.run_attempt(client.list_table, retryable_call=False)
        except Exception as error:
            response = _service_error_response(error, request)
            if response is not None:
                return response
            raise TransportFailure(outcome="target_transport_failure", reason=None) from error
        finally:
            await client.close()

        if not isinstance(table_names, list | tuple) or not all(isinstance(name, str) for name in table_names):
            raise TransportFailure(outcome="protocol_error", reason=None)
        body = {
            "InstanceName": instance_name,
            "Tables": [{"TableName": name, "InstanceName": instance_name} for name in table_names],
        }
        size = len(json.dumps(body, separators=(",", ":"), ensure_ascii=False).encode("utf-8"))
        if size > _response_limit(request, 200):
            raise ResponseTooLarge("response_too_large")
        return NormalizedApiResponse(
            status=200,
            headers=MappingProxyType({}),
            body=body,
            content_type="application/json",
            content_encoding=None,
            size=size,
        )


def _ots_client(*, endpoint: str, credential: AliyunCredential, instance_name: str, region_id: str) -> Any:
    from tablestore import AsyncOTSClient
    from tablestore.retry import NoRetryPolicy

    return AsyncOTSClient(
        endpoint,
        credential.access_key_id,
        credential.access_key_secret,
        instance_name,
        sts_token=credential.sts_token or None,
        region=region_id,
        socket_timeout=30,
        retry_policy=NoRetryPolicy(),
        enable_native=False,
    )


def _service_error_response(error: Exception, request: BuiltApiRequest) -> NormalizedApiResponse | None:
    status = _error_value(error, "get_http_status")
    if not isinstance(status, int) or isinstance(status, bool) or status < 100 or status > 599:
        return None
    code = _error_value(error, "get_error_code")
    message = _error_value(error, "get_error_message")
    request_id = _error_value(error, "get_request_id")
    body = {
        key: value
        for key, value in (("Code", code), ("Message", message), ("RequestId", request_id))
        if isinstance(value, str) and value
    }
    size = len(json.dumps(body, separators=(",", ":"), ensure_ascii=False).encode("utf-8"))
    if size > _response_limit(request, status):
        raise ResponseTooLarge("error_response_too_large")
    return NormalizedApiResponse(
        status=status,
        headers=MappingProxyType({}),
        body=body,
        content_type="application/json",
        content_encoding=None,
        size=size,
    )


def _error_value(error: Exception, method_name: str) -> Any:
    method = getattr(error, method_name, None)
    if not callable(method):
        return None
    try:
        return method()
    except Exception:
        return None
