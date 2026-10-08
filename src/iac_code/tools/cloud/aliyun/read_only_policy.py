"""Reviewed read permission compatibility for concrete IaCService routes."""

from __future__ import annotations

import re
from collections.abc import Mapping
from typing import Any
from urllib.parse import unquote_to_bytes

from iac_code.tools.cloud.aliyun.api_contract import (
    ApiCallShape,
    ApiContractError,
    CanonicalWireContract,
    _encode_path,
    _validate_parameter,
)


class IacServiceReadOnlyPolicy:
    """Accept a canonical concrete GET route only under its trusted API metadata.

    The policy has no process-local authorization state. Permission checks and
    snapshot recovery apply the same route and body constraints.
    """

    _TRUSTED_SOURCES = frozenset({"fresh", "cache", "stale_cache"})
    _PLACEHOLDER = re.compile(r"\{([^{}]+)\}")
    _ENCODED_SEGMENT = r"(?:[A-Za-z0-9_.~-]|%[0-9A-F]{2})+"

    def matches(
        self,
        contract: CanonicalWireContract,
        shape: ApiCallShape,
        metadata: CanonicalWireContract,
        tool_input: Mapping[str, Any] | None,
    ) -> bool:
        if (
            contract.product != "IaCService"
            or contract.version != "2021-08-06"
            or (contract.product, contract.version, contract.action)
            != (metadata.product, metadata.version, metadata.action)
            or contract.metadata_source not in self._TRUSTED_SOURCES
            or metadata.metadata_source not in self._TRUSTED_SOURCES
            or not contract.executable
            or not metadata.executable
            or contract.operation_type != "read"
            or metadata.operation_type != "read"
            or contract.style != "ROA"
            or metadata.style != "ROA"
            or contract.method != "GET"
            or metadata.method != "GET"
            or shape.body_source != "none"
            or "pathname" not in shape.explicit_overrides
            or shape.pathname != contract.pathname
        ):
            return False
        for field_name in shape.explicit_overrides:
            if field_name != "pathname":
                value = getattr(shape, field_name)
                if not isinstance(value, str) or value.upper() != getattr(metadata, field_name):
                    return False
        params = tool_input.get("params", {}) if tool_input is not None else {}
        if not isinstance(params, Mapping):
            return False
        return self._matches_route(metadata, contract.pathname, params)

    def _matches_route(self, metadata: CanonicalWireContract, pathname: str, params: Mapping[str, Any]) -> bool:
        parameters = {parameter.name: parameter for parameter in metadata.parameters if parameter.location == "path"}
        placeholders = list(self._PLACEHOLDER.finditer(metadata.pathname))
        if not placeholders:
            return pathname == metadata.pathname
        pattern_parts: list[str] = []
        cursor = 0
        for index, placeholder in enumerate(placeholders):
            parameter = parameters.get(placeholder.group(1))
            if parameter is None or not parameter.schema or parameter.schema.get("type") != "string":
                return False
            pattern_parts.append(re.escape(metadata.pathname[cursor : placeholder.start()]))
            value_pattern = self._ENCODED_SEGMENT
            if parameter.path_encoding == "preserve_slashes":
                value_pattern += r"(?:/" + self._ENCODED_SEGMENT + r")*"
            pattern_parts.append("(?P<value{}>{})".format(index, value_pattern))
            cursor = placeholder.end()
        pattern_parts.append(re.escape(metadata.pathname[cursor:]))
        matched = re.fullmatch("".join(pattern_parts), pathname)
        if matched is None:
            return False
        values: dict[str, str] = {}
        try:
            for index, placeholder in enumerate(placeholders):
                name = placeholder.group(1)
                parameter = parameters[name]
                encoded = matched.group("value{}".format(index))
                decoded = unquote_to_bytes(encoded).decode("utf-8")
                if (
                    "\\" in decoded
                    or any(ord(character) < 32 or ord(character) == 127 for character in decoded)
                    or any(segment in {".", ".."} for segment in decoded.split("/"))
                ):
                    return False
                _validate_parameter(parameter, decoded)
                if _encode_path(decoded, parameter.path_encoding, parameter_name=name) != encoded:
                    return False
                if name in values and values[name] != encoded:
                    return False
                values[name] = encoded
                if name in params:
                    _validate_parameter(parameter, params[name])
                    if _encode_path(params[name], parameter.path_encoding, parameter_name=name) != encoded:
                        return False
        except (ApiContractError, UnicodeError):
            return False
        return True
