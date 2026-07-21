#!/usr/bin/env python3
"""Generate the reviewed OSS async operation catalog from SDK and OpenMeta metadata."""

from __future__ import annotations

import argparse
import hashlib
import importlib.metadata
import inspect
import json
import re
from collections.abc import Mapping
from pathlib import Path
from typing import Any
from urllib.parse import unquote, urlsplit

try:
    import tomllib
except ModuleNotFoundError:  # pragma: no cover - Python 3.10 compatibility
    import tomli as tomllib

from alibabacloud_oss_v2 import Config
from alibabacloud_oss_v2.aio.client import AsyncClient
from alibabacloud_oss_v2.credentials import StaticCredentialsProvider
from alibabacloud_oss_v2.signer.v4 import SignerV4
from alibabacloud_oss_v2.types import AsyncHttpClient, Signer, SigningContext
from packaging.tags import sys_tags
from packaging.utils import canonicalize_name, parse_wheel_filename

_PACKAGE_NAME = "alibabacloud-oss-v2"
_SCHEMA_VERSION = 1
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_HELPER_REASONS = {
    "close": "sdk_lifecycle_method",
    "invoke_operation": "generic_sdk_invocation_forbidden",
    "is_bucket_exist": "sdk_convenience_method",
    "is_object_exist": "sdk_convenience_method",
    "presign": "presigning_not_supported",
}


class _ProbeHttpClient(AsyncHttpClient):
    async def send(self, request: Any, **kwargs: Any) -> Any:  # pragma: no cover - protocol probe only
        raise AssertionError((request, kwargs))

    async def open(self) -> None:
        return None

    async def close(self) -> None:
        return None


class _ProbeSigner(Signer):
    def sign(self, signing_ctx: SigningContext) -> None:  # pragma: no cover - protocol probe only
        del signing_ctx


def _validate_sdk_protocols() -> None:
    required_http_methods = {"send", "open", "close"}
    if not required_http_methods.issubset(AsyncHttpClient.__abstractmethods__):
        raise RuntimeError("oss_sdk_async_http_protocol_missing")
    if not callable(getattr(SignerV4, "sign", None)):
        raise RuntimeError("oss_sdk_signer_protocol_missing")
    context = SigningContext()
    if not hasattr(context, "additional_headers"):
        raise RuntimeError("oss_sdk_additional_headers_protocol_missing")

    http_client = _ProbeHttpClient()
    signer = _ProbeSigner()
    config = Config(
        region="cn-hangzhou",
        endpoint="oss-cn-hangzhou.aliyuncs.com",
        signature_version="v4",
        credentials_provider=StaticCredentialsProvider("test-id", "test-secret"),
        retry_max_attempts=1,
        http_client=http_client,
        use_cname=False,
        use_accelerate_endpoint=False,
        use_path_style=False,
        disable_ssl=False,
    )
    client = AsyncClient(config, signer=signer)
    options = getattr(getattr(client, "_client", None), "_options", None)
    if options is None or options.http_client is not http_client:
        raise RuntimeError("oss_sdk_http_client_injection_missing")
    if options.signer is not signer:
        raise RuntimeError("oss_sdk_signer_injection_missing")


def _public_async_methods() -> tuple[tuple[str, Any], ...]:
    return tuple(
        (name, method)
        for name, method in inspect.getmembers(AsyncClient, inspect.isfunction)
        if not name.startswith("_") and inspect.iscoroutinefunction(method)
    )


def _load_fixture(path: Path) -> tuple[Mapping[str, Any], Mapping[str, Mapping[str, Any]]]:
    document = json.loads(path.read_text(encoding="ascii"))
    if not isinstance(document, Mapping) or not isinstance(document.get("_meta"), Mapping):
        raise ValueError("invalid_openmeta_fixture")
    meta = document["_meta"]
    if meta.get("schema_version") != 1 or meta.get("product") != "Oss" or meta.get("version") != "2019-05-17":
        raise ValueError("invalid_openmeta_fixture_provenance")
    operations = document.get("operations")
    if not isinstance(operations, list):
        raise ValueError("invalid_openmeta_fixture_operations")
    by_method: dict[str, Mapping[str, Any]] = {}
    for operation in operations:
        if not isinstance(operation, Mapping):
            raise ValueError("invalid_openmeta_fixture_operation")
        sdk_method = operation.get("sdk_method")
        action = operation.get("action")
        digest = operation.get("document_sha256")
        if not isinstance(sdk_method, str) or not isinstance(action, str) or not isinstance(digest, str):
            raise ValueError("invalid_openmeta_fixture_operation")
        if _SHA256.fullmatch(digest) is None or sdk_method in by_method:
            raise ValueError("invalid_openmeta_fixture_operation_provenance")
        by_method[sdk_method] = operation
    return meta, by_method


def _request_model(method: Any) -> type[Any] | None:
    parameter = inspect.signature(method).parameters.get("request")
    if parameter is None or not inspect.isclass(parameter.annotation):
        return None
    model = parameter.annotation
    if not model.__name__.endswith("Request"):
        return None
    return model


def _result_model(method: Any) -> type[Any] | None:
    annotation = inspect.signature(method).return_annotation
    return annotation if inspect.isclass(annotation) and annotation.__name__.endswith("Result") else None


def _wire_name(sdk_field: str, description: Mapping[str, Any]) -> str:
    renamed = description.get("rename")
    return renamed if isinstance(renamed, str) else sdk_field


def _wire_name_matches(openmeta_name: str, sdk_name: str) -> bool:
    left = openmeta_name.casefold()
    right = sdk_name.casefold()
    if "*" not in left:
        return left == right
    prefix, suffix = left.split("*", 1)
    return right.startswith(prefix) and right.endswith(suffix)


def _field_mapping(request_model: type[Any], metadata: Mapping[str, Any]) -> tuple[list[dict[str, Any]], list[str]]:
    attributes = getattr(request_model, "_attribute_map", None)
    if not isinstance(attributes, Mapping):
        return [], ["sdk_request_attribute_map_missing"]
    parameters = metadata.get("parameters")
    if not isinstance(parameters, list):
        return [], ["openmeta_parameters_invalid"]
    result: list[dict[str, Any]] = []
    reasons: list[str] = []
    for parameter in parameters:
        if not isinstance(parameter, Mapping):
            reasons.append("openmeta_parameter_invalid")
            continue
        openmeta_name = parameter.get("name")
        location = parameter.get("location")
        if not isinstance(openmeta_name, str) or not isinstance(location, str):
            reasons.append("openmeta_parameter_invalid")
            continue
        candidates: list[tuple[str, Mapping[str, Any]]] = []
        for sdk_field, raw_description in attributes.items():
            if not isinstance(sdk_field, str) or not isinstance(raw_description, Mapping):
                continue
            if raw_description.get("tag") != "input" or raw_description.get("position") != location:
                continue
            if _wire_name_matches(openmeta_name, _wire_name(sdk_field, raw_description)):
                candidates.append((sdk_field, raw_description))
        if len(candidates) != 1:
            suffix = "missing" if not candidates else "ambiguous"
            reasons.append(f"field_mapping_{suffix}:{location}:{openmeta_name.casefold()}")
            continue
        sdk_field, description = candidates[0]
        sdk_type = description.get("type", "bytes" if location == "body" else "str")
        result.append(
            {
                "location": location,
                "openmeta_name": openmeta_name,
                "required": bool(parameter.get("required", False)),
                "sdk_field": sdk_field,
                "sdk_type": str(sdk_type),
                "wire_name": _wire_name(sdk_field, description),
            }
        )
    result.sort(key=lambda item: (item["location"], item["openmeta_name"].casefold(), item["sdk_field"]))
    return result, reasons


def _body_type(request_model: type[Any]) -> str:
    attributes = getattr(request_model, "_attribute_map", {})
    body_fields = [
        description
        for description in attributes.values()
        if isinstance(description, Mapping)
        and description.get("tag") == "input"
        and description.get("position") == "body"
    ]
    if not body_fields:
        return "none"
    if len(body_fields) != 1:
        return "ambiguous"
    sdk_type = str(body_fields[0].get("type", "byte")).casefold()
    if "xml" in sdk_type:
        return "xml"
    if "json" in sdk_type:
        return "json"
    return "byte"


def _response_mode(method: str, action: str, result_model: type[Any] | None) -> str:
    if action == "GetObject":
        return "stream"
    if method == "HEAD":
        return "headers_only"
    attributes = getattr(result_model, "_attribute_map", {}) if result_model is not None else {}
    has_body = any(
        isinstance(description, Mapping)
        and (description.get("tag") == "xml" or description.get("position") == "body" or sdk_field == "body")
        for sdk_field, description in attributes.items()
    )
    return "buffered" if has_body else "headers_only"


def _unsupported_row(sdk_method: str, reason: str) -> dict[str, Any]:
    return {
        "action": None,
        "body_type": "none",
        "field_mapping": [],
        "method": None,
        "request_model": None,
        "response_mode": "unsupported",
        "sdk_method": sdk_method,
        "supported": False,
        "unsupported_reasons": [reason],
    }


def _operation_row(
    sdk_method: str,
    method_function: Any,
    fixture_operation: Mapping[str, Any] | None,
) -> dict[str, Any]:
    if sdk_method in _HELPER_REASONS:
        return _unsupported_row(sdk_method, _HELPER_REASONS[sdk_method])
    request_model = _request_model(method_function)
    if request_model is None:
        return _unsupported_row(sdk_method, "sdk_request_model_missing")
    action = request_model.__name__.removesuffix("Request")
    reasons: list[str] = []
    metadata = fixture_operation
    if metadata is None:
        reasons.append("openmeta_operation_missing")
        metadata = {}
    elif metadata.get("action") != action:
        reasons.append("openmeta_action_mismatch")
    if not metadata.get("available", False):
        reasons.append("openmeta_operation_unavailable")
    method = metadata.get("method")
    if method is not None and (not isinstance(method, str) or method.upper() != method):
        reasons.append("openmeta_http_method_invalid")
    mapping, mapping_reasons = _field_mapping(request_model, metadata)
    reasons.extend(mapping_reasons)
    body_type = _body_type(request_model)
    if body_type not in {"none", "byte"}:
        reasons.append(f"request_body_type_unsupported:{body_type}")
    reasons = sorted(set(reasons))
    supported = not reasons and isinstance(method, str)
    response_mode = (
        _response_mode(method, action, _result_model(method_function))
        if supported and isinstance(method, str)
        else "unsupported"
    )
    return {
        "action": action,
        "body_type": body_type,
        "field_mapping": mapping,
        "method": method,
        "request_model": f"{request_model.__module__}.{request_model.__name__}",
        "response_mode": response_mode,
        "sdk_method": sdk_method,
        "supported": supported,
        "unsupported_reasons": reasons,
    }


def generate_catalog(*, sdk_version: str, sdk_wheel_sha256: str, openmeta_fixture: Path) -> dict[str, Any]:
    _validate_sdk_protocols()
    installed_version = importlib.metadata.version(_PACKAGE_NAME)
    if sdk_version != installed_version:
        raise ValueError("sdk_version_does_not_match_installed_distribution")
    if _SHA256.fullmatch(sdk_wheel_sha256) is None:
        raise ValueError("invalid_sdk_wheel_sha256")
    fixture_meta, fixture_by_method = _load_fixture(openmeta_fixture)
    methods = _public_async_methods()
    method_names = {name for name, _method in methods}
    unknown_fixture_methods = set(fixture_by_method) - method_names
    if unknown_fixture_methods:
        raise ValueError("openmeta_fixture_has_unknown_sdk_methods")
    rows = [_operation_row(name, method, fixture_by_method.get(name)) for name, method in methods]
    return {
        "_meta": {
            "generated_by": "scripts/aliyun/generate_oss_operations.py",
            "openmeta_fixture_schema_version": fixture_meta["schema_version"],
            "openmeta_fixture_sha256": hashlib.sha256(openmeta_fixture.read_bytes()).hexdigest(),
            "schema_version": _SCHEMA_VERSION,
            "sdk_version": sdk_version,
            "sdk_wheel_sha256": sdk_wheel_sha256,
        },
        "operations": rows,
    }


def wheel_hash(lockfile: Path) -> str:
    document = tomllib.loads(lockfile.read_text(encoding="utf-8"))
    packages = document.get("package")
    if not isinstance(packages, list):
        raise ValueError("invalid_uv_lock_packages")
    package_rows = [
        row
        for row in packages
        if isinstance(row, Mapping) and canonicalize_name(str(row.get("name", ""))) == canonicalize_name(_PACKAGE_NAME)
    ]
    if len(package_rows) != 1:
        raise ValueError("oss_sdk_lock_entry_not_unique")
    wheels = package_rows[0].get("wheels")
    if not isinstance(wheels, list):
        raise ValueError("oss_sdk_wheels_missing")
    installed_tags = set(sys_tags())
    matches: list[str] = []
    for wheel in wheels:
        if not isinstance(wheel, Mapping):
            continue
        url = wheel.get("url")
        digest = wheel.get("hash")
        if not isinstance(url, str) or not isinstance(digest, str):
            continue
        filename = Path(unquote(urlsplit(url).path)).name
        try:
            distribution, _version, _build, tags = parse_wheel_filename(filename)
        except ValueError:
            continue
        if canonicalize_name(distribution) != canonicalize_name(_PACKAGE_NAME) or not (tags & installed_tags):
            continue
        algorithm, separator, value = digest.partition(":")
        if algorithm != "sha256" or not separator or _SHA256.fullmatch(value) is None:
            raise ValueError("invalid_oss_sdk_wheel_hash")
        matches.append(value)
    if len(matches) != 1:
        raise ValueError("installed_platform_oss_wheel_not_unique")
    return matches[0]


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    subcommands = parser.add_subparsers(dest="command", required=True)
    generate = subcommands.add_parser("generate")
    generate.add_argument("--sdk-version", required=True)
    generate.add_argument("--sdk-wheel-sha256", required=True)
    generate.add_argument("--openmeta-fixture", type=Path, required=True)
    generate.add_argument("--output", type=Path, required=True)
    wheel = subcommands.add_parser("wheel-hash")
    wheel.add_argument("--lockfile", type=Path, required=True)
    return parser


def main() -> int:
    arguments = _parser().parse_args()
    if arguments.command == "wheel-hash":
        print(wheel_hash(arguments.lockfile))
        return 0
    catalog = generate_catalog(
        sdk_version=arguments.sdk_version,
        sdk_wheel_sha256=arguments.sdk_wheel_sha256,
        openmeta_fixture=arguments.openmeta_fixture,
    )
    encoded = (json.dumps(catalog, sort_keys=True, separators=(",", ":"), ensure_ascii=True) + "\n").encode("ascii")
    arguments.output.write_bytes(encoded)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
