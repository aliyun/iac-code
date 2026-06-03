"""Tests for the /skills command."""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from iac_code.commands.skills import skills_command


@pytest.mark.asyncio
async def test_skills_no_context_returns_message():
    result = await skills_command(context=None, args=[])

    assert "interactive" in result.lower()


@pytest.mark.asyncio
async def test_skills_cancel_does_not_save(monkeypatch):
    repl = MagicMock()
    repl.skill_management_items = []
    context = MagicMock(repl=repl)
    fake_picker = MagicMock()
    fake_picker.run.return_value = None
    monkeypatch.setattr("iac_code.ui.dialogs.skills_picker.SkillsPicker", MagicMock(return_value=fake_picker))
    save_mock = MagicMock()
    monkeypatch.setattr("iac_code.commands.skills.save_disabled_skills", save_mock)

    result = await skills_command(context=context, args=[])

    assert "cancel" in result.lower()
    save_mock.assert_not_called()


@pytest.mark.asyncio
async def test_skills_save_persists_and_refreshes(monkeypatch):
    repl = MagicMock()
    repl.skill_management_items = []
    repl.locked_skill_names = {"iac-aliyun"}
    context = MagicMock(repl=repl)
    fake_picker = MagicMock()
    fake_picker.run.return_value = {"team-review"}
    monkeypatch.setattr("iac_code.ui.dialogs.skills_picker.SkillsPicker", MagicMock(return_value=fake_picker))
    save_mock = MagicMock()
    monkeypatch.setattr("iac_code.commands.skills.save_disabled_skills", save_mock)

    result = await skills_command(context=context, args=[])

    assert "updated" in result.lower()
    save_mock.assert_called_once_with({"team-review"}, locked_skill_names={"iac-aliyun"})
    repl.refresh_skills.assert_called_once()
