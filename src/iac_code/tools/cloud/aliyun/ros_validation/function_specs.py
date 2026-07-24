"""The single registry for the 43 ROS 2015-09-01 runtime functions."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from types import MappingProxyType
from typing import Mapping

from iac_code.tools.cloud.aliyun.ros_validation.types import (
    ANY_VALUE,
    BOOLEAN,
    INTEGER,
    NULL,
    NUMBER,
    STRING,
    RosType,
    list_of,
    map_of,
    union_of,
)


class ExpressionContext(str, Enum):
    NORMAL = "NORMAL"
    CONDITION = "CONDITION"
    RULE = "RULE"
    COUNT = "COUNT"
    COMPUTED = "COMPUTED"
    MODULE = "MODULE"


class NoValueEffect(str, Enum):
    RECURSIVE = "RECURSIVE"
    PRESERVE = "PRESERVE"
    CONDITIONAL = "CONDITIONAL"


@dataclass(frozen=True)
class FunctionContextContract:
    implementation: str


@dataclass(frozen=True)
class FunctionSpec:
    name: str
    short_tag: str | None
    contracts_by_context: Mapping[ExpressionContext, FunctionContextContract]
    return_type: RosType
    no_value_effect: NoValueEffect = NoValueEffect.RECURSIVE

    @property
    def contexts(self) -> frozenset[ExpressionContext]:
        return frozenset(self.contracts_by_context)


_NORMAL_FUNCTIONS = (
    "Fn::FindInMap",
    "Fn::GetAZs",
    "Ref",
    "Fn::GetAtt",
    "Fn::Select",
    "Fn::Join",
    "Fn::Split",
    "Fn::Replace",
    "Fn::Base64",
    "Fn::Base64Encode",
    "Fn::Base64Decode",
    "Fn::MemberListToMap",
    "Fn::ResourceFacade",
    "Fn::If",
    "Fn::ListMerge",
    "Fn::GetJsonValue",
    "Fn::MergeMapToList",
    "Fn::SelectMapList",
    "Fn::Add",
    "Fn::Avg",
    "Fn::Str",
    "Fn::Calculate",
    "Fn::Sub",
    "Fn::Max",
    "Fn::Min",
    "Fn::GetStackOutput",
    "Fn::Jq",
    "Fn::Length",
    "Fn::Index",
    "Fn::FormatTime",
    "Fn::Any",
    "Fn::MarketplaceImage",
    "Fn::Contains",
    "Fn::EachMemberIn",
    "Fn::MatchPattern",
    "Fn::TransformNamespace",
    "Fn::Indent",
    "Fn::Cidr",
    "Fn::MergeMap",
)

_CONDITION_BASE = ("Fn::Equals", "Ref", "Fn::FindInMap", "Fn::Not", "Fn::And", "Fn::Or")
_CONDITION_EXTENDED = (
    "Fn::Select",
    "Fn::Join",
    "Fn::Split",
    "Fn::Replace",
    "Fn::Base64Encode",
    "Fn::Base64Decode",
    "Fn::MemberListToMap",
    "Fn::If",
    "Fn::ListMerge",
    "Fn::GetJsonValue",
    "Fn::MergeMapToList",
    "Fn::SelectMapList",
    "Fn::Add",
    "Fn::Avg",
    "Fn::Str",
    "Fn::Calculate",
    "Fn::Max",
    "Fn::Min",
    "Fn::Jq",
    "Fn::Length",
    "Fn::GetAZs",
    "Fn::Index",
    "Fn::FormatTime",
    "Fn::Any",
    "Fn::Contains",
    "Fn::EachMemberIn",
    "Fn::MatchPattern",
    "Fn::TransformNamespace",
    "Fn::Indent",
    "Fn::Cidr",
    "Fn::MergeMap",
)

_ALL_FUNCTIONS = tuple(dict.fromkeys((*_NORMAL_FUNCTIONS, *_CONDITION_BASE)))


_RETURN_TYPES: dict[str, RosType] = {
    "Ref": ANY_VALUE,
    "Fn::GetAtt": ANY_VALUE,
    "Fn::FindInMap": ANY_VALUE,
    "Fn::GetAZs": list_of(STRING),
    "Fn::GetStackOutput": ANY_VALUE,
    "Fn::ResourceFacade": ANY_VALUE,
    "Fn::MarketplaceImage": union_of(STRING, NULL),
    "Fn::Join": STRING,
    "Fn::Split": list_of(STRING),
    "Fn::Replace": union_of(STRING, NULL),
    "Fn::Base64": STRING,
    "Fn::Base64Encode": union_of(STRING, NULL),
    "Fn::Base64Decode": union_of(STRING, NULL),
    "Fn::Str": STRING,
    "Fn::Sub": STRING,
    "Fn::Indent": union_of(STRING, NULL),
    "Fn::FormatTime": STRING,
    "Fn::MatchPattern": BOOLEAN,
    "Fn::Select": ANY_VALUE,
    "Fn::MemberListToMap": map_of(STRING, STRING),
    "Fn::ListMerge": union_of(list_of(ANY_VALUE), NULL),
    "Fn::GetJsonValue": union_of(STRING, NULL),
    "Fn::MergeMapToList": union_of(list_of(map_of()), NULL),
    "Fn::MergeMap": map_of(),
    "Fn::SelectMapList": union_of(list_of(ANY_VALUE), NULL),
    "Fn::Jq": ANY_VALUE,
    "Fn::Length": INTEGER,
    "Fn::Index": union_of(INTEGER, NULL),
    "Fn::Any": union_of(BOOLEAN, NULL),
    "Fn::Contains": BOOLEAN,
    "Fn::EachMemberIn": BOOLEAN,
    "Fn::Add": union_of(NUMBER, list_of(), map_of(), NULL),
    "Fn::Avg": NUMBER,
    "Fn::Calculate": NUMBER,
    "Fn::Min": union_of(NUMBER, BOOLEAN, NULL),
    "Fn::Max": union_of(NUMBER, BOOLEAN, NULL),
    "Fn::Cidr": list_of(STRING),
    "Fn::Equals": BOOLEAN,
    "Fn::Not": BOOLEAN,
    "Fn::And": BOOLEAN,
    "Fn::Or": BOOLEAN,
    "Fn::If": ANY_VALUE,
    "Fn::TransformNamespace": ANY_VALUE,
}


def _context_contract(name: str, context: ExpressionContext) -> FunctionContextContract:
    implementation = (
        "ParamRef"
        if name == "Ref" and context not in {ExpressionContext.NORMAL, ExpressionContext.MODULE}
        else name
    )
    if name == "Ref" and context in {ExpressionContext.NORMAL, ExpressionContext.MODULE}:
        implementation = "RefFactory"
    return FunctionContextContract(implementation=implementation)


def _contexts_for(name: str) -> tuple[ExpressionContext, ...]:
    result: list[ExpressionContext] = []
    if name in _NORMAL_FUNCTIONS:
        result.extend((ExpressionContext.NORMAL, ExpressionContext.MODULE))
    if name in _CONDITION_BASE or name in _CONDITION_EXTENDED:
        result.extend((ExpressionContext.CONDITION, ExpressionContext.RULE))
    if name in {"Ref", "Fn::FindInMap", *_CONDITION_EXTENDED}:
        result.extend((ExpressionContext.COUNT, ExpressionContext.COMPUTED))
    return tuple(dict.fromkeys(result))


def _build_specs() -> Mapping[str, FunctionSpec]:
    result: dict[str, FunctionSpec] = {}
    for name in _ALL_FUNCTIONS:
        short_name = "Ref" if name == "Ref" else name.removeprefix("Fn::")
        result[name] = FunctionSpec(
            name=name,
            short_tag="!{}".format(short_name),
            contracts_by_context=MappingProxyType(
                {context: _context_contract(name, context) for context in _contexts_for(name)}
            ),
            return_type=_RETURN_TYPES[name],
            no_value_effect=(
                NoValueEffect.PRESERVE
                if name == "Ref"
                else NoValueEffect.CONDITIONAL
                if name == "Fn::If"
                else NoValueEffect.RECURSIVE
            ),
        )
    if len(result) != 43:
        raise AssertionError("ROS function registry must contain exactly 43 functions")
    return MappingProxyType(result)


FUNCTION_SPECS = _build_specs()
RUNTIME_FUNCTION_NAMES = frozenset(FUNCTION_SPECS)
NORMAL_FUNCTION_NAMES = frozenset(_NORMAL_FUNCTIONS)
CONDITION_FUNCTION_NAMES = frozenset((*_CONDITION_BASE, *_CONDITION_EXTENDED))
COUNT_FUNCTION_NAMES = frozenset(("Ref", "Fn::FindInMap", *_CONDITION_EXTENDED))


def function_spec(name: str) -> FunctionSpec | None:
    return FUNCTION_SPECS.get(name)


def yaml_short_function_names() -> tuple[str, ...]:
    return tuple(sorted(name.removeprefix("Fn::") for name in FUNCTION_SPECS if name.startswith("Fn::")))
