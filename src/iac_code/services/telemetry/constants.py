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
        "claude-fable-5",
        "claude-opus-5",
        "claude-opus-4-8",
        "claude-sonnet-5",
        "claude-opus-4-7",
        "claude-sonnet-4-6",
        "claude-haiku-4-5",
        "claude-opus-4-6",
        "claude-sonnet-4-5",
        "claude-sonnet-4-6-1m",
        "claude-haiku-4-5-20251001",
        # OpenAI
        "gpt-5.6",
        "gpt-5.6-sol",
        "gpt-5.6-terra",
        "gpt-5.6-luna",
        "gpt-5.5",
        "gpt-5.5-pro",
        "gpt-5.4",
        "gpt-5.4-pro",
        "gpt-5.4-mini",
        "gpt-5.4-nano",
        "gpt-5.3-codex",
        "gpt-5.2-codex",
        "gpt-5.2",
        "gpt-5.2-pro",
        "gpt-4o",
        "gpt-4o-mini",
        "gpt-4-turbo",
        "o1",
        "o1-mini",
        "o3",
        "o3-mini",
        "o4-mini",
        # Dashscope / Qwen
        "qwen3.8-max",
        "qwen3.8-max-preview",
        "qwen3.7-max",
        "qwen3.7-plus",
        "qwen3.7-flash",
        "qwen3.6-plus",
        "qwen3.6-flash",
        "qwen3.6-max-preview",
        "qwen3.5-plus",
        "qwen3.5-flash",
        "qwen3-max",
        "qwen-max",
        "qwen-plus",
        "qwen-flash",
        "qwen-turbo",
        "qwen3-coder",
        "qwen3-coder-plus",
        "qwen3-coder-next",
        "qwen2.5-coder",
        "qwen2.5-72b-instruct",
        "qwq-plus",
        "Qwen/Qwen3.5-122B-A10B",
        # Kimi / GLM / Gemini
        "kimi-k3",
        "kimi/kimi-k3",
        "kimi-k2.5",
        "kimi-k2.6",
        "kimi-k2.7-code",
        "kimi-k2.7-code-highspeed",
        "glm-5.3",
        "glm-5.2-fast-preview",
        "glm-5.2",
        "glm-5.1",
        "glm-5",
        "glm-5-turbo",
        "glm-4.7",
        "glm-4.7-flash",
        "glm-4.7-flashx",
        "glm-4.6",
        "glm-4.5",
        "glm-4.5-air",
        "glm-4.5-airx",
        "glm-4.5-flash",
        "glm-4.5-x",
        "gemini-3.7-flash",
        "gemini-3.6-flash",
        "gemini-3.5-flash",
        "gemini-3.5-flash-lite",
        "gemini-3.1-pro-preview",
        "gemini-3.1-pro-preview-customtools",
        "gemini-3.1-flash-lite",
        "gemini-3-flash-preview",
        "gemini-2.5-pro",
        "gemini-2.5-flash",
        "gemini-2.5-flash-lite",
        # DeepSeek / MiniMax / Volcengine
        "deepseek-v4-pro",
        "deepseek-v4-pro-0813",
        "deepseek-v4-flash-0731",
        "deepseek-v4-flash",
        "deepseek-v3.2",
        "MiniMax/MiniMax-M3",
        "MiniMax-M3",
        "MiniMax-M2.7",
        "MiniMax-M2.7-highspeed",
        "MiniMax-M2.5",
        "MiniMax-M2.5-highspeed",
        "minimax-m2.7",
        "doubao-seed-2-0-pro-260215",
        "doubao-seed-2-0-lite-260428",
        "doubao-seed-2-0-code-preview-260215",
    }
)

# Sentinels used throughout the module.
CUSTOM_SKILL_PLACEHOLDER = "custom"
OTHER_MODEL_PLACEHOLDER = "other"
CUSTOM_TF_PROVIDER_PLACEHOLDER = "other"
CUSTOM_ROS_RESOURCE_PLACEHOLDER = "Custom::Other"
CUSTOM_TF_RESOURCE_PLACEHOLDER = "custom_provider::other"
MCP_TOOL_PLACEHOLDER = "mcp_tool"
