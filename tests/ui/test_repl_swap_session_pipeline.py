"""Regression tests for /resume swap during pipeline mode (问题 4)."""

from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from iac_code.pipeline.config import RunMode


def _make_repl_with_pipeline(tmp_path: Path, session_id_old: str, session_id_new: str):
    from iac_code.ui.repl import InlineREPL

    repl = MagicMock(spec=InlineREPL)
    repl._pipeline = MagicMock()
    repl._pipeline_waiting_input = True
    repl._runtime_mode = RunMode.PIPELINE
    repl._session_id = session_id_old
    repl._original_cwd = "/proj"
    repl._was_resumed = False
    repl._agent_loop = MagicMock()
    repl._agent_loop.replace_session = MagicMock()
    repl.console = MagicMock()
    repl.console.file.write = MagicMock()
    repl.console.file.flush = MagicMock()
    repl.console.print = MagicMock()
    repl.store = MagicMock()
    repl.renderer = MagicMock()
    repl._load_current_session_name = MagicMock(return_value=None)
    repl.swap_session = InlineREPL.swap_session.__get__(repl)
    repl._set_runtime_mode = InlineREPL._set_runtime_mode.__get__(repl)

    sessions_root = tmp_path / "projects" / "proj"
    sessions_root.mkdir(parents=True)

    storage = MagicMock()
    storage.session_dir.side_effect = lambda cwd, sid: sessions_root / sid
    storage.session_path.side_effect = lambda cwd, sid: sessions_root / sid / "session.jsonl"
    storage.load.return_value = []
    storage.repair_interrupted.side_effect = lambda msgs: msgs

    repl._session_storage = storage
    return repl, sessions_root


@pytest.mark.asyncio
async def test_swap_session_clears_pipeline_reference(tmp_path):
    """问题 4-a：swap 后 self._pipeline 必须 None（防 sidecar 污染）。"""
    from iac_code.ui.repl import InlineREPL

    repl, _ = _make_repl_with_pipeline(tmp_path, "old", "new")
    repl.swap_session_async = InlineREPL.swap_session_async.__get__(repl)
    # 目标 session 无 sidecar
    await repl.swap_session_async("new")
    assert repl._pipeline is None
    assert repl._pipeline_waiting_input is False


@pytest.mark.asyncio
async def test_swap_session_no_sidecar_enters_normal_mode(tmp_path):
    """问题 4-b：目标 session 无 sidecar → 不弹确认，普通模式。"""
    from iac_code.ui.repl import InlineREPL

    repl, _ = _make_repl_with_pipeline(tmp_path, "old", "new")
    repl._runtime_mode = RunMode.PIPELINE
    repl.swap_session_async = InlineREPL.swap_session_async.__get__(repl)
    repl._confirm_pipeline_resume = AsyncMock()  # 不应被调

    await repl.swap_session_async("new")
    repl._confirm_pipeline_resume.assert_not_called()
    assert repl._pipeline is None
    assert repl._runtime_mode == RunMode.NORMAL


@pytest.mark.asyncio
async def test_swap_session_detects_target_sidecar_and_prompts(tmp_path):
    """问题 4-c：目标 session 有 sidecar → 弹确认 UI。"""
    import yaml

    from iac_code.ui.repl import InlineREPL

    repl, sessions_root = _make_repl_with_pipeline(tmp_path, "old", "new")
    # 在目标 session 下放 sidecar
    sidecar = sessions_root / "new" / "pipeline"
    sidecar.mkdir(parents=True)
    (sidecar / "meta.yaml").write_text(
        yaml.dump({"status": "running", "current_step": "step1", "state_machine": {}, "updated_at": 0.0}),
        encoding="utf-8",
    )

    repl.swap_session_async = InlineREPL.swap_session_async.__get__(repl)
    repl._confirm_pipeline_resume = AsyncMock(return_value="discard")

    await repl.swap_session_async("new")
    repl._confirm_pipeline_resume.assert_called_once()
    # discard 选择 → 不创建新 pipeline, but keep the sidecar for debugging.
    assert repl._pipeline is None
    assert repl._runtime_mode == RunMode.NORMAL
    assert sidecar.exists()
    meta = yaml.safe_load((sidecar / "meta.yaml").read_text(encoding="utf-8"))
    assert meta["status"] == "discarded"
    assert meta["resume_policy"] == "none"
    assert meta["terminal"] is True
    assert meta["reason"] == "discarded from /resume picker"


@pytest.mark.asyncio
async def test_swap_session_discard_marks_sidecar_without_deleting(tmp_path):
    import yaml

    from iac_code.ui.repl import InlineREPL

    repl, sessions_root = _make_repl_with_pipeline(tmp_path, "old", "new")
    sidecar = sessions_root / "new" / "pipeline"
    sidecar.mkdir(parents=True)
    (sidecar / "meta.yaml").write_text(
        yaml.dump({"status": "running", "current_step": "step1", "state_machine": {}, "updated_at": 0.0}),
        encoding="utf-8",
    )

    repl.swap_session_async = InlineREPL.swap_session_async.__get__(repl)
    repl._confirm_pipeline_resume = AsyncMock(return_value="discard")

    with (
        patch("iac_code.pipeline.engine.session.PipelineSession.delete") as delete,
        patch("iac_code.pipeline.engine.session.PipelineSession.mark_discarded") as mark_discarded,
    ):
        await repl.swap_session_async("new")

    delete.assert_not_called()
    mark_discarded.assert_called_once_with(reason="discarded from /resume picker")


@pytest.mark.asyncio
async def test_swap_session_discard_mark_failure_does_not_crash_or_delete(tmp_path):
    import yaml

    from iac_code.ui.repl import InlineREPL

    repl, sessions_root = _make_repl_with_pipeline(tmp_path, "old", "new")
    sidecar = sessions_root / "new" / "pipeline"
    sidecar.mkdir(parents=True)
    (sidecar / "meta.yaml").write_text(
        yaml.dump({"status": "running", "current_step": "step1", "state_machine": {}, "updated_at": 0.0}),
        encoding="utf-8",
    )

    repl.swap_session_async = InlineREPL.swap_session_async.__get__(repl)
    repl._confirm_pipeline_resume = AsyncMock(return_value="discard")

    with (
        patch("iac_code.pipeline.engine.session.PipelineSession.delete") as delete,
        patch(
            "iac_code.pipeline.engine.session.PipelineSession.mark_discarded",
            side_effect=OSError("disk unavailable"),
        ) as mark_discarded,
    ):
        await repl.swap_session_async("new")

    delete.assert_not_called()
    mark_discarded.assert_called_once_with(reason="discarded from /resume picker")
    assert sidecar.exists()
    assert repl._pipeline is None
    assert repl._runtime_mode == RunMode.NORMAL
    repl.renderer.print_system_message.assert_called_once()
    assert repl.renderer.print_system_message.call_args.kwargs["style"] == "yellow"
    assert "disk unavailable" in repl.renderer.print_system_message.call_args.args[0]


@pytest.mark.asyncio
async def test_swap_session_discarded_sidecar_does_not_prompt(tmp_path):
    import yaml

    from iac_code.ui.repl import InlineREPL

    repl, sessions_root = _make_repl_with_pipeline(tmp_path, "old", "new")
    sidecar = sessions_root / "new" / "pipeline"
    sidecar.mkdir(parents=True)
    (sidecar / "meta.yaml").write_text(
        yaml.dump({"status": "discarded", "current_step": None, "state_machine": {}, "updated_at": 0.0}),
        encoding="utf-8",
    )

    repl.swap_session_async = InlineREPL.swap_session_async.__get__(repl)
    repl._confirm_pipeline_resume = AsyncMock()

    await repl.swap_session_async("new")

    repl._confirm_pipeline_resume.assert_not_called()
    assert repl._pipeline is None


@pytest.mark.asyncio
async def test_swap_session_resume_choice_creates_pipeline(tmp_path):
    """问题 4-d：用户选 resume → 重建 self._pipeline。"""
    import yaml

    from iac_code.ui.repl import InlineREPL

    repl, sessions_root = _make_repl_with_pipeline(tmp_path, "old", "new")
    sidecar = sessions_root / "new" / "pipeline"
    sidecar.mkdir(parents=True)
    (sidecar / "meta.yaml").write_text(
        yaml.dump({"status": "running", "current_step": "step1", "state_machine": {}, "updated_at": 0.0}),
        encoding="utf-8",
    )

    repl.swap_session_async = InlineREPL.swap_session_async.__get__(repl)
    repl._confirm_pipeline_resume = AsyncMock(return_value="resume")
    repl._provider_manager = MagicMock()
    repl.tool_registry = MagicMock()
    repl._memory_manager = None
    repl.command_registry = MagicMock()
    repl.command_registry.get_model_invocable_skills.return_value = ["mocked_skill"]

    fake_pipeline = MagicMock()
    fake_pipeline.sidecar_restore_result = MagicMock(ok=True, status="running", reason=None)
    with patch("iac_code.pipeline.create_pipeline", return_value=fake_pipeline) as cp:
        await repl.swap_session_async("new")

    cp.assert_called_once()
    assert cp.call_args.kwargs["resume_from_sidecar"] is True
    assert cp.call_args.kwargs["session_id"] == "new"
    # Regression: /resume path must forward auto_trigger_skills so model-invocable
    # skills survive a session swap mid-pipeline.
    assert cp.call_args.kwargs.get("auto_trigger_skills") == ["mocked_skill"]
    assert repl._pipeline is fake_pipeline


@pytest.mark.asyncio
async def test_swap_session_resume_choice_switches_runtime_mode_to_pipeline(tmp_path):
    """A resumed sidecar must route subsequent chat turns back to pipeline mode."""
    import yaml

    from iac_code.ui.repl import InlineREPL

    repl, sessions_root = _make_repl_with_pipeline(tmp_path, "old", "new")
    repl._runtime_mode = RunMode.NORMAL
    sidecar = sessions_root / "new" / "pipeline"
    sidecar.mkdir(parents=True)
    (sidecar / "meta.yaml").write_text(
        yaml.dump({"status": "running", "current_step": "step1", "state_machine": {}, "updated_at": 0.0}),
        encoding="utf-8",
    )

    repl.swap_session_async = InlineREPL.swap_session_async.__get__(repl)
    repl._confirm_pipeline_resume = AsyncMock(return_value="resume")
    repl._provider_manager = MagicMock()
    repl.tool_registry = MagicMock()
    repl._memory_manager = None
    repl.command_registry = MagicMock()
    repl.command_registry.get_model_invocable_skills.return_value = []

    fake_pipeline = MagicMock()
    fake_pipeline.sidecar_restore_result = MagicMock(ok=True, status="running", reason=None)
    with patch("iac_code.pipeline.create_pipeline", return_value=fake_pipeline):
        await repl.swap_session_async("new")

    assert repl._runtime_mode == RunMode.PIPELINE
    assert repl._pipeline is fake_pipeline


@pytest.mark.asyncio
async def test_swap_session_running_resume_routes_next_message_to_interrupt_judge(tmp_path):
    """A running sidecar restored via /resume must judge the next input."""
    import yaml

    from iac_code.ui.repl import InlineREPL

    repl, sessions_root = _make_repl_with_pipeline(tmp_path, "old", "new")
    sidecar = sessions_root / "new" / "pipeline"
    sidecar.mkdir(parents=True)
    (sidecar / "meta.yaml").write_text(
        yaml.dump({"status": "running", "current_step": "step1", "state_machine": {}, "updated_at": 0.0}),
        encoding="utf-8",
    )
    repl.swap_session_async = InlineREPL.swap_session_async.__get__(repl)
    repl._handle_pipeline_chat = InlineREPL._handle_pipeline_chat.__get__(repl)
    repl._confirm_pipeline_resume = AsyncMock(return_value="resume")
    repl._provider_manager = MagicMock()
    repl.tool_registry = MagicMock()
    repl._memory_manager = None
    repl.command_registry = MagicMock()
    repl.command_registry.get_model_invocable_skills.return_value = []
    repl._render_pipeline_stream = AsyncMock(return_value=None)
    repl._handoff_pipeline_to_normal = MagicMock(return_value=None)

    fake_pipeline = MagicMock()
    fake_pipeline.sidecar_restore_result = MagicMock(ok=True, status="running", reason=None)
    fake_pipeline.continue_from_sidecar = MagicMock(return_value=_empty_stream())
    fake_pipeline.resume = MagicMock(return_value=_empty_stream())
    fake_pipeline.sidecar_status = "running"
    fake_pipeline.state_machine.is_complete = False
    fake_pipeline.mark_user_aborted = MagicMock()
    with patch("iac_code.pipeline.create_pipeline", return_value=fake_pipeline):
        await repl.swap_session_async("new")

    await repl._handle_pipeline_chat("change the plan")

    fake_pipeline.continue_from_sidecar.assert_called_once_with(user_input="change the plan")
    fake_pipeline.resume.assert_not_called()


@pytest.mark.asyncio
async def test_swap_session_waiting_input_resume_routes_next_message_to_resume(tmp_path):
    import yaml

    from iac_code.ui.repl import InlineREPL

    repl, sessions_root = _make_repl_with_pipeline(tmp_path, "old", "new")
    sidecar = sessions_root / "new" / "pipeline"
    sidecar.mkdir(parents=True)
    (sidecar / "meta.yaml").write_text(
        yaml.dump({"status": "waiting_input", "current_step": "step1", "state_machine": {}, "updated_at": 0.0}),
        encoding="utf-8",
    )
    repl.swap_session_async = InlineREPL.swap_session_async.__get__(repl)
    repl._handle_pipeline_chat = InlineREPL._handle_pipeline_chat.__get__(repl)
    repl._confirm_pipeline_resume = AsyncMock(return_value="resume")
    repl._provider_manager = MagicMock()
    repl.tool_registry = MagicMock()
    repl._memory_manager = None
    repl.command_registry = MagicMock()
    repl.command_registry.get_model_invocable_skills.return_value = []
    repl._render_pipeline_stream = AsyncMock(return_value=None)
    repl._handoff_pipeline_to_normal = MagicMock(return_value=None)

    fake_pipeline = MagicMock()
    fake_pipeline.sidecar_restore_result = MagicMock(ok=True, status="waiting_input", reason=None)
    fake_pipeline.continue_from_sidecar = MagicMock(return_value=_empty_stream())
    fake_pipeline.resume = MagicMock(return_value=_empty_stream())
    fake_pipeline.sidecar_status = "waiting_input"
    fake_pipeline.state_machine.is_complete = False
    fake_pipeline.mark_user_aborted = MagicMock()
    with patch("iac_code.pipeline.create_pipeline", return_value=fake_pipeline):
        await repl.swap_session_async("new")

    await repl._handle_pipeline_chat("option A")

    fake_pipeline.resume.assert_called_once_with("option A")
    fake_pipeline.continue_from_sidecar.assert_not_called()


@pytest.mark.asyncio
async def test_swap_session_resume_failed_restore_keeps_pipeline_none(tmp_path):
    """If PipelineRunner construction could not restore, /resume must not claim success."""
    import yaml

    from iac_code.ui.repl import InlineREPL

    repl, sessions_root = _make_repl_with_pipeline(tmp_path, "old", "new")
    sidecar = sessions_root / "new" / "pipeline"
    sidecar.mkdir(parents=True)
    (sidecar / "meta.yaml").write_text(
        yaml.dump({"status": "running", "current_step": "step1", "state_machine": {}, "updated_at": 0.0}),
        encoding="utf-8",
    )

    repl.swap_session_async = InlineREPL.swap_session_async.__get__(repl)
    repl._confirm_pipeline_resume = AsyncMock(return_value="resume")
    repl._provider_manager = MagicMock()
    repl.tool_registry = MagicMock()
    repl._memory_manager = None
    repl.command_registry = MagicMock()
    repl.command_registry.get_model_invocable_skills.return_value = []

    fake_pipeline = MagicMock()
    fake_pipeline.sidecar_restore_result = MagicMock(
        ok=False,
        status="running",
        reason="pipeline_identity_mismatch",
    )
    with patch("iac_code.pipeline.create_pipeline", return_value=fake_pipeline):
        await repl.swap_session_async("new")

    assert repl._pipeline is None
    repl.renderer.print_system_message.assert_called()


async def _empty_stream():
    return
    yield  # noqa: B901
