"""Tests for telemetry whitelist constants."""

from iac_code.services.telemetry.constants import (
    BUNDLED_SKILLS,
    KNOWN_MODELS,
    ROS_ALLOWED_PREFIXES,
    TERRAFORM_OFFICIAL_PROVIDERS,
)


def test_bundled_skills_contains_iac_aliyun():
    assert "iac_aliyun" in BUNDLED_SKILLS


def test_ros_allowed_prefixes_contains_aliyun_and_datasource():
    assert "ALIYUN::" in ROS_ALLOWED_PREFIXES
    assert "DATASOURCE::" in ROS_ALLOWED_PREFIXES


def test_terraform_providers_contains_major_clouds():
    for p in ("alicloud", "aws", "azurerm", "google", "kubernetes"):
        assert p in TERRAFORM_OFFICIAL_PROVIDERS


def test_known_models_contains_claude_and_openai():
    assert "claude-opus-4-7" in KNOWN_MODELS
    assert "gpt-4o" in KNOWN_MODELS


def test_whitelist_constants_are_frozensets():
    for c in (BUNDLED_SKILLS, KNOWN_MODELS, TERRAFORM_OFFICIAL_PROVIDERS):
        assert isinstance(c, frozenset)


def test_ros_prefixes_is_tuple_for_startswith():
    assert isinstance(ROS_ALLOWED_PREFIXES, tuple)
    assert "ALIYUN::ECS::Instance".startswith(ROS_ALLOWED_PREFIXES)
