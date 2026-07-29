"""Contract-driven validation for ROS AssociationProperty metadata."""

from __future__ import annotations

import json
import math
import re
from collections.abc import Mapping
from dataclasses import dataclass
from decimal import Decimal
from enum import Enum
from typing import Any, cast

from iac_code.i18n import _
from iac_code.tools.cloud.aliyun.ros_validation.association_property_specs import (
    ASSOCIATION_PROPERTY_SPECS,
    AssociationPropertySpecRegistry,
    load_association_property_specs,
)
from iac_code.tools.cloud.aliyun.ros_validation.facts import FactBuildResult, RulePhase
from iac_code.tools.cloud.aliyun.ros_validation.model import (
    Category,
    Diagnostic,
    MappingKeySegment,
    RelatedLocation,
    RosPath,
    SequenceIndexSegment,
    Severity,
    make_diagnostic,
    mapping_segment,
)
from iac_code.tools.cloud.aliyun.ros_validation.symbols import TemplateSymbols
from iac_code.tools.cloud.aliyun.ros_validation.template_kind import is_terraform_template

PARSED_TEMPLATE = "parsed-template"
TEMPLATE_SYMBOLS = "template-symbols"
MAX_METADATA_DEPTH = 16
_REFERENCE = re.compile(r"^\s*\$\{([^{}\r\n]+)}\s*$")
_FRONTEND_REFERENCE = re.compile(r"^\s*\$\{(.*)}\s*$")
_ESCAPED_LITERAL = re.compile(r"^\s*\$\{!(.*)}\s*$")
_ENV_REFERENCE = re.compile(r"^\{\{(.*)\}\}$")
_LODASH_REFERENCE = re.compile(r"\$\{([^{}]+)}")
_LODASH_PATH_TOKEN = re.compile(
    r"""[^.[\]]+|\[(?:([^"'][^[]*)|(["'])((?:(?!\2)[^\\]|\\.)*?)\2)\]|(?=(?:\.|\[\])(?:\.|\[\]|$))"""
)
_JS_DECIMAL_NUMBER = re.compile(r"^[+-]?(?:(?:[0-9]+(?:\.[0-9]*)?|\.[0-9]+)(?:[eE][+-]?[0-9]+)?|Infinity)$")
_JS_NON_DECIMAL_NUMBER = re.compile(r"^0(?:[xX][0-9a-fA-F]+|[oO][0-7]+|[bB][01]+)$")
_LIST_COMPONENT = "List"
_INPUT_COMPONENT = "Input"


def _lodash_path_segments(value: str) -> tuple[str, ...]:
    """Mirror the target form's field-path tokenizer."""

    result: list[str] = []
    if value.startswith("."):
        result.append("")
    for match in _LODASH_PATH_TOKEN.finditer(value):
        expression, quote, quoted = match.groups()
        if quote is not None:
            result.append(re.sub(r"\\(\\)?", lambda item: item.group(1) or "", quoted))
        elif expression is not None:
            result.append(expression.strip())
        else:
            result.append(match.group(0))
    return tuple(result)


def _is_lodash_field_path(value: str) -> bool:
    """Recognize paths routed to ``lodash.get`` that we cannot resolve statically.

    The target form does not validate the lodash suffix. Once a whole metadata reference contains
    ``.``, ``[`` or ``]``, it extracts the root up to the first ``.``/``[`` and passes
    the remainder to lodash.  Keeping a stricter hand-written lodash grammar here would
    therefore turn accepted (albeit unusual) paths into local blocking errors.
    """

    return re.search(r"[.\[\]]", value) is not None


def _js_property_key(value: Any) -> str | None:
    """Coerce YAML scalar mapping keys the way a JavaScript object does."""

    if isinstance(value, str):
        return value
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "true" if value else "false"
    if not isinstance(value, (int, float)):
        return None
    try:
        number = float(value)
    except OverflowError:
        number = math.copysign(math.inf, value)
    if math.isnan(number):
        return "NaN"
    if math.isinf(number):
        return "Infinity" if number > 0 else "-Infinity"
    if number == 0:
        return "0"
    magnitude = abs(number)
    representation = repr(number)
    if 1e-6 <= magnitude < 1e21:
        fixed = format(Decimal(representation), "f")
        return fixed.rstrip("0").rstrip(".") if "." in fixed else fixed
    mantissa, exponent = representation.lower().split("e")
    mantissa = mantissa.rstrip("0").rstrip(".")
    exponent_value = int(exponent)
    exponent_sign = "+" if exponent_value >= 0 else ""
    return f"{mantissa}e{exponent_sign}{exponent_value}"


def _js_mapping_property(mapping: Mapping[Any, Any], name: str) -> tuple[bool, Any]:
    """Read the last YAML key that becomes ``name`` on a JavaScript object."""

    found = False
    result: Any = None
    for key, value in mapping.items():
        if _js_property_key(key) == name:
            found = True
            result = value
    return found, result


def _js_array_index_key(name: str) -> int | None:
    """Return the ECMAScript array-index value used by ``Object.keys`` ordering."""

    if name == "0":
        return 0
    if not name or name[0] == "0" or not name.isascii() or not name.isdecimal():
        return None
    value = int(name)
    return value if value < 2**32 - 1 else None


def _js_mapping_properties(mapping: Mapping[Any, Any]) -> tuple[dict[str, Any], tuple[str, ...]]:
    """Build JavaScript-visible properties and their ``Object.keys`` order."""

    properties: dict[str, Any] = {}
    for raw_key, value in mapping.items():
        key = _js_property_key(raw_key)
        if key is not None:
            # Reassigning an existing JavaScript property changes its value but not
            # the insertion order used for non-array-index keys.
            properties[key] = value
    array_indexes = sorted((index, key) for key in properties if (index := _js_array_index_key(key)) is not None)
    non_indexes = tuple(key for key in properties if _js_array_index_key(key) is None)
    order = tuple(key for _, key in array_indexes) + non_indexes
    return properties, order


def _same_definition_value(left: Any, right: Any) -> bool:
    """Compare parsed values using the target form's JavaScript object behavior."""

    if isinstance(left, Mapping) and isinstance(right, Mapping):
        left_properties, left_order = _js_mapping_properties(left)
        right_properties, right_order = _js_mapping_properties(right)
        return left_order == right_order and all(
            _same_definition_value(left_properties[key], right_properties[key]) for key in left_order
        )
    if isinstance(left, (list, tuple)) and isinstance(right, (list, tuple)):
        return len(left) == len(right) and all(
            _same_definition_value(left_item, right_item) for left_item, right_item in zip(left, right, strict=True)
        )
    if isinstance(left, bool) or isinstance(right, bool):
        return isinstance(left, bool) and isinstance(right, bool) and left is right
    if isinstance(left, (int, float)) and isinstance(right, (int, float)):
        return left == right
    return type(left) is type(right) and left == right


def _js_utf16_units(value: str) -> tuple[str, ...]:
    encoded = value.encode("utf-16-le", errors="surrogatepass")
    return tuple(
        encoded[index : index + 2].decode("utf-16-le", errors="surrogatepass") for index in range(0, len(encoded), 2)
    )


class _RuntimeValueSentinel:
    def __init__(self, name: str) -> None:
        self.name = name

    def __repr__(self) -> str:
        return self.name


UNKNOWN_RUNTIME_VALUE = _RuntimeValueSentinel("UNKNOWN_RUNTIME_VALUE")
ABSENT_RUNTIME_VALUE = _RuntimeValueSentinel("ABSENT_RUNTIME_VALUE")


class ConsumerReachability(str, Enum):
    REACHED = "reached"
    NOT_REACHED = "not-reached"
    UNKNOWN = "unknown"


@dataclass(frozen=True)
class AutoCompleteConsumerReachability:
    base_default: str
    effective_default: str
    current_value: str
    raw_initializer: ConsumerReachability
    component_effect: ConsumerReachability


def js_truthy(value: Any) -> bool:
    """Apply JavaScript truthiness to parsed JSON/YAML values."""

    if value is None or value is False:
        return False
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return value != 0 and not (isinstance(value, float) and math.isnan(value))
    if isinstance(value, str):
        return value != ""
    return True  # JavaScript arrays and objects are truthy, including empty ones.


def frontend_string_to_boolean(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    return isinstance(value, str) and value.lower() == "true"


def _frontend_boolean_value(value: Any) -> Any:
    """Return the target form's string-to-boolean value, including JavaScript undefined."""

    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        normalized = value.lower()
        if normalized == "true":
            return True
        if normalized == "false":
            return False
    return ABSENT_RUNTIME_VALUE


def _js_number(value: Any) -> float:
    """Apply the JavaScript Number/unary-plus conversion used by the target ROS form."""

    if value is None:
        return 0.0
    if isinstance(value, bool):
        return 1.0 if value else 0.0
    if isinstance(value, (int, float)):
        return float(value)
    if not isinstance(value, str):
        return math.nan
    stripped = value.strip()
    if not stripped:
        return 0.0
    try:
        if _JS_NON_DECIMAL_NUMBER.fullmatch(stripped):
            return float(int(stripped, 0))
        if _JS_DECIMAL_NUMBER.fullmatch(stripped):
            return float(stripped)
    except (OverflowError, ValueError):
        return math.nan
    return math.nan


def _strict_json_parse(value: Any) -> Any:
    """Match JSON.parse, including rejection of NaN/Infinity extensions."""

    raw = str(value).lower() if isinstance(value, bool) else str(value)

    def reject_constant(constant: str) -> Any:
        raise ValueError("invalid JSON constant {}".format(constant))

    return json.loads(raw, parse_constant=reject_constant)


def normalize_association_property(value: str) -> str:
    return "ALIYUN::{}".format(value[len("APSARA::") :]) if value.startswith("APSARA::") else value


def is_frontend_ref_value(value: Any) -> bool:
    if isinstance(value, Mapping):
        return len(value) == 1 and next(iter(value), None) in {"Ref", "Fn::GetAtt"}
    return bool(value) and isinstance(value, list) and all(is_frontend_ref_value(item) for item in value)


def _runtime_value_state(value: Any) -> str:
    if value is UNKNOWN_RUNTIME_VALUE:
        return "unknown"
    if value is ABSENT_RUNTIME_VALUE:
        return "undefined"
    return "truthy" if js_truthy(value) else "falsy"


def _normalize_template_default(parameter: Mapping[Any, Any], base_default: Any) -> Any:
    if base_default is UNKNOWN_RUNTIME_VALUE or is_frontend_ref_value(base_default):
        return base_default
    default_value = base_default
    parameter_type = parameter.get("Type")
    required = parameter.get("Required")
    required_boolean_default = required if required is not None else default_value is ABSENT_RUNTIME_VALUE
    if parameter_type == "Boolean" and required_boolean_default:
        default_value = False
    # A loose comparison with undefined is false for both null and undefined.
    enters_normalization = default_value is not ABSENT_RUNTIME_VALUE and default_value is not None
    if enters_normalization and not isinstance(default_value, (Mapping, list)):
        if parameter_type == "Boolean":
            default_value = _frontend_boolean_value(default_value)
        elif parameter_type == "Json":
            try:
                # JSON.parse coerces primitive inputs to strings before parsing.
                default_value = _strict_json_parse(default_value)
            except (TypeError, ValueError):
                default_value = default_value
        elif parameter_type in {"Number", "Integer", "NumberPicker"}:
            default_value = _js_number(default_value)
        elif parameter_type == "CommaDelimitedList" and isinstance(default_value, str):
            default_value = default_value.split(",")
        elif (
            frontend_string_to_boolean(parameter.get("NoEcho"))
            or parameter.get("AssociationProperty") == "ALIYUN::ECS::Instance::Password"
        ):
            default_value = ABSENT_RUNTIME_VALUE
    return default_value


def _auto_complete_character_class_values(
    specs: AssociationPropertySpecRegistry | None = None,
) -> frozenset[str]:
    registry = specs or load_association_property_specs()
    component = registry.component("AutoCompleteInput")
    schema: Any = component.metadata if component is not None else {}
    for key in ("properties", "CharacterClasses", "items", "properties", "Class", "enum"):
        schema = schema.get(key) if isinstance(schema, Mapping) else None
    if not isinstance(schema, (list, tuple)) or not all(isinstance(item, str) for item in schema):
        raise RuntimeError("AutoCompleteInput CharacterClasses enum is missing from the local contract")
    return frozenset(schema)


def _js_integer(value: Any) -> int:
    number = _js_number(value)
    if not math.isfinite(number):
        return 0
    return math.trunc(number)


def _raw_auto_complete_value(
    metadata: Mapping[Any, Any],
    valid_classes: frozenset[str] | None = None,
) -> Any:
    prefix = metadata.get("Prefix")
    suffix = metadata.get("Suffix")
    if js_truthy(prefix) or js_truthy(suffix):
        return "generated-with-affix"
    classes = metadata.get("CharacterClasses")
    length = metadata.get("Length")
    if isinstance(classes, list) and classes:
        effective_length = _ValidationState._js_array_length(length if "Length" in metadata else 8)
        if effective_length <= 0:
            return ""
        class_values = valid_classes or _auto_complete_character_class_values()
        ordinary_pool = False
        last_special: Mapping[Any, Any] | None = None
        for item in classes:
            if not isinstance(item, Mapping):
                continue
            class_name = item.get("Class")
            if class_name in class_values - {"specialCharacter"}:
                ordinary_pool = True
                if _fill_count(item.get("Min", ABSENT_RUNTIME_VALUE), effective_length) > 0:
                    return "generated"
            elif class_name == "specialCharacter" and js_truthy(item.get("SpecialCharacters")):
                last_special = item
                excluded = set()
                if item.get("Start") is False:
                    excluded.add(0)
                if item.get("End") is False:
                    excluded.add(effective_length - 1)
                available = max(effective_length - len(excluded), 0)
                if _fill_count(item.get("Min", ABSENT_RUNTIME_VALUE), available) > 0:
                    return "generated"
        if ordinary_pool:
            return "generated"
        if last_special is not None:
            # The generator narrows its mutable pool at the first position and does
            # not restore it.  With a pure-special pool, Start=false therefore
            # empties every remaining position, while End=false only affects the
            # last position.
            if last_special.get("Start") is False:
                return ""
            if effective_length == 1 and last_special.get("End") is False:
                return ""
            return "generated"
        return ""
    # The generated identifier is truthy; a truthy Length then slices it using JavaScript numeric conversion.
    if js_truthy(length):
        slice_end = _ValidationState._js_slice_end(length)
        if slice_end == 0:
            return ""
        # The generated identifier always contains at least three segments.
        # More-negative endpoints depend on its runtime-generated length.
        if slice_end <= -3:
            return UNKNOWN_RUNTIME_VALUE
    return "generated"


def _fill_count(count: Any, available: int) -> int:
    """Return how many positions the target generator consumes for a fixed pool size."""

    if count is ABSENT_RUNTIME_VALUE or count is UNKNOWN_RUNTIME_VALUE or available <= 0 or not js_truthy(count):
        return 0
    if isinstance(count, str):
        string_number = _js_number(count)
        # A truthy non-number String enters once; count-- then becomes NaN.
        if math.isnan(string_number):
            return 1
        if string_number > 0 and float(string_number).is_integer():
            return min(int(string_number), available)
        return available
    if isinstance(count, (int, float)) and not isinstance(count, bool):
        numeric_count = float(count)
        if math.isnan(numeric_count):
            return 0
        if numeric_count > 0 and math.isfinite(numeric_count) and numeric_count.is_integer():
            return min(int(numeric_count), available)
        return available
    # Truthy booleans/objects enter once and become 0/NaN after count--.
    return 1


def evaluate_auto_complete_reachability(
    parameter: Mapping[Any, Any],
    *,
    host_initial_value: Any = UNKNOWN_RUNTIME_VALUE,
    existing_form_value: Any = UNKNOWN_RUNTIME_VALUE,
    static_parameter_value: Any = UNKNOWN_RUNTIME_VALUE,
    initial_parameter_value: Any = UNKNOWN_RUNTIME_VALUE,
    value_effect: Any = UNKNOWN_RUNTIME_VALUE,
    dynamic_value_effect: Any = UNKNOWN_RUNTIME_VALUE,
) -> AutoCompleteConsumerReachability:
    """Model the target form's initializer/host-merge/effect order without exposing values."""

    if host_initial_value is UNKNOWN_RUNTIME_VALUE:
        base_default = UNKNOWN_RUNTIME_VALUE
    elif host_initial_value is ABSENT_RUNTIME_VALUE or host_initial_value is None:
        base_default = parameter.get("Default", ABSENT_RUNTIME_VALUE)
    else:
        base_default = host_initial_value
    effective_default = _normalize_template_default(parameter, base_default)
    if effective_default is UNKNOWN_RUNTIME_VALUE:
        raw_reachability = ConsumerReachability.UNKNOWN
        initial_value = UNKNOWN_RUNTIME_VALUE
    elif is_frontend_ref_value(effective_default):
        raw_reachability = ConsumerReachability.NOT_REACHED
        initial_value = effective_default
    elif effective_default is ABSENT_RUNTIME_VALUE:
        raw_reachability = ConsumerReachability.REACHED
        metadata = parameter.get("AssociationPropertyMetadata")
        initial_value = _raw_auto_complete_value(metadata if isinstance(metadata, Mapping) else {})
    else:
        raw_reachability = ConsumerReachability.NOT_REACHED
        initial_value = effective_default

    # Form state merges the existing, generated, static, then caller-provided initial values.
    current_value = existing_form_value
    current_value = initial_value
    if static_parameter_value is not ABSENT_RUNTIME_VALUE:
        current_value = static_parameter_value
    if initial_parameter_value is not ABSENT_RUNTIME_VALUE:
        current_value = initial_parameter_value

    metadata = parameter.get("AssociationPropertyMetadata")
    if isinstance(metadata, Mapping):
        if "DynamicValue" in metadata:
            if dynamic_value_effect is UNKNOWN_RUNTIME_VALUE:
                current_value = UNKNOWN_RUNTIME_VALUE
            elif dynamic_value_effect is not ABSENT_RUNTIME_VALUE:
                current_value = dynamic_value_effect
        if "Value" in metadata:
            if value_effect is UNKNOWN_RUNTIME_VALUE:
                current_value = UNKNOWN_RUNTIME_VALUE
            elif value_effect is not ABSENT_RUNTIME_VALUE:
                current_value = value_effect

    if current_value is UNKNOWN_RUNTIME_VALUE:
        component_reachability = ConsumerReachability.UNKNOWN
    elif js_truthy(current_value):
        component_reachability = ConsumerReachability.NOT_REACHED
    else:
        component_reachability = ConsumerReachability.REACHED
    return AutoCompleteConsumerReachability(
        base_default=_runtime_value_state(base_default),
        effective_default=_runtime_value_state(effective_default),
        current_value=_runtime_value_state(current_value),
        raw_initializer=raw_reachability,
        component_effect=component_reachability,
    )


@dataclass(frozen=True)
class AssociationPropertySpecsProvider:
    provider_id: str = "builtin.association-property-specs"
    phase: RulePhase = RulePhase.STRUCTURE
    requires: frozenset[str] = frozenset()
    optional_requires: frozenset[str] = frozenset()
    provides: frozenset[str] = frozenset({ASSOCIATION_PROPERTY_SPECS})

    def build(self, context: Any) -> FactBuildResult:
        del context
        return FactBuildResult(provided={ASSOCIATION_PROPERTY_SPECS: load_association_property_specs()})


@dataclass(frozen=True)
class _ComponentResolution:
    initial_component: str
    possible_components: tuple[str, ...]
    deterministic: bool
    association_supported: bool
    definite_list_bypass: bool


@dataclass(frozen=True)
class _ResolvedReference:
    declaration: Mapping[Any, Any] | None = None
    declaration_path: RosPath | None = None
    projected_array: bool = False
    error: str | None = None
    limitation: bool = False


@dataclass(frozen=True)
class AssociationPropertyRule:
    rule_id: str = "builtin.association-property"
    phase: RulePhase = RulePhase.SYMBOLS
    requires: frozenset[str] = frozenset({PARSED_TEMPLATE, TEMPLATE_SYMBOLS, ASSOCIATION_PROPERTY_SPECS})
    optional_requires: frozenset[str] = frozenset()

    def check(self, context: Any) -> tuple[Diagnostic, ...]:
        parsed = context.fact_store.get_required(PARSED_TEMPLATE)
        symbols = context.fact_store.get_required(TEMPLATE_SYMBOLS)
        specs = context.fact_store.get_required(ASSOCIATION_PROPERTY_SPECS)
        if not isinstance(parsed.data, Mapping):
            return ()
        state = _ValidationState(parsed.data, parsed.source_map, symbols, specs)
        if is_terraform_template(parsed.data) and "Workspace" in parsed.data:
            state._emit(
                "ROS5305",
                Severity.LIMITATION,
                _("Embedded Terraform variable metadata is not analyzed locally."),
                _(
                    "Top-level Parameters are validated; metadata inside Workspace files "
                    "remains outside local analysis."
                ),
                (mapping_segment("Workspace"),),
                stable_args=("terraform-workspace-metadata",),
            )
        return state.run()


class _ValidationState:
    def __init__(
        self,
        template: Mapping[Any, Any],
        source_map: Any,
        symbols: TemplateSymbols,
        specs: AssociationPropertySpecRegistry,
    ) -> None:
        self.template = template
        self.source_map = source_map
        self.symbols = symbols
        self.specs = specs
        self.diagnostics: list[Diagnostic] = []

    def run(self) -> tuple[Diagnostic, ...]:
        parameters = self.template.get("Parameters")
        if not isinstance(parameters, Mapping):
            return ()
        root = (mapping_segment("Parameters"),)
        root_reference_parameters = self._root_reference_parameters()
        for _parameter_name, parameter, parameter_path in self._source_mapping_items(parameters, root):
            if not isinstance(parameter, Mapping):
                continue
            self._validate_parameter(
                parameter,
                parameter_path,
                depth=0,
                reference_context="template-root",
                reference_parameters=root_reference_parameters,
                reference_declaration_path=root,
            )
        return tuple(self.diagnostics)

    def _root_reference_parameters(self) -> Mapping[Any, Any]:
        parameters = self.template.get("Parameters")
        if not isinstance(parameters, Mapping):
            return {}
        return {name: parameters[name] for name in self.symbols.parameters if name in parameters}

    def _emit(
        self,
        code: str,
        severity: Severity,
        summary: str,
        detail: str,
        path: RosPath,
        *,
        subject: str | None = None,
        stable_args: tuple[str, ...] = (),
        expected: str | None = None,
        actual: str | None = None,
        suggestion: str | None = None,
        related_locations: tuple[RelatedLocation, ...] = (),
    ) -> None:
        if code in {"ROS5304", "ROS5305"} and severity == Severity.WARNING:
            severity = Severity.LIMITATION
        category = (
            Category.COMPATIBILITY
            if severity == Severity.ERROR
            else Category.LIMITATION
            if severity == Severity.LIMITATION
            else Category.QUALITY
        )
        self.diagnostics.append(
            make_diagnostic(
                code=code,
                severity=severity,
                category=category,
                summary=summary,
                detail=detail,
                path=path,
                source_map=self.source_map,
                subject=subject,
                stable_args=stable_args,
                expected=expected,
                actual=actual,
                suggestion=suggestion,
                related_locations=related_locations,
            )
        )

    def _validate_parameter(
        self,
        parameter: Mapping[Any, Any],
        path: RosPath,
        *,
        depth: int,
        reference_context: str,
        reference_parameters: Mapping[Any, Any] | None,
        reference_declaration_path: RosPath | None,
    ) -> None:
        if depth > MAX_METADATA_DEPTH:
            self._emit(
                "ROS1305",
                Severity.ERROR,
                _("AssociationPropertyMetadata nesting is too deep."),
                _("Nested Parameter/Parameters validation stops after {} levels.").format(MAX_METADATA_DEPTH),
                path,
                stable_args=("depth", str(MAX_METADATA_DEPTH)),
                expected=_("at most {} nested levels").format(MAX_METADATA_DEPTH),
                actual=str(depth),
            )
            return

        association_present = "AssociationProperty" in parameter
        raw_association = parameter.get("AssociationProperty")
        normalized_association: str | None = None
        excluded = None
        association_path = path + (mapping_segment("AssociationProperty"),)
        if association_present:
            if not isinstance(raw_association, str) or not raw_association:
                self._emit(
                    "ROS1301",
                    Severity.ERROR,
                    _("AssociationProperty must be a non-empty String."),
                    _("The target parameter form resolves AssociationProperty keys only from non-empty strings."),
                    association_path,
                    subject=str(raw_association),
                    stable_args=(type(raw_association).__name__,),
                    expected=_("non-empty String"),
                    actual=self._actual(raw_association),
                )
            else:
                normalized_association = normalize_association_property(raw_association)
                active = self.specs.association(normalized_association)
                excluded = self.specs.excluded(normalized_association)
                if active is not None and active.deprecated:
                    suggestion = _("Use {} instead.").format(active.replacement) if active.replacement else None
                    self._emit(
                        "ROS5301",
                        Severity.WARNING,
                        _("AssociationProperty value is deprecated: {}.").format(raw_association),
                        _(
                            "The stock ROS form still recognizes this value, so this warning does not block the "
                            "template."
                        ),
                        association_path,
                        subject=normalized_association,
                        stable_args=(normalized_association,),
                        actual=raw_association,
                        suggestion=suggestion,
                    )
                elif excluded is None and active is None:
                    self._emit(
                        "ROS5303",
                        Severity.WARNING,
                        _("AssociationProperty value is absent from the local frontend contract: {}.").format(
                            raw_association
                        ),
                        _(
                            "A newer target frontend may support this value. Local validation uses the stock fallback "
                            "component and does not mark the value invalid."
                        ),
                        association_path,
                        subject=normalized_association,
                        stable_args=(normalized_association,),
                        actual=raw_association,
                    )

        resolution = self._resolve_components(parameter, normalized_association)
        if excluded is not None and normalized_association is not None:
            read_only_component = self._read_only_component_for(normalized_association)
            possible = set(resolution.possible_components)
            if resolution.definite_list_bypass or resolution.initial_component not in possible:
                self._emit(
                    "ROS5302",
                    Severity.WARNING,
                    _("AssociationProperty is bypassed by a later stock form component selection."),
                    _("No resolved form branch selects the unavailable AssociationProperty value."),
                    association_path,
                    subject=normalized_association,
                    stable_args=("excluded-bypassed", normalized_association),
                    actual=str(raw_association),
                )
            elif read_only_component is not None and read_only_component in possible:
                self._emit(
                    "ROS5305",
                    Severity.WARNING,
                    _("AssociationProperty availability depends on the stock form read-only state."),
                    _(
                        "The editable form path does not support this value, while the read-only path may bypass it."
                    ),
                    association_path,
                    subject=normalized_association,
                    stable_args=("excluded-read-only-unknown", normalized_association),
                    actual=str(raw_association),
                )
            else:
                unavailable_detail = self._unavailable_association_property_detail(excluded.scope)
                self._emit(
                    "ROS1302",
                    Severity.ERROR,
                    _("AssociationProperty is unavailable in the stock ROS parameter form."),
                    unavailable_detail,
                    association_path,
                    subject=normalized_association,
                    stable_args=(normalized_association, excluded.scope),
                    expected=_("a stock ROS AssociationProperty"),
                    actual=str(raw_association),
                )
        semantic_rules = self._shared_semantic_rules(resolution)
        auto_reachability = (
            evaluate_auto_complete_reachability(parameter)
            if "auto_complete_character_capacity" in semantic_rules
            else None
        )
        if "AssociationPropertyMetadata" not in parameter:
            return
        metadata = parameter.get("AssociationPropertyMetadata")
        metadata_path = path + (mapping_segment("AssociationPropertyMetadata"),)
        if not isinstance(metadata, Mapping):
            self._emit(
                "ROS1303",
                Severity.ERROR,
                _("AssociationPropertyMetadata must be a Mapping."),
                _("The target form reads named metadata fields from an object."),
                metadata_path,
                stable_args=(type(metadata).__name__,),
                expected=_("Mapping"),
                actual=self._actual(metadata),
            )
            return

        self._validate_metadata(
            metadata,
            metadata_path,
            resolution,
            parameter,
            reference_context=reference_context,
            reference_parameters=reference_parameters,
            reference_declaration_path=reference_declaration_path,
            auto_reachability=auto_reachability,
        )
        if "parameter_metadata_condition" in self.specs.common_semantic_rules:
            self._validate_common_semantics(
                metadata,
                metadata_path,
                reference_context=reference_context,
                reference_parameters=reference_parameters,
                reference_declaration_path=reference_declaration_path,
            )
        self._validate_nested_parameters(
            metadata,
            metadata_path,
            resolution,
            depth=depth,
            reference_context=reference_context,
            reference_parameters=reference_parameters,
            reference_declaration_path=reference_declaration_path,
        )
        if "auto_complete_character_capacity" in semantic_rules:
            self._validate_auto_complete(metadata, metadata_path, auto_reachability)

    def _resolve_components(
        self,
        parameter: Mapping[Any, Any],
        association: str | None,
    ) -> _ComponentResolution:
        association_spec = self.specs.association(association) if association else None
        supported = association_spec is not None
        if supported:
            normal = association_spec.component
        elif frontend_string_to_boolean(parameter.get("NoEcho")):
            normal = "Password"
        else:
            parameter_type = parameter.get("Type")
            allowed_values = parameter.get("AllowedValues")
            if (
                js_truthy(parameter_type)
                and parameter_type not in {"Boolean", "CommaDelimitedList"}
                and js_truthy(allowed_values)
            ):
                normal = _LIST_COMPONENT
            elif js_truthy(parameter.get("TextArea")):
                normal = "TextArea"
            else:
                type_spec = self.specs.association(parameter_type) if isinstance(parameter_type, str) else None
                normal = type_spec.component if type_spec is not None else _INPUT_COMPONENT
        definite_list_bypass = not supported and (
            isinstance(parameter.get("AllowedValues"), list) or normal == _LIST_COMPONENT
        )

        possible = [normal]
        deterministic = True
        read_only_component = self._read_only_component_for(association) if association else None
        if read_only_component is not None:
            if js_truthy(parameter.get("ReadOnly")):
                possible = [read_only_component]
            else:
                possible.append(read_only_component)
                deterministic = False

        if not supported:
            if definite_list_bypass:
                # Static AllowedValues selection replaces the earlier ReadOnly/component choice.
                possible = [_LIST_COMPONENT]
            elif _LIST_COMPONENT not in possible:
                # Dynamic AllowedValues, Mapping props, and external constraints may post-select List.
                possible.append(_LIST_COMPONENT)
                deterministic = False
        possible = list(dict.fromkeys(possible))
        return _ComponentResolution(normal, tuple(possible), deterministic, supported, definite_list_bypass)

    def _read_only_component_for(self, association: str) -> str | None:
        selection = self.specs.profile.get("read_only_selection")
        if not isinstance(selection, Mapping):
            return None
        component = selection.get("component")
        exact = selection.get("exact_association_properties")
        contains = selection.get("contains")
        if not isinstance(component, str):
            return None
        if isinstance(exact, (list, tuple)) and association in exact:
            return component
        if isinstance(contains, (list, tuple)) and any(
            isinstance(fragment, str) and fragment in association for fragment in contains
        ):
            return component
        return None

    def _shared_semantic_rules(self, resolution: _ComponentResolution) -> frozenset[str]:
        rule_sets: list[set[str]] = []
        for component_name in resolution.possible_components:
            component = self.specs.component(component_name)
            if component is None:
                return frozenset()
            rule_sets.append(set(component.semantic_rules))
        if not rule_sets:
            return frozenset()
        shared = rule_sets[0]
        for rule_set in rule_sets[1:]:
            shared &= rule_set
        return frozenset(shared)

    def _validate_metadata(
        self,
        metadata: Mapping[Any, Any],
        path: RosPath,
        resolution: _ComponentResolution,
        parameter: Mapping[Any, Any],
        *,
        reference_context: str,
        reference_parameters: Mapping[Any, Any] | None,
        reference_declaration_path: RosPath | None,
        auto_reachability: AutoCompleteConsumerReachability | None,
    ) -> None:
        common_properties = self.specs.common_metadata.get("properties", {})
        components = tuple(
            component
            for name in resolution.possible_components
            if (component := self.specs.component(name)) is not None
        )
        for raw_key, value in metadata.items():
            key_path = path + (mapping_segment(raw_key),)
            if not isinstance(raw_key, str):
                self._emit(
                    "ROS1305",
                    Severity.ERROR,
                    _("AssociationPropertyMetadata field names must be Strings."),
                    _("The target form performs named property access and cannot consume this key."),
                    key_path,
                    stable_args=(type(raw_key).__name__,),
                    expected=_("String field name"),
                    actual=self._actual(raw_key),
                )
                continue
            schemas: list[Mapping[str, Any]] = []
            shadowed_by: set[str] = set()
            common_matched = False
            component_match_count = 0
            if isinstance(common_properties, Mapping):
                matched, shadowed = self._matching_schemas(common_properties, raw_key, metadata)
                schemas.extend(matched)
                shadowed_by.update(shadowed)
                common_matched = bool(matched)
            for component in components:
                properties = component.metadata.get("properties", {})
                if isinstance(properties, Mapping):
                    matched, shadowed = self._matching_schemas(properties, raw_key, metadata)
                    schemas.extend(matched)
                    shadowed_by.update(shadowed)
                    if matched:
                        component_match_count += 1
            if shadowed_by:
                self._emit(
                    "ROS5302",
                    Severity.WARNING,
                    _("AssociationPropertyMetadata field is shadowed by a higher-precedence alias."),
                    _("The target form reads {} first, so this value is ignored by that consumer.").format(
                        ", ".join(sorted(shadowed_by))
                    ),
                    key_path,
                    subject=raw_key,
                    stable_args=("alias-precedence", raw_key, *sorted(shadowed_by)),
                )
            if not schemas:
                if shadowed_by:
                    continue
                closed = (
                    self.specs.common_coverage == "complete"
                    and resolution.deterministic
                    and len(components) == len(resolution.possible_components)
                    and all(component.coverage == "complete" for component in components)
                )
                self._emit(
                    "ROS1304" if closed else "ROS5304",
                    Severity.ERROR if closed else Severity.WARNING,
                    _("AssociationPropertyMetadata field is not supported by the effective form component.")
                    if closed
                    else _("AssociationPropertyMetadata field is outside the audited contract coverage."),
                    _(
                        "The common schema or at least one possible component is partial/runtime-dependent; "
                        "the field cannot be rejected deterministically."
                    )
                    if not closed
                    else _("The complete common and possible-component schemas all reject this field."),
                    key_path,
                    subject=raw_key,
                    stable_args=(raw_key, *resolution.possible_components, "closed" if closed else "partial"),
                    expected=_("a field consumed by the common or possible component schema"),
                    actual=raw_key,
                )
                continue
            component_only_runtime_dependent = not common_matched and (
                len(components) != len(resolution.possible_components)
                or component_match_count != len(resolution.possible_components)
            )
            if component_only_runtime_dependent:
                if not any(schema.get("x-ore-nested-parameter") is True for schema in schemas):
                    self._emit(
                        "ROS5305",
                        Severity.WARNING,
                        _("Metadata field {} is used by some possible form components and ignored by others.").format(
                            raw_key
                        ),
                        _("The runtime component cannot be determined locally, so this field is left unchecked."),
                        key_path,
                        subject=raw_key,
                        stable_args=("component-field-reachability", raw_key, *resolution.possible_components),
                        suggestion=_(
                            "Do not change the template based on this limitation alone; verify whether the field "
                            "takes effect in the target ROS form if needed."
                        ),
                    )
                continue
            nested_reachability = self._nested_field_reachability(resolution, raw_key)[0]
            if nested_reachability == ConsumerReachability.UNKNOWN:
                # A component-specific nested container is not consumed on
                # every possible component branch.  Its contents cannot
                # produce deterministic schema errors on this profile.
                continue
            merged_schema: Mapping[str, Any] = schemas[0] if len(schemas) == 1 else {"anyOf": schemas}
            self._validate_schema(
                value,
                merged_schema,
                key_path,
                parameter=parameter,
                reference_context=reference_context,
                reference_parameters=reference_parameters,
                reference_declaration_path=reference_declaration_path,
                auto_reachability=auto_reachability,
                depth=0,
            )

    @staticmethod
    def _matching_schemas(
        properties: Mapping[str, Any],
        key: str,
        metadata: Mapping[Any, Any],
    ) -> tuple[list[Mapping[str, Any]], set[str]]:
        matches: list[Mapping[str, Any]] = []
        shadowed_by: set[str] = set()
        for canonical, schema in properties.items():
            if not isinstance(schema, Mapping):
                continue
            aliases = schema.get("x-ore-aliases")
            accepted = {canonical}
            if isinstance(aliases, (list, tuple)):
                accepted.update(alias for alias in aliases if isinstance(alias, str))
            if key not in accepted:
                continue
            precedence = schema.get("x-ore-precedence")
            if isinstance(precedence, (list, tuple)):
                winner = next((candidate for candidate in precedence if candidate in metadata), None)
                if winner is not None and winner != key:
                    shadowed_by.add(winner)
                    continue
            matches.append(schema)
        return matches, shadowed_by

    def _validate_schema(
        self,
        value: Any,
        schema: Mapping[str, Any],
        path: RosPath,
        *,
        parameter: Mapping[Any, Any],
        reference_context: str,
        reference_parameters: Mapping[Any, Any] | None,
        reference_declaration_path: RosPath | None,
        auto_reachability: AutoCompleteConsumerReachability | None,
        depth: int,
    ) -> None:
        if depth > MAX_METADATA_DEPTH:
            self._emit(
                "ROS1305",
                Severity.ERROR,
                _("AssociationPropertyMetadata value is nested too deeply."),
                _("Schema traversal stops after {} levels.").format(MAX_METADATA_DEPTH),
                path,
                stable_args=("schema-depth", str(MAX_METADATA_DEPTH)),
            )
            return
        (
            effective_reference_context,
            effective_reference_parameters,
            effective_reference_declaration_path,
        ) = self._schema_reference_scope(
            schema,
            reference_context,
            reference_parameters,
            reference_declaration_path,
        )
        if self._validate_reference_encoding(
            value,
            schema,
            path,
            parameter=parameter,
            reference_context=effective_reference_context,
            reference_parameters=effective_reference_parameters,
            reference_declaration_path=effective_reference_declaration_path,
            auto_reachability=auto_reachability,
        ):
            self._warn_reference_type(
                value,
                schema,
                path,
                reference_context=effective_reference_context,
                reference_parameters=effective_reference_parameters,
                reference_declaration_path=effective_reference_declaration_path,
            )
            return
        any_of = schema.get("anyOf")
        if isinstance(any_of, (list, tuple)):
            # A merged common/component schema deliberately keeps parser
            # annotations on its candidate branches.  Give every candidate a
            # chance to consume an encoded reference before literal type-based
            # branch selection.
            failed_reference_candidates: list[
                tuple[
                    Mapping[str, Any],
                    str,
                    Mapping[Any, Any] | None,
                    RosPath | None,
                    tuple[Diagnostic, ...],
                ]
            ] = []
            for candidate in any_of:
                if not isinstance(candidate, Mapping):
                    continue
                candidate_context, candidate_parameters, candidate_declaration_path = self._schema_reference_scope(
                    candidate,
                    effective_reference_context,
                    effective_reference_parameters,
                    effective_reference_declaration_path,
                )
                diagnostic_start = len(self.diagnostics)
                consumed = self._validate_reference_encoding(
                    value,
                    candidate,
                    path,
                    parameter=parameter,
                    reference_context=candidate_context,
                    reference_parameters=candidate_parameters,
                    reference_declaration_path=candidate_declaration_path,
                    auto_reachability=auto_reachability,
                )
                candidate_diagnostics = tuple(self.diagnostics[diagnostic_start:])
                del self.diagnostics[diagnostic_start:]
                if consumed and not any(item.severity == Severity.ERROR for item in candidate_diagnostics):
                    self.diagnostics.extend(candidate_diagnostics)
                    self._warn_reference_type(
                        value,
                        candidate,
                        path,
                        reference_context=candidate_context,
                        reference_parameters=candidate_parameters,
                        reference_declaration_path=candidate_declaration_path,
                    )
                    return
                if consumed:
                    failed_reference_candidates.append(
                        (
                            candidate,
                            candidate_context,
                            candidate_parameters,
                            candidate_declaration_path,
                            candidate_diagnostics,
                        )
                    )
            failed_candidate_ids = {id(candidate) for candidate, *_ in failed_reference_candidates}
            matches = [
                candidate
                for candidate in any_of
                if id(candidate) not in failed_candidate_ids and self._matches_schema(value, candidate)
            ]
            if matches:
                self._validate_schema(
                    value,
                    matches[0],
                    path,
                    parameter=parameter,
                    reference_context=effective_reference_context,
                    reference_parameters=effective_reference_parameters,
                    reference_declaration_path=effective_reference_declaration_path,
                    auto_reachability=auto_reachability,
                    depth=depth + 1,
                )
                return
            if failed_reference_candidates:
                candidate, candidate_context, candidate_parameters, candidate_declaration_path, diagnostics = (
                    failed_reference_candidates[0]
                )
                self.diagnostics.extend(diagnostics)
                self._warn_reference_type(
                    value,
                    candidate,
                    path,
                    reference_context=candidate_context,
                    reference_parameters=candidate_parameters,
                    reference_declaration_path=candidate_declaration_path,
                )
                return
            self._schema_error(value, schema, path, "anyOf")
            return
        if not self._matches_type(value, schema.get("type")):
            self._schema_error(value, schema, path, "type")
            return
        if "const" in schema and value != schema["const"]:
            self._schema_error(value, schema, path, "const")
            return
        enum = schema.get("enum")
        if isinstance(enum, (list, tuple)) and value not in enum:
            suggestions = schema.get("x-ore-value-suggestions", {})
            replacement = suggestions.get(str(value)) if isinstance(suggestions, Mapping) else None
            self._emit(
                "ROS1305",
                Severity.ERROR,
                _("AssociationPropertyMetadata value is not in the local contract enum."),
                _("The effective component only implements the exported enum values."),
                path,
                subject=str(value),
                stable_args=("enum", str(value), *(str(item) for item in enum)),
                expected=" | ".join(str(item) for item in enum),
                actual=self._actual(value),
                suggestion=_("Use {} instead.").format(replacement) if replacement else None,
            )
            return
        if isinstance(value, (int, float)) and not isinstance(value, bool) and "minimum" in schema:
            if value < schema["minimum"]:
                self._schema_error(value, schema, path, "minimum")
                return
        if isinstance(value, str) and "minLength" in schema and len(value) < schema["minLength"]:
            self._schema_error(value, schema, path, "minLength")
            return
        if isinstance(value, Mapping):
            properties = schema.get("properties", {})
            required = schema.get("required", ())
            for required_key in required if isinstance(required, (list, tuple)) else ():
                if required_key not in value:
                    missing_path = path + (mapping_segment(required_key),)
                    self._emit(
                        "ROS1305",
                        Severity.ERROR,
                        _("AssociationPropertyMetadata object is missing a required field."),
                        _("The effective form schema requires this nested field."),
                        missing_path,
                        subject=required_key,
                        stable_args=("required", required_key),
                        expected=_("required field {}").format(required_key),
                        actual=_("missing"),
                    )
            for key, child in value.items():
                child_path = path + (mapping_segment(key),)
                child_schema = properties.get(key) if isinstance(properties, Mapping) else None
                if not isinstance(child_schema, Mapping):
                    child_schema = self._pattern_schema(schema, key)
                if isinstance(child_schema, Mapping):
                    self._validate_schema(
                        child,
                        child_schema,
                        child_path,
                        parameter=parameter,
                        reference_context=effective_reference_context,
                        reference_parameters=effective_reference_parameters,
                        reference_declaration_path=effective_reference_declaration_path,
                        auto_reachability=auto_reachability,
                        depth=depth + 1,
                    )
                else:
                    additional = schema.get("additionalProperties", True)
                    if additional is False:
                        case_match = next(
                            (
                                candidate
                                for candidate in properties
                                if isinstance(candidate, str)
                                and isinstance(key, str)
                                and candidate.casefold() == key.casefold()
                            ),
                            None,
                        )
                        self._emit(
                            "ROS1305",
                            Severity.ERROR,
                            _("AssociationPropertyMetadata field uses the wrong letter case.")
                            if case_match
                            else _("AssociationPropertyMetadata object contains an unsupported nested field."),
                            _("Field names are case-sensitive; use {} instead.").format(case_match)
                            if case_match
                            else _("This nested schema is closed."),
                            child_path,
                            subject=str(key),
                            stable_args=("field-case" if case_match else "additionalProperties", str(key)),
                            expected=_("one of: {}").format(", ".join(str(item) for item in properties)),
                            actual=str(key),
                            suggestion=_("Rename {} to {}.").format(key, case_match) if case_match else None,
                        )
                    elif isinstance(additional, Mapping):
                        self._validate_schema(
                            child,
                            additional,
                            child_path,
                            parameter=parameter,
                            reference_context=effective_reference_context,
                            reference_parameters=effective_reference_parameters,
                            reference_declaration_path=effective_reference_declaration_path,
                            auto_reachability=auto_reachability,
                            depth=depth + 1,
                        )
        elif isinstance(value, list) and isinstance(schema.get("items"), Mapping):
            for index, child in enumerate(value):
                self._validate_schema(
                    child,
                    schema["items"],
                    path + (SequenceIndexSegment(index),),
                    parameter=parameter,
                    reference_context=effective_reference_context,
                    reference_parameters=effective_reference_parameters,
                    reference_declaration_path=effective_reference_declaration_path,
                    auto_reachability=auto_reachability,
                    depth=depth + 1,
                )

    def _schema_reference_scope(
        self,
        schema: Mapping[str, Any],
        reference_context: str,
        reference_parameters: Mapping[Any, Any] | None,
        reference_declaration_path: RosPath | None,
    ) -> tuple[str, Mapping[Any, Any] | None, RosPath | None]:
        schema_reference_context = schema.get("x-ore-reference-context")
        if schema_reference_context == "runtime-dependent" and reference_context in {
            "meta-list-row",
            "nested-parameter-map",
        }:
            effective_context = reference_context
        else:
            effective_context = (
                schema_reference_context if isinstance(schema_reference_context, str) else reference_context
            )
        if effective_context != "template-root":
            return effective_context, reference_parameters, reference_declaration_path
        if reference_context == "nested-parameter-map" and reference_parameters is not None:
            return effective_context, reference_parameters, reference_declaration_path
        return effective_context, self._root_reference_parameters(), (mapping_segment("Parameters"),)

    @staticmethod
    def _pattern_schema(schema: Mapping[str, Any], key: Any) -> Mapping[str, Any] | None:
        if not isinstance(key, str):
            return None
        patterns = schema.get("patternProperties")
        if not isinstance(patterns, Mapping):
            return None
        for pattern, child in patterns.items():
            try:
                if re.search(pattern, key) and isinstance(child, Mapping):
                    return child
            except re.error:
                continue
        return None

    def _schema_error(self, value: Any, schema: Mapping[str, Any], path: RosPath, reason: str) -> None:
        expected = self._expected(schema)
        self._emit(
            "ROS1305",
            Severity.ERROR,
            _("AssociationPropertyMetadata value does not match the local form schema."),
            _("The literal value failed the exported {} constraint.").format(reason),
            path,
            subject=reason,
            stable_args=(reason, self._expected_stable(schema), self._actual(value)),
            expected=expected,
            actual=self._actual(value),
        )

    def _matches_schema(self, value: Any, schema: Any) -> bool:
        if not isinstance(schema, Mapping):
            return False
        any_of = schema.get("anyOf")
        if isinstance(any_of, (list, tuple)):
            return any(self._matches_schema(value, candidate) for candidate in any_of)
        if not self._matches_type(value, schema.get("type")):
            return False
        if "const" in schema and value != schema["const"]:
            return False
        enum = schema.get("enum")
        if isinstance(enum, (list, tuple)) and value not in enum:
            return False
        minimum = schema.get("minimum")
        if (
            isinstance(minimum, (int, float))
            and not isinstance(minimum, bool)
            and isinstance(value, (int, float))
            and not isinstance(value, bool)
            and value < minimum
        ):
            return False
        min_length = schema.get("minLength")
        if isinstance(min_length, int) and not isinstance(min_length, bool) and isinstance(value, str):
            if len(value) < min_length:
                return False
        if isinstance(value, Mapping):
            required = schema.get("required", ())
            if isinstance(required, (list, tuple)) and any(key not in value for key in required):
                return False
            properties = schema.get("properties", {})
            for key, child in value.items():
                child_schema = properties.get(key) if isinstance(properties, Mapping) else None
                if not isinstance(child_schema, Mapping):
                    child_schema = self._pattern_schema(schema, key)
                if child_schema is None and isinstance(schema.get("additionalProperties"), Mapping):
                    child_schema = schema["additionalProperties"]
                if isinstance(child_schema, Mapping) and not self._matches_schema(child, child_schema):
                    return False
                if child_schema is None and schema.get("additionalProperties", True) is False:
                    return False
        if isinstance(value, list) and isinstance(schema.get("items"), Mapping):
            return all(self._matches_schema(child, schema["items"]) for child in value)
        return True

    @staticmethod
    def _matches_type(value: Any, expected: Any) -> bool:
        if expected is None:
            return True
        if expected == "null":
            return value is None
        if expected == "boolean":
            return isinstance(value, bool)
        if expected == "integer":
            return isinstance(value, int) and not isinstance(value, bool)
        if expected == "number":
            return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)
        if expected == "string":
            return isinstance(value, str)
        if expected == "array":
            return isinstance(value, list)
        if expected == "object":
            return isinstance(value, Mapping)
        return False

    def _validate_reference_encoding(
        self,
        value: Any,
        schema: Mapping[str, Any],
        path: RosPath,
        *,
        parameter: Mapping[Any, Any],
        reference_context: str,
        reference_parameters: Mapping[Any, Any] | None,
        reference_declaration_path: RosPath | None,
        auto_reachability: AutoCompleteConsumerReachability | None,
    ) -> bool:
        del parameter
        if not isinstance(value, str):
            return False
        parsers = self._schema_reference_parsers(schema)
        allowed_kinds: set[str] | None = None
        injected_symbols: set[str] = set()
        direct_kinds = schema.get("x-ore-reference-kinds")
        if isinstance(direct_kinds, (list, tuple)):
            allowed_kinds = {kind for kind in direct_kinds if isinstance(kind, str)}
        direct_symbols = schema.get("x-ore-injected-symbols")
        if isinstance(direct_symbols, (list, tuple)):
            injected_symbols = {symbol for symbol in direct_symbols if isinstance(symbol, str)}
        consumer_set_name = schema.get("x-ore-consumer-set")
        consumer_set = self.specs.consumer_sets.get(consumer_set_name) if isinstance(consumer_set_name, str) else None
        inconsistent = False
        if isinstance(consumer_set, Mapping):
            inconsistent = consumer_set.get("resolution") == "inconsistent"
            consumers = consumer_set.get("consumers", ())
            if isinstance(consumers, (list, tuple)):
                for consumer in consumers:
                    if not isinstance(consumer, Mapping) or not isinstance(consumer.get("parser"), str):
                        continue
                    consumer_kinds = consumer.get("reference_kinds")
                    if isinstance(consumer_kinds, (list, tuple)):
                        declared = {kind for kind in consumer_kinds if isinstance(kind, str)}
                        allowed_kinds = declared if allowed_kinds is None else allowed_kinds & declared
        parsers.discard("literal-only")
        if not parsers:
            return False
        looks_encoded = (
            "${" in value
            or "{{" in value
            or self._bare_reference_exists(
                value,
                reference_context,
                reference_parameters,
                reference_declaration_path,
            )
        )
        if not looks_encoded:
            return False
        valid_reference = False
        deterministic_failure = (
            auto_reachability is not None
            and auto_reachability.raw_initializer == ConsumerReachability.REACHED
            and auto_reachability.component_effect == ConsumerReachability.NOT_REACHED
        )
        uncertain = inconsistent and not deterministic_failure
        for parser in parsers:
            if parser == "whole-value-reference":
                valid_reference = (
                    self._validate_whole_reference(
                        value,
                        path,
                        reference_context,
                        reference_parameters=reference_parameters,
                        reference_declaration_path=reference_declaration_path,
                        allowed_kinds=allowed_kinds,
                        injected_symbols=injected_symbols,
                        uncertain=uncertain,
                    )
                    or valid_reference
                )
            elif parser == "lodash-template-interpolation":
                valid_reference = (
                    self._validate_interpolations(
                        value,
                        path,
                        reference_context,
                        reference_parameters=reference_parameters,
                        reference_declaration_path=reference_declaration_path,
                        allowed_kinds=allowed_kinds,
                        injected_symbols=injected_symbols,
                        uncertain=uncertain,
                    )
                    or valid_reference
                )
            elif parser == "mapping-selector-segments":
                valid_reference = (
                    self._validate_mapping_selector_segments(
                        value,
                        path,
                        reference_context,
                        reference_parameters=reference_parameters,
                        reference_declaration_path=reference_declaration_path,
                        allowed_kinds=allowed_kinds,
                        injected_symbols=injected_symbols,
                        uncertain=uncertain,
                        unresolved_as_literal=schema.get("x-ore-unresolved-reference") == "literal-segment",
                    )
                    or valid_reference
                )
        if inconsistent and valid_reference:
            self._emit(
                "ROS5305",
                Severity.WARNING,
                _("AssociationPropertyMetadata reference has inconsistent frontend consumers."),
                _(
                    "One consumer parses this value, but the template-default initializer may use the raw text. "
                    "Host values and effect timing are unavailable locally."
                ),
                path,
                subject=value,
                stable_args=("inconsistent-consumers", value),
            )
        return valid_reference

    def _schema_reference_parsers(self, schema: Mapping[str, Any]) -> set[str]:
        parsers = {parser for parser in (schema.get("x-ore-parser"),) if isinstance(parser, str)}
        consumer_set_name = schema.get("x-ore-consumer-set")
        consumer_set = self.specs.consumer_sets.get(consumer_set_name) if isinstance(consumer_set_name, str) else None
        consumers = consumer_set.get("consumers", ()) if isinstance(consumer_set, Mapping) else ()
        if isinstance(consumers, (list, tuple)):
            parsers.update(
                parser
                for consumer in consumers
                if isinstance(consumer, Mapping) and isinstance((parser := consumer.get("parser")), str)
            )
        parsers.discard("literal-only")
        return parsers

    def _validate_whole_reference(
        self,
        value: str,
        path: RosPath,
        reference_context: str,
        *,
        reference_parameters: Mapping[Any, Any] | None,
        reference_declaration_path: RosPath | None,
        allowed_kinds: set[str] | None,
        injected_symbols: set[str],
        uncertain: bool,
    ) -> bool:
        classified = self._classify_whole_reference(
            value,
            reference_context,
            reference_parameters,
            reference_declaration_path,
        )
        if classified is None:
            return False
        reference_kind, name = classified
        if reference_kind == "field-path":
            self._validate_reference_name(
                name,
                value,
                path,
                reference_context,
                reference_parameters=reference_parameters,
                reference_declaration_path=reference_declaration_path,
                allowed_kinds=allowed_kinds,
                injected_symbols=injected_symbols,
                uncertain=uncertain,
            )
            return True
        if reference_kind == "parameter":
            self._validate_exact_parameter_reference(
                name,
                value,
                path,
                reference_context,
                reference_parameters=reference_parameters,
                reference_declaration_path=reference_declaration_path,
                allowed_kinds=allowed_kinds,
                injected_symbols=injected_symbols,
                uncertain=uncertain,
            )
            return True
        if reference_kind == "env":
            if allowed_kinds is not None and "env" not in allowed_kinds:
                self._reference_error(
                    value,
                    path,
                    "environment references are not allowed by this consumer",
                    uncertain=uncertain,
                )
                return True
            self._emit(
                "ROS5305",
                Severity.WARNING,
                _("Environment metadata reference cannot be resolved locally."),
                _("The encoding is valid, but the contract does not provide an environment-data schema."),
                path,
                subject=value,
                stable_args=("env", name),
            )
            return True
        raise AssertionError("unsupported whole-value reference classification")

    def _classify_whole_reference(
        self,
        value: str,
        reference_context: str,
        reference_parameters: Mapping[Any, Any] | None,
        reference_declaration_path: RosPath | None,
    ) -> tuple[str, str] | None:
        """Classify one value in the branch order used by the target form."""

        reference_match = _FRONTEND_REFERENCE.fullmatch(value)
        name = reference_match.group(1).strip() if reference_match is not None else None
        # Field-path detection runs on the raw wrapper before ref-key extraction
        # or ${!...} processing.
        if name is not None and _is_lodash_field_path(name):
            return "field-path", name
        # The parameter-reference pattern is deliberately greedy. Keys such as A{B} are
        # exact Parameter names, while a leading ! suppresses this branch.
        if name and not name.startswith("!"):
            return "parameter", name
        # The escape branch rewrites the spelling before the legacy exact
        # Parameter lookup. JavaScript truthiness preserves whitespace-only
        # captures but treats an empty capture as absent.
        legacy_key = value
        escaped_match = _ESCAPED_LITERAL.fullmatch(value)
        if escaped_match is not None and escaped_match.group(1):
            legacy_key = "${" + escaped_match.group(1) + "}"
        if self._bare_reference_exists(
            legacy_key,
            reference_context,
            reference_parameters,
            reference_declaration_path,
        ):
            return "parameter", legacy_key
        # A raw legacy Parameter named {{x}} shadows environment data.
        env_match = _ENV_REFERENCE.fullmatch(value)
        if env_match is not None and env_match.group(1):
            return "env", env_match.group(1)
        return None

    def _validate_exact_parameter_reference(
        self,
        name: str,
        encoded: str,
        path: RosPath,
        reference_context: str,
        *,
        reference_parameters: Mapping[Any, Any] | None,
        reference_declaration_path: RosPath | None,
        allowed_kinds: set[str] | None = None,
        injected_symbols: set[str] | None = None,
        uncertain: bool = False,
    ) -> None:
        """Validate a consumer that performs one exact JavaScript property lookup."""

        if not name:
            self._reference_error(encoded, path, "empty reference", uncertain=uncertain)
            return
        if allowed_kinds is not None and "parameter" not in allowed_kinds:
            self._reference_error(
                encoded,
                path,
                "parameter references are not allowed by this consumer",
                name=name,
                uncertain=uncertain,
            )
            return
        if injected_symbols and name in injected_symbols:
            return
        resolution = self._resolve_exact_parameter_declaration(
            name,
            reference_context,
            reference_parameters,
            reference_declaration_path,
        )
        if resolution.limitation:
            self._emit_reference_limitation(name, path, reference_context)
            return
        if resolution.error is not None:
            self._reference_error(encoded, path, resolution.error, name=name, uncertain=uncertain)
            return

    def _resolve_exact_parameter_declaration(
        self,
        name: str,
        reference_context: str,
        reference_parameters: Mapping[Any, Any] | None,
        reference_declaration_path: RosPath | None,
    ) -> _ResolvedReference:
        parameters, declaration_path = self._reference_scope(
            reference_context,
            reference_parameters,
            reference_declaration_path,
            relative=False,
        )
        if parameters is None or declaration_path is None:
            return _ResolvedReference(limitation=True)
        found, declaration, resolved_path = self._source_mapping_property(
            parameters,
            declaration_path,
            name,
        )
        if not found:
            reason = (
                "nested Parameter does not exist"
                if reference_context == "nested-parameter-map"
                else "Parameter does not exist"
            )
            return _ResolvedReference(error=reason)
        if not isinstance(declaration, Mapping):
            return _ResolvedReference(error="referenced Parameter declaration is invalid")
        return _ResolvedReference(
            declaration=declaration,
            declaration_path=resolved_path,
        )

    def _validate_interpolations(
        self,
        value: str,
        path: RosPath,
        reference_context: str,
        *,
        reference_parameters: Mapping[Any, Any] | None,
        reference_declaration_path: RosPath | None,
        allowed_kinds: set[str] | None,
        injected_symbols: set[str],
        uncertain: bool,
    ) -> bool:
        matches = list(_LODASH_REFERENCE.finditer(value))
        if not matches:
            return False
        for match in matches:
            name = match.group(1).strip()
            if name.startswith("!"):
                continue
            if allowed_kinds == {"parameter"}:
                self._validate_exact_parameter_reference(
                    name,
                    match.group(0),
                    path,
                    reference_context,
                    reference_parameters=reference_parameters,
                    reference_declaration_path=reference_declaration_path,
                    allowed_kinds=allowed_kinds,
                    injected_symbols=injected_symbols,
                    uncertain=uncertain,
                )
            else:
                self._validate_reference_name(
                    name,
                    match.group(0),
                    path,
                    reference_context,
                    reference_parameters=reference_parameters,
                    reference_declaration_path=reference_declaration_path,
                    allowed_kinds=allowed_kinds,
                    injected_symbols=injected_symbols,
                    uncertain=uncertain,
                )
        return True

    def _validate_mapping_selector_segments(
        self,
        value: str,
        path: RosPath,
        reference_context: str,
        *,
        reference_parameters: Mapping[Any, Any] | None,
        reference_declaration_path: RosPath | None,
        allowed_kinds: set[str] | None,
        injected_symbols: set[str],
        uncertain: bool,
        unresolved_as_literal: bool,
    ) -> bool:
        matched_reference = False
        for segment in value.split("."):
            match = _FRONTEND_REFERENCE.fullmatch(segment)
            if match is None or match.group(1) == "":
                continue
            matched_reference = True
            name = match.group(1)
            if name in injected_symbols:
                continue
            if allowed_kinds is not None and "parameter" not in allowed_kinds:
                self._reference_error(
                    segment,
                    path,
                    "parameter references are not allowed by this consumer",
                    name=name,
                    uncertain=uncertain,
                )
                continue
            parameters, declaration_path = self._reference_scope(
                reference_context,
                reference_parameters,
                reference_declaration_path,
                relative=False,
            )
            if parameters is None or declaration_path is None:
                self._emit_reference_limitation(name, path, reference_context)
                continue
            exists, _resolved, _resolved_path = self._source_mapping_property(
                parameters,
                declaration_path,
                name,
            )
            if exists:
                continue
            if unresolved_as_literal:
                self._emit(
                    "ROS5305",
                    Severity.WARNING,
                    _("Mapping selector segment is interpreted as a literal key by the target form."),
                    _("No Parameter named {} exists; the current mapping hook falls back to the segment text.").format(
                        name
                    ),
                    path,
                    subject=name,
                    stable_args=("mapping-literal-segment", name),
                )
                continue
            self._reference_error(
                segment,
                path,
                "Parameter does not exist",
                name=name,
                uncertain=uncertain,
            )
        return matched_reference

    def _bare_reference_exists(
        self,
        value: str,
        reference_context: str,
        reference_parameters: Mapping[Any, Any] | None,
        reference_declaration_path: RosPath | None,
    ) -> bool:
        parameters, declaration_path = self._reference_scope(
            reference_context,
            reference_parameters,
            reference_declaration_path,
            relative=False,
        )
        if not isinstance(parameters, Mapping) or declaration_path is None:
            return False
        return self._source_mapping_property(parameters, declaration_path, value)[0]

    def _reference_scope(
        self,
        reference_context: str,
        reference_parameters: Mapping[Any, Any] | None,
        reference_declaration_path: RosPath | None,
        *,
        relative: bool,
    ) -> tuple[Mapping[Any, Any] | None, RosPath | None]:
        if relative:
            return (
                (reference_parameters, reference_declaration_path)
                if reference_context == "meta-list-row"
                else (None, None)
            )
        if reference_context == "meta-list-row":
            return self._root_reference_parameters(), (mapping_segment("Parameters"),)
        if reference_context in {"template-root", "nested-parameter-map"}:
            return reference_parameters, reference_declaration_path
        return None, None

    def _source_mapping_items(
        self,
        value: Mapping[Any, Any],
        value_path: RosPath,
    ) -> tuple[tuple[str, Any, RosPath], ...]:
        """Rebuild the JavaScript-visible direct properties from YAML source occurrences."""

        visible: dict[str, tuple[Any, RosPath]] = {}
        for positioned in self.source_map.occurrences:
            if (
                len(positioned.path) == len(value_path) + 1
                and positioned.path[:-1] == value_path
                and isinstance((segment := positioned.path[-1]), MappingKeySegment)
            ):
                key = _js_property_key(segment.value)
                if key is not None:
                    visible[key] = (positioned.value, positioned.path)
        for raw_key, resolved in value.items():
            key = _js_property_key(raw_key)
            if key is not None and key not in visible:
                visible[key] = (resolved, value_path + (mapping_segment(raw_key),))
        return tuple((key, resolved, path) for key, (resolved, path) in visible.items())

    def _source_mapping_property(
        self,
        value: Mapping[Any, Any],
        value_path: RosPath,
        key: str,
    ) -> tuple[bool, Any, RosPath]:
        for visible_key, resolved, resolved_path in self._source_mapping_items(value, value_path):
            if visible_key == key:
                return True, resolved, resolved_path
        return False, None, value_path + (mapping_segment(key),)

    @staticmethod
    def _parse_reference_path(name: str) -> tuple[bool, list[tuple[str, bool]]] | None:
        relative = name.startswith(".")
        raw = name[1:] if relative else name
        if not raw:
            return None
        result: list[tuple[str, bool]] = []
        for segment in raw.split("."):
            match = re.fullmatch(r"([^.[\]]+)(\[\])?", segment)
            if match is None:
                return None
            result.append((match.group(1), match.group(2) is not None))
        return relative, result

    def _validate_reference_name(
        self,
        name: str,
        encoded: str,
        path: RosPath,
        reference_context: str,
        *,
        reference_parameters: Mapping[Any, Any] | None = None,
        reference_declaration_path: RosPath | None = None,
        allowed_kinds: set[str] | None = None,
        injected_symbols: set[str] | None = None,
        uncertain: bool = False,
    ) -> None:
        if not name:
            self._reference_error(encoded, path, "empty reference", uncertain=uncertain)
            return
        parsed = self._parse_reference_path(name)
        if parsed is None:
            if _is_lodash_field_path(name):
                if allowed_kinds is not None and "field-path" not in allowed_kinds:
                    self._reference_error(
                        encoded,
                        path,
                        "field-path references are not allowed by this consumer",
                        name=name,
                        uncertain=uncertain,
                    )
                else:
                    self._emit_reference_limitation(name, path, reference_context)
                return
            self._reference_error(encoded, path, "malformed field path", name=name, uncertain=uncertain)
            return
        relative, segments = parsed
        reference_kind = "field-path" if relative or len(segments) > 1 or segments[0][1] else "parameter"
        if allowed_kinds is not None and reference_kind not in allowed_kinds:
            self._reference_error(
                encoded,
                path,
                "{} references are not allowed by this consumer".format(reference_kind),
                name=name,
                uncertain=uncertain,
            )
            return
        if not relative and segments == [(name, False)] and injected_symbols and name in injected_symbols:
            return
        array_segments = [index for index, (_, is_array) in enumerate(segments) if is_array]
        if array_segments and (relative or array_segments != [0] or len(segments) < 2):
            self._reference_error(
                encoded,
                path,
                "field paths support one top-level array projection followed by a child path",
                name=name,
                uncertain=uncertain,
            )
            return
        resolution = self._resolve_reference_declaration(
            name,
            reference_context,
            reference_parameters,
            reference_declaration_path,
        )
        if resolution.limitation:
            self._emit_reference_limitation(name, path, reference_context)
            return
        if resolution.error is not None:
            self._reference_error(
                encoded,
                path,
                resolution.error,
                name=name,
                uncertain=uncertain,
            )
        return

    def _resolve_reference_declaration(
        self,
        name: str,
        reference_context: str,
        reference_parameters: Mapping[Any, Any] | None,
        reference_declaration_path: RosPath | None,
    ) -> _ResolvedReference:
        parsed = self._parse_reference_path(name)
        if parsed is None:
            return _ResolvedReference(error="malformed field path")
        relative, segments = parsed
        array_segments = [index for index, (_, is_array) in enumerate(segments) if is_array]
        if array_segments and (relative or array_segments != [0] or len(segments) < 2):
            return _ResolvedReference(
                error="field paths support one top-level array projection followed by a child path"
            )
        parameters, declaration_path = self._reference_scope(
            reference_context,
            reference_parameters,
            reference_declaration_path,
            relative=relative,
        )
        if parameters is None or declaration_path is None:
            return _ResolvedReference(limitation=True)
        first_name, first_array = segments[0]
        found, current, current_path = self._source_mapping_property(
            parameters,
            declaration_path,
            first_name,
        )
        if not found:
            reason = "MetaList row field does not exist" if relative else "Parameter does not exist"
            if reference_context == "nested-parameter-map" and not relative:
                reason = "nested Parameter does not exist"
            return _ResolvedReference(error=reason)
        if not isinstance(current, Mapping):
            return _ResolvedReference(error="referenced Parameter declaration is invalid")
        nested_parameters_proven = False
        if first_array:
            descended = self._descend_array_reference(current, current_path)
            if descended is None:
                return _ResolvedReference(limitation=True)
            current, current_path, nested_parameters_proven = descended
        for segment_name, segment_array in segments[1:]:
            if not nested_parameters_proven:
                if current.get("Type") in {"Boolean", "CommaDelimitedList", "Integer", "Number", "String"}:
                    return _ResolvedReference(error="field path traverses a scalar Parameter")
                raw_association = current.get("AssociationProperty")
                association = (
                    normalize_association_property(raw_association)
                    if isinstance(raw_association, str) and raw_association
                    else None
                )
                resolution = self._resolve_components(current, association)
                reachability, nested_context = self._nested_field_reachability(resolution, "Parameters")
                if reachability != ConsumerReachability.REACHED or nested_context != "nested-parameter-map":
                    return _ResolvedReference(limitation=True)
            metadata = current.get("AssociationPropertyMetadata")
            nested = metadata.get("Parameters") if isinstance(metadata, Mapping) else None
            if not isinstance(nested, Mapping):
                return _ResolvedReference(limitation=True)
            nested_path = current_path + (
                mapping_segment("AssociationPropertyMetadata"),
                mapping_segment("Parameters"),
            )
            found, current, current_path = self._source_mapping_property(
                nested,
                nested_path,
                segment_name,
            )
            if not found:
                return _ResolvedReference(error="nested Parameter field does not exist")
            if not isinstance(current, Mapping):
                return _ResolvedReference(error="referenced nested Parameter declaration is invalid")
            nested_parameters_proven = False
            if segment_array:
                descended = self._descend_array_reference(current, current_path)
                if descended is None:
                    return _ResolvedReference(limitation=True)
                current, current_path, nested_parameters_proven = descended
        return _ResolvedReference(
            declaration=current,
            declaration_path=current_path,
            projected_array=first_array,
        )

    def _descend_array_reference(
        self,
        parameter: Mapping[Any, Any],
        declaration_path: RosPath,
    ) -> tuple[Mapping[Any, Any], RosPath, bool] | None:
        metadata = parameter.get("AssociationPropertyMetadata")
        if not isinstance(metadata, Mapping):
            return None
        raw_association = parameter.get("AssociationProperty")
        association = (
            normalize_association_property(raw_association)
            if isinstance(raw_association, str) and raw_association
            else None
        )
        resolution = self._resolve_components(parameter, association)
        row_fields: set[str] = set()
        for component_name in resolution.possible_components:
            component = self.specs.component(component_name)
            properties = component.metadata.get("properties", {}) if component is not None else {}
            component_row_fields = {
                field
                for field in ("Parameter", "Parameters")
                if isinstance(properties, Mapping)
                and isinstance((schema := properties.get(field)), Mapping)
                and schema.get("x-ore-reference-context") == "meta-list-row"
            }
            if len(component_row_fields) != 1:
                return None
            row_fields.update(component_row_fields)
        if len(row_fields) != 1:
            return None
        row_field = row_fields.pop()
        child = metadata.get(row_field)
        if row_field == "Parameter" and isinstance(child, Mapping):
            return (
                child,
                declaration_path
                + (
                    mapping_segment("AssociationPropertyMetadata"),
                    mapping_segment("Parameter"),
                ),
                False,
            )
        if row_field == "Parameters" and isinstance(child, Mapping):
            # MetaList/List[Parameters] represent each array item directly as
            # this Parameters map.  Keep a virtual row declaration so normal
            # child traversal yields the real terminal declaration/path.
            return {"AssociationPropertyMetadata": {"Parameters": child}}, declaration_path, True
        return None

    def _emit_reference_limitation(self, name: str, path: RosPath, reference_context: str) -> None:
        if not any(character in name for character in ".[]{}"):
            self._emit(
                "ROS5305",
                Severity.WARNING,
                _(
                    "Metadata reference {} has more than one possible lookup scope."
                ).format(name),
                _(
                    "It may resolve from template Parameters or component-local data. The runtime component is "
                    "unknown, so local validation leaves the reference unchecked."
                ),
                path,
                subject=name,
                stable_args=("runtime-reference-context", reference_context, name),
                suggestion=_(
                    "Do not change the template based on this limitation alone; verify the reference behavior in "
                    "the target ROS form if needed."
                ),
            )
            return
        self._emit(
            "ROS5305",
            Severity.WARNING,
            _("AssociationPropertyMetadata field-path reference cannot be resolved completely."),
            _("The reference depends on a nested form or MetaList row context."),
            path,
            subject=name,
            stable_args=("field-path", reference_context, name),
        )

    def _reference_error(
        self,
        encoded: str,
        path: RosPath,
        reason: str,
        *,
        name: str | None = None,
        uncertain: bool = False,
    ) -> None:
        localized_reason = self._localized_reference_reason(reason)
        self._emit(
            "ROS5305" if uncertain else "ROS1306",
            Severity.WARNING if uncertain else Severity.ERROR,
            _("AssociationPropertyMetadata reference cannot be proven valid locally.")
            if uncertain
            else _("AssociationPropertyMetadata reference is invalid."),
            _("Consumer reachability is unknown; the possible encoding problem is: {}.").format(localized_reason)
            if uncertain
            else _("The target consumer cannot resolve this encoding: {}.").format(localized_reason),
            path,
            subject=name or encoded,
            stable_args=(reason, name or encoded),
            expected=_("a valid reference in the declared consumer context"),
            actual=encoded,
        )

    def _warn_reference_type(
        self,
        value: Any,
        schema: Mapping[str, Any],
        path: RosPath,
        *,
        explicit_name: str | None = None,
        reference_context: str,
        reference_parameters: Mapping[Any, Any] | None,
        reference_declaration_path: RosPath | None,
    ) -> None:
        if not isinstance(value, str):
            return
        exact_parameter = False
        if explicit_name is not None:
            name = explicit_name
        elif "whole-value-reference" in self._schema_reference_parsers(schema):
            classified = self._classify_whole_reference(
                value,
                reference_context,
                reference_parameters,
                reference_declaration_path,
            )
            if classified is None or classified[0] == "env":
                return
            reference_kind, name = classified
            exact_parameter = reference_kind == "parameter"
        else:
            match = _REFERENCE.fullmatch(value)
            name = match.group(1).strip() if match else value
        if not name:
            return
        if exact_parameter:
            resolution = self._resolve_exact_parameter_declaration(
                name,
                reference_context,
                reference_parameters,
                reference_declaration_path,
            )
        else:
            parsed = self._parse_reference_path(name)
            if parsed is None or parsed[0]:
                return
            resolution = self._resolve_reference_declaration(
                name,
                reference_context,
                reference_parameters,
                reference_declaration_path,
            )
        if resolution.declaration is None or resolution.declaration_path is None:
            return
        expected = schema.get("type")
        if expected not in {"array", "boolean", "integer", "number", "object", "string"}:
            return
        parameter_type = resolution.declaration.get("Type")
        terminal_type = {
            "Boolean": "boolean",
            "CommaDelimitedList": "array",
            "Integer": "integer",
            "Json": "object",
            "List": "array",
            "Number": "number",
            "NumberPicker": "number",
            "String": "string",
        }.get(parameter_type if isinstance(parameter_type, str) else "")
        expected_display = str(expected)
        if resolution.projected_array:
            actual = "array<{}>".format(terminal_type) if terminal_type is not None else "array"
            if expected == "array":
                item_schema = schema.get("items")
                expected_item_types = self._schema_declared_types(item_schema)
                if (
                    terminal_type is None
                    or not expected_item_types
                    or any(self._reference_types_compatible(terminal_type, item) for item in expected_item_types)
                ):
                    return
                expected_display = "array<{}>".format(" | ".join(sorted(expected_item_types)))
        else:
            actual = terminal_type
        if actual is None or self._reference_types_compatible(actual, expected):
            return
        declaration_path = resolution.declaration_path
        node = self.source_map.node_for(declaration_path)
        related = (
            RelatedLocation(_("referenced Parameter declaration"), node.span if node else None, declaration_path),
        )
        self._emit(
            "ROS5302",
            Severity.WARNING,
            _("AssociationPropertyMetadata reference type is suspicious."),
            _("The referenced Parameter resolves as {}, while this consumer expects {}.").format(
                actual,
                expected_display,
            ),
            path,
            subject=name,
            stable_args=("reference-type", name, str(actual), expected_display),
            expected=expected_display,
            actual=str(actual if resolution.projected_array else parameter_type),
            related_locations=related,
        )

    @classmethod
    def _schema_declared_types(cls, schema: Any) -> set[str]:
        if not isinstance(schema, Mapping):
            return set()
        result = {schema_type} if isinstance((schema_type := schema.get("type")), str) else set()
        any_of = schema.get("anyOf")
        if isinstance(any_of, (list, tuple)):
            for candidate in any_of:
                result.update(cls._schema_declared_types(candidate))
        return result

    @staticmethod
    def _reference_types_compatible(actual: str, expected: Any) -> bool:
        return actual == expected or actual == "integer" and expected == "number"

    def _validate_common_semantics(
        self,
        metadata: Mapping[Any, Any],
        path: RosPath,
        *,
        reference_context: str,
        reference_parameters: Mapping[Any, Any] | None,
        reference_declaration_path: RosPath | None,
    ) -> None:
        for field in ("Visible", "ReadOnly", "Required"):
            wrapper = metadata.get(field)
            if isinstance(wrapper, Mapping) and "Condition" in wrapper:
                self._validate_condition(
                    wrapper["Condition"],
                    path + (mapping_segment(field), mapping_segment("Condition")),
                    root_string=True,
                    reference_context=reference_context,
                    reference_parameters=reference_parameters,
                    reference_declaration_path=reference_declaration_path,
                )
        for field in ("AllowedValues", "Value", "MinValue", "MaxValue"):
            values = metadata.get(field)
            if not isinstance(values, list):
                continue
            for index, item in enumerate(values):
                if isinstance(item, Mapping) and "Condition" in item:
                    condition_item = cast(Mapping[Any, Any], item)
                    self._validate_condition(
                        condition_item["Condition"],
                        path + (mapping_segment(field), SequenceIndexSegment(index), mapping_segment("Condition")),
                        root_string=True,
                        reference_context=reference_context,
                        reference_parameters=reference_parameters,
                        reference_declaration_path=reference_declaration_path,
                    )
        dynamic = metadata.get("DynamicValue")
        if isinstance(dynamic, list):
            injected_symbols = self._dynamic_condition_injected_symbols()
            for index, item in enumerate(dynamic):
                if isinstance(item, Mapping) and "Condition" in item:
                    condition_item = cast(Mapping[Any, Any], item)
                    self._validate_condition(
                        condition_item["Condition"],
                        path
                        + (mapping_segment("DynamicValue"), SequenceIndexSegment(index), mapping_segment("Condition")),
                        root_string=True,
                        reference_context=reference_context,
                        reference_parameters=reference_parameters,
                        reference_declaration_path=reference_declaration_path,
                        injected_symbols=injected_symbols,
                    )

    def _dynamic_condition_injected_symbols(self) -> set[str]:
        properties = self.specs.common_metadata.get("properties", {})
        dynamic = properties.get("DynamicValue") if isinstance(properties, Mapping) else None
        any_of = dynamic.get("anyOf", ()) if isinstance(dynamic, Mapping) else ()
        for candidate in any_of if isinstance(any_of, (list, tuple)) else ():
            if not isinstance(candidate, Mapping) or candidate.get("type") != "array":
                continue
            items = candidate.get("items")
            item_properties = items.get("properties", {}) if isinstance(items, Mapping) else {}
            condition = item_properties.get("Condition") if isinstance(item_properties, Mapping) else None
            symbols = condition.get("x-ore-injected-symbols") if isinstance(condition, Mapping) else None
            if isinstance(symbols, (list, tuple)):
                return {symbol for symbol in symbols if isinstance(symbol, str)}
        return set()

    def _validate_condition(
        self,
        condition: Any,
        path: RosPath,
        *,
        root_string: bool,
        reference_context: str,
        reference_parameters: Mapping[Any, Any] | None,
        reference_declaration_path: RosPath | None,
        injected_symbols: set[str] | None = None,
    ) -> None:
        if isinstance(condition, str):
            if root_string:
                self._validate_definition_path(
                    condition,
                    path,
                    reference_context=reference_context,
                    reference_parameters=reference_parameters,
                    reference_declaration_path=reference_declaration_path,
                    injected_symbols=injected_symbols,
                )
            else:
                self._condition_error(condition, path, "nested And/Or/Not operands must be condition objects")
            return
        if not isinstance(condition, Mapping) or not condition:
            self._condition_error(condition, path, "condition must be a non-empty String or object")
            return
        properties, key_order = _js_mapping_properties(condition)
        first_key = key_order[0]
        supported = {"Fn::And", "Fn::Contains", "Fn::Equals", "Fn::Not", "Fn::Or", "Fn::Select"}
        if first_key not in supported:
            self._condition_error(condition, path, "the first function key is unsupported")
            return
        for ignored_key in key_order[1:]:
            self._emit(
                "ROS5302",
                Severity.WARNING,
                _("Condition contains a function key that the target form ignores."),
                _("The target form evaluates only the first object key, {}.").format(first_key),
                path + (mapping_segment(ignored_key),),
                subject=str(ignored_key),
                stable_args=("ignored-condition-key", str(first_key), str(ignored_key)),
            )
        value = properties[first_key]
        value_path = path + (mapping_segment(first_key),)
        if first_key in {"Fn::Equals", "Fn::Contains"}:
            if not isinstance(value, list) or len(value) != 2:
                self._condition_error(value, value_path, "{} requires exactly two arguments".format(first_key))
            else:
                for index, child in enumerate(value):
                    self._validate_condition_operand(
                        child,
                        value_path + (SequenceIndexSegment(index),),
                        reference_context=reference_context,
                        reference_parameters=reference_parameters,
                        reference_declaration_path=reference_declaration_path,
                        injected_symbols=injected_symbols,
                    )
            return
        if first_key in {"Fn::And", "Fn::Or"}:
            if not isinstance(value, list):
                self._condition_error(value, value_path, "{} requires an array".format(first_key))
                return
            for index, child in enumerate(value):
                self._validate_condition(
                    child,
                    value_path + (SequenceIndexSegment(index),),
                    root_string=False,
                    reference_context=reference_context,
                    reference_parameters=reference_parameters,
                    reference_declaration_path=reference_declaration_path,
                    injected_symbols=injected_symbols,
                )
            return
        if first_key == "Fn::Not":
            if not isinstance(value, Mapping) or len(value) != 1:
                self._condition_error(value, value_path, "Fn::Not requires one single-key condition object")
            else:
                self._validate_condition(
                    value,
                    value_path,
                    root_string=False,
                    reference_context=reference_context,
                    reference_parameters=reference_parameters,
                    reference_declaration_path=reference_declaration_path,
                    injected_symbols=injected_symbols,
                )
            return
        self._validate_select_arguments(
            value,
            value_path,
            reference_context=reference_context,
            reference_parameters=reference_parameters,
            reference_declaration_path=reference_declaration_path,
            injected_symbols=injected_symbols,
        )

    def _validate_condition_operand(
        self,
        value: Any,
        path: RosPath,
        *,
        reference_context: str,
        reference_parameters: Mapping[Any, Any] | None,
        reference_declaration_path: RosPath | None,
        injected_symbols: set[str] | None = None,
    ) -> None:
        if isinstance(value, str):
            match = _FRONTEND_REFERENCE.fullmatch(value)
            if match is not None:
                self._validate_exact_parameter_reference(
                    match.group(1),
                    value,
                    path,
                    reference_context,
                    reference_parameters=reference_parameters,
                    reference_declaration_path=reference_declaration_path,
                    injected_symbols=injected_symbols,
                )
            return
        if isinstance(value, Mapping) and "Fn::Select" in value:
            self._validate_select_arguments(
                value["Fn::Select"],
                path + (mapping_segment("Fn::Select"),),
                reference_context=reference_context,
                reference_parameters=reference_parameters,
                reference_declaration_path=reference_declaration_path,
                injected_symbols=injected_symbols,
            )

    def _validate_select_arguments(
        self,
        value: Any,
        value_path: RosPath,
        *,
        reference_context: str,
        reference_parameters: Mapping[Any, Any] | None,
        reference_declaration_path: RosPath | None,
        injected_symbols: set[str] | None = None,
    ) -> None:
        if not isinstance(value, list):
            self._condition_error(value, value_path, "Fn::Select requires an array")
            return
        param_ref = value[0] if value else None
        key = value[1] if len(value) >= 2 else None
        if len(value) > 2:
            for index in range(2, len(value)):
                self._emit(
                    "ROS5302",
                    Severity.WARNING,
                    _("Fn::Select contains an argument that the target form ignores."),
                    _("Only the first two arguments are destructured by the current runtime."),
                    value_path + (SequenceIndexSegment(index),),
                    subject=str(index),
                    stable_args=("select-tail", str(index)),
                )
        if not js_truthy(param_ref) or not js_truthy(key):
            # The target form returns a resolved undefined primitive before consulting the
            # reference when either destructured argument is missing or empty.
            return
        if not all(isinstance(item, str) for item in value[:2]):
            self._condition_error(value, value_path, "Fn::Select uses its first two String arguments")
        else:
            raw_reference = value[0]
            match = _FRONTEND_REFERENCE.fullmatch(raw_reference)
            reference_name = match.group(1) if match is not None else raw_reference
            self._validate_exact_parameter_reference(
                reference_name,
                raw_reference,
                value_path + (SequenceIndexSegment(0),),
                reference_context,
                reference_parameters=reference_parameters,
                reference_declaration_path=reference_declaration_path,
                injected_symbols=injected_symbols,
            )

    def _condition_error(self, value: Any, path: RosPath, reason: str) -> None:
        self._emit(
            "ROS1305",
            Severity.ERROR,
            _("AssociationPropertyMetadata Condition is invalid."),
            _("The condition AST is incompatible with the target form: {}.").format(
                self._localized_condition_reason(reason)
            ),
            path,
            subject=reason,
            stable_args=("condition", reason),
            expected=_("a supported ParameterMetadataCondition"),
            actual=self._actual(value),
        )

    def _validate_definition_path(
        self,
        value: str,
        path: RosPath,
        *,
        reference_context: str,
        reference_parameters: Mapping[Any, Any] | None,
        reference_declaration_path: RosPath | None,
        injected_symbols: set[str] | None,
    ) -> None:
        metadata = self.template.get("Metadata")
        profile_results: list[tuple[str, tuple[bool, Any] | None]] = []
        if isinstance(metadata, Mapping):
            for key in ("ALIYUN::ROS::Interface", "APSARA::ROS::Interface"):
                if key not in metadata:
                    continue
                interface = metadata.get(key)
                definitions = interface.get("Definitions") if isinstance(interface, Mapping) else None
                if isinstance(definitions, Mapping):
                    profile_results.append(
                        (
                            key,
                            self._definition_path_value(
                                definitions,
                                value,
                                (
                                    mapping_segment("Metadata"),
                                    mapping_segment(key),
                                    mapping_segment("Definitions"),
                                ),
                            ),
                        )
                    )
                else:
                    profile_results.append((key, None))
        inspectable = [result for _, result in profile_results if result is not None]
        if not profile_results or not inspectable:
            self._emit(
                "ROS5305",
                Severity.WARNING,
                _("Condition Definitions path cannot be resolved locally."),
                _("The template does not provide a statically inspectable ROS Interface Definitions object."),
                path,
                subject=value,
                stable_args=("definitions-unavailable", value),
            )
            return
        found_results = [
            (profile, result[1]) for profile, result in profile_results if result is not None and result[0]
        ]
        has_unavailable = any(result is None for _, result in profile_results)
        has_missing = any(result is not None and not result[0] for _, result in profile_results)
        values_differ = bool(found_results) and any(
            not _same_definition_value(found_results[0][1], resolved) for _, resolved in found_results[1:]
        )
        if found_results and not has_unavailable and not has_missing and not values_differ:
            resolved = found_results[0][1]
            if not isinstance(resolved, Mapping):
                self._condition_error(resolved, path, "Definitions path must resolve to a condition object")
                return
            self._validate_condition(
                resolved,
                path,
                root_string=False,
                reference_context=reference_context,
                reference_parameters=reference_parameters,
                reference_declaration_path=reference_declaration_path,
                injected_symbols=injected_symbols,
            )
            return
        if found_results:
            self._emit(
                "ROS5305",
                Severity.WARNING,
                _("Condition Definitions path cannot be resolved locally."),
                _("The ALIYUN and APSARA environment profiles resolve this path differently."),
                path,
                subject=value,
                stable_args=(
                    "definitions-profile-dependent",
                    value,
                    *(
                        "{}={}".format(
                            profile,
                            "unavailable" if result is None else "found" if result[0] else "missing",
                        )
                        for profile, result in profile_results
                    ),
                ),
            )
            return
        self._reference_error(value, path, "Definitions path does not exist", name=value)

    def _definition_path_value(
        self,
        definitions: Mapping[Any, Any],
        value: str,
        definitions_path: RosPath,
    ) -> tuple[bool, Any]:
        # Field-path parsing first treats the complete string as an exact root
        # key, even when it contains dots or brackets.
        found, exact, _ = self._definition_mapping_property(definitions, definitions_path, value)
        if found:
            return True, exact
        current: Any = definitions
        current_path = definitions_path
        segments = _lodash_path_segments(value)
        if not segments:
            return False, None
        for segment in segments:
            if isinstance(current, Mapping):
                found, current, current_path = self._definition_mapping_property(
                    current,
                    current_path,
                    segment,
                )
                if not found:
                    return False, None
                continue
            if isinstance(current, (list, tuple)):
                if segment == "length":
                    current = len(current)
                    continue
                if re.fullmatch(r"(?:0|[1-9][0-9]*)", segment) is None:
                    return False, None
                index = int(segment)
                if index >= len(current):
                    return False, None
                current = current[index]
                current_path = current_path + (SequenceIndexSegment(index),)
                continue
            if isinstance(current, str):
                units = _js_utf16_units(current)
                if segment == "length":
                    current = len(units)
                    continue
                if re.fullmatch(r"(?:0|[1-9][0-9]*)", segment) is None:
                    return False, None
                index = int(segment)
                if index >= len(units):
                    return False, None
                current = units[index]
                continue
            return False, None
        return True, current

    def _definition_mapping_property(
        self,
        value: Mapping[Any, Any],
        value_path: RosPath,
        key: str,
    ) -> tuple[bool, Any, RosPath]:
        """Read one JS-visible key without losing YAML ``true/1`` or ``false/0`` siblings."""

        return self._source_mapping_property(value, value_path, key)

    def _validate_nested_parameters(
        self,
        metadata: Mapping[Any, Any],
        path: RosPath,
        resolution: _ComponentResolution,
        *,
        depth: int,
        reference_context: str,
        reference_parameters: Mapping[Any, Any] | None,
        reference_declaration_path: RosPath | None,
    ) -> None:
        for field in ("Parameter", "Parameters"):
            if field not in metadata:
                continue
            reachability, nested_context = self._nested_field_reachability(resolution, field)
            field_path = path + (mapping_segment(field),)
            if reachability == ConsumerReachability.NOT_REACHED:
                continue
            if reachability == ConsumerReachability.UNKNOWN or nested_context is None:
                self._emit(
                    "ROS5305",
                    Severity.WARNING,
                    _("AssociationPropertyMetadata nested container has runtime-dependent reachability."),
                    _(
                        "Only some possible form components consume this Parameter/Parameters field; "
                        "its nested declarations are not rejected locally."
                    ),
                    field_path,
                    subject=field,
                    stable_args=("nested-container-reachability", field, *resolution.possible_components),
                )
                continue
            if field == "Parameter":
                nested = metadata.get(field)
                if not isinstance(nested, Mapping):
                    continue
                keep_scope = nested_context == reference_context
                self._validate_parameter(
                    nested,
                    field_path,
                    depth=depth + 1,
                    reference_context=nested_context,
                    reference_parameters=reference_parameters if keep_scope else None,
                    reference_declaration_path=reference_declaration_path if keep_scope else None,
                )
                continue
            nested_map = metadata.get(field)
            if not isinstance(nested_map, Mapping):
                continue
            for _child_name, child, child_path in self._source_mapping_items(nested_map, field_path):
                if isinstance(child, Mapping):
                    self._validate_parameter(
                        child,
                        child_path,
                        depth=depth + 1,
                        reference_context=nested_context,
                        reference_parameters=nested_map,
                        reference_declaration_path=field_path,
                    )

    def _nested_field_reachability(
        self,
        resolution: _ComponentResolution,
        field: str,
    ) -> tuple[ConsumerReachability, str | None]:
        contexts: set[str] = set()
        consumer_count = 0
        for component_name in resolution.possible_components:
            component = self.specs.component(component_name)
            properties = component.metadata.get("properties", {}) if component is not None else {}
            schema = properties.get(field) if isinstance(properties, Mapping) else None
            if not isinstance(schema, Mapping) or schema.get("x-ore-nested-parameter") is not True:
                continue
            consumer_count += 1
            context = schema.get("x-ore-reference-context") if isinstance(schema, Mapping) else None
            if isinstance(context, str):
                contexts.add(context)
        if consumer_count == 0:
            return ConsumerReachability.NOT_REACHED, None
        if consumer_count == len(resolution.possible_components) and len(contexts) == 1:
            return ConsumerReachability.REACHED, next(iter(contexts))
        return ConsumerReachability.UNKNOWN, next(iter(contexts)) if len(contexts) == 1 else None

    def _validate_auto_complete(
        self,
        metadata: Mapping[Any, Any],
        path: RosPath,
        reachability: AutoCompleteConsumerReachability | None,
    ) -> None:
        classes = metadata.get("CharacterClasses")
        pattern = metadata.get("Pattern")
        if isinstance(pattern, str) and not (isinstance(classes, list) and classes):
            self._emit(
                "ROS5305",
                Severity.WARNING,
                _("AutoCompleteInput Pattern syntax is not checked locally."),
                _(
                    "The target form compiles Pattern as ECMAScript RegExp; "
                    "Python regular expressions are not equivalent."
                ),
                path + (mapping_segment("Pattern"),),
                subject="Pattern",
                stable_args=("ecma262-regex",),
            )
        has_character_classes = isinstance(classes, list) and bool(classes)
        raw_length = metadata.get("Length")
        if isinstance(raw_length, (int, float)) and not isinstance(raw_length, bool):
            if not math.isfinite(float(raw_length)) or raw_length <= 0 or float(raw_length).is_integer() is False:
                if has_character_classes:
                    normalized_length: int | str = self._js_array_length(raw_length)
                    normalized_length_stable = str(normalized_length)
                elif not js_truthy(raw_length):
                    normalized_length = _("unsliced generated identifier (Length is falsy in JavaScript)")
                    normalized_length_stable = "unsliced-generated-identifier"
                else:
                    slice_end = self._js_slice_end(raw_length)
                    normalized_length = (
                        slice_end
                        if slice_end >= 0
                        else _("generated identifier truncated at position {}").format(slice_end)
                    )
                    normalized_length_stable = str(normalized_length)
                self._emit(
                    "ROS5302",
                    Severity.WARNING,
                    _("AutoCompleteInput Length is normalized non-intuitively by JavaScript."),
                    _("Effective generated Length is {} after JavaScript array/slice conversion.").format(
                        normalized_length
                    ),
                    path + (mapping_segment("Length"),),
                    subject="Length",
                    stable_args=("length-normalization", str(raw_length), normalized_length_stable),
                    expected=_("a positive integer for predictable output"),
                    actual=str(raw_length),
                )
        if not isinstance(classes, list) or not classes:
            return
        if "Pattern" in metadata:
            self._emit(
                "ROS5302",
                Severity.WARNING,
                _("AutoCompleteInput ignores Pattern when CharacterClasses is non-empty."),
                _("The character-class generation branch does not compile Pattern."),
                path + (mapping_segment("Pattern"),),
                subject="Pattern",
                stable_args=("pattern-with-character-classes",),
            )
        length = metadata.get("Length", 8)
        effective_length = self._js_array_length(length)

        valid_minimums = True
        minimum_sum = 0
        ordinary_present = False
        special_entries: list[tuple[int, Mapping[Any, Any]]] = []
        valid_classes = _auto_complete_character_class_values(self.specs)
        prior_fills: list[tuple[int, frozenset[int]]] = []
        remaining_upper_bound = effective_length
        for index, item in enumerate(classes):
            if not isinstance(item, Mapping):
                continue
            class_item = cast(Mapping[Any, Any], item)
            if class_item.get("Class") not in valid_classes:
                continue
            item_path = path + (mapping_segment("CharacterClasses"), SequenceIndexSegment(index))
            class_name = class_item["Class"]
            if class_name != "specialCharacter":
                ordinary_present = True
                for ignored in ("SpecialCharacters", "Start", "End"):
                    if ignored in class_item:
                        self._emit(
                            "ROS5302",
                            Severity.WARNING,
                            _("AutoCompleteInput ignores a special-character-only field."),
                            _("{} is read only when Class is specialCharacter.").format(ignored),
                            item_path + (mapping_segment(ignored),),
                            subject=ignored,
                            stable_args=("non-special-ignored", str(class_name), ignored),
                        )
            else:
                special_entries.append((index, class_item))

            if "Min" not in class_item:
                valid_minimums = False
                if "min" not in class_item and len(classes) > 1:
                    self._emit(
                        "ROS5302",
                        Severity.WARNING,
                        _("AutoCompleteInput CharacterClass does not guarantee a minimum count."),
                        _("With multiple character classes, missing Min does not reserve characters for this class."),
                        item_path + (mapping_segment("Min"),),
                        subject="Min",
                        stable_args=("missing-min", str(index)),
                    )
                continue
            minimum = class_item.get("Min")
            if not isinstance(minimum, (int, float)) or isinstance(minimum, bool) or not math.isfinite(float(minimum)):
                valid_minimums = False
                continue
            fills_runtime = class_name != "specialCharacter" or js_truthy(class_item.get("SpecialCharacters"))
            excluded_count = 0
            if class_name == "specialCharacter" and fills_runtime and effective_length > 0:
                excluded_positions = set()
                if class_item.get("Start") is False:
                    excluded_positions.add(0)
                if class_item.get("End") is False:
                    excluded_positions.add(effective_length - 1)
                excluded_count = len(excluded_positions)
            if minimum < 0 or float(minimum).is_integer() is False:
                valid_minimums = False
                if fills_runtime:
                    self._emit(
                        "ROS5302",
                        Severity.WARNING,
                        _("AutoCompleteInput Min fills every available position."),
                        _(
                            "A negative or fractional counter never reaches zero; generation stops only after "
                            "available positions are exhausted."
                        ),
                        item_path + (mapping_segment("Min"),),
                        subject="Min",
                        stable_args=("non-integer-min", str(index), str(minimum)),
                        expected=_("a non-negative integer for predictable minimum semantics"),
                        actual=str(minimum),
                    )
                    if class_name == "specialCharacter":
                        remaining_upper_bound = min(remaining_upper_bound, excluded_count)
                    else:
                        remaining_upper_bound = 0
            else:
                minimum_value = int(minimum)
                if fills_runtime:
                    minimum_sum += minimum_value
                    allowed_positions = set(range(effective_length))
                    if class_name == "specialCharacter":
                        if class_item.get("Start") is False:
                            allowed_positions.discard(0)
                        if class_item.get("End") is False:
                            allowed_positions.discard(effective_length - 1)
                    required_from_prior = sum(
                        max(0, prior_minimum - len(prior_allowed - allowed_positions))
                        for prior_minimum, prior_allowed in prior_fills
                    )
                    if (
                        required_from_prior > 0
                        and len(allowed_positions) < effective_length
                        and minimum_value + required_from_prior > len(allowed_positions)
                    ):
                        self._emit_auto_semantic(
                            _("AutoCompleteInput minimum cannot be satisfied after earlier CharacterClasses."),
                            _("Earlier fills require at least {} of this entry's {} available positions.").format(
                                required_from_prior,
                                len(allowed_positions),
                            ),
                            item_path + (mapping_segment("Min"),),
                            reachability,
                            subject="shared-character-capacity",
                            stable_args=(
                                "shared-capacity",
                                str(index),
                                str(minimum_value),
                                str(required_from_prior),
                                str(len(allowed_positions)),
                            ),
                            expected=_("a minimum satisfiable after earlier fills"),
                            actual=str(minimum_value),
                        )
                    prior_fills.append((minimum_value, frozenset(allowed_positions)))
                    if class_name == "specialCharacter":
                        remaining_upper_bound = min(
                            remaining_upper_bound,
                            max(excluded_count, remaining_upper_bound - minimum_value),
                        )
                    else:
                        remaining_upper_bound = max(remaining_upper_bound - minimum_value, 0)

        if valid_minimums and minimum_sum > effective_length:
            self._emit_auto_semantic(
                _("AutoCompleteInput minimum character counts exceed Length."),
                _("The sum of CharacterClasses.Min is {}, but the effective Length is {}.").format(
                    minimum_sum,
                    effective_length,
                ),
                path + (mapping_segment("CharacterClasses"),),
                reachability,
                subject="character-capacity",
                stable_args=("capacity", str(minimum_sum), str(effective_length)),
                expected=_("sum of Min no greater than {}").format(effective_length),
                actual=str(minimum_sum),
            )

        truthy_special_indices = [
            index
            for index, item in special_entries
            if isinstance(item.get("SpecialCharacters"), str) and item.get("SpecialCharacters")
        ]
        last_truthy_special = truthy_special_indices[-1] if truthy_special_indices else None
        has_usable_pool = ordinary_present or bool(truthy_special_indices)
        for index, item in special_entries:
            item_path = path + (mapping_segment("CharacterClasses"), SequenceIndexSegment(index))
            characters = item.get("SpecialCharacters")
            minimum = item.get("Min")
            requires_characters = (
                isinstance(minimum, (int, float)) and not isinstance(minimum, bool) and minimum > 0
            ) or (not has_usable_pool and effective_length > 0)
            if not (isinstance(characters, str) and characters):
                for ignored in ("Start", "End"):
                    if ignored in item:
                        self._emit(
                            "ROS5302",
                            Severity.WARNING,
                            _("AutoCompleteInput ignores a special-character boundary flag."),
                            _(
                                "The runtime does not retain specialCharacter configuration when SpecialCharacters "
                                "is empty."
                            ),
                            item_path + (mapping_segment(ignored),),
                            subject=ignored,
                            stable_args=("empty-special-ignored", str(index), ignored),
                        )
                if requires_characters:
                    self._emit_auto_semantic(
                        _("AutoCompleteInput requires non-empty SpecialCharacters."),
                        _("This specialCharacter branch must fill at least one generated position."),
                        item_path + (mapping_segment("SpecialCharacters"),),
                        reachability,
                        subject="SpecialCharacters",
                        stable_args=("special-characters-required", str(index)),
                        expected=_("non-empty String"),
                        actual=self._actual(characters),
                    )
                continue
            excluded_positions = set()
            if item.get("Start") is False and effective_length > 0:
                excluded_positions.add(0)
            if item.get("End") is False and effective_length > 0:
                excluded_positions.add(effective_length - 1)
            available = max(effective_length - len(excluded_positions), 0)
            if isinstance(minimum, int) and not isinstance(minimum, bool) and minimum >= 0 and minimum > available:
                self._emit_auto_semantic(
                    _("AutoCompleteInput special-character minimum exceeds available positions."),
                    _("Start/End restrictions leave {} positions for {} required special characters.").format(
                        available,
                        minimum,
                    ),
                    item_path + (mapping_segment("Min"),),
                    reachability,
                    subject="special-capacity",
                    stable_args=("special-capacity", str(index), str(minimum), str(available)),
                    expected=_("Min no greater than {}").format(available),
                    actual=str(minimum),
                )
            if (
                index == last_truthy_special
                and isinstance(characters, str)
                and characters
                and (item.get("Start") is False or item.get("End") is False)
                and remaining_upper_bound > 0
            ):
                boundary = "Start" if item.get("Start") is False else "End"
                detail = (
                    _(
                        "join() can produce a string shorter than Length because the remaining-character pool becomes "
                        "empty at the restricted boundary and stays empty."
                    )
                    if not ordinary_present
                    else _(
                        "The mutable remaining-character pool narrows to ordinary classes at the restricted boundary "
                        "and does not restore special characters later."
                    )
                )
                self._emit(
                    "ROS5302",
                    Severity.WARNING,
                    _("AutoCompleteInput boundary restriction persists for later positions."),
                    detail,
                    item_path + (mapping_segment(boundary),),
                    subject=boundary,
                    stable_args=("persistent-character-pool", str(index), boundary, str(ordinary_present)),
                )

    def _emit_auto_semantic(
        self,
        summary: str,
        detail: str,
        path: RosPath,
        reachability: AutoCompleteConsumerReachability | None,
        **kwargs: Any,
    ) -> None:
        states = (
            (reachability.raw_initializer, reachability.component_effect)
            if reachability is not None
            else (ConsumerReachability.UNKNOWN,)
        )
        if ConsumerReachability.REACHED in states:
            code = "ROS1307"
            severity = Severity.ERROR
        elif all(state == ConsumerReachability.NOT_REACHED for state in states):
            code = "ROS5302"
            severity = Severity.WARNING
            detail = _("All statically reachable generation consumers are skipped. {} ").format(detail)
        else:
            code = "ROS5305"
            severity = Severity.WARNING
            detail = _("Generation consumer reachability is unknown. {} ").format(detail)
        self._emit(code, severity, summary, detail, path, **kwargs)

    @staticmethod
    def _js_array_length(value: Any) -> int:
        return max(_js_integer(value), 0)

    @staticmethod
    def _js_slice_end(value: Any) -> int:
        return _js_integer(value)

    @staticmethod
    def _actual(value: Any) -> str:
        if value is None:
            return "null"
        if isinstance(value, bool):
            return "true" if value else "false"
        if isinstance(value, str):
            return value
        if isinstance(value, list):
            return "array"
        if isinstance(value, Mapping):
            return "object"
        return str(value)

    @classmethod
    def _expected(cls, schema: Mapping[str, Any]) -> str:
        if "enum" in schema:
            return " | ".join(str(item) for item in schema["enum"])
        if "const" in schema:
            return str(schema["const"])
        if "anyOf" in schema:
            return _("one of: {}").format(" | ".join(cls._expected(item) for item in schema["anyOf"]))
        schema_type = schema.get("type")
        return str(schema_type) if schema_type is not None else _("any value")

    @classmethod
    def _expected_stable(cls, schema: Mapping[str, Any]) -> str:
        if "enum" in schema:
            return " | ".join(str(item) for item in schema["enum"])
        if "const" in schema:
            return str(schema["const"])
        if "anyOf" in schema:
            return " or ".join(cls._expected_stable(item) for item in schema["anyOf"])
        return str(schema.get("type", "any value"))

    @staticmethod
    def _unavailable_association_property_detail(scope: str) -> str:
        if scope == "OOS-only":
            return _("This AssociationProperty value is available only in the OOS parameter form.")
        return _("This AssociationProperty value is unavailable in the stock ROS parameter form.")

    @staticmethod
    def _localized_reference_reason(reason: str) -> str:
        if reason == "empty reference":
            return _("the reference is empty")
        if reason == "environment references are not allowed by this consumer":
            return _("environment references are not allowed by this consumer")
        if reason == "parameter references are not allowed by this consumer":
            return _("Parameter references are not allowed by this consumer")
        if reason == "field-path references are not allowed by this consumer":
            return _("field-path references are not allowed by this consumer")
        if reason == "malformed field path":
            return _("the field path is malformed")
        if reason == "field paths support one top-level array projection followed by a child path":
            return _("field paths support one top-level array projection followed by a child path")
        if reason == "Parameter does not exist":
            return _("the Parameter does not exist")
        if reason == "nested Parameter does not exist":
            return _("the nested Parameter does not exist")
        if reason == "MetaList row field does not exist":
            return _("the MetaList row field does not exist")
        if reason == "referenced Parameter declaration is invalid":
            return _("the referenced Parameter declaration is invalid")
        if reason == "field path traverses a scalar Parameter":
            return _("the field path traverses a scalar Parameter")
        if reason == "nested Parameter field does not exist":
            return _("the nested Parameter field does not exist")
        if reason == "referenced nested Parameter declaration is invalid":
            return _("the referenced nested Parameter declaration is invalid")
        if reason == "Definitions path does not exist":
            return _("the Definitions path does not exist")
        match = re.fullmatch(r"(.+) references are not allowed by this consumer", reason)
        if match is not None:
            return _("{} references are not allowed by this consumer").format(match.group(1))
        return _("the reference is invalid")

    @staticmethod
    def _localized_condition_reason(reason: str) -> str:
        if reason == "nested And/Or/Not operands must be condition objects":
            return _("nested And/Or/Not operands must be condition objects")
        if reason == "condition must be a non-empty String or object":
            return _("the condition must be a non-empty String or object")
        if reason == "the first function key is unsupported":
            return _("the first function key is unsupported")
        if reason == "Fn::Not requires one single-key condition object":
            return _("Fn::Not requires one condition object containing a single key")
        if reason == "Fn::Select uses its first two String arguments":
            return _("Fn::Select requires its first two arguments to be Strings")
        if reason == "Definitions path must resolve to a condition object":
            return _("the Definitions path must resolve to a condition object")
        match = re.fullmatch(r"(.+) requires exactly two arguments", reason)
        if match is not None:
            return _("{} requires exactly two arguments").format(match.group(1))
        match = re.fullmatch(r"(.+) requires an array", reason)
        if match is not None:
            return _("{} requires an array").format(match.group(1))
        return _("the condition is invalid")
