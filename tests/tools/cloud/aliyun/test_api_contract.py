from __future__ import annotations

import asyncio
import copy
import json
from dataclasses import replace
from pathlib import Path
from types import MappingProxyType
from typing import Any

import httpx
import pytest

from iac_code.tools.cloud.aliyun.api_contract import (
    ApiCallShape,
    ApiContractError,
    ApiContractResolver,
    CanonicalWireContract,
    RequestBuilder,
)
from iac_code.tools.cloud.aliyun.openmeta import (
    MetadataFetch,
    OpenMetaClient,
    ParameterMetadata,
    ProductMetadata,
    SecurityRequirement,
    normalize_api_metadata,
)
from iac_code.tools.cloud.aliyun.product_resolver import ProductResolution, ProductResolver
from iac_code.tools.cloud.aliyun.public_errors import public_aliyun_error


class FakeOpenMeta:
    def __init__(
        self,
        raw: dict[str, Any] | None,
        *,
        error: str | None = None,
        product: ProductMetadata | None = None,
    ) -> None:
        self.raw = raw
        self.error = error
        self.product = product
        self.calls: list[tuple[str, ...]] = []

    async def get_product(self, product: str) -> MetadataFetch[Any]:
        self.calls.append(("product", product))
        return MetadataFetch(
            value=self.product,
            source="fresh" if self.product else None,
            error=None if self.product else "temporarily_unavailable",
        )

    async def get_api(self, product: str, version: str, action: str) -> MetadataFetch[Any]:
        self.calls.append(("api", product, version, action))
        value = normalize_api_metadata(self.raw) if self.raw is not None else None
        return MetadataFetch(value=value, source="fresh" if value else None, error=self.error)  # type: ignore[arg-type]


def raw_api(*, security: Any = None, declared: bool = True, **changes: Any) -> dict[str, Any]:
    raw: dict[str, Any] = {
        "product": "Ecs",
        "version": "2014-05-26",
        "action": "DescribeInstances",
        "style": "RPC",
        "methods": ["POST"],
        "path": "/",
        "security": security,
        "parameters": [],
    }
    if not declared:
        raw.pop("security")
    raw.update(changes)
    return raw


def shape(**changes: Any) -> ApiCallShape:
    values: dict[str, Any] = {
        "product": "Ecs",
        "version": "2014-05-26",
        "action": "DescribeInstances",
        "region_id": "cn-hangzhou",
        "explicit_overrides": (),
        "parameter_names_by_location": MappingProxyType({"query": ("RegionId",)}),
        "body_source": "none",
    }
    values.update(changes)
    return ApiCallShape(**values)


@pytest.mark.asyncio
async def test_canonical_product_drives_the_builtin_version_fallback_after_alias_resolution() -> None:
    class AliasProductResolver:
        async def resolve(self, requested_product: str) -> ProductResolution:
            return ProductResolution(
                requested_product=requested_product,
                normalized_product=requested_product,
                metadata=ProductMetadata("Ecs", None, (), None),
                strategy="builtin_alias",
                confidence="high",
                source="fresh",
                cache_status="memory_fresh",
            )

    openmeta = FakeOpenMeta(raw_api(security=[{"AK": []}]))
    resolver = ApiContractResolver(openmeta, product_resolver=AliasProductResolver())  # type: ignore[arg-type]

    contract = await resolver.resolve(
        shape(product="ElasticComputeService", version=None),
        allow_fallback=False,
    )

    assert contract.product == "Ecs"
    assert contract.version == "2014-05-26"
    assert contract.requested_product == "ElasticComputeService"
    assert contract.product_match_strategy == "builtin_alias"
    assert openmeta.calls == [("api", "Ecs", "2014-05-26", "DescribeInstances")]


@pytest.mark.asyncio
async def test_unknown_product_with_explicit_version_continues_to_exact_api_metadata() -> None:
    class UnknownProductOpenMeta:
        def __init__(self) -> None:
            self.calls: list[tuple[str, ...]] = []

        async def get_product(self, product: str) -> MetadataFetch[Any]:
            raise AssertionError("remote product metadata must not be read")

        async def list_products(self) -> MetadataFetch[Any]:
            raise AssertionError("remote product catalog must not be read")

        async def get_api(self, product: str, version: str, action: str) -> MetadataFetch[Any]:
            self.calls.append(("api", product, version, action))
            value = normalize_api_metadata(
                raw_api(
                    product="NewService",
                    version="2026-07-19",
                    action="DescribeThings",
                    security=[{"AK": []}],
                )
            )
            return MetadataFetch(value=value, source="fresh", error=None)

    openmeta = UnknownProductOpenMeta()
    contract = await ApiContractResolver(openmeta).resolve(
        shape(
            product="NewService",
            version="2026-07-19",
            action="DescribeThings",
        ),
        allow_fallback=True,
    )

    assert contract.product == "NewService"
    assert contract.version == "2026-07-19"
    assert contract.product_match_strategy == "unverified"
    assert contract.product_match_confidence == "none"
    assert openmeta.calls == [("api", "NewService", "2026-07-19", "DescribeThings")]


@pytest.mark.asyncio
async def test_unknown_product_without_version_requests_version_instead_of_scanning_products() -> None:
    openmeta = FakeOpenMeta(raw_api(security=[{"AK": []}]))

    with pytest.raises(ApiContractError, match="^invalid_or_missing_version$"):
        await ApiContractResolver(openmeta).resolve(
            shape(product="NewService", version=None, action="DescribeThings"),
            allow_fallback=True,
        )

    assert openmeta.calls == []


@pytest.mark.asyncio
async def test_unknown_product_cannot_use_explicit_fallback_when_exact_api_metadata_is_absent() -> None:
    openmeta = FakeOpenMeta(None, error="not_found")

    with pytest.raises(ApiContractError, match="^metadata_not_found$"):
        await ApiContractResolver(openmeta).resolve(
            shape(
                product="NewService",
                version="2026-07-19",
                action="DescribeThings",
            ),
            allow_fallback=True,
        )

    assert openmeta.calls == [("api", "NewService", "2026-07-19", "DescribeThings")]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("security", "declared", "auth_type", "executable", "reasons"),
    [
        (None, False, "AK", True, ()),
        ([], True, "AK", False, ("security_explicit_empty",)),
        ([{"AK": []}], True, "AK", True, ()),
        ([{"AK": []}, {"Anonymous": []}], True, "AK", True, ()),
        ([{"AK": [], "Other": []}], True, "AK", False, ("security_requires_unsupported_scheme",)),
        ([{"Anonymous": []}], True, "Anonymous", True, ()),
        ([{"Anonymous": ["scope"]}], True, "AK", False, ("security_requires_unsupported_scheme",)),
    ],
)
async def test_security_matrix(
    security: Any,
    declared: bool,
    auth_type: str,
    executable: bool,
    reasons: tuple[str, ...],
) -> None:
    contract = await ApiContractResolver(FakeOpenMeta(raw_api(security=security, declared=declared))).resolve(
        shape(), allow_fallback=False
    )
    assert contract.auth_type == auth_type
    assert contract.executable is executable
    assert contract.unsupported_reasons == reasons


@pytest.mark.asyncio
async def test_scoped_ak_is_distinctly_unsupported() -> None:
    contract = await ApiContractResolver(FakeOpenMeta(raw_api(security=[{"AK": ["scope"]}]))).resolve(
        shape(), allow_fallback=False
    )
    assert contract.auth_type == "AK"
    assert contract.executable is False
    assert contract.unsupported_reasons == ("security_scoped_ak",)


@pytest.mark.asyncio
async def test_flat_query_style_from_openmeta_is_executable_and_uses_sdk_flat_encoding() -> None:
    raw = raw_api(
        security=[{"AK": []}],
        parameters=[
            {
                "name": "Tag",
                "in": "query",
                "style": "flat",
                "schema": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "Key": {"type": "string"},
                            "Value": {"type": "string"},
                        },
                    },
                },
            }
        ],
    )

    resolved = await ApiContractResolver(FakeOpenMeta(raw)).resolve(
        shape(parameter_names_by_location=MappingProxyType({"query": ("Tag",)})),
        allow_fallback=False,
    )
    built = await RequestBuilder().build(resolved, {"params": {"Tag": [{"Key": "env", "Value": "prod"}]}})

    assert resolved.executable is True
    assert resolved.unsupported_reasons == ()
    assert dict(built.canonical_query) == {
        "Tag.1.#3#Key": "env",
        "Tag.1.#5#Value": "prod",
    }


@pytest.mark.asyncio
async def test_parameter_style_override_can_disable_incorrect_openmeta_flat_object_style(tmp_path: Path) -> None:
    overrides = tmp_path / "api_overrides.yml"
    overrides.write_text(
        """
contract_policy_version: 1
default_signature_scheme: acs3
products:
  Eci:
    versions:
      "2018-08-08":
        parameter_styles:
          DestinationResource: null
""",
        encoding="utf-8",
    )
    raw = raw_api(
        product="Eci",
        version="2018-08-08",
        action="DescribeAvailableResource",
        security=[{"AK": []}],
        parameters=[
            {
                "name": "RegionId",
                "in": "query",
                "schema": {"type": "string", "required": True},
            },
            {
                "name": "DestinationResource",
                "in": "query",
                "style": "flat",
                "schema": {
                    "type": "object",
                    "required": True,
                    "properties": {
                        "Category": {"type": "string"},
                        "Value": {"type": "string"},
                    },
                },
            },
        ],
    )

    resolved = await ApiContractResolver(FakeOpenMeta(raw), overrides_path=overrides).resolve(
        shape(
            product="Eci",
            version="2018-08-08",
            action="DescribeAvailableResource",
            parameter_names_by_location=MappingProxyType({"query": ("RegionId", "DestinationResource")}),
        ),
        allow_fallback=False,
    )
    built = await RequestBuilder().build(
        resolved,
        {
            "params": {
                "RegionId": "cn-hangzhou",
                "DestinationResource": {
                    "Category": "InstanceTypeFamily",
                    "Value": "ecs.c6",
                },
            }
        },
    )

    assert dict(built.canonical_query) == {
        "DestinationResource.Category": "InstanceTypeFamily",
        "DestinationResource.Value": "ecs.c6",
        "RegionId": "cn-hangzhou",
    }


@pytest.mark.asyncio
async def test_signature_scheme_can_be_overridden_for_one_exact_version(tmp_path: Path) -> None:
    overrides = tmp_path / "api_overrides.yml"
    overrides.write_text(
        """
contract_policy_version: 1
default_signature_scheme: acs3
products:
  fnf:
    versions:
      "2019-03-15":
        default_signature_scheme: acs1
""",
        encoding="utf-8",
    )
    legacy = await ApiContractResolver(
        FakeOpenMeta(raw_api(product="fnf", version="2019-03-15", security=[{"AK": []}])),
        overrides_path=overrides,
    ).resolve(shape(product="fnf", version="2019-03-15"), allow_fallback=False)
    modern = await ApiContractResolver(
        FakeOpenMeta(raw_api(product="fnf", version="2026-01-01", security=[{"AK": []}])),
        overrides_path=overrides,
    ).resolve(shape(product="fnf", version="2026-01-01"), allow_fallback=False)

    assert (legacy.signature_scheme, legacy.transport) == ("acs1", "acs1")
    assert (modern.signature_scheme, modern.transport) == ("acs3", "tea")


@pytest.mark.asyncio
async def test_reviewed_auth_type_override_can_reconcile_openmeta_with_official_sdk(tmp_path: Path) -> None:
    overrides = tmp_path / "api_overrides.yml"
    overrides.write_text(
        """
contract_policy_version: 1
default_signature_scheme: acs3
products:
  Searchplat:
    versions:
      "2024-05-29":
        auth_type: AK
""",
        encoding="utf-8",
    )
    resolved = await ApiContractResolver(
        FakeOpenMeta(
            raw_api(
                product="Searchplat",
                version="2024-05-29",
                action="GetMemorySkill",
                security=[{"BearerToken": []}],
            )
        ),
        overrides_path=overrides,
    ).resolve(
        shape(product="Searchplat", version="2024-05-29", action="GetMemorySkill"),
        allow_fallback=False,
    )

    assert resolved.auth_type == "AK"
    assert resolved.executable is True
    assert resolved.unsupported_reasons == ()


@pytest.mark.asyncio
async def test_invalid_auth_type_override_fails_closed(tmp_path: Path) -> None:
    overrides = tmp_path / "api_overrides.yml"
    overrides.write_text(
        """
default_signature_scheme: acs3
products:
  Searchplat:
    versions:
      "2024-05-29":
        auth_type: BearerToken
""",
        encoding="utf-8",
    )
    resolver = ApiContractResolver(
        FakeOpenMeta(raw_api(product="Searchplat", version="2024-05-29", security=[{"BearerToken": []}])),
        overrides_path=overrides,
    )

    with pytest.raises(ApiContractError, match="^invalid_api_overrides$"):
        await resolver.resolve(
            shape(product="Searchplat", version="2024-05-29"),
            allow_fallback=False,
        )


@pytest.mark.asyncio
async def test_reviewed_consumes_override_reconciles_formdata_with_official_sdk(tmp_path: Path) -> None:
    overrides = tmp_path / "api_overrides.yml"
    overrides.write_text(
        """
contract_policy_version: 1
products:
  btripOpen:
    versions:
      "2022-05-20":
        actions:
          QueryGroupCorpList:
            consumes:
              - application/x-www-form-urlencoded
""".lstrip(),
        encoding="utf-8",
    )
    raw = raw_api(
        product="btripOpen",
        version="2022-05-20",
        action="QueryGroupCorpList",
        style="ROA",
        methods=["POST"],
        path="/sub_corps/v1/corps/action/corpList",
        security=[{"AK": []}],
        consumes=["application/json"],
        produces=["application/json"],
        parameters=[{"name": "user_id", "in": "formData", "schema": {"type": "string"}}],
    )

    resolved = await ApiContractResolver(FakeOpenMeta(raw), overrides_path=overrides).resolve(
        shape(
            product="btripOpen",
            version="2022-05-20",
            action="QueryGroupCorpList",
            body_source="formdata",
            parameter_names_by_location=MappingProxyType({"formData": ("user_id",)}),
        ),
        allow_fallback=False,
    )
    built = await RequestBuilder().build(
        resolved,
        {"region_id": "cn-hangzhou", "params": {"user_id": "xx"}},
    )

    assert resolved.executable is True
    assert resolved.consumes == ("application/x-www-form-urlencoded",)
    assert built.headers["content-type"] == "application/x-www-form-urlencoded"
    assert built.body == b"user_id=xx"


@pytest.mark.asyncio
async def test_reviewed_pathname_prefix_override_is_scoped_to_exact_version(tmp_path: Path) -> None:
    overrides = tmp_path / "api_overrides.yml"
    overrides.write_text(
        """
contract_policy_version: 1
products:
  btripOpen:
    versions:
      "2022-05-20":
        pathname_prefix: /api
""".lstrip(),
        encoding="utf-8",
    )
    old_raw = raw_api(
        product="btripOpen",
        version="2022-05-20",
        action="VatInvoiceScanQuery",
        style="ROA",
        methods=["GET"],
        path="/scan/v1/vat-invoice",
        security=[{"AK": []}],
    )
    new_raw = copy.deepcopy(old_raw)
    new_raw["version"] = "2022-05-21"

    old = await ApiContractResolver(FakeOpenMeta(old_raw), overrides_path=overrides).resolve(
        shape(product="btripOpen", version="2022-05-20", action="VatInvoiceScanQuery"),
        allow_fallback=False,
    )
    new = await ApiContractResolver(FakeOpenMeta(new_raw), overrides_path=overrides).resolve(
        shape(product="btripOpen", version="2022-05-21", action="VatInvoiceScanQuery"),
        allow_fallback=False,
    )

    assert old.pathname == "/api/scan/v1/vat-invoice"
    assert new.pathname == "/scan/v1/vat-invoice"


@pytest.mark.asyncio
async def test_action_parameter_location_override_can_fix_json_formdata_contract(tmp_path: Path) -> None:
    overrides = tmp_path / "api_overrides.yml"
    overrides.write_text(
        """
contract_policy_version: 1
default_signature_scheme: acs3
products:
  AnyTrans:
    versions:
      "2025-07-07":
        actions:
          TextTranslate:
            parameter_locations:
              workspaceId: body
              sourceLanguage: body
              targetLanguage: body
              text: body
              ext: body
""",
        encoding="utf-8",
    )
    raw = raw_api(
        product="AnyTrans",
        version="2025-07-07",
        action="TextTranslate",
        style="ROA",
        methods=["POST"],
        path="/anytrans/translate/text",
        consumes=["application/json"],
        security=[{"AK": []}],
        parameters=[
            {"name": "workspaceId", "in": "formData", "required": True, "schema": {"type": "string"}},
            {"name": "sourceLanguage", "in": "formData", "required": True, "schema": {"type": "string"}},
            {"name": "targetLanguage", "in": "formData", "required": True, "schema": {"type": "string"}},
            {"name": "text", "in": "formData", "required": True, "schema": {"type": "string"}},
            {"name": "ext", "in": "formData", "schema": {"type": "object"}},
        ],
    )

    resolved = await ApiContractResolver(FakeOpenMeta(raw), overrides_path=overrides).resolve(
        shape(
            product="AnyTrans",
            version="2025-07-07",
            action="TextTranslate",
            body_source="params_body",
            parameter_names_by_location=MappingProxyType(
                {"body": ("workspaceId", "sourceLanguage", "targetLanguage", "text", "ext")}
            ),
        ),
        allow_fallback=False,
    )
    built = await RequestBuilder().build(
        resolved,
        {
            "params": {
                "workspaceId": "llm-demo",
                "sourceLanguage": "zh",
                "targetLanguage": "en",
                "text": "hello",
                "ext": {},
            }
        },
    )

    assert resolved.executable is True
    assert resolved.unsupported_reasons == ()
    assert resolved.request_body_type == "json"
    assert {parameter.name: parameter.location for parameter in resolved.parameters} == {
        "workspaceId": "body",
        "sourceLanguage": "body",
        "targetLanguage": "body",
        "text": "body",
        "ext": "body",
    }
    assert built.headers["content-type"] == "application/json"
    assert built.body == (
        b'{"workspaceId":"llm-demo","sourceLanguage":"zh","targetLanguage":"en","text":"hello","ext":{}}'
    )


@pytest.mark.asyncio
async def test_additional_parameter_override_restores_omitted_required_host_contract(tmp_path: Path) -> None:
    overrides = tmp_path / "api_overrides.yml"
    overrides.write_text(
        """
contract_policy_version: 1
default_signature_scheme: acs3
products:
  hcs-mgw:
    versions:
      "2024-06-26":
        additional_parameters:
          userid:
            location: host
            required: true
            schema:
              type: string
""".lstrip(),
        encoding="utf-8",
    )
    raw = raw_api(
        product="hcs-mgw",
        version="2024-06-26",
        action="ListJob",
        style="ROA",
        methods=["GET"],
        path="/joblist",
        security=[{"AK": []}],
        parameters=[{"name": "count", "in": "query", "schema": {"type": "integer"}}],
    )
    resolved = await ApiContractResolver(FakeOpenMeta(raw), overrides_path=overrides).resolve(
        shape(
            product="hcs-mgw",
            version="2024-06-26",
            action="ListJob",
            parameter_names_by_location=MappingProxyType({"host": ("userid",), "query": ("count",)}),
        ),
        allow_fallback=False,
    )

    with pytest.raises(ApiContractError, match="^missing_required_parameters:userid$"):
        await RequestBuilder().build(resolved, {"params": {"count": 1}})
    built = await RequestBuilder().build(resolved, {"params": {"userid": "xx", "count": 1}})

    assert [(parameter.name, parameter.location, parameter.required) for parameter in resolved.parameters] == [
        ("count", "query", False),
        ("userid", "host", True),
    ]
    assert dict(built.host_values) == {"userid": "xx"}
    assert dict(built.canonical_query) == {"count": "1"}


@pytest.mark.asyncio
async def test_additional_parameter_override_rejects_duplicate_openmeta_parameter(tmp_path: Path) -> None:
    overrides = tmp_path / "api_overrides.yml"
    overrides.write_text(
        """
contract_policy_version: 1
products:
  hcs-mgw:
    versions:
      "2024-06-26":
        additional_parameters:
          userid:
            location: host
            required: true
            schema:
              type: string
""".lstrip(),
        encoding="utf-8",
    )
    resolver = ApiContractResolver(
        FakeOpenMeta(
            raw_api(
                product="hcs-mgw",
                version="2024-06-26",
                action="ListJob",
                security=[{"AK": []}],
                parameters=[{"name": "userid", "in": "host", "schema": {"type": "string"}}],
            )
        ),
        overrides_path=overrides,
    )

    with pytest.raises(ApiContractError, match="^invalid_api_overrides$"):
        await resolver.resolve(
            shape(product="hcs-mgw", version="2024-06-26", action="ListJob"),
            allow_fallback=False,
        )


@pytest.mark.asyncio
async def test_default_anytrans_override_moves_all_json_formdata_parameters() -> None:
    raw = raw_api(
        product="AnyTrans",
        version="2025-07-07",
        action="TextTranslate",
        style="ROA",
        methods=["POST"],
        path="/anytrans/translate/text",
        consumes=["application/json"],
        security=[{"AK": []}],
        parameters=[
            {"name": "workspaceId", "in": "formData", "schema": {"type": "string", "required": True}},
            {"name": "format", "in": "formData", "schema": {"type": "string", "required": False}},
            {"name": "sourceLanguage", "in": "formData", "schema": {"type": "string", "required": True}},
            {"name": "targetLanguage", "in": "formData", "schema": {"type": "string", "required": True}},
            {"name": "text", "in": "formData", "schema": {"type": "string", "required": True}},
            {"name": "scene", "in": "formData", "schema": {"type": "string", "required": False}},
            {
                "name": "ext",
                "in": "formData",
                "schema": {"type": "object", "required": False},
                "style": "json",
            },
        ],
    )

    resolved = await ApiContractResolver(FakeOpenMeta(raw)).resolve(
        shape(
            product="AnyTrans",
            version="2025-07-07",
            action="TextTranslate",
            body_source="params_body",
            parameter_names_by_location=MappingProxyType(
                {"body": ("workspaceId", "format", "sourceLanguage", "targetLanguage", "text", "scene", "ext")}
            ),
        ),
        allow_fallback=False,
    )

    assert resolved.executable is True
    assert resolved.unsupported_reasons == ()
    assert resolved.request_body_type == "json"
    assert {parameter.name: parameter.location for parameter in resolved.parameters} == {
        "workspaceId": "body",
        "format": "body",
        "sourceLanguage": "body",
        "targetLanguage": "body",
        "text": "body",
        "scene": "body",
        "ext": "body",
    }


@pytest.mark.asyncio
async def test_default_farui_override_moves_json_formdata_parameters_but_keeps_workspace_path() -> None:
    raw = raw_api(
        product="FaRui",
        version="2024-06-28",
        action="RunSearchLawQuery",
        style="ROA",
        methods=["POST"],
        path="/{workspaceId}/farui/search/law/query",
        consumes=["application/json"],
        security=[{"AK": []}],
        parameters=[
            {"name": "workspaceId", "in": "path", "schema": {"type": "string", "required": True}},
            {"name": "appId", "in": "formData", "schema": {"type": "string", "required": False}},
            {
                "name": "thread",
                "in": "formData",
                "schema": {"type": "object", "required": False},
                "style": "json",
            },
            {"name": "query", "in": "formData", "schema": {"type": "string", "required": True}},
            {
                "name": "queryKeywords",
                "in": "formData",
                "schema": {"type": "array", "required": False, "items": {"type": "string"}},
                "style": "json",
            },
            {
                "name": "pageParam",
                "in": "formData",
                "schema": {"type": "object", "required": False},
                "style": "json",
            },
            {
                "name": "filterCondition",
                "in": "formData",
                "schema": {"type": "object", "required": False},
                "style": "json",
            },
        ],
    )

    resolved = await ApiContractResolver(FakeOpenMeta(raw)).resolve(
        shape(
            product="FaRui",
            version="2024-06-28",
            action="RunSearchLawQuery",
            body_source="params_body",
            parameter_names_by_location=MappingProxyType(
                {
                    "path": ("workspaceId",),
                    "body": (
                        "appId",
                        "thread",
                        "query",
                        "queryKeywords",
                        "pageParam",
                        "filterCondition",
                    ),
                }
            ),
        ),
        allow_fallback=False,
    )
    built = await RequestBuilder().build(
        resolved,
        {
            "params": {
                "workspaceId": "demo-workspace",
                "appId": "demo-app",
                "thread": {"id": "demo-thread"},
                "query": "合同违约责任",
                "queryKeywords": ["合同", "违约"],
                "pageParam": {"pageNumber": 1, "pageSize": 1},
                "filterCondition": {},
            }
        },
    )

    assert resolved.executable is True
    assert resolved.unsupported_reasons == ()
    assert resolved.request_body_type == "json"
    assert {parameter.name: parameter.location for parameter in resolved.parameters} == {
        "workspaceId": "path",
        "appId": "body",
        "thread": "body",
        "query": "body",
        "queryKeywords": "body",
        "pageParam": "body",
        "filterCondition": "body",
    }
    assert built.raw_path == b"/demo-workspace/farui/search/law/query"
    assert built.headers["content-type"] == "application/json"
    assert json.loads(built.body or b"{}") == {
        "appId": "demo-app",
        "thread": {"id": "demo-thread"},
        "query": "合同违约责任",
        "queryKeywords": ["合同", "违约"],
        "pageParam": {"pageNumber": 1, "pageSize": 1},
        "filterCondition": {},
    }


@pytest.mark.parametrize(
    "parameter_schema",
    [
        {"$ref": "#/components/schemas/Missing"},
        {"$ref": "https://example.test/schema"},
        "not-a-schema",
    ],
    ids=["missing-ref", "external-ref", "invalid-schema"],
)
@pytest.mark.asyncio
async def test_invalid_parameter_schemas_fail_closed_in_canonical_contract(parameter_schema: Any) -> None:
    raw = raw_api(
        security=[{"AK": []}],
        parameters=[{"name": "Broken", "in": "query", "required": True, "schema": parameter_schema}],
        components={"schemas": {}},
    )

    resolved = await ApiContractResolver(FakeOpenMeta(raw)).resolve(shape(), allow_fallback=False)

    assert resolved.executable is False
    assert resolved.unsupported_reasons == ("parameter_schema_reference_unsupported",)


@pytest.mark.parametrize(
    ("keyword", "carrier_kind"),
    [
        ("patternProperties", "map"),
        ("dependentSchemas", "map"),
        ("$defs", "map"),
        ("definitions", "map"),
        ("unevaluatedProperties", "single"),
    ],
    ids=["pattern-properties", "dependent-schemas", "defs", "definitions", "unevaluated-properties"],
)
@pytest.mark.parametrize(
    "reference",
    ["https://example.test/schema", "#/components/schemas/Missing"],
    ids=["external-ref", "missing-ref"],
)
@pytest.mark.asyncio
async def test_nested_schema_carrier_references_fail_closed_in_canonical_contract(
    keyword: str,
    carrier_kind: str,
    reference: str,
) -> None:
    nested = {"$ref": reference}
    parameter_schema = {keyword: {"nested": nested} if carrier_kind == "map" else nested}
    raw = raw_api(
        security=[{"AK": []}],
        parameters=[{"name": "Broken", "in": "query", "required": True, "schema": parameter_schema}],
        components={"schemas": {}},
    )

    metadata = normalize_api_metadata(raw)
    resolved = await ApiContractResolver(FakeOpenMeta(raw)).resolve(shape(), allow_fallback=False)

    assert metadata.document_parameters[0].schema == parameter_schema
    assert metadata.parameters[0].schema is None
    assert resolved.executable is False
    assert resolved.unsupported_reasons == ("parameter_schema_reference_unsupported",)


@pytest.mark.parametrize(
    ("keyword", "invalid_value"),
    [
        ("patternProperties", []),
        ("dependentSchemas", {"nested": "not-a-schema"}),
        ("$defs", []),
        ("definitions", {"nested": "not-a-schema"}),
        ("unevaluatedProperties", "not-a-schema"),
    ],
    ids=["pattern-properties", "dependent-schemas", "defs", "definitions", "unevaluated-properties"],
)
@pytest.mark.asyncio
async def test_schema_carrier_keyword_types_fail_closed_in_canonical_contract(
    keyword: str,
    invalid_value: Any,
) -> None:
    raw = raw_api(
        security=[{"AK": []}],
        parameters=[{"name": "Broken", "in": "query", "required": True, "schema": {keyword: invalid_value}}],
    )

    resolved = await ApiContractResolver(FakeOpenMeta(raw)).resolve(shape(), allow_fallback=False)

    assert resolved.executable is False
    assert resolved.unsupported_reasons == ("parameter_schema_reference_unsupported",)


@pytest.mark.asyncio
async def test_schema_carrier_depth_limit_fails_closed_in_canonical_contract() -> None:
    parameter_schema: dict[str, Any] = {"type": "string"}
    for _ in range(33):
        parameter_schema = {"dependentSchemas": {"nested": parameter_schema}}
    raw = raw_api(
        security=[{"AK": []}],
        parameters=[{"name": "Deep", "in": "query", "required": True, "schema": parameter_schema}],
    )

    resolved = await ApiContractResolver(FakeOpenMeta(raw)).resolve(shape(), allow_fallback=False)

    assert resolved.executable is False
    assert resolved.unsupported_reasons == ("parameter_schema_reference_unsupported",)


@pytest.mark.asyncio
async def test_boolean_schema_carrier_remains_executable() -> None:
    raw = raw_api(
        security=[{"AK": []}],
        parameters=[
            {
                "name": "Filter",
                "in": "query",
                "schema": {"type": "object", "unevaluatedProperties": False},
            }
        ],
    )

    resolved = await ApiContractResolver(FakeOpenMeta(raw)).resolve(shape(), allow_fallback=False)

    assert resolved.executable is True
    assert resolved.unsupported_reasons == ()
    assert resolved.parameters[0].schema == {"type": "object", "unevaluatedProperties": False}


@pytest.mark.asyncio
async def test_external_response_reference_is_non_executable_without_fetching_the_reference() -> None:
    openmeta = FakeOpenMeta(
        raw_api(
            security=[{"AK": []}],
            responses={"200": {"schema": {"$ref": "https://example.test/secret-schema.json"}}},
        )
    )

    resolved = await ApiContractResolver(openmeta).resolve(shape(), allow_fallback=False)

    assert resolved.executable is False
    assert resolved.unsupported_reasons == ("response_schema_reference_unsupported",)
    assert openmeta.calls == [("api", "Ecs", "2014-05-26", "DescribeInstances")]


@pytest.mark.asyncio
async def test_recursive_parameter_schema_ref_preserves_document_and_stops_expansion_at_cycle() -> None:
    reference = "#/components/schemas/Node"
    raw = raw_api(
        security=[{"AK": []}],
        parameters=[{"name": "Node", "in": "query", "schema": {"$ref": reference}}],
        components={
            "schemas": {
                "Node": {
                    "type": "object",
                    "properties": {"child": {"$ref": reference}},
                }
            }
        },
    )

    resolved = await ApiContractResolver(FakeOpenMeta(raw)).resolve(shape(), allow_fallback=False)

    metadata = normalize_api_metadata(raw)
    assert metadata.document_parameters[0].schema == {"$ref": reference}
    assert resolved.executable is True
    assert resolved.unsupported_reasons == ()
    assert resolved.parameters[0].schema == {
        "type": "object",
        "properties": {"child": {"$ref": reference}},
    }
    built = await RequestBuilder().build(resolved, {"params": {"Node": {}}})
    assert built.canonical_query == ()


@pytest.mark.asyncio
async def test_security_digest_changes_when_local_ref_sibling_constraint_changes() -> None:
    reference = "#/components/schemas/Name"

    def api_with_max_length(max_length: int) -> dict[str, Any]:
        return raw_api(
            security=[{"AK": []}],
            parameters=[
                {
                    "name": "Name",
                    "in": "query",
                    "schema": {"$ref": reference, "allOf": [{"maxLength": max_length}]},
                }
            ],
            components={"schemas": {"Name": {"type": "string", "minLength": 1}}},
        )

    shorter = await ApiContractResolver(FakeOpenMeta(api_with_max_length(32))).resolve(shape(), allow_fallback=False)
    longer = await ApiContractResolver(FakeOpenMeta(api_with_max_length(64))).resolve(shape(), allow_fallback=False)

    assert shorter.executable is True
    assert longer.executable is True
    assert shorter.parameters[0].schema != longer.parameters[0].schema
    assert shorter.security_digest(shape()) != longer.security_digest(shape())


@pytest.mark.asyncio
async def test_object_body_is_non_executable_when_consumes_parameter_only_mentions_json() -> None:
    raw = raw_api(
        security=[{"AK": []}],
        consumes=["text/plain; note=json"],
        parameters=[
            {
                "name": "Payload",
                "in": "body",
                "required": True,
                "schema": {"type": "object", "properties": {"name": {"type": "string"}}},
            }
        ],
    )

    resolved = await ApiContractResolver(FakeOpenMeta(raw)).resolve(
        shape(body_source="body", parameter_names_by_location=MappingProxyType({"body": ("Payload",)})),
        allow_fallback=False,
    )

    assert resolved.request_body_type == "json"
    assert resolved.executable is False
    assert resolved.unsupported_reasons == ("request_media_type_unsupported",)


@pytest.mark.asyncio
async def test_legal_null_schema_values_remain_executable() -> None:
    parameter_schema = {
        "type": "object",
        "properties": {"state": {"type": "string", "default": None, "enum": [None, "ready"]}},
    }
    raw = raw_api(
        security=[{"AK": []}],
        parameters=[{"name": "Filter", "in": "query", "schema": parameter_schema}],
    )

    resolved = await ApiContractResolver(FakeOpenMeta(raw)).resolve(shape(), allow_fallback=False)

    assert resolved.executable is True
    assert resolved.unsupported_reasons == ()
    normalized = resolved.parameters[0].schema
    assert normalized is not None
    state_property = normalized["properties"]["state"]
    assert state_property["default"] is None
    assert state_property["enum"] == (None, "ready")


@pytest.mark.asyncio
async def test_current_official_parameter_shape_enforces_schema_nested_required_flag() -> None:
    raw = raw_api(
        security=[{"AK": []}],
        parameters=[
            {
                "name": "RegionId",
                "in": "query",
                "schema": {
                    "type": "string",
                    "required": True,
                    "description": "The region.",
                    "example": "cn-hangzhou",
                },
            }
        ],
    )
    resolved = await ApiContractResolver(FakeOpenMeta(raw)).resolve(shape(), allow_fallback=False)

    assert resolved.parameters[0].required is True
    with pytest.raises(ApiContractError, match="^missing_required_parameters:RegionId$"):
        await RequestBuilder().build(resolved, {"params": {}})


@pytest.mark.asyncio
async def test_current_official_parameter_shape_enforces_schema_doc_required_flag() -> None:
    raw = raw_api(
        security=[{"AK": []}],
        parameters=[
            {
                "name": "BusinessUnitId",
                "in": "formData",
                "schema": {
                    "type": "string",
                    "required": False,
                    "docRequired": True,
                    "description": "The business space.",
                    "example": "llm-demo",
                },
            }
        ],
    )
    resolved = await ApiContractResolver(FakeOpenMeta(raw)).resolve(shape(), allow_fallback=False)

    assert resolved.parameters[0].required is True
    with pytest.raises(ApiContractError, match="^missing_required_parameters:BusinessUnitId$"):
        await RequestBuilder().build(resolved, {"params": {}})


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "invalid_call",
    [
        shape(region_id="cn_hangzhou"),
        shape(pathname="https://evil.test/x", explicit_overrides=("pathname",)),
    ],
)
async def test_invalid_region_and_explicit_path_fail_before_openmeta(invalid_call: ApiCallShape) -> None:
    openmeta = FakeOpenMeta(raw_api(security=[{"AK": []}]))
    with pytest.raises(ApiContractError, match="invalid_region_id|invalid_pathname"):
        await ApiContractResolver(openmeta).resolve(invalid_call, allow_fallback=False)
    assert openmeta.calls == []


def test_unknown_signature_and_transport_fail_closed_before_network(tmp_path: Path) -> None:
    overrides = tmp_path / "overrides.yml"
    overrides.write_text("default_signature_scheme: unknown\nproducts: {}\n", encoding="utf-8")
    openmeta = FakeOpenMeta(raw_api(security=[{"AK": []}]))
    with pytest.raises(ApiContractError, match="unsupported_signature_scheme"):
        ApiContractResolver(openmeta, overrides_path=overrides)
    assert openmeta.calls == []
    with pytest.raises(ApiContractError, match="unsupported_transport"):
        contract(transport="unknown")


@pytest.mark.asyncio
async def test_security_digest_covers_contract_and_shape_but_not_business_values() -> None:
    contract = await ApiContractResolver(FakeOpenMeta(raw_api(security=[{"AK": []}]))).resolve(
        shape(), allow_fallback=False
    )
    base_digest = contract.security_digest(shape())
    mutations = {
        "metadata_source": replace(contract, metadata_source="cache"),
        "product": replace(contract, product="ROS"),
        "version": replace(contract, version="2019-09-10"),
        "action": replace(contract, action="CreateStack"),
        "style": replace(contract, style="ROA"),
        "method": replace(contract, method="GET"),
        "pathname": replace(contract, pathname="/changed"),
        "operation_type": replace(contract, operation_type="write"),
        "auth_type": replace(contract, auth_type="unsupported"),
        "signature_scheme": replace(contract, signature_scheme="oss_v4"),
        "transport": replace(contract, transport="acs3_streaming"),
        "executable": replace(contract, executable=False),
        "unsupported_reasons": replace(contract, unsupported_reasons=("changed",)),
        "parameters": replace(contract, parameters=(parameter("Changed", "query"),)),
        "consumes": replace(contract, consumes=("application/json",)),
        "produces": replace(contract, produces=("text/plain",)),
        "policy_digest": replace(contract, policy_digest="changed"),
        "protocol": replace(contract, protocol="HTTP"),
        "request_body_type": replace(contract, request_body_type="json"),
        "response_body_type": replace(contract, response_body_type="binary"),
        "security_declared": replace(contract, security_declared=False),
        "security_requirements": replace(
            contract, security_requirements=(SecurityRequirement(("AK",), (("scope",),)),)
        ),
        "header_policy_version": replace(contract, header_policy_version="headers-v2"),
        "host_policy_version": replace(contract, host_policy_version="hosts-v2"),
        "endpoint_policy_digest": replace(contract, endpoint_policy_digest="endpoint-policy-v2"),
        "catalog_schema_version": replace(contract, catalog_schema_version=2),
        "catalog_source_commit": replace(contract, catalog_source_commit="changed"),
    }
    for field_name, changed_contract in mutations.items():
        assert changed_contract.security_digest(shape()) != base_digest, field_name
    assert contract.security_digest(shape().with_business_value("changed")) == base_digest
    shape_mutations = {
        "product": replace(shape(), product="ROS"),
        "version": replace(shape(), version="2019-09-10"),
        "action": replace(shape(), action="CreateStack"),
        "region_id": replace(shape(), region_id="cn-shanghai"),
        "endpoint": replace(shape(), endpoint="ecs.cn-shanghai.aliyuncs.com"),
        "content_type": replace(shape(), content_type="application/json"),
        "max_response_bytes": replace(shape(), max_response_bytes=2048),
        "explicit_overrides": replace(shape(), explicit_overrides=("method",)),
        "body_source": replace(shape(), body_source="body"),
        "style": replace(shape(), style="ROA"),
        "method": replace(shape(), method="GET"),
        "pathname": replace(shape(), pathname="/changed"),
        "location_occupancy": replace(shape(), parameter_names_by_location=MappingProxyType({"host": ("bucket",)})),
    }
    for field_name, changed_shape in shape_mutations.items():
        assert contract.security_digest(changed_shape) != base_digest, field_name


@pytest.mark.asyncio
async def test_security_digest_changes_when_endpoint_overrides_change(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import iac_code.tools.cloud.aliyun.api_contract as api_contract_module

    baseline = await ApiContractResolver(FakeOpenMeta(raw_api(security=[{"AK": []}]))).resolve(
        shape(), allow_fallback=False
    )
    changed_overrides = tmp_path / "endpoint_overrides.yml"
    changed_overrides.write_text("trusted_endpoint_suffixes: [changed.example]\nproducts: {}\n", encoding="utf-8")
    monkeypatch.setattr(api_contract_module, "_ENDPOINT_OVERRIDES_PATH", changed_overrides, raising=False)

    changed = await ApiContractResolver(FakeOpenMeta(raw_api(security=[{"AK": []}]))).resolve(
        shape(), allow_fallback=False
    )

    assert changed.endpoint_policy_digest != baseline.endpoint_policy_digest
    assert changed.security_digest(shape()) != baseline.security_digest(shape())


@pytest.mark.asyncio
async def test_explicit_fallback_requires_exact_rpc_or_roa_shape_and_can_be_disabled() -> None:
    missing_rpc = FakeOpenMeta(
        None,
        error="temporarily_unavailable",
        product=ProductMetadata("Ecs", "2014-05-26", ("2014-05-26",), None),
    )
    rpc = await ApiContractResolver(missing_rpc).resolve(shape(), allow_fallback=True)
    assert rpc.metadata_source == "explicit_fallback"

    roa_shape = shape(
        product="FC",
        action="GetFunction",
        explicit_overrides=("style", "method", "pathname"),
        style="ROA",
        method="GET",
        pathname="/2023-03-30/functions/{functionName}",
    )
    missing_roa = FakeOpenMeta(
        None,
        error="temporarily_unavailable",
        product=ProductMetadata("FC", "2023-03-30", ("2023-03-30",), None, style="ROA"),
    )
    assert (await ApiContractResolver(missing_roa).resolve(roa_shape, allow_fallback=True)).style == "ROA"

    with pytest.raises(ApiContractError, match="metadata_unavailable"):
        await ApiContractResolver(missing_rpc).resolve(shape(style="ROA"), allow_fallback=True)
    with pytest.raises(ApiContractError, match="metadata_unavailable"):
        await ApiContractResolver(missing_rpc).resolve(shape(), allow_fallback=False)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("metadata_error", "resolver_error"),
    [
        ("not_found", "metadata_not_found"),
        ("temporarily_unavailable", "metadata_unavailable"),
    ],
)
async def test_resolver_preserves_not_found_and_temporary_metadata_errors(
    metadata_error: str,
    resolver_error: str,
) -> None:
    missing = FakeOpenMeta(None, error=metadata_error)

    with pytest.raises(ApiContractError, match=f"^{resolver_error}$"):
        await ApiContractResolver(missing).resolve(shape(), allow_fallback=False)


@pytest.mark.asyncio
@pytest.mark.parametrize("error", ["protocol_error", None, "unknown_error"])
@pytest.mark.parametrize("action", ["DescribeInstances", "CreateInstance"])
async def test_protocol_and_unknown_metadata_errors_never_enter_explicit_fallback(
    error: str | None,
    action: str,
) -> None:
    missing = FakeOpenMeta(None, error=error)

    with pytest.raises(ApiContractError, match="metadata_protocol_error"):
        await ApiContractResolver(missing).resolve(
            shape(action=action),
            allow_fallback=True,
        )


@pytest.mark.asyncio
async def test_explicit_version_uses_api_metadata_without_reading_remote_product_metadata() -> None:
    class ProductErrorOpenMeta:
        def __init__(self) -> None:
            self.calls: list[tuple[str, ...]] = []

        async def get_product(self, product: str) -> MetadataFetch[Any]:
            raise AssertionError("remote product metadata must not be read")

        async def list_products(self) -> MetadataFetch[Any]:
            raise AssertionError("remote product catalog must not be read")

        async def get_api(self, product: str, version: str, action: str) -> MetadataFetch[Any]:
            self.calls.append(("api", product, version, action))
            value = normalize_api_metadata(raw_api(security=[{"AK": []}]))
            return MetadataFetch(value=value, source="fresh", error=None)

    openmeta = ProductErrorOpenMeta()

    contract = await ApiContractResolver(openmeta).resolve(shape(version="2014-05-26"), allow_fallback=True)

    assert contract.product == "Ecs"
    assert contract.version == "2014-05-26"
    assert contract.action == "DescribeInstances"
    assert openmeta.calls == [("api", "Ecs", "2014-05-26", "DescribeInstances")]


@pytest.mark.asyncio
@pytest.mark.parametrize("api_error", ["temporarily_unavailable", "not_found"])
@pytest.mark.parametrize("explicit_version", [True, False], ids=["caller-version", "version-map"])
async def test_offline_product_catalog_does_not_mask_api_metadata_errors_when_fallback_is_disabled(
    api_error: str,
    explicit_version: bool,
) -> None:
    class ProductErrorOpenMeta:
        def __init__(self) -> None:
            self.calls: list[tuple[str, ...]] = []

        async def get_product(self, product: str) -> MetadataFetch[Any]:
            raise AssertionError("remote product metadata must not be read")

        async def list_products(self) -> MetadataFetch[Any]:
            raise AssertionError("remote product catalog must not be read")

        async def get_api(self, product: str, version: str, action: str) -> MetadataFetch[Any]:
            self.calls.append(("api", product, version, action))
            return MetadataFetch(value=None, source=None, error=api_error)  # type: ignore[arg-type]

    openmeta = ProductErrorOpenMeta()
    call = shape(version="2014-05-26" if explicit_version else None)

    expected_error = "metadata_not_found" if api_error == "not_found" else "metadata_unavailable"
    with pytest.raises(ApiContractError, match=f"^{expected_error}$"):
        await ApiContractResolver(openmeta).resolve(call, allow_fallback=False)

    assert openmeta.calls == [("api", "Ecs", "2014-05-26", "DescribeInstances")]


@pytest.mark.asyncio
async def test_offline_default_matching_controlled_version_map_can_authorize_fallback() -> None:
    missing = FakeOpenMeta(None, error="temporarily_unavailable")
    defaulted = await ApiContractResolver(missing).resolve(shape(version=None), allow_fallback=True)
    assert defaulted.metadata_source == "explicit_fallback"

    explicit = await ApiContractResolver(missing).resolve(shape(), allow_fallback=True)
    assert explicit.metadata_source == "explicit_fallback"


@pytest.mark.asyncio
async def test_offline_default_version_is_used_for_available_metadata() -> None:
    openmeta = FakeOpenMeta(raw_api(security=[{"AK": []}]))
    resolved = await ApiContractResolver(openmeta).resolve(shape(version=None), allow_fallback=False)
    assert resolved.version == "2014-05-26"
    assert ("api", "Ecs", "2014-05-26", "DescribeInstances") in openmeta.calls


class CandidateOpenMeta:
    def __init__(self, product: ProductMetadata, apis: dict[str, dict[str, Any]]) -> None:
        self.product = product
        self.apis = apis
        self.calls: list[tuple[str, ...]] = []

    async def get_product(self, product: str) -> MetadataFetch[Any]:
        self.calls.append(("product", product))
        return MetadataFetch(value=self.product, source="fresh", error=None)

    async def get_api(self, product: str, version: str, action: str) -> MetadataFetch[Any]:
        self.calls.append(("api", product, version, action))
        raw = self.apis.get(version)
        value = normalize_api_metadata(raw) if raw is not None else None
        return MetadataFetch(value=value, source="fresh" if value else None, error=None if value else "not_found")

    async def get_api_for_version_selection(
        self,
        product: str,
        version: str,
        action: str,
    ) -> MetadataFetch[Any]:
        self.calls.append(("excluded_api", product, version, action))
        raw = self.apis.get(version)
        value = normalize_api_metadata(raw) if raw is not None else None
        return MetadataFetch(value=value, source="fresh" if value else None, error=None if value else "not_found")


def candidate_resolver(openmeta: CandidateOpenMeta) -> ApiContractResolver:
    product_resolver = ProductResolver(openmeta, aliases_path=None, catalog=(openmeta.product,))
    return ApiContractResolver(openmeta, product_resolver=product_resolver)


@pytest.mark.asyncio
async def test_resolver_prefetches_selected_version_then_all_candidates_for_a_hot_product() -> None:
    product = ProductMetadata(
        "Example",
        "2025-01-01",
        ("2025-01-01", "2024-01-01"),
        None,
        ("2025-01-01",),
    )

    class PrefetchingOpenMeta(CandidateOpenMeta):
        def __init__(self) -> None:
            super().__init__(product, {})
            self.prefetches: list[tuple[str, tuple[str, ...]]] = []

        async def get_api(self, product: str, version: str, action: str) -> MetadataFetch[Any]:
            self.calls.append(("api", product, version, action))
            if version != "2025-01-01":
                return MetadataFetch(value=None, source=None, error="not_found")
            value = normalize_api_metadata(
                raw_api(
                    product=product,
                    version=version,
                    action=action,
                    security=[{"AK": []}],
                )
            )
            return MetadataFetch(value=value, source="fresh", error=None)

        def prefetch_api_docs(self, product: str, versions: tuple[str, ...]) -> None:
            self.prefetches.append((product, versions))

    openmeta = PrefetchingOpenMeta()
    resolver = candidate_resolver(openmeta)

    await resolver.resolve(
        shape(product="Example", version=None, action="DescribeExamples"),
        allow_fallback=False,
    )
    await resolver.resolve(
        shape(product="Example", version=None, action="ListExamples"),
        allow_fallback=False,
    )

    assert openmeta.prefetches == [
        ("Example", ("2025-01-01",)),
        ("Example", ("2025-01-01", "2024-01-01")),
    ]


@pytest.mark.asyncio
async def test_resolver_prefetches_the_version_selected_by_explicit_fallback() -> None:
    product = ProductMetadata("Example", "2025-01-01", ("2025-01-01",), None)

    class PrefetchingMissingOpenMeta(CandidateOpenMeta):
        def __init__(self) -> None:
            super().__init__(product, {})
            self.prefetches: list[tuple[str, tuple[str, ...]]] = []

        def prefetch_api_docs(self, product: str, versions: tuple[str, ...]) -> None:
            self.prefetches.append((product, versions))

    openmeta = PrefetchingMissingOpenMeta()
    resolver = candidate_resolver(openmeta)

    contract = await resolver.resolve(
        shape(product="Example", version="2025-01-01", action="DescribeExamples"),
        allow_fallback=True,
    )

    assert contract.metadata_source == "explicit_fallback"
    assert openmeta.prefetches == [("Example", ("2025-01-01",))]


@pytest.mark.asyncio
async def test_resolver_second_action_uses_the_prefetched_version_document_end_to_end(tmp_path: Path) -> None:
    product = ProductMetadata("Example", "2025-01-01", ("2025-01-01",), None, style="RPC")
    first_api = raw_api(
        product="Example",
        version="2025-01-01",
        action="DescribeExamples",
        security=[{"AK": []}],
    )
    second_api = raw_api(
        product="Example",
        version="2025-01-01",
        action="ListExamples",
        security=[{"AK": []}],
    )
    api_docs = {
        "info": {"style": "RPC", "product": "Example", "version": "2025-01-01"},
        "components": {"schemas": {}},
        "apis": {
            "DescribeExamples": first_api,
            "ListExamples": second_api,
        },
    }
    requests: list[str] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request.url.path)
        if request.url.path.endswith("/apis/DescribeExamples/api.json"):
            return httpx.Response(200, json=first_api)
        if request.url.path.endswith("/api-docs.json"):
            return httpx.Response(200, json=api_docs)
        raise AssertionError(f"unexpected OpenMeta request: {request.url.path}")

    openmeta = OpenMetaClient(
        cache_dir=tmp_path,
        transport=httpx.MockTransport(handler),
        exclusions_path=None,
    )
    product_resolver = ProductResolver(openmeta, aliases_path=None, catalog=(product,))
    resolver = ApiContractResolver(openmeta, product_resolver=product_resolver)
    try:
        first = await resolver.resolve(
            shape(product="Example", version=None, action="DescribeExamples"),
            allow_fallback=False,
        )
        await asyncio.gather(*tuple(openmeta._prefetch_tasks.values()))
        second = await resolver.resolve(
            shape(product="Example", version=None, action="ListExamples"),
            allow_fallback=False,
        )

        assert first.action == "DescribeExamples"
        assert second.action == "ListExamples"
        assert second.openmeta_cache_status == "memory_fresh"
        assert requests == [
            "/meta/v1/products/Example/versions/2025-01-01/apis/DescribeExamples/api.json",
            "/meta/v1/products/Example/versions/2025-01-01/api-docs.json",
        ]
    finally:
        await openmeta.aclose()


@pytest.mark.asyncio
@pytest.mark.parametrize("version", ["20240611", "iap_1.0"])
async def test_default_version_selection_accepts_safe_opaque_official_versions(version: str) -> None:
    product = ProductMetadata("Example", version, (version,), None, (version,))
    openmeta = CandidateOpenMeta(
        product,
        {
            version: raw_api(
                product="Example",
                version=version,
                security=[{"AK": []}],
            )
        },
    )

    contract = await candidate_resolver(openmeta).resolve(
        shape(product="Example", version=None),
        allow_fallback=False,
    )

    assert contract.version == version
    assert ("api", "Example", version, "DescribeInstances") in openmeta.calls


@pytest.mark.asyncio
async def test_missing_version_checks_normal_then_first_then_second_excluded_versions_by_action() -> None:
    product = ProductMetadata(
        "Example",
        "2025-01-01",
        ("2025-01-01", "2024-01-01"),
        None,
        ("2025-01-01",),
        first_class_excluded_versions=("2023-01-01",),
        second_class_excluded_versions=("2022-01-01",),
    )
    openmeta = CandidateOpenMeta(
        product,
        {
            "2022-01-01": raw_api(
                product="Example",
                version="2022-01-01",
                action="DescribeExamples",
                security=[{"AK": []}],
            )
        },
    )

    resolved = await candidate_resolver(openmeta).resolve(
        shape(product="Example", version=None, action="DescribeExamples"),
        allow_fallback=True,
    )

    assert resolved.version == "2022-01-01"
    assert openmeta.calls == [
        ("api", "Example", "2025-01-01", "DescribeExamples"),
        ("api", "Example", "2024-01-01", "DescribeExamples"),
        ("excluded_api", "Example", "2023-01-01", "DescribeExamples"),
        ("excluded_api", "Example", "2022-01-01", "DescribeExamples"),
    ]


@pytest.mark.asyncio
async def test_first_class_excluded_version_wins_before_second_class() -> None:
    product = ProductMetadata(
        "Example",
        None,
        (),
        None,
        first_class_excluded_versions=("2024-01-01",),
        second_class_excluded_versions=("2023-01-01",),
    )
    openmeta = CandidateOpenMeta(
        product,
        {
            version: raw_api(
                product="Example",
                version=version,
                action="DescribeExamples",
                security=[{"AK": []}],
            )
            for version in ("2024-01-01", "2023-01-01")
        },
    )

    resolved = await candidate_resolver(openmeta).resolve(
        shape(product="Example", version=None, action="DescribeExamples"),
        allow_fallback=True,
    )

    assert resolved.version == "2024-01-01"
    assert openmeta.calls == [
        ("excluded_api", "Example", "2024-01-01", "DescribeExamples"),
    ]


@pytest.mark.asyncio
async def test_controlled_version_map_runs_after_both_excluded_version_classes() -> None:
    product = ProductMetadata(
        "IaCService",
        None,
        (),
        None,
        first_class_excluded_versions=("2021-07-22",),
        second_class_excluded_versions=("2021-06-01",),
    )
    openmeta = CandidateOpenMeta(
        product,
        {
            "2021-08-06": raw_api(
                product="IaCService",
                version="2021-08-06",
                action="GetResource",
                security=[{"AK": []}],
            )
        },
    )

    resolved = await candidate_resolver(openmeta).resolve(
        shape(product="IaCService", version=None, action="GetResource"),
        allow_fallback=True,
    )

    assert resolved.version == "2021-08-06"
    assert openmeta.calls == [
        ("excluded_api", "IaCService", "2021-07-22", "GetResource"),
        ("excluded_api", "IaCService", "2021-06-01", "GetResource"),
        ("api", "IaCService", "2021-08-06", "GetResource"),
    ]


@pytest.mark.asyncio
async def test_controlled_version_map_can_use_explicit_fallback_after_all_metadata_candidates_miss() -> None:
    product = ProductMetadata(
        "IaCService",
        None,
        (),
        None,
        first_class_excluded_versions=("2021-07-22",),
        second_class_excluded_versions=("2021-06-01",),
    )
    openmeta = CandidateOpenMeta(product, {})

    resolved = await candidate_resolver(openmeta).resolve(
        shape(product="IaCService", version=None, action="GetResource"),
        allow_fallback=True,
    )

    assert resolved.version == "2021-08-06"
    assert resolved.metadata_source == "explicit_fallback"
    assert openmeta.calls == [
        ("excluded_api", "IaCService", "2021-07-22", "GetResource"),
        ("excluded_api", "IaCService", "2021-06-01", "GetResource"),
        ("api", "IaCService", "2021-08-06", "GetResource"),
    ]


@pytest.mark.asyncio
async def test_duplicate_version_map_keeps_final_fallback_without_refetching_the_action() -> None:
    product = ProductMetadata("Ecs", "2014-05-26", ("2014-05-26",), None)
    openmeta = CandidateOpenMeta(product, {})

    resolved = await candidate_resolver(openmeta).resolve(shape(version=None), allow_fallback=True)

    assert resolved.version == "2014-05-26"
    assert resolved.metadata_source == "explicit_fallback"
    assert openmeta.calls == [("api", "Ecs", "2014-05-26", "DescribeInstances")]


@pytest.mark.asyncio
async def test_product_exclusion_is_a_hard_boundary_before_version_fallback() -> None:
    product = ProductMetadata("Ecs", "2014-05-26", ("2014-05-26",), None)

    class ProductExcludedOpenMeta(CandidateOpenMeta):
        def is_product_excluded(self, product: str) -> bool:
            return True

    openmeta = ProductExcludedOpenMeta(product, {})

    with pytest.raises(ApiContractError, match="^product_not_found$"):
        await candidate_resolver(openmeta).resolve(shape(version=None), allow_fallback=True)

    assert openmeta.calls == []


@pytest.mark.asyncio
@pytest.mark.parametrize("version", [None, "2014-05-26"])
async def test_api_exclusion_is_a_hard_boundary_for_both_version_map_and_explicit_fallback(
    version: str | None,
) -> None:
    product = ProductMetadata("Ecs", "2014-05-26", ("2014-05-26",), None)

    class ApiExcludedOpenMeta(CandidateOpenMeta):
        def is_api_excluded(self, product: str, version: str, action: str) -> bool:
            return (product, version, action) == ("Ecs", "2014-05-26", "DescribeInstances")

    openmeta = ApiExcludedOpenMeta(product, {})

    with pytest.raises(ApiContractError, match="^metadata_not_found$"):
        await candidate_resolver(openmeta).resolve(shape(version=version), allow_fallback=True)

    assert openmeta.calls == [
        (
            "api" if version is None else "excluded_api",
            "Ecs",
            "2014-05-26",
            "DescribeInstances",
        ),
    ]


@pytest.mark.asyncio
async def test_temporary_failure_on_a_newer_version_stops_before_excluded_candidates() -> None:
    product = ProductMetadata(
        "Example",
        "2025-01-01",
        ("2025-01-01",),
        None,
        first_class_excluded_versions=("2024-01-01",),
    )

    class TemporaryFailureOpenMeta(CandidateOpenMeta):
        async def get_api(self, product: str, version: str, action: str) -> MetadataFetch[Any]:
            self.calls.append(("api", product, version, action))
            return MetadataFetch(value=None, source=None, error="temporarily_unavailable")

    openmeta = TemporaryFailureOpenMeta(product, {})

    with pytest.raises(ApiContractError, match="^metadata_unavailable$"):
        await candidate_resolver(openmeta).resolve(
            shape(product="Example", version=None, action="DescribeExamples"),
            allow_fallback=True,
        )

    assert openmeta.calls == [
        ("api", "Example", "2025-01-01", "DescribeExamples"),
    ]


@pytest.mark.asyncio
async def test_explicit_excluded_version_is_resolved_exactly_without_version_switching() -> None:
    product = ProductMetadata(
        "Example",
        "2025-01-01",
        ("2025-01-01",),
        None,
        first_class_excluded_versions=("2024-01-01",),
    )
    openmeta = CandidateOpenMeta(
        product,
        {
            "2024-01-01": raw_api(
                product="Example",
                version="2024-01-01",
                action="DescribeExamples",
                security=[{"AK": []}],
            )
        },
    )

    resolved = await candidate_resolver(openmeta).resolve(
        shape(product="Example", version="2024-01-01", action="DescribeExamples"),
        allow_fallback=False,
    )

    assert resolved.version == "2024-01-01"
    assert openmeta.calls == [
        ("excluded_api", "Example", "2024-01-01", "DescribeExamples"),
    ]


@pytest.mark.asyncio
async def test_missing_version_reports_metadata_not_found_after_all_version_candidates() -> None:
    product = ProductMetadata(
        "Example",
        None,
        (),
        None,
        first_class_excluded_versions=("2024-01-01",),
        second_class_excluded_versions=("2023-01-01",),
    )
    openmeta = CandidateOpenMeta(product, {})

    with pytest.raises(ApiContractError, match="^metadata_not_found$"):
        await candidate_resolver(openmeta).resolve(
            shape(product="Example", version=None, action="DescribeExamples"),
            allow_fallback=True,
        )

    assert openmeta.calls == [
        ("excluded_api", "Example", "2024-01-01", "DescribeExamples"),
        ("excluded_api", "Example", "2023-01-01", "DescribeExamples"),
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("raw", "error", "cache_status", "expected_source"),
    [
        (raw_api(security=[{"AK": []}]), None, "memory_fresh", "fresh"),
        (None, "not_found", "negative_hit", "explicit_fallback"),
    ],
)
async def test_resolver_carries_cache_status_separately_without_changing_security_digest(
    raw, error, cache_status, expected_source
) -> None:
    class CacheAwareOpenMeta(FakeOpenMeta):
        async def get_api(self, product: str, version: str, action: str) -> MetadataFetch[Any]:
            self.calls.append(("api", product, version, action))
            value = normalize_api_metadata(self.raw) if self.raw is not None else None
            return MetadataFetch(
                value=value,
                source="fresh" if value else None,
                error=self.error,  # type: ignore[arg-type]
                cache_status=cache_status,
            )

    product = ProductMetadata("Ecs", "2014-05-26", ("2014-05-26",), None)
    contract = await ApiContractResolver(CacheAwareOpenMeta(raw, error=error, product=product)).resolve(
        shape(), allow_fallback=True
    )

    assert contract.metadata_source == expected_source
    assert contract.openmeta_cache_status == cache_status
    other_status = "remote" if cache_status != "remote" else "memory_fresh"
    changed = replace(contract, openmeta_cache_status=other_status)
    assert changed.security_digest(shape()) == contract.security_digest(shape())


@pytest.mark.asyncio
async def test_controlled_version_map_can_authorize_fallback() -> None:
    resolved = await ApiContractResolver(FakeOpenMeta(None, error="temporarily_unavailable")).resolve(
        shape(version=None), allow_fallback=True
    )
    assert (resolved.version, resolved.metadata_source) == ("2014-05-26", "explicit_fallback")


@pytest.mark.asyncio
async def test_normalized_media_controls_body_policy_accept_and_transport() -> None:
    fixture = Path(__file__).parent / "fixtures/openmeta/media_modes.json"
    documents = json.loads(fixture.read_text(encoding="utf-8"))["apis"]
    expected = {
        "XmlUpload": ("byte", "string", "application/xml", "acs3_streaming", "xml"),
        "TextRead": ("none", "string", "text/plain", "acs3_streaming", "text"),
        "BinaryRead": ("none", "binary", "application/octet-stream", "acs3_streaming", "binary"),
    }
    for raw in documents:
        call = shape(action=raw["action"])
        resolved = await ApiContractResolver(FakeOpenMeta(raw)).resolve(call, allow_fallback=False)
        request_type, response_type, accept, transport, mode = expected[raw["action"]]
        assert resolved.consumes == tuple(raw.get("consumes", ()))
        assert resolved.produces == tuple(raw.get("produces", ()))
        assert (resolved.request_body_type, resolved.response_body_type) == (request_type, response_type)
        assert resolved.transport == transport
        tool_input = {"body_file": __file__} if request_type == "byte" else {}
        built = await RequestBuilder().build(resolved, tool_input)
        assert built.headers.get("accept") == accept
        assert built.response_policy.mode == mode


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("metadata_method", "override_method", "expected_response_type"),
    [("HEAD", "GET", "json"), ("GET", "HEAD", "none")],
)
async def test_method_override_reinfers_response_body_type_and_updates_digest(
    metadata_method: str,
    override_method: str,
    expected_response_type: str,
) -> None:
    raw = raw_api(
        security=[{"AK": []}],
        style="ROA",
        methods=[metadata_method],
        operationType="read",
        produces=["application/json"],
        responses={"200": {"schema": {"type": "object"}}},
    )
    call = shape(method=override_method, explicit_overrides=("method",))

    resolved = await ApiContractResolver(FakeOpenMeta(raw)).resolve(call, allow_fallback=False)

    assert resolved.method == override_method
    assert resolved.response_body_type == expected_response_type
    stale_type = "none" if expected_response_type == "json" else "json"
    assert replace(resolved, response_body_type=stale_type).security_digest(call) != resolved.security_digest(call)


@pytest.mark.asyncio
async def test_response_headers_enter_contract_policy_and_digest_while_sensitive_names_remain_declared() -> None:
    raw = raw_api(
        security=[{"AK": []}],
        responses={
            "200": {"headers": {"X-Result-Token": {"schema": {"type": "string"}}}},
            "400": {
                "headers": {
                    "x-error-detail": {"schema": {"type": "string"}},
                    "Authorization": {"schema": {"type": "string"}},
                }
            },
        },
    )

    resolved = await ApiContractResolver(FakeOpenMeta(raw)).resolve(shape(), allow_fallback=False)
    built = await RequestBuilder().build(resolved, {})

    assert resolved.declared_response_headers == (
        "authorization",
        "x-error-detail",
        "x-result-token",
    )
    assert built.response_policy.declared_headers == resolved.declared_response_headers
    assert replace(resolved, declared_response_headers=()).security_digest(shape()) != resolved.security_digest(shape())


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "responses",
    [
        {"200": {"headers": {"bad header": {"schema": {"type": "string"}}}}},
        {"200": {"headers": ["x-result-token"]}},
    ],
)
async def test_malformed_response_header_metadata_fails_closed(responses: Any) -> None:
    resolved = await ApiContractResolver(FakeOpenMeta(raw_api(security=[{"AK": []}], responses=responses))).resolve(
        shape(), allow_fallback=False
    )

    assert resolved.executable is False
    assert resolved.unsupported_reasons == ("response_header_metadata_invalid",)


@pytest.mark.asyncio
async def test_xml_response_media_with_object_schema_is_executable_as_string_policy() -> None:
    resolved = await ApiContractResolver(
        FakeOpenMeta(
            raw_api(
                security=[{"AK": []}],
                produces=["application/xml"],
                responses={"200": {"schema": {"type": "object"}}},
            )
        )
    ).resolve(shape(), allow_fallback=False)

    assert resolved.response_body_type == "string"
    assert resolved.executable is True
    assert resolved.unsupported_reasons == ()
    built = await RequestBuilder().build(resolved, {})
    assert built.headers["accept"] == "application/xml"
    assert built.response_policy.mode == "xml"


@pytest.mark.asyncio
async def test_text_plain_response_media_with_object_schema_remains_json_and_non_executable() -> None:
    resolved = await ApiContractResolver(
        FakeOpenMeta(
            raw_api(
                security=[{"AK": []}],
                produces=["text/plain"],
                responses={"200": {"schema": {"type": "object"}}},
            )
        )
    ).resolve(shape(), allow_fallback=False)

    assert resolved.response_body_type == "json"
    assert resolved.executable is False
    assert resolved.unsupported_reasons == ("response_media_type_unsupported",)
    with pytest.raises(ApiContractError, match="contract_not_executable"):
        await RequestBuilder().build(resolved, {})


@pytest.mark.asyncio
async def test_response_media_xml_substring_is_not_compatible_with_string_schema() -> None:
    resolved = await ApiContractResolver(
        FakeOpenMeta(
            raw_api(
                security=[{"AK": []}],
                produces=["application/notxml"],
                responses={"200": {"schema": {"type": "string"}}},
            )
        )
    ).resolve(shape(), allow_fallback=False)

    assert resolved.response_body_type == "string"
    assert resolved.executable is False
    assert resolved.unsupported_reasons == ("response_media_type_unsupported",)
    with pytest.raises(ApiContractError, match="contract_not_executable"):
        await RequestBuilder().build(resolved, {})


@pytest.mark.asyncio
async def test_response_policy_ignores_xml_media_type_parameter() -> None:
    resolved = await ApiContractResolver(
        FakeOpenMeta(
            raw_api(
                security=[{"AK": []}],
                produces=["text/plain; note=xml"],
                responses={"200": {"schema": {"type": "string"}}},
            )
        )
    ).resolve(shape(), allow_fallback=False)

    built = await RequestBuilder().build(resolved, {})

    assert resolved.executable is True
    assert built.headers["accept"] == "text/plain; note=xml"
    assert built.response_policy.mode == "text"


@pytest.mark.asyncio
async def test_reviewed_oss_override_selects_v4_and_preserves_key_slashes() -> None:
    raw = raw_api(
        security=[{"AK": []}],
        product="Oss",
        version="2019-05-17",
        action="GetObject",
        style="ROA",
        methods=["GET"],
        path="/{key}",
        parameters=[
            {"name": "key", "in": "path", "required": True, "schema": {"type": "string"}},
        ],
    )
    call = shape(product="Oss", version="2019-05-17", action="GetObject")
    resolved = await ApiContractResolver(FakeOpenMeta(raw)).resolve(call, allow_fallback=False)
    assert (resolved.signature_scheme, resolved.transport) == ("oss_v4", "oss_v4_sdk")
    assert resolved.parameters[0].path_encoding == "preserve_slashes"
    built = await RequestBuilder().build(resolved, {"params": {"key": "folder/demo %.txt"}})
    assert built.raw_path == b"/folder/demo%20%25.txt"


@pytest.mark.asyncio
async def test_oss_v4_anonymous_contract_is_not_executable() -> None:
    raw = raw_api(
        security=[{"Anonymous": []}],
        product="Oss",
        version="2019-05-17",
        action="GetObject",
        style="ROA",
        methods=["GET"],
        path="/{key}",
        parameters=[
            {"name": "key", "in": "path", "required": True, "schema": {"type": "string"}},
        ],
    )
    call = shape(product="Oss", version="2019-05-17", action="GetObject")

    resolved = await ApiContractResolver(FakeOpenMeta(raw)).resolve(call, allow_fallback=False)

    assert (resolved.auth_type, resolved.signature_scheme, resolved.transport) == (
        "Anonymous",
        "oss_v4",
        "oss_v4_sdk",
    )
    assert resolved.executable is False
    assert resolved.unsupported_reasons == ("oss_v4_anonymous_unsupported",)
    with pytest.raises(ApiContractError, match="contract_not_executable"):
        await RequestBuilder().build(resolved, {"params": {"key": "public/demo.txt"}})


def parameter(
    name: str,
    location: str,
    *,
    required: bool = False,
    style: str | None = None,
    path_encoding: str | None = None,
    schema: dict[str, Any] | None = None,
) -> ParameterMetadata:
    return ParameterMetadata(name, location, required, style, path_encoding, MappingProxyType(schema or {}), None, None)


def contract(*parameters: ParameterMetadata, **changes: Any) -> CanonicalWireContract:
    values: dict[str, Any] = {
        "metadata_source": "fresh",
        "product": "Ecs",
        "version": "2014-05-26",
        "action": "DescribeInstances",
        "style": "RPC",
        "method": "POST",
        "pathname": "/",
        "operation_type": "read",
        "auth_type": "AK",
        "signature_scheme": "acs3",
        "transport": "tea",
        "executable": True,
        "unsupported_reasons": (),
        "parameters": parameters,
        "consumes": (),
        "produces": ("application/json",),
        "policy_digest": "fixture-policy",
    }
    values.update(changes)
    return CanonicalWireContract(**values)


async def local_ref_sibling_contract(
    location: str,
    sibling_schema: dict[str, Any],
    *,
    component_schema: dict[str, Any] | None = None,
) -> tuple[CanonicalWireContract, str]:
    name = "x-mode" if location == "header" else "Payload" if location == "body" else "Mode"
    pathname = "/items/{Mode}" if location == "path" else "/"
    reference = "#/components/schemas/Mode"
    raw = raw_api(
        security=[{"AK": []}],
        style="ROA",
        path=pathname,
        consumes=["application/json"] if location == "body" else [],
        parameters=[
            {
                "name": name,
                "in": location,
                "required": location == "path",
                "schema": {"$ref": reference, **sibling_schema},
            }
        ],
        components={"schemas": {"Mode": component_schema if component_schema is not None else {"type": "string"}}},
    )
    call = shape(
        parameter_names_by_location=MappingProxyType({location: (name,)}),
        body_source="body" if location == "body" else "none",
    )
    resolved = await ApiContractResolver(FakeOpenMeta(raw)).resolve(call, allow_fallback=False)
    assert resolved.executable is True
    return resolved, name


@pytest.mark.asyncio
@pytest.mark.parametrize("location", ["query", "path", "header", "body"])
async def test_request_builder_enforces_local_ref_sibling_enum_across_wire_locations(location: str) -> None:
    api, name = await local_ref_sibling_contract(location, {"enum": ["allowed"]})
    forbidden = "business-value-forbidden"
    forbidden_input = {"body": forbidden} if location == "body" else {"params": {name: forbidden}}

    with pytest.raises(ApiContractError, match=f"^invalid_parameter_enum:{name}$") as raised:
        await RequestBuilder().build(api, forbidden_input)

    public_message = public_aliyun_error(raised.value, product="Ecs", action="DescribeInstances")
    assert forbidden not in str(raised.value)
    assert forbidden not in public_message

    allowed_input = {"body": "allowed"} if location == "body" else {"params": {name: "allowed"}}
    built = await RequestBuilder().build(api, allowed_input)
    if location == "query":
        assert dict(built.canonical_query) == {name: "allowed"}
    elif location == "path":
        assert built.raw_path == b"/items/allowed"
    elif location == "header":
        assert built.headers[name] == "allowed"
    else:
        assert built.body == b'"allowed"'


@pytest.mark.asyncio
async def test_request_builder_accepts_numeric_values_for_string_numeric_openmeta_enum() -> None:
    api = contract(parameter("PageSize", "query", schema={"type": "integer", "enum": ["30", "50", "100"]}))

    built = await RequestBuilder().build(api, {"params": {"PageSize": 30}})

    assert dict(built.canonical_query) == {"PageSize": "30"}


@pytest.mark.asyncio
async def test_request_builder_enforces_local_ref_sibling_type() -> None:
    api, name = await local_ref_sibling_contract("query", {"type": "string"}, component_schema={})

    with pytest.raises(ApiContractError, match=f"^invalid_parameter_type:{name}$") as raised:
        await RequestBuilder().build(api, {"params": {name: 7}})

    assert raised.value.parameter == name
    assert raised.value.expected_type == "string"
    assert raised.value.actual_type == "integer"
    built = await RequestBuilder().build(api, {"params": {name: "allowed"}})
    assert dict(built.canonical_query) == {name: "allowed"}


@pytest.mark.asyncio
async def test_request_builder_enforces_nested_all_of_constraints() -> None:
    api, name = await local_ref_sibling_contract(
        "query",
        {"allOf": [{"allOf": [{"enum": ["allowed"]}]}]},
    )

    with pytest.raises(ApiContractError, match=f"^invalid_parameter_enum:{name}$"):
        await RequestBuilder().build(api, {"params": {name: "forbidden"}})

    built = await RequestBuilder().build(api, {"params": {name: "allowed"}})
    assert dict(built.canonical_query) == {name: "allowed"}


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("value", "expected_type", "actual_type"),
    [("string-candidate", "integer", "string"), (7, "string", "integer")],
)
async def test_request_builder_stably_rejects_conflicting_all_of_types(
    value: Any,
    expected_type: str,
    actual_type: str,
) -> None:
    api = contract(parameter("Mode", "query", schema={"type": "string", "allOf": [{"type": "integer"}]}))

    with pytest.raises(ApiContractError, match="^invalid_parameter_type:Mode$") as raised:
        await RequestBuilder().build(api, {"params": {"Mode": value}})

    assert raised.value.parameter == "Mode"
    assert raised.value.expected_type == expected_type
    assert raised.value.actual_type == actual_type


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "schema",
    [
        {"type": "string", "allOf": {"type": "string"}},
        {"type": "string", "allOf": [{"type": "string"}, "not-a-schema"]},
    ],
    ids=["non-sequence", "non-schema-branch"],
)
async def test_request_builder_fails_closed_for_malformed_all_of(schema: dict[str, Any]) -> None:
    api = contract(parameter("Mode", "query", schema=schema))

    with pytest.raises(ApiContractError, match="^contract_not_executable$"):
        await RequestBuilder().build(api, {"params": {"Mode": "allowed"}})


@pytest.mark.asyncio
async def test_request_builder_fails_closed_when_all_of_exceeds_validation_depth() -> None:
    schema: dict[str, Any] = {"enum": ["allowed"]}
    for _ in range(33):
        schema = {"allOf": [schema]}
    api = contract(parameter("Mode", "query", schema=schema))

    with pytest.raises(ApiContractError, match="^contract_not_executable$"):
        await RequestBuilder().build(api, {"params": {"Mode": "allowed"}})


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("response_body_type", "produces", "expected"),
    [
        ("json", ("application/xml", "application/problem+json"), "application/problem+json"),
        ("string", ("application/json", "text/plain"), "text/plain"),
        ("string", ("application/json", "application/xml"), "application/xml"),
        ("binary", ("application/json", "application/octet-stream"), "application/octet-stream"),
    ],
)
async def test_request_builder_selects_accept_compatible_with_response_body_type(
    response_body_type: str,
    produces: tuple[str, ...],
    expected: str,
) -> None:
    built = await RequestBuilder().build(
        contract(response_body_type=response_body_type, produces=produces),
        {},
    )

    assert built.headers["accept"] == expected


@pytest.mark.asyncio
async def test_request_builder_encodes_wire_matrix_without_mutating_input() -> None:
    api = contract(
        parameter("id", "path", required=True, path_encoding="segment", schema={"type": "string"}),
        parameter("Tags", "query", style="repeatList", schema={"type": "array"}),
        parameter("Simple", "query", style="simple", schema={"type": "array"}),
        parameter("Spaces", "query", style="spaceDelimited", schema={"type": "array"}),
        parameter("Pipes", "query", style="pipeDelimited", schema={"type": "array"}),
        parameter("Json", "query", style="json", schema={"type": "array"}),
        parameter("Enabled", "query", schema={"type": "boolean"}),
        parameter("x-demo", "header", schema={"type": "string"}),
        parameter("x-oss-meta-*", "header", schema={"type": "object"}),
        pathname="/items/{id}",
    )
    tool_input = {
        "params": {
            "id": "a/b %",
            "Tags": [{"Key": "env", "Value": "prod"}],
            "Simple": ["a", "b"],
            "Spaces": ["a", "b"],
            "Pipes": ["a", "b"],
            "Json": ["a", "b"],
            "Enabled": True,
            "Parameters.1.ParameterKey": "Name",
            "Unknown": "kept",
            "x-demo": "ok",
            "x-oss-meta-*": {"owner": "iac"},
        }
    }
    original = copy.deepcopy(tool_input)
    built = await RequestBuilder().build(api, tool_input)
    assert built.raw_path == b"/items/a%2Fb%20%25"
    assert dict(built.canonical_query) == {
        "Enabled": "true",
        "Json": '["a","b"]',
        "Parameters.1.ParameterKey": "Name",
        "Pipes": "a|b",
        "Simple": "a,b",
        "Spaces": "a b",
        "Tags.1.Key": "env",
        "Tags.1.Value": "prod",
        "Unknown": "kept",
    }
    assert built.headers == {"accept": "application/json", "x-demo": "ok", "x-oss-meta-owner": "iac"}
    assert built.body is None
    assert tool_input == original


@pytest.mark.asyncio
async def test_request_builder_encodes_formdata_repeatlist_arrays_as_indexed_fields() -> None:
    api = contract(
        parameter("ProductTypeList", "formData", style="repeatList", schema={"type": "array"}),
        parameter("PageSize", "formData", schema={"type": "integer"}),
        request_body_type="formData",
    )

    built = await RequestBuilder().build(
        api,
        {"params": {"ProductTypeList": ["CloudApp"], "PageSize": 10}},
    )

    assert built.body == b"PageSize=10&ProductTypeList.1=CloudApp"
    assert built.headers["content-type"] == "application/x-www-form-urlencoded"


@pytest.mark.asyncio
async def test_request_builder_encodes_formdata_flat_objects_as_dotted_fields() -> None:
    api = contract(
        parameter("BizModule", "formData", required=True, schema={"type": "string"}),
        parameter("TimeRange", "formData", required=True, style="flat", schema={"type": "object"}),
        parameter("AppKey", "formData", required=True, schema={"type": "integer"}),
        request_body_type="formData",
    )

    built = await RequestBuilder().build(
        api,
        {
            "params": {
                "BizModule": "crash",
                "TimeRange": {"StartTime": 1704067200000, "EndTime": 1704153600000},
                "AppKey": 1,
            }
        },
    )

    assert built.body == (b"AppKey=1&BizModule=crash&TimeRange.EndTime=1704153600000&TimeRange.StartTime=1704067200000")


@pytest.mark.asyncio
async def test_unknown_containers_use_deterministic_compact_json_with_nested_booleans() -> None:
    built = await RequestBuilder().build(
        contract(),
        {
            "params": {
                "UnknownObject": {"z": True, "a": [False, {"nested": True}]},
                "UnknownList": [True, {"b": False, "a": 1}],
            }
        },
    )

    assert dict(built.canonical_query) == {
        "UnknownList": '[true,{"a":1,"b":false}]',
        "UnknownObject": '{"a":[false,{"nested":true}],"z":true}',
    }


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("api", "tool_input", "expected_body", "content_type"),
    [
        (
            contract(consumes=("application/json",), request_body_type="json"),
            {"body": [1, True, None]},
            b"[1,true,null]",
            "application/json",
        ),
        (
            contract(parameter("TemplateBody", "formData", required=True), request_body_type="formData"),
            {"params": {"TemplateBody": "{}"}},
            b"TemplateBody=%7B%7D",
            "application/x-www-form-urlencoded",
        ),
        (
            contract(parameter("payload", "body"), consumes=("application/json",), request_body_type="json"),
            {"params": {"payload": "value"}},
            b'"value"',
            "application/json",
        ),
    ],
)
async def test_request_builder_body_sources(
    api: CanonicalWireContract, tool_input: dict[str, Any], expected_body: bytes, content_type: str
) -> None:
    built = await RequestBuilder().build(api, tool_input)
    assert built.body == expected_body
    assert built.headers["content-type"] == content_type


@pytest.mark.asyncio
async def test_request_builder_reads_regular_body_file_with_hard_limit(tmp_path: Path) -> None:
    body_file = tmp_path / "payload.bin"
    body_file.write_bytes(b"payload")
    api = contract(
        parameter("payload", "body", schema={"type": "string", "format": "binary"}),
        request_body_type="byte",
    )
    built = await RequestBuilder().build(api, {"body_file": str(body_file), "content_type": "image/png"})
    assert built.body == b"payload"
    assert built.headers["content-type"] == "image/png"


@pytest.mark.asyncio
async def test_top_level_body_satisfies_required_body_parameter() -> None:
    api = contract(
        parameter("payload", "body", required=True, schema={"type": "object"}),
        consumes=("application/json",),
        request_body_type="json",
    )
    built = await RequestBuilder().build(api, {"body": {"name": "demo"}})
    assert built.body == b'{"name":"demo"}'


@pytest.mark.asyncio
async def test_missing_required_binary_body_requires_top_level_body_file() -> None:
    api = contract(
        parameter("body", "body", required=True, schema={"type": "string", "format": "binary"}),
        product="Oss",
        action="PutObject",
        request_body_type="byte",
    )

    with pytest.raises(ApiContractError, match="^missing_required_parameters:body_file$") as raised:
        await RequestBuilder().build(api, {})

    assert public_aliyun_error(raised.value, product="Oss", action="PutObject") == (
        "Alibaba Cloud API Oss/PutObject requires body_file for its binary request body."
    )


@pytest.mark.asyncio
async def test_body_file_requires_binary_contract_and_enforces_limit(tmp_path: Path) -> None:
    body_file = tmp_path / "payload.bin"
    body_file.write_bytes(b"payload")
    with pytest.raises(ApiContractError, match="body_file_not_supported"):
        await RequestBuilder().build(
            contract(consumes=("application/json",), request_body_type="json"),
            {"body_file": str(body_file)},
        )

    body_file.write_bytes(b"")
    with body_file.open("r+b") as handle:
        handle.truncate(32 * 1024 * 1024 + 1)
    binary = contract(parameter("payload", "body", schema={"format": "binary"}), request_body_type="byte")
    with pytest.raises(ApiContractError, match="body_file_too_large"):
        await RequestBuilder().build(binary, {"body_file": str(body_file)})


@pytest.mark.asyncio
async def test_explicit_content_type_must_match_body_kind() -> None:
    with pytest.raises(ApiContractError, match="content_type_mismatch"):
        await RequestBuilder().build(
            contract(consumes=("application/json",), request_body_type="json"),
            {"body": {}, "content_type": "text/plain"},
        )
    form_api = contract(parameter("TemplateBody", "formData"), request_body_type="formData")
    with pytest.raises(ApiContractError, match="content_type_mismatch"):
        await RequestBuilder().build(
            form_api,
            {"params": {"TemplateBody": "{}"}, "content_type": "application/json"},
        )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "content_type",
    [
        "application /json",
        "application/(comment)json",
        'application/json; charset="utf-8";',
        "application/json\x00",
        "application/json\x0b",
        'application/json; charset="utf-8',
        "application/json; charset=utf-8; CHARSET=us-ascii",
        "application/json; bad name=value",
        "application/json; charset==utf-8",
    ],
)
async def test_content_type_rejects_non_rfc_media_type_syntax(content_type: str) -> None:
    with pytest.raises(ApiContractError, match="invalid_content_type"):
        await RequestBuilder().build(
            contract(consumes=("application/json",), request_body_type="json"),
            {"body": {}, "content_type": content_type},
        )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "content_type",
    ["application/vnd.api+json; charset=utf-8", "application/merge-patch+json"],
)
async def test_content_type_preserves_approved_vendor_json_value(content_type: str) -> None:
    built = await RequestBuilder().build(
        contract(consumes=(content_type,), request_body_type="json"),
        {"body": {"name": "demo"}, "content_type": content_type},
    )

    assert built.headers["content-type"] == content_type


@pytest.mark.asyncio
async def test_content_type_rejects_valid_json_media_type_not_declared_by_contract() -> None:
    with pytest.raises(ApiContractError, match="content_type_mismatch"):
        await RequestBuilder().build(
            contract(consumes=("application/json",), request_body_type="json"),
            {"body": {"name": "demo"}, "content_type": "application/merge-patch+json"},
        )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("content_type", "declared", "expected"),
    [
        (
            "Application/Vnd.Api+Json; Charset=utf-8",
            "application/vnd.api+json",
            "application/vnd.api+json; charset=utf-8",
        ),
        (
            'application/json; profile="https://example.test/a\\"b"',
            "application/json",
            'application/json; profile="https://example.test/a\\"b"',
        ),
        ("application/json; profile=compact", "application/json", "application/json; profile=compact"),
    ],
)
async def test_content_type_writes_only_canonical_validated_value(
    content_type: str,
    declared: str,
    expected: str,
) -> None:
    built = await RequestBuilder().build(
        contract(consumes=(declared,), request_body_type="json"),
        {"body": {"name": "demo"}, "content_type": content_type},
    )

    assert built.headers["content-type"] == expected


def test_structured_content_type_parser_preserves_quoted_parameter_boundaries() -> None:
    from iac_code.tools.cloud.aliyun import api_contract as api_contract_module

    parsed = api_contract_module.parse_content_type('Text/Plain; note="x; charset=utf-16"; CHARSET="utf-8"')

    assert parsed.media_type == "text/plain"
    assert dict(parsed.parameters) == {"note": "x; charset=utf-16", "charset": "utf-8"}
    assert parsed.canonical == 'text/plain; note="x; charset=utf-16"; charset="utf-8"'


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("api", "tool_input", "error"),
    [
        (contract(), {"params": {"Signature": "x"}}, "signature_parameter_forbidden"),
        (contract(parameter("x-demo", "header")), {"params": {"x-demo": "yes\r\nno"}}, "invalid_header_value"),
        (contract(pathname="/{missing}"), {}, "unresolved_path_parameter"),
        (
            contract(parameter("payload", "body"), request_body_type="json"),
            {"body": {}, "params": {"payload": {}}},
            "conflicting_body_sources",
        ),
        (contract(), {"content_type": "text/plain"}, "content_type_without_body"),
        (contract(pathname="https://evil.test/x"), {}, "invalid_pathname"),
        (contract(pathname="//evil.test/x"), {}, "invalid_pathname"),
        (contract(pathname="/bad\\path"), {}, "invalid_pathname"),
        (contract(pathname="/bad path"), {}, "invalid_pathname"),
        (contract(), {"region_id": "cn_hangzhou"}, "invalid_region_id"),
    ],
)
async def test_request_builder_rejects_invalid_local_input(
    api: CanonicalWireContract, tool_input: dict[str, Any], error: str
) -> None:
    with pytest.raises(ApiContractError, match=error):
        await RequestBuilder().build(api, tool_input)


@pytest.mark.asyncio
async def test_reserved_signature_parameter_error_names_the_safe_parameter() -> None:
    with pytest.raises(ApiContractError, match="^signature_parameter_forbidden$") as raised:
        await RequestBuilder().build(contract(), {"params": {"Signature": "business-value"}})

    assert raised.value.parameter == "Signature"
    message = public_aliyun_error(raised.value, product="Ecs", action="DescribeInstances")
    assert message == (
        "Alibaba Cloud API Ecs/DescribeInstances parameter Signature is reserved for request signing and cannot be set."
    )
    assert "business-value" not in message


@pytest.mark.asyncio
async def test_unresolved_path_error_names_every_safe_placeholder() -> None:
    api = contract(pathname="/things/{thingId}/children/{childId}")

    with pytest.raises(ApiContractError, match="^unresolved_path_parameter$") as raised:
        await RequestBuilder().build(api, {})

    assert raised.value.parameter == "thingId,childId"
    assert public_aliyun_error(raised.value, product="FC", action="GetThing") == (
        "Alibaba Cloud API FC/GetThing is missing path parameters thingId,childId."
    )


@pytest.mark.asyncio
async def test_untyped_path_parameter_error_includes_safe_name_and_finite_types() -> None:
    api = contract(
        parameter("key", "path", required=True, schema={}),
        product="Oss",
        action="GetObject",
        pathname="/{key}",
    )

    with pytest.raises(ApiContractError, match="^invalid_path_parameter$") as raised:
        await RequestBuilder().build(api, {"params": {"key": True}})

    assert raised.value.parameter == "key"
    assert raised.value.expected_type == "scalar"
    assert raised.value.actual_type == "boolean"
    assert public_aliyun_error(raised.value, product="Oss", action="GetObject") == (
        "Alibaba Cloud API Oss/GetObject path parameter key expects scalar but received boolean."
    )


@pytest.mark.asyncio
async def test_wildcard_header_nested_value_error_keeps_parameter_and_finite_types() -> None:
    api = contract(parameter("x-oss-meta-*", "header", schema={"type": "object"}), product="Oss", action="PutObject")

    with pytest.raises(ApiContractError, match=r"^invalid_parameter_type:x-oss-meta-\*$") as raised:
        await RequestBuilder().build(api, {"params": {"x-oss-meta-*": {"owner": {"nested": True}}}})

    assert raised.value.parameter == "x-oss-meta-*"
    assert raised.value.expected_type == "scalar"
    assert raised.value.actual_type == "object"
    assert public_aliyun_error(raised.value, product="Oss", action="PutObject") == (
        "Alibaba Cloud API Oss/PutObject parameter x-oss-meta-* expects scalar but received object."
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("name", "value", "expected_type", "actual_type"),
    [
        ("x-demo", ["value"], "scalar", "array"),
        ("x-oss-meta-*", "owner=alice", "object", "string"),
    ],
)
async def test_schema_less_header_type_errors_keep_parameter_and_finite_types(
    name: str,
    value: Any,
    expected_type: str,
    actual_type: str,
) -> None:
    api = contract(parameter(name, "header", schema={}), product="Oss", action="PutObject")

    with pytest.raises(ApiContractError, match=r"^invalid_parameter_type:") as raised:
        await RequestBuilder().build(api, {"params": {name: value}})

    assert raised.value.parameter == name
    assert raised.value.expected_type == expected_type
    assert raised.value.actual_type == actual_type
    assert public_aliyun_error(raised.value, product="Oss", action="PutObject") == (
        "Alibaba Cloud API Oss/PutObject parameter {} expects {} but received {}.".format(
            name,
            expected_type,
            actual_type,
        )
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("name", "value", "code", "expected_type", "actual_type", "message"),
    [
        (
            "x-demo",
            "yes\r\nno",
            "invalid_header_value",
            "scalar",
            "string",
            "Alibaba Cloud API Oss/PutObject parameter x-demo expects a scalar header value without line breaks "
            "but received string.",
        ),
        (
            "x-oss-meta-*",
            {"bad header": "business-value"},
            "invalid_expanded_header_name",
            "string",
            "string",
            "Alibaba Cloud API Oss/PutObject parameter x-oss-meta-* expects a valid header name "
            "but received a string with invalid syntax.",
        ),
    ],
)
async def test_header_syntax_errors_keep_declared_parameter_and_finite_types_without_values(
    name: str,
    value: Any,
    code: str,
    expected_type: str,
    actual_type: str,
    message: str,
) -> None:
    api = contract(parameter(name, "header", schema={}), product="Oss", action="PutObject")

    with pytest.raises(ApiContractError, match=f"^{code}$") as raised:
        await RequestBuilder().build(api, {"params": {name: value}})

    assert raised.value.parameter == name
    assert raised.value.expected_type == expected_type
    assert raised.value.actual_type == actual_type
    assert public_aliyun_error(raised.value, product="Oss", action="PutObject") == message
    assert "business-value" not in message


@pytest.mark.asyncio
async def test_params_container_error_keeps_finite_parameter_type_context() -> None:
    with pytest.raises(ApiContractError, match=r"^invalid_parameter_type:params$") as raised:
        await RequestBuilder().build(contract(), {"params": ["business-value"]})

    assert raised.value.parameter == "params"
    assert raised.value.expected_type == "object"
    assert raised.value.actual_type == "array"
    assert public_aliyun_error(raised.value, product="Ecs", action="DescribeInstances") == (
        "Alibaba Cloud API Ecs/DescribeInstances parameter params expects object but received array."
    )


@pytest.mark.asyncio
async def test_host_label_error_keeps_declared_parameter_and_syntax_context() -> None:
    api = contract(parameter("bucket", "host", schema={"type": "string"}), product="Oss", action="GetObject")

    with pytest.raises(ApiContractError, match="^invalid_host_label$") as raised:
        await RequestBuilder().build(api, {"params": {"bucket": "bad/host"}})

    assert raised.value.parameter == "bucket"
    assert raised.value.expected_type == "string"
    assert raised.value.actual_type == "string"
    assert public_aliyun_error(raised.value, product="Oss", action="GetObject") == (
        "Alibaba Cloud API Oss/GetObject parameter bucket expects a valid DNS host-label string "
        "but received string with invalid syntax."
    )


@pytest.mark.asyncio
async def test_declared_authorization_header_is_allowed_only_for_anonymous_contract() -> None:
    authorization = parameter("Authorization", "header", required=True, schema={"type": "string"})
    anonymous = contract(
        authorization,
        auth_type="Anonymous",
        security_declared=True,
        security_requirements=(SecurityRequirement(("Anonymous",), ((),)),),
    )

    built = await RequestBuilder().build(anonymous, {"params": {"Authorization": "xx"}})

    assert built.headers["authorization"] == "xx"
    assert anonymous.header_policy_version == "declared-anonymous-authorization-v2"

    for unsafe_contract in (
        replace(anonymous, security_declared=False),
        replace(anonymous, security_requirements=()),
        replace(anonymous, security_requirements=(SecurityRequirement(("AK",), ((),)),)),
    ):
        with pytest.raises(ApiContractError, match="^reserved_header_forbidden$"):
            await RequestBuilder().build(unsafe_contract, {"params": {"Authorization": "xx"}})


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "name",
    ["Authorization", "x-acs-signature-*"],
)
async def test_reserved_header_error_keeps_declared_parameter_context(name: str) -> None:
    value: Any = {"algorithm": "business-value"} if "*" in name else "business-value"
    api = contract(parameter(name, "header", schema={}), product="Ecs", action="DescribeInstances")

    with pytest.raises(ApiContractError, match="^reserved_header_forbidden$") as raised:
        await RequestBuilder().build(api, {"params": {name: value}})

    assert raised.value.parameter == name
    assert raised.value.expected_type == "scalar"
    assert raised.value.actual_type == "string"
    assert public_aliyun_error(raised.value, product="Ecs", action="DescribeInstances") == (
        "Alibaba Cloud API Ecs/DescribeInstances parameter {} targets a reserved authentication header. "
        "Remove the parameter and retry.".format(name)
    )


@pytest.mark.asyncio
async def test_request_builder_rejects_symlink_body_file(tmp_path: Path) -> None:
    target = tmp_path / "target"
    target.write_bytes(b"secret")
    link = tmp_path / "link"
    link.symlink_to(target)
    api = contract(parameter("payload", "body", schema={"format": "binary"}), request_body_type="byte")
    with pytest.raises(ApiContractError, match="invalid_body_file"):
        await RequestBuilder().build(api, {"body_file": str(link)})


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("api", "tool_input", "error"),
    [
        (contract(request_body_type="none"), {"body": {}}, "body_source_mismatch"),
        (contract(request_body_type="byte"), {"body": {}}, "body_source_mismatch"),
        (
            contract(parameter("payload", "body"), request_body_type="formData"),
            {"params": {"payload": {}}},
            "body_source_mismatch",
        ),
        (
            contract(parameter("TemplateBody", "formData"), request_body_type="json"),
            {"params": {"TemplateBody": "{}"}},
            "body_source_mismatch",
        ),
        (
            contract(request_body_type="json"),
            {"body_file": b"payload"},
            "body_file_not_supported",
        ),
    ],
)
async def test_request_builder_rejects_sources_that_mismatch_canonical_body_type(
    api: CanonicalWireContract,
    tool_input: dict[str, Any],
    error: str,
) -> None:
    with pytest.raises(ApiContractError, match=f"^{error}$"):
        await RequestBuilder().build(api, tool_input)
