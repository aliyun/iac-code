"""Regression tests for spinner unification (问题 7)."""

from unittest.mock import patch

from rich.console import Console

from iac_code.ui.components.parallel_tabs import CandidateState, CandidateStatus, ParallelTabsRenderer


def _make_renderer(names=("alpha", "beta")):
    candidates = [
        CandidateState(sub_pipeline_id=f"evaluate_{i}", candidate_index=i, name=n, total_steps=3)
        for i, n in enumerate(names)
    ]
    return ParallelTabsRenderer(candidates, Console())


def test_renderer_uses_shimmer_spinner_not_legacy_frames():
    """问题 7：并行 tabs 应该复用 main 的 ShimmerSpinner，不再用自带 _SPINNER_FRAMES 常量。"""
    renderer = _make_renderer()
    assert not hasattr(renderer, "_SPINNER_FRAMES"), (
        "ParallelTabsRenderer still defines _SPINNER_FRAMES — should be replaced by ShimmerSpinner"
    )


def test_parallel_tabs_uses_shimmer_spinner_frame(monkeypatch):
    """问题 7：tab bar 的动画帧必须来自 ShimmerSpinner，而不是本地 time/frames 计算。"""
    monkeypatch.setattr(
        "iac_code.ui.components.parallel_tabs.random_spinner_verb",
        lambda: "Working",
    )
    monkeypatch.setattr(
        "iac_code.ui.components.parallel_tabs.ShimmerSpinner.frame",
        lambda self: "#",
    )

    renderer = _make_renderer(("alpha",))
    bar = renderer._render_tab_bar()

    assert "#" in bar.plain


def test_each_candidate_gets_independent_spinner_verb():
    """问题 7：每个 candidate 启动时独立选一次 random_spinner_verb，不全局共享。"""
    with patch(
        "iac_code.ui.components.parallel_tabs.random_spinner_verb",
        side_effect=["FirstVerb", "SecondVerb"],
    ):
        renderer = _make_renderer(("alpha", "beta"))
        verb_a = renderer._verb_for("alpha")
        verb_b = renderer._verb_for("beta")
        assert verb_a == "FirstVerb"
        assert verb_b == "SecondVerb"
        # 同一 candidate 重复取应该稳定（不每帧重新随机）
        assert renderer._verb_for("alpha") == "FirstVerb"


def test_rendered_tab_bar_contains_per_candidate_verbs():
    """问题 7：渲染后的 tab bar 文本必须包含每个 RUNNING candidate 的独立 verb。"""
    with patch(
        "iac_code.ui.components.parallel_tabs.random_spinner_verb",
        side_effect=["FirstVerb", "SecondVerb"],
    ):
        renderer = _make_renderer(("alpha", "beta"))
        bar = renderer._render_tab_bar()
        text = bar.plain  # rich.Text → plain string
        assert "FirstVerb" in text
        assert "SecondVerb" in text


def test_rendered_tab_bar_omits_verb_for_completed_candidates():
    """已完成的 candidate 不应显示 verb（避免误导）。"""
    renderer = _make_renderer(("alpha",))
    renderer._candidates[0].status = CandidateStatus.DONE
    renderer._candidates[0].completed_steps = 3
    with patch(
        "iac_code.ui.components.parallel_tabs.random_spinner_verb",
        return_value="SomeVerb",
    ):
        bar = renderer._render_tab_bar()
        text = bar.plain
        assert "SomeVerb" not in text
