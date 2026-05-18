"""Tests for auth_command and _auth_flow orchestration."""

from unittest.mock import MagicMock

import pytest

from iac_code.commands.auth import (
    _BACK,
    _aliyun_auth_flow,
    _aliyun_region_flow,
    _auth_flow,
    _cloud_auth_flow,
    _cloud_provider_display,
    _llm_auth_flow,
    auth_command,
)


@pytest.fixture(autouse=True)
def iac_home(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    return tmp_path


class TestAuthCommand:
    @pytest.mark.asyncio
    async def test_no_context_returns_error_message(self):
        # When no context/console, auth_command returns an error string
        result = await auth_command()
        assert isinstance(result, str)
        assert result  # non-empty

    @pytest.mark.asyncio
    async def test_no_context_no_console_in_kwargs(self):
        # Passing store but no console should still return error string
        result = await auth_command(store=MagicMock())
        assert isinstance(result, str)

    @pytest.mark.asyncio
    async def test_reinitialize_provider_called_after_auth(self, monkeypatch):
        """After auth completes, provider should be reinitialized so
        credential changes take effect immediately."""
        monkeypatch.setattr(
            "iac_code.commands.auth._auth_flow",
            lambda console, store: "Configured: test",
        )
        monkeypatch.setattr("sys.stdout", MagicMock())

        repl = MagicMock()
        repl.store.get_state.return_value = MagicMock(model="test-model")

        from dataclasses import dataclass

        @dataclass
        class FakeContext:
            console: object
            store: object
            repl: object

        context = FakeContext(console=MagicMock(), store=MagicMock(), repl=repl)
        result = await auth_command(context=context)

        assert result == "Configured: test"
        repl._reinitialize_provider.assert_called_once_with("test-model")


class TestAuthFlow:
    def test_select_escape_returns_cancelled_string(self, monkeypatch):
        # When _select returns None (Esc), _auth_flow returns "Auth cancelled" string
        monkeypatch.setattr("iac_code.commands.auth._select", lambda title, options, default_index=0: None)
        console = MagicMock()
        store = MagicMock()
        result = _auth_flow(console, store)
        assert isinstance(result, str)
        assert result  # non-empty string (translated "Auth cancelled")

    def test_select_llm_branch_called(self, monkeypatch):
        # Index 0 → LLM config branch
        monkeypatch.setattr("iac_code.commands.auth._select", lambda title, options, default_index=0: 0)
        called = {}

        def fake_llm(console, store):
            called["yes"] = True
            return "llm-result"

        monkeypatch.setattr("iac_code.commands.auth._llm_auth_flow", fake_llm)
        console = MagicMock()
        store = MagicMock()
        result = _auth_flow(console, store)
        assert called.get("yes") is True
        assert result == "llm-result"

    def test_select_cloud_branch_called(self, monkeypatch):
        # Index 1 → Cloud config branch
        monkeypatch.setattr("iac_code.commands.auth._select", lambda title, options, default_index=0: 1)
        called = {}

        def fake_cloud(console):
            called["yes"] = True
            return "cloud-result"

        monkeypatch.setattr("iac_code.commands.auth._cloud_auth_flow", fake_cloud)
        console = MagicMock()
        store = MagicMock()
        result = _auth_flow(console, store)
        assert called.get("yes") is True
        assert result == "cloud-result"

    def test_back_from_sub_flow_loops_back_to_category(self, monkeypatch):
        # When sub-flow returns _BACK, the loop continues; mock _select to return None
        # on the second call so the test terminates.
        call_count = {"n": 0}

        def select_side_effect(title, options, default_index=0):
            call_count["n"] += 1
            if call_count["n"] == 1:
                return 0  # first call → LLM branch
            return None  # second call → Esc → return cancelled

        monkeypatch.setattr("iac_code.commands.auth._select", select_side_effect)
        monkeypatch.setattr("iac_code.commands.auth._llm_auth_flow", lambda c, s: _BACK)
        console = MagicMock()
        store = MagicMock()
        result = _auth_flow(console, store)
        # Should have looped back and then got None → "Auth cancelled"
        assert call_count["n"] == 2
        assert isinstance(result, str)


class TestLlmAuthFlow:
    def test_escape_at_group_select_returns_back(self, monkeypatch):
        monkeypatch.setattr("iac_code.commands.auth._select", lambda title, options, default_index=0: None)
        result = _llm_auth_flow(MagicMock(), MagicMock())
        assert result is _BACK

    def test_back_at_sub_provider_returns_to_group(self, monkeypatch):
        call_count = {"select": 0}

        def select_side_effect(title, options, default_index=0):
            call_count["select"] += 1
            if call_count["select"] == 1:
                return 0  # Alibaba Cloud group (multiple items)
            if call_count["select"] == 2:
                return None  # Esc at sub-provider → back to group
            return None  # Esc at group → _BACK

        monkeypatch.setattr("iac_code.commands.auth._select", select_side_effect)
        result = _llm_auth_flow(MagicMock(), MagicMock())
        assert result is _BACK
        assert call_count["select"] == 3

    def test_back_at_api_key_input_loops_to_group_select(self, monkeypatch):
        call_count = {"select": 0}

        def select_side_effect(title, options, default_index=0):
            call_count["select"] += 1
            if call_count["select"] <= 2:
                return 0  # group + sub-provider
            return None  # Esc → _BACK

        monkeypatch.setattr("iac_code.commands.auth._select", select_side_effect)
        monkeypatch.setattr("iac_code.commands.auth._load_existing_key", lambda key_name: None)
        monkeypatch.setattr("iac_code.commands.auth._input_masked", lambda *a, **kw: _BACK)
        result = _llm_auth_flow(MagicMock(), MagicMock())
        assert result is _BACK
        assert call_count["select"] == 3

    def test_cancel_at_api_key_input_returns_cancelled(self, monkeypatch):
        # _select always returns 0 → Alibaba Cloud group → DashScope provider
        monkeypatch.setattr("iac_code.commands.auth._select", lambda title, options, default_index=0: 0)
        monkeypatch.setattr("iac_code.commands.auth._load_existing_key", lambda key_name: None)
        monkeypatch.setattr("iac_code.commands.auth._input_masked", lambda *a, **kw: None)
        result = _llm_auth_flow(MagicMock(), MagicMock())
        assert isinstance(result, str)
        assert result

    def test_single_item_group_skips_sub_selection(self, monkeypatch):
        """Groups with one provider (e.g. DeepSeek) skip the sub-provider step."""
        call_count = {"select": 0}

        def select_side_effect(title, options, default_index=0):
            call_count["select"] += 1
            if call_count["select"] == 1:
                for i, opt in enumerate(options):
                    if "DeepSeek" in opt:
                        return i
                return 6
            return None

        monkeypatch.setattr("iac_code.commands.auth._select", select_side_effect)
        monkeypatch.setattr("iac_code.commands.auth._load_existing_key", lambda key_name: None)
        monkeypatch.setattr("iac_code.commands.auth._input_masked", lambda *a, **kw: _BACK)
        result = _llm_auth_flow(MagicMock(), MagicMock())
        # _input_masked returns _BACK → loops; second _select returns None → _BACK
        assert result is _BACK
        assert call_count["select"] == 2  # group + group again (no sub-provider step)

    def test_successful_config_saves_and_returns_string(self, monkeypatch):
        # _select always returns 0 → Alibaba Cloud → DashScope (first sub-provider)
        monkeypatch.setattr("iac_code.commands.auth._select", lambda title, options, default_index=0: 0)
        monkeypatch.setattr("iac_code.commands.auth._load_existing_key", lambda key_name: None)
        monkeypatch.setattr("iac_code.commands.auth._input_masked", lambda *a, **kw: "test-api-key")
        monkeypatch.setattr(
            "iac_code.commands.auth.select_model_interactive",
            lambda models, current_model="", provider_display_name="": "qwen3.6-plus",
        )
        monkeypatch.setattr("iac_code.commands.auth.save_llm_key", lambda key_name, api_key: None)
        monkeypatch.setattr(
            "iac_code.commands.auth.save_active_provider_config",
            lambda provider, model, effort=None, api_base=None: None,
        )
        store = MagicMock()
        result = _llm_auth_flow(MagicMock(), store)
        assert isinstance(result, str)
        assert "qwen3.6-plus" in result
        store.set_state.assert_called_once_with(model="qwen3.6-plus")

    def test_openapi_compatible_uses_api_base_and_existing_key(self, monkeypatch):
        monkeypatch.setattr("iac_code.commands.auth._get_active_key_name", lambda: "openapi_compatible")
        calls = {"select": 0}

        def select_side_effect(title, options, default_index=0):
            calls["select"] += 1
            if calls["select"] == 1:
                for i, opt in enumerate(options):
                    if "Compatible" in opt:
                        return i
                return default_index
            if calls["select"] == 2:
                for i, opt in enumerate(options):
                    if "OpenAPI" in opt:
                        return i
                return 0
            return 0

        monkeypatch.setattr("iac_code.commands.auth._select", select_side_effect)
        monkeypatch.setattr("iac_code.commands.auth._load_existing_api_base", lambda key_name: "https://old.example/v1")
        monkeypatch.setattr(
            "iac_code.commands.auth._input_text_with_default", lambda *a, **kw: "https://new.example/v1"
        )
        monkeypatch.setattr("iac_code.commands.auth._load_existing_key", lambda key_name: "existing-key")
        monkeypatch.setattr("iac_code.commands.auth._input_masked", lambda *a, **kw: "existing-key")
        monkeypatch.setattr(
            "iac_code.commands.auth.select_model_interactive",
            lambda models, current_model="", provider_display_name="": "custom-model",
        )

        saved = {}

        monkeypatch.setattr(
            "iac_code.commands.auth.save_llm_key", lambda *args, **kwargs: saved.setdefault("key", True)
        )
        monkeypatch.setattr(
            "iac_code.commands.auth.save_active_provider_config",
            lambda provider, model, effort=None, api_base=None: saved.update(
                provider=provider["key_name"], model=model, api_base=api_base
            ),
        )
        store = MagicMock()

        result = _llm_auth_flow(MagicMock(), store)

        assert "custom-model" in result
        assert "key" not in saved
        assert saved == {
            "provider": "openapi_compatible",
            "model": "custom-model",
            "api_base": "https://new.example/v1",
        }
        store.set_state.assert_called_once_with(model="custom-model")

    def test_openapi_compatible_empty_api_base_restarts_group_selection(self, monkeypatch):
        calls = {"select": 0}

        def select_side_effect(title, options, default_index=0):
            calls["select"] += 1
            if calls["select"] == 1:
                for i, opt in enumerate(options):
                    if "Compatible" in opt:
                        return i
                return len(options) - 1
            if calls["select"] == 2:
                for i, opt in enumerate(options):
                    if "OpenAPI" in opt:
                        return i
                return 0
            return None

        monkeypatch.setattr("iac_code.commands.auth._select", select_side_effect)
        monkeypatch.setattr("iac_code.commands.auth._input_text_with_default", lambda *a, **kw: "   ")

        result = _llm_auth_flow(MagicMock(), MagicMock())

        assert result is _BACK
        assert calls["select"] == 3


class TestCloudAuthFlow:
    def test_cloud_provider_display_falls_back_to_original_name(self):
        assert _cloud_provider_display("custom") == "custom"

    def test_select_escape_returns_back(self, monkeypatch):
        monkeypatch.setattr("iac_code.commands.auth._select", lambda title, options, default_index=0: None)
        assert _cloud_auth_flow(MagicMock()) is _BACK

    def test_aliyun_branch_calls_subflow(self, monkeypatch):
        monkeypatch.setattr("iac_code.commands.auth._select", lambda title, options, default_index=0: 0)
        monkeypatch.setattr("iac_code.commands.auth._aliyun_auth_flow", lambda: "ok")
        assert _cloud_auth_flow(MagicMock()) == "ok"

    def test_aliyun_auth_flow_loops_back_from_submenu(self, monkeypatch):
        calls = {"select": 0}

        def select_side_effect(title, options, default_index=0):
            calls["select"] += 1
            if calls["select"] == 1:
                return 0
            return None

        monkeypatch.setattr("iac_code.commands.auth._select", select_side_effect)
        monkeypatch.setattr("iac_code.commands.auth._aliyun_credential_flow", lambda: _BACK)

        assert _aliyun_auth_flow() is _BACK
        assert calls["select"] == 2

    def test_region_flow_updates_existing_credential(self, monkeypatch):
        from iac_code.services.providers.aliyun import AliyunCredential

        existing = AliyunCredential(region_id="cn-beijing")
        saved = {}

        monkeypatch.setattr(
            "iac_code.services.providers.aliyun.AliyunCredentials._load_from_iac_code_config",
            lambda: existing,
        )
        monkeypatch.setattr(
            "iac_code.services.providers.aliyun.AliyunCredentials.load_from_aliyun_cli",
            lambda config_path=None: None,
        )
        monkeypatch.setattr("iac_code.commands.auth._input_text_with_default", lambda *a, **kw: "cn-shanghai")
        monkeypatch.setattr(
            "iac_code.services.providers.aliyun.AliyunCredentials.save",
            lambda credential: saved.setdefault("credential", credential),
        )

        result = _aliyun_region_flow()

        assert "Configured" in result
        assert saved["credential"] is existing
        assert existing.region_id == "cn-shanghai"

    def test_region_flow_creates_new_credential_when_missing(self, monkeypatch):
        saved = {}

        monkeypatch.setattr(
            "iac_code.services.providers.aliyun.AliyunCredentials._load_from_iac_code_config",
            lambda: None,
        )
        monkeypatch.setattr(
            "iac_code.services.providers.aliyun.AliyunCredentials.load_from_aliyun_cli",
            lambda config_path=None: None,
        )
        monkeypatch.setattr("iac_code.commands.auth._input_text_with_default", lambda *a, **kw: " ")
        monkeypatch.setattr(
            "iac_code.services.providers.aliyun.AliyunCredentials.save",
            lambda credential: saved.setdefault("credential", credential),
        )

        result = _aliyun_region_flow()

        assert "Configured" in result
        assert saved["credential"].region_id == "cn-hangzhou"


class TestAuthLlmSourceLock:
    def test_auth_flow_locked_shows_cloud_select_with_lock_notice(self, monkeypatch):
        """When llm_source is 'qwenpaw', _auth_flow shows lock notice in _select title."""
        titles_seen = []

        def fake_select(title, options, default_index=0):
            titles_seen.append(title)
            return 0  # select first cloud provider (aliyun)

        monkeypatch.setattr("iac_code.commands.auth.get_llm_source", lambda: "qwenpaw")
        monkeypatch.setattr("iac_code.commands.auth._select", fake_select)
        monkeypatch.setattr("iac_code.commands.auth._aliyun_auth_flow", lambda: "cloud done")
        result = _auth_flow(MagicMock(), MagicMock())
        assert result == "cloud done"
        assert any("qwenpaw" in t for t in titles_seen)

    def test_auth_flow_locked_env_shows_lock_notice(self, monkeypatch):
        """When llm_source is 'env', lock notice mentions 'env'."""
        titles_seen = []

        def fake_select(title, options, default_index=0):
            titles_seen.append(title)
            return 0

        monkeypatch.setattr("iac_code.commands.auth.get_llm_source", lambda: "env")
        monkeypatch.setattr("iac_code.commands.auth._select", fake_select)
        monkeypatch.setattr("iac_code.commands.auth._aliyun_auth_flow", lambda: "cloud done")
        _auth_flow(MagicMock(), MagicMock())
        assert any("env" in t for t in titles_seen)

    def test_auth_flow_locked_escape_returns_cancelled(self, monkeypatch):
        """When locked and user presses Esc, return cancelled."""
        monkeypatch.setattr("iac_code.commands.auth.get_llm_source", lambda: "qwenpaw")
        monkeypatch.setattr("iac_code.commands.auth._select", lambda title, options, default_index=0: None)
        result = _auth_flow(MagicMock(), MagicMock())
        assert "cancel" in result.lower()

    def test_auth_flow_normal_when_local(self, monkeypatch):
        """When llm_source is 'local', _auth_flow shows category selection as usual."""
        monkeypatch.setattr("iac_code.commands.auth.get_llm_source", lambda: "local")
        monkeypatch.setattr("iac_code.commands.auth._select", lambda title, options, default_index=0: None)
        result = _auth_flow(MagicMock(), MagicMock())
        assert "cancel" in result.lower()
