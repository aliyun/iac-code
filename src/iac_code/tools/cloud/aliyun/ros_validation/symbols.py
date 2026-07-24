"""Template symbol collection and Ref/GetAtt base contracts."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Mapping

from iac_code.i18n import _
from iac_code.tools.cloud.aliyun.ros_validation.model import EvaluationMode
from iac_code.tools.cloud.aliyun.ros_validation.resource_value_specs import ResourceValueSpecRegistry
from iac_code.tools.cloud.aliyun.ros_validation.types import (
    ANY_VALUE,
    BOOLEAN,
    JSON_DECODED_VALUE,
    NO_VALUE,
    NULL,
    NUMBER,
    STRING,
    InferredValue,
    RosType,
    list_of,
    parse_json_parameter,
    union_of,
)


@dataclass(frozen=True)
class CountInfo:
    declared: bool = False
    length: int | None = None
    valid: bool = True


@dataclass(frozen=True)
class ResourceSymbol:
    name: str
    resource_type: str
    base_ref_type: RosType
    attribute_types: Mapping[str, RosType] = field(default_factory=dict)
    count_info: CountInfo = CountInfo()
    entity_type: str | None = None


@dataclass(frozen=True)
class LocalSymbol:
    name: str
    local_type: str
    value: Any
    properties: Any = None
    inferred: InferredValue | None = None


@dataclass(frozen=True)
class TemplateSymbols:
    parameters: Mapping[Any, InferredValue]
    resources: Mapping[str, ResourceSymbol]
    mappings: Mapping[str, Any]
    conditions: frozenset[str]
    locals: Mapping[str, LocalSymbol]
    pseudo_parameters: Mapping[str, InferredValue]


def _known_parameter_value(schema: Mapping[str, Any], bindings: Mapping[Any, Any], name: Any) -> tuple[bool, Any]:
    if name in bindings:
        return True, bindings[name]
    if "Default" in schema:
        return True, schema.get("Default")
    return False, None


def infer_parameter(
    name: Any,
    schema: Mapping[str, Any],
    bindings: Mapping[Any, Any],
) -> tuple[InferredValue, str | None]:
    parameter_type = schema.get("Type")
    known, raw = _known_parameter_value(schema, bindings, name)
    if parameter_type == "Json":
        if known:
            return parse_json_parameter(raw)
        return InferredValue.dynamic(JSON_DECODED_VALUE), None
    if known:
        if parameter_type == "Number":
            if isinstance(raw, str):
                try:
                    raw = int(raw) if raw.strip().lstrip("+-").isdigit() else float(raw)
                except ValueError:
                    return InferredValue.invalid(), _("Number argument cannot be converted to a number")
        elif parameter_type == "CommaDelimitedList" and isinstance(raw, str):
            raw = [] if raw == "" else raw.split(",")
        elif parameter_type == "Boolean" and raw is not None:
            normalized = str(raw).lower()
            if normalized in {"1", "t", "true", "on", "y", "yes"}:
                raw = True
            elif normalized in {"0", "f", "false", "off", "n", "no"}:
                raw = False
            else:
                return InferredValue.invalid(), _("Boolean argument cannot be converted to a Boolean")
        return InferredValue.constant(raw), None
    if parameter_type == "String":
        return InferredValue.dynamic(ANY_VALUE), None
    if parameter_type == "Number":
        return InferredValue.dynamic(union_of(NUMBER, BOOLEAN, NULL)), None
    if parameter_type == "CommaDelimitedList":
        return InferredValue.dynamic(union_of(list_of(ANY_VALUE), NULL)), None
    if parameter_type == "Boolean":
        return InferredValue.dynamic(union_of(BOOLEAN, NULL)), None
    if parameter_type == "None":
        return InferredValue.constant(None), None
    if parameter_type in {"ALIYUN::OOS::Parameter::Value", "ALIYUN::OOS::SecretParameter::Value"}:
        return InferredValue.dynamic(union_of(STRING, list_of(STRING), NULL)), None
    return InferredValue.dynamic(ANY_VALUE), None


def pseudo_parameters() -> dict[str, InferredValue]:
    result = {
        name: InferredValue.dynamic(STRING)
        for name in (
            "ALIYUN::StackId",
            "ALIYUN::StackName",
            "ALIYUN::Region",
            "ALIYUN::AccountId",
            "ALIYUN::TenantId",
            "ALIYUN::ResourceGroupId",
        )
    }
    result["ALIYUN::NoValue"] = InferredValue.dynamic(NO_VALUE, may_refer_no_value=True)
    return result


def collect_symbols(
    template: Mapping[str, Any],
    *,
    resource_specs: ResourceValueSpecRegistry,
    evaluation_mode: EvaluationMode,
    parameter_bindings: Mapping[Any, Any] | None = None,
) -> tuple[TemplateSymbols, dict[Any, str]]:
    parameter_bindings = parameter_bindings or {}
    parameters: dict[Any, InferredValue] = {}
    parameter_errors: dict[Any, str] = {}
    raw_parameters = template.get("Parameters") or {}
    if isinstance(raw_parameters, Mapping):
        for name, schema in raw_parameters.items():
            if not isinstance(schema, Mapping):
                continue
            inferred, error = infer_parameter(name, schema, parameter_bindings)
            parameters[name] = inferred
            if error:
                parameter_errors[name] = error

    resources: dict[str, ResourceSymbol] = {}
    raw_resources = template.get("Resources") or {}
    if isinstance(raw_resources, Mapping):
        for raw_name, definition in raw_resources.items():
            if not isinstance(raw_name, str) or not isinstance(definition, Mapping):
                continue
            resource_type = definition.get("Type")
            if not isinstance(resource_type, str):
                continue
            count = definition.get("Count")
            count_info = CountInfo(declared="Count" in definition)
            if "Count" in definition:
                if isinstance(count, bool) or isinstance(count, float):
                    count_info = CountInfo(True, None, False)
                elif isinstance(count, int):
                    count_info = CountInfo(True, count if count >= 0 else None, count >= 0)
                elif isinstance(count, str):
                    try:
                        parsed = int(count)
                    except ValueError:
                        count_info = CountInfo(True, None, False)
                    else:
                        count_info = CountInfo(True, parsed if parsed >= 0 else None, parsed >= 0)
            ref_type = resource_specs.ref_type(resource_type)
            if evaluation_mode in {EvaluationMode.QUERY_PARAM, EvaluationMode.INQUIRY} and not resource_type.startswith(
                "DATASOURCE::"
            ):
                ref_type = NULL
            spec = resource_specs.get(resource_type)
            resources[raw_name] = ResourceSymbol(
                name=raw_name,
                resource_type=resource_type,
                base_ref_type=ref_type,
                attribute_types=spec.attribute_types if spec else {},
                count_info=count_info,
            )

    mappings = template.get("Mappings") or {}
    conditions = template.get("Conditions") or {}
    raw_locals = template.get("Locals") or {}
    locals_: dict[str, LocalSymbol] = {}
    if isinstance(raw_locals, Mapping):
        for name, definition in raw_locals.items():
            if not isinstance(name, str) or not isinstance(definition, Mapping):
                continue
            local_type = str(definition.get("Type") or "Macro")
            locals_[name] = LocalSymbol(
                name,
                local_type,
                definition.get("Value"),
                definition.get("Properties"),
            )
    return (
        TemplateSymbols(
            parameters=parameters,
            resources=resources,
            mappings=mappings if isinstance(mappings, Mapping) else {},
            conditions=frozenset(conditions if isinstance(conditions, Mapping) else ()),
            locals=locals_,
            pseudo_parameters=pseudo_parameters(),
        ),
        parameter_errors,
    )
