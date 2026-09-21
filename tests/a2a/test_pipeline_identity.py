"""Session level pipeline identity carried over A2A.

The remote chain is `ROS 前端 → POP → ros-ai-agent → sandbox 内 iac-code A2A`. The sandbox
template is shared by both selling pipelines, so the only place the pipeline is chosen is
``metadata.iac_code.pipeline_name`` on each request. Identity is immutable per session:
once a session runs one pipeline, a request asking for the other one must be rejected
before anything reads or writes either pipeline's durable state.
"""

from __future__ import annotations

import asyncio
import os
import time
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock

import pytest
import yaml
from a2a.utils.errors import JSON_RPC_ERROR_CODE_MAP, InvalidParamsError

from iac_code.a2a.executor import IacCodeA2AExecutor
from iac_code.a2a.metrics import NoOpA2AMetrics
from iac_code.a2a.pipeline_events import PipelineA2AContext, PipelineEventTranslator
from iac_code.a2a.pipeline_paths import a2a_pipeline_dir_for_session
from iac_code.a2a.pipeline_snapshot import A2APipelineSnapshotStore, reduce_pipeline_events
from iac_code.a2a.task_store import A2ATaskStore
from iac_code.pipeline.constants import SELLING_PIPELINE_NAME, SELLING_SOLUTION_FIRST_PIPELINE_NAME
from iac_code.pipeline.engine.events import PipelineEvent, PipelineEventType
from iac_code.pipeline.engine.session import PipelineIdentity, PipelineSession
from iac_code.services.permission_wait import RecoveredPermissionAuditBoundary
from iac_code.services.session_metadata import SESSION_LAYOUT_VERSION_V2, SessionMetadata, write_session_metadata
from iac_code.services.session_storage import SessionStorage

from .fakes import FakeEventQueue, FakeRequestContext
from .test_pipeline_executor import FakePipeline, _fake_runtime

PIPELINE_NAME_ENV = "IAC_CODE_PIPELINE_NAME"


def _pipeline_executor_module() -> Any:
    """Always read the live module: sibling tests reload it, which rebinds its classes."""
    import iac_code.a2a.pipeline_executor as module

    return module


def _mismatch_error() -> type[Exception]:
    return _pipeline_executor_module().PipelineIdentityMismatchError


def _outer_executor() -> IacCodeA2AExecutor:
    return IacCodeA2AExecutor(task_store=A2ATaskStore(metrics=NoOpA2AMetrics()), model="qwen3.6-plus")


def _inner_executor(*, pipeline_name: str | None = None) -> Any:
    return _pipeline_executor_module().IacCodeA2APipelineExecutor(
        task_store=MagicMock(),
        model="qwen3.6-plus",
        metrics=NoOpA2AMetrics(),
        artifact_store=None,
        push_notifier=None,
        permission_resolver=None,
        auto_approve_permissions=False,
        thinking_exposure_types=None,
        pipeline_name=pipeline_name,
    )


class TestRequestPipelineSelection:
    """``metadata.iac_code.pipeline_name`` is the only pipeline selector on the wire."""

    @pytest.mark.parametrize("pipeline_name", [SELLING_PIPELINE_NAME, SELLING_SOLUTION_FIRST_PIPELINE_NAME])
    def test_both_selling_pipelines_can_be_selected_per_request(self, pipeline_name: str) -> None:
        executor = _outer_executor()

        assert executor._resolve_pipeline_name({"iac_code": {"pipeline_name": pipeline_name}}) == pipeline_name

    def test_surrounding_whitespace_is_ignored(self) -> None:
        executor = _outer_executor()

        resolved = executor._resolve_pipeline_name({"iac_code": {"pipeline_name": "  selling_solution_first \n"}})

        assert resolved == SELLING_SOLUTION_FIRST_PIPELINE_NAME

    @pytest.mark.parametrize(
        "metadata",
        [
            None,
            {},
            {"iac_code": {}},
            {"iac_code": {"pipeline_name": None}},
            {"iac_code": {"pipeline_name": ""}},
            {"iac_code": {"pipeline_name": "   "}},
            {"iac_code": "not-a-mapping"},
            {"pipeline_name": SELLING_SOLUTION_FIRST_PIPELINE_NAME},
        ],
        ids=[
            "no-metadata",
            "empty-metadata",
            "no-selection",
            "null-selection",
            "empty-selection",
            "blank-selection",
            "iac-code-not-a-mapping",
            "selector-outside-iac-code",
        ],
    )
    def test_a_missing_selection_is_not_an_override(self, metadata: Any) -> None:
        """Clients that never send ``PipelineName`` must keep the old `selling` behaviour."""
        executor = _outer_executor()

        assert executor._resolve_pipeline_name(metadata) is None

    def test_an_unknown_pipeline_name_is_invalid_params(self) -> None:
        executor = _outer_executor()

        with pytest.raises(InvalidParamsError) as excinfo:
            executor._resolve_pipeline_name({"iac_code": {"pipeline_name": "selling_v2"}})

        assert JSON_RPC_ERROR_CODE_MAP[type(excinfo.value)] == -32602

    @pytest.mark.parametrize(
        "raw",
        [7, True, ["selling"], {"name": "selling"}],
        ids=["int", "bool", "list", "mapping"],
    )
    def test_a_non_string_pipeline_name_is_invalid_params(self, raw: Any) -> None:
        executor = _outer_executor()

        with pytest.raises(InvalidParamsError):
            executor._resolve_pipeline_name({"iac_code": {"pipeline_name": raw}})

    def test_protobuf_metadata_is_resolved_like_a_mapping(self) -> None:
        from google.protobuf import struct_pb2

        metadata = struct_pb2.Struct()
        metadata.update({"iac_code": {"pipeline_name": SELLING_SOLUTION_FIRST_PIPELINE_NAME}})
        executor = _outer_executor()

        assert executor._resolve_pipeline_name(metadata) == SELLING_SOLUTION_FIRST_PIPELINE_NAME

    def test_selection_never_mutates_the_process_pipeline_env(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Two concurrent sessions share one process, so the selection stays request scoped."""
        monkeypatch.delenv(PIPELINE_NAME_ENV, raising=False)
        executor = _outer_executor()

        executor._resolve_pipeline_name({"iac_code": {"pipeline_name": SELLING_SOLUTION_FIRST_PIPELINE_NAME}})
        executor._resolve_pipeline_name({"iac_code": {"pipeline_name": SELLING_PIPELINE_NAME}})

        assert PIPELINE_NAME_ENV not in os.environ


def _seed_session(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    session_id: str = "session-1",
) -> tuple[SessionStorage, str, str, Path]:
    config_dir = tmp_path / "config"
    cwd = tmp_path / "workspace"
    cwd.mkdir(exist_ok=True)
    monkeypatch.setenv("IAC_CODE_CONFIG_DIR", str(config_dir))
    storage = SessionStorage()
    session_dir = Path(storage.session_dir(str(cwd), session_id))
    write_session_metadata(
        session_dir,
        SessionMetadata(session_id=session_id, cwd=str(cwd), layout_version=SESSION_LAYOUT_VERSION_V2),
    )
    return storage, str(cwd), session_id, session_dir


def _write_sidecar(session_dir: Path, pipeline_name: str) -> Path:
    """Write the engine sidecar exactly the way a paused run writes it."""
    session = PipelineSession(session_dir / "pipeline")
    session.save_waiting_input_sync(
        "materialize_selected_candidate",
        {"current_index": 1, "rollback_count": 0, "interrupt_rollback_count": 0, "step_statuses": {}},
        {"selected_plan": {"status": "awaiting_confirmation"}},
        PipelineIdentity(
            pipeline_name=pipeline_name,
            step_ids=["confirm_and_select", "materialize_selected_candidate"],
            pipeline_fingerprint="fingerprint",
        ),
    )
    return session.meta_path


def _write_snapshot(cwd: str, session_id: str, pipeline_name: str) -> Path:
    """Write the A2A snapshot through the real translator/reducer chain."""
    pipeline_dir = a2a_pipeline_dir_for_session(cwd=cwd, session_id=session_id)
    translator = PipelineEventTranslator(
        PipelineA2AContext(
            pipeline_run_id="ctx-1",
            task_id="task-1",
            context_id="ctx-1",
            pipeline_name=pipeline_name,
            iac_code_session_id=session_id,
        )
    )
    envelopes = translator.translate(
        PipelineEvent(type=PipelineEventType.PIPELINE_STARTED, step_id=None, timestamp=time.time(), data={})
    )
    A2APipelineSnapshotStore(pipeline_dir).save(reduce_pipeline_events(envelopes))
    return pipeline_dir / "a2a-snapshot.json"


class TestDurableIdentityGuard:
    """A request may only run the pipeline the session already persisted."""

    def test_a_fresh_session_runs_the_requested_pipeline(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        storage, cwd, session_id, _session_dir = _seed_session(tmp_path, monkeypatch)
        executor = _inner_executor(pipeline_name=SELLING_SOLUTION_FIRST_PIPELINE_NAME)

        resolved = executor._resolve_request_pipeline_name(cwd=cwd, session_id=session_id, session_storage=storage)

        assert resolved == SELLING_SOLUTION_FIRST_PIPELINE_NAME

    def test_without_a_selection_a_fresh_session_keeps_the_process_default(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        storage, cwd, session_id, _session_dir = _seed_session(tmp_path, monkeypatch)
        monkeypatch.setattr("iac_code.a2a.pipeline_executor.get_pipeline_name", lambda: SELLING_PIPELINE_NAME)
        executor = _inner_executor()

        resolved = executor._resolve_request_pipeline_name(cwd=cwd, session_id=session_id, session_storage=storage)

        assert resolved == SELLING_PIPELINE_NAME

    @pytest.mark.parametrize("pipeline_name", [SELLING_PIPELINE_NAME, SELLING_SOLUTION_FIRST_PIPELINE_NAME])
    def test_a_matching_request_resumes_the_persisted_pipeline(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        pipeline_name: str,
    ) -> None:
        storage, cwd, session_id, session_dir = _seed_session(tmp_path, monkeypatch)
        _write_sidecar(session_dir, pipeline_name)
        executor = _inner_executor(pipeline_name=pipeline_name)

        resolved = executor._resolve_request_pipeline_name(cwd=cwd, session_id=session_id, session_storage=storage)

        assert resolved == pipeline_name

    def test_without_a_selection_the_persisted_pipeline_wins(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """A follow-up from a client that stopped sending the field must not switch pipelines."""
        storage, cwd, session_id, session_dir = _seed_session(tmp_path, monkeypatch)
        _write_sidecar(session_dir, SELLING_SOLUTION_FIRST_PIPELINE_NAME)
        monkeypatch.setattr("iac_code.a2a.pipeline_executor.get_pipeline_name", lambda: SELLING_PIPELINE_NAME)
        executor = _inner_executor()

        resolved = executor._resolve_request_pipeline_name(cwd=cwd, session_id=session_id, session_storage=storage)

        assert resolved == SELLING_SOLUTION_FIRST_PIPELINE_NAME

    @pytest.mark.asyncio
    async def test_permission_audit_rebuild_uses_the_persisted_pipeline(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """A restart while waiting for permission must not fall back to the process default."""
        _storage, cwd, session_id, session_dir = _seed_session(tmp_path, monkeypatch)
        _write_sidecar(session_dir, SELLING_SOLUTION_FIRST_PIPELINE_NAME)
        module = _pipeline_executor_module()
        monkeypatch.setattr(module, "get_pipeline_name", lambda: SELLING_PIPELINE_NAME)
        monkeypatch.setattr(module, "create_agent_runtime", lambda _options: _fake_runtime())
        executor = _inner_executor()
        executor._backup_service = SimpleNamespace(restore_session=lambda _cwd, _session_id: None)
        monkeypatch.setattr(executor, "_configure_agent_runtime_for_request", lambda _runtime: None)
        loaded: list[str | None] = []
        expected_event = object()

        class AuditPipeline:
            async def rebuild_permission_audit_event(self, checkpoint, recovered):
                assert checkpoint == {"boundaryId": "boundary-1"}
                assert recovered.tool_use_id == "tool-1"
                return expected_event

        def create_pipeline(**kwargs):
            loaded.append(kwargs.get("pipeline_name"))
            return AuditPipeline()

        monkeypatch.setattr(executor, "_create_pipeline", create_pipeline)
        recovered = RecoveredPermissionAuditBoundary(
            tool_name="aliyun_api",
            tool_input={"product": "ROS", "action": "CreateStack"},
            tool_use_id="tool-1",
            audit_context={},
        )

        event = await executor.rebuild_permission_audit_event(
            cwd=cwd,
            session_id=session_id,
            checkpoint={"boundaryId": "boundary-1"},
            recovered=recovered,
        )

        assert event is expected_event
        assert loaded == [SELLING_SOLUTION_FIRST_PIPELINE_NAME]

    def test_a_mismatched_request_is_rejected_without_touching_the_session(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        storage, cwd, session_id, session_dir = _seed_session(tmp_path, monkeypatch)
        meta_path = _write_sidecar(session_dir, SELLING_PIPELINE_NAME)
        snapshot_path = _write_snapshot(cwd, session_id, SELLING_PIPELINE_NAME)
        meta_before = meta_path.read_bytes()
        snapshot_before = snapshot_path.read_bytes()
        executor = _inner_executor(pipeline_name=SELLING_SOLUTION_FIRST_PIPELINE_NAME)

        with pytest.raises(_mismatch_error()) as excinfo:
            executor._resolve_request_pipeline_name(cwd=cwd, session_id=session_id, session_storage=storage)

        error = excinfo.value
        assert isinstance(error, InvalidParamsError)
        assert error.code == -32602
        assert error.data == {
            "durablePipelineName": SELLING_PIPELINE_NAME,
            "requestedPipelineName": SELLING_SOLUTION_FIRST_PIPELINE_NAME,
        }
        assert meta_path.read_bytes() == meta_before
        assert snapshot_path.read_bytes() == snapshot_before

    def test_the_snapshot_answers_when_the_engine_sidecar_is_gone(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        storage, cwd, session_id, _session_dir = _seed_session(tmp_path, monkeypatch)
        _write_snapshot(cwd, session_id, SELLING_SOLUTION_FIRST_PIPELINE_NAME)
        executor = _inner_executor(pipeline_name=SELLING_PIPELINE_NAME)

        with pytest.raises(_mismatch_error()) as excinfo:
            executor._resolve_request_pipeline_name(cwd=cwd, session_id=session_id, session_storage=storage)

        assert excinfo.value.data["durablePipelineName"] == SELLING_SOLUTION_FIRST_PIPELINE_NAME

    def test_the_engine_sidecar_outranks_a_stale_snapshot(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """A resume replays the sidecar, so the sidecar is the authoritative identity."""
        storage, cwd, session_id, session_dir = _seed_session(tmp_path, monkeypatch)
        _write_sidecar(session_dir, SELLING_SOLUTION_FIRST_PIPELINE_NAME)
        _write_snapshot(cwd, session_id, SELLING_PIPELINE_NAME)
        executor = _inner_executor()

        durable = executor._peek_durable_pipeline_name(cwd=cwd, session_id=session_id, session_storage=storage)

        assert durable == SELLING_SOLUTION_FIRST_PIPELINE_NAME

    def test_a_corrupt_sidecar_falls_back_to_the_snapshot(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        storage, cwd, session_id, session_dir = _seed_session(tmp_path, monkeypatch)
        meta_path = session_dir / "pipeline" / "meta.yaml"
        meta_path.parent.mkdir(parents=True, exist_ok=True)
        meta_path.write_text("pipeline_name: [unbalanced", encoding="utf-8")
        _write_snapshot(cwd, session_id, SELLING_SOLUTION_FIRST_PIPELINE_NAME)
        executor = _inner_executor()

        durable = executor._peek_durable_pipeline_name(cwd=cwd, session_id=session_id, session_storage=storage)

        assert durable == SELLING_SOLUTION_FIRST_PIPELINE_NAME

    @pytest.mark.parametrize(
        "meta_text",
        ["pipeline_name: [unbalanced", "- not-a-mapping", "pipeline_name: ''", "status: waiting_input"],
        ids=["corrupt", "wrong-shape", "empty-name", "no-name"],
    )
    def test_unusable_identity_sources_do_not_block_the_request(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        meta_text: str,
    ) -> None:
        storage, cwd, session_id, session_dir = _seed_session(tmp_path, monkeypatch)
        meta_path = session_dir / "pipeline" / "meta.yaml"
        meta_path.parent.mkdir(parents=True, exist_ok=True)
        meta_path.write_text(meta_text, encoding="utf-8")
        executor = _inner_executor(pipeline_name=SELLING_SOLUTION_FIRST_PIPELINE_NAME)

        assert executor._peek_durable_pipeline_name(cwd=cwd, session_id=session_id, session_storage=storage) is None
        assert (
            executor._resolve_request_pipeline_name(cwd=cwd, session_id=session_id, session_storage=storage)
            == SELLING_SOLUTION_FIRST_PIPELINE_NAME
        )

    def test_the_guard_reads_the_session_and_never_writes_the_process_env(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        storage, cwd, session_id, session_dir = _seed_session(tmp_path, monkeypatch)
        _write_sidecar(session_dir, SELLING_SOLUTION_FIRST_PIPELINE_NAME)
        monkeypatch.delenv(PIPELINE_NAME_ENV, raising=False)
        executor = _inner_executor(pipeline_name=SELLING_SOLUTION_FIRST_PIPELINE_NAME)

        executor._resolve_request_pipeline_name(cwd=cwd, session_id=session_id, session_storage=storage)

        assert PIPELINE_NAME_ENV not in os.environ

    def test_two_sessions_run_different_pipelines_in_one_process(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        storage, cwd, legacy_session, legacy_dir = _seed_session(tmp_path, monkeypatch, session_id="legacy-session")
        solution_dir = Path(storage.session_dir(cwd, "solution-session"))
        write_session_metadata(
            solution_dir,
            SessionMetadata(session_id="solution-session", cwd=cwd, layout_version=SESSION_LAYOUT_VERSION_V2),
        )
        _write_sidecar(legacy_dir, SELLING_PIPELINE_NAME)
        _write_sidecar(solution_dir, SELLING_SOLUTION_FIRST_PIPELINE_NAME)
        monkeypatch.delenv(PIPELINE_NAME_ENV, raising=False)
        legacy_executor = _inner_executor(pipeline_name=SELLING_PIPELINE_NAME)
        solution_executor = _inner_executor(pipeline_name=SELLING_SOLUTION_FIRST_PIPELINE_NAME)

        assert (
            legacy_executor._resolve_request_pipeline_name(cwd=cwd, session_id=legacy_session, session_storage=storage)
            == SELLING_PIPELINE_NAME
        )
        assert (
            solution_executor._resolve_request_pipeline_name(
                cwd=cwd, session_id="solution-session", session_storage=storage
            )
            == SELLING_SOLUTION_FIRST_PIPELINE_NAME
        )
        assert PIPELINE_NAME_ENV not in os.environ

    def test_each_runner_is_created_with_its_own_pipeline(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        storage, cwd, session_id, _session_dir = _seed_session(tmp_path, monkeypatch)
        created: list[str] = []
        inspected: list[str] = []
        monkeypatch.setattr(
            "iac_code.a2a.pipeline_executor.create_pipeline",
            lambda pipeline_name, **_kwargs: created.append(pipeline_name) or SimpleNamespace(),
        )

        for pipeline_name in (SELLING_PIPELINE_NAME, SELLING_SOLUTION_FIRST_PIPELINE_NAME):
            executor = _inner_executor(pipeline_name=pipeline_name)
            monkeypatch.setattr(
                executor,
                "_inspect_pipeline_prerequisite_metadata",
                lambda *, pipeline_name, **_kwargs: inspected.append(pipeline_name),
            )
            executor._create_pipeline(
                session_id=session_id,
                cwd=cwd,
                runtime=_fake_runtime(),
                session_storage=storage,
                pipeline_name=pipeline_name,
            )

        assert created == [SELLING_PIPELINE_NAME, SELLING_SOLUTION_FIRST_PIPELINE_NAME]
        # The frozen prerequisites of the other pipeline are never read.
        assert inspected == [SELLING_PIPELINE_NAME, SELLING_SOLUTION_FIRST_PIPELINE_NAME]


async def _seeded_context(store: A2ATaskStore, *, cwd: str) -> Any:
    return await store.get_or_create_context(
        context_id="ctx-1",
        cwd=cwd,
        runtime_factory=lambda _session_id: _fake_runtime(),
    )


class TestExecuteIdentityGuard:
    """The guard runs before the prerequisite pre-read and before any runner create/restore."""

    @pytest.mark.asyncio
    async def test_the_requested_pipeline_reaches_the_runner_and_the_prerequisite_read(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        _storage, cwd, _session_id, _session_dir = _seed_session(tmp_path, monkeypatch)
        created: list[str] = []
        inspected: list[str] = []
        store = A2ATaskStore(metrics=NoOpA2AMetrics())
        ctx = await _seeded_context(store, cwd=cwd)
        fake_pipeline = FakePipeline(
            [PipelineEvent(type=PipelineEventType.PIPELINE_COMPLETED, step_id=None, timestamp=1.0, data={})],
            session_dir=Path(SessionStorage().session_dir(cwd, ctx.session_id)) / "pipeline",
        )
        fake_pipeline.pipeline_name = SELLING_SOLUTION_FIRST_PIPELINE_NAME
        monkeypatch.setattr(
            "iac_code.a2a.pipeline_executor.create_pipeline",
            lambda pipeline_name, **_kwargs: created.append(pipeline_name) or fake_pipeline,
        )
        executor = _inner_executor(pipeline_name=SELLING_SOLUTION_FIRST_PIPELINE_NAME)
        executor._task_store = store
        monkeypatch.setattr(
            executor,
            "_inspect_pipeline_prerequisite_metadata",
            lambda *, pipeline_name, **_kwargs: inspected.append(pipeline_name),
        )

        await executor.execute(
            context=FakeRequestContext(metadata={"iac_code": {"cwd": cwd}}),
            event_queue=FakeEventQueue(),
            task=await store.get_or_create_task(task_id="task-1", context_id="ctx-1"),
            task_id="task-1",
            context_id="ctx-1",
            cwd=cwd,
            prompt="部署一个网站",
        )

        assert created == [SELLING_SOLUTION_FIRST_PIPELINE_NAME]
        assert inspected == [SELLING_SOLUTION_FIRST_PIPELINE_NAME]

    @pytest.mark.asyncio
    async def test_a_mismatch_is_rejected_before_prerequisites_and_runner_creation(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        _storage, cwd, _session_id, _session_dir = _seed_session(tmp_path, monkeypatch)
        monkeypatch.setenv("IAC_CODE_DESKTOP_RUNTIME", "1")
        monkeypatch.setattr("iac_code.desktop.external_env._WINDOWS_PRELOAD_READY", True)
        store = A2ATaskStore(metrics=NoOpA2AMetrics())
        ctx = await _seeded_context(store, cwd=cwd)
        session_dir = Path(SessionStorage().session_dir(cwd, ctx.session_id))
        meta_path = _write_sidecar(session_dir, SELLING_PIPELINE_NAME)
        snapshot_path = _write_snapshot(cwd, ctx.session_id, SELLING_PIPELINE_NAME)
        meta_before = meta_path.read_bytes()
        snapshot_before = snapshot_path.read_bytes()

        def boom(*_args: Any, **_kwargs: Any) -> Any:
            raise AssertionError("identity guard must run first")

        monkeypatch.setattr("iac_code.a2a.pipeline_executor.create_pipeline", boom)
        executor = _inner_executor(pipeline_name=SELLING_SOLUTION_FIRST_PIPELINE_NAME)
        executor._task_store = store
        monkeypatch.setattr(executor, "_inspect_pipeline_prerequisite_metadata", boom)
        queue = FakeEventQueue()
        task = await store.get_or_create_task(task_id="task-1", context_id="ctx-1")
        task_state_before = task.state

        with pytest.raises(_mismatch_error()) as excinfo:
            await executor.execute(
                context=FakeRequestContext(metadata={"iac_code": {"cwd": cwd}}),
                event_queue=queue,
                task=task,
                task_id="task-1",
                context_id="ctx-1",
                cwd=cwd,
                prompt="继续",
            )

        assert excinfo.value.data == {
            "durablePipelineName": SELLING_PIPELINE_NAME,
            "requestedPipelineName": SELLING_SOLUTION_FIRST_PIPELINE_NAME,
        }
        # No silent restart: the task is not failed and neither pipeline's state is touched.
        assert queue.events == []
        assert task.state == task_state_before
        assert meta_path.read_bytes() == meta_before
        assert snapshot_path.read_bytes() == snapshot_before
        assert not ctx.lock.locked()

    @pytest.mark.asyncio
    @pytest.mark.parametrize("active_followup_only", [True, False], ids=["followup-probe", "full-request"])
    async def test_a_mismatch_on_an_active_task_never_reaches_the_running_pipeline(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        active_followup_only: bool,
    ) -> None:
        """A follow-up into a live run must be rejected before its guidance is injected.

        The active-task branch routes the request into the pipeline that is already streaming, so the
        identity guard has to precede it: otherwise a request asking for the other pipeline would
        interrupt (or fail) a run whose durable identity disagrees with it.
        """
        _storage, cwd, _session_id, _session_dir = _seed_session(tmp_path, monkeypatch)
        store = A2ATaskStore(metrics=NoOpA2AMetrics())
        ctx = await _seeded_context(store, cwd=cwd)
        session_dir = Path(SessionStorage().session_dir(cwd, ctx.session_id))
        meta_path = _write_sidecar(session_dir, SELLING_PIPELINE_NAME)
        meta_before = meta_path.read_bytes()
        ctx.active_task_id = "task-1"

        def boom(*_args: Any, **_kwargs: Any) -> Any:
            raise AssertionError("identity guard must run before the active task is touched")

        executor = _inner_executor(pipeline_name=SELLING_SOLUTION_FIRST_PIPELINE_NAME)
        executor._task_store = store
        monkeypatch.setattr(executor, "_clear_stale_recoverable_active_task", boom)
        monkeypatch.setattr(executor, "_route_active_pipeline_interrupt", boom)
        monkeypatch.setattr(executor, "_fail_already_active", boom)
        monkeypatch.setattr("iac_code.a2a.pipeline_executor.create_pipeline", boom)
        queue = FakeEventQueue()
        task = await store.get_or_create_task(task_id="task-1", context_id="ctx-1")
        task_state_before = task.state

        with pytest.raises(_mismatch_error()) as excinfo:
            await executor.execute(
                context=FakeRequestContext(metadata={"iac_code": {"cwd": cwd}}),
                event_queue=queue,
                task=task,
                task_id="task-1",
                context_id="ctx-1",
                cwd=cwd,
                prompt="换成另一条流水线",
                active_followup_only=active_followup_only,
            )

        assert excinfo.value.data == {
            "durablePipelineName": SELLING_PIPELINE_NAME,
            "requestedPipelineName": SELLING_SOLUTION_FIRST_PIPELINE_NAME,
        }
        assert queue.events == []
        assert task.state == task_state_before
        assert ctx.active_task_id == "task-1"
        assert meta_path.read_bytes() == meta_before
        assert not ctx.lock.locked()


class TestEmittedIdentity:
    """Events and the A2A snapshot describe the pipeline that actually runs."""

    def _publisher_for(
        self,
        *,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        requested: str | None,
        running: str | None,
    ) -> Any:
        _storage, cwd, session_id, session_dir = _seed_session(tmp_path, monkeypatch)
        pipeline = FakePipeline([], session_dir=session_dir / "pipeline")
        if running is None:
            del pipeline.pipeline_name
        else:
            pipeline.pipeline_name = running
        executor = _inner_executor(pipeline_name=requested)
        return executor._publisher(
            event_queue=FakeEventQueue(),
            pipeline=pipeline,
            task_id="task-1",
            context_id="ctx-1",
            session_id=session_id,
            cwd=cwd,
        )

    def test_the_running_pipeline_names_the_stream_not_the_request(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        publisher = self._publisher_for(
            tmp_path=tmp_path,
            monkeypatch=monkeypatch,
            requested=SELLING_SOLUTION_FIRST_PIPELINE_NAME,
            running=SELLING_PIPELINE_NAME,
        )

        assert publisher.translator.context.pipeline_name == SELLING_PIPELINE_NAME

    def test_a_runner_without_its_own_name_falls_back_to_the_request(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        publisher = self._publisher_for(
            tmp_path=tmp_path,
            monkeypatch=monkeypatch,
            requested=SELLING_SOLUTION_FIRST_PIPELINE_NAME,
            running=None,
        )

        assert publisher.translator.context.pipeline_name == SELLING_SOLUTION_FIRST_PIPELINE_NAME

    def test_events_and_snapshot_agree_on_the_running_pipeline(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        publisher = self._publisher_for(
            tmp_path=tmp_path,
            monkeypatch=monkeypatch,
            requested=SELLING_SOLUTION_FIRST_PIPELINE_NAME,
            running=SELLING_SOLUTION_FIRST_PIPELINE_NAME,
        )

        envelopes = publisher.translator.translate(
            PipelineEvent(type=PipelineEventType.PIPELINE_STARTED, step_id=None, timestamp=1.0, data={})
        )

        assert [item["pipelineName"] for item in envelopes] == [SELLING_SOLUTION_FIRST_PIPELINE_NAME]
        assert reduce_pipeline_events(envelopes)["pipelineName"] == SELLING_SOLUTION_FIRST_PIPELINE_NAME


def test_sidecar_identity_written_by_the_engine_is_readable_by_the_guard(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Guard and engine must agree on where the durable pipeline name lives."""
    storage, cwd, session_id, session_dir = _seed_session(tmp_path, monkeypatch)
    meta_path = _write_sidecar(session_dir, SELLING_SOLUTION_FIRST_PIPELINE_NAME)

    raw = yaml.safe_load(meta_path.read_text(encoding="utf-8"))

    assert raw["pipeline_name"] == SELLING_SOLUTION_FIRST_PIPELINE_NAME
    assert (
        _inner_executor()._peek_durable_pipeline_name(
            cwd=cwd,
            session_id=session_id,
            session_storage=storage,
        )
        == SELLING_SOLUTION_FIRST_PIPELINE_NAME
    )


def test_concurrent_executors_resolve_independently(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    storage, cwd, _session_id, session_dir = _seed_session(tmp_path, monkeypatch, session_id="shared-session")
    _write_sidecar(session_dir, SELLING_PIPELINE_NAME)

    async def resolve(pipeline_name: str) -> Any:
        executor = _inner_executor(pipeline_name=pipeline_name)
        return await asyncio.to_thread(
            executor._resolve_request_pipeline_name,
            cwd=cwd,
            session_id="shared-session",
            session_storage=storage,
        )

    async def main() -> list[Any]:
        return await asyncio.gather(
            resolve(SELLING_PIPELINE_NAME),
            resolve(SELLING_SOLUTION_FIRST_PIPELINE_NAME),
            return_exceptions=True,
        )

    legacy, solution_first = asyncio.run(main())

    assert legacy == SELLING_PIPELINE_NAME
    assert isinstance(solution_first, _mismatch_error())
