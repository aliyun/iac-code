"""Static ROS value types used by the local template validator.

The type lattice deliberately separates a value's ROS type from whether its
runtime value is known.  In particular, a dynamic String is still a String;
``UnknownType`` is reserved for values that cannot be placed in the ROS value
domain at all.
"""

from __future__ import annotations

import math
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from enum import Enum
from typing import Any

from iac_code.i18n import _


class TypeKind(str, Enum):
    STRING = "String"
    INTEGER = "Integer"
    NUMBER = "Number"
    BOOLEAN = "Boolean"
    NULL = "Null"
    NO_VALUE = "NoValue"
    BINARY = "Binary"
    HASHABLE_SCALAR = "HashableScalar"
    LIST = "List"
    MAP = "Map"
    ANY = "AnyValue"
    NON_NULL_ANY = "NonNullAnyValue"
    UNKNOWN = "UnknownType"
    JSON_DECODED = "JsonDecodedValue"
    UNION = "Union"


@dataclass(frozen=True)
class RosType:
    kind: TypeKind
    item_type: RosType | None = None
    key_type: RosType | None = None
    value_type: RosType | None = None
    members: tuple[RosType, ...] = ()

    def __str__(self) -> str:
        if self.kind == TypeKind.LIST:
            return "List[{}]".format(self.item_type or ANY_VALUE)
        if self.kind == TypeKind.MAP:
            return "Map[{}, {}]".format(self.key_type or ANY_VALUE, self.value_type or ANY_VALUE)
        if self.kind == TypeKind.UNION:
            return " | ".join(str(item) for item in self.members)
        return self.kind.value


STRING = RosType(TypeKind.STRING)
INTEGER = RosType(TypeKind.INTEGER)
NUMBER = RosType(TypeKind.NUMBER)
BOOLEAN = RosType(TypeKind.BOOLEAN)
NULL = RosType(TypeKind.NULL)
NO_VALUE = RosType(TypeKind.NO_VALUE)
BINARY = RosType(TypeKind.BINARY)
HASHABLE_SCALAR = RosType(TypeKind.HASHABLE_SCALAR)
ANY_VALUE = RosType(TypeKind.ANY)
NON_NULL_ANY_VALUE = RosType(TypeKind.NON_NULL_ANY)
UNKNOWN_TYPE = RosType(TypeKind.UNKNOWN)
JSON_DECODED_VALUE = RosType(TypeKind.JSON_DECODED)


def list_of(item_type: RosType = ANY_VALUE) -> RosType:
    return RosType(TypeKind.LIST, item_type=item_type)


def map_of(key_type: RosType = HASHABLE_SCALAR, value_type: RosType = ANY_VALUE) -> RosType:
    return RosType(TypeKind.MAP, key_type=key_type, value_type=value_type)


def union_of(*types: RosType) -> RosType:
    flattened: list[RosType] = []
    for item in types:
        candidates = item.members if item.kind == TypeKind.UNION else (item,)
        for candidate in candidates:
            if candidate.kind == TypeKind.ANY:
                return ANY_VALUE
            if candidate not in flattened:
                flattened.append(candidate)
    if not flattened:
        return UNKNOWN_TYPE
    if len(flattened) == 1:
        return flattened[0]
    return RosType(TypeKind.UNION, members=tuple(flattened))


NUMBER_LIKE = union_of(NUMBER, BOOLEAN)
INTEGER_LIKE = union_of(INTEGER, BOOLEAN)
STRING_OR_NULL = union_of(STRING, NULL)


class ValueKnowledge(str, Enum):
    CONSTANT = "CONSTANT"
    DYNAMIC = "DYNAMIC"
    UNKNOWN = "UNKNOWN"


class NumberFiniteness(str, Enum):
    FINITE = "FINITE"
    NAN = "NAN"
    POSITIVE_INFINITY = "POSITIVE_INFINITY"
    NEGATIVE_INFINITY = "NEGATIVE_INFINITY"
    UNKNOWN = "UNKNOWN"


class FloatCoercionOutcome(str, Enum):
    FINITE = "FINITE"
    NAN = "NAN"
    POSITIVE_INFINITY = "POSITIVE_INFINITY"
    NEGATIVE_INFINITY = "NEGATIVE_INFINITY"
    OVERFLOW = "OVERFLOW"
    INVALID_TYPE = "INVALID_TYPE"
    INVALID_VALUE = "INVALID_VALUE"
    UNKNOWN = "UNKNOWN"


@dataclass(frozen=True)
class InferredValue:
    type: RosType
    knowledge: ValueKnowledge = ValueKnowledge.DYNAMIC
    value: Any = None
    poisoned: bool = False
    may_refer_no_value: bool = False
    number_finiteness: NumberFiniteness = NumberFiniteness.UNKNOWN

    @classmethod
    def constant(cls, value: Any, *, ros_type: RosType | None = None) -> InferredValue:
        normalized = normalize(value)
        return cls(
            type=ros_type or infer_type(normalized),
            knowledge=ValueKnowledge.CONSTANT,
            value=normalized,
            number_finiteness=number_finiteness(normalized),
        )

    @classmethod
    def dynamic(cls, ros_type: RosType, *, may_refer_no_value: bool = False) -> InferredValue:
        return cls(type=ros_type, knowledge=ValueKnowledge.DYNAMIC, may_refer_no_value=may_refer_no_value)

    @classmethod
    def invalid(cls) -> InferredValue:
        return cls(type=UNKNOWN_TYPE, knowledge=ValueKnowledge.UNKNOWN, poisoned=True)


def normalize(value: Any, *, _seen: set[int] | None = None) -> Any:
    """Normalize a raw ROS result without executing user supplied conversion code.

    Mapping keys are retained, Binary values become ``List[Integer]``, and raw
    iterable values are recursively materialized as lists.  Opaque objects are
    returned unchanged and are consequently inferred as ``UnknownType``.
    """

    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, bytes):
        return list(value)
    if _seen is None:
        _seen = set()
    identity = id(value)
    if identity in _seen:
        return value
    _seen.add(identity)
    try:
        if isinstance(value, Mapping):
            return {key: normalize(item, _seen=_seen) for key, item in value.items()}
        if isinstance(value, (list, tuple, set, frozenset)):
            return [normalize(item, _seen=_seen) for item in value]
        if isinstance(value, Iterable):
            return [normalize(item, _seen=_seen) for item in value]
        return value
    finally:
        _seen.discard(identity)


def infer_type(value: Any) -> RosType:
    if value is None:
        return NULL
    if isinstance(value, bool):
        return BOOLEAN
    if isinstance(value, int):
        return INTEGER
    if isinstance(value, float):
        return NUMBER
    if isinstance(value, str):
        return STRING
    if isinstance(value, bytes):
        return list_of(INTEGER)
    if isinstance(value, (list, tuple, set, frozenset)):
        members = [infer_type(item) for item in value]
        return list_of(union_of(*members) if members else ANY_VALUE)
    if isinstance(value, Mapping):
        key_types = [infer_mapping_key_type(key) for key in value]
        value_types = [infer_type(item) for item in value.values()]
        return map_of(
            union_of(*key_types) if key_types else ANY_VALUE,
            union_of(*value_types) if value_types else ANY_VALUE,
        )
    return UNKNOWN_TYPE


def infer_mapping_key_type(value: Any) -> RosType:
    if isinstance(value, bytes):
        return BINARY
    inferred = infer_type(value)
    if inferred.kind in {
        TypeKind.STRING,
        TypeKind.INTEGER,
        TypeKind.NUMBER,
        TypeKind.BOOLEAN,
        TypeKind.NULL,
    }:
        return inferred
    return UNKNOWN_TYPE


def is_json_serializable_value(value: Any) -> bool:
    """Match the reachable Python json.dumps value/key domain without coercing opaque objects."""

    if value is None or isinstance(value, (str, int, float, bool)):
        return True
    if isinstance(value, list):
        return all(is_json_serializable_value(item) for item in value)
    if isinstance(value, Mapping):
        return all(
            (key is None or isinstance(key, (str, int, float, bool))) and is_json_serializable_value(item)
            for key, item in value.items()
        )
    return False


def number_finiteness(value: Any) -> NumberFiniteness:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return NumberFiniteness.UNKNOWN
    if isinstance(value, int):
        return NumberFiniteness.FINITE
    if math.isnan(value):
        return NumberFiniteness.NAN
    if value == math.inf:
        return NumberFiniteness.POSITIVE_INFINITY
    if value == -math.inf:
        return NumberFiniteness.NEGATIVE_INFINITY
    return NumberFiniteness.FINITE


def float_coercion(value: Any, *, known: bool = True) -> FloatCoercionOutcome:
    if not known:
        return FloatCoercionOutcome.UNKNOWN
    if isinstance(value, (Mapping, list, tuple, set, bytes)) or value is None:
        return FloatCoercionOutcome.INVALID_TYPE
    # The ROS runtime calls float(value), but a local validator must not invoke
    # arbitrary user-provided __float__ code from a binding/adapter object.
    if not isinstance(value, (str, int, float, bool)):
        return FloatCoercionOutcome.UNKNOWN
    try:
        converted = float(value)
    except OverflowError:
        return FloatCoercionOutcome.OVERFLOW
    except TypeError:
        return FloatCoercionOutcome.INVALID_TYPE
    except ValueError:
        return FloatCoercionOutcome.INVALID_VALUE
    if math.isnan(converted):
        return FloatCoercionOutcome.NAN
    if converted == math.inf:
        return FloatCoercionOutcome.POSITIVE_INFINITY
    if converted == -math.inf:
        return FloatCoercionOutcome.NEGATIVE_INFINITY
    return FloatCoercionOutcome.FINITE


class Compatibility(str, Enum):
    DEFINITE_MATCH = "DEFINITE_MATCH"
    POSSIBLE_MATCH = "POSSIBLE_MATCH"
    DEFINITE_MISMATCH = "DEFINITE_MISMATCH"


def type_members(ros_type: RosType) -> tuple[RosType, ...]:
    if ros_type.kind == TypeKind.UNION:
        return ros_type.members
    return (ros_type,)


def _single_compatible(actual: RosType, expected: RosType) -> bool | None:
    if actual.kind in (TypeKind.ANY, TypeKind.UNKNOWN) or expected.kind == TypeKind.ANY:
        return None
    if expected.kind == TypeKind.NON_NULL_ANY:
        return actual.kind != TypeKind.NULL
    if actual.kind == TypeKind.NO_VALUE:
        return expected.kind in {TypeKind.NULL, TypeKind.NO_VALUE}
    if expected.kind == TypeKind.HASHABLE_SCALAR:
        return actual.kind in {
            TypeKind.STRING,
            TypeKind.BINARY,
            TypeKind.INTEGER,
            TypeKind.NUMBER,
            TypeKind.BOOLEAN,
            TypeKind.NULL,
        }
    if actual.kind == expected.kind:
        if actual.kind == TypeKind.LIST and actual.item_type and expected.item_type:
            return compatibility(actual.item_type, expected.item_type) != Compatibility.DEFINITE_MISMATCH
        if actual.kind == TypeKind.MAP and actual.value_type and expected.value_type:
            return compatibility(actual.value_type, expected.value_type) != Compatibility.DEFINITE_MISMATCH
        return True
    if expected.kind == TypeKind.NUMBER and actual.kind == TypeKind.INTEGER:
        return True
    if actual.kind == TypeKind.JSON_DECODED:
        return None
    return False


def compatibility(actual: RosType, expected: RosType) -> Compatibility:
    outcomes: list[bool | None] = []
    for actual_member in type_members(actual):
        expected_outcomes = [_single_compatible(actual_member, item) for item in type_members(expected)]
        if True in expected_outcomes:
            outcomes.append(True)
        elif None in expected_outcomes:
            outcomes.append(None)
        else:
            outcomes.append(False)
    if outcomes and all(item is True for item in outcomes):
        return Compatibility.DEFINITE_MATCH
    if outcomes and all(item is False for item in outcomes):
        return Compatibility.DEFINITE_MISMATCH
    return Compatibility.POSSIBLE_MATCH


def parse_json_parameter(raw: Any) -> tuple[InferredValue, str | None]:
    """Reproduce ROS JsonParam's dumps-then-loads ordering for known values."""

    import json

    try:
        encoded = raw if isinstance(raw, str) else json.dumps(raw, ensure_ascii=False, default=None)
        if not encoded:
            return InferredValue.constant(encoded, ros_type=JSON_DECODED_VALUE), None

        def reject_constant(token: str) -> None:
            raise ValueError(_("non-finite JSON constant {}").format(token))

        decoded = json.loads(encoded, parse_constant=reject_constant)
    except (TypeError, ValueError, OverflowError) as error:
        return InferredValue.invalid(), str(error)
    return InferredValue.constant(decoded, ros_type=JSON_DECODED_VALUE), None
