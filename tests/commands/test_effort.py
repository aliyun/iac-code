"""Tests for the /effort command."""

from unittest.mock import MagicMock

import pytest

from iac_code.commands.effort import effort_command


@pytest.mark.asyncio
async def test_effort_no_active_provider(monkeypatch):
    monkeypatch.setattr("iac_code.commands.effort.get_active_provider_key", lambda: None)
    result = await effort_command(context=None, args=[])
    assert "/auth" in result


@pytest.mark.asyncio
async def test_effort_no_model(monkeypatch):
    monkeypatch.setattr("iac_code.commands.effort.get_active_provider_key", lambda: "deepseek")
    store = MagicMock()
    store.get_state.return_value = MagicMock(model="")
    result = await effort_command(context=None, args=[], store=store)
    assert "/model" in result


@pytest.mark.asyncio
async def test_effort_unsupported_model(monkeypatch):
    monkeypatch.setattr("iac_code.commands.effort.get_active_provider_key", lambda: "dashscope")
    store = MagicMock()
    store.get_state.return_value = MagicMock(model="qwen3.6-plus")
    result = await effort_command(context=None, args=[], store=store)
    assert "does not support effort" in result


@pytest.mark.asyncio
async def test_effort_non_interactive_sets_level(monkeypatch, tmp_path):
    # Isolate settings/credentials paths
    monkeypatch.setattr("iac_code.commands.auth.get_settings_path", lambda: tmp_path / "settings.yml")
    monkeypatch.setattr("iac_code.commands.effort.get_active_provider_key", lambda: "deepseek")
    monkeypatch.setattr("iac_code.commands.effort.get_provider_config", lambda key: {})

    store = MagicMock()
    store.get_state.return_value = MagicMock(model="deepseek-v4-pro")

    result = await effort_command(context=None, args=["max"], store=store)
    assert "max" in result
    # store.set_state called with effort_level
    store.set_state.assert_called()
    _, kwargs = store.set_state.call_args
    assert kwargs.get("effort_level") is not None
    assert kwargs["effort_level"].value == "max"


@pytest.mark.asyncio
async def test_effort_rejects_out_of_range_level(monkeypatch):
    """deepseek-v4-pro only allows high/max — 'min' must be rejected."""
    monkeypatch.setattr("iac_code.commands.effort.get_active_provider_key", lambda: "deepseek")
    monkeypatch.setattr("iac_code.commands.effort.get_provider_config", lambda key: {})

    store = MagicMock()
    store.get_state.return_value = MagicMock(model="deepseek-v4-pro")

    result = await effort_command(context=None, args=["min"], store=store)
    assert "Invalid effort" in result
    store.set_state.assert_not_called()


@pytest.mark.asyncio
async def test_effort_no_console_shows_current(monkeypatch):
    monkeypatch.setattr("iac_code.commands.effort.get_active_provider_key", lambda: "deepseek")
    monkeypatch.setattr("iac_code.commands.effort.get_provider_config", lambda key: {"effort": "max"})

    store = MagicMock()
    store.get_state.return_value = MagicMock(model="deepseek-v4-pro")
    context = MagicMock(console=None, store=store)

    result = await effort_command(context=context, args=[], store=store)
    assert "max" in result


class TestEffortPerProviderRouting:
    @pytest.mark.asyncio
    async def test_bailian_qwen_reports_unsupported(self, tmp_path, monkeypatch):
        from iac_code import config
        from iac_code.commands.effort import effort_command

        settings_path = tmp_path / "settings.yml"
        settings_path.write_text(
            "activeProvider: bailian\nproviders:\n  bailian:\n    model: qwen3.6-plus\n",
            encoding="utf-8",
        )
        monkeypatch.setattr(config, "get_settings_path", lambda: settings_path)

        from types import SimpleNamespace

        class _Store:
            def get_state(self):
                return SimpleNamespace(model="qwen3.6-plus")

            def set_state(self, **_):
                pass

        ctx = SimpleNamespace(store=_Store(), console=None)
        result = await effort_command(context=ctx, args=["high"])
        assert "does not support" in result or "不支持" in result

    @pytest.mark.asyncio
    async def test_bailian_deepseek_accepts_high(self, tmp_path, monkeypatch):
        from iac_code import config
        from iac_code.commands import auth as auth_mod
        from iac_code.commands.effort import effort_command

        settings_path = tmp_path / "settings.yml"
        settings_path.write_text(
            "activeProvider: bailian\nproviders:\n  bailian:\n    model: deepseek-v4-pro\n",
            encoding="utf-8",
        )
        monkeypatch.setattr(config, "get_settings_path", lambda: settings_path)
        monkeypatch.setattr(auth_mod, "get_settings_path", lambda: settings_path)

        from types import SimpleNamespace

        class _Store:
            def __init__(self):
                self._effort = None

            def get_state(self):
                return SimpleNamespace(model="deepseek-v4-pro")

            def set_state(self, **kwargs):
                self._effort = kwargs.get("effort_level")

        store = _Store()
        ctx = SimpleNamespace(store=store, console=None)
        result = await effort_command(context=ctx, args=["high"])
        assert "high" in result.lower()
        body = settings_path.read_text(encoding="utf-8")
        assert "effort: high" in body
        # Save migrates the legacy "bailian" entry to canonical "dashscope".
        assert "dashscope" in body
        assert "bailian" not in body

    @pytest.mark.asyncio
    async def test_official_deepseek_accepts_high_in_separate_slot(self, tmp_path, monkeypatch):
        from iac_code import config
        from iac_code.commands import auth as auth_mod
        from iac_code.commands.effort import effort_command

        settings_path = tmp_path / "settings.yml"
        settings_path.write_text(
            "activeProvider: deepseek\nproviders:\n  deepseek:\n    model: deepseek-v4-pro\n",
            encoding="utf-8",
        )
        monkeypatch.setattr(config, "get_settings_path", lambda: settings_path)
        monkeypatch.setattr(auth_mod, "get_settings_path", lambda: settings_path)

        from types import SimpleNamespace

        class _Store:
            def get_state(self):
                return SimpleNamespace(model="deepseek-v4-pro")

            def set_state(self, **_):
                pass

        ctx = SimpleNamespace(store=_Store(), console=None)
        result = await effort_command(context=ctx, args=["high"])
        assert "high" in result.lower()
        body = settings_path.read_text(encoding="utf-8")
        assert "effort: high" in body
        assert "deepseek:" in body
        assert "bailian" not in body
