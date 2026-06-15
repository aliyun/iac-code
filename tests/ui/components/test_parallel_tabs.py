"""Tests for ParallelTabsRenderer component."""

from io import StringIO

from rich.console import Console
from rich.text import Text

from iac_code.ui.components.parallel_tabs import CandidateState, CandidateStatus, ParallelTabsRenderer
from iac_code.ui.core.key_event import KeyEvent


def key(k):
    return KeyEvent(key=k, char=k)


class TestCandidateState:
    def test_initial_state(self):
        cs = CandidateState(
            sub_pipeline_id="eval_a",
            candidate_index=0,
            name="方案A",
            total_steps=3,
        )
        assert cs.status == CandidateStatus.RUNNING
        assert cs.current_step == ""
        assert cs.completed_steps == 0
        assert cs.error is None

    def test_progress_label(self):
        cs = CandidateState(
            sub_pipeline_id="eval_a",
            candidate_index=0,
            name="方案A",
            total_steps=3,
        )
        cs.current_step = "模板生成"
        cs.completed_steps = 1
        assert cs.progress_label == "方案A: 模板生成 [1/3]"

    def test_progress_label_done(self):
        cs = CandidateState(
            sub_pipeline_id="eval_a",
            candidate_index=0,
            name="方案A",
            total_steps=3,
        )
        cs.status = CandidateStatus.DONE
        cs.completed_steps = 3
        assert "完成" in cs.progress_label or "✓" in cs.progress_label

    def test_progress_label_done_uses_ascii_status_symbol_when_needed(self, monkeypatch):
        monkeypatch.setattr("iac_code.ui.components.status_icon.use_ascii_symbols", lambda: True)
        cs = CandidateState(
            sub_pipeline_id="eval_a",
            candidate_index=0,
            name="方案A",
            total_steps=3,
            status=CandidateStatus.DONE,
            completed_steps=3,
        )

        assert cs.progress_label == "方案A: OK Done"

    def test_progress_label_failed(self):
        cs = CandidateState(
            sub_pipeline_id="eval_a",
            candidate_index=0,
            name="方案A",
            total_steps=3,
        )
        cs.status = CandidateStatus.FAILED
        cs.error = "审查失败"
        assert "Failed" in cs.progress_label


class TestParallelTabsRenderer:
    def make_renderer(self, n=3):
        candidates = [
            CandidateState(
                sub_pipeline_id=f"eval_{i}",
                candidate_index=i,
                name=f"方案{chr(65 + i)}",
                total_steps=3,
            )
            for i in range(n)
        ]
        console = Console(file=StringIO(), force_terminal=True, width=100)
        return ParallelTabsRenderer(candidates=candidates, console=console)

    def test_initial_selected_is_first(self):
        r = self.make_renderer()
        assert r.selected_index == 0

    def test_handle_right_key(self):
        r = self.make_renderer()
        assert r.handle_key(key("right")) is True
        assert r.selected_index == 1

    def test_handle_left_key(self):
        r = self.make_renderer()
        r.handle_key(key("right"))
        r.handle_key(key("left"))
        assert r.selected_index == 0

    def test_no_wrap_right(self):
        r = self.make_renderer()
        r.handle_key(key("right"))
        r.handle_key(key("right"))
        r.handle_key(key("right"))  # past end
        assert r.selected_index == 2

    def test_no_wrap_left(self):
        r = self.make_renderer()
        r.handle_key(key("left"))  # before start
        assert r.selected_index == 0

    def test_number_key_jumps(self):
        r = self.make_renderer()
        assert r.handle_key(key("2")) is True
        assert r.selected_index == 1
        assert r.handle_key(key("3")) is True
        assert r.selected_index == 2

    def test_number_key_out_of_range_ignored(self):
        r = self.make_renderer()
        assert r.handle_key(key("5")) is False  # only 3 candidates
        assert r.selected_index == 0

    def test_unhandled_key_returns_false(self):
        r = self.make_renderer()
        assert r.handle_key(key("enter")) is False

    def test_render_returns_renderable(self):
        r = self.make_renderer()
        result = r.render()
        assert result is not None

    def test_render_contains_candidate_names(self):
        r = self.make_renderer()
        result = r.render()
        console = Console(file=StringIO(), force_terminal=False, width=100)
        console.print(result)
        output = console.file.getvalue()
        assert "方案A" in output
        assert "方案B" in output
        assert "方案C" in output

    def test_render_shows_active_indicator(self):
        r = self.make_renderer()
        result = r.render()
        console = Console(file=StringIO(), force_terminal=False, width=100)
        console.print(result)
        output = console.file.getvalue()
        # Active tab's content section is shown below the tab bar
        assert "━━ 方案A" in output

    def test_single_candidate_no_tabs(self):
        """Single candidate should still render (no tab bar needed but acceptable)."""
        r = self.make_renderer(n=1)
        result = r.render()
        assert result is not None

    def test_update_candidate_step(self):
        r = self.make_renderer()
        r.update_step("eval_0", step_name="审查", completed=1)
        state = r.get_candidate("eval_0")
        assert state.current_step == "审查"
        assert state.completed_steps == 1

    def test_mark_done(self):
        r = self.make_renderer()
        r.mark_done("eval_0")
        state = r.get_candidate("eval_0")
        assert state.status == CandidateStatus.DONE

    def test_mark_failed(self):
        r = self.make_renderer()
        r.mark_failed("eval_1", error="回滚耗尽")
        state = r.get_candidate("eval_1")
        assert state.status == CandidateStatus.FAILED
        assert state.error == "回滚耗尽"

    def test_all_done(self):
        r = self.make_renderer(n=2)
        assert r.all_done is False
        r.mark_done("eval_0")
        assert r.all_done is False
        r.mark_done("eval_1")
        assert r.all_done is True

    def test_all_done_includes_failed(self):
        r = self.make_renderer(n=2)
        r.mark_done("eval_0")
        r.mark_failed("eval_1", error="err")
        assert r.all_done is True

    def test_render_does_not_require_external_tick(self):
        """Spinner is wall-clock-driven inside ShimmerSpinner; no external tick needed."""
        r = self.make_renderer()
        # No tick() exists anymore — render must still produce output.
        assert not hasattr(r, "tick"), "tick() should be removed; spinner is self-driven"
        assert not hasattr(r, "_frame"), "_frame state should be removed"
        result = r.render()
        assert result is not None

    def test_down_and_up_keys_scroll_content(self):
        r = self.make_renderer()
        assert r.handle_key(key("down")) is True
        assert r._scroll_offsets[0] == 3
        assert r.handle_key(key("up")) is True
        assert r._scroll_offsets[0] == 0

    def test_scroll_state_is_per_candidate(self):
        r = self.make_renderer()
        r.handle_key(key("down"))
        r.handle_key(key("right"))
        assert r.selected_index == 1
        assert r._scroll_offsets[1] == 0
        r.handle_key(key("left"))
        assert r._scroll_offsets[0] == 3

    def test_render_with_content_clips_long_output(self):
        candidates = [
            CandidateState(sub_pipeline_id="eval_0", candidate_index=0, name="方案A", total_steps=3),
        ]
        console = Console(file=StringIO(), force_terminal=True, width=80, height=12)
        r = ParallelTabsRenderer(candidates=candidates, console=console)
        long_text = "\n".join(f"line {i}" for i in range(30))

        console.print(r.render_with_content(Text(long_text)))
        output = console.file.getvalue()

        assert "line 0" in output
        assert "line 29" not in output
        assert "Scroll down to view more" in output
