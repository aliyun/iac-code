#!/usr/bin/env python3
"""Generate the reviewed Alibaba Cloud endpoint catalog from pinned inputs."""

from __future__ import annotations

import argparse
import hashlib
import ipaddress
import json
import os
import re
import tempfile
from collections.abc import Mapping
from datetime import datetime
from pathlib import Path
from string import Formatter
from typing import Any

import yaml

from iac_code.tools.cloud.aliyun.api_identifiers import SAFE_API_VERSION

SOURCE_REPOSITORY = "https://github.com/aliyun/aliyun-openapi-meta.git"
SOURCE_COMMIT = "2563691c22229a0b493606e11166b95896707095"
SOURCE_PATH = "metadatas/products.json"
PRODUCTS_SHA256 = "e79346fbe87dbacd73c4cb68520f897add17a8e90cadb8fb03e5efa217d04be5"
OPENMETA_PRODUCTS_URL = "https://api.aliyun.com/meta/v1/products.json?language=ZH_CN"
OPENMETA_PRODUCT_COUNT = 339
OPENMETA_PRODUCTS_SHA256 = "d226cf4dfe48636261a08cd3b6e94e422831aa0f8e15c08e391023784e27830f"
_IDENTIFIER = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,127}$")
_DATE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_REGION = re.compile(r"^[a-z0-9][a-z0-9-]{0,62}$")
_HOST_LABEL = re.compile(r"^[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?$")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--source-commit", required=True)
    parser.add_argument("--products-sha256", required=True)
    parser.add_argument("--openmeta-products", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    args = parser.parse_args()
    generate(
        source=args.source,
        source_commit=args.source_commit,
        products_sha256=args.products_sha256,
        openmeta_products=args.openmeta_products,
        output_dir=args.output_dir,
    )


def generate(
    *, source: Path, source_commit: str, products_sha256: str, openmeta_products: Path, output_dir: Path
) -> None:
    if source_commit != SOURCE_COMMIT:
        raise ValueError("pinned source commit mismatch")
    if products_sha256 != PRODUCTS_SHA256:
        raise ValueError("pinned products SHA-256 mismatch")
    source_bytes = source.read_bytes()
    actual_sha = hashlib.sha256(source_bytes).hexdigest()
    if actual_sha != PRODUCTS_SHA256:
        raise ValueError("products SHA-256 mismatch")
    raw = json.loads(source_bytes)
    openmeta = json.loads(openmeta_products.read_text(encoding="utf-8"))
    overrides, endpoint_overrides_sha256 = _load_overrides()
    trusted, override_products = _validate_overrides(overrides)
    products, trimmed, rejected_regions = _parse_products(raw, trusted)
    _validate_cross_source_product_names(products, override_products)
    _validate_override_deltas(products, override_products)
    openmeta_products_by_name, default_versions = _openmeta_products(openmeta)

    catalog_products: dict[str, Any] = {product: dict(versions) for product, versions in products.items()}
    for product, versions in override_products.items():
        if not isinstance(product, str) or _IDENTIFIER.fullmatch(product) is None or not isinstance(versions, Mapping):
            raise ValueError("invalid override product")
        catalog_products.setdefault(product, {})
        for version in versions:
            if not isinstance(version, str) or SAFE_API_VERSION.fullmatch(version) is None:
                raise ValueError("invalid override version")
            catalog_products[product].setdefault(version, _empty_record())

    unavailable: list[dict[str, str]] = []
    check_date = _fixture_date(openmeta)
    covered = _usable_product_identities(catalog_products, override_products)
    for product in sorted(openmeta_products_by_name, key=str.casefold):
        version = openmeta_products_by_name[product]
        if version is None:
            unavailable.append({"product": product, "checked_on": check_date, "reason": "invalid_default_version"})
        elif product.casefold() not in covered:
            unavailable.append(
                {"product": product, "checked_on": check_date, "reason": "upstream_metadata_unavailable"}
            )

    catalog = {
        "_meta": {
            "schema_version": 1,
            "source_commit": source_commit,
            "source_sha256": actual_sha,
            "source_repository": SOURCE_REPOSITORY,
            "source_path": SOURCE_PATH,
            "generated_by": "scripts/aliyun/generate_endpoints.py",
            "default_versions": default_versions,
            "trusted_endpoint_suffixes": sorted(trusted),
        },
        "products": catalog_products,
    }
    unavailable_document = {"schema_version": 1, "products": unavailable}
    report = {
        "schema_version": 1,
        "source_commit": source_commit,
        "source_sha256": actual_sha,
        "source_repository": SOURCE_REPOSITORY,
        "source_path": SOURCE_PATH,
        "openmeta_source_url": openmeta["source_url"],
        "openmeta_fetched_at": openmeta["fetched_at"],
        "openmeta_products_sha256": openmeta["products_sha256"],
        "openmeta_fixture_sha256": hashlib.sha256(openmeta_products.read_bytes()).hexdigest(),
        "endpoint_overrides_sha256": endpoint_overrides_sha256,
        "counts": {
            "catalog_products": len(catalog_products),
            "available_products": len(openmeta_products_by_name) - len(unavailable),
            "openmeta_products": len(openmeta_products_by_name),
            "openmeta_products_with_valid_default_version": len(default_versions),
            "openmeta_products_without_valid_default_version": len(openmeta_products_by_name) - len(default_versions),
            "override_records": sum(len(versions) for versions in override_products.values()),
            "source_records": sum(len(versions) for versions in products.values()),
            "trimmed_endpoints": len(trimmed),
            "rejected_regions": len(rejected_regions),
            "unavailable_products": len(unavailable),
        },
        "trimmed_endpoints": sorted(trimmed),
        "rejected_regions": sorted(rejected_regions),
    }
    output_dir.mkdir(parents=True, exist_ok=True)
    for name, document in (
        ("catalog.json", catalog),
        ("unavailable.json", unavailable_document),
        ("generation_report.json", report),
    ):
        _atomic_write(output_dir / name, document)


def _load_overrides() -> tuple[Mapping[str, Any], str]:
    path = Path(__file__).parents[2] / "src/iac_code/tools/cloud/aliyun/data/endpoints/overrides.yml"
    source = path.read_bytes()
    raw = yaml.safe_load(source) or {}
    if not isinstance(raw, Mapping):
        raise ValueError("invalid endpoint overrides")
    return raw, hashlib.sha256(source).hexdigest()


def _validate_overrides(overrides: Mapping[str, Any]) -> tuple[tuple[str, ...], Mapping[str, Any]]:
    raw_trusted = overrides.get("trusted_endpoint_suffixes")
    if not isinstance(raw_trusted, list | tuple) or not raw_trusted:
        raise ValueError("invalid trusted endpoint suffix")
    trusted: list[str] = []
    for suffix in raw_trusted:
        if not isinstance(suffix, str):
            raise ValueError("invalid trusted endpoint suffix")
        try:
            _validate_hostname_syntax(suffix)
        except ValueError as error:
            raise ValueError("invalid trusted endpoint suffix") from error
        trusted.append(suffix)

    products = overrides.get("products", {})
    if not isinstance(products, Mapping):
        raise ValueError("invalid override product")
    trusted_tuple = tuple(trusted)
    allowed_fields = {
        "location_service_code",
        "regions",
        "regional_endpoint_pattern",
        "global",
        "host_template",
        "account_id_host_template",
        "source",
        "reason",
        "checked_on",
        "note",
    }
    product_names: dict[str, str] = {}
    for product, versions in products.items():
        if not isinstance(product, str) or _IDENTIFIER.fullmatch(product) is None or not isinstance(versions, Mapping):
            raise ValueError("invalid override product")
        identity = product.casefold()
        if identity in product_names:
            raise ValueError("duplicate override product")
        product_names[identity] = product
    for product, versions in products.items():
        for version, record in versions.items():
            if not isinstance(version, str) or SAFE_API_VERSION.fullmatch(version) is None:
                raise ValueError("invalid override version")
            if not isinstance(record, Mapping) or any(field not in allowed_fields for field in record):
                raise ValueError("invalid override record")
            service_code = record.get("location_service_code")
            if service_code is not None and (
                not isinstance(service_code, str) or _IDENTIFIER.fullmatch(service_code) is None
            ):
                raise ValueError("invalid Location service code")
            regions = record.get("regions", {})
            if not isinstance(regions, Mapping):
                raise ValueError("invalid override region")
            for region, endpoint in regions.items():
                if not isinstance(region, str) or _REGION.fullmatch(region) is None:
                    raise ValueError("invalid override region")
                _validate_override_endpoint(endpoint, trusted_tuple)
            _validate_regional_endpoint_pattern(record.get("regional_endpoint_pattern"), trusted_tuple)
            _validate_override_endpoint(record.get("global"), trusted_tuple)
            _validate_host_template(record.get("host_template"), trusted_tuple)
            account_template = record.get("account_id_host_template")
            _validate_host_template(account_template, trusted_tuple)
            if account_template is not None and _host_template_fields(account_template) != {
                "account_id",
                "region_id",
            }:
                raise ValueError("invalid host template")
            _validate_override_evidence(record)
    return trusted_tuple, products


def _validate_override_evidence(record: Mapping[str, Any]) -> None:
    source = record.get("source")
    reason = record.get("reason")
    checked_on = record.get("checked_on")
    note = record.get("note")
    if not isinstance(source, str) or not source.strip():
        raise ValueError("missing override evidence")
    if not isinstance(reason, str) or not reason.strip():
        raise ValueError("missing override evidence")
    if not isinstance(checked_on, str) or _DATE.fullmatch(checked_on) is None:
        raise ValueError("missing override evidence")
    if note is not None and (not isinstance(note, str) or not note.strip()):
        raise ValueError("missing override evidence")


def _validate_override_endpoint(endpoint: Any, trusted: tuple[str, ...]) -> None:
    if endpoint is None:
        return
    if not isinstance(endpoint, str):
        raise ValueError("invalid override endpoint")
    try:
        _validate_hostname(endpoint, trusted)
    except ValueError as error:
        raise ValueError("invalid override endpoint") from error


def _validate_regional_endpoint_pattern(pattern: Any, trusted: tuple[str, ...]) -> None:
    if pattern is None:
        return
    if not isinstance(pattern, str) or pattern.split(".").count("{region_id}") != 1:
        raise ValueError("invalid regional endpoint pattern")
    try:
        parsed = tuple(Formatter().parse(pattern))
        fields = [field for _, field, format_spec, conversion in parsed if field is not None]
        if fields != ["region_id"] or any(
            format_spec or conversion for _, field, format_spec, conversion in parsed if field is not None
        ):
            raise ValueError("invalid regional endpoint pattern")
        _validate_hostname(pattern.format(region_id="cn-hangzhou"), trusted)
    except (KeyError, ValueError) as error:
        raise ValueError("invalid regional endpoint pattern") from error


def _validate_host_template(template: Any, trusted: tuple[str, ...]) -> None:
    if template is None:
        return
    if not isinstance(template, str) or not template:
        raise ValueError("invalid host template")
    try:
        parsed = tuple(Formatter().parse(template))
    except ValueError as error:
        raise ValueError("invalid host template") from error
    fields: list[str] = []
    for _, field, format_spec, conversion in parsed:
        if field is None:
            continue
        if format_spec or conversion or _IDENTIFIER.fullmatch(field) is None:
            raise ValueError("invalid host template")
        fields.append(field)
    if not fields or len(fields) != len(set(fields)):
        raise ValueError("invalid host template")
    replacements = {field: "value" for field in fields}
    if "endpoint" in replacements:
        replacements["endpoint"] = "service." + trusted[0]
    try:
        rendered = template.format_map(replacements)
        _validate_hostname(rendered, trusted)
    except (KeyError, ValueError) as error:
        raise ValueError("invalid host template") from error


def _host_template_fields(template: str) -> set[str]:
    return {field for _, field, _, _ in Formatter().parse(template) if field is not None}


def _parse_products(raw: Any, trusted: tuple[str, ...]) -> tuple[dict[str, Any], list[str], list[str]]:
    entries = raw.get("products") if isinstance(raw, Mapping) else None
    if not isinstance(entries, list):
        raise ValueError("products source must contain a products array")
    result: dict[str, Any] = {}
    product_names: dict[str, str] = {}
    trimmed: list[str] = []
    rejected_regions: list[str] = []
    for entry in entries:
        if not isinstance(entry, Mapping):
            raise ValueError("invalid product entry")
        product = entry.get("code")
        version = entry.get("version")
        if (
            not isinstance(product, str)
            or _IDENTIFIER.fullmatch(product) is None
            or not isinstance(version, str)
            or SAFE_API_VERSION.fullmatch(version) is None
        ):
            raise ValueError("invalid product metadata")
        identity = product.casefold()
        existing_product = product_names.get(identity)
        if existing_product is not None and existing_product != product:
            raise ValueError("duplicate endpoint product")
        product_names[identity] = product
        endpoints = entry.get("regional_endpoints")
        if endpoints is None:
            endpoints = {}
        if not isinstance(endpoints, Mapping):
            raise ValueError("invalid endpoint mapping")
        regional: dict[str, str] = {}
        public_global_endpoint: str | None = None
        for region, endpoint in endpoints.items():
            if not isinstance(region, str) or not isinstance(endpoint, str):
                raise ValueError("invalid regional endpoint")
            normalized = endpoint.strip(" \t\r\n\f\v")
            if normalized != endpoint:
                trimmed.append(f"{product}/{version}/{region}")
            _validate_hostname(normalized, trusted)
            if region == "public":
                public_global_endpoint = normalized
                continue
            if _REGION.fullmatch(region) is None:
                rejected_regions.append(f"{product}/{version}/{region}")
                continue
            regional[region] = normalized
        global_endpoint = entry.get("global_endpoint")
        if global_endpoint == "":
            global_endpoint = None
        if global_endpoint is not None:
            if not isinstance(global_endpoint, str):
                raise ValueError("invalid global endpoint")
            normalized_global = global_endpoint.strip(" \t\r\n\f\v")
            if normalized_global != global_endpoint:
                trimmed.append(f"{product}/{version}/global")
            _validate_hostname(normalized_global, trusted)
            global_endpoint = normalized_global
        if public_global_endpoint is not None:
            if global_endpoint is not None and global_endpoint != public_global_endpoint:
                raise ValueError("conflicting public/global endpoint")
            global_endpoint = public_global_endpoint
        service_code = entry.get("location_service_code")
        if service_code == "":
            service_code = None
        if service_code is not None and (
            not isinstance(service_code, str) or _IDENTIFIER.fullmatch(service_code) is None
        ):
            raise ValueError("invalid Location service code")
        product_versions = result.setdefault(product, {})
        record = {
            "location_service_code": service_code,
            "regional_endpoints": regional,
            "global_endpoint": global_endpoint,
            "host_template": None,
        }
        if version in product_versions and product_versions[version] != record:
            raise ValueError("conflicting product version metadata")
        product_versions[version] = record
    return result, trimmed, rejected_regions


def _openmeta_products(openmeta: Any) -> tuple[dict[str, str | None], dict[str, str]]:
    products = openmeta.get("products") if isinstance(openmeta, Mapping) else None
    if (
        not isinstance(openmeta, Mapping)
        or openmeta.get("source_url") != OPENMETA_PRODUCTS_URL
        or not isinstance(openmeta.get("fetched_at"), str)
        or not isinstance(openmeta.get("products_sha256"), str)
        or not isinstance(products, list)
    ):
        raise ValueError("OpenMeta products fixture lacks source identity")
    products_digest = hashlib.sha256(
        json.dumps(products, ensure_ascii=True, sort_keys=True, separators=(",", ":")).encode("ascii")
    ).hexdigest()
    if (
        len(products) != OPENMETA_PRODUCT_COUNT
        or openmeta["products_sha256"] != OPENMETA_PRODUCTS_SHA256
        or products_digest != OPENMETA_PRODUCTS_SHA256
    ):
        raise ValueError("pinned OpenMeta products snapshot mismatch")
    try:
        fetched_at = datetime.fromisoformat(openmeta["fetched_at"])
    except ValueError as error:
        raise ValueError("invalid OpenMeta fetched_at") from error
    if fetched_at.tzinfo is None:
        raise ValueError("invalid OpenMeta fetched_at")
    all_products: dict[str, str | None] = {}
    default_versions: dict[str, str] = {}
    identities: set[str] = set()
    for entry in products:
        product = entry.get("product") if isinstance(entry, Mapping) else None
        version = entry.get("defaultVersion") if isinstance(entry, Mapping) else None
        if not isinstance(product, str) or _IDENTIFIER.fullmatch(product) is None:
            raise ValueError("invalid OpenMeta product")
        identity = product.casefold()
        if identity in identities:
            raise ValueError("duplicate OpenMeta product")
        identities.add(identity)
        normalized_version = version if isinstance(version, str) and SAFE_API_VERSION.fullmatch(version) else None
        all_products[product] = normalized_version
        if normalized_version is not None:
            default_versions[product] = normalized_version
    return all_products, default_versions


def _validate_cross_source_product_names(products: Mapping[str, Any], override_products: Mapping[str, Any]) -> None:
    source_names = {product.casefold(): product for product in products}
    for product in override_products:
        source_name = source_names.get(product.casefold())
        if source_name is not None and source_name != product:
            raise ValueError("endpoint product case mismatch")


def _validate_override_deltas(products: Mapping[str, Any], override_products: Mapping[str, Any]) -> None:
    for product, versions in override_products.items():
        catalog_versions = products.get(product, {})
        if not isinstance(versions, Mapping) or not isinstance(catalog_versions, Mapping):
            continue
        for version, override_record in versions.items():
            catalog_record = catalog_versions.get(version)
            if not isinstance(catalog_record, Mapping) or not isinstance(override_record, Mapping):
                continue
            catalog_regions = catalog_record.get("regional_endpoints", {})
            override_regions = override_record.get("regions", {})
            if isinstance(catalog_regions, Mapping) and isinstance(override_regions, Mapping):
                for region, endpoint in override_regions.items():
                    catalog_has_region = region in catalog_regions
                    if endpoint is not None and endpoint == catalog_regions.get(region) and catalog_has_region:
                        raise ValueError("redundant override region")
            if _effective_override_data(catalog_record, override_record) == _effective_override_data(
                catalog_record, {}
            ):
                raise ValueError("redundant override record")


def _effective_override_data(catalog_record: Mapping[str, Any], override_record: Mapping[str, Any]) -> dict[str, Any]:
    service_code = catalog_record.get("location_service_code")
    if "location_service_code" in override_record:
        service_code = override_record.get("location_service_code")
    regional = dict(catalog_record.get("regional_endpoints", {}))
    override_regions = override_record.get("regions", {})
    blocked_pattern_regions: list[str] = []
    if isinstance(override_regions, Mapping):
        for region, endpoint in override_regions.items():
            regional.pop(region, None)
            if endpoint is not None:
                regional[str(region)] = str(endpoint)
            else:
                blocked_pattern_regions.append(str(region))
    global_endpoint = catalog_record.get("global_endpoint")
    if "global" in override_record:
        global_endpoint = override_record.get("global")
    host_template = catalog_record.get("host_template")
    if "host_template" in override_record:
        host_template = override_record.get("host_template")
    return {
        "location_service_code": service_code,
        "regional_endpoints": regional,
        "blocked_pattern_regions": sorted(blocked_pattern_regions),
        "regional_endpoint_pattern": override_record.get("regional_endpoint_pattern"),
        "global_endpoint": global_endpoint,
        "host_template": host_template,
        "account_id_host_template": override_record.get("account_id_host_template"),
    }


def _usable_product_identities(catalog_products: Mapping[str, Any], override_products: Mapping[str, Any]) -> set[str]:
    catalog_by_identity = {product.casefold(): versions for product, versions in catalog_products.items()}
    overrides_by_identity = {product.casefold(): versions for product, versions in override_products.items()}
    usable: set[str] = set()
    for identity in set(catalog_by_identity) | set(overrides_by_identity):
        catalog_versions = catalog_by_identity.get(identity, {})
        override_versions = overrides_by_identity.get(identity, {})
        if not isinstance(catalog_versions, Mapping) or not isinstance(override_versions, Mapping):
            continue
        for version in set(catalog_versions) | set(override_versions):
            catalog = catalog_versions.get(version)
            override = override_versions.get(version)
            if _effective_record_has_endpoint(catalog, override):
                usable.add(identity)
                break
    return usable


def _effective_record_has_endpoint(catalog_record: Any, override_record: Any) -> bool:
    catalog = catalog_record if isinstance(catalog_record, Mapping) else {}
    override = override_record if isinstance(override_record, Mapping) else {}
    service_code = (
        override.get("location_service_code")
        if "location_service_code" in override
        else catalog.get("location_service_code")
    )
    regional = dict(catalog.get("regional_endpoints", {}))
    region_overrides: dict[str, str] = {}
    override_regions = override.get("regions", {})
    if isinstance(override_regions, Mapping):
        for region, endpoint in override_regions.items():
            regional.pop(region, None)
            if endpoint is not None:
                region_overrides[str(region)] = str(endpoint)
    global_endpoint = override.get("global") if "global" in override else catalog.get("global_endpoint")
    return bool(
        service_code
        or regional
        or region_overrides
        or global_endpoint
        or override.get("regional_endpoint_pattern")
        or override.get("account_id_host_template")
    )


def _fixture_date(openmeta: Mapping[str, Any]) -> str:
    fetched_at = openmeta["fetched_at"]
    return str(fetched_at).split("T", 1)[0]


def _validate_hostname(hostname: str, trusted: tuple[str, ...]) -> None:
    _validate_hostname_syntax(hostname)
    if not any(hostname == suffix or hostname.endswith("." + suffix) for suffix in trusted):
        raise ValueError("untrusted endpoint hostname")


def _validate_hostname_syntax(hostname: str) -> None:
    if hostname != hostname.casefold() or len(hostname) > 253 or hostname.endswith("."):
        raise ValueError("invalid endpoint hostname")
    labels = hostname.split(".")
    if len(labels) < 2 or any(_HOST_LABEL.fullmatch(label) is None for label in labels):
        raise ValueError("invalid endpoint hostname")
    try:
        ipaddress.ip_address(hostname)
    except ValueError:
        return
    raise ValueError("invalid endpoint hostname")


def _empty_record() -> dict[str, Any]:
    return {
        "location_service_code": None,
        "regional_endpoints": {},
        "global_endpoint": None,
        "host_template": None,
    }


def _atomic_write(path: Path, document: Any) -> None:
    data = (json.dumps(document, ensure_ascii=True, sort_keys=True, separators=(",", ":")) + "\n").encode("ascii")
    descriptor, temporary = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    except BaseException:
        try:
            os.unlink(temporary)
        except OSError:
            pass
        raise


if __name__ == "__main__":
    main()
