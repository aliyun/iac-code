from iac_code.services.capabilities.multimodal import (
    MultiModalSpec,
    get_multimodal_spec,
    is_model_multimodal,
)


def test_builtin_claude_opus_supports_images():
    spec = get_multimodal_spec("claude-opus-4-7")
    assert isinstance(spec, MultiModalSpec)
    assert spec.support_multimodal is True
    assert "image/png" in spec.formats


def test_unknown_model_defaults_to_no_images():
    spec = get_multimodal_spec("does-not-exist-1.0")
    assert spec.support_multimodal is False
    assert is_model_multimodal("does-not-exist-1.0") is False


def test_settings_override_wins_over_builtin(monkeypatch, tmp_path):
    settings = tmp_path / "settings.yml"
    settings.write_text("multiModal:\n  models:\n    custom-vl: {supportMultimodal: true}\n", encoding="utf-8")
    monkeypatch.setattr(
        "iac_code.services.capabilities.multimodal.get_settings_path",
        lambda: settings,
    )
    assert is_model_multimodal("custom-vl") is True


def test_settings_override_can_disable_builtin(monkeypatch, tmp_path):
    settings = tmp_path / "settings.yml"
    settings.write_text("multiModal:\n  models:\n    claude-opus-4-7: {supportMultimodal: false}\n", encoding="utf-8")
    monkeypatch.setattr(
        "iac_code.services.capabilities.multimodal.get_settings_path",
        lambda: settings,
    )
    assert is_model_multimodal("claude-opus-4-7") is False


def test_builtin_set_includes_registry_flagged_models():
    """Models marked support_multimodal in providers/registry.py must be picked up."""
    from iac_code.services.capabilities.multimodal import _builtin_multimodal_models

    builtin = _builtin_multimodal_models()
    # Spot-check a few well-known vision models from each provider family.
    assert "claude-opus-4-7" in builtin
    assert "gpt-5.5" in builtin
    assert "gemini-2.5-pro" in builtin
    assert "qwen3.6-plus" in builtin
    assert "kimi-k2.6" in builtin


def test_builtin_set_excludes_non_multimodal_models():
    """Models not flagged in registry should not appear in the built-in set."""
    from iac_code.services.capabilities.multimodal import _builtin_multimodal_models

    builtin = _builtin_multimodal_models()
    assert "deepseek-v4-pro" not in builtin
    assert "qwen3-coder-plus" not in builtin
