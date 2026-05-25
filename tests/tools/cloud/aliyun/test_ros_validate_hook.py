"""Tests for ROS template validation hook."""

from __future__ import annotations

from iac_code.tools.cloud.aliyun.hooks.ros_validate import (
    _format_json_error,
    _format_yaml_error,
    _parse_template,
    _validate_structure,
    check_template,
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
        assert data is None
        assert err is not None
        assert "JSON" in err

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
        assert any("Resources" in e for e in errors)

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


class TestCheckTemplate:
    def test_no_template_body_returns_none(self) -> None:
        result = check_template("ros", "ValidateTemplate", {})
        assert result is None

    def test_valid_template_returns_none(self) -> None:
        body = '{"ROSTemplateFormatVersion": "2015-09-01", "Resources": {"V": {"Type": "ALIYUN::ECS::VPC"}}}'
        result = check_template("ros", "ValidateTemplate", {"TemplateBody": body})
        assert result is None

    def test_syntax_error_returns_error(self) -> None:
        result = check_template("ros", "ValidateTemplate", {"TemplateBody": "{bad json"})
        assert result is not None
        assert result.is_error

    def test_structure_error_returns_error(self) -> None:
        body = '{"Resources": "not_a_dict"}'
        result = check_template("ros", "ValidateTemplate", {"TemplateBody": body})
        assert result is not None
        assert result.is_error
