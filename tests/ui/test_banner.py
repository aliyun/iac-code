"""Tests for src/iac_code/ui/banner.py."""

from __future__ import annotations

import getpass
import shlex
from io import StringIO
from pathlib import Path
from typing import Any
from unittest.mock import patch

from rich.console import Console
from rich.panel import Panel


def make_console(width: int = 80) -> Console:
    return Console(
        file=StringIO(),
        width=width,
        force_terminal=True,
        color_system=None,
        legacy_windows=False,
        _environ={},
    )


def render_to_str(panel: Any, width: int = 80) -> str:
    console = make_console(width=width)
    console.print(panel)
    return console.file.getvalue()  # type: ignore[attr-defined]


# ---------------------------------------------------------------------------
# _get_provider_display
# ---------------------------------------------------------------------------


class TestGetProviderDisplay:
    """Tests for _get_provider_display()."""

    def _call(self):
        from iac_code.ui.banner import _get_provider_display

        return _get_provider_display()

    def test_no_active_key_returns_empty(self):
        with patch("iac_code.config.get_active_provider_key", return_value=None):
            result = self._call()
        assert result == ""

    def test_empty_active_key_returns_empty(self):
        with patch("iac_code.config.get_active_provider_key", return_value=""):
            result = self._call()
        assert result == ""

    def test_known_name_anthropic(self):
        with (
            patch("iac_code.config.get_active_provider_key", return_value="my-key"),
            patch(
                "iac_code.config.get_provider_config",
                return_value={"name": "Anthropic"},
            ),
        ):
            result = self._call()
        # Should pass through (possibly translated, but at minimum non-empty)
        assert result != ""
        # The key "Anthropic" is looked up in provider_display_names; translation
        # of the English string "Anthropic" is at minimum "Anthropic".
        assert "Anthropic" in result

    def test_known_name_openai(self):
        with (
            patch("iac_code.config.get_active_provider_key", return_value="k"),
            patch(
                "iac_code.config.get_provider_config",
                return_value={"name": "OpenAI"},
            ),
        ):
            result = self._call()
        assert result != ""

    def test_known_name_dashscope(self):
        with (
            patch("iac_code.config.get_active_provider_key", return_value="k"),
            patch(
                "iac_code.config.get_provider_config",
                return_value={"name": "DashScope"},
            ),
        ):
            result = self._call()
        assert result != ""

    def test_known_name_openapi_compatible(self):
        with (
            patch("iac_code.config.get_active_provider_key", return_value="k"),
            patch(
                "iac_code.config.get_provider_config",
                return_value={"name": "OpenAPI Compatible"},
            ),
        ):
            result = self._call()
        assert result != ""

    def test_unknown_name_passthrough(self):
        with (
            patch("iac_code.config.get_active_provider_key", return_value="k"),
            patch(
                "iac_code.config.get_provider_config",
                return_value={"name": "MyCustomProvider"},
            ),
        ):
            result = self._call()
        assert result == "MyCustomProvider"

    def test_exception_returns_empty(self):
        with patch(
            "iac_code.config.get_active_provider_key",
            side_effect=RuntimeError("boom"),
        ):
            result = self._call()
        assert result == ""

    def test_get_provider_config_raises_returns_empty(self):
        with (
            patch("iac_code.config.get_active_provider_key", return_value="k"),
            patch(
                "iac_code.config.get_provider_config",
                side_effect=Exception("fail"),
            ),
        ):
            result = self._call()
        assert result == ""

    def test_dashscope_token_plan_display_from_registry(self):
        with patch("iac_code.config.get_active_provider_key", return_value="dashscope_token_plan"):
            from iac_code.ui import banner

            result = banner._get_provider_display()
        assert result == "Alibaba Cloud Bailian Token Plan"

    def test_qwenpaw_source_shows_real_provider(self):
        """When llm_source is qwenpaw, display includes real provider name."""
        from unittest.mock import MagicMock

        mock_config = MagicMock()
        mock_config.provider_key = "dashscope"

        with (
            patch("iac_code.config.get_active_provider_key", return_value=None),
            patch("iac_code.config.get_llm_source", return_value="qwenpaw"),
            patch("iac_code.services.qwenpaw_source.load_from_qwenpaw", return_value=mock_config),
        ):
            result = self._call()
        assert "QwenPaw" in result
        assert "Alibaba Cloud Bailian" in result

    def test_qwenpaw_source_fallback_on_error(self):
        """When QwenPaw config fails, display falls back to just 'QwenPaw'."""
        with (
            patch("iac_code.config.get_active_provider_key", return_value=None),
            patch("iac_code.config.get_llm_source", return_value="qwenpaw"),
            patch("iac_code.services.qwenpaw_source.load_from_qwenpaw", side_effect=RuntimeError("fail")),
        ):
            result = self._call()
        assert result == "QwenPaw"

    def test_qwenpaw_source_no_config_fallback(self):
        """When QwenPaw returns None config, display falls back to just 'QwenPaw'."""
        with (
            patch("iac_code.config.get_active_provider_key", return_value=None),
            patch("iac_code.config.get_llm_source", return_value="qwenpaw"),
            patch("iac_code.services.qwenpaw_source.load_from_qwenpaw", return_value=None),
        ):
            result = self._call()
        assert result == "QwenPaw"


# ---------------------------------------------------------------------------
# render_welcome_banner
# ---------------------------------------------------------------------------


class TestRenderWelcomeBanner:
    """Tests for render_welcome_banner(model, cwd)."""

    def _call(self, model: str, cwd: str) -> Panel:
        from iac_code.ui.banner import render_welcome_banner

        return render_welcome_banner(model, cwd)

    # ------------------------------------------------------------------
    # Return-type tests
    # ------------------------------------------------------------------

    def test_returns_panel(self, tmp_path):
        result = self._call("claude-3-5-sonnet", str(tmp_path))
        assert isinstance(result, Panel)

    # ------------------------------------------------------------------
    # cwd display: inside HOME → ~/... prefix
    # ------------------------------------------------------------------

    def test_cwd_inside_home(self):
        home = Path.home()
        cwd = str(home / "projects" / "my-app")
        panel = self._call("some-model", cwd)
        text = render_to_str(panel)
        assert "~/projects/my-app" in text

    def test_cwd_outside_home(self, tmp_path):
        # tmp_path is typically under /tmp which is not inside $HOME
        cwd = str(tmp_path)
        panel = self._call("some-model", cwd)
        text = render_to_str(panel, width=200)
        # Absolute path should appear (may differ by OS symlink resolution)
        resolved = str(tmp_path.resolve())
        assert resolved.replace("/", "") in text.replace("/", "").replace("\n", "")

    # ------------------------------------------------------------------
    # model / provider display
    # ------------------------------------------------------------------

    def test_model_without_provider(self):
        """When no active provider, model string appears as-is."""
        with patch("iac_code.config.get_active_provider_key", return_value=None):
            panel = self._call("claude-3-5-sonnet", str(Path.home()))
        text = render_to_str(panel)
        assert "claude-3-5-sonnet" in text

    def test_model_with_provider(self):
        """When provider is active, display is 'Provider / model'."""
        with (
            patch("iac_code.config.get_active_provider_key", return_value="k"),
            patch(
                "iac_code.config.get_provider_config",
                return_value={"name": "Anthropic"},
            ),
        ):
            panel = self._call("claude-3-5-sonnet", str(Path.home()))
        text = render_to_str(panel)
        assert "claude-3-5-sonnet" in text
        assert "Anthropic" in text
        assert "/" in text

    def test_empty_model_no_model_line(self):
        """Empty model string: no model text is rendered (empty Text())."""
        with patch("iac_code.config.get_active_provider_key", return_value=None):
            panel = self._call("", str(Path.home()))
        # Just ensure it renders without error and returns a Panel
        assert isinstance(panel, Panel)
        text = render_to_str(panel)
        # No slash for provider/model combo
        assert " / " not in text

    # ------------------------------------------------------------------
    # Username display
    # ------------------------------------------------------------------

    def test_welcome_username_present(self):
        real_user = getpass.getuser()
        panel = self._call("model", str(Path.home()))
        text = render_to_str(panel)
        # Either capitalised first letter or original username should appear
        assert real_user[0].upper() + real_user[1:] in text or real_user in text

    def test_non_existent_user_fallback(self):
        """When getpass.getuser() raises, username falls back to 'User'."""
        with patch("getpass.getuser", side_effect=Exception("no user")):
            panel = self._call("model", str(Path.home()))
        text = render_to_str(panel)
        assert "User" in text

    # ------------------------------------------------------------------
    # Banner structure / logo content
    # ------------------------------------------------------------------

    def test_banner_contains_welcome_text(self):
        panel = self._call("model", str(Path.home()))
        text = render_to_str(panel)
        # "Welcome back" (translated, but at minimum contains those words
        # if locale is English)
        assert "Welcome back" in text or "!" in text

    def test_banner_contains_logo_chars(self):
        """Logo block characters should appear in the rendered output."""
        panel = self._call("model", str(Path.home()))
        text = render_to_str(panel)
        # At least one logo line contains '▄' or '█'
        assert "▄" in text or "█" in text

    def test_panel_border_style(self):
        """Panel border_style should be 'bright_cyan'."""
        from iac_code.ui.banner import ACCENT

        panel = self._call("model", str(Path.home()))
        assert panel.border_style == ACCENT
        assert ACCENT == "bright_cyan"

    def test_banner_renders_dashscope_token_plan_provider(self):
        with (
            patch("iac_code.config.get_active_provider_key", return_value="k"),
            patch(
                "iac_code.config.get_provider_config",
                return_value={"name": "DashScope Token Plan"},
            ),
        ):
            panel = self._call("qwen3.6-plus", str(Path.home()))
        text = render_to_str(panel, width=200)
        assert "qwen3.6-plus" in text
        # Source or translated form — either proves the dict lookup hit.
        assert "DashScope Token Plan" in text or "百炼" in text


# ---------------------------------------------------------------------------
# update prompt rendering
# ---------------------------------------------------------------------------


class TestUpdatePromptRendering:
    """Tests for update prompt and skipped-update notice rendering."""

    def _pending_update(self, update_command: tuple[str, ...]):
        from iac_code.services.update_checker import PendingUpdate

        return PendingUpdate(
            version="0.4.0",
            current_version="0.3.0",
            source="official_pypi",
            checked_at=100.0,
            update_command=update_command,
            release_notes_url="https://github.com/aliyun/iac-code/releases/latest",
        )

    def test_prompt_header_contains_versions_command_and_release_notes(self):
        from iac_code.ui.banner import render_update_prompt_header

        update = self._pending_update(("/python", "-m", "pip", "install", "--upgrade", "iac-code"))
        command_text = shlex.join(update.update_command)

        text = render_to_str(render_update_prompt_header(update), width=120)

        assert "Update available" in text
        assert update.current_version in text
        assert update.version in text
        assert command_text in text
        assert update.release_notes_url in text

    def test_notice_contains_update_notice_command_and_release_notes(self):
        from iac_code.ui.banner import render_update_notice

        update = self._pending_update(("/python", "-m", "pip", "install", "--upgrade", "iac-code"))
        command_text = shlex.join(update.update_command)

        text = render_to_str(render_update_notice(update), width=120)

        assert "Update available" in text
        assert command_text in text
        assert update.release_notes_url in text

    def test_prompt_and_notice_quote_command_parts_with_spaces(self):
        from iac_code.ui.banner import render_update_notice, render_update_prompt_header

        update = self._pending_update(("/path with spaces/python", "-m", "pip", "install", "--upgrade", "iac-code"))
        command_text = shlex.join(update.update_command)

        prompt_text = render_to_str(render_update_prompt_header(update), width=140)
        notice_text = render_to_str(render_update_notice(update), width=140)

        assert command_text == "'/path with spaces/python' -m pip install --upgrade iac-code"
        assert command_text in prompt_text
        assert command_text in notice_text
