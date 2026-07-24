"""Tolerant, non fail-fast ROS expression and template analyzer."""

# Function handlers deliberately mirror ROS intrinsic names for table-driven
# dispatch, and diagnostics are kept as single source strings for translation.
# ruff: noqa: E501, N802

from __future__ import annotations

import ast
import binascii
import ipaddress
import json
import math
import re
import string
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import replace
from types import MappingProxyType
from typing import Any, cast

from dateutil import tz

from iac_code.i18n import _
from iac_code.tools.cloud.aliyun.ros_validation.count import (
    CountSelectFoldFact,
    fold_count_select,
    getatt_count_eligibility,
    ref_count_eligibility,
)
from iac_code.tools.cloud.aliyun.ros_validation.function_specs import (
    ExpressionContext,
    FunctionSpec,
    function_spec,
)
from iac_code.tools.cloud.aliyun.ros_validation.model import (
    Category,
    Diagnostic,
    EvaluationMode,
    MappingKeySegment,
    ParsedTemplate,
    RelatedLocation,
    RosPath,
    SequenceIndexSegment,
    Severity,
    TemplateSemanticMode,
    ValidationPolicy,
    make_diagnostic,
    mapping_segment,
    path_identity,
)
from iac_code.tools.cloud.aliyun.ros_validation.resource_value_specs import ResourceValueSpecRegistry
from iac_code.tools.cloud.aliyun.ros_validation.symbols import (
    CountInfo,
    ResourceSymbol,
    TemplateSymbols,
)
from iac_code.tools.cloud.aliyun.ros_validation.types import (
    ANY_VALUE,
    BOOLEAN,
    HASHABLE_SCALAR,
    INTEGER,
    NULL,
    NUMBER,
    STRING,
    UNKNOWN_TYPE,
    Compatibility,
    FloatCoercionOutcome,
    InferredValue,
    RosType,
    TypeKind,
    ValueKnowledge,
    compatibility,
    float_coercion,
    infer_mapping_key_type,
    is_json_serializable_value,
    list_of,
    map_of,
    normalize,
    union_of,
)

MAX_FUNCTION_DEPTH = 20
MAX_SEMANTIC_VISITS = 500_000

_PLACEHOLDER = re.compile(r"\$\{([^}]*)\}")
_RESOURCE_TYPE_CORRECTIONS = {
    "ALIYUN::VPC::VPC": "ALIYUN::ECS::VPC",
    "ALIYUN::VPC::VSwitch": "ALIYUN::ECS::VSwitch",
}
_EXISTING_VPC_ASSOCIATIONS = {"ALIYUN::ECS::VPC::VPCId", "ALIYUN::VPC::VPCId"}
_DELETION_POLICIES = frozenset({"Delete", "Retain"})
_LOCAL_INELIGIBLE_RESOURCE_SECTIONS = frozenset({"DependsOn", "Metadata", "UpdatePolicy", "DeletionPolicy"})
_CONSTRUCTOR_UNKNOWN = object()
_CONDITION_ROOT_FUNCTIONS = {"Fn::Equals", "Ref", "Fn::FindInMap", "Fn::Not", "Fn::And", "Fn::Or"}
_CALCULATE_EXPRESSION = re.compile(r"^[\d.{}()+\-*/%\s]+$")
_BASE64_PATTERN = re.compile(r"^([A-Za-z0-9+/]{4})*([A-Za-z0-9+/]{4}|[A-Za-z0-9+/]{3}=|[A-Za-z0-9+/]{2}==)$")
_JQ_ENV = re.compile(r"\$ENV\b")
_SUB_STRING_PATTERN = re.compile(r"(\s*[a-zA-Z!][a-zA-Z0-9_:.]*\s*)")
_SCRIPT_CONTENT_PROPERTIES = frozenset(
    {
        "BootstrapScripts",
        "Command",
        "CommandArgs",
        "CommandContent",
        "CommandContentOnDeletion",
        "InitScript",
        "InitializationScript",
        "InstallCommand",
        "LivenessProbeExecCommands",
        "PostInstallScript",
        "ReadinessProbeExecCommands",
        "Script",
        "ScriptArgs",
        "ScriptContent",
        "ShellScript",
        "StartupScript",
        "UserCommand",
        "UserData",
        "UserDataInBase64",
    }
)
_CALCULATE_AST_NODES = (
    ast.Expression,
    ast.BinOp,
    ast.UnaryOp,
    ast.Constant,
    ast.Add,
    ast.Sub,
    ast.Mult,
    ast.Div,
    ast.FloorDiv,
    ast.Mod,
    ast.UAdd,
    ast.USub,
)


def _key(path: RosPath, value: Any) -> RosPath:
    return path + (mapping_segment(value),)


def _index(path: RosPath, value: int) -> RosPath:
    return path + (SequenceIndexSegment(value),)


def _is_mapping(value: InferredValue) -> bool:
    return compatibility(value.type, map_of()) != Compatibility.DEFINITE_MISMATCH


def _is_list(value: InferredValue) -> bool:
    return compatibility(value.type, list_of()) != Compatibility.DEFINITE_MISMATCH


def _members(ros_type: RosType) -> tuple[RosType, ...]:
    return ros_type.members if ros_type.kind == TypeKind.UNION else (ros_type,)


def _contains_kind(ros_type: RosType, *kinds: TypeKind) -> bool:
    return any(member.kind in kinds for member in _members(ros_type))


def _list_item_type(ros_type: RosType) -> RosType | None:
    item_types = [
        member.item_type
        for member in _members(ros_type)
        if member.kind == TypeKind.LIST and member.item_type is not None
    ]
    return union_of(*item_types) if item_types else None


def _raw_guarantees_non_empty_list(value: Any) -> bool:
    if isinstance(value, list):
        return bool(value)
    if not (isinstance(value, Mapping) and len(value) == 1 and "Fn::Split" in value):
        return False
    split_args = value["Fn::Split"]
    if not (isinstance(split_args, list) and len(split_args) == 2):
        return False
    content = split_args[1]
    if isinstance(content, str):
        return True
    return (
        isinstance(content, Mapping)
        and len(content) == 1
        and isinstance(content.get("Ref"), str)
        and content["Ref"].startswith("ALIYUN::")
    )


def _raw_function_name(value: Any) -> str | None:
    if not isinstance(value, Mapping) or len(value) != 1:
        return None
    name = next(iter(value))
    return name if isinstance(name, str) and function_spec(name) is not None else None


def _is_script_content_path(path: RosPath) -> bool:
    in_resources = False
    in_properties = False
    for segment in path:
        if not isinstance(segment, MappingKeySegment) or not isinstance(segment.value, str):
            continue
        if segment.value == "Resources":
            in_resources = True
            continue
        if in_resources and segment.value == "Properties":
            in_properties = True
            continue
        if in_properties and segment.value in _SCRIPT_CONTENT_PROPERTIES:
            return True
    return False


def _raw_ref_names(value: Any) -> frozenset[str]:
    names: set[str] = set()

    def visit(node: Any) -> None:
        if isinstance(node, Mapping):
            if len(node) == 1 and isinstance(node.get("Ref"), str):
                names.add(node["Ref"])
                return
            for child in node.values():
                visit(child)
        elif isinstance(node, list):
            for child in node:
                visit(child)

    visit(value)
    return frozenset(names)


class ExpressionAnalyzer:
    def __init__(
        self,
        parsed: ParsedTemplate,
        symbols: TemplateSymbols,
        resource_specs: ResourceValueSpecRegistry,
        *,
        evaluation_mode: EvaluationMode,
        semantic_mode: TemplateSemanticMode,
        policy: ValidationPolicy,
        parameter_bindings: Mapping[Any, Any] | None = None,
    ) -> None:
        self.parsed = parsed
        self.symbols = symbols
        self.resource_specs = resource_specs
        self.evaluation_mode = evaluation_mode
        self.semantic_mode = semantic_mode
        self.policy = policy
        self.parameter_bindings = parameter_bindings or {}
        self.diagnostics: list[Diagnostic] = []
        self.analysis_incomplete = False
        self.visits = 0
        self._local_stack: list[str] = []
        self._local_eval_depth = 0
        self._local_temporary_depth = 0
        self._eval_reparse_depth = 0
        self._condition_values: dict[str, InferredValue] = {}
        self._count_index_enabled = False
        self._count_index_value: int | None = None
        self.count_select_facts: Mapping[tuple[tuple[str, ...], int | None], CountSelectFoldFact] = MappingProxyType({})
        self._unknown_paths: set[tuple[str, ...]] = set()
        self._poisoned_locals: set[str] = set()
        self._cyclic_locals: set[str] = set()
        self._poisoned_symbols: set[Any] = set()
        self._local_origin_frames: list[tuple[RosPath, RosPath]] = []

    def _remap_local_path(
        self,
        path: RosPath,
        frames: list[tuple[RosPath, RosPath]] | None = None,
    ) -> RosPath:
        remapped = path
        for expanded_path, origin_path in reversed(self._local_origin_frames if frames is None else frames):
            if remapped[: len(expanded_path)] == expanded_path:
                remapped = origin_path + remapped[len(expanded_path) :]
        return remapped

    def _is_poisoned_symbol(self, name: Any) -> bool:
        try:
            return name in self._poisoned_symbols
        except TypeError:
            return False

    def diagnostic(
        self,
        code: str,
        summary: str,
        detail: str,
        path: RosPath,
        *,
        severity: Severity = Severity.ERROR,
        category: Category = Category.COMPATIBILITY,
        subject: str | None = None,
        stable_args: tuple[str, ...] = (),
        expected: str | None = None,
        actual: str | None = None,
        suggestion: str | None = None,
        related_locations: tuple[RelatedLocation, ...] = (),
    ) -> None:
        effective_path = self._remap_local_path(path)
        diagnostic = make_diagnostic(
            code=code,
            severity=severity,
            category=category,
            summary=summary,
            detail=detail,
            path=effective_path,
            source_map=self.parsed.source_map,
            subject=subject,
            stable_args=stable_args,
            expected=expected,
            actual=actual,
            suggestion=suggestion,
        )
        additional_locations = list(related_locations)
        for index, (expanded_path, _origin_path) in enumerate(self._local_origin_frames):
            consumer_path = self._remap_local_path(expanded_path, self._local_origin_frames[:index])
            consumer_node = self.parsed.source_map.node_for(consumer_path)
            additional_locations.append(
                RelatedLocation(
                    _("Local reference location"),
                    consumer_node.span if consumer_node is not None else None,
                    consumer_path,
                )
            )
        combined_locations = list(diagnostic.related_locations)
        for location in additional_locations:
            if not any(
                existing.path == location.path and existing.source_span == location.source_span
                for existing in combined_locations
            ):
                combined_locations.append(location)
        if combined_locations:
            diagnostic = replace(diagnostic, related_locations=tuple(combined_locations))
        self.diagnostics.append(diagnostic)

    def _stage_result(self, start: int) -> list[Diagnostic]:
        return self.diagnostics[start:]

    def _report_unknown_type(self, path: RosPath, *, provenance: str) -> None:
        identity = tuple(str(item) for item in path)
        if identity in self._unknown_paths:
            return
        self._unknown_paths.add(identity)
        self.diagnostic(
            "ROS9002",
            _("The ROS value type of this expression cannot be determined locally."),
            _(
                "The value is outside the modelled String, Number, Boolean, List, Map, and Null boundary; deterministic type-error derivation has stopped."
            ),
            path,
            severity=Severity.LIMITATION,
            category=Category.LIMITATION,
            stable_args=(provenance, "unknown-type"),
        )

    def analyze_structure(self) -> list[Diagnostic]:
        """Compatibility entry point for callers that do not use the registry."""

        diagnostics = self.analyze_structure_core()
        diagnostics.extend(self.analyze_locals_precompile())
        diagnostics.extend(self.analyze_count_precompile())
        return diagnostics

    def analyze_structure_core(self) -> list[Diagnostic]:
        start = len(self.diagnostics)
        data = self.parsed.data
        if not isinstance(data, Mapping):
            self.diagnostic(
                "ROS1005",
                _("The top level of a ROS template must be a Mapping."),
                _("The parsed result is not an object, so the template structure cannot be analyzed."),
                (),
                expected="Map",
                actual=type(data).__name__,
            )
            return self._stage_result(start)

        self._structure(data)
        self._module_structure(data)
        if self.policy == ValidationPolicy.STRICT:
            self._nonfinite_number_quality(data, ())
        self._symbol_conflicts(data)
        return self._stage_result(start)

    def analyze_locals_precompile(self) -> list[Diagnostic]:
        start = len(self.diagnostics)
        data = self.parsed.data
        if isinstance(data, Mapping):
            self._local_structure(data)
        return self._stage_result(start)

    def analyze_count_precompile(self) -> list[Diagnostic]:
        start = len(self.diagnostics)
        data = self.parsed.data
        if isinstance(data, Mapping):
            self._resolve_counts(data)
        return self._stage_result(start)

    def analyze_conditions_and_rules(self) -> list[Diagnostic]:
        start = len(self.diagnostics)
        data = self.parsed.data
        if not isinstance(data, Mapping):
            return self._stage_result(start)
        conditions = data.get("Conditions") or {}
        if isinstance(conditions, Mapping):
            self._check_condition_graph(conditions)
            for name, expression in conditions.items():
                path = _key(_key((), "Conditions"), name)
                if isinstance(expression, Mapping) and len(expression) == 1:
                    function_name = next(iter(expression))
                    if function_name not in _CONDITION_ROOT_FUNCTIONS:
                        self.diagnostic(
                            "ROS2002",
                            _("Function {} cannot be used as a Condition root expression.").format(function_name),
                            _(
                                "Condition roots register only Equals, Ref, FindInMap, Not, And, and Or; child expressions may use the extended function table."
                            ),
                            _key(path, function_name),
                            stable_args=(str(function_name), "condition-root"),
                        )
                inferred = self.analyze(expression, path, ExpressionContext.CONDITION, expected=BOOLEAN)
                if isinstance(name, str):
                    self._condition_values[name] = inferred

        rules = data.get("Rules") or {}
        if isinstance(rules, Mapping):
            for name, definition in rules.items():
                path = _key(_key((), "Rules"), name)
                self._analyze_rule(definition, path)
        return self._stage_result(start)

    def analyze_resources_and_outputs(self) -> list[Diagnostic]:
        start = len(self.diagnostics)
        data = self.parsed.data
        if not isinstance(data, Mapping):
            return self._stage_result(start)
        resources = data.get("Resources") or {}
        if isinstance(resources, Mapping):
            for name, definition in resources.items():
                if not isinstance(definition, Mapping):
                    continue
                resource_path = _key(_key((), "Resources"), name)
                resource_type = definition.get("Type") if isinstance(definition.get("Type"), str) else None
                expression_context = (
                    ExpressionContext.MODULE
                    if self.semantic_mode == TemplateSemanticMode.MODULE_REGISTRATION
                    else ExpressionContext.NORMAL
                )
                if "Count" in definition:
                    self.analyze(
                        definition.get("Count"),
                        _key(resource_path, "Count"),
                        ExpressionContext.COUNT,
                    )
                if "DependsOn" in definition:
                    owner_symbol = self.symbols.resources.get(name) if isinstance(name, str) else None
                    self._validate_depends_on(
                        definition.get("DependsOn"),
                        _key(resource_path, "DependsOn"),
                        owner_symbol.count_info if owner_symbol is not None else CountInfo(),
                    )
                for field in ("Metadata", "UpdatePolicy"):
                    if field in definition:
                        field_value = definition.get(field)
                        self.analyze(
                            field_value,
                            _key(resource_path, field),
                            expression_context,
                            count_position_eligible=False,
                            consumer_resource_type=resource_type,
                            consumer_section=field,
                        )
                        if not isinstance(field_value, Mapping):
                            self.diagnostic(
                                "ROS1105",
                                _("Resource {} {} must be a Mapping or ROS Function.").format(name, field),
                                _("ROS does not accept root nodes such as String, List, or Null."),
                                _key(resource_path, field),
                                stable_args=(str(name), field, type(field_value).__name__),
                                expected="Map | Function",
                                actual=type(field_value).__name__,
                            )
                if "DeletionPolicy" in definition:
                    self._validate_deletion_policy(
                        definition.get("DeletionPolicy"),
                        _key(resource_path, "DeletionPolicy"),
                        expression_context,
                        resource_type,
                    )
                properties = definition.get("Properties")
                if isinstance(properties, Mapping):
                    properties_path = _key(resource_path, "Properties")
                    previous_count_index = self._count_index_enabled
                    previous_count_value = self._count_index_value
                    condition_name = definition.get("Condition")
                    previous_condition_value: InferredValue | None = None
                    condition_was_known = False
                    condition_assumed = False
                    resource_reachable = True
                    if isinstance(condition_name, str) and condition_name in self.symbols.conditions:
                        condition_value = self._condition_values.get(condition_name, InferredValue.dynamic(BOOLEAN))
                        if condition_value.knowledge == ValueKnowledge.CONSTANT and condition_value.value is False:
                            resource_reachable = False
                        else:
                            condition_was_known = condition_name in self._condition_values
                            previous_condition_value = self._condition_values.get(condition_name)
                            # ROS only validates/evaluates the resource after its
                            # Condition includes it. Inside the resource, the same
                            # named Condition is therefore necessarily true.
                            self._condition_values[condition_name] = InferredValue.constant(True, ros_type=BOOLEAN)
                            condition_assumed = True
                    symbol = self.symbols.resources.get(name) if isinstance(name, str) else None
                    count_info = symbol.count_info if symbol is not None else CountInfo()
                    self._count_index_enabled = count_info.declared
                    instance_indexes: Iterable[int | None]
                    if count_info.declared and count_info.valid and count_info.length is not None:
                        instance_indexes = range(count_info.length)
                    else:
                        instance_indexes = (None,)
                    try:
                        for instance_index in instance_indexes:
                            self._count_index_value = instance_index
                            for property_name, property_value in properties.items():
                                if resource_type is not None and self.resource_specs.is_raw_content_property(
                                    resource_type, property_name
                                ):
                                    continue
                                self._walk_container(
                                    property_value,
                                    _key(properties_path, property_name),
                                    expression_context,
                                    count_position_eligible=True,
                                    consumer_resource_type=resource_type,
                                    semantic_reachable=resource_reachable,
                                )
                    finally:
                        self._count_index_enabled = previous_count_index
                        self._count_index_value = previous_count_value
                        if isinstance(condition_name, str) and condition_assumed:
                            if condition_was_known and previous_condition_value is not None:
                                self._condition_values[condition_name] = previous_condition_value
                            else:
                                self._condition_values.pop(condition_name, None)

        outputs = data.get("Outputs") or {}
        if isinstance(outputs, Mapping):
            for name, definition in outputs.items():
                if not isinstance(definition, Mapping) or "Value" not in definition:
                    continue
                value = definition.get("Value")
                path = _key(_key(_key((), "Outputs"), name), "Value")
                self.analyze(
                    value,
                    path,
                    ExpressionContext.NORMAL,
                    count_position_eligible=isinstance(value, Mapping) and bool(value),
                    consumer_section="Outputs",
                )

        return self._stage_result(start)

    def _validate_depends_on(self, value: Any, path: RosPath, owner_count: CountInfo) -> None:
        indexes: Iterable[int | None]
        if owner_count.declared and owner_count.valid and owner_count.length is not None:
            indexes = range(owner_count.length)
        else:
            indexes = (None,)
        previous_enabled = self._count_index_enabled
        previous_index = self._count_index_value
        try:
            for index in indexes:
                self._count_index_enabled = owner_count.declared
                self._count_index_value = index
                resolved = value
                if isinstance(value, Mapping):
                    inferred = self.analyze(
                        value,
                        path,
                        ExpressionContext.NORMAL,
                        count_position_eligible=False,
                        consumer_section="DependsOn",
                    )
                    if inferred.poisoned:
                        continue
                    if inferred.knowledge != ValueKnowledge.CONSTANT:
                        continue
                    resolved = inferred.value
                items = resolved if isinstance(resolved, list) else [resolved]
                invalid_shape = False
                for item_index, item in enumerate(items):
                    item_path = _index(path, item_index) if isinstance(resolved, list) else path
                    if isinstance(item, Mapping):
                        inferred_item = self.analyze(
                            item,
                            item_path,
                            ExpressionContext.NORMAL,
                            count_position_eligible=False,
                            consumer_section="DependsOn",
                        )
                        if inferred_item.poisoned or inferred_item.knowledge != ValueKnowledge.CONSTANT:
                            continue
                        item = inferred_item.value
                    if item is None or item == "":
                        continue
                    if not isinstance(item, str):
                        self.diagnostic(
                            "ROS3002",
                            _("DependsOn members must be String, Null, or an empty string."),
                            _("The Count precompiler can sanitize or bind only String resource logical names."),
                            item_path,
                            stable_args=("DependsOn", "member-type", str(item_index), type(item).__name__),
                            expected="String | Null | EmptyString",
                            actual=type(item).__name__,
                        )
                        invalid_shape = True
                        continue
                    if self._is_poisoned_symbol(item) or self._is_poisoned_count_instance(item):
                        continue
                    if item in self.symbols.resources:
                        continue
                    dynamic = self._dynamic_count_reference(item, item_path)
                    if dynamic is not None:
                        continue
                    if self._expanded_resource(item) is not None:
                        continue
                    self.diagnostic(
                        "ROS4002",
                        _("DependsOn references nonexistent resource {}.").format(item),
                        _("The resource logical name or expanded Count instance name is invalid."),
                        item_path,
                        stable_args=(item, "depends-on"),
                    )
                if invalid_shape:
                    continue
        finally:
            self._count_index_enabled = previous_enabled
            self._count_index_value = previous_index

    def _validate_deletion_policy(
        self,
        value: Any,
        path: RosPath,
        context: ExpressionContext,
        resource_type: str | None,
    ) -> None:
        if isinstance(value, str):
            if value not in _DELETION_POLICIES:
                self.diagnostic(
                    "ROS1104",
                    _("DeletionPolicy value {} is invalid.").format(value),
                    _(
                        "The local contract allows only Delete, Retain, or a Ref to one Parameter; Snapshot is unsupported."
                    ),
                    path,
                    stable_args=(value, "literal"),
                    expected="Delete | Retain | Parameter Ref",
                    actual="String",
                )
            return

        if isinstance(value, Mapping) and len(value) == 1 and "Ref" in value:
            target = value.get("Ref")
            if target is None:
                self.diagnostic(
                    "ROS1104",
                    _("The DeletionPolicy Ref is missing a Parameter name."),
                    _(
                        "Ref: Null is not a valid parameter name; Null is treated as Delete only when a declared Parameter resolves to Null."
                    ),
                    path,
                    stable_args=("null", "parameter-name"),
                    expected="Parameter name",
                    actual="Null",
                )
                return
            if self._is_poisoned_symbol(target):
                return
            if isinstance(target, str) and (
                target in self.symbols.resources or self._expanded_resource(target) is not None
            ):
                self.diagnostic(
                    "ROS1104",
                    _("DeletionPolicy cannot Ref resource {}.").format(target),
                    _("This position accepts only a Parameter Ref, not a Resource/DataSource Ref."),
                    path,
                    stable_args=(target, "resource-ref"),
                    expected="Parameter Ref",
                    actual="Resource Ref",
                )
                return
            if isinstance(target, str) and target in self.symbols.pseudo_parameters:
                if target == "ALIYUN::NoValue":
                    return
                self.diagnostic(
                    "ROS1104",
                    _("DeletionPolicy cannot Ref pseudo parameter {}.").format(target),
                    _("The value domain of this pseudo parameter does not contain Delete, Retain, or Null."),
                    path,
                    stable_args=(target, "pseudo-parameter-domain"),
                    expected="Delete | Retain | Null",
                    actual="Pseudo parameter String",
                )
                return
            if isinstance(target, str) and target in self.symbols.parameters:
                parameters = self.parsed.data.get("Parameters") if isinstance(self.parsed.data, Mapping) else None
                schema = parameters.get(target) if isinstance(parameters, Mapping) else None
                allowed_values = schema.get("AllowedValues") if isinstance(schema, Mapping) else None
                if isinstance(allowed_values, list) and "Snapshot" in allowed_values:
                    self.diagnostic(
                        "ROS1104",
                        _("DeletionPolicy Parameter {} permits unsupported Snapshot.").format(target),
                        _(
                            "Because AllowedValues contains Snapshot, this Parameter could place the template into a deletion policy unsupported by ROS."
                        ),
                        path,
                        stable_args=(target, "allowed-values-snapshot"),
                        expected="AllowedValues without Snapshot",
                        actual="AllowedValues containing Snapshot",
                    )
                    return
            inferred = self.analyze(
                value,
                path,
                context,
                count_position_eligible=False,
                consumer_resource_type=resource_type,
                consumer_section="DeletionPolicy",
            )
            if inferred.poisoned:
                return
            if inferred.knowledge == ValueKnowledge.CONSTANT:
                resolved = inferred.value
                if resolved is not None and (not isinstance(resolved, str) or resolved not in _DELETION_POLICIES):
                    self.diagnostic(
                        "ROS1104",
                        _("The DeletionPolicy Parameter Ref resolves to invalid value {}.").format(resolved),
                        _(
                            "The resolved value must be Delete, Retain, or Null; Snapshot is unsupported, and Null is treated as Delete."
                        ),
                        path,
                        stable_args=(str(resolved), "parameter-result"),
                        expected="Delete | Retain | Null",
                        actual=str(inferred.type),
                    )
            elif compatibility(inferred.type, union_of(STRING, NULL)) == Compatibility.DEFINITE_MISMATCH:
                self.diagnostic(
                    "ROS1104",
                    _("DeletionPolicy Parameter Ref type {} is invalid.").format(inferred.type),
                    _("The resolved value must be Delete, Retain, or Null."),
                    path,
                    stable_args=(str(inferred.type), "parameter-type"),
                    expected="String | Null",
                    actual=str(inferred.type),
                )
            return

        if isinstance(value, (Mapping, list)):
            self.analyze(
                value,
                path,
                context,
                count_position_eligible=False,
                consumer_resource_type=resource_type,
                consumer_section="DeletionPolicy",
            )
        self.diagnostic(
            "ROS1104",
            _("DeletionPolicy has an invalid structure."),
            _(
                "Only Delete, Retain, or a Ref to one Parameter is allowed; Snapshot, other functions, and explicit Null are rejected."
            ),
            path,
            stable_args=(type(value).__name__, "shape"),
            expected="Delete | Retain | Parameter Ref",
            actual=type(value).__name__,
        )

    def analyze_template(self) -> list[Diagnostic]:
        """Compatibility entry point; the registry invokes the isolated stages."""

        self.analyze_structure()
        self.analyze_conditions_and_rules()
        self.analyze_resources_and_outputs()
        return self.diagnostics

    def _nonfinite_number_quality(self, value: Any, path: RosPath) -> None:
        if isinstance(value, float) and not math.isfinite(value):
            self.diagnostic(
                "ROS5205",
                _("The template contains a non-finite Number."),
                _(
                    "The locked ROS runtime accepts this value, but it violates standard JSON and official template numeric constraints."
                ),
                path,
                severity=Severity.WARNING,
                category=Category.QUALITY,
                stable_args=(self.parsed.source_kind, "non-finite-number"),
            )
            return
        if isinstance(value, list):
            for index, item in enumerate(value):
                self._nonfinite_number_quality(item, _index(path, index))
        elif isinstance(value, Mapping):
            for key, item in value.items():
                self._nonfinite_number_quality(item, _key(path, key))

    def _module_structure(self, data: Mapping[Any, Any]) -> None:
        """Apply registration/consumer constraints that are absent from stack semantics."""

        if (
            "ROSTemplateFormatVersion" in data
            and data.get("ROSTemplateFormatVersion") != "2015-09-01"
            and not self._is_terraform(data)
        ):
            self.diagnostic(
                "ROS1120",
                _("ROSTemplateFormatVersion must be exactly 2015-09-01."),
                _("The local function contracts apply only to the ROS 2015-09-01 runtime."),
                _key((), "ROSTemplateFormatVersion"),
                stable_args=(str(data.get("ROSTemplateFormatVersion")),),
            )

        registration = self.semantic_mode == TemplateSemanticMode.MODULE_REGISTRATION
        resources = data.get("Resources")
        contains_module = isinstance(resources, Mapping) and any(
            isinstance(definition, Mapping)
            and isinstance(definition.get("Type"), str)
            and definition["Type"].startswith("MODULE::")
            for definition in resources.values()
        )
        module_constraints = registration or contains_module
        if module_constraints:
            for section in ("HeatTemplateFormatVersion", "Transform", "Workspace"):
                if section in data:
                    self.diagnostic(
                        "ROS1121",
                        _("A Module registration template cannot contain {}.").format(section),
                        _("This top-level section is not part of the ROS Module schema."),
                        _key((), section),
                        stable_args=(section,),
                    )
            if registration and isinstance(data.get("Rules"), Mapping) and data["Rules"]:
                self.diagnostic(
                    "ROS1122",
                    _("A Module registration template cannot contain non-empty Rules."),
                    _("Rules are not part of the Module registration template consumer contract."),
                    _key((), "Rules"),
                )

        invalid_chars = {
            "Parameters": {".", ":"},
            "Locals": {".", ":"},
            "Resources": {"."},
            "Outputs": {"."},
            "Conditions": {"&"},
        }
        if module_constraints:
            for section, forbidden in invalid_chars.items():
                declarations = data.get(section)
                if not isinstance(declarations, Mapping):
                    continue
                for name in declarations:
                    if isinstance(name, str) and any(char in name for char in forbidden):
                        self.diagnostic(
                            "ROS1123",
                            _("Module {} name {} contains a reserved character.").format(section, name),
                            _("Forbidden character: {}.").format(" ".join(sorted(forbidden))),
                            _key(_key((), section), name),
                            stable_args=(section, name),
                        )

        if not isinstance(resources, Mapping):
            return
        stack_types = {"ALIYUN::ROS::Stack", "ALIYUN::ROS::StackGroup", "ALIYUN::ROS::StackInstances"}
        stack_resources: list[str] = []
        for name, definition in resources.items():
            if not isinstance(definition, Mapping):
                continue
            resource_type = definition.get("Type")
            path = _key(_key((), "Resources"), name)
            if registration and resource_type in stack_types:
                stack_resources.append(str(name))
            if isinstance(resource_type, str) and resource_type.startswith("MODULE::"):
                for field in ("Metadata", "UpdatePolicy", "Count"):
                    if field in definition:
                        self.diagnostic(
                            "ROS1124",
                            _("A MODULE resource cannot contain {}.").format(field),
                            _("The Module consumer expansion protocol does not support this field."),
                            _key(path, field),
                            stable_args=(str(name), field),
                        )
        if stack_resources:
            self.diagnostic(
                "ROS9102",
                _("The Module registration template contains Stack resources constrained by the account environment."),
                _(
                    "Whether {} is allowed depends on the production/test account environment and cannot be determined from local template semantics alone."
                ).format(", ".join(stack_resources)),
                _key((), "Resources"),
                severity=Severity.LIMITATION,
                category=Category.LIMITATION,
                stable_args=tuple(stack_resources),
            )

    def _structure(self, data: Mapping[Any, Any]) -> None:
        if "ROSTemplateFormatVersion" not in data and not self._is_terraform(data):
            self.diagnostic(
                "ROS1004",
                _("The template is missing ROSTemplateFormatVersion."),
                _("A ROS template must declare a version, such as 2015-09-01."),
                (),
                suggestion=_("Add ROSTemplateFormatVersion: '2015-09-01'."),
            )
        if self._is_terraform(data):
            return
        for section in ("Parameters", "Mappings", "Conditions", "Rules", "Outputs"):
            value = data.get(section)
            if value is not None and not isinstance(value, Mapping):
                self.diagnostic(
                    "ROS1101",
                    _("{} must be a Mapping.").format(section),
                    _("The current section cannot produce symbols and expression occurrences."),
                    _key((), section),
                    stable_args=(section, type(value).__name__),
                    expected="Map",
                    actual=type(value).__name__,
                )
        resources = data.get("Resources")
        if resources is not None and not isinstance(resources, Mapping):
            self.diagnostic(
                "ROS1101",
                _("Resources must be a Mapping."),
                _("The current Resources value cannot be enumerated as resource declarations."),
                _key((), "Resources"),
                expected="Map",
                actual=type(resources).__name__,
            )
            return
        if not isinstance(resources, Mapping):
            return
        raw_parameters = data.get("Parameters")
        parameters: Mapping[Any, Any] = raw_parameters if isinstance(raw_parameters, Mapping) else {}
        for name, resource in resources.items():
            path = _key(_key((), "Resources"), name)
            if not isinstance(resource, Mapping):
                self.diagnostic(
                    "ROS1102",
                    _("The definition of resource {} must be a Mapping.").format(name),
                    _("Type and Properties cannot be read from the resource node."),
                    path,
                    expected="Map",
                    actual=type(resource).__name__,
                )
                continue
            if "Type" not in resource:
                self.diagnostic(
                    "ROS1103",
                    _("Resource {} is missing Type.").format(name),
                    _("Every ROS resource declaration must contain Type."),
                    path,
                    suggestion=_("Add Type to the resource."),
                )
                continue
            resource_type = resource.get("Type")
            type_path = _key(path, "Type")
            if resource_type in _RESOURCE_TYPE_CORRECTIONS:
                replacement = _RESOURCE_TYPE_CORRECTIONS[resource_type]
                self.diagnostic(
                    "ROS5101",
                    _("Resource {} uses incorrect type {}.").format(name, resource_type),
                    _("The corresponding ROS resource type is {}.").format(replacement),
                    type_path,
                    category=Category.QUALITY,
                    stable_args=(str(resource_type), replacement),
                    suggestion=_("Change it to {}.").format(replacement),
                )
            if resource_type == "ALIYUN::ECS::VSwitch":
                self._existing_vpc_rule(name, resource, parameters, path)

    def _check_condition_graph(self, conditions: Mapping[Any, Any]) -> None:
        names = frozenset(name for name in conditions if isinstance(name, str))
        dependencies = {
            name: self._condition_dependencies(expression)
            for name, expression in conditions.items()
            if isinstance(name, str)
        }
        for name, refs in dependencies.items():
            for target in sorted(refs - names):
                self.diagnostic(
                    "ROS4003",
                    _("Condition {} references nonexistent Condition {}.").format(name, target),
                    _("Condition names in And, Or, Not, and Fn::If must be declared in Conditions first."),
                    _key(_key((), "Conditions"), name),
                    stable_args=(name, target, "condition-graph"),
                )

        visiting: list[str] = []
        visited: set[str] = set()

        def visit(name: str) -> None:
            if name in visiting:
                cycle = visiting[visiting.index(name) :] + [name]
                self.diagnostic(
                    "ROS4004",
                    _("Conditions contain a cycle: {}.").format(" -> ".join(cycle)),
                    _("The Condition dependency graph must be acyclic."),
                    _key(_key((), "Conditions"), name),
                    stable_args=tuple(cycle),
                )
                return
            if name in visited:
                return
            visiting.append(name)
            for target in dependencies.get(name, set()) & names:
                visit(target)
            visiting.pop()
            visited.add(name)

        for name in dependencies:
            visit(name)

    def _condition_dependencies(self, value: Any) -> set[str]:
        result: set[str] = set()
        if not isinstance(value, Mapping) or len(value) != 1:
            return result
        name, args = next(iter(value.items()))
        if name == "Condition" and isinstance(args, str):
            result.add(args)
            return result
        if name in {"Fn::And", "Fn::Or"} and isinstance(args, list):
            for item in args:
                condition_name = self._raw_condition_name(item)
                if condition_name is not None:
                    result.add(condition_name)
                elif isinstance(item, str):
                    result.add(item)
                else:
                    result.update(self._condition_dependencies(item))
        elif name == "Fn::Not":
            item = args[0] if isinstance(args, list) and len(args) == 1 else args
            condition_name = self._raw_condition_name(item)
            if condition_name is not None:
                result.add(condition_name)
            elif isinstance(item, str):
                result.add(item)
            else:
                result.update(self._condition_dependencies(item))
        elif name == "Fn::If" and isinstance(args, list) and args and isinstance(args[0], str):
            result.add(args[0])
        else:
            nested = args if isinstance(args, list) else [args]
            for item in nested:
                result.update(self._condition_dependencies(item))
        return result

    @staticmethod
    def _is_terraform(data: Mapping[Any, Any]) -> bool:
        transform = data.get("Transform")
        values = transform if isinstance(transform, list) else [transform]
        return any(
            isinstance(value, str) and value.startswith(("Aliyun::Terraform-", "Aliyun::OpenTofu-")) for value in values
        )

    def _existing_vpc_rule(
        self,
        name: Any,
        resource: Mapping[Any, Any],
        parameters: Mapping[Any, Any],
        path: RosPath,
    ) -> None:
        properties = resource.get("Properties")
        if not isinstance(properties, Mapping):
            return
        vpc_ref = properties.get("VpcId")
        if not isinstance(vpc_ref, Mapping) or not isinstance(vpc_ref.get("Ref"), str):
            return
        vpc_schema = parameters.get(vpc_ref["Ref"])
        if (
            not isinstance(vpc_schema, Mapping)
            or vpc_schema.get("AssociationProperty") not in _EXISTING_VPC_ASSOCIATIONS
        ):
            return
        cidr = properties.get("CidrBlock")
        invalid = isinstance(cidr, str) and bool(cidr.strip())
        uses_default = False
        if isinstance(cidr, Mapping) and isinstance(cidr.get("Ref"), str):
            schema = parameters.get(cidr["Ref"])
            uses_default = isinstance(schema, Mapping) and schema.get("Default") is not None
            invalid = uses_default
        if invalid:
            self.diagnostic(
                "ROS5102",
                _("VSwitch {} in an existing VPC uses a static CidrBlock.").format(name),
                (
                    _(
                        "The CidrBlock Parameter has a Default; after selecting an existing VPC, deployment parameters should choose a non-conflicting CIDR block."
                    )
                    if uses_default
                    else _(
                        "After selecting an existing VPC, deployment parameters should choose a non-conflicting CidrBlock."
                    )
                ),
                _key(_key(path, "Properties"), "CidrBlock"),
                category=Category.QUALITY,
                suggestion=_("Remove the fixed value or the referenced Parameter Default."),
            )

    def _local_structure(self, data: Mapping[Any, Any]) -> None:
        raw_locals = data.get("Locals")
        locals_ = {} if raw_locals is None else raw_locals
        if not isinstance(locals_, Mapping):
            self.diagnostic(
                "ROS1110",
                _("Locals must be a Mapping."),
                _("The Local dependency graph cannot be built."),
                _key((), "Locals"),
                expected="Map",
                actual=type(locals_).__name__,
            )
            return
        self._nested_stack_locals(data, outer_has_locals=bool(locals_))
        dependencies: dict[str, set[str]] = {}
        for name, definition in locals_.items():
            path = _key(_key((), "Locals"), name)
            if not isinstance(name, str) or not isinstance(definition, Mapping):
                self.diagnostic(
                    "ROS1111",
                    _("The Local declaration has an invalid structure."),
                    _("A Local must use a String name and a Mapping definition."),
                    path,
                )
                continue
            local_type = definition.get("Type") or "Macro"
            allowed = {"Type", "Value", "Properties"}
            unknown = set(definition) - allowed
            if unknown:
                self.diagnostic(
                    "ROS1112",
                    _("Local {} contains unsupported fields.").format(name),
                    _("Only Type, Value, and Properties are allowed."),
                    path,
                    stable_args=tuple(sorted(map(str, unknown))),
                )
            if local_type in {"Macro", "Eval"}:
                if "Value" not in definition or "Properties" in definition:
                    self.diagnostic(
                        "ROS1113",
                        _("Local {} has an invalid Value/Properties combination.").format(name),
                        _("Macro/Eval must have Value and cannot have Properties."),
                        path,
                    )
            elif isinstance(local_type, str) and local_type.startswith("DATASOURCE::"):
                if "Value" in definition or not isinstance(definition.get("Properties", {}), Mapping):
                    self.diagnostic(
                        "ROS1113",
                        _("DataSource Local {} has an invalid Value/Properties combination.").format(name),
                        _("A DATASOURCE::* Local cannot have Value, and Properties must be a Mapping."),
                        path,
                    )
            else:
                self.diagnostic(
                    "ROS1114",
                    _("Local {} Type {} is unsupported.").format(name, local_type),
                    _("Macro, Eval, and DATASOURCE::* Resource Type are supported."),
                    _key(path, "Type"),
                )
            if local_type == "Eval" and self._find_mapping_value(definition, {"Ref": "ALIYUN::StackId"}):
                self.diagnostic(
                    "ROS4211",
                    _("Eval Local {} uses ALIYUN::StackId in a restricted position.").format(name),
                    _(
                        "The locked Locals precompiler rejects this Ref when it appears as a Mapping value; use Macro instead."
                    ),
                    _key(path, "Value"),
                    stable_args=(name, "stack-id-eval-guard"),
                )
            if self.policy == ValidationPolicy.STRICT and not re.fullmatch(r"[A-Za-z0-9]+", name):
                self.diagnostic(
                    "ROS5201",
                    _("Local name {} does not match the official alphanumeric format.").format(name),
                    _(
                        "The locked runtime accepts this name, but the official contract allows only ASCII letters and digits."
                    ),
                    path,
                    severity=Severity.WARNING,
                    category=Category.QUALITY,
                )
            dependency_value = (
                definition.get("Properties")
                if isinstance(local_type, str) and local_type.startswith("DATASOURCE::")
                else definition.get("Value")
            )
            dependencies[name] = self._local_dependencies(dependency_value, frozenset(locals_))
        self._check_local_cycles(dependencies)
        self._validate_eval_locals(locals_)
        self._validate_datasource_locals(locals_)

    def _validate_eval_locals(self, locals_: Mapping[Any, Any]) -> None:
        """Run Eval's temporary normal-function pass even for unused Locals."""

        for name, definition in locals_.items():
            if (
                not isinstance(name, str)
                or not isinstance(definition, Mapping)
                or definition.get("Type") != "Eval"
                or "Value" not in definition
            ):
                continue
            start = len(self.diagnostics)
            self._local_stack.append(name)
            self._local_eval_depth += 1
            self._local_temporary_depth += 1
            self._eval_reparse_depth += 1
            try:
                self.analyze(
                    definition["Value"],
                    _key(_key(_key((), "Locals"), name), "Value"),
                    ExpressionContext.NORMAL,
                )
            finally:
                self._eval_reparse_depth -= 1
                self._local_temporary_depth -= 1
                self._local_eval_depth -= 1
                self._local_stack.pop()
            if any(item.severity == Severity.ERROR for item in self.diagnostics[start:]):
                self._poisoned_locals.add(name)

    def _validate_datasource_locals(self, locals_: Mapping[Any, Any]) -> None:
        """Analyze every DataSource Local property in its temporary Local Stack."""

        for name, definition in locals_.items():
            if not isinstance(name, str) or not isinstance(definition, Mapping):
                continue
            local_type = definition.get("Type")
            properties = definition.get("Properties", {})
            if not isinstance(local_type, str) or not local_type.startswith("DATASOURCE::"):
                continue
            if not isinstance(properties, Mapping):
                continue
            start = len(self.diagnostics)
            self._local_stack.append(name)
            self._local_temporary_depth += 1
            try:
                properties_path = _key(_key(_key((), "Locals"), name), "Properties")
                for property_name, property_value in properties.items():
                    self._walk_container(
                        property_value,
                        _key(properties_path, property_name),
                        ExpressionContext.NORMAL,
                        count_position_eligible=False,
                        consumer_resource_type=local_type,
                    )
            finally:
                self._local_temporary_depth -= 1
                self._local_stack.pop()
            if any(item.severity == Severity.ERROR for item in self.diagnostics[start:]):
                self._poisoned_locals.add(name)

    def _nested_stack_locals(self, data: Mapping[Any, Any], *, outer_has_locals: bool) -> None:
        resources = data.get("Resources")
        if not isinstance(resources, Mapping):
            return
        for name, definition in resources.items():
            if not isinstance(definition, Mapping) or definition.get("Type") != "ALIYUN::ROS::Stack":
                continue
            properties = definition.get("Properties")
            if not isinstance(properties, Mapping):
                continue
            resource_path = _key(_key((), "Resources"), name)
            body = properties.get("TemplateBody")
            if isinstance(body, Mapping) and "Locals" in body:
                path = _key(_key(resource_path, "Properties"), "TemplateBody")
                if outer_has_locals:
                    self.diagnostic(
                        "ROS4213",
                        _("When the outer template uses Locals, an inline nested stack cannot also declare Locals."),
                        _(
                            "The locked PreCompilerLocals scans ALIYUN::ROS::Stack.TemplateBody and rejects this structure."
                        ),
                        _key(path, "Locals"),
                        stable_args=(str(name), "nested-locals-runtime-boundary"),
                    )
                elif self.policy == ValidationPolicy.STRICT:
                    self.diagnostic(
                        "ROS5211",
                        _("An inline nested stack declares Locals, which are not officially supported."),
                        _(
                            "The locked precompiler returns early when the outer template has no Locals, but the official nested-stack contract still does not support Locals."
                        ),
                        _key(path, "Locals"),
                        severity=Severity.WARNING,
                        category=Category.QUALITY,
                        stable_args=(str(name), "nested-locals-official"),
                    )
            template_url = properties.get("TemplateURL")
            if template_url is not None and template_url != "":
                self.diagnostic(
                    "ROS9104",
                    _("Whether the remote nested stack contains Locals cannot be verified locally."),
                    _(
                        "The TemplateURL content is not in the current TemplateBody; local validation does not access the network to retrieve it."
                    ),
                    _key(_key(resource_path, "Properties"), "TemplateURL"),
                    severity=Severity.LIMITATION,
                    category=Category.LIMITATION,
                    stable_args=(str(name), "remote-nested-locals"),
                )

    def _local_dependencies(self, value: Any, names: frozenset[Any]) -> set[str]:
        result: set[str] = set()
        if isinstance(value, Mapping):
            if len(value) == 1 and isinstance(value.get("Ref"), str) and value["Ref"] in names:
                result.add(value["Ref"])
            if len(value) == 1 and isinstance(value.get("Fn::GetAtt"), list):
                args = value["Fn::GetAtt"]
                if args and not isinstance(args[0], str):
                    # This is a constructor-time Local dependency error in ROS;
                    # the ordinary expression pass will attach the concrete
                    # type diagnostic at the same occurrence.
                    return result
                if args and args[0] in names:
                    result.add(args[0])
            for item in value.values():
                result.update(self._local_dependencies(item, names))
        elif isinstance(value, list):
            for item in value:
                result.update(self._local_dependencies(item, names))
        return result

    @classmethod
    def _find_mapping_value(cls, value: Any, target: Any) -> bool:
        """Match PreCompilerLocals.find_substructure's Mapping-value boundary."""

        if isinstance(value, Mapping):
            for child in value.values():
                if child == target or cls._find_mapping_value(child, target):
                    return True
        elif isinstance(value, list):
            return any(cls._find_mapping_value(child, target) for child in value)
        return False

    def _check_local_cycles(self, dependencies: Mapping[str, set[str]]) -> None:
        visiting: list[str] = []
        done: set[str] = set()

        def visit(name: str) -> None:
            if name in visiting:
                cycle = visiting[visiting.index(name) :] + [name]
                self._poisoned_locals.update(cycle)
                self._cyclic_locals.update(cycle)
                self.diagnostic(
                    "ROS4205",
                    _("Locals contain a cycle: {}.").format(" -> ".join(cycle)),
                    _("Local dependencies must be acyclic, with a reference depth no greater than 5."),
                    _key(_key((), "Locals"), name),
                    stable_args=tuple(cycle),
                )
                return
            if name in done:
                return
            visiting.append(name)
            if len(visiting) > 5:
                self.diagnostic(
                    "ROS4206",
                    _("Local {} has a reference depth greater than 5.").format(name),
                    _("The ROS Local precompiler supports at most five reference levels."),
                    _key(_key((), "Locals"), name),
                    stable_args=(name,),
                )
            for dependency in dependencies.get(name, set()):
                visit(dependency)
            visiting.pop()
            done.add(name)

        for name in dependencies:
            visit(name)

    def _symbol_conflicts(self, data: Mapping[Any, Any]) -> None:
        sections: dict[str, Mapping[Any, Any]] = {}
        for section in ("Parameters", "Resources", "Locals"):
            declarations = data.get(section)
            sections[section] = declarations if isinstance(declarations, Mapping) else {}
        names: dict[Any, list[tuple[str, Any]]] = {}
        for section, declarations in sections.items():
            for name in declarations:
                names.setdefault(name, []).append((section, name))
        for name, declarations in names.items():
            declared = [section for section, _raw_name in declarations]
            if len(declared) < 2:
                continue
            self._poisoned_symbols.add(name)
            declaration_paths = tuple(_key(_key((), section), raw_name) for section, raw_name in declarations)
            related_locations = tuple(
                RelatedLocation(
                    _("conflicting declaration"),
                    node.span if (node := self.parsed.source_map.node_for(declaration_path)) is not None else None,
                    declaration_path,
                )
                for declaration_path in declaration_paths[1:]
            )
            self.diagnostic(
                "ROS4201",
                _("Symbol {} is declared in multiple sections.").format(name),
                _("Logical names in Parameters, Resources, and Locals must be mutually unique: {}.").format(
                    ", ".join(declared)
                ),
                declaration_paths[0],
                stable_args=tuple(sorted(declared)),
                suggestion=_("Rename one declaration and update its references."),
                related_locations=related_locations,
            )

    def _resolve_counts(self, data: Mapping[Any, Any]) -> None:
        resources = data.get("Resources") or {}
        if not isinstance(resources, Mapping):
            return
        updated = dict(self.symbols.resources)
        generated: dict[str, str] = {}
        for name, definition in resources.items():
            if not isinstance(name, str) or not isinstance(definition, Mapping) or "Count" not in definition:
                continue
            path = _key(_key(_key((), "Resources"), name), "Count")
            raw_value = definition.get("Count")
            value = self._resolve_compile_value(raw_value)
            inferred: InferredValue | None = None
            dynamic = False
            poisoned = False
            if isinstance(value, Mapping) and len(value) == 1:
                inferred = self.analyze(value, path, ExpressionContext.COUNT)
                poisoned = inferred.poisoned
                if inferred.knowledge == ValueKnowledge.CONSTANT:
                    value = inferred.value
                elif not inferred.poisoned:
                    dynamic = True
            valid = not isinstance(value, bool) and isinstance(value, (int, str))
            parsed: int | None = None
            if valid:
                try:
                    parsed = int(value)
                except (TypeError, ValueError):
                    valid = False
                else:
                    valid = parsed >= 0 and not isinstance(value, float)
            if dynamic:
                assert inferred is not None
                valid = (
                    compatibility(inferred.type, union_of(INTEGER, NUMBER, STRING)) != Compatibility.DEFINITE_MISMATCH
                )
            if poisoned:
                valid = False
            symbol = updated.get(name)
            if symbol is not None:
                updated[name] = replace(symbol, count_info=CountInfo(True, parsed if valid else None, valid))
            if not valid and not poisoned:
                self.diagnostic(
                    "ROS4301",
                    _("Resource {} Count is not a non-negative integer.").format(name),
                    _(
                        "Count accepts an Integer, a String accepted by int(), or a dynamic expression returning either value; the current type is deterministically incompatible."
                    ),
                    path,
                    expected="non-negative Integer",
                    actual=str(inferred.type) if dynamic and inferred is not None else type(value).__name__,
                )
                continue
            if poisoned:
                continue
            if self.policy == ValidationPolicy.STRICT and isinstance(value, str):
                self.diagnostic(
                    "ROS5213",
                    _("Resource {} Count uses a String value.").format(name),
                    _(
                        "The locked runtime accepts a convertible String through int(); STRICT mode recommends using Number/Integer directly."
                    ),
                    path,
                    severity=Severity.WARNING,
                    category=Category.QUALITY,
                    stable_args=(name, "string-count"),
                    expected="Integer",
                    actual="String",
                    suggestion=_("Change Count to an unquoted non-negative integer."),
                )
            if parsed is None:
                for explicit_name in resources:
                    if isinstance(explicit_name, str) and re.fullmatch(
                        re.escape(name) + r"\[(0|[1-9][0-9]*)\]", explicit_name
                    ):
                        self.diagnostic(
                            "ROS9103",
                            _("Dynamic Count may conflict with explicit resource {}.").format(explicit_name),
                            _(
                                "Count length is unknown locally; instance-name collisions can be determined only after deployment parameters are known."
                            ),
                            _key(_key((), "Resources"), explicit_name),
                            severity=Severity.LIMITATION,
                            category=Category.LIMITATION,
                            stable_args=(name, explicit_name, "dynamic-count-conflict"),
                        )
                continue
            for index in range(parsed):
                instance = "{}[{}]".format(name, index)
                generated[instance] = name
                if instance in resources:
                    self.diagnostic(
                        "ROS4302",
                        _("Expanded Count name {} conflicts with an explicit resource.").format(instance),
                        _("Full precompilation would generate a resource instance with the same name."),
                        _key(_key((), "Resources"), instance),
                        stable_args=(name, str(index)),
                    )
        self.symbols = replace(self.symbols, resources=updated)
        self._build_count_select_facts(data)

    def _build_count_select_facts(self, data: Mapping[Any, Any]) -> None:
        facts: dict[tuple[tuple[str, ...], int | None], CountSelectFoldFact] = {}

        def visit(value: Any, path: RosPath, instance_index: int | None) -> None:
            if isinstance(value, Mapping):
                if len(value) == 1 and "Fn::Select" in value:
                    select_path = _key(path, "Fn::Select")
                    fact = fold_count_select(
                        value["Fn::Select"],
                        self._resolve_count_compile_function,
                        self._resolve_compile_value,
                    )
                    facts[(path_identity(select_path), instance_index)] = fact
                    if fact.activated:
                        return
                for key, child in value.items():
                    visit(child, _key(path, key), instance_index)
            elif isinstance(value, list):
                for index, child in enumerate(value):
                    visit(child, _index(path, index), instance_index)

        previous_enabled = self._count_index_enabled
        previous_index = self._count_index_value
        try:
            resources = data.get("Resources") or {}
            if isinstance(resources, Mapping):
                for name, definition in resources.items():
                    if not isinstance(name, str) or not isinstance(definition, Mapping):
                        continue
                    symbol = self.symbols.resources.get(name)
                    count_info = symbol.count_info if symbol is not None else CountInfo()
                    self._count_index_enabled = count_info.declared
                    indexes: Iterable[int | None]
                    if count_info.declared and count_info.valid and count_info.length is not None:
                        indexes = range(count_info.length)
                    else:
                        indexes = (None,)
                    properties = definition.get("Properties")
                    if not isinstance(properties, Mapping):
                        continue
                    resource_type = definition.get("Type")
                    for instance_index in indexes:
                        self._count_index_value = instance_index
                        for property_name, property_value in properties.items():
                            if isinstance(resource_type, str) and self.resource_specs.is_raw_content_property(
                                resource_type, property_name
                            ):
                                continue
                            visit(
                                property_value,
                                _key(_key(_key(_key((), "Resources"), name), "Properties"), property_name),
                                instance_index,
                            )
            self._count_index_enabled = False
            self._count_index_value = None
            outputs = data.get("Outputs") or {}
            if isinstance(outputs, Mapping):
                for name, definition in outputs.items():
                    if not isinstance(definition, Mapping):
                        continue
                    output_value = definition.get("Value")
                    if isinstance(output_value, Mapping) and output_value:
                        visit(output_value, _key(_key(_key((), "Outputs"), name), "Value"), None)
        finally:
            self._count_index_enabled = previous_enabled
            self._count_index_value = previous_index
        self.count_select_facts = MappingProxyType(facts)

    def _resolve_compile_value(self, value: Any) -> Any:
        if isinstance(value, Mapping) and len(value) == 1:
            if "Ref" in value:
                name = value.get("Ref")
                if name == "ALIYUN::Index" and self._count_index_value is not None:
                    return self._count_index_value
                if self._is_poisoned_symbol(name):
                    return value
                parameter = self.symbols.parameters.get(name)
                if parameter and parameter.knowledge == ValueKnowledge.CONSTANT:
                    return parameter.value
                local = self.symbols.locals.get(name) if isinstance(name, str) else None
                if local is not None and name not in self._poisoned_locals:
                    return self._resolve_compile_value(local.value)
            if "Fn::FindInMap" in value:
                args = value["Fn::FindInMap"]
                if isinstance(args, list) and len(args) == 3:
                    keys = [self._resolve_compile_value(item) for item in args]
                    try:
                        return self.symbols.mappings[keys[0]][keys[1]][keys[2]]
                    except (KeyError, TypeError):
                        return value
        return value

    def _resolve_count_compile_function(self, value: Any) -> Any:
        """Mirror PreCompiler._calc_fn, without executing arbitrary functions."""

        if not isinstance(value, Mapping) or len(value) != 1:
            return None
        name, args = next(iter(value.items()))
        if name == "Ref":
            if args == "ALIYUN::Index" and self._count_index_value is not None:
                return self._count_index_value
            if self._is_poisoned_symbol(args):
                return None
            resource = self.symbols.resources.get(args) if isinstance(args, str) else None
            if (
                resource is not None
                and resource.count_info.declared
                and resource.count_info.valid
                and resource.count_info.length is not None
            ):
                return [{"Ref": "{}[{}]".format(args, index)} for index in range(resource.count_info.length)]
            return None
        if name == "Fn::GetAtt":
            if not (isinstance(args, list) and len(args) == 2 and isinstance(args[0], str)):
                return None
            if self._is_poisoned_symbol(args[0]):
                return None
            resource = self.symbols.resources.get(args[0])
            if (
                resource is None
                or not resource.count_info.declared
                or not resource.count_info.valid
                or resource.count_info.length is None
            ):
                return None
            attribute = self._rewrite_count_compile_tree(args[1])
            return [
                {"Fn::GetAtt": ["{}[{}]".format(args[0], index), attribute]}
                for index in range(resource.count_info.length)
            ]
        if name == "Fn::Select":
            fact = fold_count_select(args, self._resolve_count_compile_function, self._resolve_compile_value)
            if not fact.activated:
                return None
            if fact.precompile_failure is not None:
                raise ValueError(fact.precompile_failure)
            return fact.transformed_node
        if name == "Fn::Sub":
            if self._count_index_value is None:
                return None
            pattern = re.compile(r"[$][{]\s*ALIYUN::Index\s*[}]")
            if isinstance(args, str):
                replaced = pattern.sub(str(self._count_index_value), args)
                return {"Fn::Sub": replaced} if replaced != args else None
            if isinstance(args, list) and len(args) >= 2 and isinstance(args[0], str):
                variables = args[1]
                if isinstance(variables, Mapping) and "ALIYUN::Index" in variables:
                    return None
                replaced = pattern.sub(str(self._count_index_value), args[0])
                if replaced == args[0]:
                    return None
                return {"Fn::Sub": [replaced, *[self._rewrite_count_compile_tree(item) for item in args[1:]]]}
        return None

    def _rewrite_count_compile_tree(self, value: Any) -> Any:
        if isinstance(value, Mapping):
            resolved = self._resolve_count_compile_function(value)
            if resolved is not None:
                return resolved
            return {key: self._rewrite_count_compile_tree(child) for key, child in value.items()}
        if isinstance(value, list):
            return [self._rewrite_count_compile_tree(child) for child in value]
        return value

    def _analyze_rule(self, definition: Any, path: RosPath) -> None:
        if not isinstance(definition, Mapping):
            return
        enabled: bool | None = True
        if "RuleCondition" in definition:
            rule_condition = definition["RuleCondition"]
            condition_path = _key(path, "RuleCondition")
            if isinstance(rule_condition, str):
                result = self._condition_reference(rule_condition, condition_path)
            elif isinstance(rule_condition, int) and not isinstance(rule_condition, bool):
                result = self._condition_reference(str(rule_condition), condition_path)
            else:
                result = self.analyze(rule_condition, condition_path, ExpressionContext.RULE)
                self._rule_result_quality("RuleCondition", result, condition_path)
            enabled = bool(result.value) if result.knowledge == ValueKnowledge.CONSTANT else None
        assertions = definition.get("Assertions") or []
        if isinstance(assertions, list):
            for index, assertion in enumerate(assertions):
                if isinstance(assertion, Mapping) and "Assert" in assertion:
                    assert_value = assertion["Assert"]
                    assert_path = _key(_index(_key(path, "Assertions"), index), "Assert")
                    if isinstance(assert_value, str):
                        result = self._condition_reference(assert_value, assert_path)
                    elif isinstance(assert_value, int) and not isinstance(assert_value, bool):
                        result = self._condition_reference(str(assert_value), assert_path)
                    else:
                        result = self.analyze(
                            assert_value,
                            assert_path,
                            ExpressionContext.RULE,
                            semantic_reachable=enabled is not False,
                        )
                        self._rule_result_quality("Assert", result, assert_path)
                    if enabled is not False and result.knowledge == ValueKnowledge.CONSTANT and result.value is False:
                        description = assertion.get("AssertDescription")
                        self.diagnostic(
                            "ROS4006",
                            _("The Rule Assert is known to be false."),
                            _("This assertion will cause template validation to fail{}.").format(
                                "：{}".format(description) if isinstance(description, str) and description else ""
                            ),
                            assert_path,
                            subject="rule-assert",
                            stable_args=(str(index), "false"),
                            suggestion=_("Correct the Parameter default, RuleCondition, or Assert expression."),
                        )

    def _rule_result_quality(self, field: str, value: InferredValue, path: RosPath) -> None:
        if (
            self.policy == ValidationPolicy.STRICT
            and not value.poisoned
            and compatibility(value.type, BOOLEAN) == Compatibility.DEFINITE_MISMATCH
        ):
            self.diagnostic(
                "ROS5207",
                _("Rules.{} returns {}; explicitly returning Boolean is recommended.").format(field, value.type),
                _(
                    "The runtime uses truthiness for RuleCondition and fails Assert only when its result is exactly False; this is retained as a compatibility extension."
                ),
                path,
                severity=Severity.WARNING,
                category=Category.QUALITY,
                stable_args=(field, str(value.type)),
            )

    def _condition_reference(self, name: str, path: RosPath) -> InferredValue:
        if name not in self.symbols.conditions:
            self.diagnostic(
                "ROS4003",
                _("A nonexistent Condition {} is referenced.").format(name),
                _("The Condition name must be declared in the Conditions section."),
                path,
                stable_args=(name, "condition-reference"),
            )
            return InferredValue.invalid()
        return self._condition_values.get(name, InferredValue.dynamic(BOOLEAN))

    def _walk_container(
        self,
        value: Any,
        path: RosPath,
        context: ExpressionContext,
        *,
        count_position_eligible: bool,
        consumer_resource_type: str | None = None,
        semantic_reachable: bool = True,
    ) -> InferredValue:
        return self.analyze(
            value,
            path,
            context,
            count_position_eligible=count_position_eligible,
            consumer_resource_type=consumer_resource_type,
            semantic_reachable=semantic_reachable,
        )

    def analyze(
        self,
        value: Any,
        path: RosPath,
        context: ExpressionContext,
        *,
        expected: RosType | None = None,
        function_depth: int = 0,
        count_position_eligible: bool = False,
        consumer_resource_type: str | None = None,
        consumer_section: str | None = None,
        semantic_reachable: bool = True,
    ) -> InferredValue:
        self.visits += 1
        if self.visits > MAX_SEMANTIC_VISITS:
            if not self.analysis_incomplete:
                self.diagnostic(
                    "ROS9001",
                    _("The ROS template exceeds the local semantic-analysis budget."),
                    _("Incomplete analysis has stopped and the API call has been blocked."),
                    path,
                    category=Category.LIMITATION,
                    stable_args=("semantic-visits",),
                )
            self.analysis_incomplete = True
            return InferredValue.invalid()

        if isinstance(value, Mapping) and len(value) == 1:
            name = next(iter(value))
            if name == "Ref" or (isinstance(name, str) and name.startswith("Fn::")):
                effective_context = context
                if self._eval_reparse_depth:
                    spec = function_spec(str(name))
                    if spec is not None and ExpressionContext.NORMAL in spec.contexts:
                        # Eval's temporary stack parses the normal function
                        # table before the expanded value reaches its final
                        # occurrence context.
                        effective_context = ExpressionContext.NORMAL
                    elif spec is None or context not in spec.contexts:
                        # An unregistered function-shaped Map survives the
                        # first Eval pass as a residual template value.
                        children = self._analyze_intrinsic_children(
                            value[name],
                            _key(path, name),
                            context,
                            function_depth,
                            count_position_eligible,
                            semantic_reachable=semantic_reachable,
                        )
                        if self._children_poisoned(children):
                            return InferredValue.invalid()
                        residual_type = self._children_type(children)
                        return self._check_expected(
                            InferredValue.dynamic(map_of(STRING, residual_type)),
                            expected,
                            path,
                        )
                if function_depth >= MAX_FUNCTION_DEPTH:
                    self.diagnostic(
                        "ROS2003",
                        _("ROS function nesting exceeds 20 levels."),
                        _("ROS does not accept a function at level 21."),
                        path,
                        stable_args=(str(function_depth + 1),),
                    )
                    return InferredValue.invalid()
                inferred = self._analyze_function(
                    str(name),
                    value[name],
                    _key(path, name),
                    effective_context,
                    function_depth + 1,
                    count_position_eligible=count_position_eligible,
                    consumer_resource_type=consumer_resource_type,
                    consumer_section=consumer_section,
                    semantic_reachable=semantic_reachable,
                )
                return self._check_expected(inferred, expected, path)

        if isinstance(value, list):
            children = [
                self.analyze(
                    item,
                    _index(path, index),
                    context,
                    function_depth=function_depth,
                    count_position_eligible=count_position_eligible,
                    consumer_resource_type=consumer_resource_type,
                    consumer_section=consumer_section,
                    semantic_reachable=semantic_reachable,
                )
                for index, item in enumerate(value)
            ]
            if any(item.poisoned for item in children):
                return InferredValue.invalid()
            item_type = union_of(*(item.type for item in children)) if children else ANY_VALUE
            if all(item.knowledge == ValueKnowledge.CONSTANT for item in children):
                inferred = InferredValue.constant([item.value for item in children], ros_type=list_of(item_type))
            else:
                inferred = InferredValue.dynamic(list_of(item_type))
            if any(item.may_refer_no_value for item in children):
                inferred = replace(inferred, may_refer_no_value=True)
            return self._check_expected(inferred, expected, path)

        if isinstance(value, Mapping):
            children: dict[Any, InferredValue] = {}
            for key, item in value.items():
                children[key] = self.analyze(
                    item,
                    _key(path, key),
                    context,
                    function_depth=function_depth,
                    count_position_eligible=count_position_eligible,
                    consumer_resource_type=consumer_resource_type,
                    consumer_section=consumer_section,
                    semantic_reachable=semantic_reachable,
                )
            if any(item.poisoned for item in children.values()):
                return InferredValue.invalid()
            key_type = union_of(*(infer_mapping_key_type(key) for key in children)) if children else ANY_VALUE
            value_type = union_of(*(item.type for item in children.values())) if children else ANY_VALUE
            if all(item.knowledge == ValueKnowledge.CONSTANT for item in children.values()):
                inferred = InferredValue.constant(
                    {key: item.value for key, item in children.items()}, ros_type=map_of(key_type, value_type)
                )
            else:
                inferred = InferredValue.dynamic(map_of(key_type, value_type))
            if any(item.may_refer_no_value for item in children.values()):
                inferred = replace(inferred, may_refer_no_value=True)
            return self._check_expected(inferred, expected, path)

        inferred = InferredValue.constant(value)
        if inferred.type.kind == TypeKind.UNKNOWN and semantic_reachable:
            self._report_unknown_type(path, provenance=type(value).__name__)
        return self._check_expected(inferred, expected, path)

    def _children_poisoned(self, children: Any) -> bool:
        if isinstance(children, InferredValue):
            return children.poisoned
        if isinstance(children, list):
            return any(self._children_poisoned(item) for item in children)
        if isinstance(children, Mapping):
            return any(self._children_poisoned(item) for item in children.values())
        return False

    def _children_type(self, children: Any) -> RosType:
        if isinstance(children, InferredValue):
            return children.type
        if isinstance(children, list):
            return list_of(union_of(*(self._children_type(item) for item in children))) if children else list_of()
        if isinstance(children, Mapping):
            return (
                map_of(
                    union_of(*(infer_mapping_key_type(key) for key in children)),
                    union_of(*(self._children_type(item) for item in children.values())),
                )
                if children
                else map_of()
            )
        return UNKNOWN_TYPE

    def _check_expected(self, inferred: InferredValue, expected: RosType | None, path: RosPath) -> InferredValue:
        if expected is None or inferred.poisoned:
            return inferred
        if compatibility(inferred.type, expected) == Compatibility.DEFINITE_MISMATCH:
            self.diagnostic(
                "ROS3002",
                _("Expression type {} is incompatible with its consumer position.").format(inferred.type),
                _("This position requires {}.").format(expected),
                path,
                subject="result",
                stable_args=(str(expected), str(inferred.type)),
                expected=str(expected),
                actual=str(inferred.type),
            )
        return inferred

    def _analyze_function(
        self,
        name: str,
        args: Any,
        path: RosPath,
        context: ExpressionContext,
        depth: int,
        *,
        count_position_eligible: bool,
        consumer_resource_type: str | None,
        consumer_section: str | None,
        semantic_reachable: bool,
    ) -> InferredValue:
        spec = function_spec(name)
        if spec is None:
            self.diagnostic(
                "ROS2001",
                _("Unknown ROS function {}.").format(name),
                _("This name is not in the registry of 43 ROS 2015-09-01 runtime functions."),
                path,
                stable_args=(name,),
                suggestion=_("Check the function name."),
            )
            self._analyze_intrinsic_children(
                args,
                path,
                context,
                depth,
                count_position_eligible,
                consumer_resource_type=consumer_resource_type,
                consumer_section=consumer_section,
            )
            return InferredValue.invalid()
        if context not in spec.contracts_by_context:
            self.diagnostic(
                "ROS2002",
                _("Function {} cannot be used in {} context.").format(name, context.value),
                _("ROS registers different function tables for normal expressions, Conditions/Rules, and Count."),
                path,
                stable_args=(name, context.value),
            )
            self._analyze_intrinsic_children(
                args,
                path,
                context,
                depth,
                count_position_eligible,
                consumer_resource_type=consumer_resource_type,
                consumer_section=consumer_section,
            )
            return InferredValue.invalid()
        contract = spec.contracts_by_context[context]

        if self.semantic_mode == TemplateSemanticMode.MODULE_REGISTRATION and name == "Fn::GetStackOutput":
            self.diagnostic(
                "ROS1125",
                _("A Module registration template cannot use Fn::GetStackOutput."),
                _("Module registration cannot depend on the runtime output of another Stack."),
                path,
                stable_args=(name,),
            )
            self._analyze_intrinsic_children(
                args,
                path,
                context,
                depth,
                count_position_eligible,
                consumer_resource_type=consumer_resource_type,
                consumer_section=consumer_section,
                semantic_reachable=semantic_reachable,
            )
            return InferredValue.invalid()

        if not semantic_reachable:
            self._unreachable_source_shape_valid(name, args, path, consumer_section)
            self._analyze_intrinsic_children(
                args,
                path,
                context,
                depth,
                count_position_eligible,
                consumer_resource_type=consumer_resource_type,
                consumer_section=consumer_section,
                semantic_reachable=False,
            )
            return InferredValue.dynamic(spec.return_type)

        if name == "Fn::Select" and count_position_eligible:
            folded = self.count_select_facts.get(
                (path_identity(path), self._count_index_value),
                fold_count_select(args, self._resolve_count_compile_function, self._resolve_compile_value),
            )
            if folded.activated:
                if folded.precompile_failure is not None:
                    self.diagnostic(
                        "ROS4304",
                        _("Fn::Select cannot select an element during Count precompilation."),
                        folded.precompile_failure,
                        path,
                        stable_args=(folded.precompile_failure,),
                    )
                    return InferredValue.invalid()
                origin = folded.selected_origin if folded.selected_origin is not None else 1
                return self.analyze(
                    folded.transformed_node,
                    _index(path, origin),
                    context,
                    function_depth=depth,
                    # Positive out-of-bounds CountSelectFold returns args[2]
                    # directly.  ROS does not run Count _calc() over that raw
                    # default before deleting the Count base resources.
                    count_position_eligible=count_position_eligible and origin != 2,
                    consumer_resource_type=consumer_resource_type,
                    consumer_section=consumer_section,
                    semantic_reachable=semantic_reachable,
                )

        if name == "Ref":
            if (
                isinstance(args, str)
                and args in self.symbols.locals
                and context != ExpressionContext.RULE
                and consumer_section not in _LOCAL_INELIGIBLE_RESOURCE_SECTIONS
            ):
                if self._is_poisoned_symbol(args):
                    return InferredValue.invalid()
                return self._resolve_local(
                    args,
                    path,
                    context,
                    count_position_eligible,
                    consumer_resource_type,
                    consumer_section,
                )
            if contract.implementation == "ParamRef":
                return self._param_ref(
                    args,
                    path,
                    context,
                    count_position_eligible,
                    semantic_reachable,
                    consumer_resource_type,
                    consumer_section,
                )
            return self._ref(
                args,
                path,
                context,
                count_position_eligible,
                semantic_reachable,
                consumer_resource_type,
                consumer_section,
            )
        if name == "Fn::GetAtt":
            return self._get_att(
                args,
                path,
                context,
                depth,
                count_position_eligible,
                semantic_reachable,
                consumer_resource_type,
                consumer_section,
            )
        if name == "Fn::If":
            return self._if(
                args,
                path,
                context,
                depth,
                count_position_eligible,
                consumer_resource_type,
                consumer_section,
            )
        if name == "Fn::Select":
            return self._select_lazy(
                args,
                path,
                context,
                depth,
                count_position_eligible,
                consumer_resource_type,
                consumer_section,
            )

        children = self._analyze_intrinsic_children(
            args,
            path,
            context,
            depth,
            count_position_eligible,
            consumer_resource_type=consumer_resource_type,
            consumer_section=consumer_section,
            semantic_reachable=semantic_reachable,
        )
        if name != "Fn::Sub" and not _is_script_content_path(path):
            self._scan_unexpanded_strings(
                args,
                path,
                ref_names=_raw_ref_names(args),
            )
        handler = getattr(self, "_fn_{}".format(name.removeprefix("Fn::").replace("::", "_")), None)
        if handler is None:
            return InferredValue.dynamic(spec.return_type)
        try:
            result = handler(
                args,
                children,
                path,
                context,
                depth,
                count_position_eligible,
                consumer_resource_type,
                consumer_section,
                spec,
            )
            if self._children_may_refer_no_value(children) and not result.poisoned:
                result = replace(result, may_refer_no_value=True)
            return result
        except (AttributeError, IndexError, KeyError, OverflowError, TypeError, ValueError) as error:
            self.diagnostic(
                "ROS3003",
                _("The known arguments of {} cannot be evaluated under the ROS runtime contract.").format(name),
                _(
                    "Local precomputation failed at the {} boundary; the remaining independent expressions will still be validated."
                ).format(type(error).__name__),
                path,
                stable_args=(name, type(error).__name__),
            )
            return InferredValue.invalid()

    def _children_may_refer_no_value(self, children: Any) -> bool:
        if isinstance(children, InferredValue):
            return children.may_refer_no_value
        if isinstance(children, list):
            return any(self._children_may_refer_no_value(item) for item in children)
        if isinstance(children, Mapping):
            return any(self._children_may_refer_no_value(item) for item in children.values())
        return False

    def _analyze_intrinsic_children(
        self,
        args: Any,
        path: RosPath,
        context: ExpressionContext,
        depth: int,
        count_position_eligible: bool,
        *,
        consumer_resource_type: str | None = None,
        consumer_section: str | None = None,
        semantic_reachable: bool = True,
    ) -> Any:
        if isinstance(args, Mapping) and len(args) == 1:
            name = next(iter(args))
            if name == "Ref" or (isinstance(name, str) and name.startswith("Fn::")):
                return self.analyze(
                    args,
                    path,
                    context,
                    function_depth=depth,
                    count_position_eligible=count_position_eligible,
                    consumer_resource_type=consumer_resource_type,
                    consumer_section=consumer_section,
                    semantic_reachable=semantic_reachable,
                )
        if isinstance(args, list):
            return [
                self.analyze(
                    item,
                    _index(path, index),
                    context,
                    function_depth=depth,
                    count_position_eligible=count_position_eligible,
                    consumer_resource_type=consumer_resource_type,
                    consumer_section=consumer_section,
                    semantic_reachable=semantic_reachable,
                )
                for index, item in enumerate(args)
            ]
        if isinstance(args, Mapping):
            return {
                key: self.analyze(
                    item,
                    _key(path, key),
                    context,
                    function_depth=depth,
                    count_position_eligible=count_position_eligible,
                    consumer_resource_type=consumer_resource_type,
                    consumer_section=consumer_section,
                    semantic_reachable=semantic_reachable,
                )
                for key, item in args.items()
            }
        if isinstance(args, (str, int, float, bool, bytes)) or args is None:
            return InferredValue.constant(args)
        return InferredValue.dynamic(UNKNOWN_TYPE)

    def _argument_value(self, children: Any) -> InferredValue:
        """Reconstruct the value represented by a function's complete argument node.

        ``_analyze_intrinsic_children`` preserves container structure so handlers
        with positional arguments can inspect individual items.  Functions that
        consume one arbitrary expression need the corresponding aggregate value
        instead; treating a literal List/Map as Poisoned would suppress the root
        type error and lose useful diagnostics.
        """

        if isinstance(children, InferredValue):
            return children
        if isinstance(children, list):
            if any(value.poisoned for value in children):
                return InferredValue.invalid()
            may_refer_no_value = any(value.may_refer_no_value for value in children)
            if all(value.knowledge == ValueKnowledge.CONSTANT for value in children):
                return replace(
                    InferredValue.constant([value.value for value in children]),
                    may_refer_no_value=may_refer_no_value,
                )
            item_type = union_of(*(value.type for value in children)) if children else ANY_VALUE
            return InferredValue.dynamic(list_of(item_type), may_refer_no_value=may_refer_no_value)
        if isinstance(children, Mapping):
            values = cast(list[InferredValue], list(children.values()))
            if any(value.poisoned for value in values):
                return InferredValue.invalid()
            may_refer_no_value = any(value.may_refer_no_value for value in values)
            if all(value.knowledge == ValueKnowledge.CONSTANT for value in values):
                return replace(
                    InferredValue.constant({key: value.value for key, value in children.items()}),
                    may_refer_no_value=may_refer_no_value,
                )
            key_type = union_of(*(infer_mapping_key_type(key) for key in children)) if children else ANY_VALUE
            value_type = union_of(*(value.type for value in values)) if values else ANY_VALUE
            return InferredValue.dynamic(
                map_of(key_type, value_type),
                may_refer_no_value=may_refer_no_value,
            )
        return InferredValue.dynamic(UNKNOWN_TYPE)

    def _shape_error(self, name: str, path: RosPath, expected: str, actual: Any) -> InferredValue:
        self.diagnostic(
            "ROS3001",
            _("{} has an invalid argument shape.").format(name),
            _("The runtime requires {}.").format(expected),
            path,
            subject="arguments",
            stable_args=(name, expected, type(actual).__name__),
            expected=expected,
            actual=type(actual).__name__,
        )
        return InferredValue.invalid()

    def _type_error(self, name: str, index: int, expected: str, actual: InferredValue, path: RosPath) -> None:
        if actual.poisoned:
            return
        self.diagnostic(
            "ROS3002",
            _("{} argument {} has incompatible type {}.").format(name, index + 1, actual.type),
            _("This argument requires {}.").format(expected),
            _index(path, index),
            subject="argument-{}".format(index),
            stable_args=(name, str(index), expected, str(actual.type)),
            expected=expected,
            actual=str(actual.type),
        )

    def _warn_boolean_as_integer(self, name: str, index: int, value: InferredValue, path: RosPath) -> None:
        if self.policy != ValidationPolicy.STRICT or not _contains_kind(value.type, TypeKind.BOOLEAN):
            return
        self.diagnostic(
            "ROS5003",
            _("{} argument {} uses Boolean as Integer.").format(name, index + 1),
            _(
                "The ROS Python runtime accepts this value, but an explicit Integer is clearer and better matches the official template contract."
            ),
            _index(path, index),
            severity=Severity.WARNING,
            category=Category.QUALITY,
            subject="argument-{}".format(index),
            stable_args=(name, str(index), "boolean-as-integer"),
        )

    def _warn_nonfinite_result(self, name: str, path: RosPath) -> None:
        if self.policy != ValidationPolicy.STRICT:
            return
        self.diagnostic(
            "ROS5205",
            _("The known result of {} is a non-finite Number.").format(name),
            _(
                "The locked runtime returns NaN/Infinity, but that value violates standard JSON and the official template numeric constraints."
            ),
            path,
            severity=Severity.WARNING,
            category=Category.QUALITY,
            subject="nonfinite-result",
            stable_args=(name, "nonfinite-result"),
        )

    def _validate_hashable_members(
        self,
        name: str,
        argument_index: int,
        value: InferredValue,
        path: RosPath,
        raw_value: Any,
    ) -> bool:
        if value.knowledge != ValueKnowledge.CONSTANT:
            item_type = _list_item_type(value.type)
            if (
                _raw_guarantees_non_empty_list(raw_value)
                and item_type is not None
                and compatibility(item_type, HASHABLE_SCALAR) == Compatibility.DEFINITE_MISMATCH
            ):
                self._type_error(name, argument_index, "List[HashableScalar]", value, path)
                return False
            return True
        if not isinstance(value.value, list):
            return True
        valid = True
        for member_index, member in enumerate(value.value):
            if getattr(member, "__hash__", None) is not None:
                continue
            self.diagnostic(
                "ROS3003",
                _("{} List members must be hashable.").format(name),
                _("The runtime checks each item for hashability before comparing collections."),
                _index(_index(path, argument_index), member_index),
                stable_args=(name, "unhashable-member", str(argument_index), str(member_index)),
            )
            valid = False
        return valid

    def _list_args(
        self, name: str, args: Any, children: Any, path: RosPath, lengths: tuple[int, ...]
    ) -> list[InferredValue] | None:
        if not isinstance(args, list) or len(args) not in lengths or not isinstance(children, list):
            self._shape_error(name, path, "List with length {}".format("/".join(map(str, lengths))), args)
            return None
        return children

    def _iterable_args(
        self,
        name: str,
        args: Any,
        children: Any,
        path: RosPath,
        lengths: tuple[int, ...],
        *,
        reject_string: bool = False,
        reject_mapping: bool = False,
    ) -> tuple[list[Any], list[InferredValue]] | None:
        """Return the values consumed by a ROS constructor that unpacks args.

        Several locked runtime constructors use ``a, b = self.args`` without a
        List guard.  A Mapping therefore contributes its keys and other raw
        iterables contribute their members.  The ordinary recursive parser has
        already visited Mapping values; constructor consumers must nevertheless
        reason about the iterated keys, not those values.
        """

        if reject_string and isinstance(args, str):
            self._shape_error(
                name, path, "non-String Iterable with length {}".format("/".join(map(str, lengths))), args
            )
            return None
        if reject_mapping and isinstance(args, Mapping):
            self._shape_error(
                name, path, "non-Mapping Iterable with length {}".format("/".join(map(str, lengths))), args
            )
            return None
        if not isinstance(args, Iterable):
            self._shape_error(name, path, "Iterable with length {}".format("/".join(map(str, lengths))), args)
            return None
        try:
            raw_values = list(args)
        except (TypeError, ValueError):
            self._shape_error(name, path, "Iterable with length {}".format("/".join(map(str, lengths))), args)
            return None
        if len(raw_values) not in lengths:
            self._shape_error(name, path, "Iterable with length {}".format("/".join(map(str, lengths))), args)
            return None
        if isinstance(args, list) and isinstance(children, list):
            return raw_values, children
        # Mapping/string/tuple outer forms are constructor-only runtime
        # extensions. Their iterated members are raw scalar arguments.
        if self.policy == ValidationPolicy.STRICT:
            self.diagnostic(
                "ROS5208",
                _("{} uses a runtime-only non-List Iterable outer container.").format(name),
                _(
                    "The locked constructor unpacks by iteration order; the official template contract recommends a List."
                ),
                path,
                severity=Severity.WARNING,
                category=Category.QUALITY,
                stable_args=(name, type(args).__name__, "iterable-outer"),
            )
        return raw_values, [InferredValue.constant(item) for item in raw_values]

    def _constructor_visible_value(
        self,
        value: Any,
        consumer_section: str | None,
        inferred: InferredValue | None = None,
        seen_locals: frozenset[str] = frozenset(),
    ) -> Any:
        """Return the value visible when the final ROS function is constructed.

        Macro Locals are substituted structurally before the final parse, while
        Eval Locals are resolved and then substituted. Parameter Ref and other
        intrinsic mappings remain Function objects and therefore must not be
        mistaken for their eventual result at constructor time.
        """

        if not isinstance(value, Mapping) or len(value) != 1 or "Ref" not in value:
            return value
        if consumer_section in _LOCAL_INELIGIBLE_RESOURCE_SECTIONS or not isinstance(value.get("Ref"), str):
            return value
        local_name = value["Ref"]
        local = self.symbols.locals.get(local_name)
        if local is None:
            return value
        if local_name in seen_locals:
            return _CONSTRUCTOR_UNKNOWN
        next_seen = seen_locals | {local_name}
        if local.local_type == "Macro":
            return self._expand_macro_constructor_value(local.value, consumer_section, next_seen)
        if local.local_type == "Eval":
            if inferred is not None and not inferred.poisoned and inferred.knowledge == ValueKnowledge.CONSTANT:
                return inferred.value
            expanded = self._expand_macro_constructor_value(local.value, consumer_section, next_seen)
            if expanded is _CONSTRUCTOR_UNKNOWN or self._contains_constructor_function(expanded):
                return _CONSTRUCTOR_UNKNOWN
            return normalize(expanded)
        return _CONSTRUCTOR_UNKNOWN

    def _expand_macro_constructor_value(
        self,
        value: Any,
        consumer_section: str | None,
        seen_locals: frozenset[str],
    ) -> Any:
        if isinstance(value, Mapping) and len(value) == 1 and "Ref" in value:
            return self._constructor_visible_value(value, consumer_section, seen_locals=seen_locals)
        if isinstance(value, Mapping):
            expanded: dict[Any, Any] = {}
            for key, item in value.items():
                resolved = self._expand_macro_constructor_value(item, consumer_section, seen_locals)
                expanded[key] = item if resolved is _CONSTRUCTOR_UNKNOWN else resolved
            return expanded
        if isinstance(value, list):
            expanded_items: list[Any] = []
            for item in value:
                resolved = self._expand_macro_constructor_value(item, consumer_section, seen_locals)
                expanded_items.append(item if resolved is _CONSTRUCTOR_UNKNOWN else resolved)
            return expanded_items
        return value

    @staticmethod
    def _contains_constructor_function(value: Any) -> bool:
        if _raw_function_name(value) is not None:
            return True
        if isinstance(value, Mapping):
            return any(ExpressionAnalyzer._contains_constructor_function(item) for item in value.values())
        if isinstance(value, list):
            return any(ExpressionAnalyzer._contains_constructor_function(item) for item in value)
        return False

    def _constructor_visible_mapping(
        self,
        value: Any,
        consumer_section: str | None,
        inferred: InferredValue | None = None,
    ) -> bool | None:
        visible = self._constructor_visible_value(value, consumer_section, inferred)
        if visible is _CONSTRUCTOR_UNKNOWN:
            return None
        return isinstance(visible, Mapping) and _raw_function_name(visible) is None

    def _unreachable_source_shape_valid(
        self,
        name: str,
        args: Any,
        path: RosPath,
        consumer_section: str | None,
    ) -> bool:
        """Check constructor-visible raw shape without evaluating consumers.

        Fn::If and Fn::Select still construct functions in branches/defaults
        that are not selected.  Only intrinsic/raw-shape failures from those
        nodes are relevant; Ref/GetAtt lookup and resolved type errors remain
        suppressed.
        """

        exact_list_lengths: dict[str, tuple[int, ...]] = {
            "Fn::Calculate": (2, 3),
            "Fn::Contains": (2,),
            "Fn::EachMemberIn": (2,),
            "Fn::Equals": (2,),
            "Fn::GetStackOutput": (2, 3),
            "Fn::If": (3,),
            "Fn::MatchPattern": (2,),
            "Fn::MergeMap": (2,),
            "Fn::TransformNamespace": (3,),
        }
        if name in exact_list_lengths:
            valid = isinstance(args, list) and len(args) in exact_list_lengths[name]
        elif name in {"Fn::Join", "Fn::Split", "Fn::Avg"}:
            valid = isinstance(args, Iterable) and not isinstance(args, (str, Mapping)) and len(list(args)) == 2
        elif name in {
            "Fn::FindInMap",
            "Fn::GetAtt",
            "Fn::MemberListToMap",
            "Fn::Cidr",
        }:
            valid = (
                isinstance(args, Iterable) and len(list(args)) == 3
                if name != "Fn::GetAtt"
                else (isinstance(args, Iterable) and len(list(args)) == 2)
            )
        elif name in {"Fn::GetJsonValue", "Fn::SelectMapList"}:
            valid = isinstance(args, Iterable) and len(list(args)) == 2
        elif name == "Fn::Select":
            valid = (isinstance(args, list) and len(args) in (2, 3, 4)) or (
                not isinstance(args, list) and isinstance(args, Iterable) and len(list(args)) == 2
            )
            if valid and isinstance(args, list) and len(args) == 4:
                error_message = self._constructor_visible_value(args[3], consumer_section)
                valid = error_message is _CONSTRUCTOR_UNKNOWN or error_message is None or isinstance(error_message, str)
        elif name == "Fn::Sub":
            valid = isinstance(args, str) or (
                isinstance(args, list)
                and len(args) == 2
                and (
                    isinstance(args[0], str)
                    or (isinstance(args[0], Mapping) and len(args[0]) == 1 and "Ref" in args[0])
                )
                and isinstance(args[1], Mapping)
                and all(isinstance(key, str) for key in args[1])
            )
        elif name == "Fn::Replace":
            values = list(args) if isinstance(args, Iterable) and not isinstance(args, (str, Mapping)) else []
            valid = len(values) == 2 and self._constructor_visible_mapping(values[0], consumer_section) is not False
        elif name == "Fn::MergeMapToList":
            valid = isinstance(args, list)
        elif name == "Fn::FormatTime":
            valid = (
                isinstance(args, str)
                or (isinstance(args, list) and len(args) in (1, 2))
                or _raw_function_name(args) is not None
            )
        elif name == "Fn::ListMerge":
            valid = isinstance(args, list) or _raw_function_name(args) is not None
        elif name == "Fn::MarketplaceImage":
            image = self._constructor_visible_value(args, consumer_section)
            valid = image is _CONSTRUCTOR_UNKNOWN or isinstance(image, str) and bool(image)
        elif name == "Fn::ResourceFacade":
            valid = isinstance(args, str) and args in {"Metadata", "DeletionPolicy", "UpdatePolicy"}
        elif name == "Fn::Jq":
            valid = (isinstance(args, list) and len(args) == 3) or (
                isinstance(args, Mapping) and len(args) == 3 and set(args) == {0, 1, 2}
            )
        elif name == "Fn::Index":
            valid = (isinstance(args, list) and len(args) == 2) or (
                isinstance(args, Mapping) and len(args) == 2 and set(args) == {0, 1}
            )
        else:
            # The remaining constructors either accept a single expression or
            # perform their raw-shape check only after resolving that expression.
            valid = True
        if not valid:
            self._shape_error(name, path, "runtime constructor source shape", args)
        return valid

    def _indexable_args(
        self, name: str, args: Any, children: Any, path: RosPath, length: int
    ) -> list[InferredValue] | None:
        if isinstance(args, list) and len(args) == length and isinstance(children, list):
            return children
        if (
            isinstance(args, Mapping)
            and len(args) == length
            and set(args) == set(range(length))
            and isinstance(children, Mapping)
        ):
            if self.policy == ValidationPolicy.STRICT:
                self.diagnostic(
                    "ROS5208",
                    _("{} uses a runtime-only indexable Mapping outer container.").format(name),
                    _(
                        "The locked implementation indexes this container from 0 through {}; the official template contract recommends a List."
                    ).format(length - 1),
                    path,
                    severity=Severity.WARNING,
                    category=Category.QUALITY,
                    stable_args=(name, "indexable-mapping"),
                )
            return [children[index] for index in range(length)]
        self._shape_error(name, path, "indexable container with length {}".format(length), args)
        return None

    def _ref(
        self,
        args: Any,
        path: RosPath,
        context: ExpressionContext,
        count_position_eligible: bool,
        semantic_reachable: bool,
        consumer_resource_type: str | None,
        consumer_section: str | None,
    ) -> InferredValue:
        # Locals are occurrence-expanded before the context-specific function
        # table selects ParamRef. Rules are intentionally outside the locked
        # PreCompilerLocals scan range.
        if (
            isinstance(args, str)
            and args in self.symbols.locals
            and context != ExpressionContext.RULE
            and consumer_section not in _LOCAL_INELIGIBLE_RESOURCE_SECTIONS
        ):
            return self._resolve_local(
                args,
                path,
                context,
                count_position_eligible,
                consumer_resource_type,
                consumer_section,
            )
        if context not in {ExpressionContext.NORMAL, ExpressionContext.MODULE}:
            return self._param_ref(
                args,
                path,
                context,
                count_position_eligible,
                semantic_reachable,
                consumer_resource_type,
                consumer_section,
            )
        if isinstance(args, Mapping) and len(args) == 1:
            resolved = self.analyze(
                args,
                path,
                context,
                count_position_eligible=count_position_eligible,
                consumer_resource_type=consumer_resource_type,
                consumer_section=consumer_section,
                semantic_reachable=semantic_reachable,
            )
            if resolved.poisoned:
                return resolved
            if resolved.knowledge == ValueKnowledge.CONSTANT:
                resolved_name = resolved.value
                if self._is_poisoned_symbol(resolved_name):
                    return InferredValue.invalid()
                try:
                    if resolved_name in self.symbols.parameters:
                        return self.symbols.parameters[resolved_name]
                except TypeError:
                    pass
                if isinstance(resolved_name, str) and resolved_name in self.symbols.pseudo_parameters:
                    return self.symbols.pseudo_parameters[resolved_name]
                if semantic_reachable:
                    self.diagnostic(
                        "ROS4001",
                        _("Function-form Ref resolves to {}, which is not a visible Parameter.").format(resolved_name),
                        _(
                            "The Ref factory selects ParamRef before function evaluation; it does not switch to ResourceRef when the result is a resource name."
                        ),
                        path,
                        stable_args=(str(resolved_name), "function-param-ref"),
                    )
                return InferredValue.invalid()
            if compatibility(resolved.type, HASHABLE_SCALAR) == Compatibility.DEFINITE_MISMATCH:
                self._type_error("Ref", 0, "HashableScalar", resolved, path)
                return InferredValue.invalid()
            return InferredValue.dynamic(ANY_VALUE)
        if isinstance(args, Mapping) or isinstance(args, list) or isinstance(args, (int, float, bool, bytes)):
            self._shape_error("Ref", path, "String, Null or Function", args)
            return InferredValue.invalid()
        if args is None:
            return InferredValue.constant(None)
        if not isinstance(args, str):
            return InferredValue.dynamic(ANY_VALUE)

        if self._is_poisoned_symbol(args):
            return InferredValue.invalid()
        if self._is_poisoned_count_instance(args):
            return InferredValue.invalid()

        if args == "ALIYUN::Index":
            if self._count_index_enabled:
                if self._count_index_value is not None:
                    return InferredValue.constant(self._count_index_value, ros_type=INTEGER)
                return InferredValue.dynamic(INTEGER)
            if semantic_reachable:
                self.diagnostic(
                    "ROS4001",
                    _("Ref ALIYUN::Index can be used only in a resource that declares Count."),
                    _("This pseudo parameter is visible only in rewritable content of a Count resource instance."),
                    path,
                    stable_args=(args, "count-scope"),
                )
            return InferredValue.invalid()

        if args in self.symbols.parameters:
            value = self.symbols.parameters[args]
            if value.type.kind == TypeKind.UNKNOWN:
                self._report_unknown_type(path, provenance="parameter")
            return value
        if args in self.symbols.pseudo_parameters:
            return self.symbols.pseudo_parameters[args]
        if args in self.symbols.locals:
            if consumer_section in _LOCAL_INELIGIBLE_RESOURCE_SECTIONS:
                if semantic_reachable:
                    self.diagnostic(
                        "ROS4214",
                        _("{} contains Ref {}, which cannot reference a Local.").format(consumer_section, args),
                        _(
                            "ROS Local precompilation replaces only resource Count and Properties; names in this field are resolved as Parameter Ref."
                        ),
                        path,
                        stable_args=(consumer_section, args, "local-scope"),
                    )
                return InferredValue.invalid()
            return self._resolve_local(
                args,
                path,
                context,
                count_position_eligible,
                consumer_resource_type,
                consumer_section,
            )
        if args in self.symbols.resources:
            if self._local_temporary_depth:
                if semantic_reachable:
                    self.diagnostic(
                        "ROS4212",
                        _("A temporary Local Stack cannot reference outer resource {}.").format(args),
                        _(
                            "The temporary Eval/DataSource Local template copies only Parameters and Locals, not outer Resources/DataSources."
                        ),
                        path,
                        stable_args=(args, "eval-resource-scope"),
                    )
                return InferredValue.invalid()
            symbol = self.symbols.resources[args]
            if symbol.count_info.declared:
                eligibility = ref_count_eligibility(count_position_eligible, args)
                if eligibility.eligible:
                    return InferredValue.dynamic(list_of(symbol.base_ref_type))
                if semantic_reachable:
                    self.diagnostic(
                        "ROS4303",
                        _(
                            "Count resource {} expands into multiple instances, but this Ref cannot be automatically rewritten into an instance list."
                        ).format(args),
                        _(
                            "Place Ref in an expandable expression under resource Properties or Outputs, or reference an explicit instance such as {}[0]."
                        ).format(args),
                        path,
                        stable_args=(args, "ref"),
                    )
                return InferredValue.invalid()
            return InferredValue.dynamic(symbol.base_ref_type)
        expanded = self._dynamic_count_reference(args, path) or self._expanded_resource(args)
        if expanded is not None:
            return InferredValue.dynamic(expanded.base_ref_type)
        if semantic_reachable:
            self.diagnostic(
                "ROS4001",
                _("Ref references nonexistent symbol {}.").format(args),
                _("The name does not appear in Parameters, Resources, Locals, or pseudo parameters."),
                path,
                stable_args=(args,),
                suggestion=_("Correct the logical name or add the missing declaration."),
            )
        return InferredValue.invalid()

    def _param_ref(
        self,
        args: Any,
        path: RosPath,
        context: ExpressionContext,
        count_position_eligible: bool,
        semantic_reachable: bool,
        consumer_resource_type: str | None,
        consumer_section: str | None,
    ) -> InferredValue:
        del count_position_eligible
        if isinstance(args, Mapping) and len(args) == 1:
            name = next(iter(args))
            if name == "Ref" or (isinstance(name, str) and name.startswith("Fn::")):
                resolved = self.analyze(
                    args,
                    path,
                    context,
                    consumer_resource_type=consumer_resource_type,
                    consumer_section=consumer_section,
                    semantic_reachable=semantic_reachable,
                )
                if resolved.poisoned:
                    return resolved
                if resolved.knowledge != ValueKnowledge.CONSTANT:
                    candidates = [*self.symbols.parameters.values(), *self.symbols.pseudo_parameters.values()]
                    return InferredValue.dynamic(union_of(*(item.type for item in candidates)))
                args = resolved.value
        key = normalize(args)
        if self._is_poisoned_symbol(key):
            return InferredValue.invalid()
        try:
            value = self.symbols.parameters.get(key) or self.symbols.pseudo_parameters.get(key)
        except TypeError:
            value = None
        if key == "ALIYUN::Index" and context in {ExpressionContext.COUNT, ExpressionContext.COMPUTED}:
            value = InferredValue.dynamic(INTEGER)
        if value is not None:
            if self.policy == ValidationPolicy.STRICT and not isinstance(key, str):
                self.diagnostic(
                    "ROS5206",
                    _("ParamRef in {} context uses a non-String parameter key.").format(context.value),
                    _(
                        "The locked runtime can look up a HashableScalar, but the official parameter-name and Ref contracts use String."
                    ),
                    path,
                    severity=Severity.WARNING,
                    category=Category.QUALITY,
                    stable_args=(context.value, type(key).__name__),
                )
            return value
        if semantic_reachable:
            self.diagnostic(
                "ROS4001",
                _("{} context contains Ref {}, which is not a visible Parameter.").format(context.value, key),
                _(
                    "This context uses ParamRef; normal Resource/DataSource values are not allowed, and List/Map cannot be used as parameter keys."
                ),
                path,
                stable_args=(context.value, str(key)),
            )
        return InferredValue.invalid()

    def _expanded_resource(self, name: str) -> ResourceSymbol | None:
        direct = self.symbols.resources.get(name)
        if direct is not None:
            return direct
        match = re.fullmatch(r"(.+)\[(0|[1-9][0-9]*)\]", name)
        if match is None:
            return None
        base = self.symbols.resources.get(match.group(1))
        if base is None or not base.count_info.declared or not base.count_info.valid:
            return None
        if base.count_info.length is None:
            return base
        return base if int(match.group(2)) < base.count_info.length else None

    def _is_poisoned_count_instance(self, name: str) -> bool:
        if name in self.symbols.resources:
            return False
        match = re.fullmatch(r"(.+)\[(0|[1-9][0-9]*)\]", name)
        if match is None:
            return False
        base_name = match.group(1)
        base = self.symbols.resources.get(base_name)
        if base is None or not base.count_info.declared:
            return False
        if not base.count_info.valid:
            return True
        if not self._is_poisoned_symbol(base_name):
            return False
        return base.count_info.length is None or int(match.group(2)) < base.count_info.length

    def _dynamic_count_reference(self, name: str, path: RosPath) -> ResourceSymbol | None:
        match = re.fullmatch(r"(.+)\[(0|[1-9][0-9]*)\]", name)
        if match is None or name in self.symbols.resources:
            return None
        base = self.symbols.resources.get(match.group(1))
        if (
            base is None
            or not base.count_info.declared
            or not base.count_info.valid
            or base.count_info.length is not None
        ):
            return None
        self.diagnostic(
            "ROS9103",
            _("Whether Count instance reference {} exists cannot be determined locally.").format(name),
            _(
                "Count length is determined by a dynamic expression; if an instance exists at deployment time, this reference returns a single-instance value."
            ),
            path,
            severity=Severity.LIMITATION,
            category=Category.LIMITATION,
            stable_args=(name, "dynamic-count-reference"),
        )
        return base

    def _resolve_local(
        self,
        name: str,
        path: RosPath,
        context: ExpressionContext,
        count_position_eligible: bool,
        consumer_resource_type: str | None,
        consumer_section: str | None,
    ) -> InferredValue:
        if name in self._local_stack or (name in self._poisoned_locals and name not in self._cyclic_locals):
            return InferredValue.invalid()
        symbol = self.symbols.locals[name]
        if symbol.local_type.startswith("DATASOURCE::"):
            if self._local_stack:
                return InferredValue.dynamic(self.resource_specs.ref_type(symbol.local_type))
            self.diagnostic(
                "ROS4210",
                _("DataSource Local {} cannot remain in the final template scope.").format(name),
                _("A DataSource Local is visible only inside the Local dependency graph."),
                path,
                stable_args=(name,),
            )
            return InferredValue.invalid()
        self._local_stack.append(name)
        local_value_path = _key(_key(_key((), "Locals"), name), "Value")
        self._local_origin_frames.append((path, local_value_path))
        is_eval = symbol.local_type == "Eval"
        if is_eval:
            self._local_eval_depth += 1
            self._local_temporary_depth += 1
            self._eval_reparse_depth += 1
        try:
            return self.analyze(
                symbol.value,
                path,
                context,
                count_position_eligible=count_position_eligible,
                consumer_resource_type=consumer_resource_type,
                consumer_section=consumer_section,
            )
        finally:
            if is_eval:
                self._eval_reparse_depth -= 1
                self._local_temporary_depth -= 1
                self._local_eval_depth -= 1
            self._local_origin_frames.pop()
            self._local_stack.pop()

    def _get_att(
        self,
        args: Any,
        path: RosPath,
        context: ExpressionContext,
        depth: int,
        count_position_eligible: bool,
        semantic_reachable: bool,
        consumer_resource_type: str | None,
        consumer_section: str | None,
    ) -> InferredValue:
        children = self._analyze_intrinsic_children(
            args,
            path,
            context,
            depth,
            count_position_eligible,
            consumer_resource_type=consumer_resource_type,
            consumer_section=consumer_section,
        )
        unpacked = self._iterable_args("Fn::GetAtt", args, children, path, (2,))
        if unpacked is None:
            return InferredValue.invalid()
        _raw_values, values = unpacked
        resource_value, attribute_value = values
        if compatibility(resource_value.type, STRING) == Compatibility.DEFINITE_MISMATCH:
            self._type_error("Fn::GetAtt", 0, "String resource name", values[0], path)
            return InferredValue.invalid()
        if compatibility(attribute_value.type, STRING) == Compatibility.DEFINITE_MISMATCH:
            self._type_error("Fn::GetAtt", 1, "String attribute name", values[1], path)
            return InferredValue.invalid()
        if resource_value.knowledge != ValueKnowledge.CONSTANT:
            return InferredValue.dynamic(ANY_VALUE)
        resource_name = resource_value.value
        if not isinstance(resource_name, str):
            self._type_error("Fn::GetAtt", 0, "String resource name", values[0], path)
            return InferredValue.invalid()
        if self._is_poisoned_symbol(resource_name):
            return InferredValue.invalid()
        if self._is_poisoned_count_instance(resource_name):
            return InferredValue.invalid()
        attribute = attribute_value.value if attribute_value.knowledge == ValueKnowledge.CONSTANT else None
        if attribute is not None and not isinstance(attribute, str):
            self._type_error("Fn::GetAtt", 1, "String attribute name", values[1], path)
            return InferredValue.invalid()
        local = self.symbols.locals.get(resource_name)
        if local is not None and local.local_type.startswith("DATASOURCE::"):
            if not self._local_stack:
                if semantic_reachable:
                    self.diagnostic(
                        "ROS4210",
                        _("DataSource Local {} cannot use Fn::GetAtt in final template scope.").format(resource_name),
                        _("A DataSource Local is visible only in the temporary Stack for the Local dependency graph."),
                        path,
                        stable_args=(resource_name, "getatt"),
                    )
                return InferredValue.invalid()
            return InferredValue.dynamic(
                self.resource_specs.attribute_type(local.local_type, attribute) if attribute is not None else ANY_VALUE
            )
        symbol = self.symbols.resources.get(resource_name)
        expanded_instance = False
        if symbol is not None and self._local_temporary_depth:
            if semantic_reachable:
                self.diagnostic(
                    "ROS4212",
                    _("A temporary Local Stack cannot read attributes of outer resource {}.").format(resource_name),
                    _("The temporary Eval/DataSource Local template does not copy outer Resources/DataSources."),
                    path,
                    stable_args=(resource_name, "eval-getatt-scope"),
                )
            return InferredValue.invalid()
        if symbol is None:
            symbol = self._dynamic_count_reference(resource_name, _index(path, 0)) or self._expanded_resource(
                resource_name
            )
            if symbol is None:
                if semantic_reachable:
                    self.diagnostic(
                        "ROS4002",
                        _("Fn::GetAtt references nonexistent resource {}.").format(resource_name),
                        _("The resource logical name or Count instance name is invalid."),
                        _index(path, 0),
                        stable_args=(resource_name,),
                    )
                return InferredValue.invalid()
            expanded_instance = True
        base_type = (
            self.resource_specs.attribute_type(symbol.resource_type, attribute) if attribute is not None else ANY_VALUE
        )
        if self.evaluation_mode in {
            EvaluationMode.QUERY_PARAM,
            EvaluationMode.INQUIRY,
        } and not symbol.resource_type.startswith("DATASOURCE::"):
            base_type = NULL
        if expanded_instance:
            return InferredValue.dynamic(base_type)
        if symbol.count_info.declared:
            eligibility = getatt_count_eligibility(count_position_eligible, args)
            if eligibility.eligible:
                return InferredValue.dynamic(list_of(base_type))
            if semantic_reachable:
                self.diagnostic(
                    "ROS4303",
                    _(
                        "Count resource {} expands into multiple instances, but this Fn::GetAtt cannot be automatically rewritten into an attribute list."
                    ).format(resource_name),
                    _(
                        "Use Fn::GetAtt: [{}, attribute-name] in an expandable expression under resource Properties or Outputs, or reference an explicit instance such as {}[0]."
                    ).format(resource_name, resource_name),
                    path,
                    stable_args=(resource_name, "getatt"),
                )
            return InferredValue.invalid()
        return InferredValue.dynamic(base_type)

    def _if(
        self,
        args: Any,
        path: RosPath,
        context: ExpressionContext,
        depth: int,
        count_position_eligible: bool,
        consumer_resource_type: str | None,
        consumer_section: str | None,
    ) -> InferredValue:
        if not isinstance(args, list) or len(args) != 3:
            self._analyze_intrinsic_children(
                args,
                path,
                context,
                depth,
                count_position_eligible,
                consumer_resource_type=consumer_resource_type,
                consumer_section=consumer_section,
            )
            return self._shape_error("Fn::If", path, "three-item List", args)
        condition = self._if_condition(
            args[0],
            _index(path, 0),
            context,
            depth,
            False,
            consumer_resource_type,
            consumer_section,
        )
        if condition.poisoned:
            self.analyze(
                args[1],
                _index(path, 1),
                context,
                function_depth=depth,
                count_position_eligible=count_position_eligible,
                consumer_resource_type=consumer_resource_type,
                consumer_section=consumer_section,
                semantic_reachable=False,
            )
            self.analyze(
                args[2],
                _index(path, 2),
                context,
                function_depth=depth,
                count_position_eligible=count_position_eligible,
                consumer_resource_type=consumer_resource_type,
                consumer_section=consumer_section,
                semantic_reachable=False,
            )
            return InferredValue.invalid()
        known = condition.knowledge == ValueKnowledge.CONSTANT and isinstance(condition.value, bool)
        selected_index = 1 if condition.value is True else 2
        if known:
            selected = self.analyze(
                args[selected_index],
                _index(path, selected_index),
                context,
                function_depth=depth,
                count_position_eligible=count_position_eligible,
                consumer_resource_type=consumer_resource_type,
                consumer_section=consumer_section,
            )
            self.analyze(
                args[3 - selected_index],
                _index(path, 3 - selected_index),
                context,
                function_depth=depth,
                count_position_eligible=count_position_eligible,
                consumer_resource_type=consumer_resource_type,
                consumer_section=consumer_section,
                semantic_reachable=False,
            )
            return selected
        true_value = self.analyze(
            args[1],
            _index(path, 1),
            context,
            function_depth=depth,
            count_position_eligible=count_position_eligible,
            consumer_resource_type=consumer_resource_type,
            consumer_section=consumer_section,
        )
        false_value = self.analyze(
            args[2],
            _index(path, 2),
            context,
            function_depth=depth,
            count_position_eligible=count_position_eligible,
            consumer_resource_type=consumer_resource_type,
            consumer_section=consumer_section,
        )
        if true_value.poisoned and false_value.poisoned:
            return InferredValue.invalid()
        return InferredValue.dynamic(
            union_of(true_value.type, false_value.type),
            may_refer_no_value=true_value.may_refer_no_value or false_value.may_refer_no_value,
        )

    def _if_condition(
        self,
        raw: Any,
        path: RosPath,
        context: ExpressionContext,
        depth: int,
        count_position_eligible: bool,
        consumer_resource_type: str | None,
        consumer_section: str | None,
    ) -> InferredValue:
        children = self._analyze_intrinsic_children(
            raw,
            path,
            context,
            depth,
            count_position_eligible,
            consumer_resource_type=consumer_resource_type,
            consumer_section=consumer_section,
        )
        if self._children_poisoned(children):
            return InferredValue.invalid()

        # function.resolve() resolves every List member before
        # ConditionBoolean.get_condition_key() reads only the first one.
        if isinstance(raw, list) and isinstance(children, list):
            if not children:
                return self._invalid_if_condition(path, _("A List condition cannot be empty."), "empty-list")
            first = children[0]
            first_path = _index(path, 0)
            if first.knowledge == ValueKnowledge.CONSTANT and isinstance(first.value, Mapping):
                key = InferredValue.constant(first.value.get("Condition"))
                return self._if_condition_key(key, _key(first_path, "Condition"))
            if (
                first.knowledge != ValueKnowledge.CONSTANT
                and compatibility(first.type, map_of()) != Compatibility.DEFINITE_MISMATCH
            ):
                return InferredValue.dynamic(BOOLEAN)
            return self._if_condition_key(first, first_path)

        if isinstance(raw, Mapping) and isinstance(children, Mapping):
            if "Condition" not in children:
                return self._invalid_if_condition(
                    path, _("A Map condition must contain a Condition key."), "missing-map-key"
                )
            return self._if_condition_key(children["Condition"], _key(path, "Condition"))

        return self._if_condition_argument(self._argument_value(children), path)

    def _if_condition_argument(self, value: InferredValue, path: RosPath) -> InferredValue:
        if value.poisoned:
            return value
        if value.knowledge != ValueKnowledge.CONSTANT:
            allowed = union_of(BOOLEAN, STRING, list_of(), map_of())
            if compatibility(value.type, allowed) == Compatibility.DEFINITE_MISMATCH:
                return self._invalid_if_condition(
                    path, _("condition must resolve to Boolean, String, List, or Map."), "invalid-type"
                )
            return InferredValue.dynamic(BOOLEAN)

        resolved = value.value
        if isinstance(resolved, bool):
            return InferredValue.constant(resolved, ros_type=BOOLEAN)
        if isinstance(resolved, list):
            if not resolved:
                return self._invalid_if_condition(path, _("A List condition cannot be empty."), "empty-list")
            key = resolved[0].get("Condition") if isinstance(resolved[0], Mapping) else resolved[0]
            return self._if_condition_key(InferredValue.constant(key), _index(path, 0))
        if isinstance(resolved, Mapping):
            return self._if_condition_key(InferredValue.constant(resolved.get("Condition")), _key(path, "Condition"))
        return self._if_condition_key(value, path)

    def _if_condition_key(self, key: InferredValue, path: RosPath) -> InferredValue:
        if key.poisoned:
            return key
        if key.knowledge != ValueKnowledge.CONSTANT:
            if compatibility(key.type, union_of(STRING, BOOLEAN)) == Compatibility.DEFINITE_MISMATCH:
                return self._invalid_if_condition(
                    path, _("The condition key must resolve to String or Boolean."), "invalid-key-type"
                )
            # Conditions.is_enable() returns True for every Boolean key, while
            # a dynamic String key depends on the named Condition.
            if compatibility(key.type, BOOLEAN) == Compatibility.DEFINITE_MATCH:
                return InferredValue.constant(True, ros_type=BOOLEAN)
            return InferredValue.dynamic(BOOLEAN)
        if key.value is None:
            return self._invalid_if_condition(path, _("The condition key cannot be Null."), "null-key")
        if isinstance(key.value, bool):
            return InferredValue.constant(True, ros_type=BOOLEAN)
        if isinstance(key.value, str):
            return self._condition_reference(key.value, path)
        return self._invalid_if_condition(path, _("The condition key must be a String or Boolean."), "invalid-key")

    def _invalid_if_condition(self, path: RosPath, detail: str, reason: str) -> InferredValue:
        self.diagnostic(
            "ROS3003",
            _("The known Fn::If condition cannot be evaluated under the ROS runtime contract."),
            detail,
            path,
            stable_args=("Fn::If", "condition", reason),
        )
        return InferredValue.invalid()

    def _scan_unexpanded_strings(
        self,
        value: Any,
        path: RosPath,
        *,
        ref_names: frozenset[str] = frozenset(),
    ) -> None:
        if isinstance(value, str):
            for match in _PLACEHOLDER.finditer(value):
                name = match.group(1).split(".", 1)[0]
                if name not in ref_names and (
                    name in self.symbols.parameters
                    or name in self.symbols.resources
                    or name in self.symbols.locals
                    or name in self.symbols.pseudo_parameters
                ):
                    self.diagnostic(
                        "ROS5001",
                        _("{} in a plain string is not expanded by ROS.").format(match.group(0)),
                        _("Only Fn::Sub expands ${...}; the current function treats it as a literal string."),
                        path,
                        category=Category.QUALITY,
                        stable_args=(name,),
                        suggestion=_("Use Ref/Fn::GetAtt, or place the string inside Fn::Sub."),
                    )
        elif isinstance(value, list):
            for index, item in enumerate(value):
                self._scan_unexpanded_strings(item, _index(path, index), ref_names=ref_names)
        elif isinstance(value, Mapping):
            nested_function = _raw_function_name(value)
            if nested_function in {"Fn::Join", "Fn::Sub"}:
                return
            for key, item in value.items():
                self._scan_unexpanded_strings(item, _key(path, key), ref_names=ref_names)

    def _select_lazy(
        self,
        args: Any,
        path: RosPath,
        context: ExpressionContext,
        depth: int,
        count_position_eligible: bool,
        consumer_resource_type: str | None,
        consumer_section: str | None,
    ) -> InferredValue:
        if isinstance(args, list) and len(args) in (2, 3, 4):
            raw_args = args
        elif isinstance(args, Iterable) and not isinstance(args, list):
            try:
                raw_args = list(args)
            except (TypeError, ValueError):
                raw_args = []
            if len(raw_args) != 2:
                self._analyze_intrinsic_children(
                    args,
                    path,
                    context,
                    depth,
                    count_position_eligible,
                    consumer_resource_type=consumer_resource_type,
                    consumer_section=consumer_section,
                )
                return self._shape_error("Fn::Select", path, "two-item Iterable or 2～4 item List", args)
            # Runtime-only two-item iterable forms consume their members (for a
            # Mapping, its keys). Still visit Mapping values for intrinsic
            # diagnostics because the generic parser constructs them first.
            self._analyze_intrinsic_children(
                args,
                path,
                context,
                depth,
                count_position_eligible,
                consumer_resource_type=consumer_resource_type,
                consumer_section=consumer_section,
            )
            if self.policy == ValidationPolicy.STRICT:
                self.diagnostic(
                    "ROS5208",
                    _("Fn::Select uses a runtime-only non-List outer container."),
                    _(
                        "The locked constructor can unpack exactly two members; the official template contract recommends a List."
                    ),
                    path,
                    severity=Severity.WARNING,
                    category=Category.QUALITY,
                    stable_args=("Fn::Select", type(args).__name__),
                )
        else:
            self._analyze_intrinsic_children(
                args,
                path,
                context,
                depth,
                count_position_eligible,
                consumer_resource_type=consumer_resource_type,
                consumer_section=consumer_section,
            )
            return self._shape_error("Fn::Select", path, "two-item Iterable or 2～4 item List", args)
        lookup = self.analyze(
            raw_args[0],
            _index(path, 0),
            context,
            function_depth=depth,
            count_position_eligible=count_position_eligible,
            consumer_resource_type=consumer_resource_type,
            consumer_section=consumer_section,
        )
        collection = self.analyze(
            raw_args[1],
            _index(path, 1),
            context,
            function_depth=depth,
            count_position_eligible=count_position_eligible,
            consumer_resource_type=consumer_resource_type,
            consumer_section=consumer_section,
        )
        default_reachable = self._select_default_may_be_reached(lookup, collection)
        children = [lookup, collection]
        if len(raw_args) >= 3:
            children.append(
                self.analyze(
                    raw_args[2],
                    _index(path, 2),
                    context,
                    function_depth=depth,
                    count_position_eligible=count_position_eligible,
                    consumer_resource_type=consumer_resource_type,
                    consumer_section=consumer_section,
                    semantic_reachable=default_reachable,
                )
            )
        if len(raw_args) == 4:
            children.append(
                self.analyze(
                    raw_args[3],
                    _index(path, 3),
                    context,
                    function_depth=depth,
                    count_position_eligible=count_position_eligible,
                    consumer_resource_type=consumer_resource_type,
                    consumer_section=consumer_section,
                    semantic_reachable=default_reachable,
                )
            )
        if not _is_script_content_path(path):
            ref_names = _raw_ref_names(raw_args)
            self._scan_unexpanded_strings(raw_args[:2], path, ref_names=ref_names)
            if default_reachable and len(raw_args) >= 3:
                self._scan_unexpanded_strings(raw_args[2:], _index(path, 2), ref_names=ref_names)
        result = self._fn_Select(
            raw_args,
            children,
            path,
            context,
            depth,
            count_position_eligible,
            consumer_resource_type,
            consumer_section,
        )
        if self._children_may_refer_no_value(children) and not result.poisoned:
            result = replace(result, may_refer_no_value=True)
        return result

    @staticmethod
    def _select_default_may_be_reached(lookup: InferredValue, collection: InferredValue) -> bool:
        if lookup.knowledge == ValueKnowledge.CONSTANT and lookup.value is None:
            return True
        if lookup.knowledge != ValueKnowledge.CONSTANT or collection.knowledge != ValueKnowledge.CONSTANT:
            return True
        value = collection.value
        if value in (None, ""):
            return True
        if isinstance(value, str):
            try:
                value = json.loads(value)
            except (json.JSONDecodeError, ValueError):
                return False
            if value is None:
                return True
        try:
            if isinstance(value, list):
                key = lookup.value
                if isinstance(key, str) and ":" in key:
                    parts = [int(item) if item else None for item in key.split(":")]
                    return len(parts) not in (2, 3) or (len(parts) == 3 and parts[2] == 0)
                value[int(key)]
                return False
            if isinstance(value, Mapping) and isinstance(lookup.value, str):
                value[lookup.value]
                return False
        except (KeyError, IndexError, TypeError, ValueError, OverflowError):
            return True
        return False

    # -- Individual runtime contracts -------------------------------------------------

    def _fn_FindInMap(self, args: Any, children: Any, path: RosPath, *rest: Any) -> InferredValue:
        unpacked = self._iterable_args("Fn::FindInMap", args, children, path, (3,))
        if unpacked is None:
            return InferredValue.invalid()
        _raw_args, values = unpacked
        invalid = False
        for index, value in enumerate(values):
            if (
                value.knowledge != ValueKnowledge.CONSTANT
                and compatibility(value.type, HASHABLE_SCALAR) == Compatibility.DEFINITE_MISMATCH
            ):
                self._type_error("Fn::FindInMap", index, "HashableScalar", value, path)
                invalid = True
        if invalid:
            return InferredValue.invalid()
        current: Any = self.symbols.mappings
        labels = ("map-name", "top-key", "second-key")
        for index, value in enumerate(values):
            if value.knowledge != ValueKnowledge.CONSTANT:
                current = None
                continue
            if current is None:
                if getattr(value.value, "__hash__", None) is None:
                    self._type_error("Fn::FindInMap", index, "HashableScalar", value, path)
                    return InferredValue.invalid()
                continue
            try:
                exists = isinstance(current, Mapping) and value.value in current
            except TypeError:
                exists = False
            if not exists:
                self.diagnostic(
                    "ROS4101",
                    _("The literal Mapping key for Fn::FindInMap does not exist."),
                    _("No entry was found in map-name, top-key, second-key order."),
                    _index(path, index),
                    stable_args=(labels[index], str(value.value)),
                )
                return InferredValue.invalid()
            current = current[value.value]
        if all(item.knowledge == ValueKnowledge.CONSTANT for item in values):
            return InferredValue.constant(current)
        return InferredValue.dynamic(ANY_VALUE)

    def _fn_GetAZs(
        self,
        args: Any,
        children: Any,
        path: RosPath,
        context: ExpressionContext,
        depth: int,
        eligible: bool,
        consumer_resource_type: str | None,
        consumer_section: str | None,
        spec: FunctionSpec,
    ) -> InferredValue:
        del context, depth, eligible, spec
        value = self._argument_value(children)
        if not value.poisoned and compatibility(value.type, STRING) == Compatibility.DEFINITE_MISMATCH:
            self._type_error("Fn::GetAZs", 0, "String region", value, path)
        if self.policy == ValidationPolicy.STRICT and (
            consumer_section == "Outputs"
            or (
                consumer_resource_type is not None
                and not consumer_resource_type.startswith(("ALIYUN::ECS::", "ALIYUN::VPC::"))
            )
        ):
            self.diagnostic(
                "ROS5202",
                _("Fn::GetAZs is consumed outside the official ECS/VPC scope."),
                _(
                    "The runtime does not restrict this consumer position, so this is reported only as a quality difference."
                ),
                path,
                severity=Severity.WARNING,
                category=Category.QUALITY,
            )
        return InferredValue.dynamic(list_of(STRING))

    def _fn_GetStackOutput(self, args: Any, children: Any, path: RosPath, *rest: Any) -> InferredValue:
        values = self._list_args("Fn::GetStackOutput", args, children, path, (2, 3))
        if values is None:
            return InferredValue.invalid()
        for index, value in enumerate(values):
            if value.knowledge == ValueKnowledge.CONSTANT and value.value is None:
                return InferredValue.constant(None)
            if compatibility(value.type, union_of(STRING, NULL)) == Compatibility.DEFINITE_MISMATCH:
                self._type_error("Fn::GetStackOutput", index, "String | Null", value, path)
                return InferredValue.invalid()
        return InferredValue.dynamic(ANY_VALUE)

    def _fn_ResourceFacade(self, args: Any, children: Any, path: RosPath, *rest: Any) -> InferredValue:
        if not isinstance(args, str) or args not in {"Metadata", "DeletionPolicy", "UpdatePolicy"}:
            return self._shape_error("Fn::ResourceFacade", path, "Metadata/DeletionPolicy/UpdatePolicy", args)
        self.diagnostic(
            "ROS3010",
            _("Fn::ResourceFacade has no parent_resource in a top-level template."),
            _("This internal function can be evaluated only in a resource facade context."),
            path,
            stable_args=(str(args),),
        )
        return InferredValue.invalid()

    def _fn_MarketplaceImage(
        self,
        args: Any,
        children: Any,
        path: RosPath,
        _context: ExpressionContext,
        _depth: int,
        _eligible: bool,
        _consumer_resource_type: str | None,
        consumer_section: str | None,
        *rest: Any,
    ) -> InferredValue:
        value = self._argument_value(children)
        image = self._constructor_visible_value(args, consumer_section, value)
        if image is _CONSTRUCTOR_UNKNOWN:
            if compatibility(value.type, STRING) == Compatibility.DEFINITE_MISMATCH:
                return self._shape_error("Fn::MarketplaceImage", path, "non-empty raw String", args)
        elif not isinstance(image, str) or not image:
            return self._shape_error("Fn::MarketplaceImage", path, "non-empty raw String", args)
        return InferredValue.dynamic(union_of(STRING, NULL))

    def _fn_Join(self, args: Any, children: Any, path: RosPath, *rest: Any) -> InferredValue:
        unpacked = self._iterable_args("Fn::Join", args, children, path, (2,), reject_string=True, reject_mapping=True)
        if unpacked is None:
            return InferredValue.invalid()
        raw_values, values = unpacked
        delimiter, collection = values
        if collection.knowledge == ValueKnowledge.CONSTANT and collection.value is None:
            collection_value: list[Any] = []
        elif not _is_list(collection):
            self._type_error("Fn::Join", 1, "List | Null", collection, path)
            return InferredValue.invalid()
        else:
            collection_value = collection.value if collection.knowledge == ValueKnowledge.CONSTANT else []
        if collection.knowledge != ValueKnowledge.CONSTANT:
            item_type = _list_item_type(collection.type)
            join_member_type = union_of(STRING, INTEGER, BOOLEAN, NULL)
            if (
                _raw_guarantees_non_empty_list(raw_values[1])
                and item_type is not None
                and compatibility(item_type, join_member_type) == Compatibility.DEFINITE_MISMATCH
            ):
                self._type_error("Fn::Join", 1, "List[String | Integer | Boolean | Null]", collection, path)
                return InferredValue.invalid()
        if compatibility(delimiter.type, STRING) == Compatibility.DEFINITE_MISMATCH:
            self._type_error("Fn::Join", 0, "String", delimiter, path)
            return InferredValue.invalid()
        if delimiter.knowledge == ValueKnowledge.CONSTANT and collection.knowledge == ValueKnowledge.CONSTANT:
            if self.policy == ValidationPolicy.STRICT and any(isinstance(item, bool) for item in collection_value):
                self.diagnostic(
                    "ROS5003",
                    _("The Fn::Join collection uses Boolean as Integer text."),
                    _("The runtime inherits Python bool/int semantics; convert it explicitly first."),
                    _index(path, 1),
                    severity=Severity.WARNING,
                    category=Category.QUALITY,
                    stable_args=("Fn::Join", "boolean-member"),
                )
            invalid = [item for item in collection_value if not isinstance(item, (str, int, bool)) and item is not None]
            if invalid:
                self.diagnostic(
                    "ROS3002",
                    _("The Fn::Join collection contains an item that cannot be joined."),
                    _("Only String, Integer, Boolean, and Null are accepted."),
                    _index(path, 1),
                )
                return InferredValue.invalid()
            return InferredValue.constant(
                str(delimiter.value).join("" if item is None else str(item) for item in collection_value),
                ros_type=STRING,
            )
        return InferredValue.dynamic(STRING)

    def _fn_Split(
        self,
        args: Any,
        children: Any,
        path: RosPath,
        _context: ExpressionContext,
        _depth: int,
        _eligible: bool,
        _consumer_resource_type: str | None,
        consumer_section: str | None,
        *rest: Any,
    ) -> InferredValue:
        unpacked = self._iterable_args("Fn::Split", args, children, path, (2,), reject_string=True, reject_mapping=True)
        if unpacked is None:
            return InferredValue.invalid()
        raw_values, values = unpacked
        delimiter, content = values
        visible_delimiter = self._constructor_visible_value(raw_values[0], consumer_section, delimiter)
        if visible_delimiter is _CONSTRUCTOR_UNKNOWN:
            invalid_delimiter = compatibility(delimiter.type, STRING) == Compatibility.DEFINITE_MISMATCH
        else:
            invalid_delimiter = not isinstance(visible_delimiter, str)
        if invalid_delimiter:
            self._type_error("Fn::Split", 0, "raw String delimiter", delimiter, path)
            return InferredValue.invalid()
        resolved_delimiter = (
            visible_delimiter
            if isinstance(visible_delimiter, str)
            else delimiter.value
            if delimiter.knowledge == ValueKnowledge.CONSTANT and isinstance(delimiter.value, str)
            else None
        )
        if content.knowledge == ValueKnowledge.CONSTANT and content.value is None:
            return InferredValue.constant([], ros_type=list_of(STRING))
        if compatibility(content.type, union_of(STRING, NULL)) == Compatibility.DEFINITE_MISMATCH:
            self._type_error("Fn::Split", 1, "String | Null", content, path)
            return InferredValue.invalid()
        if resolved_delimiter == "" and (
            content.knowledge == ValueKnowledge.CONSTANT
            and content.value is not None
            or compatibility(content.type, NULL) == Compatibility.DEFINITE_MISMATCH
        ):
            self.diagnostic(
                "ROS3003",
                _("The Fn::Split delimiter cannot be empty."),
                _("Python split('') raises an empty separator error."),
                _index(path, 0),
            )
            return InferredValue.invalid()
        if content.knowledge == ValueKnowledge.CONSTANT and resolved_delimiter is not None:
            return InferredValue.constant(content.value.split(resolved_delimiter), ros_type=list_of(STRING))
        return InferredValue.dynamic(list_of(STRING))

    def _fn_Replace(
        self,
        args: Any,
        children: Any,
        path: RosPath,
        context: ExpressionContext,
        depth: int,
        eligible: bool,
        consumer_resource_type: str | None,
        consumer_section: str | None,
        *rest: Any,
    ) -> InferredValue:
        unpacked = self._iterable_args(
            "Fn::Replace", args, children, path, (2,), reject_string=True, reject_mapping=True
        )
        if unpacked is None:
            return InferredValue.invalid()
        raw_values, values = unpacked
        mapping_value, template = values
        visible_mapping = self._constructor_visible_value(raw_values[0], consumer_section, mapping_value)
        constructor_mapping = self._constructor_visible_mapping(raw_values[0], consumer_section, mapping_value)
        if constructor_mapping is False or (
            constructor_mapping is None
            and compatibility(mapping_value.type, map_of()) == Compatibility.DEFINITE_MISMATCH
        ):
            self._type_error("Fn::Replace", 0, "raw Map", mapping_value, path)
            return InferredValue.invalid()
        if template.knowledge == ValueKnowledge.CONSTANT and template.value is None:
            return InferredValue.constant(None)
        if compatibility(template.type, union_of(STRING, NULL)) == Compatibility.DEFINITE_MISMATCH:
            self._type_error("Fn::Replace", 1, "String | Null", template, path)
            return InferredValue.invalid()
        member_mapping: Mapping[Any, Any] | None = None
        if isinstance(visible_mapping, Mapping) and _raw_function_name(visible_mapping) is None:
            member_mapping = visible_mapping
        elif mapping_value.knowledge == ValueKnowledge.CONSTANT and isinstance(mapping_value.value, Mapping):
            member_mapping = mapping_value.value
        for key, value in (member_mapping or {}).items():
            if not isinstance(key, str):
                self.diagnostic(
                    "ROS3002",
                    _("An Fn::Replace replacement key must be a String."),
                    _("A non-Null template consumes the key with replace()."),
                    _key(_index(path, 0), key),
                )
                return InferredValue.invalid()
            child = self.analyze(
                value,
                _key(_index(path, 0), key),
                context,
                function_depth=depth,
                count_position_eligible=eligible,
                consumer_resource_type=consumer_resource_type,
                consumer_section=consumer_section,
            )
            replacement_type = union_of(STRING, INTEGER, NUMBER, BOOLEAN, NULL)
            if compatibility(child.type, replacement_type) == Compatibility.DEFINITE_MISMATCH:
                self._type_error("Fn::Replace", 0, "scalar replacement value", child, path)
                return InferredValue.invalid()
        if template.knowledge == ValueKnowledge.CONSTANT and mapping_value.knowledge == ValueKnowledge.CONSTANT:
            if len(mapping_value.value) > 1:
                return InferredValue.dynamic(union_of(STRING, NULL))
            result = template.value
            for old, new in mapping_value.value.items():
                result = result.replace(old, "" if new is None else str(new))
            return InferredValue.constant(result, ros_type=STRING)
        return InferredValue.dynamic(union_of(STRING, NULL))

    def _fn_Base64(self, args: Any, children: Any, path: RosPath, *rest: Any) -> InferredValue:
        value = self._argument_value(children)
        if compatibility(value.type, STRING) == Compatibility.DEFINITE_MISMATCH:
            self._type_error("Fn::Base64", 0, "String", value, path)
            return InferredValue.invalid()
        return value if value.knowledge == ValueKnowledge.CONSTANT else InferredValue.dynamic(STRING)

    def _fn_Base64Encode(self, args: Any, children: Any, path: RosPath, *rest: Any) -> InferredValue:
        value = self._argument_value(children)
        if value.knowledge == ValueKnowledge.CONSTANT and value.value is None:
            return InferredValue.constant(None)
        if compatibility(value.type, union_of(STRING, NULL)) == Compatibility.DEFINITE_MISMATCH:
            self._type_error("Fn::Base64Encode", 0, "String | Null", value, path)
            return InferredValue.invalid()
        if value.knowledge == ValueKnowledge.CONSTANT:
            encoded = binascii.b2a_base64(value.value.encode("utf-8")).decode("utf-8")
            return InferredValue.constant(encoded, ros_type=STRING)
        return InferredValue.dynamic(union_of(STRING, NULL))

    def _fn_Base64Decode(self, args: Any, children: Any, path: RosPath, *rest: Any) -> InferredValue:
        value = self._argument_value(children)
        if value.knowledge == ValueKnowledge.CONSTANT and value.value is None:
            return InferredValue.constant(None)
        if compatibility(value.type, union_of(STRING, NULL)) == Compatibility.DEFINITE_MISMATCH:
            self._type_error("Fn::Base64Decode", 0, "String | Null", value, path)
            return InferredValue.invalid()
        if value.knowledge == ValueKnowledge.CONSTANT:
            if not _BASE64_PATTERN.match(value.value):
                self.diagnostic(
                    "ROS3003",
                    _("The constant Fn::Base64Decode input does not match the ROS Base64 format."),
                    _(
                        "Input must be non-empty and match the locked runtime padding regex; only one trailing newline accepted by regex `$` is allowed."
                    ),
                    path,
                )
                return InferredValue.invalid()
            try:
                decoded = binascii.a2b_base64(value.value).decode("utf-8")
            except (ValueError, UnicodeDecodeError, binascii.Error):
                self.diagnostic(
                    "ROS3003",
                    _("The constant Fn::Base64Decode input is invalid."),
                    _("Input must be non-empty Base64 that decodes as UTF-8."),
                    path,
                )
                return InferredValue.invalid()
            return InferredValue.constant(decoded, ros_type=STRING)
        return InferredValue.dynamic(union_of(STRING, NULL))

    def _fn_Str(self, args: Any, children: Any, path: RosPath, *rest: Any) -> InferredValue:
        value = self._argument_value(children)
        if value.knowledge == ValueKnowledge.CONSTANT:
            if isinstance(value.value, (list, Mapping)) and not is_json_serializable_value(value.value):
                self.diagnostic(
                    "ROS3004",
                    _("The Fn::Str container cannot be JSON-serialized."),
                    _("The container contains an unsupported key or value."),
                    path,
                )
                return InferredValue.invalid()
            try:
                result = json.dumps(value.value) if isinstance(value.value, (list, Mapping)) else str(value.value)
            except (TypeError, ValueError):
                self.diagnostic(
                    "ROS3004",
                    _("The Fn::Str container cannot be JSON-serialized."),
                    _("The container contains an unsupported key or value."),
                    path,
                )
                return InferredValue.invalid()
            return InferredValue.constant(result, ros_type=STRING)
        return InferredValue.dynamic(STRING)

    def _sub_resource_exists(self, name: str) -> bool:
        return name in self.symbols.resources or self._expanded_resource(name) is not None

    def _sub_binding(self, token: str) -> tuple[str, str]:
        """Return the locked runtime's REF/GETATT/INVALID binding and resource name."""

        if "." not in token:
            return "REF", token
        parts = token.split(".")
        if len(parts) >= 3 and parts[-2] == "Outputs":
            resource_name = ".".join(parts[:-2])
            if self._sub_resource_exists(resource_name):
                parts = [resource_name, ".".join(parts[-2:])]
        if len(parts) >= 3:
            resource_name = ".".join(parts[:-1])
            if self._sub_resource_exists(resource_name):
                parts = [resource_name, parts[-1]]
        if len(parts) != 2:
            return ("REF", token) if self._sub_resource_exists(token) else ("INVALID", token)
        if not self._sub_resource_exists(parts[0]) and self._sub_resource_exists(token):
            return "REF", token
        return "GETATT", parts[0]

    def _fn_Sub(
        self,
        args: Any,
        children: Any,
        path: RosPath,
        context: ExpressionContext,
        depth: int,
        count_position_eligible: bool,
        *rest: Any,
    ) -> InferredValue:
        del context, rest
        if isinstance(args, str):
            template = args
            variables: Mapping[str, Any] = {}
        elif isinstance(args, list) and len(args) == 2 and isinstance(args[1], Mapping):
            if not all(isinstance(key, str) for key in args[1]):
                self.diagnostic(
                    "ROS3002",
                    _("Fn::Sub variable-map keys must be Strings."),
                    _("The Sub constructor iterates over and validates every variable name before evaluation."),
                    _index(path, 1),
                    stable_args=("Fn::Sub", "variable-key"),
                )
                return InferredValue.invalid()
            if isinstance(args[0], str):
                template = args[0]
            elif isinstance(args[0], Mapping) and len(args[0]) == 1 and "Ref" in args[0] and isinstance(children, list):
                template_value = children[0]
                if template_value.poisoned:
                    return InferredValue.invalid()
                if isinstance(args[0]["Ref"], str) and args[0]["Ref"] in self.symbols.resources:
                    return self._shape_error("Fn::Sub", path, "raw String/Parameter Ref as first item", args[0])
                if template_value.knowledge != ValueKnowledge.CONSTANT:
                    return InferredValue.dynamic(STRING)
                if isinstance(template_value.value, (list, Mapping)):
                    if not is_json_serializable_value(template_value.value):
                        self.diagnostic(
                            "ROS3004",
                            _("The Fn::Sub Parameter Ref result cannot be converted to String."),
                            _("The ROS constructor calls to_string on the ParamRef result first."),
                            _index(path, 0),
                        )
                        return InferredValue.invalid()
                    template = json.dumps(template_value.value)
                else:
                    template = str(template_value.value)
            else:
                return self._shape_error("Fn::Sub", path, "raw String/Parameter Ref as first item", args[0])
            variables = args[1]
        else:
            return self._shape_error("Fn::Sub", path, "raw String or [String, Map]", args)
        used: set[str] = set()
        invalid_placeholder = False
        poisoned_placeholder = False
        for match in _PLACEHOLDER.finditer(template):
            token = match.group(1).strip()
            if token.startswith("!"):
                continue
            if not _SUB_STRING_PATTERN.search(match.group(1)):
                self.diagnostic(
                    "ROS3003",
                    _("The Fn::Sub variable name does not match the runtime syntax."),
                    _("The ${...} content must contain at least one name matched by SUB_STRING_PATTERN."),
                    path,
                    stable_args=("Fn::Sub", "placeholder-pattern"),
                )
                invalid_placeholder = True
                continue
            if "." not in token and token in variables:
                used.add(token)
                variable = self.analyze(
                    variables[token],
                    _key(_index(path, 1), token),
                    ExpressionContext.NORMAL,
                    function_depth=depth,
                    count_position_eligible=count_position_eligible,
                )
                if (
                    variable.knowledge == ValueKnowledge.CONSTANT
                    and isinstance(variable.value, (list, Mapping))
                    and not is_json_serializable_value(variable.value)
                ):
                    self.diagnostic(
                        "ROS3004",
                        _("Fn::Sub variable {} cannot be JSON-serialized.").format(token),
                        _("A used List/Map variable enters the runtime to_string JSON branch."),
                        _key(_index(path, 1), token),
                        stable_args=(token, "json-serializable"),
                    )
                continue
            binding, resource = self._sub_binding(token)
            if self._is_poisoned_symbol(resource) or self._is_poisoned_count_instance(resource):
                poisoned_placeholder = True
                continue
            if token == "ALIYUN::Index" and self._count_index_enabled:
                continue
            if binding == "GETATT":
                symbol = self.symbols.resources.get(resource)
                if symbol is not None and symbol.count_info.declared:
                    self.diagnostic(
                        "ROS4303",
                        _(
                            "{} in Fn::Sub references a Count resource that expands into multiple instances; ROS cannot determine which instance attribute to read."
                        ).format(match.group(0)),
                        _(
                            "Use an expandable Fn::GetAtt in the variable map, or reference an explicit instance such as {}[0]."
                        ).format(resource),
                        path,
                        stable_args=(resource, "sub-getatt"),
                    )
                elif not self._sub_resource_exists(resource):
                    self.diagnostic(
                        "ROS4002",
                        _("Fn::Sub references nonexistent resource attribute {}.").format(token),
                        _("The implicit GetAtt cannot bind to a resource."),
                        path,
                        stable_args=(token,),
                    )
            elif binding == "REF":
                symbol = self.symbols.resources.get(resource)
                if symbol is not None and symbol.count_info.declared:
                    self.diagnostic(
                        "ROS4303",
                        _(
                            "{} in Fn::Sub references a Count resource that expands into multiple instances; ROS cannot determine which instance to use."
                        ).format(match.group(0)),
                        _(
                            "Use an expandable Ref in the variable map, or reference an explicit instance such as {}[0]."
                        ).format(resource),
                        path,
                        stable_args=(resource, "sub-ref"),
                    )
                elif (
                    resource not in self.symbols.parameters
                    and resource not in self.symbols.pseudo_parameters
                    and not self._sub_resource_exists(resource)
                ):
                    self.diagnostic(
                        "ROS4001",
                        _("Fn::Sub references nonexistent symbol {}.").format(token),
                        _("The name does not exist in either the variable map or the template symbol table."),
                        path,
                        stable_args=(token,),
                    )
            else:
                self.diagnostic(
                    "ROS4002",
                    _("Fn::Sub references nonexistent resource attribute {}.").format(token),
                    _("The implicit GetAtt cannot bind to a resource."),
                    path,
                    stable_args=(token,),
                )
        if invalid_placeholder or poisoned_placeholder:
            return InferredValue.invalid()
        if self.policy == ValidationPolicy.STRICT:
            for key in variables:
                if key not in used:
                    self.diagnostic(
                        "ROS5203",
                        _("Fn::Sub variable {} is not used by the template.").format(key),
                        _("Unused variables do not affect the result."),
                        _key(_index(path, 1), key),
                        severity=Severity.WARNING,
                        category=Category.QUALITY,
                        stable_args=(str(key),),
                    )
        return InferredValue.dynamic(STRING)

    def _fn_Indent(self, args: Any, children: Any, path: RosPath, *rest: Any) -> InferredValue:
        if args is None:
            return InferredValue.constant(None)
        if isinstance(children, InferredValue):
            if children.knowledge == ValueKnowledge.CONSTANT and children.value is None:
                return InferredValue.constant(None)
            if compatibility(children.type, list_of()) == Compatibility.DEFINITE_MISMATCH:
                self._type_error("Fn::Indent", 0, "Function returning List", children, path)
                return InferredValue.invalid()
            if children.knowledge != ValueKnowledge.CONSTANT:
                item_type = _list_item_type(children.type)
                integer_like = union_of(INTEGER, BOOLEAN)
                if item_type is not None and (
                    compatibility(item_type, STRING) == Compatibility.DEFINITE_MISMATCH
                    or compatibility(item_type, integer_like) == Compatibility.DEFINITE_MISMATCH
                ):
                    self._type_error("Fn::Indent", 0, "List[String, IntegerLike, ...]", children, path)
                    return InferredValue.invalid()
                return InferredValue.dynamic(union_of(STRING, NULL))
            resolved = children.value
            if not isinstance(resolved, list) or len(resolved) not in (2, 3, 4):
                return self._shape_error("Fn::Indent", path, "resolved List with length 2/3/4", resolved)
            values = [InferredValue.constant(value) for value in resolved]
        else:
            values = self._list_args("Fn::Indent", args, children, path, (2, 3, 4))
            if values is None:
                return InferredValue.invalid()
        invalid = False
        if compatibility(values[0].type, STRING) == Compatibility.DEFINITE_MISMATCH:
            self._type_error("Fn::Indent", 0, "String", values[0], path)
            invalid = True
        integer_like = union_of(INTEGER, BOOLEAN)
        for index in (1, 2):
            if index < len(values):
                self._warn_boolean_as_integer("Fn::Indent", index, values[index], path)
                if compatibility(values[index].type, integer_like) == Compatibility.DEFINITE_MISMATCH:
                    self._type_error("Fn::Indent", index, "IntegerLike", values[index], path)
                    invalid = True
                    continue
                if values[index].knowledge == ValueKnowledge.CONSTANT:
                    number = values[index].value
                    limit = 20 if index == 1 else 4
                    if isinstance(number, int) and 0 <= number <= limit:
                        continue
                    self.diagnostic(
                        "ROS3003",
                        _("Integer argument {} of Fn::Indent is out of range.").format(index + 1),
                        _("The allowed range is 0 to {}.").format(limit),
                        _index(path, index),
                    )
                    invalid = True
        if (
            len(values) == 4
            and self.policy == ValidationPolicy.STRICT
            and not _contains_kind(values[3].type, TypeKind.BOOLEAN)
        ):
            self.diagnostic(
                "ROS5204",
                _("The fourth argument of Fn::Indent should be a Boolean."),
                _("The runtime uses truthy semantics, but an explicit Boolean is clearer."),
                _index(path, 3),
                severity=Severity.WARNING,
                category=Category.QUALITY,
            )
        return InferredValue.invalid() if invalid else InferredValue.dynamic(union_of(STRING, NULL))

    def _fn_FormatTime(self, args: Any, children: Any, path: RosPath, *rest: Any) -> InferredValue:
        values: list[InferredValue]
        if isinstance(args, list):
            listed_values = self._list_args("Fn::FormatTime", args, children, path, (1, 2))
            if listed_values is None:
                return InferredValue.invalid()
            values = listed_values
            invalid = False
            for index, value in enumerate(values):
                if compatibility(value.type, STRING) == Compatibility.DEFINITE_MISMATCH:
                    self._type_error("Fn::FormatTime", index, "String", value, path)
                    invalid = True
            if invalid:
                return InferredValue.invalid()
        elif isinstance(args, Mapping):
            value = self._argument_value(children)
            if compatibility(value.type, STRING) == Compatibility.DEFINITE_MISMATCH:
                self._type_error("Fn::FormatTime", 0, "Function returning String", value, path)
                return InferredValue.invalid()
            values = [value]
        elif not isinstance(args, (str, Mapping)):
            return self._shape_error("Fn::FormatTime", path, "String/Function or one/two-item List", args)
        else:
            values = [InferredValue.constant(args)]
        if len(values) == 2 and values[1].knowledge == ValueKnowledge.CONSTANT:
            if not isinstance(values[1].value, str) or tz.gettz(values[1].value) is None:
                self.diagnostic(
                    "ROS3003",
                    _("The Fn::FormatTime time zone is invalid."),
                    _("The locked runtime requires time_zone to be recognized by the time-zone database."),
                    _index(path, 1),
                    stable_args=("Fn::FormatTime", "invalid-time-zone", str(values[1].value)),
                )
                return InferredValue.invalid()
        return InferredValue.dynamic(STRING)

    def _fn_MatchPattern(self, args: Any, children: Any, path: RosPath, *rest: Any) -> InferredValue:
        values = self._list_args("Fn::MatchPattern", args, children, path, (2,))
        if values is None:
            return InferredValue.invalid()
        invalid = False
        for index, value in enumerate(values):
            if compatibility(value.type, STRING) == Compatibility.DEFINITE_MISMATCH:
                self._type_error("Fn::MatchPattern", index, "String", value, path)
                invalid = True
        if invalid:
            return InferredValue.invalid()
        compiled_pattern: re.Pattern[str] | None = None
        if values[0].knowledge == ValueKnowledge.CONSTANT:
            try:
                compiled_pattern = re.compile(values[0].value)
            except re.error:
                self.diagnostic(
                    "ROS3003",
                    _("The Fn::MatchPattern regular expression is invalid."),
                    _("The literal pattern cannot be compiled."),
                    _index(path, 0),
                )
                return InferredValue.invalid()
        if all(value.knowledge == ValueKnowledge.CONSTANT for value in values):
            assert compiled_pattern is not None
            match = compiled_pattern.match(values[1].value)
            return InferredValue.constant(match is not None and match.end() == len(values[1].value), ros_type=BOOLEAN)
        return InferredValue.dynamic(BOOLEAN)

    def _fn_Select(
        self,
        args: Any,
        children: Any,
        path: RosPath,
        _context: ExpressionContext,
        _depth: int,
        _eligible: bool,
        _consumer_resource_type: str | None,
        consumer_section: str | None,
        *rest: Any,
    ) -> InferredValue:
        if not isinstance(args, list) or len(args) not in (2, 3, 4) or not isinstance(children, list):
            return self._shape_error("Fn::Select", path, "2～4 item List", args)
        error_message = (
            self._constructor_visible_value(args[3], consumer_section, children[3]) if len(args) == 4 else None
        )
        invalid_error_message = (
            len(args) == 4
            and error_message is not _CONSTRUCTOR_UNKNOWN
            and error_message is not None
            and not isinstance(error_message, str)
        )
        if invalid_error_message:
            self.diagnostic(
                "ROS3002",
                _("The fourth Fn::Select argument must be a raw String or Null."),
                _("error_message is checked during construction, regardless of whether the key matches."),
                _index(path, 3),
                stable_args=("Fn::Select", "raw-error-message", type(error_message).__name__),
                expected="raw String | Null",
                actual=type(error_message).__name__,
            )
            return InferredValue.invalid()
        lookup, collection = children[0], children[1]
        self._warn_boolean_as_integer("Fn::Select", 0, lookup, path)
        default = children[2] if len(children) >= 3 else InferredValue.constant("")
        if collection.knowledge == ValueKnowledge.CONSTANT and collection.value == "":
            return default
        if lookup.knowledge == ValueKnowledge.CONSTANT and lookup.value is None:
            return default
        collection_value = collection.value if collection.knowledge == ValueKnowledge.CONSTANT else None
        possible_maps = any(member.kind == TypeKind.MAP for member in _members(collection.type))
        possible_lists = any(member.kind == TypeKind.LIST for member in _members(collection.type))
        if (
            possible_maps
            and not possible_lists
            and compatibility(lookup.type, STRING) == Compatibility.DEFINITE_MISMATCH
        ):
            self._type_error("Fn::Select", 0, "String map key", lookup, path)
            return InferredValue.invalid()
        sequence_lookup = union_of(STRING, INTEGER, NUMBER, BOOLEAN, NULL)
        if (
            possible_lists
            and not possible_maps
            and compatibility(lookup.type, sequence_lookup) == Compatibility.DEFINITE_MISMATCH
        ):
            self._type_error("Fn::Select", 0, "IntegerLike or slice String", lookup, path)
            return InferredValue.invalid()
        if isinstance(collection_value, str):
            try:
                collection_value = json.loads(collection_value)
            except (json.JSONDecodeError, ValueError):
                self._select_collection_error(collection, path)
                return InferredValue.invalid()
            if collection_value is None:
                return default
            if not isinstance(collection_value, (list, dict)):
                self._select_collection_error(collection, path)
                return InferredValue.invalid()
        elif (
            compatibility(collection.type, union_of(list_of(), map_of(), NULL, STRING))
            == Compatibility.DEFINITE_MISMATCH
        ):
            self._select_collection_error(collection, path)
            return InferredValue.invalid()
        if collection_value is None and collection.knowledge == ValueKnowledge.CONSTANT:
            return default
        if collection_value is not None and lookup.knowledge == ValueKnowledge.CONSTANT:
            key = lookup.value
            try:
                if isinstance(collection_value, list):
                    if isinstance(key, str) and ":" in key:
                        parts = [int(item) if item else None for item in key.split(":")]
                        if len(parts) not in (2, 3) or (len(parts) == 3 and parts[2] == 0):
                            raise ValueError
                        return InferredValue.constant(collection_value[slice(*parts)])
                    index_value = int(key)
                    selected = collection_value[index_value]
                else:
                    if not isinstance(key, str):
                        raise TypeError
                    selected = collection_value[key]
            except (KeyError, IndexError):
                if len(args) == 4 and error_message is not _CONSTRUCTOR_UNKNOWN and error_message is not None:
                    self.diagnostic(
                        "ROS3003",
                        _("The literal Fn::Select key/index did not match."),
                        _("The fourth error_message is not Null, so the runtime reports an error."),
                        _index(path, 0),
                        stable_args=(str(key),),
                    )
                    return InferredValue.invalid()
                if error_message is _CONSTRUCTOR_UNKNOWN:
                    return InferredValue.dynamic(default.type)
                return default
            except (TypeError, ValueError, OverflowError):
                self.diagnostic(
                    "ROS3003",
                    _("The literal Fn::Select key/index is invalid."),
                    _(
                        "The value cannot be converted to an Integer, a valid slice, or the key required by the collection."
                    ),
                    _index(path, 0),
                    stable_args=("Fn::Select", "invalid-key", str(key)),
                )
                return InferredValue.invalid()
            return InferredValue.constant(selected)
        slice_lookup = False
        if lookup.knowledge == ValueKnowledge.CONSTANT and isinstance(lookup.value, str) and ":" in lookup.value:
            try:
                slice_parts = [int(item) if item else None for item in lookup.value.split(":")]
                slice_lookup = len(slice_parts) in (2, 3) and not (len(slice_parts) == 3 and slice_parts[2] == 0)
            except (TypeError, ValueError):
                slice_lookup = False
        if slice_lookup and possible_lists:
            slice_types: list[RosType] = []
            for member in _members(collection.type):
                if member.kind == TypeKind.LIST:
                    slice_types.append(list_of(member.item_type or ANY_VALUE))
                elif member.kind == TypeKind.MAP:
                    slice_types.append(member.value_type or ANY_VALUE)
                elif member.kind in {TypeKind.NULL, TypeKind.STRING}:
                    slice_types.append(default.type)
            return InferredValue.dynamic(union_of(*(slice_types or [list_of()])))

        item_types: list[RosType] = []
        for member in _members(collection.type):
            if member.kind == TypeKind.LIST and member.item_type:
                item_types.append(member.item_type)
            elif member.kind == TypeKind.MAP and member.value_type:
                item_types.append(member.value_type)
        return InferredValue.dynamic(union_of(*(item_types or [ANY_VALUE]), default.type))

    def _select_collection_error(self, collection: InferredValue, path: RosPath) -> None:
        self.diagnostic(
            "ROS5002",
            _("The second Fn::Select argument is definitively {}, which cannot be used as a collection.").format(
                collection.type
            ),
            _("This argument must be List, Map, Null, EmptyString, or a String that decodes to JSON List/Map/Null."),
            _index(path, 1),
            category=Category.QUALITY,
            subject="argument-1",
            stable_args=(str(collection.type),),
            expected="List | Map | Null | JSON collection String",
            actual=str(collection.type),
            suggestion=_("Make the second argument return an actual collection, or remove Fn::Select."),
        )

    def _fn_MemberListToMap(
        self,
        args: Any,
        children: Any,
        path: RosPath,
        _context: ExpressionContext,
        _depth: int,
        _eligible: bool,
        _consumer_resource_type: str | None,
        consumer_section: str | None,
        *rest: Any,
    ) -> InferredValue:
        unpacked = self._iterable_args("Fn::MemberListToMap", args, children, path, (3,))
        if unpacked is None:
            return InferredValue.invalid()
        raw_values, values = unpacked
        key_name = self._constructor_visible_value(raw_values[0], consumer_section, values[0])
        value_name = self._constructor_visible_value(raw_values[1], consumer_section, values[1])
        invalid_names = any(
            visible is not _CONSTRUCTOR_UNKNOWN
            and not isinstance(visible, str)
            or visible is _CONSTRUCTOR_UNKNOWN
            and compatibility(inferred.type, STRING) == Compatibility.DEFINITE_MISMATCH
            for visible, inferred in ((key_name, values[0]), (value_name, values[1]))
        )
        if invalid_names:
            self.diagnostic(
                "ROS3002",
                _("The first two Fn::MemberListToMap arguments must be raw Strings."),
                _("The name and value field names are consumed during construction."),
                path,
            )
            return InferredValue.invalid()
        if compatibility(values[2].type, union_of(STRING, list_of(), map_of())) == Compatibility.DEFINITE_MISMATCH:
            self._type_error("Fn::MemberListToMap", 2, "Iterable[String]", values[2], path)
            return InferredValue.invalid()
        item_type = _list_item_type(values[2].type)
        if (
            values[2].knowledge != ValueKnowledge.CONSTANT
            and _raw_guarantees_non_empty_list(raw_values[2])
            and item_type is not None
            and compatibility(item_type, STRING) == Compatibility.DEFINITE_MISMATCH
        ):
            self._type_error("Fn::MemberListToMap", 2, "Iterable[String]", values[2], path)
            return InferredValue.invalid()
        if values[2].knowledge == ValueKnowledge.CONSTANT:
            member_list = values[2].value
            if not isinstance(member_list, Iterable):
                self._type_error("Fn::MemberListToMap", 2, "Iterable[String]", values[2], path)
                return InferredValue.invalid()
            for index, member in enumerate(member_list):
                if not isinstance(member, str):
                    self.diagnostic(
                        "ROS3002",
                        _("Fn::MemberListToMap members must be Strings."),
                        _("The runtime calls split('=', 1) for every item."),
                        _index(_index(path, 2), index),
                        stable_args=("Fn::MemberListToMap", "member-string", str(index)),
                    )
                    return InferredValue.invalid()
                if len(member.split("=", 1)) != 2:
                    self.diagnostic(
                        "ROS3003",
                        _("An Fn::MemberListToMap member is missing an equals sign."),
                        _("The member cannot be converted to the key/value pair required by dict()."),
                        _index(_index(path, 2), index),
                        stable_args=("Fn::MemberListToMap", "member-pair", str(index)),
                    )
                    return InferredValue.invalid()
        return InferredValue.dynamic(map_of(STRING, STRING))

    def _fn_ListMerge(self, args: Any, children: Any, path: RosPath, *rest: Any) -> InferredValue:
        if isinstance(children, InferredValue):
            if compatibility(children.type, list_of()) == Compatibility.DEFINITE_MISMATCH:
                self._type_error("Fn::ListMerge", 0, "Function returning List", children, path)
                return InferredValue.invalid()
            item_type = _list_item_type(children.type)
            if (
                _raw_guarantees_non_empty_list(args)
                and item_type is not None
                and compatibility(item_type, union_of(list_of(), NULL)) == Compatibility.DEFINITE_MISMATCH
            ):
                self._type_error("Fn::ListMerge", 0, "Function returning List[List | Null]", children, path)
                return InferredValue.invalid()
            return InferredValue.dynamic(union_of(list_of(ANY_VALUE), NULL))
        if not isinstance(args, list) or not isinstance(children, list):
            return self._shape_error("Fn::ListMerge", path, "List or Function returning List", args)
        values = cast(list[InferredValue], children)
        item_types: list[RosType] = []
        for index, value in enumerate(values):
            if value.knowledge == ValueKnowledge.CONSTANT and value.value is None:
                continue
            if not _is_list(value):
                self._type_error("Fn::ListMerge", index, "List | Null", value, path)
            else:
                item_types.extend(
                    item.item_type for item in _members(value.type) if item.kind == TypeKind.LIST and item.item_type
                )
        return InferredValue.dynamic(union_of(list_of(union_of(*(item_types or [ANY_VALUE]))), NULL))

    def _fn_GetJsonValue(
        self,
        args: Any,
        children: Any,
        path: RosPath,
        _context: ExpressionContext,
        _depth: int,
        _eligible: bool,
        _consumer_resource_type: str | None,
        consumer_section: str | None,
        *rest: Any,
    ) -> InferredValue:
        unpacked = self._iterable_args("Fn::GetJsonValue", args, children, path, (2,))
        if unpacked is None:
            return InferredValue.invalid()
        raw_values, values = unpacked
        key = self._constructor_visible_value(raw_values[0], consumer_section, values[0])
        if key is _CONSTRUCTOR_UNKNOWN:
            invalid_key = compatibility(values[0].type, STRING) == Compatibility.DEFINITE_MISMATCH
        else:
            invalid_key = not isinstance(key, str)
        if invalid_key:
            self._type_error("Fn::GetJsonValue", 0, "raw String key", values[0], path)
            return InferredValue.invalid()
        source = values[1]
        if compatibility(source.type, union_of(STRING, map_of(), NULL)) == Compatibility.DEFINITE_MISMATCH:
            self._type_error("Fn::GetJsonValue", 1, "String/Map/Null", source, path)
            return InferredValue.invalid()
        if source.knowledge == ValueKnowledge.CONSTANT:
            value = source.value
            if value in (None, ""):
                return InferredValue.constant(None)
            if isinstance(value, str):
                try:
                    value = json.loads(value)
                except json.JSONDecodeError:
                    self.diagnostic(
                        "ROS3003",
                        _("Fn::GetJsonValue has an invalid JSON String."),
                        _("The second argument cannot be decoded as a JSON object."),
                        _index(path, 1),
                    )
                    return InferredValue.invalid()
            if not isinstance(value, Mapping):
                self._type_error("Fn::GetJsonValue", 1, "Map/JSON object/Null", source, path)
                return InferredValue.invalid()
            if key is _CONSTRUCTOR_UNKNOWN:
                return InferredValue.dynamic(union_of(STRING, NULL))
            selected = value.get(key)
            if selected is None:
                return InferredValue.constant(None)
            if isinstance(selected, str):
                return InferredValue.constant(selected, ros_type=STRING)
            try:
                return InferredValue.constant(json.dumps(selected), ros_type=STRING)
            except (TypeError, ValueError):
                self.diagnostic(
                    "ROS3004",
                    _("The value selected by Fn::GetJsonValue cannot be JSON-serialized."),
                    _("Only the selected non-String cell enters json.dumps."),
                    path,
                )
                return InferredValue.invalid()
        return InferredValue.dynamic(union_of(STRING, NULL))

    def _fn_MergeMapToList(self, args: Any, children: Any, path: RosPath, *rest: Any) -> InferredValue:
        if not isinstance(args, list) or not isinstance(children, list):
            return self._shape_error("Fn::MergeMapToList", path, "raw List", args)
        values = cast(list[InferredValue], children)
        invalid = False
        constant_pairs: list[tuple[Any, list[Any]]] = []
        all_constant = True
        for index, value in enumerate(values):
            if not _is_mapping(value):
                self._type_error("Fn::MergeMapToList", index, "Map", value, path)
                invalid = True
                continue
            map_value_types = [
                member.value_type
                for member in _members(value.type)
                if member.kind == TypeKind.MAP and member.value_type is not None
            ]
            if value.knowledge != ValueKnowledge.CONSTANT:
                all_constant = False
                if (
                    map_value_types
                    and compatibility(union_of(*map_value_types), union_of(list_of(), NULL))
                    == Compatibility.DEFINITE_MISMATCH
                ):
                    self._type_error("Fn::MergeMapToList", index, "Map[Any, List | Null]", value, path)
                    invalid = True
                continue
            non_empty_values = 0
            for key, member in value.value.items():
                if non_empty_values > 0:
                    self.diagnostic(
                        "ROS3003",
                        _("An Fn::MergeMapToList Map contains more than one valid key/value pair."),
                        _("The locked runtime rejects this Map before the second non-empty value."),
                        _key(_index(path, index), key),
                        stable_args=("Fn::MergeMapToList", "multiple-pairs", str(index)),
                    )
                    invalid = True
                    break
                normalized_member = [""] if member is None else member
                if not isinstance(normalized_member, list):
                    self.diagnostic(
                        "ROS3002",
                        _("An Fn::MergeMapToList Map value must be List or Null."),
                        _("Null is treated as ['']; other types fail deterministically at runtime."),
                        _key(_index(path, index), key),
                        stable_args=("Fn::MergeMapToList", "map-value", str(index), str(key)),
                        expected="List | Null",
                        actual=type(member).__name__,
                    )
                    invalid = True
                    break
                if normalized_member:
                    non_empty_values += 1
                    constant_pairs.append(("" if key is None else key, normalized_member))
        if invalid:
            return InferredValue.invalid()
        if all_constant:
            if not constant_pairs:
                return InferredValue.constant(None)
            size = max(len(member) for _key_name, member in constant_pairs)
            result = [
                {key: member[index] if index < len(member) else member[-1] for key, member in constant_pairs}
                for index in range(size)
            ]
            return InferredValue.constant(result)
        return InferredValue.dynamic(union_of(list_of(map_of()), NULL))

    def _fn_MergeMap(self, args: Any, children: Any, path: RosPath, *rest: Any) -> InferredValue:
        values = self._list_args("Fn::MergeMap", args, children, path, (2,))
        if values is None:
            return InferredValue.invalid()
        invalid = False
        for index, value in enumerate(values):
            if not _is_mapping(value):
                self._type_error("Fn::MergeMap", index, "Map", value, path)
                invalid = True
        if invalid:
            return InferredValue.invalid()
        if all(value.knowledge == ValueKnowledge.CONSTANT for value in values):
            return InferredValue.constant(self._merge_maps(values[0].value, values[1].value))
        return InferredValue.dynamic(map_of())

    def _merge_maps(self, first: Mapping[Any, Any], second: Mapping[Any, Any]) -> dict[Any, Any]:
        result = dict(first)
        for key, value in second.items():
            if key in result and isinstance(result[key], Mapping) and isinstance(value, Mapping):
                result[key] = self._merge_maps(result[key], value)
            else:
                result[key] = value
        return result

    def _fn_SelectMapList(self, args: Any, children: Any, path: RosPath, *rest: Any) -> InferredValue:
        unpacked = self._iterable_args("Fn::SelectMapList", args, children, path, (2,))
        if unpacked is None:
            return InferredValue.invalid()
        _raw_args, values = unpacked
        key, collection = values
        if key.knowledge == ValueKnowledge.CONSTANT and key.value is None:
            return InferredValue.constant(None)
        if collection.knowledge == ValueKnowledge.CONSTANT and collection.value is None:
            return InferredValue.constant(None)
        if not _is_list(collection):
            self._type_error("Fn::SelectMapList", 1, "List[Map | Null] | Null", collection, path)
            return InferredValue.invalid()
        if key.knowledge == ValueKnowledge.CONSTANT and getattr(key.value, "__hash__", None) is None:
            self.diagnostic(
                "ROS3003",
                _("The Fn::SelectMapList key must be hashable."),
                _("The runtime evaluates `key in map_item`; a List/Map key fails deterministically."),
                _index(path, 0),
                stable_args=("Fn::SelectMapList", "unhashable-key"),
            )
            return InferredValue.invalid()
        if (
            key.knowledge != ValueKnowledge.CONSTANT
            and compatibility(key.type, HASHABLE_SCALAR) == Compatibility.DEFINITE_MISMATCH
        ):
            self._type_error("Fn::SelectMapList", 0, "HashableScalar | Null", key, path)
            return InferredValue.invalid()
        item_type = _list_item_type(collection.type)
        if (
            collection.knowledge != ValueKnowledge.CONSTANT
            and _raw_guarantees_non_empty_list(args[1])
            and item_type is not None
            and compatibility(item_type, union_of(map_of(), NULL)) == Compatibility.DEFINITE_MISMATCH
        ):
            self._type_error("Fn::SelectMapList", 1, "List[Map | Null]", collection, path)
            return InferredValue.invalid()
        if collection.knowledge == ValueKnowledge.CONSTANT:
            invalid = False
            for index, member in enumerate(collection.value):
                if member is not None and not isinstance(member, Mapping):
                    self.diagnostic(
                        "ROS3002",
                        _("Fn::SelectMapList collection members must be Maps or Null."),
                        _("Null is skipped; other types cannot participate in key lookup."),
                        _index(_index(path, 1), index),
                        stable_args=("Fn::SelectMapList", "collection-member", str(index)),
                        expected="Map | Null",
                        actual=type(member).__name__,
                    )
                    invalid = True
            if invalid:
                return InferredValue.invalid()
        return InferredValue.dynamic(union_of(list_of(ANY_VALUE), NULL))

    def _fn_Jq(
        self,
        args: Any,
        children: Any,
        path: RosPath,
        _context: ExpressionContext,
        _depth: int,
        _eligible: bool,
        _consumer_resource_type: str | None,
        consumer_section: str | None,
        *rest: Any,
    ) -> InferredValue:
        values = self._indexable_args("Fn::Jq", args, children, path, 3)
        if values is None:
            return InferredValue.invalid()
        method = self._constructor_visible_value(args[0], consumer_section, values[0])
        if method is not _CONSTRUCTOR_UNKNOWN and method not in {"First", "All"}:
            self.diagnostic(
                "ROS3003",
                _("The Fn::Jq method must be First or All."),
                _("method is checked during raw construction."),
                _index(path, 0),
            )
            return InferredValue.invalid()
        raw_script = args[1]
        if isinstance(raw_script, str) and _JQ_ENV.search(raw_script):
            self.diagnostic(
                "ROS3003",
                _("Fn::Jq forbids $ENV in a raw script."),
                _(
                    "This regular-expression check occurs before function evaluation; the result of a dynamic script is not checked here."
                ),
                _index(path, 1),
            )
            return InferredValue.invalid()
        jq_value: Any = values[2].value
        if values[2].knowledge == ValueKnowledge.CONSTANT and values[2].value != "":
            if isinstance(jq_value, str):
                try:
                    jq_value = json.loads(jq_value)
                except json.JSONDecodeError:
                    self.diagnostic(
                        "ROS3003",
                        _("Fn::Jq has a non-empty String value that is not valid JSON."),
                        _("The runtime calls json.loads before executing jq."),
                        _index(path, 2),
                        stable_args=("Fn::Jq", "invalid-json-value"),
                    )
                    return InferredValue.invalid()
        if (
            values[1].knowledge == ValueKnowledge.CONSTANT
            and values[1].value is None
            or values[2].knowledge == ValueKnowledge.CONSTANT
            and values[2].value in (None, "")
        ):
            return InferredValue.constant(None)
        if compatibility(values[1].type, union_of(STRING, NULL)) == Compatibility.DEFINITE_MISMATCH:
            self._type_error("Fn::Jq", 1, "String | Null script", values[1], path)
            return InferredValue.invalid()
        if values[2].knowledge == ValueKnowledge.CONSTANT and not is_json_serializable_value(jq_value):
            self.diagnostic(
                "ROS3004",
                _("The Fn::Jq value cannot be JSON-serialized."),
                _("A value that is not short-circuited crosses the JSON boundary before the jq backend."),
                _index(path, 2),
            )
            return InferredValue.invalid()
        if method == "First" or method is _CONSTRUCTOR_UNKNOWN:
            return InferredValue.dynamic(ANY_VALUE)
        return InferredValue.dynamic(union_of(list_of(ANY_VALUE), NULL))

    def _fn_Length(self, args: Any, children: Any, path: RosPath, *rest: Any) -> InferredValue:
        value = self._argument_value(children)
        if compatibility(value.type, union_of(STRING, list_of(), map_of(), NULL)) == Compatibility.DEFINITE_MISMATCH:
            self._type_error("Fn::Length", 0, "String | List | Map | Null", value, path)
            return InferredValue.invalid()
        if value.knowledge == ValueKnowledge.CONSTANT:
            return InferredValue.constant(0 if value.value is None else len(value.value), ros_type=INTEGER)
        return InferredValue.dynamic(INTEGER)

    def _fn_Index(self, args: Any, children: Any, path: RosPath, *rest: Any) -> InferredValue:
        values = self._indexable_args("Fn::Index", args, children, path, 2)
        if values is None:
            return InferredValue.invalid()
        collection_type = union_of(list_of(), NULL)
        if compatibility(values[1].type, collection_type) == Compatibility.DEFINITE_MISMATCH:
            self._type_error("Fn::Index", 1, "List | Null", values[1], path)
            return InferredValue.invalid()
        if values[1].knowledge == ValueKnowledge.CONSTANT:
            collection = values[1].value
            if not collection:
                return InferredValue.constant(None)
            if values[0].knowledge != ValueKnowledge.CONSTANT:
                return InferredValue.dynamic(union_of(INTEGER, NULL))
            try:
                return InferredValue.constant(collection.index(values[0].value), ros_type=INTEGER)
            except ValueError:
                return InferredValue.constant(None)
        return InferredValue.dynamic(union_of(INTEGER, NULL))

    def _fn_Any(self, args: Any, children: Any, path: RosPath, *rest: Any) -> InferredValue:
        value = self._argument_value(children)
        if value.knowledge == ValueKnowledge.CONSTANT and value.value is None:
            return InferredValue.constant(None)
        if not _is_list(value):
            self._type_error("Fn::Any", 0, "List | Null", value, path)
            return InferredValue.invalid()
        return (
            InferredValue.constant(any(value.value), ros_type=BOOLEAN)
            if value.knowledge == ValueKnowledge.CONSTANT
            else InferredValue.dynamic(union_of(BOOLEAN, NULL))
        )

    def _fn_Contains(self, args: Any, children: Any, path: RosPath, *rest: Any) -> InferredValue:
        values = self._list_args("Fn::Contains", args, children, path, (2,))
        if values is None:
            return InferredValue.invalid()
        if not _is_list(values[0]):
            self._type_error("Fn::Contains", 0, "List", values[0], path)
            return InferredValue.invalid()
        if not self._validate_hashable_members("Fn::Contains", 0, values[0], path, args[0]):
            return InferredValue.invalid()
        if all(value.knowledge == ValueKnowledge.CONSTANT for value in values):
            return InferredValue.constant(values[1].value in values[0].value, ros_type=BOOLEAN)
        return InferredValue.dynamic(BOOLEAN)

    def _fn_EachMemberIn(self, args: Any, children: Any, path: RosPath, *rest: Any) -> InferredValue:
        values = self._list_args("Fn::EachMemberIn", args, children, path, (2,))
        if values is None:
            return InferredValue.invalid()
        invalid = False
        for index, value in enumerate(values):
            if not _is_list(value):
                self._type_error("Fn::EachMemberIn", index, "List", value, path)
                invalid = True
                continue
            if value.knowledge == ValueKnowledge.CONSTANT and len(value.value) > 10_000:
                self.diagnostic(
                    "ROS3003",
                    _("The Fn::EachMemberIn List exceeds 10,000 items."),
                    _("The runtime limits each collection to 10,000 items."),
                    _index(path, index),
                )
                invalid = True
            if not self._validate_hashable_members("Fn::EachMemberIn", index, value, path, args[index]):
                invalid = True
        if invalid:
            return InferredValue.invalid()
        if all(value.knowledge == ValueKnowledge.CONSTANT for value in values):
            return InferredValue.constant(set(values[0].value).issubset(set(values[1].value)), ros_type=BOOLEAN)
        return InferredValue.dynamic(BOOLEAN)

    def _fn_Add(self, args: Any, children: Any, path: RosPath, *rest: Any) -> InferredValue:
        if args is None:
            return InferredValue.constant(None)
        if isinstance(children, InferredValue):
            if children.knowledge == ValueKnowledge.CONSTANT and children.value is None:
                return InferredValue.constant(None)
            if compatibility(children.type, list_of()) == Compatibility.DEFINITE_MISMATCH:
                self._type_error("Fn::Add", 0, "Function returning List/Tuple", children, path)
                return InferredValue.invalid()
            if children.knowledge != ValueKnowledge.CONSTANT:
                item_type = _list_item_type(children.type)
                numeric = union_of(NUMBER, BOOLEAN)
                if item_type is not None and all(
                    compatibility(item_type, candidate) == Compatibility.DEFINITE_MISMATCH
                    for candidate in (numeric, list_of(), map_of())
                ):
                    self._type_error("Fn::Add", 0, "homogeneous Number/List/Map arguments", children, path)
                    return InferredValue.invalid()
                return InferredValue.dynamic(union_of(NUMBER, list_of(), map_of(), NULL))
            resolved_args = children.value
            if not isinstance(resolved_args, list):
                return self._shape_error("Fn::Add", path, "resolved List/Tuple", resolved_args)
            values = [InferredValue.constant(value) for value in resolved_args]
        elif isinstance(children, list):
            values = cast(list[InferredValue], children)
        else:
            return self._shape_error("Fn::Add", path, "List/Tuple with at least two arguments", args)
        if len(values) < 2:
            return self._shape_error("Fn::Add", path, "at least two arguments", args)
        if any(value.knowledge == ValueKnowledge.CONSTANT and value.value is None for value in values):
            return InferredValue.constant(None)
        numeric_type = union_of(NUMBER, BOOLEAN)
        numeric = all(compatibility(value.type, numeric_type) != Compatibility.DEFINITE_MISMATCH for value in values)
        lists = all(compatibility(value.type, list_of()) != Compatibility.DEFINITE_MISMATCH for value in values)
        maps = all(compatibility(value.type, map_of()) != Compatibility.DEFINITE_MISMATCH for value in values)
        branches = (numeric, lists, maps)
        if not any(branches):
            self.diagnostic(
                "ROS3002",
                _("All Fn::Add arguments must be Numbers, Lists, or Maps of the same category."),
                _("The three operation branches cannot be mixed."),
                path,
            )
            return InferredValue.invalid()
        if all(value.knowledge == ValueKnowledge.CONSTANT for value in values):
            raw_values = [value.value for value in values]
            if all(isinstance(value, (int, float)) for value in raw_values):
                return InferredValue.constant(sum(raw_values))
            if all(isinstance(value, list) for value in raw_values):
                return InferredValue.constant([item for value in raw_values for item in value])
            if all(isinstance(value, Mapping) for value in raw_values):
                result: dict[Any, Any] = {}
                for value in raw_values:
                    result.update(value)
                return InferredValue.constant(result)
        result_types = [result for enabled, result in zip(branches, (NUMBER, list_of(), map_of())) if enabled]
        return InferredValue.dynamic(union_of(*result_types))

    def _fn_Avg(self, args: Any, children: Any, path: RosPath, *rest: Any) -> InferredValue:
        unpacked = self._iterable_args("Fn::Avg", args, children, path, (2,), reject_string=True, reject_mapping=True)
        if unpacked is None:
            return InferredValue.invalid()
        _raw_args, values = unpacked
        ndigits, numbers = values
        self._warn_boolean_as_integer("Fn::Avg", 0, ndigits, path)
        if compatibility(ndigits.type, union_of(INTEGER, BOOLEAN)) == Compatibility.DEFINITE_MISMATCH:
            self._type_error("Fn::Avg", 0, "IntegerLike", ndigits, path)
            return InferredValue.invalid()
        if compatibility(numbers.type, list_of()) == Compatibility.DEFINITE_MISMATCH:
            self._type_error("Fn::Avg", 1, "non-empty Sequence[number-like]", numbers, path)
            return InferredValue.invalid()
        if numbers.knowledge != ValueKnowledge.CONSTANT:
            item_type = _list_item_type(numbers.type)
            number_member_type = union_of(NUMBER, BOOLEAN, STRING, NULL)
            if (
                _raw_guarantees_non_empty_list(args[1])
                and item_type is not None
                and compatibility(item_type, number_member_type) == Compatibility.DEFINITE_MISMATCH
            ):
                self._type_error("Fn::Avg", 1, "non-empty Sequence[number-like]", numbers, path)
                return InferredValue.invalid()
        nonfinite = False
        can_fold_numbers = True
        converted_numbers: list[float] = []
        if numbers.knowledge == ValueKnowledge.CONSTANT:
            if isinstance(numbers.value, str) or not isinstance(numbers.value, Sequence) or not numbers.value:
                self._type_error("Fn::Avg", 1, "non-empty Sequence[number-like]", numbers, path)
                return InferredValue.invalid()
            for index, item in enumerate(numbers.value):
                if item is None:
                    converted_numbers.append(0.0)
                    continue
                outcome = float_coercion(item)
                if outcome == FloatCoercionOutcome.UNKNOWN:
                    self._report_unknown_type(_index(_index(path, 1), index), provenance="Fn::Avg-number")
                    can_fold_numbers = False
                    continue
                if outcome in {
                    FloatCoercionOutcome.INVALID_TYPE,
                    FloatCoercionOutcome.INVALID_VALUE,
                    FloatCoercionOutcome.OVERFLOW,
                }:
                    self.diagnostic(
                        "ROS3003",
                        _("Member {} of Fn::Avg cannot be converted to Float.").format(index + 1),
                        _("The conversion result is {}.").format(outcome.value),
                        _index(_index(path, 1), index),
                    )
                    return InferredValue.invalid()
                converted = float(cast(str | int | float | bool, item))
                converted_numbers.append(converted)
                nonfinite = nonfinite or not math.isfinite(converted)
        if can_fold_numbers and converted_numbers:
            try:
                average = sum(converted_numbers) / len(converted_numbers)
                rounded = round(average, ndigits.value) if ndigits.knowledge == ValueKnowledge.CONSTANT else average
            except (OverflowError, TypeError, ValueError, ZeroDivisionError):
                self.diagnostic(
                    "ROS3003",
                    _("The known Fn::Avg arguments fail during summation or rounding."),
                    _("The ROS runtime cannot evaluate this literal numeric combination."),
                    path,
                    stable_args=("Fn::Avg", "numeric-evaluation"),
                )
                return InferredValue.invalid()
            nonfinite = nonfinite or (isinstance(rounded, float) and not math.isfinite(rounded))
            if (
                nonfinite
                and ndigits.knowledge == ValueKnowledge.CONSTANT
                and isinstance(ndigits.value, int)
                and ndigits.value <= 0
            ):
                self.diagnostic(
                    "ROS3003",
                    _("The non-finite Fn::Avg result cannot be converted to Integer."),
                    _(
                        "When ndigits is at most 0, the runtime eventually calls int(); NaN/Infinity fails deterministically."
                    ),
                    path,
                    stable_args=("Fn::Avg", "nonfinite-int"),
                )
                return InferredValue.invalid()
            if nonfinite:
                self._warn_nonfinite_result("Fn::Avg", path)
            if ndigits.knowledge == ValueKnowledge.CONSTANT and isinstance(ndigits.value, int):
                return InferredValue.constant(
                    int(rounded) if ndigits.value <= 0 else rounded,
                    ros_type=INTEGER if ndigits.value <= 0 else NUMBER,
                )
        result_type = INTEGER if ndigits.knowledge == ValueKnowledge.CONSTANT and ndigits.value <= 0 else NUMBER
        return InferredValue.dynamic(result_type)

    @staticmethod
    def _calculate_ast_value(node: ast.AST) -> int | float:
        if isinstance(node, ast.Expression):
            return ExpressionAnalyzer._calculate_ast_value(node.body)
        if isinstance(node, ast.Constant) and isinstance(node.value, (int, float)) and not isinstance(node.value, bool):
            return node.value
        if isinstance(node, ast.UnaryOp):
            operand = ExpressionAnalyzer._calculate_ast_value(node.operand)
            if isinstance(node.op, ast.UAdd):
                return +operand
            if isinstance(node.op, ast.USub):
                return -operand
        if isinstance(node, ast.BinOp):
            left = ExpressionAnalyzer._calculate_ast_value(node.left)
            right = ExpressionAnalyzer._calculate_ast_value(node.right)
            if isinstance(node.op, ast.Add):
                return left + right
            if isinstance(node.op, ast.Sub):
                return left - right
            if isinstance(node.op, ast.Mult):
                return left * right
            if isinstance(node.op, ast.Div):
                return left / right
            if isinstance(node.op, ast.FloorDiv):
                return left // right
            if isinstance(node.op, ast.Mod):
                return left % right
        raise ValueError("unsupported calculate AST")

    @staticmethod
    def _calculate_fields(expression: str) -> tuple[int, ...]:
        fields: list[int] = []
        for _literal_text, field_name, format_spec, conversion in string.Formatter().parse(expression):
            if field_name is None:
                continue
            if not field_name.isdigit() or format_spec or conversion:
                raise ValueError("invalid calculate placeholder")
            fields.append(int(field_name))
        return tuple(fields)

    def _fn_Calculate(
        self,
        args: Any,
        children: Any,
        path: RosPath,
        _context: ExpressionContext,
        _depth: int,
        _eligible: bool,
        _consumer_resource_type: str | None,
        consumer_section: str | None,
        *rest: Any,
    ) -> InferredValue:
        values = self._list_args("Fn::Calculate", args, children, path, (2, 3))
        if values is None:
            return InferredValue.invalid()
        visible_expression = self._constructor_visible_value(args[0], consumer_section, values[0])
        expression = visible_expression if isinstance(visible_expression, str) else None
        if visible_expression is _CONSTRUCTOR_UNKNOWN:
            invalid_expression = compatibility(values[0].type, STRING) == Compatibility.DEFINITE_MISMATCH
        else:
            invalid_expression = (
                expression is None or "**" in expression or not _CALCULATE_EXPRESSION.fullmatch(expression)
            )
        if invalid_expression:
            self.diagnostic(
                "ROS3003",
                _("The Fn::Calculate expression is outside the safe allowlist."),
                _("The expression must be a raw String; ** and non-arithmetic characters are forbidden."),
                _index(path, 0),
            )
            return InferredValue.invalid()
        fields: tuple[int, ...] = ()
        if expression is not None:
            try:
                fields = self._calculate_fields(expression)
            except ValueError:
                self.diagnostic(
                    "ROS3003",
                    _("Fn::Calculate has an invalid placeholder format."),
                    _(
                        "Only positional placeholders such as {0} and {1}, without conversions or format specs, are allowed."
                    ),
                    _index(path, 0),
                    stable_args=("Fn::Calculate", "placeholder"),
                )
                return InferredValue.invalid()
        numbers = values[2] if len(values) == 3 else InferredValue.constant([])
        formatted_expression: str | None = None
        numbers_contain_unknown = False
        if compatibility(numbers.type, list_of()) == Compatibility.DEFINITE_MISMATCH:
            self._type_error("Fn::Calculate", 2, "Sequence[number-like]", numbers, path)
            return InferredValue.invalid()
        if numbers.knowledge != ValueKnowledge.CONSTANT:
            item_type = _list_item_type(numbers.type)
            number_member_type = union_of(NUMBER, BOOLEAN, STRING, NULL)
            if (
                len(args) == 3
                and _raw_guarantees_non_empty_list(args[2])
                and item_type is not None
                and compatibility(item_type, number_member_type) == Compatibility.DEFINITE_MISMATCH
            ):
                self._type_error("Fn::Calculate", 2, "Sequence[number-like]", numbers, path)
                return InferredValue.invalid()
        if numbers.knowledge == ValueKnowledge.CONSTANT:
            if not isinstance(numbers.value, Sequence) or isinstance(numbers.value, str):
                self._type_error("Fn::Calculate", 2, "Sequence[number-like]", numbers, path)
                return InferredValue.invalid()
            for index, item in enumerate(numbers.value):
                if item is None:
                    continue
                outcome = float_coercion(item)
                if outcome == FloatCoercionOutcome.UNKNOWN:
                    self._report_unknown_type(
                        _index(_index(path, 2), index),
                        provenance="Fn::Calculate-number",
                    )
                    numbers_contain_unknown = True
                    continue
                if outcome in {
                    FloatCoercionOutcome.INVALID_TYPE,
                    FloatCoercionOutcome.INVALID_VALUE,
                    FloatCoercionOutcome.OVERFLOW,
                }:
                    self.diagnostic(
                        "ROS3003",
                        _("Fn::Calculate number {} failed preflight validation.").format(index + 1),
                        _("float() produced {}; preflight runs before placeholder reachability.").format(outcome.value),
                        _index(_index(path, 2), index),
                    )
                    return InferredValue.invalid()
            if fields and max(fields) >= len(numbers.value):
                self.diagnostic(
                    "ROS3003",
                    _("Fn::Calculate references a number placeholder that does not exist."),
                    _("The maximum expression index is {}, but only {} numbers were provided.").format(
                        max(fields), len(numbers.value)
                    ),
                    _index(path, 0),
                    stable_args=("Fn::Calculate", "placeholder-index", str(max(fields)), str(len(numbers.value))),
                )
                return InferredValue.invalid()
            if not numbers_contain_unknown and expression is not None:
                try:
                    formatted_expression = expression.format(*numbers.value)
                except (IndexError, KeyError, TypeError, ValueError):
                    self.diagnostic(
                        "ROS3003",
                        _("The Fn::Calculate expression cannot be formatted with the raw numbers."),
                        _("The runtime calls expression.format(*numbers) before arithmetic evaluation."),
                        _index(path, 0),
                        stable_args=("Fn::Calculate", "format"),
                    )
                    return InferredValue.invalid()
        ndigits = values[1]
        self._warn_boolean_as_integer("Fn::Calculate", 1, ndigits, path)
        if compatibility(ndigits.type, union_of(INTEGER, BOOLEAN)) == Compatibility.DEFINITE_MISMATCH:
            self._type_error("Fn::Calculate", 1, "IntegerLike", ndigits, path)
            return InferredValue.invalid()
        result_type = (
            INTEGER
            if ndigits.knowledge == ValueKnowledge.CONSTANT and isinstance(ndigits.value, int) and ndigits.value <= 0
            else NUMBER
        )
        if formatted_expression is None or ndigits.knowledge != ValueKnowledge.CONSTANT:
            return InferredValue.dynamic(result_type)
        try:
            tree = ast.parse(formatted_expression, mode="eval")
            if any(not isinstance(node, _CALCULATE_AST_NODES) for node in ast.walk(tree)):
                raise ValueError("unsupported calculate token")
            result = self._calculate_ast_value(tree)
            rounded = round(result, ndigits.value)
            if ndigits.value <= 0:
                rounded = int(rounded)
        except (OverflowError, SyntaxError, TypeError, ValueError, ZeroDivisionError):
            self.diagnostic(
                "ROS3003",
                _("The known Fn::Calculate expression cannot be evaluated under the runtime arithmetic contract."),
                _(
                    "The formatted result contains an invalid token, division by zero, overflow, or a non-finite value that cannot be converted to Integer."
                ),
                path,
                stable_args=("Fn::Calculate", "arithmetic"),
            )
            return InferredValue.invalid()
        if isinstance(rounded, float) and not math.isfinite(rounded):
            self._warn_nonfinite_result("Fn::Calculate", path)
        return InferredValue.constant(rounded, ros_type=result_type)

    def _fn_Min(self, args: Any, children: Any, path: RosPath, *rest: Any) -> InferredValue:
        return self._min_max("Fn::Min", args, children, path)

    def _fn_Max(self, args: Any, children: Any, path: RosPath, *rest: Any) -> InferredValue:
        return self._min_max("Fn::Max", args, children, path)

    def _min_max(self, name: str, args: Any, children: Any, path: RosPath) -> InferredValue:
        if args is None:
            return InferredValue.constant(None)
        if isinstance(children, InferredValue):
            if children.knowledge == ValueKnowledge.CONSTANT and children.value is None:
                return InferredValue.constant(None)
            if compatibility(children.type, list_of()) == Compatibility.DEFINITE_MISMATCH:
                self._type_error(name, 0, "Function returning List/Tuple", children, path)
                return InferredValue.invalid()
            if children.knowledge != ValueKnowledge.CONSTANT:
                item_type = _list_item_type(children.type)
                if (
                    _raw_guarantees_non_empty_list(args)
                    and item_type is not None
                    and compatibility(item_type, union_of(NUMBER, BOOLEAN)) == Compatibility.DEFINITE_MISMATCH
                ):
                    self._type_error(name, 0, "List[NumberLike] | Null", children, path)
                    return InferredValue.invalid()
                return InferredValue.dynamic(union_of(NUMBER, BOOLEAN, NULL))
            if not isinstance(children.value, list):
                return self._shape_error(name, path, "resolved List/Tuple | Null", children.value)
            values = [InferredValue.constant(value) for value in children.value]
        elif isinstance(children, list):
            values = cast(list[InferredValue], children)
        else:
            return self._shape_error(name, path, "List/Tuple | Null", args)
        if not values:
            return InferredValue.constant(None)
        for index, value in enumerate(values):
            if value.knowledge == ValueKnowledge.CONSTANT and value.value is None:
                return InferredValue.constant(None)
            if compatibility(value.type, union_of(NUMBER, BOOLEAN)) == Compatibility.DEFINITE_MISMATCH:
                self._type_error(name, index, "NumberLike", value, path)
                return InferredValue.invalid()
        if all(value.knowledge == ValueKnowledge.CONSTANT for value in values):
            operator = min if name == "Fn::Min" else max
            return InferredValue.constant(operator(value.value for value in values))
        return InferredValue.dynamic(union_of(NUMBER, BOOLEAN, NULL))

    def _fn_Cidr(self, args: Any, children: Any, path: RosPath, *rest: Any) -> InferredValue:
        unpacked = self._iterable_args("Fn::Cidr", args, children, path, (3,))
        if unpacked is None:
            return InferredValue.invalid()
        _raw_args, values = unpacked
        self._warn_boolean_as_integer("Fn::Cidr", 1, values[1], path)
        self._warn_boolean_as_integer("Fn::Cidr", 2, values[2], path)
        invalid = False
        if compatibility(values[0].type, STRING) == Compatibility.DEFINITE_MISMATCH:
            self._type_error("Fn::Cidr", 0, "String", values[0], path)
            invalid = True
        for index in (1, 2):
            if compatibility(values[index].type, union_of(INTEGER, BOOLEAN)) == Compatibility.DEFINITE_MISMATCH:
                self._type_error("Fn::Cidr", index, "IntegerLike", values[index], path)
                invalid = True
        if invalid:
            return InferredValue.invalid()
        for index, lower, upper in ((1, 1, 256), (2, 1, 32)):
            value = values[index]
            if value.knowledge == ValueKnowledge.CONSTANT and not lower <= value.value <= upper:
                self.diagnostic(
                    "ROS3003",
                    _("Integer argument {} of Fn::Cidr is out of range.").format(index + 1),
                    _("The allowed range is {} to {}.").format(lower, upper),
                    _index(path, index),
                    stable_args=("Fn::Cidr", "range", str(index), str(lower), str(upper)),
                )
                return InferredValue.invalid()
        if values[0].knowledge == ValueKnowledge.CONSTANT:
            try:
                network = ipaddress.ip_network(values[0].value, strict=False)
                if network.version != 4:
                    raise ValueError
            except (TypeError, ValueError):
                self.diagnostic(
                    "ROS3003",
                    _("The literal Fn::Cidr IPv4 network is invalid."),
                    _("ipBlock must be a valid IPv4 CIDR."),
                    _index(path, 0),
                    stable_args=("Fn::Cidr", "ip-block"),
                )
                return InferredValue.invalid()
        if all(value.knowledge == ValueKnowledge.CONSTANT for value in values):
            ip_block, count, bits = (value.value for value in values)
            try:
                network = ipaddress.ip_network(ip_block, strict=False)
                if network.version != 4 or not 1 <= count <= 256 or not 1 <= bits <= 32:
                    raise ValueError
                new_prefix = 32 - bits
                if new_prefix < network.prefixlen or count > 2 ** (new_prefix - network.prefixlen):
                    raise ValueError
            except (TypeError, ValueError):
                self.diagnostic(
                    "ROS3003",
                    _("The literal Fn::Cidr network arguments are invalid."),
                    _("IPv4, count from 1 to 256, cidrBits from 1 to 32, and sufficient capacity are required."),
                    path,
                )
                return InferredValue.invalid()
        return InferredValue.dynamic(list_of(STRING))

    def _fn_Equals(self, args: Any, children: Any, path: RosPath, *rest: Any) -> InferredValue:
        values = self._list_args("Fn::Equals", args, children, path, (2,))
        if values is None:
            return InferredValue.invalid()
        if all(value.knowledge == ValueKnowledge.CONSTANT for value in values):
            return InferredValue.constant(values[0].value == values[1].value, ros_type=BOOLEAN)
        return InferredValue.dynamic(BOOLEAN)

    def _fn_Not(self, args: Any, children: Any, path: RosPath, *rest: Any) -> InferredValue:
        if args is None or (isinstance(args, list) and len(args) != 1):
            return self._shape_error("Fn::Not", path, "condition or one-item List", args)
        raw = args[0] if isinstance(args, list) else args
        value = children[0] if isinstance(children, list) else children
        condition_name = self._raw_condition_name(raw)
        if condition_name is not None:
            value = self._condition_reference(condition_name, _index(path, 0) if isinstance(args, list) else path)
        elif isinstance(raw, str):
            value = self._condition_reference(raw, _index(path, 0) if isinstance(args, list) else path)
        elif isinstance(args, list) and value.knowledge == ValueKnowledge.CONSTANT:
            if isinstance(value.value, str):
                value = self._condition_reference(value.value, _index(path, 0))
            elif value.value is None:
                self._type_error("Fn::Not", 0, "non-Null condition", value, path)
                return InferredValue.invalid()
            elif isinstance(value.value, bool):
                return InferredValue.constant(False, ros_type=BOOLEAN)
        elif isinstance(args, list) and compatibility(value.type, BOOLEAN) == Compatibility.DEFINITE_MATCH:
            return InferredValue.constant(False, ros_type=BOOLEAN)
        elif compatibility(value.type, BOOLEAN) == Compatibility.DEFINITE_MISMATCH:
            self._type_error("Fn::Not", 0, "Boolean or Condition name", value, path)
            return InferredValue.invalid()
        return (
            InferredValue.constant(not bool(value.value), ros_type=BOOLEAN)
            if value.knowledge == ValueKnowledge.CONSTANT
            else InferredValue.dynamic(BOOLEAN)
        )

    def _fn_And(self, args: Any, children: Any, path: RosPath, *rest: Any) -> InferredValue:
        return self._and_or("Fn::And", args, children, path, all)

    def _fn_Or(self, args: Any, children: Any, path: RosPath, *rest: Any) -> InferredValue:
        return self._and_or("Fn::Or", args, children, path, any)

    def _and_or(self, name: str, args: Any, children: Any, path: RosPath, operator: Any) -> InferredValue:
        if not isinstance(args, Sequence) or isinstance(args, str) or len(args) < 2 or not isinstance(children, list):
            return self._shape_error(name, path, "non-String Sequence with at least two conditions", args)
        values: list[InferredValue] = []
        invalid = False
        typed_children = cast(list[InferredValue], children)
        for index, (raw, value) in enumerate(zip(args, typed_children)):
            condition_name = self._raw_condition_name(raw)
            if condition_name is not None:
                value = self._condition_reference(condition_name, _index(path, index))
            elif isinstance(raw, str):
                value = self._condition_reference(raw, _index(path, index))
            elif compatibility(value.type, BOOLEAN) == Compatibility.DEFINITE_MISMATCH:
                self._type_error(name, index, "Boolean or Condition name", value, path)
                invalid = True
            values.append(value)
        if invalid or all(value.poisoned for value in values):
            return InferredValue.invalid()
        if all(value.knowledge == ValueKnowledge.CONSTANT for value in values):
            return InferredValue.constant(operator(bool(value.value) for value in values), ros_type=BOOLEAN)
        return InferredValue.dynamic(BOOLEAN)

    @staticmethod
    def _raw_condition_name(value: Any) -> str | None:
        if isinstance(value, Mapping) and len(value) == 1 and isinstance(value.get("Condition"), str):
            return value["Condition"]
        return None

    def _fn_TransformNamespace(self, args: Any, children: Any, path: RosPath, *rest: Any) -> InferredValue:
        values = self._list_args("Fn::TransformNamespace", args, children, path, (3,))
        if values is None:
            return InferredValue.invalid()
        invalid = False
        for index, value in enumerate(values[:2]):
            if compatibility(value.type, STRING) == Compatibility.DEFINITE_MISMATCH:
                self._type_error("Fn::TransformNamespace", index, "String", value, path)
                invalid = True
        if invalid:
            return InferredValue.invalid()
        transform_type, namespace, value = values
        if transform_type.knowledge != ValueKnowledge.CONSTANT:
            return InferredValue.dynamic(ANY_VALUE)
        if transform_type.value not in {"Condition", "DependsOn"}:
            self.diagnostic(
                "ROS3003",
                _("Fn::TransformNamespace transform_type is invalid."),
                _("Only Condition or DependsOn is supported."),
                _index(path, 0),
                stable_args=("Fn::TransformNamespace", str(transform_type.value)),
            )
            return InferredValue.invalid()
        if transform_type.value == "Condition":
            if value.knowledge != ValueKnowledge.CONSTANT or namespace.knowledge != ValueKnowledge.CONSTANT:
                return InferredValue.dynamic(ANY_VALUE)
            raw = value.value
            if isinstance(raw, bool):
                return InferredValue.constant(raw, ros_type=BOOLEAN)
            if isinstance(raw, list):
                if not raw:
                    self.diagnostic(
                        "ROS3003",
                        _("The Fn::TransformNamespace Condition List cannot be empty."),
                        _("The runtime reads the first member, so an empty List raises IndexError."),
                        _index(path, 2),
                    )
                    return InferredValue.invalid()
                first = raw[0]
                condition_key = first.get("Condition") if isinstance(first, Mapping) else first
            elif isinstance(raw, Mapping):
                condition_key = raw.get("Condition")
            else:
                condition_key = raw
            if isinstance(condition_key, str):
                return InferredValue.constant(str(namespace.value) + condition_key, ros_type=STRING)
            return InferredValue.constant(condition_key)

        if value.knowledge == ValueKnowledge.CONSTANT:
            raw = value.value
            if not isinstance(raw, Mapping) or not isinstance(raw.get("Lookup"), Mapping):
                self._type_error("Fn::TransformNamespace", 2, "Map with Lookup Map", value, path)
                return InferredValue.invalid()
            lookup = raw["Lookup"]
            if any(
                not isinstance(items, list) or any(not isinstance(item, str) for item in items)
                for items in lookup.values()
            ):
                self.diagnostic(
                    "ROS3003",
                    _("Fn::TransformNamespace.DependsOn has an invalid Lookup value."),
                    _("Every Lookup value must be List[String]."),
                    _key(_index(path, 2), "Lookup"),
                )
                return InferredValue.invalid()
        return InferredValue.dynamic(list_of(ANY_VALUE))
