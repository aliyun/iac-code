"""Tests for auth_command and _auth_flow orchestration."""

from datetime import datetime
from unittest.mock import MagicMock

import pytest

from iac_code.commands.auth import (
    _BACK,
    _aliyun_auth_flow,
    _aliyun_credential_flow,
    _aliyun_region_flow,
    _auth_flow,
    _cloud_auth_flow,
    _cloud_provider_display,
    _llm_auth_flow,
    _oauth_escape_cancel_event,
    _render_credential_info,
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
        """After auth completes, provider and cloud tools refresh immediately."""
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
        repl.refresh_cloud_tools.assert_called_once_with()


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
                return 0  # Alibaba Cloud group (first entry when no third-party)
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
            if call_count["select"] == 1:
                return 0  # Alibaba Cloud group (first entry when no third-party)
            if call_count["select"] == 2:
                return 0  # sub-provider
            return None  # Esc → _BACK

        monkeypatch.setattr("iac_code.commands.auth._select", select_side_effect)
        monkeypatch.setattr("iac_code.commands.auth._load_existing_key", lambda key_name: None)
        monkeypatch.setattr("iac_code.commands.auth._input_masked", lambda *a, **kw: _BACK)
        result = _llm_auth_flow(MagicMock(), MagicMock())
        assert result is _BACK
        assert call_count["select"] == 3

    def test_cancel_at_api_key_input_returns_cancelled(self, monkeypatch):
        # _select returns 0 for group (Alibaba Cloud, first entry when no third-party) then 0 for sub-provider
        call_count = {"n": 0}

        def select_side_effect(title, options, default_index=0):
            call_count["n"] += 1
            if call_count["n"] == 1:
                return 0  # Alibaba Cloud group (first entry when no third-party)
            return 0  # DashScope provider

        monkeypatch.setattr("iac_code.commands.auth._select", select_side_effect)
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
        # _select returns 0 for group (Alibaba Cloud, first entry when no third-party) then 0 for sub-provider
        call_count = {"n": 0}

        def select_side_effect(title, options, default_index=0):
            call_count["n"] += 1
            if call_count["n"] == 1:
                return 0  # Alibaba Cloud group (first entry when no third-party)
            return 0  # DashScope (first sub-provider)

        monkeypatch.setattr("iac_code.commands.auth._select", select_side_effect)
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

    def test_render_credential_info_formats_oauth_expiration_as_local_datetime(self, monkeypatch):
        from iac_code.services.providers.aliyun import AliyunCredential

        writes: list[str] = []
        monkeypatch.setattr("iac_code.commands.auth._write", writes.append)
        credential = AliyunCredential(
            mode="OAuth",
            region_id="cn-hangzhou",
            oauth_site_type="CN",
            oauth_access_token="access-token",
            oauth_refresh_token="refresh-token",
            oauth_access_token_expire=1780397040,
            sts_expiration=1780397041,
        )

        _render_credential_info(credential, "iac-code")

        output = "".join(writes)
        expected_access_time = datetime.fromtimestamp(1780397040).astimezone().strftime("%Y-%m-%d %H:%M:%S")
        expected_sts_time = datetime.fromtimestamp(1780397041).astimezone().strftime("%Y-%m-%d %H:%M:%S")
        assert expected_access_time in output
        assert expected_sts_time in output
        assert "1780397040" not in output
        assert "1780397041" not in output

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

    def test_aliyun_credential_flow_shows_oauth_mode(self, monkeypatch):
        options_seen = []

        monkeypatch.setattr(
            "iac_code.services.providers.aliyun.AliyunCredentials._load_from_iac_code_config",
            lambda: None,
        )
        monkeypatch.setattr(
            "iac_code.services.providers.aliyun.AliyunCredentials.load_from_aliyun_cli",
            lambda config_path=None: None,
        )

        def fake_select(title, options, default_index=0):
            if "credential type" in title.lower():
                options_seen.extend(options)
            return None

        monkeypatch.setattr("iac_code.commands.auth._select", fake_select)

        assert _aliyun_credential_flow() is _BACK
        assert "OAuth Login (Browser)" in options_seen

    def test_aliyun_credential_flow_oauth_login_saves_credentials(self, monkeypatch):
        from iac_code.services.providers.aliyun_oauth import OAuthStsCredentials, OAuthToken

        saved = {}

        monkeypatch.setattr(
            "iac_code.services.providers.aliyun.AliyunCredentials._load_from_iac_code_config",
            lambda: None,
        )
        monkeypatch.setattr(
            "iac_code.services.providers.aliyun.AliyunCredentials.load_from_aliyun_cli",
            lambda config_path=None: None,
        )

        def fake_select(title, options, default_index=0):
            if "credential type" in title.lower():
                return options.index("OAuth Login (Browser)")
            if "site type" in title.lower():
                return options.index("China")
            return None

        class FakeOAuthClient:
            def __init__(self, site):
                self.site = site

            def exchange_access_token_for_sts(self, access_token):
                assert access_token == "access-token"
                return OAuthStsCredentials("tmp-ak", "tmp-sk", "tmp-sts", 1798794000)

        def fake_browser_oauth_flow(site_type, oauth_client=None, cancel_event=None):
            assert site_type == "CN"
            assert isinstance(oauth_client, FakeOAuthClient)
            assert cancel_event is not None
            return OAuthToken("access-token", "refresh-token", 1798790400, 1822320000)

        monkeypatch.setattr("iac_code.commands.auth._select", fake_select)
        monkeypatch.setattr(
            "iac_code.services.providers.aliyun_oauth.run_browser_oauth_flow",
            fake_browser_oauth_flow,
        )
        monkeypatch.setattr("iac_code.services.providers.aliyun_oauth.AliyunOAuthClient", FakeOAuthClient)
        monkeypatch.setattr(
            "iac_code.services.providers.aliyun.AliyunCredentials.save",
            lambda credential: saved.setdefault("credential", credential),
        )

        result = _aliyun_credential_flow()

        assert result == "Configured: Alibaba Cloud OAuth credentials saved"
        credential = saved["credential"]
        assert credential.mode == "OAuth"
        assert credential.region_id == "cn-hangzhou"
        assert credential.oauth_site_type == "CN"
        assert credential.oauth_access_token == "access-token"
        assert credential.oauth_refresh_token == "refresh-token"
        assert credential.oauth_access_token_expire == 1798790400
        assert credential.oauth_refresh_token_expire == 1822320000
        assert credential.access_key_id == "tmp-ak"
        assert credential.access_key_secret == "tmp-sk"
        assert credential.sts_token == "tmp-sts"
        assert credential.sts_expiration == 1798794000

    def test_aliyun_credential_flow_oauth_preserves_existing_region(self, monkeypatch):
        from iac_code.services.providers.aliyun import AliyunCredential
        from iac_code.services.providers.aliyun_oauth import OAuthStsCredentials, OAuthToken

        existing = AliyunCredential(region_id="cn-shanghai")
        saved = {}

        monkeypatch.setattr(
            "iac_code.services.providers.aliyun.AliyunCredentials._load_from_iac_code_config",
            lambda: existing,
        )
        monkeypatch.setattr(
            "iac_code.services.providers.aliyun.AliyunCredentials.load_from_aliyun_cli",
            lambda config_path=None: None,
        )
        monkeypatch.setattr(
            "iac_code.commands.auth._select_with_info",
            lambda title, options, info_renderer=None, default_index=0: 0,
        )

        def fake_select(title, options, default_index=0):
            if "credential type" in title.lower():
                return options.index("OAuth Login (Browser)")
            if "site type" in title.lower():
                return options.index("International")
            return None

        class FakeOAuthClient:
            def __init__(self, site):
                self.site = site

            def exchange_access_token_for_sts(self, access_token):
                assert access_token == "access-token"
                return OAuthStsCredentials("tmp-ak", "tmp-sk", "tmp-sts", 1798794000)

        def fake_browser_oauth_flow(site_type, oauth_client=None, cancel_event=None):
            assert site_type == "INTL"
            assert isinstance(oauth_client, FakeOAuthClient)
            assert cancel_event is not None
            return OAuthToken("access-token", "refresh-token", 1798790400, 1822320000)

        monkeypatch.setattr("iac_code.commands.auth._select", fake_select)
        monkeypatch.setattr(
            "iac_code.services.providers.aliyun_oauth.run_browser_oauth_flow",
            fake_browser_oauth_flow,
        )
        monkeypatch.setattr("iac_code.services.providers.aliyun_oauth.AliyunOAuthClient", FakeOAuthClient)
        monkeypatch.setattr(
            "iac_code.services.providers.aliyun.AliyunCredentials.save",
            lambda credential: saved.setdefault("credential", credential),
        )

        _aliyun_credential_flow()

        assert saved["credential"].region_id == "cn-shanghai"
        assert saved["credential"].oauth_site_type == "INTL"

    def test_aliyun_credential_flow_oauth_error_returns_message_without_saving(self, monkeypatch):
        from iac_code.services.providers.aliyun_oauth import AliyunOAuthError

        saved = {}

        monkeypatch.setattr(
            "iac_code.services.providers.aliyun.AliyunCredentials._load_from_iac_code_config",
            lambda: None,
        )
        monkeypatch.setattr(
            "iac_code.services.providers.aliyun.AliyunCredentials.load_from_aliyun_cli",
            lambda config_path=None: None,
        )

        def fake_select(title, options, default_index=0):
            if "credential type" in title.lower():
                return options.index("OAuth Login (Browser)")
            if "site type" in title.lower():
                return options.index("China")
            return None

        def fail_oauth(site_type, oauth_client=None, cancel_event=None):
            assert site_type == "CN"
            assert oauth_client is not None
            assert cancel_event is not None
            raise AliyunOAuthError("No available callback port")

        monkeypatch.setattr("iac_code.commands.auth._select", fake_select)
        monkeypatch.setattr("iac_code.services.providers.aliyun_oauth.run_browser_oauth_flow", fail_oauth)
        monkeypatch.setattr(
            "iac_code.services.providers.aliyun.AliyunCredentials.save",
            lambda credential: saved.setdefault("credential", credential),
        )

        result = _aliyun_credential_flow()

        assert result == "Alibaba Cloud OAuth login failed: No available callback port"
        assert saved == {}

    def test_aliyun_credential_flow_oauth_cancel_returns_to_mode_selection(self, monkeypatch):
        from iac_code.services.providers.aliyun_oauth import AliyunOAuthCancelledError

        monkeypatch.setattr(
            "iac_code.services.providers.aliyun.AliyunCredentials._load_from_iac_code_config",
            lambda: None,
        )
        monkeypatch.setattr(
            "iac_code.services.providers.aliyun.AliyunCredentials.load_from_aliyun_cli",
            lambda config_path=None: None,
        )

        selections = iter(["credential", "site", "cancel"])

        def fake_select(title, options, default_index=0):
            step = next(selections)
            if step == "credential":
                return options.index("OAuth Login (Browser)")
            if step == "site":
                return options.index("China")
            return None

        def cancel_oauth(site_type, oauth_client=None, cancel_event=None):
            assert site_type == "CN"
            assert cancel_event is not None
            raise AliyunOAuthCancelledError("OAuth login cancelled.")

        monkeypatch.setattr("iac_code.commands.auth._select", fake_select)
        monkeypatch.setattr("iac_code.services.providers.aliyun_oauth.run_browser_oauth_flow", cancel_oauth)

        assert _aliyun_credential_flow() is _BACK

    def test_oauth_escape_cancel_event_uses_cbreak_mode_to_preserve_output_newlines(self, monkeypatch):
        calls: list[tuple] = []

        class FakeStdin:
            def isatty(self):
                return True

            def fileno(self):
                return 42

        class FakeThread:
            def __init__(self, target, args, daemon=False):
                calls.append(("thread", target.__name__, daemon))

            def start(self):
                calls.append(("start",))

            def join(self, timeout=None):
                calls.append(("join", timeout))

        def fail_setraw(fd):
            raise AssertionError("OAuth Esc listener should not use raw mode because it breaks terminal newlines")

        monkeypatch.setattr("iac_code.commands.auth._IS_WIN32", False)
        monkeypatch.setattr("iac_code.commands.auth.sys.stdin", FakeStdin())
        monkeypatch.setattr("iac_code.commands.auth.threading.Thread", FakeThread)
        monkeypatch.setattr("termios.tcgetattr", lambda fd: calls.append(("tcgetattr", fd)) or "old-settings")
        monkeypatch.setattr("termios.tcsetattr", lambda fd, when, settings: calls.append(("tcsetattr", fd, settings)))
        monkeypatch.setattr("tty.setraw", fail_setraw)
        monkeypatch.setattr("tty.setcbreak", lambda fd: calls.append(("setcbreak", fd)))

        with _oauth_escape_cancel_event():
            calls.append(("body",))

        assert ("setcbreak", 42) in calls
        assert ("body",) in calls
        assert ("tcsetattr", 42, "old-settings") in calls


class TestAuthLlmSourceLock:
    def test_auth_flow_always_shows_category_selection(self, monkeypatch):
        """_auth_flow always shows category selection regardless of llm_source."""
        monkeypatch.setattr("iac_code.commands.auth._select", lambda title, options, default_index=0: None)
        result = _auth_flow(MagicMock(), MagicMock())
        assert "cancel" in result.lower()

    def test_auth_flow_llm_branch_works_with_qwenpaw_source(self, monkeypatch):
        """Even with qwenpaw source, user can access LLM config."""
        call_count = {"n": 0}

        def fake_select(title, options, default_index=0):
            call_count["n"] += 1
            if call_count["n"] == 1:
                return 0  # LLM branch
            return None

        monkeypatch.setattr("iac_code.commands.auth._select", fake_select)
        monkeypatch.setattr("iac_code.commands.auth._llm_auth_flow", lambda c, s: "llm-done")
        result = _auth_flow(MagicMock(), MagicMock())
        assert result == "llm-done"


class TestPartnerSourceInLlmFlow:
    def test_third_party_shown_at_top_when_partner_available(self, monkeypatch):
        """'Third-party' should appear as the first option when partners are available."""
        from iac_code.config import PartnerSource

        options_seen = []

        def fake_select(title, options, default_index=0):
            options_seen.extend(options)
            return None  # Esc

        monkeypatch.setattr("iac_code.commands.auth._select", fake_select)
        monkeypatch.setattr("iac_code.commands.auth.get_active_provider_key", lambda: None)
        monkeypatch.setattr(
            "iac_code.commands.auth.get_available_partner_sources",
            lambda: [PartnerSource(key="qwenpaw", display_name="QwenPaw")],
        )
        monkeypatch.setattr("iac_code.commands.auth.get_llm_source", lambda: "local")
        result = _llm_auth_flow(MagicMock(), MagicMock())
        assert result is _BACK
        assert len(options_seen) > 0
        assert "Third-party" in options_seen[0]

    def test_selecting_third_party_clears_active_provider(self, monkeypatch, iac_home):
        """Selecting Third-party → QwenPaw clears activeProvider and sets llm_source."""
        from iac_code.config import PartnerSource, _load_yaml

        settings_path = iac_home / ".iac-code" / "settings.yml"
        settings_path.parent.mkdir(parents=True, exist_ok=True)
        settings_path.write_text("activeProvider: dashscope\nproviders:\n  dashscope:\n    model: qwen-plus\n")

        monkeypatch.setattr("iac_code.commands.auth.get_settings_path", lambda: settings_path)

        def fake_select(title, options, default_index=0):
            return 0  # Select Third-party, then single partner auto-selects

        monkeypatch.setattr("iac_code.commands.auth._select", fake_select)
        monkeypatch.setattr("iac_code.commands.auth.get_active_provider_key", lambda: "dashscope")
        monkeypatch.setattr(
            "iac_code.commands.auth.get_available_partner_sources",
            lambda: [PartnerSource(key="qwenpaw", display_name="QwenPaw")],
        )
        monkeypatch.setattr("iac_code.commands.auth.get_llm_source", lambda: "local")
        monkeypatch.setattr(
            "iac_code.commands.auth.PARTNER_SOURCES", [PartnerSource(key="qwenpaw", display_name="QwenPaw")]
        )
        _llm_auth_flow(MagicMock(), MagicMock())

        config = _load_yaml(settings_path)
        assert "activeProvider" not in config
        assert config.get("llm_source") == "qwenpaw"
        # providers dict preserved
        assert "dashscope" in config.get("providers", {})

    def test_selecting_third_party_returns_configured_message(self, monkeypatch, iac_home):
        """Selecting Third-party → QwenPaw returns a success message."""
        from iac_code.config import PartnerSource

        settings_path = iac_home / ".iac-code" / "settings.yml"
        settings_path.parent.mkdir(parents=True, exist_ok=True)
        settings_path.write_text("")

        monkeypatch.setattr("iac_code.commands.auth.get_settings_path", lambda: settings_path)

        def fake_select(title, options, default_index=0):
            return 0  # Select Third-party, then single partner auto-selects

        monkeypatch.setattr("iac_code.commands.auth._select", fake_select)
        monkeypatch.setattr("iac_code.commands.auth.get_active_provider_key", lambda: None)
        monkeypatch.setattr(
            "iac_code.commands.auth.get_available_partner_sources",
            lambda: [PartnerSource(key="qwenpaw", display_name="QwenPaw")],
        )
        monkeypatch.setattr("iac_code.commands.auth.get_llm_source", lambda: "local")
        monkeypatch.setattr(
            "iac_code.commands.auth.PARTNER_SOURCES", [PartnerSource(key="qwenpaw", display_name="QwenPaw")]
        )
        result = _llm_auth_flow(MagicMock(), MagicMock())

        assert isinstance(result, str)
        assert "QwenPaw" in result
