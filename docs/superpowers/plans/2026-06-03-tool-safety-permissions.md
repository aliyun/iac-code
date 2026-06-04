# Tool Safety Permissions Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Fix GitHub issues #87, #88, #78, and #89 by adding safe read boundaries, hardening bash readonly classification, and bounding large file/HTTP reads.

**Architecture:** Add one shared path-safety module for read permission decisions and keep bash-specific argv parsing in the bash package. `read_file` and bash read commands will ask before reading outside trusted roots or sensitive paths. `read_file` and `web_fetch` will stream with hard caps instead of loading full content before truncation.

**Tech Stack:** Python 3.10+, pytest, pytest-asyncio, uv, existing `ToolPermissionContext`, `PermissionResult`, bash tree-sitter parser, and httpx async streaming.

---

## File Structure

- Create `src/iac_code/tools/path_safety.py`
  - Owns read-path resolution, sensitive-path detection, trusted root checks, and iac-code application root detection.
- Create `src/iac_code/tools/bash/argv_safety.py`
  - Owns bash argv helpers for sed in-place detection, sed execute detection, dangerous readonly args, pip-like matching, and read-path extraction.
- Modify `src/iac_code/types/permissions.py`
  - Add `trusted_read_directories: list[str]` with a default empty list.
- Modify `src/iac_code/services/permissions/loader.py`
  - Keep settings loading stable and initialize `trusted_read_directories`.
- Create `src/iac_code/services/permissions/trusted_roots.py`
  - Builds current-session trusted read roots for tool results and image cache.
- Modify `src/iac_code/services/agent_factory.py`
  - Adds session trusted read roots after loading permission context.
- Modify `src/iac_code/ui/repl.py`
  - Adds session trusted read roots after loading permission context.
- Modify `src/iac_code/tools/read_file.py`
  - Override `check_permissions()` and replace `readlines()` with capped streaming reads.
- Modify `src/iac_code/tools/bash/path_validation.py`
  - Reuse path-safety helpers and add read-path constraints.
- Modify `src/iac_code/tools/bash/safety_checks.py`
  - Re-export sensitive path helpers from `path_safety` to preserve existing tests.
- Modify `src/iac_code/tools/bash/readonly_commands.py`
  - Use argv safety helpers; make dangerous readonly args non-readonly.
- Modify `src/iac_code/tools/bash/permissions.py`
  - Return `ask` for dangerous readonly arguments and use the shared sed helper.
- Modify `src/iac_code/tools/web_fetch.py`
  - Replace full-body `get()` with streaming chunk reads.
- Test `tests/tools/test_path_safety.py`
- Test `tests/types/test_permissions_types.py`
- Test `tests/services/permissions/test_loader.py`
- Test `tests/services/permissions/test_trusted_roots.py`
- Test `tests/services/test_agent_factory.py`
- Test `tests/ui/test_repl_shell_escape.py`
- Test `tests/tools/test_read_file.py`
- Test `tests/tools/bash/test_readonly_commands.py`
- Test `tests/tools/bash/test_permissions.py`
- Test `tests/tools/bash/test_path_validation.py`
- Test `tests/test_tools/test_web_fetch.py`

---

### Task 1: Shared Read Path Safety

**Files:**
- Create: `src/iac_code/tools/path_safety.py`
- Modify: `src/iac_code/tools/bash/safety_checks.py`
- Modify: `src/iac_code/types/permissions.py`
- Modify: `src/iac_code/services/permissions/loader.py`
- Test: `tests/tools/test_path_safety.py`
- Test: `tests/types/test_permissions_types.py`
- Test: `tests/services/permissions/test_loader.py`

- [ ] **Step 1: Write failing tests for read path decisions**

Create `tests/tools/test_path_safety.py`:

```python
from pathlib import Path

from iac_code.tools.path_safety import (
    ReadPathDecision,
    check_read_path,
    get_iac_code_application_root,
    is_sensitive_path,
)


def test_project_file_is_allowed(tmp_path):
    target = tmp_path / "src" / "app.py"
    target.parent.mkdir()
    target.write_text("print('ok')", encoding="utf-8")

    result = check_read_path(str(target), cwd=str(tmp_path), additional_directories=[], trusted_read_directories=[])

    assert result == ReadPathDecision("allow")


def test_additional_directory_file_is_allowed(tmp_path):
    cwd = tmp_path / "project"
    shared = tmp_path / "shared"
    cwd.mkdir()
    shared.mkdir()
    target = shared / "notes.txt"
    target.write_text("notes", encoding="utf-8")

    result = check_read_path(
        str(target),
        cwd=str(cwd),
        additional_directories=[str(shared)],
        trusted_read_directories=[],
    )

    assert result == ReadPathDecision("allow")


def test_iac_code_application_root_is_allowed(tmp_path):
    app_root = get_iac_code_application_root()
    target = app_root / "__init__.py"
    if not target.exists():
        target = next(app_root.rglob("__init__.py"))

    result = check_read_path(
        str(target),
        cwd=str(tmp_path),
        additional_directories=[],
        trusted_read_directories=[],
    )

    assert result.behavior == "allow"


def test_project_outside_file_asks(tmp_path):
    cwd = tmp_path / "project"
    outside = tmp_path / "outside"
    cwd.mkdir()
    outside.mkdir()
    target = outside / "secret.txt"
    target.write_text("secret", encoding="utf-8")

    result = check_read_path(str(target), cwd=str(cwd), additional_directories=[], trusted_read_directories=[])

    assert result.behavior == "ask"
    assert result.reason_type == "path_constraint"


def test_sensitive_path_asks_even_inside_cwd(tmp_path):
    target = tmp_path / ".env"
    target.write_text("TOKEN=fake", encoding="utf-8")

    result = check_read_path(str(target), cwd=str(tmp_path), additional_directories=[], trusted_read_directories=[])

    assert result.behavior == "ask"
    assert result.reason_type == "safety_check"


def test_iac_code_credentials_are_sensitive(tmp_path):
    target = tmp_path / ".iac-code" / ".credentials.yml"
    target.parent.mkdir()
    target.write_text("openai: fake", encoding="utf-8")

    assert is_sensitive_path(str(target)) is True


def test_trusted_read_directory_is_allowed(tmp_path):
    cwd = tmp_path / "project"
    trusted = tmp_path / ".iac-code" / "tool-results" / "session-1"
    cwd.mkdir()
    trusted.mkdir(parents=True)
    target = trusted / "tool.txt"
    target.write_text("large result", encoding="utf-8")

    result = check_read_path(
        str(target),
        cwd=str(cwd),
        additional_directories=[],
        trusted_read_directories=[str(trusted)],
    )

    assert result.behavior == "allow"
```

- [ ] **Step 2: Write failing tests for the new permission context field**

Add this method inside `class TestToolPermissionContext` in `tests/types/test_permissions_types.py`:

```python
def test_trusted_read_directories_default_empty():
    ctx = ToolPermissionContext(cwd="/tmp")
    assert ctx.trusted_read_directories == []
```

Add this test to `tests/services/permissions/test_loader.py`:

```python
def test_load_permission_context_initializes_trusted_read_directories(tmp_path, monkeypatch):
    monkeypatch.setenv("IAC_CODE_CONFIG_DIR", str(tmp_path / "config"))

    from iac_code.services.permissions.loader import load_permission_context

    ctx = load_permission_context(str(tmp_path))

    assert ctx.trusted_read_directories == []
```

- [ ] **Step 3: Run tests to verify they fail**

Run:

```bash
PATH="$HOME/.local/bin:$PATH" uv run pytest tests/tools/test_path_safety.py tests/types/test_permissions_types.py::TestToolPermissionContext::test_trusted_read_directories_default_empty tests/services/permissions/test_loader.py::test_load_permission_context_initializes_trusted_read_directories -v
```

Expected: FAIL because `iac_code.tools.path_safety` and `trusted_read_directories` do not exist.

- [ ] **Step 4: Implement `ToolPermissionContext.trusted_read_directories`**

In `src/iac_code/types/permissions.py`, update `ToolPermissionContext`:

```python
@dataclass
class ToolPermissionContext:
    """Resolved permission rules and workspace constraints for tool checks."""

    mode: PermissionMode = PermissionMode.DEFAULT
    cwd: str = ""
    allow_rules: dict[str, list[str]] = field(default_factory=dict)
    deny_rules: dict[str, list[str]] = field(default_factory=dict)
    ask_rules: dict[str, list[str]] = field(default_factory=dict)
    additional_directories: list[str] = field(default_factory=list)
    trusted_read_directories: list[str] = field(default_factory=list)
```

In `src/iac_code/services/permissions/loader.py`, pass the default explicitly when returning the context:

```python
return ToolPermissionContext(
    mode=resolved_mode,
    cwd=cwd,
    allow_rules=allow_rules,
    deny_rules=deny_rules,
    ask_rules=ask_rules,
    additional_directories=additional_directories,
    trusted_read_directories=[],
)
```

- [ ] **Step 5: Implement path-safety module**

Create `src/iac_code/tools/path_safety.py`:

```python
"""Shared path safety checks for model-initiated file reads."""

from __future__ import annotations

import os
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from iac_code.i18n import _
from iac_code.types.permissions import PermissionDecisionReason, PermissionResult
from iac_code.utils.platform import normalize_user_path

SENSITIVE_PATHS = [
    ".git/",
    ".git",
    ".iac-code/.credentials.yml",
    ".iac-code/.cloud-credentials.yml",
    ".iac-code/",
    ".iac-code",
    ".bashrc",
    ".zshrc",
    ".profile",
    ".bash_profile",
    ".ssh/",
    ".ssh",
    ".env",
    ".aliyun/",
    ".aliyun",
    ".alibabacloud/",
    ".alibabacloud",
    ".aws/credentials",
]

if sys.platform == "win32":
    SENSITIVE_PATHS.extend(
        [
            "AppData/Roaming/Microsoft/Windows/PowerShell",
            "AppData/Local/Microsoft/Credentials",
            "ntuser.dat",
        ]
    )


@dataclass(frozen=True)
class ReadPathDecision:
    behavior: Literal["allow", "ask"]
    reason_type: str = ""
    detail: str = ""

    def to_permission_result(self) -> PermissionResult:
        if self.behavior == "allow":
            return PermissionResult(behavior="passthrough")
        message = self.detail
        return PermissionResult(
            behavior="ask",
            message=message,
            reason=PermissionDecisionReason(type=self.reason_type, detail=message),
        )


def _build_sensitive_lookups() -> tuple[frozenset[str], tuple[str, ...]]:
    single: set[str] = set()
    multi: list[str] = []
    for entry in SENSITIVE_PATHS:
        cleaned = entry.rstrip("/")
        if not cleaned:
            continue
        if "/" in cleaned:
            multi.append(cleaned.replace("\\", "/"))
        else:
            single.add(cleaned)
    return frozenset(single), tuple(multi)


_SENSITIVE_SINGLE, _SENSITIVE_MULTI = _build_sensitive_lookups()


def _path_hits_sensitive(abs_norm: str) -> bool:
    normalized = abs_norm.replace("\\", "/")
    parts = normalized.split("/")
    if any(part in _SENSITIVE_SINGLE for part in parts):
        return True
    return any(sub in normalized for sub in _SENSITIVE_MULTI)


def is_sensitive_path(path: str) -> bool:
    return _path_hits_sensitive(os.path.realpath(normalize_user_path(path)))


def resolve_candidate(path: str, cwd: str) -> str:
    normalized = normalize_user_path(path)
    if os.path.isabs(normalized):
        return os.path.realpath(normalized)
    return os.path.realpath(os.path.join(cwd, normalized))


def _is_within(path: str, root: str) -> bool:
    path_r = os.path.realpath(path)
    root_r = os.path.realpath(root)
    return path_r == root_r or path_r.startswith(root_r + os.sep)


def get_iac_code_application_root() -> Path:
    package_root = Path(__file__).resolve().parents[1]
    for parent in [package_root, *package_root.parents]:
        if (parent / "pyproject.toml").is_file() and (parent / "src" / "iac_code").is_dir():
            return parent
    return package_root


def check_read_path(
    path: str,
    *,
    cwd: str,
    additional_directories: list[str],
    trusted_read_directories: list[str],
) -> ReadPathDecision:
    resolved = resolve_candidate(path, cwd)
    if is_sensitive_path(resolved):
        detail = _("operation touches a sensitive path: {}").format(path)
        return ReadPathDecision("ask", "safety_check", detail)

    roots = [
        str(get_iac_code_application_root()),
        cwd,
        *additional_directories,
        *trusted_read_directories,
    ]
    for root in roots:
        if root and _is_within(resolved, root):
            return ReadPathDecision("allow")

    detail = _("path outside allowed directories: {}").format(path)
    return ReadPathDecision("ask", "path_constraint", detail)
```

- [ ] **Step 6: Preserve bash safety helper compatibility**

In `src/iac_code/tools/bash/safety_checks.py`, replace the local sensitive-path constants and `_path_hits_sensitive()` implementation with imports while keeping exported names:

```python
from iac_code.tools.path_safety import SENSITIVE_PATHS, _path_hits_sensitive
```

Keep `_resolve_for_check()`, `_argv_paths_for_safety()`, and `check_safety()` unchanged except that they call the imported `_path_hits_sensitive()`.

- [ ] **Step 7: Run tests to verify the task passes**

Run:

```bash
PATH="$HOME/.local/bin:$PATH" uv run pytest tests/tools/test_path_safety.py tests/tools/bash/test_safety_checks.py tests/tools/bash/test_safety_checks_windows.py tests/types/test_permissions_types.py tests/services/permissions/test_loader.py -v
```

Expected: PASS.

- [ ] **Step 8: Commit**

Run:

```bash
git add src/iac_code/tools/path_safety.py src/iac_code/tools/bash/safety_checks.py src/iac_code/types/permissions.py src/iac_code/services/permissions/loader.py tests/tools/test_path_safety.py tests/types/test_permissions_types.py tests/services/permissions/test_loader.py
PATH="$HOME/.local/bin:/opt/homebrew/bin:$PATH" git commit -m "feat: add shared read path safety"
```

---

### Task 2: Current Session Trusted Read Roots

**Files:**
- Create: `src/iac_code/services/permissions/trusted_roots.py`
- Modify: `src/iac_code/services/agent_factory.py`
- Modify: `src/iac_code/ui/repl.py`
- Test: `tests/services/permissions/test_trusted_roots.py`
- Test: `tests/services/test_agent_factory.py`
- Test: `tests/ui/test_repl_shell_escape.py`

- [ ] **Step 1: Write failing tests for trusted roots helper**

Create `tests/services/permissions/test_trusted_roots.py`:

```python
from pathlib import Path


def test_build_session_trusted_read_directories_uses_session_artifact_dirs(monkeypatch, tmp_path):
    monkeypatch.setattr("iac_code.services.permissions.trusted_roots.get_config_dir", lambda: tmp_path / ".iac-code")

    from iac_code.services.permissions.trusted_roots import build_session_trusted_read_directories

    roots = build_session_trusted_read_directories("abc123")

    assert roots == [
        str(Path(tmp_path / ".iac-code" / "tool-results" / "abc123")),
        str(Path(tmp_path / ".iac-code" / "image-cache" / "abc123")),
    ]
```

- [ ] **Step 2: Write failing runtime plumbing tests**

Add this test to `tests/services/test_agent_factory.py`:

```python
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
```

Add this focused test to `tests/ui/test_repl_shell_escape.py`:

```python
def test_shell_escape_permission_context_supports_trusted_read_directories(tmp_path):
    trusted = str(tmp_path / "config" / "tool-results" / "session-1")
    permission_context = ToolPermissionContext(cwd=str(tmp_path), trusted_read_directories=[trusted])
    tool = FakeBashTool(ToolResult.success("unused"))

    repl = make_repl(tool, str(tmp_path), permission_context=permission_context)
    permission_context = repl.store.get_state().permission_context

    assert permission_context.trusted_read_directories == [trusted]
```

- [ ] **Step 3: Run tests to verify they fail**

Run:

```bash
PATH="$HOME/.local/bin:$PATH" uv run pytest tests/services/permissions/test_trusted_roots.py tests/services/test_agent_factory.py::test_create_agent_runtime_adds_session_trusted_read_directories tests/ui/test_repl_shell_escape.py -v
```

Expected: FAIL because the helper and plumbing do not exist.

- [ ] **Step 4: Implement trusted roots helper**

Create `src/iac_code/services/permissions/trusted_roots.py`:

```python
"""Trusted read roots for current-session runtime artifacts."""

from __future__ import annotations

from iac_code.config import get_config_dir


def build_session_trusted_read_directories(session_id: str | None) -> list[str]:
    if not session_id:
        return []
    config_dir = get_config_dir()
    return [
        str(config_dir / "tool-results" / session_id),
        str(config_dir / "image-cache" / session_id),
    ]
```

- [ ] **Step 5: Wire trusted roots in runtime creation**

In `src/iac_code/services/agent_factory.py`, after `permission_context = load_permission_context(...)`, add:

```python
from iac_code.services.permissions.trusted_roots import build_session_trusted_read_directories

permission_context.trusted_read_directories.extend(build_session_trusted_read_directories(session_id))
```

In `src/iac_code/ui/repl.py`, after `permission_context = load_permission_context(...)`, add:

```python
from iac_code.services.permissions.trusted_roots import build_session_trusted_read_directories

permission_context.trusted_read_directories.extend(build_session_trusted_read_directories(self._session_id))
```

- [ ] **Step 6: Run tests to verify the task passes**

Run:

```bash
PATH="$HOME/.local/bin:$PATH" uv run pytest tests/services/permissions/test_trusted_roots.py tests/services/test_agent_factory.py tests/ui/test_repl_shell_escape.py -v
```

Expected: PASS.

- [ ] **Step 7: Commit**

Run:

```bash
git add src/iac_code/services/permissions/trusted_roots.py src/iac_code/services/agent_factory.py src/iac_code/ui/repl.py tests/services/permissions/test_trusted_roots.py tests/services/test_agent_factory.py tests/ui/test_repl_shell_escape.py
PATH="$HOME/.local/bin:/opt/homebrew/bin:$PATH" git commit -m "feat: trust current session read artifacts"
```

---

### Task 3: `read_file` Permission Boundary and Streaming Reads

**Files:**
- Modify: `src/iac_code/tools/read_file.py`
- Test: `tests/tools/test_read_file.py`
- Test: `tests/services/permissions/test_pipeline.py`

- [ ] **Step 1: Write failing permission tests**

Add to `tests/tools/test_read_file.py`:

```python
from iac_code.types.permissions import ToolPermissionContext


class TestReadFilePermissions:
    @pytest.mark.asyncio
    async def test_project_file_allowed(self, tmp_path, read_file_tool):
        target = tmp_path / "app.py"
        target.write_text("print('ok')", encoding="utf-8")
        ctx = ToolPermissionContext(cwd=str(tmp_path))

        result = await read_file_tool.check_permissions({"path": str(target)}, ctx)

        assert result.behavior == "allow"

    @pytest.mark.asyncio
    async def test_outside_project_file_asks(self, tmp_path, read_file_tool):
        project = tmp_path / "project"
        outside = tmp_path / "outside"
        project.mkdir()
        outside.mkdir()
        target = outside / "secret.txt"
        target.write_text("secret", encoding="utf-8")
        ctx = ToolPermissionContext(cwd=str(project))

        result = await read_file_tool.check_permissions({"path": str(target)}, ctx)

        assert result.behavior == "ask"
        assert result.reason.type == "path_constraint"

    @pytest.mark.asyncio
    async def test_sensitive_project_file_asks(self, tmp_path, read_file_tool):
        target = tmp_path / ".env"
        target.write_text("TOKEN=fake", encoding="utf-8")
        ctx = ToolPermissionContext(cwd=str(tmp_path))

        result = await read_file_tool.check_permissions({"path": str(target)}, ctx)

        assert result.behavior == "ask"
        assert result.reason.type == "safety_check"

    @pytest.mark.asyncio
    async def test_trusted_read_directory_allowed(self, tmp_path, read_file_tool):
        project = tmp_path / "project"
        trusted = tmp_path / ".iac-code" / "tool-results" / "session-1"
        project.mkdir()
        trusted.mkdir(parents=True)
        target = trusted / "tool.txt"
        target.write_text("result", encoding="utf-8")
        ctx = ToolPermissionContext(cwd=str(project), trusted_read_directories=[str(trusted)])

        result = await read_file_tool.check_permissions({"path": str(target)}, ctx)

        assert result.behavior == "allow"
```

Add to `tests/services/permissions/test_pipeline.py`:

```python
@pytest.mark.asyncio
async def test_read_file_outside_project_asks(tmp_path):
    from iac_code.services.permissions.pipeline import check_tool_permission
    from iac_code.tools.read_file import ReadFileTool

    project = tmp_path / "project"
    outside = tmp_path / "outside"
    project.mkdir()
    outside.mkdir()
    target = outside / "secret.txt"
    target.write_text("secret", encoding="utf-8")
    ctx = ToolPermissionContext(cwd=str(project))

    result = await check_tool_permission(ReadFileTool(), {"path": str(target)}, ctx)

    assert result.behavior == "ask"
```

- [ ] **Step 2: Write failing streaming tests**

Add to `tests/tools/test_read_file.py`:

```python
@pytest.mark.asyncio
async def test_large_file_is_truncated_by_line_limit(tmp_path, read_file_tool, monkeypatch):
    monkeypatch.setattr("iac_code.tools.read_file.MAX_READ_LINES", 3)
    target = tmp_path / "large.txt"
    target.write_text("one\ntwo\nthree\nfour\nfive\n", encoding="utf-8")
    context = ToolContext(cwd=str(tmp_path))

    result = await read_file_tool.execute(tool_input={"path": str(target)}, context=context)

    assert result.is_error is False
    assert "truncated" in result.content.lower()
    assert "three" in result.content
    assert "four" not in result.content


@pytest.mark.asyncio
async def test_large_file_is_truncated_by_byte_limit(tmp_path, read_file_tool, monkeypatch):
    monkeypatch.setattr("iac_code.tools.read_file.MAX_READ_BYTES", 8)
    target = tmp_path / "large.txt"
    target.write_text("12345\n67890\n", encoding="utf-8")
    context = ToolContext(cwd=str(tmp_path))

    result = await read_file_tool.execute(tool_input={"path": str(target)}, context=context)

    assert result.is_error is False
    assert "truncated" in result.content.lower()
    assert "12345" in result.content
```

- [ ] **Step 3: Run tests to verify they fail**

Run:

```bash
PATH="$HOME/.local/bin:$PATH" uv run pytest tests/tools/test_read_file.py tests/services/permissions/test_pipeline.py::test_read_file_outside_project_asks -v
```

Expected: FAIL because `ReadFileTool.check_permissions()` still auto-allows all reads and no streaming caps exist.

- [ ] **Step 4: Implement read_file permission checks**

In `src/iac_code/tools/read_file.py`, add imports:

```python
from iac_code.tools.path_safety import check_read_path, resolve_candidate
from iac_code.types.permissions import PermissionResult, ToolPermissionContext
```

Add constants near the top:

```python
MAX_READ_BYTES = 10 * 1024 * 1024
MAX_READ_LINES = 50_000
```

Add helper and permission method to `ReadFileTool`:

```python
def _resolve_input_path(path: str, cwd: str) -> str:
    return resolve_candidate(path, cwd)


async def check_permissions(self, input: dict, context=None) -> PermissionResult:
    if not isinstance(context, ToolPermissionContext):
        return await super().check_permissions(input, context)
    path = input.get("path") or input.get("file_path") or ""
    if not path:
        return PermissionResult(behavior="ask", message=_("Allow {}?").format(self.user_facing_name(input)))
    decision = check_read_path(
        path,
        cwd=context.cwd,
        additional_directories=context.additional_directories,
        trusted_read_directories=context.trusted_read_directories,
    )
    if decision.behavior == "allow":
        return PermissionResult(behavior="allow")
    return decision.to_permission_result()
```

- [ ] **Step 5: Replace `readlines()` with capped streaming**

In `ReadFileTool.execute()`, replace the full-file `readlines()` block with:

```python
try:
    selected_lines: list[tuple[int, str]] = []
    total_lines = 0
    bytes_read = 0
    truncated = False
    start = max(1, int(start_line or 1))
    end = int(end_line) if end_line is not None else None

    with open(path, encoding="utf-8") as f:
        for raw_line in f:
            encoded_len = len(raw_line.encode("utf-8"))
            if bytes_read + encoded_len > MAX_READ_BYTES or total_lines >= MAX_READ_LINES:
                truncated = True
                break
            bytes_read += encoded_len
            total_lines += 1
            line_no = total_lines
            if line_no >= start and (end is None or line_no <= end):
                selected_lines.append((line_no, raw_line))
            if end is not None and line_no >= end:
                break
except FileNotFoundError:
    return ToolResult.error(f"File not found: {path}")
except PermissionError:
    return ToolResult.error(f"Permission denied: {path}")
except UnicodeDecodeError:
    return ToolResult.error(f"Cannot read binary file: {path}")
except Exception as e:
    return ToolResult.error(f"Error reading file: {e}")

if selected_lines:
    content = "".join(f"{line_no:>6}\t{line}" for line_no, line in selected_lines)
else:
    content = "(empty file)" if total_lines == 0 else ""

if start_line is not None or end_line is not None:
    range_end = end_line if end_line is not None else total_lines
    header = f"File: {path} (lines {start}-{range_end} of {total_lines}"
else:
    header = f"File: {path} ({total_lines} lines"
if truncated:
    header += ", truncated"
header += ")"

return ToolResult.success(f"{header}\n\n{content}")
```

Keep existing UI rendering methods unchanged.

- [ ] **Step 6: Run tests to verify the task passes**

Run:

```bash
PATH="$HOME/.local/bin:$PATH" uv run pytest tests/tools/test_read_file.py tests/services/permissions/test_pipeline.py -v
```

Expected: PASS.

- [ ] **Step 7: Commit**

Run:

```bash
git add src/iac_code/tools/read_file.py tests/tools/test_read_file.py tests/services/permissions/test_pipeline.py
PATH="$HOME/.local/bin:/opt/homebrew/bin:$PATH" git commit -m "fix: constrain read_file permissions and size"
```

---

### Task 4: Bash Dangerous Readonly Arguments and Shared Arg Helpers

**Files:**
- Create: `src/iac_code/tools/bash/argv_safety.py`
- Modify: `src/iac_code/tools/bash/readonly_commands.py`
- Modify: `src/iac_code/tools/bash/permissions.py`
- Test: `tests/tools/bash/test_readonly_commands.py`
- Test: `tests/tools/bash/test_permissions.py`

- [ ] **Step 1: Write failing readonly classification tests**

Add to `tests/tools/bash/test_readonly_commands.py`:

```python
class TestDangerousReadonlyArguments:
    @pytest.mark.parametrize(
        "cmd",
        [
            "find . -delete",
            "find . -exec sh -c 'echo marker' ;",
            "find . -execdir sh -c 'echo marker' ;",
            "find . -ok sh -c 'echo marker' ;",
            "fd . -x sh -c 'echo marker'",
            "fd . -X sh -c 'echo marker'",
            "fd . --exec sh -c 'echo marker'",
            "fd . --exec=echo",
            "rg --pre 'sh -c echo-marker' needle .",
            "rg --pre=cat needle .",
            "sort --compress-program=sh file.txt",
        ],
    )
    def test_dangerous_args_are_not_readonly(self, cmd):
        assert is_command_readonly(SimpleCommand(text=cmd, argv=cmd.split(), redirects=[])) is False

    @pytest.mark.parametrize(
        "cmd",
        [
            "find . -name '*.py'",
            "fd pattern src",
            "rg needle src",
            "sort file.txt",
        ],
    )
    def test_safe_args_remain_readonly(self, cmd):
        assert is_command_readonly(SimpleCommand(text=cmd, argv=cmd.split(), redirects=[])) is True


class TestPipLikeBase:
    @pytest.mark.parametrize("cmd", ["pip list", "pip3 list", "pip3.11 list"])
    def test_versioned_pip_readonly(self, cmd):
        assert is_command_readonly(SimpleCommand(text=cmd, argv=cmd.split(), redirects=[])) is True

    @pytest.mark.parametrize("cmd", ["pipx list", "pip-audit list", "pip-compile list", "pipeline-deploy list"])
    def test_non_pip_prefixes_not_readonly(self, cmd):
        assert is_command_readonly(SimpleCommand(text=cmd, argv=cmd.split(), redirects=[])) is False
```

Add sed execution tests:

```python
class TestSedExecutionScripts:
    @pytest.mark.parametrize(
        "cmd",
        [
            "sed -n '1e echo marker' file.txt",
            "sed 's/.*/echo marker/e' file.txt",
        ],
    )
    def test_sed_execution_scripts_not_readonly(self, cmd):
        assert is_command_readonly(SimpleCommand(text=cmd, argv=cmd.split(), redirects=[])) is False
```

- [ ] **Step 2: Write failing permission tests**

Add to `tests/tools/bash/test_permissions.py`:

```python
class TestDangerousReadonlyArgumentPermission:
    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "command",
        [
            "find . -delete",
            "find . -exec sh -c 'echo marker' ;",
            "fd . -x sh -c 'echo marker'",
            "sed -n '1e echo marker' file.txt",
            "sed 's/.*/echo marker/e' file.txt",
            "rg --pre cat needle .",
            "sort --compress-program=sh file.txt",
        ],
    )
    async def test_dangerous_readonly_args_ask(self, command):
        result = await bash_tool_has_permission(command, _ctx())
        assert result.behavior == "ask"
        assert result.reason.type == "dangerous_readonly_argument"
```

- [ ] **Step 3: Run tests to verify they fail**

Run:

```bash
PATH="$HOME/.local/bin:$PATH" uv run pytest tests/tools/bash/test_readonly_commands.py tests/tools/bash/test_permissions.py::TestDangerousReadonlyArgumentPermission -v
```

Expected: FAIL because dangerous args are currently classified as readonly or passthrough.

- [ ] **Step 4: Implement argv safety helpers**

Create `src/iac_code/tools/bash/argv_safety.py`:

```python
"""Argument-level safety helpers for bash permission checks."""

from __future__ import annotations

import os
import re

DANGEROUS_READONLY_REASON = "dangerous readonly argument requires confirmation"


def basename(argv0: str) -> str:
    return os.path.basename(argv0)


def pip_like_base(base: str) -> bool:
    return base == "pip" or re.fullmatch(r"pip\d+(?:\.\d+)*", base) is not None


def sed_inplace_edit(argv: list[str]) -> bool:
    for arg in argv[1:]:
        if arg.startswith("--in-place"):
            return True
        if arg == "-i":
            return True
        if len(arg) > 2 and arg.startswith("-i") and arg[2] in "./":
            return True
    return False


def sed_executes_shell(argv: list[str]) -> bool:
    for arg in argv[1:]:
        if arg.startswith("-"):
            continue
        if re.search(r"(?:^|[;{\n])\s*\d*\s*e(?:\s|$)", arg):
            return True
        if re.search(r"s(.).*\1.*\1[a-zA-Z]*e[a-zA-Z]*$", arg):
            return True
    return False


def dangerous_readonly_argument(argv: list[str]) -> str | None:
    if not argv:
        return None
    base = basename(argv[0])
    args = argv[1:]

    if base == "find":
        for arg in args:
            if arg in {"-delete", "-exec", "-execdir", "-ok"}:
                return arg
    if base == "fd":
        for arg in args:
            if arg in {"-x", "-X", "--exec"} or arg.startswith("--exec="):
                return arg
    if base == "sed":
        if sed_inplace_edit(argv):
            return "sed in-place edit"
        if sed_executes_shell(argv):
            return "sed shell execution"
    if base == "rg":
        for arg in args:
            if arg == "--pre" or arg.startswith("--pre="):
                return arg
    if base == "sort":
        for arg in args:
            if arg == "--compress-program" or arg.startswith("--compress-program="):
                return arg
    return None
```

- [ ] **Step 5: Use helpers in readonly classification**

In `src/iac_code/tools/bash/readonly_commands.py`:

```python
from iac_code.tools.bash.argv_safety import basename as _basename
from iac_code.tools.bash.argv_safety import dangerous_readonly_argument, pip_like_base
from iac_code.tools.bash.argv_safety import sed_inplace_edit
```

Remove the local `_basename()`, `_sed_inplace_edit()`, and `_pip_like_base()` implementations.

Update references:

```python
if base == "sed" and sed_inplace_edit(argv):
    return False

if dangerous_readonly_argument(argv) is not None:
    return False

if pip_like_base(base) and verb in _PIP_READONLY_VERBS:
    return True
```

- [ ] **Step 6: Return ask in bash permission engine**

In `src/iac_code/tools/bash/permissions.py`, import helpers:

```python
from iac_code.tools.bash.argv_safety import dangerous_readonly_argument, sed_inplace_edit
```

Remove the local `_sed_inplace_edit()` function. Replace `_sed_inplace_edit(cmd.argv)` with `sed_inplace_edit(cmd.argv)`.

Before `mode_res = check_permission_mode(cmd, context.mode)`, add:

```python
dangerous_arg = dangerous_readonly_argument(cmd.argv)
if dangerous_arg is not None:
    detail = _("{}: {}").format(_("complex command requires confirmation"), dangerous_arg)
    return PermissionResult(
        behavior="ask",
        message=detail,
        reason=PermissionDecisionReason(type="dangerous_readonly_argument", detail=detail),
    )
```

This reuses existing translatable text and avoids adding new catalog entries.

- [ ] **Step 7: Run tests to verify the task passes**

Run:

```bash
PATH="$HOME/.local/bin:$PATH" uv run pytest tests/tools/bash/test_readonly_commands.py tests/tools/bash/test_permissions.py -v
```

Expected: PASS.

- [ ] **Step 8: Commit**

Run:

```bash
git add src/iac_code/tools/bash/argv_safety.py src/iac_code/tools/bash/readonly_commands.py src/iac_code/tools/bash/permissions.py tests/tools/bash/test_readonly_commands.py tests/tools/bash/test_permissions.py
PATH="$HOME/.local/bin:/opt/homebrew/bin:$PATH" git commit -m "fix: guard dangerous readonly bash arguments"
```

---

### Task 5: Bash Read Command Path Constraints

**Files:**
- Modify: `src/iac_code/tools/bash/argv_safety.py`
- Modify: `src/iac_code/tools/bash/path_validation.py`
- Modify: `src/iac_code/tools/bash/permissions.py`
- Test: `tests/tools/bash/test_path_validation.py`
- Test: `tests/tools/bash/test_permissions.py`

- [ ] **Step 1: Write failing read-path extraction tests**

Add to `tests/tools/bash/test_path_validation.py`:

```python
class TestReadPathConstraints:
    def test_cat_outside_cwd_asks(self, tmp_path):
        cmd = SimpleCommand(text="cat /etc/passwd", argv=["cat", "/etc/passwd"], redirects=[])
        result = check_read_path_constraints(cmd, str(tmp_path), [], [])
        assert result.behavior == "ask"
        assert result.reason.type == "path_constraint"

    def test_cat_inside_cwd_passthrough(self, tmp_path):
        target = tmp_path / "file.txt"
        target.write_text("ok", encoding="utf-8")
        cmd = SimpleCommand(text="cat file.txt", argv=["cat", "file.txt"], redirects=[])
        result = check_read_path_constraints(cmd, str(tmp_path), [], [])
        assert result.behavior == "passthrough"

    def test_grep_sensitive_path_asks(self, tmp_path):
        cmd = SimpleCommand(text="grep -R token ~/.iac-code", argv=["grep", "-R", "token", "~/.iac-code"], redirects=[])
        result = check_read_path_constraints(cmd, str(tmp_path), [], [])
        assert result.behavior == "ask"
        assert result.reason.type == "safety_check"

    def test_grep_pattern_is_not_treated_as_path(self, tmp_path):
        target = tmp_path / "file.txt"
        target.write_text("needle", encoding="utf-8")
        cmd = SimpleCommand(text="grep needle file.txt", argv=["grep", "needle", "file.txt"], redirects=[])
        result = check_read_path_constraints(cmd, str(tmp_path), [], [])
        assert result.behavior == "passthrough"
```

Import `check_read_path_constraints` from `iac_code.tools.bash.path_validation`.

- [ ] **Step 2: Write failing permission integration tests**

Add to `tests/tools/bash/test_permissions.py`:

```python
class TestReadCommandPathPermission:
    @pytest.mark.asyncio
    async def test_cat_outside_project_asks(self, tmp_path):
        ctx = _ctx(cwd=str(tmp_path))
        result = await bash_tool_has_permission("cat /etc/passwd", ctx)
        assert result.behavior == "ask"
        assert result.reason.type == "path_constraint"

    @pytest.mark.asyncio
    async def test_cat_project_file_allows(self, tmp_path):
        target = tmp_path / "file.txt"
        target.write_text("ok", encoding="utf-8")
        ctx = _ctx(cwd=str(tmp_path))
        result = await bash_tool_has_permission("cat file.txt", ctx)
        assert result.behavior == "allow"

    @pytest.mark.asyncio
    async def test_trusted_read_directory_allows(self, tmp_path):
        project = tmp_path / "project"
        trusted = tmp_path / "config" / "tool-results" / "session-1"
        project.mkdir()
        trusted.mkdir(parents=True)
        target = trusted / "result.txt"
        target.write_text("ok", encoding="utf-8")
        ctx = ToolPermissionContext(cwd=str(project), trusted_read_directories=[str(trusted)])
        result = await bash_tool_has_permission(f"cat {target}", ctx)
        assert result.behavior == "allow"
```

- [ ] **Step 3: Run tests to verify they fail**

Run:

```bash
PATH="$HOME/.local/bin:$PATH" uv run pytest tests/tools/bash/test_path_validation.py::TestReadPathConstraints tests/tools/bash/test_permissions.py::TestReadCommandPathPermission -v
```

Expected: FAIL because read-path constraints do not exist.

- [ ] **Step 4: Add read-path extraction helpers**

In `src/iac_code/tools/bash/argv_safety.py`, add:

```python
_READ_PATH_COMMANDS = frozenset(
    {
        "cat",
        "head",
        "tail",
        "less",
        "more",
        "wc",
        "file",
        "stat",
        "du",
        "grep",
        "egrep",
        "fgrep",
        "rg",
        "ag",
        "ack",
        "find",
        "fd",
        "sed",
        "sort",
        "uniq",
        "cut",
    }
)

_FLAGS_WITH_VALUE = frozenset(
    {
        "-e",
        "-f",
        "-m",
        "-n",
        "-A",
        "-B",
        "-C",
        "--after-context",
        "--before-context",
        "--context",
        "--max-count",
        "--regexp",
        "--file",
        "--type",
        "-t",
    }
)


def _looks_like_path(token: str) -> bool:
    return (
        token.startswith("/")
        or token.startswith("~/")
        or token.startswith("./")
        or token.startswith("../")
        or "/" in token
        or token in {".", ".."}
    )


def extract_read_paths(argv: list[str]) -> list[str]:
    if not argv:
        return []
    base = basename(argv[0])
    if base not in _READ_PATH_COMMANDS:
        return []

    args = argv[1:]
    paths: list[str] = []
    seen_double_dash = False
    non_flag_positionals: list[str] = []
    i = 0
    while i < len(args):
        arg = args[i]
        if seen_double_dash:
            non_flag_positionals.append(arg)
            i += 1
            continue
        if arg == "--":
            seen_double_dash = True
            i += 1
            continue
        if arg in _FLAGS_WITH_VALUE and i + 1 < len(args):
            i += 2
            continue
        if arg.startswith("--") and "=" in arg:
            i += 1
            continue
        if arg.startswith("-") and len(arg) > 1:
            i += 1
            continue
        non_flag_positionals.append(arg)
        i += 1

    if base in {"grep", "egrep", "fgrep", "rg", "ag", "ack"} and non_flag_positionals:
        paths.extend(non_flag_positionals[1:])
    elif base == "sed" and non_flag_positionals:
        paths.extend(non_flag_positionals[1:])
    elif base == "fd" and non_flag_positionals:
        paths.extend(non_flag_positionals[1:])
    else:
        paths.extend(non_flag_positionals)

    return [p for p in paths if _looks_like_path(p) or base in {"cat", "head", "tail", "less", "more", "wc", "file", "stat", "du", "sort", "uniq", "cut"}]
```

- [ ] **Step 5: Add read path constraints**

In `src/iac_code/tools/bash/path_validation.py`, import helpers:

```python
from iac_code.tools.bash.argv_safety import extract_read_paths
from iac_code.tools.path_safety import check_read_path
```

Add:

```python
def check_read_path_constraints(
    cmd: SimpleCommand,
    cwd: str,
    additional_directories: list[str],
    trusted_read_directories: list[str],
) -> PermissionResult:
    candidates = list(dict.fromkeys(extract_read_paths(cmd.argv)))
    if not candidates:
        return PermissionResult(behavior="passthrough")

    for rel_or_abs in candidates:
        decision = check_read_path(
            rel_or_abs,
            cwd=cwd,
            additional_directories=additional_directories,
            trusted_read_directories=trusted_read_directories,
        )
        if decision.behavior == "ask":
            return decision.to_permission_result()

    return PermissionResult(behavior="passthrough")
```

- [ ] **Step 6: Run read constraints before readonly auto-allow**

In `src/iac_code/tools/bash/permissions.py`, import `check_read_path_constraints`:

```python
from iac_code.tools.bash.path_validation import check_path_constraints, check_read_path_constraints
```

After write `path_res = check_path_constraints(...)`, add:

```python
read_path_res = check_read_path_constraints(
    cmd,
    context.cwd,
    context.additional_directories,
    context.trusted_read_directories,
)
if read_path_res.behavior != "passthrough":
    return read_path_res
```

- [ ] **Step 7: Run tests to verify the task passes**

Run:

```bash
PATH="$HOME/.local/bin:$PATH" uv run pytest tests/tools/bash/test_path_validation.py tests/tools/bash/test_permissions.py tests/tools/bash/test_permissions_integration.py -v
```

Expected: PASS.

- [ ] **Step 8: Commit**

Run:

```bash
git add src/iac_code/tools/bash/argv_safety.py src/iac_code/tools/bash/path_validation.py src/iac_code/tools/bash/permissions.py tests/tools/bash/test_path_validation.py tests/tools/bash/test_permissions.py
PATH="$HOME/.local/bin:/opt/homebrew/bin:$PATH" git commit -m "fix: constrain bash read command paths"
```

---

### Task 6: `web_fetch` Streaming Download Cap

**Files:**
- Modify: `src/iac_code/tools/web_fetch.py`
- Test: `tests/test_tools/test_web_fetch.py`

- [ ] **Step 1: Write failing streaming tests**

Add to `tests/test_tools/test_web_fetch.py`:

```python
class AsyncByteStream:
    def __init__(self, chunks):
        self.chunks = chunks
        self.consumed = 0

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return False

    def raise_for_status(self):
        return None

    @property
    def headers(self):
        return {"content-type": "text/plain; charset=utf-8"}

    @property
    def encoding(self):
        return "utf-8"

    async def aiter_bytes(self):
        for chunk in self.chunks:
            self.consumed += 1
            yield chunk


@pytest.mark.asyncio
async def test_streaming_stops_at_download_byte_cap(web_fetch_tool, context, monkeypatch):
    monkeypatch.setattr("iac_code.tools.web_fetch.MAX_DOWNLOAD_BYTES", 5)
    stream = AsyncByteStream([b"abc", b"def", b"ghi"])
    mock_client = AsyncMock()
    mock_client.__aenter__ = AsyncMock(return_value=mock_client)
    mock_client.__aexit__ = AsyncMock(return_value=False)
    mock_client.stream = MagicMock(return_value=stream)

    with patch("iac_code.tools.web_fetch.httpx.AsyncClient", return_value=mock_client):
        result = await web_fetch_tool.execute(tool_input={"url": "https://example.com", "max_length": 100}, context=context)

    assert result.is_error is False
    assert "abcde" in result.content
    assert "truncated" in result.content.lower()
    assert stream.consumed == 2


@pytest.mark.asyncio
async def test_streaming_html_still_strips_tags(web_fetch_tool, context):
    class HtmlStream(AsyncByteStream):
        @property
        def headers(self):
            return {"content-type": "text/html; charset=utf-8"}

    stream = HtmlStream([b"<html><body><h1>Hello</h1><script>bad()</script></body></html>"])
    mock_client = AsyncMock()
    mock_client.__aenter__ = AsyncMock(return_value=mock_client)
    mock_client.__aexit__ = AsyncMock(return_value=False)
    mock_client.stream = MagicMock(return_value=stream)

    with patch("iac_code.tools.web_fetch.httpx.AsyncClient", return_value=mock_client):
        result = await web_fetch_tool.execute(tool_input={"url": "https://example.com"}, context=context)

    assert result.is_error is False
    assert "Hello" in result.content
    assert "bad" not in result.content
```

- [ ] **Step 2: Run tests to verify they fail**

Run:

```bash
PATH="$HOME/.local/bin:$PATH" uv run pytest tests/test_tools/test_web_fetch.py::test_streaming_stops_at_download_byte_cap tests/test_tools/test_web_fetch.py::test_streaming_html_still_strips_tags -v
```

Expected: FAIL because `WebFetchTool` still uses `client.get()` and `response.text`.

- [ ] **Step 3: Implement streaming download cap**

In `src/iac_code/tools/web_fetch.py`, add:

```python
MAX_DOWNLOAD_BYTES = 10 * 1024 * 1024
```

Replace the `client.get()` block with:

```python
async with client.stream("GET", url) as response:
    response.raise_for_status()
    content_type = response.headers.get("content-type", "")
    chunks: list[bytes] = []
    bytes_read = 0
    truncated = False

    async for chunk in response.aiter_bytes():
        if not chunk:
            continue
        remaining = MAX_DOWNLOAD_BYTES - bytes_read
        if remaining <= 0:
            truncated = True
            break
        if len(chunk) > remaining:
            chunks.append(chunk[:remaining])
            bytes_read += remaining
            truncated = True
            break
        chunks.append(chunk)
        bytes_read += len(chunk)

    raw = b"".join(chunks)
    encoding = getattr(response, "encoding", None) or "utf-8"
    text = raw.decode(encoding, errors="replace")

    if "text/html" in content_type:
        text = _extract_text_from_html(text)

    if len(text) > max_length:
        text = text[:max_length]

    if truncated:
        text += "\n\n[truncated]"

    return ToolResult.success(text)
```

Use the plain `"[truncated]"` marker exactly as shown. It is part of tool output content rather than UI chrome, and this plan avoids adding a new translation catalog entry.

- [ ] **Step 4: Update old WebFetch mocks**

Existing tests in `tests/test_tools/test_web_fetch.py` mock `client.get()`. Convert those mocks to `client.stream()` by using `AsyncByteStream` with:

```python
stream = AsyncByteStream([text_response.encode("utf-8")])
mock_client.stream = MagicMock(return_value=stream)
```

For HTTP error tests, set:

```python
mock_client.stream = MagicMock(side_effect=httpx.HTTPError("Connection failed"))
```

- [ ] **Step 5: Run tests to verify the task passes**

Run:

```bash
PATH="$HOME/.local/bin:$PATH" uv run pytest tests/test_tools/test_web_fetch.py -v
```

Expected: PASS.

- [ ] **Step 6: Commit**

Run:

```bash
git add src/iac_code/tools/web_fetch.py tests/test_tools/test_web_fetch.py
PATH="$HOME/.local/bin:/opt/homebrew/bin:$PATH" git commit -m "fix: stream and cap web fetch downloads"
```

---

### Task 7: Final Integration Verification

**Files:**
- Inspect: repository status and verification outputs.
- Commit: verification fixes only when a focused failure is caused by the task changes.

- [ ] **Step 1: Run focused security and resource tests**

Run:

```bash
PATH="$HOME/.local/bin:$PATH" uv run pytest tests/tools/test_path_safety.py tests/tools/bash tests/tools/test_read_file.py tests/test_tools/test_web_fetch.py tests/services/permissions tests/types/test_permissions_types.py -v
```

Expected: PASS.

- [ ] **Step 2: Run lint and type checks**

Run:

```bash
PATH="$HOME/.local/bin:$PATH" make lint
```

Expected: PASS.

- [ ] **Step 3: Run full test suite**

Run:

```bash
PATH="$HOME/.local/bin:$PATH" make test
```

Expected: PASS, or FAIL only on the pre-existing `tests/test_i18n.py` failures caused by missing `src/iac_code/i18n/messages.pot`.

- [ ] **Step 4: Resolve unexpected verification failures**

When Step 3 reports failures outside the known `tests/test_i18n.py` POT-file failures, run each failing test directly, fix the touched implementation, and rerun the focused command from Step 1.

Use this loop:

```bash
PATH="$HOME/.local/bin:$PATH" uv run pytest <failing-test-nodeid> -v
PATH="$HOME/.local/bin:$PATH" uv run pytest tests/tools/test_path_safety.py tests/tools/bash tests/tools/test_read_file.py tests/test_tools/test_web_fetch.py tests/services/permissions tests/types/test_permissions_types.py -v
```

Expected: focused failures are fixed before final status reporting.

- [ ] **Step 5: Check status and summarize known baseline**

Run:

```bash
git status --short --branch
```

Expected: clean except verification fixes that still need to be committed. Record whether `make test` passed or failed only on the known i18n baseline.

- [ ] **Step 6: Final commit for verification fixes**

When Step 4 changed files, commit them:

```bash
git add src tests
PATH="$HOME/.local/bin:/opt/homebrew/bin:$PATH" git commit -m "test: stabilize tool safety verification"
```
