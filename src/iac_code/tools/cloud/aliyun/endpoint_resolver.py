"""Reviewed endpoint, Location discovery, and host binding resolution."""

from __future__ import annotations

import hashlib
import json
import os
import re
import tempfile
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from string import Formatter
from types import MappingProxyType
from typing import Any, Literal, Protocol

import yaml

from iac_code.tools.cloud.aliyun.api_contract import (
    ApiContractError,
    CanonicalWireContract,
    validate_host_parameter_values,
)
from iac_code.utils.file_security import ensure_private_dir
from iac_code.utils.state_io import fsync_parent_dir

_ENDPOINT_DATA_DIR = Path(__file__).parent / "data" / "endpoints"
_CATALOG_PATH = _ENDPOINT_DATA_DIR / "catalog.json"
_UNAVAILABLE_PATH = _ENDPOINT_DATA_DIR / "unavailable.json"
_OVERRIDES_PATH = _ENDPOINT_DATA_DIR / "overrides.yml"
_REGION = re.compile(r"^[a-z0-9][a-z0-9-]{0,62}$")
_HOST_LABEL = re.compile(r"^[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?$")
_ACCOUNT_ID = re.compile(r"^[0-9]{1,32}$")
_FRESH_TTL = timedelta(days=7)
_STALE_TTL = timedelta(days=30)
_EMPTY_TTL = timedelta(minutes=10)
_CACHE_SCHEMA = 1
_LOCATION_SOURCE_URL = "https://location.aliyuncs.com/"


class EndpointResolutionError(ValueError):
    """Stable endpoint or host binding validation error."""


@dataclass(frozen=True)
class EndpointResolution:
    endpoint: str
    source: Literal["explicit", "override", "location", "catalog_region", "catalog_global", "override_pattern"]
    host_template: str | None
    expected_host: str | None = None
    region_id: str | None = None

    @property
    def wire_endpoint(self) -> str:
        return self.expected_host or self.endpoint


@dataclass(frozen=True)
class EffectiveEndpointRecord:
    location_service_code: str | None
    region_overrides: Mapping[str, str]
    regional: Mapping[str, str]
    global_endpoint: str | None
    regional_endpoint_pattern: str | None
    blocked_pattern_regions: frozenset[str]
    host_template: str | None
    account_id_host_template: str | None


@dataclass(frozen=True)
class _LocationCacheEntry:
    endpoint: str | None
    fetched_at: datetime


class LocationProvider(Protocol):
    async def resolve(
        self, product: str, version: str, region_id: str, service_code: str, credential: Any
    ) -> str | None: ...


class AccountIdentityProvider(Protocol):
    async def resolve(self, credential: Any, region_id: str) -> str: ...


class AccountIdentityResolver:
    """Resolve the caller account for services whose public host is account-scoped."""

    def __init__(self, request: Callable[[str, Any], Awaitable[Mapping[str, Any]]] | None = None) -> None:
        self._request = request or _call_identity_api
        self._account_id: str | None = None

    async def resolve(self, credential: Any, region_id: str) -> str:
        if self._account_id is not None:
            return self._account_id
        response = await self._request(
            "sts.aliyuncs.com",
            {
                "credential": credential,
                "action": "GetCallerIdentity",
                "version": "2015-04-01",
                "region_id": region_id,
            },
        )
        account_id = response.get("AccountId")
        if not isinstance(account_id, str) or _ACCOUNT_ID.fullmatch(account_id) is None:
            raise EndpointResolutionError("account_id_discovery_failed")
        self._account_id = account_id
        return account_id


class LocationResolver:
    """Adapter for the fixed public Location endpoint."""

    def __init__(self, request: Callable[[str, Any], Awaitable[Mapping[str, Any]]] | None = None) -> None:
        self._request = request or _call_location_api

    async def resolve(
        self, product: str, version: str, region_id: str, service_code: str, credential: Any
    ) -> str | None:
        del product, version
        response = await self._request(
            "location.aliyuncs.com",
            {
                "credential": credential,
                "action": "DescribeEndpoints",
                "version": "2015-06-12",
                "region_id": region_id,
                "service_code": service_code,
            },
        )
        endpoints = response.get("Endpoints", {})
        entries = endpoints.get("Endpoint", ()) if isinstance(endpoints, Mapping) else ()
        if isinstance(entries, list | tuple):
            for entry in entries:
                if isinstance(entry, Mapping) and entry.get("Type") == "openAPI":
                    endpoint = entry.get("Endpoint")
                    if isinstance(endpoint, str) and endpoint:
                        return endpoint
        return None


def _discovery_config_values(host: str, credential: Any, region_id: str) -> dict[str, Any]:
    """Build the shared Tea config for Location and identity discovery calls.

    Dynamic modes (`RamRoleArn`, `EcsRamRole`) get the credential runtime's shared
    client; static modes keep the existing inline AK/STS values.
    """
    from iac_code.services.providers.aliyun_credentials_runtime import aliyun_credential_runtime
    from iac_code.tools.cloud.aliyun.user_agent import build_user_agent

    config_values: dict[str, Any] = {
        "endpoint": host,
        "region_id": region_id,
        "user_agent": build_user_agent(),
    }
    dynamic_client = aliyun_credential_runtime().sdk_client(credential)
    if dynamic_client is not None:
        config_values["credential"] = dynamic_client
        return config_values
    config_values["access_key_id"] = getattr(credential, "access_key_id", "")
    config_values["access_key_secret"] = getattr(credential, "access_key_secret", "")
    if getattr(credential, "mode", "AK") in {"StsToken", "OAuth"}:
        config_values["security_token"] = getattr(credential, "sts_token", "")
    return config_values


async def _call_location_api(host: str, request: Mapping[str, Any]) -> Mapping[str, Any]:
    from alibabacloud_tea_openapi import models as open_api_models
    from alibabacloud_tea_openapi.client import Client as OpenApiClient
    from darabonba.runtime import RuntimeOptions

    region_id = str(request["region_id"])
    config_values = _discovery_config_values(host, request["credential"], region_id)
    client = OpenApiClient(open_api_models.Config(**config_values))
    params = open_api_models.Params(
        action=str(request["action"]),
        version=str(request["version"]),
        protocol="HTTPS",
        pathname="/",
        method="POST",
        auth_type="AK",
        style="RPC",
        body_type="json",
        req_body_type="json",
    )
    api_request = open_api_models.OpenApiRequest(query={"Id": region_id, "ServiceCode": str(request["service_code"])})
    result = await client.call_api_async(params, api_request, RuntimeOptions())
    body = result.get("body", result)
    return body if isinstance(body, Mapping) else {}


async def _call_identity_api(host: str, request: Mapping[str, Any]) -> Mapping[str, Any]:
    from alibabacloud_tea_openapi import models as open_api_models
    from alibabacloud_tea_openapi.client import Client as OpenApiClient
    from darabonba.runtime import RuntimeOptions

    config_values = _discovery_config_values(host, request["credential"], str(request["region_id"]))
    client = OpenApiClient(open_api_models.Config(**config_values))
    params = open_api_models.Params(
        action=str(request["action"]),
        version=str(request["version"]),
        protocol="HTTPS",
        pathname="/",
        method="POST",
        auth_type="AK",
        style="RPC",
        body_type="json",
        req_body_type="json",
    )
    result = await client.call_api_async(params, open_api_models.OpenApiRequest(), RuntimeOptions())
    body = result.get("body", result)
    return body if isinstance(body, Mapping) else {}


class EndpointResolver:
    def __init__(
        self,
        *,
        cache_dir: Path,
        location: LocationProvider | None = None,
        account_identity: AccountIdentityProvider | None = None,
        clock: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
        catalog_path: Path = _CATALOG_PATH,
        unavailable_path: Path = _UNAVAILABLE_PATH,
        overrides_path: Path = _OVERRIDES_PATH,
        cache_writer: Callable[[Path, Mapping[str, Any]], None] = lambda path, document: _atomic_json(path, document),
    ) -> None:
        catalog = _read_json(catalog_path)
        unavailable = _read_json(unavailable_path)
        endpoint_overrides = yaml.safe_load(overrides_path.read_text(encoding="utf-8")) or {}
        if not isinstance(endpoint_overrides, Mapping):
            raise EndpointResolutionError("invalid_endpoint_overrides")
        self._catalog = catalog
        self._override_products = endpoint_overrides.get("products", {})
        self._trusted_suffixes = tuple(endpoint_overrides.get("trusted_endpoint_suffixes", ()))
        self._unavailable = {
            str(item.get("product", "")).casefold(): str(item.get("reason", "endpoint_unavailable"))
            for item in unavailable.get("products", ())
            if isinstance(item, Mapping)
        }
        self._location = location or LocationResolver()
        self._account_identity = account_identity or AccountIdentityResolver()
        self._clock = clock
        self._cache_path = ensure_private_dir(cache_dir / "endpoints") / "location.json"
        self._cache_writer = cache_writer
        self._location_cache = self._read_cache()
        self._host_binding_resolver = HostBindingResolver(self._trusted_suffixes)

    @property
    def trusted_suffixes(self) -> tuple[str, ...]:
        return self._trusted_suffixes

    @property
    def host_binding_resolver(self) -> HostBindingResolver:
        return self._host_binding_resolver

    async def resolve(
        self,
        contract: CanonicalWireContract,
        region_id: str,
        credential: Any,
        *,
        host_values: Mapping[str, Any] | None = None,
        explicit_endpoint: str | None = None,
    ) -> EndpointResolution:
        if contract.transport not in {"tea", "acs1", "acs3_streaming", "oss_v4_sdk"}:
            raise EndpointResolutionError("unsupported_transport")
        if not isinstance(region_id, str) or _REGION.fullmatch(region_id) is None:
            raise EndpointResolutionError("invalid_region_id")
        product, version, catalog_record, override_record = self._select_records(contract.product, contract.version)
        effective = merge_endpoint_record(catalog_record, override_record)
        self._host_binding_resolver.validate_values(contract, host_values or {})
        host_template = _host_template_for_contract(contract, effective.host_template)
        if explicit_endpoint is not None:
            return EndpointResolution(self._validated(explicit_endpoint), "explicit", host_template)
        if catalog_record is None and override_record is None:
            raise self._unavailable_error(contract.product)
        if endpoint := effective.region_overrides.get(region_id):
            return EndpointResolution(self._validated(endpoint), "override", host_template)
        if effective.account_id_host_template:
            account_id = await self._account_identity.resolve(credential, region_id)
            endpoint = effective.account_id_host_template.format(account_id=account_id, region_id=region_id)
            return EndpointResolution(self._validated(endpoint), "override", host_template)
        if effective.location_service_code:
            endpoint = await self._resolve_location(
                product, version, region_id, effective.location_service_code, credential
            )
            if endpoint:
                return EndpointResolution(endpoint, "location", host_template)
        if endpoint := effective.regional.get(region_id):
            return EndpointResolution(self._validated(endpoint), "catalog_region", host_template)
        if effective.global_endpoint:
            return EndpointResolution(self._validated(effective.global_endpoint), "catalog_global", host_template)
        if effective.regional_endpoint_pattern and region_id not in effective.blocked_pattern_regions:
            endpoint = effective.regional_endpoint_pattern.format(region_id=region_id)
            return EndpointResolution(self._validated(endpoint), "override_pattern", host_template)
        raise self._unavailable_error(contract.product)

    def _unavailable_error(self, product: str) -> EndpointResolutionError:
        reason = self._unavailable.get(product.casefold())
        suffix = f":{reason}" if reason is not None else ""
        return EndpointResolutionError("endpoint_unavailable" + suffix)

    def _select_records(
        self, requested_product: str, requested_version: str
    ) -> tuple[str, str, Mapping[str, Any] | None, Mapping[str, Any] | None]:
        catalog_products = self._catalog.get("products", {})
        product_names = {
            str(name).casefold(): str(name)
            for source in (catalog_products, self._override_products)
            if isinstance(source, Mapping)
            for name in source
        }
        product = product_names.get(requested_product.casefold(), requested_product)
        catalog_versions = catalog_products.get(product, {}) if isinstance(catalog_products, Mapping) else {}
        override_versions = (
            self._override_products.get(product, {}) if isinstance(self._override_products, Mapping) else {}
        )
        versions = set(catalog_versions if isinstance(catalog_versions, Mapping) else ()) | set(
            override_versions if isinstance(override_versions, Mapping) else ()
        )
        version = requested_version
        if version not in versions:
            defaults = self._catalog.get("_meta", {}).get("default_versions", {})
            default = defaults.get(product) if isinstance(defaults, Mapping) else None
            if isinstance(default, str) and default in versions:
                version = default
            elif len(versions) == 1:
                version = next(iter(versions))
        catalog_record = catalog_versions.get(version) if isinstance(catalog_versions, Mapping) else None
        override_record = override_versions.get(version) if isinstance(override_versions, Mapping) else None
        return product, version, _as_mapping(catalog_record), _as_mapping(override_record)

    async def _resolve_location(
        self, product: str, version: str, region_id: str, service_code: str, credential: Any
    ) -> str | None:
        key = (product, version, region_id, service_code)
        cached = self._location_cache.get(key)
        now = self._clock()
        if cached is not None:
            age = _age(now, cached.fetched_at)
            ttl = _FRESH_TTL if cached.endpoint is not None else _EMPTY_TTL
            if age <= ttl:
                return self._validated_or_none(cached.endpoint)
        try:
            endpoint = await self._location.resolve(product, version, region_id, service_code, credential)
            endpoint = self._validated_or_none(endpoint)
        except Exception:
            if cached is not None and cached.endpoint is not None and _age(now, cached.fetched_at) <= _STALE_TTL:
                return self._validated_or_none(cached.endpoint)
            return None
        prospective = dict(self._location_cache)
        prospective[key] = _LocationCacheEntry(endpoint=endpoint, fetched_at=now)
        try:
            self._write_cache(prospective)
        except OSError as error:
            raise EndpointResolutionError("location_cache_write_failed") from error
        self._location_cache = prospective
        return endpoint

    def _validated(self, endpoint: str) -> str:
        if not _is_trusted_hostname(endpoint, self._trusted_suffixes):
            raise EndpointResolutionError("untrusted_endpoint")
        return endpoint

    def _validated_or_none(self, endpoint: str | None) -> str | None:
        if endpoint is None:
            return None
        return endpoint if _is_trusted_hostname(endpoint, self._trusted_suffixes) else None

    def _read_cache(self) -> dict[tuple[str, str, str, str], _LocationCacheEntry]:
        try:
            document = _read_json(self._cache_path)
            if document.get("schema_version") != _CACHE_SCHEMA:
                return {}
            envelope_fetched_at = document.get("fetched_at")
            if not isinstance(envelope_fetched_at, str) or document.get("source_url") != _LOCATION_SOURCE_URL:
                return {}
            envelope_time = datetime.fromisoformat(envelope_fetched_at)
            if envelope_time.tzinfo is None:
                return {}
            payload = document.get("payload")
            if not isinstance(payload, Mapping):
                return {}
            if document.get("payload_sha256") != _payload_checksum(payload):
                return {}
            entries: dict[tuple[str, str, str, str], _LocationCacheEntry] = {}
            raw_entries = payload.get("entries")
            if not isinstance(raw_entries, list):
                return {}
            for item in raw_entries:
                if not isinstance(item, Mapping):
                    continue
                key = item.get("key")
                fetched_at = item.get("fetched_at")
                endpoint = item.get("endpoint")
                if not isinstance(key, list) or len(key) != 4 or not all(isinstance(value, str) for value in key):
                    continue
                if endpoint is not None and not isinstance(endpoint, str):
                    continue
                parsed = datetime.fromisoformat(fetched_at) if isinstance(fetched_at, str) else None
                if parsed is None or parsed.tzinfo is None:
                    continue
                entries[tuple(key)] = _LocationCacheEntry(endpoint=endpoint, fetched_at=parsed)
            return entries
        except (OSError, ValueError, TypeError, json.JSONDecodeError):
            return {}

    def _write_cache(self, cache: Mapping[tuple[str, str, str, str], _LocationCacheEntry]) -> None:
        entries = [
            {"key": list(key), "endpoint": value.endpoint, "fetched_at": value.fetched_at.isoformat()}
            for key, value in sorted(cache.items())
        ]
        payload = {"entries": entries}
        document = {
            "schema_version": _CACHE_SCHEMA,
            "fetched_at": self._clock().isoformat(),
            "source_url": _LOCATION_SOURCE_URL,
            "payload_sha256": _payload_checksum(payload),
            "payload": payload,
        }
        self._cache_writer(self._cache_path, document)


class HostBindingResolver:
    def __init__(self, trusted_suffixes: tuple[str, ...]) -> None:
        self._trusted_suffixes = trusted_suffixes

    def bind(
        self,
        contract: CanonicalWireContract,
        endpoint: str,
        host_template: str | None,
        host_values: Mapping[str, Any],
    ) -> str:
        validated = self.validate_values(contract, host_values)
        declared = {parameter.name for parameter in contract.parameters if parameter.location == "host"}
        if not declared:
            if host_template is not None:
                fields = _template_fields(host_template)
                if fields - {"endpoint"}:
                    raise EndpointResolutionError("invalid_host_template")
            if not _is_trusted_hostname(endpoint, self._trusted_suffixes):
                raise EndpointResolutionError("untrusted_endpoint")
            return endpoint
        if host_template is None:
            raise EndpointResolutionError("host_template_required")
        fields = _template_fields(host_template)
        if declared - fields or fields - (declared | {"endpoint"}):
            raise EndpointResolutionError("invalid_host_template")
        if declared - set(validated):
            raise EndpointResolutionError("missing_host_parameter")
        replacements: dict[str, str] = {"endpoint": endpoint}
        for name in declared:
            replacements[name] = validated[name]
        try:
            result = host_template.format_map(replacements)
        except (KeyError, ValueError) as error:
            raise EndpointResolutionError("invalid_host_template") from error
        if not _is_trusted_hostname(result, self._trusted_suffixes):
            raise EndpointResolutionError("untrusted_endpoint")
        return result

    def validate_values(self, contract: CanonicalWireContract, host_values: Mapping[str, Any]) -> Mapping[str, str]:
        try:
            return validate_host_parameter_values(contract, host_values)
        except ApiContractError as error:
            raise EndpointResolutionError(str(error)) from error


def merge_endpoint_record(
    catalog_record: Mapping[str, Any] | None, override_record: Mapping[str, Any] | None
) -> EffectiveEndpointRecord:
    catalog = catalog_record or {}
    override = override_record or {}
    service_code = catalog.get("location_service_code")
    if "location_service_code" in override:
        service_code = override.get("location_service_code")
    regional = dict(catalog.get("regional_endpoints", {}))
    public_global_endpoint = regional.pop("public", None)
    region_overrides: dict[str, str] = {}
    blocked_pattern_regions: set[str] = set()
    override_regions = override.get("regions", {})
    if isinstance(override_regions, Mapping):
        for region, endpoint in override_regions.items():
            regional.pop(region, None)
            if endpoint is not None:
                region_overrides[str(region)] = str(endpoint)
            else:
                blocked_pattern_regions.add(str(region))
    global_endpoint = catalog.get("global_endpoint")
    if global_endpoint is None and public_global_endpoint is not None:
        global_endpoint = public_global_endpoint
    if "global" in override:
        global_endpoint = override.get("global")
    host_template = catalog.get("host_template")
    if "host_template" in override:
        host_template = override.get("host_template")
    account_id_host_template = override.get("account_id_host_template")
    if account_id_host_template is not None:
        if not isinstance(account_id_host_template, str) or _template_fields(account_id_host_template) != {
            "account_id",
            "region_id",
        }:
            raise EndpointResolutionError("invalid_account_id_host_template")
    regional_endpoint_pattern = _regional_endpoint_pattern(override.get("regional_endpoint_pattern"))
    return EffectiveEndpointRecord(
        location_service_code=str(service_code) if service_code is not None else None,
        region_overrides=MappingProxyType(region_overrides),
        regional=MappingProxyType({str(key): str(value) for key, value in regional.items()}),
        global_endpoint=str(global_endpoint) if global_endpoint is not None else None,
        regional_endpoint_pattern=regional_endpoint_pattern,
        blocked_pattern_regions=frozenset(blocked_pattern_regions),
        host_template=str(host_template) if host_template is not None else None,
        account_id_host_template=account_id_host_template,
    )


def _is_trusted_hostname(hostname: str, suffixes: tuple[str, ...]) -> bool:
    if (
        not isinstance(hostname, str)
        or hostname != hostname.casefold()
        or len(hostname) > 253
        or hostname.endswith(".")
    ):
        return False
    labels = hostname.split(".")
    if len(labels) < 2 or any(_HOST_LABEL.fullmatch(label) is None for label in labels):
        return False
    return any(hostname == suffix or hostname.endswith("." + suffix) for suffix in suffixes)


def _template_fields(template: str) -> set[str]:
    try:
        parsed = tuple(Formatter().parse(template))
    except ValueError as error:
        raise EndpointResolutionError("invalid_host_template") from error
    if any(format_spec or conversion for _, field, format_spec, conversion in parsed if field is not None):
        raise EndpointResolutionError("invalid_host_template")
    fields = {field for _, field, _, _ in parsed if field is not None}
    if any(not field or "." in field or "[" in field for field in fields):
        raise EndpointResolutionError("invalid_host_template")
    return fields


def _regional_endpoint_pattern(pattern: Any) -> str | None:
    if pattern is None:
        return None
    try:
        fields = _template_fields(pattern) if isinstance(pattern, str) else set()
    except EndpointResolutionError as error:
        raise EndpointResolutionError("invalid_regional_endpoint_pattern") from error
    if pattern.split(".").count("{region_id}") != 1 or fields != {"region_id"}:
        raise EndpointResolutionError("invalid_regional_endpoint_pattern")
    return pattern


def _host_template_for_contract(contract: CanonicalWireContract, host_template: str | None) -> str | None:
    if host_template is None:
        return None
    if any(parameter.location == "host" for parameter in contract.parameters):
        return host_template
    return None


def _as_mapping(value: Any) -> Mapping[str, Any] | None:
    return value if isinstance(value, Mapping) else None


def _read_json(path: Path) -> Mapping[str, Any]:
    raw = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(raw, Mapping):
        raise EndpointResolutionError("invalid_endpoint_catalog")
    return raw


def _atomic_json(path: Path, document: Mapping[str, Any]) -> None:
    data = (json.dumps(document, sort_keys=True, separators=(",", ":")) + "\n").encode("ascii")
    descriptor, temporary = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        fsync_parent_dir(path)
    except BaseException:
        try:
            os.unlink(temporary)
        except OSError:
            pass
        raise


def _payload_checksum(payload: Mapping[str, Any]) -> str:
    data = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("ascii")
    return hashlib.sha256(data).hexdigest()


def _age(now: datetime, then: datetime) -> timedelta:
    if now.tzinfo is None or then.tzinfo is None:
        return timedelta.max
    return max(now - then, timedelta())


def catalog_digest(path: Path = _CATALOG_PATH) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()
