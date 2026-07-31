import asyncio
import json
import os
from contextlib import suppress
from datetime import datetime, timezone

import pytest

from iac_code.agent.message import (
    COMPACTION_SUMMARY_TAIL_METADATA_KEY,
    Message,
    ToolResultBlock,
    create_compaction_summary_message,
)
from iac_code.services.session_metadata import (
    SESSION_JSONL_FILENAME,
    SESSION_LAYOUT_VERSION_V2,
    SESSION_METADATA_FILENAME,
)
from iac_code.utils import project_paths
from iac_code.web.session_manager import (
    QueuedInputActionError,
    WebSessionManager,
    _context_usage_payload,
    _is_listable_session,
    _read_web_session_metadata,
    reorder_compaction_markers,
)


def _mark_web_session(manager, cwd, session_id):
    """给已播种的会话补 web-session.json 侧车,使其在新语义下视为 web(非外来)会话。"""
    sidecar = manager.storage.session_dir(cwd, session_id) / "web-session.json"
    sidecar.write_text("{}", encoding="utf-8")


class _FakeTurnTask:
    """duck-typed 替身：steer 只调用 turn_task.done()。"""

    def __init__(self, done: bool = False) -> None:
        self._done = done

    def done(self) -> bool:
        return self._done


class _FakeAgentLoop:
    """记录 try_inject_user_message 调用，并按 accept 决定是否接受注入。"""

    def __init__(self, accept: bool = True) -> None:
        self.accept = accept
        self.injected: list[str] = []
        self.injected_metadata: list[dict[str, str] | None] = []

    def try_inject_user_message(self, message: str, *, metadata: dict[str, str] | None = None) -> bool:
        if not self.accept:
            return False
        self.injected.append(message)
        self.injected_metadata.append(metadata)
        return True


def test_create_session_uses_directory_storage_and_metadata(tmp_path) -> None:
    cwd = str(tmp_path / "project")
    manager = WebSessionManager(projects_dir=tmp_path / "projects")

    session = manager.create_session(cwd=cwd, mode="pipeline", pipeline_name="selling")
    session_dir = manager.storage.session_dir(cwd, session.session_id)

    assert (session_dir / SESSION_JSONL_FILENAME).exists()
    assert (session_dir / SESSION_METADATA_FILENAME).exists()
    assert session.cwd == cwd
    assert session.mode == "pipeline"
    assert session.pipeline_name == "selling"
    assert isinstance(session.turn_lock, asyncio.Lock)
    assert session.draft == ""
    assert session.pending_permissions == {}
    assert session.pending_questions == {}


def test_cancel_pending_request_tolerates_owner_loop_closing_during_resolution(tmp_path) -> None:
    class ClosingLoop:
        def __init__(self) -> None:
            self.closed_checks = 0

        def is_closed(self) -> bool:
            self.closed_checks += 1
            return self.closed_checks > 1

        def is_running(self) -> bool:
            return False

    class RacingFuture:
        def __init__(self) -> None:
            self.loop = ClosingLoop()

        def done(self) -> bool:
            return False

        def get_loop(self):
            return self.loop

        def set_result(self, _result) -> None:
            raise RuntimeError("Event loop is closed")

    manager = WebSessionManager(projects_dir=tmp_path / "projects", cwd=tmp_path / "project")
    session = manager.create_session(session_id="pending-loop-close-race")
    manager.add_permission_request(session, {"toolName": "bash"}, future=RacingFuture())

    manager.cancel_pending_requests_for_session(session)

    assert session.pending_permissions == {}


def test_create_session_preserves_storage_layout_v2_metadata(tmp_path) -> None:
    cwd = str(tmp_path / "project")
    manager = WebSessionManager(projects_dir=tmp_path / "projects")

    session = manager.create_session(cwd=cwd, session_id="layout-v2")

    metadata = manager.storage.read_metadata(cwd, session.session_id)
    assert metadata is not None
    assert metadata.layout_version == SESSION_LAYOUT_VERSION_V2
    assert manager.storage.v2_session_dir(cwd, session.session_id) == manager.storage.session_dir(
        cwd, session.session_id
    )


def test_ensure_permission_context_trusts_v2_session_runtime_dirs(tmp_path) -> None:
    # 回归：V2 会话在首个回合之前改权限模式会提前构造会话级权限上下文；
    # 该上下文的授信读目录必须包含新布局的 session_dir/tool-results 与
    # session_dir/image-cache，否则 agent 读自己落盘的外部化工具结果/图片时会莫名弹权限框
    # （见 docs/web-rebase-impact-gaps-20260727.md A1）。
    cwd = str(tmp_path / "project")
    manager = WebSessionManager(projects_dir=tmp_path / "projects")
    session = manager.create_session(cwd=cwd, session_id="layout-v2-perm")

    manager.set_permission_mode(session, "default")

    session_dir = manager.storage.session_dir(cwd, session.session_id)
    trusted = set(session.permission_context.trusted_read_directories)
    assert str(session_dir / "tool-results") in trusted
    assert str(session_dir / "image-cache") in trusted


def test_web_mcp_elicitation_handler_resolves_form_answer(tmp_path) -> None:
    # A2：web 必须像权限/提问一样，把 MCP elicitation 请求转成前端待办 + future 回灌。
    # 见 docs/web-rebase-impact-gaps-20260727.md A2。
    from iac_code.web.permissions import elicitation_result_from_body

    manager = WebSessionManager(projects_dir=tmp_path / "projects")
    session = manager.create_session(cwd=str(tmp_path / "project"), mode="normal")
    params = {
        "message": "Provide a region",
        "requestedSchema": {
            "type": "object",
            "properties": {
                "region": {"title": "Region", "type": "string", "enum": ["cn-hangzhou", "cn-beijing"]},
                "confirm": {"title": "Confirm", "type": "boolean"},
            },
            "required": ["region"],
        },
    }

    async def scenario():
        handler_task = asyncio.ensure_future(manager.request_mcp_elicitation(session, "acme", params))
        for _ in range(20):
            await asyncio.sleep(0)
            if session.pending_elicitations:
                break
        assert session.pending_elicitations
        request_id = next(iter(session.pending_elicitations))
        pending = manager.get_pending_elicitation(request_id, session_id=session.session_id)
        assert pending is not None
        assert pending.payload["mode"] == "form"
        assert pending.payload["server"] == "acme"
        assert {field["name"] for field in pending.payload["fields"]} == {"region", "confirm"}
        result = elicitation_result_from_body(
            {"action": "accept", "content": {"region": "cn-hangzhou", "confirm": "yes"}},
            schema=pending.schema,
        )
        manager.resolve_elicitation(request_id, result, session_id=session.session_id)
        return await handler_task

    outcome = asyncio.run(scenario())
    assert outcome == {"action": "accept", "content": {"region": "cn-hangzhou", "confirm": True}}


def test_web_mcp_elicitation_handler_cancel_returns_cancel(tmp_path) -> None:
    manager = WebSessionManager(projects_dir=tmp_path / "projects")
    session = manager.create_session(cwd=str(tmp_path / "project"), mode="normal")

    async def scenario():
        handler_task = asyncio.ensure_future(
            manager.request_mcp_elicitation(session, "acme", {"message": "hi", "mode": "url", "url": "https://x"})
        )
        for _ in range(20):
            await asyncio.sleep(0)
            if session.pending_elicitations:
                break
        assert session.pending_elicitations
        request_id = next(iter(session.pending_elicitations))
        pending = manager.get_pending_elicitation(request_id, session_id=session.session_id)
        assert pending is not None
        assert pending.payload["mode"] == "url"
        manager.cancel_elicitation_request(request_id, session_id=session.session_id)
        return await handler_task

    assert asyncio.run(scenario()) == {"action": "cancel"}


def test_cancel_pending_requests_clears_elicitations(tmp_path) -> None:
    manager = WebSessionManager(projects_dir=tmp_path / "projects")
    session = manager.create_session(cwd=str(tmp_path / "project"), mode="normal")

    async def scenario():
        handler_task = asyncio.ensure_future(manager.request_mcp_elicitation(session, "acme", {"message": "hi"}))
        for _ in range(20):
            await asyncio.sleep(0)
            if session.pending_elicitations:
                break
        manager.cancel_pending_requests_for_session(session)
        return await handler_task

    assert asyncio.run(scenario()) == {"action": "cancel"}
    assert session.pending_elicitations == {}


def test_to_dict_exposes_queued_input_contents(tmp_path) -> None:
    # 回归：to_dict 必须暴露排队消息的完整内容（不只是数量），否则前端在 resync/
    # 切换会话重建状态时无法恢复“排队中”列表，权限确认一出现排队就会消失。
    manager = WebSessionManager(projects_dir=tmp_path / "projects")
    session = manager.create_session(cwd=str(tmp_path / "project"), mode="normal")
    session.queued_inputs = ["第一条", "  第二条  "]

    payload = session.to_dict()

    assert payload["queuedInputs"] == [{"text": "第一条"}, {"text": "  第二条  "}]
    # turn 子块里仍保留数量，供状态徽标使用。
    assert payload["turn"]["queuedInputs"] == 2


def test_delete_and_edit_queued_input_mutate_and_emit_events(tmp_path) -> None:
    manager = WebSessionManager(projects_dir=tmp_path / "projects")
    session = manager.create_session(cwd=str(tmp_path / "project"), mode="normal")
    session.queued_inputs = ["第一条", "第二条", "第三条"]

    edit_result = manager.edit_queued_input(session, 1, text="第二条改", expected_text="第二条")
    delete_result = manager.delete_queued_input(session, 0, expected_text="第一条")

    assert edit_result == {"updated": True, "index": 1, "text": "第二条改"}
    assert delete_result == {"removed": True, "index": 0}
    assert session.queued_inputs == ["第二条改", "第三条"]

    events = session.events.replay_after(0)
    updated = [event for event in events if event["type"] == "queued-input.updated"]
    removed = [event for event in events if event["type"] == "queued-input.removed"]
    assert updated[0]["payload"] == {"index": 1, "text": "第二条改"}
    assert removed[0]["payload"] == {"index": 0}


def test_pop_next_queued_input_pops_front_and_emits_removed(tmp_path) -> None:
    # 逐条排空:每次只取队首一条,发 queued-input.removed(index=0)让前端移除 chip;空则返回 None。
    manager = WebSessionManager(projects_dir=tmp_path / "projects")
    session = manager.create_session(cwd=str(tmp_path / "project"), mode="normal")
    session.queued_inputs = ["第一条", "第二条"]

    first = manager.pop_next_queued_input(session)
    second = manager.pop_next_queued_input(session)
    empty = manager.pop_next_queued_input(session)

    assert first == "第一条"
    assert second == "第二条"
    assert empty is None
    assert session.queued_inputs == []

    removed = [event for event in session.events.replay_after(0) if event["type"] == "queued-input.removed"]
    assert [event["payload"] for event in removed] == [{"index": 0}, {"index": 0}]


def test_queued_input_action_guards_raise_typed_errors(tmp_path) -> None:
    manager = WebSessionManager(projects_dir=tmp_path / "projects")
    session = manager.create_session(cwd=str(tmp_path / "project"), mode="normal")
    session.queued_inputs = ["第一条"]

    with pytest.raises(QueuedInputActionError) as stale:
        manager.delete_queued_input(session, 0, expected_text="不是这条")
    with pytest.raises(QueuedInputActionError) as out_of_range:
        manager.delete_queued_input(session, 5, expected_text="第一条")

    assert stale.value.status == 409
    assert out_of_range.value.status == 409
    # 校验失败不得改动队列。
    assert session.queued_inputs == ["第一条"]


def test_deny_permission_audits_without_blocking_rule_application(tmp_path, monkeypatch) -> None:
    from iac_code.types.stream_events import PermissionRequestEvent

    manager = WebSessionManager(projects_dir=tmp_path / "projects", cwd=tmp_path / "project")
    session = manager.create_session(session_id="permission-audit-deny")
    future = asyncio.new_event_loop().create_future()
    event = PermissionRequestEvent(
        tool_name="bash",
        tool_input={"command": "rm -rf build"},
        tool_use_id="tool-deny-1",
        response_future=future,
        audit_context={"session_id": session.session_id, "cwd": session.cwd},
    )
    calls = []

    def reject_audit(_event, **kwargs):
        calls.append(kwargs)
        return False

    monkeypatch.setattr(
        "iac_code.services.permissions.audit.emit_permission_boundary_audit",
        reject_audit,
    )
    request_id = manager.add_permission_request(
        session,
        {"toolName": "bash", "toolUseId": "tool-deny-1", "allowAlways": True},
        future=future,
        audit_event=event,
    )

    manager.resolve_permission(request_id, {"choice": "always_deny"}, session_id=session.session_id)

    assert future.result() is False
    assert session.permission_context is not None
    assert "bash" in session.permission_context.deny_rules["session"]
    assert calls == [
        {
            "session_id": session.session_id,
            "decision": "deny",
            "scope": "session_rule",
            "source": "web_prompt",
            "reason_type": "prompt_selection",
            "reason_detail": "always_deny",
            "rule": "bash",
        }
    ]


def test_steer_queued_input_injects_and_emits_unique_user_message(tmp_path) -> None:
    manager = WebSessionManager(projects_dir=tmp_path / "projects")
    session = manager.create_session(cwd=str(tmp_path / "project"), mode="normal")
    session.queued_inputs = ["插队消息"]
    loop = _FakeAgentLoop(accept=True)
    session.active_agent_loop = loop
    session.active_turn_task = _FakeTurnTask(done=False)
    session.active_turn_id = "T1"

    result = manager.steer_queued_input(session, 0, expected_text="插队消息")

    assert result["steered"] is True
    assert result["injected"] is True
    assert loop.injected == ["插队消息"]
    assert session.queued_inputs == []

    events = session.events.replay_after(0)
    user_messages = [event for event in events if event["type"] == "user.message"]
    removed = [event for event in events if event["type"] == "queued-input.removed"]
    assert len(user_messages) == 1
    payload = user_messages[0]["payload"]
    assert payload["text"] == "插队消息"
    assert payload["source"] == "steer"
    # 唯一 messageId 必须区别于首条 prompt 的 user-<turnId>，避免覆盖气泡。
    assert payload["messageId"].startswith("user-T1-steer-")
    assert payload["messageId"] != "user-T1"
    assert loop.injected_metadata == [
        {
            "messageId": payload["messageId"],
            "turnId": "T1",
            "source": "steer",
        }
    ]
    assert removed[0]["payload"] == {"index": 0}


def test_steer_queued_input_requeues_when_loop_refuses_injection(tmp_path) -> None:
    manager = WebSessionManager(projects_dir=tmp_path / "projects")
    session = manager.create_session(cwd=str(tmp_path / "project"), mode="normal")
    session.queued_inputs = ["先", "插队消息"]
    loop = _FakeAgentLoop(accept=False)
    session.active_agent_loop = loop
    session.active_turn_task = _FakeTurnTask(done=False)
    session.active_turn_id = "T1"

    result = manager.steer_queued_input(session, 1, expected_text="插队消息")

    assert result == {"steered": False, "requeued": True, "index": 1}
    # 注入被拒 → 回插队首，消息不丢；不发 user.message/removed。
    assert session.queued_inputs == ["插队消息", "先"]
    event_types = [event["type"] for event in session.events.replay_after(0)]
    assert "user.message" not in event_types
    assert "queued-input.removed" not in event_types


def test_steer_queued_input_without_active_turn_raises(tmp_path) -> None:
    manager = WebSessionManager(projects_dir=tmp_path / "projects")
    session = manager.create_session(cwd=str(tmp_path / "project"), mode="normal")
    session.queued_inputs = ["插队消息"]

    with pytest.raises(QueuedInputActionError) as exc:
        manager.steer_queued_input(session, 0, expected_text="插队消息")

    assert exc.value.status == 409
    assert session.queued_inputs == ["插队消息"]


def test_list_sessions_uses_existing_index(tmp_path) -> None:
    cwd = str(tmp_path / "project")
    manager = WebSessionManager(projects_dir=tmp_path / "projects")
    created = manager.create_session(cwd=cwd)
    manager.storage.append(cwd, created.session_id, Message(role="user", content="persisted prompt"))

    fresh_manager = WebSessionManager(projects_dir=tmp_path / "projects")
    listed = fresh_manager.list_sessions()

    assert [session.session_id for session in listed] == [created.session_id]
    assert listed[0].cwd == cwd
    assert listed[0].title == "persisted prompt"


def test_list_sessions_hides_persisted_empty_sessions_without_hiding_lookup(tmp_path) -> None:
    cwd = str(tmp_path / "project")
    manager = WebSessionManager(projects_dir=tmp_path / "projects")
    created = manager.create_session(cwd=cwd, session_id="abcdef123456")

    fresh_manager = WebSessionManager(projects_dir=tmp_path / "projects")

    assert fresh_manager.list_sessions() == []
    assert fresh_manager.get_session("abcdef") is not None
    assert fresh_manager.get_session("abcdef").session_id == created.session_id


def test_list_sessions_limit_counts_visible_sessions_after_empty_filtering(tmp_path) -> None:
    cwd = str(tmp_path / "project")
    storage_manager = WebSessionManager(projects_dir=tmp_path / "projects")
    for index in range(3):
        session_id = "real-{}".format(index)
        storage_manager.storage.append(cwd, session_id, Message(role="user", content="real prompt {}".format(index)))
        _mark_web_session(storage_manager, cwd, session_id)
    for index in range(5):
        storage_manager.storage.save(cwd, "empty-{}".format(index), [])

    fresh_manager = WebSessionManager(projects_dir=tmp_path / "projects")
    sessions, total = fresh_manager.list_sessions_page(limit=2)

    assert [session.title for session in sessions] == ["real prompt 2", "real prompt 1"]
    assert total == 8


def test_list_sessions_limit_scans_past_archived_sessions(tmp_path) -> None:
    cwd = str(tmp_path / "project")
    seed = WebSessionManager(projects_dir=tmp_path / "projects")
    for index in range(2):
        session_id = "active-{}".format(index)
        seed.storage.append(cwd, session_id, Message(role="user", content="active {}".format(index)))
        _mark_web_session(seed, cwd, session_id)
        os.utime(seed.storage.session_path(cwd, session_id), (100 + index, 100 + index))
    for index in range(2):
        session_id = "archived-{}".format(index)
        archived = seed.create_session(cwd=cwd, session_id=session_id)
        seed.storage.append(cwd, session_id, Message(role="user", content="archived {}".format(index)))
        archived.archived = True
        archived.updated_at = "2026-07-15T00:00:0{}Z".format(index)
        seed.persist_web_metadata(archived)
        os.utime(seed.storage.session_path(cwd, session_id), (200 + index, 200 + index))

    fresh = WebSessionManager(projects_dir=tmp_path / "projects")
    sessions, total = fresh.list_sessions_page(limit=2)

    assert [session.session_id for session in sessions] == ["active-1", "active-0"]
    assert total == 4


def test_list_sessions_limit_never_falls_back_to_archived_sessions(tmp_path) -> None:
    cwd = str(tmp_path / "project")
    seed = WebSessionManager(projects_dir=tmp_path / "projects")
    archived = seed.create_session(cwd=cwd, session_id="archived-only")
    seed.storage.append(cwd, "archived-only", Message(role="user", content="archived"))
    archived.archived = True
    seed.persist_web_metadata(archived)

    fresh = WebSessionManager(projects_dir=tmp_path / "projects")
    sessions, total = fresh.list_sessions_page(limit=1)

    assert sessions == []
    assert total == 1


def test_in_memory_empty_pipeline_session_is_hidden_from_flat_listings(tmp_path) -> None:
    """售卖流水线会话在拿到自动标题前标题恒为「(empty)」,其真实意图存于 pipeline 侧存储、
    不落 web session.jsonl。它以 web 会话身份常驻 _sessions,不应泄漏进展开项目/全量列表,
    须与分组视图(_collect_project_groups)/搜索的空会话过滤保持一致。"""
    cwd = str(tmp_path / "project")
    manager = WebSessionManager(projects_dir=tmp_path / "projects")
    empty_pipeline = manager.create_session(cwd=cwd, mode="pipeline", pipeline_name="selling")
    # 同项目下再放一个有真实标题的普通会话,让全量列表走主循环而非空兜底分支。
    real = manager.create_session(cwd=cwd, session_id="real-session")
    manager.storage.append(cwd, real.session_id, Message(role="user", content="真实需求"))
    real.title = "真实需求"

    assert empty_pipeline.title == "(empty)"
    assert not manager._foreign_hidden(cwd, empty_pipeline.session_id)

    project_ids = [session.session_id for session in manager.list_project_sessions(cwd)[0]]
    assert empty_pipeline.session_id not in project_ids
    assert real.session_id in project_ids

    page_ids = [session.session_id for session in manager.list_sessions_page()[0]]
    assert empty_pipeline.session_id not in page_ids
    assert real.session_id in page_ids


def test_list_project_sessions_returns_only_requested_project(tmp_path) -> None:
    first_cwd = str(tmp_path / "project-a")
    second_cwd = str(tmp_path / "project-b")
    storage_manager = WebSessionManager(projects_dir=tmp_path / "projects")
    for index in range(3):
        storage_manager.storage.append(
            first_cwd,
            "first-{}".format(index),
            Message(role="user", content="first prompt {}".format(index)),
        )
        _mark_web_session(storage_manager, first_cwd, "first-{}".format(index))
    storage_manager.storage.append(second_cwd, "second-1", Message(role="user", content="second prompt"))
    _mark_web_session(storage_manager, second_cwd, "second-1")
    storage_manager.storage.save(first_cwd, "empty", [])

    fresh_manager = WebSessionManager(projects_dir=tmp_path / "projects")
    sessions, total = fresh_manager.list_project_sessions(first_cwd, limit=2)

    assert [session.cwd for session in sessions] == [first_cwd, first_cwd]
    assert [session.title for session in sessions] == ["first prompt 2", "first prompt 1"]
    assert total == 3


def test_batch_reads_matches_unbatched_and_scans_once(tmp_path) -> None:
    projects_dir = tmp_path / "projects"
    seed = WebSessionManager(projects_dir=projects_dir)
    for cwd, sid in (("/proj-a", "a-1"), ("/proj-b", "b-1")):
        seed.storage.append(cwd, sid, Message(role="user", content="hello"))
        _mark_web_session(seed, cwd, sid)

    # Baseline: the same four homepage listings without batching.
    plain = WebSessionManager(projects_dir=projects_dir)
    plain_sessions, plain_total = plain.list_sessions_page(limit=10)
    plain_projects, plain_ptotal, plain_stotal = plain.list_session_projects(per_project_limit=5)
    plain_pinned_sessions = plain.list_pinned_sessions()
    plain_pinned_projects = plain.list_pinned_projects(per_project_limit=5)

    fresh = WebSessionManager(projects_dir=projects_dir)
    scans = {"count": 0}
    original_scan = fresh.index._scan_all_entries

    def counting_scan():
        scans["count"] += 1
        return original_scan()

    fresh.index._scan_all_entries = counting_scan  # type: ignore[method-assign]

    with fresh.batch_reads():
        batch_sessions, batch_total = fresh.list_sessions_page(limit=10)
        batch_projects, batch_ptotal, batch_stotal = fresh.list_session_projects(per_project_limit=5)
        batch_pinned_sessions = fresh.list_pinned_sessions()
        batch_pinned_projects = fresh.list_pinned_projects(per_project_limit=5)

    # All four listings triggered exactly one full-project scan between them.
    assert scans["count"] == 1
    # Results are identical to the unbatched path.
    assert {s.session_id for s in batch_sessions} == {s.session_id for s in plain_sessions}
    assert batch_total == plain_total
    assert {p["cwd"] for p in batch_projects} == {p["cwd"] for p in plain_projects}
    assert (batch_ptotal, batch_stotal) == (plain_ptotal, plain_stotal)
    assert {s.session_id for s in batch_pinned_sessions} == {s.session_id for s in plain_pinned_sessions}
    assert {p["cwd"] for p in batch_pinned_projects} == {p["cwd"] for p in plain_pinned_projects}


def test_batch_reads_caches_foreign_visibility_flags(tmp_path, monkeypatch) -> None:
    # 播种多个「外来」会话(无 web-session.json 侧车),使装配循环对每个条目都调用
    # _foreign_hidden → is_foreign_normal_visible。修复前:该开关会被每会话、每趟装配
    # 重复读取 settings.yml(4526 次 YAML 解析是首屏 ~4s 卡顿主因);修复后:一次请求
    # (batch_reads 窗口)内至多读取一次。
    projects_dir = tmp_path / "projects"
    seed = WebSessionManager(projects_dir=projects_dir)
    for cwd, sid in (("/proj-a", "a-1"), ("/proj-b", "b-1"), ("/proj-c", "c-1")):
        seed.storage.append(cwd, sid, Message(role="user", content="hello"))

    import iac_code.web.session_manager as sm

    normal_calls = {"count": 0}
    original_normal = sm.is_foreign_normal_visible

    def counting_normal():
        normal_calls["count"] += 1
        return original_normal()

    monkeypatch.setattr(sm, "is_foreign_normal_visible", counting_normal)

    fresh = WebSessionManager(projects_dir=projects_dir)
    with fresh.batch_reads():
        fresh.list_sessions_page(limit=10)
        fresh.list_session_projects(per_project_limit=5)
        fresh.list_pinned_sessions()
        fresh.list_pinned_projects(per_project_limit=5)

    # 一次请求窗口内,外来普通可见性开关至多读取一次(而非每会话每趟)。
    assert normal_calls["count"] <= 1


def test_foreign_pipeline_stays_read_only_after_web_sidecar_updates(tmp_path) -> None:
    cwd = str(tmp_path / "project")
    seed = WebSessionManager(projects_dir=tmp_path / "projects")
    seed.storage.append(cwd, "foreign-pipeline", Message(role="user", content="pipeline prompt"))
    replay_path = seed.storage.session_dir(cwd, "foreign-pipeline") / "pipeline" / "display.jsonl"
    replay_path.parent.mkdir(parents=True)
    replay_path.write_text("{}\n", encoding="utf-8")

    manager = WebSessionManager(projects_dir=tmp_path / "projects")
    entry = next(entry for entry in manager.index.list_all_projects() if entry.session_id == "foreign-pipeline")
    session = manager._from_entry(entry)
    assert manager.is_session_read_only(session) is True

    session.pinned = True
    manager.persist_web_metadata(session)

    assert _read_web_session_metadata(manager.storage, cwd, session.session_id)["origin"] == "foreign"
    fresh = WebSessionManager(projects_dir=tmp_path / "projects")
    restored_entry = next(entry for entry in fresh.index.list_all_projects() if entry.session_id == session.session_id)
    restored = fresh._from_entry(restored_entry)
    assert restored.pinned is True
    assert fresh.is_session_read_only(restored) is True


def test_legacy_web_sidecar_without_origin_remains_writable(tmp_path) -> None:
    cwd = str(tmp_path / "project")
    seed = WebSessionManager(projects_dir=tmp_path / "projects")
    seed.storage.append(cwd, "legacy-web", Message(role="user", content="prompt"))
    _mark_web_session(seed, cwd, "legacy-web")

    fresh = WebSessionManager(projects_dir=tmp_path / "projects")
    entry = next(entry for entry in fresh.index.list_all_projects() if entry.session_id == "legacy-web")
    session = fresh._from_entry(entry)

    assert fresh.is_session_read_only(session) is False


def test_project_metadata_reads_legacy_long_path_candidate_and_migrates_on_write(tmp_path) -> None:
    projects_dir = tmp_path / "projects"
    projects_dir.mkdir()
    cwd = "C:\\{}".format("long-project-" * 18)
    current_project_dir, legacy_project_dir = project_paths.project_dir_candidates(cwd, projects_dir)
    current_project_dir.mkdir()
    legacy_project_dir.mkdir()
    (legacy_project_dir / "web-project.json").write_text(
        json.dumps(
            {
                "schemaVersion": 1,
                "pinned": True,
                "pinnedAt": "2026-07-14T12:00:00Z",
                "collapsed": True,
                "label": "Legacy project",
            }
        ),
        encoding="utf-8",
    )
    manager = WebSessionManager(projects_dir=projects_dir)

    assert manager.read_project_metadata(cwd) == {
        "pinned": True,
        "pinnedAt": "2026-07-14T12:00:00Z",
        "archived": False,
        "hidden": False,
        "collapsed": True,
        "label": "Legacy project",
    }

    updated = manager.update_project_metadata(cwd, collapsed=False)

    assert updated["pinned"] is True
    assert updated["label"] == "Legacy project"
    migrated = json.loads((current_project_dir / "web-project.json").read_text(encoding="utf-8"))
    assert migrated["collapsed"] is False
    assert migrated["pinned"] is True
    assert migrated["label"] == "Legacy project"


def test_project_metadata_write_does_not_follow_legacy_temp_symlink(tmp_path) -> None:
    manager = WebSessionManager(projects_dir=tmp_path / "projects")
    cwd = str(tmp_path / "workspace")
    metadata_path = manager.storage.project_dir(cwd) / "web-project.json"
    metadata_path.parent.mkdir(parents=True, exist_ok=True)
    outside = tmp_path / "outside-project.json"
    outside.write_text("do not overwrite", encoding="utf-8")
    metadata_path.with_suffix(".json.tmp").symlink_to(outside)

    updated = manager.update_project_metadata(cwd, collapsed=True)

    assert updated["collapsed"] is True
    assert outside.read_text(encoding="utf-8") == "do not overwrite"
    assert metadata_path.is_symlink() is False
    assert json.loads(metadata_path.read_text(encoding="utf-8"))["collapsed"] is True


def test_web_session_metadata_write_does_not_follow_legacy_temp_symlink(tmp_path) -> None:
    manager = WebSessionManager(projects_dir=tmp_path / "projects")
    session = manager.create_session(cwd=str(tmp_path / "workspace"), session_id="safe-metadata")
    metadata_path = manager.storage.session_dir(session.cwd, session.session_id) / "web-session.json"
    outside = tmp_path / "outside-session.json"
    outside.write_text("do not overwrite", encoding="utf-8")
    metadata_path.with_suffix(".json.tmp").symlink_to(outside)

    session.unread = True
    manager.persist_web_metadata(session)

    assert outside.read_text(encoding="utf-8") == "do not overwrite"
    assert metadata_path.is_symlink() is False
    assert json.loads(metadata_path.read_text(encoding="utf-8"))["unread"] is True


def test_pinned_long_path_project_alias_is_not_listed_as_a_second_empty_project(tmp_path) -> None:
    projects_dir = tmp_path / "projects"
    cwd = "/" + "long-project/" * 24
    seed = WebSessionManager(projects_dir=projects_dir)
    seed.storage.append(cwd, "long-session", Message(role="user", content="prompt"))
    _mark_web_session(seed, cwd, "long-session")
    seed.update_project_metadata(cwd, pinned=True)

    fresh = WebSessionManager(projects_dir=projects_dir)
    active, _total_projects, _total_sessions = fresh.list_session_projects(include_empty=True)
    pinned = fresh.list_pinned_projects()

    assert active == []
    assert [project["cwd"] for project in pinned] == [cwd]


def test_list_session_projects_hides_projects_without_visible_sessions(tmp_path) -> None:
    visible_cwd = str(tmp_path / "visible-project")
    empty_cwd = str(tmp_path / "empty-project")
    storage_manager = WebSessionManager(projects_dir=tmp_path / "projects")
    storage_manager.storage.append(visible_cwd, "visible-1", Message(role="user", content="visible prompt"))
    _mark_web_session(storage_manager, visible_cwd, "visible-1")
    storage_manager.create_session(cwd=empty_cwd, session_id="empty-1")

    fresh_manager = WebSessionManager(projects_dir=tmp_path / "projects")
    projects, project_total, total_sessions = fresh_manager.list_session_projects(per_project_limit=5)

    by_cwd = {project["cwd"]: project for project in projects}
    # 只有带可见会话的项目进入侧栏;无可见会话的空项目(total==0)被隐藏。
    assert project_total == 1
    assert total_sessions == 1
    assert by_cwd[visible_cwd]["total"] == 1
    assert [session.title for session in by_cwd[visible_cwd]["sessions"]] == ["visible prompt"]
    assert empty_cwd not in by_cwd


def test_list_session_projects_hides_empty_project_directories(tmp_path) -> None:
    projects_dir = tmp_path / "projects"
    visible_cwd = str(tmp_path / "visible-project")
    storage_manager = WebSessionManager(projects_dir=projects_dir)
    storage_manager.storage.append(visible_cwd, "visible-1", Message(role="user", content="visible prompt"))
    _mark_web_session(storage_manager, visible_cwd, "visible-1")
    (projects_dir / "-Users-ehzyo-repo-empty-project").mkdir()

    fresh_manager = WebSessionManager(projects_dir=projects_dir)
    projects, project_total, total_sessions = fresh_manager.list_session_projects(per_project_limit=5)

    by_cwd = {project["cwd"]: project for project in projects}
    # 从未有会话的真·空目录不显示。
    assert project_total == 1
    assert total_sessions == 1
    assert "-Users-ehzyo-repo-empty-project" not in by_cwd


def test_empty_project_directories_are_hidden_even_when_recent(tmp_path) -> None:
    projects_dir = tmp_path / "projects"
    old_cwd = str(tmp_path / "old-visible-project")
    storage_manager = WebSessionManager(projects_dir=projects_dir)
    storage_manager.storage.append(old_cwd, "old-visible-1", Message(role="user", content="old visible prompt"))
    _mark_web_session(storage_manager, old_cwd, "old-visible-1")
    empty_project_dir = projects_dir / "-Users-ehzyo-repo-recent-empty-project"
    empty_project_dir.mkdir()

    old_timestamp = datetime(2026, 6, 20, 7, 30, tzinfo=timezone.utc).timestamp()
    recent_timestamp = datetime(2026, 6, 21, 7, 30, tzinfo=timezone.utc).timestamp()
    os.utime(storage_manager.storage.session_path(old_cwd, "old-visible-1"), (old_timestamp, old_timestamp))
    os.utime(empty_project_dir, (recent_timestamp, recent_timestamp))

    fresh_manager = WebSessionManager(projects_dir=projects_dir)
    projects, project_total, total_sessions = fresh_manager.list_session_projects(per_project_limit=5)

    # 即便空目录 mtime 更新,也不因排序而出现——空项目一律隐藏。
    assert project_total == 1
    assert total_sessions == 1
    assert [project["cwd"] for project in projects] == [old_cwd]


def test_indexed_session_without_metadata_uses_file_mtime_for_updated_time(tmp_path) -> None:
    cwd = str(tmp_path / "project")
    storage_manager = WebSessionManager(projects_dir=tmp_path / "projects")
    storage_manager.storage.append(cwd, "old-session", Message(role="user", content="old prompt"))
    old_timestamp = datetime(2026, 6, 20, 7, 30, tzinfo=timezone.utc).timestamp()
    os.utime(storage_manager.storage.session_path(cwd, "old-session"), (old_timestamp, old_timestamp))
    _mark_web_session(storage_manager, cwd, "old-session")
    (storage_manager.storage.session_dir(cwd, "old-session") / SESSION_METADATA_FILENAME).unlink()

    fresh_manager = WebSessionManager(projects_dir=tmp_path / "projects")
    sessions, total = fresh_manager.list_project_sessions(cwd, limit=1)

    expected = datetime.fromtimestamp(old_timestamp, timezone.utc).isoformat().replace("+00:00", "Z")
    assert total == 1
    assert sessions[0].title == "old prompt"
    assert sessions[0].created_at == expected
    assert sessions[0].updated_at == expected


def test_touch_session_activity_bumps_and_persists_updated_at(tmp_path) -> None:
    import json as _json

    from iac_code.web.session_manager import _web_session_metadata_path

    cwd = str(tmp_path / "project")
    manager = WebSessionManager(projects_dir=tmp_path / "projects")
    session = manager.create_session(cwd=cwd, session_id="abcdef123456")
    stale = "2000-01-01T00:00:00Z"
    session.updated_at = stale

    manager.touch_session_activity(session)

    # 内存中的「上一次操作」被刷新——这正是侧边栏实时显示读取的字段。
    assert session.updated_at != stale
    # 持久化忠实写入同一时间(不再另取 now 造成漂移)。
    path = _web_session_metadata_path(manager.storage, session.cwd, session.session_id)
    persisted = _json.loads(path.read_text(encoding="utf-8"))
    assert persisted["updatedAt"] == session.updated_at


def test_get_session_finds_unique_prefix_from_index(tmp_path) -> None:
    cwd = str(tmp_path / "project")
    manager = WebSessionManager(projects_dir=tmp_path / "projects")
    created = manager.create_session(cwd=cwd, session_id="abcdef123456")

    fresh_manager = WebSessionManager(projects_dir=tmp_path / "projects")
    found = fresh_manager.get_session("abcdef")

    assert found is not None
    assert found.session_id == created.session_id
    assert found.cwd == cwd


def test_get_session_caches_indexed_session(tmp_path) -> None:
    cwd = str(tmp_path / "project")
    manager = WebSessionManager(projects_dir=tmp_path / "projects")
    created = manager.create_session(cwd=cwd, session_id="abcdef123456")

    fresh_manager = WebSessionManager(projects_dir=tmp_path / "projects")
    found = fresh_manager.get_session(created.session_id)
    assert found is not None
    found.draft = "keep this in memory"

    found_again = fresh_manager.get_session(created.session_id)

    assert found_again is found
    assert found_again.draft == "keep this in memory"


def test_web_session_id_distinguishes_duplicate_session_ids_across_projects(tmp_path) -> None:
    first_cwd = str(tmp_path / "project-a")
    second_cwd = str(tmp_path / "project-b")
    manager = WebSessionManager(projects_dir=tmp_path / "projects")
    first = manager.create_session(cwd=first_cwd, session_id="same-id")
    second = manager.create_session(cwd=second_cwd, session_id="same-id")

    listed = sorted(manager.list_sessions(), key=lambda session: session.cwd)

    assert first.session_id == "same-id"
    assert second.session_id == "same-id"
    assert first.web_session_id != second.web_session_id
    assert "/" not in first.web_session_id
    assert "\\" not in first.web_session_id
    assert [session.to_dict()["sessionId"] for session in listed] == ["same-id", "same-id"]
    assert [session.to_dict()["webSessionId"] for session in listed] == [
        first.web_session_id,
        second.web_session_id,
    ]
    assert manager.get_session(first.web_session_id) is first
    assert manager.get_session(second.web_session_id) is second
    assert manager.get_session("same-id") is None


def test_web_session_id_resolves_indexed_duplicate_sessions_exactly(tmp_path) -> None:
    first_cwd = str(tmp_path / "project-a")
    second_cwd = str(tmp_path / "project-b")
    manager = WebSessionManager(projects_dir=tmp_path / "projects")
    first_ref = manager.create_session(cwd=first_cwd, session_id="same-id").web_session_id
    second_ref = manager.create_session(cwd=second_cwd, session_id="same-id").web_session_id

    fresh_manager = WebSessionManager(projects_dir=tmp_path / "projects")
    first = fresh_manager.get_session(first_ref)
    second = fresh_manager.get_session(second_ref)

    assert first is not None
    assert second is not None
    assert first.cwd == first_cwd
    assert second.cwd == second_cwd
    assert first.session_id == "same-id"
    assert second.session_id == "same-id"
    assert fresh_manager.get_session("same-id") is None


def test_create_session_rejects_invalid_mode(tmp_path) -> None:
    manager = WebSessionManager(projects_dir=tmp_path / "projects")

    with pytest.raises(ValueError, match="mode must be normal or pipeline"):
        manager.create_session(mode="chat")  # type: ignore[arg-type]


def test_create_session_resumes_existing_session(tmp_path) -> None:
    cwd = str(tmp_path / "project")
    manager = WebSessionManager(projects_dir=tmp_path / "projects")
    created = manager.create_session(cwd=cwd, session_id="session-1")

    resumed = manager.create_session(cwd=cwd, session_id="session-1")

    assert resumed.session_id == created.session_id
    assert manager.list_sessions()[0].session_id == "session-1"


def test_apply_pipeline_auto_title_sets_title_and_persists_sidecar(tmp_path) -> None:
    cwd = str(tmp_path / "project")
    manager = WebSessionManager(projects_dir=tmp_path / "projects")
    session = manager.create_session(cwd=cwd, mode="pipeline", pipeline_name="selling", session_id="pipe-1")
    assert session.title == "(empty)"

    changed = manager.apply_pipeline_auto_title(session, "帮我搭一条完整的售卖流水线")

    assert changed is True
    assert session.title == "帮我搭一条完整的售卖流水线"
    sidecar = _read_web_session_metadata(manager.storage, cwd, session.session_id)
    assert sidecar["autoTitle"] == "帮我搭一条完整的售卖流水线"


def test_apply_pipeline_auto_title_is_noop_when_title_already_set(tmp_path) -> None:
    cwd = str(tmp_path / "project")
    manager = WebSessionManager(projects_dir=tmp_path / "projects")
    session = manager.create_session(cwd=cwd, mode="pipeline", pipeline_name="selling", session_id="pipe-1")
    manager.apply_pipeline_auto_title(session, "第一条 prompt")

    changed = manager.apply_pipeline_auto_title(session, "后来的另一条 prompt")

    assert changed is False
    assert session.title == "第一条 prompt"


def test_apply_pipeline_auto_title_is_noop_for_blank_text(tmp_path) -> None:
    cwd = str(tmp_path / "project")
    manager = WebSessionManager(projects_dir=tmp_path / "projects")
    session = manager.create_session(cwd=cwd, mode="pipeline", pipeline_name="selling", session_id="pipe-1")

    changed = manager.apply_pipeline_auto_title(session, "   ")

    assert changed is False
    assert session.title == "(empty)"


def test_persist_web_metadata_writes_auto_title_field(tmp_path) -> None:
    cwd = str(tmp_path / "project")
    manager = WebSessionManager(projects_dir=tmp_path / "projects")
    session = manager.create_session(cwd=cwd, mode="pipeline", pipeline_name="selling", session_id="pipe-1")

    session.title = "已经有了标题"
    manager.persist_web_metadata(session)

    sidecar = _read_web_session_metadata(manager.storage, cwd, session.session_id)
    assert sidecar["autoTitle"] == "已经有了标题"


def test_persist_web_metadata_auto_title_is_none_when_empty(tmp_path) -> None:
    cwd = str(tmp_path / "project")
    manager = WebSessionManager(projects_dir=tmp_path / "projects")
    session = manager.create_session(cwd=cwd, mode="pipeline", pipeline_name="selling", session_id="pipe-1")

    manager.persist_web_metadata(session)

    sidecar = _read_web_session_metadata(manager.storage, cwd, session.session_id)
    assert sidecar["autoTitle"] is None


def test_to_dict_includes_unread_flag(tmp_path) -> None:
    manager = WebSessionManager(projects_dir=tmp_path / "projects")
    session = manager.create_session(cwd=str(tmp_path / "project"))

    assert session.to_dict()["unread"] is False
    session.unread = True
    assert session.to_dict()["unread"] is True


def test_persist_web_metadata_round_trips_unread(tmp_path) -> None:
    cwd = str(tmp_path / "project")
    manager = WebSessionManager(projects_dir=tmp_path / "projects")
    session = manager.create_session(cwd=cwd, session_id="abcdef123456")
    session.unread = True
    manager.persist_web_metadata(session)

    sidecar = _read_web_session_metadata(manager.storage, cwd, session.session_id)
    assert sidecar["unread"] is True

    fresh = WebSessionManager(projects_dir=tmp_path / "projects")
    reloaded = fresh.get_session("abcdef123456")
    assert reloaded is not None
    assert reloaded.unread is True


def test_mark_session_completed_sets_unread_and_persists_without_watchers(tmp_path) -> None:
    cwd = str(tmp_path / "project")
    manager = WebSessionManager(projects_dir=tmp_path / "projects")
    session = manager.create_session(cwd=cwd)

    changed = manager.mark_session_completed(session)

    assert changed is True
    assert session.unread is True
    sidecar = _read_web_session_metadata(manager.storage, cwd, session.session_id)
    assert sidecar["unread"] is True


def test_mark_session_completed_skips_when_someone_is_watching(tmp_path) -> None:
    manager = WebSessionManager(projects_dir=tmp_path / "projects")
    session = manager.create_session(cwd=str(tmp_path / "project"))

    async def scenario() -> bool:
        stream = session.events.stream_after(0)
        pending = asyncio.create_task(anext(stream))
        await asyncio.sleep(0)  # 进入生成器 body，订阅计数 +1。
        assert session.events.subscriber_count == 1
        changed = manager.mark_session_completed(session)
        pending.cancel()
        with suppress(asyncio.CancelledError):
            await pending
        await stream.aclose()
        return changed

    changed = asyncio.run(scenario())

    assert changed is False
    assert session.unread is False


def test_mark_session_completed_skips_archived_session(tmp_path) -> None:
    manager = WebSessionManager(projects_dir=tmp_path / "projects")
    session = manager.create_session(cwd=str(tmp_path / "project"))
    session.archived = True

    changed = manager.mark_session_completed(session)

    assert changed is False
    assert session.unread is False


def test_mark_session_viewed_clears_unread_and_persists(tmp_path) -> None:
    cwd = str(tmp_path / "project")
    manager = WebSessionManager(projects_dir=tmp_path / "projects")
    session = manager.create_session(cwd=cwd)
    session.unread = True

    changed = manager.mark_session_viewed(session)

    assert changed is True
    assert session.unread is False
    sidecar = _read_web_session_metadata(manager.storage, cwd, session.session_id)
    assert sidecar["unread"] is False


def test_mark_session_viewed_is_noop_when_already_read(tmp_path) -> None:
    manager = WebSessionManager(projects_dir=tmp_path / "projects")
    session = manager.create_session(cwd=str(tmp_path / "project"))

    assert manager.mark_session_viewed(session) is False


def test_mark_session_running_clears_unread_and_persists(tmp_path) -> None:
    cwd = str(tmp_path / "project")
    manager = WebSessionManager(projects_dir=tmp_path / "projects")
    session = manager.create_session(cwd=cwd)
    session.unread = True

    changed = manager.mark_session_running(session)

    assert changed is True
    assert session.unread is False
    sidecar = _read_web_session_metadata(manager.storage, cwd, session.session_id)
    assert sidecar["unread"] is False


def test_mark_session_running_is_noop_when_already_read(tmp_path) -> None:
    manager = WebSessionManager(projects_dir=tmp_path / "projects")
    session = manager.create_session(cwd=str(tmp_path / "project"))

    assert manager.mark_session_running(session) is False


def test_running_session_is_never_unread_in_summary(tmp_path) -> None:
    # 回归:运行中的会话不得同时为未读。上一轮无人观看结束后 unread=True,
    # 新一轮开始须清未读,否则侧栏列表快照会把未读圆点画在进行中的会话上。
    manager = WebSessionManager(projects_dir=tmp_path / "projects")
    session = manager.create_session(cwd=str(tmp_path / "project"))
    manager.mark_session_completed(session)
    assert session.unread is True

    async def scenario() -> dict:
        async with session.turn_lock:
            session.active_turn_task = asyncio.current_task()
            manager.mark_session_running(session)
            return session.to_dict()

    summary = asyncio.run(scenario())

    assert summary["currentTurnActive"] is True
    assert summary["unread"] is False


def test_from_entry_falls_back_to_sidecar_auto_title_when_index_empty(tmp_path) -> None:
    cwd = str(tmp_path / "project")
    manager = WebSessionManager(projects_dir=tmp_path / "projects")
    session = manager.create_session(cwd=cwd, mode="pipeline", pipeline_name="selling", session_id="pipe-1")
    manager.apply_pipeline_auto_title(session, "售卖流水线的首条 prompt")

    fresh_manager = WebSessionManager(projects_dir=tmp_path / "projects")
    entry = next(entry for entry in fresh_manager.index.list_all_projects() if entry.session_id == session.session_id)
    assert entry.title in ("", "(empty)")

    rehydrated = fresh_manager._from_entry(entry)

    assert rehydrated.title == "售卖流水线的首条 prompt"
    assert rehydrated.mode == "pipeline"


def test_from_entry_restores_session_model_and_sidecar_updated_at(tmp_path) -> None:
    cwd = str(tmp_path / "project")
    manager = WebSessionManager(projects_dir=tmp_path / "projects")
    session = manager.create_session(
        cwd=cwd,
        mode="normal",
        session_id="normal-model-1",
        provider="openai",
        model="gpt-5",
        effort="high",
    )
    session.updated_at = "2099-01-02T03:04:05Z"
    manager.persist_web_metadata(session)

    fresh_manager = WebSessionManager(projects_dir=tmp_path / "projects")
    entry = next(entry for entry in fresh_manager.index.list_all_projects() if entry.session_id == session.session_id)
    rehydrated = fresh_manager._from_entry(entry)

    assert rehydrated.provider == "openai"
    assert rehydrated.model == "gpt-5"
    assert rehydrated.effort == "high"
    assert rehydrated.updated_at == "2099-01-02T03:04:05Z"


def test_from_entry_uses_index_title_when_no_sidecar_auto_title(tmp_path) -> None:
    cwd = str(tmp_path / "project")
    manager = WebSessionManager(projects_dir=tmp_path / "projects")
    session = manager.create_session(cwd=cwd, mode="normal", session_id="normal-1")
    # 创建即写入 sidecar(autoTitle=None);首条 prompt 落到 JSONL 后由 index 派生。
    manager.storage.append(cwd, session.session_id, Message(role="user", content="真正的 JSONL prompt"))

    fresh_manager = WebSessionManager(projects_dir=tmp_path / "projects")
    entry = next(entry for entry in fresh_manager.index.list_all_projects() if entry.session_id == session.session_id)

    rehydrated = fresh_manager._from_entry(entry)

    assert rehydrated.title == "真正的 JSONL prompt"


def test_from_entry_prefers_frozen_sidecar_title_over_polluted_index(tmp_path) -> None:
    # Bug 2:标题应在会话开始就冻结、不受压缩影响。压缩会重写 JSONL,index 现扫的
    # auto_title 可能变成保留尾部里的另一条提问(甚至 [Conversation Summary]);此时应
    # 以 sidecar 里冻结的 autoTitle 为准,而非 index 现扫值。
    cwd = str(tmp_path / "project")
    manager = WebSessionManager(projects_dir=tmp_path / "projects")
    session = manager.create_session(cwd=cwd, mode="normal", session_id="normal-1")
    # sidecar 冻结会话开始时的标题。
    session.title = "会话开始冻结的标题"
    manager.persist_web_metadata(session)
    # JSONL 首条提问(模拟压缩后保留尾部现扫出的另一条提问)与冻结标题不同。
    manager.storage.append(cwd, session.session_id, Message(role="user", content="压缩后现扫的另一条提问"))

    fresh_manager = WebSessionManager(projects_dir=tmp_path / "projects")
    entry = next(entry for entry in fresh_manager.index.list_all_projects() if entry.session_id == session.session_id)
    assert entry.auto_title == "压缩后现扫的另一条提问"

    rehydrated = fresh_manager._from_entry(entry)

    assert rehydrated.title == "会话开始冻结的标题"


def test_from_entry_prefers_explicit_rename_over_frozen_sidecar(tmp_path) -> None:
    # 显式重命名(目录元数据 name)优先级最高,压过 sidecar 冻结标题与 index 派生标题。
    cwd = str(tmp_path / "project")
    manager = WebSessionManager(projects_dir=tmp_path / "projects")
    session = manager.create_session(cwd=cwd, mode="normal", session_id="normal-1")
    manager.storage.append(cwd, session.session_id, Message(role="user", content="首条 prompt"))
    session.title = "sidecar 冻结标题"
    manager.persist_web_metadata(session)
    manager.rename_session(session, "user-renamed")

    fresh_manager = WebSessionManager(projects_dir=tmp_path / "projects")
    entry = next(entry for entry in fresh_manager.index.list_all_projects() if entry.session_id == session.session_id)

    rehydrated = fresh_manager._from_entry(entry)

    assert rehydrated.title == "user-renamed"


def test_from_entry_existing_keeps_frozen_title_against_polluted_index(tmp_path) -> None:
    # 内存中已冻结的标题不应被 index 现扫的 auto_title 覆盖(压缩重扫时的核心防护)。
    cwd = str(tmp_path / "project")
    manager = WebSessionManager(projects_dir=tmp_path / "projects")
    session = manager.create_session(cwd=cwd, mode="normal", session_id="normal-1")
    session.title = "会话开始冻结的标题"
    # index 现扫出与冻结标题不同的 auto_title(模拟压缩后重写)。
    manager.storage.append(cwd, session.session_id, Message(role="user", content="压缩后现扫的另一条提问"))

    entry = next(entry for entry in manager.index.list_all_projects() if entry.session_id == session.session_id)
    assert entry.auto_title == "压缩后现扫的另一条提问"

    rehydrated = manager._from_entry(entry)

    assert rehydrated is session
    assert rehydrated.title == "会话开始冻结的标题"


def test_from_entry_existing_adopts_index_title_when_memory_title_empty(tmp_path) -> None:
    # 内存标题仍为空时,首次从 index 捕获实时首条提问(此时尚未被压缩污染)。
    cwd = str(tmp_path / "project")
    manager = WebSessionManager(projects_dir=tmp_path / "projects")
    session = manager.create_session(cwd=cwd, mode="normal", session_id="normal-1")
    assert session.title == "(empty)"
    manager.storage.append(cwd, session.session_id, Message(role="user", content="首条真实提问"))

    entry = next(entry for entry in manager.index.list_all_projects() if entry.session_id == session.session_id)

    rehydrated = manager._from_entry(entry)

    assert rehydrated is session
    assert rehydrated.title == "首条真实提问"


def test_from_entry_existing_adopts_explicit_rename(tmp_path) -> None:
    # 内存已有冻结标题,但用户显式重命名后 index 重扫应把内存标题更新为新名字。
    cwd = str(tmp_path / "project")
    manager = WebSessionManager(projects_dir=tmp_path / "projects")
    session = manager.create_session(cwd=cwd, mode="normal", session_id="normal-1")
    session.title = "旧的冻结标题"
    from iac_code.services.session_metadata import SessionMetadata, write_session_metadata

    write_session_metadata(
        manager.storage.session_dir(cwd, session.session_id),
        SessionMetadata(session_id=session.session_id, cwd=cwd, name="user-renamed"),
    )

    entry = next(entry for entry in manager.index.list_all_projects() if entry.session_id == session.session_id)
    assert entry.name == "user-renamed"

    rehydrated = manager._from_entry(entry)

    assert rehydrated is session
    assert rehydrated.title == "user-renamed"


def test_from_entry_does_not_overwrite_live_title_with_empty_index(tmp_path) -> None:
    cwd = str(tmp_path / "project")
    manager = WebSessionManager(projects_dir=tmp_path / "projects")
    session = manager.create_session(cwd=cwd, mode="pipeline", pipeline_name="selling", session_id="pipe-1")
    manager.apply_pipeline_auto_title(session, "实时设置的流水线标题")

    entry = next(entry for entry in manager.index.list_all_projects() if entry.session_id == session.session_id)
    assert entry.title in ("", "(empty)")

    rehydrated = manager._from_entry(entry)

    assert rehydrated is session
    assert rehydrated.title == "实时设置的流水线标题"


def test_load_visible_transcript_rebuilds_pipeline_from_a2a_journal(tmp_path, monkeypatch) -> None:
    # A2A 持久化根位于 config_dir/a2a;隔离到 tmp 避免读到本机真实配置。
    monkeypatch.setenv("IAC_CODE_CONFIG_DIR", str(tmp_path / "config"))

    from iac_code.a2a.persistence import A2AContextSnapshot, A2APersistenceStore
    from iac_code.a2a.pipeline_journal import A2APipelineJournal
    from iac_code.a2a.pipeline_paths import a2a_pipeline_dir_for_session

    cwd = str(tmp_path / "project")
    manager = WebSessionManager(projects_dir=tmp_path / "projects")
    web_session = manager.create_session(cwd=cwd, mode="pipeline", pipeline_name="selling", session_id="web-pipe-1")
    web_session.context_id = "ctx-reload-1"
    web_session.task_id = "task-reload-1"
    manager.persist_web_metadata(web_session)

    # 流水线在独立的 A2A pipeline 会话里运行;把其 session_id/cwd 记进 context 快照。
    pipeline_session_id = "a2a-pipe-1"
    A2APersistenceStore(tmp_path / "config" / "a2a").save_context(
        A2AContextSnapshot(context_id="ctx-reload-1", session_id=pipeline_session_id, cwd=cwd)
    )

    journal = A2APipelineJournal(a2a_pipeline_dir_for_session(cwd=cwd, session_id=pipeline_session_id))
    journal.append_many(
        [
            {"eventType": "pipeline_started", "scope": "pipeline", "sequence": 1, "data": {"totalSteps": 2}},
            {
                "eventType": "step_started",
                "scope": "step",
                "sequence": 2,
                "step": {"id": "intent_parsing", "runId": "step-1", "index": 1, "total": 2},
            },
            {
                "eventType": "thinking_delta",
                "scope": "step",
                "sequence": 3,
                "step": {"id": "intent_parsing", "runId": "step-1", "index": 1, "total": 2},
                "data": {"type": "raw_thinking", "text": "先想清楚需求"},
            },
            {
                "eventType": "text_delta",
                "scope": "step",
                "sequence": 4,
                "step": {"id": "intent_parsing", "runId": "step-1", "index": 1, "total": 2},
                "data": {"text": "解析需求"},
            },
            {
                "eventType": "step_completed",
                "scope": "step",
                "sequence": 5,
                "step": {"id": "intent_parsing", "runId": "step-1", "index": 1, "total": 2},
                "data": {"durationS": 1.0},
            },
        ]
    )

    transcript = manager.load_visible_transcript("web-pipe-1", cwd=cwd)
    messages = transcript["messages"]

    step_markers = [msg for msg in messages if msg.get("kind") == "pipeline_step"]
    assert step_markers, "reload should rebuild pipeline step markers from the A2A journal"
    assert step_markers[0]["pipelineStep"]["status"] == "completed"
    assert step_markers[0]["pipelineStep"]["depth"] == 0
    assert any("解析需求" in (msg.get("content") or "") for msg in messages)
    # 重载后思考内容随消息回来,前端据此渲染「思考完成」(与实时/普通模式一致)。
    assert any("先想清楚需求" in (msg.get("thinking") or "") for msg in messages)


def test_collect_project_groups_includes_pipeline_session_via_sidecar_title(tmp_path) -> None:
    cwd = str(tmp_path / "project")
    manager = WebSessionManager(projects_dir=tmp_path / "projects")
    session = manager.create_session(cwd=cwd, mode="pipeline", pipeline_name="selling", session_id="pipe-1")
    manager.apply_pipeline_auto_title(session, "侧栏应显示的流水线标题")

    fresh_manager = WebSessionManager(projects_dir=tmp_path / "projects")
    projects, _project_total, total_sessions = fresh_manager.list_session_projects()

    by_cwd = {project["cwd"]: project for project in projects}
    assert total_sessions == 1
    assert by_cwd[cwd]["total"] == 1
    assert [session.title for session in by_cwd[cwd]["sessions"]] == ["侧栏应显示的流水线标题"]


def test_persist_pipeline_user_prompt_tags_normal_chat(tmp_path) -> None:
    # Issue 5/7: 流水线回合的 prompt 补写进 web JSONL 时带 ``source=pipeline``;交接给普通
    # 对话后的 prompt 额外带 ``normalChat``,供恢复时定位「↪ 普通对话」分隔。
    cwd = str(tmp_path / "project")
    manager = WebSessionManager(projects_dir=tmp_path / "projects")
    session = manager.create_session(cwd=cwd, mode="pipeline", session_id="pipe-prompt-1")

    manager.persist_pipeline_user_prompt(session, "流水线回合 prompt")
    manager.persist_pipeline_user_prompt(session, "交接后普通对话 prompt", normal_chat=True)
    manager.persist_pipeline_user_prompt(session, "   ")  # 空白 prompt 不落盘

    stored = manager.load_resume_messages(session.session_id, cwd=cwd)
    prompts = [msg for msg in stored if msg.role == "user"]
    assert [msg.get_text() for msg in prompts] == ["流水线回合 prompt", "交接后普通对话 prompt"]
    assert prompts[0].metadata == {"source": "pipeline"}
    assert prompts[1].metadata.get("source") == "pipeline"
    assert prompts[1].metadata.get("normalChat") is True


def test_persist_pipeline_user_prompt_records_turn_id(tmp_path) -> None:
    # Issue 7c/d: prompt 落盘时带 turnId,恢复路径据此赋与实时 user.message 相同的
    # ``user-<turnId>`` 稳定键,让中途 reload 时磁盘行与被回放的实时事件按同一键去重。
    from iac_code.web.session_manager import _persisted_message_stable_id

    cwd = str(tmp_path / "project")
    manager = WebSessionManager(projects_dir=tmp_path / "projects")
    session = manager.create_session(cwd=cwd, mode="pipeline", session_id="pipe-turn-1")

    manager.persist_pipeline_user_prompt(session, "带 turnId 的 prompt", turn_id="turn-xyz")
    manager.persist_pipeline_user_prompt(session, "无 turnId 的 prompt")

    stored = [msg for msg in manager.load_resume_messages(session.session_id, cwd=cwd) if msg.role == "user"]
    assert stored[0].metadata.get("turnId") == "turn-xyz"
    assert "turnId" not in stored[1].metadata

    # 稳定键助手:有 turnId → user-<turnId>;无 turnId 的旧行 → None(沿用 stored-N 定位)。
    assert _persisted_message_stable_id(stored[0]) == "user-turn-xyz"
    assert _persisted_message_stable_id(stored[1]) is None


def test_persist_pipeline_attachment_only_prompt_survives_visible_reload(tmp_path) -> None:
    cwd = str(tmp_path / "project")
    manager = WebSessionManager(projects_dir=tmp_path / "projects")
    session = manager.create_session(cwd=cwd, mode="pipeline", session_id="pipe-attachments")

    manager.persist_pipeline_user_prompt(
        session,
        "",
        turn_id="turn-attachments",
        image_ids=["image-1"],
        file_refs=["template.yaml"],
    )

    stored = [
        message for message in manager.load_resume_messages(session.session_id, cwd=cwd) if message.role == "user"
    ]
    assert len(stored) == 1
    assert stored[0].metadata["imageIds"] == ["image-1"]
    assert stored[0].metadata["fileRefs"] == ["template.yaml"]

    visible = manager.load_visible_messages(session.session_id, cwd=cwd)
    assert visible == [
        {
            "messageId": "user-turn-attachments",
            "role": "user",
            "content": "",
            "text": "",
            "thinking": "",
            "toolUseIds": [],
            "blocks": [{"type": "text", "text": ""}],
            "stored": True,
            "status": "completed",
            "sequence": 1,
            "imageIds": ["image-1"],
            "fileRefs": ["template.yaml"],
        }
    ]


def test_switch_session_to_normal_after_handoff_flips_mode_and_keeps_routing(tmp_path) -> None:
    # Issue 4: 交接给普通对话后把模式翻转为 normal(否则后续输入继续走流水线路径被忽略);
    # contextId/taskId 有意保留,reload 仍能据 sidecar 重建整段流水线转录。
    cwd = str(tmp_path / "project")
    manager = WebSessionManager(projects_dir=tmp_path / "projects")
    session = manager.create_session(cwd=cwd, mode="pipeline", session_id="pipe-handoff-1")
    session.context_id = "ctx-1"
    session.task_id = "task-1"

    assert manager.switch_session_to_normal_after_handoff(session) is True
    assert session.mode == "normal"
    assert session.context_id == "ctx-1"
    assert session.task_id == "task-1"

    # sidecar 已持久化模式翻转,但 contextId 仍在,供 reload 的 is_pipeline_session 判定。
    sidecar = _read_web_session_metadata(manager.storage, cwd, session.session_id)
    assert sidecar.get("mode") == "normal"
    assert sidecar.get("contextId") == "ctx-1"

    # 已是 normal 的会话再调用应为 no-op,返回 False。
    assert manager.switch_session_to_normal_after_handoff(session) is False


def test_persist_pipeline_handoff_context_feeds_resume_but_hidden_in_transcript(tmp_path) -> None:
    # 交接给普通对话时,引擎生成的交接摘要须落入 web JSONL:普通回合 load_resume_messages 读到它,
    # LLM 才知道流水线创建了什么(否则问「你刚才创建了什么」答什么都没创建);但它不能渲染成用户
    # 气泡,须被可见转录过滤。
    cwd = str(tmp_path / "project")
    manager = WebSessionManager(projects_dir=tmp_path / "projects")
    session = manager.create_session(cwd=cwd, mode="pipeline", session_id="pipe-handoff-ctx-1")
    session.context_id = "ctx-1"
    session.task_id = "task-1"

    summary = "[Pipeline Handoff Context]\nPipeline: selling\nOutcome: completed\n创建了 VPC 与 ECS。"
    assert manager.persist_pipeline_handoff_context(session, summary) is True
    # 幂等:同内容再注入应为 no-op。
    assert manager.persist_pipeline_handoff_context(session, summary) is False
    # 空/空白摘要不落盘。
    assert manager.persist_pipeline_handoff_context(session, "   ") is False
    assert manager.persist_pipeline_handoff_context(session, None) is False

    # resume 上下文(喂给 LLM)必须包含交接摘要。
    resume = manager.load_resume_messages(session.session_id, cwd=cwd)
    assert any(msg.role == "user" and msg.get_text() == summary for msg in resume)
    assert sum(1 for msg in resume if msg.get_text() == summary) == 1

    # 可见转录不得把交接摘要渲染成用户气泡。
    transcript = manager.load_visible_transcript(session.session_id, cwd=cwd)
    dumped = json.dumps(transcript, ensure_ascii=False)
    assert "[Pipeline Handoff Context]" not in dumped


def test_clear_session_model_resets_override_persists_and_emits_event(tmp_path) -> None:
    # 合作方源不在 PROVIDER_REGISTRY,无法作为会话级 provider 存储;切换到合作方源前须先清掉
    # 本会话的覆盖,否则 to_payload 里 `self.provider or runtime['provider']` 仍走旧的会话级 provider,
    # 全局合作方源无法对当前会话生效。此处验证清除会重置内存、落盘并广播 session.updated。
    cwd = str(tmp_path / "project")
    manager = WebSessionManager(projects_dir=tmp_path / "projects")
    session = manager.create_session(cwd=cwd, mode="normal", session_id="clear-model-1")
    # 直接写会话级覆盖(绕过 set_session_model 的 registry 校验)。
    session.provider = "openai"
    session.model = "gpt-5.5"
    session.effort = "high"
    manager.persist_web_metadata(session)

    result = manager.clear_session_model(session)

    assert result == {"provider": None, "model": None, "effort": None}
    assert session.provider is None
    assert session.model is None
    assert session.effort is None

    # 落盘:新 manager 重新读回时会话级覆盖已消失。
    fresh = WebSessionManager(projects_dir=tmp_path / "projects")
    reloaded = fresh.get_session(session.session_id)
    assert reloaded is not None
    assert reloaded.provider is None
    assert reloaded.model is None
    assert reloaded.effort is None

    # 广播 session.updated,前端据此把 provider/model 归位到全局(含合作方源)。
    updated = [event for event in session.events.replay_after(0) if event["type"] == "session.updated"]
    assert updated
    assert updated[-1]["payload"] == {"provider": None, "model": None, "effort": None}


def test_load_visible_transcript_folds_compaction_marker(tmp_path) -> None:
    # 内联压缩标记(role=user,metadata.type=compaction_summary)在普通模式 reload 时应折成
    # kind=context_compaction_boundary 的分隔条行,而不是渲染成巨型 user 摘要气泡;标记之上
    # 的完整历史仍须保留。
    cwd = str(tmp_path / "project")
    manager = WebSessionManager(projects_dir=tmp_path / "projects")
    session = manager.create_session(cwd=cwd, mode="normal")
    messages = [
        Message(role="user", content="测试一下ls"),
        Message(role="assistant", content="好的，执行 ls"),
        create_compaction_summary_message("这里是摘要正文"),
        Message(role="user", content="再 sleep 一下"),
        Message(role="assistant", content="done"),
    ]
    for message in messages:
        manager.storage.append(cwd, session.session_id, message)

    fresh = WebSessionManager(projects_dir=tmp_path / "projects")
    rows = fresh.load_visible_transcript(session.session_id, cwd=cwd)["messages"]

    boundary = [row for row in rows if row.get("kind") == "context_compaction_boundary"]
    assert len(boundary) == 1
    assert boundary[0]["role"] == "assistant"
    assert boundary[0]["content"] == "这里是摘要正文"  # 已去掉 [Conversation Summary]\n 前缀
    # 标记之上的历史仍在。
    assert any("测试一下ls" in str(row.get("content", "")) for row in rows)
    # 不出现巨型 user 摘要气泡。
    assert not any(
        row.get("role") == "user" and str(row.get("content", "")).startswith("[Conversation Summary]") for row in rows
    )


def _marker_with_tail(summary: str, tail: int):
    marker = create_compaction_summary_message(summary)
    marker.metadata[COMPACTION_SUMMARY_TAIL_METADATA_KEY] = tail
    return marker


def test_reorder_compaction_markers_sinks_past_tail() -> None:
    # 存储序:标记排在其保留尾部之前(u2/a2 是压缩时保留的最近一轮,tail=2)。
    u1 = Message(role="user", content="u1")
    a1 = Message(role="assistant", content="a1")
    marker = _marker_with_tail("SUM", 2)
    u2 = Message(role="user", content="u2")
    a2 = Message(role="assistant", content="a2")
    reordered = reorder_compaction_markers([u1, a1, marker, u2, a2])
    # 标记下沉到尾部之后 → 落到整段末尾(用户实际执行 /compact 的位置)。
    assert reordered == [u1, a1, u2, a2, marker]


def test_reorder_compaction_markers_legacy_zero_tail_stays_put() -> None:
    # 旧会话标记无 tail 字段(count=0),保持原位,向后兼容。
    u1 = Message(role="user", content="u1")
    marker = create_compaction_summary_message("SUM")  # 无 tail
    a1 = Message(role="assistant", content="a1")
    reordered = reorder_compaction_markers([u1, marker, a1])
    assert reordered == [u1, marker, a1]


def test_reorder_compaction_markers_clamps_tail_to_end() -> None:
    # tail 超出剩余长度时钳到末尾,不越界。
    u1 = Message(role="user", content="u1")
    marker = _marker_with_tail("SUM", 99)
    a1 = Message(role="assistant", content="a1")
    reordered = reorder_compaction_markers([u1, marker, a1])
    assert reordered == [u1, a1, marker]


def test_reorder_compaction_markers_keeps_marker_at_mid_turn_trigger_point() -> None:
    # 自动压缩可能在某个用户回合的工具循环「中途」触发(needs_compaction 每次 ReAct 迭代都查),
    # 此时保留尾部是「半个回合」:标记后只记了当时已有的 u2/a2/tr2(tail=3),压缩完成后同一回合
    # 又继续追加 a3/tr3/a4。reorder 只按 tail 把标记下沉到 index+tail=5,即压缩真实触发的那一刻
    # ——落在 u2 回合的工具循环正中间(tr2 与 a3 之间)。这正是它该在的位置,不额外吸附到回合边界。
    # 由此产生的「一个已处理组被分隔线切两半」由前端 renderCollapsedTurn 把分隔线折进同一组内解决,
    # 而不是在这里挪动标记。
    u1 = Message(role="user", content="u1")
    a1 = Message(role="assistant", content="a1")
    marker = _marker_with_tail("SUM", 3)
    u2 = Message(role="user", content="u2")
    a2 = Message(role="assistant", content="a2")
    tr2 = Message(role="user", content=[ToolResultBlock(tool_use_id="t2", content="r2")])
    a3 = Message(role="assistant", content="a3")
    tr3 = Message(role="user", content=[ToolResultBlock(tool_use_id="t3", content="r3")])
    a4 = Message(role="assistant", content="a4")

    reordered = reorder_compaction_markers([u1, a1, marker, u2, a2, tr2, a3, tr3, a4])

    # 标记停在 index+tail=5(tr2 与 a3 之间),回合中途——压缩真实触发点,未被吸附走。
    assert reordered == [u1, a1, u2, a2, tr2, marker, a3, tr3, a4]
    marker_pos = reordered.index(marker)
    assert reordered[marker_pos - 1] is tr2
    assert reordered[marker_pos + 1] is a3


def test_reorder_compaction_markers_last_marker_sinks_past_relocated_markers() -> None:
    # 多次压缩:靠前的标记 m_early 下沉时会跨过后一个标记 m_last 的尾部区间、落进其中(这里落到
    # r2 与 r3 之间)。m_last(最后一次压缩,通常即手动 /compact)的真实尾部是 r2/r3/r4 三条,应
    # 落到 r4(本回合最终回答)之后。若按裸偏移 index+tail 计,重定位进来的 m_early 会被 m_last
    # 误当作自己的一条尾部,使其少下沉一格——停在 r4 之前(回归前的症状:手动压缩标记被折进
    # 「已处理」而不显示在末尾)。修复后计数跳过已重排标记,m_last 稳稳落到 r4 之后。
    m_early = _marker_with_tail("EARLY", 3)
    r1 = Message(role="assistant", content="r1")
    m_last = _marker_with_tail("MANUAL_LAST", 3)
    r2 = Message(role="user", content="r2")
    r3 = Message(role="assistant", content="r3")
    r4 = Message(role="assistant", content="r4")  # 本回合最终回答

    reordered = reorder_compaction_markers([m_early, r1, m_last, r2, r3, r4])

    # m_last 是最后一次压缩,必须落到 r4(最终回答)之后——整段末尾。
    assert reordered[-1] is m_last
    assert reordered.index(r4) < reordered.index(m_last)
    # m_early 仍下沉到自己的尾部区间(不因把 m_last 计入而错位)。
    assert reordered.index(m_early) < reordered.index(m_last)


def test_load_visible_transcript_sinks_boundary_to_operation_point(tmp_path) -> None:
    # 手动 /compact 场景:两轮对话后执行 /compact,压缩标记在存储里排在保留尾部(第二轮)之前,
    # 但可视转录应把边界下沉到第二轮 llm 输出之后——即用户实际操作的位置(#40)。
    cwd = str(tmp_path / "project")
    manager = WebSessionManager(projects_dir=tmp_path / "projects")
    session = manager.create_session(cwd=cwd, mode="normal")
    messages = [
        Message(role="user", content="第一轮 ha-web 模板"),
        Message(role="assistant", content="ha-web 生成完成"),
        _marker_with_tail("摘要正文", 2),  # 存储序:标记在第二轮之前
        Message(role="user", content="第二轮 nginx 模板"),
        Message(role="assistant", content="nginx 生成完成"),
    ]
    for message in messages:
        manager.storage.append(cwd, session.session_id, message)

    fresh = WebSessionManager(projects_dir=tmp_path / "projects")
    rows = fresh.load_visible_transcript(session.session_id, cwd=cwd)["messages"]

    def _index(predicate):
        return next(i for i, row in enumerate(rows) if predicate(row))

    boundary_idx = _index(lambda r: r.get("kind") == "context_compaction_boundary")
    nginx_answer_idx = _index(lambda r: "nginx 生成完成" in str(r.get("content", "")))
    nginx_prompt_idx = _index(lambda r: "第二轮 nginx" in str(r.get("content", "")))
    # 边界落在第二轮 prompt 与其 llm 输出之后,而不是插在两轮之间。
    assert boundary_idx > nginx_answer_idx
    assert boundary_idx > nginx_prompt_idx
    assert len([r for r in rows if r.get("kind") == "context_compaction_boundary"]) == 1


def test_load_visible_transcript_folds_legacy_summary_without_metadata(tmp_path) -> None:
    # 旧会话的压缩标记只有文本前缀、没有 metadata,也须识别并折成分隔条行。
    cwd = str(tmp_path / "project")
    manager = WebSessionManager(projects_dir=tmp_path / "projects")
    session = manager.create_session(cwd=cwd, mode="normal")
    messages = [
        Message(role="user", content="hi"),
        Message.from_dict({"role": "user", "content": "[Conversation Summary]\n遗留摘要", "version": "0.7.0"}),
        Message(role="assistant", content="ok"),
    ]
    for message in messages:
        manager.storage.append(cwd, session.session_id, message)

    fresh = WebSessionManager(projects_dir=tmp_path / "projects")
    rows = fresh.load_visible_transcript(session.session_id, cwd=cwd)["messages"]

    boundary = [row for row in rows if row.get("kind") == "context_compaction_boundary"]
    assert len(boundary) == 1
    assert boundary[0]["role"] == "assistant"
    assert boundary[0]["content"] == "遗留摘要"


def test_to_dict_thinking_effective_dashscope_unset_is_on(tmp_path, monkeypatch) -> None:
    # 核心回归(用户报告的 qwen3.7-max 场景):override=None 且 provider 也未配置
    # thinkingEnabled 时,DashScope 家族默认 enable_thinking=True,故本回合真的会思考,
    # thinkingEffective 须为 True;thinkingEnabled 仍保持 None(三态不丢)。
    from iac_code.web import session_manager as sm

    manager = WebSessionManager(projects_dir=tmp_path / "projects")
    session = manager.create_session(cwd=str(tmp_path / "project"), mode="normal")
    session.provider = "dashscope"
    session.model = "qwen3.7-max"
    session.thinking_enabled = None
    # 隔离真实 settings.yml:配置项未设置 → None,只考验家族默认。
    monkeypatch.setattr(sm, "_provider_thinking_config", lambda provider, model: None)

    payload = session.to_dict()
    assert payload["thinkingEnabled"] is None
    assert payload["thinkingEffective"] is True

    # 显式关闭覆盖仍压过家族默认。
    session.thinking_enabled = False
    assert session.to_dict()["thinkingEffective"] is False


def test_to_dict_thinking_effective_reasoning_family_unset_is_off(tmp_path, monkeypatch) -> None:
    # reasoning-effort 家族(openai)未配置时不下发思考指令,效果无法从本地观测,
    # 故 thinkingEffective 解析为“关”;显式打开覆盖后为“开”。
    from iac_code.web import session_manager as sm

    manager = WebSessionManager(projects_dir=tmp_path / "projects")
    session = manager.create_session(cwd=str(tmp_path / "project"), mode="normal")
    session.provider = "openai"
    session.model = "gpt-5.5"
    monkeypatch.setattr(sm, "_provider_thinking_config", lambda provider, model: None)

    session.thinking_enabled = None
    assert session.to_dict()["thinkingEffective"] is False

    session.thinking_enabled = True
    assert session.to_dict()["thinkingEffective"] is True


def test_context_usage_payload_injects_cached_overhead() -> None:
    # /status 据持久化消息重建的 ContextManager 没有系统提示与工具定义,get_usage 会少算这两项;
    # 调用方传入本会话缓存的开销后,重载/状态用量须与 composer 实时圆环口径一致(见问题 #1)。
    messages = [
        Message(role="user", content="hello there, this is a normal user turn"),
        Message(role="assistant", content="and here is a normal assistant reply"),
    ]

    baseline = _context_usage_payload(messages, model="qwen")
    injected = _context_usage_payload(
        messages,
        model="qwen",
        system_prompt_tokens=9000,
        tool_definition_tokens=4000,
    )

    # 基线(无开销)只统计消息;注入后固定开销体现在对应字段与总量上。
    assert baseline["systemPromptTokens"] == 0
    assert baseline["toolDefinitionTokens"] == 0
    assert injected["systemPromptTokens"] == 9000
    assert injected["toolDefinitionTokens"] == 4000
    assert injected["totalTokens"] == baseline["totalTokens"] + 13000

    window = injected["contextWindow"]
    assert injected["usagePercent"] == injected["totalTokens"] / window * 100
    assert injected["usagePercent"] > baseline["usagePercent"]


def test_context_usage_payload_without_overhead_matches_defaults() -> None:
    # 服务器重启后首轮前缓存为 0(降级态):不注入开销,保持旧行为,不应报错或改变基线。
    messages = [Message(role="user", content="a short message")]
    assert _context_usage_payload(messages, model="qwen") == _context_usage_payload(
        messages, model="qwen", system_prompt_tokens=0, tool_definition_tokens=0
    )


def test_new_session_marks_pending_llm_title(tmp_path) -> None:
    manager = WebSessionManager(projects_dir=tmp_path / "projects")
    session = manager.create_session(cwd=str(tmp_path / "project"), session_id="new-1")
    assert session.pending_llm_title is True


def test_reopened_existing_session_does_not_mark_pending_llm_title(tmp_path) -> None:
    manager = WebSessionManager(projects_dir=tmp_path / "projects")
    cwd = str(tmp_path / "project")
    manager.create_session(cwd=cwd, session_id="reopen-1")
    # 从 _sessions 缓存移除，强制走「storage 已存在」重开路径
    manager._sessions.pop(next(iter(manager._sessions)), None)
    reopened = manager.create_session(cwd=cwd, session_id="reopen-1")
    assert reopened.pending_llm_title is False


def test_apply_llm_auto_title_sets_title_persists_and_emits(tmp_path) -> None:
    cwd = str(tmp_path / "project")
    manager = WebSessionManager(projects_dir=tmp_path / "projects")
    session = manager.create_session(cwd=cwd, session_id="llm-1")

    changed = manager.apply_llm_auto_title(session, "创建 OSS 存储桶")

    assert changed is True
    assert session.title == "创建 OSS 存储桶"
    sidecar = _read_web_session_metadata(manager.storage, cwd, session.session_id)
    assert sidecar["autoTitle"] == "创建 OSS 存储桶"


def test_apply_llm_auto_title_noop_when_already_titled(tmp_path) -> None:
    manager = WebSessionManager(projects_dir=tmp_path / "projects")
    session = manager.create_session(cwd=str(tmp_path / "project"), session_id="llm-2")
    session.title = "用户已重命名"
    changed = manager.apply_llm_auto_title(session, "LLM 想覆盖的标题")
    assert changed is False
    assert session.title == "用户已重命名"


@pytest.mark.asyncio
async def test_schedule_llm_title_applies_generated_title(tmp_path, monkeypatch) -> None:
    from iac_code.web import session_manager as sm

    async def fake_generate(**_kwargs):
        return "生成的标题"

    monkeypatch.setattr(sm.session_titler, "generate_session_title", fake_generate)
    manager = WebSessionManager(projects_dir=tmp_path / "projects")
    session = manager.create_session(cwd=str(tmp_path / "project"), session_id="llm-3")
    assert session.pending_llm_title is True

    manager.schedule_llm_title(session, text="帮我建个桶", image_ids=[])
    assert session.pending_llm_title is False  # 立即消费,避免重触发
    # 等待在途标题任务完成
    for task in list(session.active_local_tasks):
        await task
    assert session.title == "生成的标题"


@pytest.mark.asyncio
async def test_schedule_llm_title_falls_back_to_image_name(tmp_path, monkeypatch) -> None:
    from iac_code.web import session_manager as sm

    async def fake_generate(**_kwargs):
        return None  # 两次都失败

    monkeypatch.setattr(sm.session_titler, "generate_session_title", fake_generate)
    monkeypatch.setattr(sm, "load_cached_image", lambda image_id, *, cwd, session_id: _FakeImg())
    manager = WebSessionManager(projects_dir=tmp_path / "projects")
    session = manager.create_session(cwd=str(tmp_path / "project"), session_id="llm-4")

    manager.schedule_llm_title(session, text="", image_ids=["img-1"])
    for task in list(session.active_local_tasks):
        await task
    # 纯图片失败 → 非空通用名,不再是 (empty),可被侧栏列出
    assert session.title and session.title != "(empty)"
    assert _is_listable_session(session)


class _FakeImg:
    media_type = "image/png"
    base64_data = "AAAA"


def test_apply_pipeline_auto_title_marks_title_provisional(tmp_path) -> None:
    manager = WebSessionManager(projects_dir=tmp_path / "projects")
    session = manager.create_session(
        cwd=str(tmp_path / "project"), mode="pipeline", pipeline_name="selling", session_id="pipe-prov-1"
    )
    assert session.title_provisional is False

    manager.apply_pipeline_auto_title(session, "帮我搭一条售卖流水线")

    # 流水线首个回合设的是「即时占位」标题,标记为临时,允许随后 LLM 结果覆盖。
    assert session.title_provisional is True


def test_apply_llm_auto_title_overwrites_provisional_pipeline_title(tmp_path) -> None:
    cwd = str(tmp_path / "project")
    manager = WebSessionManager(projects_dir=tmp_path / "projects")
    session = manager.create_session(cwd=cwd, mode="pipeline", pipeline_name="selling", session_id="pipe-prov-2")
    manager.apply_pipeline_auto_title(session, "帮我搭一条售卖流水线")

    changed = manager.apply_llm_auto_title(session, "售卖流水线搭建")

    # LLM 结果应覆盖临时占位标题,并清除临时标记(冻结为正式标题)。
    assert changed is True
    assert session.title == "售卖流水线搭建"
    assert session.title_provisional is False
    sidecar = _read_web_session_metadata(manager.storage, cwd, session.session_id)
    assert sidecar["autoTitle"] == "售卖流水线搭建"
    # 覆盖后不再是临时标题,第二次 LLM 结果不得再覆盖。
    assert manager.apply_llm_auto_title(session, "又一个标题") is False
    assert session.title == "售卖流水线搭建"


def test_apply_llm_auto_title_does_not_overwrite_rename_over_provisional(tmp_path) -> None:
    manager = WebSessionManager(projects_dir=tmp_path / "projects")
    session = manager.create_session(
        cwd=str(tmp_path / "project"), mode="pipeline", pipeline_name="selling", session_id="pipe-prov-3"
    )
    manager.apply_pipeline_auto_title(session, "帮我搭一条售卖流水线")
    # 用户在 LLM 结果回来之前手动重命名 → 重命名必须胜出,不被在途 LLM 结果覆盖。
    manager.rename_session(session, "my-project")

    changed = manager.apply_llm_auto_title(session, "LLM 想覆盖的标题")

    assert changed is False
    assert session.title == "my-project"


def test_rename_session_clears_title_provisional(tmp_path) -> None:
    manager = WebSessionManager(projects_dir=tmp_path / "projects")
    session = manager.create_session(
        cwd=str(tmp_path / "project"), mode="pipeline", pipeline_name="selling", session_id="pipe-prov-4"
    )
    manager.apply_pipeline_auto_title(session, "帮我搭一条售卖流水线")
    assert session.title_provisional is True

    manager.rename_session(session, "my-project")

    assert session.title_provisional is False


def test_schedule_llm_title_noop_when_not_pending(tmp_path, monkeypatch) -> None:
    from iac_code.web import session_manager as sm

    called = {"n": 0}

    async def fake_generate(**_kwargs):
        called["n"] += 1
        return "x"

    monkeypatch.setattr(sm.session_titler, "generate_session_title", fake_generate)
    manager = WebSessionManager(projects_dir=tmp_path / "projects")
    session = manager.create_session(cwd=str(tmp_path / "project"), session_id="llm-5")
    session.pending_llm_title = False
    manager.schedule_llm_title(session, text="hi", image_ids=[])
    assert not session.active_local_tasks
    assert called["n"] == 0
