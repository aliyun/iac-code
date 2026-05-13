"""Tests for the /resume command."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from iac_code.commands.resume import resume_command
from iac_code.services.session_index import SessionEntry


def _entry(**overrides) -> SessionEntry:
    defaults = dict(
        session_id="abc-1",
        cwd="/proj/x",
        project_name="x",
        git_branch="main",
        title="hello",
        mtime=0.0,
        size_bytes=42,
    )
    defaults.update(overrides)
    return SessionEntry(**defaults)


@pytest.mark.asyncio
async def test_resume_no_context_returns_message():
    result = await resume_command(context=None, args=[])
    assert "interactive" in result.lower()


@pytest.mark.asyncio
async def test_resume_with_id_swaps_when_found():
    entry = _entry()
    repl = MagicMock()
    repl.session_index.find_by_id_or_prefix.return_value = entry
    repl.swap_or_announce_session = AsyncMock()
    context = MagicMock(repl=repl)

    result = await resume_command(context=context, args=["abc-1"])

    assert result == ""
    repl.session_index.find_by_id_or_prefix.assert_called_once_with("abc-1")
    repl.swap_or_announce_session.assert_awaited_once_with(entry)


@pytest.mark.asyncio
async def test_resume_with_id_not_found_returns_error():
    repl = MagicMock()
    repl.session_index.find_by_id_or_prefix.return_value = None
    context = MagicMock(repl=repl)

    result = await resume_command(context=context, args=["nope"])

    assert "not found" in result.lower()


@pytest.mark.asyncio
async def test_resume_no_args_invokes_picker(monkeypatch):
    entry = _entry()
    repl = MagicMock()
    repl._original_cwd = "/proj/x"
    repl.session_id = "current"
    repl.swap_or_announce_session = AsyncMock()
    context = MagicMock(repl=repl)

    fake_picker_instance = MagicMock()
    fake_picker_instance.run.return_value = entry
    fake_picker_cls = MagicMock(return_value=fake_picker_instance)
    monkeypatch.setattr("iac_code.ui.dialogs.resume_picker.ResumePicker", fake_picker_cls)

    result = await resume_command(context=context, args=[])

    assert result == ""
    fake_picker_cls.assert_called_once()
    repl.swap_or_announce_session.assert_awaited_once_with(entry)


@pytest.mark.asyncio
async def test_resume_picker_cancel_returns_cancel_message(monkeypatch):
    repl = MagicMock()
    repl._original_cwd = "/proj/x"
    repl.session_id = "current"
    repl.swap_or_announce_session = AsyncMock()
    context = MagicMock(repl=repl)

    fake_picker_instance = MagicMock()
    fake_picker_instance.run.return_value = None
    monkeypatch.setattr(
        "iac_code.ui.dialogs.resume_picker.ResumePicker",
        MagicMock(return_value=fake_picker_instance),
    )

    result = await resume_command(context=context, args=[])

    assert "cancel" in result.lower()
    repl.swap_or_announce_session.assert_not_awaited()
