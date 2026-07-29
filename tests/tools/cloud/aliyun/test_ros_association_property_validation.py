from __future__ import annotations

import json
from dataclasses import replace
from types import MappingProxyType

import pytest

from iac_code.tools.cloud.aliyun.ros_validation.association_property_specs import (
    load_association_property_specs,
)
from iac_code.tools.cloud.aliyun.ros_validation.model import (
    MaterializedTemplateSource,
    RequestValidationContext,
    Severity,
    SourceMap,
    display_path,
)
from iac_code.tools.cloud.aliyun.ros_validation.rules import association_property as association_property_rule
from iac_code.tools.cloud.aliyun.ros_validation.rules.association_property import (
    ABSENT_RUNTIME_VALUE,
    UNKNOWN_RUNTIME_VALUE,
    ConsumerReachability,
    evaluate_auto_complete_reachability,
    frontend_string_to_boolean,
    is_frontend_ref_value,
    js_truthy,
    normalize_association_property,
)
from iac_code.tools.cloud.aliyun.ros_validation.symbols import TemplateSymbols
from iac_code.tools.cloud.aliyun.ros_validation.validator import validate_ros_template


def _validate(parameter: dict, *, extra_parameters: dict | None = None, template_metadata: dict | None = None):
    parameters = {"Target": parameter, **(extra_parameters or {})}
    template = {
        "ROSTemplateFormatVersion": "2015-09-01",
        "Parameters": parameters,
        "Resources": {},
    }
    if template_metadata is not None:
        template["Metadata"] = template_metadata
    return validate_ros_template(
        MaterializedTemplateSource(json.dumps(template, ensure_ascii=False), origin="test-template"),
        RequestValidationContext(action="ValidateTemplate"),
    )


def _validate_yaml(template: str):
    return validate_ros_template(
        MaterializedTemplateSource(template, origin="test-template"),
        RequestValidationContext(action="ValidateTemplate"),
    )


def _association_diagnostics(report):
    return [item for item in report.diagnostics if item.code.startswith(("ROS13", "ROS53"))]


def _codes(report):
    return [item.code for item in _association_diagnostics(report)]


def _resolve_components(parameter: dict, association: str | None = None):
    state = object.__new__(association_property_rule._ValidationState)
    state.specs = load_association_property_specs()
    return state._resolve_components(parameter, association)


def test_javascript_compatibility_helpers_match_frontend_boundaries() -> None:
    assert js_truthy([])
    assert js_truthy({})
    assert js_truthy("false")
    assert not js_truthy("")
    assert not js_truthy(0)
    assert frontend_string_to_boolean(True)
    assert frontend_string_to_boolean("TRUE")
    assert not frontend_string_to_boolean(1)
    assert not frontend_string_to_boolean("1")
    assert normalize_association_property("APSARA::ECS::RegionId") == "ALIYUN::ECS::RegionId"
    assert is_frontend_ref_value({"Ref": "P"})
    assert is_frontend_ref_value({"Fn::GetAtt": ["R", "A"]})
    assert is_frontend_ref_value([{"Ref": "P"}])
    assert not is_frontend_ref_value([])
    assert not is_frontend_ref_value({"Ref": "P", "Other": True})
    assert association_property_rule._js_property_key(True) == "true"
    assert association_property_rule._js_property_key(None) == "null"
    assert association_property_rule._js_property_key(-0.0) == "0"
    assert association_property_rule._js_property_key(1.0) == "1"
    assert association_property_rule._js_property_key(1e20) == "100000000000000000000"
    assert association_property_rule._js_property_key(1e21) == "1e+21"
    assert association_property_rule._js_property_key(9007199254740993) == "9007199254740992"


def test_any_of_branch_matching_applies_every_supported_value_constraint() -> None:
    state = object.__new__(association_property_rule._ValidationState)

    assert not state._matches_schema("abc", {"type": "string", "minLength": 5})
    assert state._matches_schema("abc", {"type": "string", "minLength": 1})
    assert not state._matches_schema(3, {"type": "number", "minimum": 5})
    assert state._matches_schema(3, {"type": "number", "minimum": 1})
    assert not state._matches_schema(
        {"key": "x"},
        {"type": "object", "additionalProperties": {"type": "number"}},
    )


def test_any_of_discards_failed_parser_diagnostics_when_a_literal_branch_accepts() -> None:
    state = association_property_rule._ValidationState(
        {"Parameters": {}},
        SourceMap({}, ()),
        TemplateSymbols({}, {}, {}, frozenset(), {}, {}),
        load_association_property_specs(),
    )
    state._validate_schema(
        "${Missing}",
        {
            "anyOf": [
                {
                    "type": "boolean",
                    "x-ore-parser": "whole-value-reference",
                    "x-ore-reference-context": "template-root",
                    "x-ore-reference-kinds": ["parameter"],
                },
                {"type": "string"},
            ]
        },
        (),
        parameter={},
        reference_context="template-root",
        reference_parameters={},
        reference_declaration_path=(),
        auto_reachability=None,
        depth=0,
    )

    assert state.diagnostics == []


def test_stock_component_selection_matches_frontend_precedence_and_javascript_truthiness() -> None:
    supported = _resolve_components(
        {"Type": "String", "NoEcho": True, "AllowedValues": ["a"]},
        "AutoCompleteInput",
    )
    assert supported.initial_component == "AutoCompleteInput"
    assert supported.possible_components == ("AutoCompleteInput",)
    assert supported.deterministic

    assert _resolve_components({"Type": "String", "NoEcho": True}).initial_component == "Password"
    assert _resolve_components({"Type": "String", "NoEcho": "1"}).initial_component == "String"
    assert _resolve_components({"Type": "String", "AllowedValues": []}).initial_component == "List"
    assert _resolve_components({"Type": "String", "AllowedValues": {}}).initial_component == "List"
    assert _resolve_components({"TextArea": "false"}).initial_component == "TextArea"
    assert _resolve_components({}).initial_component == "Input"

    boolean_list = _resolve_components({"Type": "Boolean", "AllowedValues": []})
    assert boolean_list.initial_component == "Boolean"
    assert boolean_list.possible_components == ("List",)


def test_component_semantic_dispatch_is_driven_by_the_vendored_component_rules() -> None:
    registry = load_association_property_specs()
    resolution = _resolve_components({"Type": "String"}, "AutoCompleteInput")
    state = object.__new__(association_property_rule._ValidationState)
    state.specs = registry
    assert state._shared_semantic_rules(resolution) == {"auto_complete_character_capacity"}

    auto_complete = registry.component("AutoCompleteInput")
    assert auto_complete is not None
    components = dict(registry.components)
    components["AutoCompleteInput"] = replace(auto_complete, semantic_rules=())
    state.specs = replace(registry, components=MappingProxyType(components))
    assert state._shared_semantic_rules(resolution) == set()


def test_read_only_and_dynamic_allowed_values_expand_possible_components_without_guessing_host_inputs() -> None:
    association = "ALIYUN::ECS::Instance::InstanceType"
    normal = _resolve_components({"Type": "String"}, association)
    assert normal.initial_component == "ECSInstanceType"
    assert normal.possible_components == ("ECSInstanceType", "ReadOnlyItem")
    assert not normal.deterministic

    condition = _resolve_components(
        {"Type": "String", "AssociationPropertyMetadata": {"ReadOnly": {"Condition": "Conditions.Locked"}}},
        association,
    )
    assert condition.possible_components == ("ECSInstanceType", "ReadOnlyItem")
    assert not condition.deterministic

    forced = _resolve_components({"Type": "String", "ReadOnly": True}, association)
    assert forced.possible_components == ("ReadOnlyItem",)
    assert forced.deterministic

    unknown = _resolve_components({"Type": "String"}, "ALIYUN::Future::InstanceType")
    assert unknown.initial_component == "String"
    assert unknown.possible_components == ("String", "ReadOnlyItem", "List")
    assert not unknown.deterministic

    static_list = _resolve_components(
        {"Type": "String", "ReadOnly": True, "AllowedValues": ["a"]},
        "ALIYUN::Future::InstanceType",
    )
    assert static_list.initial_component == "List"
    assert static_list.possible_components == ("List",)
    assert static_list.deterministic


def _reachability(parameter: dict, **overrides):
    runtime = {
        "host_initial_value": ABSENT_RUNTIME_VALUE,
        "existing_form_value": ABSENT_RUNTIME_VALUE,
        "static_parameter_value": ABSENT_RUNTIME_VALUE,
        "initial_parameter_value": ABSENT_RUNTIME_VALUE,
        "value_effect": ABSENT_RUNTIME_VALUE,
        "dynamic_value_effect": ABSENT_RUNTIME_VALUE,
    }
    runtime.update(overrides)
    return evaluate_auto_complete_reachability(parameter, **runtime)


def test_auto_complete_reachability_defaults_to_unknown_without_host_context() -> None:
    reachability = evaluate_auto_complete_reachability({"Type": "String", "AssociationProperty": "AutoCompleteInput"})

    assert reachability.base_default == "unknown"
    assert reachability.effective_default == "unknown"
    assert reachability.raw_initializer == ConsumerReachability.UNKNOWN
    assert reachability.component_effect == ConsumerReachability.UNKNOWN


def test_auto_complete_reachability_uses_nullish_host_default_and_ref_gate() -> None:
    from_default = _reachability(
        {"Type": "String", "Default": "template-default"},
        host_initial_value=None,
    )
    assert from_default.base_default == "truthy"
    assert from_default.raw_initializer == ConsumerReachability.NOT_REACHED
    assert from_default.component_effect == ConsumerReachability.NOT_REACHED

    host_wins = _reachability(
        {"Type": "String", "Default": "template-default"},
        host_initial_value="",
    )
    assert host_wins.base_default == "falsy"
    assert host_wins.raw_initializer == ConsumerReachability.NOT_REACHED
    assert host_wins.component_effect == ConsumerReachability.REACHED

    for default in ({"Ref": "P"}, {"Fn::GetAtt": ["R", "A"]}, [{"Ref": "P"}]):
        referenced = _reachability({"Type": "String", "Default": default})
        assert referenced.raw_initializer == ConsumerReachability.NOT_REACHED
        assert referenced.component_effect == ConsumerReachability.NOT_REACHED


def test_auto_complete_reachability_matches_type_and_password_branch_precedence() -> None:
    password = _reachability({"Type": "String", "Default": "secret", "NoEcho": True})
    assert password.effective_default == "undefined"
    assert password.raw_initializer == ConsumerReachability.REACHED
    assert password.component_effect == ConsumerReachability.NOT_REACHED

    no_echo_one = _reachability({"Type": "String", "Default": "secret", "NoEcho": "1"})
    assert no_echo_one.raw_initializer == ConsumerReachability.NOT_REACHED

    number = _reachability({"Type": "Number", "Default": "1", "NoEcho": True})
    assert number.effective_default == "truthy"
    assert number.raw_initializer == ConsumerReachability.NOT_REACHED

    invalid_boolean = _reachability({"Type": "Boolean", "Default": "not-a-boolean"})
    assert invalid_boolean.effective_default == "undefined"
    assert invalid_boolean.raw_initializer == ConsumerReachability.REACHED

    json_null = _reachability({"Type": "Json", "Default": "null"})
    assert json_null.effective_default == "falsy"
    assert json_null.raw_initializer == ConsumerReachability.NOT_REACHED
    assert json_null.component_effect == ConsumerReachability.REACHED

    hexadecimal_number = _reachability({"Type": "Number", "Default": "0x10"})
    assert hexadecimal_number.effective_default == "truthy"
    assert hexadecimal_number.component_effect == ConsumerReachability.NOT_REACHED

    for non_javascript_number in ("1_000", "inf", "infinity", "+0x10"):
        invalid_number = _reachability({"Type": "Number", "Default": non_javascript_number})
        assert invalid_number.effective_default == "falsy"
        assert invalid_number.component_effect == ConsumerReachability.REACHED

    javascript_infinity = _reachability({"Type": "Number", "Default": "Infinity"})
    assert javascript_infinity.effective_default == "truthy"
    assert javascript_infinity.component_effect == ConsumerReachability.NOT_REACHED

    strict_json_nan = _reachability({"Type": "Json", "Default": "NaN"})
    assert strict_json_nan.effective_default == "truthy"
    assert strict_json_nan.component_effect == ConsumerReachability.NOT_REACHED


def test_auto_complete_raw_initializer_truthiness_and_slice_conversion() -> None:
    unresolved_length = _reachability(
        {
            "Type": "String",
            "AssociationPropertyMetadata": {"Length": "${P}"},
        }
    )
    assert unresolved_length.raw_initializer == ConsumerReachability.REACHED
    assert unresolved_length.current_value == "falsy"
    assert unresolved_length.component_effect == ConsumerReachability.REACHED

    numeric_string_length = _reachability(
        {
            "Type": "String",
            "AssociationPropertyMetadata": {
                "Length": "3",
                "CharacterClasses": [{"Class": "number", "Min": 1}],
            },
        }
    )
    assert numeric_string_length.current_value == "truthy"
    assert numeric_string_length.component_effect == ConsumerReachability.NOT_REACHED

    for metadata in ({"Prefix": "pre"}, {"Suffix": "post"}):
        with_affix = _reachability({"Type": "String", "AssociationPropertyMetadata": metadata})
        assert with_affix.raw_initializer == ConsumerReachability.REACHED
        assert with_affix.component_effect == ConsumerReachability.NOT_REACHED

    negative_slice = _reachability({"Type": "String", "AssociationPropertyMetadata": {"Length": -1}})
    assert negative_slice.current_value == "truthy"
    assert negative_slice.component_effect == ConsumerReachability.NOT_REACHED

    runtime_length_dependent_slice = _reachability({"Type": "String", "AssociationPropertyMetadata": {"Length": -999}})
    assert runtime_length_dependent_slice.current_value == "unknown"
    assert runtime_length_dependent_slice.component_effect == ConsumerReachability.UNKNOWN

    exhausted_special_pool = _reachability(
        {
            "Type": "String",
            "AssociationPropertyMetadata": {
                "Length": 1,
                "CharacterClasses": [{"Class": "specialCharacter", "Min": 1, "SpecialCharacters": "!", "Start": False}],
            },
        }
    )
    assert exhausted_special_pool.current_value == "falsy"
    assert exhausted_special_pool.component_effect == ConsumerReachability.REACHED

    end_excludes_single_position = _reachability(
        {
            "Type": "String",
            "AssociationPropertyMetadata": {
                "Length": 1,
                "CharacterClasses": [{"Class": "specialCharacter", "Min": 0, "SpecialCharacters": "!", "End": False}],
            },
        }
    )
    assert end_excludes_single_position.current_value == "falsy"
    assert end_excludes_single_position.component_effect == ConsumerReachability.REACHED

    start_empties_persistent_pure_special_pool = _reachability(
        {
            "Type": "String",
            "AssociationPropertyMetadata": {
                "Length": 2,
                "CharacterClasses": [{"Class": "specialCharacter", "Min": 0, "SpecialCharacters": "!", "Start": False}],
            },
        }
    )
    assert start_empties_persistent_pure_special_pool.current_value == "falsy"
    assert start_empties_persistent_pure_special_pool.component_effect == ConsumerReachability.REACHED

    missing_min_does_not_prefill_pure_special_pool = _reachability(
        {
            "Type": "String",
            "AssociationPropertyMetadata": {
                "Length": 2,
                "CharacterClasses": [{"Class": "specialCharacter", "SpecialCharacters": "!", "Start": False}],
            },
        }
    )
    assert missing_min_does_not_prefill_pure_special_pool.current_value == "falsy"
    assert missing_min_does_not_prefill_pure_special_pool.component_effect == ConsumerReachability.REACHED

    end_only_removes_last_position = _reachability(
        {
            "Type": "String",
            "AssociationPropertyMetadata": {
                "Length": 2,
                "CharacterClasses": [{"Class": "specialCharacter", "Min": 0, "SpecialCharacters": "!", "End": False}],
            },
        }
    )
    assert end_only_removes_last_position.current_value == "truthy"
    assert end_only_removes_last_position.component_effect == ConsumerReachability.NOT_REACHED


def test_auto_complete_host_merge_and_metadata_effects_control_component_guard() -> None:
    parameter = {"Type": "String"}
    generated_overrides_existing = _reachability(
        parameter,
        existing_form_value="existing",
    )
    assert generated_overrides_existing.current_value == "truthy"

    static_clears_generated = _reachability(
        parameter,
        static_parameter_value="",
    )
    assert static_clears_generated.component_effect == ConsumerReachability.REACHED

    initial_wins_last = _reachability(
        parameter,
        static_parameter_value="",
        initial_parameter_value="initial",
    )
    assert initial_wins_last.component_effect == ConsumerReachability.NOT_REACHED

    dynamic_clears = _reachability(
        {"Type": "String", "AssociationPropertyMetadata": {"DynamicValue": "${P}"}},
        dynamic_value_effect="",
    )
    assert dynamic_clears.component_effect == ConsumerReachability.REACHED

    unknown_dynamic = _reachability(
        {"Type": "String", "AssociationPropertyMetadata": {"DynamicValue": "${P}"}},
        dynamic_value_effect=UNKNOWN_RUNTIME_VALUE,
    )
    assert unknown_dynamic.component_effect == ConsumerReachability.UNKNOWN

    value_wins_after_dynamic = _reachability(
        {
            "Type": "String",
            "AssociationPropertyMetadata": {"DynamicValue": "${P}", "Value": []},
        },
        dynamic_value_effect="",
        value_effect="value-effect",
    )
    assert value_wins_after_dynamic.component_effect == ConsumerReachability.NOT_REACHED


@pytest.mark.parametrize("value", [None, "", 1, [], {}])
def test_association_property_must_be_a_non_empty_string(value) -> None:
    report = _validate({"Type": "String", "AssociationProperty": value})

    diagnostic = next(item for item in report.diagnostics if item.code == "ROS1301")
    assert diagnostic.severity == Severity.ERROR
    assert display_path(diagnostic.path) == "$.Parameters.Target.AssociationProperty"


def test_known_unknown_excluded_apsara_and_deprecated_association_properties() -> None:
    valid = _validate({"Type": "String", "AssociationProperty": "AutoCompleteInput"})
    assert not _association_diagnostics(valid)

    apsara = _validate({"Type": "String", "AssociationProperty": "APSARA::ECS::RegionId"})
    assert "ROS5303" not in _codes(apsara)

    unknown = _validate({"Type": "String", "AssociationProperty": "ALIYUN::Future::Selector"})
    assert _codes(unknown) == ["ROS5303"]
    unknown_diagnostic = next(item for item in unknown.diagnostics if item.code == "ROS5303")
    assert "ALIYUN::Future::Selector" in unknown_diagnostic.summary
    assert "does not mark the value invalid" in unknown_diagnostic.detail

    excluded = _validate({"Type": "String", "AssociationProperty": "ALIYUN::OOS::Component::ActionChoice"})
    assert _codes(excluded) == ["ROS1302"]
    excluded_diagnostic = next(item for item in excluded.diagnostics if item.code == "ROS1302")
    assert excluded_diagnostic.detail == (
        "This AssociationProperty value is available only in the OOS parameter form."
    )

    for allowed_values in ([], ["a"], {}):
        bypassed = _validate(
            {
                "Type": "String",
                "AssociationProperty": "ALIYUN::OOS::Component::ActionChoice",
                "AllowedValues": allowed_values,
            }
        )
        assert "ROS1302" not in _codes(bypassed)
        bypassed_diagnostic = next(
            item
            for item in bypassed.diagnostics
            if item.code == "ROS5302" and item.subject == "ALIYUN::OOS::Component::ActionChoice"
        )
        assert bypassed_diagnostic.detail == (
            "No resolved form branch selects the unavailable AssociationProperty value."
        )

    read_only_oos = _validate({"Type": "String", "AssociationProperty": "TemplateParameter", "ReadOnly": True})
    assert "ROS1302" not in _codes(read_only_oos)
    assert "ROS5302" in _codes(read_only_oos)

    runtime_read_only_oos = _validate({"Type": "String", "AssociationProperty": "TemplateParameter"})
    assert "ROS1302" not in _codes(runtime_read_only_oos)
    assert "ROS5305" in _codes(runtime_read_only_oos)
    runtime_read_only_diagnostic = next(item for item in runtime_read_only_oos.diagnostics if item.code == "ROS5305")
    assert runtime_read_only_diagnostic.detail == (
        "The editable form path does not support this value, while the read-only path may bypass it."
    )

    deprecated = _validate({"Type": "String", "AssociationProperty": "ALIYUN::ECS::Instance"})
    assert _codes(deprecated) == ["ROS5301"]
    deprecated_diagnostic = next(item for item in deprecated.diagnostics if item.code == "ROS5301")
    assert "ALIYUN::ECS::Instance" in deprecated_diagnostic.summary
    assert "does not block the template" in deprecated_diagnostic.detail


def test_runtime_dependent_component_and_reference_limitations_explain_the_uncertainty() -> None:
    component_dependent = _validate(
        {
            "Type": "String",
            "AssociationProperty": "ALIYUN::ECS::Instance::InstanceType",
            "AssociationPropertyMetadata": {"ZoneId": "${ZoneId}"},
        },
        extra_parameters={"ZoneId": {"Type": "String"}},
    )
    field_diagnostic = next(
        item for item in component_dependent.diagnostics if item.code == "ROS5305" and item.subject == "ZoneId"
    )
    assert "field ZoneId" in field_diagnostic.summary
    assert "used by some possible form components and ignored by others" in field_diagnostic.summary
    assert "runtime component cannot be determined locally" in field_diagnostic.detail
    assert field_diagnostic.suggestion and "Do not change the template" in field_diagnostic.suggestion

    reference_scope = _validate(
        {
            "Type": "String",
            "AssociationProperty": "ALIYUN::ECS::VSwitch::VSwitchId",
            "AssociationPropertyMetadata": {"VpcId": "${VpcId}"},
        },
        extra_parameters={"VpcId": {"Type": "String"}},
    )
    reference_diagnostic = next(
        item for item in reference_scope.diagnostics if item.code == "ROS5305" and item.subject == "VpcId"
    )
    assert "reference VpcId" in reference_diagnostic.summary
    assert "template Parameters or component-local data" in reference_diagnostic.detail
    assert "leaves the reference unchecked" in reference_diagnostic.detail
    assert reference_diagnostic.suggestion and "Do not change the template" in reference_diagnostic.suggestion


def test_metadata_is_valid_without_association_property_and_must_be_a_mapping() -> None:
    valid = _validate(
        {
            "Type": "String",
            "AssociationPropertyMetadata": {"Visible": {"Condition": {"Fn::Equals": [1, 1]}}},
        }
    )
    assert "ROS1303" not in _codes(valid)

    invalid = _validate({"Type": "String", "AssociationPropertyMetadata": []})
    diagnostic = next(item for item in invalid.diagnostics if item.code == "ROS1303")
    assert display_path(diagnostic.path) == "$.Parameters.Target.AssociationPropertyMetadata"


def test_fallback_component_metadata_is_validated_without_association_property() -> None:
    valid = _validate({"Type": "String", "AssociationPropertyMetadata": {"EnableEmptyString": True}})
    assert "ROS1305" not in _codes(valid)

    invalid = _validate({"Type": "String", "AssociationPropertyMetadata": {"EnableEmptyString": "true"}})
    assert "ROS1305" not in _codes(invalid)
    assert any(item.code == "ROS5305" and item.subject == "EnableEmptyString" for item in invalid.diagnostics)

    boolean_post_list = _validate(
        {
            "Type": "Boolean",
            "AllowedValues": [],
            "AssociationPropertyMetadata": {"ForceRadio": True},
        }
    )
    assert "ROS1305" not in _codes(boolean_post_list)


def test_unknown_metadata_key_is_warning_while_common_contract_is_partial() -> None:
    report = _validate(
        {
            "Type": "String",
            "AssociationProperty": "AutoCompleteInput",
            "AssociationPropertyMetadata": {"FutureField": 1},
        }
    )

    diagnostic = next(item for item in report.diagnostics if item.code == "ROS5304")
    assert diagnostic.severity == Severity.LIMITATION
    assert display_path(diagnostic.path).endswith("AssociationPropertyMetadata.FutureField")


def test_unknown_metadata_key_is_blocking_only_when_common_and_component_contracts_are_closed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    registry = load_association_property_specs()
    closed_common = dict(registry.common_metadata)
    closed_common["additionalProperties"] = False
    closed_registry = replace(
        registry,
        common_coverage="complete",
        common_metadata=MappingProxyType(closed_common),
    )
    monkeypatch.setattr(
        association_property_rule,
        "load_association_property_specs",
        lambda: closed_registry,
    )

    report = _validate(
        {
            "Type": "String",
            "AssociationProperty": "AutoCompleteInput",
            "AssociationPropertyMetadata": {"FutureField": 1},
        }
    )

    diagnostic = next(item for item in report.diagnostics if item.code == "ROS1304")
    assert diagnostic.severity == Severity.ERROR


def test_auto_complete_digit_is_one_precise_blocking_enum_error() -> None:
    report = _validate(
        {
            "Type": "String",
            "AssociationProperty": "AutoCompleteInput",
            "AssociationPropertyMetadata": {
                "Length": 8,
                "CharacterClasses": [{"Class": "digit", "Min": 1}],
            },
        }
    )

    diagnostics = [item for item in report.diagnostics if item.code == "ROS1305"]
    assert len(diagnostics) == 1
    diagnostic = diagnostics[0]
    assert diagnostic.severity == Severity.ERROR
    assert diagnostic.expected == "lowercase | uppercase | number | specialCharacter"
    assert diagnostic.actual == "digit"
    assert diagnostic.suggestion == "Use number instead."
    assert display_path(diagnostic.path) == (
        "$.Parameters.Target.AssociationPropertyMetadata.CharacterClasses[0].Class"
    )


def test_local_contract_corrections_accept_localized_labels_and_boolean_only_key() -> None:
    localized_labels = _validate(
        {
            "Type": "String",
            "AllowedValues": ["CreateNew"],
            "AssociationPropertyMetadata": {"ValueLabelMapping": {"CreateNew": {"zh-cn": "新建", "en": "Create new"}}},
        }
    )
    assert not [item for item in localized_labels.diagnostics if item.code == "ROS1305"]

    invalid_label = _validate(
        {
            "Type": "String",
            "AssociationPropertyMetadata": {"ValueLabelMapping": {"CreateNew": {"en": 1}}},
        }
    )
    assert any(item.code == "ROS1305" for item in invalid_label.diagnostics)

    only_key = _validate(
        {
            "Type": "String",
            "AssociationProperty": "ALIYUN::Bailian::ApiKey::ApiKeyInfo",
            "AssociationPropertyMetadata": {"OnlyKey": True},
        }
    )
    assert not [item for item in only_key.diagnostics if item.code == "ROS1305"]

    invalid_only_key = _validate(
        {
            "Type": "String",
            "AssociationProperty": "ALIYUN::Bailian::ApiKey::ApiKeyInfo",
            "AssociationPropertyMetadata": {"OnlyKey": "true"},
        }
    )
    assert any(item.code == "ROS1305" for item in invalid_only_key.diagnostics)


def test_auto_complete_number_is_valid() -> None:
    report = _validate(
        {
            "Type": "String",
            "AssociationProperty": "AutoCompleteInput",
            "AssociationPropertyMetadata": {
                "Length": 8,
                "CharacterClasses": [{"Class": "number", "Min": 1}],
            },
        }
    )

    assert not [item for item in _association_diagnostics(report) if item.severity == Severity.ERROR]


@pytest.mark.parametrize(
    ("metadata", "expected_path"),
    [
        ({"CharacterClasses": {"Class": "number"}}, "CharacterClasses"),
        ({"CharacterClasses": ["number"]}, "CharacterClasses[0]"),
        ({"CharacterClasses": [{"Min": 1}]}, "CharacterClasses[0].Class"),
        (
            {"CharacterClasses": [{"Class": "number", "Min": 1, "Unknown": True}]},
            "CharacterClasses[0].Unknown",
        ),
    ],
)
def test_auto_complete_deep_structure_errors(metadata: dict, expected_path: str) -> None:
    report = _validate(
        {
            "Type": "String",
            "AssociationProperty": "AutoCompleteInput",
            "AssociationPropertyMetadata": metadata,
        }
    )

    diagnostic = next(item for item in report.diagnostics if item.code == "ROS1305")
    assert display_path(diagnostic.path).endswith(expected_path)


def test_auto_complete_missing_min_warns_only_for_multiple_character_classes() -> None:
    missing = _validate(
        {
            "Type": "String",
            "AssociationProperty": "AutoCompleteInput",
            "AssociationPropertyMetadata": {"CharacterClasses": [{"Class": "number"}]},
        }
    )
    assert not [item for item in missing.diagnostics if item.code == "ROS5302" and item.subject == "Min"]

    multiple = _validate(
        {
            "Type": "String",
            "AssociationProperty": "AutoCompleteInput",
            "AssociationPropertyMetadata": {
                "CharacterClasses": [{"Class": "number"}, {"Class": "lowercase", "Min": 1}]
            },
        }
    )
    diagnostic = next(item for item in multiple.diagnostics if item.code == "ROS5302" and item.subject == "Min")
    assert "does not guarantee" in diagnostic.summary
    assert "multiple character classes" in diagnostic.detail


def test_auto_complete_wrong_case_min_is_one_actionable_error() -> None:
    report = _validate(
        {
            "Type": "String",
            "AssociationProperty": "AutoCompleteInput",
            "AssociationPropertyMetadata": {"CharacterClasses": [{"Class": "number", "min": 1}]},
        }
    )

    diagnostics = [item for item in report.diagnostics if item.subject in {"min", "Min"}]
    assert len(diagnostics) == 1
    assert diagnostics[0].code == "ROS1305"
    assert diagnostics[0].suggestion == "Rename min to Min."


def test_auto_complete_non_integer_min_is_a_runtime_warning() -> None:

    for minimum in (-1, 1.5):
        report = _validate(
            {
                "Type": "String",
                "AssociationProperty": "AutoCompleteInput",
                "AssociationPropertyMetadata": {
                    "Length": 8,
                    "CharacterClasses": [{"Class": "number", "Min": minimum}],
                },
            }
        )
        diagnostic = next(item for item in report.diagnostics if item.code == "ROS5302" and item.subject == "Min")
        assert "every available position" in diagnostic.summary

    skipped_empty_special = _validate(
        {
            "Type": "String",
            "AssociationProperty": "AutoCompleteInput",
            "AssociationPropertyMetadata": {
                "Length": 4,
                "CharacterClasses": [
                    {"Class": "number", "Min": 1},
                    {"Class": "specialCharacter", "Min": -1, "SpecialCharacters": ""},
                ],
            },
        }
    )
    assert not any(
        item.subject == "Min" and "every available position" in item.summary
        for item in skipped_empty_special.diagnostics
    )


def test_auto_complete_capacity_and_special_character_requirements() -> None:
    overflow = _validate(
        {
            "Type": "String",
            "AssociationProperty": "AutoCompleteInput",
            "AssociationPropertyMetadata": {
                "Length": 2,
                "CharacterClasses": [
                    {"Class": "number", "Min": 2},
                    {"Class": "uppercase", "Min": 1},
                ],
            },
        }
    )
    assert any(item.code == "ROS5305" and item.subject == "character-capacity" for item in overflow.diagnostics)

    missing_specials = _validate(
        {
            "Type": "String",
            "AssociationProperty": "AutoCompleteInput",
            "AssociationPropertyMetadata": {
                "Length": 4,
                "CharacterClasses": [{"Class": "specialCharacter", "Min": 1}],
            },
        }
    )
    assert any(item.code == "ROS5305" and item.subject == "SpecialCharacters" for item in missing_specials.diagnostics)

    optional_specials = _validate(
        {
            "Type": "String",
            "AssociationProperty": "AutoCompleteInput",
            "AssociationPropertyMetadata": {
                "Length": 4,
                "CharacterClasses": [
                    {"Class": "number", "Min": 1},
                    {"Class": "specialCharacter", "Min": 0},
                ],
            },
        }
    )
    assert not [item for item in optional_specials.diagnostics if item.code == "ROS1307"]


def test_auto_complete_empty_and_multiple_special_character_entries_follow_runtime_state() -> None:
    empty_special_does_not_add_capacity = _validate(
        {
            "Type": "String",
            "AssociationProperty": "AutoCompleteInput",
            "AssociationPropertyMetadata": {
                "Length": 2,
                "CharacterClasses": [
                    {"Class": "number", "Min": 2},
                    {"Class": "specialCharacter", "Min": 99, "SpecialCharacters": ""},
                ],
            },
        }
    )
    assert not any(item.subject == "character-capacity" for item in empty_special_does_not_add_capacity.diagnostics)

    shared_positions = _validate(
        {
            "Type": "String",
            "AssociationProperty": "AutoCompleteInput",
            "AssociationPropertyMetadata": {
                "Length": 4,
                "CharacterClasses": [
                    {
                        "Class": "specialCharacter",
                        "Min": 2,
                        "SpecialCharacters": "!",
                        "Start": False,
                        "End": False,
                    },
                    {
                        "Class": "specialCharacter",
                        "Min": 2,
                        "SpecialCharacters": "@",
                        "Start": False,
                        "End": False,
                    },
                ],
            },
        }
    )
    assert any(item.subject == "shared-character-capacity" for item in shared_positions.diagnostics)
    boundary_warnings = [
        item for item in shared_positions.diagnostics if item.code == "ROS5302" and item.subject in {"Start", "End"}
    ]
    assert len(boundary_warnings) == 1
    assert display_path(boundary_warnings[0].path).endswith("CharacterClasses[1].Start")


def test_auto_complete_ignored_fields_pattern_and_mutable_remaining_pool() -> None:
    non_special = _validate(
        {
            "Type": "String",
            "AssociationProperty": "AutoCompleteInput",
            "AssociationPropertyMetadata": {
                "Pattern": "[0-9]+",
                "CharacterClasses": [
                    {
                        "Class": "number",
                        "Min": 1,
                        "SpecialCharacters": "!",
                        "Start": False,
                        "End": False,
                    }
                ],
            },
        }
    )
    ignored_subjects = {item.subject for item in non_special.diagnostics if item.code == "ROS5302"}
    assert {"Pattern", "SpecialCharacters", "Start", "End"} <= ignored_subjects

    only_special = _validate(
        {
            "Type": "String",
            "AssociationProperty": "AutoCompleteInput",
            "AssociationPropertyMetadata": {
                "Length": 4,
                "CharacterClasses": [
                    {
                        "Class": "specialCharacter",
                        "Min": 1,
                        "SpecialCharacters": "!",
                        "Start": False,
                    }
                ],
            },
        }
    )
    pool = next(item for item in only_special.diagnostics if item.code == "ROS5302" and item.subject == "Start")
    assert "shorter than Length" in pool.detail

    fully_prefilled = _validate(
        {
            "Type": "String",
            "AssociationProperty": "AutoCompleteInput",
            "AssociationPropertyMetadata": {
                "Length": 4,
                "CharacterClasses": [
                    {"Class": "number", "Min": 4},
                    {
                        "Class": "specialCharacter",
                        "Min": 0,
                        "SpecialCharacters": "!",
                        "Start": False,
                    },
                ],
            },
        }
    )
    assert not any(
        item.code == "ROS5302" and item.subject == "Start" and "boundary restriction persists" in item.summary
        for item in fully_prefilled.diagnostics
    )


def test_auto_complete_length_and_pattern_limitations() -> None:
    normalized = _validate(
        {
            "Type": "String",
            "AssociationProperty": "AutoCompleteInput",
            "AssociationPropertyMetadata": {
                "Length": 2.5,
                "CharacterClasses": [{"Class": "number", "Min": 1}],
            },
        }
    )
    assert any(
        item.code == "ROS5302" and item.subject == "Length" and "Effective generated Length is 2" in item.detail
        for item in normalized.diagnostics
    )

    pattern = _validate(
        {
            "Type": "String",
            "AssociationProperty": "AutoCompleteInput",
            "AssociationPropertyMetadata": {"Pattern": "(?<name>a)"},
        }
    )
    assert any(item.code == "ROS5305" and item.subject == "Pattern" for item in pattern.diagnostics)


@pytest.mark.parametrize(
    ("length", "detail"),
    [
        (2.5, "Effective generated Length is 2"),
        (0, "unsliced generated identifier"),
        (-1, "generated identifier truncated at position -1"),
    ],
)
def test_auto_complete_generated_identifier_branch_reports_actual_length_normalization(length, detail: str) -> None:
    report = _validate(
        {
            "Type": "String",
            "AssociationProperty": "AutoCompleteInput",
            "AssociationPropertyMetadata": {"Length": length},
        }
    )

    warning = next(item for item in report.diagnostics if item.code == "ROS5302" and item.subject == "Length")
    assert detail in warning.detail


def test_empty_special_character_configuration_ignores_boundary_flags_even_when_minimum_is_zero() -> None:
    report = _validate(
        {
            "Type": "String",
            "AssociationProperty": "AutoCompleteInput",
            "AssociationPropertyMetadata": {
                "Length": 4,
                "CharacterClasses": [
                    {"Class": "number", "Min": 1},
                    {
                        "Class": "specialCharacter",
                        "Min": 0,
                        "SpecialCharacters": "",
                        "Start": False,
                        "End": False,
                    },
                ],
            },
        }
    )

    ignored = {
        item.subject
        for item in report.diagnostics
        if item.code == "ROS5302" and "does not retain specialCharacter" in item.detail
    }
    assert ignored == {"Start", "End"}
    assert not any(
        item.subject == "SpecialCharacters" and item.code in {"ROS1307", "ROS5305"} for item in report.diagnostics
    )


def test_whole_value_parameter_references_validate_scope_type_and_consumer_consistency() -> None:
    valid = _validate(
        {
            "Type": "String",
            "AssociationProperty": "AutoCompleteInput",
            "AssociationPropertyMetadata": {"Length": "${Length}"},
        },
        extra_parameters={"Length": {"Type": "Number"}},
    )
    assert "ROS1305" not in _codes(valid)
    assert "ROS5305" in _codes(valid)
    assert not any(item.code == "ROS5302" and item.subject == "Length" for item in valid.diagnostics)

    wrong_type = _validate(
        {
            "Type": "String",
            "AssociationProperty": "AutoCompleteInput",
            "AssociationPropertyMetadata": {"Length": "${Length}"},
        },
        extra_parameters={"Length": {"Type": "String"}},
    )
    warning = next(item for item in wrong_type.diagnostics if item.code == "ROS5302" and item.subject == "Length")
    assert warning.related_locations

    missing = _validate(
        {
            "Type": "String",
            "AssociationProperty": "AutoCompleteInput",
            "AssociationPropertyMetadata": {"Length": "${Missing}"},
        }
    )
    assert "ROS1306" not in _codes(missing)
    assert any(item.code == "ROS5305" and item.subject == "Missing" for item in missing.diagnostics)


def test_dynamic_and_environment_reference_parsers() -> None:
    dynamic = _validate(
        {
            "Type": "String",
            "AssociationPropertyMetadata": {"DynamicValue": "prefix-${Missing}"},
        }
    )
    assert "ROS1306" in _codes(dynamic)

    environment = _validate(
        {
            "Type": "String",
            "AssociationProperty": "AutoCompleteInput",
            "AssociationPropertyMetadata": {"Prefix": "{{env.prefix}}"},
        }
    )
    assert "ROS5305" in _codes(environment)

    malformed = _validate(
        {
            "Type": "String",
            "AssociationProperty": "AutoCompleteInput",
            "AssociationPropertyMetadata": {"Prefix": "${Missing"},
        }
    )
    assert "ROS1306" not in _codes(malformed)
    assert "ROS5305" not in _codes(malformed)

    escaped_literal = _validate(
        {
            "Type": "String",
            "AssociationPropertyMetadata": {"DynamicValue": "prefix-${!not-a-parameter}"},
        }
    )
    assert not any(
        item.code == "ROS1306" and item.subject == "!not-a-parameter" for item in escaped_literal.diagnostics
    )

    whole_escaped_literal = _validate(
        {
            "Type": "String",
            "AssociationProperty": "AutoCompleteInput",
            "AssociationPropertyMetadata": {"Prefix": "${!not-a-parameter}"},
        }
    )
    assert not any(
        item.code == "ROS1306" and item.subject == "!not-a-parameter" for item in whole_escaped_literal.diagnostics
    )

    escaped_field_path = _validate(
        {
            "Type": "String",
            "AssociationProperty": "AutoCompleteInput",
            "AssociationPropertyMetadata": {"Prefix": "${!Object.Name}"},
        },
        extra_parameters={
            "Object": {
                "Type": "Json",
                "AssociationPropertyMetadata": {"Parameters": {"Name": {"Type": "String"}}},
            }
        },
    )
    assert any(item.code == "ROS5305" and item.subject == "!Object.Name" for item in escaped_field_path.diagnostics)

    injected_region = _validate(
        {
            "Type": "String",
            "AssociationPropertyMetadata": {"DynamicValue": "prefix-${RegionId}"},
        }
    )
    injected_region_array = _validate(
        {
            "Type": "String",
            "AssociationPropertyMetadata": {
                "DynamicValue": [{"Condition": {"Fn::Equals": [1, 1]}, "Value": "prefix-${RegionId}"}]
            },
        }
    )
    for report in (injected_region, injected_region_array):
        assert not any(item.code == "ROS1306" and item.subject == "RegionId" for item in report.diagnostics)

    injected_region_condition = _validate(
        {
            "Type": "String",
            "AssociationPropertyMetadata": {
                "DynamicValue": [
                    {
                        "Condition": {"Fn::Equals": ["${RegionId}", "cn-hangzhou"]},
                        "Value": "test",
                    }
                ]
            },
        }
    )
    assert not any(
        item.code == "ROS1306" and item.subject == "RegionId" for item in injected_region_condition.diagnostics
    )

    ordinary_condition = _validate(
        {
            "Type": "String",
            "AssociationPropertyMetadata": {"Visible": {"Condition": {"Fn::Equals": ["${RegionId}", "cn-hangzhou"]}}},
        }
    )
    assert any(item.code == "ROS1306" and item.subject == "RegionId" for item in ordinary_condition.diagnostics)


def test_dynamic_value_condition_values_and_mapping_selector_segments_use_their_actual_parsers() -> None:
    dynamic_array = _validate(
        {
            "Type": "String",
            "AssociationPropertyMetadata": {
                "DynamicValue": [
                    {
                        "Condition": {"Fn::Equals": [1, 1]},
                        "Value": "prefix-${Missing}",
                    }
                ]
            },
        }
    )
    assert any(item.code == "ROS1306" and item.subject == "Missing" for item in dynamic_array.diagnostics)

    for literal_fragment in ("${", "prefix-${", "${P}${"):
        unmatched_fragment = _validate(
            {
                "Type": "String",
                "AssociationPropertyMetadata": {"DynamicValue": literal_fragment},
            },
            extra_parameters={"P": {"Type": "String"}},
        )
        assert not any(
            item.code == "ROS1306" and item.subject in {literal_fragment, "P"}
            for item in unmatched_fragment.diagnostics
        )

    disallowed_field_paths = _validate(
        {
            "Type": "String",
            "AssociationPropertyMetadata": {
                "DynamicValue": [{"Condition": {"Fn::Equals": [1, 1]}, "Value": "${Object.Name}"}]
            },
        },
        extra_parameters={
            "Object": {
                "Type": "Json",
                "AssociationPropertyMetadata": {"Parameters": {"Name": {"Type": "String"}}},
            }
        },
    )
    assert any(item.code == "ROS1306" and item.subject == "Object.Name" for item in disallowed_field_paths.diagnostics)

    partial_segment = _validate(
        {
            "Type": "String",
            "AssociationPropertyMetadata": {
                "MappingMetadata": {
                    "MappingName": "Example",
                    "MappedPropsName": "AllowedValues",
                    "ValueSelector": "By${Missing}.Value",
                }
            },
        }
    )
    assert not any(item.code == "ROS1306" and item.subject == "Missing" for item in partial_segment.diagnostics)

    whole_segment = _validate(
        {
            "Type": "String",
            "AssociationPropertyMetadata": {
                "MappingMetadata": {
                    "MappingName": "Example",
                    "MappedPropsName": "AllowedValues",
                    "ValueSelector": "${Missing}.Value",
                }
            },
        }
    )
    assert not any(item.code == "ROS1306" and item.subject == "Missing" for item in whole_segment.diagnostics)
    assert any(item.code == "ROS5305" and item.subject == "Missing" for item in whole_segment.diagnostics)

    greedy_mapping_parameter = _validate(
        {
            "Type": "String",
            "AssociationPropertyMetadata": {
                "MappingMetadata": {
                    "MappingName": "Example",
                    "MappedPropsName": "AllowedValues",
                    "ValueSelector": "${A{B}}",
                }
            },
        },
        extra_parameters={"A{B}": {"Type": "String"}},
    )
    assert not any(
        item.code in {"ROS1306", "ROS5305"} and item.subject == "A{B}" for item in greedy_mapping_parameter.diagnostics
    )

    greedy_mapping_literal = _validate(
        {
            "Type": "String",
            "AssociationPropertyMetadata": {
                "MappingMetadata": {
                    "MappingName": "Example",
                    "MappedPropsName": "AllowedValues",
                    "ValueSelector": "${A{B}}",
                }
            },
        }
    )
    assert any(item.code == "ROS5305" and item.subject == "A{B}" for item in greedy_mapping_literal.diagnostics)

    empty_mapping_reference_is_literal = _validate(
        {
            "Type": "String",
            "AssociationPropertyMetadata": {
                "MappingMetadata": {
                    "MappingName": "Example",
                    "MappedPropsName": "AllowedValues",
                    "ValueSelector": "${}",
                }
            },
        }
    )
    assert not any(
        item.code in {"ROS1306", "ROS5305"} and item.subject == ""
        for item in empty_mapping_reference_is_literal.diagnostics
    )

    projected_name_is_literal = _validate(
        {
            "Type": "String",
            "AssociationPropertyMetadata": {
                "MappingMetadata": {
                    "MappingName": "Example",
                    "MappedPropsName": "AllowedValues",
                    "ValueSelector": "${Rows[]}",
                }
            },
        },
        extra_parameters={"Rows": {"Type": "Json"}},
    )
    assert not any(
        item.code == "ROS1306" and item.subject == "Rows[]" for item in projected_name_is_literal.diagnostics
    )
    assert any(item.code == "ROS5305" and item.subject == "Rows[]" for item in projected_name_is_literal.diagnostics)

    mapping_region = _validate(
        {
            "Type": "String",
            "AssociationPropertyMetadata": {
                "MappingMetadata": {
                    "MappingName": "Example",
                    "MappedPropsName": "AllowedValues",
                    "ValueSelector": "ByRegion.${RegionId}",
                }
            },
        }
    )
    assert not any(item.code == "ROS1306" and item.subject == "RegionId" for item in mapping_region.diagnostics)

    spaced_mapping_parameter = _validate(
        {
            "Type": "String",
            "AssociationPropertyMetadata": {
                "MappingMetadata": {
                    "MappingName": "Example",
                    "MappedPropsName": "AllowedValues",
                    "ValueSelector": "${ P }",
                }
            },
        },
        extra_parameters={"P": {"Type": "String"}},
    )
    assert any(item.code == "ROS5305" and item.subject == " P " for item in spaced_mapping_parameter.diagnostics)

    spaced_mapping_region = _validate(
        {
            "Type": "String",
            "AssociationPropertyMetadata": {
                "MappingMetadata": {
                    "MappingName": "Example",
                    "MappedPropsName": "AllowedValues",
                    "ValueSelector": "${ RegionId }",
                }
            },
        }
    )
    assert any(item.code == "ROS5305" and item.subject == " RegionId " for item in spaced_mapping_region.diagnostics)


def test_condition_ast_arity_ignored_keys_and_definitions_paths() -> None:
    assert association_property_rule._same_definition_value(
        {"1": "one", "0": "zero"},
        {"0": "zero", "1": "one"},
    )
    assert not association_property_rule._same_definition_value(
        {"Fn::Equals": [1, 1], "Fn::Contains": [[1], 1]},
        {"Fn::Contains": [[1], 1], "Fn::Equals": [1, 1]},
    )

    empty_definition_path = _validate(
        {
            "Type": "String",
            "AssociationPropertyMetadata": {"Visible": {"Condition": ""}},
        },
        template_metadata={"ALIYUN::ROS::Interface": {"Definitions": {}}},
    )
    assert any(item.code == "ROS1306" and item.subject == "" for item in empty_definition_path.diagnostics)

    invalid_arity = _validate(
        {
            "Type": "String",
            "AssociationPropertyMetadata": {
                "Visible": {"Condition": {"Fn::Equals": [1]}},
            },
        }
    )
    assert "ROS1305" in _codes(invalid_arity)

    ignored = _validate(
        {
            "Type": "String",
            "AssociationPropertyMetadata": {
                "Visible": {
                    "Condition": {
                        "Fn::Equals": [1, 1],
                        "Fn::Contains": [[1], 1],
                    }
                },
            },
        }
    )
    assert any(item.code == "ROS5302" and item.subject == "Fn::Contains" for item in ignored.diagnostics)

    js_integer_first_key = _validate(
        {
            "Type": "String",
            "AssociationPropertyMetadata": {
                "Visible": {
                    "Condition": {
                        "Fn::Equals": [1, 1],
                        "0": "unsupported",
                    }
                },
            },
        }
    )
    assert "ROS1305" in _codes(js_integer_first_key)

    unresolved = _validate(
        {
            "Type": "String",
            "AssociationPropertyMetadata": {"Visible": {"Condition": "Conditions.Visible"}},
        }
    )
    assert "ROS5305" in _codes(unresolved)

    resolved = _validate(
        {
            "Type": "String",
            "AssociationPropertyMetadata": {"Visible": {"Condition": "Conditions.Visible"}},
        },
        template_metadata={
            "ALIYUN::ROS::Interface": {"Definitions": {"Conditions": {"Visible": {"Fn::Equals": [1, 1]}}}}
        },
    )
    assert "ROS1306" not in _codes(resolved)
    assert "ROS5305" not in _codes(resolved)

    for lodash_path, definitions in (
        ("Conditions[0]", {"Conditions": [{"Fn::Equals": [1, 1]}]}),
        ("Groups['A.B']", {"Groups": {"A.B": {"Fn::Equals": [1, 1]}}}),
        ("A.B", {"A.B": {"Fn::Equals": [1, 1]}, "A": {}}),
        ("Conditions[ foo ]", {"Conditions": {"foo": {"Fn::Equals": [1, 1]}}}),
        ("A[.5]", {"A": {".5": {"Fn::Equals": [1, 1]}}}),
        ("A[[x]]", {"A": {"[x]": {"Fn::Equals": [1, 1]}}}),
        ("S[0]", {"S": "abc"}),
        ("S[1]", {"S": "😀"}),
        ("S.length", {"S": "abc"}),
        ("A.length", {"A": [1, 2]}),
    ):
        lodash_resolved = _validate(
            {
                "Type": "String",
                "AssociationPropertyMetadata": {"Visible": {"Condition": lodash_path}},
            },
            template_metadata={"ALIYUN::ROS::Interface": {"Definitions": definitions}},
        )
        assert not any(
            item.code in {"ROS1306", "ROS5305"} and item.subject == lodash_path for item in lodash_resolved.diagnostics
        )

    whitespace_is_trimmed = _validate(
        {
            "Type": "String",
            "AssociationPropertyMetadata": {"Visible": {"Condition": "Conditions[ foo ]"}},
        },
        template_metadata={
            "ALIYUN::ROS::Interface": {"Definitions": {"Conditions": {" foo ": {"Fn::Equals": [1, 1]}}}}
        },
    )
    assert any(
        item.code == "ROS1306" and item.subject == "Conditions[ foo ]" for item in whitespace_is_trimmed.diagnostics
    )

    string_index_out_of_range = _validate(
        {
            "Type": "String",
            "AssociationPropertyMetadata": {"Visible": {"Condition": "S[3]"}},
        },
        template_metadata={"ALIYUN::ROS::Interface": {"Definitions": {"S": "abc"}}},
    )
    assert any(item.code == "ROS1306" and item.subject == "S[3]" for item in string_index_out_of_range.diagnostics)

    non_bmp_index_out_of_range = _validate(
        {
            "Type": "String",
            "AssociationPropertyMetadata": {"Visible": {"Condition": "S[2]"}},
        },
        template_metadata={"ALIYUN::ROS::Interface": {"Definitions": {"S": "😀"}}},
    )
    assert any(item.code == "ROS1306" and item.subject == "S[2]" for item in non_bmp_index_out_of_range.diagnostics)

    noncanonical_array_index = _validate(
        {
            "Type": "String",
            "AssociationPropertyMetadata": {"Visible": {"Condition": "Conditions[01]"}},
        },
        template_metadata={
            "ALIYUN::ROS::Interface": {"Definitions": {"Conditions": [{"Fn::Equals": [1, 1]}, {"Fn::Equals": [1, 1]}]}}
        },
    )
    assert any(
        item.code == "ROS1306" and item.subject == "Conditions[01]" for item in noncanonical_array_index.diagnostics
    )

    missing = _validate(
        {
            "Type": "String",
            "AssociationPropertyMetadata": {"Visible": {"Condition": "Conditions.Missing"}},
        },
        template_metadata={"ALIYUN::ROS::Interface": {"Definitions": {"Conditions": {}}}},
    )
    assert "ROS1306" in _codes(missing)

    invalid_resolved_ast = _validate(
        {
            "Type": "String",
            "AssociationPropertyMetadata": {"Visible": {"Condition": "Broken"}},
        },
        template_metadata={"ALIYUN::ROS::Interface": {"Definitions": {"Broken": {"Fn::Equals": [1]}}}},
    )
    assert "ROS1305" in _codes(invalid_resolved_ast)

    invalid_resolved_js_integer_first_key = _validate(
        {
            "Type": "String",
            "AssociationPropertyMetadata": {"Visible": {"Condition": "IntegerFirst"}},
        },
        template_metadata={
            "ALIYUN::ROS::Interface": {
                "Definitions": {
                    "IntegerFirst": {
                        "Fn::Equals": [1, 1],
                        "0": "unsupported",
                    }
                }
            }
        },
    )
    assert "ROS1305" in _codes(invalid_resolved_js_integer_first_key)

    profile_value_dependent = _validate(
        {
            "Type": "String",
            "AssociationPropertyMetadata": {"Visible": {"Condition": "Shared"}},
        },
        template_metadata={
            "ALIYUN::ROS::Interface": {"Definitions": {"Shared": {"Fn::Equals": [1, 1]}}},
            "APSARA::ROS::Interface": {"Definitions": {"Shared": {"Fn::Equals": [1]}}},
        },
    )
    assert "ROS1305" not in _codes(profile_value_dependent)
    assert any(item.code == "ROS5305" and item.subject == "Shared" for item in profile_value_dependent.diagnostics)

    profile_first_key_dependent = _validate(
        {
            "Type": "String",
            "AssociationPropertyMetadata": {"Visible": {"Condition": "Shared"}},
        },
        template_metadata={
            "ALIYUN::ROS::Interface": {
                "Definitions": {
                    "Shared": {
                        "Fn::Equals": [1, 1],
                        "Fn::Contains": [[1], 1],
                    }
                }
            },
            "APSARA::ROS::Interface": {
                "Definitions": {
                    "Shared": {
                        "Fn::Contains": [[1], 1],
                        "Fn::Equals": [1, 1],
                    }
                }
            },
        },
    )
    assert any(item.code == "ROS5305" and item.subject == "Shared" for item in profile_first_key_dependent.diagnostics)

    environment_dependent = _validate(
        {
            "Type": "String",
            "AssociationPropertyMetadata": {"Visible": {"Condition": "Conditions.Visible"}},
        },
        template_metadata={
            "ALIYUN::ROS::Interface": {"Definitions": {"Conditions": {}}},
            "APSARA::ROS::Interface": {"Definitions": {"Conditions": {"Visible": {"Fn::Equals": [1, 1]}}}},
        },
    )
    assert "ROS1306" not in _codes(environment_dependent)
    assert any(
        item.code == "ROS5305" and item.subject == "Conditions.Visible" for item in environment_dependent.diagnostics
    )

    for unavailable_aliyun in ({}, {"Definitions": []}):
        unavailable_profile = _validate(
            {
                "Type": "String",
                "AssociationPropertyMetadata": {"Visible": {"Condition": "Conditions.Visible"}},
            },
            template_metadata={
                "ALIYUN::ROS::Interface": unavailable_aliyun,
                "APSARA::ROS::Interface": {"Definitions": {"Conditions": {"Visible": {"Fn::Equals": [1, 1]}}}},
            },
        )
        assert not any(
            item.code == "ROS1306" and item.subject == "Conditions.Visible" for item in unavailable_profile.diagnostics
        )
        assert any(
            item.code == "ROS5305" and item.subject == "Conditions.Visible" for item in unavailable_profile.diagnostics
        )

    both_unavailable = _validate(
        {
            "Type": "String",
            "AssociationPropertyMetadata": {"Visible": {"Condition": "Conditions.Visible"}},
        },
        template_metadata={
            "ALIYUN::ROS::Interface": {},
            "APSARA::ROS::Interface": {"Definitions": []},
        },
    )
    assert any(item.code == "ROS5305" and item.subject == "Conditions.Visible" for item in both_unavailable.diagnostics)

    missing_in_both_profiles = _validate(
        {
            "Type": "String",
            "AssociationPropertyMetadata": {"Visible": {"Condition": "Conditions.Missing"}},
        },
        template_metadata={
            "ALIYUN::ROS::Interface": {"Definitions": {"Conditions": {}}},
            "APSARA::ROS::Interface": {"Definitions": {"Conditions": {}}},
        },
    )
    assert "ROS1306" in _codes(missing_in_both_profiles)


def test_condition_definitions_coerce_yaml_scalar_keys_like_javascript_objects() -> None:
    report = validate_ros_template(
        MaterializedTemplateSource(
            """
ROSTemplateFormatVersion: '2015-09-01'
Metadata:
  ALIYUN::ROS::Interface:
    Definitions:
      Numeric:
        1:
          Fn::Equals: [1, 1]
      Nullish:
        null:
          Fn::Equals: [1, 1]
      Boolean:
        true:
          Fn::Equals: [1, 1]
      Float:
        1.5:
          Fn::Equals: [1, 1]
Parameters:
  NumericTarget:
    Type: String
    AssociationPropertyMetadata:
      Visible:
        Condition: Numeric.1
  NullTarget:
    Type: String
    AssociationPropertyMetadata:
      Visible:
        Condition: Nullish.null
  BooleanTarget:
    Type: String
    AssociationPropertyMetadata:
      Visible:
        Condition: Boolean.true
  FloatTarget:
    Type: String
    AssociationPropertyMetadata:
      Visible:
        Condition: Float[1.5]
Resources: {}
""".strip(),
            origin="yaml-definitions-test",
        ),
        RequestValidationContext(action="ValidateTemplate"),
    )

    subjects = {"Numeric.1", "Nullish.null", "Boolean.true", "Float[1.5]"}
    assert not [item for item in report.diagnostics if item.code in {"ROS1306", "ROS5305"} and item.subject in subjects]


def test_condition_definitions_preserve_javascript_distinct_boolean_and_numeric_yaml_keys() -> None:
    report = validate_ros_template(
        MaterializedTemplateSource(
            """
ROSTemplateFormatVersion: '2015-09-01'
Metadata:
  ALIYUN::ROS::Interface:
    Definitions:
      true:
        Fn::Equals: [1, 1]
      1:
        Unknown: invalid
      false:
        Fn::Equals: [1, 1]
      0:
        Unknown: invalid
Parameters:
  BooleanTrue:
    Type: String
    AssociationPropertyMetadata:
      Visible: {Condition: 'true'}
  NumericOne:
    Type: String
    AssociationPropertyMetadata:
      Visible: {Condition: '1'}
  BooleanFalse:
    Type: String
    AssociationPropertyMetadata:
      Visible: {Condition: 'false'}
  NumericZero:
    Type: String
    AssociationPropertyMetadata:
      Visible: {Condition: '0'}
Resources: {}
""".strip(),
            origin="typed-definition-keys.yaml",
        ),
        RequestValidationContext(action="ValidateTemplate"),
    )

    condition_errors = [item for item in report.diagnostics if item.code == "ROS1305"]
    assert not any("BooleanTrue" in display_path(item.path) for item in condition_errors)
    assert not any("BooleanFalse" in display_path(item.path) for item in condition_errors)
    assert any("NumericOne" in display_path(item.path) for item in condition_errors)
    assert any("NumericZero" in display_path(item.path) for item in condition_errors)


def test_condition_children_select_and_not_shapes() -> None:
    report = _validate(
        {
            "Type": "String",
            "AssociationPropertyMetadata": {
                "Visible": {
                    "Condition": {
                        "Fn::And": [
                            {"Fn::Equals": [1, 1]},
                            1,
                            {"Fn::Not": {}},
                            {"Fn::Select": ["${P}"]},
                            {"Fn::Select": ["${P}", "key", "ignored"]},
                        ]
                    }
                },
            },
        },
        extra_parameters={"P": {"Type": "Json"}},
    )

    assert sum(item.code == "ROS1305" for item in report.diagnostics) >= 2
    assert any(item.code == "ROS5302" and item.subject == "2" for item in report.diagnostics)

    short_select = _validate(
        {
            "Type": "String",
            "AssociationPropertyMetadata": {
                "Visible": {
                    "Condition": {
                        "Fn::Equals": [
                            {"Fn::Select": ["${Missing}"]},
                            "value",
                        ]
                    }
                }
            },
        }
    )
    assert "ROS1305" not in _codes(short_select)
    assert not any(item.code == "ROS1306" and item.subject == "Missing" for item in short_select.diagnostics)

    nested_string = _validate(
        {
            "Type": "String",
            "AssociationPropertyMetadata": {"Visible": {"Condition": {"Fn::And": ["Conditions.Visible"]}}},
        }
    )
    assert "ROS1305" in _codes(nested_string)


def test_condition_operands_validate_parameter_references_and_fn_select_uses_operand_semantics() -> None:
    missing = _validate(
        {
            "Type": "String",
            "AssociationPropertyMetadata": {
                "Visible": {
                    "Condition": {
                        "Fn::And": [
                            {"Fn::Equals": ["${MissingEquals}", 1]},
                            {"Fn::Contains": [[1], "${MissingContains}"]},
                            {"Fn::Select": ["${MissingSelect}", "key"]},
                        ]
                    }
                }
            },
        }
    )
    assert {"MissingEquals", "MissingContains", "MissingSelect"} <= {
        item.subject for item in missing.diagnostics if item.code == "ROS1306"
    }

    non_first_select = _validate(
        {
            "Type": "String",
            "AssociationPropertyMetadata": {
                "Visible": {
                    "Condition": {
                        "Fn::Equals": [
                            {"Other": 1, "Fn::Select": ["${P}", "key"]},
                            "value",
                        ]
                    }
                }
            },
        },
        extra_parameters={"P": {"Type": "Json"}},
    )
    assert not any(
        item.code == "ROS1305" and item.subject and "first function key" in item.subject
        for item in non_first_select.diagnostics
    )
    assert not any(item.code == "ROS1306" and item.subject == "P" for item in non_first_select.diagnostics)

    spaced_references = _validate(
        {
            "Type": "String",
            "AssociationPropertyMetadata": {
                "DynamicValue": [
                    {
                        "Condition": {
                            "Fn::And": [
                                {"Fn::Equals": ["${ P }", "value"]},
                                {"Fn::Equals": ["${ RegionId }", "cn-hangzhou"]},
                                {"Fn::Select": ["${ P }", "key"]},
                            ]
                        },
                        "Value": "value",
                    }
                ]
            },
        },
        extra_parameters={"P": {"Type": "Json"}},
    )
    assert {" P ", " RegionId "} <= {item.subject for item in spaced_references.diagnostics if item.code == "ROS1306"}


def test_condition_references_use_exact_keys_and_select_short_circuits_empty_arguments() -> None:
    nested_only = _validate(
        {
            "Type": "String",
            "AssociationPropertyMetadata": {
                "Visible": {
                    "Condition": {
                        "Fn::And": [
                            {"Fn::Equals": ["${Object.Name}", "value"]},
                            {"Fn::Select": ["${Object.Name}", "key"]},
                        ]
                    }
                }
            },
        },
        extra_parameters={
            "Object": {
                "Type": "Json",
                "AssociationProperty": "Json",
                "AssociationPropertyMetadata": {"Parameters": {"Name": {"Type": "String"}}},
            }
        },
    )
    assert sum(item.code == "ROS1306" and item.subject == "Object.Name" for item in nested_only.diagnostics) == 2

    exact_dotted_key = _validate(
        {
            "Type": "String",
            "AssociationPropertyMetadata": {
                "Visible": {
                    "Condition": {
                        "Fn::And": [
                            {"Fn::Equals": ["${Object.Name}", "value"]},
                            {"Fn::Select": ["${Object.Name}", "key"]},
                        ]
                    }
                }
            },
        },
        extra_parameters={"Object.Name": {"Type": "Json"}},
    )
    assert not any(item.code == "ROS1306" and item.subject == "Object.Name" for item in exact_dotted_key.diagnostics)

    empty_arguments = _validate(
        {
            "Type": "String",
            "AssociationPropertyMetadata": {
                "Visible": {
                    "Condition": {
                        "Fn::And": [
                            {"Fn::Select": ["", "key"]},
                            {"Fn::Select": ["${Missing}", ""]},
                        ]
                    }
                }
            },
        }
    )
    assert not any(
        item.code in {"ROS1305", "ROS1306"} and item.subject in {"", "Missing"} for item in empty_arguments.diagnostics
    )

    empty_arguments_with_tail = _validate(
        {
            "Type": "String",
            "AssociationPropertyMetadata": {
                "Visible": {
                    "Condition": {
                        "Fn::And": [
                            {"Fn::Select": ["", "key", "ignored"]},
                            {"Fn::Select": ["${Missing}", "", "ignored"]},
                        ]
                    }
                }
            },
        }
    )
    assert sum(item.code == "ROS5302" and item.subject == "2" for item in empty_arguments_with_tail.diagnostics) == 2
    assert not any(
        item.code in {"ROS1305", "ROS1306"} and item.subject in {"", "Missing"}
        for item in empty_arguments_with_tail.diagnostics
    )


def test_field_reference_context_overrides_nested_parameter_context() -> None:
    report = _validate(
        {
            "Type": "Json",
            "AssociationProperty": "List[Parameter]",
            "AssociationPropertyMetadata": {
                "Parameter": {
                    "Type": "String",
                    "AssociationPropertyMetadata": {"DynamicValue": "prefix-${P}"},
                }
            },
        },
        extra_parameters={"P": {"Type": "String"}},
    )

    assert not [item for item in report.diagnostics if item.subject == "P" and item.code in {"ROS1306", "ROS5305"}]


def test_nested_parameter_map_and_meta_list_row_reference_contexts() -> None:
    nested_map = _validate(
        {
            "Type": "Json",
            "AssociationProperty": "Json",
            "AssociationPropertyMetadata": {
                "Parameters": {
                    "Local": {"Type": "String"},
                    "Dependent": {
                        "Type": "String",
                        "AssociationPropertyMetadata": {"DynamicValue": "${Local}"},
                    },
                }
            },
        }
    )
    assert not [
        item for item in nested_map.diagnostics if item.subject == "Local" and item.code in {"ROS1306", "ROS5305"}
    ]

    meta_list = _validate(
        {
            "Type": "Json",
            "AssociationProperty": "ALIYUN::ROS::Type::MetaList",
            "AssociationPropertyMetadata": {
                "Parameters": {
                    "Sibling": {"Type": "String"},
                    "Dependent": {
                        "Type": "String",
                        "AssociationProperty": "ALIYUN::Hologres::Instance::InstanceId",
                        "AssociationPropertyMetadata": {"cmsInstanceType": "${.Sibling}"},
                    },
                }
            },
        }
    )
    assert not [
        item for item in meta_list.diagnostics if item.subject == ".Sibling" and item.code in {"ROS1306", "ROS5305"}
    ]

    missing_row_field = _validate(
        {
            "Type": "Json",
            "AssociationProperty": "ALIYUN::ROS::Type::MetaList",
            "AssociationPropertyMetadata": {
                "Parameters": {
                    "Dependent": {
                        "Type": "String",
                        "AssociationProperty": "ALIYUN::Hologres::Instance::InstanceId",
                        "AssociationPropertyMetadata": {"cmsInstanceType": "${.Missing}"},
                    }
                }
            },
        }
    )
    assert any(item.code == "ROS1306" and item.subject == ".Missing" for item in missing_row_field.diagnostics)


def test_nested_reference_validation_uses_local_declarations_full_paths_and_related_locations() -> None:
    local = _validate(
        {
            "Type": "Json",
            "AssociationProperty": "Json",
            "AssociationPropertyMetadata": {
                "Parameters": {
                    "Flag": {"Type": "Boolean"},
                    "Dependent": {
                        "Type": "String",
                        "AssociationProperty": "AutoCompleteInput",
                        "AssociationPropertyMetadata": {"Length": "Flag"},
                    },
                }
            },
        },
        extra_parameters={"Flag": {"Type": "Number"}},
    )
    type_warning = next(item for item in local.diagnostics if item.code == "ROS5302" and item.subject == "Flag")
    assert type_warning.actual == "Boolean"
    assert type_warning.related_locations
    assert "AssociationPropertyMetadata.Parameters.Flag" in display_path(type_warning.related_locations[0].path)

    malformed_and_missing_path = _validate(
        {
            "Type": "Json",
            "AssociationProperty": "Json",
            "AssociationPropertyMetadata": {
                "Parameters": {
                    "Flag": {"Type": "Boolean"},
                    "Malformed": {
                        "Type": "String",
                        "AssociationPropertyMetadata": {"DynamicValue": "${Flag[}"},
                    },
                    "MissingPath": {
                        "Type": "String",
                        "AssociationPropertyMetadata": {"DynamicValue": "${Flag.missing}"},
                    },
                }
            },
        }
    )
    assert {"Flag[", "Flag.missing"} <= {
        item.subject for item in malformed_and_missing_path.diagnostics if item.code == "ROS1306"
    }

    known_path = _validate(
        {
            "Type": "Json",
            "AssociationProperty": "Json",
            "AssociationPropertyMetadata": {
                "Parameters": {
                    "Object": {
                        "Type": "Json",
                        "AssociationPropertyMetadata": {"Parameters": {"Name": {"Type": "String"}}},
                    },
                    "Dependent": {
                        "Type": "String",
                        "AssociationPropertyMetadata": {"DynamicValue": "${Object.Name}"},
                    },
                }
            },
        }
    )
    assert any(item.subject == "Object.Name" and item.code == "ROS1306" for item in known_path.diagnostics)

    known_array_path = _validate(
        {
            "Type": "Json",
            "AssociationProperty": "Json",
            "AssociationPropertyMetadata": {
                "Parameters": {
                    "Rows": {
                        "Type": "Json",
                        "AssociationProperty": "List[Parameter]",
                        "AssociationPropertyMetadata": {
                            "Parameter": {
                                "Type": "Json",
                                "AssociationPropertyMetadata": {"Parameters": {"Name": {"Type": "String"}}},
                            }
                        },
                    },
                    "Dependent": {
                        "Type": "String",
                        "AssociationPropertyMetadata": {"DynamicValue": "${Rows[].Name}"},
                    },
                }
            },
        }
    )
    assert any(item.subject == "Rows[].Name" and item.code == "ROS1306" for item in known_array_path.diagnostics)


def test_merged_common_and_component_any_of_preserves_branch_reference_parser() -> None:
    report = _validate(
        {
            "Type": "String",
            "AssociationProperty": "Code",
            "AssociationPropertyMetadata": {"ReadOnly": "${Flag}"},
        },
        extra_parameters={"Flag": {"Type": "Boolean"}},
    )

    assert not any(item.code == "ROS1305" and "ReadOnly" in display_path(item.path) for item in report.diagnostics)
    assert not any(item.code == "ROS1306" and item.subject == "Flag" for item in report.diagnostics)


@pytest.mark.parametrize("literal", ["${", "x${P}", "{{bad"])
def test_unmatched_whole_value_reference_fragments_remain_literals(literal: str) -> None:
    report = _validate(
        {
            "Type": "String",
            "AssociationProperty": "AutoCompleteInput",
            "AssociationPropertyMetadata": {"Prefix": literal},
        },
        extra_parameters={"P": {"Type": "String"}},
    )
    assert not any(item.code in {"ROS1306", "ROS5305"} and item.subject == literal for item in report.diagnostics)


def test_whole_value_reference_accepts_exact_parameter_keys_without_identifier_grammar() -> None:
    report = _validate(
        {
            "Type": "String",
            "AssociationProperty": "AutoCompleteInput",
            "AssociationPropertyMetadata": {"Prefix": "${1A}"},
        },
        extra_parameters={"1A": {"Type": "String"}},
    )

    assert not any(item.code in {"ROS1306", "ROS5305"} and item.subject == "1A" for item in report.diagnostics)


@pytest.mark.parametrize("parameter_key", ["A{B}", "{{env.path}}", "{{}}"])
def test_whole_value_reference_uses_greedy_and_legacy_exact_parameter_keys(parameter_key: str) -> None:
    encoded = "${A{B}}" if parameter_key == "A{B}" else parameter_key
    report = _validate(
        {
            "Type": "String",
            "AssociationProperty": "AutoCompleteInput",
            "AssociationPropertyMetadata": {"Prefix": encoded},
        },
        extra_parameters={parameter_key: {"Type": "String"}},
    )

    assert not any(item.code == "ROS1306" for item in report.diagnostics)
    assert not any(
        item.code == "ROS5305" and "Environment metadata reference" in item.summary for item in report.diagnostics
    )


def test_whole_value_escaped_literal_can_resolve_a_transformed_legacy_parameter_key() -> None:
    report = _validate(
        {
            "Type": "String",
            "AssociationProperty": "AutoCompleteInput",
            "AssociationPropertyMetadata": {"Prefix": "${!literal}"},
        },
        extra_parameters={"${literal}": {"Type": "String"}},
    )

    assert not any(item.code == "ROS1306" for item in report.diagnostics)


@pytest.mark.parametrize(
    ("encoded", "parameter_key"),
    [
        ("${1A}", "1A"),
        ("${A{B}}", "A{B}"),
        ("1A", "1A"),
        ("${!literal}", "${literal}"),
    ],
)
def test_whole_value_nonstandard_exact_keys_keep_reference_type_warnings(
    encoded: str,
    parameter_key: str,
) -> None:
    report = _validate(
        {
            "Type": "String",
            "AssociationProperty": "AutoCompleteInput",
            "AssociationPropertyMetadata": {"Length": encoded},
        },
        extra_parameters={parameter_key: {"Type": "String"}},
    )

    warning = next(
        item for item in report.diagnostics if item.code == "ROS5302" and "reference type is suspicious" in item.summary
    )
    assert warning.subject == parameter_key
    assert warning.related_locations
    assert warning.related_locations[0].path[-1].value == parameter_key


def test_yaml_scalar_parameter_keys_use_javascript_property_lookup_in_every_exact_parser() -> None:
    report = _validate_yaml(
        """ROSTemplateFormatVersion: '2015-09-01'
Parameters:
  Target:
    Type: String
    AssociationProperty: AutoCompleteInput
    AssociationPropertyMetadata:
      Prefix: "${true}"
      Suffix: "${1}"
      DynamicValue: "value-${false}"
      MappingMetadata:
        MappingName: Example
        MappedPropsName: AllowedValues
        ValueSelector: "${null}.${0}"
      Visible:
        Condition:
          Fn::Equals: ["${false}", enabled]
  true:
    Type: String
  1:
    Type: Number
  false:
    Type: String
  0:
    Type: Number
  null:
    Type: String
Resources: {}
"""
    )

    assert not any(
        item.code in {"ROS1306", "ROS5305"} and item.subject in {"true", "1", "false", "0", "null"}
        for item in report.diagnostics
    )
    type_warnings = [
        item
        for item in report.diagnostics
        if item.code == "ROS5302" and item.subject in {"true", "1"} and "reference type is suspicious" in item.summary
    ]
    assert [item.subject for item in type_warnings] == ["1"]
    assert association_property_rule._js_property_key(type_warnings[0].related_locations[0].path[-1].value) == "1"


def test_yaml_boolean_and_numeric_parameter_siblings_are_all_validated() -> None:
    report = _validate_yaml(
        """ROSTemplateFormatVersion: '2015-09-01'
Parameters:
  true:
    Type: String
    AssociationProperty: AutoCompleteInput
    AssociationPropertyMetadata:
      Length: 8
      CharacterClasses:
        - Class: digit
          Min: 1
  1:
    Type: String
    AssociationProperty: AutoCompleteInput
    AssociationPropertyMetadata:
      Length: 8
      CharacterClasses:
        - Class: number
          Min: 1
  false:
    Type: String
    AssociationProperty: AutoCompleteInput
    AssociationPropertyMetadata:
      Length: 8
      CharacterClasses:
        - Class: digit
          Min: 1
  0:
    Type: String
    AssociationProperty: AutoCompleteInput
    AssociationPropertyMetadata:
      Length: 8
      CharacterClasses:
        - Class: number
          Min: 1
Resources: {}
"""
    )

    invalid_classes = [item for item in report.diagnostics if item.code == "ROS1305" and item.subject == "digit"]
    assert len(invalid_classes) == 2


def test_yaml_large_integer_parameter_keys_follow_binary64_rounding_and_last_wins() -> None:
    report = _validate_yaml(
        """ROSTemplateFormatVersion: '2015-09-01'
Parameters:
  Target:
    Type: String
    AssociationProperty: AutoCompleteInput
    AssociationPropertyMetadata:
      Prefix: "${9007199254740992.Name}"
      Suffix: "${9007199254740992}"
      DynamicValue: "value-${9007199254740992}"
      MappingMetadata:
        MappingName: Example
        MappedPropsName: AllowedValues
        ValueSelector: "${9007199254740992}"
      Visible:
        Condition:
          Fn::Equals: ["${9007199254740992}", enabled]
  9007199254740992:
    Type: String
    AssociationProperty: AutoCompleteInput
    AssociationPropertyMetadata:
      Length: 8
      CharacterClasses:
        - Class: digit
          Min: 1
  9007199254740993:
    Type: Json
    AssociationProperty: Json
    AssociationPropertyMetadata:
      Parameters:
        Name:
          Type: String
Resources: {}
"""
    )

    rounded_key = "9007199254740992"
    assert not any(item.code in {"ROS1306", "ROS5305"} and item.subject == rounded_key for item in report.diagnostics)
    assert not any(item.code == "ROS1305" and item.subject == "digit" for item in report.diagnostics)
    warning = next(
        item
        for item in report.diagnostics
        if item.code == "ROS5302" and item.subject == rounded_key and "reference type is suspicious" in item.summary
    )
    assert warning.related_locations[0].path[-1].value == 9007199254740993


def test_nested_yaml_scalar_parameter_keys_use_source_mapped_javascript_properties() -> None:
    report = _validate_yaml(
        """ROSTemplateFormatVersion: '2015-09-01'
Parameters:
  Target:
    Type: String
    AssociationProperty: AutoCompleteInput
    AssociationPropertyMetadata:
      Prefix: "${Object.true}"
      Suffix: "${Object.1}"
      Length: "${Object.0}"
      Pattern: "${Object.false}"
      CharacterClasses: "${Object.9007199254740992}"
      Required: "${Object.null}"
  Object:
    Type: Json
    AssociationProperty: Json
    AssociationPropertyMetadata:
      Parameters:
        true:
          Type: String
        1:
          Type: Number
        false:
          Type: String
        0:
          Type: Number
        null:
          Type: Boolean
        9007199254740992:
          Type: String
          AssociationProperty: AutoCompleteInput
          AssociationPropertyMetadata:
            Length: 8
            CharacterClasses:
              - Class: digit
                Min: 1
        9007199254740993:
          Type: CommaDelimitedList
Resources: {}
"""
    )

    names = {
        "Object.true",
        "Object.1",
        "Object.false",
        "Object.0",
        "Object.null",
        "Object.9007199254740992",
    }
    assert not any(item.code in {"ROS1306", "ROS5305"} and item.subject in names for item in report.diagnostics)
    assert not any(item.code == "ROS1305" and item.subject == "digit" for item in report.diagnostics)
    warning = next(
        item
        for item in report.diagnostics
        if item.code == "ROS5302" and item.subject == "Object.1" and "reference type is suspicious" in item.summary
    )
    assert warning.related_locations[0].path[-1].value == 1


def test_whole_value_field_paths_validate_terminal_type_projection_and_location() -> None:
    object_parameter = {
        "Type": "Json",
        "AssociationProperty": "Json",
        "AssociationPropertyMetadata": {"Parameters": {"Name": {"Type": "Number"}}},
    }
    mismatch = _validate(
        {
            "Type": "String",
            "AssociationProperty": "AutoCompleteInput",
            "AssociationPropertyMetadata": {"Prefix": "${Object.Name}"},
        },
        extra_parameters={"Object": object_parameter},
    )
    warning = next(item for item in mismatch.diagnostics if item.code == "ROS5302" and item.subject == "Object.Name")
    assert warning.actual == "Number"
    assert warning.related_locations
    assert display_path(warning.related_locations[0].path).endswith(
        "Object.AssociationPropertyMetadata.Parameters.Name"
    )

    matching_object = _validate(
        {
            "Type": "String",
            "AssociationProperty": "AutoCompleteInput",
            "AssociationPropertyMetadata": {"Prefix": "${Object.Name}"},
        },
        extra_parameters={
            "Object": {
                "Type": "Json",
                "AssociationProperty": "Json",
                "AssociationPropertyMetadata": {"Parameters": {"Name": {"Type": "String"}}},
            }
        },
    )
    assert not any(item.code == "ROS5302" and item.subject == "Object.Name" for item in matching_object.diagnostics)

    rows_parameter = {
        "Type": "Json",
        "AssociationProperty": "List[Parameter]",
        "AssociationPropertyMetadata": {
            "Parameter": {
                "Type": "Json",
                "AssociationProperty": "Json",
                "AssociationPropertyMetadata": {"Parameters": {"Name": {"Type": "String"}}},
            }
        },
    }
    string_projection_to_object_items = _validate(
        {
            "Type": "String",
            "AssociationProperty": "AutoCompleteInput",
            "AssociationPropertyMetadata": {"CharacterClasses": "${Rows[].Name}"},
        },
        extra_parameters={"Rows": rows_parameter},
    )
    item_warning = next(
        item
        for item in string_projection_to_object_items.diagnostics
        if item.code == "ROS5302" and item.subject == "Rows[].Name"
    )
    assert item_warning.actual == "array<string>"
    assert item_warning.expected == "array<object>"
    assert item_warning.related_locations

    matching_object_projection = _validate(
        {
            "Type": "String",
            "AssociationProperty": "AutoCompleteInput",
            "AssociationPropertyMetadata": {"CharacterClasses": "${Rows[].Details}"},
        },
        extra_parameters={
            "Rows": {
                "Type": "Json",
                "AssociationProperty": "List[Parameter]",
                "AssociationPropertyMetadata": {
                    "Parameter": {
                        "Type": "Json",
                        "AssociationPropertyMetadata": {"Parameters": {"Details": {"Type": "Json"}}},
                    }
                },
            }
        },
    )
    assert not any(
        item.code == "ROS5302" and item.subject == "Rows[].Details" for item in matching_object_projection.diagnostics
    )

    parameters_row_projection = _validate(
        {
            "Type": "String",
            "AssociationProperty": "AutoCompleteInput",
            "AssociationPropertyMetadata": {"CharacterClasses": "${Rows[].Name}"},
        },
        extra_parameters={
            "Rows": {
                "Type": "Json",
                "AssociationProperty": "List[Parameters]",
                "AssociationPropertyMetadata": {"Parameters": {"Name": {"Type": "String"}}},
            }
        },
    )
    parameters_row_warning = next(
        item
        for item in parameters_row_projection.diagnostics
        if item.code == "ROS5302" and item.subject == "Rows[].Name"
    )
    assert parameters_row_warning.actual == "array<string>"
    assert parameters_row_warning.expected == "array<object>"
    assert display_path(parameters_row_warning.related_locations[0].path).endswith(
        "Rows.AssociationPropertyMetadata.Parameters.Name"
    )

    meta_list_with_both_row_shapes = _validate(
        {
            "Type": "String",
            "AssociationProperty": "AutoCompleteInput",
            "AssociationPropertyMetadata": {"CharacterClasses": "${Rows[].Name}"},
        },
        extra_parameters={
            "Rows": {
                "Type": "Json",
                "AssociationProperty": "ALIYUN::ROS::Type::MetaList",
                "AssociationPropertyMetadata": {
                    "Parameter": {
                        "Type": "Json",
                        "AssociationPropertyMetadata": {"Parameters": {"Wrong": {"Type": "Number"}}},
                    },
                    "Parameters": {"Name": {"Type": "String"}},
                },
            }
        },
    )
    meta_list_warning = next(
        item
        for item in meta_list_with_both_row_shapes.diagnostics
        if item.code == "ROS5302" and item.subject == "Rows[].Name"
    )
    assert meta_list_warning.actual == "array<string>"
    assert display_path(meta_list_warning.related_locations[0].path).endswith(
        "Rows.AssociationPropertyMetadata.Parameters.Name"
    )
    assert not any(
        item.code == "ROS5305" and item.subject == "Rows[].Name" for item in meta_list_with_both_row_shapes.diagnostics
    )

    scalar_consumer_projection = _validate(
        {
            "Type": "String",
            "AssociationProperty": "AutoCompleteInput",
            "AssociationPropertyMetadata": {"Prefix": "${Rows[].Name}"},
        },
        extra_parameters={"Rows": rows_parameter},
    )
    projection_warning = next(
        item
        for item in scalar_consumer_projection.diagnostics
        if item.code == "ROS5302" and item.subject == "Rows[].Name"
    )
    assert projection_warning.actual == "array<string>"

    runtime_component_path = _validate(
        {
            "Type": "String",
            "AssociationProperty": "AutoCompleteInput",
            "AssociationPropertyMetadata": {"Prefix": "${Object.Name}"},
        },
        extra_parameters={
            "Object": {
                "Type": "Json",
                "AssociationPropertyMetadata": {"Parameters": {"Other": {"Type": "String"}}},
            }
        },
    )
    assert any(item.code == "ROS5305" and item.subject == "Object.Name" for item in runtime_component_path.diagnostics)
    assert not any(
        item.code == "ROS1306" and item.subject == "Object.Name" for item in runtime_component_path.diagnostics
    )


@pytest.mark.parametrize(
    "reference",
    ["Rows[].Children[].Name", "Object.Children[].Name", "Rows[]"],
)
def test_whole_value_field_paths_reject_unsupported_array_shapes(reference: str) -> None:
    report = _validate(
        {
            "Type": "String",
            "AssociationProperty": "AutoCompleteInput",
            "AssociationPropertyMetadata": {"Prefix": "${" + reference + "}"},
        },
        extra_parameters={"Rows": {"Type": "Json"}, "Object": {"Type": "Json"}},
    )
    diagnostic = next(
        item for item in report.diagnostics if item.subject == reference and item.code in {"ROS1306", "ROS5305"}
    )
    assert diagnostic.actual == "${" + reference + "}"


@pytest.mark.parametrize(
    "reference",
    [
        "Object.Children[0].Name",
        "Rows[].Children[0].Name",
        "Object['A.B']",
        "Object['']",
        "Object[0]Name",
        "Object.[0].Name",
        "Object..Name",
        "Object.",
        "Object]Name",
    ],
)
def test_lodash_fixed_index_and_quoted_key_field_paths_are_valid_but_static_limitations(reference: str) -> None:
    report = _validate(
        {
            "Type": "String",
            "AssociationProperty": "AutoCompleteInput",
            "AssociationPropertyMetadata": {"Prefix": "${" + reference + "}"},
        },
        extra_parameters={"Rows": {"Type": "Json"}, "Object": {"Type": "Json"}},
    )
    assert any(
        item.code == "ROS5305"
        and item.subject == reference
        and "field-path reference cannot be resolved completely" in item.summary
        for item in report.diagnostics
    )
    assert not any(item.code == "ROS1306" and item.subject == reference for item in report.diagnostics)


def test_frontend_greedy_reference_wrapper_defers_non_string_literal_checks() -> None:
    report = _validate(
        {
            "Type": "String",
            "AssociationProperty": "ALIYUN::DomainName",
            "AssociationPropertyMetadata": {
                "MaxLength": "${Object.{Name}}",
                "CheckICP": "${Object]Name}",
            },
        },
        extra_parameters={"Object": {"Type": "Json"}},
    )

    for field, subject in (("MaxLength", "Object.{Name}"), ("CheckICP", "Object]Name")):
        assert any(item.code == "ROS5305" and item.subject == subject for item in report.diagnostics)
        assert not any(
            item.code in {"ROS1305", "ROS1306"}
            and display_path(item.path).endswith(".AssociationPropertyMetadata." + field)
            for item in report.diagnostics
        )


def test_env_wrapper_matches_frontend_whitespace_greedy_and_truthiness_semantics() -> None:
    outer_whitespace = _validate(
        {
            "Type": "String",
            "AssociationProperty": "ALIYUN::DomainName",
            "AssociationPropertyMetadata": {"MaxLength": " {{env.path}} "},
        }
    )
    assert "ROS1305" in _codes(outer_whitespace)
    assert not any(item.code == "ROS5305" and item.subject == " {{env.path}} " for item in outer_whitespace.diagnostics)

    greedy_inner_brace = _validate(
        {
            "Type": "String",
            "AssociationProperty": "ALIYUN::DomainName",
            "AssociationPropertyMetadata": {
                "MaxLength": "{{Object.{Name}}}",
                "CheckICP": "{{Object]Name}}",
            },
        }
    )
    for field, subject in (
        ("MaxLength", "{{Object.{Name}}}"),
        ("CheckICP", "{{Object]Name}}"),
    ):
        assert any(item.code == "ROS5305" and item.subject == subject for item in greedy_inner_brace.diagnostics)
        assert not any(
            item.code == "ROS1305" and display_path(item.path).endswith(".AssociationPropertyMetadata." + field)
            for item in greedy_inner_brace.diagnostics
        )

    empty_env_wrapper = _validate(
        {
            "Type": "String",
            "AssociationProperty": "ALIYUN::DomainName",
            "AssociationPropertyMetadata": {"MaxLength": "{{}}"},
        }
    )
    assert "ROS1305" in _codes(empty_env_wrapper)


def test_lodash_path_fallback_still_respects_the_consumers_declared_reference_kinds() -> None:
    report = _validate(
        {
            "Type": "String",
            "AssociationProperty": "ALIYUN::ACR::Namespace::Name",
            "AssociationPropertyMetadata": {"RegionId": "${Object['']}"},
        },
        extra_parameters={"Object": {"Type": "Json"}},
    )

    assert any(item.code == "ROS5305" and item.subject == "Object['']" for item in report.diagnostics)
    assert not any(item.code == "ROS1306" and item.subject == "Object['']" for item in report.diagnostics)


def test_runtime_dependent_context_does_not_assume_the_root_parameter_scope() -> None:
    report = _validate(
        {
            "Type": "String",
            "AssociationProperty": "ALIYUN::Hologres::Instance::InstanceId",
            "AssociationPropertyMetadata": {"cmsInstanceType": "${Target}"},
        }
    )

    assert any(item.code == "ROS5305" and item.subject == "Target" for item in report.diagnostics)


def test_associated_property_alias_precedence_matches_use_associated_property() -> None:
    alias_only = _validate(
        {
            "Type": "String",
            "AssociationProperty": "ALIYUN::Hologres::Instance::InstanceId",
            "AssociationPropertyMetadata": {"CmsInstanceType": "Standard"},
        }
    )
    assert not [item for item in alias_only.diagnostics if item.code in {"ROS1305", "ROS5304"}]

    both = _validate(
        {
            "Type": "String",
            "AssociationProperty": "ALIYUN::Hologres::Instance::InstanceId",
            "AssociationPropertyMetadata": {
                "CmsInstanceType": "Standard",
                "cmsInstanceType": 123,
            },
        }
    )
    assert not [item for item in both.diagnostics if item.code == "ROS1305"]
    ignored = next(item for item in both.diagnostics if item.subject == "cmsInstanceType")
    assert ignored.code == "ROS5302"
    assert "CmsInstanceType" in ignored.detail


def test_nested_parameter_metadata_is_recursively_validated() -> None:
    report = _validate(
        {
            "Type": "Json",
            "AssociationProperty": "Json",
            "AssociationPropertyMetadata": {
                "Parameters": {
                    "Inner": {
                        "Type": "String",
                        "AssociationProperty": "AutoCompleteInput",
                        "AssociationPropertyMetadata": {"CharacterClasses": [{"Class": "digit", "Min": 1}]},
                    }
                }
            },
        }
    )

    diagnostic = next(item for item in report.diagnostics if item.code == "ROS1305")
    assert display_path(diagnostic.path).endswith(
        "AssociationPropertyMetadata.Parameters.Inner.AssociationPropertyMetadata.CharacterClasses[0].Class"
    )


def test_component_owned_metadata_is_not_blocked_when_its_consumer_is_unreachable() -> None:
    auto_complete = _validate(
        {
            "Type": "String",
            "AssociationProperty": "AutoCompleteInput",
            "AssociationPropertyMetadata": {
                "DisabledValues": "not-an-array",
                "ListMetadata": {"ShowHeader": "not-a-boolean"},
                "Parameter": {
                    "Type": "String",
                    "AssociationProperty": "AutoCompleteInput",
                    "AssociationPropertyMetadata": {"CharacterClasses": [{"Class": "digit", "Min": 1}]},
                },
            },
        }
    )
    assert not [item for item in auto_complete.diagnostics if item.code == "ROS1305"]

    runtime_dependent_json = _validate(
        {
            "Type": "Json",
            "AssociationPropertyMetadata": {
                "Parameters": {
                    "Inner": {
                        "Type": "String",
                        "AssociationProperty": "AutoCompleteInput",
                        "AssociationPropertyMetadata": {"CharacterClasses": [{"Class": "digit", "Min": 1}]},
                    }
                }
            },
        }
    )
    assert not [item for item in runtime_dependent_json.diagnostics if item.code == "ROS1305"]
    assert any(item.code == "ROS5305" and item.subject == "Parameters" for item in runtime_dependent_json.diagnostics)


def test_independent_errors_have_stable_ids_and_precise_source_spans() -> None:
    parameter = {
        "Type": "String",
        "AssociationProperty": "AutoCompleteInput",
        "AssociationPropertyMetadata": {
            "Length": "bad",
            "CharacterClasses": [
                {"Class": "digit", "Min": 1},
                {"Class": "number", "Min": "bad"},
            ],
        },
    }
    first = _validate(parameter)
    second = _validate(parameter)
    diagnostics = [item for item in first.diagnostics if item.code == "ROS1305"]

    assert len(diagnostics) == 3
    assert all(item.source_span is not None for item in diagnostics)
    assert [item.diagnostic_id for item in diagnostics] == [
        item.diagnostic_id for item in second.diagnostics if item.code == "ROS1305"
    ]


@pytest.mark.parametrize("transform", ["Aliyun::Terraform-v1.6", "Aliyun::OpenTofu-v1.8"])
def test_terraform_template_validates_top_level_parameters_and_marks_workspace_limit(transform: str) -> None:
    template = {
        "Transform": transform,
        "Workspace": {"main.tf": "variable {}"},
        "Parameters": {
            "Target": {
                "Type": "String",
                "AssociationProperty": "AutoCompleteInput",
                "AssociationPropertyMetadata": {"CharacterClasses": [{"Class": "digit", "Min": 1}]},
            }
        },
    }
    report = validate_ros_template(
        MaterializedTemplateSource(json.dumps(template)),
        RequestValidationContext(action="ValidateTemplate"),
    )

    assert any(item.code == "ROS1305" and item.subject == "digit" for item in report.diagnostics)
    limitation = next(item for item in report.diagnostics if item.code == "ROS5305")
    assert limitation.severity == Severity.LIMITATION
    assert "Top-level Parameters are validated" in limitation.detail


def test_nested_parameter_depth_limit_is_a_deterministic_error() -> None:
    child: dict = {
        "Type": "String",
        "AssociationProperty": "AutoCompleteInput",
        "AssociationPropertyMetadata": {"CharacterClasses": [{"Class": "number", "Min": 1}]},
    }
    for index in range(18):
        child = {
            "Type": "Json",
            "AssociationProperty": "Json",
            "AssociationPropertyMetadata": {"Parameters": {"Level{}".format(index): child}},
        }

    report = _validate(child)

    assert any(
        item.code == "ROS1305"
        and item.severity == Severity.ERROR
        and item.subject is None
        and "nesting is too deep" in item.summary
        for item in report.diagnostics
    )


def test_contract_provider_failure_is_fail_closed_and_does_not_escape(monkeypatch) -> None:
    def fail_contract_load():
        raise RuntimeError("invalid vendored contract")

    monkeypatch.setattr(association_property_rule, "load_association_property_specs", fail_contract_load)

    report = _validate(
        {
            "Type": "String",
            "AssociationProperty": "AutoCompleteInput",
            "AssociationPropertyMetadata": {"CharacterClasses": [{"Class": "digit", "Min": 1}]},
        }
    )

    diagnostic = next(item for item in report.diagnostics if item.code == "ROS9999")
    assert diagnostic.subject == "builtin.association-property-specs"
    assert report.analysis_incomplete
