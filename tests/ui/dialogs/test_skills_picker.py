"""Tests for SkillsPicker dialog."""

from __future__ import annotations

from rich.console import Console

from iac_code.skills.management import SkillManagementItem
from iac_code.types.skill_source import SkillSource
from iac_code.ui.core.key_event import KeyEvent
from iac_code.ui.dialogs.skills_picker import SkillsPicker


def key(name: str, char: str | None = None, *, ctrl: bool = False) -> KeyEvent:
    return KeyEvent(key=name, char=name if char is None else char, ctrl=ctrl)


def _item(
    name: str,
    source: SkillSource,
    *,
    enabled: bool = True,
    locked: bool = False,
    size: int = 100,
    description: str | None = None,
) -> SkillManagementItem:
    return SkillManagementItem(
        name=name,
        description=description or f"{name} description",
        source=source,
        content_length=size,
        path=f"/repo/{name}",
        enabled=enabled,
        locked=locked,
    )


def _render_text(picker: SkillsPicker) -> str:
    console = Console(record=True, width=140)
    console.print(picker.render())
    return console.export_text()


def test_space_toggles_non_bundled_skill():
    picker = SkillsPicker([_item("team-review", SkillSource.PROJECT)])

    picker.handle_key(key(" ", " "))

    assert picker.disabled_skill_names == {"team-review"}


def test_space_does_not_toggle_locked_bundled_skill():
    picker = SkillsPicker([_item("iac-aliyun", SkillSource.BUNDLED, locked=True)])

    picker.handle_key(key(" ", " "))

    assert picker.disabled_skill_names == set()
    assert "cannot be disabled" in picker.status_message.lower()


def test_enter_returns_disabled_set():
    picker = SkillsPicker([_item("team-review", SkillSource.PROJECT)])
    picker.handle_key(key(" ", " "))

    picker.handle_key(key("enter", ""))

    assert picker.result == {"team-review"}
    assert picker.done is True


def test_escape_cancels():
    picker = SkillsPicker([_item("team-review", SkillSource.PROJECT)])

    picker.handle_key(key("escape", ""))

    assert picker.result is None
    assert picker.done is True


def test_search_filters_by_name_and_description():
    picker = SkillsPicker(
        [
            _item("team-review", SkillSource.PROJECT, description="review"),
            _item("deploy", SkillSource.USER, description="deploy"),
        ]
    )

    picker.handle_key(key("r", "r"))
    picker.handle_key(key("e", "e"))
    picker.handle_key(key("v", "v"))

    assert [item.name for item in picker.filtered_items] == ["team-review"]


def test_slash_is_search_text():
    picker = SkillsPicker(
        [
            _item("team/review", SkillSource.PROJECT),
            _item("deploy", SkillSource.USER),
        ]
    )

    picker.handle_key(key("/", "/"))

    assert [item.name for item in picker.filtered_items] == ["team/review"]


def test_t_is_search_text():
    picker = SkillsPicker(
        [
            _item("team-review", SkillSource.PROJECT, description="team"),
            _item("deploy", SkillSource.USER, description="deploy"),
        ]
    )

    picker.handle_key(key("t", "t"))

    assert [item.name for item in picker.filtered_items] == ["team-review"]
    assert picker.sort_mode == "name"


def test_description_only_match_is_labeled():
    picker = SkillsPicker(
        [
            _item("iac-aliyun", SkillSource.BUNDLED, description="Terraform template", locked=True),
        ]
    )

    picker.handle_key(key("t", "t"))

    assert "matched description" in _render_text(picker)


def test_name_match_does_not_show_description_label():
    picker = SkillsPicker(
        [
            _item("team-review", SkillSource.PROJECT, description="Terraform template"),
        ]
    )

    picker.handle_key(key("t", "t"))

    assert "matched description" not in _render_text(picker)


def test_tab_cycles_sort_by_source_then_size():
    picker = SkillsPicker(
        [
            _item("zeta", SkillSource.USER, size=400),
            _item("alpha", SkillSource.PROJECT, size=800),
            _item("bundled", SkillSource.BUNDLED, locked=True, size=100),
        ]
    )
    assert [item.name for item in picker.filtered_items] == ["alpha", "bundled", "zeta"]

    picker.handle_key(key("tab", "\t"))
    assert [item.name for item in picker.filtered_items] == ["bundled", "alpha", "zeta"]

    picker.handle_key(key("tab", "\t"))
    assert [item.name for item in picker.filtered_items] == ["bundled", "zeta", "alpha"]
