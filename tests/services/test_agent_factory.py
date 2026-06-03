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
