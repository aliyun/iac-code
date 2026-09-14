"""Tests for the /effort command."""

from unittest.mock import MagicMock

import pytest

from iac_code.commands.effort import effort_command
from iac_code.providers.thinking import EffortLevel


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


@pytest.mark.asyncio
@pytest.mark.parametrize("effort", ["minimal", "ultra"])
async def test_deepseek_v41_accepts_documented_effort_endpoints(monkeypatch, effort):
    save = MagicMock()
    monkeypatch.setattr("iac_code.commands.effort.get_active_provider_key", lambda: "dashscope")
    monkeypatch.setattr("iac_code.commands.effort.get_provider_config", lambda key: {})
    monkeypatch.setattr("iac_code.commands.effort.save_active_provider_config", save)

    store = MagicMock()
    store.get_state.return_value = MagicMock(model="deepseek-v4.1-flash")

    result = await effort_command(context=None, args=[effort], store=store)

    assert effort in result
    assert save.call_args.kwargs["effort"] == effort
    assert store.set_state.call_args.kwargs["effort_level"] is EffortLevel(effort)


@pytest.mark.asyncio
@pytest.mark.parametrize("effort", ["1", "100", "ultra-plus"])
async def test_deepseek_v41_rejects_unsupported_effort_values(monkeypatch, effort):
    monkeypatch.setattr("iac_code.commands.effort.get_active_provider_key", lambda: "dashscope")
    monkeypatch.setattr("iac_code.commands.effort.get_provider_config", lambda key: {})

    store = MagicMock()
    store.get_state.return_value = MagicMock(model="deepseek-v4.1-flash")

    result = await effort_command(context=None, args=[effort], store=store)

    assert "Invalid effort" in result
    assert "minimal" in result
    assert "ultra" in result
    store.set_state.assert_not_called()


@pytest.mark.asyncio
async def test_deepseek_v41_interactive_effort_lists_documented_values(monkeypatch):
    captured = {}

    def select_effort(title, options, default_index=0):
        captured["title"] = title
        captured["options"] = options
        captured["default_index"] = default_index
        return 6

    save = MagicMock()
    monkeypatch.setattr("iac_code.commands.effort.get_active_provider_key", lambda: "dashscope")
    monkeypatch.setattr("iac_code.commands.effort.get_provider_config", lambda key: {})
    monkeypatch.setattr("iac_code.commands.effort.save_active_provider_config", save)
    monkeypatch.setattr("iac_code.commands.effort._select", select_effort)

    store = MagicMock()
    store.get_state.return_value = MagicMock(model="deepseek-v4.1-flash")
    console = MagicMock()
    context = MagicMock(store=store, console=console)

    result = await effort_command(context=context, args=[])

    assert result.endswith("ultra")
    assert [option.rsplit("  ", 1)[-1] for option in captured["options"]] == [
        "minimal",
        "low",
        "medium",
        "high",
        "xhigh",
        "max",
        "ultra",
    ]
    assert captured["default_index"] == 0
    assert save.call_args.kwargs["effort"] == "ultra"


@pytest.mark.asyncio
async def test_kimi_code_interactive_effort_lists_exact_supported_values(monkeypatch):
    captured = {}

    def select_effort(title, options, default_index=0):
        captured["options"] = options
        captured["default_index"] = default_index
        return 0

    save = MagicMock()
    monkeypatch.setattr("iac_code.commands.effort.get_active_provider_key", lambda: "kimi_code")
    monkeypatch.setattr("iac_code.commands.effort.get_provider_config", lambda key: {})
    monkeypatch.setattr("iac_code.commands.effort.save_active_provider_config", save)
    monkeypatch.setattr("iac_code.commands.effort._select", select_effort)

    store = MagicMock()
    store.get_state.return_value = MagicMock(model="kimi-for-coding")
    context = MagicMock(store=store, console=MagicMock())

    result = await effort_command(context=context, args=[])

    assert [option.rsplit("  ", 1)[-1] for option in captured["options"]] == ["low", "high", "max"]
    assert captured["default_index"] == 2
    assert result.endswith("low")
    assert save.call_args.kwargs["effort"] == "low"


@pytest.mark.asyncio
async def test_kimi_code_rejects_server_aliases_from_user_effort_list(monkeypatch):
    monkeypatch.setattr("iac_code.commands.effort.get_active_provider_key", lambda: "kimi_code")
    monkeypatch.setattr("iac_code.commands.effort.get_provider_config", lambda key: {})
    store = MagicMock()
    store.get_state.return_value = MagicMock(model="kimi-for-coding")

    result = await effort_command(context=None, args=["medium"], store=store)

    assert "Invalid effort" in result
    assert "low" in result and "high" in result and "max" in result
    store.set_state.assert_not_called()


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
    async def test_bailian_glm52_accepts_effort_without_default_effort(self, tmp_path, monkeypatch):
        from iac_code import config
        from iac_code.commands import auth as auth_mod
        from iac_code.commands.effort import effort_command

        settings_path = tmp_path / "settings.yml"
        settings_path.write_text(
            "activeProvider: dashscope\nproviders:\n  dashscope:\n    model: glm-5.2\n",
            encoding="utf-8",
        )
        monkeypatch.setattr(config, "get_settings_path", lambda: settings_path)
        monkeypatch.setattr(auth_mod, "get_settings_path", lambda: settings_path)

        from types import SimpleNamespace

        class _Store:
            def get_state(self):
                return SimpleNamespace(model="glm-5.2")

            def set_state(self, **_):
                pass

        ctx = SimpleNamespace(store=_Store(), console=None)
        result = await effort_command(context=ctx, args=["high"])

        assert "high" in result.lower()
        body = settings_path.read_text(encoding="utf-8")
        assert "effort: high" in body

    @pytest.mark.asyncio
    async def test_bailian_glm52_without_saved_effort_reports_not_configured(self, tmp_path, monkeypatch):
        from iac_code import config
        from iac_code.commands import auth as auth_mod
        from iac_code.commands.effort import effort_command

        settings_path = tmp_path / "settings.yml"
        settings_path.write_text(
            "activeProvider: dashscope\nproviders:\n  dashscope:\n    model: glm-5.2\n",
            encoding="utf-8",
        )
        monkeypatch.setattr(config, "get_settings_path", lambda: settings_path)
        monkeypatch.setattr(auth_mod, "get_settings_path", lambda: settings_path)

        from types import SimpleNamespace

        class _Store:
            def get_state(self):
                return SimpleNamespace(model="glm-5.2")

            def set_state(self, **_):
                pass

        ctx = SimpleNamespace(store=_Store(), console=None)
        result = await effort_command(context=ctx, args=[])

        assert "not configured" in result
        assert "low" not in result.lower()

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
