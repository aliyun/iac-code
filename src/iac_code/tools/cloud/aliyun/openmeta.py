"""Anonymous Alibaba Cloud OpenMeta client and immutable metadata models."""

from __future__ import annotations

import asyncio
import copy
import hashlib
import json
import logging
import os
import re
import sys
import tempfile
from collections import OrderedDict
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import MappingProxyType
from typing import Any, Generic, Literal, TypeVar, cast
from urllib.parse import quote

import httpx
import yaml

from iac_code.tools.cloud.aliyun.api_identifiers import is_safe_api_version
from iac_code.tools.cloud.aliyun.user_agent import build_user_agent
from iac_code.utils.async_lifecycle import await_task_to_completion
from iac_code.utils.file_security import ensure_private_dir

MetadataSource = Literal["fresh", "cache", "stale_cache"]
OpenMetaCacheStatus = Literal["memory_fresh", "disk_fresh", "remote", "disk_stale", "negative_hit", "miss"]
OpenMetaError = Literal["not_found", "temporarily_unavailable", "protocol_error"]
OpenMetaRequestOutcome = Literal["success", "not_found", "temporarily_unavailable", "protocol_error"]
_SchemaValidationError = Literal["invalid_reference", "depth", "type"]
_JsonPayload = Mapping[str, Any] | list[Any]
T = TypeVar("T")

_API_HOST = "api.aliyun.com"
_BASE_URL = f"https://{_API_HOST}"
_OPENMETA_EXCLUSIONS_PATH = Path(__file__).parent / "data" / "openmeta" / "exclusions.yml"
_SAFE_SEGMENT = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,127}$")
_HEADER_TOKEN = re.compile(r"^[!#$%&'*+.^_`|~0-9A-Za-z-]+$")
_SCHEMA_VERSION = 1
_MAX_SCHEMA_DEPTH = 32
_MAX_SINGLEFLIGHT = 256
_MAX_PREFETCH_CONCURRENCY = 3
_MAX_PREFETCH_TASKS = 64
_MAX_MEMORY_BYTES = 256 * 1024 * 1024
_MAX_MEMORY_ENTRIES = 1024
_MAX_API_DOCS_RESPONSE_BYTES = 32 * 1024 * 1024
_MAX_API_RESPONSE_BYTES = 8 * 1024 * 1024
_MAX_PRODUCTS_RESPONSE_BYTES = 32 * 1024 * 1024
_MAX_BACKGROUND_INFLIGHT_BYTES = 128 * 1024 * 1024
_MAX_DISK_CACHE_BYTES = 10 * 1024 * 1024 * 1024
_MAX_NEGATIVE_ENTRIES = 1024
_MAX_PREFETCH_FAILURE_ENTRIES = 256
_DISK_CLEANUP_INTERVAL = timedelta(minutes=10)
_DISK_CLEANUP_WRITE_INTERVAL = 64
_DISK_ENVELOPE_OVERHEAD_BYTES = 1024 * 1024
_PARAMETER_LOCATIONS = frozenset({"query", "path", "header", "body", "formData", "host"})
_PATH_ENCODINGS = frozenset({"segment", "preserve_slashes"})
_SCHEMA_MAP_KEYWORDS = frozenset({"$defs", "definitions", "dependentSchemas", "patternProperties", "properties"})
_SCHEMA_SINGLE_KEYWORDS = frozenset(
    {"contains", "contentSchema", "else", "if", "items", "not", "propertyNames", "then"}
)
_SCHEMA_OR_BOOL_KEYWORDS = frozenset(
    {"additionalItems", "additionalProperties", "unevaluatedItems", "unevaluatedProperties"}
)
_SCHEMA_SEQUENCE_KEYWORDS = frozenset({"allOf", "anyOf", "oneOf", "prefixItems"})
_PRODUCT_FRESH_TTL = timedelta(hours=24)
_API_FRESH_TTL = timedelta(days=7)
_STALE_TTL = timedelta(days=30)
_NEGATIVE_TTL = timedelta(minutes=10)

logger = logging.getLogger(__name__)


def utc_now() -> datetime:
    """Return a timezone-aware UTC time suitable for cache expiry checks."""
    return datetime.now(timezone.utc)


def _freeze(value: Any) -> Any:
    if isinstance(value, Mapping):
        return MappingProxyType({str(key): _freeze(item) for key, item in value.items()})
    if isinstance(value, list | tuple):
        return tuple(_freeze(item) for item in value)
    return value


def _copy_jsonish(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {str(key): _copy_jsonish(item) for key, item in value.items()}
    if isinstance(value, list | tuple):
        return [_copy_jsonish(item) for item in value]
    return copy.deepcopy(value)


def _deep_size(value: Any) -> int:
    """Estimate retained Python object bytes without following duplicate references twice."""
    seen: set[int] = set()
    pending = [value]
    total = 0
    while pending:
        item = pending.pop()
        identity = id(item)
        if identity in seen:
            continue
        seen.add(identity)
        total += sys.getsizeof(item)
        if isinstance(item, Mapping):
            pending.extend(item.keys())
            pending.extend(item.values())
        elif isinstance(item, list | tuple | set | frozenset):
            pending.extend(item)
        elif isinstance(item, _CachedPayload):
            pending.extend((item.fetched_at, item.source_url, item.payload))
    return total


def _mapping(value: Any) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else MappingProxyType({})


def _string(value: Any) -> str | None:
    return value if isinstance(value, str) and value else None


def _string_tuple(value: Any) -> tuple[str, ...]:
    return tuple(item for item in value if isinstance(item, str)) if isinstance(value, list | tuple) else ()


def _validated_versions(raw: Mapping[str, Any], field: str) -> tuple[str, ...]:
    if field not in raw:
        return ()
    value = raw[field]
    if value is None:
        return ()
    if not isinstance(value, list | tuple):
        raise ValueError("OpenMeta product has invalid version metadata")
    if any(not isinstance(item, str) for item in value):
        raise ValueError("OpenMeta product has invalid version metadata")
    return tuple(item for item in value if is_safe_api_version(item))


def _optional_product_string(raw: Mapping[str, Any], field: str) -> str | None:
    if field not in raw:
        return None
    raw_value = raw[field]
    if raw_value is None or raw_value == "":
        return None
    value = _string(raw_value)
    if value is None:
        raise ValueError("OpenMeta product has invalid string metadata")
    return value


def _optional_product_style(raw: Mapping[str, Any]) -> str | None:
    style = _optional_product_string(raw, "style")
    if style is None:
        return None
    normalized = style.upper()
    if normalized not in {"RPC", "ROA"}:
        raise ValueError("OpenMeta product has invalid style metadata")
    return normalized


def _normalized_string_values(value: Any, *, uppercase: bool = False) -> tuple[str, ...]:
    if value is None:
        return ()
    if not isinstance(value, list | tuple):
        raise ValueError("OpenMeta metadata has an invalid string list")
    normalized: list[str] = []
    for item in value:
        if not isinstance(item, str) or not item.strip():
            raise ValueError("OpenMeta metadata has an invalid string list")
        stripped = item.strip()
        normalized.append(stripped.upper() if uppercase else stripped.casefold())
    return tuple(normalized)


def _select_method(style: str, methods: tuple[str, ...], operation_type: str | None) -> str:
    if style == "RPC" and "POST" in methods:
        return "POST"
    if style == "ROA":
        preferred = {"GET", "HEAD"} if operation_type == "read" else {"POST"}
        for method in methods:
            if method in preferred:
                return method
    return methods[0]


def _log_ignored_parameter(reason: str) -> None:
    logger.debug("Ignoring malformed OpenMeta parameter: %s", reason)


@dataclass(frozen=True)
class SecurityRequirement:
    schemes: tuple[str, ...]
    scopes: tuple[tuple[str, ...], ...]


@dataclass(frozen=True)
class ParameterMetadata:
    name: str
    location: str
    required: bool
    style: str | None
    path_encoding: str | None
    schema: Mapping[str, Any] | None
    description: str | None
    example: Any


@dataclass(frozen=True)
class _SchemaValidation:
    value: Any
    error: _SchemaValidationError | None = None


@dataclass(frozen=True)
class ProductMetadata:
    product: str
    default_version: str | None
    versions: tuple[str, ...]
    documentation_url: str | None
    recommended_versions: tuple[str, ...] = ()
    style: str | None = None
    first_class_excluded_versions: tuple[str, ...] = ()
    second_class_excluded_versions: tuple[str, ...] = ()
    short_name: str | None = None

    @classmethod
    def from_openmeta(cls, raw: Mapping[str, Any]) -> ProductMetadata:
        product = _string(raw.get("product")) or _string(raw.get("code"))
        raw_default_version = raw.get("defaultVersion")
        if raw_default_version is not None and not isinstance(raw_default_version, str):
            raise ValueError("OpenMeta product has invalid default version metadata")
        default_version_value = _string(raw_default_version)
        default_version = default_version_value if is_safe_api_version(default_version_value) else None
        if product is None or _SAFE_SEGMENT.fullmatch(product) is None:
            raise ValueError("OpenMeta product has invalid core fields")
        versions = _validated_versions(raw, "versions")
        recommended_versions = _validated_versions(raw, "recommendVersions")
        style = _optional_product_style(raw)
        documentation_url = _optional_product_string(raw, "documentationUrl")
        short_name = _optional_product_string(raw, "shortName")
        return cls(
            product=product,
            default_version=default_version,
            versions=versions,
            documentation_url=documentation_url,
            recommended_versions=recommended_versions,
            style=style,
            short_name=short_name,
        )


@dataclass(frozen=True)
class OpenMetaExclusionEntry:
    reason: str
    note: str | None = None
    category: str | None = None
    observed: str | None = None
    discovered_on: str | None = None
    source: str | None = None


@dataclass(frozen=True)
class OpenMetaExclusions:
    product_entries: Mapping[str, OpenMetaExclusionEntry]
    version_entries: Mapping[tuple[str, str], OpenMetaExclusionEntry]
    api_entries: Mapping[tuple[str, str, str], OpenMetaExclusionEntry]

    @classmethod
    def empty(cls) -> OpenMetaExclusions:
        return cls(MappingProxyType({}), MappingProxyType({}), MappingProxyType({}))

    def product_entry(self, product: str) -> OpenMetaExclusionEntry | None:
        return self.product_entries.get(product.casefold())

    def version_entry(self, product: str, version: str) -> OpenMetaExclusionEntry | None:
        return self.version_entries.get((product.casefold(), version))

    def api_entry(self, product: str, version: str, action: str) -> OpenMetaExclusionEntry | None:
        return self.api_entries.get((product.casefold(), version, action.casefold()))

    def product_excluded(self, product: str) -> bool:
        return self.product_entry(product) is not None

    def version_excluded(self, product: str, version: str) -> bool:
        return self.version_entry(product, version) is not None

    def api_excluded(self, product: str, version: str, action: str) -> bool:
        return self.api_entry(product, version, action) is not None

    def version_entries_for_product(self, product: str) -> tuple[tuple[str, OpenMetaExclusionEntry], ...]:
        product_key = product.casefold()
        return tuple(
            (version, entry)
            for (entry_product, version), entry in self.version_entries.items()
            if entry_product == product_key
        )

    def filter_product(self, metadata: ProductMetadata) -> ProductMetadata | None:
        if self.product_excluded(metadata.product):
            return None
        official_versions = tuple(
            dict.fromkeys(
                (
                    *((metadata.default_version,) if metadata.default_version is not None else ()),
                    *metadata.recommended_versions,
                    *metadata.versions,
                )
            )
        )
        configured_excluded_versions = tuple(
            version for version, _entry in self.version_entries_for_product(metadata.product)
        )
        excluded_versions = tuple(
            dict.fromkeys(
                (
                    *(version for version in official_versions if version in configured_excluded_versions),
                    *sorted(
                        (version for version in configured_excluded_versions if version not in official_versions),
                        reverse=True,
                    ),
                )
            )
        )
        recommended_set = set(metadata.recommended_versions)
        official_version_count = len(official_versions)
        first_class_excluded_versions = tuple(
            version
            for version in excluded_versions
            if (official_version_count == 1 and version in official_versions) or version in recommended_set
        )
        first_class_set = set(first_class_excluded_versions)
        second_class_excluded_versions = tuple(
            version for version in excluded_versions if version not in first_class_set
        )
        versions = tuple(
            version for version in metadata.versions if not self.version_excluded(metadata.product, version)
        )
        recommended_versions = tuple(
            version
            for version in metadata.recommended_versions
            if not self.version_excluded(metadata.product, version) and (not metadata.versions or version in versions)
        )
        if metadata.versions:
            default_version = metadata.default_version if metadata.default_version in versions else None
        else:
            default_version = (
                metadata.default_version
                if metadata.default_version is not None
                and not self.version_excluded(metadata.product, metadata.default_version)
                else None
            )
        if (
            versions == metadata.versions
            and recommended_versions == metadata.recommended_versions
            and default_version == metadata.default_version
            and first_class_excluded_versions == metadata.first_class_excluded_versions
            and second_class_excluded_versions == metadata.second_class_excluded_versions
        ):
            return metadata
        return replace(
            metadata,
            default_version=default_version,
            versions=versions,
            recommended_versions=recommended_versions,
            first_class_excluded_versions=first_class_excluded_versions,
            second_class_excluded_versions=second_class_excluded_versions,
        )


@dataclass(frozen=True)
class ApiMetadata:
    product: str
    version: str
    action: str
    title: str | None
    summary: str | None
    style: str
    methods: tuple[str, ...]
    method: str
    pathname: str
    schemes: tuple[str, ...]
    consumes: tuple[str, ...]
    produces: tuple[str, ...]
    request_body_type: Literal["json", "formData", "byte", "none"]
    response_body_type: Literal["json", "string", "binary", "none"]
    operation_type: str | None
    parameters: tuple[ParameterMetadata, ...]
    document_parameters: tuple[ParameterMetadata, ...]
    security_declared: bool
    security_requirements: tuple[SecurityRequirement, ...]
    document_components: Mapping[str, Any]
    validation_components: Mapping[str, Any]
    responses: Mapping[str, Any]
    declared_response_headers: tuple[str, ...]
    response_header_metadata_valid: bool
    response_schema_references_valid: bool
    deprecated: bool
    error_codes: Mapping[str, Any]
    change_set: tuple[Any, ...]
    static_info: Mapping[str, Any]

    def response_body_type_for_method(self, method: str) -> Literal["json", "string", "binary", "none"]:
        component_schemas = _mapping(_mapping(self.document_components).get("schemas"))
        return _response_body_type(method.upper(), self.produces, self.responses, component_schemas, self.schemes)

    @classmethod
    def from_openmeta(
        cls,
        raw: Mapping[str, Any],
        document_components: Mapping[str, Any],
        validation_components: Mapping[str, Any],
        *,
        product_style: str | None = None,
    ) -> ApiMetadata:
        product = _string(raw.get("product"))
        version = _string(raw.get("version"))
        action = _string(raw.get("action"))
        pathname_value = _string(raw.get("path")) if "path" in raw else None
        explicit_style = _string(raw.get("style")) if "style" in raw else None
        style_value = explicit_style or product_style
        methods = _normalized_string_values(raw.get("methods"), uppercase=True)
        if explicit_style is None and "style" not in raw:
            if pathname_value not in {None, "/"}:
                style_value = "ROA"
            elif "path" not in raw and methods:
                style_value = "RPC"
        style = style_value.strip().upper() if style_value is not None else None
        pathname = pathname_value or "/" if style == "RPC" else pathname_value
        if (
            product is None
            or _SAFE_SEGMENT.fullmatch(product) is None
            or version is None
            or not is_safe_api_version(version)
            or action is None
            or _SAFE_SEGMENT.fullmatch(action) is None
            or style not in {"RPC", "ROA"}
            or not methods
            or pathname is None
        ):
            raise ValueError("OpenMeta API has invalid core fields")

        security_declared = "security" in raw
        requirements: list[SecurityRequirement] = []
        security = raw.get("security")
        if security_declared and isinstance(security, list):
            for alternative in security:
                if not isinstance(alternative, Mapping):
                    continue
                schemes = tuple(name for name in alternative if isinstance(name, str))
                if not schemes:
                    continue
                scopes = tuple(_string_tuple(alternative.get(scheme)) for scheme in schemes)
                requirements.append(SecurityRequirement(schemes=schemes, scopes=scopes))

        parameters: list[ParameterMetadata] = []
        document_parameters: list[ParameterMetadata] = []
        component_schemas = _mapping(_mapping(raw.get("components")).get("schemas"))
        for item in raw.get("parameters", ()) if isinstance(raw.get("parameters"), list) else ():
            if not isinstance(item, Mapping):
                _log_ignored_parameter("entry_not_object")
                continue
            name = _string(item.get("name"))
            location = _string(item.get("in"))
            if name is None or location is None:
                _log_ignored_parameter("missing_name_or_location")
                continue
            if location not in _PARAMETER_LOCATIONS:
                _log_ignored_parameter("unknown_location")
                continue
            path_encoding = _string(item.get("pathEncoding")) if "pathEncoding" in item else None
            if "pathEncoding" in item and path_encoding not in _PATH_ENCODINGS:
                _log_ignored_parameter("unknown_path_encoding")
                continue
            schema = item.get("schema")
            schema_fields = schema if isinstance(schema, Mapping) else {}
            required = item.get("required") if "required" in item else schema_fields.get("required")
            doc_required = item.get("docRequired") is True or schema_fields.get("docRequired") is True
            description = _string(item.get("description")) or _string(schema_fields.get("description"))
            example = item.get("example") if "example" in item else schema_fields.get("example")
            common = {
                "name": name,
                "location": location,
                "required": required is True or doc_required,
                "style": _string(item.get("style")),
                "path_encoding": path_encoding,
                "description": description,
                "example": _freeze(example),
            }
            parameters.append(
                ParameterMetadata(
                    **common,
                    schema=_freeze(_resolve_schema(schema, component_schemas)),
                )
            )
            document_parameters.append(
                ParameterMetadata(
                    **common,
                    schema=_freeze(schema) if isinstance(schema, Mapping) else None,
                )
            )

        schemes = _normalized_string_values(raw.get("schemes"), uppercase=True)
        consumes = _normalized_string_values(raw.get("consumes"))
        produces = _normalized_string_values(raw.get("produces"))
        operation_type_value = _string(raw.get("operationType"))
        operation_type = operation_type_value.strip().casefold() if operation_type_value is not None else None
        method = _select_method(style, methods, operation_type)
        request_body_type = _request_body_type(tuple(parameters), consumes)
        responses = _mapping(raw.get("responses"))
        response_body_type = _response_body_type(method, produces, responses, component_schemas, schemes)
        change_set = raw.get("changeSet")
        if not isinstance(change_set, list | tuple):
            change_set = ()
        declared_response_headers, response_header_metadata_valid = _response_header_metadata(raw.get("responses"))
        response_schema_references_valid = _response_references_valid(responses, component_schemas)

        return cls(
            product=product,
            version=version,
            action=action,
            title=_string(raw.get("title")),
            summary=_string(raw.get("summary")),
            style=style,
            methods=methods,
            method=method,
            pathname=pathname,
            schemes=schemes,
            consumes=consumes,
            produces=produces,
            request_body_type=request_body_type,
            response_body_type=response_body_type,
            operation_type=operation_type,
            parameters=tuple(parameters),
            document_parameters=tuple(document_parameters),
            security_declared=security_declared,
            security_requirements=tuple(requirements),
            document_components=document_components,
            validation_components=validation_components,
            responses=_freeze(responses),
            declared_response_headers=declared_response_headers,
            response_header_metadata_valid=response_header_metadata_valid,
            response_schema_references_valid=response_schema_references_valid,
            deprecated=raw.get("deprecated") is True,
            error_codes=_freeze(_mapping(raw.get("errorCodes"))),
            change_set=_freeze(change_set),
            static_info=_freeze(_mapping(raw.get("staticInfo"))),
        )


def _request_body_type(
    parameters: tuple[ParameterMetadata, ...], consumes: tuple[str, ...]
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
    media_types = tuple(_media_type(value) for value in consumes)
    if any(_is_json_media_type(value) for value in media_types):
        return "json"
    if media_types and all(value != "application/x-www-form-urlencoded" for value in media_types):
        return "byte"
    return "json"


def _media_type(value: str) -> str:
    return value.partition(";")[0].strip().casefold()


def _is_json_media_type(value: str) -> bool:
    return value == "application/json" or value.endswith("+json")


def _response_body_type(
    method: str,
    produces: tuple[str, ...],
    responses: Mapping[str, Any],
    component_schemas: Mapping[str, Any],
    schemes: tuple[str, ...] = (),
) -> Literal["json", "string", "binary", "none"]:
    if method == "HEAD":
        return "none"
    successful = _successful_responses(responses)
    if successful and all(status == 204 for status, _ in successful):
        return "none"
    if "SSE" in schemes:
        return "string"
    if _produces_xml_response(produces):
        return "string"
    for _, response in successful:
        schema = response.get("schema")
        inferred = _schema_response_body_type(schema, component_schemas)
        if inferred is not None:
            return inferred
    for media_type in produces:
        normalized_media_type = _media_type(media_type)
        if _is_json_media_type(normalized_media_type):
            return "json"
        if normalized_media_type in {"application/xml", "text/xml"} or normalized_media_type.endswith("+xml"):
            return "string"
        if normalized_media_type.startswith("text/"):
            return "string"
        return "binary"
    if successful:
        return "none"
    if not responses:
        return "json"
    return "json"


def _produces_xml_response(produces: tuple[str, ...]) -> bool:
    media_types = tuple(_media_type(value) for value in produces)
    if not media_types or any(_is_json_media_type(value) for value in media_types):
        return False
    return all(value in {"application/xml", "text/xml"} or value.endswith("+xml") for value in media_types)


def _successful_responses(responses: Mapping[str, Any]) -> tuple[tuple[int, Mapping[str, Any]], ...]:
    successful: list[tuple[int, Mapping[str, Any]]] = []
    for raw_status, response in responses.items():
        if not isinstance(response, Mapping):
            continue
        status_text = str(raw_status)
        if not status_text.isdigit():
            continue
        status = int(status_text)
        if 200 <= status < 300:
            successful.append((status, response))
    return tuple(successful)


def _schema_response_body_type(
    schema: Any,
    component_schemas: Mapping[str, Any],
) -> Literal["json", "string", "binary"] | None:
    resolved = _resolve_schema(schema, component_schemas)
    if not isinstance(resolved, Mapping):
        return None
    schema_type = resolved.get("type")
    schema_format = resolved.get("format")
    if schema_format == "binary":
        return "binary"
    if schema_type == "string":
        return "string"
    if schema_type in {"object", "array", "integer", "number", "boolean"}:
        return "json"
    return None


@dataclass(frozen=True)
class MetadataFetch(Generic[T]):
    value: T | None
    source: MetadataSource | None
    error: OpenMetaError | None
    cache_status: OpenMetaCacheStatus = "miss"


@dataclass(frozen=True)
class _CacheKey:
    resource: Literal["products", "product", "api", "api_docs"]
    product: str = ""
    version: str = ""
    action: str = ""


@dataclass(frozen=True)
class _CachedPayload:
    fetched_at: datetime
    source_url: str
    payload: Mapping[str, Any]


@dataclass(frozen=True)
class _MemoryEntry:
    cached: _CachedPayload
    weight_bytes: int


class _ByteBudget:
    """A cancellation-safe weighted semaphore for background response bodies."""

    def __init__(self, capacity: int) -> None:
        self._capacity = capacity
        self._used = 0
        self._condition = asyncio.Condition()

    @property
    def used(self) -> int:
        return self._used

    async def acquire(self, weight: int) -> None:
        if weight > self._capacity:
            raise ValueError("OpenMeta byte reservation exceeds capacity")
        async with self._condition:
            await self._condition.wait_for(lambda: self._used + weight <= self._capacity)
            self._used += weight

    async def release(self, weight: int) -> None:
        async with self._condition:
            self._used -= weight
            self._condition.notify_all()


def load_openmeta_exclusions(path: Path | None = _OPENMETA_EXCLUSIONS_PATH) -> OpenMetaExclusions:
    if path is None or not path.exists():
        return OpenMetaExclusions.empty()
    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except (OSError, yaml.YAMLError) as exc:
        raise ValueError("invalid OpenMeta exclusions") from exc
    if not isinstance(raw, Mapping):
        raise ValueError("invalid OpenMeta exclusions")
    schema_version = raw.get("schema_version", 1)
    if schema_version != 1:
        raise ValueError("invalid OpenMeta exclusions")

    products: dict[str, OpenMetaExclusionEntry] = {}
    versions: dict[tuple[str, str], OpenMetaExclusionEntry] = {}
    apis: dict[tuple[str, str, str], OpenMetaExclusionEntry] = {}

    raw_products = raw.get("products") or {}
    if not isinstance(raw_products, Mapping):
        raise ValueError("invalid OpenMeta product exclusions")
    for product, entry in raw_products.items():
        products[_exclusion_product_key(product)] = _exclusion_entry(entry)

    raw_versions = raw.get("versions") or {}
    if not isinstance(raw_versions, Mapping):
        raise ValueError("invalid OpenMeta version exclusions")
    for product, product_versions in raw_versions.items():
        product_key = _exclusion_product_key(product)
        if not isinstance(product_versions, Mapping):
            raise ValueError("invalid OpenMeta version exclusions")
        for version, entry in product_versions.items():
            versions[(product_key, _exclusion_version_key(version))] = _exclusion_entry(entry)

    raw_apis = raw.get("apis") or {}
    if not isinstance(raw_apis, Mapping):
        raise ValueError("invalid OpenMeta API exclusions")
    for product, product_versions in raw_apis.items():
        product_key = _exclusion_product_key(product)
        if not isinstance(product_versions, Mapping):
            raise ValueError("invalid OpenMeta API exclusions")
        for version, version_apis in product_versions.items():
            version_key = _exclusion_version_key(version)
            if not isinstance(version_apis, Mapping):
                raise ValueError("invalid OpenMeta API exclusions")
            for action, entry in version_apis.items():
                apis[(product_key, version_key, _exclusion_action_key(action))] = _exclusion_entry(entry)

    return OpenMetaExclusions(
        MappingProxyType(products),
        MappingProxyType(versions),
        MappingProxyType(apis),
    )


def _exclusion_entry(raw: Any) -> OpenMetaExclusionEntry:
    if not isinstance(raw, Mapping):
        raise ValueError("invalid OpenMeta exclusion entry")
    required: dict[str, str] = {}
    for field in ("reason", "category", "observed", "discovered_on", "source"):
        value = raw.get(field)
        if not isinstance(value, str) or not value.strip():
            raise ValueError("missing required OpenMeta exclusion audit field")
        required[field] = value.strip()
    note = raw.get("note")
    if note is not None:
        if not isinstance(note, str) or not note.strip():
            raise ValueError("invalid OpenMeta exclusion entry")
        note = note.strip()
    return OpenMetaExclusionEntry(
        reason=required["reason"],
        note=note,
        category=required["category"],
        observed=required["observed"],
        discovered_on=required["discovered_on"],
        source=required["source"],
    )


def _exclusion_product_key(value: Any) -> str:
    if not isinstance(value, str) or _SAFE_SEGMENT.fullmatch(value) is None:
        raise ValueError("invalid OpenMeta product exclusion")
    return value.casefold()


def _exclusion_version_key(value: Any) -> str:
    if not is_safe_api_version(value):
        raise ValueError("invalid OpenMeta version exclusion")
    return value


def _exclusion_action_key(value: Any) -> str:
    if not isinstance(value, str) or _SAFE_SEGMENT.fullmatch(value) is None:
        raise ValueError("invalid OpenMeta API exclusion")
    return value.casefold()


@dataclass
class _SingleflightEntry:
    task: asyncio.Future[Any]
    waiters: int = 0


class _Singleflight:
    """A bounded task table that isolates a refresh from caller cancellation."""

    def __init__(self) -> None:
        self._entries: OrderedDict[_CacheKey, _SingleflightEntry] = OrderedDict()
        self._capacity = asyncio.Condition()
        self._notification_tasks: set[asyncio.Task[None]] = set()
        self._closed = False
        self._close_task: asyncio.Task[None] | None = None

    @property
    def size(self) -> int:
        return len(self._entries)

    async def run(self, key: _CacheKey, factory: Callable[[], Awaitable[T]]) -> T:
        async with self._capacity:
            while True:
                if self._closed:
                    raise RuntimeError("OpenMeta client is closed")
                entry = self._entries.get(key)
                if entry is not None and entry.task.done() and entry.waiters == 0:
                    self._entries.pop(key)
                    entry = None
                if entry is not None:
                    self._entries.move_to_end(key)
                    entry.waiters += 1
                    break
                self._evict_for_admission()
                if len(self._entries) < _MAX_SINGLEFLIGHT:
                    task = asyncio.ensure_future(factory())
                    task.add_done_callback(self._schedule_capacity_notification)
                    entry = _SingleflightEntry(task=task, waiters=1)
                    self._entries[key] = entry
                    break
                await self._capacity.wait()
        try:
            return await asyncio.shield(entry.task)
        finally:
            async with self._capacity:
                entry.waiters -= 1
                self._capacity.notify_all()

    def _evict_for_admission(self) -> None:
        while len(self._entries) >= _MAX_SINGLEFLIGHT:
            idle_key = next(
                (
                    candidate_key
                    for candidate_key, candidate in self._entries.items()
                    if candidate.task.done() and candidate.waiters == 0
                ),
                None,
            )
            if idle_key is None:
                return
            self._entries.pop(idle_key)

    def _schedule_capacity_notification(self, _: asyncio.Future[Any]) -> None:
        if self._closed:
            return
        task = asyncio.create_task(self._notify_capacity())
        self._notification_tasks.add(task)
        task.add_done_callback(self._notification_tasks.discard)

    async def _notify_capacity(self) -> None:
        async with self._capacity:
            self._capacity.notify_all()

    async def aclose(self) -> None:
        task = self._close_task
        if task is None:
            self._closed = True
            task = asyncio.create_task(self._close())
            self._close_task = task
        await await_task_to_completion(task)

    async def _close(self) -> None:
        async with self._capacity:
            refreshes = [entry.task for entry in self._entries.values()]
            for task in refreshes:
                if not task.done():
                    task.cancel()
            self._capacity.notify_all()
        if refreshes:
            await asyncio.gather(*refreshes, return_exceptions=True)
        await asyncio.sleep(0)
        while self._notification_tasks:
            notifications = list(self._notification_tasks)
            for task in notifications:
                if not task.done():
                    task.cancel()
            await asyncio.gather(*notifications, return_exceptions=True)
            await asyncio.sleep(0)
        async with self._capacity:
            self._entries.clear()
            self._capacity.notify_all()


class OpenMetaClient:
    """Fetch public OpenMeta documents without involving Alibaba credentials."""

    def __init__(
        self,
        *,
        cache_dir: Path,
        clock: Callable[[], datetime] = utc_now,
        transport: httpx.AsyncBaseTransport | None = None,
        request_outcome_observer: Callable[[OpenMetaRequestOutcome], None] | None = None,
        cache_status_observer: Callable[[OpenMetaCacheStatus], None] | None = None,
        exclusions_path: Path | None = _OPENMETA_EXCLUSIONS_PATH,
    ) -> None:
        self._cache_dir = ensure_private_dir(cache_dir)
        self._clock = clock
        self._exclusions = load_openmeta_exclusions(exclusions_path)
        self._client = httpx.AsyncClient(
            follow_redirects=False,
            headers={"User-Agent": build_user_agent()},
            timeout=httpx.Timeout(connect=2.0, read=3.0, write=3.0, pool=3.0),
            transport=transport,
        )
        self._memory: OrderedDict[_CacheKey, _MemoryEntry] = OrderedDict()
        self._memory_weight_bytes = 0
        self._product_catalog_payload: Mapping[str, Any] | None = None
        self._product_catalog: tuple[ProductMetadata, ...] = ()
        self._product_catalog_weight_bytes = 0
        self._negative: OrderedDict[_CacheKey, datetime] = OrderedDict()
        self._singleflight = _Singleflight()
        self._prefetch_semaphore = asyncio.Semaphore(_MAX_PREFETCH_CONCURRENCY)
        self._background_bytes = _ByteBudget(_MAX_BACKGROUND_INFLIGHT_BYTES)
        self._decode_semaphore = asyncio.Semaphore(2)
        self._prefetch_tasks: dict[_CacheKey, asyncio.Task[None]] = {}
        self._prefetch_failures: OrderedDict[_CacheKey, datetime] = OrderedDict()
        self._disk_cleanup_last = self._clock()
        self._disk_writes_since_cleanup = 0
        self._managed_disk_bytes: int | None = None
        self._accepting = True
        self._closed = False
        self._close_task: asyncio.Task[None] | None = None
        self._request_outcome_observer = request_outcome_observer
        self._cache_status_observer = cache_status_observer

    @property
    def singleflight_size(self) -> int:
        return self._singleflight.size

    @property
    def prefetch_size(self) -> int:
        return len(self._prefetch_tasks)

    @property
    def memory_entry_count(self) -> int:
        return len(self._memory)

    @property
    def memory_weight_bytes(self) -> int:
        return self._memory_weight_bytes

    @property
    def background_inflight_bytes(self) -> int:
        return self._background_bytes.used

    async def aclose(self) -> None:
        if self._closed:
            return
        self._accepting = False
        task = self._close_task
        if task is None:
            task = asyncio.create_task(self._close())
            self._close_task = task
        try:
            await await_task_to_completion(task)
        finally:
            if task.done() and not self._closed and self._close_task is task:
                self._close_task = None

    async def _close(self) -> None:
        prefetches = list(self._prefetch_tasks.values())
        for task in prefetches:
            if not task.done():
                task.cancel()
        if prefetches:
            await asyncio.gather(*prefetches, return_exceptions=True)
        self._prefetch_tasks.clear()
        await self._singleflight.aclose()
        await self._client.aclose()
        self._memory.clear()
        self._memory_weight_bytes = 0
        self._product_catalog_payload = None
        self._product_catalog = ()
        self._product_catalog_weight_bytes = 0
        self._negative.clear()
        self._prefetch_failures.clear()
        self._closed = True

    async def get_product(self, product: str) -> MetadataFetch[ProductMetadata]:
        if not self._accepting:
            raise RuntimeError("OpenMeta client is closed")
        result = await self._get_product(product)
        if self._cache_status_observer is not None:
            self._cache_status_observer(result.cache_status)
        return result

    async def list_products(self) -> MetadataFetch[tuple[ProductMetadata, ...]]:
        if not self._accepting:
            raise RuntimeError("OpenMeta client is closed")
        products = await self._get_products(_CacheKey("products"))
        if products.value is None:
            result = MetadataFetch[tuple[ProductMetadata, ...]](
                value=None,
                source=products.source,
                error=products.error,
                cache_status=products.cache_status,
            )
        else:
            try:
                catalog = self._normalized_product_catalog(products.value)
            except ValueError:
                result = MetadataFetch[tuple[ProductMetadata, ...]](
                    value=None,
                    source=products.source,
                    error="protocol_error",
                    cache_status=products.cache_status,
                )
            else:
                result = MetadataFetch(
                    value=catalog,
                    source=products.source,
                    error=None,
                    cache_status=products.cache_status,
                )
        if self._cache_status_observer is not None:
            self._cache_status_observer(result.cache_status)
        return result

    def _normalized_product_catalog(self, payload: Mapping[str, Any]) -> tuple[ProductMetadata, ...]:
        if payload is self._product_catalog_payload:
            return self._product_catalog
        catalog: list[ProductMetadata] = []
        normalized_count = 0
        for raw in _products_from_payload(payload):
            try:
                metadata = normalize_product_metadata(raw)
            except ValueError:
                continue
            normalized_count += 1
            metadata = self._exclusions.filter_product(metadata)
            if metadata is not None:
                catalog.append(metadata)
        if normalized_count == 0:
            raise ValueError("OpenMeta product catalog has no valid products")
        normalized = tuple(catalog)
        self._cache_normalized_product_catalog(payload, normalized)
        return normalized

    async def _get_product(self, product: str) -> MetadataFetch[ProductMetadata]:
        try:
            requested = normalize_product(product)
        except ValueError:
            return MetadataFetch(value=None, source=None, error="protocol_error", cache_status="miss")
        missing_key = _CacheKey("product", requested.casefold())
        if self._exclusions.product_excluded(requested):
            self._remember_negative(missing_key)
            return MetadataFetch(value=None, source=None, error="not_found", cache_status="negative_hit")
        if self._negative_fresh(missing_key):
            return MetadataFetch(value=None, source=None, error="not_found", cache_status="negative_hit")
        key = _CacheKey("products")
        products = await self._get_products(key)
        if products.value is None:
            if products.error == "not_found":
                self._remember_negative(missing_key)
            return MetadataFetch(
                value=None,
                source=products.source,
                error=products.error,
                cache_status=products.cache_status,
            )
        for raw in _products_from_payload(products.value):
            try:
                metadata = normalize_product_metadata(raw)
            except ValueError:
                continue
            metadata = self._exclusions.filter_product(metadata)
            if metadata is None:
                continue
            if metadata.product.casefold() == requested.casefold():
                return MetadataFetch(
                    value=metadata,
                    source=products.source,
                    error=None,
                    cache_status=products.cache_status,
                )
        if products.cache_status in {"memory_fresh", "disk_fresh"}:
            refreshed = await self._singleflight.run(key, lambda: self._refresh_products_remote(key))
            if refreshed.value is None:
                if refreshed.error == "not_found":
                    self._remember_negative(missing_key)
                if refreshed.error == "temporarily_unavailable":
                    return MetadataFetch(
                        value=None,
                        source=refreshed.source,
                        error=refreshed.error,
                        cache_status=refreshed.cache_status,
                    )
                if refreshed.error != "temporarily_unavailable":
                    return MetadataFetch(
                        value=None,
                        source=refreshed.source,
                        error=refreshed.error,
                        cache_status=refreshed.cache_status,
                    )
            else:
                for raw in _products_from_payload(refreshed.value):
                    try:
                        metadata = normalize_product_metadata(raw)
                    except ValueError:
                        continue
                    metadata = self._exclusions.filter_product(metadata)
                    if metadata is None:
                        continue
                    if metadata.product.casefold() == requested.casefold():
                        return MetadataFetch(
                            value=metadata,
                            source=refreshed.source,
                            error=None,
                            cache_status=refreshed.cache_status,
                        )
                products = refreshed
        self._remember_negative(missing_key)
        return MetadataFetch(value=None, source=None, error="not_found", cache_status=products.cache_status)

    async def get_api(self, product: str, version: str, action: str) -> MetadataFetch[ApiMetadata]:
        if not self._accepting:
            raise RuntimeError("OpenMeta client is closed")
        result = await self._get_api(product, version, action, include_excluded_version=False)
        if self._cache_status_observer is not None:
            self._cache_status_observer(result.cache_status)
        return result

    async def get_api_for_version_selection(
        self,
        product: str,
        version: str,
        action: str,
    ) -> MetadataFetch[ApiMetadata]:
        if not self._accepting:
            raise RuntimeError("OpenMeta client is closed")
        result = await self._get_api(product, version, action, include_excluded_version=True)
        if self._cache_status_observer is not None:
            self._cache_status_observer(result.cache_status)
        return result

    def is_product_excluded(self, product: str) -> bool:
        return self._exclusions.product_excluded(product)

    def filter_product_metadata(self, metadata: ProductMetadata) -> ProductMetadata | None:
        """Apply configured product/version exclusions to bundled catalog metadata."""
        return self._exclusions.filter_product(metadata)

    def is_api_excluded(self, product: str, version: str, action: str) -> bool:
        return self._exclusions.api_excluded(product, version, action)

    def prefetch_api_docs(self, product: str, versions: tuple[str, ...]) -> None:
        """Schedule bounded, best-effort product-version metadata prefetches."""
        if not self._accepting:
            return
        try:
            normalized_product = normalize_product(product)
        except ValueError:
            return
        for index, version in enumerate(versions):
            try:
                normalized_version = normalize_version(version)
            except ValueError:
                continue
            key = _CacheKey("api_docs", normalized_product, normalized_version)
            if key in self._prefetch_tasks or self._memory_fresh(key, _API_FRESH_TTL) is not None:
                continue
            if self._prefetch_failure_fresh(key):
                continue
            if len(self._prefetch_tasks) >= _MAX_PREFETCH_TASKS:
                return
            task = asyncio.create_task(self._run_api_docs_prefetch(key, memory_admission=index == 0))
            self._prefetch_tasks[key] = task
            task.add_done_callback(lambda completed, item=key: self._prefetch_tasks.pop(item, None))

    async def _run_api_docs_prefetch(self, key: _CacheKey, *, memory_admission: bool) -> None:
        try:
            async with self._prefetch_semaphore:
                result = await self._get_api_docs(
                    key,
                    memory_admission=memory_admission,
                    background=True,
                )
            if result.value is None or result.cache_status == "disk_stale":
                self._remember_prefetch_failure(key)
            else:
                self._prefetch_failures.pop(key, None)
        except asyncio.CancelledError:
            raise
        except Exception:
            self._remember_prefetch_failure(key)
            logger.debug(
                "OpenMeta API docs prefetch failed: product=%s version=%s",
                key.product,
                key.version,
                exc_info=True,
            )

    async def _get_api(
        self,
        product: str,
        version: str,
        action: str,
        *,
        include_excluded_version: bool,
    ) -> MetadataFetch[ApiMetadata]:
        try:
            key = _CacheKey("api", normalize_product(product), normalize_version(version), normalize_action(action))
        except ValueError:
            return MetadataFetch(value=None, source=None, error="protocol_error", cache_status="miss")
        if self._exclusions.product_excluded(key.product) or self._exclusions.api_excluded(
            key.product, key.version, key.action
        ):
            self._remember_negative(key)
            return MetadataFetch(value=None, source=None, error="not_found", cache_status="negative_hit")
        if not include_excluded_version and self._exclusions.version_excluded(key.product, key.version):
            return MetadataFetch(value=None, source=None, error="not_found", cache_status="negative_hit")
        cached = self._memory_fresh(key, _API_FRESH_TTL)
        if cached is not None:
            return await self._api_fetch_with_version_components(
                cached.payload,
                key,
                "fresh",
                "memory_fresh",
            )
        cached = self._disk_fresh(key, _API_FRESH_TTL)
        if cached is not None:
            self._memory_put(key, cached)
            return await self._api_fetch_with_version_components(
                cached.payload,
                key,
                "cache",
                "disk_fresh",
            )
        docs_fetch = self._api_fetch_from_cached_docs(key)
        if docs_fetch is not None:
            return docs_fetch
        if self._negative_fresh(key):
            return MetadataFetch(value=None, source=None, error="not_found", cache_status="negative_hit")
        remote = await self._singleflight.run(key, lambda: self._refresh_api(key))
        if remote.value is not None:
            return remote
        if remote.error == "temporarily_unavailable":
            cached = self._disk_stale(key)
            if cached is not None:
                return await self._api_fetch_with_version_components(
                    cached.payload,
                    key,
                    "stale_cache",
                    "disk_stale",
                )
            stale_docs = self._api_fetch_from_stale_docs(key)
            if stale_docs is not None:
                return stale_docs
        return remote

    def _api_fetch_from_cached_docs(self, key: _CacheKey) -> MetadataFetch[ApiMetadata] | None:
        docs_key = _CacheKey("api_docs", key.product, key.version)
        cached = self._memory_fresh(docs_key, _API_FRESH_TTL)
        source: MetadataSource = "fresh"
        cache_status: OpenMetaCacheStatus = "memory_fresh"
        if cached is None:
            cached = self._disk_fresh(docs_key, _API_FRESH_TTL)
            if cached is None:
                return None
            self._memory_put(docs_key, cached)
            source = "cache"
            cache_status = "disk_fresh"
        return self._api_fetch_from_docs_payload(
            key,
            cached,
            source=source,
            cache_status=cache_status,
            authoritative_miss=True,
        )

    def _api_fetch_from_stale_docs(self, key: _CacheKey) -> MetadataFetch[ApiMetadata] | None:
        docs_key = _CacheKey("api_docs", key.product, key.version)
        cached = self._disk_stale(docs_key)
        if cached is None:
            return None
        return self._api_fetch_from_docs_payload(
            key,
            cached,
            source="stale_cache",
            cache_status="disk_stale",
            authoritative_miss=False,
        )

    def _api_fetch_from_docs_payload(
        self,
        key: _CacheKey,
        cached: _CachedPayload,
        *,
        source: MetadataSource,
        cache_status: OpenMetaCacheStatus,
        authoritative_miss: bool,
    ) -> MetadataFetch[ApiMetadata] | None:
        apis = cached.payload.get("apis")
        if not isinstance(apis, Mapping) or not apis:
            return None
        raw_api = apis.get(key.action)
        if raw_api is None:
            if authoritative_miss:
                return MetadataFetch(value=None, source=None, error="not_found", cache_status=cache_status)
            return None
        if not isinstance(raw_api, Mapping):
            if authoritative_miss:
                return MetadataFetch(value=None, source=None, error="protocol_error", cache_status=cache_status)
            return None
        payload = _merge_version_components(raw_api, cached.payload)
        product_style = self._product_style(key.product) or _api_docs_product_style(cached.payload)
        return _api_fetch(payload, key, source, cache_status, product_style=product_style)

    async def _api_fetch_with_version_components(
        self,
        payload: Mapping[str, Any],
        key: _CacheKey,
        source: MetadataSource,
        cache_status: OpenMetaCacheStatus,
    ) -> MetadataFetch[ApiMetadata]:
        product_style = self._product_style(key.product)
        exact = _api_fetch(payload, key, source, cache_status, product_style=product_style)
        if exact.value is not None and exact.value.response_schema_references_valid:
            return exact
        if exact.value is None and not _api_requires_version_style(payload, key, product_style):
            return exact
        docs = await self._get_api_docs(_CacheKey("api_docs", key.product, key.version))
        if docs.value is None:
            return exact
        merged = _merge_version_components(payload, docs.value)
        version_style = product_style or _api_docs_product_style(docs.value)
        enriched = _api_fetch(merged, key, source, cache_status, product_style=version_style)
        if exact.value is None:
            return enriched if enriched.value is not None else exact
        if enriched.value is not None and enriched.value.response_schema_references_valid:
            return enriched
        return exact

    async def _get_products(self, key: _CacheKey) -> MetadataFetch[Mapping[str, Any]]:
        cached = self._memory_fresh(key, _PRODUCT_FRESH_TTL)
        if cached is not None:
            return MetadataFetch(value=cached.payload, source="fresh", error=None, cache_status="memory_fresh")
        cached = self._disk_fresh(key, _PRODUCT_FRESH_TTL)
        if cached is not None:
            self._memory_put(key, cached)
            return MetadataFetch(value=cached.payload, source="cache", error=None, cache_status="disk_fresh")
        remote = await self._singleflight.run(key, lambda: self._refresh_products(key))
        if remote.value is not None:
            return remote
        if remote.error == "temporarily_unavailable":
            cached = self._disk_stale(key)
            if cached is not None:
                self._memory_put(key, cached)
                return MetadataFetch(value=cached.payload, source="stale_cache", error=None, cache_status="disk_stale")
        self._memory_pop(key)
        return remote

    async def _get_api_docs(
        self,
        key: _CacheKey,
        *,
        memory_admission: bool = True,
        background: bool = False,
    ) -> MetadataFetch[Mapping[str, Any]]:
        cached = self._memory_fresh(key, _API_FRESH_TTL)
        if cached is not None:
            return MetadataFetch(value=cached.payload, source="fresh", error=None, cache_status="memory_fresh")
        cached = self._disk_fresh(key, _API_FRESH_TTL)
        if cached is not None:
            if memory_admission:
                self._memory_put(key, cached)
            return MetadataFetch(value=cached.payload, source="cache", error=None, cache_status="disk_fresh")
        remote = await self._singleflight.run(
            key,
            lambda: self._refresh_api_docs(key, memory_admission=memory_admission, background=background),
        )
        if remote.value is not None:
            if memory_admission and self._memory_fresh(key, _API_FRESH_TTL) is None:
                promoted = self._disk_fresh(key, _API_FRESH_TTL)
                if promoted is not None:
                    self._memory_put(key, promoted)
            return remote
        if remote.error == "temporarily_unavailable":
            cached = self._disk_stale(key)
            if cached is not None:
                return MetadataFetch(value=cached.payload, source="stale_cache", error=None, cache_status="disk_stale")
        return remote

    async def _refresh_api(self, key: _CacheKey) -> MetadataFetch[ApiMetadata]:
        cached = self._memory_fresh(key, _API_FRESH_TTL)
        if cached is not None:
            return await self._api_fetch_with_version_components(
                cached.payload,
                key,
                "fresh",
                "memory_fresh",
            )
        payload, error, source_url = await self._request_json(
            _api_url(key),
            max_bytes=_MAX_API_RESPONSE_BYTES,
        )
        if error is not None:
            if error == "not_found":
                self._remember_negative(key)
            return self._record_request_outcome(
                MetadataFetch(value=None, source=None, error=error, cache_status="miss")
            )
        if not isinstance(payload, Mapping):
            return self._record_request_outcome(
                MetadataFetch(value=None, source=None, error="protocol_error", cache_status="miss")
            )
        api_payload: dict[str, Any] = dict(payload)
        normalized_fetch = await self._api_fetch_with_version_components(api_payload, key, "fresh", "remote")
        if normalized_fetch.value is None:
            return self._record_request_outcome(
                MetadataFetch(value=None, source=None, error="protocol_error", cache_status="miss")
            )
        cached = _CachedPayload(fetched_at=self._clock(), source_url=source_url, payload=api_payload)
        self._memory_put(key, cached)
        self._write_disk(key, cached)
        return self._record_request_outcome(normalized_fetch)

    async def _refresh_api_docs(
        self,
        key: _CacheKey,
        *,
        memory_admission: bool,
        background: bool,
    ) -> MetadataFetch[Mapping[str, Any]]:
        cached = self._memory_fresh(key, _API_FRESH_TTL)
        if cached is not None:
            return MetadataFetch(value=cached.payload, source="fresh", error=None, cache_status="memory_fresh")
        if background:
            await self._background_bytes.acquire(_MAX_API_DOCS_RESPONSE_BYTES)
        try:
            payload, error, source_url = await self._request_json(
                _api_docs_url(key),
                max_bytes=_MAX_API_DOCS_RESPONSE_BYTES,
            )
        finally:
            if background:
                await self._background_bytes.release(_MAX_API_DOCS_RESPONSE_BYTES)
        if error is not None:
            return MetadataFetch(value=None, source=None, error=error, cache_status="miss")
        if not isinstance(payload, Mapping):
            return MetadataFetch(value=None, source=None, error="protocol_error", cache_status="miss")
        payload_mapping = cast(Mapping[str, Any], payload)
        if not _api_docs_payload_is_valid(key, payload_mapping):
            return MetadataFetch(value=None, source=None, error="protocol_error", cache_status="miss")
        cached = _CachedPayload(fetched_at=self._clock(), source_url=source_url, payload=dict(payload_mapping))
        if memory_admission:
            self._memory_put(key, cached)
        self._write_disk(key, cached)
        return MetadataFetch(value=cached.payload, source="fresh", error=None, cache_status="remote")

    async def _refresh_products(self, key: _CacheKey) -> MetadataFetch[Mapping[str, Any]]:
        cached = self._memory_fresh(key, _PRODUCT_FRESH_TTL)
        if cached is not None:
            return MetadataFetch(value=cached.payload, source="fresh", error=None, cache_status="memory_fresh")
        return await self._refresh_products_remote(key)

    async def _refresh_products_remote(self, key: _CacheKey) -> MetadataFetch[Mapping[str, Any]]:
        payload, error, source_url = await self._request_json(
            _products_url(),
            max_bytes=_MAX_PRODUCTS_RESPONSE_BYTES,
        )
        if error is not None:
            return self._record_request_outcome(
                MetadataFetch(value=None, source=None, error=error, cache_status="miss")
            )
        normalized_payload = _normalize_products_payload(payload)
        if normalized_payload is None:
            return self._record_request_outcome(
                MetadataFetch(value=None, source=None, error="protocol_error", cache_status="miss")
            )
        cached = _CachedPayload(fetched_at=self._clock(), source_url=source_url, payload=normalized_payload)
        self._memory_put(key, cached)
        self._write_disk(key, cached)
        return self._record_request_outcome(
            MetadataFetch(value=normalized_payload, source="fresh", error=None, cache_status="remote")
        )

    def _record_request_outcome(self, result: MetadataFetch[T]) -> MetadataFetch[T]:
        if self._request_outcome_observer is not None:
            self._request_outcome_observer(result.error or "success")
        return result

    async def _request_json(
        self,
        url: str,
        *,
        max_bytes: int,
    ) -> tuple[_JsonPayload | None, OpenMetaError | None, str]:
        current = httpx.URL(url)
        for _ in range(3):
            try:
                async with self._client.stream("GET", current) as response:
                    if response.status_code in {301, 302, 303, 307, 308}:
                        location = response.headers.get("location")
                        if location is None:
                            return None, "temporarily_unavailable", str(current)
                        candidate = current.join(location)
                        if not _safe_redirect(candidate):
                            return None, "temporarily_unavailable", str(current)
                        current = candidate
                        continue
                    if response.status_code in {204, 404}:
                        return None, "not_found", str(current)
                    if response.status_code == 429 or response.status_code >= 500:
                        return None, "temporarily_unavailable", str(current)
                    if response.status_code != 200:
                        return None, "protocol_error", str(current)
                    content_length = response.headers.get("content-length")
                    if content_length is not None:
                        try:
                            if int(content_length) > max_bytes:
                                return None, "protocol_error", str(current)
                        except ValueError:
                            pass
                    chunks: list[bytes] = []
                    received = 0
                    async for chunk in response.aiter_bytes():
                        received += len(chunk)
                        if received > max_bytes:
                            return None, "protocol_error", str(current)
                        chunks.append(chunk)
            except httpx.HTTPError:
                return None, "temporarily_unavailable", str(current)
            try:
                async with self._decode_semaphore:
                    payload = await asyncio.to_thread(json.loads, b"".join(chunks))
            except (json.JSONDecodeError, UnicodeDecodeError):
                return None, "temporarily_unavailable", str(current)
            if not isinstance(payload, Mapping | list):
                return None, "protocol_error", str(current)
            if isinstance(payload, Mapping):
                semantic_error = _semantic_error(payload)
                if semantic_error is not None:
                    return None, semantic_error, str(current)
            return payload, None, str(current)
        return None, "temporarily_unavailable", str(current)

    def _memory_fresh(self, key: _CacheKey, ttl: timedelta) -> _CachedPayload | None:
        entry = self._memory.get(key)
        if entry is None:
            return None
        if self._age(entry.cached.fetched_at) > ttl:
            self._memory_pop(key)
            return None
        self._memory.move_to_end(key)
        return entry.cached

    def _memory_put(self, key: _CacheKey, cached: _CachedPayload) -> bool:
        weight = _deep_size(cached)
        self._memory_pop(key)
        if weight > _MAX_MEMORY_BYTES:
            return False
        self._memory[key] = _MemoryEntry(cached=cached, weight_bytes=weight)
        self._memory_weight_bytes += weight
        while len(self._memory) > _MAX_MEMORY_ENTRIES or self._memory_weight_bytes > _MAX_MEMORY_BYTES:
            oldest_key = next(iter(self._memory))
            self._memory_pop(oldest_key)
        return key in self._memory

    def _memory_pop(self, key: _CacheKey) -> None:
        entry = self._memory.pop(key, None)
        if entry is not None:
            self._memory_weight_bytes -= entry.weight_bytes
        if key.resource == "products":
            self._clear_normalized_product_catalog()

    def _cache_normalized_product_catalog(
        self,
        payload: Mapping[str, Any],
        catalog: tuple[ProductMetadata, ...],
    ) -> None:
        key = _CacheKey("products")
        entry = self._memory.get(key)
        if entry is None or entry.cached.payload is not payload:
            return
        self._clear_normalized_product_catalog()
        weight = _deep_size(catalog)
        if weight > _MAX_MEMORY_BYTES:
            return
        while self._memory and self._memory_weight_bytes + weight > _MAX_MEMORY_BYTES:
            oldest_key = next(iter(self._memory))
            if oldest_key == key and len(self._memory) == 1:
                return
            self._memory_pop(oldest_key)
        if key not in self._memory:
            return
        self._product_catalog_payload = payload
        self._product_catalog = catalog
        self._product_catalog_weight_bytes = weight
        self._memory_weight_bytes += weight

    def _clear_normalized_product_catalog(self) -> None:
        self._memory_weight_bytes -= self._product_catalog_weight_bytes
        self._product_catalog_payload = None
        self._product_catalog = ()
        self._product_catalog_weight_bytes = 0

    def _disk_fresh(self, key: _CacheKey, ttl: timedelta) -> _CachedPayload | None:
        cached = self._read_disk(key)
        return cached if cached is not None and self._age(cached.fetched_at) <= ttl else None

    def _disk_stale(self, key: _CacheKey) -> _CachedPayload | None:
        cached = self._read_disk(key)
        return cached if cached is not None and self._age(cached.fetched_at) <= _STALE_TTL else None

    def _negative_fresh(self, key: _CacheKey) -> bool:
        cached = self._negative.get(key)
        if cached is None:
            return False
        if self._age(cached) > _NEGATIVE_TTL:
            self._negative.pop(key, None)
            return False
        self._negative.move_to_end(key)
        return True

    def _remember_negative(self, key: _CacheKey) -> None:
        self._remember_timestamp(self._negative, key, _MAX_NEGATIVE_ENTRIES)

    def _prefetch_failure_fresh(self, key: _CacheKey) -> bool:
        failed_at = self._prefetch_failures.get(key)
        if failed_at is None:
            return False
        if self._age(failed_at) > _NEGATIVE_TTL:
            self._prefetch_failures.pop(key, None)
            return False
        self._prefetch_failures.move_to_end(key)
        return True

    def _remember_prefetch_failure(self, key: _CacheKey) -> None:
        self._remember_timestamp(self._prefetch_failures, key, _MAX_PREFETCH_FAILURE_ENTRIES)

    def _remember_timestamp(
        self,
        entries: OrderedDict[_CacheKey, datetime],
        key: _CacheKey,
        capacity: int,
    ) -> None:
        entries.pop(key, None)
        entries[key] = self._clock()
        while len(entries) > capacity:
            entries.popitem(last=False)

    def _age(self, fetched_at: datetime) -> timedelta:
        if fetched_at.tzinfo is None:
            return timedelta.max
        return max(self._clock() - fetched_at, timedelta())

    def _cache_path(self, key: _CacheKey) -> Path:
        if key.resource == "products":
            return self._cache_dir / "products.zh-cn.json"
        if key.resource == "api_docs":
            return self._cache_dir / "api-docs" / key.product / f"{key.version}.json"
        return self._cache_dir / "apis" / key.product / key.version / f"{key.action}.json"

    def _read_disk(self, key: _CacheKey) -> _CachedPayload | None:
        path = self._cache_path(key)
        try:
            if path.stat().st_size > self._disk_entry_size_limit(key):
                return None
            envelope = json.loads(path.read_text(encoding="utf-8"))
            if not isinstance(envelope, Mapping) or envelope.get("schema_version") != _SCHEMA_VERSION:
                return None
            payload = envelope.get("payload")
            fetched_at = envelope.get("fetched_at")
            source_url = envelope.get("source_url")
            checksum = envelope.get("payload_sha256")
            if not isinstance(payload, Mapping) or not isinstance(fetched_at, str) or not isinstance(source_url, str):
                return None
            if not isinstance(checksum, str) or checksum != _payload_checksum(payload):
                return None
            if not self._disk_payload_is_valid(key, payload):
                logger.debug("Ignoring structurally invalid OpenMeta disk cache: resource=%s", key.resource)
                return None
            parsed = datetime.fromisoformat(fetched_at)
            if parsed.tzinfo is None:
                return None
            return _CachedPayload(fetched_at=parsed, source_url=source_url, payload=payload)
        except (OSError, ValueError, TypeError, json.JSONDecodeError):
            return None

    def _disk_payload_is_valid(self, key: _CacheKey, payload: Mapping[str, Any]) -> bool:
        if key.resource == "products":
            return bool(_products_from_payload(payload))
        if key.resource == "api_docs":
            return _api_docs_payload_is_valid(key, payload)
        if key.resource != "api":
            return False
        try:
            metadata = normalize_api_metadata(
                payload,
                product_style=self._product_style(key.product),
                identity=(key.product, key.version, key.action),
            )
        except ValueError:
            return False
        return metadata.product == key.product and metadata.version == key.version and metadata.action == key.action

    def _product_style(self, product: str) -> str | None:
        entry = self._memory.get(_CacheKey("products"))
        if entry is None or self._age(entry.cached.fetched_at) > _STALE_TTL:
            return None
        for raw in _products_from_payload(entry.cached.payload):
            metadata = normalize_product_metadata(raw)
            if metadata.product.casefold() == product.casefold():
                return metadata.style
        return None

    @staticmethod
    def _disk_entry_size_limit(key: _CacheKey) -> int:
        if key.resource == "api":
            return _MAX_API_RESPONSE_BYTES + _DISK_ENVELOPE_OVERHEAD_BYTES
        if key.resource == "api_docs":
            return _MAX_API_DOCS_RESPONSE_BYTES + _DISK_ENVELOPE_OVERHEAD_BYTES
        return _MAX_PRODUCTS_RESPONSE_BYTES + _DISK_ENVELOPE_OVERHEAD_BYTES

    def _write_disk(self, key: _CacheKey, cached: _CachedPayload) -> None:
        path = self._cache_path(key)
        ensure_private_dir(path.parent)
        try:
            previous_size = path.stat().st_size
        except OSError:
            previous_size = 0
        payload = cached.payload
        envelope = {
            "schema_version": _SCHEMA_VERSION,
            "fetched_at": cached.fetched_at.isoformat(),
            "source_url": cached.source_url,
            "payload_sha256": _payload_checksum(payload),
            "payload": payload,
        }
        data = json.dumps(envelope, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
        fd, temporary = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
        try:
            with os.fdopen(fd, "wb") as file:
                file.write(data)
                file.flush()
                os.fsync(file.fileno())
            os.replace(temporary, path)
            _fsync_directory(path.parent)
            if key.resource in {"api", "api_docs"}:
                self._record_managed_disk_write(len(data), previous_size)
        except OSError:
            try:
                os.unlink(temporary)
            except OSError:
                pass

    def _record_managed_disk_write(self, size: int, previous_size: int) -> None:
        now = self._clock()
        self._disk_writes_since_cleanup += 1
        if self._managed_disk_bytes is None:
            self._cleanup_disk_cache(now)
            return
        self._managed_disk_bytes += size - previous_size
        if (
            self._managed_disk_bytes <= _MAX_DISK_CACHE_BYTES
            and self._disk_writes_since_cleanup < _DISK_CLEANUP_WRITE_INTERVAL
            and now - self._disk_cleanup_last < _DISK_CLEANUP_INTERVAL
        ):
            return
        self._cleanup_disk_cache(now)

    def _cleanup_disk_cache(self, now: datetime) -> None:
        self._disk_cleanup_last = now
        self._disk_writes_since_cleanup = 0
        files: list[tuple[float, int, Path]] = []
        total = 0
        stale_before = now.timestamp() - _STALE_TTL.total_seconds()
        for directory in (self._cache_dir / "apis", self._cache_dir / "api-docs"):
            if not directory.exists():
                continue
            for path in directory.rglob("*.json"):
                try:
                    stat = path.stat()
                    if stat.st_mtime < stale_before:
                        path.unlink()
                        continue
                    files.append((stat.st_mtime, stat.st_size, path))
                    total += stat.st_size
                except OSError:
                    continue
        if total <= _MAX_DISK_CACHE_BYTES:
            self._managed_disk_bytes = total
            return
        for _, size, path in sorted(files):
            try:
                path.unlink()
            except OSError:
                continue
            total -= size
            if total <= _MAX_DISK_CACHE_BYTES:
                break
        self._managed_disk_bytes = total


def normalize_product(value: str) -> str:
    if not isinstance(value, str) or not _SAFE_SEGMENT.fullmatch(value):
        raise ValueError("invalid OpenMeta product")
    return value


def normalize_version(value: str) -> str:
    if not is_safe_api_version(value):
        raise ValueError("invalid OpenMeta version")
    return value


def normalize_action(value: str) -> str:
    if not isinstance(value, str) or not _SAFE_SEGMENT.fullmatch(value):
        raise ValueError("invalid OpenMeta action")
    return value


def normalize_product_metadata(raw: Mapping[str, Any]) -> ProductMetadata:
    return ProductMetadata.from_openmeta(copy.deepcopy(raw))


def _validate_api_payload_shape(raw: Mapping[str, Any]) -> None:
    parameters = raw.get("parameters")
    if "parameters" in raw and not isinstance(parameters, list | tuple):
        raise ValueError("OpenMeta API has invalid parameters metadata")

    components = raw.get("components")
    if components is not None and not isinstance(components, Mapping):
        raise ValueError("OpenMeta API has invalid components metadata")
    if isinstance(components, Mapping):
        schemas = components.get("schemas")
        if schemas is not None and not isinstance(schemas, Mapping):
            raise ValueError("OpenMeta API has invalid component schemas metadata")

    responses = raw.get("responses")
    if "responses" in raw and not isinstance(responses, Mapping):
        raise ValueError("OpenMeta API has invalid responses metadata")
    if isinstance(responses, Mapping) and any(not isinstance(response, Mapping) for response in responses.values()):
        raise ValueError("OpenMeta API has invalid response metadata")

    error_codes = raw.get("errorCodes")
    if error_codes is not None and not isinstance(error_codes, Mapping):
        raise ValueError("OpenMeta API has invalid error code metadata")
    if isinstance(error_codes, Mapping) and any(
        not isinstance(entries, list | tuple) or any(not isinstance(entry, Mapping) for entry in entries)
        for entries in error_codes.values()
    ):
        raise ValueError("OpenMeta API has invalid error code metadata")

    change_set = raw.get("changeSet")
    if change_set is not None and (
        not isinstance(change_set, list | tuple) or any(not isinstance(item, Mapping) for item in change_set)
    ):
        raise ValueError("OpenMeta API has invalid change set metadata")

    static_info = raw.get("staticInfo")
    if static_info is not None and not isinstance(static_info, Mapping):
        raise ValueError("OpenMeta API has invalid static info metadata")

    if "security" in raw:
        security = raw.get("security")
        if not isinstance(security, list):
            raise ValueError("OpenMeta API has invalid security metadata")
        for alternative in security:
            if not isinstance(alternative, Mapping):
                raise ValueError("OpenMeta API has invalid security metadata")
            for scheme, scopes in alternative.items():
                if (
                    not isinstance(scheme, str)
                    or not isinstance(scopes, list | tuple)
                    or any(not isinstance(scope, str) for scope in scopes)
                ):
                    raise ValueError("OpenMeta API has invalid security metadata")


def _bind_api_identity(raw: dict[str, Any], identity: tuple[str, str, str]) -> None:
    expected_product = normalize_product(identity[0])
    expected_version = normalize_version(identity[1])
    expected_action = normalize_action(identity[2])
    fields = (
        ("product", expected_product, normalize_product),
        ("version", expected_version, normalize_version),
        ("action", expected_action, normalize_action),
    )
    for field, expected, validator in fields:
        if field in raw:
            try:
                actual = validator(raw[field])
            except ValueError as exc:
                raise ValueError("OpenMeta API has invalid identity metadata") from exc
            matches = actual.casefold() == expected.casefold() if field == "product" else actual == expected
            if not matches:
                raise ValueError("OpenMeta API identity does not match the request")
        raw[field] = expected


def normalize_api_metadata(
    raw: Mapping[str, Any],
    *,
    product_style: str | None = None,
    identity: tuple[str, str, str] | None = None,
) -> ApiMetadata:
    copied: dict[str, Any] = copy.deepcopy(dict(raw))
    _validate_api_payload_shape(copied)
    if identity is not None:
        _bind_api_identity(copied, identity)
    document_components, validation_components = build_schema_views(copied, max_depth=_MAX_SCHEMA_DEPTH)
    return ApiMetadata.from_openmeta(
        copied,
        document_components,
        validation_components,
        product_style=product_style,
    )


def build_schema_views(
    raw: Mapping[str, Any], *, max_depth: int = _MAX_SCHEMA_DEPTH
) -> tuple[Mapping[str, Any], Mapping[str, Any]]:
    components = _mapping(raw.get("components"))
    document_components = _freeze(components)
    schemas = _mapping(components.get("schemas"))
    validation: dict[str, Any] = {}
    for name, schema in schemas.items():
        if not isinstance(name, str):
            continue
        result = _validate_schema(schema, schemas, max_depth, (name,))
        validation[name] = result.value if result.error is None else None
    return document_components, _freeze({"schemas": validation})


def _validate_schema(
    value: Any,
    schemas: Mapping[str, Any],
    depth: int,
    stack: tuple[str, ...],
) -> _SchemaValidation:
    if depth <= 0:
        return _SchemaValidation(value=None, error="depth")
    if not isinstance(value, Mapping):
        return _SchemaValidation(value=None, error="type")
    resolved_reference: Any = None
    reference = value.get("$ref")
    if "$ref" in value:
        if not isinstance(reference, str):
            return _SchemaValidation(value=None, error="type")
        if not reference.startswith("#/components/schemas/"):
            return _SchemaValidation(value=None, error="invalid_reference")
        name = reference.removeprefix("#/components/schemas/")
        if not name or name not in schemas:
            return _SchemaValidation(value=None, error="invalid_reference")
        target = schemas[name]
        if not isinstance(target, Mapping):
            return _SchemaValidation(value=None, error="type")
        if name in stack:
            resolved_reference = {"$ref": reference}
        else:
            validated_target = _validate_schema(target, schemas, depth - 1, (*stack, name))
            if validated_target.error is not None:
                return validated_target
            resolved_reference = validated_target.value
    result: dict[str, Any] = {}
    for key, item in value.items():
        if not isinstance(key, str):
            return _SchemaValidation(value=None, error="type")
        if key == "$ref":
            continue
        if key in _SCHEMA_MAP_KEYWORDS:
            if not isinstance(item, Mapping):
                return _SchemaValidation(value=None, error="type")
            mapped: dict[str, Any] = {}
            for child_name, child_schema in item.items():
                if not isinstance(child_name, str):
                    return _SchemaValidation(value=None, error="type")
                validated = _validate_schema(child_schema, schemas, depth - 1, stack)
                if validated.error is not None:
                    return validated
                mapped[child_name] = validated.value
            result[key] = mapped
        elif key in _SCHEMA_OR_BOOL_KEYWORDS:
            if isinstance(item, bool):
                result[key] = item
                continue
            validated = _validate_schema(item, schemas, depth - 1, stack)
            if validated.error is not None:
                return validated
            result[key] = validated.value
        elif key in _SCHEMA_SINGLE_KEYWORDS:
            if key == "items" and isinstance(item, list | tuple):
                parts: list[Any] = []
                for part in item:
                    validated = _validate_schema(part, schemas, depth - 1, stack)
                    if validated.error is not None:
                        return validated
                    parts.append(validated.value)
                result[key] = parts
                continue
            validated = _validate_schema(item, schemas, depth - 1, stack)
            if validated.error is not None:
                return validated
            result[key] = validated.value
        elif key in _SCHEMA_SEQUENCE_KEYWORDS:
            if not isinstance(item, list | tuple):
                return _SchemaValidation(value=None, error="type")
            parts: list[Any] = []
            for part in item:
                validated = _validate_schema(part, schemas, depth - 1, stack)
                if validated.error is not None:
                    return validated
                parts.append(validated.value)
            result[key] = parts
        elif key == "dependencies":
            if not isinstance(item, Mapping):
                return _SchemaValidation(value=None, error="type")
            dependencies: dict[str, Any] = {}
            for dependency_name, dependency in item.items():
                if not isinstance(dependency_name, str):
                    return _SchemaValidation(value=None, error="type")
                if isinstance(dependency, list | tuple):
                    if any(not isinstance(field, str) for field in dependency):
                        return _SchemaValidation(value=None, error="type")
                    dependencies[dependency_name] = list(dependency)
                    continue
                validated = _validate_schema(dependency, schemas, depth - 1, stack)
                if validated.error is not None:
                    return validated
                dependencies[dependency_name] = validated.value
            result[key] = dependencies
        else:
            result[key] = _copy_jsonish(item)
    if "$ref" in value:
        if not result:
            return _SchemaValidation(value=resolved_reference)
        if not isinstance(resolved_reference, Mapping):
            return _SchemaValidation(value=None, error="type")
        combined: dict[str, Any] = _copy_jsonish(resolved_reference)
        # Preserve target hints used by execution while applying JSON Schema siblings conjunctively.
        if "allOf" in combined:
            target_all_of = combined["allOf"]
            if not isinstance(target_all_of, list | tuple):
                return _SchemaValidation(value=None, error="type")
            combined["allOf"] = [*target_all_of, result]
        else:
            combined["allOf"] = [result]
        return _SchemaValidation(value=combined)
    return _SchemaValidation(value=result)


def _resolve_schema(schema: Any, component_schemas: Mapping[str, Any]) -> Any:
    if not isinstance(schema, Mapping):
        return None
    result = _validate_schema(schema, component_schemas, _MAX_SCHEMA_DEPTH, ())
    if result.error is not None:
        return None
    return result.value


def _response_references_valid(
    value: Any,
    component_schemas: Mapping[str, Any],
    depth: int = _MAX_SCHEMA_DEPTH,
    stack: tuple[str, ...] = (),
) -> bool:
    if depth <= 0:
        return False
    if isinstance(value, Mapping):
        if "$ref" in value:
            return _validate_schema(value, component_schemas, depth, stack).error is None
        return all(_response_references_valid(item, component_schemas, depth - 1, stack) for item in value.values())
    if isinstance(value, list | tuple):
        return all(_response_references_valid(item, component_schemas, depth - 1, stack) for item in value)
    return True


def _response_header_metadata(value: Any) -> tuple[tuple[str, ...], bool]:
    if value is None:
        return (), True
    if not isinstance(value, Mapping):
        return (), False
    names: set[str] = set()
    for response in value.values():
        if not isinstance(response, Mapping):
            return (), False
        if "headers" not in response:
            continue
        headers = response.get("headers")
        if not isinstance(headers, Mapping):
            return (), False
        for name, declaration in headers.items():
            if (
                not isinstance(name, str)
                or _HEADER_TOKEN.fullmatch(name) is None
                or not isinstance(declaration, Mapping)
            ):
                return (), False
            names.add(name.casefold())
    return tuple(sorted(names)), True


def _products_url() -> str:
    return f"{_BASE_URL}/meta/v1/products.json?language=ZH_CN"


def _api_url(key: _CacheKey) -> str:
    segments = (quote(key.product, safe=""), quote(key.version, safe=""), quote(key.action, safe=""))
    return (
        f"{_BASE_URL}/meta/v1/products/{segments[0]}/versions/{segments[1]}/apis/{segments[2]}/api.json?language=ZH_CN"
    )


def _api_docs_url(key: _CacheKey) -> str:
    segments = (quote(key.product, safe=""), quote(key.version, safe=""))
    return f"{_BASE_URL}/meta/v1/products/{segments[0]}/versions/{segments[1]}/api-docs.json?language=ZH_CN"


def _safe_redirect(url: httpx.URL) -> bool:
    return url.scheme == "https" and url.host == _API_HOST and url.port in {None, 443} and url.userinfo == b""


def _semantic_error(payload: Mapping[str, Any]) -> OpenMetaError | None:
    message = payload.get("message")
    lowered = message.casefold() if isinstance(message, str) else ""
    if "api not found" in lowered or (payload.get("code") == 1 and "product not found" in lowered):
        return "not_found"
    if "message" in payload or ("code" in payload and payload.get("code") not in (0, "0", None)):
        return "protocol_error"
    return None


def _products_from_payload(payload: Mapping[str, Any]) -> tuple[Mapping[str, Any], ...]:
    products = payload.get("products")
    if not isinstance(products, list) or not products:
        return ()
    normalized: list[Mapping[str, Any]] = []
    for item in products:
        if not isinstance(item, Mapping):
            return ()
        try:
            normalize_product_metadata(item)
        except ValueError:
            return ()
        normalized.append(item)
    return tuple(normalized)


def _normalize_products_payload(payload: _JsonPayload | None) -> Mapping[str, Any] | None:
    if isinstance(payload, list):
        normalized: Mapping[str, Any] = {"products": payload}
    elif isinstance(payload, Mapping):
        normalized = payload
    else:
        return None
    return normalized if _products_from_payload(normalized) else None


def _api_fetch(
    payload: Mapping[str, Any],
    key: _CacheKey,
    source: MetadataSource,
    cache_status: OpenMetaCacheStatus,
    *,
    product_style: str | None = None,
) -> MetadataFetch[ApiMetadata]:
    try:
        return MetadataFetch(
            value=normalize_api_metadata(
                payload,
                product_style=product_style,
                identity=(key.product, key.version, key.action),
            ),
            source=source,
            error=None,
            cache_status=cache_status,
        )
    except ValueError:
        return MetadataFetch(value=None, source=None, error="protocol_error", cache_status=cache_status)


def _api_requires_version_style(payload: Mapping[str, Any], key: _CacheKey, product_style: str | None) -> bool:
    if product_style is not None or "style" in payload:
        return False
    return all(
        _api_fetch(payload, key, "fresh", "miss", product_style=style).value is not None for style in ("RPC", "ROA")
    )


def _payload_checksum(payload: Mapping[str, Any]) -> str:
    data = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(data).hexdigest()


def _api_docs_payload_is_valid(key: _CacheKey, payload: Mapping[str, Any]) -> bool:
    info = payload.get("info")
    if info is not None and not isinstance(info, Mapping):
        return False
    if isinstance(info, Mapping):
        product = info.get("product")
        version = info.get("version")
        style = info.get("style")
        if product is not None and not isinstance(product, str):
            return False
        if version is not None and not isinstance(version, str):
            return False
        if style is not None and (not isinstance(style, str) or style.upper() not in {"RPC", "ROA"}):
            return False
        if isinstance(product, str) and product.casefold() != key.product.casefold():
            return False
        if isinstance(version, str) and version != key.version:
            return False
    components = payload.get("components")
    if components is not None and not isinstance(components, Mapping):
        return False
    if isinstance(components, Mapping):
        schemas = components.get("schemas")
        if schemas is not None and not isinstance(schemas, Mapping):
            return False
    apis = payload.get("apis")
    return apis is None or isinstance(apis, Mapping)


def _api_docs_product_style(payload: Mapping[str, Any]) -> str | None:
    info = payload.get("info")
    if not isinstance(info, Mapping):
        return None
    style = info.get("style")
    normalized = style.upper() if isinstance(style, str) else None
    return normalized if normalized in {"RPC", "ROA"} else None


def _merge_version_components(api_payload: Mapping[str, Any], api_docs_payload: Mapping[str, Any]) -> Mapping[str, Any]:
    version_components = api_docs_payload.get("components")
    if not isinstance(version_components, Mapping):
        return api_payload
    version_schemas = version_components.get("schemas")
    if not isinstance(version_schemas, Mapping):
        return api_payload

    merged: dict[str, Any] = copy.deepcopy(dict(api_payload))
    exact_components = merged.get("components")
    exact_component_map = exact_components if isinstance(exact_components, Mapping) else {}
    exact_schemas = exact_component_map.get("schemas") if isinstance(exact_component_map, Mapping) else None

    components = copy.deepcopy(dict(version_components))
    schemas = copy.deepcopy(dict(version_schemas))
    if isinstance(exact_schemas, Mapping):
        schemas.update(copy.deepcopy(dict(exact_schemas)))
    components["schemas"] = schemas
    merged["components"] = components
    return merged


def _fsync_directory(path: Path) -> None:
    try:
        fd = os.open(path, os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(fd)
    except OSError:
        pass
    finally:
        os.close(fd)
