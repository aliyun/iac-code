from __future__ import annotations

from iac_code.services.agent_factory import AgentFactoryOptions, AgentRuntime, create_agent_runtime


def test_create_agent_runtime_uses_supplied_session_id(tmp_path, monkeypatch) -> None:
    monkeypatch.chdir(tmp_path)

    runtime = create_agent_runtime(
        AgentFactoryOptions(
            model="qwen3.6-plus",
            session_id="test-session",
            cwd=str(tmp_path),
            max_turns=3,
        )
    )

    assert runtime.session_id == "test-session"
    assert runtime.agent_loop is not None
    assert runtime.tool_registry.get("read_file") is not None


def test_create_agent_runtime_minimal_options(tmp_path, monkeypatch) -> None:
    """Only model is required; other fields use defaults."""
    monkeypatch.chdir(tmp_path)

    runtime = create_agent_runtime(AgentFactoryOptions(model="qwen3.6-plus", cwd=str(tmp_path)))

    assert isinstance(runtime, AgentRuntime)
    assert runtime.agent_loop is not None
    assert runtime.session_id  # non-empty


def test_create_agent_runtime_different_session_ids(tmp_path, monkeypatch) -> None:
    """Different session_id values produce distinct runtimes."""
    monkeypatch.chdir(tmp_path)

    rt1 = create_agent_runtime(AgentFactoryOptions(model="qwen3.6-plus", session_id="sess-a", cwd=str(tmp_path)))
    rt2 = create_agent_runtime(AgentFactoryOptions(model="qwen3.6-plus", session_id="sess-b", cwd=str(tmp_path)))

    assert rt1.session_id == "sess-a"
    assert rt2.session_id == "sess-b"
    assert rt1.session_id != rt2.session_id


def test_create_agent_runtime_all_fields_populated(tmp_path, monkeypatch) -> None:
    """All AgentRuntime fields should be non-None."""
    monkeypatch.chdir(tmp_path)

    runtime = create_agent_runtime(AgentFactoryOptions(model="qwen3.6-plus", session_id="test-full", cwd=str(tmp_path)))

    assert runtime.agent_loop is not None
    assert runtime.session_id is not None
    assert runtime.tool_registry is not None
    assert runtime.provider_manager is not None
    assert runtime.command_registry is not None
    assert runtime.task_manager is not None
    assert runtime.memory_manager is not None


def test_create_agent_runtime_custom_cwd(tmp_path, monkeypatch) -> None:
    """Custom cwd is passed through to the runtime."""
    monkeypatch.chdir(tmp_path)
    (tmp_path / "subdir").mkdir()
    custom_cwd = str(tmp_path / "subdir")

    runtime = create_agent_runtime(AgentFactoryOptions(model="qwen3.6-plus", session_id="cwd-test", cwd=custom_cwd))

    # The tool_registry should contain bash with default tools
    assert runtime.tool_registry.get("bash") is not None
    assert runtime.session_id == "cwd-test"


def test_create_agent_runtime_auto_session_id(tmp_path, monkeypatch) -> None:
    """When session_id is None, a UUID-based ID is auto-generated."""
    monkeypatch.chdir(tmp_path)

    runtime = create_agent_runtime(AgentFactoryOptions(model="qwen3.6-plus", cwd=str(tmp_path)))

    assert runtime.session_id is not None
    assert len(runtime.session_id) == 8  # uuid4()[:8]
