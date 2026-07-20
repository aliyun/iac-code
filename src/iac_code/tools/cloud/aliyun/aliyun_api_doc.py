"""Anonymous exact Alibaba Cloud API contract documentation."""

from __future__ import annotations

import json
from collections.abc import Mapping
from typing import TYPE_CHECKING, Any

from iac_code.i18n import _
from iac_code.tools.base import Tool, ToolContext, ToolResult
from iac_code.tools.cloud.aliyun.api_contract import ApiCallShape, ApiContractError, CanonicalWireContract
from iac_code.tools.cloud.aliyun.openmeta import ApiMetadata, ParameterMetadata
from iac_code.tools.cloud.aliyun.public_errors import (
    AliyunApiIdentity,
    normalize_api_identity,
    public_aliyun_error,
    public_aliyun_unsupported_reasons,
)
from iac_code.tools.cloud.aliyun.runtime import emit_aliyun_api_doc

if TYPE_CHECKING:
    from iac_code.tools.cloud.aliyun.runtime import AliyunRuntimeServices


_DETAILS = frozenset({"summary", "full"})
_MAX_INLINE_CHARACTERS = 48_000
_DOCUMENT_REGION_SENTINEL = "cn-hangzhou"
_TRUNCATED_SECTIONS = [
    "parameters",
    "responses",
    "components",
    "error_codes",
    "change_set",
    "static_info",
]


class AliyunApiDoc(Tool):
    """Render the same canonical contract used by Alibaba Cloud execution."""

    def __init__(self, services: AliyunRuntimeServices) -> None:
        if services is None:
            raise TypeError("aliyun_runtime_services_required")
        self._services = services

    @property
    def name(self) -> str:
        return "aliyun_api_doc"

    @property
    def description(self) -> str:
        return (
            "Get the exact machine-readable Alibaba Cloud API contract for a product and action. "
            "Use detail=full for complete parameters, responses, and reachable schemas."
        )

    @property
    def input_schema(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "product": {"type": "string"},
                "action": {"type": "string"},
                "version": {"type": "string"},
                "detail": {
                    "type": "string",
                    "enum": ["summary", "full"],
                    "default": "summary",
                },
            },
            "required": ["product", "action"],
            "additionalProperties": False,
        }

    def is_read_only(self, input: dict | None = None) -> bool:
        return True

    def is_destructive(self, input: dict | None = None) -> bool:
        return False

    def user_facing_name(self, input: dict | None = None) -> str:
        return _("Aliyun API Documentation")

    @property
    def render_verbose_result_in_transcript(self) -> bool:
        return True

    def render_tool_result_message(
        self,
        output: str,
        *,
        is_error: bool = False,
        verbose: bool = False,
    ) -> str | None:
        text = output.strip()
        if not text:
            return None
        if verbose or is_error:
            return text

        try:
            document = json.loads(text)
        except (json.JSONDecodeError, UnicodeDecodeError):
            return text if len(text) <= 240 else text[:237] + "..."
        if not isinstance(document, Mapping):
            return text if len(text) <= 240 else text[:237] + "..."

        def display_value(name: str) -> str:
            value = document.get(name)
            return str(value) if value not in (None, "") else "?"

        required = document.get("required_parameters")
        optional = document.get("optional_parameters")
        executable = document.get("executable")
        return _(
            "{product}/{version} {action} | {style} {method} {path} | "
            "required={required} | optional={optional} | executable={executable}"
        ).format(
            product=display_value("product"),
            version=display_value("version"),
            action=display_value("action"),
            style=display_value("style"),
            method=display_value("method"),
            path=display_value("path"),
            required=len(required) if isinstance(required, list) else 0,
            optional=len(optional) if isinstance(optional, list) else 0,
            executable=str(executable).lower() if isinstance(executable, bool) else "?",
        )

    def validation_error_result(self, tool_input: dict[str, Any]) -> ToolResult | None:
        detail = _safe_doc_detail(tool_input)
        try:
            _normalize_doc_input(tool_input)
        except ApiContractError as error:
            emit_aliyun_api_doc(detail, "invalid_input")
            return ToolResult.error(
                public_aliyun_error(
                    error,
                    product=tool_input.get("product"),
                    version=tool_input.get("version"),
                    action=tool_input.get("action"),
                )
            )
        emit_aliyun_api_doc(detail, "invalid_input")
        return ToolResult.error(
            public_aliyun_error(
                "invalid_tool_input",
                product=tool_input.get("product"),
                version=tool_input.get("version"),
                action=tool_input.get("action"),
            )
        )

    async def execute(self, *, tool_input: dict[str, Any], context: ToolContext) -> ToolResult:
        detail = _safe_doc_detail(tool_input)
        try:
            identity = _normalize_doc_input(tool_input)
        except ApiContractError as error:
            emit_aliyun_api_doc(detail, "invalid_input")
            return ToolResult.error(
                public_aliyun_error(
                    error,
                    product=tool_input.get("product"),
                    version=tool_input.get("version"),
                    action=tool_input.get("action"),
                )
            )

        product = identity.product
        action = identity.action
        explicit_version = identity.version
        try:
            contract = await self._services.contract_resolver.resolve(
                ApiCallShape(
                    product=product,
                    version=str(explicit_version) if explicit_version is not None else None,
                    action=action,
                    region_id=_DOCUMENT_REGION_SENTINEL,
                    explicit_overrides=(),
                    parameter_names_by_location={},
                    body_source="none",
                ),
                allow_fallback=False,
            )
        except (ApiContractError, TypeError, ValueError) as error:
            emit_aliyun_api_doc(detail, "not_found" if str(error) == "product_not_found" else "contract_error")
            return ToolResult.error(
                public_aliyun_error(
                    error,
                    product=getattr(error, "product", None) or product,
                    version=explicit_version,
                    action=action,
                )
            )

        canonical_product = contract.product
        version = contract.version
        fetch_for_selection = getattr(self._services.openmeta, "get_api_for_version_selection", None)
        if callable(fetch_for_selection):
            metadata_fetch = await fetch_for_selection(canonical_product, version, action)
        else:
            metadata_fetch = await self._services.openmeta.get_api(canonical_product, version, action)
        if metadata_fetch.value is None:
            outcome = metadata_fetch.error or "protocol_error"
            emit_aliyun_api_doc(detail, outcome)
            return ToolResult.error(
                public_aliyun_error(
                    _metadata_public_error(outcome, not_found="metadata_not_found"),
                    product=canonical_product,
                    version=version,
                    action=action,
                )
            )

        try:
            document = _render_document(contract, metadata_fetch.value, detail=detail)
            content = _serialize(document)
        except (ApiContractError, TypeError, ValueError) as error:
            emit_aliyun_api_doc(detail, "contract_error")
            return ToolResult.error(
                public_aliyun_error(
                    error,
                    product=canonical_product,
                    version=version,
                    action=action,
                )
            )

        inline_content = content if detail == "summary" else _fit_inline_document(document)
        emit_aliyun_api_doc(detail, "success")
        return ToolResult.success(inline_content if inline_content is not None else content)


def _normalize_doc_input(tool_input: Mapping[str, Any]) -> AliyunApiIdentity:
    detail = tool_input.get("detail", "summary")
    if not isinstance(detail, str) or detail not in _DETAILS:
        raise ApiContractError("invalid_detail")
    if set(tool_input) - {"product", "action", "version", "detail"}:
        raise ApiContractError("invalid_tool_input")
    return normalize_api_identity(tool_input)


def _safe_doc_detail(tool_input: Mapping[str, Any]) -> str:
    detail = tool_input.get("detail", "summary")
    return detail if isinstance(detail, str) and detail in _DETAILS else "summary"


def _metadata_public_error(outcome: str, *, not_found: str) -> str:
    if outcome == "not_found":
        return not_found
    if outcome == "temporarily_unavailable":
        return "metadata_unavailable"
    return "metadata_protocol_error"


def _render_document(
    contract: CanonicalWireContract,
    metadata: ApiMetadata,
    *,
    detail: str,
) -> dict[str, Any]:
    parameters = contract.parameters
    document_parameters = getattr(metadata, "document_parameters", parameters)
    document_parameter_index = {(parameter.name, parameter.location): parameter for parameter in document_parameters}
    components, _document_reasons = _reachable_components(metadata, parameters, document_parameter_index)
    result: dict[str, Any] = {
        "product": contract.product,
        "version": contract.version,
        "action": contract.action,
        "summary": getattr(metadata, "summary", None),
        "style": contract.style,
        "method": contract.method,
        "path": contract.pathname,
        "operation_type": contract.operation_type,
        "executable": contract.executable,
        "unsupported_reasons": public_aliyun_unsupported_reasons(
            contract.unsupported_reasons,
            product=contract.product,
            action=contract.action,
        ),
        "documentation_url": _documentation_url(contract, metadata),
        "required_parameters": [_summary_parameter(parameter) for parameter in parameters if parameter.required],
        "optional_parameters": [parameter.name for parameter in parameters if not parameter.required],
    }
    if detail == "summary":
        return result

    result.update(
        {
            "parameters": [
                _full_parameter(
                    parameter,
                    document_parameter_index.get((parameter.name, parameter.location)),
                )
                for parameter in parameters
            ],
            "consumes": list(contract.consumes),
            "produces": list(contract.produces),
            "schemes": list(metadata.schemes),
            "security": _security_view(metadata),
            "deprecated": bool(getattr(metadata, "deprecated", False)),
            "responses": _json_value(metadata.responses),
            "components": components,
            "error_codes": _error_code_index(getattr(metadata, "error_codes", {})),
            "change_set": _json_value(getattr(metadata, "change_set", ())),
            "static_info": _json_value(getattr(metadata, "static_info", {})),
        }
    )
    return result


def _summary_parameter(parameter: ParameterMetadata) -> dict[str, Any]:
    schema = parameter.schema if isinstance(parameter.schema, Mapping) else {}
    result: dict[str, Any] = {"name": parameter.name, "in": parameter.location}
    for key, value in (
        ("type", schema.get("type")),
        ("style", parameter.style),
        ("path_encoding", parameter.path_encoding),
        ("format", schema.get("format")),
        ("enum", schema.get("enum")),
        ("example", parameter.example),
    ):
        if value is not None:
            result[key] = _json_value(value)
    return result


def _full_parameter(
    parameter: ParameterMetadata,
    document_parameter: ParameterMetadata | None,
) -> dict[str, Any]:
    view = document_parameter or parameter
    return {
        "name": parameter.name,
        "in": parameter.location,
        "required": parameter.required,
        "schema": _json_value(view.schema),
        "description": view.description,
        "example": _json_value(view.example),
    }


def _security_view(metadata: ApiMetadata) -> Any:
    if not metadata.security_declared:
        return None
    result: list[dict[str, list[str]]] = []
    for requirement in metadata.security_requirements:
        result.append({scheme: list(requirement.scopes[index]) for index, scheme in enumerate(requirement.schemes)})
    return result


def _documentation_url(contract: CanonicalWireContract, metadata: ApiMetadata) -> str:
    value = getattr(metadata, "documentation_url", None)
    if isinstance(value, str) and value:
        return value
    return "https://api.aliyun.com/api/{}/{}/{}".format(
        contract.product,
        contract.version,
        contract.action,
    )


def _reachable_components(
    metadata: ApiMetadata,
    parameters: tuple[ParameterMetadata, ...],
    document_parameters: Mapping[tuple[str, str], ParameterMetadata],
) -> tuple[dict[str, Any], tuple[str, ...]]:
    document_components = getattr(metadata, "document_components", {})
    validation_components = getattr(metadata, "validation_components", {})
    document_schemas = _mapping(document_components).get("schemas", {})
    validation_schemas = _mapping(validation_components).get("schemas", {})
    document_schemas = _mapping(document_schemas)
    validation_schemas = _mapping(validation_schemas)
    roots: list[str] = []
    reasons: list[str] = []

    for parameter in parameters:
        if parameter.schema is None:
            reasons.append("parameter_schema_reference_unsupported")
            continue
        document_parameter = document_parameters.get((parameter.name, parameter.location), parameter)
        _collect_references(document_parameter.schema, document_schemas, roots, reasons)
        parameter_value = _json_value(parameter.schema)
        for name, schema in validation_schemas.items():
            if isinstance(name, str) and _json_value(schema) == parameter_value:
                roots.append(name)
                break
    _collect_references(metadata.responses, document_schemas, roots, reasons)

    selected: set[str] = set()
    pending = list(dict.fromkeys(roots))
    while pending:
        name = pending.pop(0)
        if name in selected:
            continue
        schema = document_schemas.get(name)
        if not isinstance(schema, Mapping):
            reasons.append("schema_reference_not_found")
            continue
        selected.add(name)
        nested: list[str] = []
        _collect_references(schema, document_schemas, nested, reasons)
        pending.extend(item for item in nested if item not in selected)

    ordered = {
        str(name): _json_value(schema)
        for name, schema in document_schemas.items()
        if isinstance(name, str) and name in selected
    }
    return {"schemas": ordered}, tuple(dict.fromkeys(reasons))


def _collect_references(
    value: Any,
    schemas: Mapping[str, Any],
    roots: list[str],
    reasons: list[str],
) -> None:
    if isinstance(value, Mapping):
        reference = value.get("$ref")
        if isinstance(reference, str):
            prefix = "#/components/schemas/"
            if not reference.startswith(prefix):
                reasons.append("schema_reference_unsupported")
            else:
                name = reference.removeprefix(prefix)
                if not name or not isinstance(schemas.get(name), Mapping):
                    reasons.append("schema_reference_not_found")
                else:
                    roots.append(name)
        for item in value.values():
            _collect_references(item, schemas, roots, reasons)
    elif isinstance(value, list | tuple):
        for item in value:
            _collect_references(item, schemas, roots, reasons)


def _error_code_index(value: Any) -> dict[str, list[str]]:
    if not isinstance(value, Mapping):
        return {}
    result: dict[str, list[str]] = {}
    for status, rows in value.items():
        names: list[str] = []
        if isinstance(rows, Mapping):
            rows = (rows,)
        if isinstance(rows, list | tuple):
            for row in rows:
                if not isinstance(row, Mapping):
                    continue
                name = row.get("Code", row.get("code", row.get("name")))
                if isinstance(name, str) and name:
                    names.append(name)
        result[str(status)] = names
    return result


def _mapping(value: Any) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def _json_value(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {str(key): _json_value(item) for key, item in value.items()}
    if isinstance(value, list | tuple):
        return [_json_value(item) for item in value]
    return value


def _serialize(document: Mapping[str, Any]) -> str:
    return json.dumps(document, ensure_ascii=False, separators=(",", ":"))


def _fit_inline_document(document: Mapping[str, Any]) -> str | None:
    full_content = _serialize(document)
    if len(full_content) <= _MAX_INLINE_CHARACTERS:
        return full_content

    candidate = _json_value(document)
    truncated: list[str] = []

    def serialized_if_fits() -> str | None:
        candidate["truncated_sections"] = list(truncated)
        content = _serialize(candidate)
        return content if len(content) <= _MAX_INLINE_CHARACTERS else None

    for section in ("static_info", "change_set", "error_codes"):
        value = candidate.get(section)
        if value not in (None, {}, []):
            candidate.pop(section, None)
            truncated.append(section)
            if content := serialized_if_fits():
                return content

    responses = candidate.get("responses")
    if _remove_response_annotations(responses):
        truncated.append("response_annotations")
        if content := serialized_if_fits():
            return content

    if _remove_document_schema_annotations(candidate):
        truncated.append("schema_annotations")
        if content := serialized_if_fits():
            return content

    required_parameters = candidate.get("required_parameters")
    if _remove_required_parameter_schema_summaries(required_parameters):
        truncated.append("required_parameter_schema_summaries")
        if content := serialized_if_fits():
            return content

    if candidate.get("summary") not in (None, ""):
        candidate.pop("summary", None)
        truncated.append("summary")
        if content := serialized_if_fits():
            return content

    if "parameters" in candidate and any(
        section in candidate for section in ("required_parameters", "optional_parameters")
    ):
        candidate.pop("required_parameters", None)
        candidate.pop("optional_parameters", None)
        truncated.append("parameter_summary_indexes")
        if content := serialized_if_fits():
            return content

    parameters = candidate.get("parameters")
    if isinstance(parameters, list):
        for field, section in (("example", "parameter_examples"), ("description", "parameter_descriptions")):
            removed = False
            for parameter in parameters:
                if isinstance(parameter, dict) and parameter.get(field) is not None:
                    parameter.pop(field, None)
                    removed = True
            if field == "example":
                required_parameters = candidate.get("required_parameters")
                if isinstance(required_parameters, list):
                    for parameter in required_parameters:
                        if isinstance(parameter, dict) and parameter.get(field) is not None:
                            parameter.pop(field, None)
                            removed = True
            if removed:
                truncated.append(section)
                if content := serialized_if_fits():
                    return content

    return None


def _remove_response_annotations(value: Any) -> bool:
    removed = False
    if isinstance(value, dict):
        for key in list(value):
            if key in {"description", "example", "examples"} or (isinstance(key, str) and key.startswith("x-")):
                value.pop(key, None)
                removed = True
                continue
            if key != "schema":
                removed = _remove_response_annotations(value[key]) or removed
    elif isinstance(value, list):
        for item in value:
            removed = _remove_response_annotations(item) or removed
    return removed


def _remove_document_schema_annotations(document: Mapping[str, Any]) -> bool:
    removed = False
    parameters = document.get("parameters")
    if isinstance(parameters, list):
        for parameter in parameters:
            if isinstance(parameter, dict):
                removed = _remove_schema_annotations(parameter.get("schema")) or removed
    removed = _remove_nested_response_schema_annotations(document.get("responses")) or removed
    components = document.get("components")
    if isinstance(components, dict):
        schemas = components.get("schemas")
        if isinstance(schemas, dict):
            for schema in schemas.values():
                removed = _remove_schema_annotations(schema) or removed
    return removed


def _remove_nested_response_schema_annotations(value: Any) -> bool:
    removed = False
    if isinstance(value, dict):
        for key, item in value.items():
            if key == "schema":
                removed = _remove_schema_annotations(item) or removed
            else:
                removed = _remove_nested_response_schema_annotations(item) or removed
    elif isinstance(value, list):
        for item in value:
            removed = _remove_nested_response_schema_annotations(item) or removed
    return removed


def _remove_schema_annotations(value: Any) -> bool:
    removed = False
    if isinstance(value, dict):
        for key in list(value):
            if key in {
                "description",
                "example",
                "examples",
                "externalDocs",
                "title",
            } or (isinstance(key, str) and key.startswith("x-")):
                value.pop(key, None)
                removed = True
                continue
            removed = _remove_schema_annotations(value[key]) or removed
    elif isinstance(value, list):
        for item in value:
            removed = _remove_schema_annotations(item) or removed
    return removed


def _remove_required_parameter_schema_summaries(value: Any) -> bool:
    removed = False
    if not isinstance(value, list):
        return False
    for parameter in value:
        if not isinstance(parameter, dict):
            continue
        for field in ("type", "format", "enum", "example"):
            if field in parameter:
                parameter.pop(field, None)
                removed = True
    return removed
