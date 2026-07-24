import json

import pytest
from starlette.testclient import TestClient

from iac_code.agent.message import (
    RECALLED_MEMORY_MARKER,
    Message,
    TextBlock,
    ThinkingBlock,
    ToolResultBlock,
    ToolUseBlock,
    create_recalled_memory_message,
)


def _visible_pairs(messages: list[dict]) -> list[dict[str, str]]:
    return [{"role": message.get("role"), "content": message.get("content")} for message in messages]


def test_load_visible_messages_skips_last_prompt_meta_and_returns_user_message(tmp_path) -> None:
    from iac_code.web.session_manager import WebSessionManager

    cwd = str(tmp_path / "project")
    manager = WebSessionManager(projects_dir=tmp_path / "projects")
    session = manager.create_session(cwd=cwd, session_id="session-1")

    manager.storage.append(cwd, session.session_id, Message(role="user", content="visible user prompt"))
    manager.storage.append_meta(
        cwd,
        session.session_id,
        {
            "type": "last-prompt",
            "last_prompt": "hidden prompt summary",
        },
    )

    assert _visible_pairs(manager.load_visible_messages(session.session_id, cwd=cwd)) == [
        {
            "role": "user",
            "content": "visible user prompt",
        }
    ]


def test_load_visible_messages_skips_recalled_memory_metadata(tmp_path) -> None:
    from iac_code.web.session_manager import WebSessionManager

    cwd = str(tmp_path / "project")
    manager = WebSessionManager(projects_dir=tmp_path / "projects")
    session = manager.create_session(cwd=cwd, session_id="session-1")

    manager.storage.append(cwd, session.session_id, Message(role="user", content="visible"))
    manager.storage.append(
        cwd,
        session.session_id,
        create_recalled_memory_message("# Recalled Memory\nhidden memory", ["memory.md"]),
    )

    messages = manager.load_visible_messages(session.session_id, cwd=cwd)

    assert _visible_pairs(messages) == [{"role": "user", "content": "visible"}]


def test_load_visible_messages_skips_legacy_recalled_memory_text_without_metadata(tmp_path) -> None:
    from iac_code.web.session_manager import WebSessionManager

    cwd = str(tmp_path / "project")
    manager = WebSessionManager(projects_dir=tmp_path / "projects")
    session = manager.create_session(cwd=cwd, session_id="session-1")

    manager.storage.append(cwd, session.session_id, Message(role="user", content="visible"))
    manager.storage.append(
        cwd,
        session.session_id,
        Message(role="user", content="{}\nlegacy hidden memory".format(RECALLED_MEMORY_MARKER)),
    )

    messages = manager.load_visible_messages(session.session_id, cwd=cwd)

    assert _visible_pairs(messages) == [{"role": "user", "content": "visible"}]


def test_load_visible_messages_skips_cleanup_prompt_metadata_when_available(tmp_path) -> None:
    try:
        from iac_code.pipeline.engine.cleanup import create_cleanup_prompt_message
    except Exception as exc:  # pragma: no cover - optional import guard
        pytest.skip("cleanup prompt helper is not importable: {}".format(exc))

    from iac_code.web.session_manager import WebSessionManager

    cwd = str(tmp_path / "project")
    manager = WebSessionManager(projects_dir=tmp_path / "projects")
    session = manager.create_session(cwd=cwd, session_id="session-1")

    manager.storage.append(cwd, session.session_id, create_cleanup_prompt_message("hidden cleanup prompt"))
    manager.storage.append(cwd, session.session_id, Message(role="assistant", content="visible assistant reply"))

    messages = manager.load_visible_messages(session.session_id, cwd=cwd)

    assert _visible_pairs(messages) == [{"role": "assistant", "content": "visible assistant reply"}]


def test_load_visible_messages_skips_legacy_cleanup_prompt_text_without_metadata(tmp_path) -> None:
    from iac_code.web.session_manager import WebSessionManager

    cwd = str(tmp_path / "project")
    manager = WebSessionManager(projects_dir=tmp_path / "projects")
    session = manager.create_session(cwd=cwd, session_id="session-1")

    manager.storage.append(
        cwd,
        session.session_id,
        Message(
            role="user",
            content="Rollback cleanup required for leftover resource stack-abc in DELETE_COMPLETE.",
        ),
    )
    manager.storage.append(cwd, session.session_id, Message(role="assistant", content="visible assistant reply"))

    messages = manager.load_visible_messages(session.session_id, cwd=cwd)

    assert _visible_pairs(messages) == [{"role": "assistant", "content": "visible assistant reply"}]


def test_load_visible_messages_skips_internal_skill_context_metadata(tmp_path) -> None:
    from iac_code.web.session_manager import WebSessionManager

    cwd = str(tmp_path / "project")
    manager = WebSessionManager(projects_dir=tmp_path / "projects")
    session = manager.create_session(cwd=cwd, session_id="session-1")

    manager.storage.append(
        cwd,
        session.session_id,
        Message(
            role="user",
            content="hidden bundled skill instructions",
            metadata={"type": "internal-skill-context"},
        ),
    )
    manager.storage.append(cwd, session.session_id, Message(role="user", content="visible request"))

    assert _visible_pairs(manager.load_visible_messages(session.session_id, cwd=cwd)) == [
        {
            "role": "user",
            "content": "visible request",
        }
    ]


def test_resume_messages_keep_hidden_replay_messages_that_browser_hides(tmp_path) -> None:
    from iac_code.pipeline.constants import CLEANUP_PROMPT_METADATA_TYPE
    from iac_code.web.session_manager import WebSessionManager

    cwd = str(tmp_path / "project")
    manager = WebSessionManager(projects_dir=tmp_path / "projects")
    session = manager.create_session(cwd=cwd, session_id="session-1")
    hidden_memory = create_recalled_memory_message("# Recalled Memory\nhidden memory", ["memory.md"])
    hidden_skill_context = Message(
        role="user",
        content="hidden bundled skill instructions",
        metadata={"type": "internal-skill-context"},
    )
    hidden_cleanup = Message(
        role="user",
        content="hidden cleanup context",
        metadata={"type": CLEANUP_PROMPT_METADATA_TYPE},
    )

    manager.storage.append(cwd, session.session_id, Message(role="user", content="visible request"))
    manager.storage.append(cwd, session.session_id, hidden_memory)
    manager.storage.append(cwd, session.session_id, hidden_cleanup)
    manager.storage.append(cwd, session.session_id, hidden_skill_context)

    assert _visible_pairs(manager.load_visible_messages(session.session_id, cwd=cwd)) == [
        {
            "role": "user",
            "content": "visible request",
        }
    ]
    assert [message.content for message in manager.load_resume_messages(session.session_id, cwd=cwd)] == [
        "visible request",
        hidden_memory.content,
        "hidden cleanup context",
        "hidden bundled skill instructions",
    ]


def test_load_visible_messages_skips_pipeline_handoff_context(tmp_path) -> None:
    from iac_code.web.session_manager import WebSessionManager

    cwd = str(tmp_path / "project")
    manager = WebSessionManager(projects_dir=tmp_path / "projects")
    session = manager.create_session(cwd=cwd, session_id="session-1")
    manager.storage.append(
        cwd,
        session.session_id,
        Message(role="user", content="[Pipeline Handoff Context]\nPipeline: selling\nOutcome: completed"),
    )
    manager.storage.append(cwd, session.session_id, Message(role="user", content="visible follow-up"))

    assert _visible_pairs(manager.load_visible_messages(session.session_id, cwd=cwd)) == [
        {"role": "user", "content": "visible follow-up"}
    ]


def test_visible_transcript_groups_tool_calls_thinking_and_markdown_text(tmp_path) -> None:
    from iac_code.web.session_manager import WebSessionManager

    cwd = str(tmp_path / "project")
    manager = WebSessionManager(projects_dir=tmp_path / "projects")
    session = manager.create_session(cwd=cwd, session_id="session-1")

    manager.storage.append(cwd, session.session_id, Message(role="user", content="build wordpress"))
    manager.storage.append(
        cwd,
        session.session_id,
        Message(
            role="assistant",
            content=[
                ThinkingBlock(thinking="private chain summary"),
                TextBlock(text="## Plan\n\n- create vpc\n- deploy wordpress"),
                ToolUseBlock(id="toolu_1", name="write_file", input={"path": "template.yml"}),
            ],
        ),
    )
    manager.storage.append(
        cwd,
        session.session_id,
        Message(role="user", content=[ToolResultBlock(tool_use_id="toolu_1", content="wrote template.yml")]),
    )
    manager.storage.append(cwd, session.session_id, Message(role="assistant", content="Done."))

    transcript = manager.load_visible_transcript(session.session_id, cwd=cwd)
    messages = transcript["messages"]

    assert _visible_pairs(messages) == [
        {"role": "user", "content": "build wordpress"},
        {"role": "assistant", "content": "## Plan\n\n- create vpc\n- deploy wordpress"},
        {"role": "assistant", "content": "Done."},
    ]
    assert messages[1]["thinking"] == "private chain summary"
    assert messages[1]["toolUseIds"] == ["toolu_1"]
    assert "private chain summary" not in messages[1]["content"]
    assert transcript["tools"]["toolu_1"]["toolName"] == "write_file"
    assert transcript["tools"]["toolu_1"]["status"] == "completed"
    assert transcript["tools"]["toolu_1"]["input"] == {"path": "template.yml"}
    assert transcript["tools"]["toolu_1"]["results"] == [
        {"content": "wrote template.yml", "isError": False, "toolUseId": "toolu_1"}
    ]


def test_visible_transcript_preserves_text_tool_text_boundaries(tmp_path) -> None:
    from iac_code.web.session_manager import WebSessionManager

    cwd = str(tmp_path / "project")
    manager = WebSessionManager(projects_dir=tmp_path / "projects")
    session = manager.create_session(cwd=cwd, session_id="session-1")

    manager.storage.append(cwd, session.session_id, Message(role="user", content="inspect repo"))
    manager.storage.append(
        cwd,
        session.session_id,
        Message(
            role="assistant",
            content=[
                TextBlock(text="I will inspect the workspace."),
                ToolUseBlock(id="toolu_read", name="read_file", input={"path": "Makefile"}),
                ToolUseBlock(id="toolu_list", name="list_files", input={"path": "src"}),
                TextBlock(text="The project is uv-managed, so I will run tests."),
                ToolUseBlock(id="toolu_shell", name="bash", input={"command": "make test"}),
            ],
        ),
    )
    manager.storage.append(
        cwd,
        session.session_id,
        Message(role="user", content=[ToolResultBlock(tool_use_id="toolu_read", content="Makefile content")]),
    )
    manager.storage.append(
        cwd,
        session.session_id,
        Message(role="user", content=[ToolResultBlock(tool_use_id="toolu_list", content="src/iac_code")]),
    )
    manager.storage.append(
        cwd,
        session.session_id,
        Message(role="user", content=[ToolResultBlock(tool_use_id="toolu_shell", content="passed")]),
    )

    messages = manager.load_visible_transcript(session.session_id, cwd=cwd)["messages"]

    assert _visible_pairs(messages) == [
        {"role": "user", "content": "inspect repo"},
        {"role": "assistant", "content": "I will inspect the workspace."},
        {"role": "assistant", "content": "The project is uv-managed, so I will run tests."},
    ]
    assert messages[1]["toolUseIds"] == ["toolu_read", "toolu_list"]
    assert messages[2]["toolUseIds"] == ["toolu_shell"]


def test_get_session_messages_route_returns_messages_and_missing_session_404(tmp_path) -> None:
    from iac_code.web.app import create_app
    from iac_code.web.session_manager import WebSessionManager

    cwd = str(tmp_path / "project")
    manager = WebSessionManager(projects_dir=tmp_path / "projects")
    session = manager.create_session(cwd=cwd, session_id="session-1")
    manager.storage.append(cwd, session.session_id, Message(role="user", content="visible over HTTP"))
    app = create_app(session_manager=manager)

    with TestClient(app) as client:
        found_response = client.get("/api/sessions/session-1/messages")
        missing_response = client.get("/api/sessions/missing/messages")

    assert found_response.status_code == 200
    payload = found_response.json()
    assert _visible_pairs(payload["messages"]) == [{"role": "user", "content": "visible over HTTP"}]
    assert payload["tools"] == {}
    assert missing_response.status_code == 404
    assert missing_response.json() == {"error": {"message": "session not found"}}


def test_legacy_pipeline_sidecar_is_exposed_for_web_recovery(tmp_path) -> None:
    from iac_code.web.app import create_app
    from iac_code.web.session_manager import WebSessionManager

    cwd = str(tmp_path / "project")
    session_id = "pipeline-1"
    manager = WebSessionManager(projects_dir=tmp_path / "projects")
    manager.storage.save(cwd, session_id, [Message(role="user", content="run pipeline")])
    display_path = manager.storage.session_dir(cwd, session_id) / "pipeline" / "display.jsonl"
    display_path.parent.mkdir(parents=True)
    display_events = [
        {
            "version": 1,
            "type": "pipeline_started",
            "pipeline_name": "selling",
            "timestamp": 1,
            "payload": {"pipeline_type": "selling"},
        },
        {
            "version": 1,
            "type": "step_started",
            "step_id": "collect_requirements",
            "timestamp": 2,
            "payload": {"index": 1, "total": 2, "step_type": "agent"},
        },
        {
            "version": 1,
            "type": "tool_used",
            "step_id": "collect_requirements",
            "timestamp": 3,
            "payload": {"name": "ask_user_question", "tool_use_id": "toolu_question"},
        },
        {
            "version": 1,
            "type": "step_completed",
            "step_id": "collect_requirements",
            "timestamp": 4,
            "payload": {},
        },
        {
            "version": 1,
            "type": "pipeline_completed",
            "timestamp": 5,
            "payload": {"failed": False},
        },
    ]
    display_path.write_text("\n".join(json.dumps(event) for event in display_events) + "\n", encoding="utf-8")

    session = manager.get_session(session_id)

    assert session is not None
    assert session.mode == "pipeline"
    replay = manager.load_pipeline_display_replay(session_id, cwd=cwd)
    assert replay["pipelineName"] == "selling"
    assert replay["completed"] is True
    assert replay["attempts"][0]["stepId"] == "collect_requirements"
    assert replay["attempts"][0]["tools"] == [{"name": "ask_user_question", "toolUseId": "toolu_question"}]

    app = create_app(session_manager=manager)
    with TestClient(app) as client:
        response = client.get("/api/sessions/pipeline-1")

    assert response.status_code == 200
    payload = response.json()
    assert payload["mode"] == "pipeline"
    assert payload["pipeline"]["displayReplay"]["attempts"][0]["stepId"] == "collect_requirements"


def test_visible_transcript_replays_pipeline_sidecar_step_transcripts(tmp_path) -> None:
    from iac_code.pipeline.engine.transcript_storage import PipelineTranscriptStorage
    from iac_code.web.session_manager import WebSessionManager

    cwd = str(tmp_path / "project")
    session_id = "pipeline-1"
    manager = WebSessionManager(projects_dir=tmp_path / "projects")
    manager.storage.save(
        cwd,
        session_id,
        [
            Message(role="user", content="选择一个已有vpc，创建一个vswitch"),
            Message(role="user", content="[Pipeline Handoff Context]\nPipeline: selling\nOutcome: completed"),
            Message(role="user", content="后续追问"),
        ],
    )
    display_path = manager.storage.session_dir(cwd, session_id) / "pipeline" / "display.jsonl"
    display_path.parent.mkdir(parents=True)
    display_events = [
        {
            "version": 1,
            "type": "pipeline_started",
            "pipeline_name": "selling",
            "timestamp": 1,
            "payload": {"pipeline_type": "selling"},
        },
        {
            "version": 1,
            "type": "step_started",
            "step_id": "intent_parsing",
            "timestamp": 2,
            "payload": {
                "index": 1,
                "total": 2,
                "active_attempt_id": "att_0001",
                "transcript_id": "transcript_att_0001",
            },
        },
        {
            "version": 1,
            "type": "tool_used",
            "step_id": "intent_parsing",
            "timestamp": 3,
            "payload": {"name": "complete_step", "tool_use_id": "toolu_complete"},
        },
        {
            "version": 1,
            "type": "step_completed",
            "step_id": "intent_parsing",
            "timestamp": 4,
            "payload": {},
        },
        {
            "version": 1,
            "type": "step_started",
            "step_id": "evaluate_candidates",
            "timestamp": 5,
            "payload": {
                "index": 2,
                "total": 2,
                "active_attempt_id": "att_0002",
                "step_type": "parallel_sub_pipeline",
            },
        },
        {
            "version": 1,
            "type": "sub_step_started",
            "step_id": "template_generating",
            "timestamp": 6,
            "payload": {
                "parent_step_id": "evaluate_candidates",
                "sub_pipeline_id": "candidate_0",
                "sub_pipeline_name": "evaluate_candidate",
                "candidate_name": "已有VPC下新建VSwitch",
                "active_attempt_id": "att_0003",
                "transcript_id": "transcript_att_0003",
            },
        },
        {
            "version": 1,
            "type": "sub_step_started",
            "step_id": "cost_estimating",
            "timestamp": 6.5,
            "payload": {
                "parent_step_id": "evaluate_candidates",
                "sub_pipeline_id": "candidate_0",
                "sub_pipeline_name": "evaluate_candidate",
                "candidate_name": "已有VPC下新建VSwitch",
                "active_attempt_id": "att_0004",
                "transcript_id": "transcript_att_0004",
            },
        },
        {
            "version": 1,
            "type": "pipeline_completed",
            "timestamp": 7,
            "payload": {"failed": False},
        },
    ]
    display_path.write_text("\n".join(json.dumps(event) for event in display_events) + "\n", encoding="utf-8")

    transcript_storage = PipelineTranscriptStorage(display_path.parent)
    transcript_storage.save(
        cwd,
        "transcript_att_0001",
        [
            Message(role="user", content="选择一个已有vpc，创建一个vswitch"),
            Message(
                role="assistant",
                content=[
                    TextBlock(text="我会解析网络需求。"),
                    ToolUseBlock(id="toolu_complete", name="complete_step", input={"ok": True}),
                ],
            ),
            Message(role="user", content=[ToolResultBlock(tool_use_id="toolu_complete", content="done")]),
        ],
    )
    transcript_storage.save(
        cwd,
        "transcript_att_0003",
        [
            Message(role="user", content="请完成当前步骤：template_generating。"),
            Message(
                role="assistant",
                content=[
                    TextBlock(text="已生成 VSwitch 模板。"),
                    ToolUseBlock(id="toolu_shell", name="bash", input={"command": "make test"}),
                ],
            ),
            Message(role="user", content=[ToolResultBlock(tool_use_id="toolu_shell", content="passed")]),
        ],
    )
    transcript_storage.save(
        cwd,
        "transcript_att_0004",
        [
            Message(role="user", content="请完成当前步骤：cost_estimating。"),
            Message(role="assistant", content=[TextBlock(text="VSwitch 不产生费用。")]),
        ],
    )

    transcript = manager.load_visible_transcript(session_id, cwd=cwd)

    assert _visible_pairs(transcript["messages"]) == [
        {"role": "user", "content": "选择一个已有vpc，创建一个vswitch"},
        {"role": "assistant", "content": "● Intent parsing (1/2)"},
        {"role": "assistant", "content": "我会解析网络需求。"},
        {"role": "assistant", "content": "● Evaluate candidates (2/2)"},
        {"role": "assistant", "content": "◆ Plan: 已有VPC下新建VSwitch"},
        {"role": "assistant", "content": "· Template generation"},
        {"role": "assistant", "content": "已生成 VSwitch 模板。"},
        {"role": "assistant", "content": "· Cost estimation"},
        {"role": "assistant", "content": "VSwitch 不产生费用。"},
        {"role": "assistant", "content": "↪ Normal chat"},
        {"role": "user", "content": "后续追问"},
    ]
    assert transcript["messages"][1]["kind"] == "pipeline_step"
    assert transcript["messages"][1]["pipelineStep"] == {
        "level": "step",
        "stepId": "intent_parsing",
        "title": "Intent parsing",
        "index": 1,
        "total": 2,
        "status": "completed",
        "attemptNo": 1,
        "parentStepId": "",
        "candidateName": "",
        "groupId": "step:intent_parsing:att_0001",
        "parentGroupId": "",
        "depth": 0,
    }
    assert transcript["messages"][3]["kind"] == "pipeline_step"
    assert transcript["messages"][4]["kind"] == "pipeline_candidate"
    assert transcript["messages"][4]["pipelineStep"]["level"] == "candidate"
    assert transcript["messages"][4]["pipelineStep"]["groupId"] == "candidate:candidate_0"
    assert transcript["messages"][4]["pipelineStep"]["parentGroupId"] == "step:evaluate_candidates:att_0002"
    assert transcript["messages"][5]["kind"] == "pipeline_sub_step"
    assert transcript["messages"][5]["pipelineStep"]["parentStepId"] == "evaluate_candidates"
    assert transcript["messages"][5]["pipelineStep"]["candidateName"] == "已有VPC下新建VSwitch"
    assert transcript["messages"][5]["pipelineStep"]["parentGroupId"] == "candidate:candidate_0"
    assert transcript["messages"][7]["kind"] == "pipeline_sub_step"
    assert transcript["messages"][9]["kind"] == "normal_chat_boundary"
    # complete_step is now surfaced so its conclusion renders as a card in the restored transcript.
    assert "toolu_complete" in transcript["tools"]
    assert transcript["tools"]["toolu_complete"]["toolName"] == "complete_step"
    assert transcript["tools"]["toolu_complete"]["input"] == {"ok": True}
    assert transcript["tools"]["toolu_complete"]["results"] == [
        {"content": "done", "isError": False, "toolUseId": "toolu_complete"}
    ]
    assert transcript["messages"][6]["toolUseIds"] == ["toolu_shell"]
    assert transcript["tools"]["toolu_shell"]["toolName"] == "bash"
    assert transcript["tools"]["toolu_shell"]["results"] == [
        {"content": "passed", "isError": False, "toolUseId": "toolu_shell"}
    ]


def test_steered_injected_message_replays_exactly_once(tmp_path) -> None:
    # 风险门:引导(steer)会即时发一条带唯一 messageId 的 live `user.message` 气泡,随后
    # agent_loop 的 _drain_pending_injections 会把同一条消息 add_user_message + 存储 append。
    # 完整 reload/replay 只从存储重建状态(不重放 live 事件),因此这条被注入的用户消息
    # 必须恰好出现一次,既不被当作隐藏元数据吞掉,也不重复。
    from iac_code.web.session_manager import WebSessionManager

    cwd = str(tmp_path / "project")
    manager = WebSessionManager(projects_dir=tmp_path / "projects")
    session = manager.create_session(cwd=cwd, session_id="session-1")

    manager.storage.append(cwd, session.session_id, Message(role="user", content="首条 prompt"))
    manager.storage.append(cwd, session.session_id, Message(role="assistant", content="working..."))
    # agent_loop 排空注入后落盘的普通用户消息(steer 的持久化形态)。
    manager.storage.append(cwd, session.session_id, Message(role="user", content="插队消息"))
    manager.storage.append(cwd, session.session_id, Message(role="assistant", content="acknowledged"))

    messages = manager.load_visible_transcript(session.session_id, cwd=cwd)["messages"]
    steer_pairs = [pair for pair in _visible_pairs(messages) if pair["content"] == "插队消息"]

    assert len(steer_pairs) == 1
    assert steer_pairs[0] == {"role": "user", "content": "插队消息"}


def test_stored_sessions_can_be_listed_and_resumed_after_memory_cache_is_cleared(tmp_path) -> None:
    from iac_code.web.session_manager import WebSessionManager

    cwd = str(tmp_path / "project")
    manager = WebSessionManager(projects_dir=tmp_path / "projects")
    session = manager.create_session(cwd=cwd, session_id="session-1")
    manager.storage.append(cwd, session.session_id, Message(role="user", content="persisted prompt"))

    manager._sessions.clear()

    listed = manager.list_sessions()
    resumed = manager.get_session("session-1")

    assert [item.session_id for item in listed] == ["session-1"]
    assert resumed is not None
    assert resumed.session_id == "session-1"
    assert resumed.cwd == cwd
    assert _visible_pairs(manager.load_visible_messages(resumed.session_id, cwd=resumed.cwd)) == [
        {"role": "user", "content": "persisted prompt"}
    ]


def test_stored_sessions_can_be_resumed_by_new_manager(tmp_path) -> None:
    from iac_code.web.session_manager import WebSessionManager

    cwd = str(tmp_path / "project")
    first_manager = WebSessionManager(projects_dir=tmp_path / "projects")
    session = first_manager.create_session(cwd=cwd, session_id="session-1")
    first_manager.storage.append(cwd, session.session_id, Message(role="user", content="persisted prompt"))

    second_manager = WebSessionManager(projects_dir=tmp_path / "projects")
    resumed = second_manager.get_session("session-1")

    assert resumed is not None
    assert resumed.session_id == "session-1"
    assert resumed.cwd == cwd
    assert _visible_pairs(second_manager.load_visible_messages(resumed.session_id, cwd=resumed.cwd)) == [
        {"role": "user", "content": "persisted prompt"}
    ]


def test_legacy_jsonl_session_lookup_migrates_and_preserves_visible_replay(tmp_path) -> None:
    from iac_code.web.session_manager import WebSessionManager

    cwd = str(tmp_path / "project")
    manager = WebSessionManager(projects_dir=tmp_path / "projects")
    legacy_path = manager.storage.legacy_session_path(cwd, "legacy-1")
    legacy_path.parent.mkdir(parents=True, exist_ok=True)
    row = Message(role="user", content="legacy prompt").to_dict()
    row.update({"cwd": cwd, "session_id": "legacy-1", "version": "test"})
    legacy_path.write_text(json.dumps(row) + "\n", encoding="utf-8")
    assert legacy_path.exists()

    manager._sessions.clear()
    resumed = manager.get_session("legacy-1")

    assert resumed is not None
    assert resumed.session_id == "legacy-1"
    assert resumed.cwd == cwd
    assert manager.storage.session_path(cwd, "legacy-1").name == "session.jsonl"
    assert not legacy_path.exists()
    assert _visible_pairs(manager.load_visible_messages("legacy-1", cwd=cwd)) == [
        {"role": "user", "content": "legacy prompt"}
    ]


def test_visible_transcript_exposes_turn_elapsed_seconds(tmp_path) -> None:
    from iac_code.web.session_manager import WebSessionManager

    cwd = str(tmp_path / "project")
    manager = WebSessionManager(projects_dir=tmp_path / "projects")
    session = manager.create_session(cwd=cwd, session_id="session-elapsed")

    manager.storage.append(cwd, session.session_id, Message(role="user", content="deploy something"))
    manager.storage.append(
        cwd,
        session.session_id,
        Message(role="assistant", content="Done.", elapsed_seconds=12.5),
    )

    messages = manager.load_visible_messages(session.session_id, cwd=cwd)
    assistant = [message for message in messages if message.get("role") == "assistant"]
    assert assistant, "expected an assistant message in the visible transcript"
    assert assistant[-1].get("elapsedSeconds") == pytest.approx(12.5)


def test_visible_transcript_omits_elapsed_seconds_when_unset(tmp_path) -> None:
    from iac_code.web.session_manager import WebSessionManager

    cwd = str(tmp_path / "project")
    manager = WebSessionManager(projects_dir=tmp_path / "projects")
    session = manager.create_session(cwd=cwd, session_id="session-no-elapsed")

    manager.storage.append(cwd, session.session_id, Message(role="user", content="hi"))
    manager.storage.append(cwd, session.session_id, Message(role="assistant", content="hello"))

    messages = manager.load_visible_messages(session.session_id, cwd=cwd)
    assistant = [message for message in messages if message.get("role") == "assistant"]
    assert assistant, "expected an assistant message in the visible transcript"
    assert "elapsedSeconds" not in assistant[-1]


def test_reload_boundary_before_first_normalchat_prompt(tmp_path) -> None:
    # Issue 7 恢复路径:交接后带 ``normalChat`` 标记的首条 prompt 前插入「↪ 普通对话」分隔;
    # 流水线中途(``source=pipeline`` 无标记)的 input_required 回复不能触发分隔。
    from iac_code.web.session_manager import WebSessionManager

    cwd = str(tmp_path / "project")
    manager = WebSessionManager(projects_dir=tmp_path / "projects")
    session = manager.create_session(cwd=cwd, mode="pipeline", session_id="pipe-reload-1")

    manager.persist_pipeline_user_prompt(session, "选择一个已有vpc，创建一个vswitch")
    manager.persist_pipeline_user_prompt(session, "用香港地域")  # 流水线中途补充
    manager.persist_pipeline_user_prompt(session, "帮我看下费用", normal_chat=True)

    messages = manager.load_visible_transcript(session.session_id, cwd=cwd)["messages"]
    boundaries = [i for i, msg in enumerate(messages) if msg.get("kind") == "normal_chat_boundary"]
    assert len(boundaries) == 1
    boundary_index = boundaries[0]
    assert messages[boundary_index]["content"] == "↪ Normal chat"
    # 分隔紧贴交接后的首条普通对话 prompt。
    assert messages[boundary_index + 1]["content"] == "帮我看下费用"
    # 流水线中途补充仍在分隔之前(留在流水线内部)。
    mid_index = next(i for i, msg in enumerate(messages) if msg.get("content") == "用香港地域")
    assert mid_index < boundary_index


def test_reload_no_boundary_for_midpipeline_replies(tmp_path) -> None:
    # Issue 7 回归:全程都在流水线内部(所有 prompt 都是 ``source=pipeline`` 无 ``normalChat``)
    # 时,不能凭空插入「↪ 普通对话」分隔。
    from iac_code.web.session_manager import WebSessionManager

    cwd = str(tmp_path / "project")
    manager = WebSessionManager(projects_dir=tmp_path / "projects")
    session = manager.create_session(cwd=cwd, mode="pipeline", session_id="pipe-reload-2")

    manager.persist_pipeline_user_prompt(session, "选择一个已有vpc，创建一个vswitch")
    manager.persist_pipeline_user_prompt(session, "用香港地域")

    messages = manager.load_visible_transcript(session.session_id, cwd=cwd)["messages"]
    assert not any(msg.get("kind") == "normal_chat_boundary" for msg in messages)


def test_reload_weaves_midpipeline_answer_into_confirm_region(tmp_path, monkeypatch) -> None:
    # Issue 2: 流水线中途对 confirm_and_select 的选择答复("0")在 reload 时被追加到整段
    # 流水线回放之后(错位到最后面),而不是落在回放里 confirm 提示与 deploying 步骤之间的
    # 时序位置。修复后应把该答复织入回放,紧跟 confirm 提示行、位于 deploying 步骤标记之前。
    from iac_code.web.session_manager import WebSessionManager

    cwd = str(tmp_path / "project")
    manager = WebSessionManager(projects_dir=tmp_path / "projects")
    session = manager.create_session(cwd=cwd, mode="pipeline", session_id="pipe-weave-1")

    manager.persist_pipeline_user_prompt(session, "选择一个已有vpc，创建一个vswitch")  # launcher
    manager.persist_pipeline_user_prompt(session, "0")  # confirm 的中途选择答复

    def _env(event_type: str, scope: str, sequence: int, **extra):
        env = {"eventType": event_type, "scope": scope, "sequence": sequence, "data": extra.pop("data", {})}
        env.update(extra)
        return env

    confirm = {"id": "confirm_and_select", "runId": "step-confirm_and_select-1", "index": 4, "total": 5}
    deploying = {"id": "deploying", "runId": "step-deploying-1", "index": 5, "total": 5}
    envelopes = [
        _env("step_started", "step", 1, step=confirm),
        _env(
            "tool_result",
            "step",
            2,
            step=confirm,
            data={"toolName": "complete_step", "toolUseId": "c1", "result": "options"},
        ),
        _env("step_completed", "step", 3, step=confirm, data={"durationS": 1.0}),
        _env("input_required", "step", 4, step=confirm, data={"prompt": "请选择要部署的方案："}),
        _env("input_received", "step", 5, step=confirm, data={"kind": "candidate_selection", "selectedValue": "0"}),
        _env("step_started", "step", 6, step=deploying),
        _env(
            "tool_result",
            "step",
            7,
            step=deploying,
            data={"toolName": "ros_stack", "toolUseId": "d1", "result": "deployed"},
        ),
        _env("step_completed", "step", 8, step=deploying, data={"durationS": 2.0}),
    ]
    monkeypatch.setattr(manager, "_load_a2a_pipeline_envelopes", lambda ctx: envelopes)

    messages = manager.load_visible_transcript(session.session_id, cwd=cwd)["messages"]

    idx_confirm = next(i for i, msg in enumerate(messages) if "请选择要部署的方案" in (msg.get("content") or ""))
    idx_deploy = next(i for i, msg in enumerate(messages) if msg.get("messageId") == "plmk-step-deploying-1")
    answer_indices = [
        i for i, msg in enumerate(messages) if msg.get("role") == "user" and (msg.get("content") or "").strip() == "0"
    ]
    assert answer_indices, "mid-pipeline answer '0' should be present in the reload transcript"
    idx_answer = answer_indices[0]
    # 答复织入 confirm 提示与 deploying 之间(即"中间"),而非追加到整段回放末尾。
    assert idx_confirm < idx_answer < idx_deploy, (idx_confirm, idx_answer, idx_deploy)
    # 且答复不再是转录的最后一条。
    assert idx_answer != len(messages) - 1


def test_compute_replay_sequence_idle_normal_session_skips_buffer_replay() -> None:
    # 普通会话空闲(无进行中轮次)重载时,存储转录即完整历史;replaySequence 必须回到
    # latestSequence 以跳过整段缓冲区回放。否则已完成轮次会被回放,而回放的实时事件用
    # uuid/`user-<turnId>` 作键、与存储行的 `stored-N` 不匹配,导致每条 assistant 消息
    # (含最终答复)重复渲染两次。
    from iac_code.web.session_manager import compute_replay_sequence

    replay = compute_replay_sequence(
        latest_sequence=12,
        floor_sequence=1,
        is_pipeline=False,
        active_turn=False,
        active_turn_floor_sequence=None,
    )
    assert replay == 12


def test_compute_replay_sequence_active_normal_replays_only_active_turn() -> None:
    # 进行中的普通轮次:仅回放该轮次事件(其消息尚未全部持久化),已完成轮次不重放。
    from iac_code.web.session_manager import compute_replay_sequence

    replay = compute_replay_sequence(
        latest_sequence=12,
        floor_sequence=1,
        is_pipeline=False,
        active_turn=True,
        active_turn_floor_sequence=8,
    )
    assert replay == 8


def test_compute_replay_sequence_clamps_stale_active_turn_floor_to_buffer_floor() -> None:
    from iac_code.web.session_manager import compute_replay_sequence

    replay = compute_replay_sequence(
        latest_sequence=20,
        floor_sequence=12,
        is_pipeline=False,
        active_turn=True,
        active_turn_floor_sequence=3,
    )

    assert replay == 11


def test_compute_replay_sequence_active_normal_without_floor_falls_back_to_buffer_floor() -> None:
    from iac_code.web.session_manager import compute_replay_sequence

    replay = compute_replay_sequence(
        latest_sequence=12,
        floor_sequence=3,
        is_pipeline=False,
        active_turn=True,
        active_turn_floor_sequence=None,
    )
    assert replay == 2


def test_compute_replay_sequence_pipeline_keeps_buffer_replay() -> None:
    # 流水线会话依赖 floor 回放 + 稳定 id 去重重建转录,行为保持不变。
    from iac_code.web.session_manager import compute_replay_sequence

    replay = compute_replay_sequence(
        latest_sequence=12,
        floor_sequence=3,
        is_pipeline=True,
        active_turn=False,
        active_turn_floor_sequence=None,
    )
    assert replay == 2


def test_compute_replay_sequence_empty_buffer_returns_latest() -> None:
    from iac_code.web.session_manager import compute_replay_sequence

    replay = compute_replay_sequence(
        latest_sequence=0,
        floor_sequence=0,
        is_pipeline=False,
        active_turn=False,
        active_turn_floor_sequence=None,
    )
    assert replay == 0


def test_completed_normal_turn_reports_no_replay_on_reload(tmp_path) -> None:
    # 端到端:普通会话跑完一个轮次后(idle),to_dict 暴露的 replaySequence 必须等于
    # latestSequence,使前端重载时不回放已完成轮次 → 不再出现最终答复重复两次。
    import asyncio

    from iac_code.web.runtime import FakeStreamRuntime, WebTurnRequest
    from iac_code.web.session_manager import WebSessionManager

    async def run_turn() -> dict[str, object]:
        manager = WebSessionManager(projects_dir=tmp_path / "projects")
        session = manager.create_session(cwd=str(tmp_path / "project"), session_id="session-replay-1")
        runtime = FakeStreamRuntime(session, assistant_text="完成，`sleep 10` 已执行成功。")
        await runtime.start_turn(WebTurnRequest(text="执行一下 sleep 10", image_ids=[], file_refs=[]))
        return session.to_dict()

    snapshot = asyncio.run(run_turn())
    assert snapshot["latestSequence"] > 0
    # 修复前此处为 floor_sequence - 1(=0),会触发整段缓冲区回放导致重复渲染。
    assert snapshot["replaySequence"] == snapshot["latestSequence"]
    assert snapshot["context"]["replaySequence"] == snapshot["latestSequence"]
