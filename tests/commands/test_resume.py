"""Tests for the /resume command."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

import iac_code.commands.resume as resume_module
from iac_code.commands.resume import resume_command
from iac_code.services.session_index import SessionEntry
from iac_code.services.session_resolver import ResolutionStatus, SessionResolution


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
async def test_resume_with_id_swaps_when_found(monkeypatch):
    entry = _entry()
    repl = MagicMock()
    repl._original_cwd = "/proj/x"
    repl.swap_or_announce_session = AsyncMock()
    context = MagicMock(repl=repl)
    monkeypatch.setattr(
        resume_module,
        "resolve_session_argument",
        MagicMock(return_value=SessionResolution(status=ResolutionStatus.FOUND, entry=entry)),
        raising=False,
    )

    result = await resume_command(context=context, args=["abc-1"])

    assert result == ""
    repl.swap_or_announce_session.assert_awaited_once_with(entry)


@pytest.mark.asyncio
async def test_resume_with_name_uses_resolver_and_swaps_when_found(monkeypatch):
    entry = _entry(name="deploy-prod", title="deploy-prod")
    repl = MagicMock()
    repl._original_cwd = "/proj/x"
    repl.swap_or_announce_session = AsyncMock()
    context = MagicMock(repl=repl)

    resolve_session_argument = MagicMock(
        return_value=SessionResolution(status=ResolutionStatus.FOUND, entry=entry),
    )
    monkeypatch.setattr(resume_module, "resolve_session_argument", resolve_session_argument, raising=False)

    result = await resume_command(context=context, args=["deploy-prod"])

    assert result == ""
    resolve_session_argument.assert_called_once_with(repl.session_index, "/proj/x", "deploy-prod")
    repl.session_index.find_by_id_or_prefix.assert_not_called()
    repl.swap_or_announce_session.assert_awaited_once_with(entry)


@pytest.mark.asyncio
async def test_resume_with_id_not_found_returns_error(monkeypatch):
    repl = MagicMock()
    repl._original_cwd = "/proj/x"
    context = MagicMock(repl=repl)
    monkeypatch.setattr(
        resume_module,
        "resolve_session_argument",
        MagicMock(return_value=SessionResolution(status=ResolutionStatus.NOT_FOUND)),
        raising=False,
    )

    result = await resume_command(context=context, args=["nope"])

    assert "not found" in result.lower()
    repl.session_index.find_by_id_or_prefix.assert_not_called()


@pytest.mark.asyncio
async def test_resume_with_ambiguous_name_opens_picker_with_candidates_and_swaps(monkeypatch):
    selected = _entry(session_id="picked", name="deploy-prod", title="deploy-prod")
    candidates = [
        selected,
        _entry(session_id="other", cwd="/proj/y", project_name="y", name="deploy-prod", title="deploy-prod"),
    ]
    repl = MagicMock()
    repl._original_cwd = "/proj/x"
    repl.session_id = "current"
    repl._keybinding_manager = object()
    repl.renderer = object()
    repl.swap_or_announce_session = AsyncMock()
    context = MagicMock(repl=repl)
    monkeypatch.setattr(
        resume_module,
        "resolve_session_argument",
        MagicMock(return_value=SessionResolution(status=ResolutionStatus.AMBIGUOUS_NAME, candidates=candidates)),
        raising=False,
    )

    fake_picker_instance = MagicMock()
    fake_picker_instance.run.return_value = selected
    fake_picker_cls = MagicMock(return_value=fake_picker_instance)
    monkeypatch.setattr("iac_code.ui.dialogs.resume_picker.ResumePicker", fake_picker_cls)

    result = await resume_command(context=context, args=["deploy-prod"])

    assert result == ""
    fake_picker_cls.assert_called_once_with(
        index=repl.session_index,
        current_cwd="/proj/x",
        current_session_id="current",
        keybinding_manager=repl._keybinding_manager,
        renderer=repl.renderer,
        entries=candidates,
    )
    repl.swap_or_announce_session.assert_awaited_once_with(selected)


@pytest.mark.asyncio
async def test_resume_with_unknown_resolution_status_returns_error(monkeypatch):
    repl = MagicMock()
    repl._original_cwd = "/proj/x"
    repl.swap_or_announce_session = AsyncMock()
    context = MagicMock(repl=repl)
    resolution = MagicMock()
    resolution.status = "future-status"
    monkeypatch.setattr(
        resume_module,
        "resolve_session_argument",
        MagicMock(return_value=resolution),
        raising=False,
    )

    result = await resume_command(context=context, args=["deploy-prod"])

    assert result
    assert "unable" in result.lower()
    repl.swap_or_announce_session.assert_not_awaited()


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
