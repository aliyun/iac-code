"""Whitelists used by the sanitize module."""

from __future__ import annotations

# bundled skills keep their real name. Custom user skills
# (outside this set) become "custom".
BUNDLED_SKILLS: frozenset[str] = frozenset(
    {
        "iac_aliyun",
    }
)

# Terraform official providers keep their real name.
# Custom providers become "other".
TERRAFORM_OFFICIAL_PROVIDERS: frozenset[str] = frozenset(
    {
        "alicloud",
        "aws",
        "azurerm",
        "google",
        "kubernetes",
        "oci",
        "tencentcloud",
        "huaweicloud",
        "volcengine",
        "vsphere",
        "helm",
        "null",
        "random",
        "time",
        "archive",
        "local",
        "external",
        "http",
        "tls",
    }
)

# ROS resource type prefixes.
ROS_ALLOWED_PREFIXES: tuple[str, ...] = ("ALIYUN::", "DATASOURCE::")

# normalized model names. Unknown → "other".
KNOWN_MODELS: frozenset[str] = frozenset(
    {
        # Anthropic
        "claude-opus-4-7",
        "claude-sonnet-4-6",
        "claude-haiku-4-5",
        "claude-opus-4-6",
        "claude-sonnet-4-5",
        "claude-haiku-4-5-20251001",
        # OpenAI
        "gpt-4o",
        "gpt-4o-mini",
        "gpt-4-turbo",
        "o1",
        "o1-mini",
        "o3-mini",
        # Dashscope / Qwen
        "qwen-max",
        "qwen-plus",
        "qwen-turbo",
        "qwen3-coder",
        "qwen2.5-coder",
        "qwen2.5-72b-instruct",
    }
)

# Sentinels used throughout the module.
CUSTOM_SKILL_PLACEHOLDER = "custom"
OTHER_MODEL_PLACEHOLDER = "other"
CUSTOM_TF_PROVIDER_PLACEHOLDER = "other"
CUSTOM_ROS_RESOURCE_PLACEHOLDER = "Custom::Other"
CUSTOM_TF_RESOURCE_PLACEHOLDER = "custom_provider::other"
MCP_TOOL_PLACEHOLDER = "mcp_tool"
