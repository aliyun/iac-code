import asyncio
import json
import os
import threading
import uuid

import httpx
import pytest
from starlette.testclient import TestClient


def _error_message(response) -> str:
    error = response.json()["error"]
    return error["message"] if isinstance(error, dict) else error


def _task_cancellation_requested(task: asyncio.Task) -> bool:
    """Report a pending cancellation on Python 3.10 and newer."""
    cancelling = getattr(task, "cancelling", None)
    if callable(cancelling):
        return cancelling() > 0
    if bool(getattr(task, "_must_cancel", False)):
        return True
    waiter = getattr(task, "_fut_waiter", None)
    return isinstance(waiter, asyncio.Future) and waiter.cancelled()


def _mark_web_session(manager, cwd, session_id):
    """给已播种的会话补 web-session.json 侧车,使其在新语义下视为 web(非外来)会话。"""
    sidecar = manager.storage.session_dir(cwd, session_id) / "web-session.json"
    sidecar.write_text("{}", encoding="utf-8")


def _foreign_pipeline_session(tmp_path, *, session_id="foreign-pipeline"):
    from iac_code.agent.message import Message
    from iac_code.web.session_manager import WebSessionManager

    cwd = str(tmp_path / "foreign-project")
    projects_dir = tmp_path / "projects"
    seed = WebSessionManager(projects_dir=projects_dir, cwd=cwd)
    seed.storage.append(cwd, session_id, Message(role="user", content="foreign pipeline prompt"))
    display = seed.storage.session_dir(cwd, session_id) / "pipeline" / "display.jsonl"
    display.parent.mkdir(parents=True, exist_ok=True)
    display.write_text("{}\n", encoding="utf-8")

    manager = WebSessionManager(projects_dir=projects_dir, cwd=cwd)
    session = manager.get_session(session_id)
    assert session is not None
    assert manager.is_session_read_only(session) is True
    return manager, session


def test_web_app_exposes_health_and_index() -> None:
    from iac_code.web.app import create_app

    app = create_app()

    with TestClient(app) as client:
        health_response = client.get("/health")
        index_response = client.get("/")

    assert health_response.status_code == 200
    assert health_response.json() == {"service": "iac-code-web", "status": "ok"}
    assert index_response.status_code == 200
    assert "iac-code-web-root" in index_response.text
    assert "IaC Code Web" in index_response.text
    assert "/static/styles.css" in index_response.text
    assert "/static/js/app.js" in index_response.text


def test_index_marks_only_explicit_token_mode() -> None:
    from iac_code.web.app import create_app

    with TestClient(create_app()) as client:
        default_html = client.get("/").text
    with TestClient(create_app(token_mode=True)) as client:
        token_html = client.get("/").text

    assert 'data-token-mode="true"' not in default_html
    assert 'data-token-mode="true"' in token_html


def test_restart_endpoint_schedules_restart_and_returns_202(monkeypatch) -> None:
    from iac_code.web import server as web_server
    from iac_code.web.app import create_app

    calls = []
    monkeypatch.setattr(web_server, "schedule_restart", lambda **kw: calls.append(kw) or object())

    app = create_app()
    with TestClient(app) as client:
        response = client.post("/api/server/restart")

    assert response.status_code == 202
    assert response.json() == {"status": "restarting"}
    assert len(calls) == 1


def test_get_messages_seeds_event_buffer_above_visible_row_count(tmp_path) -> None:
    """重启后拉取转录须把 buffer 序号播种到可见行数之上,令后续实时事件排在存储行后(Issue 3)。"""
    from iac_code.agent.message import Message
    from iac_code.web.app import create_app
    from iac_code.web.session_manager import WebSessionManager

    cwd = str(tmp_path / "project")
    manager = WebSessionManager(projects_dir=tmp_path / "projects", cwd=cwd)
    session = manager.create_session(session_id="reload-seed")
    _mark_web_session(manager, cwd, session.session_id)
    # 模拟重启后的全新 buffer:三轮问答落成存储转录,而 buffer 从序号 1 重新计数。
    manager.storage.append(cwd, session.session_id, Message(role="user", content="hi"))
    manager.storage.append(cwd, session.session_id, Message(role="assistant", content="hello"))
    manager.storage.append(cwd, session.session_id, Message(role="user", content="again"))
    assert session.events.latest_sequence == 0

    app = create_app(session_manager=manager)
    with TestClient(app) as client:
        response = client.get("/api/sessions/{}/messages".format(session.web_session_id))

    assert response.status_code == 200
    visible = response.json()["messages"]
    assert len(visible) >= 1
    # 拉取转录后补发的实时事件必须排到最后一条存储行之后。
    live = session.events.append("pipeline.event", {"marker": "resumed"})
    assert live["sequence"] > len(visible)


@pytest.mark.asyncio
async def test_web_app_shutdown_cancels_active_turns_before_pipeline_runner(tmp_path) -> None:
    from iac_code.web.app import create_app
    from iac_code.web.session_manager import WebSessionManager

    turn_started = asyncio.Event()
    turn_canceled = asyncio.Event()

    class BlockingRuntime:
        async def start_turn(self, _request):
            turn_started.set()
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                turn_canceled.set()
                raise

    class RecordingPipelineRunner:
        active_turn_during_shutdown = True
        pending_permission_during_shutdown = True

        async def startup(self) -> None:
            return None

        async def shutdown(self) -> None:
            active_task = session.active_turn_task
            self.active_turn_during_shutdown = active_task is not None and not active_task.done()
            self.pending_permission_during_shutdown = bool(session.pending_permissions)

    manager = WebSessionManager(projects_dir=tmp_path / "projects", cwd=tmp_path / "project")
    session = manager.create_session(session_id="shutdown-active-turn")
    permission_future = asyncio.get_running_loop().create_future()
    runner = RecordingPipelineRunner()
    app = create_app(
        session_manager=manager,
        runtime_factory=lambda _session: BlockingRuntime(),
        pipeline_action_runner_factory=lambda: runner,
    )

    active_task = None
    try:
        async with app.router.lifespan_context(app):
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app),
                base_url="http://testserver",
            ) as client:
                response = await client.post(
                    "/api/sessions/{}/messages".format(session.web_session_id),
                    json={"text": "keep running", "imageIds": [], "fileRefs": []},
                )
            assert response.status_code == 202
            await asyncio.wait_for(turn_started.wait(), timeout=1)
            active_task = session.active_turn_task
            manager.add_permission_request(
                session,
                {"toolName": "bash"},
                future=permission_future,
            )

        canceled_before_test_cleanup = turn_canceled.is_set()
        active_after_shutdown = session.active_turn_task
        pending_after_shutdown = dict(session.pending_permissions)
    finally:
        if active_task is not None and not active_task.done():
            active_task.cancel()
            await asyncio.gather(active_task, return_exceptions=True)

    assert canceled_before_test_cleanup is True
    assert active_after_shutdown is None
    assert pending_after_shutdown == {}
    assert permission_future.done() is True
    assert permission_future.result() is False
    assert runner.active_turn_during_shutdown is False
    assert runner.pending_permission_during_shutdown is False


def test_web_app_shutdown_does_not_await_local_work_from_another_event_loop(tmp_path) -> None:
    from iac_code.web.app import create_app
    from iac_code.web.session_manager import WebSessionManager

    manager = WebSessionManager(projects_dir=tmp_path / "projects", cwd=tmp_path / "project")
    session = manager.create_session(session_id="shutdown-cross-loop-local-task")
    other_loop = asyncio.new_event_loop()
    local_task = other_loop.create_task(asyncio.sleep(60))
    session.active_local_tasks.add(local_task)
    app = create_app(session_manager=manager)

    try:
        with TestClient(app):
            pass
        other_loop.call_later(0.05, other_loop.stop)
        other_loop.run_forever()
        canceled_by_shutdown = local_task.cancelled()
    finally:
        if not local_task.done():
            local_task.cancel()
            try:
                other_loop.run_until_complete(local_task)
            except asyncio.CancelledError:
                pass
        other_loop.close()

    assert canceled_by_shutdown is True


def test_web_app_shutdown_leaves_a_non_running_event_loop_to_drain_its_cancellation(tmp_path) -> None:
    from iac_code.web.app import create_app
    from iac_code.web.session_manager import WebSessionManager

    cleanup_completed = threading.Event()
    other_loop = asyncio.new_event_loop()

    async def cleanup_after_cancellation() -> None:
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            await asyncio.sleep(0)
            cleanup_completed.set()
            raise

    local_task = other_loop.create_task(cleanup_after_cancellation())
    other_loop.run_until_complete(asyncio.sleep(0))
    manager = WebSessionManager(projects_dir=tmp_path / "projects", cwd=tmp_path / "project")
    session = manager.create_session(session_id="shutdown-non-running-loop-cleanup")
    session.active_local_tasks.add(local_task)

    class RecordingPipelineRunner:
        cancellation_executed_during_shutdown = True
        cleanup_completed_during_shutdown = True

        async def startup(self) -> None:
            return None

        async def shutdown(self) -> None:
            self.cancellation_executed_during_shutdown = _task_cancellation_requested(local_task)
            self.cleanup_completed_during_shutdown = cleanup_completed.is_set()

    runner = RecordingPipelineRunner()
    app = create_app(session_manager=manager, pipeline_action_runner_factory=lambda: runner)

    try:
        with TestClient(app):
            pass

        assert runner.cancellation_executed_during_shutdown is False
        assert runner.cleanup_completed_during_shutdown is False
        other_loop.run_until_complete(asyncio.gather(local_task, return_exceptions=True))
        assert cleanup_completed.is_set() is True
        assert local_task.cancelled() is True
    finally:
        if not local_task.done():
            local_task.cancel()
            try:
                other_loop.run_until_complete(local_task)
            except asyncio.CancelledError:
                pass
        other_loop.close()


def test_web_app_shutdown_does_not_drive_a_non_running_event_loop_with_multiple_tasks(tmp_path, monkeypatch) -> None:
    import iac_code.web.app as web_app
    from iac_code.web.session_manager import WebSessionManager

    other_loop = asyncio.new_event_loop()

    async def delay_cancellation_cleanup() -> None:
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            await asyncio.sleep(0.03)
            raise

    manager = WebSessionManager(projects_dir=tmp_path / "projects", cwd=tmp_path / "project")
    session = manager.create_session(session_id="shutdown-one-driver-per-stopped-loop")

    async def hold_admission_lock() -> None:
        await session.turn_admission_lock.acquire()
        try:
            await delay_cancellation_cleanup()
        finally:
            if session.turn_admission_lock.locked():
                session.turn_admission_lock.release()

    admission_owner = other_loop.create_task(hold_admission_lock())
    turn_task = other_loop.create_task(delay_cancellation_cleanup())
    local_task = other_loop.create_task(delay_cancellation_cleanup())
    tasks = (admission_owner, turn_task, local_task)
    other_loop.run_until_complete(asyncio.sleep(0))
    assert session.turn_admission_lock.owner_task is admission_owner
    session.active_turn_task = turn_task
    session.active_local_tasks.add(local_task)

    original_run_until_complete = other_loop.run_until_complete
    driver_call_count = 0

    def observed_run_until_complete(awaitable):
        nonlocal driver_call_count
        driver_call_count += 1
        return original_run_until_complete(awaitable)

    monkeypatch.setattr(other_loop, "run_until_complete", observed_run_until_complete)

    class RecordingPipelineRunner:
        cancellation_executed_during_shutdown = True

        async def startup(self) -> None:
            return None

        async def shutdown(self) -> None:
            self.cancellation_executed_during_shutdown = any(_task_cancellation_requested(task) for task in tasks)

    runner = RecordingPipelineRunner()
    app = web_app.create_app(session_manager=manager, pipeline_action_runner_factory=lambda: runner)
    monkeypatch.setattr(web_app, "WEB_SHUTDOWN_TASK_TIMEOUT_SECONDS", 0.1, raising=False)

    try:
        with TestClient(app):
            pass

        assert runner.cancellation_executed_during_shutdown is False
        assert driver_call_count == 0
    finally:
        monkeypatch.setattr(other_loop, "run_until_complete", original_run_until_complete)
        other_loop.run_until_complete(asyncio.gather(*tasks, return_exceptions=True))
        assert all(task.cancelled() for task in tasks)
        other_loop.close()


def test_web_app_shutdown_propagates_live_foreign_loop_cancellation_failures(tmp_path) -> None:
    from iac_code.web.app import create_app
    from iac_code.web.session_manager import WebSessionManager

    ready = threading.Event()
    loop_state = {}

    class FailingCancelFuture(asyncio.Future):
        def cancel(self, msg=None) -> bool:
            del msg
            raise RuntimeError("foreign cancellation failed")

    def run_foreign_loop() -> None:
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        future = FailingCancelFuture()
        loop_state.update(loop=loop, future=future)
        ready.set()
        loop.run_forever()
        loop.close()

    loop_thread = threading.Thread(target=run_foreign_loop)
    loop_thread.start()
    assert ready.wait(timeout=1)
    foreign_loop = loop_state["loop"]
    foreign_future = loop_state["future"]
    manager = WebSessionManager(projects_dir=tmp_path / "projects", cwd=tmp_path / "project")
    session = manager.create_session(session_id="shutdown-live-foreign-loop-cancel-failure")
    session.active_local_tasks.add(foreign_future)

    class RecordingPipelineRunner:
        shutdown_called = False

        async def startup(self) -> None:
            return None

        async def shutdown(self) -> None:
            self.shutdown_called = True

    runner = RecordingPipelineRunner()
    app = create_app(session_manager=manager, pipeline_action_runner_factory=lambda: runner)

    try:
        with pytest.raises(RuntimeError, match="foreign cancellation failed"):
            with TestClient(app):
                pass
        assert runner.shutdown_called is True
    finally:
        foreign_loop.call_soon_threadsafe(foreign_loop.stop)
        loop_thread.join(timeout=1)


def test_web_app_shutdown_cancels_work_on_a_live_foreign_event_loop(tmp_path) -> None:
    from iac_code.web.app import create_app
    from iac_code.web.session_manager import WebSessionManager

    ready = threading.Event()
    turn_cleanup_completed = threading.Event()
    local_cleanup_completed = threading.Event()
    loop_state = {}

    async def cleanup_after_cancellation(completed: threading.Event) -> None:
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            await asyncio.sleep(0.03)
            completed.set()

    def run_foreign_loop() -> None:
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        turn_task = loop.create_task(cleanup_after_cancellation(turn_cleanup_completed))
        local_task = loop.create_task(cleanup_after_cancellation(local_cleanup_completed))
        loop_state.update(loop=loop, turn_task=turn_task, local_task=local_task)
        ready.set()
        loop.run_forever()
        loop.close()

    loop_thread = threading.Thread(target=run_foreign_loop)
    loop_thread.start()
    assert ready.wait(timeout=1)
    foreign_loop = loop_state["loop"]
    turn_task = loop_state["turn_task"]
    local_task = loop_state["local_task"]
    manager = WebSessionManager(projects_dir=tmp_path / "projects", cwd=tmp_path / "project")
    session = manager.create_session(session_id="shutdown-live-foreign-loop-work")
    session.active_turn_task = turn_task
    session.active_local_tasks.add(local_task)

    class RecordingPipelineRunner:
        work_completed_during_shutdown = False

        async def startup(self) -> None:
            return None

        async def shutdown(self) -> None:
            self.work_completed_during_shutdown = turn_task.done() and local_task.done()

    runner = RecordingPipelineRunner()
    app = create_app(session_manager=manager, pipeline_action_runner_factory=lambda: runner)

    try:
        with TestClient(app):
            pass
        assert runner.work_completed_during_shutdown is True
        assert turn_cleanup_completed.wait(timeout=1)
        assert local_cleanup_completed.wait(timeout=1)
    finally:
        foreign_loop.call_soon_threadsafe(turn_task.cancel)
        foreign_loop.call_soon_threadsafe(local_task.cancel)
        turn_cleanup_completed.wait(timeout=1)
        local_cleanup_completed.wait(timeout=1)
        foreign_loop.call_soon_threadsafe(foreign_loop.stop)
        loop_thread.join(timeout=1)


def test_web_app_shutdown_recancels_timed_out_work_on_a_live_foreign_event_loop(tmp_path, monkeypatch) -> None:
    import iac_code.web.app as web_app
    from iac_code.web.session_manager import WebSessionManager

    ready = threading.Event()
    first_cancellation = threading.Event()
    second_cancellation = threading.Event()
    task_completed = threading.Event()
    loop_state = {}

    async def resist_first_cancellation() -> None:
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            first_cancellation.set()
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                second_cancellation.set()
                await asyncio.sleep(0.01)
                raise

    def run_foreign_loop() -> None:
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        task = loop.create_task(resist_first_cancellation())
        task.add_done_callback(lambda _task: task_completed.set())
        loop_state.update(loop=loop, task=task)
        ready.set()
        loop.run_forever()
        loop.close()

    loop_thread = threading.Thread(target=run_foreign_loop)
    loop_thread.start()
    assert ready.wait(timeout=1)
    foreign_loop = loop_state["loop"]
    foreign_task = loop_state["task"]
    manager = WebSessionManager(projects_dir=tmp_path / "projects", cwd=tmp_path / "project")
    session = manager.create_session(session_id="shutdown-timed-out-live-foreign-loop-work")
    session.active_turn_task = foreign_task

    class RecordingPipelineRunner:
        work_completed_during_shutdown = False

        async def startup(self) -> None:
            return None

        async def shutdown(self) -> None:
            self.work_completed_during_shutdown = foreign_task.done()

    runner = RecordingPipelineRunner()
    app = web_app.create_app(session_manager=manager, pipeline_action_runner_factory=lambda: runner)
    monkeypatch.setattr(web_app, "WEB_SHUTDOWN_TASK_TIMEOUT_SECONDS", 0.05, raising=False)

    try:
        with TestClient(app):
            pass
        assert first_cancellation.wait(timeout=1)
        assert second_cancellation.wait(timeout=1)
        assert runner.work_completed_during_shutdown is True
        assert foreign_task.cancelled() is True
    finally:
        foreign_loop.call_soon_threadsafe(foreign_task.cancel)
        second_cancellation.wait(timeout=1)
        task_completed.wait(timeout=1)
        foreign_loop.call_soon_threadsafe(foreign_loop.stop)
        loop_thread.join(timeout=1)


@pytest.mark.asyncio
async def test_web_app_shutdown_removes_completed_local_tasks(tmp_path) -> None:
    from iac_code.web.app import create_app
    from iac_code.web.session_manager import WebSessionManager

    manager = WebSessionManager(projects_dir=tmp_path / "projects", cwd=tmp_path / "project")
    session = manager.create_session(session_id="shutdown-completed-local-task")
    completed_task = asyncio.create_task(asyncio.sleep(0))
    await completed_task
    session.active_local_tasks.add(completed_task)
    app = create_app(session_manager=manager)

    async with app.router.lifespan_context(app):
        pass

    assert session.active_local_tasks == set()


@pytest.mark.asyncio
async def test_web_app_shutdown_times_out_uncooperative_active_work(tmp_path, monkeypatch) -> None:
    import iac_code.web.app as web_app
    from iac_code.web.session_manager import WebSessionManager

    release_task = asyncio.Event()
    task_started = asyncio.Event()

    class RecordingPipelineRunner:
        shutdown_called = False

        async def startup(self) -> None:
            return None

        async def shutdown(self) -> None:
            self.shutdown_called = True
            release_task.set()

    async def resist_first_cancellation() -> None:
        task_started.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            await release_task.wait()

    manager = WebSessionManager(projects_dir=tmp_path / "projects", cwd=tmp_path / "project")
    session = manager.create_session(session_id="shutdown-uncooperative-local-task")
    local_task = asyncio.create_task(resist_first_cancellation())
    session.active_local_tasks.add(local_task)
    runner = RecordingPipelineRunner()
    app = web_app.create_app(session_manager=manager, pipeline_action_runner_factory=lambda: runner)
    monkeypatch.setattr(web_app, "WEB_SHUTDOWN_TASK_TIMEOUT_SECONDS", 0.01, raising=False)
    await asyncio.wait_for(task_started.wait(), timeout=0.2)

    async def run_lifespan() -> None:
        async with app.router.lifespan_context(app):
            pass

    await asyncio.wait_for(run_lifespan(), timeout=0.2)
    await asyncio.gather(local_task, return_exceptions=True)
    assert runner.shutdown_called is True
    assert local_task.cancelled() is True
    assert session.active_local_tasks == set()


@pytest.mark.asyncio
async def test_web_app_shutdown_recancels_and_detaches_uncooperative_active_work(tmp_path, monkeypatch) -> None:
    import iac_code.web.app as web_app
    from iac_code.web.session_manager import WebSessionManager

    task_started = asyncio.Event()
    first_cancellation = asyncio.Event()
    second_cancellation = asyncio.Event()

    class RecordingPipelineRunner:
        active_turn_was_detached = False

        async def startup(self) -> None:
            return None

        async def shutdown(self) -> None:
            self.active_turn_was_detached = session.active_turn_task is None

    async def resist_first_cancellation() -> None:
        task_started.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            first_cancellation.set()
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                second_cancellation.set()
                raise

    manager = WebSessionManager(projects_dir=tmp_path / "projects", cwd=tmp_path / "project")
    session = manager.create_session(session_id="shutdown-uncooperative-active-turn")
    await session.turn_admission_lock.acquire()
    active_task = asyncio.create_task(resist_first_cancellation())
    session.active_turn_task = active_task
    runner = RecordingPipelineRunner()
    app = web_app.create_app(session_manager=manager, pipeline_action_runner_factory=lambda: runner)
    monkeypatch.setattr(web_app, "WEB_SHUTDOWN_TASK_TIMEOUT_SECONDS", 0.01, raising=False)
    await asyncio.wait_for(task_started.wait(), timeout=0.2)

    try:
        async with app.router.lifespan_context(app):
            pass

        assert first_cancellation.is_set() is True
        assert second_cancellation.is_set() is True
        assert active_task.cancelled() is True
        assert session.active_turn_task is None
        assert session.turn_admission_lock.locked() is False
        assert runner.active_turn_was_detached is True
    finally:
        if not active_task.done():
            active_task.cancel()
            await asyncio.gather(active_task, return_exceptions=True)
        if session.turn_admission_lock.locked():
            session.turn_admission_lock.release()


def test_web_app_shutdown_ignores_pending_requests_from_a_closed_event_loop(tmp_path) -> None:
    from iac_code.web.app import create_app
    from iac_code.web.session_manager import WebSessionManager

    class RecordingPipelineRunner:
        shutdown_called = False

        async def startup(self) -> None:
            return None

        async def shutdown(self) -> None:
            self.shutdown_called = True

    manager = WebSessionManager(projects_dir=tmp_path / "projects", cwd=tmp_path / "project")
    session = manager.create_session(session_id="shutdown-closed-loop-pending")
    other_loop = asyncio.new_event_loop()
    permission_future = other_loop.create_future()
    question_future = other_loop.create_future()
    permission_future.add_done_callback(lambda _future: None)
    question_future.add_done_callback(lambda _future: None)
    manager.add_permission_request(session, {"toolName": "bash"}, future=permission_future)
    manager.add_question_request(session, {"questions": []}, future=question_future)
    other_loop.close()
    runner = RecordingPipelineRunner()
    app = create_app(session_manager=manager, pipeline_action_runner_factory=lambda: runner)

    with TestClient(app):
        pass

    assert session.pending_permissions == {}
    assert session.pending_questions == {}
    assert runner.shutdown_called is True


def test_web_app_shutdown_cancels_closed_loop_active_and_admission_work(tmp_path) -> None:
    from iac_code.web.app import create_app
    from iac_code.web.session_manager import WebSessionManager

    manager = WebSessionManager(projects_dir=tmp_path / "projects", cwd=tmp_path / "project")
    session = manager.create_session(session_id="shutdown-closed-loop-active-work")
    other_loop = asyncio.new_event_loop()
    active_future = other_loop.create_future()

    async def hold_admission_lock() -> None:
        await session.turn_admission_lock.acquire()
        try:
            await asyncio.Event().wait()
        finally:
            if session.turn_admission_lock.locked():
                session.turn_admission_lock.release()

    admission_owner = other_loop.create_task(hold_admission_lock())
    other_loop.run_until_complete(asyncio.sleep(0))
    assert session.turn_admission_lock.owner_task is admission_owner
    session.active_turn_task = active_future
    other_loop.close()

    class RecordingPipelineRunner:
        active_future_was_cancelled = False
        admission_owner_was_cancelled = False

        async def startup(self) -> None:
            return None

        async def shutdown(self) -> None:
            self.active_future_was_cancelled = active_future.cancelled()
            self.admission_owner_was_cancelled = _task_cancellation_requested(admission_owner)

    runner = RecordingPipelineRunner()
    app = create_app(session_manager=manager, pipeline_action_runner_factory=lambda: runner)

    try:
        with TestClient(app):
            pass

        assert runner.active_future_was_cancelled is True
        assert runner.admission_owner_was_cancelled is True
        assert session.turn_admission_lock.locked() is False
    finally:
        admission_owner.get_coro().close()
        admission_owner._log_destroy_pending = False


def test_web_app_shutdown_resolves_pending_requests_on_a_live_foreign_event_loop(tmp_path) -> None:
    from iac_code.web.app import create_app
    from iac_code.web.session_manager import WebSessionManager

    class RecordingPipelineRunner:
        shutdown_called = False

        async def startup(self) -> None:
            return None

        async def shutdown(self) -> None:
            self.shutdown_called = True

    ready = threading.Event()
    resolved = threading.Event()
    loop_state = {}

    def run_foreign_loop() -> None:
        loop = asyncio.new_event_loop()
        loop.set_debug(True)
        asyncio.set_event_loop(loop)
        future = loop.create_future()
        future.add_done_callback(lambda _future: resolved.set())
        loop_state.update(loop=loop, future=future)
        ready.set()
        loop.run_forever()
        loop.close()

    loop_thread = threading.Thread(target=run_foreign_loop)
    loop_thread.start()
    assert ready.wait(timeout=1)
    foreign_loop = loop_state["loop"]
    permission_future = loop_state["future"]
    manager = WebSessionManager(projects_dir=tmp_path / "projects", cwd=tmp_path / "project")
    session = manager.create_session(session_id="shutdown-live-foreign-loop-pending")
    manager.add_permission_request(session, {"toolName": "bash"}, future=permission_future)
    runner = RecordingPipelineRunner()
    app = create_app(session_manager=manager, pipeline_action_runner_factory=lambda: runner)

    try:
        with TestClient(app):
            pass
        assert resolved.wait(timeout=1)
        assert permission_future.result() is False
        assert runner.shutdown_called is True
    finally:
        foreign_loop.call_soon_threadsafe(foreign_loop.stop)
        loop_thread.join(timeout=1)


def test_web_app_shutdown_runs_pipeline_runner_when_session_cleanup_fails(tmp_path, monkeypatch) -> None:
    from iac_code.web.app import create_app
    from iac_code.web.session_manager import WebSessionManager

    class RecordingPipelineRunner:
        shutdown_called = False

        async def startup(self) -> None:
            return None

        async def shutdown(self) -> None:
            self.shutdown_called = True

    manager = WebSessionManager(projects_dir=tmp_path / "projects", cwd=tmp_path / "project")
    manager.create_session(session_id="shutdown-cleanup-failure")
    runner = RecordingPipelineRunner()
    app = create_app(session_manager=manager, pipeline_action_runner_factory=lambda: runner)

    def fail_session_cleanup(_session) -> None:
        raise RuntimeError("session cleanup failed")

    monkeypatch.setattr(
        manager,
        "cancel_pending_requests_for_session",
        fail_session_cleanup,
    )

    with pytest.raises(RuntimeError, match="session cleanup failed"):
        with TestClient(app):
            pass

    assert runner.shutdown_called is True


@pytest.mark.asyncio
async def test_web_app_shutdown_finalizes_all_sessions_when_pending_request_cleanup_fails(
    tmp_path,
    monkeypatch,
) -> None:
    from iac_code.web.app import create_app
    from iac_code.web.session_manager import WebSessionManager

    class RecordingPipelineRunner:
        shutdown_called = False

        async def startup(self) -> None:
            return None

        async def shutdown(self) -> None:
            self.shutdown_called = True

    manager = WebSessionManager(projects_dir=tmp_path / "projects", cwd=tmp_path / "project")
    sessions = [
        manager.create_session(session_id="shutdown-cleanup-failure-first"),
        manager.create_session(session_id="shutdown-cleanup-failure-second"),
    ]
    tasks = []
    for session in sessions:
        await session.turn_admission_lock.acquire()
        turn_task = asyncio.create_task(asyncio.Event().wait())
        local_task = asyncio.create_task(asyncio.Event().wait())
        session.active_turn_task = turn_task
        session.active_local_tasks.add(local_task)
        tasks.extend((turn_task, local_task))

    cleaned_session_ids = []

    def fail_first_session_cleanup(session) -> None:
        cleaned_session_ids.append(session.web_session_id)
        if session is sessions[0]:
            raise RuntimeError("session cleanup failed")

    monkeypatch.setattr(
        manager,
        "cancel_pending_requests_for_session",
        fail_first_session_cleanup,
    )
    runner = RecordingPipelineRunner()
    app = create_app(session_manager=manager, pipeline_action_runner_factory=lambda: runner)

    try:
        with pytest.raises(RuntimeError, match="session cleanup failed"):
            async with app.router.lifespan_context(app):
                pass

        assert cleaned_session_ids == [session.web_session_id for session in sessions]
        assert all(task.cancelled() for task in tasks)
        assert all(session.active_turn_task is None for session in sessions)
        assert all(session.active_local_tasks == set() for session in sessions)
        assert all(session.turn_admission_lock.locked() is False for session in sessions)
        assert runner.shutdown_called is True
    finally:
        for task in tasks:
            if not task.done():
                task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        for session in sessions:
            if session.turn_admission_lock.locked():
                session.turn_admission_lock.release()


def test_web_app_shutdown_releases_live_foreign_loop_lock_without_masking_cleanup_failure(
    tmp_path,
    monkeypatch,
) -> None:
    from iac_code.web.app import create_app
    from iac_code.web.session_manager import WebSessionManager

    class RecordingPipelineRunner:
        shutdown_called = False

        async def startup(self) -> None:
            return None

        async def shutdown(self) -> None:
            self.shutdown_called = True

    manager = WebSessionManager(projects_dir=tmp_path / "projects", cwd=tmp_path / "project")
    foreign_session = manager.create_session(session_id="shutdown-foreign-loop-lock")
    later_session = manager.create_session(session_id="shutdown-after-foreign-loop-lock")
    later_loop = asyncio.new_event_loop()
    later_turn = later_loop.create_future()
    later_turn.set_result(None)
    later_loop.close()
    later_session.active_turn_task = later_turn

    ready = threading.Event()
    waiter_finished = threading.Event()
    loop_state = {}

    def run_foreign_loop() -> None:
        loop = asyncio.new_event_loop()
        loop.set_debug(True)
        asyncio.set_event_loop(loop)

        async def wait_for_admission() -> None:
            acquired = False
            try:
                await foreign_session.turn_admission_lock.acquire()
                acquired = True
            finally:
                if acquired and foreign_session.turn_admission_lock.locked():
                    foreign_session.turn_admission_lock.release()
                waiter_finished.set()

        async def prepare_lock() -> None:
            await foreign_session.turn_admission_lock.acquire()
            waiter = asyncio.create_task(wait_for_admission())
            await asyncio.sleep(0)
            loop_state.update(loop=loop, waiter=waiter)
            ready.set()

        loop.run_until_complete(prepare_lock())
        loop.run_forever()
        loop.close()

    loop_thread = threading.Thread(target=run_foreign_loop)
    loop_thread.start()
    assert ready.wait(timeout=1)
    foreign_loop = loop_state["loop"]
    waiter = loop_state["waiter"]

    def fail_first_session_cleanup(session) -> None:
        if session is foreign_session:
            raise RuntimeError("session cleanup failed")

    monkeypatch.setattr(
        manager,
        "cancel_pending_requests_for_session",
        fail_first_session_cleanup,
    )
    runner = RecordingPipelineRunner()
    app = create_app(session_manager=manager, pipeline_action_runner_factory=lambda: runner)

    try:
        with pytest.raises(RuntimeError, match="session cleanup failed"):
            with TestClient(app):
                pass

        assert waiter_finished.wait(timeout=1)
        assert foreign_session.turn_admission_lock.locked() is False
        assert later_session.active_turn_task is None
        assert runner.shutdown_called is True
    finally:
        if not waiter.done():
            foreign_loop.call_soon_threadsafe(waiter.cancel)
        waiter_finished.wait(timeout=1)
        foreign_loop.call_soon_threadsafe(foreign_loop.stop)
        loop_thread.join(timeout=1)


def test_web_app_shutdown_cancels_live_foreign_admission_owner_before_releasing_lock(
    tmp_path,
    monkeypatch,
) -> None:
    from iac_code.web.app import create_app
    from iac_code.web.session_manager import WebSessionManager

    class RecordingPipelineRunner:
        shutdown_called = False
        owner_finished_during_shutdown = False

        async def startup(self) -> None:
            return None

        async def shutdown(self) -> None:
            self.shutdown_called = True
            self.owner_finished_during_shutdown = owner_finished.is_set()

    manager = WebSessionManager(projects_dir=tmp_path / "projects", cwd=tmp_path / "project")
    session = manager.create_session(session_id="shutdown-foreign-admission-owner")
    ready = threading.Event()
    owner_finished = threading.Event()
    loop_state = {}

    def run_foreign_loop() -> None:
        loop = asyncio.new_event_loop()
        loop.set_debug(True)
        asyncio.set_event_loop(loop)

        async def hold_admission_lock() -> None:
            await session.turn_admission_lock.acquire()
            ready.set()
            try:
                await asyncio.Event().wait()
            finally:
                if session.turn_admission_lock.locked():
                    session.turn_admission_lock.release()
                owner_finished.set()

        owner = loop.create_task(hold_admission_lock())
        loop_state.update(loop=loop, owner=owner)
        loop.run_forever()
        loop.close()

    loop_thread = threading.Thread(target=run_foreign_loop)
    loop_thread.start()
    assert ready.wait(timeout=1)
    foreign_loop = loop_state["loop"]
    owner = loop_state["owner"]

    def fail_session_cleanup(_session) -> None:
        raise RuntimeError("session cleanup failed")

    monkeypatch.setattr(
        manager,
        "cancel_pending_requests_for_session",
        fail_session_cleanup,
    )
    runner = RecordingPipelineRunner()
    app = create_app(session_manager=manager, pipeline_action_runner_factory=lambda: runner)

    try:
        with pytest.raises(RuntimeError, match="session cleanup failed"):
            with TestClient(app):
                pass

        assert owner_finished.wait(timeout=1)
        assert owner.cancelled() is True
        assert session.turn_admission_lock.locked() is False
        assert runner.shutdown_called is True
        assert runner.owner_finished_during_shutdown is True
    finally:
        if not owner.done():
            foreign_loop.call_soon_threadsafe(owner.cancel)
        owner_finished.wait(timeout=1)
        foreign_loop.call_soon_threadsafe(foreign_loop.stop)
        loop_thread.join(timeout=1)


def test_web_app_shutdown_preserves_session_cleanup_failure_when_runner_also_fails(tmp_path, monkeypatch) -> None:
    from iac_code.web.app import create_app
    from iac_code.web.session_manager import WebSessionManager

    class FailingPipelineRunner:
        shutdown_called = False

        async def startup(self) -> None:
            return None

        async def shutdown(self) -> None:
            self.shutdown_called = True
            raise RuntimeError("runner shutdown failed")

    manager = WebSessionManager(projects_dir=tmp_path / "projects", cwd=tmp_path / "project")
    manager.create_session(session_id="shutdown-double-failure")
    runner = FailingPipelineRunner()
    app = create_app(session_manager=manager, pipeline_action_runner_factory=lambda: runner)

    def fail_session_cleanup(_session) -> None:
        raise RuntimeError("session cleanup failed")

    monkeypatch.setattr(
        manager,
        "cancel_pending_requests_for_session",
        fail_session_cleanup,
    )

    with pytest.raises(RuntimeError, match="session cleanup failed"):
        with TestClient(app):
            pass

    assert runner.shutdown_called is True


@pytest.mark.asyncio
async def test_web_app_shutdown_preserves_lifespan_body_failure_when_runner_also_fails(tmp_path) -> None:
    from iac_code.web.app import create_app
    from iac_code.web.session_manager import WebSessionManager

    class FailingPipelineRunner:
        shutdown_called = False

        async def startup(self) -> None:
            return None

        async def shutdown(self) -> None:
            self.shutdown_called = True
            raise RuntimeError("runner shutdown failed")

    manager = WebSessionManager(projects_dir=tmp_path / "projects", cwd=tmp_path / "project")
    runner = FailingPipelineRunner()
    app = create_app(session_manager=manager, pipeline_action_runner_factory=lambda: runner)

    with pytest.raises(RuntimeError, match="lifespan body failed"):
        async with app.router.lifespan_context(app):
            raise RuntimeError("lifespan body failed")

    assert runner.shutdown_called is True


@pytest.mark.asyncio
async def test_web_app_shutdown_runs_pipeline_runner_when_startup_fails(tmp_path) -> None:
    from iac_code.web.app import create_app
    from iac_code.web.session_manager import WebSessionManager

    class FailingStartupPipelineRunner:
        shutdown_called = False

        async def startup(self) -> None:
            raise RuntimeError("runner startup failed")

        async def shutdown(self) -> None:
            self.shutdown_called = True

    manager = WebSessionManager(projects_dir=tmp_path / "projects", cwd=tmp_path / "project")
    runner = FailingStartupPipelineRunner()
    app = create_app(session_manager=manager, pipeline_action_runner_factory=lambda: runner)

    with pytest.raises(RuntimeError, match="runner startup failed"):
        async with app.router.lifespan_context(app):
            pass

    assert runner.shutdown_called is True


def test_web_app_serves_static_assets() -> None:
    from iac_code.web.app import create_app

    app = create_app()

    with TestClient(app) as client:
        css_response = client.get("/static/styles.css")
        js_response = client.get("/static/js/app.js")

    assert css_response.status_code == 200
    assert "text/css" in css_response.headers["content-type"]
    assert js_response.status_code == 200
    assert "javascript" in js_response.headers["content-type"]


def test_web_app_disables_browser_cache_for_local_static_assets() -> None:
    from iac_code.web.app import create_app

    app = create_app()

    with TestClient(app) as client:
        index_response = client.get("/")
        css_response = client.get("/static/styles.css")
        js_response = client.get("/static/js/app.js")

    assert index_response.headers["cache-control"] == "no-store"
    assert css_response.headers["cache-control"] == "no-store"
    assert js_response.headers["cache-control"] == "no-store"


def test_session_routes_create_list_and_get_session(tmp_path) -> None:
    from iac_code.services.session_metadata import SESSION_JSONL_FILENAME, SESSION_METADATA_FILENAME
    from iac_code.web.app import create_app
    from iac_code.web.session_manager import WebSessionManager

    cwd = str(tmp_path / "project")
    manager = WebSessionManager(projects_dir=tmp_path / "projects")
    app = create_app(session_manager=manager)

    with TestClient(app) as client:
        create_response = client.post("/api/sessions", json={"cwd": cwd, "mode": "pipeline", "pipelineName": "selling"})
        session_id = create_response.json()["sessionId"]
        list_response = client.get("/api/sessions")
        detail_response = client.get(f"/api/sessions/{session_id}")

    assert create_response.status_code == 201
    created = create_response.json()
    assert created["sessionId"] == session_id
    assert created["cwd"] == cwd
    assert created["mode"] == "pipeline"
    assert created["pipelineName"] == "selling"
    assert created["status"] == "idle"
    assert created["title"] == "(empty)"
    assert created["draft"] == ""
    assert created["permissionMode"] == "default"
    assert created["pendingPermissionCount"] == 0
    assert created["pendingQuestionCount"] == 0
    assert created["createdAt"]
    assert created["updatedAt"]
    assert list_response.status_code == 200
    assert list_response.json()["sessions"] == [created]
    assert detail_response.status_code == 200
    detail = detail_response.json()
    assert detail["sessionId"] == created["sessionId"]
    assert detail["cwd"] == created["cwd"]
    assert detail["mode"] == created["mode"]
    assert "contextUsage" in detail
    assert detail["contextUsage"]["contextWindow"] >= 128_000
    assert (manager.storage.session_dir(cwd, session_id) / SESSION_JSONL_FILENAME).exists()
    assert (manager.storage.session_dir(cwd, session_id) / SESSION_METADATA_FILENAME).exists()
    events = manager.get_session(session_id).events.replay_after(0)
    assert events[0]["type"] == "session.started"
    assert events[0]["payload"]["sessionId"] == session_id


def test_foreign_pipeline_routes_reject_mutation_but_allow_pin_and_archive(tmp_path) -> None:
    from iac_code.web.app import create_app

    manager, session = _foreign_pipeline_session(tmp_path)
    app = create_app(session_manager=manager)
    session_ref = session.web_session_id

    with TestClient(app) as client:
        rejected = [
            client.patch(f"/api/sessions/{session_ref}", json={"title": "changed"}),
            client.put(f"/api/sessions/{session_ref}/permission-mode", json={"mode": "dont_ask"}),
            client.put(
                f"/api/sessions/{session_ref}/model",
                json={"provider": "openai", "model": "gpt-5"},
            ),
            client.delete(f"/api/sessions/{session_ref}/model"),
            client.post(f"/api/sessions/{session_ref}/compact"),
            client.post(
                f"/api/sessions/{session_ref}/images",
                json={"mediaType": "image/png", "data": "ZmFrZQ=="},
            ),
            client.post(f"/api/sessions/{session_ref}/commands", json={"command": "!echo no"}),
            client.post(f"/api/sessions/{session_ref}/queued-inputs", json={"text": "no"}),
            client.post(f"/api/sessions/{session_ref}/interrupt", json={"message": "no"}),
            client.delete(f"/api/sessions/{session_ref}"),
        ]
        mixed = client.patch(
            f"/api/sessions/{session_ref}",
            json={"pinned": True, "title": "changed"},
        )
        pin = client.patch(f"/api/sessions/{session_ref}", json={"pinned": True})
        archive = client.patch(f"/api/sessions/{session_ref}", json={"archived": True})
        status = client.post(f"/api/sessions/{session_ref}/commands", json={"command": "/status"})
        delete_archived = client.delete("/api/sessions/archived")

    assert all(response.status_code == 409 for response in rejected)
    assert all(response.json()["error"]["code"] == "foreign_read_only" for response in rejected)
    assert mixed.status_code == 409
    assert session.title != "changed"
    assert pin.status_code == 200
    assert archive.status_code == 200
    assert session.archived is True
    assert session.pinned is False
    assert session.origin == "foreign"
    assert status.status_code == 200
    assert delete_archived.status_code == 200
    assert delete_archived.json() == {"deleted": 0}
    assert manager.storage.exists(session.cwd, session.session_id) is True


def test_delete_session_rejects_an_active_turn_without_removing_storage(tmp_path) -> None:
    from iac_code.agent.message import Message
    from iac_code.web.app import create_app
    from iac_code.web.session_manager import WebSessionManager

    async def scenario() -> tuple[httpx.Response, bool]:
        manager = WebSessionManager(projects_dir=tmp_path / "projects")
        session = manager.create_session(cwd=str(tmp_path / "project"), session_id="active-delete")
        manager.storage.append(session.cwd, session.session_id, Message(role="user", content="keep me"))
        active_turn = asyncio.create_task(asyncio.Event().wait())
        session.active_turn_task = active_turn
        app = create_app(session_manager=manager)
        try:
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app),
                base_url="http://testserver",
            ) as client:
                response = await client.delete("/api/sessions/{}".format(session.web_session_id))
            return response, manager.storage.exists(session.cwd, session.session_id)
        finally:
            active_turn.cancel()
            with pytest.raises(asyncio.CancelledError):
                await active_turn

    response, storage_exists = asyncio.run(scenario())

    assert response.status_code == 409
    assert response.json() == {
        "deleted": False,
        "sessionId": "active-delete",
        "reason": "turn already running",
    }
    assert storage_exists is True


def test_deleted_pipeline_session_cannot_start_a_waiting_message_and_recreate_storage(tmp_path) -> None:
    from types import SimpleNamespace

    from iac_code.agent.message import Message
    from iac_code.web.app import create_app
    from iac_code.web.session_manager import WebSessionManager

    class RecordingRunner:
        def __init__(self) -> None:
            self.start_calls = 0

        async def start(self, *_args, **_kwargs):
            self.start_calls += 1
            return SimpleNamespace(
                accepted=True,
                status_code=202,
                response={"accepted": True},
                events=[],
                terminal_outcome=None,
            )

    async def scenario() -> tuple[httpx.Response, httpx.Response, bool, int]:
        cwd = str(tmp_path / "project")
        manager = WebSessionManager(projects_dir=tmp_path / "projects", cwd=cwd)
        session = manager.create_session(
            cwd=cwd,
            mode="pipeline",
            pipeline_name="selling",
            session_id="delete-before-pipeline-message",
        )
        manager.storage.append(cwd, session.session_id, Message(role="user", content="seed"))
        runner = RecordingRunner()
        app = create_app(session_manager=manager, pipeline_action_runner_factory=lambda: runner)

        await session.turn_admission_lock.acquire()
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://testserver",
        ) as client:
            delete_task = asyncio.create_task(client.delete("/api/sessions/{}".format(session.web_session_id)))
            await asyncio.sleep(0)
            message_task = asyncio.create_task(
                client.post(
                    "/api/sessions/{}/messages".format(session.web_session_id),
                    json={"text": "must not run", "imageIds": [], "fileRefs": []},
                )
            )
            await asyncio.sleep(0)
            session.turn_admission_lock.release()
            delete_response, message_response = await asyncio.gather(delete_task, message_task)

        return (
            delete_response,
            message_response,
            manager.storage.exists(cwd, session.session_id),
            runner.start_calls,
        )

    delete_response, message_response, storage_exists, start_calls = asyncio.run(scenario())

    assert delete_response.status_code == 200
    assert delete_response.json()["deleted"] is True
    assert message_response.status_code == 404
    assert message_response.json() == {"error": {"message": "session not found"}}
    assert storage_exists is False
    assert start_calls == 0


def test_delete_archived_sessions_skips_sessions_with_active_turns(tmp_path) -> None:
    from iac_code.agent.message import Message
    from iac_code.web.app import create_app
    from iac_code.web.session_manager import WebSessionManager

    async def scenario() -> tuple[httpx.Response, bool]:
        manager = WebSessionManager(projects_dir=tmp_path / "projects")
        session = manager.create_session(cwd=str(tmp_path / "project"), session_id="active-archived-delete")
        manager.storage.append(session.cwd, session.session_id, Message(role="user", content="keep me"))
        session.archived = True
        manager.persist_web_metadata(session)
        active_turn = asyncio.create_task(asyncio.Event().wait())
        session.active_turn_task = active_turn
        app = create_app(session_manager=manager)
        try:
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app),
                base_url="http://testserver",
            ) as client:
                response = await client.delete("/api/sessions/archived")
            return response, manager.storage.exists(session.cwd, session.session_id)
        finally:
            active_turn.cancel()
            with pytest.raises(asyncio.CancelledError):
                await active_turn

    response, storage_exists = asyncio.run(scenario())

    assert response.status_code == 200
    assert response.json() == {"deleted": 0}
    assert storage_exists is True


def test_queued_input_delete_and_edit_endpoints(tmp_path) -> None:
    from iac_code.web.app import create_app
    from iac_code.web.session_manager import WebSessionManager

    cwd = str(tmp_path / "project")
    manager = WebSessionManager(projects_dir=tmp_path / "projects")
    session = manager.create_session(cwd=cwd, session_id="session-1")
    session.queued_inputs = ["第一条", "第二条", "第三条"]
    app = create_app(session_manager=manager)

    with TestClient(app) as client:
        # 编辑第二条。
        patch_response = client.patch(
            f"/api/sessions/{session.session_id}/queued-inputs/1",
            json={"text": "第二条改", "expectedText": "第二条"},
        )
        # 删除第一条。
        delete_response = client.request(
            "DELETE",
            f"/api/sessions/{session.session_id}/queued-inputs/0",
            json={"expectedText": "第一条"},
        )

    assert patch_response.status_code == 200
    assert patch_response.json() == {"updated": True, "index": 1, "text": "第二条改"}
    assert delete_response.status_code == 200
    assert delete_response.json() == {"removed": True, "index": 0}
    assert session.queued_inputs == ["第二条改", "第三条"]

    event_types = [event["type"] for event in session.events.replay_after(0)]
    assert "queued-input.updated" in event_types
    assert "queued-input.removed" in event_types


def test_queued_input_action_guards_return_conflict_and_bad_request(tmp_path) -> None:
    from iac_code.web.app import create_app
    from iac_code.web.session_manager import WebSessionManager

    cwd = str(tmp_path / "project")
    manager = WebSessionManager(projects_dir=tmp_path / "projects")
    session = manager.create_session(cwd=cwd, session_id="session-1")
    session.queued_inputs = ["第一条"]
    app = create_app(session_manager=manager)

    with TestClient(app) as client:
        # expectedText 不匹配 → 409。
        stale = client.request(
            "DELETE",
            f"/api/sessions/{session.session_id}/queued-inputs/0",
            json={"expectedText": "不是这条"},
        )
        # 越界 → 409。
        out_of_range = client.request(
            "DELETE",
            f"/api/sessions/{session.session_id}/queued-inputs/5",
            json={"expectedText": "第一条"},
        )
        # index 非整数 → 400。
        bad_index = client.request(
            "DELETE",
            f"/api/sessions/{session.session_id}/queued-inputs/abc",
            json={"expectedText": "第一条"},
        )
        # session 不存在 → 404。
        missing = client.request(
            "DELETE",
            "/api/sessions/does-not-exist/queued-inputs/0",
            json={"expectedText": "第一条"},
        )

    assert stale.status_code == 409
    assert out_of_range.status_code == 409
    assert bad_index.status_code == 400
    assert missing.status_code == 404
    # 校验失败不得改动队列。
    assert session.queued_inputs == ["第一条"]


def test_queued_input_steer_without_active_turn_returns_conflict(tmp_path) -> None:
    from iac_code.web.app import create_app
    from iac_code.web.session_manager import WebSessionManager

    cwd = str(tmp_path / "project")
    manager = WebSessionManager(projects_dir=tmp_path / "projects")
    session = manager.create_session(cwd=cwd, session_id="session-1")
    session.queued_inputs = ["插队消息"]
    app = create_app(session_manager=manager)

    with TestClient(app) as client:
        response = client.post(
            f"/api/sessions/{session.session_id}/queued-inputs/0/steer",
            json={"expectedText": "插队消息"},
        )

    assert response.status_code == 409
    # 无活跃 turn 时不得移除排队条。
    assert session.queued_inputs == ["插队消息"]


def test_queued_inputs_processed_one_at_a_time_after_turn(tmp_path) -> None:
    """排队消息应在本轮结束后逐条、各自独立成 turn 依次处理,而不是一次性批量注入本轮。"""
    from iac_code.web.app import create_app
    from iac_code.web.runtime import WebTurnRequest
    from iac_code.web.session_manager import WebSessionManager

    async def run() -> tuple[list, list[str], list[dict]]:
        manager = WebSessionManager(projects_dir=tmp_path / "projects")
        session = manager.create_session(session_id="session-1")

        first_started = asyncio.Event()
        release_first = asyncio.Event()
        requests: list[WebTurnRequest] = []
        effective_turn_ids: list[str] = []

        class RecordingRuntime:
            async def start_turn(self, request: WebTurnRequest) -> dict[str, object]:
                requests.append(request)
                # 复刻真实 runtime:每轮 turn_id = request.turn_id or 新生成,保证各轮独立。
                turn_id = request.turn_id or uuid.uuid4().hex
                effective_turn_ids.append(turn_id)
                if request.source == "composer":
                    # 首轮阻塞,期间连发两条进入队列。
                    first_started.set()
                    await release_first.wait()
                return {"accepted": True, "turnId": turn_id}

        app = create_app(session_manager=manager, runtime_factory=lambda _session: RecordingRuntime())

        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://testserver") as client:
            await client.post("/api/sessions/session-1/messages", json={"text": "第一条"})
            await asyncio.wait_for(first_started.wait(), timeout=1)
            await client.post("/api/sessions/session-1/queued-inputs", json={"text": "第二条"})
            await client.post("/api/sessions/session-1/queued-inputs", json={"text": "第三条"})
            release_first.set()
            active = manager.get_session("session-1").active_turn_task
            if active is not None:
                await asyncio.wait_for(active, timeout=1)
            return requests, effective_turn_ids, list(session.queued_inputs), session.events.replay_after(0)

    requests, effective_turn_ids, remaining_queue, events = asyncio.run(run())

    # 三条各自独立成 turn,按顺序 start_turn 三次。
    assert [request.text for request in requests] == ["第一条", "第二条", "第三条"]
    # 首条来自 composer,后两条来自队列。
    assert [request.source for request in requests] == ["composer", "queued", "queued"]
    # 每个排队轮都有稳定 turn_id,供取消/恢复路径追踪输入消费所有权。
    assert all(request.turn_id for request in requests)
    assert effective_turn_ids == [request.turn_id for request in requests]
    # 每个 turn 有各自不同的 turnId(独立气泡 / 独立 turn.done)。
    assert len(set(effective_turn_ids)) == 3
    # 队列已排空。
    assert remaining_queue == []
    # 每条排空都发 queued-input.removed 以移除对应 chip。
    removed = [event for event in events if event["type"] == "queued-input.removed"]
    assert len(removed) == 2


def test_queued_inputs_not_drained_when_turn_not_accepted(tmp_path) -> None:
    """本轮被中断 / 失败 / 拒绝(accepted=False)时停止排空,剩余排队消息保留不丢。"""
    from iac_code.web.app import create_app
    from iac_code.web.runtime import WebTurnRequest
    from iac_code.web.session_manager import WebSessionManager

    async def run() -> tuple[list, list[str]]:
        manager = WebSessionManager(projects_dir=tmp_path / "projects")
        session = manager.create_session(session_id="session-1")
        # 队列预置一条:若本轮未正常完成,不应被排空。
        session.queued_inputs = ["排队保留"]

        requests: list[WebTurnRequest] = []

        class RejectingRuntime:
            async def start_turn(self, request: WebTurnRequest) -> dict[str, object]:
                requests.append(request)
                return {"accepted": False, "reason": "turn canceled", "turnId": request.turn_id or "turn-x"}

        app = create_app(session_manager=manager, runtime_factory=lambda _session: RejectingRuntime())

        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://testserver") as client:
            await client.post("/api/sessions/session-1/messages", json={"text": "第一条"})
            active = manager.get_session("session-1").active_turn_task
            if active is not None:
                await asyncio.wait_for(active, timeout=1)
            return requests, list(session.queued_inputs)

    requests, remaining_queue = asyncio.run(run())

    # 只跑了首轮,未继续排空队列。
    assert [request.text for request in requests] == ["第一条"]
    assert remaining_queue == ["排队保留"]


def test_queue_drains_after_user_stop(tmp_path) -> None:
    """显式 STOP 取消当前轮次后,排队的 prompt 应按顺序自动逐条提交。"""
    from iac_code.web.app import create_app
    from iac_code.web.runtime import WebTurnRequest
    from iac_code.web.session_manager import WebSessionManager

    async def run():
        manager = WebSessionManager(projects_dir=tmp_path / "projects")
        session = manager.create_session(session_id="session-1")

        first_started = asyncio.Event()
        release_first = asyncio.Event()  # 不 set:首轮仅通过 STOP 取消结束
        requests: list[WebTurnRequest] = []

        class RecordingRuntime:
            async def start_turn(self, request: WebTurnRequest) -> dict[str, object]:
                requests.append(request)
                turn_id = request.turn_id or uuid.uuid4().hex
                if request.source == "composer":
                    first_started.set()
                    await release_first.wait()  # 阻塞直到被 STOP 取消
                return {"accepted": True, "turnId": turn_id}

        app = create_app(session_manager=manager, runtime_factory=lambda _session: RecordingRuntime())

        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://testserver") as client:
            await client.post("/api/sessions/session-1/messages", json={"text": "第一条"})
            await asyncio.wait_for(first_started.wait(), timeout=1)
            await client.post("/api/sessions/session-1/queued-inputs", json={"text": "第二条"})
            await client.post("/api/sessions/session-1/queued-inputs", json={"text": "第三条"})

            response = await client.post("/api/sessions/session-1/interrupt", json={"text": ""})
            assert response.status_code == 200

            # 等待队列被自动排空:队列清空 + 无活动轮 + 无残留本地任务。
            for _ in range(400):
                if not session.queued_inputs and session.active_turn_task is None and not session.active_local_tasks:
                    break
                await asyncio.sleep(0.005)

            return (
                [request.text for request in requests],
                [request.source for request in requests],
                list(session.queued_inputs),
                session.events.replay_after(0),
                list(session.active_local_tasks),
                session.active_turn_task,
            )

    texts, sources, remaining_queue, events, local_tasks, active_turn = asyncio.run(run())

    # 首轮 composer 被停止后,队列两条各自独立成 turn 依次自动提交。
    assert texts == ["第一条", "第二条", "第三条"]
    assert sources == ["composer", "queued", "queued"]
    assert remaining_queue == []
    # drain 任务收尾后从 active_local_tasks 摘除,无残留;活动轮已清空。
    assert local_tasks == []
    assert active_turn is None
    # 两条排队各发一次 queued-input.removed(移除 chip);STOP 有 interrupt.accepted。
    removed = [event for event in events if event["type"] == "queued-input.removed"]
    assert len(removed) == 2
    assert any(event["type"] == "interrupt.accepted" for event in events)


def test_empty_queue_stop_does_not_start_new_turn(tmp_path) -> None:
    """队列为空时 STOP 不应起新轮,drain 任务应干净收尾无泄漏。"""
    from iac_code.web.app import create_app
    from iac_code.web.runtime import WebTurnRequest
    from iac_code.web.session_manager import WebSessionManager

    async def run():
        manager = WebSessionManager(projects_dir=tmp_path / "projects")
        session = manager.create_session(session_id="session-1")

        first_started = asyncio.Event()
        release_first = asyncio.Event()
        requests: list[WebTurnRequest] = []

        class RecordingRuntime:
            async def start_turn(self, request: WebTurnRequest) -> dict[str, object]:
                requests.append(request)
                turn_id = request.turn_id or uuid.uuid4().hex
                if request.source == "composer":
                    first_started.set()
                    await release_first.wait()
                return {"accepted": True, "turnId": turn_id}

        app = create_app(session_manager=manager, runtime_factory=lambda _session: RecordingRuntime())

        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://testserver") as client:
            await client.post("/api/sessions/session-1/messages", json={"text": "第一条"})
            await asyncio.wait_for(first_started.wait(), timeout=1)

            response = await client.post("/api/sessions/session-1/interrupt", json={"text": ""})
            assert response.status_code == 200

            for _ in range(400):
                if session.active_turn_task is None and not session.active_local_tasks:
                    break
                await asyncio.sleep(0.005)

            return (
                [request.source for request in requests],
                session.active_turn_task,
                list(session.active_local_tasks),
            )

    sources, active_turn, local_tasks = asyncio.run(run())

    # 只有首轮 composer,无 queued 源新轮。
    assert sources == ["composer"]
    assert active_turn is None
    assert local_tasks == []


def test_pending_drain_does_not_start_new_turn_during_shutdown(tmp_path) -> None:
    """STOP 已调度 drain 但尚未起新轮时触发关闭,shutdown 应取消 drain,不复活队列轮次。"""
    from iac_code.web.app import create_app
    from iac_code.web.runtime import WebTurnRequest
    from iac_code.web.session_manager import WebSessionManager

    async def run():
        manager = WebSessionManager(projects_dir=tmp_path / "projects")
        session = manager.create_session(session_id="session-1")

        first_started = asyncio.Event()
        release_first = asyncio.Event()
        unwind_gate = asyncio.Event()  # 让被取消轮次的 unwind 保持挂起,直到 shutdown 强制取消
        requests: list[WebTurnRequest] = []

        class RecordingRuntime:
            async def start_turn(self, request: WebTurnRequest) -> dict[str, object]:
                requests.append(request)
                turn_id = request.turn_id or uuid.uuid4().hex
                if request.source == "composer":
                    first_started.set()
                    try:
                        await release_first.wait()
                    except asyncio.CancelledError:
                        # 保持 unwind 挂起,使 drain 的 gather 停留在 pending,
                        # 命中「shutdown 时 drain 仍未起新轮」窗口。
                        await unwind_gate.wait()
                        raise
                return {"accepted": True, "turnId": turn_id}

        app = create_app(session_manager=manager, runtime_factory=lambda _session: RecordingRuntime())

        async with app.router.lifespan_context(app):
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app), base_url="http://testserver"
            ) as client:
                await client.post("/api/sessions/session-1/messages", json={"text": "第一条"})
                await asyncio.wait_for(first_started.wait(), timeout=1)
                await client.post("/api/sessions/session-1/queued-inputs", json={"text": "第二条"})
                await client.post("/api/sessions/session-1/queued-inputs", json={"text": "第三条"})

                response = await client.post("/api/sessions/session-1/interrupt", json={"text": ""})
                assert response.status_code == 200
            # 退出 lifespan → shutdown_session_work 运行:置 shutdown flag、取消 drain 与活动轮。

        return (
            [request.source for request in requests],
            list(session.queued_inputs),
            session.active_turn_task,
            list(session.active_local_tasks),
        )

    sources, remaining_queue, active_turn, local_tasks = asyncio.run(run())

    # shutdown 期间不得起 queued 源新轮;队列保留不丢。
    assert "queued" not in sources
    assert remaining_queue == ["第二条", "第三条"]
    assert active_turn is None
    assert local_tasks == []


def test_queued_input_is_restored_when_followup_runtime_fails_before_user_message(tmp_path, monkeypatch) -> None:
    """A queued prompt must survive failure while constructing its follow-up runtime."""
    from iac_code.web.app import create_app
    from iac_code.web.session_manager import WebSessionManager

    first_turn_started = asyncio.Event()
    release_first_turn = asyncio.Event()
    runtime_creations = 0

    class FakeAgentLoop:
        async def run_streaming(self, _user_input):
            first_turn_started.set()
            await release_first_turn.wait()
            if False:
                yield None

    class FakeAgentRuntime:
        agent_loop = FakeAgentLoop()

    def create_runtime(_options):
        nonlocal runtime_creations
        runtime_creations += 1
        if runtime_creations == 2:
            raise OSError("agent factory failed before user.message")
        return FakeAgentRuntime()

    monkeypatch.setattr("iac_code.web.runtime.create_agent_runtime", create_runtime)

    async def run() -> tuple[list[str], list[dict], int]:
        manager = WebSessionManager(projects_dir=tmp_path / "projects")
        session = manager.create_session(session_id="session-1")
        app = create_app(session_manager=manager)

        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://testserver") as client:
            response = await client.post("/api/sessions/session-1/messages", json={"text": "第一条"})
            assert response.status_code == 202
            await asyncio.wait_for(first_turn_started.wait(), timeout=1)
            queued = await client.post(
                "/api/sessions/session-1/queued-inputs",
                json={"text": "follow-up-must-survive"},
            )
            assert queued.status_code == 200
            release_first_turn.set()
            active = session.active_turn_task
            if active is not None:
                await asyncio.wait_for(active, timeout=1)

        return list(session.queued_inputs), session.events.replay_after(0), runtime_creations

    remaining_queue, events, creation_count = asyncio.run(run())

    assert creation_count == 2
    assert remaining_queue == ["follow-up-must-survive"]
    assert [event["payload"]["text"] for event in events if event["type"] == "user.message"] == ["第一条"]
    assert any(
        event["type"] == "queued-input.accepted" and event["payload"].get("restored") is True for event in events
    )


@pytest.mark.parametrize("mode", ["normal", "pipeline"])
def test_initial_accepted_prompt_is_restored_when_background_turn_fails_before_user_message(
    tmp_path, monkeypatch, mode
) -> None:
    """A 202 response must retain ownership until the prompt becomes a user message."""
    from iac_code.web.app import create_app
    from iac_code.web.session_manager import WebSessionManager

    if mode == "normal":
        monkeypatch.setattr(
            "iac_code.web.runtime.create_agent_runtime",
            lambda _options: (_ for _ in ()).throw(OSError("factory failed before user.message")),
        )
    else:
        snapshot_loads = 0

        async def fail_background_snapshot(**_kwargs):
            nonlocal snapshot_loads
            snapshot_loads += 1
            if snapshot_loads == 1:
                return None
            raise OSError("snapshot failed before user.message")

        monkeypatch.setattr("iac_code.web.pipeline_actions.load_pipeline_snapshot", fail_background_snapshot)

    async def run() -> tuple[int, str, list[str], list[dict]]:
        manager = WebSessionManager(projects_dir=tmp_path / "projects")
        session = manager.create_session(
            session_id="session-1",
            mode=mode,
            context_id="ctx-1" if mode == "pipeline" else None,
            task_id="task-1" if mode == "pipeline" else None,
        )
        app = create_app(session_manager=manager)

        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://testserver") as client:
            response = await client.post("/api/sessions/session-1/messages", json={"text": "must survive"})
            active = session.active_turn_task
            if isinstance(active, asyncio.Task):
                await asyncio.wait_for(asyncio.gather(active, return_exceptions=True), timeout=1)

        return response.status_code, session.draft, list(session.queued_inputs), session.events.replay_after(0)

    status_code, draft, queue, events = asyncio.run(run())

    assert status_code == 202
    assert draft == "must survive"
    assert queue == []
    assert [event for event in events if event["type"] == "user.message"] == []
    assert [
        event["payload"]["draft"]
        for event in events
        if event["type"] == "draft.updated" and event["payload"].get("restored") is True
    ] == ["must survive"]


def test_consumed_prompt_is_not_restored_after_event_buffer_rollover(tmp_path) -> None:
    """Explicit runtime ownership must win over the bounded SSE replay buffer."""
    from iac_code.web.app import create_app
    from iac_code.web.session_manager import WebSessionManager

    async def run() -> tuple[str, list[dict]]:
        manager = WebSessionManager(projects_dir=tmp_path / "projects")
        session = manager.create_session(session_id="session-1")

        class Runtime:
            async def start_turn(self, request):
                for index in range(501):
                    session.events.append("debug.stream_event", {"index": index})
                return {
                    "accepted": False,
                    "reason": "failed after persistence",
                    "turnId": request.turn_id,
                    "inputConsumed": True,
                }

        app = create_app(session_manager=manager, runtime_factory=lambda _session: Runtime())
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://testserver") as client:
            response = await client.post("/api/sessions/session-1/messages", json={"text": "do not duplicate"})
            assert response.status_code == 202
            active = session.active_turn_task
            if isinstance(active, asyncio.Task):
                await asyncio.wait_for(active, timeout=1)
        return session.draft, session.events.replay_after(0)

    draft, events = asyncio.run(run())

    assert draft == ""
    assert [event for event in events if event["type"] == "draft.updated"] == []


def test_pipeline_model_selection_failure_releases_admission_reservation(tmp_path, monkeypatch) -> None:
    from iac_code.web.app import create_app
    from iac_code.web.session_manager import WebSessionManager

    manager = WebSessionManager(projects_dir=tmp_path / "projects")
    session = manager.create_session(
        session_id="pipeline-selection-failure",
        mode="pipeline",
        context_id="ctx-1",
        task_id="task-1",
    )
    monkeypatch.setattr(
        "iac_code.web.runtime.model_selection_for_session",
        lambda _session: (_ for _ in ()).throw(OSError("settings unavailable")),
    )
    app = create_app(session_manager=manager)

    with TestClient(app, raise_server_exceptions=False) as client:
        response = client.post(f"/api/sessions/{session.session_id}/messages", json={"text": "hello"})

    assert response.status_code == 500
    assert session.active_turn_task is None
    assert session.turn_admission_lock.locked() is False


def test_consumed_queued_prompt_is_not_requeued_after_rollover_and_repeated_cancel(tmp_path) -> None:
    """Consumed input ownership must survive replay rollover and repeated cancellation."""
    from iac_code.web.app import create_app
    from iac_code.web.session_manager import WebSessionManager

    async def run() -> tuple[list[str], list[str], list[dict]]:
        manager = WebSessionManager(projects_dir=tmp_path / "projects")
        session = manager.create_session(session_id="session-1")
        session.queued_inputs = ["follow-up"]
        queued_started = asyncio.Event()
        cleanup_started = asyncio.Event()

        class Runtime:
            async def start_turn(self, request):
                if request.source == "composer":
                    return {"accepted": True, "turnId": request.turn_id, "inputConsumed": True}
                await session.events.publish(
                    "user.message",
                    {
                        "turnId": request.turn_id,
                        "text": request.text,
                        "imageIds": [],
                        "fileRefs": [],
                        "source": request.source,
                    },
                )
                for index in range(session.events.max_events + 1):
                    session.events.append("debug.stream_event", {"index": index})
                queued_started.set()
                try:
                    await asyncio.Event().wait()
                except asyncio.CancelledError:
                    cleanup_started.set()
                    await asyncio.Event().wait()

        app = create_app(session_manager=manager, runtime_factory=lambda _session: Runtime())
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://testserver") as client:
            first = await client.post("/api/sessions/session-1/messages", json={"text": "first"})
            assert first.status_code == 202
            await asyncio.wait_for(queued_started.wait(), timeout=1)
            interrupt_one = asyncio.create_task(client.post("/api/sessions/session-1/interrupt", json={"message": ""}))
            await asyncio.wait_for(cleanup_started.wait(), timeout=1)
            interrupt_two = await client.post("/api/sessions/session-1/interrupt", json={"message": ""})
            await asyncio.wait_for(interrupt_one, timeout=1)
            assert interrupt_two.status_code == 200
            active = session.active_turn_task
            if isinstance(active, asyncio.Task):
                await asyncio.wait_for(asyncio.gather(active, return_exceptions=True), timeout=1)

        events = session.events.replay_after(0)
        return list(session.queued_inputs), events

    queue, events = asyncio.run(run())

    assert queue == []
    assert [
        event
        for event in events
        if event["type"] == "queued-input.accepted" and event["payload"].get("restored") is True
    ] == []


def test_project_pin_archive_rename_remove_and_collapse(tmp_path) -> None:
    from iac_code.agent.message import Message
    from iac_code.web.app import create_app
    from iac_code.web.session_manager import WebSessionManager

    cwd = str(tmp_path / "project")
    manager = WebSessionManager(projects_dir=tmp_path / "projects")
    # 播种一个可见 web 会话:项目须有 total>0 才会出现在活动侧栏(空项目一律隐藏)。
    manager.storage.append(cwd, "visible-1", Message(role="user", content="visible prompt"))
    _mark_web_session(manager, cwd, "visible-1")
    app = create_app(session_manager=manager)

    with TestClient(app) as client:
        # Pin the project -> it leaves the active list and appears in pinnedProjects.
        pin = client.patch("/api/projects", json={"cwd": cwd, "pinned": True})
        assert pin.status_code == 200
        assert pin.json()["pinned"] is True
        assert pin.json()["pinnedAt"]
        listed = client.get("/api/sessions").json()
        assert [p["cwd"] for p in listed["projects"]] == []
        assert [p["cwd"] for p in listed["pinnedProjects"]] == [cwd]

        # Rename (display label only) and collapse persist through the metadata file.
        client.patch("/api/projects", json={"cwd": cwd, "label": "自定义名称", "collapsed": True})
        listed = client.get("/api/sessions").json()
        assert listed["pinnedProjects"][0]["label"] == "自定义名称"
        assert listed["pinnedProjects"][0]["collapsed"] is True

        # Unpin -> back to active list.
        client.patch("/api/projects", json={"cwd": cwd, "pinned": False})
        listed = client.get("/api/sessions").json()
        assert [p["cwd"] for p in listed["projects"]] == [cwd]
        assert listed["pinnedProjects"] == []

        # Archive hides the whole project group.
        client.patch("/api/projects", json={"cwd": cwd, "archived": True})
        listed = client.get("/api/sessions").json()
        assert listed["projects"] == []
        assert listed["pinnedProjects"] == []

        # Un-archive then remove (hidden) also hides it, recoverably.
        client.patch("/api/projects", json={"cwd": cwd, "archived": False})
        assert [p["cwd"] for p in client.get("/api/sessions").json()["projects"]] == [cwd]
        client.patch("/api/projects", json={"cwd": cwd, "hidden": True})
        assert client.get("/api/sessions").json()["projects"] == []

        # Unsupported fields are rejected.
        bad = client.patch("/api/projects", json={"cwd": cwd, "bogus": True})
        assert bad.status_code == 400


def test_archived_sessions_are_grouped_by_project_and_deletable(tmp_path) -> None:
    from iac_code.web.app import create_app
    from iac_code.web.session_manager import WebSessionManager

    cwd = str(tmp_path / "project")
    manager = WebSessionManager(projects_dir=tmp_path / "projects")
    app = create_app(session_manager=manager)

    with TestClient(app) as client:
        # A pristine "(empty)" session is not listable and never surfaces in the
        # archived view, so give each session a real title before archiving it.
        session_ids = []
        for title in ("alpha-one", "beta-two"):
            created = client.post("/api/sessions", json={"cwd": cwd})
            session_id = created.json()["sessionId"]
            session_ids.append(session_id)
            client.patch("/api/sessions/{}".format(session_id), json={"title": title})
            client.patch("/api/sessions/{}".format(session_id), json={"archived": True})

        # An un-archived, titled session in the same project stays out of the view.
        active = client.post("/api/sessions", json={"cwd": cwd})
        active_id = active.json()["sessionId"]
        client.patch("/api/sessions/{}".format(active_id), json={"title": "gamma-live"})

        archived = client.get("/api/sessions/archived")
        assert archived.status_code == 200
        payload = archived.json()
        assert [group["cwd"] for group in payload["projects"]] == [cwd]
        group = payload["projects"][0]
        assert group["total"] == 2
        titles = {session["title"] for session in group["sessions"]}
        assert titles == {"alpha-one", "beta-two"}
        assert all(session["archived"] is True for session in group["sessions"])

        # Per-project delete removes exactly that project's archived sessions.
        deleted = client.delete("/api/sessions/archived", params={"cwd": group["cwd"]})
        assert deleted.status_code == 200
        assert deleted.json()["deleted"] == 2

        empty = client.get("/api/sessions/archived")
        assert empty.json()["projects"] == []

        # The still-active session survived the archived purge.
        listed = client.get("/api/sessions").json()
        assert [p["cwd"] for p in listed["projects"]] == [cwd]


def test_archive_project_sessions_moves_all_into_archived_view(tmp_path) -> None:
    from iac_code.web.app import create_app
    from iac_code.web.session_manager import WebSessionManager

    cwd = str(tmp_path / "project")
    other = str(tmp_path / "other")
    manager = WebSessionManager(projects_dir=tmp_path / "projects")
    app = create_app(session_manager=manager)

    with TestClient(app) as client:
        # Three live sessions in the target project (one of them pinned) plus a
        # session in a different project that must stay untouched.
        session_ids = []
        for title in ("alpha-one", "beta-two", "gamma-three"):
            created = client.post("/api/sessions", json={"cwd": cwd})
            session_id = created.json()["sessionId"]
            session_ids.append(session_id)
            client.patch("/api/sessions/{}".format(session_id), json={"title": title})
        client.patch("/api/sessions/{}".format(session_ids[0]), json={"pinned": True})

        untouched = client.post("/api/sessions", json={"cwd": other})
        untouched_id = untouched.json()["sessionId"]
        client.patch("/api/sessions/{}".format(untouched_id), json={"title": "other-live"})

        # Archiving the project archives every one of its sessions (pinned included).
        response = client.post("/api/projects/archive-sessions", json={"cwd": cwd})
        assert response.status_code == 200
        assert response.json() == {"cwd": cwd, "archived": 3}

        archived = client.get("/api/sessions/archived").json()
        target_group = next(group for group in archived["projects"] if group["cwd"] == cwd)
        assert target_group["total"] == 3
        assert {session["title"] for session in target_group["sessions"]} == {
            "alpha-one",
            "beta-two",
            "gamma-three",
        }
        assert all(session["archived"] is True for session in target_group["sessions"])

        # The project's sessions leave both the active list and the pinned list;
        # with every session archived the now-empty project card is hidden from
        # the active sidebar entirely. The other project's live session is untouched.
        listed = client.get("/api/sessions").json()
        assert not any(p["cwd"] == cwd for p in listed["projects"])
        other_active = next(p for p in listed["projects"] if p["cwd"] == other)
        assert other_active["total"] == 1
        assert not any(group["cwd"] == cwd for group in listed.get("pinnedProjects", []))

        # Un-archiving a single session brings it straight back to the sidebar
        # without needing to touch the project itself.
        client.patch("/api/sessions/{}".format(session_ids[1]), json={"archived": False})
        relisted = client.get("/api/sessions").json()
        back = next(p for p in relisted["projects"] if p["cwd"] == cwd)
        assert back["total"] == 1
        assert {session["title"] for session in back["sessions"]} == {"beta-two"}


@pytest.mark.asyncio
async def test_delete_wins_while_bulk_archive_waits_for_session_admission(tmp_path) -> None:
    from iac_code.agent.message import Message
    from iac_code.web.session_manager import WebSessionManager

    cwd = str(tmp_path / "project")
    manager = WebSessionManager(projects_dir=tmp_path / "projects", cwd=cwd)
    session = manager.create_session(cwd=cwd, session_id="bulk-archive-delete-race")
    manager.storage.append(cwd, session.session_id, Message(role="user", content="keep identity stable"))

    await session.turn_admission_lock.acquire()
    archive_task = asyncio.create_task(manager.archive_project_sessions(cwd))
    try:
        await asyncio.sleep(0)
        assert archive_task.done() is False
        deleted = manager.delete_session(session.web_session_id)
    finally:
        session.turn_admission_lock.release()

    archived = await asyncio.wait_for(archive_task, timeout=1)

    assert deleted is True
    assert archived == 0
    assert manager.get_session(session.web_session_id) is None
    assert manager.storage.exists(cwd, session.session_id) is False


def test_archive_project_sessions_requires_cwd(tmp_path) -> None:
    from iac_code.web.app import create_app
    from iac_code.web.session_manager import WebSessionManager

    manager = WebSessionManager(projects_dir=tmp_path / "projects")
    app = create_app(session_manager=manager)

    with TestClient(app) as client:
        missing = client.post("/api/projects/archive-sessions", json={})
        assert missing.status_code == 400
        # A project with no sessions archives nothing rather than erroring.
        empty = client.post("/api/projects/archive-sessions", json={"cwd": str(tmp_path / "nope")})
        assert empty.status_code == 200
        assert empty.json() == {"cwd": str(tmp_path / "nope"), "archived": 0}


def test_delete_single_session_removes_it_from_listings(tmp_path) -> None:
    from iac_code.web.app import create_app
    from iac_code.web.session_manager import WebSessionManager

    cwd = str(tmp_path / "project")
    manager = WebSessionManager(projects_dir=tmp_path / "projects")
    app = create_app(session_manager=manager)

    with TestClient(app) as client:
        created = client.post("/api/sessions", json={"cwd": cwd})
        session_id = created.json()["sessionId"]
        client.patch("/api/sessions/{}".format(session_id), json={"title": "delete-me"})

        removed = client.delete("/api/sessions/{}".format(session_id))
        assert removed.status_code == 200
        assert removed.json() == {"deleted": True, "sessionId": session_id}

        # A second delete is a no-op reporting 404 rather than a 500.
        again = client.delete("/api/sessions/{}".format(session_id))
        assert again.status_code == 404
        assert again.json()["deleted"] is False

        assert client.get("/api/sessions/{}".format(session_id)).status_code == 404


def test_project_reveal_requires_existing_directory(tmp_path) -> None:
    from iac_code.web.app import create_app
    from iac_code.web.session_manager import WebSessionManager

    manager = WebSessionManager(projects_dir=tmp_path / "projects")
    app = create_app(session_manager=manager)

    with TestClient(app) as client:
        # A non-existent path is rejected before any OS reveal command runs
        # (the success path is not exercised here to avoid launching a file manager).
        missing = client.post("/api/projects/reveal", json={"cwd": str(tmp_path / "does-not-exist")})
        assert missing.status_code == 404
        empty = client.post("/api/projects/reveal", json={})
        assert empty.status_code == 400


def test_session_list_is_limited_by_default_and_reports_more(tmp_path) -> None:
    from iac_code.web.app import create_app
    from iac_code.web.session_manager import WebSessionManager

    cwd = str(tmp_path / "project")
    manager = WebSessionManager(projects_dir=tmp_path / "projects")
    for index in range(55):
        session = manager.create_session(cwd=cwd, session_id="session-{:02d}".format(index))
        session.title = "Session {:02d}".format(index)
        manager.persist_web_metadata(session)
    app = create_app(session_manager=manager)

    with TestClient(app) as client:
        default_response = client.get("/api/sessions")
        limited_response = client.get("/api/sessions?limit=3")

    assert default_response.status_code == 200
    default_payload = default_response.json()
    assert len(default_payload["sessions"]) == 50
    assert default_payload["total"] == 55
    assert default_payload["limit"] == 50
    assert default_payload["hasMore"] is True

    assert limited_response.status_code == 200
    limited_payload = limited_response.json()
    assert len(limited_payload["sessions"]) == 3
    assert limited_payload["total"] == 55
    assert limited_payload["limit"] == 3
    assert limited_payload["hasMore"] is True


def test_session_list_total_and_has_more_only_count_visible_sessions(tmp_path) -> None:
    from iac_code.agent.message import Message
    from iac_code.web.app import create_app
    from iac_code.web.session_manager import WebSessionManager

    cwd = str(tmp_path / "project")
    manager = WebSessionManager(projects_dir=tmp_path / "projects")
    manager.storage.append(cwd, "visible", Message(role="user", content="visible prompt"))
    _mark_web_session(manager, cwd, "visible")
    manager.storage.save(cwd, "empty", [])
    archived = manager.create_session(cwd=cwd, session_id="archived")
    manager.storage.append(cwd, "archived", Message(role="user", content="archived prompt"))
    archived.archived = True
    manager.persist_web_metadata(archived)
    app = create_app(session_manager=WebSessionManager(projects_dir=tmp_path / "projects"))

    with TestClient(app) as client:
        response = client.get("/api/sessions", params={"limit": 50})

    assert response.status_code == 200
    payload = response.json()
    assert [session["sessionId"] for session in payload["sessions"]] == ["visible"]
    assert payload["total"] == 1
    assert payload["hasMore"] is False


def test_session_project_headers_are_not_limited_by_default(tmp_path) -> None:
    from iac_code.agent.message import Message
    from iac_code.web.app import create_app
    from iac_code.web.session_manager import WebSessionManager

    manager = WebSessionManager(projects_dir=tmp_path / "projects")
    for index in range(55):
        cwd = str(tmp_path / "project-{:02d}".format(index))
        manager.storage.append(cwd, "session-{:02d}".format(index), Message(role="user", content="prompt"))
        _mark_web_session(manager, cwd, "session-{:02d}".format(index))
    app = create_app(session_manager=manager)

    with TestClient(app) as client:
        response = client.get("/api/sessions", params={"perProjectLimit": 1})

    assert response.status_code == 200
    payload = response.json()
    assert len(payload["projects"]) == 55
    assert payload["projectTotal"] == 55
    assert payload["perProjectLimit"] == 1
    assert all(len(project["sessions"]) == 1 for project in payload["projects"])


def test_session_list_disables_browser_cache(tmp_path) -> None:
    from iac_code.web.app import create_app
    from iac_code.web.session_manager import WebSessionManager

    manager = WebSessionManager(projects_dir=tmp_path / "projects")
    app = create_app(session_manager=manager)

    with TestClient(app) as client:
        response = client.get("/api/sessions", params={"limit": 50, "perProjectLimit": 5})

    assert response.status_code == 200
    assert response.headers["cache-control"] == "no-store"


def test_session_projects_hide_empty_projects(tmp_path) -> None:
    from iac_code.agent.message import Message
    from iac_code.web.app import create_app
    from iac_code.web.session_manager import WebSessionManager

    visible_cwd = str(tmp_path / "visible-project")
    empty_cwd = str(tmp_path / "empty-project")
    manager = WebSessionManager(projects_dir=tmp_path / "projects")
    manager.storage.append(visible_cwd, "visible-1", Message(role="user", content="visible prompt"))
    _mark_web_session(manager, visible_cwd, "visible-1")
    manager.create_session(cwd=empty_cwd, session_id="empty-1")
    app = create_app(session_manager=manager)

    with TestClient(app) as client:
        response = client.get("/api/sessions", params={"perProjectLimit": 5})

    assert response.status_code == 200
    payload = response.json()
    by_cwd = {project["cwd"]: project for project in payload["projects"]}
    # 无可见会话的空项目(total==0)不出现在侧栏。
    assert payload["projectTotal"] == 1
    assert payload["totalProjectSessions"] == 1
    assert by_cwd[visible_cwd]["total"] == 1
    assert [session["title"] for session in by_cwd[visible_cwd]["sessions"]] == ["visible prompt"]
    assert empty_cwd not in by_cwd


def test_session_projects_hide_empty_directories(tmp_path) -> None:
    from iac_code.agent.message import Message
    from iac_code.web.app import create_app
    from iac_code.web.session_manager import WebSessionManager

    projects_dir = tmp_path / "projects"
    visible_cwd = str(tmp_path / "visible-project")
    manager = WebSessionManager(projects_dir=projects_dir)
    manager.storage.append(visible_cwd, "visible-1", Message(role="user", content="visible prompt"))
    _mark_web_session(manager, visible_cwd, "visible-1")
    (projects_dir / "-Users-ehzyo-repo-empty-project").mkdir()
    app = create_app(session_manager=manager)

    with TestClient(app) as client:
        response = client.get("/api/sessions", params={"perProjectLimit": 5})

    assert response.status_code == 200
    payload = response.json()
    by_cwd = {project["cwd"]: project for project in payload["projects"]}
    # 从未有会话的真·空目录不显示。
    assert payload["projectTotal"] == 1
    assert "-Users-ehzyo-repo-empty-project" not in by_cwd


def test_session_projects_hide_empty_directories_even_when_recent(tmp_path) -> None:
    from iac_code.agent.message import Message
    from iac_code.web.app import create_app
    from iac_code.web.session_manager import WebSessionManager

    projects_dir = tmp_path / "projects"
    old_cwd = str(tmp_path / "old-visible-project")
    manager = WebSessionManager(projects_dir=projects_dir)
    manager.storage.append(old_cwd, "old-visible-1", Message(role="user", content="old visible prompt"))
    _mark_web_session(manager, old_cwd, "old-visible-1")
    empty_project_dir = projects_dir / "-Users-ehzyo-repo-recent-empty-project"
    empty_project_dir.mkdir()
    os.utime(manager.storage.session_path(old_cwd, "old-visible-1"), (1000, 1000))
    os.utime(empty_project_dir, (2000, 2000))
    app = create_app(session_manager=manager)

    with TestClient(app) as client:
        response = client.get("/api/sessions", params={"perProjectLimit": 5})

    assert response.status_code == 200
    payload = response.json()
    # 即便空目录 mtime 更新也不因排序出现——空项目一律隐藏。
    assert [project["cwd"] for project in payload["projects"]] == [old_cwd]
    assert payload["projectTotal"] == 1
    assert payload["projects"][0]["total"] == 1


def test_session_list_can_be_filtered_by_project_cwd(tmp_path) -> None:
    from iac_code.agent.message import Message
    from iac_code.web.app import create_app
    from iac_code.web.session_manager import WebSessionManager

    first_cwd = str(tmp_path / "project-a")
    second_cwd = str(tmp_path / "project-b")
    manager = WebSessionManager(projects_dir=tmp_path / "projects")
    for index in range(3):
        manager.storage.append(
            first_cwd,
            "first-{}".format(index),
            Message(role="user", content="first {}".format(index)),
        )
        _mark_web_session(manager, first_cwd, "first-{}".format(index))
    manager.storage.append(second_cwd, "second-1", Message(role="user", content="second"))
    _mark_web_session(manager, second_cwd, "second-1")
    app = create_app(session_manager=manager)

    with TestClient(app) as client:
        response = client.get("/api/sessions", params={"cwd": first_cwd, "limit": 2})

    assert response.status_code == 200
    payload = response.json()
    assert [session["cwd"] for session in payload["sessions"]] == [first_cwd, first_cwd]
    assert [session["title"] for session in payload["sessions"]] == ["first 2", "first 1"]
    assert payload["total"] == 3
    assert payload["limit"] == 2
    assert payload["hasMore"] is True


def test_session_detail_returns_json_404_for_missing_session(tmp_path) -> None:
    from iac_code.web.app import create_app
    from iac_code.web.session_manager import WebSessionManager

    manager = WebSessionManager(projects_dir=tmp_path / "projects")
    app = create_app(session_manager=manager)

    with TestClient(app) as client:
        response = client.get("/api/sessions/missing")

    assert response.status_code == 404
    assert response.json() == {"error": {"message": "session not found"}}


def test_patch_session_updates_title_debug_and_escape_settings(tmp_path) -> None:
    from iac_code.web.app import create_app
    from iac_code.web.session_manager import WebSessionManager

    manager = WebSessionManager(projects_dir=tmp_path / "projects", cwd=tmp_path / "project")
    session = manager.create_session(mode="pipeline")
    app = create_app(session_manager=manager)

    with TestClient(app) as client:
        response = client.patch(
            f"/api/sessions/{session.session_id}",
            json={
                "title": "production-stack",
                "debugEnabled": True,
                "allowUserEscapes": {"skill": True, "command": True, "shell": False},
            },
        )

    assert response.status_code == 200
    payload = response.json()
    assert payload["sessionId"] == session.session_id
    assert payload["title"] == "production-stack"
    assert payload["debugEnabled"] is True
    assert payload["allowUserEscapes"] == {"skill": True, "command": True, "shell": False}


def test_patch_session_pins_and_archives_sessions_in_list_payload(tmp_path) -> None:
    from iac_code.web.app import create_app
    from iac_code.web.session_manager import WebSessionManager

    manager = WebSessionManager(projects_dir=tmp_path / "projects", cwd=tmp_path / "project")
    pinned_session = manager.create_session(session_id="pinned-session", cwd=str(tmp_path / "project"))
    archived_session = manager.create_session(session_id="archived-session", cwd=str(tmp_path / "project"))
    manager.rename_session(pinned_session, "pin-me")
    manager.rename_session(archived_session, "archive-me")
    app = create_app(session_manager=manager)

    with TestClient(app) as client:
        pin_response = client.patch(f"/api/sessions/{pinned_session.session_id}", json={"pinned": True})
        archive_response = client.patch(f"/api/sessions/{archived_session.session_id}", json={"archived": True})
        list_response = client.get("/api/sessions")

    assert pin_response.status_code == 200
    assert archive_response.status_code == 200
    assert pin_response.json()["pinned"] is True
    assert archive_response.json()["archived"] is True
    list_payload = list_response.json()
    assert [session["sessionId"] for session in list_payload["pinnedSessions"]] == [pinned_session.session_id]
    assert all(session["sessionId"] != archived_session.session_id for session in list_payload["sessions"])
    assert all(
        session["sessionId"] != archived_session.session_id
        for project in list_payload["projects"]
        for session in project["sessions"]
    )

    reloaded_manager = WebSessionManager(projects_dir=tmp_path / "projects", cwd=tmp_path / "project")
    reloaded_pinned = reloaded_manager.get_session(pinned_session.session_id)
    reloaded_archived = reloaded_manager.get_session(archived_session.session_id)
    assert reloaded_pinned is not None
    assert reloaded_archived is not None
    assert reloaded_pinned.to_dict()["pinned"] is True
    assert reloaded_archived.to_dict()["archived"] is True


@pytest.mark.parametrize("busy_state", ["turn", "permission", "question"])
def test_patch_session_rejects_archive_while_session_has_inflight_work(tmp_path, busy_state) -> None:
    from iac_code.web.app import create_app
    from iac_code.web.session_manager import WebSessionManager

    class ActiveTurn:
        @staticmethod
        def done() -> bool:
            return False

    class PendingRequest:
        @staticmethod
        def to_dict() -> dict:
            return {}

    manager = WebSessionManager(projects_dir=tmp_path / "projects", cwd=tmp_path / "project")
    session = manager.create_session(session_id="busy-archive-{}".format(busy_state))
    manager.rename_session(session, "busy-archive")
    if busy_state == "turn":
        session.active_turn_task = ActiveTurn()
    elif busy_state == "permission":
        session.pending_permissions["permission-1"] = PendingRequest()
    else:
        session.pending_questions["question-1"] = PendingRequest()
    app = create_app(session_manager=manager)

    with TestClient(app) as client:
        response = client.patch(f"/api/sessions/{session.session_id}", json={"archived": True})
        session.active_turn_task = None
        session.pending_permissions.clear()
        session.pending_questions.clear()

    assert response.status_code == 409
    assert response.json()["error"]["code"] == "session_busy"
    assert session.archived is False


@pytest.mark.parametrize("mode", ["normal", "pipeline"])
def test_archived_session_rejects_new_message_work(tmp_path, mode) -> None:
    from types import SimpleNamespace

    from iac_code.web.app import create_app
    from iac_code.web.session_manager import WebSessionManager

    calls: list[str] = []

    class RecordingRuntime:
        async def start_turn(self, request):
            calls.append(request.text)
            return {"accepted": True, "turnId": request.turn_id or "turn-1"}

    class RecordingPipelineRunner:
        async def start(self, _session, message, _image_ids, _file_refs, **_kwargs):
            calls.append(message)
            return SimpleNamespace(
                accepted=True,
                status_code=202,
                response={"accepted": True},
                events=[],
                terminal_outcome=None,
            )

    manager = WebSessionManager(projects_dir=tmp_path / "projects", cwd=tmp_path / "project")
    session = manager.create_session(session_id="archived-work", mode=mode, pipeline_name="selling")
    app = create_app(
        session_manager=manager,
        runtime_factory=lambda _session: RecordingRuntime(),
        pipeline_action_runner_factory=lambda: RecordingPipelineRunner(),
    )

    with TestClient(app) as client:
        archived = client.patch(f"/api/sessions/{session.session_id}", json={"archived": True})
        response = client.post(f"/api/sessions/{session.session_id}/messages", json={"text": "must not run"})
        compact = client.post(f"/api/sessions/{session.session_id}/compact")
        command = client.post(f"/api/sessions/{session.session_id}/commands", json={"command": "!echo no"})
        queued = client.post(f"/api/sessions/{session.session_id}/queued-inputs", json={"text": "later"})
        interrupt = client.post(f"/api/sessions/{session.session_id}/interrupt", json={"message": "stop"})
        status = client.post(f"/api/sessions/{session.session_id}/commands", json={"command": "/status"})

    assert archived.status_code == 200
    for blocked in (response, compact, command, queued, interrupt):
        assert blocked.status_code == 409
        assert blocked.json()["error"]["code"] == "session_archived"
    assert status.status_code == 200
    assert calls == []


@pytest.mark.asyncio
@pytest.mark.parametrize("operation", ["queue", "edit", "delete", "steer", "interrupt"])
async def test_archive_wins_while_queue_or_interrupt_body_is_being_read(tmp_path, operation) -> None:
    from iac_code.web.app import create_app
    from iac_code.web.session_manager import WebSessionManager

    manager = WebSessionManager(projects_dir=tmp_path / "projects", cwd=tmp_path / "project")
    session = manager.create_session(session_id="archive-body-race")
    if operation in {"edit", "delete", "steer"}:
        session.queued_inputs = ["old"]
    app = create_app(session_manager=manager)
    body_started = asyncio.Event()
    release_body = asyncio.Event()

    if operation == "queue":
        method, path, body = "POST", "queued-inputs", b'{"text":"later"}'
    elif operation == "edit":
        method, path, body = "PATCH", "queued-inputs/0", b'{"text":"new","expectedText":"old"}'
    elif operation == "delete":
        method, path, body = "DELETE", "queued-inputs/0", b'{"expectedText":"old"}'
    elif operation == "steer":
        method, path, body = "POST", "queued-inputs/0/steer", b'{"expectedText":"old"}'
    else:
        method, path, body = "POST", "interrupt", b'{"message":"later"}'

    async def slow_body():
        yield body[:1]
        body_started.set()
        await release_body.wait()
        yield body[1:]

    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://testserver") as client:
        request_task = asyncio.create_task(
            client.request(
                method,
                f"/api/sessions/{session.session_id}/{path}",
                content=slow_body(),
                headers={"Content-Type": "application/json"},
            )
        )
        await asyncio.wait_for(body_started.wait(), timeout=1)
        archived = await client.patch(f"/api/sessions/{session.session_id}", json={"archived": True})
        release_body.set()
        response = await asyncio.wait_for(request_task, timeout=1)

    assert archived.status_code == 200
    assert response.status_code == 409
    assert response.json()["error"]["code"] == "session_archived"
    assert session.queued_inputs == (["old"] if operation in {"edit", "delete", "steer"} else [])


@pytest.mark.asyncio
async def test_archive_wins_while_normal_message_body_is_being_read(tmp_path) -> None:
    from iac_code.web.app import create_app
    from iac_code.web.session_manager import WebSessionManager

    runtime_calls: list[str] = []

    class RecordingRuntime:
        async def start_turn(self, request):
            runtime_calls.append(request.text)
            return {"accepted": True, "turnId": request.turn_id or "turn-1"}

    manager = WebSessionManager(projects_dir=tmp_path / "projects", cwd=tmp_path / "project")
    session = manager.create_session(session_id="normal-message-archive-race")
    app = create_app(session_manager=manager, runtime_factory=lambda _session: RecordingRuntime())
    body_started = asyncio.Event()
    release_body = asyncio.Event()
    body = b'{"text":"must not run"}'

    async def slow_body():
        yield body[:1]
        body_started.set()
        await release_body.wait()
        yield body[1:]

    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://testserver") as client:
        message_task = asyncio.create_task(
            client.post(
                f"/api/sessions/{session.session_id}/messages",
                content=slow_body(),
                headers={"Content-Type": "application/json"},
            )
        )
        await asyncio.wait_for(body_started.wait(), timeout=1)
        archived = await client.patch(f"/api/sessions/{session.session_id}", json={"archived": True})
        release_body.set()
        response = await asyncio.wait_for(message_task, timeout=1)

    assert archived.status_code == 200
    assert response.status_code == 409
    assert response.json()["error"]["code"] == "session_archived"
    assert runtime_calls == []


@pytest.mark.asyncio
async def test_delete_wins_while_archive_patch_body_is_being_read(tmp_path) -> None:
    from iac_code.agent.message import Message
    from iac_code.web.app import create_app
    from iac_code.web.session_manager import WebSessionManager

    cwd = str(tmp_path / "project")
    manager = WebSessionManager(projects_dir=tmp_path / "projects", cwd=cwd)
    session = manager.create_session(cwd=cwd, session_id="archive-patch-delete-race")
    manager.storage.append(cwd, session.session_id, Message(role="user", content="keep identity stable"))
    app = create_app(session_manager=manager)
    body_started = asyncio.Event()
    release_body = asyncio.Event()
    body = b'{"archived":true}'

    async def slow_body():
        yield body[:1]
        body_started.set()
        await release_body.wait()
        yield body[1:]

    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://testserver") as client:
        archive_task = asyncio.create_task(
            client.patch(
                "/api/sessions/{}".format(session.web_session_id),
                content=slow_body(),
                headers={"Content-Type": "application/json"},
            )
        )
        await asyncio.wait_for(body_started.wait(), timeout=1)
        deleted = await client.delete("/api/sessions/{}".format(session.web_session_id))
        release_body.set()
        archived = await asyncio.wait_for(archive_task, timeout=1)

    assert deleted.status_code == 200
    assert deleted.json()["deleted"] is True
    assert archived.status_code == 404
    assert archived.json() == {"error": {"message": "session not found"}}
    assert manager.get_session(session.web_session_id) is None
    assert manager.storage.exists(cwd, session.session_id) is False


@pytest.mark.parametrize(
    ("operation", "method", "suffix", "body"),
    [
        ("title", "PATCH", "", b'{"title":"stale-title"}'),
        ("pinned", "PATCH", "", b'{"pinned":true}'),
        ("permission", "PUT", "/permission-mode", b'{"mode":"bypass_permissions"}'),
        ("model", "PUT", "/model", b'{"provider":"openai","model":"gpt-5.5"}'),
    ],
)
@pytest.mark.asyncio
async def test_stale_session_mutation_cannot_overwrite_recreated_session(
    tmp_path,
    operation,
    method,
    suffix,
    body,
) -> None:
    from iac_code.web.app import create_app
    from iac_code.web.session_manager import WebSessionManager

    cwd = str(tmp_path / "project")
    projects_dir = tmp_path / "projects"
    manager = WebSessionManager(projects_dir=projects_dir, cwd=cwd)
    old_session = manager.create_session(cwd=cwd, session_id="stale-mutation-race")
    manager.rename_session(old_session, "old-session")
    app = create_app(session_manager=manager)
    body_started = asyncio.Event()
    release_body = asyncio.Event()

    async def slow_body():
        yield body[:1]
        body_started.set()
        await release_body.wait()
        yield body[1:]

    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://testserver") as client:
        mutation_task = asyncio.create_task(
            client.request(
                method,
                "/api/sessions/{}{}".format(old_session.web_session_id, suffix),
                content=slow_body(),
                headers={"Content-Type": "application/json"},
            )
        )
        await asyncio.wait_for(body_started.wait(), timeout=1)
        deleted = await client.delete("/api/sessions/{}".format(old_session.web_session_id))
        fresh_session = manager.create_session(cwd=cwd, session_id=old_session.session_id)
        manager.rename_session(fresh_session, "fresh-session")
        release_body.set()
        mutation = await asyncio.wait_for(mutation_task, timeout=1)

    assert deleted.status_code == 200
    assert deleted.json()["deleted"] is True
    assert mutation.status_code == 404
    assert mutation.json() == {"error": {"message": "session not found"}}
    assert manager.get_session(fresh_session.web_session_id) is fresh_session
    assert fresh_session.title == "fresh-session"
    assert fresh_session.pinned is False
    assert fresh_session.to_dict()["permissionMode"] == "default"
    assert fresh_session.provider is None
    assert fresh_session.model is None

    reloaded_manager = WebSessionManager(projects_dir=projects_dir, cwd=cwd)
    reloaded_session = reloaded_manager.create_session(cwd=cwd, session_id=fresh_session.session_id)
    assert reloaded_session.title == "fresh-session"
    assert reloaded_session.pinned is False
    assert reloaded_session.to_dict()["permissionMode"] == "default"
    assert reloaded_session.provider is None
    assert reloaded_session.model is None


@pytest.mark.parametrize("command", ["/rename stale-title", "/debug on", "/clear"])
@pytest.mark.asyncio
async def test_stale_command_cannot_mutate_recreated_session(tmp_path, command) -> None:
    from iac_code.web.app import create_app
    from iac_code.web.session_manager import WebSessionManager

    cwd = str(tmp_path / "project")
    projects_dir = tmp_path / "projects"
    manager = WebSessionManager(projects_dir=projects_dir, cwd=cwd)
    old_session = manager.create_session(cwd=cwd, session_id="stale-command-race")
    manager.rename_session(old_session, "old-session")
    app = create_app(session_manager=manager)
    body_started = asyncio.Event()
    release_body = asyncio.Event()
    body = json.dumps({"command": command}).encode()

    async def slow_body():
        yield body[:1]
        body_started.set()
        await release_body.wait()
        yield body[1:]

    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://testserver") as client:
        command_task = asyncio.create_task(
            client.post(
                "/api/sessions/{}/commands".format(old_session.web_session_id),
                content=slow_body(),
                headers={"Content-Type": "application/json"},
            )
        )
        await asyncio.wait_for(body_started.wait(), timeout=1)
        deleted = await client.delete("/api/sessions/{}".format(old_session.web_session_id))
        fresh_session = manager.create_session(cwd=cwd, session_id=old_session.session_id)
        manager.rename_session(fresh_session, "fresh-session")
        fresh_session.draft = "fresh-draft"
        release_body.set()
        response = await asyncio.wait_for(command_task, timeout=1)

    assert deleted.status_code == 200
    assert response.status_code == 404
    assert response.json() == {"error": {"message": "session not found"}}
    assert manager.get_session(fresh_session.web_session_id) is fresh_session
    assert fresh_session.title == "fresh-session"
    assert fresh_session.debug_enabled is False
    assert fresh_session.draft == "fresh-draft"

    reloaded_manager = WebSessionManager(projects_dir=projects_dir, cwd=cwd)
    reloaded_session = reloaded_manager.create_session(cwd=cwd, session_id=fresh_session.session_id)
    assert reloaded_session.title == "fresh-session"
    assert reloaded_session.debug_enabled is False


def test_archived_sessions_exclude_foreign_sessions_hidden_by_settings(tmp_path, monkeypatch) -> None:
    from iac_code.web import session_manager as session_manager_module
    from iac_code.web.app import create_app

    monkeypatch.setattr(session_manager_module, "is_foreign_pipeline_visible", lambda: False)
    manager, session = _foreign_pipeline_session(tmp_path, session_id="hidden-foreign-archive")
    session.archived = True
    manager.persist_web_metadata(session)
    app = create_app(session_manager=manager)

    with TestClient(app) as client:
        response = client.get("/api/sessions/archived")

    assert response.status_code == 200
    assert response.json()["projects"] == []


def test_patch_session_rejects_unsupported_fields_with_json_error(tmp_path) -> None:
    from iac_code.web.app import create_app
    from iac_code.web.session_manager import WebSessionManager

    manager = WebSessionManager(projects_dir=tmp_path / "projects", cwd=tmp_path / "project")
    session = manager.create_session()
    app = create_app(session_manager=manager)

    with TestClient(app) as client:
        response = client.patch(f"/api/sessions/{session.session_id}", json={"apiKey": "sk-test-secret"})

    assert response.status_code == 400
    assert response.headers["content-type"].startswith("application/json")
    assert response.json() == {"error": {"message": "unsupported session fields: apiKey"}}
    assert "sk-test-secret" not in response.text


def test_put_session_permission_mode_updates_session_context(tmp_path) -> None:
    from iac_code.types.permissions import PermissionMode
    from iac_code.web.app import create_app
    from iac_code.web.session_manager import WebSessionManager

    manager = WebSessionManager(projects_dir=tmp_path / "projects", cwd=tmp_path / "project")
    session = manager.create_session()
    app = create_app(session_manager=manager)

    with TestClient(app) as client:
        response = client.put(
            f"/api/sessions/{session.session_id}/permission-mode",
            json={"mode": "bypass_permissions"},
        )

    assert response.status_code == 200
    payload = response.json()
    assert payload["sessionId"] == session.session_id
    assert payload["permissionMode"] == "bypass_permissions"
    assert session.permission_context is not None
    assert session.permission_context.mode is PermissionMode.BYPASS_PERMISSIONS
    assert session.events.replay_after(0)[-1]["payload"] == {"permissionMode": "bypass_permissions"}

    reloaded_manager = WebSessionManager(projects_dir=tmp_path / "projects", cwd=tmp_path / "project")
    reloaded_session = reloaded_manager.create_session(session_id=session.session_id)
    assert reloaded_session.to_dict()["permissionMode"] == "bypass_permissions"


def test_put_session_permission_mode_rejects_invalid_mode(tmp_path) -> None:
    from iac_code.web.app import create_app
    from iac_code.web.session_manager import WebSessionManager

    manager = WebSessionManager(projects_dir=tmp_path / "projects", cwd=tmp_path / "project")
    session = manager.create_session()
    app = create_app(session_manager=manager)

    with TestClient(app) as client:
        response = client.put(f"/api/sessions/{session.session_id}/permission-mode", json={"mode": "danger"})

    assert response.status_code == 400
    assert "Invalid --permission-mode" in response.json()["error"]["message"]
    assert session.permission_context is None


def test_put_session_thinking_enabled_persists_and_injects(tmp_path) -> None:
    from iac_code.web.app import create_app
    from iac_code.web.runtime import WebModelSelection, agent_factory_options_for_session
    from iac_code.web.session_manager import WebSessionManager

    manager = WebSessionManager(projects_dir=tmp_path / "projects", cwd=tmp_path / "project")
    session = manager.create_session()
    # 固定为可思考模型,让 thinkingEffective 的解析与运行环境的默认 provider 无关。
    session.provider = "dashscope"
    session.model = "qwen3.7-max"
    app = create_app(session_manager=manager)

    with TestClient(app) as client:
        response = client.put(
            f"/api/sessions/{session.session_id}/thinking-enabled",
            json={"enabled": True},
        )

    assert response.status_code == 200
    payload = response.json()
    assert payload["sessionId"] == session.session_id
    assert payload["thinkingEnabled"] is True
    # 显式开启后 effective 即等于 override,供其它标签页初始渲染对齐。
    assert payload["thinkingEffective"] is True
    assert session.thinking_enabled is True
    assert session.events.replay_after(0)[-1]["payload"] == {
        "thinkingEnabled": True,
        "thinkingEffective": True,
    }

    # 会话级覆盖只注入到本回合内存副本(不落 settings.yml)。
    selection = WebModelSelection(provider="qwen", model="qwen-max", effort=None, provider_config_override={})
    options = agent_factory_options_for_session(session, manager, model_selection=selection)
    assert options.provider_config_override is not None
    assert options.provider_config_override["thinkingEnabled"] is True
    # 未污染传入的 selection 副本。
    assert selection.provider_config_override == {}

    # 持久化:重载会话后仍为 True。
    reloaded_manager = WebSessionManager(projects_dir=tmp_path / "projects", cwd=tmp_path / "project")
    reloaded_session = reloaded_manager.create_session(session_id=session.session_id)
    assert reloaded_session.to_dict()["thinkingEnabled"] is True


def test_put_session_thinking_enabled_null_clears_override(tmp_path) -> None:
    from iac_code.web.app import create_app
    from iac_code.web.runtime import WebModelSelection, agent_factory_options_for_session
    from iac_code.web.session_manager import WebSessionManager

    manager = WebSessionManager(projects_dir=tmp_path / "projects", cwd=tmp_path / "project")
    session = manager.create_session()
    session.thinking_enabled = True
    app = create_app(session_manager=manager)

    with TestClient(app) as client:
        response = client.put(
            f"/api/sessions/{session.session_id}/thinking-enabled",
            json={"enabled": None},
        )

    assert response.status_code == 200
    assert response.json()["thinkingEnabled"] is None
    assert session.thinking_enabled is None

    # 清除覆盖后不再注入 thinkingEnabled 键,回落 provider 默认。
    selection = WebModelSelection(provider="qwen", model="qwen-max", effort=None, provider_config_override={})
    options = agent_factory_options_for_session(session, manager, model_selection=selection)
    assert "thinkingEnabled" not in (options.provider_config_override or {})


def test_put_session_thinking_enabled_rejects_non_bool(tmp_path) -> None:
    from iac_code.web.app import create_app
    from iac_code.web.session_manager import WebSessionManager

    manager = WebSessionManager(projects_dir=tmp_path / "projects", cwd=tmp_path / "project")
    session = manager.create_session()
    app = create_app(session_manager=manager)

    with TestClient(app) as client:
        bad_value = client.put(
            f"/api/sessions/{session.session_id}/thinking-enabled",
            json={"enabled": "yes"},
        )
        missing = client.put(f"/api/sessions/{session.session_id}/thinking-enabled", json={})

    assert bad_value.status_code == 400
    assert missing.status_code == 400
    assert session.thinking_enabled is None


def test_suggestions_hide_thinking_enabled_command(tmp_path) -> None:
    from iac_code.web.app import create_app
    from iac_code.web.session_manager import WebSessionManager

    manager = WebSessionManager(projects_dir=tmp_path / "projects", cwd=tmp_path / "project")
    session = manager.create_session()
    app = create_app(session_manager=manager)

    with TestClient(app) as client:
        response = client.get(
            "/api/suggestions",
            params={"kind": "command", "q": "", "sessionId": session.session_id},
        )

    assert response.status_code == 200
    suggestions = response.json()["suggestions"]
    values = [item.get("value", "") for item in suggestions]
    # 命令列表非空(证明过滤非空跑通),且不含 thinking_enabled。
    assert values
    assert all("thinking_enabled" not in value for value in values)


def test_patch_session_reports_secret_like_unsupported_field_names_locally(tmp_path) -> None:
    from iac_code.web.app import create_app
    from iac_code.web.session_manager import WebSessionManager

    manager = WebSessionManager(projects_dir=tmp_path / "projects", cwd=tmp_path / "project")
    session = manager.create_session()
    app = create_app(session_manager=manager)

    with TestClient(app) as client:
        response = client.patch(f"/api/sessions/{session.session_id}", json={"sk-test-secret": True})

    assert response.status_code == 400
    assert response.headers["content-type"].startswith("application/json")
    assert "sk-test-secret" in response.text


def test_patch_session_rejects_invalid_payload_without_partial_mutation(tmp_path) -> None:
    from iac_code.web.app import create_app
    from iac_code.web.session_manager import WebSessionManager

    manager = WebSessionManager(projects_dir=tmp_path / "projects", cwd=tmp_path / "project")
    session = manager.create_session(mode="pipeline")
    original_title = session.title
    app = create_app(session_manager=manager)

    with TestClient(app) as client:
        response = client.patch(
            f"/api/sessions/{session.session_id}",
            json={
                "title": "changed",
                "debugEnabled": True,
                "allowUserEscapes": {"skill": True, "command": True, "shell": "yes"},
            },
        )

    assert response.status_code == 400
    assert response.headers["content-type"].startswith("application/json")
    assert session.title == original_title
    assert session.debug_enabled is False
    assert session.allow_user_escapes.skill is False
    assert session.allow_user_escapes.command is False
    assert session.allow_user_escapes.shell is False


def test_session_debug_route_returns_local_status_snapshot(tmp_path) -> None:
    from iac_code.web.app import create_app
    from iac_code.web.session_manager import WebSessionManager

    manager = WebSessionManager(projects_dir=tmp_path / "projects", cwd=tmp_path / "project")
    session = manager.create_session(mode="pipeline", context_id="ctx-1", task_id="task-1")
    manager.toggle_debug(session, enabled=True)
    session.events.append("error", {"message": "apiKey=sk-test-secret"})
    app = create_app(session_manager=manager)

    with TestClient(app) as client:
        response = client.get(f"/api/sessions/{session.session_id}/debug")

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("application/json")
    payload = response.json()
    assert payload["sessionId"] == session.session_id
    assert payload["debugEnabled"] is True
    assert payload["mode"] == "pipeline"
    assert payload["status"] == "idle"
    assert payload["latestSequence"] == session.events.latest_sequence
    assert payload["pendingPermissionCount"] == 0
    assert payload["pendingQuestionCount"] == 0
    assert payload["currentTurnActive"] is False
    assert payload["lastError"]["message"] == "apiKey=sk-test-secret"
    assert payload["pipeline"]["pipelineRunId"] == "ctx-1"
    assert payload["pipeline"]["taskId"] == "task-1"
    assert payload["pipeline"]["contextId"] == "ctx-1"
    assert "lastSequence" in payload["pipeline"]
    assert "currentStep" in payload["pipeline"]
    assert "waitingInput" in payload["pipeline"]
    assert "handoff" in payload["pipeline"]
    assert "cleanup" in payload["pipeline"]
    assert "warningHistory" in payload["pipeline"]
    assert "rollbackHistory" in payload["pipeline"]
    assert "candidateRestarts" in payload["pipeline"]
    assert "permissionRules" in payload
    assert "toolTimeline" in payload
    assert "providerSummary" in payload
    assert "cloudSummary" in payload
    assert "sk-test-secret" in response.text


def test_session_status_route_includes_runtime_pipeline_usage_and_cleanup_sections(tmp_path) -> None:
    from iac_code.agent.message import Message
    from iac_code.services.session_usage import SessionUsageStore
    from iac_code.types.stream_events import Usage
    from iac_code.web.app import create_app
    from iac_code.web.session_manager import WebSessionManager

    project = tmp_path / "project"
    manager = WebSessionManager(projects_dir=tmp_path / "projects", cwd=project)
    session = manager.create_session(mode="pipeline", pipeline_name="selling", context_id="ctx-1", task_id="task-1")
    manager.storage.append(str(project), session.session_id, Message(role="user", content="hello"))
    SessionUsageStore(projects_dir=tmp_path / "projects").append(
        str(project),
        session.session_id,
        Usage(input_tokens=3, output_tokens=5),
        provider="openai",
        model="gpt-5.5",
    )
    app = create_app(session_manager=manager)

    with TestClient(app) as client:
        response = client.get(f"/api/sessions/{session.session_id}/status")

    assert response.status_code == 200
    payload = response.json()
    assert payload["messageCounts"] == {"visible": 1, "resume": 1}
    assert payload["usage"]["inputTokens"] == 3
    assert payload["usage"]["outputTokens"] == 5
    assert payload["turn"]["active"] is False
    assert payload["pipeline"]["pipelineName"] == "selling"
    assert payload["pipeline"]["pipelineRunId"] == "ctx-1"
    assert payload["pipeline"]["contextId"] == "ctx-1"
    assert payload["pipeline"]["taskId"] == "task-1"
    assert "lastSequence" in payload["pipeline"]
    assert "currentStep" in payload["pipeline"]
    assert "waitingInput" in payload["pipeline"]
    assert "handoff" in payload["pipeline"]
    assert "warningHistory" in payload["pipeline"]
    assert "rollbackHistory" in payload["pipeline"]
    assert "candidateRestarts" in payload["pipeline"]
    assert "cleanup" in payload
    assert payload["pipeline"]["cleanupStatus"] == payload["cleanup"]["status"]
    assert "cloud" in payload
    assert "activeProvider" in payload


def test_session_status_route_includes_estimated_context_usage(tmp_path) -> None:
    from iac_code.agent.message import Message
    from iac_code.web.app import create_app
    from iac_code.web.session_manager import WebSessionManager

    project = tmp_path / "project"
    manager = WebSessionManager(projects_dir=tmp_path / "projects", cwd=project)
    session = manager.create_session(mode="normal")
    manager.storage.append(str(project), session.session_id, Message(role="user", content="hello context"))
    manager.storage.append(str(project), session.session_id, Message(role="assistant", content="hello back"))
    app = create_app(session_manager=manager)

    with TestClient(app) as client:
        response = client.get(f"/api/sessions/{session.session_id}/status")

    assert response.status_code == 200
    payload = response.json()
    assert payload["contextUsage"]["totalTokens"] > 0
    assert payload["contextUsage"]["contextWindow"] >= 128_000
    assert payload["contextUsage"]["messageCount"] == 2


def test_session_prompt_route_returns_truthful_local_snapshot(tmp_path) -> None:
    from iac_code.agent.message import Message
    from iac_code.web.app import create_app
    from iac_code.web.session_manager import WebSessionManager

    project = tmp_path / "project"
    manager = WebSessionManager(projects_dir=tmp_path / "projects", cwd=project)
    session = manager.create_session(mode="pipeline", pipeline_name="deploy", context_id="ctx-1", task_id="task-1")
    manager.storage.append(str(project), session.session_id, Message(role="user", content="apiKey=sk-test-secret"))
    app = create_app(session_manager=manager)

    with TestClient(app) as client:
        response = client.get(f"/api/sessions/{session.session_id}/prompt")

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("application/json")
    payload = response.json()
    assert payload["sessionId"] == session.session_id
    assert payload["redacted"] is False
    assert payload["available"] is True
    assert payload["mode"] == "pipeline"
    assert payload["cwd"] == str(project)
    assert payload["pipeline"]["pipelineName"] == "deploy"
    assert payload["pipeline"]["contextId"] == "ctx-1"
    assert payload["pipeline"]["taskId"] == "task-1"
    assert payload["messageCounts"]["visible"] == 1
    assert payload["messageCounts"]["resume"] == 1
    assert payload["sources"]["normal"]["messageCount"] == 1
    assert payload["sources"]["pipeline"]["contextId"] == "ctx-1"
    assert "systemSections" in payload
    assert "providerMessages" in payload
    assert "toolDefinitions" in payload
    assert "memorySections" in payload
    assert "cleanupPromptSummary" in payload
    assert "sk-test-secret" in response.text


def test_session_prompt_route_populates_available_local_sources(tmp_path, monkeypatch) -> None:
    from iac_code.web import cleanup as cleanup_module
    from iac_code.web.app import create_app
    from iac_code.web.memory import save_project_instruction
    from iac_code.web.session_manager import WebSessionManager
    from iac_code.web.settings import save_active_provider

    monkeypatch.setenv("IAC_CODE_CONFIG_DIR", str(tmp_path / "config"))

    async def fake_pipeline_state(query_params, **_kwargs):
        assert query_params["contextId"] == "ctx-1"
        return {
            "snapshot": {
                "pipelineRunId": "run-1",
                "contextId": "ctx-1",
                "taskId": "task-1",
                "lastSequence": 42,
                "currentStep": {"id": "confirm", "title": "Confirm plan"},
                "waitingInput": {"kind": "candidateSelection", "message": "choose"},
                "cleanup": {
                    "status": "pending",
                    "resourceCount": 1,
                    "resources": [{"resourceId": "stack-1"}],
                    "prompt": "cleanup accessKeySecret=super-secret",
                },
                "warningHistory": [{"message": "warn token=unsafe-token"}],
                "rollbackHistory": [{"from": "deploy", "to": "review"}],
                "candidateRestarts": [{"candidateName": "Plan A"}],
                "handoff": {"status": "pending", "summary": "handoff"},
            }
        }

    monkeypatch.setattr("iac_code.web.pipeline.pipeline_state_from_query", fake_pipeline_state)
    monkeypatch.setattr(cleanup_module, "pipeline_state_from_query", fake_pipeline_state)
    save_active_provider(
        {
            "provider": "openai",
            "model": "gpt-5.5",
            "effort": "medium",
            "apiKey": "sk-realpromptsecret123456",
        }
    )
    project = tmp_path / "project"
    project.mkdir()
    save_project_instruction(project, "Project rule with apiKey=sk-memorysecret123456")
    manager = WebSessionManager(projects_dir=tmp_path / "projects", cwd=project)
    session = manager.create_session(mode="pipeline", pipeline_name="deploy", context_id="ctx-1", task_id="task-1")
    app = create_app(session_manager=manager)

    with TestClient(app) as client:
        response = client.get(f"/api/sessions/{session.session_id}/prompt")

    assert response.status_code == 200
    payload = response.json()
    assert payload["providerMessages"]
    assert payload["providerMessages"][0]["provider"] == "openai"
    assert payload["providerMessages"][0]["credentialConfigured"] is True
    assert payload["toolDefinitions"]
    assert {tool["name"] for tool in payload["toolDefinitions"]} >= {"/status", "/prompt", "/resume"}
    assert payload["memorySections"]
    assert payload["memorySections"][0]["scope"] == "project"
    assert payload["cleanupPromptSummary"]["available"] is True
    assert payload["cleanupPromptSummary"]["status"] == "pending"
    assert payload["sources"]["pipeline"]["snapshot"]["lastSequence"] == 42
    assert payload["sources"]["pipeline"]["currentStep"]["id"] == "confirm"
    # Provider summaries intentionally expose only configuration state, not credential values.
    assert "sk-realpromptsecret" not in response.text
    assert "sk-memorysecret" in response.text
    assert "super-secret" in response.text


def test_status_and_debug_merge_recovered_pipeline_snapshot(tmp_path, monkeypatch) -> None:
    from iac_code.web import cleanup as cleanup_module
    from iac_code.web.app import create_app
    from iac_code.web.session_manager import WebSessionManager
    from iac_code.web.settings import save_active_provider

    monkeypatch.setenv("IAC_CODE_CONFIG_DIR", str(tmp_path / "config"))

    async def fake_pipeline_state(query_params, **_kwargs):
        assert query_params["contextId"] == "ctx-1"
        return {
            "snapshot": {
                "pipelineRunId": "run-1",
                "contextId": "ctx-1",
                "taskId": "task-1",
                "lastSequence": 99,
                "currentStep": {"id": "deploy", "status": "running"},
                "waitingInput": {"kind": "approval", "message": "approve deploy"},
                "handoff": {"status": "pending", "summary": "needs handoff"},
                "cleanup": {"status": "pending", "resourceCount": 2},
                "warningHistory": [{"message": "warning apiKey=sk-warningsecret123456"}],
                "rollbackHistory": [{"from": "deploy", "to": "review", "reason": "fix"}],
                "candidateRestarts": [{"candidateName": "Plan B", "count": 1}],
            }
        }

    monkeypatch.setattr("iac_code.web.pipeline.pipeline_state_from_query", fake_pipeline_state)
    monkeypatch.setattr(cleanup_module, "pipeline_state_from_query", fake_pipeline_state)
    save_active_provider(
        {
            "provider": "openai",
            "model": "gpt-5.5",
            "effort": "medium",
            "apiKey": "sk-statussecret123456",
        }
    )
    manager = WebSessionManager(projects_dir=tmp_path / "projects", cwd=tmp_path / "project")
    session = manager.create_session(mode="pipeline", pipeline_name="deploy", context_id="ctx-1", task_id="task-1")
    app = create_app(session_manager=manager)

    with TestClient(app) as client:
        status_response = client.get(f"/api/sessions/{session.session_id}/status")
        debug_response = client.get(f"/api/sessions/{session.session_id}/debug")

    assert status_response.status_code == 200
    assert debug_response.status_code == 200
    for response in (status_response, debug_response):
        pipeline = response.json()["pipeline"]
        assert pipeline["pipelineRunId"] == "run-1"
        assert pipeline["lastSequence"] == 99
        assert pipeline["currentStep"] == {"id": "deploy", "status": "running"}
        assert pipeline["waitingInput"]["kind"] == "approval"
        assert pipeline["handoff"]["status"] == "pending"
        assert pipeline["cleanup"]["status"] == "pending"
        assert pipeline["warningHistory"][0]["message"] == "warning apiKey=sk-warningsecret123456"
        assert pipeline["rollbackHistory"] == [{"from": "deploy", "to": "review", "reason": "fix"}]
        assert pipeline["candidateRestarts"] == [{"candidateName": "Plan B", "count": 1}]
        assert "sk-statussecret" not in response.text
        assert "sk-warningsecret" in response.text
        assert response.json()["providerSummary"]["credentialConfigured"] is True


def test_session_compact_route_runs_agent_compaction_and_emits_events(tmp_path) -> None:
    from iac_code.web.app import create_app
    from iac_code.web.session_manager import WebSessionManager

    manager = WebSessionManager(projects_dir=tmp_path / "projects", cwd=tmp_path / "project")
    session = manager.create_session()
    app = create_app(session_manager=manager)

    with TestClient(app) as client:
        response = client.post(f"/api/sessions/{session.session_id}/compact")

    assert response.status_code == 202
    assert response.headers["content-type"].startswith("application/json")
    payload = response.json()
    assert payload["accepted"] is False
    assert payload["state"] == "empty"
    assert payload["available"] is True
    events = session.events.replay_after(0)
    assert [event["type"] for event in events] == ["compaction.started", "compaction.finished"]
    assert events[-1]["payload"]["state"] == "empty"


@pytest.mark.parametrize("lifecycle_action", ["archive", "delete"])
@pytest.mark.asyncio
async def test_session_compaction_reports_lifecycle_winner_without_finished_event(
    tmp_path,
    lifecycle_action,
) -> None:
    from iac_code.web.app import create_app
    from iac_code.web.session_manager import WebSessionManager

    manager = WebSessionManager(projects_dir=tmp_path / "projects", cwd=tmp_path / "project")
    session = manager.create_session(session_id="compact-lifecycle-race")
    app = create_app(session_manager=manager)
    await session.turn_admission_lock.acquire()

    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://testserver") as client:
        if lifecycle_action == "archive":
            lifecycle_task = asyncio.create_task(
                client.patch(f"/api/sessions/{session.session_id}", json={"archived": True})
            )
        else:
            lifecycle_task = asyncio.create_task(client.delete(f"/api/sessions/{session.session_id}"))
        await asyncio.sleep(0)
        compact_task = asyncio.create_task(client.post(f"/api/sessions/{session.session_id}/compact"))
        await asyncio.sleep(0)
        session.turn_admission_lock.release()
        lifecycle_response, compact_response = await asyncio.gather(lifecycle_task, compact_task)

    assert lifecycle_response.status_code == 200
    if lifecycle_action == "archive":
        assert compact_response.status_code == 409
        assert compact_response.json()["error"]["code"] == "session_archived"
    else:
        assert compact_response.status_code == 404
        assert compact_response.json() == {"error": {"message": "session not found"}}
    assert not any(event["type"] == "compaction.finished" for event in session.events.replay_after(0))


@pytest.mark.asyncio
async def test_manual_compaction_runtime_creation_does_not_block_other_requests(tmp_path, monkeypatch) -> None:
    import threading
    import time

    from iac_code.web.app import create_app
    from iac_code.web.session_manager import WebSessionManager

    release = threading.Event()

    class FakeResult:
        status = "success"
        original_tokens = 100
        compacted_tokens = 20
        preserve_recent_turns = 2

    class FakeRuntime:
        class AgentLoop:
            async def compact(self):
                return FakeResult()

        agent_loop = AgentLoop()

    def create_runtime(_options):
        release.wait(timeout=1)
        return FakeRuntime()

    monkeypatch.setattr("iac_code.web.runtime.create_agent_runtime", create_runtime)
    monkeypatch.setattr("iac_code.services.agent_factory.create_agent_runtime", create_runtime)
    manager = WebSessionManager(projects_dir=tmp_path / "projects", cwd=tmp_path)
    session = manager.create_session(session_id="compact-nonblocking")
    app = create_app(session_manager=manager)
    timer = threading.Timer(0.3, release.set)
    timer.start()
    try:
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://testserver") as client:
            started_at = time.monotonic()
            compact_task = asyncio.create_task(client.post(f"/api/sessions/{session.session_id}/compact"))
            await asyncio.sleep(0)
            health_response = await client.get("/health")
            health_elapsed = time.monotonic() - started_at
            compact_response = await compact_task
    finally:
        release.set()
        timer.cancel()

    assert health_response.status_code == 200
    assert health_elapsed < 0.15
    assert compact_response.status_code == 202


def test_manual_compaction_blocks_turns_and_drains_queued_input(tmp_path, monkeypatch) -> None:
    """手动 /compact 期间:并发提交被拒(不并跑),排队输入在压缩完成后自动排空为独立 turn。"""
    from iac_code.web.app import create_app
    from iac_code.web.runtime import WebTurnRequest
    from iac_code.web.session_manager import WebSessionManager

    compaction_started = asyncio.Event()
    release_compaction = asyncio.Event()

    class _FakeCompactResult:
        status = "success"
        original_tokens = 1200
        compacted_tokens = 400
        preserve_recent_turns = 2

    class _FakeAgentLoop:
        async def compact(self):
            compaction_started.set()
            await release_compaction.wait()
            return _FakeCompactResult()

    class _FakeRuntime:
        agent_loop = _FakeAgentLoop()

    monkeypatch.setattr(
        "iac_code.services.agent_factory.create_agent_runtime",
        lambda *_args, **_kwargs: _FakeRuntime(),
    )
    monkeypatch.setattr(
        "iac_code.web.runtime.create_agent_runtime",
        lambda *_args, **_kwargs: _FakeRuntime(),
    )

    drained: list[WebTurnRequest] = []

    class RecordingRuntime:
        async def start_turn(self, request: WebTurnRequest) -> dict[str, object]:
            drained.append(request)
            return {"accepted": True, "turnId": request.turn_id or uuid.uuid4().hex}

    async def run() -> tuple[int, list[str], list[str], list[str]]:
        manager = WebSessionManager(projects_dir=tmp_path / "projects")
        session = manager.create_session(session_id="session-1")
        app = create_app(session_manager=manager, runtime_factory=lambda _session: RecordingRuntime())

        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://testserver") as client:
            compact_task = asyncio.ensure_future(client.post("/api/sessions/session-1/compact"))
            await asyncio.wait_for(compaction_started.wait(), timeout=1)
            # 压缩进行中:并发普通提交必须被拒(名额被压缩占用),不得并跑。
            concurrent = await client.post("/api/sessions/session-1/messages", json={"text": "并发输入"})
            # 同期排队输入照常接受,等压缩完成后排空。
            await client.post("/api/sessions/session-1/queued-inputs", json={"text": "排队输入"})
            release_compaction.set()
            await asyncio.wait_for(compact_task, timeout=1)
            active = session.active_turn_task
            if active is not None:
                await asyncio.wait_for(active, timeout=1)
            return (
                concurrent.status_code,
                [request.text for request in drained],
                [request.source for request in drained],
                list(session.queued_inputs),
            )

    concurrent_status, drained_texts, drained_sources, remaining_queue = asyncio.run(run())

    assert concurrent_status == 409
    # 排队输入在压缩完成后被排空成独立 turn。
    assert drained_texts == ["排队输入"]
    assert drained_sources == ["queued"]
    assert remaining_queue == []


def test_manual_compaction_restores_queued_input_when_followup_runtime_creation_fails(tmp_path, monkeypatch) -> None:
    """A pre-start failure after compaction must not consume the queued prompt."""
    from iac_code.web.app import create_app
    from iac_code.web.session_manager import WebSessionManager

    compaction_started = asyncio.Event()
    release_compaction = asyncio.Event()

    class FakeCompactResult:
        status = "success"
        original_tokens = 1200
        compacted_tokens = 400
        preserve_recent_turns = 2

    class FakeAgentLoop:
        async def compact(self):
            compaction_started.set()
            await release_compaction.wait()
            return FakeCompactResult()

    class FakeRuntime:
        agent_loop = FakeAgentLoop()

    monkeypatch.setattr(
        "iac_code.services.agent_factory.create_agent_runtime",
        lambda *_args, **_kwargs: FakeRuntime(),
    )
    monkeypatch.setattr(
        "iac_code.web.runtime.create_agent_runtime",
        lambda *_args, **_kwargs: FakeRuntime(),
    )

    def fail_followup_runtime(_session):
        raise ValueError("queued runtime failed before start")

    async def run() -> tuple[int, list[str], list[str]]:
        manager = WebSessionManager(projects_dir=tmp_path / "projects")
        session = manager.create_session(session_id="compact-restore-queue")
        app = create_app(session_manager=manager, runtime_factory=fail_followup_runtime)

        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://testserver") as client:
            compact_task = asyncio.create_task(client.post(f"/api/sessions/{session.session_id}/compact"))
            await asyncio.wait_for(compaction_started.wait(), timeout=1)
            queued_response = await client.post(
                f"/api/sessions/{session.session_id}/queued-inputs",
                json={"text": "must survive"},
            )
            assert queued_response.is_success
            release_compaction.set()
            compact_response = await asyncio.wait_for(compact_task, timeout=1)

        restored_events = [
            event["payload"]
            for event in session.events.replay_after(0)
            if event["type"] == "queued-input.accepted" and event["payload"].get("restored")
        ]
        return compact_response.status_code, list(session.queued_inputs), [event["text"] for event in restored_events]

    status_code, remaining_queue, restored_texts = asyncio.run(run())

    assert status_code == 202
    assert remaining_queue == ["must survive"]
    assert restored_texts == ["must survive"]


def test_manual_compaction_uses_session_runtime_overrides_and_closes_runtime(tmp_path, monkeypatch) -> None:
    from iac_code.web.app import create_app
    from iac_code.web.session_manager import WebSessionManager

    captured = {}

    class FakeCompactResult:
        status = "success"
        original_tokens = 100
        compacted_tokens = 20
        preserve_recent_turns = 2

    class FakeRuntime:
        closed = False

        class AgentLoop:
            async def compact(self):
                return FakeCompactResult()

        agent_loop = AgentLoop()

        async def aclose(self) -> None:
            self.closed = True

    fake_runtime = FakeRuntime()

    def create_runtime(options):
        captured["options"] = options
        return fake_runtime

    monkeypatch.setattr("iac_code.web.runtime.create_agent_runtime", create_runtime)
    monkeypatch.setattr("iac_code.services.agent_factory.create_agent_runtime", create_runtime)
    monkeypatch.setattr("iac_code.web.runtime.flush_telemetry", lambda: None)
    manager = WebSessionManager(projects_dir=tmp_path / "projects", cwd=tmp_path)
    session = manager.create_session(session_id="compact-overrides")
    session.provider = "custom-provider"
    session.model = "custom-model"
    session.effort = "high"
    app = create_app(session_manager=manager)

    with TestClient(app) as client:
        response = client.post(f"/api/sessions/{session.session_id}/compact")

    assert response.status_code == 202
    options = captured["options"]
    assert options.provider_key_override == "custom-provider"
    assert options.model == "custom-model"
    assert options.effort_override == "high"
    assert fake_runtime.closed is True


def test_manual_compaction_closes_runtime_when_compaction_fails(tmp_path, monkeypatch) -> None:
    from iac_code.web.app import create_app
    from iac_code.web.session_manager import WebSessionManager

    class FakeRuntime:
        close_calls = 0

        class AgentLoop:
            async def compact(self):
                raise RuntimeError("compact failed")

        agent_loop = AgentLoop()

        async def aclose(self) -> None:
            self.close_calls += 1

    fake_runtime = FakeRuntime()
    monkeypatch.setattr("iac_code.services.agent_factory.create_agent_runtime", lambda _options: fake_runtime)
    monkeypatch.setattr("iac_code.web.runtime.create_agent_runtime", lambda _options: fake_runtime)
    monkeypatch.setattr("iac_code.web.runtime.flush_telemetry", lambda: None)
    manager = WebSessionManager(projects_dir=tmp_path / "projects", cwd=tmp_path)
    session = manager.create_session(session_id="compact-failure")
    app = create_app(session_manager=manager)

    with TestClient(app) as client:
        response = client.post(f"/api/sessions/{session.session_id}/compact")

    assert response.status_code == 202
    assert fake_runtime.close_calls == 1
    finished = session.events.replay_after(0)[-1]
    assert finished["type"] == "compaction.finished"
    assert finished["payload"]["state"] == "failed"


def test_generic_404_returns_json(tmp_path) -> None:
    from iac_code.web.app import create_app
    from iac_code.web.session_manager import WebSessionManager

    manager = WebSessionManager(projects_dir=tmp_path / "projects")
    app = create_app(session_manager=manager)

    with TestClient(app) as client:
        response = client.get("/does-not-exist")

    assert response.status_code == 404
    assert response.headers["content-type"].startswith("application/json")
    assert response.json() == {"error": {"message": "not found"}}


def test_create_session_accepts_empty_body(tmp_path) -> None:
    from iac_code.web.app import create_app
    from iac_code.web.session_manager import WebSessionManager

    manager = WebSessionManager(projects_dir=tmp_path / "projects", cwd=tmp_path / "project")
    app = create_app(session_manager=manager)

    with TestClient(app) as client:
        response = client.post("/api/sessions")

    assert response.status_code == 201
    assert response.json()["mode"] == "normal"


def test_create_session_uses_env_defaults_when_fields_are_omitted(tmp_path, monkeypatch) -> None:
    from iac_code.web.app import create_app
    from iac_code.web.session_manager import WebSessionManager

    env_cwd = tmp_path / "env-project"
    monkeypatch.setenv("IAC_CODE_MODE", "pipeline")
    monkeypatch.setenv("IAC_CODE_PIPELINE_NAME", "selling")
    monkeypatch.setenv("IAC_CODE_CWD", str(env_cwd))
    manager = WebSessionManager(projects_dir=tmp_path / "projects")
    app = create_app(session_manager=manager)

    with TestClient(app) as client:
        response = client.post("/api/sessions", json={})

    assert response.status_code == 201
    payload = response.json()
    assert payload["mode"] == "pipeline"
    assert payload["pipelineName"] == "selling"
    assert payload["cwd"] == str(env_cwd.resolve())


def test_create_session_publishes_blocking_cleanup_state_before_normal_turns(tmp_path, monkeypatch) -> None:
    from iac_code.web.app import create_app
    from iac_code.web.session_manager import WebSessionManager

    async def pending_cleanup(_session):
        return {
            "status": "pending",
            "blocksNormalChat": True,
            "resources": [],
            "resourceCount": 0,
        }

    monkeypatch.setattr("iac_code.web.cleanup.session_cleanup_summary", pending_cleanup)
    manager = WebSessionManager(projects_dir=tmp_path / "projects", cwd=tmp_path / "project")
    app = create_app(session_manager=manager)

    with TestClient(app) as client:
        response = client.post("/api/sessions", json={"mode": "normal", "sessionId": "session-1"})

    assert response.status_code == 201
    session = manager.get_session("session-1")
    assert session is not None
    events = session.events.replay_after(0)
    assert [event["type"] for event in events] == ["session.started", "cleanup.status"]
    assert events[1]["payload"]["status"] == "pending"
    assert events[1]["payload"]["blocksNormalChat"] is True


def test_concurrent_messages_only_one_turn_is_accepted(tmp_path) -> None:
    from iac_code.web.app import create_app
    from iac_code.web.runtime import WebTurnRequest
    from iac_code.web.session_manager import WebSessionManager

    started_texts: list[str] = []
    ready = asyncio.Event()

    class ControllableRuntime:
        def __init__(self, _session) -> None:
            self.started_texts = started_texts

        async def start_turn(self, request: WebTurnRequest) -> dict[str, object]:
            self.started_texts.append(request.text)
            await ready.wait()
            return {"accepted": True, "turnId": request.turn_id}

    manager = WebSessionManager(projects_dir=tmp_path / "projects", cwd=tmp_path / "project")
    session = manager.create_session(session_id="session-1")
    app = create_app(session_manager=manager, runtime_factory=ControllableRuntime)

    async def post_concurrently():
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
            first = asyncio.create_task(
                client.post(f"/api/sessions/{session.session_id}/messages", json={"text": "first"})
            )
            await asyncio.sleep(0)
            second = asyncio.create_task(
                client.post(f"/api/sessions/{session.session_id}/messages", json={"text": "second"})
            )
            await asyncio.sleep(0.05)
            ready.set()
            return await asyncio.gather(first, second)

    responses = asyncio.run(post_concurrently())
    assert sorted(response.status_code for response in responses) == [202, 409]
    assert started_texts == ["first"]


def test_web_session_metadata_recovers_from_corrupt_json_and_rewrites_atomically(tmp_path) -> None:
    import json

    from iac_code.web.session_manager import WebSessionManager

    manager = WebSessionManager(projects_dir=tmp_path / "projects", cwd=tmp_path / "project")
    session = manager.create_session(session_id="session-1", mode="pipeline", context_id="ctx-1", task_id="task-1")
    metadata_path = manager.storage.session_dir(session.cwd, session.session_id) / "web-session.json"
    metadata_path.write_text("{not-json", encoding="utf-8")

    reloaded_manager = WebSessionManager(projects_dir=tmp_path / "projects", cwd=tmp_path / "project")
    reloaded = reloaded_manager.create_session(session_id="session-1")
    reloaded.mode = "pipeline"
    reloaded.context_id = "ctx-2"
    reloaded.task_id = "task-2"
    reloaded_manager.persist_web_metadata(reloaded)

    assert not metadata_path.with_suffix(".json.tmp").exists()
    data = json.loads(metadata_path.read_text(encoding="utf-8"))
    assert data["mode"] == "pipeline"
    assert data["contextId"] == "ctx-2"
    assert data["taskId"] == "task-2"


def test_create_session_rejects_malformed_json(tmp_path) -> None:
    from iac_code.web.app import create_app
    from iac_code.web.session_manager import WebSessionManager

    manager = WebSessionManager(projects_dir=tmp_path / "projects")
    app = create_app(session_manager=manager)

    with TestClient(app) as client:
        response = client.post("/api/sessions", content="{", headers={"content-type": "application/json"})

    assert response.status_code == 400
    assert response.json() == {"error": {"message": "malformed JSON request body"}}
    assert manager.list_sessions() == []


def test_create_session_rejects_non_object_json(tmp_path) -> None:
    from iac_code.web.app import create_app
    from iac_code.web.session_manager import WebSessionManager

    manager = WebSessionManager(projects_dir=tmp_path / "projects")
    app = create_app(session_manager=manager)

    with TestClient(app) as client:
        response = client.post("/api/sessions", json=[])

    assert response.status_code == 400
    assert response.json() == {"error": {"message": "request body must be a JSON object"}}
    assert manager.list_sessions() == []


def test_create_session_rejects_invalid_field_types(tmp_path) -> None:
    from iac_code.web.app import create_app
    from iac_code.web.session_manager import WebSessionManager

    manager = WebSessionManager(projects_dir=tmp_path / "projects")
    app = create_app(session_manager=manager)

    with TestClient(app, raise_server_exceptions=False) as client:
        responses = [
            client.post("/api/sessions", json={"cwd": 123}),
            client.post("/api/sessions", json={"mode": 123}),
            client.post("/api/sessions", json={"pipelineName": 123}),
            client.post("/api/sessions", json={"sessionId": 123}),
        ]

    assert [response.status_code for response in responses] == [400, 400, 400, 400]
    assert [_error_message(response) for response in responses] == [
        "cwd must be a string",
        "mode must be a string",
        "pipelineName must be a string",
        "sessionId must be a string",
    ]
    assert manager.list_sessions() == []


def test_create_session_rejects_non_string_context_id_with_json_error(tmp_path) -> None:
    from iac_code.web.app import create_app
    from iac_code.web.session_manager import WebSessionManager

    manager = WebSessionManager(projects_dir=tmp_path / "projects")
    app = create_app(session_manager=manager)

    with TestClient(app) as client:
        response = client.post("/api/sessions", json={"contextId": {"nested": "x"}})

    assert response.status_code == 400
    assert response.json() == {"error": {"message": "contextId must be a string"}}
    assert manager.list_sessions() == []


def test_create_session_rejects_non_string_task_id_with_json_error(tmp_path) -> None:
    from iac_code.web.app import create_app
    from iac_code.web.session_manager import WebSessionManager

    manager = WebSessionManager(projects_dir=tmp_path / "projects")
    app = create_app(session_manager=manager)

    with TestClient(app) as client:
        response = client.post("/api/sessions", json={"taskId": ["x"]})

    assert response.status_code == 400
    assert response.json() == {"error": {"message": "taskId must be a string"}}
    assert manager.list_sessions() == []


def test_create_session_rejects_invalid_mode_with_json_error(tmp_path) -> None:
    from iac_code.web.app import create_app
    from iac_code.web.session_manager import WebSessionManager

    manager = WebSessionManager(projects_dir=tmp_path / "projects")
    app = create_app(session_manager=manager)

    with TestClient(app) as client:
        response = client.post("/api/sessions", json={"mode": "chat"})

    assert response.status_code == 400
    assert response.json() == {"error": {"message": "mode must be normal or pipeline"}}
    assert manager.list_sessions() == []


def test_create_session_rejects_empty_mode_with_json_error(tmp_path) -> None:
    from iac_code.web.app import create_app
    from iac_code.web.session_manager import WebSessionManager

    manager = WebSessionManager(projects_dir=tmp_path / "projects")
    app = create_app(session_manager=manager)

    with TestClient(app) as client:
        response = client.post("/api/sessions", json={"mode": ""})

    assert response.status_code == 400
    assert response.json() == {"error": {"message": "mode must be normal or pipeline"}}
    assert manager.list_sessions() == []


def test_create_session_rejects_absolute_session_id_without_writing_outside_projects(tmp_path) -> None:
    from iac_code.web.app import create_app
    from iac_code.web.session_manager import WebSessionManager

    projects_dir = tmp_path / "projects"
    outside_session_dir = tmp_path / "outside-session"
    manager = WebSessionManager(projects_dir=projects_dir)
    app = create_app(session_manager=manager)

    with TestClient(app) as client:
        response = client.post("/api/sessions", json={"sessionId": str(outside_session_dir)})

    assert response.status_code == 400
    assert _error_message(response) == "sessionId is invalid"
    assert not outside_session_dir.exists()
    assert not any(projects_dir.rglob("*"))


def test_create_session_rejects_traversal_session_id_without_writing_outside_projects(tmp_path) -> None:
    from iac_code.web.app import create_app
    from iac_code.web.session_manager import WebSessionManager

    projects_dir = tmp_path / "projects"
    outside_session_dir = tmp_path / "escaped-session"
    manager = WebSessionManager(projects_dir=projects_dir, cwd=tmp_path / "project")
    app = create_app(session_manager=manager)

    with TestClient(app) as client:
        response = client.post("/api/sessions", json={"sessionId": "../../escaped-session"})

    assert response.status_code == 400
    assert _error_message(response) == "sessionId is invalid"
    assert not outside_session_dir.exists()
    assert not any(projects_dir.rglob("*"))


def test_provider_config_and_active_routes(tmp_path, monkeypatch) -> None:
    from iac_code.providers.registry import PROVIDER_REGISTRY
    from iac_code.web.app import create_app

    monkeypatch.setenv("IAC_CODE_CONFIG_DIR", str(tmp_path / "config"))
    app = create_app()

    key = next(k for k, d in PROVIDER_REGISTRY.items() if d.model_ids)
    model = PROVIDER_REGISTRY[key].model_ids[0]

    with TestClient(app) as client:
        # 保存配置:不激活
        resp = client.put("/api/providers/config", json={"provider": key, "model": model})
        assert resp.status_code == 200
        body = resp.json()
        entry = next(p for p in body["providers"] if p["key"] == key)
        assert entry["savedModel"] == model
        assert body["active"]["provider"] is None

        # 设为当前
        resp = client.put("/api/providers/active", json={"provider": key})
        assert resp.status_code == 200
        assert resp.json()["active"]["provider"] == key

        # 未配置 provider 设为当前 → 400
        other = next(k for k in PROVIDER_REGISTRY if k != key)
        resp = client.put("/api/providers/active", json={"provider": other})
        assert resp.status_code == 400


def test_delete_provider_config_route(tmp_path, monkeypatch) -> None:
    from iac_code.providers.registry import PROVIDER_REGISTRY
    from iac_code.web.app import create_app

    monkeypatch.setenv("IAC_CODE_CONFIG_DIR", str(tmp_path / "config"))
    app = create_app()

    key = next(k for k, d in PROVIDER_REGISTRY.items() if d.model_ids)
    model = PROVIDER_REGISTRY[key].model_ids[0]

    with TestClient(app) as client:
        client.put("/api/providers/config", json={"provider": key, "model": model, "apiKey": "sk-fake-delete"})

        # 清空配置:回到未配置状态
        resp = client.request("DELETE", "/api/providers/config", json={"provider": key})
        assert resp.status_code == 200
        entry = next(p for p in resp.json()["providers"] if p["key"] == key)
        assert entry["savedModel"] is None
        assert entry["hasApiKey"] is False

        # 当前激活的 provider 不可清空 → 400
        client.put("/api/providers/config", json={"provider": key, "model": model})
        client.put("/api/providers/active", json={"provider": key})
        resp = client.request("DELETE", "/api/providers/config", json={"provider": key})
        assert resp.status_code == 400


def _seed_search_session(manager, *, cwd, session_id, title, updated_at, archived=False):
    session = manager.create_session(cwd=cwd, session_id=session_id)
    session.title = title
    session.updated_at = updated_at
    session.archived = archived
    return session


def test_search_sessions_matches_title_label_and_cwd_case_insensitively(tmp_path) -> None:
    from iac_code.web.session_manager import WebSessionManager

    manager = WebSessionManager(projects_dir=tmp_path / "projects")
    alpha = str(tmp_path / "alpha-project")
    beta = str(tmp_path / "beta-project")
    _seed_search_session(manager, cwd=alpha, session_id="a1", title="Deploy VPC", updated_at="2026-07-01T00:00:00Z")
    _seed_search_session(manager, cwd=beta, session_id="b1", title="Something else", updated_at="2026-07-02T00:00:00Z")

    # 命中标题(大小写不敏感)。
    by_title, total = manager.search_sessions("deploy")
    assert total == 1
    assert [item["sessionId"] for item in by_title] == ["a1"]
    assert by_title[0]["projectLabel"] == "alpha-project"

    # 命中项目名 / cwd basename。
    by_project, _ = manager.search_sessions("beta-project")
    assert [item["sessionId"] for item in by_project] == ["b1"]


def test_search_sessions_empty_query_returns_all_sorted_desc(tmp_path) -> None:
    from iac_code.web.session_manager import WebSessionManager

    manager = WebSessionManager(projects_dir=tmp_path / "projects")
    cwd = str(tmp_path / "proj")
    _seed_search_session(manager, cwd=cwd, session_id="old", title="Older", updated_at="2026-07-01T00:00:00Z")
    _seed_search_session(manager, cwd=cwd, session_id="new", title="Newer", updated_at="2026-07-03T00:00:00Z")

    results, total = manager.search_sessions("")
    assert total == 2
    assert [item["sessionId"] for item in results] == ["new", "old"]


def test_search_sessions_excludes_archived_by_default_and_includes_on_flag(tmp_path) -> None:
    from iac_code.web.session_manager import WebSessionManager

    manager = WebSessionManager(projects_dir=tmp_path / "projects")
    cwd = str(tmp_path / "proj")
    _seed_search_session(manager, cwd=cwd, session_id="live", title="Live one", updated_at="2026-07-02T00:00:00Z")
    _seed_search_session(
        manager, cwd=cwd, session_id="gone", title="Archived one", updated_at="2026-07-01T00:00:00Z", archived=True
    )

    default_results, default_total = manager.search_sessions("one")
    assert default_total == 1
    assert [item["sessionId"] for item in default_results] == ["live"]

    with_archived, archived_total = manager.search_sessions("one", include_archived=True)
    assert archived_total == 2
    assert {item["sessionId"] for item in with_archived} == {"live", "gone"}


def test_search_sessions_limit_truncates_but_total_counts_all(tmp_path) -> None:
    from iac_code.web.session_manager import WebSessionManager

    manager = WebSessionManager(projects_dir=tmp_path / "projects")
    cwd = str(tmp_path / "proj")
    for index in range(5):
        _seed_search_session(
            manager,
            cwd=cwd,
            session_id="s{}".format(index),
            title="Match {}".format(index),
            updated_at="2026-07-0{}T00:00:00Z".format(index + 1),
        )

    results, total = manager.search_sessions("match", limit=2)
    assert total == 5
    assert len(results) == 2
    # 倒序:最新的 s4、s3 在前。
    assert [item["sessionId"] for item in results] == ["s4", "s3"]


def test_search_sessions_includes_pinned(tmp_path) -> None:
    from iac_code.web.session_manager import WebSessionManager

    manager = WebSessionManager(projects_dir=tmp_path / "projects")
    cwd = str(tmp_path / "proj")
    session = _seed_search_session(
        manager, cwd=cwd, session_id="pin", title="Pinned chat", updated_at="2026-07-01T00:00:00Z"
    )
    session.pinned = True

    results, total = manager.search_sessions("pinned")
    assert total == 1
    assert results[0]["sessionId"] == "pin"
    assert results[0]["projectLabel"] == "proj"


def test_search_route_returns_results_and_total(tmp_path) -> None:
    from iac_code.web.app import create_app
    from iac_code.web.session_manager import WebSessionManager

    manager = WebSessionManager(projects_dir=tmp_path / "projects")
    cwd = str(tmp_path / "project")
    app = create_app(session_manager=manager)

    with TestClient(app) as client:
        for title in ("deploy-vpc", "deploy-ecs", "unrelated"):
            created = client.post("/api/sessions", json={"cwd": cwd})
            session_id = created.json()["sessionId"]
            client.patch("/api/sessions/{}".format(session_id), json={"title": title})

        response = client.get("/api/sessions/search", params={"q": "deploy"})
        assert response.status_code == 200
        assert response.headers["cache-control"] == "no-store"
        payload = response.json()
        assert payload["total"] == 2
        titles = {item["title"] for item in payload["results"]}
        assert titles == {"deploy-vpc", "deploy-ecs"}
        assert all("projectLabel" in item for item in payload["results"])


def test_search_route_empty_query_lists_recent(tmp_path) -> None:
    from iac_code.web.app import create_app
    from iac_code.web.session_manager import WebSessionManager

    manager = WebSessionManager(projects_dir=tmp_path / "projects")
    cwd = str(tmp_path / "project")
    app = create_app(session_manager=manager)

    with TestClient(app) as client:
        created = client.post("/api/sessions", json={"cwd": cwd})
        client.patch("/api/sessions/{}".format(created.json()["sessionId"]), json={"title": "only-one"})

        response = client.get("/api/sessions/search")
        assert response.status_code == 200
        payload = response.json()
        assert payload["total"] == 1
        assert payload["results"][0]["title"] == "only-one"


def test_search_route_limit_and_bad_limit(tmp_path) -> None:
    from iac_code.web.app import create_app
    from iac_code.web.session_manager import WebSessionManager

    manager = WebSessionManager(projects_dir=tmp_path / "projects")
    cwd = str(tmp_path / "project")
    app = create_app(session_manager=manager)

    with TestClient(app) as client:
        for index in range(3):
            created = client.post("/api/sessions", json={"cwd": cwd})
            client.patch(
                "/api/sessions/{}".format(created.json()["sessionId"]),
                json={"title": "match-{}".format(index)},
            )

        limited = client.get("/api/sessions/search", params={"q": "match", "limit": 1})
        assert limited.status_code == 200
        limited_payload = limited.json()
        assert limited_payload["total"] == 3
        assert len(limited_payload["results"]) == 1

        bad = client.get("/api/sessions/search", params={"q": "match", "limit": "abc"})
        assert bad.status_code == 400


def test_search_route_wraps_search_in_batch_reads(tmp_path) -> None:
    """搜索路由须把 search_sessions 放进 batch_reads 窗口(缓存 foreign_hidden + 索引快照),
    否则逐会话重读 settings.yml,搜索延迟随会话数线性升高。"""
    from unittest.mock import patch

    from iac_code.web.app import create_app
    from iac_code.web.session_manager import WebSessionManager

    manager = WebSessionManager(projects_dir=tmp_path / "projects")
    cwd = str(tmp_path / "project")
    app = create_app(session_manager=manager)

    with TestClient(app) as client:
        created = client.post("/api/sessions", json={"cwd": cwd})
        client.patch("/api/sessions/{}".format(created.json()["sessionId"]), json={"title": "deploy"})

        depth_during_search: list[int] = []
        real_search = manager.search_sessions

        def spy_search(*args, **kwargs):
            depth_during_search.append(manager._batch_depth)
            return real_search(*args, **kwargs)

        with patch.object(manager, "search_sessions", side_effect=spy_search):
            response = client.get("/api/sessions/search", params={"q": "deploy"})

        assert response.status_code == 200
        # search_sessions 执行时须处于至少一层 batch_reads 窗口内。
        assert depth_during_search and all(depth >= 1 for depth in depth_during_search)


def test_memory_projects_route_wraps_scan_in_batch_reads(tmp_path) -> None:
    """记忆/插件/MCP 面板共用的项目枚举端点须把 known_project_entries 的全量扫描放进
    batch_reads 窗口(复用索引快照 + foreign_hidden 缓存),否则「加载项目」随会话数线性变慢。"""
    from unittest.mock import patch

    from iac_code.web.app import create_app
    from iac_code.web.session_manager import WebSessionManager

    manager = WebSessionManager(projects_dir=tmp_path / "projects")
    app = create_app(session_manager=manager)

    with TestClient(app) as client:
        client.post("/api/sessions", json={"cwd": str(tmp_path / "project")})

        depths: list[int] = []
        real = manager.list_session_projects

        def spy(*args, **kwargs):
            depths.append(manager._batch_depth)
            return real(*args, **kwargs)

        with patch.object(manager, "list_session_projects", side_effect=spy):
            response = client.get("/api/memory/projects")

        assert response.status_code == 200, response.text
        assert depths and all(depth >= 1 for depth in depths)


@pytest.mark.parametrize("path", ["/api/skills", "/api/memory", "/api/mcp/servers"])
def test_settings_cwd_routes_wrap_project_scan_in_batch_reads(tmp_path, path) -> None:
    """带 cwd 的技能/记忆/MCP 列表端点解析项目时会全量扫描;须放进 batch_reads 窗口,
    与首屏「加载项目」共用同一份缓存,避免第二段列表加载重复扫描出现秒级延迟。"""
    from unittest.mock import patch

    from iac_code.web.app import create_app
    from iac_code.web.session_manager import WebSessionManager

    manager = WebSessionManager(projects_dir=tmp_path / "projects")
    cwd = str(tmp_path / "project")
    app = create_app(session_manager=manager)

    with TestClient(app) as client:
        client.post("/api/sessions", json={"cwd": cwd})

        depths: list[int] = []
        real = manager.list_session_projects

        def spy(*args, **kwargs):
            depths.append(manager._batch_depth)
            return real(*args, **kwargs)

        with patch.object(manager, "list_session_projects", side_effect=spy):
            response = client.get(path, params={"cwd": cwd})

        assert response.status_code == 200, response.text
        # 项目枚举(list_session_projects)执行时须处于 batch_reads 窗口内。
        assert depths and all(depth >= 1 for depth in depths), "{} must scan inside batch_reads".format(path)


def test_archived_sessions_route_wraps_scan_in_batch_reads(tmp_path) -> None:
    """「已归档会话」端点须把 list_archived_projects 的全量扫描放进 batch_reads 窗口。"""
    from unittest.mock import patch

    from iac_code.web.app import create_app
    from iac_code.web.session_manager import WebSessionManager

    manager = WebSessionManager(projects_dir=tmp_path / "projects")
    app = create_app(session_manager=manager)

    with TestClient(app) as client:
        client.post("/api/sessions", json={"cwd": str(tmp_path / "project")})

        depths: list[int] = []
        real = manager.list_archived_projects

        def spy(*args, **kwargs):
            depths.append(manager._batch_depth)
            return real(*args, **kwargs)

        with patch.object(manager, "list_archived_projects", side_effect=spy):
            response = client.get("/api/sessions/archived")

        assert response.status_code == 200, response.text
        assert depths and all(depth >= 1 for depth in depths)


def test_update_status_reports_pending(monkeypatch) -> None:
    from iac_code.services.update_checker import PendingUpdate
    from iac_code.web import app as web_app
    from iac_code.web.app import create_app

    pending = PendingUpdate(
        version="9.9.9",
        current_version="0.9.1",
        source="official_pypi",
        checked_at=0.0,
        update_command=("py", "-m", "pip", "install", "--upgrade", "iac-code"),
        release_notes_url="https://example.invalid/notes",
    )
    monkeypatch.setattr(web_app, "get_pending_update", lambda **kw: pending)

    app = create_app()
    with TestClient(app) as client:
        response = client.get("/api/update/status")

    assert response.status_code == 200
    body = response.json()
    assert body["available"] is True
    assert body["currentVersion"] == "0.9.1"
    assert body["latestVersion"] == "9.9.9"
    assert body["releaseNotesUrl"] == "https://example.invalid/notes"
    assert body["applyState"] == "idle"
    assert body["error"] is None


def test_update_status_reports_no_update(monkeypatch) -> None:
    from iac_code.web import app as web_app
    from iac_code.web.app import create_app

    monkeypatch.setattr(web_app, "get_pending_update", lambda **kw: None)

    app = create_app()
    with TestClient(app) as client:
        response = client.get("/api/update/status")

    assert response.status_code == 200
    body = response.json()
    assert body["available"] is False
    assert body["latestVersion"] is None
    assert body["applyState"] == "idle"


def test_update_apply_without_pending_returns_409(monkeypatch) -> None:
    from iac_code.web import app as web_app
    from iac_code.web.app import create_app

    monkeypatch.setattr(web_app, "get_pending_update", lambda **kw: None)

    app = create_app()
    with TestClient(app) as client:
        response = client.post("/api/update/apply")

    assert response.status_code == 409


def test_update_apply_runs_command_and_reaches_done(monkeypatch) -> None:
    import subprocess
    import time

    from iac_code.services.update_checker import PendingUpdate
    from iac_code.web import app as web_app
    from iac_code.web.app import create_app

    pending = PendingUpdate(
        version="9.9.9",
        current_version="0.9.1",
        source="official_pypi",
        checked_at=0.0,
        update_command=("py", "-m", "pip", "install", "--upgrade", "iac-code"),
    )
    monkeypatch.setattr(web_app, "get_pending_update", lambda **kw: pending)

    calls = []

    def fake_run(update, **kwargs):
        calls.append(update)
        return subprocess.CompletedProcess(update.update_command, 0, stdout="ok", stderr="")

    monkeypatch.setattr(web_app, "run_update_command", fake_run)

    app = create_app()
    with TestClient(app) as client:
        response = client.post("/api/update/apply")
        assert response.status_code == 202
        assert response.json() == {"status": "updating"}

        # 后台线程执行升级,轮询状态直到 done。
        deadline = time.monotonic() + 5.0
        state = None
        while time.monotonic() < deadline:
            state = client.get("/api/update/status").json()["applyState"]
            if state in {"done", "failed"}:
                break
            time.sleep(0.02)

    assert state == "done"
    assert calls and calls[0] is pending


def test_update_apply_reports_failure(monkeypatch) -> None:
    import subprocess
    import time

    from iac_code.services.update_checker import PendingUpdate
    from iac_code.web import app as web_app
    from iac_code.web.app import create_app

    pending = PendingUpdate(
        version="9.9.9",
        current_version="0.9.1",
        source="official_pypi",
        checked_at=0.0,
        update_command=("py", "-m", "pip", "install", "--upgrade", "iac-code"),
    )
    monkeypatch.setattr(web_app, "get_pending_update", lambda **kw: pending)

    def fake_run(update, **kwargs):
        return subprocess.CompletedProcess(update.update_command, 1, stdout="", stderr="boom failure")

    monkeypatch.setattr(web_app, "run_update_command", fake_run)

    app = create_app()
    with TestClient(app) as client:
        client.post("/api/update/apply")
        deadline = time.monotonic() + 5.0
        body = None
        while time.monotonic() < deadline:
            body = client.get("/api/update/status").json()
            if body["applyState"] in {"done", "failed"}:
                break
            time.sleep(0.02)

    assert body["applyState"] == "failed"
    assert "boom failure" in body["error"]


def test_update_apply_while_running_returns_409(monkeypatch) -> None:
    import subprocess
    import threading

    from iac_code.services.update_checker import PendingUpdate
    from iac_code.web import app as web_app
    from iac_code.web.app import create_app

    pending = PendingUpdate(
        version="9.9.9",
        current_version="0.9.1",
        source="official_pypi",
        checked_at=0.0,
        update_command=("py", "-m", "pip", "install", "--upgrade", "iac-code"),
    )
    monkeypatch.setattr(web_app, "get_pending_update", lambda **kw: pending)

    started = threading.Event()
    release = threading.Event()

    def fake_run(update, **kwargs):
        # 阻塞升级线程,让首个 apply 停在 running,验证并发 apply 被拒。
        started.set()
        release.wait(5.0)
        return subprocess.CompletedProcess(update.update_command, 0, stdout="ok", stderr="")

    monkeypatch.setattr(web_app, "run_update_command", fake_run)

    app = create_app()
    try:
        with TestClient(app) as client:
            first = client.post("/api/update/apply")
            assert first.status_code == 202
            assert started.wait(5.0)

            # 已有升级在跑 → 第二次 apply 返回 409,避免并发升级。
            second = client.post("/api/update/apply")
            assert second.status_code == 409
            assert client.get("/api/update/status").json()["applyState"] == "running"
    finally:
        release.set()  # 放行后台线程,避免测试后线程泄漏


def test_update_dismiss_suppresses_pending_version(monkeypatch) -> None:
    from iac_code.services.update_checker import PendingUpdate
    from iac_code.web import app as web_app
    from iac_code.web.app import create_app

    pending = PendingUpdate(
        version="9.9.9",
        current_version="0.9.1",
        source="official_pypi",
        checked_at=0.0,
        update_command=("py", "-m", "pip", "install", "--upgrade", "iac-code"),
    )
    monkeypatch.setattr(web_app, "get_pending_update", lambda **kw: pending)
    suppressed = []
    monkeypatch.setattr(web_app, "suppress_version", lambda version, **kw: suppressed.append(version))

    app = create_app()
    with TestClient(app) as client:
        response = client.post("/api/update/dismiss")

    assert response.status_code == 204
    assert suppressed == ["9.9.9"]


def test_update_dismiss_without_pending_is_noop(monkeypatch) -> None:
    from iac_code.web import app as web_app
    from iac_code.web.app import create_app

    monkeypatch.setattr(web_app, "get_pending_update", lambda **kw: None)
    suppressed = []
    monkeypatch.setattr(web_app, "suppress_version", lambda version, **kw: suppressed.append(version))

    app = create_app()
    with TestClient(app) as client:
        response = client.post("/api/update/dismiss")

    assert response.status_code == 204
    assert suppressed == []


def test_index_injects_i18n_catalog(monkeypatch) -> None:
    from iac_code.web.app import create_app

    monkeypatch.setattr("iac_code.web.app.resolve_ui_language", lambda override: "en")
    monkeypatch.setattr("iac_code.web.app.load_webui_catalog", lambda lang: {})

    app = create_app()
    with TestClient(app) as client:
        html = client.get("/").text

    assert "window.__IAC_I18N__" in html
    assert '"lang": "en"' in html
    assert '"messages": {}' in html


def test_index_escapes_angle_bracket_in_injected_i18n(monkeypatch) -> None:
    # A translation containing </script> must not break out of the inline <script>.
    from iac_code.web.app import create_app

    monkeypatch.setattr("iac_code.web.app.resolve_ui_language", lambda override: "ja")
    monkeypatch.setattr("iac_code.web.app.load_webui_catalog", lambda lang: {"x": "a</script>b"})

    app = create_app()
    with TestClient(app) as client:
        html = client.get("/").text

    assert "window.__IAC_I18N__" in html
    assert "a</script>b" not in html
    assert "a\\u003c/script>b" in html


def test_index_sets_html_lang_for_language(monkeypatch) -> None:
    from iac_code.web.app import create_app

    monkeypatch.setattr("iac_code.web.app.resolve_ui_language", lambda override: "ja")
    monkeypatch.setattr("iac_code.web.app.load_webui_catalog", lambda lang: {"Send": "送信"})

    app = create_app()
    with TestClient(app) as client:
        html = client.get("/").text

    assert '<html lang="ja"' in html
    assert "送信" in html
