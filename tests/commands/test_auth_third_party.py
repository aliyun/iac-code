"""Tests for third-party category in /auth LLM flow."""

from unittest.mock import MagicMock

from iac_code.commands.auth import _BACK, _llm_auth_flow
from iac_code.config import PartnerSource


class TestThirdPartyCategory:
    def test_third_party_shown_when_partner_available(self, monkeypatch):
        """When at least one partner source is available, 'Third-party' appears in group options."""
        options_seen = []

        def fake_select(title, options, default_index=0):
            options_seen.extend(options)
            return None  # Esc

        available = [PartnerSource(key="qwenpaw", display_name="QwenPaw")]
        monkeypatch.setattr("iac_code.commands.auth.get_available_partner_sources", lambda: available)
        monkeypatch.setattr("iac_code.commands.auth.get_llm_source", lambda: "local")
        monkeypatch.setattr("iac_code.commands.auth._select", fake_select)

        result = _llm_auth_flow(MagicMock(), MagicMock())
        assert result is _BACK
        assert any("Third-party" in opt for opt in options_seen)

    def test_third_party_hidden_when_no_partner_available(self, monkeypatch):
        """When no partner sources are available and llm_source is 'local', no 'Third-party' entry."""
        options_seen = []

        def fake_select(title, options, default_index=0):
            options_seen.extend(options)
            return None  # Esc

        monkeypatch.setattr("iac_code.commands.auth.get_available_partner_sources", lambda: [])
        monkeypatch.setattr("iac_code.commands.auth.get_llm_source", lambda: "local")
        monkeypatch.setattr("iac_code.commands.auth._select", fake_select)

        result = _llm_auth_flow(MagicMock(), MagicMock())
        assert result is _BACK
        assert not any("Third-party" in opt for opt in options_seen)

    def test_third_party_shown_when_llm_source_is_partner(self, monkeypatch):
        """When llm_source matches a partner key, 'Third-party' appears even if detection fails."""
        options_seen = []

        def fake_select(title, options, default_index=0):
            options_seen.extend(options)
            return None  # Esc

        monkeypatch.setattr("iac_code.commands.auth.get_available_partner_sources", lambda: [])
        monkeypatch.setattr("iac_code.commands.auth.get_llm_source", lambda: "qwenpaw")
        monkeypatch.setattr(
            "iac_code.commands.auth.PARTNER_SOURCES", [PartnerSource(key="qwenpaw", display_name="QwenPaw")]
        )
        monkeypatch.setattr("iac_code.commands.auth._select", fake_select)

        result = _llm_auth_flow(MagicMock(), MagicMock())
        assert result is _BACK
        assert any("Third-party" in opt for opt in options_seen)

    def test_third_party_marked_current_when_partner_active(self, monkeypatch):
        """When current llm_source is a partner key, 'Third-party' shows '(current)'."""
        options_seen = []

        def fake_select(title, options, default_index=0):
            options_seen.extend(options)
            return None

        monkeypatch.setattr(
            "iac_code.commands.auth.get_available_partner_sources",
            lambda: [PartnerSource(key="qwenpaw", display_name="QwenPaw")],
        )
        monkeypatch.setattr("iac_code.commands.auth.get_llm_source", lambda: "qwenpaw")
        monkeypatch.setattr(
            "iac_code.commands.auth.PARTNER_SOURCES", [PartnerSource(key="qwenpaw", display_name="QwenPaw")]
        )
        monkeypatch.setattr("iac_code.commands.auth._select", fake_select)

        result = _llm_auth_flow(MagicMock(), MagicMock())
        assert result is _BACK
        third_party_opt = [opt for opt in options_seen if "Third-party" in opt]
        assert len(third_party_opt) == 1
        assert "(current)" in third_party_opt[0]

    def test_selecting_third_party_enters_sub_flow(self, monkeypatch, tmp_path):
        """Selecting 'Third-party' enters the sub-flow and configures the partner."""
        monkeypatch.setenv("HOME", str(tmp_path))
        settings_path = tmp_path / ".iac-code" / "settings.yml"
        settings_path.parent.mkdir(parents=True, exist_ok=True)
        settings_path.write_text("", encoding="utf-8")

        monkeypatch.setattr("iac_code.commands.auth.get_settings_path", lambda: settings_path)
        monkeypatch.setattr(
            "iac_code.commands.auth.get_available_partner_sources",
            lambda: [PartnerSource(key="qwenpaw", display_name="QwenPaw")],
        )
        monkeypatch.setattr("iac_code.commands.auth.get_llm_source", lambda: "local")
        monkeypatch.setattr(
            "iac_code.commands.auth.PARTNER_SOURCES", [PartnerSource(key="qwenpaw", display_name="QwenPaw")]
        )

        def fake_select(title, options, default_index=0):
            return 0  # Select first option (Third-party, then the single partner)

        monkeypatch.setattr("iac_code.commands.auth._select", fake_select)
        monkeypatch.setattr("iac_code.commands.auth.get_active_provider_key", lambda: None)

        result = _llm_auth_flow(MagicMock(), MagicMock())
        assert isinstance(result, str)
        assert "QwenPaw" in result

    def test_back_from_third_party_sub_flow_returns_to_group(self, monkeypatch):
        """Pressing Esc in third-party sub-flow returns to group selection."""
        call_count = {"select": 0}

        def fake_select(title, options, default_index=0):
            call_count["select"] += 1
            if call_count["select"] == 1:
                return 0  # Select Third-party
            if call_count["select"] == 2:
                return None  # Esc in sub-flow
            return None  # Esc at group level

        monkeypatch.setattr(
            "iac_code.commands.auth.get_available_partner_sources",
            lambda: [
                PartnerSource(key="partner_a", display_name="PartnerA"),
                PartnerSource(key="partner_b", display_name="PartnerB"),
            ],
        )
        monkeypatch.setattr("iac_code.commands.auth.get_llm_source", lambda: "local")
        monkeypatch.setattr("iac_code.commands.auth._select", fake_select)

        result = _llm_auth_flow(MagicMock(), MagicMock())
        assert result is _BACK
        assert call_count["select"] == 3
