"""Real filesystem regressions for cwd-only writes, including deliberate races."""

import errno
import os
import stat

import pytest

from iac_code.a2a.runtime_overrides import a2a_request_context
from iac_code.services.permissions.pipeline import check_tool_permission
from iac_code.tools import safe_file_io
from iac_code.tools.base import ToolContext
from iac_code.tools.edit_file import EditFileTool
from iac_code.tools.write_file import WriteFileTool
from iac_code.types.permissions import PermissionMode, ToolPermissionContext


@pytest.fixture
def workspace(tmp_path, monkeypatch):
    monkeypatch.setenv("IAC_CODE_A2A_SAFE_MODE", "1")
    path = tmp_path / "workspace"
    path.mkdir()
    return path


@pytest.fixture(params=[WriteFileTool, EditFileTool], ids=["write_file", "edit_file"])
def tool(request):
    return request.param()


def _values(tool, path):
    if tool.name == "write_file":
        return {"path": str(path), "content": "updated 中文\r\n"}
    return {"path": str(path), "old_string": "original", "new_string": "updated 中文"}


supported = pytest.mark.skipif(not safe_file_io.supported(), reason="Secure directory descriptors unavailable")


@supported
@pytest.mark.parametrize("path", ["absolute", "nested/../template.yml"])
def test_write_boundary_error_displays_relative_cwd_hint(workspace, path):
    path = str(workspace.parent / "external.yml") if path == "absolute" else path
    with pytest.raises(ValueError) as error:
        safe_file_io.validate_safe_write_path(path, cwd=str(workspace), language="en")
    assert str(error.value) == "Files can only be written under ./."
    assert str(workspace) not in str(error.value)


@supported
@pytest.mark.asyncio
@pytest.mark.parametrize("mode", list(PermissionMode))
async def test_hard_link_cannot_modify_an_external_file(workspace, tmp_path, tool, mode):
    external = tmp_path / "external.yml"
    external.write_text("original", encoding="utf-8")
    target = workspace / "template.yml"
    os.link(external, target)
    context = ToolPermissionContext(cwd=str(workspace), mode=mode, allow_rules={"user_settings": [tool.name]})

    with a2a_request_context(preferred_language="en"):
        permission = await check_tool_permission(tool, _values(tool, target), context)
        result = await tool.execute(tool_input=_values(tool, target), context=ToolContext(cwd=str(workspace)))

    assert permission.behavior == "deny"
    assert "hard links" in permission.message
    assert result.is_error
    assert external.read_text(encoding="utf-8") == "original"
    assert target.read_text(encoding="utf-8") == "original"


@supported
@pytest.mark.asyncio
@pytest.mark.parametrize("link_kind", ["file", "directory"])
async def test_even_links_to_an_internal_target_are_rejected(workspace, tool, link_kind):
    real = workspace / "real"
    real.mkdir()
    original = real / "template.yml"
    original.write_text("original", encoding="utf-8")
    if link_kind == "file":
        path = workspace / "template.yml"
        path.symlink_to(original)
    else:
        link = workspace / "link"
        link.symlink_to(real, target_is_directory=True)
        path = link / "template.yml"

    with a2a_request_context(preferred_language="en"):
        context = ToolPermissionContext(cwd=str(workspace), mode=PermissionMode.BYPASS_PERMISSIONS)
        assert (await check_tool_permission(tool, _values(tool, path), context)).behavior == "deny"
        result = await tool.execute(tool_input=_values(tool, path), context=ToolContext(cwd=str(workspace)))

    assert result.is_error
    assert original.read_text(encoding="utf-8") == "original"


@supported
@pytest.mark.asyncio
@pytest.mark.parametrize("swap_root", [False, True], ids=["parent-directory", "cwd"])
async def test_parent_swapped_after_open_does_not_redirect_the_write(workspace, tmp_path, tool, monkeypatch, swap_root):
    parent = workspace if swap_root else workspace / "output"
    parent.mkdir(exist_ok=True)
    target = parent / "template.yml"
    target.write_text("original", encoding="utf-8")
    external = tmp_path / "external"
    external.mkdir()
    outside = external / target.name
    outside.write_text("outside original", encoding="utf-8")
    held = tmp_path / "original-workspace" if swap_root else workspace / "original-output"
    fdopen = os.fdopen
    swapped = False

    def swap(fd, mode, **kwargs):
        nonlocal swapped
        if mode == "w" and not swapped:
            swapped = True
            parent.rename(held)
            parent.symlink_to(external, target_is_directory=True)
        return fdopen(fd, mode, **kwargs)

    monkeypatch.setattr(safe_file_io.os, "fdopen", swap)
    with a2a_request_context(preferred_language="en"):
        result = await tool.execute(tool_input=_values(tool, target), context=ToolContext(cwd=str(workspace)))

    assert swapped
    assert result.is_error
    assert outside.read_text(encoding="utf-8") == "outside original"
    assert (held / target.name).read_text(encoding="utf-8") == "original"
    assert not list(held.glob(".iac-write-*"))


@supported
@pytest.mark.asyncio
@pytest.mark.parametrize("replacement", ["symlink", "hardlink", "regular"])
async def test_target_changed_during_temp_write_is_not_overwritten(workspace, tmp_path, tool, monkeypatch, replacement):
    target = workspace / "template.yml"
    target.write_text("original", encoding="utf-8")
    external = tmp_path / "external.yml"
    external.write_text("outside original", encoding="utf-8")
    fdopen = os.fdopen
    swapped = False

    def swap(fd, mode, **kwargs):
        nonlocal swapped
        if mode == "w" and not swapped:
            swapped = True
            target.unlink()
            if replacement == "symlink":
                target.symlink_to(external)
            elif replacement == "hardlink":
                os.link(external, target)
            else:
                target.write_text("concurrent update", encoding="utf-8")
        return fdopen(fd, mode, **kwargs)

    monkeypatch.setattr(safe_file_io.os, "fdopen", swap)
    with a2a_request_context(preferred_language="en"):
        result = await tool.execute(tool_input=_values(tool, target), context=ToolContext(cwd=str(workspace)))

    assert swapped
    assert result.is_error
    assert external.read_text(encoding="utf-8") == "outside original"
    assert target.read_text(encoding="utf-8") == (
        "concurrent update" if replacement == "regular" else "outside original"
    )
    assert not list(workspace.glob(".iac-write-*"))


@supported
@pytest.mark.asyncio
async def test_failed_publish_preserves_original_and_removes_temporary(workspace, tool, monkeypatch):
    target = workspace / "template.yml"
    target.write_text("original", encoding="utf-8")

    def fail(*args, **kwargs):
        raise OSError("fixture publish failure")

    monkeypatch.setattr(safe_file_io.os, "replace", fail)
    with a2a_request_context(preferred_language="en"):
        result = await tool.execute(tool_input=_values(tool, target), context=ToolContext(cwd=str(workspace)))

    assert result.is_error
    assert "fixture publish failure" in result.content
    assert target.read_text(encoding="utf-8") == "original"
    assert not list(workspace.glob(".iac-write-*"))


@supported
@pytest.mark.asyncio
async def test_safe_update_preserves_mode_and_text_behavior(workspace, tool):
    target = workspace / "template.yml"
    target.write_bytes(b"original\r\n")
    target.chmod(0o640)
    original_inode = target.stat().st_ino

    with a2a_request_context(preferred_language="en"):
        result = await tool.execute(tool_input=_values(tool, target), context=ToolContext(cwd=str(workspace)))

    assert not result.is_error
    assert target.read_bytes() == ("updated 中文\r\n" if tool.name == "write_file" else "updated 中文\n").encode()
    assert stat.S_IMODE(target.stat().st_mode) == 0o640
    assert target.stat().st_ino != original_inode
    assert not list(workspace.glob(".iac-write-*"))


@supported
@pytest.mark.asyncio
async def test_missing_parents_are_created_only_during_execution(workspace):
    target = workspace / "new" / "nested" / "template.yml"
    context = ToolPermissionContext(cwd=str(workspace), allow_rules={"user_settings": ["write_file"]})
    with a2a_request_context(preferred_language="en"):
        assert (
            await check_tool_permission(WriteFileTool(), _values(WriteFileTool(), target), context)
        ).behavior == "allow"
        assert not target.parent.exists()
        result = await WriteFileTool().execute(
            tool_input=_values(WriteFileTool(), target),
            context=ToolContext(cwd=str(workspace)),
        )
    assert not result.is_error
    assert target.read_text(encoding="utf-8").startswith("updated 中文")


@supported
@pytest.mark.asyncio
async def test_link_inserted_while_creating_parents_cannot_redirect_mkdir(workspace, tmp_path, monkeypatch):
    external = tmp_path / "external"
    external.mkdir()
    target = workspace / "new" / "nested" / "template.yml"
    mkdir = os.mkdir
    inserted = False

    def insert_link(name, *args, **kwargs):
        nonlocal inserted
        if name == "new" and "dir_fd" in kwargs:
            inserted = True
            os.symlink(external, name, dir_fd=kwargs["dir_fd"])
            raise FileExistsError(errno.EEXIST, "fixture race")
        return mkdir(name, *args, **kwargs)

    monkeypatch.setattr(safe_file_io.os, "mkdir", insert_link)
    with a2a_request_context(preferred_language="en"):
        result = await WriteFileTool().execute(
            tool_input=_values(WriteFileTool(), target),
            context=ToolContext(cwd=str(workspace)),
        )

    assert inserted
    assert result.is_error
    assert not list(external.iterdir())


@supported
@pytest.mark.asyncio
async def test_non_regular_file_is_rejected_without_opening_it(workspace, tool):
    target = workspace / "pipe"
    os.mkfifo(target)
    with a2a_request_context(preferred_language="en"):
        context = ToolPermissionContext(cwd=str(workspace), mode=PermissionMode.BYPASS_PERMISSIONS)
        assert (await check_tool_permission(tool, _values(tool, target), context)).behavior == "deny"
        result = await tool.execute(tool_input=_values(tool, target), context=ToolContext(cwd=str(workspace)))
    assert result.is_error
    assert stat.S_ISFIFO(target.stat().st_mode)


@supported
@pytest.mark.asyncio
async def test_read_only_target_is_not_replaced(workspace, tool):
    target = workspace / "template.yml"
    target.write_text("original", encoding="utf-8")
    target.chmod(0o440)
    with a2a_request_context(preferred_language="en"):
        result = await tool.execute(tool_input=_values(tool, target), context=ToolContext(cwd=str(workspace)))
    assert result.is_error
    assert target.read_text(encoding="utf-8") == "original"
    assert stat.S_IMODE(target.stat().st_mode) == 0o440


@supported
@pytest.mark.asyncio
async def test_fdopen_failure_closes_descriptors(workspace, tool, monkeypatch):
    target = workspace / "template.yml"
    target.write_text("original", encoding="utf-8")
    opened = []
    file_open = os.open

    def record_open(*args, **kwargs):
        descriptor = file_open(*args, **kwargs)
        opened.append(descriptor)
        return descriptor

    def fail(*args, **kwargs):
        raise OSError("fixture fdopen failure")

    monkeypatch.setattr(safe_file_io.os, "open", record_open)
    monkeypatch.setattr(safe_file_io.os, "fdopen", fail)
    with a2a_request_context(preferred_language="en"):
        result = await tool.execute(tool_input=_values(tool, target), context=ToolContext(cwd=str(workspace)))
    assert result.is_error
    assert target.read_text(encoding="utf-8") == "original"
    for descriptor in opened:
        with pytest.raises(OSError) as error:
            os.fstat(descriptor)
        assert error.value.errno == errno.EBADF
    assert not list(workspace.glob(".iac-write-*"))


@pytest.mark.asyncio
async def test_unsupported_platform_refuses_safe_writes_instead_of_falling_back(workspace, tool, monkeypatch):
    monkeypatch.setattr(safe_file_io, "_HAS_SAFE_DIR_FD", False)
    target = workspace / "template.yml"
    target.write_text("original", encoding="utf-8")
    context = ToolPermissionContext(cwd=str(workspace), mode=PermissionMode.BYPASS_PERMISSIONS)
    with a2a_request_context(preferred_language="en"):
        permission = await check_tool_permission(tool, _values(tool, target), context)
        result = await tool.execute(tool_input=_values(tool, target), context=ToolContext(cwd=str(workspace)))
    assert permission.behavior == "deny"
    assert "not supported on this platform" in permission.message
    assert result.is_error
    assert target.read_text(encoding="utf-8") == "original"


@pytest.mark.asyncio
async def test_unsupported_platform_keeps_non_safe_mode_behavior(workspace, tool, monkeypatch):
    monkeypatch.setenv("IAC_CODE_A2A_SAFE_MODE", "0")
    monkeypatch.setattr(safe_file_io, "_HAS_SAFE_DIR_FD", False)
    target = workspace / "template.yml"
    target.write_text("original", encoding="utf-8")
    with a2a_request_context(preferred_language="en"):
        result = await tool.execute(tool_input=_values(tool, target), context=ToolContext(cwd=str(workspace)))
    assert not result.is_error
