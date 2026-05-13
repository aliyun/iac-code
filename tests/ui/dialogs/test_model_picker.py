"""Tests for ModelPicker dialog."""

from __future__ import annotations

from unittest.mock import patch

from iac_code.ui.core.key_event import KeyEvent
from iac_code.ui.dialogs.model_picker import (
    _EFFORT_ORDER,
    EFFORT_SYMBOLS,
    EffortLevel,
    ModelPicker,
)


def key(k: str, ctrl: bool = False) -> KeyEvent:
    return KeyEvent(key=k, char=k, ctrl=ctrl)


# ---------------------------------------------------------------------------
# EffortLevel
# ---------------------------------------------------------------------------


class TestEffortLevel:
    def test_values(self):
        assert EffortLevel.LOW.value == "low"
        assert EffortLevel.MEDIUM.value == "medium"
        assert EffortLevel.HIGH.value == "high"
        assert EffortLevel.XHIGH.value == "xhigh"
        assert EffortLevel.MAX.value == "max"
        assert EffortLevel.AUTO.value == "auto"

    def test_effort_order(self):
        assert _EFFORT_ORDER == [
            EffortLevel.LOW,
            EffortLevel.MEDIUM,
            EffortLevel.HIGH,
            EffortLevel.XHIGH,
            EffortLevel.MAX,
            EffortLevel.AUTO,
        ]

    def test_effort_symbols(self):
        assert EFFORT_SYMBOLS[EffortLevel.LOW] == "◆"
        assert EFFORT_SYMBOLS[EffortLevel.MEDIUM] == "◆◆"
        assert EFFORT_SYMBOLS[EffortLevel.HIGH] == "◆◆◆"
        assert EFFORT_SYMBOLS[EffortLevel.XHIGH] == "◆◆◆◆"
        assert EFFORT_SYMBOLS[EffortLevel.MAX] == "◆◆◆◆◆"
        assert EFFORT_SYMBOLS[EffortLevel.AUTO] == "◆"


# ---------------------------------------------------------------------------
# ModelPicker — construction and items
# ---------------------------------------------------------------------------


def make_picker(
    initial_model="qwen3.6-plus",
    configured_providers=None,
    on_select=None,
    on_cancel=None,
):
    if configured_providers is None:
        configured_providers = ["dashscope", "openai", "anthropic"]
    if on_select is None:

        def on_select(m, e):
            return None

    if on_cancel is None:

        def on_cancel():
            return None

    return ModelPicker(
        initial_model=initial_model,
        configured_providers=configured_providers,
        on_select=on_select,
        on_cancel=on_cancel,
    )


class TestModelPickerCreate:
    def test_create(self):
        picker = make_picker()
        assert picker is not None

    def test_items_contain_headers_and_models(self):
        picker = make_picker(configured_providers=["dashscope"])
        items = picker._build_items()
        # Should have at least one header
        headers = [it for it in items if "header" in it]
        models = [it for it in items if "model" in it]
        assert len(headers) >= 1
        assert len(models) >= 1

    def test_only_configured_providers_shown_dashscope(self):
        picker = make_picker(configured_providers=["dashscope"])
        items = picker._build_items()
        models = [it["model"] for it in items if "model" in it]
        # Only qwen/qwq models should appear
        assert "qwen3.6-plus" in models
        assert "qwen3.5-plus" in models
        # No OpenAI or Anthropic
        assert "gpt-5.5" not in models
        assert "claude-sonnet-4-6" not in models

    def test_only_configured_providers_shown_openai(self):
        picker = make_picker(configured_providers=["openai"])
        items = picker._build_items()
        models = [it["model"] for it in items if "model" in it]
        assert "gpt-5.5" in models
        assert "gpt-5.4" in models
        assert "qwen3.6-plus" not in models
        assert "claude-sonnet-4-6" not in models

    def test_only_configured_providers_shown_anthropic(self):
        picker = make_picker(configured_providers=["anthropic"])
        items = picker._build_items()
        models = [it["model"] for it in items if "model" in it]
        assert "claude-sonnet-4-6" in models
        assert "qwen3.6-plus" not in models
        assert "gpt-5.5" not in models

    def test_all_providers_shown_when_all_configured(self):
        picker = make_picker(configured_providers=["dashscope", "openai", "anthropic"])
        items = picker._build_items()
        models = [it["model"] for it in items if "model" in it]
        assert "qwen3.6-plus" in models
        assert "gpt-5.5" in models
        assert "claude-sonnet-4-6" in models

    def test_no_providers_shows_nothing(self):
        picker = make_picker(configured_providers=[])
        items = picker._build_items()
        assert items == []

    def test_header_display_name(self):
        picker = make_picker(configured_providers=["dashscope"])
        items = picker._build_items()
        headers = [it["header"] for it in items if "header" in it]
        assert any("阿里云百炼" in h for h in headers)

    def test_items_carry_provider_key(self):
        picker = make_picker(configured_providers=["dashscope", "openai"])
        items = picker._build_items()
        for item in items:
            if "model" in item:
                assert "provider_key" in item
        openai_models = [it for it in items if "model" in it and it.get("provider_key") == "openai"]
        assert any(it["model"] == "gpt-5.5" for it in openai_models)
        dashscope_models = [it for it in items if "model" in it and it.get("provider_key") == "dashscope"]
        assert any(it["model"] == "qwen3.6-plus" for it in dashscope_models)


# ---------------------------------------------------------------------------
# ModelPicker — effort cycling
# ---------------------------------------------------------------------------


class TestModelPickerEffortCycle:
    def test_default_effort_initialized_from_capability(self):
        picker = make_picker(
            initial_model="gpt-5.5",
            configured_providers=["openai", "anthropic"],
        )
        assert picker._efforts[("openai", "gpt-5.5")] == EffortLevel.HIGH
        assert picker._efforts[("anthropic", "claude-opus-4-7")] == EffortLevel.HIGH

    def test_effort_cycle_up_from_high(self):
        picker = make_picker(configured_providers=["openai"])
        picker._efforts[("openai", "gpt-5.5")] = EffortLevel.HIGH
        picker._cycle_effort(("openai", "gpt-5.5"), 1)
        assert picker._efforts[("openai", "gpt-5.5")] == EffortLevel.XHIGH

    def test_effort_cycle_up_clamps_at_range_max(self):
        picker = make_picker(configured_providers=["openai"])
        picker._efforts[("openai", "gpt-5.5")] = EffortLevel.XHIGH
        picker._cycle_effort(("openai", "gpt-5.5"), 1)
        # gpt-5.5 range max is XHIGH, should clamp
        assert picker._efforts[("openai", "gpt-5.5")] == EffortLevel.XHIGH

    def test_effort_cycle_down_from_high(self):
        picker = make_picker(configured_providers=["openai"])
        picker._efforts[("openai", "gpt-5.5")] = EffortLevel.HIGH
        picker._cycle_effort(("openai", "gpt-5.5"), -1)
        assert picker._efforts[("openai", "gpt-5.5")] == EffortLevel.MEDIUM

    def test_effort_cycle_down_clamps_at_range_min(self):
        picker = make_picker(configured_providers=["openai"])
        picker._efforts[("openai", "gpt-5.5")] = EffortLevel.LOW
        picker._cycle_effort(("openai", "gpt-5.5"), -1)
        assert picker._efforts[("openai", "gpt-5.5")] == EffortLevel.LOW

    def test_effort_cycle_no_op_for_non_effort_model(self):
        picker = make_picker(configured_providers=["dashscope"])
        # qwen3.6-plus does not support effort
        initial = picker._efforts.get(("dashscope", "qwen3.6-plus"))
        picker._cycle_effort(("dashscope", "qwen3.6-plus"), 1)
        assert picker._efforts.get(("dashscope", "qwen3.6-plus")) == initial

    def test_deepseek_effort_cycle_skips_xhigh(self):
        """DeepSeek V4 only accepts high/max — cycling must skip xhigh."""
        picker = make_picker(configured_providers=["deepseek"])
        picker._efforts[("deepseek", "deepseek-v4-pro")] = EffortLevel.HIGH
        # Clamps at HIGH going down
        picker._cycle_effort(("deepseek", "deepseek-v4-pro"), -1)
        assert picker._efforts[("deepseek", "deepseek-v4-pro")] == EffortLevel.HIGH
        # Cycle up: HIGH → MAX (skipping XHIGH)
        picker._cycle_effort(("deepseek", "deepseek-v4-pro"), 1)
        assert picker._efforts[("deepseek", "deepseek-v4-pro")] == EffortLevel.MAX
        # Clamps at MAX going up
        picker._cycle_effort(("deepseek", "deepseek-v4-pro"), 1)
        assert picker._efforts[("deepseek", "deepseek-v4-pro")] == EffortLevel.MAX


# ---------------------------------------------------------------------------
# ModelPicker — key navigation
# ---------------------------------------------------------------------------


class TestModelPickerNavigation:
    def test_initial_focus_on_initial_model(self):
        picker = make_picker(
            initial_model="qwen3.6-plus",
            configured_providers=["dashscope"],
        )
        items = picker._build_items()
        focused_item = items[picker._focused_index]
        assert focused_item.get("model") == "qwen3.6-plus"

    def test_down_skips_header(self):
        picker = make_picker(configured_providers=["dashscope", "openai"])
        items = picker._build_items()
        dashscope_model_names = [
            "qwen3.6-plus",
            "qwen3.5-plus",
            "qwen3.5-flash",
            "kimi-k2.6",
            "glm-5.1",
        ]
        dashscope_models = [i for i, it in enumerate(items) if "model" in it and it["model"] in dashscope_model_names]
        last_dashscope_idx = dashscope_models[-1]
        picker._focused_index = last_dashscope_idx
        picker.handle_key(key("down"))
        new_item = items[picker._focused_index]
        assert "model" in new_item

    def test_up_skips_header(self):
        picker = make_picker(configured_providers=["dashscope", "openai"])
        items = picker._build_items()
        openai_model_names = [
            "gpt-5.5",
            "gpt-5.4",
            "gpt-5.4-mini",
            "gpt-5.3-codex",
            "gpt-5.2",
        ]
        openai_models_idx = [i for i, it in enumerate(items) if "model" in it and it["model"] in openai_model_names]
        first_openai_idx = openai_models_idx[0]
        picker._focused_index = first_openai_idx
        picker.handle_key(key("up"))
        new_item = items[picker._focused_index]
        assert "model" in new_item

    def test_down_at_last_item_stays(self):
        picker = make_picker(configured_providers=["dashscope"])
        items = picker._build_items()
        selectable = [i for i, it in enumerate(items) if "model" in it]
        picker._focused_index = selectable[-1]
        picker.handle_key(key("down"))
        assert picker._focused_index == selectable[-1]

    def test_up_at_first_item_stays(self):
        picker = make_picker(configured_providers=["dashscope"])
        items = picker._build_items()
        selectable = [i for i, it in enumerate(items) if "model" in it]
        picker._focused_index = selectable[0]
        picker.handle_key(key("up"))
        assert picker._focused_index == selectable[0]

    def test_right_cycles_effort_up(self):
        picker = make_picker(configured_providers=["openai"])
        items = picker._build_items()
        gpt_idx = next(i for i, it in enumerate(items) if it.get("model") == "gpt-5.5")
        picker._focused_index = gpt_idx
        picker._efforts[("openai", "gpt-5.5")] = EffortLevel.HIGH
        picker.handle_key(key("right"))
        assert picker._efforts[("openai", "gpt-5.5")] == EffortLevel.XHIGH

    def test_left_cycles_effort_down(self):
        picker = make_picker(configured_providers=["openai"])
        items = picker._build_items()
        gpt_idx = next(i for i, it in enumerate(items) if it.get("model") == "gpt-5.5")
        picker._focused_index = gpt_idx
        picker._efforts[("openai", "gpt-5.5")] = EffortLevel.HIGH
        picker.handle_key(key("left"))
        assert picker._efforts[("openai", "gpt-5.5")] == EffortLevel.MEDIUM

    def test_right_no_op_for_non_effort_model(self):
        picker = make_picker(configured_providers=["dashscope"])
        items = picker._build_items()
        qwen_idx = next(i for i, it in enumerate(items) if it.get("model") == "qwen3.6-plus")
        picker._focused_index = qwen_idx
        before = picker._efforts.get(("dashscope", "qwen3.6-plus"))
        picker.handle_key(key("right"))
        assert picker._efforts.get(("dashscope", "qwen3.6-plus")) == before

    def test_enter_calls_on_select(self):
        results = []
        picker = make_picker(
            initial_model="qwen3.6-plus",
            configured_providers=["dashscope"],
            on_select=lambda m, e: results.append((m, e)),
        )
        items = picker._build_items()
        qwen_idx = next(i for i, it in enumerate(items) if it.get("model") == "qwen3.6-plus")
        picker._focused_index = qwen_idx
        picker.handle_key(key("enter"))
        assert len(results) == 1
        assert results[0][0] == "qwen3.6-plus"

    def test_enter_on_effort_model_returns_effort(self):
        results = []
        picker = make_picker(
            initial_model="gpt-5.5",
            configured_providers=["openai"],
            on_select=lambda m, e: results.append((m, e)),
        )
        items = picker._build_items()
        gpt_idx = next(i for i, it in enumerate(items) if it.get("model") == "gpt-5.5")
        picker._focused_index = gpt_idx
        picker._efforts[("openai", "gpt-5.5")] = EffortLevel.XHIGH
        picker.handle_key(key("enter"))
        assert len(results) == 1
        assert results[0][0] == "gpt-5.5"
        assert results[0][1] == EffortLevel.XHIGH

    def test_enter_on_non_effort_model_returns_none_effort(self):
        results = []
        picker = make_picker(
            initial_model="qwen3.6-plus",
            configured_providers=["dashscope"],
            on_select=lambda m, e: results.append((m, e)),
        )
        items = picker._build_items()
        qwen_idx = next(i for i, it in enumerate(items) if it.get("model") == "qwen3.6-plus")
        picker._focused_index = qwen_idx
        picker.handle_key(key("enter"))
        assert len(results) == 1
        assert results[0][1] is None

    def test_escape_calls_on_cancel(self):
        cancelled = []
        picker = make_picker(
            on_cancel=lambda: cancelled.append(True),
        )
        picker.handle_key(key("escape"))
        assert cancelled == [True]

    def test_unknown_key_returns_false(self):
        picker = make_picker()
        assert picker.handle_key(key("tab")) is False


# ---------------------------------------------------------------------------
# ModelPicker — render
# ---------------------------------------------------------------------------


class TestModelPickerRender:
    def test_render_returns_renderable(self):
        picker = make_picker(configured_providers=["dashscope"])
        renderable = picker.render()
        assert renderable is not None

    def test_render_contains_current_marker(self):
        from io import StringIO

        from rich.console import Console

        picker = make_picker(
            initial_model="qwen3.6-plus",
            configured_providers=["dashscope"],
        )
        buf = StringIO()
        console = Console(file=buf, highlight=False, markup=False)
        console.print(picker.render())
        output = buf.getvalue()
        assert "(current)" in output

    def test_render_contains_effort_symbol_for_effort_model(self):
        from io import StringIO

        from rich.console import Console

        picker = make_picker(
            initial_model="gpt-5.5",
            configured_providers=["openai"],
        )
        buf = StringIO()
        console = Console(file=buf, highlight=False, markup=False)
        console.print(picker.render())
        output = buf.getvalue()
        # gpt-5.5 supports effort, should have effort symbols
        assert "◆" in output

    def test_render_header_present(self):
        from io import StringIO

        from rich.console import Console

        picker = make_picker(configured_providers=["dashscope"])
        buf = StringIO()
        console = Console(file=buf, highlight=False, markup=False)
        console.print(picker.render())
        output = buf.getvalue()
        assert "阿里云百炼" in output

    def test_render_focused_model_has_indicator(self):
        from io import StringIO

        from rich.console import Console

        picker = make_picker(
            initial_model="qwen3.6-plus",
            configured_providers=["dashscope"],
        )
        buf = StringIO()
        console = Console(file=buf, highlight=False, markup=False)
        console.print(picker.render())
        output = buf.getvalue()
        assert "> " in output


class TestModelPickerRun:
    def test_run_returns_selected_tuple(self):
        picker = make_picker(initial_model="gpt-5.5", configured_providers=["openai"])

        def fake_dialog_run(*, body_builder, key_handler):
            body_builder()
            key_handler(key("enter"))

        with (
            patch("iac_code.ui.components.dialog.Dialog") as mock_dialog,
            patch("rich.console.Console"),
        ):
            mock_dialog.return_value.run.side_effect = fake_dialog_run
            result = picker.run()

        assert result == ("gpt-5.5", EffortLevel.HIGH)

    def test_run_returns_none_on_cancel(self):
        picker = make_picker(initial_model="qwen3.6-plus", configured_providers=["dashscope"])

        def fake_dialog_run(*, body_builder, key_handler):
            body_builder()
            key_handler(key("escape"))

        with (
            patch("iac_code.ui.components.dialog.Dialog") as mock_dialog,
            patch("rich.console.Console"),
        ):
            mock_dialog.return_value.run.side_effect = fake_dialog_run
            result = picker.run()

        assert result is None

    def test_run_reuses_keybinding_manager_when_compatible(self):
        from iac_code.ui.keybindings.manager import KeybindingManager

        km = KeybindingManager()
        picker = ModelPicker(
            initial_model="qwen3.6-plus",
            configured_providers=["dashscope"],
            on_select=lambda m, e: None,
            on_cancel=lambda: None,
            keybinding_manager=km,
        )

        with (
            patch("iac_code.ui.components.dialog.Dialog") as mock_dialog,
            patch("rich.console.Console"),
        ):
            mock_dialog.return_value.run.side_effect = lambda **kwargs: kwargs["key_handler"](key("escape"))
            picker.run()

        assert mock_dialog.call_args.kwargs["keybinding_manager"] is km
