from __future__ import annotations

import base64
import logging
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from iac_code.services.agent_factory import AgentFactoryOptions, AgentRuntime, create_agent_runtime
from iac_code.services.session_metadata import (
    SESSION_LAYOUT_VERSION_V2,
    SessionMetadata,
    read_session_metadata,
    write_session_metadata,
)
from iac_code.services.session_storage import SessionStorage


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


def test_create_agent_runtime_logs_session_start_parameters(tmp_path, monkeypatch, caplog) -> None:
    monkeypatch.setenv("IAC_CODE_CONFIG_DIR", str(tmp_path / "config"))
    monkeypatch.setenv("IAC_CODE_PROVIDER", "dashscope")
    monkeypatch.setenv("IAC_CODE_API_KEY", "fake-key")
    monkeypatch.chdir(tmp_path)
    caplog.set_level(logging.INFO, logger="iac_code.services.session_logging")

    create_agent_runtime(
        AgentFactoryOptions(
            model="qwen3.6-plus",
            session_id="logged-session",
            cwd=str(tmp_path),
            max_turns=3,
            source="test",
        )
    )

    assert "Session started" in caplog.text
    assert "session_id=logged-session" in caplog.text
    assert "source=test" in caplog.text
    assert "provider=dashscope" in caplog.text
    assert "model=qwen3.6-plus" in caplog.text
    assert "endpoint_origin=https://dashscope.aliyuncs.com" in caplog.text
    assert "endpoint_custom=false" in caplog.text
    assert "max_turns=3" in caplog.text
    assert "tool_count=" in caplog.text
    assert "mcp_server_count=0" in caplog.text


def test_create_agent_runtime_ignores_session_start_logging_failure(tmp_path, monkeypatch, caplog) -> None:
    monkeypatch.setenv("IAC_CODE_CONFIG_DIR", str(tmp_path / "config"))
    monkeypatch.setenv("IAC_CODE_PROVIDER", "dashscope")
    monkeypatch.setenv("IAC_CODE_API_KEY", "fake-key")
    monkeypatch.chdir(tmp_path)
    caplog.set_level(logging.INFO, logger="iac_code.services.session_logging")

    def fail_session_start_settings(self) -> dict[str, object]:
        raise RuntimeError("session logging failed")

    monkeypatch.setattr(
        "iac_code.providers.manager.ProviderManager.session_start_settings",
        fail_session_start_settings,
    )

    runtime = create_agent_runtime(
        AgentFactoryOptions(
            model="qwen3.6-plus",
            session_id="logging-failure-session",
            cwd=str(tmp_path),
        )
    )

    assert runtime.session_id == "logging-failure-session"
    assert runtime.agent_loop is not None


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
    session_dir = runtime.agent_loop._session_storage.session_dir(str(tmp_path), "session-42")
    assert str(tmp_path / "config" / "tool-results" / "session-42") in roots
    assert str(tmp_path / "config" / "image-cache" / "session-42") in roots
    assert str(session_dir / "tool-results") in roots
    assert str(session_dir / "image-cache") in roots


def test_create_agent_runtime_result_storage_uses_v2_session_dir(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("IAC_CODE_CONFIG_DIR", str(tmp_path / "config"))
    session_id = "session-results"
    storage = SessionStorage()
    session_dir = storage.session_dir(str(tmp_path), session_id)
    write_session_metadata(
        session_dir,
        SessionMetadata(session_id=session_id, cwd=str(tmp_path), layout_version=SESSION_LAYOUT_VERSION_V2),
    )

    runtime = create_agent_runtime(
        AgentFactoryOptions(
            model="qwen3.7-max",
            session_id=session_id,
            cwd=str(tmp_path),
        )
    )

    assert Path(runtime.agent_loop._result_storage._storage_dir) == session_dir / "tool-results"


def test_create_agent_runtime_prepares_new_session_as_v2_before_first_turn(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("IAC_CODE_CONFIG_DIR", str(tmp_path / "config"))
    session_id = "new-session-results"
    storage = SessionStorage()
    session_dir = storage.session_dir(str(tmp_path), session_id)

    runtime = create_agent_runtime(
        AgentFactoryOptions(
            model="qwen3.7-max",
            session_id=session_id,
            cwd=str(tmp_path),
        )
    )

    assert Path(runtime.agent_loop._result_storage._storage_dir) == session_dir / "tool-results"
    metadata = read_session_metadata(session_dir)
    assert metadata is not None
    assert metadata.layout_version == SESSION_LAYOUT_VERSION_V2


def test_create_agent_runtime_result_storage_uses_legacy_global_dir_without_session_metadata(
    tmp_path,
    monkeypatch,
) -> None:
    monkeypatch.setenv("IAC_CODE_CONFIG_DIR", str(tmp_path / "config"))
    session_id = "legacy-session-results"
    storage = SessionStorage()
    session_dir = storage.session_dir(str(tmp_path), session_id)
    session_dir.mkdir(parents=True)
    (session_dir / "session.jsonl").write_text("", encoding="utf-8")

    runtime = create_agent_runtime(
        AgentFactoryOptions(
            model="qwen3.7-max",
            session_id=session_id,
            cwd=str(tmp_path),
        )
    )

    assert Path(runtime.agent_loop._result_storage._storage_dir) == tmp_path / "config" / "tool-results" / session_id
    assert Path(runtime.agent_loop._result_storage._storage_dir) != session_dir / "tool-results"


@pytest.mark.asyncio
async def test_create_agent_runtime_mcp_binary_output_uses_session_dir(tmp_path, monkeypatch):
    from iac_code.mcp.types import MCPToolRecord
    from iac_code.tools.base import ToolContext

    class BinaryMCPManager:
        async def connect_all(self) -> None:
            pass

        def list_tools(self) -> list[MCPToolRecord]:
            return [
                MCPToolRecord(
                    server_name="ros",
                    tool_name="render",
                    public_name="mcp__ros__render",
                    input_schema={"type": "object"},
                )
            ]

        def list_resources(self) -> list:
            return []

        def list_prompts(self) -> list:
            return []

        def list_connections(self) -> list:
            return []

        def needs_auth_servers(self) -> list[str]:
            return []

        async def call_tool(self, *args, **kwargs):
            return {
                "content": [
                    {
                        "type": "image",
                        "data": base64.b64encode(b"factory-png").decode("ascii"),
                        "mimeType": "image/png",
                    }
                ]
            }

    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("IAC_CODE_CONFIG_DIR", str(tmp_path / "config"))
    manager = BinaryMCPManager()

    runtime = create_agent_runtime(
        AgentFactoryOptions(
            model="qwen3.7-max",
            session_id="session-1",
            cwd=str(tmp_path),
            mcp_configs=[{"name": "ros", "command": "uvx"}],
            mcp_manager_factory=lambda configs, roots: manager,
        )
    )

    tool = runtime.tool_registry.get("mcp__ros__render")
    result = await tool.execute(tool_input={}, context=ToolContext())

    session_dir = runtime.agent_loop._session_storage.session_dir(str(tmp_path), "session-1")
    artifact_path = result.metadata["mcp"]["artifacts"][0]["path"]
    assert artifact_path.startswith(str(session_dir / "tool-results" / "mcp" / "ros" / "render"))
    assert not (tmp_path / "config" / "tool-results" / "session-1").exists()


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


def test_create_agent_runtime_a2a_safe_mode_filters_tools_and_skips_mcp(tmp_path, monkeypatch) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("IAC_CODE_CONFIG_DIR", str(tmp_path / "config"))
    monkeypatch.setattr(
        "iac_code.services.cloud_credentials.CloudCredentials.has_provider",
        lambda self, provider: provider == "aliyun",
    )
    mcp_factory_called = False

    def mcp_manager_factory(configs, roots):
        nonlocal mcp_factory_called
        mcp_factory_called = True
        raise AssertionError("safe mode must not connect MCP servers")

    runtime = create_agent_runtime(
        AgentFactoryOptions(
            model="qwen3.7-max",
            session_id="safe-session",
            cwd=str(tmp_path),
            mcp_configs=[{"name": "ros", "command": "uvx"}],
            mcp_manager_factory=mcp_manager_factory,
            a2a_safe_mode=True,
        )
    )

    names = {tool.name for tool in runtime.tool_registry.list_tools()}
    assert {
        "read_file",
        "list_files",
        "glob",
        "grep",
        "aliyun_api",
        "aliyun_doc_search",
        "aliyun_api_doc",
        "ros_stack",
        "ros_stack_instances",
        "ros_stack_group",
        "ros_template",
        "ros_template_scratch",
        "ros_diagnostic",
        "ros_resource_type_registration",
        "ros_tag",
        "skill",
        "read_memory",
        "write_memory",
    }.issubset(names)
    assert not {
        "bash",
        "write_file",
        "edit_file",
        "web_fetch",
        "agent",
        "task_list",
        "task_get",
        "task_stop",
        "list_mcp_resources",
        "read_mcp_resource",
    }.intersection(names)
    assert not any(name.startswith("mcp__") for name in names)
    assert runtime.mcp_manager is None
    assert mcp_factory_called is False

    permission_context = runtime.agent_loop._permission_context
    session_dir = runtime.agent_loop._session_storage.session_dir(str(tmp_path), "safe-session")
    assert permission_context.read_path_violation_behavior == "deny"
    assert str(tmp_path) in permission_context.strict_read_directories
    assert str(session_dir) in permission_context.strict_read_directories


def test_create_agent_runtime_disable_external_services_skips_mcp_and_keyring(tmp_path, monkeypatch) -> None:
    """离线核算契约:置 disable_external_services 后不构造/连接 MCP、不读 MCP 钥匙串,

    但保留完整本地工具集(区别于 a2a_safe_mode 的裁剪),以保证系统提示 + 工具定义 token 口径准确。
    """
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("IAC_CODE_CONFIG_DIR", str(tmp_path / "config"))
    monkeypatch.setattr(
        "iac_code.services.cloud_credentials.CloudCredentials.has_provider",
        lambda self, provider: provider == "aliyun",
    )

    def mcp_manager_factory(configs, roots):
        raise AssertionError("offline accounting must not construct or connect MCP servers")

    keyring_reads: list[str] = []
    monkeypatch.setattr(
        "iac_code.mcp.storage.MCPSecretStorage.get_secret",
        lambda self, key: keyring_reads.append(key),
    )

    runtime = create_agent_runtime(
        AgentFactoryOptions(
            model="qwen3.7-max",
            session_id="accounting-session",
            cwd=str(tmp_path),
            mcp_configs=[{"name": "ros", "command": "uvx"}],
            mcp_manager_factory=mcp_manager_factory,
            disable_external_services=True,
        )
    )

    # 没有任何 MCP 副作用。
    assert runtime.mcp_manager is None
    assert keyring_reads == []

    # 完整本地工具集仍在(a2a_safe_mode 会裁掉这些),token 口径才准确。
    names = {tool.name for tool in runtime.tool_registry.list_tools()}
    assert {"read_file", "bash", "write_file", "edit_file", "agent", "task_list"}.issubset(names)
    assert not any(name.startswith("mcp__") for name in names)

    # 不复用 a2a 的严格读目录/拒绝行为——离线核算只关闭外部连接,不改权限语义。
    permission_context = runtime.agent_loop._permission_context
    assert permission_context.read_path_violation_behavior != "deny"


def test_a2a_safe_mode_keeps_cloud_tool_refresh_filtered(tmp_path, monkeypatch) -> None:
    from iac_code.a2a.runtime_overrides import refresh_runtime_cloud_tools

    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("IAC_CODE_CONFIG_DIR", str(tmp_path / "config"))
    monkeypatch.setattr(
        "iac_code.services.cloud_credentials.CloudCredentials.has_provider",
        lambda self, provider: provider == "aliyun",
    )

    runtime = create_agent_runtime(
        AgentFactoryOptions(
            model="qwen3.7-max",
            session_id="safe-session",
            cwd=str(tmp_path),
            a2a_safe_mode=True,
        )
    )

    assert runtime.tool_registry.get("aliyun_doc_search") is not None
    assert runtime.tool_registry.get("aliyun_api_doc") is not None
    assert runtime.tool_registry.get("aliyun_api") is not None
    assert runtime.tool_registry.get("ros_stack") is not None
    assert runtime.tool_registry.get("ros_stack_instances") is not None
    assert runtime.tool_registry.get("ros_stack_group") is not None
    assert runtime.tool_registry.get("ros_template") is not None
    assert runtime.tool_registry.get("ros_template_scratch") is not None
    assert runtime.tool_registry.get("ros_diagnostic") is not None
    assert runtime.tool_registry.get("ros_resource_type_registration") is not None
    assert runtime.tool_registry.get("ros_tag") is not None

    refresh_runtime_cloud_tools(runtime)

    assert runtime.tool_registry.get("aliyun_doc_search") is not None
    assert runtime.tool_registry.get("aliyun_api_doc") is not None
    assert runtime.tool_registry.get("aliyun_api") is not None
    assert runtime.tool_registry.get("ros_stack") is not None
    assert runtime.tool_registry.get("ros_stack_instances") is not None
    assert runtime.tool_registry.get("ros_stack_group") is not None
    assert runtime.tool_registry.get("ros_template") is not None
    assert runtime.tool_registry.get("ros_template_scratch") is not None
    assert runtime.tool_registry.get("ros_diagnostic") is not None
    assert runtime.tool_registry.get("ros_resource_type_registration") is not None
    assert runtime.tool_registry.get("ros_tag") is not None


@pytest.mark.asyncio
async def test_a2a_safe_mode_allows_existing_legacy_session_dir(tmp_path, monkeypatch) -> None:
    from iac_code.services.permissions.pipeline import check_tool_permission

    monkeypatch.setenv("IAC_CODE_CONFIG_DIR", str(tmp_path / "config"))
    session_id = "legacy-safe-session"
    storage = SessionStorage()
    session_dir = storage.session_dir(str(tmp_path), session_id)
    session_dir.mkdir(parents=True)
    (session_dir / "session.jsonl").write_text("", encoding="utf-8")
    session_file = session_dir / "artifact.txt"
    session_file.write_text("artifact", encoding="utf-8")

    runtime = create_agent_runtime(
        AgentFactoryOptions(
            model="qwen3.7-max",
            session_id=session_id,
            cwd=str(tmp_path),
            a2a_safe_mode=True,
        )
    )

    permission_context = runtime.agent_loop._permission_context
    assert str(session_dir) in permission_context.strict_read_directories
    permission = await check_tool_permission(
        runtime.tool_registry.get("read_file"),
        {"path": str(session_file)},
        permission_context,
    )

    assert permission.behavior == "allow"


def test_create_agent_runtime_uses_project_memory_context(tmp_path, monkeypatch) -> None:
    from iac_code.memory.memory_manager import MemoryManager
    from iac_code.memory.project_memory import get_project_memory_dir

    project = tmp_path / "project"
    project.mkdir()
    config_dir = tmp_path / "config"
    monkeypatch.chdir(project)
    monkeypatch.setenv("IAC_CODE_CONFIG_DIR", str(config_dir))
    (config_dir).mkdir()
    (config_dir / "AGENTS.md").write_text("User memory instruction\n", encoding="utf-8")
    (project / "AGENTS.md").write_text("Project memory instruction\n", encoding="utf-8")
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
    assert "topic-a.md" not in runtime.agent_loop.system_prompt
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


def test_session_provider_override_wins_over_global_qwenpaw(tmp_path, monkeypatch) -> None:
    """A session-level provider override must beat a globally-active QwenPaw partner source.

    Repro of the web bug: the user activates QwenPaw (global ``llm_source=qwenpaw``), it fails,
    then switches the session to a regular provider (``session.provider=dashscope``). The session's
    explicit choice must take effect instead of being force-routed back through the broken QwenPaw
    endpoint. QwenPaw resolves to the *same* provider_key but with its own model/base_url, so the
    tell is that model/base_url stay the session's, not QwenPaw's.
    """
    from iac_code.services.qwenpaw_source import QwenPawConfig

    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("IAC_CODE_CONFIG_DIR", str(tmp_path / "config"))

    monkeypatch.setattr("iac_code.config.get_llm_source", lambda: "qwenpaw")

    def _fake_load_from_qwenpaw() -> QwenPawConfig:
        return QwenPawConfig(
            model="qwenpaw-model",
            provider_key="dashscope",
            api_key="fake-qwenpaw-key",
            base_url="https://qwenpaw.invalid/v1",
        )

    monkeypatch.setattr("iac_code.services.qwenpaw_source.load_from_qwenpaw", _fake_load_from_qwenpaw)

    runtime = create_agent_runtime(
        AgentFactoryOptions(
            model="qwen3.7-max",
            session_id="override-wins",
            cwd=str(tmp_path),
            provider_key_override="dashscope",
        )
    )

    pm = runtime.provider_manager
    assert pm._provider_key_override == "dashscope"
    assert pm._model == "qwen3.7-max"
    assert pm._base_url_override is None
    # The per-request QwenPaw hot-reload must be disabled so stream() cannot revert
    # the session's provider back to the (broken) partner endpoint.
    assert pm._ignore_llm_source is True


def test_frozen_partner_provider_config_reaches_runtime_without_rereading_source(tmp_path, monkeypatch) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("IAC_CODE_CONFIG_DIR", str(tmp_path / "config"))
    monkeypatch.setattr("iac_code.config.get_llm_source", lambda: "qwenpaw")
    monkeypatch.setattr(
        "iac_code.services.qwenpaw_source.load_from_qwenpaw",
        lambda: (_ for _ in ()).throw(AssertionError("partner source must not be reread")),
    )

    runtime = create_agent_runtime(
        AgentFactoryOptions(
            model="partner-model-snapshot",
            session_id="frozen-partner",
            cwd=str(tmp_path),
            provider_key_override="dashscope",
            provider_api_key_override="fake-partner-key",
            provider_base_url_override="https://partner.invalid/v1",
            provider_config_frozen=True,
        )
    )

    pm = runtime.provider_manager
    assert pm._provider_key_override == "dashscope"
    assert pm._model == "partner-model-snapshot"
    assert pm._credentials == {"dashscope": "fake-partner-key"}
    assert pm._base_url_override == "https://partner.invalid/v1"
    assert pm._ignore_llm_source is True


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


def test_create_agent_runtime_includes_runtime_provider_and_model(tmp_path, monkeypatch) -> None:
    from iac_code.agent.system_prompt import split_by_dynamic_boundary

    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("IAC_CODE_CONFIG_DIR", str(tmp_path / "config"))

    runtime = create_agent_runtime(
        AgentFactoryOptions(model="qwen3.7-max", session_id="runtime-model", cwd=str(tmp_path))
    )

    static, dynamic = split_by_dynamic_boundary(runtime.agent_loop.system_prompt)
    runtime_line = "Provider & Model: Alibaba Cloud Bailian / qwen3.7-max"
    assert runtime_line not in static
    assert runtime_line in dynamic


class _LifecycleAliyunServices:
    def __init__(self) -> None:
        self.openmeta = object()
        self.contract_resolver = object()
        self.internal_caller = object()
        self.delegated_executor_factory = lambda action: SimpleNamespace(action=action)
        self.action_group_executor_factory = lambda spec: SimpleNamespace(spec=spec)
        self.aclose = AsyncMock()


@pytest.mark.asyncio
async def test_agent_runtime_reuses_one_aliyun_services_instance_and_closes_it_once(tmp_path, monkeypatch) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("IAC_CODE_CONFIG_DIR", str(tmp_path / "config"))
    services = _LifecycleAliyunServices()
    factory = MagicMock(return_value=services)
    monkeypatch.setattr("iac_code.tools.cloud.aliyun.runtime.create_aliyun_runtime_services", factory)

    runtime = create_agent_runtime(
        AgentFactoryOptions(model="qwen3.7-max", session_id="aliyun-runtime", cwd=str(tmp_path))
    )

    assert factory.call_count == 1
    assert runtime.aliyun_services is services
    assert runtime.tool_registry.get("aliyun_api_doc")._services is services
    runtime.refresh_cloud_tools()
    assert factory.call_count == 1
    assert runtime.tool_registry.get("aliyun_api_doc")._services is services

    await runtime.aclose()
    await runtime.aclose()
    services.aclose.assert_awaited_once()


def test_create_agent_runtime_failure_closes_aliyun_services(tmp_path, monkeypatch) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("IAC_CODE_CONFIG_DIR", str(tmp_path / "config"))
    services = _LifecycleAliyunServices()
    monkeypatch.setattr(
        "iac_code.tools.cloud.aliyun.runtime.create_aliyun_runtime_services",
        MagicMock(return_value=services),
    )
    monkeypatch.setattr(
        "iac_code.tools.base.ToolRegistry.register_default_tools",
        MagicMock(side_effect=RuntimeError("tool-registry-setup")),
    )

    with pytest.raises(RuntimeError, match="tool-registry-setup"):
        create_agent_runtime(AgentFactoryOptions(model="qwen3.7-max", session_id="failure", cwd=str(tmp_path)))

    services.aclose.assert_awaited_once_with()


@pytest.mark.asyncio
async def test_agent_runtime_mcp_close_failure_does_not_skip_aliyun_cleanup() -> None:
    services = _LifecycleAliyunServices()
    manager = SimpleNamespace(disconnect_all=AsyncMock(side_effect=RuntimeError("mcp-close")))
    runtime = AgentRuntime(
        agent_loop=object(),
        session_id="session",
        tool_registry=object(),
        provider_manager=object(),
        command_registry=object(),
        task_manager=object(),
        memory_manager=object(),
        legacy_memory_manager=object(),
        aliyun_services=services,
        mcp_manager=manager,
    )

    await runtime.aclose()

    manager.disconnect_all.assert_awaited_once()
    services.aclose.assert_awaited_once()
