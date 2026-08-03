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


def test_known_models_contains_researched_provider_updates():
    for model in (
        "claude-fable-5",
        "claude-opus-5",
        "gpt-5.6-sol",
        "qwen3.8-max",
        "qwen3.8-max-preview",
        "kimi-k3",
        "glm-5.2-fast-preview",
        "glm-5.2",
        "gemini-3.6-flash",
        "gemini-3.5-flash",
        "gemini-3.5-flash-lite",
        "gemini-2.5-pro",
        "deepseek-v4-pro",
        "deepseek-v4-flash-0731",
        "MiniMax-M3",
        "doubao-seed-2-0-pro-260215",
    ):
        assert model in KNOWN_MODELS


def test_every_static_registry_model_is_allowed_in_telemetry():
    from iac_code.providers.registry import PROVIDER_REGISTRY

    missing = {
        model.id
        for descriptor in PROVIDER_REGISTRY.values()
        for model in descriptor.models
        if model.id not in KNOWN_MODELS
    }
    assert missing == set()


def test_every_thinking_registry_model_is_allowed_in_telemetry():
    from iac_code.providers.thinking import MODEL_THINKING

    missing = {
        model for provider_models in MODEL_THINKING.values() for model in provider_models if model not in KNOWN_MODELS
    }
    assert missing == set()


def test_whitelist_constants_are_frozensets():
    for c in (BUNDLED_SKILLS, KNOWN_MODELS, TERRAFORM_OFFICIAL_PROVIDERS):
        assert isinstance(c, frozenset)


def test_ros_prefixes_is_tuple_for_startswith():
    assert isinstance(ROS_ALLOWED_PREFIXES, tuple)
    assert "ALIYUN::ECS::Instance".startswith(ROS_ALLOWED_PREFIXES)
