"""A2A cannot turn installed skill resources into template output files."""

import asyncio
import os

import pytest

from iac_code.a2a.runtime_overrides import a2a_request_context
from iac_code.services.permissions.pipeline import check_tool_permission
from iac_code.tools.base import ToolContext
from iac_code.tools.edit_file import EditFileTool
from iac_code.tools.path_safety import check_write_path
from iac_code.tools.read_file import ReadFileTool
from iac_code.tools.write_file import WriteFileTool
from iac_code.types.permissions import PermissionMode, ToolPermissionContext


@pytest.fixture
def installation(tmp_path, monkeypatch):
    # Reproduce an installation nested inside cwd, like /home/app under /home.
    root = tmp_path / "app" / "src" / "iac_code"
    root.mkdir(parents=True)
    monkeypatch.setattr("iac_code.tools.path_safety.get_iac_code_application_root", lambda: root)
    return root


def _input(tool, path):
    if tool.name == "write_file":
        return {"path": str(path), "content": "new template"}
    return {"path": str(path), "old_string": "original", "new_string": "new template"}


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", list(PermissionMode))
@pytest.mark.parametrize("tool", [WriteFileTool(), EditFileTool()], ids=lambda tool: tool.name)
@pytest.mark.parametrize("safe_mode", ["0", "1"])
async def test_a2a_denies_installation_writes_despite_workspace_and_allow_rules(
    tmp_path, monkeypatch, installation, mode, tool, safe_mode
):
    monkeypatch.setenv("IAC_CODE_A2A_SAFE_MODE", safe_mode)
    target = installation / "pipeline" / "skills" / "templates" / "0-ecs.yml"
    target.parent.mkdir(parents=True)
    target.write_text("original", encoding="utf-8")
    values = _input(tool, target)
    context = ToolPermissionContext(
        cwd=str(tmp_path),
        mode=mode,
        additional_directories=[str(installation)],
        allow_rules={"user_settings": [tool.name]},
    )

    with a2a_request_context(preferred_language="en"):
        permission = await check_tool_permission(tool, values, context)
        # Also check actual execution: a cached approval must not bypass protection.
        result = await tool.execute(tool_input=values, context=ToolContext(cwd=str(tmp_path)))

    assert permission.behavior == "deny"
    assert permission.reason.type == "path_constraint"
    assert "installation directory is read-only" in permission.message
    assert "under ./." in permission.message
    assert str(tmp_path) not in permission.message
    assert result.is_error
    assert result.content == permission.message
    assert target.read_text(encoding="utf-8") == "original"


@pytest.mark.asyncio
@pytest.mark.parametrize("alias", [False, True])
async def test_a2a_blocks_creation_and_parent_traversal(tmp_path, installation, alias):
    target = installation / "pipeline" / "skills" / "templates" / "0-ecs.yml"
    path = os.path.join("app", "src", "iac_code", "..", "iac_code", "pipeline", "skills", "templates", "0-ecs.yml")
    tool = WriteFileTool()
    values = {"file_path" if alias else "path": path, "content": "new template"}
    context = ToolPermissionContext(cwd=str(tmp_path), allow_rules={"user_settings": ["write_file"]})

    with a2a_request_context():
        permission = await check_tool_permission(tool, values, context)
        tool.normalize_input(values)
        result = await tool.execute(tool_input=values, context=ToolContext(cwd=str(tmp_path)))

    assert permission.behavior == "deny"
    assert result.is_error
    assert not target.parent.exists()


@pytest.mark.asyncio
@pytest.mark.parametrize("tool", [WriteFileTool(), EditFileTool()], ids=lambda tool: tool.name)
async def test_execution_rechecks_symlinks_after_permission(tmp_path, installation, tool):
    source = installation / "SKILL.md"
    source.write_text("original", encoding="utf-8")
    link = tmp_path / "template.yml"
    link.write_text("original", encoding="utf-8")
    values = _input(tool, link)
    context = ToolPermissionContext(cwd=str(tmp_path), allow_rules={"user_settings": [tool.name]})

    with a2a_request_context():
        assert (await check_tool_permission(tool, values, context)).behavior == "allow"
        link.unlink()
        try:
            link.symlink_to(source)
        except OSError as error:
            pytest.skip(f"Symlink creation unavailable: {error}")
        assert (await check_tool_permission(tool, values, context)).behavior == "deny"
        result = await tool.execute(tool_input=values, context=ToolContext(cwd=str(tmp_path)))

    assert result.is_error
    assert source.read_text(encoding="utf-8") == "original"


@pytest.mark.asyncio
async def test_a2a_denies_creation_through_a_directory_symlink(tmp_path, installation):
    link = tmp_path / "output"
    try:
        link.symlink_to(installation, target_is_directory=True)
    except OSError as error:
        pytest.skip(f"Symlink creation unavailable: {error}")
    tool = WriteFileTool()
    values = _input(tool, link / "templates" / "new.yml")

    with a2a_request_context():
        context = ToolPermissionContext(cwd=str(tmp_path), mode=PermissionMode.BYPASS_PERMISSIONS)
        assert (await check_tool_permission(tool, values, context)).behavior == "deny"
        result = await tool.execute(tool_input=values, context=ToolContext(cwd=str(tmp_path)))

    assert result.is_error
    assert not (installation / "templates").exists()


@pytest.mark.asyncio
async def test_a2a_keeps_workspace_writes_and_skill_resource_reads(tmp_path, installation):
    reference = installation / "references" / "rules.md"
    reference.parent.mkdir()
    reference.write_text("template rules", encoding="utf-8")
    context = ToolPermissionContext(
        cwd=str(tmp_path),
        relative_read_directories=[str(installation)],
        allow_rules={"user_settings": ["read_file", "write_file", "edit_file"]},
    )
    execution_context = ToolContext(cwd=str(tmp_path), relative_read_directories=[str(installation)])

    with a2a_request_context():
        for tool in [WriteFileTool(), EditFileTool()]:
            values = _input(tool, "templates/0-ecs.yml")
            if tool.name == "edit_file":
                values["old_string"] = "new template"
                values["new_string"] = "updated template"
            assert (await check_tool_permission(tool, values, context)).behavior == "allow"
            assert not (await tool.execute(tool_input=values, context=execution_context)).is_error
        read = ReadFileTool()
        values = {"path": "references/rules.md"}
        assert (await check_tool_permission(read, values, context)).behavior == "allow"
        assert "template rules" in (await read.execute(tool_input=values, context=execution_context)).content

    assert (tmp_path / "templates/0-ecs.yml").read_text(encoding="utf-8") == "updated template"


@pytest.mark.asyncio
async def test_protection_is_request_local_and_inherited_by_a2a_tasks(tmp_path, installation):
    target = str(installation / "new.yml")
    ready, release = asyncio.Event(), asyncio.Event()

    def decision():
        return check_write_path(target, cwd=str(tmp_path), additional_directories=[]).behavior

    async def a2a_request():
        with a2a_request_context():

            async def child():
                return decision()

            assert await asyncio.create_task(child()) == "deny"
            ready.set()
            await release.wait()
            with pytest.raises(RuntimeError), a2a_request_context(preferred_language="ja"):
                assert decision() == "deny"
                raise RuntimeError("request failed")
            assert decision() == "deny"
        assert decision() == "allow"

    task = asyncio.create_task(a2a_request())
    try:
        await asyncio.wait_for(ready.wait(), timeout=5)
        # A concurrent local developer can still edit the checkout.
        assert decision() == "allow"
    finally:
        release.set()
        await task


def test_denial_uses_request_language_and_restores_nested_language(tmp_path, installation, monkeypatch):
    monkeypatch.setattr(
        "iac_code.tools.path_safety.translate_message", lambda message, *, language: f"{language}: {message}"
    )

    def message():
        return check_write_path(str(installation / "new.yml"), cwd=str(tmp_path), additional_directories=[]).detail

    with a2a_request_context(preferred_language="zh"):
        assert message().startswith("zh:")
        with a2a_request_context():
            assert message().startswith("zh:")
        with a2a_request_context(preferred_language="ja"):
            assert message().startswith("ja:")
        assert message().startswith("zh:")


@pytest.mark.asyncio
@pytest.mark.parametrize("tool", [WriteFileTool(), EditFileTool()], ids=lambda tool: tool.name)
@pytest.mark.parametrize("mode", list(PermissionMode))
@pytest.mark.parametrize("pipeline_mode", [False, True], ids=["normal", "pipeline"])
async def test_safe_mode_rejects_writes_outside_cwd_without_permission_prompt(
    tmp_path, monkeypatch, tool, mode, pipeline_mode
):
    monkeypatch.setenv("IAC_CODE_A2A_SAFE_MODE", "true")
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    outside = tmp_path / "additional"
    outside.mkdir()
    target = outside / "template.yml"
    target.write_text("original", encoding="utf-8")
    values = _input(tool, target)
    context = ToolPermissionContext(
        cwd=str(workspace),
        mode=mode,
        pipeline_mode=pipeline_mode,
        additional_directories=[str(outside)],
        allow_rules={"user_settings": [tool.name]},
    )

    with a2a_request_context(preferred_language="en"):
        permission = await check_tool_permission(tool, values, context)
        result = await tool.execute(tool_input=values, context=ToolContext(cwd=str(workspace)))

    assert permission.behavior == "deny"
    assert permission.reason.type == "path_constraint"
    assert permission.message == "Files can only be written under ./."
    assert str(workspace) not in permission.message
    assert result.is_error
    assert result.content == permission.message
    assert target.read_text(encoding="utf-8") == "original"


@pytest.mark.asyncio
@pytest.mark.parametrize("path_kind", ["absolute", "parent", "symlink"])
async def test_safe_mode_rejects_outside_creation_before_creating_directories(tmp_path, monkeypatch, path_kind):
    monkeypatch.setenv("IAC_CODE_A2A_SAFE_MODE", "1")
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    target = tmp_path / "outside" / "new" / "template.yml"
    path = str(target)
    if path_kind == "parent":
        path = os.path.join("..", "outside", "new", "template.yml")
    elif path_kind == "symlink":
        outside = tmp_path / "outside"
        outside.mkdir()
        try:
            (workspace / "link").symlink_to(outside, target_is_directory=True)
        except OSError as error:
            pytest.skip(f"Symlink creation unavailable: {error}")
        path = "link/new/template.yml"

    with a2a_request_context(preferred_language="en"):
        context = ToolPermissionContext(cwd=str(workspace), mode=PermissionMode.BYPASS_PERMISSIONS)
        values = {"path": path, "content": "template"}
        assert (await check_tool_permission(WriteFileTool(), values, context)).behavior == "deny"
        result = await WriteFileTool().execute(tool_input=values, context=ToolContext(cwd=str(workspace)))

    assert result.is_error
    assert result.content == "Files can only be written under ./."
    assert str(workspace) not in result.content
    assert not target.parent.exists()


@pytest.mark.asyncio
@pytest.mark.parametrize("safe_mode", ["0", "1"])
async def test_workspace_writes_and_internal_result_storage_remain_available(tmp_path, monkeypatch, safe_mode):
    from iac_code.tools.result_storage import ResultStorage
    from iac_code.tools.safe_file_io import supported

    monkeypatch.setenv("IAC_CODE_A2A_SAFE_MODE", safe_mode)
    if safe_mode == "1" and not supported():
        pytest.skip("Secure writes are refused on platforms without directory descriptors")
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    context = ToolPermissionContext(cwd=str(workspace), allow_rules={"user_settings": ["write_file", "edit_file"]})
    execution = ToolContext(cwd=str(workspace))
    storage = ResultStorage(str(tmp_path / "session" / "tool-results"))
    large_text = "tool output\n" * 2000

    with a2a_request_context():
        for tool in [WriteFileTool(), EditFileTool()]:
            values = _input(tool, "templates/0-ecs.yml")
            if tool.name == "edit_file":
                values["old_string"] = "new template"
                values["new_string"] = "updated template"
            assert (await check_tool_permission(tool, values, context)).behavior == "allow"
            assert not (await tool.execute(tool_input=values, context=execution)).is_error
        result = storage.process("large-result", large_text)

    assert result.is_externalized
    assert (workspace / "templates/0-ecs.yml").read_text(encoding="utf-8") == "updated template"
    assert (tmp_path / "session/tool-results/large-result.txt").read_text(encoding="utf-8") == large_text


@pytest.mark.asyncio
async def test_safe_mode_preserves_dedicated_memory_writes_outside_cwd(tmp_path, monkeypatch):
    from iac_code.memory.memory_manager import MemoryManager
    from iac_code.memory.memory_tools import WriteMemoryTool

    monkeypatch.setenv("IAC_CODE_A2A_SAFE_MODE", "1")
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    memory_dir = tmp_path / "config" / "project-memory"
    tool = WriteMemoryTool(MemoryManager(str(memory_dir)), is_enabled=lambda: True)
    values = {
        "name": "project-notes",
        "content": "Remember the selected region.",
        "memory_type": "project",
        "description": "Region",
    }
    context = ToolPermissionContext(cwd=str(workspace), allow_rules={"user_settings": ["write_memory"]})

    with a2a_request_context():
        assert (await check_tool_permission(tool, values, context)).behavior == "allow"
        result = await tool.execute(tool_input=values, context=ToolContext(cwd=str(workspace)))

    assert not result.is_error
    assert "Remember the selected region." in (memory_dir / "project-notes.md").read_text(encoding="utf-8")


@pytest.mark.asyncio
async def test_disabling_safe_mode_keeps_existing_extra_directory_permissions(tmp_path, monkeypatch):
    monkeypatch.setenv("IAC_CODE_A2A_SAFE_MODE", "0")
    workspace, additional = tmp_path / "workspace", tmp_path / "additional"
    workspace.mkdir()
    additional.mkdir()
    values = _input(WriteFileTool(), additional / "template.yml")
    context = ToolPermissionContext(
        cwd=str(workspace),
        additional_directories=[str(additional)],
        allow_rules={"user_settings": ["write_file"]},
    )

    with a2a_request_context():
        assert (await check_tool_permission(WriteFileTool(), values, context)).behavior == "allow"
    context.additional_directories = []
    with a2a_request_context():
        assert (await check_tool_permission(WriteFileTool(), values, context)).behavior == "ask"


def test_nested_request_cannot_weaken_safe_mode_and_policy_does_not_leak(tmp_path, monkeypatch):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    outside = str(tmp_path / "outside.yml")

    def decision():
        return check_write_path(outside, cwd=str(workspace), additional_directories=[str(tmp_path)]).behavior

    monkeypatch.setenv("IAC_CODE_A2A_SAFE_MODE", "1")
    with a2a_request_context():
        assert decision() == "deny"
        monkeypatch.setenv("IAC_CODE_A2A_SAFE_MODE", "0")
        with a2a_request_context():
            assert decision() == "deny"
    assert decision() == "allow"


@pytest.mark.asyncio
@pytest.mark.parametrize("pipeline_mode", [False, True], ids=["normal", "pipeline"])
async def test_loop_returns_outside_write_error_to_model_without_permission_event(tmp_path, monkeypatch, pipeline_mode):
    from iac_code.agent.agent_loop import AgentLoop
    from iac_code.tools.base import ToolRegistry
    from iac_code.types.stream_events import (
        MessageEndEvent,
        MessageStartEvent,
        PermissionRequestEvent,
        TextDeltaEvent,
        ToolResultEvent,
        ToolUseEndEvent,
        ToolUseStartEvent,
        Usage,
    )

    monkeypatch.setenv("IAC_CODE_A2A_SAFE_MODE", "1")
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    outside = tmp_path / "outside.yml"

    class Provider:
        calls = 0

        def get_model_name(self):
            return "offline"

        async def stream(self, messages, system, tools=None):
            self.calls += 1
            yield MessageStartEvent(message_id=str(self.calls))
            if self.calls == 1:
                yield ToolUseStartEvent(tool_use_id="outside-write", name="write_file")
                yield ToolUseEndEvent(
                    tool_use_id="outside-write", name="write_file", input={"path": str(outside), "content": "template"}
                )
            else:
                results = [
                    block
                    for message in messages
                    if isinstance(message.content, list)
                    for block in message.content
                    if getattr(block, "type", None) == "tool_result"
                ]
                assert len(results) == 1 and results[0].is_error
                assert "Files can only be written under ./." in results[0].content
                assert str(workspace) not in results[0].content
                yield TextDeltaEvent(text="Use a path inside the working directory.")
            yield MessageEndEvent(stop_reason="tool_use" if self.calls == 1 else "end_turn", usage=Usage())

    registry = ToolRegistry()
    registry.register(WriteFileTool())
    provider = Provider()
    loop = AgentLoop(
        provider_manager=provider,
        system_prompt="Offline write boundary test.",
        tool_registry=registry,
        cwd=str(workspace),
        pipeline_mode=pipeline_mode,
        permission_context=ToolPermissionContext(cwd=str(workspace), mode=PermissionMode.BYPASS_PERMISSIONS),
        result_storage_dir=tmp_path / "results",
    )
    with a2a_request_context(preferred_language="en"):
        events = [event async for event in loop.run_streaming("Write a template.")]

    assert provider.calls == 2
    assert not any(isinstance(event, PermissionRequestEvent) for event in events)
    assert any(isinstance(event, ToolResultEvent) and event.is_error for event in events)
    assert not outside.exists()
