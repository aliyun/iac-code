import json
import logging
from pathlib import Path

import pytest
import yaml

from iac_code.pipeline.engine.session import PipelineSession


@pytest.fixture
def session_dir(tmp_path):
    return tmp_path / "test_session.pipeline"


@pytest.fixture
def session(session_dir):
    return PipelineSession(session_dir)


class TestSaveStepCompletion:
    @pytest.mark.asyncio
    async def test_creates_sidecar_directory(self, session, session_dir):
        assert not session_dir.exists()
        await session.save_step_completion(
            step_id="intent_parsing",
            state_machine_snapshot={"current_index": 1, "rollback_count": 0, "step_statuses": {}},
            context_snapshot={"intent": {"value": {"type": "e-commerce"}, "version": 1, "stale": False}},
        )
        assert session_dir.exists()
        assert session_dir.is_dir()

    @pytest.mark.asyncio
    async def test_writes_meta_yaml(self, session, session_dir):
        await session.save_step_completion(
            step_id="intent_parsing",
            state_machine_snapshot={"current_index": 1},
            context_snapshot={},
        )
        meta = yaml.safe_load((session_dir / "meta.yaml").read_text(encoding="utf-8"))
        assert meta["current_step"] == "intent_parsing"
        assert meta["state_machine"] == {"current_index": 1}
        assert "updated_at" in meta

    @pytest.mark.asyncio
    async def test_preserves_metadata_kwargs(self, session, session_dir):
        execution = {"kind": "step", "step_id": "intent_parsing", "active_attempt_id": "att_0001"}
        attempts = {"next_attempt_number": 2, "items": {"att_0001": {"status": "running"}}}
        normal_handoff = {"status": "pending", "switched_to_normal": False}

        await session.save_step_completion(
            step_id="intent_parsing",
            state_machine_snapshot={"current_index": 1},
            context_snapshot={},
            execution=execution,
            attempts=attempts,
            normal_handoff=normal_handoff,
        )

        meta = yaml.safe_load((session_dir / "meta.yaml").read_text(encoding="utf-8"))
        assert meta["execution"] == execution
        assert meta["attempts"] == attempts
        assert meta["normal_handoff"] == normal_handoff

    @pytest.mark.asyncio
    async def test_writes_context_yaml(self, session, session_dir):
        ctx_snap = {"intent": {"value": {"type": "blog"}, "version": 1, "stale": False}}
        await session.save_step_completion(
            step_id="intent_parsing",
            state_machine_snapshot={},
            context_snapshot=ctx_snap,
        )
        context = yaml.safe_load((session_dir / "context.yaml").read_text(encoding="utf-8"))
        assert context["intent"]["value"]["type"] == "blog"

    @pytest.mark.asyncio
    async def test_context_written_before_meta(self, session, session_dir):
        """context.yaml mtime should be <= meta.yaml mtime."""
        await session.save_step_completion(
            step_id="x",
            state_machine_snapshot={},
            context_snapshot={},
        )
        ctx_mtime = (session_dir / "context.yaml").stat().st_mtime
        meta_mtime = (session_dir / "meta.yaml").stat().st_mtime
        assert ctx_mtime <= meta_mtime


def test_session_save_failure_reraises_without_lower_layer_warning(session, caplog, monkeypatch):
    def fail_write(path, data):
        raise OSError("disk full")

    monkeypatch.setattr(session, "_atomic_write_yaml", fail_write)
    caplog.set_level(logging.WARNING, logger="iac_code.pipeline.engine.session")

    with pytest.raises(OSError, match="disk full"):
        session.save_running_sync(
            "intent",
            {"current_index": 0},
            {},
            _identity(),
            reason="advanced from plan",
        )

    assert "Failed to save pipeline sidecar" not in caplog.text
    assert not [
        record
        for record in caplog.records
        if record.name == "iac_code.pipeline.engine.session" and record.levelno >= logging.WARNING
    ]


def test_sidecar_yaml_uses_atomic_state_write(monkeypatch, tmp_path) -> None:
    calls = []

    def fake_atomic_write_text(path, content, *, durable=True, replace_attempts=3, encoding="utf-8"):
        calls.append((Path(path).name, durable))
        Path(path).write_text(content, encoding=encoding)

    monkeypatch.setattr("iac_code.pipeline.engine.session.atomic_write_text", fake_atomic_write_text)

    session = PipelineSession(tmp_path / "pipeline")
    session.save_running_sync(
        "step",
        {"current_index": 0, "rollback_count": 0, "step_statuses": {"step": "running"}},
        {},
        {"pipeline_name": "test", "step_ids": ["step"], "sub_pipeline_step_ids": {}, "pipeline_fingerprint": "fp"},
    )

    assert ("context.yaml", True) in calls
    assert ("meta.yaml", True) in calls


class TestSaveRollback:
    @pytest.mark.asyncio
    async def test_updates_meta_with_target_step(self, session, session_dir):
        await session.save_step_completion(
            step_id="c",
            state_machine_snapshot={"idx": 2},
            context_snapshot={},
        )
        await session.save_rollback(
            from_step="c",
            to_step="a",
            reason="wrong",
            state_machine_snapshot={"idx": 0},
            context_snapshot={"a": {"value": None}},
        )
        meta = yaml.safe_load((session_dir / "meta.yaml").read_text(encoding="utf-8"))
        assert meta["current_step"] == "a"

    @pytest.mark.asyncio
    async def test_appends_to_events_jsonl(self, session, session_dir):
        await session.save_rollback(
            from_step="c",
            to_step="a",
            reason="cost_too_high",
            state_machine_snapshot={},
            context_snapshot={},
        )
        lines = (session_dir / "events.jsonl").read_text(encoding="utf-8").strip().split("\n")
        event = json.loads(lines[0])
        assert event["type"] == "rollback"
        assert event["from"] == "c"
        assert event["to"] == "a"

    def test_sync_preserves_attempt_metadata(self, session):
        identity = _identity()
        execution = {
            "kind": "step",
            "step_id": "intent",
            "active_attempt_id": "att_0002",
        }
        attempts = {
            "next_attempt_number": 3,
            "items": {
                "att_0001": {"scope": "parent", "step_id": "confirm", "status": "rolled_back"},
                "att_0002": {"scope": "parent", "step_id": "intent", "status": "running"},
            },
        }
        normal_handoff = {"status": "pending", "switched_to_normal": False}

        session.save_rollback_sync(
            from_step="confirm",
            to_step="intent",
            reason="retry intent",
            state_machine_snapshot={
                "current_index": 0,
                "rollback_count": 1,
                "interrupt_rollback_count": 0,
                "step_statuses": {"intent": "running"},
            },
            context_snapshot={},
            identity=identity,
            execution=execution,
            attempts=attempts,
            normal_handoff=normal_handoff,
        )

        meta = yaml.safe_load(session.meta_path.read_text(encoding="utf-8"))
        assert meta["execution"] == execution
        assert meta["attempts"] == attempts
        assert meta["normal_handoff"] == normal_handoff

        restored = session.restore_sync(identity)
        assert restored.ok is True
        assert restored.execution == execution
        assert restored.attempts == attempts
        assert restored.normal_handoff == normal_handoff

    def test_sync_without_metadata_preserves_existing_attempt_metadata(self, session):
        identity = _identity()
        execution = {
            "kind": "step",
            "step_id": "confirm",
            "active_attempt_id": "att_0002",
        }
        attempts = {
            "next_attempt_number": 3,
            "items": {
                "att_0001": {"scope": "parent", "step_id": "intent", "status": "completed"},
                "att_0002": {"scope": "parent", "step_id": "confirm", "status": "running"},
            },
        }
        normal_handoff = {"status": "pending", "switched_to_normal": False}

        session.save_running_sync(
            "confirm",
            {
                "current_index": 1,
                "rollback_count": 0,
                "interrupt_rollback_count": 0,
                "step_statuses": {"intent": "completed", "confirm": "running"},
            },
            {},
            identity,
            execution=execution,
            attempts=attempts,
            normal_handoff=normal_handoff,
        )

        session.save_rollback_sync(
            from_step="confirm",
            to_step="intent",
            reason="retry intent",
            state_machine_snapshot={
                "current_index": 0,
                "rollback_count": 1,
                "interrupt_rollback_count": 0,
                "step_statuses": {"intent": "running", "confirm": "stale"},
            },
            context_snapshot={},
            identity=identity,
        )

        meta = yaml.safe_load(session.meta_path.read_text(encoding="utf-8"))
        assert meta["execution"] == execution
        assert meta["attempts"] == attempts
        assert meta["normal_handoff"] == normal_handoff

        restored = session.restore_sync(identity)
        assert restored.ok is True
        assert restored.execution == execution
        assert restored.attempts == attempts
        assert restored.normal_handoff == normal_handoff


class TestRestore:
    @pytest.mark.asyncio
    async def test_roundtrip(self, session):
        sm_snap = {"current_index": 2, "rollback_count": 1, "step_statuses": {"a": "completed"}}
        ctx_snap = {"intent": {"value": {"type": "blog"}, "version": 1, "stale": False}}
        await session.save_step_completion("b", sm_snap, ctx_snap)

        data = await session.restore()
        assert data["state_machine_snapshot"] == sm_snap
        assert data["context_snapshot"] == ctx_snap
        assert data["current_step"] == "b"

    def test_missing_meta_without_sidecar_does_not_log_warning(self, session, caplog):
        caplog.set_level(logging.WARNING, logger="iac_code.pipeline.engine.session")

        result = session.restore_sync(_identity())

        assert result.ok is False
        assert result.reason == "missing_meta"
        assert not [
            record
            for record in caplog.records
            if record.name == "iac_code.pipeline.engine.session" and record.levelno >= logging.WARNING
        ]

    def test_corrupt_meta_logs_warning_with_reason_status_and_path(self, session, caplog):
        session.session_dir.mkdir(parents=True)
        session.meta_path.write_text("status: [", encoding="utf-8")
        caplog.set_level(logging.WARNING, logger="iac_code.pipeline.engine.session")

        result = session.restore_sync(_identity())

        assert result.ok is False
        assert result.reason == "corrupt_meta"
        assert "Failed to restore pipeline sidecar" in caplog.text
        assert "reason=corrupt_meta" in caplog.text
        assert "status=None" in caplog.text
        assert str(session.session_dir) in caplog.text

    @pytest.mark.parametrize("status", ["completed", "user_aborted", "failed", "discarded"])
    def test_terminal_status_restore_does_not_log_warning(self, session, caplog, status):
        sm_snap = {"current_index": 0, "rollback_count": 0, "interrupt_rollback_count": 0, "step_statuses": {}}
        if status == "completed":
            session.save_completed_sync("intent", sm_snap, {}, _identity(), reason="done")
        elif status == "user_aborted":
            session.save_user_aborted_sync("intent", sm_snap, {}, _identity(), reason="ctrl-c")
        elif status == "failed":
            session.save_failed_sync("intent", sm_snap, {}, _identity(), reason="step failed")
        else:
            session.mark_discarded(reason="user chose discard")
        caplog.set_level(logging.WARNING, logger="iac_code.pipeline.engine.session")

        restored = session.restore_sync(_identity())

        assert restored.ok is False
        assert restored.status == status
        assert not [
            record
            for record in caplog.records
            if record.name == "iac_code.pipeline.engine.session" and record.levelno >= logging.WARNING
        ]

    def test_identity_mismatch_restore_does_not_log_warning(self, session, caplog):
        sm_snap = {"current_index": 0, "rollback_count": 0, "interrupt_rollback_count": 0, "step_statuses": {}}
        session.save_running_sync("intent", sm_snap, {}, _identity(fingerprint="old"))
        caplog.set_level(logging.WARNING, logger="iac_code.pipeline.engine.session")

        restored = session.restore_sync(_identity(fingerprint="new"))

        assert restored.ok is False
        assert restored.reason == "pipeline_identity_mismatch"
        assert restored.status == "running"
        assert not [
            record
            for record in caplog.records
            if record.name == "iac_code.pipeline.engine.session" and record.levelno >= logging.WARNING
        ]


class TestExists:
    def test_not_exists_initially(self, session):
        assert not session.exists()

    @pytest.mark.asyncio
    async def test_exists_after_save(self, session):
        await session.save_step_completion("x", {}, {})
        assert session.exists()


class TestDelete:
    def test_delete_when_missing_is_noop(self, session, session_dir):
        assert not session_dir.exists()
        assert session.delete() is True
        assert not session_dir.exists()

    @pytest.mark.asyncio
    async def test_delete_marks_discarded_without_removing_sidecar(self, session, session_dir):
        await session.save_step_completion("x", {}, {})
        assert session_dir.exists()
        assert session.delete() is True
        assert session_dir.exists()
        assert session.exists()
        meta = yaml.safe_load((session_dir / "meta.yaml").read_text(encoding="utf-8"))
        assert meta["status"] == "discarded"
        assert meta["resume_policy"] == "none"
        assert meta["terminal"] is True

    def test_delete_logs_exception_when_mark_discarded_raises(self, session, session_dir, caplog, monkeypatch):
        session_dir.mkdir(parents=True)

        def fail_mark_discarded(reason=None):
            raise OSError("locked")

        monkeypatch.setattr(session, "mark_discarded", fail_mark_discarded)
        caplog.set_level(logging.WARNING, logger="iac_code.pipeline.engine.session")

        assert session.delete() is False
        assert "Failed to mark pipeline sidecar discarded" in caplog.text
        assert str(session_dir) in caplog.text


class TestUtf8Encoding:
    """Regression: pipeline session persistence must use UTF-8 (W-C1)."""

    @pytest.mark.asyncio
    async def test_chinese_content_roundtrip(self, session, session_dir):
        ctx_snap = {
            "intent": {
                "value": {"业务类型": "博客网站", "需求": "高可用"},
                "version": 1,
                "stale": False,
            }
        }
        await session.save_step_completion(
            step_id="intent_parsing",
            state_machine_snapshot={"current_index": 0},
            context_snapshot=ctx_snap,
        )
        # Byte-level: file must contain the UTF-8 encoding of the Chinese
        # string, NOT the cp1252/cp936 fallback that silently corrupts it.
        raw = (session_dir / "context.yaml").read_bytes()
        assert "博客网站".encode("utf-8") in raw
        # Round-trip through restore() must preserve the Chinese characters.
        restored = await session.restore()
        assert restored["context_snapshot"]["intent"]["value"]["业务类型"] == "博客网站"

    @pytest.mark.asyncio
    async def test_chinese_content_in_rollback(self, session, session_dir):
        ctx_snap = {"intent": {"value": {"需求": "便宜"}, "version": 1, "stale": False}}
        await session.save_rollback(
            from_step="evaluate_candidates",
            to_step="intent_parsing",
            reason="用户想要更便宜的方案",
            state_machine_snapshot={"current_index": 0},
            context_snapshot=ctx_snap,
        )
        raw = (session_dir / "context.yaml").read_bytes()
        assert "便宜".encode("utf-8") in raw
        events_raw = (session_dir / "events.jsonl").read_bytes()
        assert "便宜".encode("utf-8") in events_raw


def _identity(
    *,
    pipeline_name: str = "selling",
    step_ids: list[str] | None = None,
    fingerprint: str = "abc123",
) -> dict:
    return {
        "pipeline_name": pipeline_name,
        "step_ids": step_ids or ["intent", "confirm"],
        "sub_pipeline_step_ids": {"candidate": ["template", "review"]},
        "pipeline_fingerprint": fingerprint,
    }


def test_identity_helper_returns_pipeline_contract_fields():
    assert _identity() == {
        "pipeline_name": "selling",
        "step_ids": ["intent", "confirm"],
        "sub_pipeline_step_ids": {"candidate": ["template", "review"]},
        "pipeline_fingerprint": "abc123",
    }


@pytest.mark.asyncio
async def test_save_running_writes_current_contract(session, session_dir):
    sm_snap = {"current_index": 0, "rollback_count": 0, "interrupt_rollback_count": 0, "step_statuses": {}}
    ctx_snap = {"intent": {"value": {"business": "blog"}, "version": 1, "stale": False}}

    await session.save_running("intent", sm_snap, ctx_snap, _identity(), reason="start")

    meta = yaml.safe_load((session_dir / "meta.yaml").read_text(encoding="utf-8"))
    context = yaml.safe_load((session_dir / "context.yaml").read_text(encoding="utf-8"))
    assert meta["pipeline_name"] == "selling"
    assert meta["status"] == "running"
    assert meta["current_step"] == "intent"
    assert meta["step_ids"] == ["intent", "confirm"]
    assert meta["sub_pipeline_step_ids"] == {"candidate": ["template", "review"]}
    assert meta["pipeline_fingerprint"] == "abc123"
    assert meta["reason"] == "start"
    assert meta["state_machine"] == sm_snap
    assert context == ctx_snap


@pytest.mark.asyncio
@pytest.mark.parametrize("status", ["completed", "user_aborted", "failed", "discarded"])
async def test_terminal_and_discarded_statuses_are_not_resumable(session, status):
    sm_snap = {"current_index": 0, "rollback_count": 0, "interrupt_rollback_count": 0, "step_statuses": {}}
    ctx_snap = {}
    if status == "completed":
        await session.save_completed("intent", sm_snap, ctx_snap, _identity(), reason="done")
    elif status == "user_aborted":
        await session.save_user_aborted("intent", sm_snap, ctx_snap, _identity(), reason="ctrl-c")
    elif status == "failed":
        await session.save_failed("intent", sm_snap, ctx_snap, _identity(), reason="step failed")
    else:
        session.mark_discarded(reason="user chose discard")

    assert session.is_resumable(_identity()) is False
    restored = session.restore_sync(_identity())
    assert restored.ok is False
    assert restored.status == status


@pytest.mark.asyncio
async def test_restore_returns_failure_for_corrupt_meta(session, session_dir):
    session_dir.mkdir(parents=True)
    (session_dir / "meta.yaml").write_text("status: [", encoding="utf-8")
    (session_dir / "context.yaml").write_text("{}", encoding="utf-8")

    restored = await session.restore(_identity())

    assert restored.ok is False
    assert restored.reason == "corrupt_meta"


@pytest.mark.asyncio
async def test_restore_returns_failure_for_invalid_utf8_meta(session, session_dir):
    session_dir.mkdir(parents=True)
    (session_dir / "meta.yaml").write_bytes(b"\xff\xfe\xfa")
    (session_dir / "context.yaml").write_text("{}", encoding="utf-8")

    restored = await session.restore(_identity())

    assert restored.ok is False
    assert restored.reason == "corrupt_meta"


@pytest.mark.asyncio
async def test_restore_returns_failure_for_non_scalar_status(session, session_dir):
    session_dir.mkdir(parents=True)
    (session_dir / "meta.yaml").write_text(
        yaml.dump(
            {
                "pipeline_name": "selling",
                "status": [],
                "current_step": "intent",
                "state_machine": {"current_index": 0, "rollback_count": 0, "step_statuses": {}},
                "step_ids": ["intent", "confirm"],
                "sub_pipeline_step_ids": {"candidate": ["template", "review"]},
                "pipeline_fingerprint": "abc123",
                "updated_at": 1.0,
                "reason": None,
            }
        ),
        encoding="utf-8",
    )
    (session_dir / "context.yaml").write_text("{}", encoding="utf-8")

    restored = await session.restore(_identity())

    assert restored.ok is False
    assert restored.reason == "invalid_meta"


@pytest.mark.asyncio
async def test_restore_returns_failure_for_missing_context(session, session_dir):
    session_dir.mkdir(parents=True)
    (session_dir / "meta.yaml").write_text(
        yaml.dump(
            {
                "pipeline_name": "selling",
                "status": "running",
                "current_step": "intent",
                "state_machine": {"current_index": 0, "rollback_count": 0, "step_statuses": {}},
                "step_ids": ["intent", "confirm"],
                "sub_pipeline_step_ids": {"candidate": ["template", "review"]},
                "pipeline_fingerprint": "abc123",
                "updated_at": 1.0,
                "reason": None,
            }
        ),
        encoding="utf-8",
    )

    restored = await session.restore(_identity())

    assert restored.ok is False
    assert restored.reason == "missing_context"


def test_restore_accepts_list_context_values(session):
    identity = _identity(step_ids=["evaluate_candidates", "confirm_and_select"])
    session.save_running_sync(
        "confirm_and_select",
        {
            "current_index": 1,
            "rollback_count": 0,
            "interrupt_rollback_count": 0,
            "step_statuses": {"evaluate_candidates": "completed", "confirm_and_select": "running"},
        },
        {
            "evaluated_candidates": {
                "value": [{"candidate": {"name": "Plan A"}, "failed": False}],
                "version": 1,
                "stale": False,
                "updated_at": 1.0,
                "history": [],
            }
        },
        identity,
    )

    restored = session.restore_sync(identity)

    assert restored.ok is True
    assert restored.context_snapshot["evaluated_candidates"]["value"][0]["candidate"]["name"] == "Plan A"


@pytest.mark.parametrize(
    "metadata",
    [
        {"attempts": {"next_attempt_number": "bad", "items": {}}},
        {"attempts": {"next_attempt_number": 2, "items": []}},
        {"execution": {"kind": "step", "active_attempt_id": []}},
        {"execution": {"kind": "step", "transcript_id": []}},
    ],
)
def test_restore_rejects_malformed_execution_metadata(session, session_dir, metadata):
    session_dir.mkdir(parents=True)
    meta = {
        "pipeline_name": "selling",
        "status": "running",
        "current_step": "intent",
        "state_machine": {"current_index": 0, "rollback_count": 0, "step_statuses": {}},
        "step_ids": ["intent", "confirm"],
        "sub_pipeline_step_ids": {"candidate": ["template", "review"]},
        "pipeline_fingerprint": "abc123",
        "updated_at": 1.0,
        "reason": None,
    }
    meta.update(metadata)
    (session_dir / "meta.yaml").write_text(yaml.dump(meta), encoding="utf-8")
    (session_dir / "context.yaml").write_text("{}", encoding="utf-8")

    restored = session.restore_sync(_identity())

    assert restored.ok is False
    assert restored.reason == "invalid_meta"


@pytest.mark.asyncio
async def test_restore_rejects_pipeline_identity_mismatch(session):
    sm_snap = {"current_index": 0, "rollback_count": 0, "interrupt_rollback_count": 0, "step_statuses": {}}
    await session.save_running("intent", sm_snap, {}, _identity(fingerprint="old"))

    restored = await session.restore(_identity(fingerprint="new"))

    assert restored.ok is False
    assert restored.reason == "pipeline_identity_mismatch"


@pytest.mark.asyncio
async def test_restore_rejects_non_string_current_step(session, session_dir):
    session_dir.mkdir(parents=True)
    (session_dir / "meta.yaml").write_text(
        yaml.dump(
            {
                "pipeline_name": "selling",
                "status": "running",
                "current_step": [],
                "state_machine": {"current_index": 0, "rollback_count": 0, "step_statuses": {}},
                "step_ids": ["intent", "confirm"],
                "sub_pipeline_step_ids": {"candidate": ["template", "review"]},
                "pipeline_fingerprint": "abc123",
                "updated_at": 1.0,
                "reason": None,
            }
        ),
        encoding="utf-8",
    )
    (session_dir / "context.yaml").write_text("{}", encoding="utf-8")

    restored = await session.restore(_identity())

    assert restored.ok is False
    assert restored.reason == "invalid_meta"


@pytest.mark.asyncio
async def test_restore_rejects_running_meta_without_identity_for_identity_restore(session, session_dir):
    session_dir.mkdir(parents=True)
    (session_dir / "meta.yaml").write_text(
        yaml.dump(
            {
                "status": "running",
                "current_step": "intent",
                "state_machine": {"current_index": 0, "rollback_count": 0, "step_statuses": {}},
                "updated_at": 1.0,
                "reason": None,
            }
        ),
        encoding="utf-8",
    )
    (session_dir / "context.yaml").write_text("{}", encoding="utf-8")

    restored = await session.restore(_identity())

    assert restored.ok is False
    assert restored.reason == "pipeline_identity_mismatch"


@pytest.mark.asyncio
async def test_no_identity_restore_terminal_sidecar_returns_legacy_empty_snapshot(session):
    sm_snap = {"current_index": 1, "rollback_count": 0, "interrupt_rollback_count": 0, "step_statuses": {}}
    await session.save_completed("confirm", sm_snap, {}, _identity(), reason="done")

    restored = await session.restore()

    assert restored == {
        "state_machine_snapshot": {
            "current_index": 0,
            "rollback_count": 0,
            "interrupt_rollback_count": 0,
            "step_statuses": {},
        },
        "context_snapshot": {},
        "current_step": None,
    }


def test_mark_discarded_writes_skip_status_without_context(session, session_dir):
    session.mark_discarded(reason="discard from picker")

    meta = yaml.safe_load((session_dir / "meta.yaml").read_text(encoding="utf-8"))
    assert meta["status"] == "discarded"
    assert meta["reason"] == "discard from picker"
    assert not (session_dir / "context.yaml").exists()
    assert session.is_resumable(_identity()) is False
    assert session.has_resumable_status() is False


@pytest.mark.parametrize("status,expected", [("running", True), ("waiting_input", True), ("failed", False)])
def test_has_resumable_status_checks_lightweight_meta(session_dir, status, expected):
    session = PipelineSession(session_dir)
    session_dir.mkdir(parents=True)
    (session_dir / "meta.yaml").write_text(
        yaml.dump({"status": status, "current_step": "intent", "state_machine": {}}),
        encoding="utf-8",
    )

    assert session.has_resumable_status() is expected


def _running_snapshot():
    return {
        "current_index": 0,
        "rollback_count": 0,
        "interrupt_rollback_count": 0,
        "step_statuses": {"intent_parsing": "running"},
    }


def test_save_running_persists_execution_and_attempts(session, session_dir):
    identity = _identity(step_ids=["intent_parsing", "confirm"])
    execution = {
        "kind": "step",
        "step_id": "intent_parsing",
        "active_attempt_id": "att_0001",
        "transcript_id": "transcript_att_0001",
    }
    attempts = {
        "next_attempt_number": 2,
        "items": {
            "att_0001": {
                "scope": "parent",
                "step_id": "intent_parsing",
                "status": "running",
                "transcript_id": "transcript_att_0001",
            }
        },
    }

    session.save_running_sync(
        "intent_parsing",
        _running_snapshot(),
        {},
        identity,
        reason="step started",
        execution=execution,
        attempts=attempts,
    )

    meta = yaml.safe_load(session.meta_path.read_text(encoding="utf-8"))
    assert meta["resume_policy"] == "active"
    assert meta["terminal"] is False
    assert meta["execution"] == execution
    assert meta["attempts"] == attempts

    restored = session.restore_sync(identity)
    assert restored.ok is True
    assert restored.execution == execution
    assert restored.attempts == attempts


def test_metadata_unaware_running_save_preserves_existing_dict_metadata(session):
    identity = _identity()
    execution = {"kind": "step", "step_id": "intent", "active_attempt_id": "att_0001"}
    attempts = {"next_attempt_number": 2, "items": {"att_0001": {"status": "running"}}}
    normal_handoff = {"status": "pending", "switched_to_normal": False}

    session.save_running_sync(
        "intent",
        {
            "current_index": 0,
            "rollback_count": 0,
            "interrupt_rollback_count": 0,
            "step_statuses": {"intent": "running", "confirm": "pending"},
        },
        {},
        identity,
        execution=execution,
        attempts=attempts,
        normal_handoff=normal_handoff,
    )

    session.save_running_sync(
        "confirm",
        {
            "current_index": 1,
            "rollback_count": 0,
            "interrupt_rollback_count": 0,
            "step_statuses": {"intent": "completed", "confirm": "running"},
        },
        {},
        identity,
        reason="advanced to confirm",
    )

    meta = yaml.safe_load(session.meta_path.read_text(encoding="utf-8"))
    assert meta["reason"] == "advanced to confirm"
    assert meta["execution"] == execution
    assert meta["attempts"] == attempts
    assert meta["normal_handoff"] == normal_handoff

    restored = session.restore_sync(identity)
    assert restored.ok is True
    assert restored.execution == execution
    assert restored.attempts == attempts
    assert restored.normal_handoff == normal_handoff


def test_explicit_none_execution_clears_existing_execution_metadata(session):
    identity = _identity(step_ids=["intent", "confirm"])
    execution = {"kind": "step", "step_id": "intent", "active_attempt_id": "att_0001"}
    attempts = {"next_attempt_number": 2, "items": {"att_0001": {"status": "completed"}}}

    session.save_running_sync(
        "intent",
        {
            "current_index": 0,
            "rollback_count": 0,
            "interrupt_rollback_count": 0,
            "step_statuses": {"intent": "running", "confirm": "pending"},
        },
        {},
        identity,
        execution=execution,
        attempts={"next_attempt_number": 2, "items": {"att_0001": {"status": "running"}}},
    )

    session.save_running_sync(
        "confirm",
        {
            "current_index": 1,
            "rollback_count": 0,
            "interrupt_rollback_count": 0,
            "step_statuses": {"intent": "completed", "confirm": "running"},
        },
        {},
        identity,
        reason="advanced to confirm",
        execution=None,
        attempts=attempts,
    )

    meta = yaml.safe_load(session.meta_path.read_text(encoding="utf-8"))
    assert "execution" not in meta
    assert meta["attempts"] == attempts

    restored = session.restore_sync(identity)
    assert restored.ok is True
    assert restored.execution is None
    assert restored.attempts == attempts


def test_completed_sidecar_is_terminal_but_preserves_attempts(session):
    identity = _identity(step_ids=["intent", "plan"])
    state_machine = {
        "current_index": 1,
        "rollback_count": 0,
        "interrupt_rollback_count": 0,
        "step_statuses": {"intent": "completed", "plan": "completed"},
    }
    execution = {
        "kind": "step",
        "step_id": "plan",
        "active_attempt_id": "att_0002",
    }
    attempts = {
        "next_attempt_number": 3,
        "items": {
            "att_0001": {"scope": "parent", "step_id": "intent", "status": "completed"},
            "att_0002": {"scope": "parent", "step_id": "plan", "status": "completed"},
        },
    }

    session.save_completed_sync(
        "plan",
        state_machine,
        {},
        identity,
        reason="pipeline completed",
        execution=execution,
        attempts=attempts,
        normal_handoff={"status": "succeeded", "switched_to_normal": True},
    )

    meta = yaml.safe_load(session.meta_path.read_text(encoding="utf-8"))
    assert meta["status"] == "completed"
    assert meta["resume_policy"] == "none"
    assert meta["terminal"] is True
    assert meta["execution"] == execution
    assert meta["attempts"] == attempts
    assert meta["normal_handoff"] == {"status": "succeeded", "switched_to_normal": True}
    assert session.has_resumable_status() is False

    restored = session.restore_sync(identity)
    assert restored.ok is False
    assert restored.status == "completed"
    assert restored.execution == execution
    assert restored.attempts == attempts
    assert restored.normal_handoff == {"status": "succeeded", "switched_to_normal": True}


def test_metadata_unaware_completed_save_preserves_existing_dict_metadata(session):
    identity = _identity()
    execution = {"kind": "step", "step_id": "confirm", "active_attempt_id": "att_0002"}
    attempts = {
        "next_attempt_number": 3,
        "items": {
            "att_0001": {"scope": "parent", "step_id": "intent", "status": "completed"},
            "att_0002": {"scope": "parent", "step_id": "confirm", "status": "running"},
        },
    }
    normal_handoff = {"status": "pending", "switched_to_normal": False}
    state_machine = {
        "current_index": 1,
        "rollback_count": 0,
        "interrupt_rollback_count": 0,
        "step_statuses": {"intent": "completed", "confirm": "running"},
    }

    session.save_running_sync(
        "confirm",
        state_machine,
        {},
        identity,
        execution=execution,
        attempts=attempts,
        normal_handoff=normal_handoff,
    )

    session.save_completed_sync("confirm", state_machine, {}, identity, reason="pipeline completed")

    meta = yaml.safe_load(session.meta_path.read_text(encoding="utf-8"))
    assert meta["status"] == "completed"
    assert meta["reason"] == "pipeline completed"
    assert meta["execution"] == execution
    assert meta["attempts"] == attempts
    assert meta["normal_handoff"] == normal_handoff

    restored = session.restore_sync(identity)
    assert restored.ok is False
    assert restored.status == "completed"
    assert restored.execution == execution
    assert restored.attempts == attempts
    assert restored.normal_handoff == normal_handoff


def test_mark_discarded_preserves_existing_meta_fields(session):
    execution = {"kind": "step", "step_id": "intent", "active_attempt_id": "att_0001"}
    attempts = {"next_attempt_number": 2, "items": {"att_0001": {"status": "running"}}}

    session.save_running_sync(
        "intent",
        _running_snapshot(),
        {},
        _identity(),
        execution=execution,
        attempts=attempts,
    )

    session.mark_discarded(reason="user chose discard")

    meta = yaml.safe_load(session.meta_path.read_text(encoding="utf-8"))
    assert meta["status"] == "discarded"
    assert meta["resume_policy"] == "none"
    assert meta["terminal"] is True
    assert meta["reason"] == "user chose discard"
    assert meta["attempts"]["items"]["att_0001"]["status"] == "running"

    restored = session.restore_sync(_identity())
    assert restored.ok is False
    assert restored.status == "discarded"
    assert restored.execution == execution
    assert restored.attempts == attempts
