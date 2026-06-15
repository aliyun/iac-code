"""Test PipelineRunner.iter_active_agent_loops (问题 6 dependency)."""

from unittest.mock import MagicMock


def test_iter_active_agent_loops_yields_step_loop():
    """普通 step 进行中：yield step_executor 的 current_agent_loop。"""
    from iac_code.pipeline.engine.pipeline_runner import PipelineRunner

    runner = MagicMock(spec=PipelineRunner)
    runner._step_executor = MagicMock()
    fake_loop = MagicMock(name="fake_step_loop")
    runner._step_executor.current_agent_loop = fake_loop
    runner._current_sub_executor_list = None

    runner.iter_active_agent_loops = PipelineRunner.iter_active_agent_loops.__get__(runner)
    loops = list(runner.iter_active_agent_loops())
    assert loops == [fake_loop]


def test_iter_active_agent_loops_yields_nothing_when_idle():
    """空闲（无 step 进行中）：yield 空。"""
    from iac_code.pipeline.engine.pipeline_runner import PipelineRunner

    runner = MagicMock(spec=PipelineRunner)
    runner._step_executor = MagicMock()
    runner._step_executor.current_agent_loop = None
    runner._current_sub_executor_list = None

    runner.iter_active_agent_loops = PipelineRunner.iter_active_agent_loops.__get__(runner)
    loops = list(runner.iter_active_agent_loops())
    assert loops == []


def test_iter_active_agent_loops_yields_candidate_loops_during_parallel():
    """并行 step 进行中：yield 各 candidate 的 current_step_executor_agent_loop。"""
    from iac_code.pipeline.engine.pipeline_runner import PipelineRunner

    runner = MagicMock(spec=PipelineRunner)
    runner._step_executor = MagicMock()
    runner._step_executor.current_agent_loop = None
    cand_a = MagicMock()
    cand_a.current_step_executor_agent_loop = MagicMock(name="cand_a_loop")
    cand_b = MagicMock()
    cand_b.current_step_executor_agent_loop = MagicMock(name="cand_b_loop")
    runner._current_sub_executor_list = [cand_a, cand_b]

    runner.iter_active_agent_loops = PipelineRunner.iter_active_agent_loops.__get__(runner)
    loops = list(runner.iter_active_agent_loops())
    assert len(loops) == 2
