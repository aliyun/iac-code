from __future__ import annotations

import json
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from iac_code.a2a.metrics import NoOpA2AMetrics
from iac_code.a2a.persistence import A2APersistenceStore
from iac_code.a2a.pipeline_executor import IacCodeA2APipelineExecutor
from iac_code.a2a.task_store import A2ATaskStore
from iac_code.services.session_storage import SessionStorage
from iac_code.utils import state_io


@pytest.mark.asyncio
async def test_unchanged_context_reuses_version_without_suppressing_activity_or_repairs(tmp_path, monkeypatch):
    cwd = str(tmp_path / "workspace")
    store = A2ATaskStore(metrics=NoOpA2AMetrics(), persistence=A2APersistenceStore(tmp_path / "state"))
    context = await store.get_or_create_context(context_id="ctx", cwd=cwd, runtime_factory=lambda sid: object())
    context_path = SessionStorage().session_dir(cwd, context.session_id) / "a2a" / "context.json"
    initial = json.loads(context_path.read_text(encoding="utf-8"))
    write = Mock(wraps=state_io.atomic_write_bytes)
    monkeypatch.setattr(state_io, "atomic_write_bytes", write)
    last_active = context.last_active
    context.touch()
    store.mirror_context(context)
    store.mirror_context(context)
    assert context.last_active >= last_active
    assert write.call_count == 0
    assert json.loads(context_path.read_text(encoding="utf-8")) == initial

    context_path.unlink()
    store.mirror_context(context)
    assert write.call_count == 1
    assert json.loads(context_path.read_text(encoding="utf-8")) == initial
    context_path.write_text("external replacement", encoding="utf-8")
    store.mirror_context(context)
    assert write.call_count == 2
    assert json.loads(context_path.read_text(encoding="utf-8")) == initial

    context.active_task_id = "new-task"
    store.mirror_context(context)
    changed = json.loads(context_path.read_text(encoding="utf-8"))
    assert changed["active_task_id"] == "new-task"
    assert changed["updated_at"] >= initial["updated_at"]
    assert write.call_count > 2


@pytest.mark.asyncio
async def test_context_failed_write_is_retried_without_a_state_change(tmp_path, monkeypatch):
    store = A2ATaskStore(persistence=A2APersistenceStore(tmp_path / "state"))
    context = await store.get_or_create_context(
        context_id="ctx", cwd=str(tmp_path), runtime_factory=lambda sid: object()
    )
    context.active_task_id = "changed"
    original = state_io.atomic_write_bytes
    fail = Mock(side_effect=OSError("fixture write failure"))
    monkeypatch.setattr(state_io, "atomic_write_bytes", fail)
    store.mirror_context(context)
    assert fail.call_count >= 1
    monkeypatch.setattr(state_io, "atomic_write_bytes", original)
    store.mirror_context(context)
    assert store._persistence.load_context("ctx").active_task_id == "changed"


@pytest.mark.asyncio
async def test_publication_task_mirrors_reuse_timestamp_but_keep_real_updates(tmp_path, monkeypatch):
    store = A2ATaskStore(persistence=A2APersistenceStore(tmp_path / "state"))
    context = await store.get_or_create_context(
        context_id="ctx", cwd=str(tmp_path), runtime_factory=lambda sid: object()
    )
    task = await store.get_or_create_task(task_id="task", context_id="ctx")
    executor = SimpleNamespace(_task_store=store)
    envelope = {"eventType": "input_required", "status": "input_required", "createdAt": "2026-11-26T00:00:00Z"}
    mirror = IacCodeA2APipelineExecutor._mirror_a2a_snapshots_for_pipeline_publication
    mirror(executor, envelope, task=task, ctx=context)
    write = Mock(wraps=state_io.atomic_write_bytes)
    monkeypatch.setattr(state_io, "atomic_write_bytes", write)
    mirror(executor, envelope, task=task, ctx=context)
    assert write.call_count == 0
    task.output_text.append("a newer user-visible result")
    task.updated_at += 1
    mirror(executor, envelope, task=task, ctx=context)
    assert write.call_count > 0
    snapshot = store._persistence.load_task("task")
    assert snapshot.output_text == task.output_text
    assert snapshot.updated_at == task.updated_at
    assert task.state != "input-required"  # The input-boundary projection remains a copy.
