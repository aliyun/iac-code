"""Tests for the pre-write ROS template validation used by write_file."""

import json

import pytest

from iac_code.tools.base import ToolContext
from iac_code.tools.cloud.aliyun.ros_validation.write_preflight import (
    looks_like_ros_template,
    validate_template_before_write,
)
from iac_code.tools.write_file import WriteFileTool

VALID_TEMPLATE = json.dumps(
    {
        "ROSTemplateFormatVersion": "2015-09-01",
        "Resources": {
            "Vpc": {"Type": "ALIYUN::ECS::VPC", "Properties": {"CidrBlock": "10.0.0.0/8"}},
        },
    }
)

VALID_YAML_TEMPLATE = (
    "ROSTemplateFormatVersion: '2015-09-01'\n"
    "Resources:\n"
    "  Vpc:\n"
    "    Type: ALIYUN::ECS::VPC\n"
    "    Properties:\n"
    "      CidrBlock: 10.0.0.0/8\n"
)

UNDOCUMENTED_TYPE_TEMPLATE = json.dumps(
    {
        "ROSTemplateFormatVersion": "2015-09-01",
        "Resources": {"Thing": {"Type": "ALIYUN::NOSUCH::Thing", "Properties": {}}},
    }
)

UNDOCUMENTED_ATTRIBUTE_TEMPLATE = json.dumps(
    {
        "ROSTemplateFormatVersion": "2015-09-01",
        "Resources": {
            "Vpc": {"Type": "ALIYUN::ECS::VPC", "Properties": {"CidrBlock": "10.0.0.0/8"}},
            "VSwitch": {
                "Type": "ALIYUN::ECS::VSwitch",
                "Properties": {
                    "VpcId": {"Fn::GetAtt": ["Vpc", "NoSuchAttribute"]},
                    "ZoneId": "cn-hangzhou-a",
                    "CidrBlock": "10.0.1.0/24",
                },
            },
        },
    }
)


class TestLooksLikeRosTemplate:
    def test_json_template_is_recognized(self):
        assert looks_like_ros_template("template.json", VALID_TEMPLATE) is True

    def test_yaml_template_is_recognized(self):
        assert looks_like_ros_template("template.yaml", VALID_YAML_TEMPLATE) is True
        assert looks_like_ros_template("template.yml", VALID_YAML_TEMPLATE) is True

    def test_resources_only_template_is_recognized(self):
        content = json.dumps({"Resources": {"Vpc": {"Type": "ALIYUN::ECS::VPC"}}})
        assert looks_like_ros_template("template.json", content) is True

    def test_unrelated_suffix_is_not_a_template(self):
        assert looks_like_ros_template("notes.txt", VALID_TEMPLATE) is False
        assert looks_like_ros_template("main.py", VALID_TEMPLATE) is False

    def test_unrelated_json_is_not_a_template(self):
        assert looks_like_ros_template("config.json", '{"name": "demo"}') is False

    def test_resources_list_is_not_a_template(self):
        assert looks_like_ros_template("config.json", '{"Resources": ["a", "b"]}') is False

    def test_empty_content_is_not_a_template(self):
        assert looks_like_ros_template("template.json", "") is False
        assert looks_like_ros_template("template.json", "   \n") is False

    def test_unparsable_content_without_marker_is_not_a_template(self):
        assert looks_like_ros_template("config.json", "{oops") is False

    def test_unparsable_content_with_marker_is_a_template(self):
        assert looks_like_ros_template("template.json", '{"ROSTemplateFormatVersion": oops') is True


class TestValidateTemplateBeforeWrite:
    def test_non_template_returns_none(self):
        assert validate_template_before_write("main.py", "x = 1\n") is None

    def test_valid_template_is_not_blocking(self):
        outcome = validate_template_before_write("template.json", VALID_TEMPLATE)
        assert outcome is not None
        assert outcome.blocking_result is None
        assert outcome.report.error_count == 0

    def test_undocumented_resource_type_is_blocking(self):
        outcome = validate_template_before_write("template.json", UNDOCUMENTED_TYPE_TEMPLATE)
        assert outcome is not None
        assert outcome.blocking_result is not None
        assert [item.code for item in outcome.report.diagnostics] == ["ROS5103"]

    def test_undocumented_attribute_is_blocking(self):
        outcome = validate_template_before_write("template.json", UNDOCUMENTED_ATTRIBUTE_TEMPLATE)
        assert outcome is not None
        assert outcome.blocking_result is not None
        assert "ROS4207" in [item.code for item in outcome.report.diagnostics]


class TestWriteFileRosPreflight:
    @pytest.fixture
    def tool(self):
        return WriteFileTool()

    @pytest.mark.asyncio
    async def test_undocumented_resource_type_is_not_written(self, tool, tmp_path):
        target = tmp_path / "template.json"
        result = await tool.execute(
            tool_input={"path": str(target), "content": UNDOCUMENTED_TYPE_TEMPLATE},
            context=ToolContext(cwd=str(tmp_path)),
        )

        assert result.is_error is True
        assert target.exists() is False
        assert result.metadata["ros_validation"]["error_count"] == 1
        assert "ROS5103" in result.content

    @pytest.mark.asyncio
    async def test_undocumented_attribute_is_not_written(self, tool, tmp_path):
        target = tmp_path / "template.json"
        result = await tool.execute(
            tool_input={"path": str(target), "content": UNDOCUMENTED_ATTRIBUTE_TEMPLATE},
            context=ToolContext(cwd=str(tmp_path)),
        )

        assert result.is_error is True
        assert target.exists() is False
        assert "ROS4207" in result.content

    @pytest.mark.asyncio
    async def test_valid_template_is_written(self, tool, tmp_path):
        target = tmp_path / "template.json"
        result = await tool.execute(
            tool_input={"path": str(target), "content": VALID_TEMPLATE},
            context=ToolContext(cwd=str(tmp_path)),
        )

        assert result.is_error is False
        assert target.read_text(encoding="utf-8") == VALID_TEMPLATE
        assert result.metadata["artifact"]["filename"] == "template.json"

    @pytest.mark.asyncio
    async def test_valid_yaml_template_is_written(self, tool, tmp_path):
        target = tmp_path / "template.yaml"
        result = await tool.execute(
            tool_input={"path": str(target), "content": VALID_YAML_TEMPLATE},
            context=ToolContext(cwd=str(tmp_path)),
        )

        assert result.is_error is False
        assert target.read_text(encoding="utf-8") == VALID_YAML_TEMPLATE

    @pytest.mark.asyncio
    async def test_non_template_write_is_unaffected(self, tool, tmp_path):
        target = tmp_path / "main.py"
        result = await tool.execute(
            tool_input={"path": str(target), "content": "x = 1\n"},
            context=ToolContext(cwd=str(tmp_path)),
        )

        assert result.is_error is False
        assert target.read_text(encoding="utf-8") == "x = 1\n"
        assert "ros_validation" not in (result.metadata or {})

    @pytest.mark.asyncio
    async def test_unrelated_json_write_is_unaffected(self, tool, tmp_path):
        target = tmp_path / "config.json"
        result = await tool.execute(
            tool_input={"path": str(target), "content": '{"name": "demo"}'},
            context=ToolContext(cwd=str(tmp_path)),
        )

        assert result.is_error is False
        assert target.read_text(encoding="utf-8") == '{"name": "demo"}'

    @pytest.mark.asyncio
    async def test_warning_only_template_is_written_with_diagnostics(self, tool, tmp_path, monkeypatch):
        from iac_code.tools.cloud.aliyun.ros_validation.model import (
            Category,
            Severity,
            ValidationReport,
            make_diagnostic,
        )
        from iac_code.tools.cloud.aliyun.ros_validation.outcome import outcome_from_report

        warning = make_diagnostic(
            code="ROS5001",
            severity=Severity.WARNING,
            category=Category.QUALITY,
            summary="quality warning",
            detail="non blocking",
        )
        outcome = outcome_from_report(ValidationReport.build([warning]), template_analyzed=True)
        monkeypatch.setattr(
            "iac_code.tools.cloud.aliyun.ros_validation.write_preflight.validate_template_before_write",
            lambda path, content: outcome,
        )

        target = tmp_path / "template.json"
        result = await tool.execute(
            tool_input={"path": str(target), "content": VALID_TEMPLATE},
            context=ToolContext(cwd=str(tmp_path)),
        )

        assert result.is_error is False
        assert target.read_text(encoding="utf-8") == VALID_TEMPLATE
        assert result.metadata["ros_validation"]["warning_count"] == 1
        assert "quality warning" in result.content

    @pytest.mark.asyncio
    async def test_preflight_failure_degrades_to_plain_write(self, tool, tmp_path, monkeypatch):
        def boom(path, content):
            raise RuntimeError("validator unavailable")

        monkeypatch.setattr(
            "iac_code.tools.cloud.aliyun.ros_validation.write_preflight.validate_template_before_write",
            boom,
        )

        target = tmp_path / "template.json"
        result = await tool.execute(
            tool_input={"path": str(target), "content": UNDOCUMENTED_TYPE_TEMPLATE},
            context=ToolContext(cwd=str(tmp_path)),
        )

        assert result.is_error is False
        assert target.read_text(encoding="utf-8") == UNDOCUMENTED_TYPE_TEMPLATE
