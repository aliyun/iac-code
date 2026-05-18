from iac_code.a2a.persistence import A2AContextSnapshot, A2APersistenceStore, A2ARouteSnapshot, A2ATaskSnapshot


def test_persistence_round_trips_task_and_context(tmp_path) -> None:
    store = A2APersistenceStore(tmp_path)

    store.save_task(A2ATaskSnapshot(task_id="task-1", context_id="ctx-1", state="working", output_text=["hi"]))
    store.save_context(A2AContextSnapshot(context_id="ctx-1", session_id="session-1", cwd=str(tmp_path)))

    assert store.load_task("task-1").state == "working"
    assert store.load_context("ctx-1").session_id == "session-1"


def test_persistence_rejects_path_traversal_ids(tmp_path) -> None:
    store = A2APersistenceStore(tmp_path)

    try:
        store.save_task(A2ATaskSnapshot(task_id="../escape", context_id="ctx-1", state="working"))
    except ValueError as exc:
        assert "Invalid A2A id" in str(exc)
    else:
        raise AssertionError("path traversal task id should be rejected")

    assert not (tmp_path / "escape.json").exists()


def test_working_tasks_restore_as_interrupted(tmp_path) -> None:
    store = A2APersistenceStore(tmp_path)
    store.save_task(A2ATaskSnapshot(task_id="task-1", context_id="ctx-1", state="working"))

    restored = store.restore_task("task-1")

    assert restored.state == "interrupted"
    assert "cannot be revived" in restored.status_message
    assert store.load_task("task-1").state == "interrupted"


def test_submitted_tasks_restore_as_interrupted(tmp_path) -> None:
    store = A2APersistenceStore(tmp_path)
    store.save_task(A2ATaskSnapshot(task_id="task-1", context_id="ctx-1", state="submitted"))

    restored = store.restore_task("task-1")

    assert restored.state == "interrupted"
    assert store.load_task("task-1").state == "interrupted"


def test_input_required_tasks_restore_without_interruption(tmp_path) -> None:
    store = A2APersistenceStore(tmp_path)
    store.save_task(A2ATaskSnapshot(task_id="task-1", context_id="ctx-1", state="input-required"))

    restored = store.restore_task("task-1")

    assert restored.state == "input-required"
    assert store.load_task("task-1").state == "input-required"


def test_corrupt_task_file_is_skipped(tmp_path) -> None:
    store = A2APersistenceStore(tmp_path)
    (tmp_path / "tasks").mkdir()
    (tmp_path / "tasks" / "bad.json").write_text("{broken", encoding="utf-8")

    assert store.list_tasks() == []


def test_persistence_round_trips_route_snapshots(tmp_path) -> None:
    store = A2APersistenceStore(tmp_path)

    store.save_routes(
        [A2ARouteSnapshot(name="template", url="http://template", skills=["iac_generation"], tags=["ros"])]
    )

    assert store.load_routes() == [
        A2ARouteSnapshot(name="template", url="http://template", skills=["iac_generation"], tags=["ros"])
    ]
