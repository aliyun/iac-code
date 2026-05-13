"""Tests for the help command."""

from unittest.mock import MagicMock

import pytest

from iac_code.commands.help import help_command


@pytest.mark.asyncio
async def test_help_renders_commands_and_shortcuts():
    cmd1 = MagicMock()
    cmd1.name = "clear"
    cmd1.description = "Clear the screen"
    cmd2 = MagicMock()
    cmd2.name = "help"
    cmd2.description = "Show help"

    registry = MagicMock()
    registry.get_all.return_value = [cmd1, cmd2]

    console = MagicMock()
    context = MagicMock(console=console)

    result = await help_command(registry=registry, context=context)
    assert result is None
    console.print.assert_called_once()
    # Inspect the printed Text object
    text_arg = console.print.call_args.args[0]
    rendered = text_arg.plain
    assert "iac-code" in rendered
    assert "clear" in rendered
    assert "Clear the screen" in rendered
    assert "Enter" in rendered  # shortcut
