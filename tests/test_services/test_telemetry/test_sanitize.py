"""Tests for telemetry text sanitization.

Per spec §5.4 (matrix) and §5.5 (sanitize_error_message implementation).
"""

import pytest

from iac_code.services.telemetry.sanitize import (
    bucket_resource_count,
    sanitize_error_message,
    sanitize_model_name,
    sanitize_resource_type,
    sanitize_skill_name,
    sanitize_terraform_provider,
    sanitize_tool_name,
)


@pytest.fixture(autouse=True)
def _default_privacy(monkeypatch):
    monkeypatch.delenv("DISABLE_TELEMETRY", raising=False)
    monkeypatch.delenv("IAC_CODE_DISABLE_NONESSENTIAL_TRAFFIC", raising=False)


# ---- sanitize_error_message -----------------------------------------


def test_error_message_none_stays_none():
    assert sanitize_error_message(None) is None


def test_error_message_unchanged_when_short_and_clean():
    assert sanitize_error_message("rate limit exceeded") == "rate limit exceeded"


def test_error_message_newlines_replaced_with_space():
    assert sanitize_error_message("line1\nline2\rline3\tend") == "line1 line2 line3 end"


def test_error_message_truncated_at_512_bytes():
    raw = "x" * 1000
    out = sanitize_error_message(raw)
    assert out.endswith("... (truncated)")
    assert len(out.encode("utf-8")) <= 512


def test_error_message_stripped_in_essential_traffic_mode(monkeypatch):
    monkeypatch.setenv("IAC_CODE_DISABLE_NONESSENTIAL_TRAFFIC", "1")
    assert sanitize_error_message("rate limit exceeded") is None


# ---- sanitize_skill_name --------------------------------------------


def test_skill_name_bundled_passes_through():
    assert sanitize_skill_name("iac_aliyun") == "iac_aliyun"


def test_skill_name_custom_becomes_placeholder():
    assert sanitize_skill_name("acme_internal_deploy") == "custom"


def test_skill_name_none_stays_none():
    assert sanitize_skill_name(None) is None


# ---- sanitize_resource_type (ROS) -----------------------------------


def test_ros_resource_type_aliyun_prefix_passes_through():
    assert sanitize_resource_type("ALIYUN::ECS::Instance", kind="ros") == "ALIYUN::ECS::Instance"


def test_ros_resource_type_datasource_prefix_passes_through():
    assert sanitize_resource_type("DATASOURCE::ECS::Images", kind="ros") == "DATASOURCE::ECS::Images"


def test_ros_resource_type_custom_becomes_placeholder():
    assert sanitize_resource_type("Custom::AcmeCorp::Thing", kind="ros") == "Custom::Other"


# ---- sanitize_resource_type (Terraform) -----------------------------


def test_tf_resource_type_alicloud_passes_through():
    assert sanitize_resource_type("alicloud_instance", kind="terraform") == "alicloud_instance"


def test_tf_resource_type_aws_passes_through():
    assert sanitize_resource_type("aws_s3_bucket", kind="terraform") == "aws_s3_bucket"


def test_tf_resource_type_unknown_provider_becomes_placeholder():
    assert sanitize_resource_type("acmecorp_internal", kind="terraform") == "custom_provider::other"


def test_tf_resource_type_without_underscore_becomes_placeholder():
    assert sanitize_resource_type("broken", kind="terraform") == "custom_provider::other"


# ---- sanitize_terraform_provider ------------------------------------


def test_tf_provider_official_passes_through():
    assert sanitize_terraform_provider("alicloud") == "alicloud"


def test_tf_provider_unknown_becomes_other():
    assert sanitize_terraform_provider("acmecorp") == "other"


# ---- sanitize_model_name --------------------------------------------


def test_model_name_known_passes_through():
    assert sanitize_model_name("claude-opus-4-7") == "claude-opus-4-7"


def test_model_name_dev_version_trimmed():
    assert sanitize_model_name("claude-opus-4-7-20260101") == "claude-opus-4-7"


def test_model_name_unknown_becomes_other():
    assert sanitize_model_name("acmecorp-private-llm") == "other"


# ---- sanitize_tool_name ---------------------------------------------


def test_tool_name_normal_passes_through():
    assert sanitize_tool_name("Bash") == "Bash"


def test_tool_name_mcp_becomes_placeholder():
    assert sanitize_tool_name("mcp__acmecorp__query") == "mcp_tool"


# ---- bucket_resource_count ------------------------------------------


@pytest.mark.parametrize(
    "n,expected",
    [
        (0, "1-5"),
        (1, "1-5"),
        (5, "1-5"),
        (6, "6-20"),
        (20, "6-20"),
        (21, "21-50"),
        (50, "21-50"),
        (51, "50+"),
        (9999, "50+"),
    ],
)
def test_bucket_resource_count(n, expected):
    assert bucket_resource_count(n) == expected
