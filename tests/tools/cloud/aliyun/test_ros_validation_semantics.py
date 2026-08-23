from __future__ import annotations

import hashlib
import json

from iac_code.tools.cloud.aliyun.ros_validation.model import (
    Category,
    MaterializedTemplateSource,
    RequestValidationContext,
    ScalarKind,
    Severity,
    TemplateSemanticMode,
    ValidationReport,
    display_path,
    make_diagnostic,
    mapping_segment,
    path_identity,
    path_segments,
)
from iac_code.tools.cloud.aliyun.ros_validation.parser import parse_template_source
from iac_code.tools.cloud.aliyun.ros_validation.validator import validate_ros_template


def validate(body: str):
    return validate_ros_template(
        MaterializedTemplateSource(body),
        RequestValidationContext(action="PreviewStack"),
    )


def test_reports_select_and_replace_independent_errors_with_positions() -> None:
    report = validate(
        """ROSTemplateFormatVersion: 2015-09-01
Resources:
  Bucket:
    Type: ALIYUN::OSS::Bucket
    Properties:
      BucketName:
        Fn::Select:
          - "29:32"
          - Fn::Replace:
              - {"-": ""}
              - "${ALIYUN::StackId}"
"""
    )
    assert [item.code for item in report.diagnostics] == ["ROS5002", "ROS5001"]
    assert [item.source_span.line for item in report.diagnostics] == [9, 11]
    assert all(item.source_span.column > 0 for item in report.diagnostics)
    assert display_path(report.diagnostics[0].path).endswith(".Fn::Select[1]")
    assert report.error_count == 2


def test_getazs_is_a_list_and_plain_string_is_not_a_collection() -> None:
    report = validate(
        """ROSTemplateFormatVersion: 2015-09-01
Resources:
  Vpc:
    Type: ALIYUN::ECS::VPC
  A:
    Type: ALIYUN::ROS::Sleep
    Properties:
      Triggers:
        Good: {Fn::Select: [0, {Fn::GetAZs: ""}]}
        Bad: {Fn::Select: [0, "Vpc"]}
"""
    )
    select_errors = [item for item in report.diagnostics if item.code == "ROS5002"]
    assert len(select_errors) == 1
    assert "String" in select_errors[0].summary


def test_select_slice_of_dynamic_list_remains_a_list_for_join() -> None:
    report = validate(
        """ROSTemplateFormatVersion: 2015-09-01
Parameters:
  DomainName: {Type: String}
Resources:
  Wait:
    Type: ALIYUN::ROS::Sleep
    Properties:
      Triggers:
        DomainName:
          Fn::Join:
            - .
            - Fn::Select: ["1:", {Fn::Split: [., {Ref: DomainName}]}]
"""
    )
    assert not any(item.code == "ROS3002" and "Fn::Join" in item.summary for item in report.diagnostics)


def test_extension_datasource_ref_list_is_valid_select_collection() -> None:
    report = validate(
        """ROSTemplateFormatVersion: 2015-09-01
Resources:
  Instances:
    Type: DATASOURCE::DTS::MigrationJobs
  Wait:
    Type: ALIYUN::ROS::Sleep
    Properties:
      Triggers:
        First: {Fn::Select: [0, {Ref: Instances}]}
"""
    )
    assert not any(item.code == "ROS5002" for item in report.diagnostics)


def test_getatt_attribute_existence_is_deferred_to_validate_template() -> None:
    report = validate(
        """ROSTemplateFormatVersion: 2015-09-01
Resources:
  Gateway:
    Type: ALIYUN::APIG::Gateway
  Wait:
    Type: ALIYUN::ROS::Sleep
    Properties:
      Triggers:
        Good: {Fn::GetAtt: [Gateway, GatewayId]}
        Bad: {Fn::GetAtt: [Gateway, DefinitelyMissingAttribute]}
"""
    )
    assert not any(item.code == "ROS4005" for item in report.diagnostics)


def test_missing_template_version_does_not_also_report_wrong_version() -> None:
    missing = validate("Resources: {}\n")
    assert sum(item.code == "ROS1004" for item in missing.diagnostics) == 1
    assert not any(item.code == "ROS1120" for item in missing.diagnostics)

    wrong = validate("ROSTemplateFormatVersion: 2010-09-09\nResources: {}\n")
    assert not any(item.code == "ROS1004" for item in wrong.diagnostics)
    assert sum(item.code == "ROS1120" for item in wrong.diagnostics) == 1


def test_core_resource_dynamic_getatt_is_not_rejected_by_static_catalog() -> None:
    report = validate(
        """ROSTemplateFormatVersion: 2015-09-01
Resources:
  Cleaner:
    Type: ALIYUN::ROS::ResourceCleaner
  Wait:
    Type: ALIYUN::ROS::Sleep
    Properties:
      Triggers:
        Dynamic: {Fn::GetAtt: [Cleaner, "Detail:ECS:Instance:cn-hangzhou:i-123"]}
"""
    )
    assert not any(item.code == "ROS4005" for item in report.diagnostics)


def test_nested_stack_getatt_requires_outputs_prefix_and_nonempty_output_name() -> None:
    report = validate(
        """ROSTemplateFormatVersion: 2015-09-01
Resources:
  Nested:
    Type: ALIYUN::ROS::Stack
    Properties:
      TemplateBody:
        ROSTemplateFormatVersion: 2015-09-01
        Outputs:
          Child: {Value: ok}
Outputs:
  Valid: {Value: {Fn::GetAtt: [Nested, Outputs.Child]}}
  MissingPrefix: {Value: {Fn::GetAtt: [Nested, Child]}}
  EmptyOutputName: {Value: {Fn::GetAtt: [Nested, Outputs.]}}
  WrongAttribute: {Value: {Fn::GetAtt: [Nested, StackId]}}
"""
    )

    errors = [item for item in report.diagnostics if item.code == "ROS4005"]
    assert len(errors) == 3
    assert {display_path(item.path) for item in errors} == {
        "$.Outputs.MissingPrefix.Value.Fn::GetAtt[1]",
        "$.Outputs.EmptyOutputName.Value.Fn::GetAtt[1]",
        "$.Outputs.WrongAttribute.Value.Fn::GetAtt[1]",
    }
    assert all("Outputs.<nested_stack_output_name>" in item.summary for item in errors)
    assert all(item.expected == "Outputs.<nested_stack_output_name>" for item in errors)
    assert {item.actual for item in errors} == {"Child", "Outputs.", "StackId"}
    assert all("Fn::GetAtt: [Nested, Outputs.MyOutput]" in (item.suggestion or "") for item in errors)


def test_count_ref_is_lifted_once_and_invalid_count_is_independent() -> None:
    report = validate(
        """ROSTemplateFormatVersion: 2015-09-01
Resources:
  Servers:
    Type: ALIYUN::ECS::Instance
    Count: 2
  Broken:
    Type: ALIYUN::ECS::Instance
    Count: -1
  Wait:
    Type: ALIYUN::ROS::Sleep
    Properties:
      Triggers:
        First: {Fn::Select: [0, {Ref: Servers}]}
"""
    )
    assert not any(item.code == "ROS5002" for item in report.diagnostics)
    assert sum(item.code == "ROS4301" for item in report.diagnostics) == 1


def test_strict_warns_when_string_count_relies_on_runtime_coercion() -> None:
    report = validate(
        """ROSTemplateFormatVersion: 2015-09-01
Resources:
  Repeated:
    Type: ALIYUN::ROS::Sleep
    Count: "2"
"""
    )
    warnings = [item for item in report.diagnostics if item.code == "ROS5213"]
    assert len(warnings) == 1
    assert warnings[0].severity == Severity.WARNING
    assert warnings[0].source_span is not None and warnings[0].source_span.line == 5
    assert report.error_count == 0


def test_parameter_literals_follow_runtime_conversion_without_quality_warning() -> None:
    report = validate(
        """ROSTemplateFormatVersion: 2015-09-01
Parameters:
  Storage: {Type: Number, Default: '20'}
  Enabled: {Type: Boolean, Default: 'False'}
Resources: {}
"""
    )
    assert not any(item.code == "ROS5212" for item in report.diagnostics)
    assert report.error_count == 0


def test_replace_does_not_reject_dynamic_any_value_as_definite_non_scalar() -> None:
    report = validate(
        """ROSTemplateFormatVersion: 2015-09-01
Resources:
  Wait:
    Type: ALIYUN::ROS::Sleep
    Properties:
      Triggers:
        Value:
          Fn::Replace:
            - x: {Fn::GetStackOutput: [stack, output]}
            - x
"""
    )
    assert not any(item.severity == Severity.ERROR for item in report.diagnostics)


def test_replace_rejects_function_as_constructor_mapping() -> None:
    report = validate(
        """ROSTemplateFormatVersion: 2015-09-01
Parameters:
  Replacements: {Type: Json, Default: {x: y}}
Resources:
  Wait:
    Type: ALIYUN::ROS::Sleep
    Properties:
      Triggers:
        Value: {Fn::Replace: [{Ref: Replacements}, x]}
"""
    )
    unreachable = validate(
        """ROSTemplateFormatVersion: 2015-09-01
Parameters:
  Replacements: {Type: Json, Default: {x: y}}
Conditions:
  Never: {Fn::Equals: [x, y]}
Resources:
  Wait:
    Type: ALIYUN::ROS::Sleep
    Properties:
      Triggers:
        Value: {Fn::If: [Never, {Fn::Replace: [{Ref: Replacements}, x]}, ok]}
"""
    )
    local_mapping = validate(
        """ROSTemplateFormatVersion: 2015-09-01
Locals:
  Replacements: {Type: Macro, Value: {x: y}}
Resources:
  Wait:
    Type: ALIYUN::ROS::Sleep
    Properties:
      Triggers:
        Value: {Fn::Replace: [{Ref: Replacements}, x]}
"""
    )

    assert [item.code for item in report.diagnostics] == ["ROS3002"]
    assert "Fn::Replace" in report.diagnostics[0].summary
    assert any(item.code == "ROS3001" and "Fn::Replace" in item.summary for item in unreachable.diagnostics)
    assert not any("Fn::Replace" in item.summary for item in local_mapping.diagnostics)


def test_raw_constructor_arguments_use_macro_and_eval_local_expansion() -> None:
    report = validate(
        """ROSTemplateFormatVersion: 2015-09-01
Locals:
  Delimiter: {Type: Macro, Value: ","}
  Key: {Type: Eval, Value: x}
  KeyName: {Type: Macro, Value: Name}
  ValueName: {Type: Eval, Value: Value}
  Method: {Type: Eval, Value: First}
  Expression: {Type: Macro, Value: "{0}+1"}
Resources:
  Wait:
    Type: ALIYUN::ROS::Sleep
    Properties:
      Triggers:
        Split: {Fn::Split: [{Ref: Delimiter}, "a,b"]}
        Json: {Fn::GetJsonValue: [{Ref: Key}, {x: ok}]}
        Members: {Fn::MemberListToMap: [{Ref: KeyName}, {Ref: ValueName}, [".member.0.Name=x", ".member.0.Value=y"]]}
        Jq: {Fn::Jq: [{Ref: Method}, ".x", {x: ok}]}
        Calculate: {Fn::Calculate: [{Ref: Expression}, 0, [1]]}
"""
    )

    assert not any(item.severity == Severity.ERROR for item in report.diagnostics)


def test_select_error_message_and_marketplace_image_use_expanded_locals() -> None:
    report = validate(
        """ROSTemplateFormatVersion: 2015-09-01
Locals:
  MacroMessage: {Type: Macro, Value: "missing {key}"}
  EvalMessage: {Type: Eval, Value: "missing {key}"}
  MacroImage: {Type: Macro, Value: cmjj026649}
  EvalImage: {Type: Eval, Value: cmjj026649}
Conditions:
  Always: true
Resources:
  Wait:
    Type: ALIYUN::ROS::Sleep
    Properties:
      Triggers:
        SelectMacro: {Fn::Select: [0, [ok], fallback, {Ref: MacroMessage}]}
        SelectEval: {Fn::Select: [0, [ok], fallback, {Ref: EvalMessage}]}
        SelectUnreachable: {Fn::If: [Always, ok, {Fn::Select: [0, [ok], fallback, {Ref: MacroMessage}]}]}
        ImageMacro: {Fn::MarketplaceImage: {Ref: MacroImage}}
        ImageEval: {Fn::MarketplaceImage: {Ref: EvalImage}}
"""
    )

    assert not any(item.severity == Severity.ERROR for item in report.diagnostics)


def test_select_error_message_and_marketplace_image_reject_expanded_non_strings() -> None:
    report = validate(
        """ROSTemplateFormatVersion: 2015-09-01
Locals:
  BadMessage: {Type: Macro, Value: [not-a-string]}
  BadImage: {Type: Eval, Value: [not-a-string]}
Resources:
  Wait:
    Type: ALIYUN::ROS::Sleep
    Properties:
      Triggers:
        Select: {Fn::Select: [0, [ok], fallback, {Ref: BadMessage}]}
        Image: {Fn::MarketplaceImage: {Ref: BadImage}}
"""
    )

    assert sum(item.code == "ROS3002" and "Fn::Select" in item.summary for item in report.diagnostics) == 1
    assert sum(item.code == "ROS3001" and "Fn::MarketplaceImage" in item.summary for item in report.diagnostics) == 1


def test_replace_checks_values_in_expanded_macro_and_eval_local_maps() -> None:
    report = validate(
        """ROSTemplateFormatVersion: 2015-09-01
Locals:
  MacroReplacements: {Type: Macro, Value: {x: []}}
  EvalReplacements: {Type: Eval, Value: {x: []}}
Resources:
  Wait:
    Type: ALIYUN::ROS::Sleep
    Properties:
      Triggers:
        Macro: {Fn::Replace: [{Ref: MacroReplacements}, x]}
        Eval: {Fn::Replace: [{Ref: EvalReplacements}, x]}
"""
    )

    errors = [item for item in report.diagnostics if item.code == "ROS3002" and "Fn::Replace" in item.summary]
    assert len(errors) == 2


def test_unreachable_replace_rejects_eval_local_with_literal_non_map() -> None:
    report = validate(
        """ROSTemplateFormatVersion: 2015-09-01
Locals:
  Replacements: {Type: Eval, Value: not-a-map}
Conditions:
  Never: {Fn::Equals: [x, y]}
Resources:
  Wait:
    Type: ALIYUN::ROS::Sleep
    Properties:
      Triggers:
        Value: {Fn::If: [Never, {Fn::Replace: [{Ref: Replacements}, x]}, ok]}
"""
    )

    assert any(item.code == "ROS3001" and "Fn::Replace" in item.summary for item in report.diagnostics)


def test_join_and_avg_reject_dynamic_members_with_definitely_incompatible_type() -> None:
    report = validate(
        """ROSTemplateFormatVersion: 2015-09-01
Parameters:
  P: {Type: String}
Conditions:
  C: {Fn::Equals: [{Ref: P}, x]}
Resources:
  Wait:
    Type: ALIYUN::ROS::Sleep
    Properties:
      Triggers:
        Join: {Fn::Join: [",", [{Fn::If: [C, {a: b}, {c: d}]}]]}
        Avg: {Fn::Avg: [0, [{Fn::If: [C, {a: b}, {c: d}]}]]}
"""
    )
    errors = [item for item in report.diagnostics if item.severity == Severity.ERROR]
    assert sum("Fn::Join" in item.summary for item in errors) == 1
    assert sum("Fn::Avg" in item.summary for item in errors) == 1


def test_calculate_rejects_dynamic_non_empty_numbers_with_incompatible_item_type() -> None:
    report = validate(
        """ROSTemplateFormatVersion: 2015-09-01
Parameters:
  P: {Type: String}
Conditions:
  C: {Fn::Equals: [{Ref: P}, x]}
Resources:
  Wait:
    Type: ALIYUN::ROS::Sleep
    Properties:
      Triggers:
        Value: {Fn::Calculate: ["{0}+1", 0, [{Fn::If: [C, {a: b}, {c: d}]}]]}
"""
    )
    errors = [item for item in report.diagnostics if item.severity == Severity.ERROR]
    assert len(errors) == 1
    assert "Fn::Calculate" in errors[0].summary


def test_contains_and_each_member_in_reject_dynamic_unhashable_members() -> None:
    report = validate(
        """ROSTemplateFormatVersion: 2015-09-01
Parameters:
  P: {Type: String}
Conditions:
  C: {Fn::Equals: [{Ref: P}, x]}
Resources:
  Wait:
    Type: ALIYUN::ROS::Sleep
    Properties:
      Triggers:
        Contains: {Fn::Contains: [[{Fn::If: [C, {a: b}, {c: d}]}], x]}
        Each: {Fn::EachMemberIn: [[{Fn::If: [C, {a: b}, {c: d}]}], [x]]}
"""
    )
    errors = [item for item in report.diagnostics if item.severity == Severity.ERROR]
    assert sum("Fn::Contains" in item.summary for item in errors) == 1
    assert sum("Fn::EachMemberIn" in item.summary for item in errors) == 1


def test_dynamic_count_is_not_rejected_and_reports_only_real_uncertainty() -> None:
    report = validate(
        """ROSTemplateFormatVersion: 2015-09-01
Parameters:
  CountValue: {Type: Number}
Resources:
  Dynamic:
    Type: ALIYUN::ROS::Sleep
    Count: {Ref: CountValue}
  MaybeInstance:
    Type: ALIYUN::ROS::Sleep
    Properties:
      Triggers: {Value: {Ref: "Dynamic[0]"}}
  Collision:
    Type: ALIYUN::ROS::Sleep
    Count: {Ref: CountValue}
  Collision[0]:
    Type: ALIYUN::ROS::Sleep
"""
    )
    assert not any(item.code == "ROS4301" for item in report.diagnostics)
    limitations = [item for item in report.diagnostics if item.code == "ROS9103"]
    assert len(limitations) == 2


def test_count_select_fold_only_analyzes_transformed_reachable_node() -> None:
    report = validate(
        """ROSTemplateFormatVersion: 2015-09-01
Parameters:
  Index: {Type: Number, Default: 0}
Resources:
  Repeated: {Type: ALIYUN::ROS::Sleep, Count: 2}
  Wait:
    Type: ALIYUN::ROS::Sleep
    Properties:
      Triggers:
        Folded:
          Fn::Select:
            - {Ref: Index}
            - {Ref: Repeated}
            - {Ref: DeletedDefaultMustNotBeAnalyzed}
"""
    )
    assert not any(item.code == "ROS4001" for item in report.diagnostics)
    assert report.error_count == 0


def test_count_select_positive_oob_keeps_raw_default_outside_count_rewrite() -> None:
    report = validate(
        """ROSTemplateFormatVersion: 2015-09-01
Parameters:
  Index: {Type: Number, Default: 5}
Resources:
  Selected: {Type: ALIYUN::ROS::Sleep, Count: 1}
  Defaulted: {Type: ALIYUN::ROS::Sleep, Count: 1}
  Wait:
    Type: ALIYUN::ROS::Sleep
    Properties:
      Triggers:
        Value: {Fn::Select: [{Ref: Index}, {Ref: Selected}, {Ref: Defaulted}]}
"""
    )
    errors = [item for item in report.diagnostics if item.code == "ROS4303"]
    assert len(errors) == 1
    assert "Defaulted" in errors[0].summary


def test_dynamic_count_depends_on_reports_instance_existence_limitation() -> None:
    report = validate(
        """ROSTemplateFormatVersion: 2015-09-01
Parameters:
  CountValue: {Type: Number}
Resources:
  Dynamic: {Type: ALIYUN::ROS::Sleep, Count: {Ref: CountValue}}
  Wait:
    Type: ALIYUN::ROS::Sleep
    DependsOn: Dynamic[0]
"""
    )
    limitations = [item for item in report.diagnostics if item.code == "ROS9103"]
    assert len(limitations) == 1
    assert "Dynamic[0]" in limitations[0].summary


def test_count_select_does_not_precompile_find_in_map_rhs() -> None:
    report = validate(
        """ROSTemplateFormatVersion: 2015-09-01
Mappings:
  Values: {Default: {Items: [only]}}
Resources:
  Repeated:
    Type: ALIYUN::ROS::Sleep
    Count: 2
    Properties:
      Triggers:
        Value: {Fn::Select: [{Ref: ALIYUN::Index}, {Fn::FindInMap: [Values, Default, Items]}]}
"""
    )
    assert not any(item.code == "ROS4304" for item in report.diagnostics)


def test_count_select_step_zero_reports_precompile_failure() -> None:
    report = validate(
        """ROSTemplateFormatVersion: 2015-09-01
Parameters:
  Index: {Type: String, Default: "::0"}
Resources:
  Repeated: {Type: ALIYUN::ROS::Sleep, Count: 2}
  Wait:
    Type: ALIYUN::ROS::Sleep
    Properties:
      Triggers:
        Value: {Fn::Select: [{Ref: Index}, {Ref: Repeated}]}
"""
    )
    assert sum(item.code == "ROS4304" for item in report.diagnostics) == 1


def test_count_getatt_allows_function_attribute_and_depends_on_binds_instances() -> None:
    report = validate(
        """ROSTemplateFormatVersion: 2015-09-01
Parameters:
  Attribute: {Type: String, Default: Data}
Resources:
  Repeated: {Type: ALIYUN::ROS::Sleep, Count: 2}
  Good:
    Type: ALIYUN::ROS::Sleep
    DependsOn: [Repeated, "Repeated[1]", null, ""]
    Properties:
      Triggers:
        Values: {Fn::GetAtt: [Repeated, {Ref: Attribute}]}
  Bad:
    Type: ALIYUN::ROS::Sleep
    DependsOn: "Repeated[2]"
"""
    )
    assert not any(item.code == "ROS3002" and "Fn::GetAtt" in item.summary for item in report.diagnostics)
    errors = [item for item in report.diagnostics if item.code == "ROS4002" and "DependsOn" in item.summary]
    assert len(errors) == 1
    assert "Repeated[2]" in errors[0].summary


def test_datasource_local_properties_are_analyzed_in_temporary_scope() -> None:
    report = validate(
        """ROSTemplateFormatVersion: 2015-09-01
Locals:
  Zones:
    Type: DATASOURCE::ECS::Zones
    Properties:
      RegionId: {Ref: MissingParameter}
Resources: {}
"""
    )
    error = next(item for item in report.diagnostics if item.code == "ROS4001")
    assert display_path(error.path).endswith("Locals.Zones.Properties.RegionId.Ref")


def test_context_visibility_rejects_getatt_in_condition() -> None:
    report = validate(
        """ROSTemplateFormatVersion: 2015-09-01
Resources:
  Vpc: {Type: ALIYUN::ECS::VPC}
Conditions:
  Bad: {Fn::GetAtt: [Vpc, VpcId]}
"""
    )
    assert any(item.code == "ROS2002" and "CONDITION" in item.summary for item in report.diagnostics)


def test_poisoned_child_suppresses_parent_type_cascade_but_keeps_sibling() -> None:
    report = validate(
        """ROSTemplateFormatVersion: 2015-09-01
Resources:
  Wait:
    Type: ALIYUN::ROS::Sleep
    Properties:
      Triggers:
        Parent: {Fn::Join: [",", [{Ref: Missing}]]}
        Sibling: {Fn::Select: [0, "not-a-list"]}
"""
    )
    assert sum(item.code == "ROS4001" for item in report.diagnostics) == 1
    assert sum(item.code == "ROS5002" for item in report.diagnostics) == 1
    assert not any(item.code == "ROS3002" and "Fn::Join" in item.summary for item in report.diagnostics)


def test_parser_preserves_duplicate_occurrence_and_short_tag_locations() -> None:
    result = parse_template_source(
        """ROSTemplateFormatVersion: 2015-09-01
Parameters:
  Name: {Type: String}
  Name: {Type: Number}
Outputs:
  V: {Value: !GetAtt Nested.Stack.Outputs.Value}
"""
    )
    assert result.template is not None
    duplicate = next(item for item in result.diagnostics if item.code == "ROS1003")
    assert duplicate.source_span.line == 4
    value = result.template.data["Outputs"]["V"]["Value"]
    assert value == {"Fn::GetAtt": ["Nested.Stack", "Outputs.Value"]}


def test_yaml_and_json_duplicate_diagnostics_point_to_duplicate_key_occurrences() -> None:
    yaml_result = parse_template_source(
        """ROSTemplateFormatVersion: 2015-09-01
Parameters:
  Name:
    Type: String
  Name:
    Type: Number
Resources: {}
"""
    )
    json_result = parse_template_source(
        """{
  "ROSTemplateFormatVersion": "2015-09-01",
  "Resources": {},
  "Name":
    {"Type": "String"},
  "Name":
    {"Type": "Number"}
}
"""
    )

    yaml_duplicate = next(item for item in yaml_result.diagnostics if item.code == "ROS1003")
    json_duplicate = next(item for item in json_result.diagnostics if item.code == "ROS1003")
    assert (yaml_duplicate.source_span.line, yaml_duplicate.source_span.column) == (5, 3)
    assert (json_duplicate.source_span.line, json_duplicate.source_span.column) == (6, 3)


def test_long_string_mapping_keys_keep_distinct_source_map_identities() -> None:
    first_key = "a" * 96 + "x"
    second_key = "a" * 96 + "y"
    result = parse_template_source("{}: first\n{}: second\n".format(first_key, second_key))

    assert result.template is not None
    first_path = (mapping_segment(first_key),)
    second_path = (mapping_segment(second_key),)
    assert path_identity(first_path) != path_identity(second_path)
    assert hashlib.sha256(first_key.encode()).hexdigest() in path_identity(first_path)[0]
    assert display_path(first_path) != display_path(second_path)
    assert path_segments(first_path) != path_segments(second_path)
    assert first_key not in display_path(first_path)
    assert second_key not in display_path(second_path)
    sensitive_key = "password-value"
    assert hashlib.sha256(sensitive_key.encode()).hexdigest() in path_identity((mapping_segment(sensitive_key),))[0]
    first = result.template.source_map.node_for(first_path)
    second = result.template.source_map.node_for(second_path)
    assert first is not None and (first.value, first.span.line) == ("first", 1)
    assert second is not None and (second.value, second.span.line) == ("second", 2)


def test_yaml_typed_equal_keys_do_not_report_duplicates_and_keep_merge_occurrences() -> None:
    direct = parse_template_source("true: from-boolean\n1: from-integer\n")
    assert direct.template is not None
    assert direct.template.data == {True: "from-integer"}
    assert not any(item.code == "ROS1003" for item in direct.diagnostics)

    result = parse_template_source(
        """Defaults: &defaults
  true: from-boolean
Result:
  <<: *defaults
  1: from-integer
"""
    )

    assert result.template is not None
    assert not any(item.code == "ROS1003" for item in result.diagnostics)
    assert result.template.data["Result"] == {True: "from-integer"}

    result_path = (mapping_segment("Result"),)
    typed_occurrences = {
        node.path[-1].key_kind
        for node in result.template.source_map.occurrences
        if node.path[:-1] == result_path and hasattr(node.path[-1], "key_kind")
    }
    assert {ScalarKind.BOOLEAN, ScalarKind.INTEGER} <= typed_occurrences

    semantic = result.template.source_map.node_for(result_path + (mapping_segment(True),))
    explicit = result.template.source_map.node_for(result_path + (mapping_segment(1),))
    assert semantic is not None and (semantic.value, semantic.span.line) == ("from-integer", 5)
    assert explicit is not None and (explicit.value, explicit.span.line) == ("from-integer", 5)


def test_yaml_short_tag_synthetic_node_keeps_source_text_coordinate_kind() -> None:
    from iac_code.tools.cloud.aliyun.ros_validation.renderer import render_validation_report

    report = validate(
        """ROSTemplateFormatVersion: 2015-09-01
Resources:
  Wait:
    Type: ALIYUN::ROS::Sleep
    Properties:
      Triggers:
        Value: !Select [0]
"""
    )

    shape_error = next(item for item in report.diagnostics if item.code == "ROS3001")
    assert shape_error.source_span is not None
    assert not shape_error.source_span.synthetic
    assert "generated JSON" not in render_validation_report(report, blocking=True)


def test_renderer_condenses_high_volume_association_property_limitations() -> None:
    from iac_code.tools.cloud.aliyun.ros_validation.renderer import render_validation_report

    report = ValidationReport.build(
        [
            make_diagnostic(
                code="ROS5305",
                severity=Severity.LIMITATION,
                category=Category.LIMITATION,
                summary="Runtime-dependent metadata cannot be checked locally.",
                detail="The value depends on runtime context.",
                path=(mapping_segment("Parameters"), mapping_segment("P{}".format(index))),
                subject="P{}".format(index),
            )
            for index in range(11)
        ]
    )

    rendered = render_validation_report(report, blocking=False)

    assert report.limitation_count == 11
    assert report.to_dict()["limitation_count"] == 11
    assert "11 local-analysis limitations; details condensed." in rendered
    assert "11 occurrences; showing 3 examples." in rendered
    assert "8 additional occurrences are available in structured diagnostics." in rendered
    assert rendered.count("example path:") == 3


def test_json_parser_accepts_tab_whitespace_and_retains_positions() -> None:
    result = parse_template_source('{\n\t"ROSTemplateFormatVersion": "2015-09-01",\n\t"Resources": {}\n}')

    assert result.template is not None
    assert result.template.source_kind == "JSON"
    resources = result.template.source_map.node_for((mapping_segment("Resources"),))
    assert resources is not None
    assert resources.span.line == 3
    assert resources.span.column == 15


def test_last_wins_semantic_diagnostic_points_to_last_duplicate_value() -> None:
    report = validate(
        """ROSTemplateFormatVersion: 2015-09-01
Resources:
  Repeated: {Type: ALIYUN::ROS::Sleep}
  Repeated: invalid-last-value
"""
    )
    structure = next(item for item in report.diagnostics if item.code == "ROS1102")
    duplicate = next(item for item in report.diagnostics if item.code == "ROS1003")
    assert structure.source_span.line == 4
    assert duplicate.source_span.line == 4


def test_last_wins_container_source_map_uses_container_root_not_last_descendant() -> None:
    report = validate(
        """ROSTemplateFormatVersion: 2015-09-01
Resources:
  Wait:
    Type: ALIYUN::ROS::Sleep
    Properties:
      Triggers:
        Value: {Fn::Select: [0, [first]]}
        Value:
          Fn::Select:
            - 0
            - invalid
"""
    )
    error = next(item for item in report.diagnostics if item.code == "ROS5002")
    assert error.source_span is not None and error.source_span.line == 11


def test_parser_applies_nested_yaml_merge_while_retaining_source_map() -> None:
    result = parse_template_source(
        """ROSTemplateFormatVersion: 2015-09-01
Defaults: &defaults
  Type: ALIYUN::ROS::Sleep
  Properties: {CreateDuration: 1}
Resources:
  Wait:
    <<: *defaults
"""
    )
    assert result.template is not None
    assert result.template.data["Resources"]["Wait"]["Type"] == "ALIYUN::ROS::Sleep"
    assert any(node.origin_node_ids for node in result.template.source_map.occurrences)


def test_merged_field_diagnostic_points_to_merge_use_and_relates_anchor_field() -> None:
    report = validate(
        """ROSTemplateFormatVersion: 2015-09-01
Defaults: &defaults
  Type: ALIYUN::ROS::Sleep
  Properties:
    Triggers: {Value: {Fn::Select: [0, text]}}
Resources:
  Wait:
    <<: *defaults
"""
    )
    error = next(item for item in report.diagnostics if item.code == "ROS5002")
    assert error.source_span is not None and error.source_span.line == 8
    assert len(error.related_locations) == 1
    assert error.related_locations[0].source_span is not None
    assert error.related_locations[0].source_span.line == 5


def test_parser_stage_zero_safely_rejects_empty_nul_and_non_string() -> None:
    assert parse_template_source("").analysis_incomplete
    assert parse_template_source("a:\x00b").analysis_incomplete
    non_string = parse_template_source(123)  # type: ignore[arg-type]
    assert non_string.analysis_incomplete
    assert non_string.diagnostics[0].code == "ROS1000"


def test_bad_runtime_values_never_become_internal_errors_or_hide_siblings() -> None:
    report = validate(
        """ROSTemplateFormatVersion: 2015-09-01
Resources:
  Wait:
    Type: ALIYUN::ROS::Sleep
    Properties:
      Triggers:
        BadMerge: {Fn::MergeMap: [1, 2]}
        BadIndex: {Fn::Index: [x, {a: x}]}
        BadSelect: {Fn::Select: [0, text]}
"""
    )
    assert not any(item.code == "ROS9999" for item in report.diagnostics)
    assert any("Fn::MergeMap" in item.summary for item in report.diagnostics)
    assert any("Fn::Index" in item.summary for item in report.diagnostics)
    assert any(item.code == "ROS5002" for item in report.diagnostics)


def test_known_if_unreachable_branch_suppresses_runtime_compatibility_errors() -> None:
    report = validate(
        """ROSTemplateFormatVersion: 2015-09-01
Conditions:
  Always: {Fn::Equals: [same, same]}
Resources:
  Wait:
    Type: ALIYUN::ROS::Sleep
    Properties:
      Triggers:
        Value: {Fn::If: [Always, selected, {Fn::Select: [0, text]}]}
"""
    )
    assert not any(item.code == "ROS5002" for item in report.diagnostics)


def test_unreachable_branch_keeps_constructor_shape_but_suppresses_lookup_errors() -> None:
    report = validate(
        """ROSTemplateFormatVersion: 2015-09-01
Conditions: {Always: true}
Resources:
  Wait:
    Type: ALIYUN::ROS::Sleep
    Properties:
      Triggers:
        BadShape: {Fn::If: [Always, selected, {Fn::Join: bad}]}
        MissingLookup: {Fn::If: [Always, selected, {Ref: Missing}]}
"""
    )
    assert sum(item.code == "ROS3001" for item in report.diagnostics) == 1
    assert not any(item.code == "ROS4001" for item in report.diagnostics)


def test_select_does_not_evaluate_an_unselected_default() -> None:
    report = validate(
        """ROSTemplateFormatVersion: 2015-09-01
Resources:
  Wait:
    Type: ALIYUN::ROS::Sleep
    Properties:
      Triggers:
        Value: {Fn::Select: [0, [selected], {Ref: MissingUnselectedDefault}]}
"""
    )
    assert not any(item.code == "ROS4001" for item in report.diagnostics)


def test_nested_sub_owns_its_placeholders() -> None:
    report = validate(
        """ROSTemplateFormatVersion: 2015-09-01
Parameters:
  P: {Type: String}
Resources:
  Wait:
    Type: ALIYUN::ROS::Sleep
    Properties:
      Triggers:
        Value: {Fn::Join: [",", [{Fn::Sub: "${P}"}]]}
"""
    )
    assert not any(item.code == "ROS5001" for item in report.diagnostics)


def test_script_properties_and_function_local_refs_do_not_report_ros_placeholders() -> None:
    report = validate(
        """ROSTemplateFormatVersion: 2015-09-01
Parameters:
  Limit: {Type: String}
Resources:
  Instance:
    Type: ALIYUN::ECS::InstanceGroup
    Properties:
      UserData:
        Fn::Join: ["", ["echo ${ALIYUN::StackId}"]]
  Command:
    Type: ALIYUN::ECS::RunCommand
    Properties:
      CommandContent:
        Fn::Join: ["", ["echo ${Limit}"]]
  Wait:
    Type: ALIYUN::ROS::Sleep
    Properties:
      Triggers:
        JoinVariable:
          Fn::Join: ["", [{Ref: Limit}, "echo ${Limit}"]]
        ReplaceVariable:
          Fn::Replace:
            - __LIMIT__: {Ref: Limit}
            - "export LIMIT=__LIMIT__; echo ${Limit}"
        Wrong:
          Fn::Replace: [{x: y}, "echo ${Limit}"]
"""
    )
    errors = [item for item in report.diagnostics if item.code == "ROS5001"]
    assert len(errors) == 1
    assert ".Wrong." in display_path(errors[0].path)


def test_count_index_is_visible_only_inside_a_count_resource() -> None:
    report = validate(
        """ROSTemplateFormatVersion: 2015-09-01
Resources:
  Repeated:
    Type: ALIYUN::ROS::Sleep
    Count: 2
    Properties:
      Triggers: {Index: {Ref: ALIYUN::Index}}
  Single:
    Type: ALIYUN::ROS::Sleep
    Properties:
      Triggers: {Index: {Ref: ALIYUN::Index}}
"""
    )
    missing = [item for item in report.diagnostics if item.code == "ROS4001"]
    assert len(missing) == 1
    assert display_path(missing[0].path).startswith("$.Resources.Single")


def test_count_index_is_visible_to_sub_inside_a_count_resource() -> None:
    report = validate(
        """ROSTemplateFormatVersion: 2015-09-01
Resources:
  Repeated:
    Type: ALIYUN::ROS::Sleep
    Count: 2
    Properties:
      Triggers:
        Name: {Fn::Sub: "node-${ALIYUN::Index}"}
  Single:
    Type: ALIYUN::ROS::Sleep
    Properties:
      Triggers:
        Name: {Fn::Sub: "node-${ALIYUN::Index}"}
"""
    )
    missing = [item for item in report.diagnostics if item.code == "ROS4001"]
    assert len(missing) == 1
    assert display_path(missing[0].path).startswith("$.Resources.Single")


def test_sub_variable_map_preserves_count_getatt_rewrite_eligibility() -> None:
    report = validate(
        """ROSTemplateFormatVersion: 2015-09-01
Resources:
  Repeated:
    Type: ALIYUN::ROS::Sleep
    Count: 2
  Wait:
    Type: ALIYUN::ROS::Sleep
    Properties:
      Triggers:
        Values:
          Fn::Sub:
            - "${Values}"
            - Values: {Fn::GetAtt: [Repeated, Data]}
"""
    )
    assert not any(item.code == "ROS4303" for item in report.diagnostics)


def test_raw_content_property_is_not_parsed_as_parent_stack_functions() -> None:
    report = validate(
        """ROSTemplateFormatVersion: 2015-09-01
Resources:
  Nested:
    Type: ALIYUN::ROS::Stack
    Properties:
      TemplateBody:
        ROSTemplateFormatVersion: 2015-09-01
        Outputs:
          Child: {Value: "${ChildOnlySymbol}"}
      Parameters:
        Parent: {Ref: MissingParentSymbol}
"""
    )
    missing = [item for item in report.diagnostics if item.code == "ROS4001"]
    assert len(missing) == 1
    assert "MissingParentSymbol" in missing[0].summary
    assert not any(item.code == "ROS5001" for item in report.diagnostics)


def test_condition_root_uses_base_function_table() -> None:
    report = validate(
        """ROSTemplateFormatVersion: 2015-09-01
Conditions:
  BadRoot: {Fn::Contains: [[a], a]}
  GoodInner: {Fn::Equals: [{Fn::Contains: [[a], a]}, true]}
Resources: {}
"""
    )
    errors = [item for item in report.diagnostics if item.code == "ROS2002"]
    assert len(errors) == 1
    assert display_path(errors[0].path).startswith("$.Conditions.BadRoot")


def test_module_registration_rejects_stack_output_and_forbidden_sections() -> None:
    report = validate_ros_template(
        MaterializedTemplateSource(
            """ROSTemplateFormatVersion: 2015-09-01
Workspace: {}
Rules:
  Rule: {Assertions: [{Assert: {Fn::Equals: [a, a]}}]}
Resources:
  Nested:
    Type: ALIYUN::ROS::Stack
Outputs:
  Value: {Value: {Fn::GetStackOutput: [stack, output]}}
"""
        ),
        RequestValidationContext(
            action="RegisterResourceType",
            semantic_mode=TemplateSemanticMode.MODULE_REGISTRATION,
            entity_type="Module",
        ),
    )
    assert any("Workspace" in item.summary for item in report.diagnostics)
    assert any("Rules" in item.summary for item in report.diagnostics)
    assert any("Fn::GetStackOutput" in item.summary for item in report.diagnostics)


def test_stack_template_with_module_uses_module_consumer_constraints_only_on_module_resources() -> None:
    report = validate(
        """ROSTemplateFormatVersion: 2015-09-01
Workspace: {}
Resources:
  Module.Bad:
    Type: MODULE::Example::Service::Type
    Count: 1
  Ordinary:
    Type: ALIYUN::ROS::Sleep
    Count: 1
"""
    )
    assert any("Workspace" in item.summary for item in report.diagnostics)
    assert any("Resources" in item.summary and "reserved character" in item.summary for item in report.diagnostics)
    count_errors = [item for item in report.diagnostics if "A MODULE resource cannot contain Count" in item.summary]
    assert len(count_errors) == 1


def test_json_semantic_values_and_synthetic_source_origin_are_preserved() -> None:
    parsed = parse_template_source('{"ROSTemplateFormatVersion":"2015-09-01","Values":[NaN,Infinity,-Infinity]}')
    assert parsed.template is not None
    values = parsed.template.data["Values"]
    assert all(isinstance(item, float) for item in values)

    report = validate_ros_template(
        MaterializedTemplateSource(
            '{"ROSTemplateFormatVersion":"2015-09-01","Resources":{"R":"bad"}}',
            origin_kind="SYNTHETIC_ADAPTER",
        ),
        RequestValidationContext(action="ValidateTemplate"),
    )
    diagnostic = next(item for item in report.diagnostics if item.code == "ROS1102")
    assert diagnostic.source_span is not None and diagnostic.source_span.synthetic
    assert report.to_dict()["diagnostics"][0]["synthetic"] is True


def test_function_valued_ref_and_list_merge_arguments_are_not_raw_shape_errors() -> None:
    report = validate(
        """ROSTemplateFormatVersion: 2015-09-01
Parameters:
  P: {Type: String, Default: value}
Conditions:
  Always: {Fn::Equals: [same, same]}
Resources:
  Wait:
    Type: ALIYUN::ROS::Sleep
    Properties:
      Triggers:
        RefValue: {Ref: {Fn::If: [Always, P, P]}}
        Merge: {Fn::ListMerge: {Fn::If: [Always, [[a], [b]], [[c], [d]]]}}
"""
    )
    assert not any(item.code == "ROS3001" for item in report.diagnostics)


def test_function_valued_ref_does_not_rebind_from_paramref_to_resource_ref() -> None:
    report = validate(
        """ROSTemplateFormatVersion: 2015-09-01
Conditions:
  Always: {Fn::Equals: [same, same]}
Resources:
  Target: {Type: ALIYUN::ROS::Sleep}
  Wait:
    Type: ALIYUN::ROS::Sleep
    Properties:
      Triggers:
        Bad: {Ref: {Fn::If: [Always, Target, Target]}}
"""
    )
    assert any(item.code == "ROS4001" and "ParamRef" in item.detail for item in report.diagnostics)


def test_sub_accepts_parameter_ref_template_and_rejects_implicit_count_resource() -> None:
    report = validate(
        """ROSTemplateFormatVersion: 2015-09-01
Parameters:
  Template: {Type: String, Default: "${Value}"}
Resources:
  Repeated:
    Type: ALIYUN::ROS::Sleep
    Count: 1
  Wait:
    Type: ALIYUN::ROS::Sleep
    Properties:
      Triggers:
        Good: {Fn::Sub: [{Ref: Template}, {Value: ok}]}
        BadRef: {Fn::Sub: "${Repeated}"}
        BadGetAtt: {Fn::Sub: "${Repeated.Data}"}
"""
    )
    assert not any(item.code == "ROS3001" and "Fn::Sub" in item.summary for item in report.diagnostics)
    assert sum(item.code == "ROS4303" for item in report.diagnostics) == 2


def test_condition_names_are_resolved_and_cycles_are_reported() -> None:
    report = validate(
        """ROSTemplateFormatVersion: 2015-09-01
Conditions:
  A: {Fn::Not: B}
  B: {Fn::And: [A, Missing]}
Resources: {}
"""
    )
    assert any(item.code == "ROS4003" and "Missing" in item.summary for item in report.diagnostics)
    assert any(item.code == "ROS4004" for item in report.diagnostics)


def test_condition_paramref_supports_runtime_hashable_parameter_keys_but_normal_ref_does_not() -> None:
    report = validate(
        """ROSTemplateFormatVersion: 2015-09-01
Parameters:
  1: {Type: Boolean, Default: true}
Conditions:
  RuntimeExtension: {Ref: 1}
Resources:
  Wait:
    Type: ALIYUN::ROS::Sleep
    Properties:
      Triggers: {NormalFactory: {Ref: 1}}
"""
    )
    assert any(item.code == "ROS5206" for item in report.diagnostics)
    normal_errors = [item for item in report.diagnostics if item.code == "ROS3001"]
    assert len(normal_errors) == 1


def test_boolean_parameter_string_default_is_normalized_before_condition_evaluation() -> None:
    report = validate(
        """ROSTemplateFormatVersion: 2015-09-01
Parameters:
  Enabled: {Type: Boolean, Default: "false"}
Conditions:
  Disabled: {Fn::Equals: [{Ref: Enabled}, false]}
Resources:
  Wait:
    Type: ALIYUN::ROS::Sleep
    Properties:
      Triggers: {Value: {Fn::If: [Disabled, ok, {Ref: Unreachable}]}}
"""
    )
    assert not any(item.code == "ROS4001" for item in report.diagnostics)


def test_invalid_boolean_parameter_default_is_reported() -> None:
    report = validate(
        """ROSTemplateFormatVersion: 2015-09-01
Parameters:
  Enabled: {Type: Boolean, Default: definitely-not-boolean}
Resources: {}
"""
    )
    assert any(item.code == "ROS4102" and "Boolean" in item.detail for item in report.diagnostics)


def test_empty_comma_delimited_list_default_selects_fallback() -> None:
    report = validate(
        """ROSTemplateFormatVersion: 2015-09-01
Parameters:
  Values: {Type: CommaDelimitedList, Default: ""}
Resources:
  Wait:
    Type: ALIYUN::ROS::Sleep
    Properties:
      Triggers: {Value: {Fn::Select: [0, {Ref: Values}, {Ref: MissingFallback}]}}
"""
    )
    assert any(item.code == "ROS4001" and "MissingFallback" in item.summary for item in report.diagnostics)


def test_precise_collection_function_consumer_types() -> None:
    report = validate(
        """ROSTemplateFormatVersion: 2015-09-01
Resources:
  Wait:
    Type: ALIYUN::ROS::Sleep
    Properties:
      Triggers:
        MemberList: {Fn::MemberListToMap: [name, value, 1]}
        Index: {Fn::Index: [a, text]}
        Jq: {Fn::Jq: [First, 1, {a: b}]}
        Namespace: {Fn::TransformNamespace: [1, 2, {arbitrary: value}]}
"""
    )
    summaries = "\n".join(item.summary for item in report.diagnostics)
    assert "Fn::MemberListToMap" in summaries
    assert "Fn::Index" in summaries
    assert "Fn::Jq" in summaries
    assert summaries.count("Fn::TransformNamespace") == 2
    assert not any(item.code == "ROS9999" for item in report.diagnostics)


def test_indexable_mapping_runtime_overloads_are_accepted_with_quality_warning() -> None:
    report = validate(
        """ROSTemplateFormatVersion: 2015-09-01
Resources:
  Wait:
    Type: ALIYUN::ROS::Sleep
    Properties:
      Triggers:
        Index:
          Fn::Index:
            0: a
            1: [a, b]
        Jq:
          Fn::Jq:
            0: First
            1: .
            2: {a: b}
"""
    )
    assert not any(item.code == "ROS3001" for item in report.diagnostics)
    assert sum(item.code == "ROS5208" for item in report.diagnostics) == 2


def test_rules_keep_runtime_truthiness_but_warn_for_non_boolean_function_results() -> None:
    report = validate(
        """ROSTemplateFormatVersion: 2015-09-01
Conditions:
  Named: true
Rules:
  Compatible:
    RuleCondition: Named
    Assertions:
      - Assert: {Fn::Length: [a]}
Resources: {}
"""
    )
    assert not any(item.code == "ROS3002" for item in report.diagnostics)
    assert any(item.code == "ROS5207" and "Assert" in item.summary for item in report.diagnostics)


def test_rules_report_known_false_assert_and_skip_disabled_assertion() -> None:
    report = validate(
        """ROSTemplateFormatVersion: 2015-09-01
Rules:
  Failing:
    Assertions:
      - Assert: false
        AssertDescription: expected failure
  Disabled:
    RuleCondition: false
    Assertions:
      - Assert: false
Resources: {}
"""
    )
    failures = [item for item in report.diagnostics if item.code == "ROS4006"]
    assert len(failures) == 1
    assert failures[0].source_span is not None and failures[0].source_span.line == 5
    assert "expected failure" in failures[0].detail


def test_calculate_whitelist_placeholder_order_and_nonfinite_integer_result() -> None:
    report = validate(
        """ROSTemplateFormatVersion: 2015-09-01
Resources:
  Wait:
    Type: ALIYUN::ROS::Sleep
    Properties:
      Triggers:
        Valid: {Fn::Calculate: ["{0} + {1}", 0, [1, 2]]}
        InvalidToken: {Fn::Calculate: [x, 0, [1]]}
        MissingNumber: {Fn::Calculate: ["{1}", 0, [1]]}
        NonFiniteInteger: {Fn::Calculate: ["1e10000", 0]}
"""
    )
    errors = [item for item in report.diagnostics if item.code == "ROS3003"]
    assert len(errors) == 3
    assert {item.source_span.line for item in errors if item.source_span is not None} == {8, 9, 10}


def test_avg_null_is_zero_but_nonfinite_integer_conversion_fails() -> None:
    report = validate(
        """ROSTemplateFormatVersion: 2015-09-01
Resources:
  Wait:
    Type: ALIYUN::ROS::Sleep
    Properties:
      Triggers:
        NullMember: {Fn::Avg: [0, [null, 2]]}
        NonFinite: {Fn::Avg: [0, [.inf, 2]]}
"""
    )
    errors = [item for item in report.diagnostics if item.code == "ROS3003"]
    assert len(errors) == 1
    assert "non-finite" in errors[0].summary


def test_avg_and_calculate_warn_when_runtime_returns_nonfinite_number() -> None:
    report = validate(
        """ROSTemplateFormatVersion: 2015-09-01
Resources:
  Wait:
    Type: ALIYUN::ROS::Sleep
    Properties:
      Triggers:
        Average: {Fn::Avg: [1, ["inf", 2]]}
        Calculate: {Fn::Calculate: ["{0}", 1, ["1e10000"]]}
"""
    )
    assert report.error_count == 0
    warnings = [item for item in report.diagnostics if item.code == "ROS5205"]
    assert len(warnings) == 2
    assert {item.subject for item in warnings} == {"nonfinite-result"}


def test_locals_are_expanded_before_context_and_count_analysis() -> None:
    report = validate(
        """ROSTemplateFormatVersion: 2015-09-01
Parameters: {P: {Type: String, Default: a}}
Locals:
  IsA: {Type: Eval, Value: {Fn::Equals: [{Ref: P}, a]}}
  Copies: {Type: Macro, Value: 2}
Conditions: {UseIt: {Ref: IsA}}
Resources:
  Repeated: {Type: ALIYUN::ROS::Sleep, Count: {Ref: Copies}}
  Wait:
    Type: ALIYUN::ROS::Sleep
    Properties: {Triggers: {First: {Fn::Select: [0, {Ref: Repeated}]}}}
"""
    )
    assert report.error_count == 0


def test_conflicting_symbols_are_poisoned_and_link_every_declaration() -> None:
    report = validate(
        """ROSTemplateFormatVersion: 2015-09-01
Parameters:
  Shared: {Type: String, Default: scalar}
Locals:
  Shared: {Type: Macro, Value: local}
Resources:
  Shared: {Type: DATASOURCE::DTS::MigrationJobs}
  Wait:
    Type: ALIYUN::ROS::Sleep
    Properties:
      Triggers:
        First: {Fn::Select: [0, {Ref: Shared}]}
"""
    )
    conflicts = [item for item in report.diagnostics if item.code == "ROS4201"]
    assert len(conflicts) == 1
    assert conflicts[0].source_span is not None and conflicts[0].source_span.line == 3
    assert {
        location.source_span.line for location in conflicts[0].related_locations if location.source_span is not None
    } == {5, 7}
    assert not any(item.code == "ROS5002" for item in report.diagnostics)


def test_conflicting_symbol_poison_suppresses_getatt_and_sub_cascades() -> None:
    report = validate(
        """ROSTemplateFormatVersion: 2015-09-01
Parameters:
  Shared: {Type: String, Default: parameter}
Resources:
  Shared:
    Type: ALIYUN::ROS::Sleep
    Count: 2
  Wait:
    Type: ALIYUN::ROS::Sleep
    Properties:
      Triggers:
        ImplicitRef: {Fn::Sub: "${Shared}"}
        ExplicitVariable: {Fn::Sub: ["${Shared}", {Shared: value}]}
Outputs:
  Values:
    Value:
      - {Fn::GetAtt: [Shared, Data]}
"""
    )
    assert sum(item.code == "ROS4201" for item in report.diagnostics) == 1
    assert not any(item.code == "ROS4303" for item in report.diagnostics)


def test_dotted_sub_uses_exact_or_longest_resource_name_for_poison() -> None:
    report = validate(
        """ROSTemplateFormatVersion: 2015-09-01
Parameters:
  A.B: {Type: String, Default: parameter}
Resources:
  A.B: {Type: ALIYUN::ROS::Sleep, Count: 2}
  Wait:
    Type: ALIYUN::ROS::Sleep
    Properties:
      Triggers:
        Exact: {Fn::ListMerge: {Fn::Sub: "${A.B}"}}
        Attribute: {Fn::ListMerge: {Fn::Sub: "${A.B.Data}"}}
"""
    )
    assert [item.code for item in report.diagnostics] == ["ROS4201"]


def test_dotted_sub_prefers_runtime_prefix_resource_binding() -> None:
    report = validate(
        """ROSTemplateFormatVersion: 2015-09-01
Parameters:
  A.B: {Type: String, Default: parameter}
Resources:
  A: {Type: ALIYUN::ROS::Sleep}
  A.B: {Type: ALIYUN::ROS::Sleep}
  Wait:
    Type: ALIYUN::ROS::Sleep
    Properties:
      Triggers:
        Value: {Fn::ListMerge: {Fn::Sub: "${A.B}"}}
"""
    )
    assert [item.code for item in report.diagnostics] == ["ROS4201", "ROS3002"]


def test_dotted_sub_count_instance_inherits_dotted_base_poison() -> None:
    report = validate(
        """ROSTemplateFormatVersion: 2015-09-01
Parameters:
  A.B: {Type: String, Default: parameter}
Resources:
  A.B: {Type: DATASOURCE::ACM::Configurations, Count: 1}
  Wait:
    Type: ALIYUN::ROS::Sleep
    Properties:
      Triggers:
        RefValue: {Fn::Sub: "${A.B[0]}"}
        AttributeValue: {Fn::Sub: "${A.B[0].Data}"}
"""
    )
    assert [item.code for item in report.diagnostics] == ["ROS4201"]


def test_sub_parameter_ref_consumes_conflicting_symbol_poison() -> None:
    report = validate(
        """ROSTemplateFormatVersion: 2015-09-01
Parameters:
  Shared: {Type: String, Default: "${X}"}
Resources:
  Shared: {Type: ALIYUN::ROS::Sleep}
  Wait:
    Type: ALIYUN::ROS::Sleep
    Properties:
      Triggers:
        Value: {Fn::Sub: [{Ref: Shared}, {X: ok}]}
"""
    )
    assert [item.code for item in report.diagnostics] == ["ROS4201"]


def test_count_instances_inherit_conflicting_base_symbol_poison() -> None:
    report = validate(
        """ROSTemplateFormatVersion: 2015-09-01
Parameters:
  Shared: {Type: String, Default: parameter}
Resources:
  Shared: {Type: DATASOURCE::ACM::Configurations, Count: 1}
  Wait:
    Type: ALIYUN::ROS::Sleep
    Properties:
      Triggers:
        RefValue: {Fn::Base64: {Ref: "Shared[0]"}}
        AttributeValue: {Fn::Base64: {Fn::GetAtt: ["Shared[0]", DataIds]}}
"""
    )
    assert [item.code for item in report.diagnostics] == ["ROS4201"]


def test_explicit_bracket_resource_does_not_inherit_base_poison() -> None:
    report = validate(
        """ROSTemplateFormatVersion: 2015-09-01
Parameters:
  Shared: {Type: String, Default: parameter}
Resources:
  Shared: {Type: ALIYUN::ROS::Sleep}
  Shared[0]: {Type: DATASOURCE::ACM::Configurations}
  Wait:
    Type: ALIYUN::ROS::Sleep
    Properties:
      Triggers:
        Value: {Fn::Base64: {Ref: "Shared[0]"}}
"""
    )
    assert [item.code for item in report.diagnostics] == ["ROS4201", "ROS3002"]


def test_depends_on_count_instances_inherit_conflicting_base_poison() -> None:
    static_report = validate(
        """ROSTemplateFormatVersion: 2015-09-01
Parameters:
  Shared: {Type: String, Default: parameter}
Resources:
  Shared: {Type: ALIYUN::ROS::Sleep, Count: 1}
  Wait:
    Type: ALIYUN::ROS::Sleep
    DependsOn: "Shared[2]"
"""
    )
    dynamic_report = validate(
        """ROSTemplateFormatVersion: 2015-09-01
Parameters:
  Shared: {Type: String, Default: parameter}
  Copies: {Type: Number}
Resources:
  Shared: {Type: ALIYUN::ROS::Sleep, Count: {Ref: Copies}}
  Wait:
    Type: ALIYUN::ROS::Sleep
    DependsOn: "Shared[0]"
"""
    )

    assert [item.code for item in static_report.diagnostics] == ["ROS4201", "ROS4002"]
    assert [item.code for item in dynamic_report.diagnostics] == ["ROS4201"]


def test_invalid_count_instances_are_poisoned_in_all_reference_forms() -> None:
    report = validate(
        """ROSTemplateFormatVersion: 2015-09-01
Resources:
  Bad: {Type: ALIYUN::ROS::Sleep, Count: nope}
  Wait:
    Type: ALIYUN::ROS::Sleep
    DependsOn: "Bad[0]"
    Properties:
      Triggers:
        RefValue: {Fn::Base64: {Ref: "Bad[0]"}}
        AttributeValue: {Fn::Base64: {Fn::GetAtt: ["Bad[0]", Data]}}
        SubValue: {Fn::Base64: {Fn::Sub: "${Bad[0]}"}}
"""
    )
    assert [item.code for item in report.diagnostics] == ["ROS4301"]


def test_count_local_cycle_does_not_raise_internal_error() -> None:
    report = validate(
        """ROSTemplateFormatVersion: 2015-09-01
Locals:
  A: {Type: Macro, Value: {Ref: A}}
Resources:
  Wait:
    Type: ALIYUN::ROS::Sleep
    Count: {Ref: A}
"""
    )
    assert [item.code for item in report.diagnostics] == ["ROS4205"]


def test_cyclic_local_still_reports_independent_sibling_error() -> None:
    report = validate(
        """ROSTemplateFormatVersion: 2015-09-01
Locals:
  A:
    Type: Macro
    Value:
      Self: {Ref: A}
      Bad: {Fn::Select: [0, not-a-list]}
Resources:
  Wait:
    Type: ALIYUN::ROS::Sleep
    Properties:
      Triggers: {Value: {Ref: A}}
"""
    )
    assert [item.code for item in report.diagnostics] == ["ROS4205", "ROS5002"]
    assert display_path(report.diagnostics[1].path).endswith(".Locals.A.Value.Bad.Fn::Select[1]")


def test_metadata_and_update_policy_analyze_noneligible_count_references() -> None:
    report = validate(
        """ROSTemplateFormatVersion: 2015-09-01
Resources:
  Repeated: {Type: ALIYUN::ROS::Sleep, Count: 1}
  Wait:
    Type: ALIYUN::ROS::Sleep
    Metadata:
      BadRef: {Ref: Repeated}
    UpdatePolicy:
      BadGetAtt: {Fn::GetAtt: [Repeated, Data]}
"""
    )
    count_errors = [item for item in report.diagnostics if item.code == "ROS4303"]
    assert len(count_errors) == 2
    assert {display_path(item.path) for item in count_errors} == {
        "$.Resources.Wait.Metadata.BadRef.Ref",
        "$.Resources.Wait.UpdatePolicy.BadGetAtt.Fn::GetAtt",
    }


def test_metadata_and_update_policy_require_mapping_or_function_roots() -> None:
    report = validate(
        """ROSTemplateFormatVersion: 2015-09-01
Parameters:
  Policy: {Type: Json, Default: {Mode: Auto}}
Resources:
  Valid:
    Type: ALIYUN::ROS::Sleep
    Metadata: {Ref: Policy}
    UpdatePolicy: {Ref: Policy}
  Invalid:
    Type: ALIYUN::ROS::Sleep
    Metadata: nope
    UpdatePolicy: []
"""
    )
    shape_errors = [item for item in report.diagnostics if item.code == "ROS1105"]
    assert len(shape_errors) == 2
    assert {display_path(item.path) for item in shape_errors} == {
        "$.Resources.Invalid.Metadata",
        "$.Resources.Invalid.UpdatePolicy",
    }


def test_resource_auxiliary_fields_do_not_expand_local_refs() -> None:
    report = validate(
        """ROSTemplateFormatVersion: 2015-09-01
Locals:
  Keep: {Type: Macro, Value: Retain}
Resources:
  Wait:
    Type: ALIYUN::ROS::Sleep
    Count: 1
    DependsOn: {Ref: Keep}
    Metadata:
      Value: {Ref: Keep}
    UpdatePolicy:
      Value: {Ref: Keep}
    DeletionPolicy: {Ref: Keep}
"""
    )
    local_scope_errors = [item for item in report.diagnostics if item.code == "ROS4214"]
    assert len(local_scope_errors) == 4
    assert {display_path(item.path) for item in local_scope_errors} == {
        "$.Resources.Wait.DependsOn.Ref",
        "$.Resources.Wait.Metadata.Value.Ref",
        "$.Resources.Wait.UpdatePolicy.Value.Ref",
        "$.Resources.Wait.DeletionPolicy.Ref",
    }


def test_deletion_policy_only_accepts_enum_string_or_parameter_ref() -> None:
    report = validate(
        """ROSTemplateFormatVersion: 2015-09-01
Parameters:
  ValidPolicy: {Type: String, Default: Retain}
  NullPolicy: {Type: String, Default: null}
  InvalidPolicy: {Type: String, Default: Archive}
  InvalidListPolicy: {Type: Json, Default: [Retain]}
  SnapshotPolicy: {Type: String, Default: Snapshot}
Resources:
  Target: {Type: ALIYUN::ROS::Sleep}
  Valid:
    Type: ALIYUN::ROS::Sleep
    DeletionPolicy: {Ref: ValidPolicy}
  ValidNullResult:
    Type: ALIYUN::ROS::Sleep
    DeletionPolicy: {Ref: NullPolicy}
  ResourceRef:
    Type: ALIYUN::ROS::Sleep
    DeletionPolicy: {Ref: Target}
  OtherFunction:
    Type: ALIYUN::ROS::Sleep
    DeletionPolicy: {Fn::Join: ["", [Ret, ain]]}
  ExplicitNull:
    Type: ALIYUN::ROS::Sleep
    DeletionPolicy: null
  InvalidLiteral:
    Type: ALIYUN::ROS::Sleep
    DeletionPolicy: Archive
  InvalidParameterValue:
    Type: ALIYUN::ROS::Sleep
    DeletionPolicy: {Ref: InvalidPolicy}
  InvalidParameterType:
    Type: ALIYUN::ROS::Sleep
    DeletionPolicy: {Ref: InvalidListPolicy}
  NullRefName:
    Type: ALIYUN::ROS::Sleep
    DeletionPolicy: {Ref: null}
  SnapshotLiteral:
    Type: ALIYUN::ROS::Sleep
    DeletionPolicy: Snapshot
  SnapshotParameter:
    Type: ALIYUN::ROS::Sleep
    DeletionPolicy: {Ref: SnapshotPolicy}
"""
    )
    policy_errors = [item for item in report.diagnostics if item.code == "ROS1104"]
    assert len(policy_errors) == 9
    assert not any("ValidPolicy" in item.summary for item in policy_errors)
    assert sum("Snapshot" in display_path(item.path) for item in policy_errors) == 2
    assert sum("missing a Parameter name" in item.summary for item in policy_errors) == 1


def test_deletion_policy_rejects_fixed_pseudo_parameter_and_snapshot_allowed_values() -> None:
    report = validate(
        """ROSTemplateFormatVersion: 2015-09-01
Parameters:
  SnapshotOnly: {Type: String, AllowedValues: [Snapshot]}
  SnapshotOptional: {Type: String, AllowedValues: [Delete, Retain, Snapshot]}
  ValidPolicy: {Type: String, AllowedValues: [Delete, Retain]}
Resources:
  StackIdPolicy:
    Type: ALIYUN::ROS::Sleep
    DeletionPolicy: {Ref: ALIYUN::StackId}
  NoValuePolicy:
    Type: ALIYUN::ROS::Sleep
    DeletionPolicy: {Ref: ALIYUN::NoValue}
  SnapshotOnlyPolicy:
    Type: ALIYUN::ROS::Sleep
    DeletionPolicy: {Ref: SnapshotOnly}
  SnapshotOptionalPolicy:
    Type: ALIYUN::ROS::Sleep
    DeletionPolicy: {Ref: SnapshotOptional}
  Valid:
    Type: ALIYUN::ROS::Sleep
    DeletionPolicy: {Ref: ValidPolicy}
"""
    )

    errors = [item for item in report.diagnostics if item.code == "ROS1104"]
    assert len(errors) == 3
    assert sum("Snapshot" in item.summary for item in errors) == 2
    assert sum("pseudo parameter" in item.summary for item in errors) == 1
    assert not any("NoValuePolicy" in display_path(item.path) for item in errors)


def test_count_select_getatt_does_not_fold_conflicting_resource() -> None:
    report = validate(
        """ROSTemplateFormatVersion: 2015-09-01
Parameters:
  Shared: {Type: String, Default: parameter}
  Index: {Type: Number, Default: 5}
Resources:
  Shared: {Type: DATASOURCE::ACM::Configurations, Count: 1}
  Wait:
    Type: ALIYUN::ROS::Sleep
    Properties:
      Triggers:
        Value: {Fn::Select: [{Ref: Index}, {Fn::GetAtt: [Shared, DataIds]}]}
"""
    )
    assert [item.code for item in report.diagnostics] == ["ROS4201"]


def test_symbol_conflict_related_path_preserves_each_typed_key() -> None:
    report = validate(
        """ROSTemplateFormatVersion: 2015-09-01
Parameters:
  true: {Type: String, Default: value}
Resources:
  1: {Type: ALIYUN::ROS::Sleep}
"""
    )
    conflict = next(item for item in report.diagnostics if item.code == "ROS4201")
    assert len(conflict.related_locations) == 1
    related_segments = path_segments(conflict.related_locations[0].path)
    assert related_segments[-1]["key_kind"] == ScalarKind.INTEGER.value


def test_conflicting_symbol_does_not_suppress_independent_ref_shape_error() -> None:
    report = validate(
        """ROSTemplateFormatVersion: 2015-09-01
Parameters:
  true: {Type: String, Default: parameter}
Resources:
  1: {Type: ALIYUN::ROS::Sleep}
  Wait:
    Type: ALIYUN::ROS::Sleep
    Properties:
      Triggers:
        Value: {Ref: 1}
"""
    )
    assert sum(item.code == "ROS4201" for item in report.diagnostics) == 1
    assert sum(item.code == "ROS3001" for item in report.diagnostics) == 1


def test_local_occurrence_preserves_resource_and_output_consumer_context() -> None:
    resource_report = validate(
        """ROSTemplateFormatVersion: 2015-09-01
Locals:
  Zones:
    Value: {Fn::GetAZs: ""}
Resources:
  Wait:
    Type: ALIYUN::ROS::Sleep
    Properties:
      Triggers:
        Value: {Ref: Zones}
"""
    )
    output_report = validate(
        """ROSTemplateFormatVersion: 2015-09-01
Locals:
  Zones:
    Value: {Fn::GetAZs: ""}
Resources: {}
Outputs:
  Zones:
    Value: {Ref: Zones}
"""
    )

    for report, consumer_line in ((resource_report, 10), (output_report, 8)):
        warnings = [item for item in report.diagnostics if item.code == "ROS5202"]
        assert len(warnings) == 1
        assert warnings[0].source_span is not None and warnings[0].source_span.line == 4
        assert len(warnings[0].related_locations) == 1
        assert warnings[0].related_locations[0].source_span is not None
        assert warnings[0].related_locations[0].source_span.line == consumer_line


def test_nested_functions_preserve_resource_consumer_context() -> None:
    direct_report = validate(
        """ROSTemplateFormatVersion: 2015-09-01
Resources:
  Wait:
    Type: ALIYUN::ROS::Sleep
    Properties:
      Triggers:
        Value: {Fn::Select: [0, {Fn::GetAZs: ""}]}
"""
    )
    local_report = validate(
        """ROSTemplateFormatVersion: 2015-09-01
Locals:
  FirstZone:
    Value: {Fn::Select: [0, {Fn::GetAZs: ""}]}
Resources:
  Wait:
    Type: ALIYUN::ROS::Sleep
    Properties:
      Triggers:
        Value: {Ref: FirstZone}
"""
    )

    assert sum(item.code == "ROS5202" for item in direct_report.diagnostics) == 1
    local_warning = next(item for item in local_report.diagnostics if item.code == "ROS5202")
    assert local_warning.source_span is not None and local_warning.source_span.line == 4
    assert len(local_warning.related_locations) == 1
    assert local_warning.related_locations[0].source_span is not None
    assert local_warning.related_locations[0].source_span.line == 10


def test_if_condition_preserves_resource_consumer_context() -> None:
    report = validate(
        """ROSTemplateFormatVersion: 2015-09-01
Resources:
  Wait:
    Type: ALIYUN::ROS::Sleep
    Properties:
      Triggers:
        Value:
          Fn::If:
            - {Fn::Select: [0, {Fn::GetAZs: ""}]}
            - yes
            - no
"""
    )
    assert sum(item.code == "ROS5202" for item in report.diagnostics) == 1


def test_local_expansion_diagnostic_points_to_origin_and_links_consumer() -> None:
    report = validate(
        """ROSTemplateFormatVersion: 2015-09-01
Locals:
  Bad:
    Value: {Fn::Select: [0, not-a-list]}
Resources:
  Wait:
    Type: ALIYUN::ROS::Sleep
    Properties:
      Triggers:
        First: {Ref: Bad}
        Second: {Ref: Bad}
"""
    )
    error = next(item for item in report.diagnostics if item.code == "ROS5002")
    assert error.source_span is not None and error.source_span.line == 4
    assert display_path(error.path) == "$.Locals.Bad.Value.Fn::Select[1]"
    assert {location.source_span.line for location in error.related_locations if location.source_span is not None} == {
        10,
        11,
    }

    from iac_code.tools.cloud.aliyun.ros_validation.renderer import render_validation_report

    rendered = render_validation_report(report, blocking=True)
    assert "line 10:" in rendered and "$.Resources.Wait.Properties.Triggers.First.Ref" in rendered
    assert "line 11:" in rendered and "$.Resources.Wait.Properties.Triggers.Second.Ref" in rendered


def test_nested_local_diagnostic_links_intermediate_and_final_consumers() -> None:
    report = validate(
        """ROSTemplateFormatVersion: 2015-09-01
Locals:
  Bad:
    Value: {Fn::Select: [0, not-a-list]}
  Wrapper:
    Value: {Ref: Bad}
Resources:
  Wait:
    Type: ALIYUN::ROS::Sleep
    Properties:
      Triggers:
        Value: {Ref: Wrapper}
"""
    )
    error = next(item for item in report.diagnostics if item.code == "ROS5002")
    assert error.source_span is not None and error.source_span.line == 4
    assert {location.source_span.line for location in error.related_locations if location.source_span is not None} == {
        6,
        12,
    }


def test_eval_local_runs_normal_first_pass_then_reparses_residual_at_occurrence() -> None:
    report = validate(
        """ROSTemplateFormatVersion: 2015-09-01
Locals:
  EncodedEquals:
    Type: Eval
    Value: {Fn::Equals: [{Fn::Base64: a}, YQ==]}
  Residual:
    Type: Eval
    Value: {Fn::Equals: [a, a]}
Conditions: {Matches: {Ref: EncodedEquals}}
Resources:
  Wait:
    Type: ALIYUN::ROS::Sleep
    Properties: {Triggers: {LiteralMap: {Ref: Residual}}}
"""
    )
    assert report.error_count == 0
    assert not any(item.code == "ROS2002" for item in report.diagnostics)


def test_unused_eval_local_still_rejects_outer_resource_scope() -> None:
    report = validate(
        """ROSTemplateFormatVersion: 2015-09-01
Locals:
  Invalid: {Type: Eval, Value: {Ref: Outer}}
Resources:
  Outer: {Type: ALIYUN::ROS::Sleep}
"""
    )
    errors = [item for item in report.diagnostics if item.code == "ROS4212"]
    assert len(errors) == 1
    assert errors[0].source_span is not None and errors[0].source_span.line == 3


def test_datasource_local_is_scoped_to_local_dependency_graph() -> None:
    report = validate(
        """ROSTemplateFormatVersion: 2015-09-01
Locals:
  Account: {Type: DATASOURCE::RAM::AccountAlias, Properties: {}}
  Expanded: {Type: Macro, Value: {Ref: Account}}
Resources:
  Wait:
    Type: ALIYUN::ROS::Sleep
    Properties:
      Triggers:
        Good: {Ref: Expanded}
        Bad: {Ref: Account}
"""
    )
    assert sum(item.code == "ROS4210" for item in report.diagnostics) == 1


def test_nested_stack_locals_match_runtime_and_official_boundaries() -> None:
    with_outer_locals = validate(
        """ROSTemplateFormatVersion: 2015-09-01
Locals: {Value: {Type: Macro, Value: ok}}
Resources:
  Nested:
    Type: ALIYUN::ROS::Stack
    Properties:
      TemplateBody:
        ROSTemplateFormatVersion: 2015-09-01
        Locals: {}
"""
    )
    assert sum(item.code == "ROS4213" for item in with_outer_locals.diagnostics) == 1

    without_outer_locals = validate(
        """ROSTemplateFormatVersion: 2015-09-01
Resources:
  Nested:
    Type: ALIYUN::ROS::Stack
    Properties:
      TemplateBody:
        ROSTemplateFormatVersion: 2015-09-01
        Locals: {}
"""
    )
    assert not any(item.code == "ROS4213" for item in without_outer_locals.diagnostics)
    assert sum(item.code == "ROS5211" for item in without_outer_locals.diagnostics) == 1

    remote = validate(
        """ROSTemplateFormatVersion: 2015-09-01
Resources:
  Nested:
    Type: ALIYUN::ROS::Stack
    Properties: {TemplateURL: https://example.invalid/template.yaml}
"""
    )
    assert sum(item.code == "ROS9104" for item in remote.diagnostics) == 1


def test_count_rejects_getazs_and_does_not_lift_mapping_getatt() -> None:
    report = validate(
        """ROSTemplateFormatVersion: 2015-09-01
Resources:
  Invalid: {Type: ALIYUN::ROS::Sleep, Count: {Fn::GetAZs: ""}}
  Repeated: {Type: ALIYUN::ECS::Instance, Count: 1}
  Wait:
    Type: ALIYUN::ROS::Sleep
    Properties:
      Triggers:
        Value:
          Fn::GetAtt:
            Repeated: InstanceId
            InstanceId: ignored
"""
    )
    assert sum(item.code == "ROS4301" for item in report.diagnostics) == 1
    count_getatt_error = next(item for item in report.diagnostics if item.code == "ROS4303")
    assert "expands into multiple instances" in count_getatt_error.summary
    assert "cannot be automatically rewritten into an attribute list" in count_getatt_error.summary
    assert "eligible" not in count_getatt_error.detail
    assert sum(item.code == "ROS5208" for item in report.diagnostics) == 1


def test_mapping_getatt_runtime_iterable_is_accepted_without_count() -> None:
    report = validate(
        """ROSTemplateFormatVersion: 2015-09-01
Resources:
  Target: {Type: ALIYUN::ECS::Instance}
  Wait:
    Type: ALIYUN::ROS::Sleep
    Properties:
      Triggers:
        Value:
          Fn::GetAtt:
            Target: InstanceId
            InstanceId: ignored
"""
    )
    assert report.error_count == 0
    assert sum(item.code == "ROS5208" for item in report.diagnostics) == 1


def test_unknown_parameter_binding_reports_nonblocking_limitation() -> None:
    report = validate_ros_template(
        MaterializedTemplateSource(
            """ROSTemplateFormatVersion: 2015-09-01
Parameters: {P: {Type: String}}
Resources:
  Wait:
    Type: ALIYUN::ROS::Sleep
    Properties: {Triggers: {Value: {Ref: P}}}
"""
        ),
        RequestValidationContext(action="PreviewStack"),
        parameter_bindings={"P": object()},
    )
    assert report.error_count == 0
    assert sum(item.code == "ROS9002" and item.severity == Severity.LIMITATION for item in report.diagnostics) == 1


def test_alias_diagnostic_points_to_use_and_relates_anchor() -> None:
    report = validate(
        """ROSTemplateFormatVersion: 2015-09-01
Values: &bad
  Fn::Select: [0, text]
Resources:
  Wait:
    Type: ALIYUN::ROS::Sleep
    Properties:
      Triggers: *bad
"""
    )
    error = next(item for item in report.diagnostics if item.code == "ROS5002")
    assert error.source_span is not None and error.source_span.line == 8
    assert len(error.related_locations) == 1
    assert error.related_locations[0].source_span is not None
    assert error.related_locations[0].source_span.line == 3


def test_mapping_key_alias_does_not_steal_later_value_alias_position() -> None:
    body = """ROSTemplateFormatVersion: 2015-09-01
AliasName: &key TriggerName
AliasKeyMap:
  *key: value
Bad: &bad
  Fn::Select: [0, text]
Resources:
  Wait:
    Type: ALIYUN::ROS::Sleep
    Properties:
      Triggers: *bad
"""
    parsed = parse_template_source(body)
    assert parsed.template is not None
    alias_key = next(
        node for node in parsed.template.source_map.occurrences if node.value == "TriggerName" and node.span.line == 4
    )
    assert len(alias_key.origin_node_ids) == 1
    origin = parsed.template.source_map.node_by_id(alias_key.origin_node_ids[0])
    assert origin is not None and origin.span.line == 2

    report = validate(body)
    error = next(item for item in report.diagnostics if item.code == "ROS5002")
    assert error.source_span is not None and error.source_span.line == 11
    assert len(error.related_locations) == 1
    assert error.related_locations[0].source_span is not None
    assert error.related_locations[0].source_span.line == 6


def test_machine_diagnostic_path_is_json_safe_for_non_json_yaml_keys() -> None:
    path = (mapping_segment(b"binary-key"), mapping_segment(float("nan")))
    diagnostic = make_diagnostic(
        code="TEST",
        severity=Severity.ERROR,
        category=Category.COMPATIBILITY,
        summary="test",
        detail="test",
        path=path,
    )
    payload = ValidationReport.build([diagnostic]).to_dict()
    encoded = json.dumps(payload, allow_nan=False)
    assert '"path_kind": "ROS_PATH"' in encoded
    values = [item["value"] for item in payload["diagnostics"][0]["path_segments"]]
    assert values[0]["kind"] == "Binary"
    assert values[0]["byte_length"] == len(b"binary-key")
    assert values[1] == {"kind": "Number", "finiteness": "NAN"}


def test_all_public_runtime_function_handlers_have_a_valid_smoke_path() -> None:
    report = validate(
        """ROSTemplateFormatVersion: 2015-09-01
Parameters:
  P: {Type: String, Default: p}
Mappings:
  M: {A: {B: value}}
Conditions:
  Eq: {Fn::Equals: [a, a]}
  Negated: {Fn::Not: [Eq]}
  Both: {Fn::And: [Eq, Negated]}
  Either: {Fn::Or: [Eq, Negated]}
Resources:
  Vpc: {Type: ALIYUN::ECS::VPC}
  Wait:
    Type: ALIYUN::ROS::Sleep
    Properties:
      Triggers:
        FindInMap: {Fn::FindInMap: [M, A, B]}
        GetAZs: {Fn::GetAZs: ""}
        Ref: {Ref: P}
        GetAtt: {Fn::GetAtt: [Vpc, VpcId]}
        Select: {Fn::Select: [0, [a]]}
        Join: {Fn::Join: [",", [a, 1, null]]}
        Split: {Fn::Split: [",", "a,b"]}
        Replace: {Fn::Replace: [{a: b}, a]}
        Base64: {Fn::Base64: a}
        Base64Encode: {Fn::Base64Encode: a}
        Base64Decode: {Fn::Base64Decode: YQ==}
        MemberListToMap: {Fn::MemberListToMap: [name, value, [member.0.name=x]]}
        If: {Fn::If: [Eq, a, b]}
        ListMerge: {Fn::ListMerge: [[a], [b]]}
        GetJsonValue: {Fn::GetJsonValue: [a, {a: b}]}
        MergeMapToList: {Fn::MergeMapToList: [{a: [x]}]}
        MergeMap: {Fn::MergeMap: [{a: 1}, {b: 2}]}
        SelectMapList: {Fn::SelectMapList: [a, [{a: b}]]}
        Add: {Fn::Add: [1, 2]}
        Avg: {Fn::Avg: [0, [1, 2]]}
        Str: {Fn::Str: {a: b}}
        Calculate: {Fn::Calculate: ["{0}", 0, [1]]}
        Sub: {Fn::Sub: "${P}"}
        Max: {Fn::Max: [1, 2]}
        Min: {Fn::Min: [1, 2]}
        GetStackOutput: {Fn::GetStackOutput: [stack, output]}
        Jq: {Fn::Jq: [First, ., {a: b}]}
        Length: {Fn::Length: [a]}
        Index: {Fn::Index: [a, [a]]}
        FormatTime: {Fn::FormatTime: "%Y"}
        Any: {Fn::Any: [true, false]}
        MarketplaceImage: {Fn::MarketplaceImage: image}
        Contains: {Fn::Contains: [[a], a]}
        EachMemberIn: {Fn::EachMemberIn: [[a], [a, b]]}
        MatchPattern: {Fn::MatchPattern: ["a+", aa]}
        TransformNamespace: {Fn::TransformNamespace: [Condition, Ns, {Condition: Eq}]}
        Indent: {Fn::Indent: [value, 1]}
        Cidr: {Fn::Cidr: [10.0.0.0/24, 2, 1]}
"""
    )
    assert not any(item.severity.value == "ERROR" for item in report.diagnostics), [
        (item.code, item.summary) for item in report.diagnostics
    ]


def test_single_expression_container_arguments_report_root_type_errors() -> None:
    report = validate(
        """ROSTemplateFormatVersion: 2015-09-01
Resources:
  Wait:
    Type: ALIYUN::ROS::Sleep
    Properties:
      Triggers:
        Base64: {Fn::Base64Encode: [a, b]}
        AZs: {Fn::GetAZs: []}
        Join: {Fn::Join: [{Fn::Any: [true, false]}, [a, b]]}
"""
    )
    summaries = [item.summary for item in report.diagnostics if item.code == "ROS3002"]
    assert any("Fn::Base64Encode" in summary for summary in summaries)
    assert any("Fn::GetAZs" in summary for summary in summaries)
    assert any("Fn::Join" in summary for summary in summaries)


def test_runtime_ordered_function_edge_contracts() -> None:
    report = validate(
        """ROSTemplateFormatVersion: 2015-09-01
Parameters:
  N: {Type: Number, Default: 42}
  Script: {Type: String, Default: "$ENV"}
Mappings:
  Args:
    Values:
      Add: [1, 2]
Resources:
  A.B: {Type: ALIYUN::ROS::Sleep}
  Wait:
    Type: ALIYUN::ROS::Sleep
    Properties:
      Triggers:
        AddNull: {Fn::Add: [1, null]}
        AddFunction: {Fn::Add: {Fn::FindInMap: [Args, Values, Add]}}
        SubNumber: {Fn::Sub: [{Ref: N}, {}]}
        SubDottedResource: {Fn::Sub: "${A.B}"}
        DynamicEnvIsAllowed: {Fn::Jq: [First, {Ref: Script}, "{}"]}
"""
    )
    assert report.error_count == 0, [(item.code, item.summary) for item in report.diagnostics]


def test_strict_runtime_function_failures_are_detected() -> None:
    report = validate(
        """ROSTemplateFormatVersion: 2015-09-01
Resources:
  Wait:
    Type: ALIYUN::ROS::Sleep
    Properties:
      Triggers:
        Cidr: {Fn::Cidr: [192.168.0.0/24, 5, 6]}
        Select: {Fn::Select: [0, [hit], fallback, {Ref: MissingButUnreachable}]}
        Member: {Fn::MemberListToMap: [Name, Value, [1]]}
        Merge: {Fn::MergeMapToList: [{a: 1}]}
        SelectMap: {Fn::SelectMapList: [x, [1]]}
        JqEnv: {Fn::Jq: [First, ". | $ENV", "{}"]}
        JqJson: {Fn::Jq: [First, ., not-json]}
        Contains: {Fn::Contains: [[[1]], 1]}
        Each: {Fn::EachMemberIn: [[[1]], [[1]]]}
        Avg: {Fn::Avg: [0, "12"]}
        Time: {Fn::FormatTime: ["%Y", Definitely/Not_A_Timezone]}
"""
    )
    summaries = "\n".join(item.summary for item in report.diagnostics)
    for name in (
        "Fn::Cidr",
        "Fn::Select",
        "Fn::MemberListToMap",
        "Fn::MergeMapToList",
        "Fn::SelectMapList",
        "Fn::Jq",
        "Fn::Contains",
        "Fn::EachMemberIn",
        "Fn::Avg",
        "Fn::FormatTime",
    ):
        assert name in summaries


def test_function_form_ref_accepts_hashable_non_string_parameter_name() -> None:
    report = validate(
        """ROSTemplateFormatVersion: 2015-09-01
Parameters:
  1: {Type: String, Default: value}
Mappings:
  M: {Top: {Key: 1}}
Resources:
  Wait:
    Type: ALIYUN::ROS::Sleep
    Properties:
      Triggers: {Value: {Ref: {Fn::FindInMap: [M, Top, Key]}}}
"""
    )
    assert not any(item.code == "ROS4001" for item in report.diagnostics)


def test_find_in_map_reports_known_prefix_failure_before_dynamic_tail() -> None:
    report = validate(
        """ROSTemplateFormatVersion: 2015-09-01
Parameters:
  Tail: {Type: String}
Mappings:
  M: {Top: {Key: value}}
Resources:
  Wait:
    Type: ALIYUN::ROS::Sleep
    Properties:
      Triggers: {Value: {Fn::FindInMap: [M, [], {Ref: Tail}]}}
"""
    )
    assert sum(item.code == "ROS4101" for item in report.diagnostics) == 1


def test_resolved_null_and_jq_runtime_guard_order() -> None:
    report = validate(
        """ROSTemplateFormatVersion: 2015-09-01
Conditions: {Always: true}
Resources:
  Wait:
    Type: ALIYUN::ROS::Sleep
    Properties:
      Triggers:
        Add: {Fn::Add: {Fn::If: [Always, null, [1, 2]]}}
        Indent: {Fn::Indent: {Fn::If: [Always, null, [text, 1]]}}
        Min: {Fn::Min: {Fn::If: [Always, null, [1, 2]]}}
        Max: {Fn::Max: {Fn::If: [Always, null, [1, 2]]}}
        JqBadJsonBeforeNull: {Fn::Jq: [First, null, not-json]}
        JqEmptyBeforeScriptType: {Fn::Jq: [First, 42, ""]}
"""
    )
    errors = [item for item in report.diagnostics if item.severity == Severity.ERROR]
    assert len(errors) == 1
    assert errors[0].code == "ROS3003" and "Fn::Jq" in errors[0].summary


def test_base64_decode_accepts_runtime_trailing_lf_and_encode_round_trip() -> None:
    report = validate(
        """ROSTemplateFormatVersion: 2015-09-01
Resources:
  Wait:
    Type: ALIYUN::ROS::Sleep
    Properties:
      Triggers:
        Literal: {Fn::Base64Decode: "YQ==\\n"}
        RoundTrip: {Fn::Base64Decode: {Fn::Base64Encode: a}}
"""
    )
    assert report.error_count == 0


def test_known_dynamic_consumer_types_and_iterable_outer_contracts() -> None:
    report = validate(
        """ROSTemplateFormatVersion: 2015-09-01
Resources:
  Wait:
    Type: ALIYUN::ROS::Sleep
    Properties:
      Triggers:
        FindMap: {Fn::FindInMap: [{Fn::GetAZs: ""}, Top, Key]}
        Indent: {Fn::Indent: [text, {Fn::GetAZs: ""}]}
        Avg: {Fn::Avg: [{Fn::GetAZs: ""}, [1, 2]]}
        Calculate: {Fn::Calculate: ["{0}", {Fn::GetAZs: ""}, [1]]}
        Cidr: {Fn::Cidr: [10.0.0.0/24, {Fn::GetAZs: ""}, 8]}
        MergeMap: {Fn::MergeMapToList: [{a: {Fn::Base64Encode: value}}]}
"""
    )
    summaries = "\n".join(item.summary for item in report.diagnostics if item.code == "ROS3002")
    for name in ("Fn::FindInMap", "Fn::Indent", "Fn::Avg", "Fn::Calculate", "Fn::Cidr", "Fn::MergeMapToList"):
        assert name in summaries


def test_replace_keeps_occurrence_context_and_unreachable_branches_keep_constructor_checks() -> None:
    report = validate(
        """ROSTemplateFormatVersion: 2015-09-01
Conditions:
  Always: true
  Nested: {Fn::Equals: [{Fn::Replace: [{x: {Fn::Equals: [a, a]}}, x]}, true]}
Resources:
  Wait:
    Type: ALIYUN::ROS::Sleep
    Properties:
      Triggers:
        Select: {Fn::If: [Always, ok, {Fn::Select: [0, [], fallback, {Ref: P}]}]}
        Replace: {Fn::If: [Always, ok, {Fn::Replace: [1, text]}]}
        Sub: {Fn::If: [Always, ok, {Fn::Sub: [1, {}]}]}
        Merge: {Fn::If: [Always, ok, {Fn::MergeMapToList: invalid}]}
"""
    )
    assert not any(item.code == "ROS2002" and "Fn::Equals" in item.summary for item in report.diagnostics)
    assert sum(item.code in {"ROS3001", "ROS3002"} for item in report.diagnostics) == 4


def test_binary_normalize_condition_objects_and_function_listmerge_null() -> None:
    report = validate(
        """ROSTemplateFormatVersion: 2015-09-01
Conditions:
  A: true
  N: {Fn::Not: {Condition: A}}
  Both: {Fn::And: [{Condition: A}, {Condition: N}]}
  Either: {Fn::Or: [{Condition: A}, {Condition: N}]}
Resources:
  Wait:
    Type: ALIYUN::ROS::Sleep
    Properties:
      Triggers:
        Base64: {Fn::Base64: !!binary YQ==}
        Encode: {Fn::Base64Encode: !!binary YQ==}
        Decode: {Fn::Base64Decode: !!binary YQ==}
        ListMerge: {Fn::ListMerge: {Fn::If: [A, null, []]}}
"""
    )
    assert not any(item.code in {"ROS2002", "ROS4003"} for item in report.diagnostics)
    assert sum(item.code == "ROS3002" for item in report.diagnostics) == 4


def test_boolean_constant_folds_preserve_if_reachability_and_split_guard() -> None:
    report = validate(
        """ROSTemplateFormatVersion: 2015-09-01
Conditions:
  Match: {Fn::Equals: [{Fn::MatchPattern: ["a+", aa]}, true]}
  Members: {Fn::Equals: [{Fn::EachMemberIn: [[a], [a, b]]}, true]}
Resources:
  Wait:
    Type: ALIYUN::ROS::Sleep
    Properties:
      Triggers:
        Match: {Fn::If: [Match, ok, {Ref: MissingMatch}]}
        Members: {Fn::If: [Members, ok, {Ref: MissingMembers}]}
        Split: {Fn::Split: ["", {Ref: ALIYUN::Region}]}
"""
    )
    assert not any(item.code == "ROS4001" for item in report.diagnostics)
    assert sum(item.code == "ROS3003" and "Fn::Split" in item.summary for item in report.diagnostics) == 1


def test_resource_condition_constrains_if_branch_reachability() -> None:
    report = validate(
        """ROSTemplateFormatVersion: 2015-09-01
Parameters:
  Mode: {Type: String}
Conditions:
  Enabled: {Fn::Equals: [{Ref: Mode}, enabled]}
Resources:
  Guarded:
    Type: ALIYUN::ROS::Sleep
    Condition: Enabled
    Properties:
      Triggers:
        Assertion:
          Fn::If:
            - Enabled
            - ok
            - Fn::Select: [missing, {}, null, should-not-run]
  Reachable:
    Type: ALIYUN::ROS::Sleep
    Condition: Enabled
    Properties:
      Triggers:
        Assertion:
          Fn::If:
            - Enabled
            - Fn::Select: [missing, {}, null, should-run]
            - ok
"""
    )
    errors = [item for item in report.diagnostics if item.code == "ROS3003" and "Fn::Select" in item.summary]
    assert len(errors) == 1
    assert errors[0].source_span is not None and errors[0].source_span.line == 25


def test_false_resource_condition_suppresses_runtime_select_failure() -> None:
    report = validate(
        """ROSTemplateFormatVersion: 2015-09-01
Parameters:
  Mode: {Type: String, Default: disabled}
Conditions:
  Enabled: {Fn::Equals: [{Ref: Mode}, enabled]}
Resources:
  Guarded:
    Type: ALIYUN::ROS::Sleep
    Condition: Enabled
    Properties:
      Triggers:
        Assertion: {Fn::Select: [missing, {}, null, should-not-run]}
"""
    )
    assert not any(item.code == "ROS3003" for item in report.diagnostics)


def test_getatt_catalog_does_not_reject_unknown_core_extension_count_or_local_attributes() -> None:
    report = validate(
        """ROSTemplateFormatVersion: 2015-09-01
Locals:
  Zones: {Type: DATASOURCE::ECS::Zones, Properties: {}}
  EvalBad: {Type: Eval, Value: {Fn::GetAtt: [Zones, DefinitelyMissingAttribute]}}
Resources:
  Domain: {Type: ALIYUN::APIG::Domain}
  Instance: {Type: ALIYUN::ECS::Instance}
  Gateways: {Type: ALIYUN::APIG::Gateway, Count: 1}
  Wait:
    Type: ALIYUN::ROS::Sleep
    Properties:
      Triggers:
        Extension: {Fn::GetAtt: [Domain, DefinitelyMissingAttribute]}
        Core: {Fn::GetAtt: [Instance, DefinitelyMissingAttribute]}
        Expanded: {Fn::GetAtt: ["Gateways[0]", DefinitelyMissingAttribute]}
"""
    )
    assert not any(item.code == "ROS4005" for item in report.diagnostics)
    assert not any(item.code == "ROS5210" for item in report.diagnostics)


def test_getatt_catalog_does_not_reject_unknown_old_core_or_empty_schema_attributes() -> None:
    report = validate(
        """ROSTemplateFormatVersion: 2015-09-01
Resources:
  Package: {Type: ALIYUN::CDT::ResourcePackage}
  Monitor: {Type: ALIYUN::OSS::BucketAccessMonitor}
  Wait:
    Type: ALIYUN::ROS::Sleep
    Properties:
      Triggers:
        Core: {Fn::GetAtt: [Package, DefinitelyMissingAttribute]}
        Empty: {Fn::GetAtt: [Monitor, DefinitelyMissingAttribute]}
"""
    )
    assert not any(item.code == "ROS4005" for item in report.diagnostics)


def test_diagnostic_sanitizer_redacts_access_keys_and_high_entropy_values() -> None:
    secret = "Q7vN2xLm9P4cR8tY1wK6sD3fG0hJ5bV2nM9qZ4uX7eC1aL8pT6rW3yF0iS5dH2kB"
    report = validate(
        """ROSTemplateFormatVersion: 2015-09-01
Resources:
  Wait:
    Type: ALIYUN::ROS::Sleep
    Properties: {Triggers: {Value: {Ref: %s}}}
"""
        % secret
    )
    rendered = json.dumps(report.to_dict(), ensure_ascii=False)
    assert secret not in rendered
    assert "<redacted:sha256-" in rendered
    error = next(item for item in report.diagnostics if item.code == "ROS4001")
    assert error.summary.startswith("Ref references nonexistent symbol ")
    assert error.summary.endswith(">")
    assert "<redacted:sha256-" in error.summary


def test_many_short_tags_do_not_consume_yaml_alias_budget() -> None:
    body = "ROSTemplateFormatVersion: 2015-09-01\nValues:\n" + "".join(
        '  V{}: !Join [",", [a]]\n'.format(index) for index in range(2600)
    )
    result = parse_template_source(body)
    assert result.template is not None
    assert not any(item.code == "ROS9001" for item in result.diagnostics)


def test_select_null_lookup_short_circuits_collection_consumption() -> None:
    report = validate(
        """ROSTemplateFormatVersion: 2015-09-01
Resources:
  Wait:
    Type: ALIYUN::ROS::Sleep
    Properties:
      Triggers:
        Value: {Fn::Select: [null, not-json, {Ref: MissingDefault}]}
"""
    )
    assert sum(item.code == "ROS4001" for item in report.diagnostics) == 1
    assert not any(item.code == "ROS5002" for item in report.diagnostics)


def test_index_does_not_fold_with_dynamic_lookup() -> None:
    report = validate(
        """ROSTemplateFormatVersion: 2015-09-01
Parameters:
  P: {Type: String}
Conditions:
  IndexIsMissing: {Fn::Equals: [{Fn::Index: [{Ref: P}, [a]]}, null]}
Resources:
  Wait:
    Type: ALIYUN::ROS::Sleep
    Properties:
      Triggers:
        Value: {Fn::If: [IndexIsMissing, ok, {Ref: MissingWhenPIsA}]}
"""
    )
    assert sum(item.code == "ROS4001" for item in report.diagnostics) == 1


def test_if_accepts_condition_object_and_keeps_only_reachable_branch() -> None:
    report = validate(
        """ROSTemplateFormatVersion: 2015-09-01
Conditions:
  A: true
Resources:
  Wait:
    Type: ALIYUN::ROS::Sleep
    Properties:
      Triggers:
        Value: {Fn::If: [{Condition: A}, ok, {Ref: UnreachableMissing}]}
"""
    )
    assert not any(item.code in {"ROS4001", "ROS4003"} for item in report.diagnostics)


def test_if_normalizes_direct_list_and_map_conditions_like_ros_runtime() -> None:
    report = validate(
        """ROSTemplateFormatVersion: 2015-09-01
Conditions:
  A: true
Resources:
  Wait:
    Type: ALIYUN::ROS::Sleep
    Properties:
      Triggers:
        DirectTrue: {Fn::If: [true, ok, {Ref: MissingDirectTrue}]}
        DirectFalse: {Fn::If: [false, {Ref: MissingDirectFalse}, ok]}
        ListBooleanKey: {Fn::If: [[false], ok, {Ref: MissingListBooleanKey}]}
        ListMapKey: {Fn::If: [[{Condition: A}], ok, {Ref: MissingListMapKey}]}
        MapBooleanKey: {Fn::If: [{Condition: false}, ok, {Ref: MissingMapBooleanKey}]}
"""
    )
    assert not any(item.code in {"ROS4001", "ROS4003"} for item in report.diagnostics)


def test_if_rejects_empty_none_and_non_string_condition_keys_like_ros_runtime() -> None:
    report = validate(
        """ROSTemplateFormatVersion: 2015-09-01
Resources:
  Wait:
    Type: ALIYUN::ROS::Sleep
    Properties:
      Triggers:
        EmptyList: {Fn::If: [[], ok, fallback]}
        DirectNone: {Fn::If: [null, ok, fallback]}
        DirectNumber: {Fn::If: [1, ok, fallback]}
        ListNone: {Fn::If: [[null], ok, fallback]}
        ListNumber: {Fn::If: [[1], ok, fallback]}
        MapNone: {Fn::If: [{Condition: null}, ok, fallback]}
        MapNumber: {Fn::If: [{Condition: 1}, ok, fallback]}
"""
    )
    errors = [item for item in report.diagnostics if item.code == "ROS3003" and "Fn::If" in item.summary]
    assert len(errors) == 7


def test_unreachable_if_branch_checks_every_immediate_constructor_shape() -> None:
    report = validate(
        """ROSTemplateFormatVersion: 2015-09-01
Conditions:
  A: true
Resources:
  Wait:
    Type: ALIYUN::ROS::Sleep
    Properties:
      Triggers:
        GetStackOutput: {Fn::If: [A, ok, {Fn::GetStackOutput: [only]}]}
        FormatTime: {Fn::If: [A, ok, {Fn::FormatTime: []}]}
        ListMerge: {Fn::If: [A, ok, {Fn::ListMerge: invalid}]}
        MarketplaceImage: {Fn::If: [A, ok, {Fn::MarketplaceImage: ""}]}
        ResourceFacade: {Fn::If: [A, ok, {Fn::ResourceFacade: Bogus}]}
        Contains: {Fn::If: [A, ok, {Fn::Contains: [one]}]}
        EachMemberIn: {Fn::If: [A, ok, {Fn::EachMemberIn: [one]}]}
"""
    )
    summaries = "\n".join(item.summary for item in report.diagnostics if item.severity == Severity.ERROR)
    for name in (
        "Fn::GetStackOutput",
        "Fn::FormatTime",
        "Fn::ListMerge",
        "Fn::MarketplaceImage",
        "Fn::ResourceFacade",
        "Fn::Contains",
        "Fn::EachMemberIn",
    ):
        assert name in summaries


def test_function_returned_list_is_checked_by_all_whole_argument_consumers() -> None:
    report = validate(
        """ROSTemplateFormatVersion: 2015-09-01
Resources:
  Wait:
    Type: ALIYUN::ROS::Sleep
    Properties:
      Triggers:
        Indent: {Fn::Indent: {Fn::Split: [",", {Ref: ALIYUN::Region}]}}
        ListMerge: {Fn::ListMerge: {Fn::Split: [",", {Ref: ALIYUN::Region}]}}
        Add: {Fn::Add: {Fn::Split: [",", {Ref: ALIYUN::Region}]}}
        Min: {Fn::Min: {Fn::Split: [",", {Ref: ALIYUN::Region}]}}
        Max: {Fn::Max: {Fn::Split: [",", {Ref: ALIYUN::Region}]}}
"""
    )
    summaries = "\n".join(item.summary for item in report.diagnostics if item.severity == Severity.ERROR)
    for name in ("Fn::Indent", "Fn::ListMerge", "Fn::Add", "Fn::Min", "Fn::Max"):
        assert name in summaries


def test_select_rejects_known_dynamic_list_lookup_for_list_collection() -> None:
    report = validate(
        """ROSTemplateFormatVersion: 2015-09-01
Resources:
  Wait:
    Type: ALIYUN::ROS::Sleep
    Properties:
      Triggers:
        Value: {Fn::Select: [{Fn::Split: [",", {Ref: ALIYUN::Region}]}, [a]]}
"""
    )
    assert any(item.code == "ROS3002" and "Fn::Select" in item.summary for item in report.diagnostics)


def test_select_map_list_checks_dynamic_key_and_member_types() -> None:
    report = validate(
        """ROSTemplateFormatVersion: 2015-09-01
Resources:
  Wait:
    Type: ALIYUN::ROS::Sleep
    Properties:
      Triggers:
        Key: {Fn::SelectMapList: [{Fn::Split: [",", {Ref: ALIYUN::Region}]}, [{x: 1}]]}
        Members: {Fn::SelectMapList: [x, {Fn::Split: [",", {Ref: ALIYUN::Region}]}]}
"""
    )
    assert sum(item.code == "ROS3002" and "Fn::SelectMapList" in item.summary for item in report.diagnostics) == 2


def test_collection_consumers_check_known_dynamic_member_types() -> None:
    report = validate(
        """ROSTemplateFormatVersion: 2015-09-01
Resources:
  Wait:
    Type: ALIYUN::ROS::Sleep
    Properties:
      Triggers:
        MemberList: {Fn::MemberListToMap: [Name, Value, {Fn::MergeMapToList: [{a: [1]}]}]}
        Contains: {Fn::Contains: [{Fn::MergeMapToList: [{a: [1]}]}, x]}
        Each: {Fn::EachMemberIn: [{Fn::MergeMapToList: [{a: [1]}]}, []]}
"""
    )
    summaries = "\n".join(item.summary for item in report.diagnostics if item.severity == Severity.ERROR)
    for name in ("Fn::MemberListToMap", "Fn::Contains", "Fn::EachMemberIn"):
        assert name in summaries


def test_sub_checks_raw_variable_keys_and_runtime_placeholder_pattern() -> None:
    report = validate(
        """ROSTemplateFormatVersion: 2015-09-01
Resources:
  Wait:
    Type: ALIYUN::ROS::Sleep
    Properties:
      Triggers:
        Key: {Fn::Sub: [plain, {1: value}]}
        Numeric: {Fn::Sub: ["${1}", {"1": value}]}
        Punctuation: {Fn::Sub: ["${---}", {"---": value}]}
"""
    )
    assert sum(item.severity == Severity.ERROR and "Fn::Sub" in item.summary for item in report.diagnostics) == 3


def test_sub_accepts_parameter_ref_with_function_name_expression() -> None:
    report = validate(
        """ROSTemplateFormatVersion: 2015-09-01
Parameters:
  P: {Type: String, Default: hello}
Mappings:
  M: {A: {B: P}}
Resources:
  Wait:
    Type: ALIYUN::ROS::Sleep
    Properties:
      Triggers:
        Value: {Fn::Sub: [{Ref: {Fn::FindInMap: [M, A, B]}}, {}]}
"""
    )
    assert report.error_count == 0


def test_format_time_uses_locked_dateutil_timezone_rules() -> None:
    report = validate(
        """ROSTemplateFormatVersion: 2015-09-01
Resources:
  Wait:
    Type: ALIYUN::ROS::Sleep
    Properties:
      Triggers:
        Value: {Fn::FormatTime: ["%Y", "GMT+8"]}
"""
    )
    assert report.error_count == 0


def test_not_preserves_runtime_single_item_list_semantics() -> None:
    report = validate(
        """ROSTemplateFormatVersion: 2015-09-01
Conditions:
  N: {Fn::Not: [false]}
Resources:
  Wait:
    Type: ALIYUN::ROS::Sleep
    Properties:
      Triggers:
        Value: {Fn::If: [N, {Ref: UnreachableMissing}, ok]}
"""
    )
    assert not any(item.code == "ROS4001" for item in report.diagnostics)


def test_match_pattern_preserves_runtime_first_match_branch_reachability() -> None:
    report = validate(
        """ROSTemplateFormatVersion: 2015-09-01
Conditions:
  RuntimeFalse: {Fn::Equals: [{Fn::MatchPattern: ["a|ab", ab]}, true]}
Resources:
  Wait:
    Type: ALIYUN::ROS::Sleep
    Properties:
      Triggers:
        Value: {Fn::If: [RuntimeFalse, {Ref: UnreachableMissing}, ok]}
"""
    )

    assert not any(item.code == "ROS4001" for item in report.diagnostics)


def test_replace_does_not_fold_order_dependent_multi_key_mapping() -> None:
    report = validate(
        """ROSTemplateFormatVersion: 2015-09-01
Conditions:
  OrderDependent: {Fn::Equals: [{Fn::Replace: [{a: b, b: c}, a]}, c]}
Resources:
  Wait:
    Type: ALIYUN::ROS::Sleep
    Properties:
      Triggers:
        Value: {Fn::If: [OrderDependent, ok, {Ref: MustRemainReachable}]}
"""
    )
    assert sum(item.code == "ROS4001" for item in report.diagnostics) == 1


def test_select_rejects_invalid_literal_indexes_instead_of_using_default() -> None:
    report = validate(
        """ROSTemplateFormatVersion: 2015-09-01
Resources:
  Wait:
    Type: ALIYUN::ROS::Sleep
    Properties:
      Triggers:
        Text: {Fn::Select: [abc, [a], fallback]}
        Infinity: {Fn::Select: [.inf, [a], fallback]}
        Nan: {Fn::Select: [.nan, [a], fallback]}
        ZeroStep: {Fn::Select: ["::0", [a], fallback]}
"""
    )
    assert sum(item.severity == Severity.ERROR and "Fn::Select" in item.summary for item in report.diagnostics) == 4


def test_get_json_value_rejects_known_dynamic_list_source() -> None:
    report = validate(
        """ROSTemplateFormatVersion: 2015-09-01
Resources:
  Wait:
    Type: ALIYUN::ROS::Sleep
    Properties:
      Triggers:
        Value: {Fn::GetJsonValue: [x, {Fn::GetAZs: ""}]}
"""
    )
    assert any(item.code == "ROS3002" and "Fn::GetJsonValue" in item.summary for item in report.diagnostics)


def test_split_with_nullable_content_is_not_assumed_non_empty() -> None:
    report = validate(
        """ROSTemplateFormatVersion: 2015-09-01
Parameters:
  P: {Type: String}
Conditions:
  C: {Fn::Equals: [{Ref: P}, x]}
Resources:
  Wait:
    Type: ALIYUN::ROS::Sleep
    Properties:
      Triggers:
        Min: {Fn::Min: {Fn::Split: [",", {Fn::If: [C, null, "1"]}]}}
        Merge: {Fn::ListMerge: {Fn::Split: [",", {Fn::If: [C, null, x]}]}}
        SelectMap: {Fn::SelectMapList: [x, {Fn::Split: [",", {Fn::If: [C, null, x]}]}]}
"""
    )
    assert not any(item.severity == Severity.ERROR for item in report.diagnostics)


def test_list_merge_function_input_preserves_nullable_return_type() -> None:
    report = validate(
        """ROSTemplateFormatVersion: 2015-09-01
Parameters:
  P: {Type: String}
Conditions:
  C: {Fn::Equals: [{Ref: P}, x]}
Resources:
  Wait:
    Type: ALIYUN::ROS::Sleep
    Properties:
      Triggers:
        Value: {Fn::Base64Encode: {Fn::ListMerge: {Fn::If: [C, [], [[a]]]}}}
"""
    )
    assert not any(item.code == "ROS3002" and "Fn::Base64Encode" in item.summary for item in report.diagnostics)


def test_not_rejects_null_inside_runtime_single_item_list() -> None:
    report = validate(
        """ROSTemplateFormatVersion: 2015-09-01
Conditions:
  N: {Fn::Not: [null]}
Resources:
  Wait:
    Type: ALIYUN::ROS::Sleep
"""
    )
    assert any(item.severity == Severity.ERROR and "Fn::Not" in item.summary for item in report.diagnostics)


def test_sub_rejects_empty_placeholder() -> None:
    report = validate(
        """ROSTemplateFormatVersion: 2015-09-01
Resources:
  Wait:
    Type: ALIYUN::ROS::Sleep
    Properties:
      Triggers:
        Value: {Fn::Sub: ["${}", {}]}
"""
    )
    assert any(item.severity == Severity.ERROR and "Fn::Sub" in item.summary for item in report.diagnostics)


def test_reports_undocumented_resource_type() -> None:
    report = validate(
        """ROSTemplateFormatVersion: 2015-09-01
Resources:
  Thing:
    Type: ALIYUN::NOSUCH::Thing
"""
    )
    codes = [item.code for item in report.diagnostics if item.severity == Severity.ERROR]
    assert "ROS5103" in codes


def test_undocumented_resource_type_suggests_closest_documented_type() -> None:
    report = validate(
        """ROSTemplateFormatVersion: 2015-09-01
Resources:
  Bucket:
    Type: ALIYUN::OSS::Buckett
"""
    )
    item = next(item for item in report.diagnostics if item.code == "ROS5103")
    assert item.expected == "ALIYUN::OSS::Bucket"


def test_accepts_documented_resource_type() -> None:
    report = validate(
        """ROSTemplateFormatVersion: 2015-09-01
Resources:
  Bucket:
    Type: ALIYUN::OSS::Bucket
"""
    )
    assert not [item for item in report.diagnostics if item.code == "ROS5103"]


def test_skips_undocumented_check_for_module_and_datasource_types() -> None:
    report = validate(
        """ROSTemplateFormatVersion: 2015-09-01
Resources:
  Mod:
    Type: MODULE::MyOrg::MyModule
  Zones:
    Type: DATASOURCE::ECS::Zones
"""
    )
    assert not [item for item in report.diagnostics if item.code == "ROS5103"]


def test_skips_undocumented_check_for_terraform_templates() -> None:
    report = validate(
        """ROSTemplateFormatVersion: 2015-09-01
Transform: Aliyun::Terraform-v1.5
Resources:
  Thing:
    Type: ALIYUN::NOSUCH::Thing
"""
    )
    assert not [item for item in report.diagnostics if item.code == "ROS5103"]


def test_reports_undocumented_getatt_attribute() -> None:
    report = validate(
        """ROSTemplateFormatVersion: 2015-09-01
Resources:
  Vpc:
    Type: ALIYUN::ECS::VPC
    Properties:
      CidrBlock: 10.0.0.0/8
Outputs:
  Bad:
    Value:
      Fn::GetAtt: [Vpc, NoSuchAttribute]
"""
    )
    item = next(item for item in report.diagnostics if item.code == "ROS4207")
    assert item.severity == Severity.ERROR
    assert item.actual == "NoSuchAttribute"


def test_accepts_documented_getatt_attribute() -> None:
    report = validate(
        """ROSTemplateFormatVersion: 2015-09-01
Resources:
  Vpc:
    Type: ALIYUN::ECS::VPC
    Properties:
      CidrBlock: 10.0.0.0/8
Outputs:
  Good:
    Value:
      Fn::GetAtt: [Vpc, VpcId]
"""
    )
    assert not [item for item in report.diagnostics if item.code == "ROS4207"]


def test_skips_getatt_attribute_check_when_catalog_documents_no_attributes() -> None:
    report = validate(
        """ROSTemplateFormatVersion: 2015-09-01
Resources:
  Command:
    Type: ALIYUN::ECS::RunCommand
Outputs:
  Results:
    Value:
      Fn::GetAtt: [Command, InvokeResults]
"""
    )
    assert not [item for item in report.diagnostics if item.code == "ROS4207"]
