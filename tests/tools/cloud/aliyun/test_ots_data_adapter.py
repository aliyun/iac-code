from __future__ import annotations

import json
import time
from dataclasses import replace
from types import MappingProxyType
from typing import Any

import pytest

from iac_code.services.providers.aliyun import AliyunCredential
from iac_code.tools.base import ToolContext
from iac_code.tools.cloud.aliyun.acs3_transport import ResponseTooLarge
from iac_code.tools.cloud.aliyun.api_contract import BuiltApiRequest, CanonicalWireContract, ResponseBodyPolicy
from iac_code.tools.cloud.aliyun.endpoint_resolver import EndpointResolution
from iac_code.tools.cloud.aliyun.ots_data_adapter import OtsDataAdapter, ots_list_table_metadata
from iac_code.tools.cloud.aliyun.retry_policy import RetryBudget, TransportFailure


def contract() -> CanonicalWireContract:
    metadata = ots_list_table_metadata()
    return CanonicalWireContract(
        metadata_source="explicit_fallback",
        product=metadata.product,
        version=metadata.version,
        action=metadata.action,
        style=metadata.style,
        method=metadata.method,
        pathname=metadata.pathname,
        operation_type=None,
        auth_type="AK",
        signature_scheme="ots_v4",
        transport="ots_sdk",
        executable=True,
        unsupported_reasons=(),
        parameters=metadata.parameters,
        consumes=(),
        produces=("application/json",),
        policy_digest="test",
    )


def request() -> BuiltApiRequest:
    return BuiltApiRequest(
        method="POST",
        raw_path=b"/ListTable",
        canonical_query=(),
        headers=MappingProxyType({}),
        body=None,
        response_policy=ResponseBodyPolicy("json", 1024 * 1024, ()),
        host_values=MappingProxyType({"InstanceName": "fvt-ros"}),
    )


def endpoint() -> EndpointResolution:
    return EndpointResolution(
        endpoint="cn-shenzhen.ots.aliyuncs.com",
        source="override_pattern",
        host_template="{InstanceName}.{endpoint}",
        expected_host="fvt-ros.cn-shenzhen.ots.aliyuncs.com",
        region_id="cn-shenzhen",
    )


def credential() -> AliyunCredential:
    return AliyunCredential(
        access_key_id="fake-access-key",
        access_key_secret="fake-access-secret",
        sts_token="fake-sts-token",
        region_id="cn-shenzhen",
    )


def budget() -> RetryBudget:
    return RetryBudget(deadline=time.monotonic() + 30)


@pytest.mark.asyncio
async def test_ots_adapter_normalizes_tables_and_closes_the_client(monkeypatch: pytest.MonkeyPatch) -> None:
    from iac_code.tools.cloud.aliyun import ots_data_adapter as adapter_module

    signing_credential = credential()

    async def resolve(value: AliyunCredential) -> AliyunCredential:
        assert value == signing_credential
        return signing_credential

    monkeypatch.setattr(adapter_module, "resolve_signing_credential", resolve)
    calls: list[dict[str, Any]] = []

    class Client:
        closed = False

        async def list_table(self) -> tuple[str, ...]:
            return ("orders", "inventory")

        async def close(self) -> None:
            self.closed = True

    client = Client()

    def client_factory(**kwargs: Any) -> Client:
        calls.append(kwargs)
        return client

    response = await OtsDataAdapter(client_factory=client_factory).execute(
        contract=contract(),
        request=request(),
        endpoint=endpoint(),
        credential=signing_credential,
        context=ToolContext(),
        budget=budget(),
    )

    assert calls == [
        {
            "endpoint": "https://fvt-ros.cn-shenzhen.ots.aliyuncs.com",
            "credential": signing_credential,
            "instance_name": "fvt-ros",
            "region_id": "cn-shenzhen",
        }
    ]
    assert response.status == 200
    assert response.body == {
        "InstanceName": "fvt-ros",
        "Tables": [
            {"TableName": "orders", "InstanceName": "fvt-ros"},
            {"TableName": "inventory", "InstanceName": "fvt-ros"},
        ],
    }
    assert client.closed is True


@pytest.mark.asyncio
async def test_ots_adapter_preserves_service_error_code(monkeypatch: pytest.MonkeyPatch) -> None:
    from iac_code.tools.cloud.aliyun import ots_data_adapter as adapter_module

    async def resolve(value: AliyunCredential) -> AliyunCredential:
        return value

    monkeypatch.setattr(adapter_module, "resolve_signing_credential", resolve)

    class ServiceError(Exception):
        def get_http_status(self) -> int:
            return 403

        def get_error_code(self) -> str:
            return "OTSAuthFailed"

        def get_error_message(self) -> str:
            return "Access denied"

        def get_request_id(self) -> str:
            return "00000000-0000-0000-0000-000000000000"

    class Client:
        closed = False

        async def list_table(self) -> tuple[str, ...]:
            raise ServiceError()

        async def close(self) -> None:
            self.closed = True

    client = Client()
    response = await OtsDataAdapter(client_factory=lambda **_: client).execute(
        contract=contract(),
        request=request(),
        endpoint=endpoint(),
        credential=credential(),
        context=ToolContext(),
        budget=budget(),
    )

    assert response.status == 403
    assert response.body == {
        "Code": "OTSAuthFailed",
        "Message": "Access denied",
        "RequestId": "00000000-0000-0000-0000-000000000000",
    }
    assert client.closed is True


@pytest.mark.asyncio
async def test_ots_adapter_rejects_unstructured_client_failures(monkeypatch: pytest.MonkeyPatch) -> None:
    from iac_code.tools.cloud.aliyun import ots_data_adapter as adapter_module

    async def resolve(value: AliyunCredential) -> AliyunCredential:
        return value

    monkeypatch.setattr(adapter_module, "resolve_signing_credential", resolve)

    class Client:
        async def list_table(self) -> tuple[str, ...]:
            raise RuntimeError("network failed")

        async def close(self) -> None:
            return None

    with pytest.raises(TransportFailure, match="target_transport_failure"):
        await OtsDataAdapter(client_factory=lambda **_: Client()).execute(
            contract=contract(),
            request=request(),
            endpoint=endpoint(),
            credential=credential(),
            context=ToolContext(),
            budget=budget(),
        )


@pytest.mark.asyncio
@pytest.mark.parametrize("margin", [-1, 0, 1])
async def test_ots_adapter_enforces_utf8_response_limit_and_closes_client(monkeypatch, margin):
    from iac_code.tools.cloud.aliyun import ots_data_adapter as adapter_module

    async def resolve(value):
        return value

    monkeypatch.setattr(adapter_module, "resolve_signing_credential", resolve)
    body = {"InstanceName": "fvt-ros", "Tables": [{"TableName": "订单", "InstanceName": "fvt-ros"}]}
    size = len(json.dumps(body, separators=(",", ":"), ensure_ascii=False).encode("utf-8"))
    limited = replace(request(), response_policy=ResponseBodyPolicy("json", size + margin, ()))

    class Client:
        calls = 0
        closed = False

        async def list_table(self):
            self.calls += 1
            return ["订单"]

        async def close(self):
            self.closed = True

    client = Client()
    call = OtsDataAdapter(client_factory=lambda **_: client).execute(
        contract=contract(), request=limited, endpoint=endpoint(), credential=credential(),
        context=ToolContext(), budget=budget(),
    )
    if margin < 0:
        with pytest.raises(ResponseTooLarge, match="^response_too_large$"):
            await call
    else:
        response = await call
        assert response.body == body and response.size == size
    assert client.closed and client.calls == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("oversized", [False, True])
async def test_ots_service_errors_keep_separate_error_response_limit(monkeypatch, oversized):
    from iac_code.tools.cloud.aliyun import ots_data_adapter as adapter_module

    async def resolve(value):
        return value

    monkeypatch.setattr(adapter_module, "resolve_signing_credential", resolve)

    class ServiceError(Exception):
        def get_http_status(self):
            return 403

        def get_error_code(self):
            return "OTSAuthFailed"

        def get_error_message(self):
            return "x" * (1024 * 1024 if oversized else 100)

    class Client:
        closed = False

        async def list_table(self):
            raise ServiceError()

        async def close(self):
            self.closed = True

    client = Client()
    call = OtsDataAdapter(client_factory=lambda **_: client).execute(
        contract=contract(), request=replace(request(), response_policy=ResponseBodyPolicy("json", 1, ())),
        endpoint=endpoint(), credential=credential(), context=ToolContext(), budget=budget(),
    )
    if oversized:
        with pytest.raises(ResponseTooLarge, match="^error_response_too_large$"):
            await call
    else:
        response = await call
        assert response.status == 403 and response.body["Code"] == "OTSAuthFailed"
    assert client.closed
