"""Tests for the exit command."""

import pytest

from iac_code.commands.exit import exit_command
from iac_code.ui.repl import ExitREPLError


@pytest.mark.asyncio
async def test_exit_raises():
    with pytest.raises(ExitREPLError):
        await exit_command()


@pytest.mark.asyncio
async def test_exit_ignores_kwargs():
    with pytest.raises(ExitREPLError):
        await exit_command(context=None, store=None)
