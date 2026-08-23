"""Canonical Alibaba Cloud API contracts and deterministic request assembly."""

from __future__ import annotations

import asyncio
import copy
import hashlib
import json
import os
import re
import stat
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field, fields, is_dataclass, replace
from pathlib import Path
from types import MappingProxyType
from typing import Any, Literal
from urllib.parse import quote, urlencode

import yaml
from alibabacloud_openapi_util.client import Client as OpenApiUtil

from iac_code.tools.cloud.aliyun.api_identifiers import is_safe_api_version
from iac_code.tools.cloud.aliyun.openmeta import (
    ApiMetadata,
    MetadataFetch,
    OpenMetaCacheStatus,
    OpenMetaClient,
    ParameterMetadata,
    SecurityRequirement,
)
from iac_code.tools.cloud.aliyun.product_resolver import (
    PRODUCT_MATCH_CONFIDENCES,
    PRODUCT_MATCH_STRATEGIES,
    ProductResolution,
    ProductResolver,
)

_VERSION_MAP = {
    "ros": "2019-09-10",
    "ecs": "2014-05-26",
    "rds": "2014-08-15",
    "r-kvstore": "2015-01-01",
    "slb": "2014-05-15",
    "alb": "2024-03-27",
    "nlb": "2022-04-30",
    "vpc": "2016-04-28",
    "oss": "2019-05-17",
    "iacservice": "2021-08-06",
}
_PRODUCT_NAMES = {name: "IaCService" if name == "iacservice" else name.title() for name in _VERSION_MAP}
_SAFE_REGION = re.compile(r"^[a-z0-9][a-z0-9-]{0,62}$")
_SAFE_METHODS = {"GET", "POST", "PUT", "DELETE", "PATCH", "HEAD", "OPTIONS"}
_SAFE_STYLES = {"RPC", "ROA"}
_SAFE_PARAMETER_LOCATIONS = {"query", "path", "header", "body", "formData", "host"}
_SAFE_PARAMETER_STYLES = {None, "repeatList", "simple", "spaceDelimited", "pipeDelimited", "json", "flat"}
_ALLOWED_METADATA_ABSENCE = frozenset({"not_found", "temporarily_unavailable"})
_HEADER_TOKEN = re.compile(r"^[!#$%&'*+.^_`|~0-9A-Za-z-]+$")
_HEADER_TOKEN_CHARACTERS = frozenset("!#$%&'*+-.^_`|~0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz")
_HOST_BINDING_LABEL = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?$")
_PATH_PLACEHOLDER = re.compile(r"\{([^{}]+)\}")
_MAX_BODY_BYTES = 32 * 1024 * 1024
_DEFAULT_RESPONSE_BYTES = 8 * 1024 * 1024
_MAX_RESPONSE_BYTES = 16 * 1024 * 1024
_TRANSPORTS = {"tea", "acs1", "acs3_streaming", "oss_v4_sdk"}
_SIGNATURE_SCHEMES = {"acs1", "acs3", "oss_v4"}
_AUTH_TYPES = {"AK", "Anonymous"}
_OPENMETA_CACHE_STATUSES = {"memory_fresh", "disk_fresh", "remote", "disk_stale", "negative_hit", "miss"}
_OSS_OPENMETA_FALLBACK_REASON = "oss_openmeta_required_for_complete_request"
_MAX_PARAMETER_SCHEMA_DEPTH = 32
_DATA_DIR = Path(__file__).parent / "data"
_OVERRIDES_PATH = _DATA_DIR / "openmeta" / "api_overrides.yml"
_ENDPOINT_OVERRIDES_PATH = _DATA_DIR / "endpoints" / "overrides.yml"
_CATALOG_PATH = _DATA_DIR / "endpoints" / "catalog.json"
_UNAVAILABLE_PATH = _DATA_DIR / "endpoints" / "unavailable.json"
_SIGNATURE_QUERY_FIELDS = {
    "accesskeyid",
    "signature",
    "signaturenonce",
    "securitytoken",
    "timestamp",
}


def _metadata_error_code(error: Any) -> str:
    if error == "not_found":
        return "metadata_not_found"
    if error == "temporarily_unavailable":
        return "metadata_unavailable"
    return "metadata_protocol_error"


_RESERVED_HEADERS = {
    "host",
    "authorization",
    "proxy-authorization",
    "content-length",
    "transfer-encoding",
    "connection",
    "accept",
    "content-type",
    "x-acs-date",
    "x-acs-action",
    "x-acs-version",
}


class ApiContractError(ValueError):
    """Stable local contract or request validation error."""

    def __init__(
        self,
        code: str,
        *,
        product: str | None = None,
        parameter: str | None = None,
        expected_type: str | None = None,
        actual_type: str | None = None,
        suggestions: tuple[str, ...] = (),
    ) -> None:
        super().__init__(code)
        self.code = code
        self.product = product
        self.parameter = parameter
        self.expected_type = expected_type
        self.actual_type = actual_type
        self.suggestions = suggestions


@dataclass(frozen=True)
class ApiCallShape:
    product: str
    version: str | None
    action: str
    region_id: str
    explicit_overrides: tuple[str, ...]
    parameter_names_by_location: Mapping[str, tuple[str, ...]]
    body_source: Literal["none", "body", "body_file", "params_body", "formdata"]
    endpoint: str | None = None
    style: str | None = None
    method: str | None = None
    pathname: str | None = None
    content_type: str | None = None
    max_response_bytes: int = _DEFAULT_RESPONSE_BYTES
    _business_value: Any = field(default=None, repr=False, compare=False)

    def __post_init__(self) -> None:
        frozen = {str(key): tuple(value) for key, value in self.parameter_names_by_location.items()}
        object.__setattr__(self, "parameter_names_by_location", MappingProxyType(frozen))

    def with_business_value(self, value: Any) -> ApiCallShape:
        return ApiCallShape(
            product=self.product,
            version=self.version,
            action=self.action,
            region_id=self.region_id,
            explicit_overrides=self.explicit_overrides,
            parameter_names_by_location=self.parameter_names_by_location,
            body_source=self.body_source,
            endpoint=self.endpoint,
            style=self.style,
            method=self.method,
            pathname=self.pathname,
            content_type=self.content_type,
            max_response_bytes=self.max_response_bytes,
            _business_value=value,
        )

    def security_view(self) -> Mapping[str, Any]:
        return {
            "product": self.product,
            "version": self.version,
            "action": self.action,
            "region_id": self.region_id,
            "explicit_overrides": self.explicit_overrides,
            "parameter_names_by_location": self.parameter_names_by_location,
            "body_source": self.body_source,
            "endpoint": self.endpoint,
            "style": self.style,
            "method": self.method,
            "pathname": self.pathname,
            "content_type": self.content_type,
            "max_response_bytes": self.max_response_bytes,
        }


@dataclass(frozen=True)
class CanonicalWireContract:
    metadata_source: str
    product: str
    version: str
    action: str
    style: str
    method: str
    pathname: str
    operation_type: str | None
    auth_type: str
    signature_scheme: str
    transport: str
    executable: bool
    unsupported_reasons: tuple[str, ...]
    parameters: tuple[ParameterMetadata, ...]
    consumes: tuple[str, ...]
    produces: tuple[str, ...]
    policy_digest: str
    protocol: str = "HTTPS"
    request_body_type: Literal["json", "formData", "byte", "none"] = "none"
    response_body_type: Literal["json", "string", "binary", "none"] = "json"
    security_declared: bool = False
    security_requirements: tuple[SecurityRequirement, ...] = ()
    declared_response_headers: tuple[str, ...] = ()
    header_policy_version: str = "declared-anonymous-authorization-v2"
    query_policy_version: str = "unknown-container-json-v1"
    host_policy_version: str = "host-binding-v1"
    endpoint_policy_digest: str = ""
    catalog_schema_version: int = 1
    catalog_source_commit: str = ""
    oss_catalog_schema_version: int = 0
    oss_catalog_digest: str = ""
    oss_sdk_version: str = ""
    openmeta_cache_status: str = "miss"
    requested_product: str = ""
    product_match_strategy: str = "exact_code"
    product_match_confidence: str = "high"

    def __post_init__(self) -> None:
        if self.transport not in _TRANSPORTS:
            raise ApiContractError("unsupported_transport")
        if self.signature_scheme not in _SIGNATURE_SCHEMES:
            raise ApiContractError("unsupported_signature_scheme")
        if self.openmeta_cache_status not in _OPENMETA_CACHE_STATUSES:
            raise ApiContractError("invalid_openmeta_cache_status")
        if self.product_match_strategy not in PRODUCT_MATCH_STRATEGIES:
            raise ApiContractError("invalid_product_match_strategy")
        if self.product_match_confidence not in PRODUCT_MATCH_CONFIDENCES:
            raise ApiContractError("invalid_product_match_confidence")

    def security_digest(self, shape: ApiCallShape) -> str:
        contract = _json_value(self)
        contract.pop("openmeta_cache_status")
        payload = {"contract": contract, "call_shape": shape.security_view()}
        encoded = json.dumps(_json_value(payload), sort_keys=True, separators=(",", ":"), ensure_ascii=True)
        return hashlib.sha256(encoded.encode("ascii")).hexdigest()


@dataclass(frozen=True)
class ResponseBodyPolicy:
    mode: Literal["json", "text", "xml", "binary", "headers_only"]
    max_bytes: int
    declared_headers: tuple[str, ...]


@dataclass(frozen=True)
class ParsedContentType:
    canonical: str
    media_type: str
    parameters: Mapping[str, str]


@dataclass(frozen=True)
class BuiltApiRequest:
    method: str
    raw_path: bytes
    canonical_query: tuple[tuple[str, str], ...]
    headers: Mapping[str, str]
    body: bytes | None
    response_policy: ResponseBodyPolicy
    host_values: Mapping[str, str] = field(default_factory=lambda: MappingProxyType({}))


@dataclass(frozen=True)
class _VersionCandidate:
    version: str
    source: str
    include_excluded_version: bool = False


class ApiContractResolver:
    def __init__(
        self,
        openmeta: OpenMetaClient,
        *,
        policy_digest: str | None = None,
        overrides_path: Path = _OVERRIDES_PATH,
        catalog_path: Path = _CATALOG_PATH,
        unavailable_path: Path = _UNAVAILABLE_PATH,
        oss_catalog: Any | None = None,
        product_resolver: ProductResolver | None = None,
    ) -> None:
        self._openmeta = openmeta
        self._product_resolver = product_resolver or ProductResolver(openmeta)
        override_bytes = overrides_path.read_bytes()
        loaded = yaml.safe_load(override_bytes) or {}
        if not isinstance(loaded, Mapping):
            raise ApiContractError("invalid_api_overrides")
        self._overrides = loaded
        self._validate_signature_policy()
        self._policy_digest = policy_digest or hashlib.sha256(override_bytes).hexdigest()
        catalog_bytes = catalog_path.read_bytes()
        catalog = json.loads(catalog_bytes)
        catalog_meta = catalog.get("_meta", {}) if isinstance(catalog, Mapping) else {}
        self._catalog_schema_version = int(catalog_meta.get("schema_version", 0))
        self._catalog_source_commit = str(catalog_meta.get("source_commit", ""))
        endpoint_policy = (
            catalog_bytes + unavailable_path.read_bytes() + override_bytes + _ENDPOINT_OVERRIDES_PATH.read_bytes()
        )
        self._endpoint_policy_digest = hashlib.sha256(endpoint_policy).hexdigest()
        if oss_catalog is None:
            from iac_code.tools.cloud.aliyun.oss_v4_adapter import OssOperationCatalog

            oss_catalog = OssOperationCatalog.load()
        if not callable(getattr(oss_catalog, "get", None)) or not isinstance(
            getattr(oss_catalog, "meta", None), Mapping
        ):
            raise ApiContractError("invalid_oss_operation_catalog")
        self._oss_catalog = oss_catalog
        self._first_product_actions: dict[str, str] = {}
        self._hot_products: set[str] = set()

    def _validate_signature_policy(self) -> None:
        candidates = [self._overrides.get("default_signature_scheme")]
        products = self._overrides.get("products", {})
        if isinstance(products, Mapping):
            for record in products.values():
                if not isinstance(record, Mapping):
                    continue
                candidates.append(record.get("default_signature_scheme"))
                versions = record.get("versions", {})
                if isinstance(versions, Mapping):
                    candidates.extend(
                        version.get("default_signature_scheme")
                        for version in versions.values()
                        if isinstance(version, Mapping)
                    )
        if any(value is not None and value not in _SIGNATURE_SCHEMES for value in candidates):
            raise ApiContractError("unsupported_signature_scheme")

    async def resolve(self, call: ApiCallShape, *, allow_fallback: bool) -> CanonicalWireContract:
        self._validate_call(call)
        product, candidates, product_resolution = await self._resolve_product_versions(call)
        if not candidates:
            raise ApiContractError("invalid_or_missing_version", product=product)
        version_map = _VERSION_MAP.get(product.casefold()) if call.version is None else None
        version_map_cache_status: OpenMetaCacheStatus = "miss"
        for candidate in candidates:
            metadata_fetch = await self._get_api_for_candidate(product, candidate, call.action)
            if candidate.version == version_map:
                version_map_cache_status = metadata_fetch.cache_status
            if metadata_fetch.value is not None:
                contract = self._with_product_resolution(
                    self._merge_and_validate(
                        call,
                        metadata_fetch.value,
                        metadata_fetch.source or "fresh",
                        metadata_fetch.cache_status,
                    ),
                    product_resolution,
                )
                self._schedule_api_docs_prefetch(product, candidate, candidates, call.action)
                return contract
            api_excluded = self._is_api_excluded(product, candidate.version, call.action)
            if api_excluded:
                if call.version is not None:
                    raise ApiContractError("metadata_not_found", product=product)
                continue
            if metadata_fetch.error == "not_found" and call.version is None and candidate.source != "version_map":
                continue
            if (
                metadata_fetch.error not in _ALLOWED_METADATA_ABSENCE
                or not allow_fallback
                or not self._fallback_allowed(
                    call,
                    "version_map" if candidate.version == version_map else candidate.source,
                )
            ):
                raise ApiContractError(_metadata_error_code(metadata_fetch.error), product=product)
            contract = self._with_product_resolution(
                self._fallback_contract(
                    call,
                    product,
                    candidate.version,
                    openmeta_cache_status=metadata_fetch.cache_status,
                ),
                product_resolution,
            )
            self._schedule_api_docs_prefetch(product, candidate, candidates, call.action)
            return contract
        if (
            version_map is not None
            and allow_fallback
            and self._fallback_allowed(call, "version_map")
            and not self._is_api_excluded(product, version_map, call.action)
        ):
            contract = self._with_product_resolution(
                self._fallback_contract(
                    call,
                    product,
                    version_map,
                    openmeta_cache_status=version_map_cache_status,
                ),
                product_resolution,
            )
            selected = _VersionCandidate(version_map, "version_map")
            self._schedule_api_docs_prefetch(product, selected, candidates, call.action)
            return contract
        raise ApiContractError("metadata_not_found", product=product)

    def _schedule_api_docs_prefetch(
        self,
        product: str,
        selected: _VersionCandidate,
        candidates: tuple[_VersionCandidate, ...],
        action: str,
    ) -> None:
        prefetch = getattr(self._openmeta, "prefetch_api_docs", None)
        if not callable(prefetch):
            return
        product_key = product.casefold()
        action_key = action.casefold()
        first_action = self._first_product_actions.setdefault(product_key, action_key)
        if first_action != action_key:
            self._hot_products.add(product_key)
        versions = [selected.version]
        if product_key in self._hot_products:
            versions.extend(candidate.version for candidate in candidates if candidate.version != selected.version)
        prefetch(product, tuple(versions))

    @staticmethod
    def _validate_call(call: ApiCallShape) -> None:
        if _SAFE_REGION.fullmatch(call.region_id) is None:
            raise ApiContractError("invalid_region_id")
        if call.pathname is not None:
            _validate_pathname(call.pathname)
        if not 0 < call.max_response_bytes <= _MAX_RESPONSE_BYTES:
            raise ApiContractError("invalid_max_response_bytes")

    async def _resolve_product_versions(
        self,
        call: ApiCallShape,
    ) -> tuple[str, tuple[_VersionCandidate, ...], ProductResolution]:
        product_resolution = await self._product_resolver.resolve(call.product)
        requested = product_resolution.normalized_product.casefold()
        metadata = product_resolution.metadata
        if metadata is None:
            if product_resolution.strategy == "excluded" or product_resolution.strategy.endswith("_ambiguous"):
                raise ApiContractError(
                    "product_not_found",
                    product=product_resolution.normalized_product or call.product,
                    suggestions=product_resolution.suggestions,
                )
            product = _PRODUCT_NAMES.get(requested, product_resolution.normalized_product or call.product)
            if call.version is not None:
                unverified = replace(
                    product_resolution,
                    normalized_product=product,
                    strategy="unverified",
                    confidence="none",
                )
                return (
                    product,
                    (_VersionCandidate(call.version, "caller_unverified_product", include_excluded_version=True),),
                    unverified,
                )
            if product_resolution.error not in _ALLOWED_METADATA_ABSENCE:
                raise ApiContractError(_metadata_error_code(product_resolution.error), product=product)
        else:
            product = metadata.product
        if call.version is not None:
            candidates = (_VersionCandidate(call.version, "caller", include_excluded_version=True),)
            return product, candidates, product_resolution

        candidates: list[_VersionCandidate] = []
        seen: set[str] = set()

        def add(version: str | None, source: str, *, include_excluded_version: bool = False) -> None:
            if version is None or version in seen or not is_safe_api_version(version):
                return
            seen.add(version)
            candidates.append(_VersionCandidate(version, source, include_excluded_version))

        if metadata is not None:
            add(metadata.default_version, "openmeta_default")
            for version in metadata.recommended_versions:
                add(version, "openmeta_recommended")
            for version in metadata.versions:
                add(version, "openmeta_version")
            for version in metadata.first_class_excluded_versions:
                add(version, "openmeta_excluded_first", include_excluded_version=True)
            for version in metadata.second_class_excluded_versions:
                add(version, "openmeta_excluded_second", include_excluded_version=True)
        add(_VERSION_MAP.get(product.casefold()), "version_map")
        return product, tuple(candidates), product_resolution

    @staticmethod
    def _with_product_resolution(
        contract: CanonicalWireContract,
        resolution: ProductResolution,
    ) -> CanonicalWireContract:
        return replace(
            contract,
            requested_product=resolution.requested_product,
            product_match_strategy=resolution.strategy,
            product_match_confidence=resolution.confidence,
        )

    async def _get_api_for_candidate(
        self,
        product: str,
        candidate: _VersionCandidate,
        action: str,
    ) -> MetadataFetch[ApiMetadata]:
        if candidate.include_excluded_version:
            fetch_excluded = getattr(self._openmeta, "get_api_for_version_selection", None)
            if callable(fetch_excluded):
                return await fetch_excluded(product, candidate.version, action)
        return await self._openmeta.get_api(product, candidate.version, action)

    def _is_api_excluded(self, product: str, version: str, action: str) -> bool:
        predicate = getattr(self._openmeta, "is_api_excluded", None)
        return bool(predicate(product, version, action)) if callable(predicate) else False

    @staticmethod
    def _fallback_allowed(call: ApiCallShape, version_source: str) -> bool:
        if version_source not in {"caller", "version_map"}:
            return False
        style = (call.style or "RPC").upper()
        if style == "RPC":
            return (call.method or "POST").upper() == "POST" and (call.pathname or "/") == "/"
        required = {"style", "method", "pathname"}
        return (
            style == "ROA"
            and required.issubset(call.explicit_overrides)
            and (call.method or "").upper() in _SAFE_METHODS
            and bool(call.pathname)
        )

    def _fallback_contract(
        self,
        call: ApiCallShape,
        product: str,
        version: str,
        *,
        openmeta_cache_status: str,
    ) -> CanonicalWireContract:
        style = (call.style or "RPC").upper()
        method = (call.method or "POST").upper()
        pathname = call.pathname or "/"
        signature_scheme = self._signature_scheme(product, version)
        oss_reasons = self._oss_reasons(signature_scheme, call.action, method)
        if signature_scheme == "oss_v4":
            oss_reasons = tuple(dict.fromkeys((*oss_reasons, _OSS_OPENMETA_FALLBACK_REASON)))
        oss_schema, oss_digest, oss_sdk_version = self._oss_provenance(signature_scheme)
        return CanonicalWireContract(
            metadata_source="explicit_fallback",
            product=product,
            version=version,
            action=call.action,
            style=style,
            method=method,
            pathname=pathname,
            operation_type=None,
            auth_type="AK",
            signature_scheme=signature_scheme,
            transport=_select_transport(
                signature_scheme, "json" if call.body_source != "none" else "none", "json", (), ()
            ),
            executable=not oss_reasons,
            unsupported_reasons=oss_reasons,
            parameters=(),
            consumes=(),
            produces=(),
            policy_digest=self._policy_digest,
            request_body_type="json" if call.body_source != "none" else "none",
            response_body_type="json",
            endpoint_policy_digest=self._endpoint_policy_digest,
            catalog_schema_version=self._catalog_schema_version,
            catalog_source_commit=self._catalog_source_commit,
            oss_catalog_schema_version=oss_schema,
            oss_catalog_digest=oss_digest,
            oss_sdk_version=oss_sdk_version,
            openmeta_cache_status=openmeta_cache_status,
        )

    def _merge_and_validate(
        self,
        call: ApiCallShape,
        metadata: ApiMetadata,
        metadata_source: str,
        openmeta_cache_status: str,
    ) -> CanonicalWireContract:
        style_value = call.style if "style" in call.explicit_overrides else metadata.style
        method_value = call.method if "method" in call.explicit_overrides else metadata.method
        pathname = call.pathname if "pathname" in call.explicit_overrides else metadata.pathname
        if style_value is None or method_value is None or pathname is None:
            raise ApiContractError("invalid_explicit_override")
        if "pathname" not in call.explicit_overrides:
            prefix = self._pathname_prefix_override(metadata.product, metadata.version, metadata.action)
            if prefix is not None:
                pathname = prefix.rstrip("/") + "/" + pathname.lstrip("/")
                _validate_pathname(pathname)
        style = style_value.upper()
        method = method_value.upper()
        reasons: list[str] = []
        if style not in _SAFE_STYLES:
            reasons.append("api_style_unsupported")
        if method not in _SAFE_METHODS:
            reasons.append("http_method_unsupported")
        parameters = self._apply_parameter_overrides(
            metadata.product,
            metadata.version,
            metadata.action,
            metadata.parameters,
        )
        consumes = (
            self._consumes_override(
                metadata.product,
                metadata.version,
                metadata.action,
            )
            or metadata.consumes
        )
        request_body_type = _request_body_type(parameters, consumes)
        for parameter in parameters:
            if parameter.style not in _SAFE_PARAMETER_STYLES:
                reasons.append("parameter_style_unsupported")
            if parameter.schema is None:
                reasons.append("parameter_schema_reference_unsupported")
        if not metadata.response_header_metadata_valid:
            reasons.append("response_header_metadata_invalid")
        if not metadata.response_schema_references_valid:
            reasons.append("response_schema_reference_unsupported")
        auth_type, auth_reason = _resolve_auth(metadata.security_declared, metadata.security_requirements)
        auth_type_override = self._auth_type_override(metadata.product, metadata.version, metadata.action)
        if auth_type_override is not None:
            auth_type = auth_type_override
            auth_reason = None
        if auth_reason is not None:
            reasons.append(auth_reason)
        signature_scheme = self._signature_scheme(metadata.product, metadata.version)
        reasons.extend(self._oss_reasons(signature_scheme, metadata.action, method))
        if signature_scheme == "oss_v4" and auth_type == "Anonymous":
            reasons.append("oss_v4_anonymous_unsupported")
        oss_schema, oss_digest, oss_sdk_version = self._oss_provenance(signature_scheme)
        if metadata.schemes and "HTTPS" not in metadata.schemes:
            reasons.append("https_required")
        response_body_type = metadata.response_body_type_for_method(method)
        if (
            metadata.produces
            and response_body_type != "none"
            and _preferred_accept(metadata.produces, method, response_body_type) is None
        ):
            reasons.append("response_media_type_unsupported")
        if consumes and request_body_type != "none" and _preferred_media(consumes, request_body_type) is None:
            reasons.append("request_media_type_unsupported")
        transport = _select_transport(
            signature_scheme,
            request_body_type,
            response_body_type,
            consumes,
            metadata.produces,
        )
        return CanonicalWireContract(
            metadata_source=metadata_source,
            product=metadata.product,
            version=metadata.version,
            action=metadata.action,
            style=style,
            method=method,
            pathname=pathname,
            operation_type=metadata.operation_type,
            auth_type=auth_type,
            signature_scheme=signature_scheme,
            transport=transport,
            executable=not reasons,
            unsupported_reasons=tuple(dict.fromkeys(reasons)),
            parameters=parameters,
            consumes=consumes,
            produces=metadata.produces,
            policy_digest=self._policy_digest,
            protocol="HTTPS",
            request_body_type=request_body_type,
            response_body_type=response_body_type,
            security_declared=metadata.security_declared,
            security_requirements=metadata.security_requirements,
            declared_response_headers=metadata.declared_response_headers,
            endpoint_policy_digest=self._endpoint_policy_digest,
            catalog_schema_version=self._catalog_schema_version,
            catalog_source_commit=self._catalog_source_commit,
            oss_catalog_schema_version=oss_schema,
            oss_catalog_digest=oss_digest,
            oss_sdk_version=oss_sdk_version,
            openmeta_cache_status=openmeta_cache_status,
        )

    def _oss_reasons(self, signature_scheme: str, action: str, method: str) -> tuple[str, ...]:
        if signature_scheme != "oss_v4":
            return ()
        operation = self._oss_catalog.get(action)
        if operation is None:
            return ("oss_operation_not_cataloged",)
        if operation.method is not None and operation.method != method:
            return ("oss_http_method_mismatch",)
        return () if operation.supported else tuple(operation.unsupported_reasons)

    def _oss_provenance(self, signature_scheme: str) -> tuple[int, str, str]:
        if signature_scheme != "oss_v4":
            return 0, "", ""
        return (
            int(self._oss_catalog.meta.get("schema_version", 0)),
            str(self._oss_catalog.policy_digest),
            str(self._oss_catalog.meta.get("sdk_version", "")),
        )

    def _signature_scheme(self, product: str, version: str) -> str:
        products = self._overrides.get("products", {})
        if isinstance(products, Mapping):
            for name, record in products.items():
                if isinstance(name, str) and name.casefold() == product.casefold() and isinstance(record, Mapping):
                    versions = record.get("versions", {})
                    version_record = versions.get(version) if isinstance(versions, Mapping) else None
                    if isinstance(version_record, Mapping):
                        value = version_record.get("default_signature_scheme")
                        if isinstance(value, str):
                            if value not in _SIGNATURE_SCHEMES:
                                raise ApiContractError("unsupported_signature_scheme")
                            return value
                    value = record.get("default_signature_scheme")
                    if isinstance(value, str):
                        if value not in _SIGNATURE_SCHEMES:
                            raise ApiContractError("unsupported_signature_scheme")
                        return value
        default = self._overrides.get("default_signature_scheme")
        value = default if isinstance(default, str) else "acs3"
        if value not in _SIGNATURE_SCHEMES:
            raise ApiContractError("unsupported_signature_scheme")
        return value

    def _auth_type_override(self, product: str, version: str, action: str) -> Literal["AK", "Anonymous"] | None:
        products = self._overrides.get("products", {})
        if not isinstance(products, Mapping):
            return None
        product_record: Mapping[str, Any] | None = None
        for name, record in products.items():
            if isinstance(name, str) and name.casefold() == product.casefold() and isinstance(record, Mapping):
                product_record = record
                break
        if product_record is None:
            return None
        versions = product_record.get("versions", {})
        version_record = versions.get(version, {}) if isinstance(versions, Mapping) else {}
        if not isinstance(version_record, Mapping):
            raise ApiContractError("invalid_api_overrides")
        actions = version_record.get("actions", {})
        action_record: Mapping[str, Any] = {}
        if isinstance(actions, Mapping):
            for name, record in actions.items():
                if isinstance(name, str) and name.casefold() == action.casefold() and isinstance(record, Mapping):
                    action_record = record
                    break
        elif actions:
            raise ApiContractError("invalid_api_overrides")
        for record in (action_record, version_record, product_record):
            value = record.get("auth_type")
            if value is None:
                continue
            if value not in _AUTH_TYPES:
                raise ApiContractError("invalid_api_overrides")
            return value
        return None

    def _consumes_override(self, product: str, version: str, action: str) -> tuple[str, ...] | None:
        products = self._overrides.get("products", {})
        if not isinstance(products, Mapping):
            return None
        product_record: Mapping[str, Any] | None = None
        for name, record in products.items():
            if isinstance(name, str) and name.casefold() == product.casefold() and isinstance(record, Mapping):
                product_record = record
                break
        if product_record is None:
            return None
        versions = product_record.get("versions", {})
        version_record = versions.get(version, {}) if isinstance(versions, Mapping) else {}
        if not isinstance(version_record, Mapping):
            raise ApiContractError("invalid_api_overrides")
        actions = version_record.get("actions", {})
        action_record: Mapping[str, Any] = {}
        if isinstance(actions, Mapping):
            for name, record in actions.items():
                if isinstance(name, str) and name.casefold() == action.casefold() and isinstance(record, Mapping):
                    action_record = record
                    break
        elif actions:
            raise ApiContractError("invalid_api_overrides")
        for record in (action_record, version_record, product_record):
            value = record.get("consumes")
            if value is None:
                continue
            if not isinstance(value, list) or not value or not all(isinstance(item, str) for item in value):
                raise ApiContractError("invalid_api_overrides")
            try:
                return tuple(validate_content_type(item) for item in value)
            except ApiContractError as error:
                raise ApiContractError("invalid_api_overrides") from error
        return None

    def _pathname_prefix_override(self, product: str, version: str, action: str) -> str | None:
        products = self._overrides.get("products", {})
        if not isinstance(products, Mapping):
            return None
        product_record: Mapping[str, Any] | None = None
        for name, record in products.items():
            if isinstance(name, str) and name.casefold() == product.casefold() and isinstance(record, Mapping):
                product_record = record
                break
        if product_record is None:
            return None
        versions = product_record.get("versions", {})
        version_record = versions.get(version, {}) if isinstance(versions, Mapping) else {}
        if not isinstance(version_record, Mapping):
            raise ApiContractError("invalid_api_overrides")
        actions = version_record.get("actions", {})
        action_record: Mapping[str, Any] = {}
        if isinstance(actions, Mapping):
            for name, record in actions.items():
                if isinstance(name, str) and name.casefold() == action.casefold() and isinstance(record, Mapping):
                    action_record = record
                    break
        elif actions:
            raise ApiContractError("invalid_api_overrides")
        for record in (action_record, version_record, product_record):
            value = record.get("pathname_prefix")
            if value is None:
                continue
            if not isinstance(value, str) or value == "/" or value.endswith("/"):
                raise ApiContractError("invalid_api_overrides")
            try:
                _validate_pathname(value)
            except ApiContractError as error:
                raise ApiContractError("invalid_api_overrides") from error
            return value
        return None

    def _apply_parameter_overrides(
        self,
        product: str,
        version: str,
        action: str,
        parameters: tuple[ParameterMetadata, ...],
    ) -> tuple[ParameterMetadata, ...]:
        products = self._overrides.get("products", {})
        product_record: Mapping[str, Any] | None = None
        if isinstance(products, Mapping):
            for name, record in products.items():
                if isinstance(name, str) and name.casefold() == product.casefold() and isinstance(record, Mapping):
                    product_record = record
                    break
        versions = product_record.get("versions", {}) if product_record is not None else {}
        version_record = versions.get(version, {}) if isinstance(versions, Mapping) else {}
        if not isinstance(version_record, Mapping):
            version_record = {}
        actions = version_record.get("actions", {})
        action_record: Mapping[str, Any] = {}
        if isinstance(actions, Mapping):
            for name, record in actions.items():
                if isinstance(name, str) and name.casefold() == action.casefold() and isinstance(record, Mapping):
                    action_record = record
                    break
        elif actions:
            raise ApiContractError("invalid_api_overrides")
        path_parameters = _override_mapping(version_record, "path_parameters")
        parameter_styles = _override_mapping(version_record, "parameter_styles")
        parameter_locations = _override_mapping(version_record, "parameter_locations")
        parameter_enums = _override_mapping(version_record, "parameter_enums")
        additional_parameters = _override_mapping(version_record, "additional_parameters")
        for source, target in (
            (action_record.get("path_parameters", {}) or {}, path_parameters),
            (action_record.get("parameter_styles", {}) or {}, parameter_styles),
            (action_record.get("parameter_locations", {}) or {}, parameter_locations),
            (action_record.get("parameter_enums", {}) or {}, parameter_enums),
            (action_record.get("additional_parameters", {}) or {}, additional_parameters),
        ):
            if not isinstance(source, Mapping):
                raise ApiContractError("invalid_api_overrides")
            target.update(source)
        if (
            not isinstance(path_parameters, Mapping)
            or not isinstance(parameter_styles, Mapping)
            or not isinstance(parameter_locations, Mapping)
            or not isinstance(parameter_enums, Mapping)
        ):
            raise ApiContractError("invalid_api_overrides")
        result: list[ParameterMetadata] = []
        for parameter in parameters:
            encoding = path_parameters.get(parameter.name)
            if encoding is not None and encoding not in {"segment", "preserve_slashes"}:
                raise ApiContractError("invalid_api_overrides")
            replacement = replace(parameter, path_encoding=encoding) if isinstance(encoding, str) else parameter
            if parameter.name in parameter_styles:
                style = parameter_styles[parameter.name]
                if style not in _SAFE_PARAMETER_STYLES:
                    raise ApiContractError("invalid_api_overrides")
                replacement = replace(replacement, style=style)
            if parameter.name in parameter_locations:
                location = parameter_locations[parameter.name]
                if location not in _SAFE_PARAMETER_LOCATIONS:
                    raise ApiContractError("invalid_api_overrides")
                replacement = replace(replacement, location=location)
            if parameter.name in parameter_enums:
                replacement = replace(
                    replacement,
                    schema=_schema_with_reviewed_enum(replacement.schema, parameter_enums[parameter.name]),
                )
            result.append(replacement)
        existing_names = {parameter.name.casefold() for parameter in parameters}
        for name, raw in additional_parameters.items():
            if (
                not isinstance(name, str)
                or _HEADER_TOKEN.fullmatch(name) is None
                or name.casefold() in existing_names
                or not isinstance(raw, Mapping)
            ):
                raise ApiContractError("invalid_api_overrides")
            allowed = {"location", "required", "style", "path_encoding", "schema", "description", "example"}
            if set(raw) - allowed:
                raise ApiContractError("invalid_api_overrides")
            location = raw.get("location")
            required = raw.get("required", False)
            style = raw.get("style")
            path_encoding = raw.get("path_encoding")
            schema = raw.get("schema")
            description = raw.get("description")
            if (
                location not in _SAFE_PARAMETER_LOCATIONS
                or not isinstance(required, bool)
                or style not in _SAFE_PARAMETER_STYLES
                or path_encoding not in {None, "segment", "preserve_slashes"}
                or (path_encoding is not None and location != "path")
                or not isinstance(schema, Mapping)
                or schema.get("type") not in {"string", "integer", "number", "boolean", "array", "object"}
                or (description is not None and not isinstance(description, str))
            ):
                raise ApiContractError("invalid_api_overrides")
            result.append(
                ParameterMetadata(
                    name=name,
                    location=location,
                    required=required,
                    style=style,
                    path_encoding=path_encoding,
                    schema=MappingProxyType(copy.deepcopy(dict(schema))),
                    description=description,
                    example=copy.deepcopy(raw.get("example")),
                )
            )
            existing_names.add(name.casefold())
        return tuple(result)


class RequestBuilder:
    async def build(self, contract: CanonicalWireContract, tool_input: Mapping[str, Any]) -> BuiltApiRequest:
        if not contract.executable:
            raise ApiContractError("contract_not_executable")
        normalized = copy.deepcopy(dict(tool_input))
        return await self._encode_wire_parts(contract, normalized)

    async def _encode_wire_parts(self, contract: CanonicalWireContract, tool_input: dict[str, Any]) -> BuiltApiRequest:
        _validate_pathname(contract.pathname)
        region_id = tool_input.get("region_id")
        if region_id is not None and (not isinstance(region_id, str) or _SAFE_REGION.fullmatch(region_id) is None):
            raise ApiContractError("invalid_region_id")
        params = tool_input.get("params", {})
        if not isinstance(params, Mapping):
            raise ApiContractError(
                "invalid_parameter_type:params",
                parameter="params",
                expected_type="object",
                actual_type=_json_type_name(params),
            )
        params = copy.deepcopy(dict(params))
        metadata = {parameter.name: parameter for parameter in contract.parameters}
        body_present = "body" in tool_input
        file_present = "body_file" in tool_input
        top_level_body = file_present if contract.request_body_type == "byte" else body_present or file_present
        missing = [
            "body_file" if parameter.location == "body" and contract.request_body_type == "byte" else parameter.name
            for parameter in contract.parameters
            if parameter.required
            and parameter.name not in params
            and not (parameter.location == "body" and top_level_body)
        ]
        if missing:
            raise ApiContractError("missing_required_parameters:" + ",".join(missing))
        for name, value in params.items():
            parameter = metadata.get(name)
            if parameter is not None:
                _validate_parameter(parameter, value)

        pathname = contract.pathname
        query: dict[str, str] = {}
        headers: dict[str, str] = {}
        form: dict[str, Any] = {}
        body_parameters: list[tuple[str, Any]] = []
        raw_host_values: dict[str, Any] = {}
        for name, value in params.items():
            parameter = metadata.get(name)
            location = parameter.location if parameter is not None else "query"
            if location == "path":
                assert parameter is not None
                pathname = pathname.replace(
                    "{" + name + "}",
                    _encode_path(value, parameter.path_encoding, parameter_name=parameter.name),
                )
            elif location == "header":
                assert parameter is not None
                _add_header(
                    headers,
                    parameter,
                    value,
                    allow_business_authorization=_allows_declared_business_authorization(contract, parameter),
                )
            elif location == "formData":
                assert parameter is not None
                _add_form(form, name, value, parameter)
            elif location == "body":
                body_parameters.append((name, value))
            elif location == "host":
                raw_host_values[name] = value
            else:
                _add_query(query, name, value, parameter)
        unresolved_path_parameters = tuple(dict.fromkeys(_PATH_PLACEHOLDER.findall(pathname)))
        if unresolved_path_parameters:
            raise ApiContractError(
                "unresolved_path_parameter",
                parameter=",".join(unresolved_path_parameters),
            )

        sources = int(body_present) + int(file_present) + int(bool(body_parameters)) + int(bool(form))
        if sources > 1:
            raise ApiContractError("conflicting_body_sources")
        actual_body_type = (
            "byte" if file_present else "formData" if form else "json" if body_present or body_parameters else None
        )
        if actual_body_type is not None and actual_body_type != contract.request_body_type:
            if file_present:
                raise ApiContractError("body_file_not_supported")
            raise ApiContractError("body_source_mismatch")
        content_type = tool_input.get("content_type")
        if sources == 0 and content_type is not None:
            raise ApiContractError("content_type_without_body")
        if content_type is not None:
            content_type = validate_content_type(content_type)

        body: bytes | None = None
        if file_present:
            if not _supports_binary_body(contract):
                raise ApiContractError("body_file_not_supported")
            body_file = tool_input["body_file"]
            if isinstance(body_file, bytes):
                if len(body_file) > _MAX_BODY_BYTES:
                    raise ApiContractError("body_file_too_large")
                body = body_file
            elif isinstance(body_file, str):
                body = await asyncio.to_thread(_read_body_file, Path(body_file))
            else:
                raise ApiContractError("invalid_body_file")
            _validate_content_type(content_type, "byte", contract.consumes)
            content_type = content_type or _preferred_media(contract.consumes, "byte") or "application/octet-stream"
        elif body_present:
            _validate_content_type(content_type, "json", contract.consumes)
            declared_body = [parameter for parameter in contract.parameters if parameter.location == "body"]
            if len(declared_body) == 1:
                _validate_parameter(declared_body[0], tool_input["body"])
            body = _json_bytes(tool_input["body"])
            content_type = content_type or _preferred_media(contract.consumes, "json") or "application/json"
        elif body_parameters:
            _validate_content_type(content_type, "json", contract.consumes)
            body_value = body_parameters[0][1] if len(body_parameters) == 1 else dict(body_parameters)
            body = _json_bytes(body_value)
            content_type = content_type or _preferred_media(contract.consumes, "json") or "application/json"
        elif form:
            _validate_content_type(content_type, "formData", contract.consumes)
            body = urlencode(sorted(form.items())).encode("ascii")
            content_type = content_type or "application/x-www-form-urlencoded"
        if content_type is not None:
            headers["content-type"] = validate_content_type(content_type)
        accept = _preferred_accept(contract.produces, contract.method, contract.response_body_type)
        if accept is not None:
            headers["accept"] = accept
        max_bytes = tool_input.get("max_response_bytes", _DEFAULT_RESPONSE_BYTES)
        if not isinstance(max_bytes, int) or isinstance(max_bytes, bool) or not 0 < max_bytes <= _MAX_RESPONSE_BYTES:
            raise ApiContractError("invalid_max_response_bytes")
        response_policy = _response_policy(contract, max_bytes)
        host_values = validate_host_parameter_values(contract, raw_host_values)
        return BuiltApiRequest(
            method=contract.method,
            raw_path=pathname.encode("ascii"),
            canonical_query=tuple(sorted(query.items())),
            headers=MappingProxyType(dict(sorted(headers.items()))),
            body=body,
            response_policy=response_policy,
            host_values=host_values,
        )


def validate_host_parameter_values(
    contract: CanonicalWireContract, host_values: Mapping[str, Any]
) -> Mapping[str, str]:
    parameters = {parameter.name: parameter for parameter in contract.parameters if parameter.location == "host"}
    if set(host_values) - set(parameters):
        raise ApiContractError("undeclared_host_parameter")
    normalized: dict[str, str] = {}
    for name, parameter in parameters.items():
        value = host_values.get(name)
        if value is None and not parameter.required:
            continue
        if not isinstance(value, str) or _HOST_BINDING_LABEL.fullmatch(value) is None:
            raise ApiContractError(
                "invalid_host_label",
                parameter=name,
                expected_type="string",
                actual_type=_json_type_name(value),
            )
        normalized[name] = value.casefold()
    return MappingProxyType(normalized)


def _allows_declared_business_authorization(
    contract: CanonicalWireContract,
    parameter: ParameterMetadata,
) -> bool:
    return (
        parameter.name.casefold() == "authorization"
        and contract.auth_type == "Anonymous"
        and contract.security_declared
        and bool(contract.security_requirements)
        and all(
            requirement.schemes == ("Anonymous",) and requirement.scopes == ((),)
            for requirement in contract.security_requirements
        )
    )


def _resolve_auth(
    declared: bool,
    requirements: tuple[SecurityRequirement, ...],
) -> tuple[Literal["AK", "Anonymous"], str | None]:
    if not declared:
        return "AK", None
    if not requirements:
        return "AK", "security_explicit_empty"
    for alternative in requirements:
        if alternative.schemes == ("AK",) and alternative.scopes == ((),):
            return "AK", None
    if all(alternative.schemes == ("Anonymous",) and alternative.scopes == ((),) for alternative in requirements):
        return "Anonymous", None
    if any("AK" in alternative.schemes and any(alternative.scopes) for alternative in requirements):
        return "AK", "security_scoped_ak"
    return "AK", "security_requires_unsupported_scheme"


def _select_transport(
    signature_scheme: str,
    request_body_type: str,
    response_body_type: str,
    consumes: tuple[str, ...],
    produces: tuple[str, ...],
) -> str:
    if signature_scheme not in _SIGNATURE_SCHEMES:
        raise ApiContractError("unsupported_signature_scheme")
    if signature_scheme == "acs1":
        return "acs1"
    if signature_scheme == "oss_v4":
        return "oss_v4_sdk"
    media = tuple(_normalized_media_type(value) for value in (*consumes, *produces))
    non_json = any(value is None or not _is_json_media_type(value) for value in media)
    streaming_body = request_body_type == "byte" or response_body_type in {"binary", "string"}
    return "acs3_streaming" if streaming_body or non_json else "tea"


def _validate_pathname(pathname: str) -> None:
    if (
        not isinstance(pathname, str)
        or not pathname.startswith("/")
        or pathname.startswith("//")
        or "\\" in pathname
        or any(character.isspace() or ord(character) < 32 or ord(character) == 127 for character in pathname)
        or "://" in pathname
    ):
        raise ApiContractError("invalid_pathname")


def _validate_parameter(parameter: ParameterMetadata, value: Any) -> None:
    schema = parameter.schema or {}
    _validate_parameter_schema(parameter, value, schema, depth=_MAX_PARAMETER_SCHEMA_DEPTH)


def _validate_parameter_schema(
    parameter: ParameterMetadata,
    value: Any,
    schema: Mapping[str, Any],
    *,
    depth: int,
) -> None:
    if depth <= 0 or not isinstance(schema, Mapping):
        raise ApiContractError("contract_not_executable")
    expected = schema.get("type")
    valid = {
        "string": isinstance(value, str),
        "integer": isinstance(value, int) and not isinstance(value, bool),
        "number": isinstance(value, int | float) and not isinstance(value, bool),
        "boolean": isinstance(value, bool),
        "array": isinstance(value, list | tuple),
        "object": isinstance(value, Mapping),
    }
    if expected in valid and not valid[expected]:
        raise ApiContractError(
            f"invalid_parameter_type:{parameter.name}",
            parameter=parameter.name,
            expected_type=str(expected),
            actual_type=_json_type_name(value),
        )
    enum = schema.get("enum")
    if isinstance(enum, list | tuple) and enum and not _enum_contains_value(enum, value, expected):
        raise ApiContractError(
            f"invalid_parameter_enum:{parameter.name}",
            parameter=parameter.name,
            suggestions=tuple(_wire_scalar(item) for item in enum),
        )
    if "allOf" not in schema:
        return
    branches = schema["allOf"]
    if not isinstance(branches, list | tuple):
        raise ApiContractError("contract_not_executable")
    for branch in branches:
        if not isinstance(branch, Mapping):
            raise ApiContractError("contract_not_executable")
        _validate_parameter_schema(parameter, value, branch, depth=depth - 1)


def _enum_contains_value(enum: list[Any] | tuple[Any, ...], value: Any, expected_type: Any) -> bool:
    if value in enum:
        return True
    if expected_type not in {"integer", "number", "boolean"}:
        return False
    rendered = _wire_scalar(value)
    return any(isinstance(item, str) and item == rendered for item in enum)


def _json_type_name(value: Any) -> str:
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "boolean"
    if isinstance(value, int):
        return "integer"
    if isinstance(value, float):
        return "number"
    if isinstance(value, str):
        return "string"
    if isinstance(value, list | tuple):
        return "array"
    if isinstance(value, Mapping):
        return "object"
    return "unknown"


def _add_query(query: dict[str, str], name: str, value: Any, parameter: ParameterMetadata | None) -> None:
    if name.casefold() in _SIGNATURE_QUERY_FIELDS:
        raise ApiContractError("signature_parameter_forbidden", parameter=name)
    if parameter is None and isinstance(value, Mapping | list | tuple):
        try:
            query[name] = json.dumps(
                value,
                sort_keys=True,
                separators=(",", ":"),
                ensure_ascii=False,
                allow_nan=False,
            )
        except (TypeError, ValueError) as error:
            raise ApiContractError("invalid_unknown_query_container") from error
        return
    normalized = _normalize_bools(value)
    style = parameter.style if parameter is not None else None
    schema_type = parameter.schema.get("type") if parameter is not None and parameter.schema else None
    if style == "flat":
        query.update(
            {
                str(key): _wire_scalar(item)
                for key, item in OpenApiUtil.query({name: OpenApiUtil.map_to_flat_style(normalized)}).items()
            }
        )
    elif style in {"simple", "spaceDelimited", "pipeDelimited"}:
        query[name] = OpenApiUtil.array_to_string_with_specified_style(normalized, name, style)
    elif style == "json":
        query[name] = json.dumps(normalized, separators=(",", ":"), ensure_ascii=False)
    elif style == "repeatList" or (style is None and schema_type in {"array", "object"}):
        query.update({str(key): _wire_scalar(item) for key, item in OpenApiUtil.query({name: normalized}).items()})
    else:
        query[name] = _wire_scalar(normalized)


def _add_form(form: dict[str, str], name: str, value: Any, parameter: ParameterMetadata) -> None:
    normalized = _normalize_bools(value)
    style = parameter.style
    schema_type = parameter.schema.get("type") if parameter.schema else None
    if style == "flat" and isinstance(normalized, Mapping):
        _add_flat_form_fields(form, name, normalized)
    elif style == "flat":
        form.update(
            {
                str(key): _wire_scalar(item)
                for key, item in OpenApiUtil.query({name: OpenApiUtil.map_to_flat_style(normalized)}).items()
            }
        )
    elif style in {"simple", "spaceDelimited", "pipeDelimited"}:
        form[name] = OpenApiUtil.array_to_string_with_specified_style(normalized, name, style)
    elif style == "json":
        form[name] = json.dumps(normalized, separators=(",", ":"), ensure_ascii=False)
    elif style == "repeatList" or (style is None and schema_type in {"array", "object"}):
        form.update({str(key): _wire_scalar(item) for key, item in OpenApiUtil.query({name: normalized}).items()})
    else:
        form[name] = _wire_scalar(normalized)


def _add_flat_form_fields(form: dict[str, str], prefix: str, value: Mapping[str, Any]) -> None:
    for key, item in value.items():
        field_name = f"{prefix}.{key}"
        if isinstance(item, Mapping):
            _add_flat_form_fields(form, field_name, item)
        elif isinstance(item, list | tuple):
            form.update({str(k): _wire_scalar(v) for k, v in OpenApiUtil.query({field_name: item}).items()})
        else:
            form[field_name] = _wire_scalar(item)


def _add_header(
    headers: dict[str, str],
    parameter: ParameterMetadata,
    value: Any,
    *,
    allow_business_authorization: bool = False,
) -> None:
    if "*" in parameter.name:
        if not isinstance(value, Mapping):
            raise ApiContractError(
                f"invalid_parameter_type:{parameter.name}",
                parameter=parameter.name,
                expected_type="object",
                actual_type=_json_type_name(value),
            )
        prefix, suffix = parameter.name.split("*", 1)
        for dynamic_name, dynamic_value in value.items():
            name = f"{prefix}{dynamic_name}{suffix}"
            try:
                _set_header(headers, name, dynamic_value)
            except ApiContractError as error:
                code = str(error)
                if code == "invalid_header_name":
                    raise ApiContractError(
                        "invalid_expanded_header_name",
                        parameter=parameter.name,
                        expected_type="string",
                        actual_type=_json_type_name(dynamic_name),
                    ) from error
                if code == "reserved_header_forbidden":
                    raise ApiContractError(
                        "reserved_header_forbidden",
                        parameter=parameter.name,
                        expected_type="scalar",
                        actual_type=_json_type_name(dynamic_value),
                    ) from error
                if code != "invalid_header_value":
                    raise
                if not isinstance(dynamic_value, Mapping | list | tuple) and dynamic_value is not None:
                    raise ApiContractError(
                        "invalid_header_value",
                        parameter=parameter.name,
                        expected_type="scalar",
                        actual_type=_json_type_name(dynamic_value),
                    ) from error
                raise ApiContractError(
                    f"invalid_parameter_type:{parameter.name}",
                    parameter=parameter.name,
                    expected_type="scalar",
                    actual_type=_json_type_name(dynamic_value),
                ) from error
        return
    if isinstance(value, Mapping | list | tuple) or value is None:
        raise ApiContractError(
            f"invalid_parameter_type:{parameter.name}",
            parameter=parameter.name,
            expected_type="scalar",
            actual_type=_json_type_name(value),
        )
    try:
        _set_header(
            headers,
            parameter.name,
            value,
            allow_business_authorization=allow_business_authorization,
        )
    except ApiContractError as error:
        code = str(error)
        if code == "reserved_header_forbidden":
            raise ApiContractError(
                "reserved_header_forbidden",
                parameter=parameter.name,
                expected_type="scalar",
                actual_type=_json_type_name(value),
            ) from error
        if code != "invalid_header_value":
            raise
        raise ApiContractError(
            "invalid_header_value",
            parameter=parameter.name,
            expected_type="scalar",
            actual_type=_json_type_name(value),
        ) from error


def _set_header(
    headers: dict[str, str],
    name: str,
    value: Any,
    *,
    allow_business_authorization: bool = False,
) -> None:
    lowered = name.casefold()
    if (
        (lowered in _RESERVED_HEADERS and not (allow_business_authorization and lowered == "authorization"))
        or lowered.startswith("x-acs-signature")
        or lowered.startswith("x-acs-credential")
        or "security-token" in lowered
    ):
        raise ApiContractError("reserved_header_forbidden")
    if _HEADER_TOKEN.fullmatch(name) is None:
        raise ApiContractError("invalid_header_name")
    if isinstance(value, Mapping | list | tuple) or value is None:
        raise ApiContractError("invalid_header_value")
    rendered = _wire_scalar(value)
    if "\r" in rendered or "\n" in rendered:
        raise ApiContractError("invalid_header_value")
    headers[lowered] = rendered


def _encode_path(value: Any, path_encoding: str | None, *, parameter_name: str) -> str:
    if not isinstance(value, str | int | float) or isinstance(value, bool):
        raise ApiContractError(
            "invalid_path_parameter",
            parameter=parameter_name,
            expected_type="scalar",
            actual_type=_json_type_name(value),
        )
    rendered = str(value)
    if path_encoding == "preserve_slashes":
        return "/".join(quote(segment, safe="-._~") for segment in rendered.split("/"))
    return quote(rendered, safe="-._~")


def _wire_scalar(value: Any) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if value is None:
        return ""
    return str(value)


def _normalize_bools(value: Any) -> Any:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, Mapping):
        return {str(key): _normalize_bools(item) for key, item in value.items()}
    if isinstance(value, list | tuple):
        return [_normalize_bools(item) for item in value]
    return value


def _json_bytes(value: Any) -> bytes:
    try:
        return json.dumps(value, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    except (TypeError, ValueError) as error:
        raise ApiContractError("invalid_json_body") from error


def _override_mapping(record: Mapping[str, Any], key: str) -> dict[Any, Any]:
    value = record.get(key, {}) or {}
    if not isinstance(value, Mapping):
        raise ApiContractError("invalid_api_overrides")
    return dict(value)


def _schema_with_reviewed_enum(schema: Mapping[str, Any] | None, values: Any) -> Mapping[str, Any]:
    """Attach a reviewed closed value set to a parameter whose OpenMeta schema omits ``enum``.

    OpenMeta documents some closed value sets only in prose, so an out-of-range value can only
    be rejected by the target service as an HTTP 400. A reviewed override lets the request
    builder reject it locally instead. An upstream ``enum`` always wins, so a stale override can
    never narrow the value set OpenMeta currently declares.
    """

    if (
        not isinstance(values, list)
        or not values
        or any(not isinstance(item, str | int | float | bool) for item in values)
        or len({(type(item).__name__, item) for item in values}) != len(values)
    ):
        raise ApiContractError("invalid_api_overrides")
    merged = copy.deepcopy(dict(schema)) if isinstance(schema, Mapping) else {}
    expected = merged.get("type")
    if expected is not None and any(not _schema_type_matches(expected, item) for item in values):
        raise ApiContractError("invalid_api_overrides")
    if isinstance(merged.get("enum"), list | tuple):
        return MappingProxyType(merged)
    merged["enum"] = list(values)
    return MappingProxyType(merged)


def _schema_type_matches(expected: Any, value: Any) -> bool:
    if expected == "boolean":
        return isinstance(value, bool)
    if expected == "integer":
        return isinstance(value, int) and not isinstance(value, bool)
    if expected == "number":
        return isinstance(value, int | float) and not isinstance(value, bool)
    if expected == "string":
        return isinstance(value, str)
    return False


def _request_body_type(
    parameters: tuple[ParameterMetadata, ...],
    consumes: tuple[str, ...],
) -> Literal["json", "formData", "byte", "none"]:
    if any(parameter.location == "formData" for parameter in parameters):
        return "formData"
    body = tuple(parameter for parameter in parameters if parameter.location == "body")
    if not body:
        return "none"
    if any(parameter.schema and parameter.schema.get("format") == "binary" for parameter in body):
        return "byte"
    schema_types = {
        str(parameter.schema.get("type")).casefold()
        for parameter in body
        if parameter.schema is not None and parameter.schema.get("type") is not None
    }
    if schema_types.intersection({"object", "array", "number", "integer", "boolean"}):
        return "json"
    media_types = tuple(_normalized_media_type(value) for value in consumes)
    if any(value is not None and _is_json_media_type(value) for value in media_types):
        return "json"
    if media_types and all(value != "application/x-www-form-urlencoded" for value in media_types if value is not None):
        return "byte"
    return "json"


def _preferred_media(media_types: tuple[str, ...], body_type: str) -> str | None:
    for value in media_types:
        media_type = _normalized_media_type(value)
        if media_type is None:
            continue
        if body_type == "json" and _is_json_media_type(media_type):
            return value
        if body_type == "formData" and media_type == "application/x-www-form-urlencoded":
            return value
        if (
            body_type == "byte"
            and not _is_json_media_type(media_type)
            and media_type != "application/x-www-form-urlencoded"
        ):
            return value
    return None


def _supports_binary_body(contract: CanonicalWireContract) -> bool:
    if contract.request_body_type == "byte":
        return True
    if any(
        parameter.location == "body" and parameter.schema and parameter.schema.get("format") == "binary"
        for parameter in contract.parameters
    ):
        return True
    return _preferred_media(contract.consumes, "byte") is not None


def _normalized_media_type(value: str) -> str | None:
    try:
        return validate_content_type(value).partition(";")[0]
    except ApiContractError:
        return None


def _is_json_media_type(value: str) -> bool:
    return value == "application/json" or value.endswith("+json")


def _is_xml_media_type(value: str) -> bool:
    maintype, separator, subtype = value.partition("/")
    return bool(
        separator and ((maintype, subtype) in {("application", "xml"), ("text", "xml")} or subtype.endswith("+xml"))
    )


def _validate_content_type(content_type: Any, body_type: str, consumes: tuple[str, ...]) -> None:
    if content_type is None:
        return
    lowered = validate_content_type(content_type).partition(";")[0]
    is_json = lowered == "application/json" or lowered.endswith("+json")
    is_form = lowered == "application/x-www-form-urlencoded"
    if body_type == "json" and not is_json:
        raise ApiContractError("content_type_mismatch")
    if body_type == "formData" and not is_form:
        raise ApiContractError("content_type_mismatch")
    if body_type == "byte" and (is_json or is_form):
        raise ApiContractError("content_type_mismatch")
    if body_type != "byte" and consumes:
        declared = {validate_content_type(value).partition(";")[0] for value in consumes}
        if lowered not in declared:
            raise ApiContractError("content_type_mismatch")


def _preferred_accept(media_types: tuple[str, ...], method: str, body_type: str) -> str | None:
    if method == "HEAD" or body_type == "none":
        return None
    for value in media_types:
        media_type = _normalized_media_type(value)
        if media_type is None:
            continue
        is_json = _is_json_media_type(media_type)
        is_text = media_type.startswith("text/") or _is_xml_media_type(media_type)
        if body_type == "json" and is_json:
            return value
        if body_type == "string" and is_text:
            return value
        if body_type == "binary" and not is_json and not is_text:
            return value
    return None


def _response_policy(contract: CanonicalWireContract, max_bytes: int) -> ResponseBodyPolicy:
    if contract.method == "HEAD" or contract.response_body_type == "none":
        mode: Literal["json", "text", "xml", "binary", "headers_only"] = "headers_only"
    elif contract.response_body_type == "binary":
        mode = "binary"
    elif contract.response_body_type == "string":
        selected = _preferred_accept(contract.produces, contract.method, contract.response_body_type)
        media_type = _normalized_media_type(selected) if selected is not None else "text/plain"
        mode = "xml" if media_type is not None and _is_xml_media_type(media_type) else "text"
    else:
        mode = "json"
    return ResponseBodyPolicy(
        mode=mode,
        max_bytes=max_bytes,
        declared_headers=contract.declared_response_headers,
    )


def parse_content_type(content_type: Any) -> ParsedContentType:
    if not isinstance(content_type, str) or not content_type:
        raise ApiContractError("invalid_content_type")
    if any(ord(character) < 32 or ord(character) > 126 for character in content_type):
        raise ApiContractError("invalid_content_type")
    index = 0
    maintype, index = _read_media_token(content_type, index)
    if index >= len(content_type) or content_type[index] != "/":
        raise ApiContractError("invalid_content_type")
    subtype, index = _read_media_token(content_type, index + 1)
    parameters: list[tuple[str, str, str]] = []
    parameter_names: set[str] = set()
    while index < len(content_type):
        if content_type[index] != ";":
            raise ApiContractError("invalid_content_type")
        index += 1
        while index < len(content_type) and content_type[index] == " ":
            index += 1
        name, index = _read_media_token(content_type, index)
        canonical_name = name.casefold()
        if canonical_name in parameter_names or index >= len(content_type) or content_type[index] != "=":
            raise ApiContractError("invalid_content_type")
        parameter_names.add(canonical_name)
        index += 1
        if index < len(content_type) and content_type[index] == '"':
            canonical_value, decoded_value, index = _read_quoted_media_value(content_type, index)
        else:
            canonical_value, index = _read_media_token(content_type, index)
            decoded_value = canonical_value
        parameters.append((canonical_name, canonical_value, decoded_value))
    media_type = f"{maintype.casefold()}/{subtype.casefold()}"
    canonical = media_type
    if parameters:
        canonical += "; " + "; ".join(f"{name}={value}" for name, value, _decoded in parameters)
    return ParsedContentType(
        canonical=canonical,
        media_type=media_type,
        parameters=MappingProxyType({name: decoded for name, _canonical, decoded in parameters}),
    )


def validate_content_type(content_type: Any) -> str:
    return parse_content_type(content_type).canonical


def _read_media_token(value: str, start: int) -> tuple[str, int]:
    index = start
    while index < len(value) and value[index] in _HEADER_TOKEN_CHARACTERS:
        index += 1
    if index == start:
        raise ApiContractError("invalid_content_type")
    return value[start:index], index


def _read_quoted_media_value(value: str, start: int) -> tuple[str, str, int]:
    index = start + 1
    decoded: list[str] = []
    while index < len(value):
        character = value[index]
        if character == '"':
            escaped = "".join("\\" + item if item in {'"', "\\"} else item for item in decoded)
            return f'"{escaped}"', "".join(decoded), index + 1
        if character == "\\":
            index += 1
            if index >= len(value) or not 32 <= ord(value[index]) <= 126:
                raise ApiContractError("invalid_content_type")
            character = value[index]
        elif not 32 <= ord(character) <= 126:
            raise ApiContractError("invalid_content_type")
        decoded.append(character)
        index += 1
    raise ApiContractError("invalid_content_type")


def _open_body_file_posix(path: Path) -> int:
    nofollow = getattr(os, "O_NOFOLLOW", 0)
    if not nofollow or os.open not in os.supports_dir_fd:
        raise OSError("secure body file reads are unsupported on this platform")
    absolute = Path(os.path.abspath(path))
    if not absolute.anchor or len(absolute.parts) < 2:
        raise OSError("body file path has no leaf component")
    directory_flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | nofollow | getattr(os, "O_CLOEXEC", 0)
    directory_fd = os.open(absolute.anchor, directory_flags)
    try:
        if not stat.S_ISDIR(os.fstat(directory_fd).st_mode):
            raise OSError("body file root is not a directory")
        for component in absolute.parts[1:-1]:
            next_fd = os.open(component, directory_flags, dir_fd=directory_fd)
            try:
                if not stat.S_ISDIR(os.fstat(next_fd).st_mode):
                    raise OSError("body file parent is not a directory")
            except BaseException:
                os.close(next_fd)
                raise
            os.close(directory_fd)
            directory_fd = next_fd
        file_flags = os.O_RDONLY | getattr(os, "O_BINARY", 0) | nofollow | getattr(os, "O_CLOEXEC", 0)
        return os.open(absolute.parts[-1], file_flags, dir_fd=directory_fd)
    finally:
        os.close(directory_fd)


def _normalize_windows_handle_path(path: str) -> str:
    if path.startswith("\\\\?\\UNC\\"):
        path = "\\\\" + path[8:]
    elif path.startswith("\\\\?\\"):
        path = path[4:]
    return os.path.normcase(os.path.normpath(path))


def _windows_final_path(descriptor: int) -> str:
    import ctypes
    import msvcrt
    from ctypes import wintypes

    windll_factory: Any = getattr(ctypes, "WinDLL", None)
    get_osfhandle: Any = getattr(msvcrt, "get_osfhandle", None)
    get_last_error: Callable[[], int] = getattr(ctypes, "get_last_error", lambda: 1)
    if windll_factory is None or get_osfhandle is None:
        raise OSError("secure body file reads are unsupported on this platform")
    kernel32 = windll_factory("kernel32", use_last_error=True)
    get_final_path = kernel32.GetFinalPathNameByHandleW
    get_final_path.argtypes = [wintypes.HANDLE, wintypes.LPWSTR, wintypes.DWORD, wintypes.DWORD]
    get_final_path.restype = wintypes.DWORD
    handle = get_osfhandle(descriptor)
    required = get_final_path(handle, None, 0, 0)
    if required == 0:
        raise OSError(get_last_error(), "could not inspect opened body file path")
    buffer = ctypes.create_unicode_buffer(required)
    length = get_final_path(handle, buffer, required, 0)
    if length == 0 or length >= required:
        raise OSError(get_last_error(), "could not inspect opened body file path")
    return buffer.value


def _open_body_file_windows(path: Path) -> int:
    absolute = Path(os.path.abspath(path))
    descriptor = os.open(absolute, os.O_RDONLY | getattr(os, "O_BINARY", 0))
    try:
        opened_path = _normalize_windows_handle_path(_windows_final_path(descriptor))
        expected_path = _normalize_windows_handle_path(str(absolute))
        if opened_path != expected_path:
            raise OSError("body file path traverses a symlink or reparse point")
        return descriptor
    except BaseException:
        os.close(descriptor)
        raise


def _open_body_file(path: Path) -> int:
    if os.name == "nt":
        return _open_body_file_windows(path)
    return _open_body_file_posix(path)


def _read_body_file(path: Path) -> bytes:
    try:
        descriptor = _open_body_file(path)
    except OSError as error:
        raise ApiContractError("invalid_body_file") from error
    try:
        info = os.fstat(descriptor)
        if not stat.S_ISREG(info.st_mode):
            raise ApiContractError("invalid_body_file")
        if info.st_size > _MAX_BODY_BYTES:
            raise ApiContractError("body_file_too_large")
        chunks: list[bytes] = []
        remaining = _MAX_BODY_BYTES + 1
        while remaining:
            chunk = os.read(descriptor, min(1024 * 1024, remaining))
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        data = b"".join(chunks)
        if len(data) > _MAX_BODY_BYTES:
            raise ApiContractError("body_file_too_large")
        return data
    finally:
        os.close(descriptor)


def _json_value(value: Any) -> Any:
    if is_dataclass(value) and not isinstance(value, type):
        return {item.name: _json_value(getattr(value, item.name)) for item in fields(value)}
    if isinstance(value, Mapping):
        return {str(key): _json_value(item) for key, item in value.items()}
    if isinstance(value, tuple | list):
        return [_json_value(item) for item in value]
    return value
