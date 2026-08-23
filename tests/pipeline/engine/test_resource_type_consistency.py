"""Tests for deterministic resource type consistency checks."""

from __future__ import annotations

import pytest

from iac_code.pipeline.engine.resource_type_consistency import (
    format_resource_type_issues,
    known_resource_types,
    validate_template_resource_types,
)

EXISTING_VPC_INTENTS = [
    {"product": "SecurityGroup", "action": "create"},
    {"product": "VPC", "action": "use_existing"},
]

SECURITY_GROUP_ONLY = """
Parameters:
  VpcId:
    Type: String
Resources:
  AppSecurityGroup:
    Type: ALIYUN::ECS::SecurityGroup
    Properties:
      VpcId: !Ref VpcId
Outputs:
  SecurityGroupId:
    Value: !GetAtt AppSecurityGroup.SecurityGroupId
"""


def _codes(issues) -> list[str]:
    return sorted(issue.code for issue in issues)


class TestResourceTypeCatalog:
    def test_catalog_contains_real_types(self):
        catalog = known_resource_types()
        assert "ALIYUN::ECS::VPC" in catalog
        assert "ALIYUN::ECS::SecurityGroup" in catalog
        assert "ALIYUN::OSS::Bucket" in catalog

    def test_catalog_excludes_hallucinated_types(self):
        catalog = known_resource_types()
        assert "ALIYUN::VPC::VPC" not in catalog
        assert "ALIYUN::VPC::VSwitch" not in catalog


class TestIntentConsistency:
    def test_compliant_template_has_no_issues(self):
        assert validate_template_resource_types(SECURITY_GROUP_ONLY, EXISTING_VPC_INTENTS) == []

    def test_creating_a_use_existing_product_is_rejected(self):
        template = """
Resources:
  AppVpc:
    Type: ALIYUN::ECS::VPC
    Properties:
      CidrBlock: 192.168.0.0/16
  AppSecurityGroup:
    Type: ALIYUN::ECS::SecurityGroup
    Properties:
      VpcId: !Ref AppVpc
"""
        issues = validate_template_resource_types(template, EXISTING_VPC_INTENTS)
        assert _codes(issues) == ["use_existing_resource_created"]
        assert issues[0].resource_name == "AppVpc"
        assert issues[0].resource_type == "ALIYUN::ECS::VPC"

    def test_reference_action_is_treated_like_use_existing(self):
        template = """
Resources:
  AppVpc:
    Type: ALIYUN::ECS::VPC
    Properties:
      CidrBlock: 192.168.0.0/16
"""
        issues = validate_template_resource_types(template, [{"product": "VPC", "action": "reference"}])
        assert _codes(issues) == ["use_existing_resource_created"]

    def test_missing_create_product_is_reported(self):
        issues = validate_template_resource_types(
            SECURITY_GROUP_ONLY,
            [{"product": "SecurityGroup", "action": "create"}, {"product": "OSS", "action": "create"}],
        )
        assert _codes(issues) == ["missing_required_resource"]
        assert issues[0].product == "OSS"

    def test_forbidden_product_is_reported(self):
        template = """
Resources:
  AppSecurityGroup:
    Type: ALIYUN::ECS::SecurityGroup
    Properties: {}
  AppVSwitch:
    Type: ALIYUN::ECS::VSwitch
    Properties: {}
"""
        issues = validate_template_resource_types(
            template,
            [{"product": "SecurityGroup", "action": "create"}, {"product": "VSwitch", "action": "forbid"}],
        )
        assert _codes(issues) == ["forbidden_resource_created"]
        assert issues[0].resource_name == "AppVSwitch"

    def test_undeclared_product_is_reported_as_extra(self):
        template = """
Resources:
  AppServer:
    Type: ALIYUN::ECS::Instance
    Properties: {}
  AppBucket:
    Type: ALIYUN::OSS::Bucket
    Properties: {}
"""
        issues = validate_template_resource_types(template, [{"product": "ECS", "action": "create"}])
        assert _codes(issues) == ["extra_resource_product"]
        assert issues[0].resource_name == "AppBucket"

    def test_unknown_resource_type_is_reported(self):
        template = """
Resources:
  AppVpc:
    Type: ALIYUN::VPC::VPC
    Properties: {}
"""
        issues = validate_template_resource_types(template, [{"product": "VPC", "action": "create"}])
        assert "unknown_resource_type" in _codes(issues)

    def test_templates_without_intents_only_check_type_existence(self):
        template = """
Resources:
  AppServer:
    Type: ALIYUN::ECS::Instance
    Properties: {}
  AppBucket:
    Type: ALIYUN::OSS::Bucket
    Properties: {}
"""
        assert validate_template_resource_types(template, []) == []
        assert validate_template_resource_types(template, None) == []

    @pytest.mark.parametrize(
        "product",
        ["ECS", "ecs", "Instance", "ALIYUN::ECS::Instance", "ECS Instance"],
    )
    def test_product_spelling_variants_match_the_same_resource(self, product):
        template = """
Resources:
  AppServer:
    Type: ALIYUN::ECS::Instance
    Properties: {}
"""
        assert validate_template_resource_types(template, [{"product": product, "action": "create"}]) == []

    def test_vswitch_belongs_to_the_vpc_product(self):
        template = """
Resources:
  AppVSwitch:
    Type: ALIYUN::ECS::VSwitch
    Properties: {}
"""
        assert validate_template_resource_types(template, [{"product": "VPC", "action": "create"}]) == []


class TestMalformedInput:
    @pytest.mark.parametrize("template", ["", "   ", None, 42, []])
    def test_non_template_values_are_rejected(self, template):
        issues = validate_template_resource_types(template, EXISTING_VPC_INTENTS)
        assert _codes(issues) == ["invalid_template_structure"]

    def test_invalid_yaml_is_rejected(self):
        issues = validate_template_resource_types("Resources: [unclosed", EXISTING_VPC_INTENTS)
        assert _codes(issues) == ["invalid_template_structure"]

    def test_missing_resources_section_is_rejected(self):
        issues = validate_template_resource_types("Parameters:\n  VpcId:\n    Type: String\n", EXISTING_VPC_INTENTS)
        assert _codes(issues) == ["invalid_template_structure"]

    def test_resource_without_type_is_rejected(self):
        issues = validate_template_resource_types("Resources:\n  App:\n    Properties: {}\n", EXISTING_VPC_INTENTS)
        assert _codes(issues) == ["invalid_template_structure"]

    def test_malformed_intent_entries_are_reported(self):
        issues = validate_template_resource_types(
            SECURITY_GROUP_ONLY,
            [{"product": "SecurityGroup", "action": "create"}, {"action": "create"}],
        )
        assert "invalid_resource_intent" in _codes(issues)


class TestIssueFormatting:
    def test_summary_mentions_resource_and_code(self):
        template = """
Resources:
  AppVpc:
    Type: ALIYUN::ECS::VPC
    Properties: {}
  AppSecurityGroup:
    Type: ALIYUN::ECS::SecurityGroup
    Properties: {}
"""
        summary = format_resource_type_issues(validate_template_resource_types(template, EXISTING_VPC_INTENTS))
        assert "use_existing_resource_created" in summary
        assert "AppVpc" in summary
        assert "ALIYUN::ECS::VPC" in summary

    def test_empty_issue_list_renders_empty_summary(self):
        assert format_resource_type_issues([]) == ""
