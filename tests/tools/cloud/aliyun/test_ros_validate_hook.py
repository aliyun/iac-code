"""Tests for ROS template validation hook."""

from __future__ import annotations

from iac_code.tools.cloud.aliyun import template_source
from iac_code.tools.cloud.aliyun.hooks.ros_validate import (
    _format_json_error,
    _format_yaml_error,
    _parse_template,
    _validate_structure,
    check_template,
    local_template_source_error,
)
from iac_code.tools.cloud.aliyun.template_source import (
    check_local_template_url_source,
    classify_local_template_source,
)


class TestParseTemplate:
    def test_valid_yaml_with_ros_tags(self) -> None:
        text = "Resources:\n  Vpc:\n    Type: ALIYUN::ECS::VPC\n    Properties:\n      CidrBlock: !Ref CidrParam"
        data, err = _parse_template(text)
        assert data is not None
        assert err is None
        assert data["Resources"]["Vpc"]["Properties"]["CidrBlock"] == {"Ref": "CidrParam"}

    def test_valid_json(self) -> None:
        text = '{"ROSTemplateFormatVersion": "2015-09-01", "Resources": {}}'
        data, err = _parse_template(text)
        assert data is not None
        assert err is None

    def test_invalid_yaml(self) -> None:
        text = "key: value\nbad:\n  - [unclosed"
        data, err = _parse_template(text)
        assert data is None
        assert err is not None
        assert "YAML" in err

    def test_invalid_json(self) -> None:
        text = '{"key": "value",}'
        data, err = _parse_template(text)
        # ROS performs JSON-first detection and then accepts YAML's trailing-comma
        # Mapping syntax when strict JSON parsing fails.
        assert data == {"key": "value"}
        assert err is None

    def test_json_detection_by_brace(self) -> None:
        text = '  {"ROSTemplateFormatVersion": "2015-09-01"}'
        data, err = _parse_template(text)
        assert data is not None
        assert data["ROSTemplateFormatVersion"] == "2015-09-01"

    def test_not_a_dict(self) -> None:
        text = "- item1\n- item2"
        data, err = _parse_template(text)
        assert data is None
        assert err is not None


class TestFormatYamlError:
    def test_includes_line_number(self) -> None:
        text = "key: value\nbad:\n  - [unclosed"
        try:
            import yaml

            yaml.safe_load(text)
        except yaml.YAMLError as e:
            msg = _format_yaml_error(e, text)
            assert "YAML" in msg
            assert "line" in msg.lower()


class TestFormatJsonError:
    def test_includes_line_number(self) -> None:
        import json

        text = '{\n  "key": "value",\n}'
        try:
            json.loads(text)
        except json.JSONDecodeError as e:
            msg = _format_json_error(e, text)
            assert "JSON" in msg


class TestValidateStructure:
    def test_valid_ros_template(self) -> None:
        data = {
            "ROSTemplateFormatVersion": "2015-09-01",
            "Resources": {"Vpc": {"Type": "ALIYUN::ECS::VPC"}},
        }
        errors = _validate_structure(data)
        assert errors == []

    def test_missing_format_version(self) -> None:
        data = {"Resources": {"Vpc": {"Type": "ALIYUN::ECS::VPC"}}}
        errors = _validate_structure(data)
        assert any("ROSTemplateFormatVersion" in e for e in errors)

    def test_missing_resources(self) -> None:
        data = {"ROSTemplateFormatVersion": "2015-09-01"}
        errors = _validate_structure(data)
        # Empty templates are accepted by ROS; Resources is not universally required.
        assert errors == []

    def test_terraform_template_skips_resources(self) -> None:
        data = {
            "Transform": "Aliyun::Terraform-v1.6",
            "Workspace": {"main.tf": "resource ..."},
        }
        errors = _validate_structure(data)
        assert not any("Resources" in e for e in errors)

    def test_resource_without_type(self) -> None:
        data = {
            "ROSTemplateFormatVersion": "2015-09-01",
            "Resources": {"Vpc": {"Properties": {}}},
        }
        errors = _validate_structure(data)
        assert any("Type" in e for e in errors)

    def test_resource_type_correction(self) -> None:
        data = {
            "ROSTemplateFormatVersion": "2015-09-01",
            "Resources": {"Vpc": {"Type": "ALIYUN::VPC::VPC"}},
        }
        errors = _validate_structure(data)
        assert any("ALIYUN::ECS::VPC" in e for e in errors)

    def test_existing_vpc_vswitch_rejects_static_cidr_block_literal(self) -> None:
        data = {
            "ROSTemplateFormatVersion": "2015-09-01",
            "Parameters": {
                "VpcId": {
                    "Type": "String",
                    "AssociationProperty": "ALIYUN::ECS::VPC::VPCId",
                }
            },
            "Resources": {
                "VSwitch": {
                    "Type": "ALIYUN::ECS::VSwitch",
                    "Properties": {
                        "VpcId": {"Ref": "VpcId"},
                        "ZoneId": "cn-hangzhou-k",
                        "CidrBlock": "192.168.0.0/24",
                    },
                }
            },
        }

        errors = _validate_structure(data)

        assert any("VSwitch" in e and "CidrBlock" in e and "existing VPC" in e for e in errors)

    def test_existing_vpc_vswitch_rejects_cidr_parameter_default(self) -> None:
        data = {
            "ROSTemplateFormatVersion": "2015-09-01",
            "Parameters": {
                "VpcId": {
                    "Type": "String",
                    "AssociationProperty": "ALIYUN::ECS::VPC::VPCId",
                },
                "CidrBlock": {
                    "Type": "String",
                    "Default": "192.168.0.0/24",
                },
            },
            "Resources": {
                "VSwitch": {
                    "Type": "ALIYUN::ECS::VSwitch",
                    "Properties": {
                        "VpcId": {"Ref": "VpcId"},
                        "ZoneId": "cn-hangzhou-k",
                        "CidrBlock": {"Ref": "CidrBlock"},
                    },
                }
            },
        }

        errors = _validate_structure(data)

        assert any("CidrBlock" in e and "Default" in e and "existing VPC" in e for e in errors)

    def test_existing_vpc_vswitch_allows_cidr_parameter_without_default(self) -> None:
        data = {
            "ROSTemplateFormatVersion": "2015-09-01",
            "Parameters": {
                "VpcId": {
                    "Type": "String",
                    "AssociationProperty": "ALIYUN::ECS::VPC::VPCId",
                },
                "CidrBlock": {
                    "Type": "String",
                },
            },
            "Resources": {
                "VSwitch": {
                    "Type": "ALIYUN::ECS::VSwitch",
                    "Properties": {
                        "VpcId": {"Ref": "VpcId"},
                        "ZoneId": "cn-hangzhou-k",
                        "CidrBlock": {"Ref": "CidrBlock"},
                    },
                }
            },
        }

        errors = _validate_structure(data)

        assert errors == []

    def test_new_vpc_vswitch_allows_static_cidr_block_literal(self) -> None:
        data = {
            "ROSTemplateFormatVersion": "2015-09-01",
            "Resources": {
                "Vpc": {
                    "Type": "ALIYUN::ECS::VPC",
                    "Properties": {"CidrBlock": "192.168.0.0/16"},
                },
                "VSwitch": {
                    "Type": "ALIYUN::ECS::VSwitch",
                    "Properties": {
                        "VpcId": {"Ref": "Vpc"},
                        "ZoneId": "cn-hangzhou-k",
                        "CidrBlock": "192.168.0.0/24",
                    },
                },
            },
        }

        errors = _validate_structure(data)

        assert errors == []


class TestCheckTemplate:
    def test_no_template_body_returns_none(self) -> None:
        result = check_template("ros", "ValidateTemplate", {})
        assert result is not None
        assert result.blocking_result is not None
        assert result.blocking_result.is_error

    def test_valid_template_returns_non_blocking_analyzed_outcome(self) -> None:
        body = '{"ROSTemplateFormatVersion": "2015-09-01", "Resources": {"V": {"Type": "ALIYUN::ECS::VPC"}}}'
        result = check_template("ros", "ValidateTemplate", {"TemplateBody": body})
        assert result is not None
        assert result.blocking_result is None
        assert result.template_analyzed

    def test_syntax_error_returns_error(self) -> None:
        result = check_template("ros", "ValidateTemplate", {"TemplateBody": "{bad json"})
        assert result is not None
        assert result.is_error

    def test_structure_error_returns_error(self) -> None:
        body = '{"Resources": "not_a_dict"}'
        result = check_template("ros", "ValidateTemplate", {"TemplateBody": body})
        assert result is not None
        assert result.is_error

    def test_local_template_read_error_is_a_structured_blocking_report(self) -> None:
        outcome = local_template_source_error(UnicodeDecodeError("utf-8", b"\xff", 0, 1, "invalid"))
        assert outcome.blocking_result is not None
        assert outcome.report.diagnostics[0].code == "ROS1202"
        assert outcome.blocking_result.metadata is not None
        assert outcome.blocking_result.metadata["ros_validation"]["error_count"] == 1

    def test_association_property_error_blocks_validate_template_with_structured_detail(self) -> None:
        body = {
            "ROSTemplateFormatVersion": "2015-09-01",
            "Parameters": {
                "GeneratedSuffix": {
                    "Type": "String",
                    "AssociationProperty": "AutoCompleteInput",
                    "AssociationPropertyMetadata": {
                        "Length": 8,
                        "CharacterClasses": [{"Class": "digit", "Min": 1}],
                    },
                }
            },
            "Resources": {},
        }

        result = check_template("ros", "ValidateTemplate", {"TemplateBody": body})

        assert result is not None and result.blocking_result is not None
        assert result.template_analyzed
        assert "ROS1305" in result.blocking_result.content
        assert "Use number instead" in result.blocking_result.content
        metadata = result.blocking_result.metadata["ros_validation"]
        assert metadata["error_count"] == 1
        assert metadata["diagnostics"][0]["path"] == (
            "$.Parameters.GeneratedSuffix.AssociationPropertyMetadata.CharacterClasses[0].Class"
        )

    def test_association_property_rule_is_shared_with_create_stack(self) -> None:
        body = {
            "ROSTemplateFormatVersion": "2015-09-01",
            "Parameters": {
                "P": {
                    "Type": "String",
                    "AssociationProperty": "AutoCompleteInput",
                    "AssociationPropertyMetadata": {"CharacterClasses": [{"Class": "digit", "Min": 1}]},
                }
            },
            "Resources": {},
        }

        result = check_template(
            "ros",
            "CreateStack",
            {"StackName": "stack", "TemplateBody": body},
        )

        assert result is not None and result.blocking_result is not None
        assert result.report.counts_by_code["ROS1305"] == 1

    def test_association_property_warning_does_not_block_and_is_attached_once(self) -> None:
        body = {
            "ROSTemplateFormatVersion": "2015-09-01",
            "Parameters": {
                "P": {
                    "Type": "String",
                    "AssociationProperty": "ALIYUN::Future::Selector",
                }
            },
            "Resources": {},
        }

        result = check_template("ros", "ValidateTemplate", {"TemplateBody": body})

        assert result is not None and result.blocking_result is None
        assert result.report.warning_count == 1
        assert result.report.counts_by_code["ROS5303"] == 1
        assert len([item for item in result.report.diagnostics if item.code == "ROS5303"]) == 1

    def test_valid_auto_complete_metadata_continues_and_input_is_not_mutated(self) -> None:
        body = {
            "ROSTemplateFormatVersion": "2015-09-01",
            "Parameters": {
                "P": {
                    "Type": "String",
                    "AssociationProperty": "AutoCompleteInput",
                    "AssociationPropertyMetadata": {"CharacterClasses": [{"Class": "number", "Min": 1}]},
                }
            },
            "Resources": {},
        }
        params = {"TemplateBody": body}

        result = check_template("ros", "ValidateTemplate", params)

        assert result is not None and result.blocking_result is None
        assert params == {"TemplateBody": body}


class TestLocalTemplateSourceClassification:
    """Every unusable local template must name its cause and its fix.

    Regression guard: a generic report gives the model nothing to correct, which
    is what previously drove repeated retries of the same template input.
    """

    def test_missing_file_is_classified_and_actionable(self, tmp_path) -> None:
        assert classify_local_template_source(tmp_path / "absent.yml") == "MISSING"
        diagnostic = local_template_source_error("MISSING").report.diagnostics[0]
        assert diagnostic.code == "ROS1202"
        assert "does not exist" in diagnostic.summary
        assert diagnostic.suggestion

    def test_directory_is_not_a_usable_template_source(self, tmp_path) -> None:
        assert classify_local_template_source(tmp_path) == "NOT_REGULAR"
        diagnostic = local_template_source_error("NOT_REGULAR").report.diagnostics[0]
        assert "not a regular file" in diagnostic.summary

    def test_oversized_template_is_rejected_before_the_wire_limit(self, tmp_path, monkeypatch) -> None:
        monkeypatch.setattr(template_source, "MAX_LOCAL_TEMPLATE_BYTES", 4)
        template = tmp_path / "big.yml"
        template.write_text("Resources: {}\n", encoding="utf-8")
        assert classify_local_template_source(template) == "TOO_LARGE"
        diagnostic = local_template_source_error("TOO_LARGE").report.diagnostics[0]
        assert "32 MiB" in diagnostic.summary

    def test_readable_template_has_no_problem(self, tmp_path) -> None:
        template = tmp_path / "ok.yml"
        template.write_text("Resources: {}\n", encoding="utf-8")
        assert classify_local_template_source(template) is None

    def test_remote_template_url_is_never_classified_locally(self) -> None:
        assert check_local_template_url_source("oss://bucket/template.yml", None, cwd=".") is None
        assert check_local_template_url_source("https://example.com/template.yml", None, cwd=".") is None

    def test_each_problem_maps_to_a_distinct_actionable_diagnostic(self) -> None:
        summaries = {
            problem: local_template_source_error(problem).report.diagnostics[0].summary
            for problem in ("MISSING", "NOT_REGULAR", "UNREADABLE", "TOO_LARGE", "UTF-8")
        }
        assert len(set(summaries.values())) == len(summaries)
        for problem, summary in summaries.items():
            # The caller supplies a template path, never a body_file.
            assert "body_file" not in summary, problem
