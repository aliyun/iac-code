from __future__ import annotations

from iac_code.services.agent_factory import AgentFactoryOptions, AgentRuntime, create_agent_runtime


def _current_time_line(prompt: str) -> str:
    return next(line for line in prompt.splitlines() if line.startswith("- Current time: "))


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


def test_create_agent_runtime_adds_session_trusted_read_directories(tmp_path, monkeypatch):
    monkeypatch.setenv("IAC_CODE_CONFIG_DIR", str(tmp_path / "config"))

    from iac_code.services.agent_factory import AgentFactoryOptions, create_agent_runtime

    runtime = create_agent_runtime(
        AgentFactoryOptions(
            model="qwen3.7-max",
            session_id="session-42",
            cwd=str(tmp_path),
        )
    )

    roots = runtime.agent_loop._permission_context.trusted_read_directories
    assert str(tmp_path / "config" / "tool-results" / "session-42") in roots
    assert str(tmp_path / "config" / "image-cache" / "session-42") in roots


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


def test_create_agent_runtime_respects_disabled_skills(tmp_path, monkeypatch) -> None:
    from iac_code.skills.frontmatter import SkillFrontmatter
    from iac_code.skills.skill_definition import SkillDefinition
    from iac_code.types.skill_source import SkillSource

    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("IAC_CODE_CONFIG_DIR", str(tmp_path / "config"))

    enabled_skill = SkillDefinition(
        name="enabled-skill",
        description="Enabled skill",
        frontmatter=SkillFrontmatter(description="Enabled skill", auto_trigger={"script": "auto_trigger.py"}),
        content="Enabled body",
        source=SkillSource.PROJECT,
    )
    disabled_skill = SkillDefinition(
        name="disabled-skill",
        description="Disabled skill",
        frontmatter=SkillFrontmatter(description="Disabled skill", auto_trigger={"script": "auto_trigger.py"}),
        content="Disabled body",
        source=SkillSource.PROJECT,
    )

    monkeypatch.setattr(
        "iac_code.skills.discovery.discover_all_skills",
        lambda cwd: [enabled_skill, disabled_skill],
    )
    monkeypatch.setattr("iac_code.skills.settings.load_disabled_skills", lambda: {"disabled-skill"})

    captured_listing = {}

    def fake_build_skill_listing(commands):
        captured_listing["names"] = [command.name for command in commands]
        return "skill listing"

    monkeypatch.setattr("iac_code.skills.listing.build_skill_listing", fake_build_skill_listing)

    runtime = create_agent_runtime(
        AgentFactoryOptions(model="qwen3.6-plus", session_id="skill-runtime", cwd=str(tmp_path))
    )

    assert runtime.command_registry.get("enabled-skill") is not None
    assert runtime.command_registry.get("disabled-skill") is None
    assert captured_listing["names"] == ["enabled-skill"]
    assert [command.name for command in runtime.agent_loop._auto_trigger_skills] == ["enabled-skill"]

    skill_tool = runtime.tool_registry.get("skill")
    assert skill_tool is not None
    assert "disabled-skill" in skill_tool._disabled_skills


def test_create_agent_runtime_uses_project_memory_context(tmp_path, monkeypatch) -> None:
    from iac_code.memory.memory_manager import MemoryManager
    from iac_code.memory.project_memory import get_project_memory_dir

    project = tmp_path / "project"
    project.mkdir()
    config_dir = tmp_path / "config"
    monkeypatch.chdir(project)
    monkeypatch.setenv("IAC_CODE_CONFIG_DIR", str(config_dir))
    (config_dir).mkdir()
    (config_dir / "IAC-CODE.md").write_text("User memory instruction\n", encoding="utf-8")
    (project / "IAC-CODE.md").write_text("Project memory instruction\n", encoding="utf-8")
    topic_manager = MemoryManager(memory_dir=str(get_project_memory_dir(str(project))))
    topic_manager.save(
        "topic-a",
        "Topic body should not be always injected",
        memory_type="project",
        description="Topic A",
    )

    runtime = create_agent_runtime(AgentFactoryOptions(model="qwen3.6-plus", session_id="memory-runtime"))

    assert runtime.memory_manager._memory_dir == get_project_memory_dir(str(project))
    assert runtime.agent_loop._memory_recall_service is not None
    assert "User memory instruction" in runtime.agent_loop.system_prompt
    assert "Project memory instruction" in runtime.agent_loop.system_prompt
    assert "topic-a.md" in runtime.agent_loop.system_prompt
    assert "Topic body should not be always injected" not in runtime.agent_loop.system_prompt


def test_create_agent_runtime_exposes_legacy_memory_manager_for_hidden_command(tmp_path, monkeypatch) -> None:
    from iac_code.config import get_config_dir
    from iac_code.memory.project_memory import get_project_memory_dir

    project = tmp_path / "project"
    project.mkdir()
    config_dir = tmp_path / "config"
    monkeypatch.chdir(project)
    monkeypatch.setenv("IAC_CODE_CONFIG_DIR", str(config_dir))

    runtime = create_agent_runtime(AgentFactoryOptions(model="qwen3.6-plus", session_id="memory-runtime"))

    assert runtime.memory_manager._memory_dir == get_project_memory_dir(str(project))
    assert runtime.legacy_memory_manager._memory_dir == get_config_dir() / "memory"


def test_system_prompt_refresher_reuses_runtime_current_time(tmp_path, monkeypatch) -> None:
    from datetime import datetime as real_datetime

    from iac_code.agent import system_prompt

    class FakeDateTime:
        calls = 0

        @classmethod
        def now(cls):
            cls.calls += 1
            return real_datetime(2026, 6, 5, 10, cls.calls, 0)

    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("IAC_CODE_CONFIG_DIR", str(tmp_path / "config"))
    monkeypatch.setattr(system_prompt, "datetime", FakeDateTime)

    runtime = create_agent_runtime(
        AgentFactoryOptions(model="qwen3.7-max", session_id="time-stable", cwd=str(tmp_path))
    )

    initial_line = _current_time_line(runtime.agent_loop.system_prompt)
    refreshed_line = _current_time_line(runtime.agent_loop._system_prompt_refresher())

    assert refreshed_line == initial_line
