from __future__ import annotations

from pathlib import Path

from iac_code.ui.core.key_event import KeyEvent
from iac_code.ui.dialogs.memory import MemoryDialog


def test_memory_dialog_renders_auto_memory_with_blank_line_before_options(tmp_path):
    dialog = MemoryDialog(
        project_path=tmp_path / "IAC-CODE.md",
        user_path=Path("~/.iac-code/IAC-CODE.md"),
        auto_memory_dir=tmp_path / "memory",
        auto_memory_enabled=True,
    )

    lines = dialog.render_lines()

    assert lines[0] == "  Memory"
    auto_index = lines.index("  ❯ Auto-memory: on")
    project_index = lines.index("    1. Project memory           Saved in {}".format(tmp_path / "IAC-CODE.md"))
    assert lines[auto_index + 1] == ""
    assert project_index == auto_index + 2


def test_memory_dialog_enter_toggles_auto_memory_when_focused(tmp_path):
    toggled: list[bool] = []
    dialog = MemoryDialog(
        project_path=tmp_path / "IAC-CODE.md",
        user_path=Path("~/.iac-code/IAC-CODE.md"),
        auto_memory_dir=tmp_path / "memory",
        auto_memory_enabled=True,
        on_toggle=toggled.append,
    )

    consumed = dialog.handle_key(KeyEvent("enter", "\n"))

    assert consumed is True
    assert dialog.auto_memory_enabled is False
    assert toggled == [False]
    assert dialog.result is None


def test_memory_dialog_hides_auto_memory_folder_when_disabled(tmp_path):
    dialog = MemoryDialog(
        project_path=tmp_path / "IAC-CODE.md",
        user_path=Path("~/.iac-code/IAC-CODE.md"),
        auto_memory_dir=tmp_path / "memory",
        auto_memory_enabled=False,
    )

    rendered = "\n".join(dialog.render_lines())

    assert "Auto-memory: off" in rendered
    assert "Open auto-memory folder" not in rendered


def test_memory_dialog_selects_project_after_moving_down_from_toggle(tmp_path):
    dialog = MemoryDialog(
        project_path=tmp_path / "IAC-CODE.md",
        user_path=Path("~/.iac-code/IAC-CODE.md"),
        auto_memory_dir=tmp_path / "memory",
        auto_memory_enabled=True,
    )

    dialog.handle_key(KeyEvent("down", ""))
    dialog.handle_key(KeyEvent("enter", "\n"))

    assert dialog.result == "project"


def test_memory_dialog_shows_folder_when_enabled_and_selects_it_as_third_option(tmp_path):
    dialog = MemoryDialog(
        project_path=tmp_path / "IAC-CODE.md",
        user_path=Path("~/.iac-code/IAC-CODE.md"),
        auto_memory_dir=tmp_path / "memory",
        auto_memory_enabled=True,
    )

    rendered = "\n".join(dialog.render_lines())
    dialog.handle_key(KeyEvent("down", ""))
    dialog.handle_key(KeyEvent("down", ""))
    dialog.handle_key(KeyEvent("down", ""))
    dialog.handle_key(KeyEvent("enter", "\n"))

    assert "3. Open auto-memory folder" in rendered
    assert dialog.result == "folder"


def test_memory_dialog_can_initially_focus_folder_action(tmp_path):
    dialog = MemoryDialog(
        project_path=tmp_path / "IAC-CODE.md",
        user_path=Path("~/.iac-code/IAC-CODE.md"),
        auto_memory_dir=tmp_path / "memory",
        auto_memory_enabled=True,
        initial_focus_action="folder",
    )

    lines = dialog.render_lines()

    assert any(line.startswith("  ❯ 3. Open auto-memory folder") for line in lines)
