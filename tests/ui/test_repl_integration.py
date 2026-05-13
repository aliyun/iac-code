"""Tests for InlineREPL integration with ProviderManager."""

from __future__ import annotations

import re
from unittest.mock import patch


class TestREPLProviderIntegration:
    @patch("iac_code.ui.repl.ProviderManager")
    @patch("iac_code.ui.repl.SessionStorage")
    @patch("iac_code.ui.repl.MemoryManager")
    def test_init_creates_provider_manager(self, mock_mm, mock_ss, mock_pm):
        from iac_code.ui.repl import InlineREPL

        repl = InlineREPL(model="claude-sonnet-4-6")
        assert hasattr(repl, "_provider_manager")

    @patch("iac_code.ui.repl.ProviderManager")
    @patch("iac_code.ui.repl.SessionStorage")
    @patch("iac_code.ui.repl.MemoryManager")
    def test_init_creates_task_manager(self, mock_mm, mock_ss, mock_pm):
        from iac_code.ui.repl import InlineREPL

        repl = InlineREPL(model="claude-sonnet-4-6")
        assert hasattr(repl, "_task_manager")

    @patch("iac_code.ui.repl.ProviderManager")
    @patch("iac_code.ui.repl.SessionStorage")
    @patch("iac_code.ui.repl.MemoryManager")
    def test_agent_tool_registered(self, mock_mm, mock_ss, mock_pm):
        from iac_code.ui.repl import InlineREPL

        repl = InlineREPL(model="claude-sonnet-4-6")
        assert repl.tool_registry.get("agent") is not None

    @patch("iac_code.ui.repl.ProviderManager")
    @patch("iac_code.ui.repl.SessionStorage")
    @patch("iac_code.ui.repl.MemoryManager")
    def test_memory_tools_registered(self, mock_mm, mock_ss, mock_pm):
        from iac_code.ui.repl import InlineREPL

        repl = InlineREPL(model="claude-sonnet-4-6")
        assert repl.tool_registry.get("read_memory") is not None
        assert repl.tool_registry.get("write_memory") is not None

    @patch("iac_code.ui.repl.ProviderManager")
    @patch("iac_code.ui.repl.SessionStorage")
    @patch("iac_code.ui.repl.MemoryManager")
    def test_task_tools_registered(self, mock_mm, mock_ss, mock_pm):
        from iac_code.ui.repl import InlineREPL

        repl = InlineREPL(model="claude-sonnet-4-6")
        assert repl.tool_registry.get("task_list") is not None
        assert repl.tool_registry.get("task_stop") is not None


UUID4_RE = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$")


@patch("iac_code.ui.repl.ProviderManager")
@patch("iac_code.ui.repl.SessionStorage")
@patch("iac_code.ui.repl.MemoryManager")
def test_new_session_id_is_full_uuid(mock_mm, mock_ss, mock_pm):
    from iac_code.ui.repl import InlineREPL

    repl = InlineREPL(model="test-model")
    assert UUID4_RE.match(repl.session_id), f"expected UUID4, got {repl.session_id!r}"


@patch("iac_code.ui.repl.ProviderManager")
@patch("iac_code.ui.repl.SessionStorage")
@patch("iac_code.ui.repl.MemoryManager")
def test_resume_str_accepted_when_session_exists(mock_mm, mock_ss, mock_pm):
    from iac_code.ui.repl import InlineREPL

    existing_id = "99646984-35a9-4850-b72a-4131a1690774"
    mock_ss.return_value.exists.return_value = True
    mock_ss.return_value.load.return_value = []
    mock_ss.return_value.repair_interrupted.return_value = []
    repl = InlineREPL(model="test-model", resume_session_id=existing_id)
    assert repl.session_id == existing_id


@patch("iac_code.ui.repl.ProviderManager")
@patch("iac_code.ui.repl.SessionStorage")
@patch("iac_code.ui.repl.MemoryManager")
def test_resume_str_raises_when_session_missing(mock_mm, mock_ss, mock_pm):
    from iac_code.ui.repl import InlineREPL

    mock_ss.return_value.exists.return_value = False
    mock_ss.return_value.find_session_anywhere.return_value = None
    import pytest

    with pytest.raises(ValueError, match="Session not found"):
        InlineREPL(model="test-model", resume_session_id="no-such-id")


@patch("iac_code.ui.repl.ProviderManager")
@patch("iac_code.ui.repl.SessionStorage")
@patch("iac_code.ui.repl.MemoryManager")
def test_resume_str_cross_project_raises_with_hint(mock_mm, mock_ss, mock_pm, tmp_path):
    """A resume id resolved in a different project must surface the cd command."""
    from iac_code.ui.repl import InlineREPL

    mock_ss.return_value.exists.return_value = False
    mock_ss.return_value.find_session_anywhere.return_value = (
        "/elsewhere/repo",
        tmp_path / "fake.jsonl",
    )
    import pytest

    with pytest.raises(ValueError, match=r"cd /elsewhere/repo && iac-code --resume"):
        InlineREPL(model="test-model", resume_session_id="some-id")
