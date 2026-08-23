from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from iac_code import __version__
from iac_code.agent.agent_loop import AgentLoop
from iac_code.agent.message import ImageBlock, Message, TextBlock, ToolUseBlock
from iac_code.pipeline.engine.context import PipelineContext
from iac_code.pipeline.engine.pipeline_runner import PipelineRunner
from iac_code.pipeline.engine.step_executor import StepExecutor
from iac_code.pipeline.engine.step_spec import LoadedPipeline, StepSpec
from iac_code.pipeline.engine.transcript_storage import PipelineTranscriptStorage
from iac_code.services.permissions.pipeline import check_tool_permission
from iac_code.services.session_layout import SessionPaths, UnsupportedSessionLayoutError
from iac_code.services.session_metadata import SESSION_LAYOUT_VERSION_V2, SessionMetadata, write_session_metadata
from iac_code.services.session_storage import SessionStorage
from iac_code.services.session_usage import SessionUsageStore
from iac_code.tools.base import Tool, ToolContext, ToolRegistry, ToolResult
from iac_code.tools.bash.bash_tool import BashTool
from iac_code.types.permissions import PermissionAuditMetadata, PermissionResult, ToolPermissionContext
from iac_code.types.stream_events import (
    MessageEndEvent,
    MessageStartEvent,
    PermissionRequestEvent,
    ToolUseEndEvent,
    ToolUseStartEvent,
    Usage,
)


def _symlink_or_skip(target: Path, link: Path, *, target_is_directory: bool = False) -> None:
    try:
        link.symlink_to(target, target_is_directory=target_is_directory)
    except (NotImplementedError, OSError) as exc:
        pytest.skip(f"symlink creation unsupported: {exc}")


class _FakeProvider:
    def __init__(self) -> None:
        self._call_count = 0

    def get_model_name(self) -> str:
        return "fake-model"

    async def stream(self, messages, system, tools=None, max_tokens=8192):
        self._call_count += 1
        if self._call_count > 1:
            yield MessageStartEvent(message_id="msg-2")
            yield MessageEndEvent(stop_reason="end_turn", usage=Usage())
            return
        yield MessageStartEvent(message_id="msg-1")
        yield ToolUseStartEvent(tool_use_id="tool-1", name="fake_permission")
        yield ToolUseEndEvent(tool_use_id="tool-1", name="fake_permission", input={"payload": "value"})
        yield MessageEndEvent(stop_reason="tool_use", usage=Usage())


class _FakePermissionTool(Tool):
    @property
    def name(self) -> str:
        return "fake_permission"

    @property
    def description(self) -> str:
        return "Fake permission-controlled tool"

    @property
    def input_schema(self) -> dict[str, Any]:
        return {"type": "object", "properties": {"payload": {"type": "string"}}}

    async def execute(self, *, tool_input: dict[str, Any], context: ToolContext) -> ToolResult:
        return ToolResult.success("executed")

    async def check_permissions(self, input: dict, context=None) -> PermissionResult:
        return PermissionResult(
            behavior="ask",
            audit=PermissionAuditMetadata(
                scope="once",
                source="permission_pipeline",
                reason_type="needs_prompt",
                operation={"product": "ROS", "action": "CreateStack"},
            ),
        )


def test_append_and_load_roundtrip(tmp_path: Path):
    storage = PipelineTranscriptStorage(tmp_path / "pipeline")

    storage.append("/repo", "transcript_att_0001", Message(role="user", content="hello"), git_branch="main")
    storage.append(
        "/repo",
        "transcript_att_0001",
        Message(role="assistant", content=[TextBlock(text="hi")]),
        git_branch="main",
    )

    messages = storage.load("/repo", "transcript_att_0001")

    assert [message.role for message in messages] == ["user", "assistant"]
    assert messages[0].content == "hello"
    assert messages[1].get_text() == "hi"


def test_pipeline_transcript_round_trips_image_blocks(tmp_path: Path):
    storage = PipelineTranscriptStorage(tmp_path / "pipeline")
    messages = [
        Message(
            role="user",
            content=[TextBlock(text="diagram"), ImageBlock(media_type="image/png", data="aGVsbG8=")],
        )
    ]

    storage.save("/repo", "transcript_att_0001", messages)
    loaded = storage.load("/repo", "transcript_att_0001")

    assert loaded == messages


def test_transcript_lives_inside_sidecar(tmp_path: Path):
    storage = PipelineTranscriptStorage(tmp_path / "pipeline")

    storage.append("/repo", "transcript_att_0001", Message(role="user", content="hello"))

    assert storage.session_path("/repo", "transcript_att_0001") == (
        tmp_path / "pipeline" / "transcripts" / "transcript_att_0001" / "session.jsonl"
    )
    assert storage.session_dir("/repo", "transcript_att_0001") == (
        tmp_path / "pipeline" / "transcripts" / "transcript_att_0001"
    )


def test_transcript_refuses_dangling_symlinked_metadata_before_fallback(tmp_path: Path):
    session_dir = tmp_path / "session"
    session_dir.mkdir()
    _symlink_or_skip(tmp_path / "missing-metadata.json", session_dir / "metadata.json")
    outside = tmp_path / "outside-pipeline"
    outside.mkdir()
    _symlink_or_skip(outside, session_dir / "pipeline", target_is_directory=True)
    storage = PipelineTranscriptStorage(session_dir / "pipeline")

    with pytest.raises(UnsupportedSessionLayoutError, match="Unsupported session metadata"):
        storage.append("/repo", "transcript_att_0001", Message(role="user", content="hello"))

    assert not (outside / "transcripts" / "transcript_att_0001" / "session.jsonl").exists()


def test_load_skips_lite_meta_rows(tmp_path: Path):
    storage = PipelineTranscriptStorage(tmp_path / "pipeline")
    path = storage.session_path("/repo", "transcript_att_0001")
    path.parent.mkdir(parents=True)
    path.write_text(
        '{"type": "pipeline_init", "session_id": "transcript_att_0001"}\n'
        '{"role": "user", "content": "real", "session_id": "transcript_att_0001"}\n',
        encoding="utf-8",
    )

    messages = storage.load("/repo", "transcript_att_0001")

    assert len(messages) == 1
    assert messages[0].content == "real"


def test_append_stamps_required_fields(tmp_path: Path):
    storage = PipelineTranscriptStorage(tmp_path / "pipeline")

    storage.append("/repo", "transcript_att_0001", Message(role="user", content="hello"), git_branch="main")

    row = json.loads(storage.session_path("/repo", "transcript_att_0001").read_text(encoding="utf-8"))
    assert row["session_id"] == "transcript_att_0001"
    assert row["cwd"] == "/repo"
    assert row["git_branch"] == "main"
    assert row["version"] == __version__
    assert row["metadata"]["createdAt"].endswith("Z")


def test_save_stamps_created_at_on_every_message(tmp_path: Path):
    storage = PipelineTranscriptStorage(tmp_path / "pipeline")

    storage.save(
        "/repo",
        "transcript_att_0001",
        [Message(role="user", content="one"), Message(role="assistant", content="two")],
        git_branch="main",
    )

    lines = storage.session_path("/repo", "transcript_att_0001").read_text(encoding="utf-8").splitlines()
    rows = [json.loads(line) for line in lines if line]
    assert len(rows) == 2
    for row in rows:
        assert row["metadata"]["createdAt"].endswith("Z")


def test_append_meta_requires_type(tmp_path: Path):
    storage = PipelineTranscriptStorage(tmp_path / "pipeline")

    try:
        storage.append_meta("/repo", "transcript_att_0001", {"last_prompt": "missing type"})
    except ValueError as exc:
        assert "meta_entry must include a 'type' field" in str(exc)
    else:
        raise AssertionError("meta row without type was accepted")


def test_append_meta_stamps_session_and_is_skipped_by_load(tmp_path: Path):
    storage = PipelineTranscriptStorage(tmp_path / "pipeline")

    storage.append_meta("/repo", "transcript_att_0001", {"type": "pipeline_init"})

    row = json.loads(storage.session_path("/repo", "transcript_att_0001").read_text(encoding="utf-8"))
    assert row["type"] == "pipeline_init"
    assert row["session_id"] == "transcript_att_0001"
    assert row["createdAt"].endswith("Z")
    assert storage.load("/repo", "transcript_att_0001") == []


def test_save_overwrites_and_stamps_messages(tmp_path: Path):
    storage = PipelineTranscriptStorage(tmp_path / "pipeline")
    storage.append("/repo", "transcript_att_0001", Message(role="user", content="old"), git_branch="old-branch")

    storage.save(
        "/repo",
        "transcript_att_0001",
        [
            Message(role="user", content="new"),
            Message(role="assistant", content=[TextBlock(text="saved")]),
        ],
        git_branch="main",
    )

    rows = [
        json.loads(line)
        for line in storage.session_path("/repo", "transcript_att_0001").read_text(encoding="utf-8").splitlines()
    ]
    assert [row["role"] for row in rows] == ["user", "assistant"]
    assert [row["content"] for row in rows] == ["new", [{"type": "text", "text": "saved"}]]
    assert all(row["session_id"] == "transcript_att_0001" for row in rows)
    assert all(row["cwd"] == "/repo" for row in rows)
    assert all(row["git_branch"] == "main" for row in rows)
    assert all(row["version"] == __version__ for row in rows)

    messages = storage.load("/repo", "transcript_att_0001")
    assert [message.get_text() for message in messages] == ["new", "saved"]


def test_exists_tracks_session_path(tmp_path: Path):
    storage = PipelineTranscriptStorage(tmp_path / "pipeline")

    assert storage.exists("/repo", "transcript_att_0001") is False

    storage.append("/repo", "transcript_att_0001", Message(role="user", content="hello"))

    assert storage.exists("/repo", "transcript_att_0001") is True


def test_load_ignores_symlinked_transcript_leaf(tmp_path: Path):
    storage = PipelineTranscriptStorage(tmp_path / "pipeline")
    path = storage.session_path("/repo", "transcript_att_0001")
    path.parent.mkdir(parents=True)
    outside = tmp_path / "outside-session.jsonl"
    outside.write_text('{"role":"user","content":"outside"}\n', encoding="utf-8")
    _symlink_or_skip(outside, path)

    assert storage.exists("/repo", "transcript_att_0001") is False
    assert storage.load("/repo", "transcript_att_0001") == []


def test_load_reraises_regular_transcript_leaf_read_errors(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    storage = PipelineTranscriptStorage(tmp_path / "pipeline")
    path = storage.session_path("/repo", "transcript_att_0001")
    path.parent.mkdir(parents=True)
    path.write_text('{"role":"user","content":"hello"}\n', encoding="utf-8")

    def fail_open(*_args, **_kwargs):
        raise PermissionError("locked")

    monkeypatch.setattr("iac_code.pipeline.engine.transcript_storage.open_text_no_follow", fail_open)

    with pytest.raises(PermissionError, match="locked"):
        storage.load("/repo", "transcript_att_0001")


def test_repair_interrupted_delegates_to_session_storage(tmp_path: Path):
    storage = PipelineTranscriptStorage(tmp_path / "pipeline")
    messages = [
        Message(role="user", content="run tool"),
        Message(role="assistant", content=[ToolUseBlock(id="tu_1", name="complete_step", input={})]),
    ]

    repaired = storage.repair_interrupted(messages)

    assert len(repaired) == 3
    assert repaired[-1].role == "user"
    assert repaired[-1].content[0].tool_use_id == "tu_1"
    assert repaired[-1].content[0].is_error is True


def test_rejects_unsafe_transcript_id(tmp_path: Path):
    storage = PipelineTranscriptStorage(tmp_path / "pipeline")

    try:
        storage.append("/repo", "../escape", Message(role="user", content="bad"))
    except ValueError as exc:
        assert "unsafe transcript id" in str(exc)
    else:
        raise AssertionError("unsafe transcript id was accepted")


def test_agent_loop_accepts_transcript_runtime_paths(tmp_path: Path):
    transcript_dir = tmp_path / "pipeline" / "transcripts" / "transcript_att_0001"
    usage_path = transcript_dir / "usage.jsonl"
    result_storage_dir = transcript_dir / "tool-results"
    audit_log_path = transcript_dir / "permission-audit.jsonl"

    loop = AgentLoop(
        provider_manager=SimpleNamespace(get_model_name=lambda: "fake-model"),
        system_prompt="system",
        tool_registry=ToolRegistry(),
        session_id="transcript_att_0001",
        root_session_id="root-session",
        transcript_id="transcript_att_0001",
        result_storage_dir=result_storage_dir,
        audit_log_path=audit_log_path,
        session_usage_store=SessionUsageStore(path_provider=lambda _cwd, _session_id: usage_path),
        cwd="/repo",
    )

    assert loop.session_id == "transcript_att_0001"
    assert loop._root_session_id == "root-session"
    assert loop._transcript_id == "transcript_att_0001"
    assert loop._audit_log_path == str(audit_log_path)
    assert loop._result_storage._storage_dir == str(result_storage_dir)
    assert loop._session_usage_store.append("/repo", loop.session_id, Usage(input_tokens=1, output_tokens=2))
    assert usage_path.exists()


def test_pipeline_runner_loads_legacy_transcript_session_when_no_v2_sidecar(tmp_path: Path):
    storage = SessionStorage(projects_dir=tmp_path / "projects")
    legacy_path = storage.legacy_session_path("/repo", "transcript_att_0001")
    legacy_path.parent.mkdir(parents=True)
    legacy_path.write_text(
        '{"role":"assistant","content":[{"type":"tool_use","id":"tu_1","name":"complete_step","input":{}}]}\n',
        encoding="utf-8",
    )
    runner = PipelineRunner.__new__(PipelineRunner)
    runner._cwd = "/repo"
    runner._transcript_storage = None
    runner._session_storage = storage

    messages = runner._load_repaired_resume_messages("transcript_att_0001")

    assert messages is not None
    assert [message.role for message in messages] == ["assistant", "user"]


@pytest.mark.asyncio
async def test_step_executor_routes_transcript_runtime_paths(tmp_path: Path):
    (tmp_path / "prompts").mkdir()
    (tmp_path / "prompts" / "step.md").write_text("Run step.", encoding="utf-8")
    root_storage = SessionStorage(projects_dir=tmp_path / "projects")
    root_session_dir = root_storage.session_dir("/repo", "root-session")
    write_session_metadata(
        root_session_dir,
        SessionMetadata(session_id="root-session", cwd="/repo", layout_version=SESSION_LAYOUT_VERSION_V2),
    )
    transcript_storage = PipelineTranscriptStorage(tmp_path / "detached-pipeline")
    registry = ToolRegistry()
    registry.register(_FakePermissionTool())
    step = StepSpec(step_id="step", conclusion_field="out", forward=None, prompt_file="prompts/step.md")
    pipeline = LoadedPipeline(
        name="test",
        steps=[step],
        context_dependencies={"out": []},
        max_rollbacks=1,
        skills={},
    )
    executor = StepExecutor(
        provider_manager=_FakeProvider(),
        base_tool_registry=registry,
        pipeline=pipeline,
        pipeline_dir=tmp_path,
        session_storage=transcript_storage,
        root_session_storage=root_storage,
        cwd="/repo",
        permission_context_getter=lambda: ToolPermissionContext(cwd="/repo"),
    )

    agent_context = executor.build_agent_loop_context(
        step,
        PipelineContext({"out": []}),
        "root-session",
        transcript_id="transcript_att_0001",
    )

    loop = agent_context.agent_loop
    assert loop is not None
    transcript_dir = SessionPaths.from_session_dir(root_session_dir).transcript_dir("transcript_att_0001")
    assert loop.session_id == "transcript_att_0001"
    assert loop._root_session_id == "root-session"
    assert loop._transcript_id == "transcript_att_0001"
    assert loop._result_storage._storage_dir == str(transcript_dir / "tool-results")
    assert loop._audit_log_path == str(transcript_dir / "permission-audit.jsonl")
    assert loop._session_usage_store.append("/repo", loop.session_id, Usage(input_tokens=3, output_tokens=4))
    assert (transcript_dir / "usage.jsonl").exists()
    assert not (tmp_path / "detached-pipeline" / "transcripts" / "transcript_att_0001" / "usage.jsonl").exists()

    permission_events = []
    async for event in loop.run_streaming("run fake tool"):
        if isinstance(event, PermissionRequestEvent):
            permission_events.append(event)
            event.response_future.set_result(False)

    [permission_event] = permission_events
    assert permission_event.audit_context["root_session_id"] == "root-session"
    assert permission_event.audit_context["transcript_id"] == "transcript_att_0001"
    assert permission_event.audit_context["audit_log_path"] == str(transcript_dir / "permission-audit.jsonl")


@pytest.mark.asyncio
async def test_step_executor_trusts_current_transcript_artifacts_for_bash_reads(tmp_path: Path):
    (tmp_path / "prompts").mkdir()
    (tmp_path / "prompts" / "step.md").write_text("Run step.", encoding="utf-8")
    root_storage = SessionStorage(projects_dir=tmp_path / "projects")
    root_session_dir = root_storage.session_dir("/repo", "root-session")
    write_session_metadata(
        root_session_dir,
        SessionMetadata(session_id="root-session", cwd="/repo", layout_version=SESSION_LAYOUT_VERSION_V2),
    )
    transcript_storage = PipelineTranscriptStorage(tmp_path / "detached-pipeline")
    step = StepSpec(step_id="step", conclusion_field="out", forward=None, prompt_file="prompts/step.md")
    pipeline = LoadedPipeline(
        name="test",
        steps=[step],
        context_dependencies={"out": []},
        max_rollbacks=1,
        skills={},
    )
    executor = StepExecutor(
        provider_manager=_FakeProvider(),
        base_tool_registry=ToolRegistry(),
        pipeline=pipeline,
        pipeline_dir=tmp_path,
        session_storage=transcript_storage,
        root_session_storage=root_storage,
        cwd="/repo",
        permission_context_getter=lambda: ToolPermissionContext(cwd="/repo"),
    )

    agent_context = executor.build_agent_loop_context(
        step,
        PipelineContext({"out": []}),
        "root-session",
        transcript_id="transcript_att_0001",
    )

    loop = agent_context.agent_loop
    assert loop is not None
    session_paths = SessionPaths.require_supported(root_session_dir)
    transcript_tool_results_dir = session_paths.transcript_tool_results_dir("transcript_att_0001")
    transcript_image_cache_dir = session_paths.transcript_image_cache_dir("transcript_att_0001")
    result_file = transcript_tool_results_dir / "tool-1.txt"
    result_file.parent.mkdir(parents=True, exist_ok=True)
    result_file.write_text("saved result", encoding="utf-8")
    image_file = transcript_image_cache_dir / "1.png"
    image_file.parent.mkdir(parents=True, exist_ok=True)
    image_file.write_bytes(b"png")

    result_permission = await check_tool_permission(
        BashTool(),
        {"command": f"cat {result_file}"},
        ToolPermissionContext(
            cwd="/repo",
            trusted_read_directories=list(loop._tool_context_trusted_read_directories),
        ),
    )
    image_permission = await check_tool_permission(
        BashTool(),
        {"command": f"cat {image_file}"},
        ToolPermissionContext(
            cwd="/repo",
            trusted_read_directories=list(loop._tool_context_trusted_read_directories),
        ),
    )

    assert str(transcript_tool_results_dir) in loop._tool_context_trusted_read_directories
    assert str(transcript_image_cache_dir) in loop._tool_context_trusted_read_directories
    assert str(root_session_dir) not in loop._tool_context_trusted_read_directories
    assert str(transcript_tool_results_dir.parent) not in loop._tool_context_trusted_read_directories
    assert result_permission.behavior == "allow"
    assert image_permission.behavior == "allow"


def test_step_executor_keeps_legacy_root_on_legacy_runtime_paths(tmp_path: Path):
    (tmp_path / "prompts").mkdir()
    (tmp_path / "prompts" / "step.md").write_text("Run step.", encoding="utf-8")
    root_storage = SessionStorage(projects_dir=tmp_path / "projects")
    root_session_dir = root_storage.session_dir("/repo", "root-session")
    root_session_dir.mkdir(parents=True)
    registry = ToolRegistry()
    registry.register(_FakePermissionTool())
    step = StepSpec(step_id="step", conclusion_field="out", forward=None, prompt_file="prompts/step.md")
    pipeline = LoadedPipeline(
        name="test",
        steps=[step],
        context_dependencies={"out": []},
        max_rollbacks=1,
        skills={},
    )
    executor = StepExecutor(
        provider_manager=_FakeProvider(),
        base_tool_registry=registry,
        pipeline=pipeline,
        pipeline_dir=tmp_path,
        session_storage=root_storage,
        root_session_storage=root_storage,
        cwd="/repo",
        permission_context_getter=lambda: ToolPermissionContext(cwd="/repo"),
    )

    agent_context = executor.build_agent_loop_context(
        step,
        PipelineContext({"out": []}),
        "root-session",
        transcript_id="transcript_att_0001",
    )

    loop = agent_context.agent_loop
    assert loop is not None
    assert Path(loop._result_storage._storage_dir).parts[-2:] == ("tool-results", "transcript_att_0001")
    assert loop._audit_log_path is None
    assert not (root_session_dir / "pipeline" / "transcripts" / "transcript_att_0001").exists()


def test_save_accepts_preserve_cleanup_prompts(tmp_path: Path):
    from iac_code.agent.message import create_compaction_summary_message

    storage = PipelineTranscriptStorage(tmp_path / "pipeline")
    messages = [
        Message(role="user", content="hi"),
        create_compaction_summary_message("summary text"),
    ]

    # 不得抛 TypeError
    storage.save("/repo", "transcript_att_0001", messages, git_branch="main", preserve_cleanup_prompts=True)

    loaded = storage.load("/repo", "transcript_att_0001")
    assert any(message.get_text().startswith("[Conversation Summary]") for message in loaded)


def test_save_preserves_prior_cleanup_prompt_once(tmp_path: Path):
    from iac_code.pipeline.constants import CLEANUP_PROMPT_METADATA_TYPE

    storage = PipelineTranscriptStorage(tmp_path / "pipeline")
    cleanup = Message(role="user", content="C", metadata={"type": CLEANUP_PROMPT_METADATA_TYPE})
    storage.save("/repo", "transcript_att_0001", [cleanup, Message(role="assistant", content="a")])

    # 第二次保存不含 cleanup,但 preserve=True 应把旧 cleanup 合并回,且只一份
    storage.save(
        "/repo",
        "transcript_att_0001",
        [Message(role="assistant", content="b")],
        preserve_cleanup_prompts=True,
    )

    loaded = storage.load("/repo", "transcript_att_0001")
    assert sum(1 for message in loaded if message.metadata.get("type") == CLEANUP_PROMPT_METADATA_TYPE) == 1
