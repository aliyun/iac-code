# Review Fixes Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Fix every actionable item in `/Users/ehzyo/open_repo/iac-code3/docs/review.md`, including the historical hardening section, then review and repair until no review findings remain.

**Architecture:** Add a small shared state I/O foundation, then route recovery-relevant A2A and pipeline state changes through durable persistence boundaries. Keep cleanup service truth in the private ledger, keep the public A2A snapshot display-only, and close Windows, i18n, documentation, and minor compatibility gaps without changing normal chat behavior except where the review explicitly calls out shared storage overhead.

**Tech Stack:** Python 3.10, pytest, PyYAML, JSONL, existing A2A/pipeline modules under `/Users/ehzyo/open_repo/iac-code3/src/iac_code`, existing `uv`/Make targets.

---

## File Structure

- Create `/Users/ehzyo/open_repo/iac-code3/src/iac_code/utils/state_io.py`: review-scoped atomic write, fsync, parent-dir fsync, replace retry, and JSONL append lock helpers.
- Create `/Users/ehzyo/open_repo/iac-code3/tests/utils/test_state_io.py`: focused tests for atomic text/YAML/JSON writes, retry, parent fsync best-effort, and JSONL append serialization.
- Modify `/Users/ehzyo/open_repo/iac-code3/src/iac_code/a2a/pipeline_journal.py`: durable append, durable append groups, group replay, strict tail repair preservation.
- Modify `/Users/ehzyo/open_repo/iac-code3/src/iac_code/a2a/pipeline_snapshot.py`: use state I/O helper for snapshot replace, preserve public cleanup sanitization.
- Modify `/Users/ehzyo/open_repo/iac-code3/src/iac_code/a2a/pipeline_stream.py`: centralized durable-event classifier, durable publication gate, durable artifact metadata handling.
- Modify `/Users/ehzyo/open_repo/iac-code3/src/iac_code/a2a/pipeline_executor.py`: active sidecar mismatch error, cancel handoff event group, private-ledger cleanup handoff data.
- Modify `/Users/ehzyo/open_repo/iac-code3/src/iac_code/a2a/executor.py`: cleanup state unavailable behavior, A2A image i18n, deferred cleanup prompt handling.
- Modify `/Users/ehzyo/open_repo/iac-code3/src/iac_code/pipeline/engine/cleanup.py`: in-process ledger serialization, merge rules, tool-use mapping, corrupt-ledger fail-closed reporting, English msgids.
- Create `/Users/ehzyo/open_repo/iac-code3/src/iac_code/pipeline/engine/constants.py`: low-dependency cleanup prompt metadata type and cleanup event names.
- Modify `/Users/ehzyo/open_repo/iac-code3/src/iac_code/pipeline/engine/pipeline_runner.py`: sidecar save errors become hard pipeline errors, no downstream work after persistence failure, observed-resource write failure surfacing.
- Modify `/Users/ehzyo/open_repo/iac-code3/src/iac_code/pipeline/engine/session.py`: use state I/O helper for sidecar YAML files while keeping accepted two-file residual risk.
- Modify `/Users/ehzyo/open_repo/iac-code3/src/iac_code/services/session_storage.py`: atomic full-file save, opt-in cleanup prompt preservation, locked JSONL append helper, Windows-safe legacy migration.
- Modify `/Users/ehzyo/open_repo/iac-code3/src/iac_code/services/session_index.py`: shared cleanup constant and broader legacy cleanup prompt hiding.
- Modify `/Users/ehzyo/open_repo/iac-code3/src/iac_code/tools/base.py`: restore `ToolContext` positional compatibility.
- Modify `/Users/ehzyo/open_repo/iac-code3/src/iac_code/tools/read_file.py`: reuse cross-platform path normalization.
- Modify `/Users/ehzyo/open_repo/iac-code3/src/iac_code/tools/cloud/base_stack.py`: do not emit empty observed resource ids.
- Modify `/Users/ehzyo/open_repo/iac-code3/src/iac_code/ui/repl.py`: damaged sidecar fallback, Windows signal fallback, cleanup scan reduction, English cleanup UI strings.
- Modify `/Users/ehzyo/open_repo/iac-code3/scripts/a2a/debugger.py`: display delivery task/context aliases and image docs alignment.
- Modify `/Users/ehzyo/open_repo/iac-code3/scripts/a2a/selling_console.py`: Windows socket reuse behavior, delivery alias display, KeyboardInterrupt shutdown, module docstring.
- Modify `/Users/ehzyo/open_repo/iac-code3/scripts/repl/e2e/run_pipeline_scenarios.py`: POSIX guard and Windows-safe command parsing.
- Modify `/Users/ehzyo/open_repo/iac-code3/src/iac_code/utils/image/store.py`: document or improve Windows privacy behavior through existing `file_security` utilities.
- Modify `/Users/ehzyo/open_repo/iac-code3/src/iac_code/i18n/locales/zh/LC_MESSAGES/messages.po` and sibling catalogs only through `make translate`.
- Create `/Users/ehzyo/open_repo/iac-code3/docs/pipeline-schema-reference.md`: formal schema reference.
- Create `/Users/ehzyo/open_repo/iac-code3/scripts/repl/e2e/README.md`: English REPL E2E docs.
- Create `/Users/ehzyo/open_repo/iac-code3/docs/review-fix-summary.md`: closure matrix after implementation and verification.

## Task 1: State I/O Foundation

**Files:**
- Create: `/Users/ehzyo/open_repo/iac-code3/src/iac_code/utils/state_io.py`
- Create: `/Users/ehzyo/open_repo/iac-code3/tests/utils/test_state_io.py`
- Modify: `/Users/ehzyo/open_repo/iac-code3/src/iac_code/utils/file_security.py`

- [ ] **Step 1: Write failing tests for atomic state writes**

Add `/Users/ehzyo/open_repo/iac-code3/tests/utils/test_state_io.py` with these tests:

```python
from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from iac_code.utils.state_io import append_jsonl_locked, atomic_write_json, atomic_write_text


def test_atomic_write_text_replaces_file_and_removes_temp(tmp_path: Path) -> None:
    path = tmp_path / "state.txt"
    path.write_text("old", encoding="utf-8")

    atomic_write_text(path, "new", durable=True)

    assert path.read_text(encoding="utf-8") == "new"
    assert not list(tmp_path.glob(".state.txt.*.tmp"))


def test_atomic_write_json_fails_without_overwriting_target(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    path = tmp_path / "state.json"
    path.write_text('{"ok": true}\n', encoding="utf-8")

    def fail_replace(src: str, dst: str) -> None:
        raise PermissionError("locked")

    monkeypatch.setattr("iac_code.utils.state_io.os.replace", fail_replace)

    with pytest.raises(PermissionError, match="locked"):
        atomic_write_json(path, {"ok": False}, durable=True, replace_attempts=1)

    assert path.read_text(encoding="utf-8") == '{"ok": true}\n'
    assert not list(tmp_path.glob(".state.json.*.tmp"))


def test_append_jsonl_locked_writes_one_complete_line_per_record(tmp_path: Path) -> None:
    path = tmp_path / "session.jsonl"

    append_jsonl_locked(path, [{"a": 1}, {"b": 2}], durable=False)

    lines = path.read_text(encoding="utf-8").splitlines()
    assert [json.loads(line) for line in lines] == [{"a": 1}, {"b": 2}]


def test_parent_directory_fsync_is_best_effort(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    path = tmp_path / "state.txt"
    calls: list[int] = []
    original_fsync = os.fsync

    def flaky_fsync(fd: int) -> None:
        calls.append(fd)
        if len(calls) > 1:
            raise OSError("directory fsync unsupported")
        original_fsync(fd)

    monkeypatch.setattr("iac_code.utils.state_io.os.fsync", flaky_fsync)

    atomic_write_text(path, "ok", durable=True)

    assert path.read_text(encoding="utf-8") == "ok"
```

- [ ] **Step 2: Run the new state I/O tests and verify they fail**

Run:

```bash
uv run pytest tests/utils/test_state_io.py -q
```

Expected: FAIL because `iac_code.utils.state_io` does not exist.

- [ ] **Step 3: Implement the state I/O helper**

Create `/Users/ehzyo/open_repo/iac-code3/src/iac_code/utils/state_io.py`:

```python
"""Durable state-file I/O helpers for recovery-critical files."""

from __future__ import annotations

import json
import os
import sys
import tempfile
import threading
import time
from collections.abc import Iterable
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator

_PATH_LOCKS: dict[Path, threading.RLock] = {}
_PATH_LOCKS_LOCK = threading.Lock()


def _path_lock(path: Path) -> threading.RLock:
    resolved = path.resolve()
    with _PATH_LOCKS_LOCK:
        lock = _PATH_LOCKS.get(resolved)
        if lock is None:
            lock = threading.RLock()
            _PATH_LOCKS[resolved] = lock
        return lock


def safe_replace(src: str | Path, dst: str | Path, *, attempts: int = 3, delay: float = 0.05) -> None:
    for attempt in range(attempts):
        try:
            os.replace(src, dst)
            return
        except PermissionError:
            if attempt >= attempts - 1:
                raise
            time.sleep(delay * (attempt + 1))


def fsync_parent_dir(path: Path) -> None:
    if sys.platform == "win32":
        return
    try:
        fd = os.open(str(path.parent), os.O_RDONLY)
    except OSError:
        return
    try:
        try:
            os.fsync(fd)
        except OSError:
            return
    finally:
        os.close(fd)


def atomic_write_bytes(
    path: str | Path,
    content: bytes,
    *,
    durable: bool = True,
    replace_attempts: int = 3,
) -> None:
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(prefix=f".{target.name}.", suffix=".tmp", dir=str(target.parent))
    tmp_path = Path(tmp_name)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(content)
            handle.flush()
            if durable:
                os.fsync(handle.fileno())
        safe_replace(tmp_path, target, attempts=replace_attempts)
        if durable:
            fsync_parent_dir(target)
    except Exception:
        try:
            tmp_path.unlink()
        except FileNotFoundError:
            pass
        raise


def atomic_write_text(
    path: str | Path,
    content: str,
    *,
    encoding: str = "utf-8",
    durable: bool = True,
    replace_attempts: int = 3,
) -> None:
    atomic_write_bytes(path, content.encode(encoding), durable=durable, replace_attempts=replace_attempts)


def atomic_write_json(
    path: str | Path,
    value: Any,
    *,
    durable: bool = True,
    replace_attempts: int = 3,
) -> None:
    content = json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True, allow_nan=False) + "\n"
    atomic_write_text(path, content, durable=durable, replace_attempts=replace_attempts)


@contextmanager
def _cross_process_append_lock(path: Path) -> Iterator[None]:
    lock_path = path.with_name(f".{path.name}.lock")
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with lock_path.open("a+b") as lock_file:
        if sys.platform == "win32":
            import msvcrt

            try:
                msvcrt.locking(lock_file.fileno(), msvcrt.LK_LOCK, 1)
            except OSError as exc:
                raise RuntimeError(f"could not acquire append lock for {path}") from exc
            try:
                yield
            finally:
                lock_file.seek(0)
                msvcrt.locking(lock_file.fileno(), msvcrt.LK_UNLCK, 1)
        else:
            import fcntl

            try:
                fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX)
            except OSError as exc:
                raise RuntimeError(f"could not acquire append lock for {path}") from exc
            try:
                yield
            finally:
                fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)


def append_jsonl_locked(
    path: str | Path,
    records: Iterable[dict[str, Any]],
    *,
    durable: bool = False,
) -> None:
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    lines = [json.dumps(record, ensure_ascii=False, separators=(",", ":"), allow_nan=False) + "\n" for record in records]
    if not lines:
        return
    with _path_lock(target):
        with _cross_process_append_lock(target):
            with target.open("ab") as handle:
                for line in lines:
                    handle.write(line.encode("utf-8"))
                handle.flush()
                if durable:
                    os.fsync(handle.fileno())
```

- [ ] **Step 4: Re-export compatible helpers where existing code imports `file_security.safe_replace`**

Modify `/Users/ehzyo/open_repo/iac-code3/src/iac_code/utils/file_security.py` so `safe_replace` delegates to `state_io.safe_replace` and `atomic_write_text` delegates to `state_io.atomic_write_text`:

```python
from iac_code.utils.state_io import atomic_write_text as durable_atomic_write_text
from iac_code.utils.state_io import safe_replace as durable_safe_replace


def safe_replace(src: str, dst: str) -> None:
    """os.replace with retry for Windows file locking."""
    durable_safe_replace(src, dst)


def atomic_write_text(path: Path, content: str, *, encoding: str = "utf-8") -> None:
    """Atomically replace *path* with text content."""
    durable_atomic_write_text(path, content, encoding=encoding, durable=True)
```

- [ ] **Step 5: Run state I/O tests**

Run:

```bash
uv run pytest tests/utils/test_state_io.py tests/services/capabilities/test_auto_detect.py -q
```

Expected: PASS.

- [ ] **Step 6: Commit Task 1**

Run:

```bash
git add src/iac_code/utils/state_io.py src/iac_code/utils/file_security.py tests/utils/test_state_io.py
git commit -m "fix: add durable state file helpers"
```

## Task 2: Session Storage Durability And Compatibility

**Files:**
- Modify: `/Users/ehzyo/open_repo/iac-code3/src/iac_code/services/session_storage.py`
- Modify: `/Users/ehzyo/open_repo/iac-code3/src/iac_code/pipeline/engine/constants.py`
- Modify: `/Users/ehzyo/open_repo/iac-code3/src/iac_code/pipeline/engine/cleanup.py`
- Modify: `/Users/ehzyo/open_repo/iac-code3/src/iac_code/services/session_index.py`
- Test: `/Users/ehzyo/open_repo/iac-code3/tests/services/test_session_storage.py`
- Test: `/Users/ehzyo/open_repo/iac-code3/tests/services/test_session_index.py`

- [ ] **Step 1: Write failing SessionStorage tests**

Append to `/Users/ehzyo/open_repo/iac-code3/tests/services/test_session_storage.py`:

```python
from iac_code.agent.message import Message


def test_save_does_not_scan_old_file_unless_preserving_cleanup_prompts(tmp_path, monkeypatch):
    storage = SessionStorage(projects_dir=tmp_path)
    storage.append("/tmp/project", "sid", Message(role="user", content="old"))

    def fail_load(cwd, session_id):
        raise AssertionError("save should not load existing messages")

    monkeypatch.setattr(storage, "load", fail_load)

    storage.save("/tmp/project", "sid", [Message(role="user", content="new")])

    assert [message.content for message in SessionStorage(projects_dir=tmp_path).load("/tmp/project", "sid")] == ["new"]


def test_save_can_preserve_cleanup_prompts_when_requested(tmp_path):
    storage = SessionStorage(projects_dir=tmp_path)
    cleanup = create_cleanup_prompt_message("cleanup stack-123", cleanup_ledger_path=tmp_path / "cleanup.yaml")
    storage.append("/tmp/project", "sid", cleanup)

    storage.save(
        "/tmp/project",
        "sid",
        [Message(role="user", content="new")],
        preserve_cleanup_prompts=True,
    )

    loaded = SessionStorage(projects_dir=tmp_path).load("/tmp/project", "sid")
    assert [message.content for message in loaded] == ["new", "cleanup stack-123"]


def test_append_uses_locked_jsonl_helper(tmp_path, monkeypatch):
    storage = SessionStorage(projects_dir=tmp_path)
    calls = []

    def fake_append(path, records, *, durable=False):
        calls.append((path.name, list(records), durable))

    monkeypatch.setattr("iac_code.services.session_storage.append_jsonl_locked", fake_append)

    storage.append("/tmp/project", "sid", Message(role="user", content="hello"), git_branch="main")

    assert calls[0][0] == "session.jsonl"
    assert calls[0][1][0]["content"] == "hello"
    assert calls[0][1][0]["git_branch"] == "main"


def test_legacy_migration_keeps_directory_session_when_present(tmp_path):
    storage = SessionStorage(projects_dir=tmp_path)
    directory = storage.session_dir("/tmp/project", "sid")
    directory.mkdir(parents=True)
    directory_path = directory / "session.jsonl"
    directory_path.write_text('{"role":"user","content":"directory"}\n', encoding="utf-8")
    legacy_path = storage.legacy_session_path("/tmp/project", "sid")
    legacy_path.parent.mkdir(parents=True, exist_ok=True)
    legacy_path.write_text('{"role":"user","content":"legacy"}\n', encoding="utf-8")

    assert storage._ensure_directory_format("/tmp/project", "sid") == directory

    assert directory_path.read_text(encoding="utf-8") == '{"role":"user","content":"directory"}\n'
```

- [ ] **Step 2: Run SessionStorage tests and verify failure**

Run:

```bash
uv run pytest tests/services/test_session_storage.py::test_save_does_not_scan_old_file_unless_preserving_cleanup_prompts tests/services/test_session_storage.py::test_save_can_preserve_cleanup_prompts_when_requested tests/services/test_session_storage.py::test_append_uses_locked_jsonl_helper tests/services/test_session_storage.py::test_legacy_migration_keeps_directory_session_when_present -q
```

Expected: FAIL because `preserve_cleanup_prompts` and locked append are not implemented.

- [ ] **Step 3: Add shared cleanup constants**

Create `/Users/ehzyo/open_repo/iac-code3/src/iac_code/pipeline/engine/constants.py`:

```python
"""Low-dependency pipeline engine constants."""

CLEANUP_PROMPT_METADATA_TYPE = "pipeline_cleanup_prompt"

PIPELINE_EVENT_CLEANUP_STARTED = "cleanup_started"
PIPELINE_EVENT_CLEANUP_PROGRESS = "cleanup_progress"
PIPELINE_EVENT_CLEANUP_COMPLETED = "cleanup_completed"
PIPELINE_EVENT_CLEANUP_FAILED = "cleanup_failed"
```

Update cleanup and session index imports:

```python
from iac_code.pipeline.engine.constants import CLEANUP_PROMPT_METADATA_TYPE
```

- [ ] **Step 4: Update SessionStorage write paths**

Modify `/Users/ehzyo/open_repo/iac-code3/src/iac_code/services/session_storage.py`:

```python
from iac_code.utils.state_io import append_jsonl_locked, atomic_write_text, safe_replace
```

Change `append()` and `append_meta()` to call `append_jsonl_locked(path, [data])` and `append_jsonl_locked(path, [entry])`.

Change `save()` signature and body:

```python
def save(
    self,
    cwd: str,
    session_id: str,
    messages: list[Message],
    *,
    git_branch: str | None = None,
    preserve_cleanup_prompts: bool = False,
) -> None:
    """Overwrite the session file with the given messages."""
    if preserve_cleanup_prompts:
        messages = self._merge_preserved_cleanup_prompts(cwd, session_id, messages)
    path = self._session_path(cwd, session_id)
    ensure_private_dir(path.parent)
    lines = []
    for msg in messages:
        data = self._stamp(msg.to_dict(), cwd, session_id, git_branch)
        lines.append(json.dumps(data, ensure_ascii=False) + "\n")
    atomic_write_text(path, "".join(lines), durable=True)
    ensure_private_file(path)
```

Change `_ensure_directory_format()` legacy migration:

```python
if directory_path.exists():
    return session_dir
if not legacy_path.exists():
    ensure_private_dir(session_dir)
    directory_path.touch()
    ensure_private_file(directory_path)
    return session_dir
ensure_private_dir(session_dir)
safe_replace(str(legacy_path), str(directory_path))
ensure_private_file(directory_path)
return session_dir
```

- [ ] **Step 5: Update call sites that intentionally preserve cleanup prompts**

Search:

```bash
rg -n "save\\(.*messages|\\.save\\(" src/iac_code tests | rg "session_storage|SessionStorage"
```

For flows that rewrite/compact context while retaining hidden cleanup prompts, pass `preserve_cleanup_prompts=True`. Leave normal turn saves at the default `False`.

- [ ] **Step 6: Broaden legacy cleanup prompt hiding**

In `/Users/ehzyo/open_repo/iac-code3/src/iac_code/services/session_index.py`, replace Chinese-only detection with metadata-first plus conservative legacy substrings:

```python
_LEGACY_CLEANUP_PROMPT_MARKERS = (
    "pipeline rollback",
    "rollback cleanup",
    "cleanup required",
    "待清理资源",
    "回滚残留资源",
    "严格白名单",
)


def _is_cleanup_prompt_message(message: Message) -> bool:
    metadata = message.metadata
    if isinstance(metadata, dict) and metadata.get("type") == CLEANUP_PROMPT_METADATA_TYPE:
        return True
    content = message.content
    if not isinstance(content, str):
        return False
    lowered = content.lower()
    return any(marker.lower() in lowered for marker in _LEGACY_CLEANUP_PROMPT_MARKERS)
```

- [ ] **Step 7: Run SessionStorage and session index tests**

Run:

```bash
uv run pytest tests/services/test_session_storage.py tests/services/test_session_index.py tests/agent/test_agent_loop_continue.py -q
```

Expected: PASS.

- [ ] **Step 8: Commit Task 2**

Run:

```bash
git add src/iac_code/services/session_storage.py src/iac_code/services/session_index.py src/iac_code/pipeline/engine/constants.py src/iac_code/pipeline/engine/cleanup.py tests/services/test_session_storage.py tests/services/test_session_index.py
git commit -m "fix: harden session storage writes"
```

## Task 3: A2A Journal And Publisher Durability

**Files:**
- Modify: `/Users/ehzyo/open_repo/iac-code3/src/iac_code/a2a/pipeline_journal.py`
- Modify: `/Users/ehzyo/open_repo/iac-code3/src/iac_code/a2a/pipeline_snapshot.py`
- Modify: `/Users/ehzyo/open_repo/iac-code3/src/iac_code/a2a/pipeline_stream.py`
- Test: `/Users/ehzyo/open_repo/iac-code3/tests/a2a/test_pipeline_journal.py`
- Test: `/Users/ehzyo/open_repo/iac-code3/tests/a2a/test_pipeline_stream.py`

- [ ] **Step 1: Write failing journal group tests**

Append to `/Users/ehzyo/open_repo/iac-code3/tests/a2a/test_pipeline_journal.py`:

```python
def test_append_many_replays_group_as_events(tmp_path) -> None:
    journal = A2APipelineJournal(tmp_path / "pipeline")

    journal.append_many([_event(1, "evt-cancel"), _event(2, "evt-handoff")], durable=True)

    assert [event["eventId"] for event in journal.read_all_strict()] == ["evt-cancel", "evt-handoff"]


def test_append_many_sorts_group_events_with_regular_events(tmp_path) -> None:
    journal = A2APipelineJournal(tmp_path / "pipeline")

    journal.append(_event(3, "evt-after"))
    journal.append_many([_event(1, "evt-cancel"), _event(2, "evt-handoff")], durable=True)

    assert [event["eventId"] for event in journal.read_all()] == ["evt-cancel", "evt-handoff", "evt-after"]
```

- [ ] **Step 2: Write failing publisher durability tests**

Append to `/Users/ehzyo/open_repo/iac-code3/tests/a2a/test_pipeline_stream.py`:

```python
@pytest.mark.asyncio
async def test_recovery_semantic_event_is_not_enqueued_when_metadata_persistence_fails(tmp_path: Path, monkeypatch):
    publisher, queue = _publisher(tmp_path)

    def fail_append(event, durable=False):
        raise OSError("journal locked")

    monkeypatch.setattr(publisher.journal, "append", fail_append)
    monkeypatch.setattr(publisher.snapshot_store, "save", lambda snapshot: False)

    result = await publisher.publish_manual("pipeline_started", "pipeline")

    assert result is None
    assert queue.events == []


@pytest.mark.asyncio
async def test_text_delta_can_be_enqueued_when_only_durable_metadata_fails(tmp_path: Path, monkeypatch):
    publisher, queue = _publisher(tmp_path)

    def fail_append(event, durable=False):
        if durable:
            raise OSError("journal locked")
        publisher.journal.__class__.append(publisher.journal, event, durable=durable)

    monkeypatch.setattr(publisher.journal, "append", fail_append)
    monkeypatch.setattr(publisher.snapshot_store, "save", lambda snapshot: False)

    returned = await publisher.publish(TextDeltaEvent(text="hello"))

    assert returned == "hello"
    assert len(queue.events) == 1
```

- [ ] **Step 3: Run failing A2A durability tests**

Run:

```bash
uv run pytest tests/a2a/test_pipeline_journal.py::test_append_many_replays_group_as_events tests/a2a/test_pipeline_journal.py::test_append_many_sorts_group_events_with_regular_events tests/a2a/test_pipeline_stream.py::test_recovery_semantic_event_is_not_enqueued_when_metadata_persistence_fails tests/a2a/test_pipeline_stream.py::test_text_delta_can_be_enqueued_when_only_durable_metadata_fails -q
```

Expected: FAIL because `append_many()` and durable classification are not implemented.

- [ ] **Step 4: Implement durable journal append and group replay**

In `/Users/ehzyo/open_repo/iac-code3/src/iac_code/a2a/pipeline_journal.py`, change `append()` to:

```python
def append(self, event: dict[str, Any], durable: bool = False) -> None:
    self.pipeline_dir.mkdir(parents=True, exist_ok=True)
    safe_event = to_json_safe(event)
    try:
        line = json.dumps(safe_event, ensure_ascii=False, separators=(",", ":"), allow_nan=False)
    except (TypeError, ValueError):
        logger.warning("Skipping non-JSON-safe A2A pipeline journal event in %s", self.path, exc_info=True)
        return
    with self.path.open("a", encoding="utf-8") as handle:
        handle.write(line + "\n")
        handle.flush()
        if durable:
            os.fsync(handle.fileno())
```

Add `append_many()`:

```python
def append_many(self, events: list[dict[str, Any]], durable: bool = False) -> None:
    self.pipeline_dir.mkdir(parents=True, exist_ok=True)
    safe_events = []
    for event in events:
        safe_event = to_json_safe(event)
        if not isinstance(safe_event, dict):
            raise TypeError("A2A journal group events must be JSON objects")
        safe_events.append(safe_event)
    record = {
        "__iac_code_record_type": "event_group",
        "schemaVersion": "1.0",
        "groupId": uuid.uuid4().hex,
        "events": safe_events,
    }
    line = json.dumps(record, ensure_ascii=False, separators=(",", ":"), allow_nan=False)
    with self.path.open("a", encoding="utf-8") as handle:
        handle.write(line + "\n")
        handle.flush()
        if durable:
            os.fsync(handle.fileno())
```

In `_read_all()`, when a parsed object has `__iac_code_record_type == "event_group"` and `events` is a list of dicts, extend `events` with those child events instead of appending the group record.

- [ ] **Step 5: Use durable state I/O for snapshots**

In `/Users/ehzyo/open_repo/iac-code3/src/iac_code/a2a/pipeline_snapshot.py`, replace temp write plus `tmp_path.replace(self.path)` with:

```python
from iac_code.utils.state_io import atomic_write_json
```

and:

```python
atomic_write_json(self.path, next_snapshot, durable=True)
return True
```

- [ ] **Step 6: Add A2A durable-event classifier**

In `/Users/ehzyo/open_repo/iac-code3/src/iac_code/a2a/pipeline_stream.py`, add:

```python
_RECOVERY_SEMANTIC_EVENT_TYPES = {
    "pipeline_started",
    "step_started",
    "step_completed",
    "step_failed",
    "candidate_selected",
    "candidate_completed",
    "candidate_failed",
    "input_required",
    "pipeline_completed",
    "pipeline_failed",
    "pipeline_canceled",
    "pipeline_handoff_ready",
    "cleanup_started",
    "cleanup_progress",
    "cleanup_completed",
    "cleanup_failed",
    "artifact_created",
    "rollback_completed",
    "candidate_restart_requested",
}


def _is_recovery_semantic_event(envelope: dict[str, Any]) -> bool:
    event_type = envelope.get("eventType")
    if event_type in _RECOVERY_SEMANTIC_EVENT_TYPES:
        return True
    if envelope.get("scope") in {"step", "candidate", "candidateStep"} and envelope.get("status") in {
        "working",
        "waiting_input",
        "completed",
        "failed",
        "canceled",
    }:
        return True
    return False
```

Then in `_persist_and_enqueue()` set:

```python
durable_required = require_durable_metadata or _is_recovery_semantic_event(safe_envelope)
```

Call:

```python
self.journal.append(safe_envelope, durable=durable_required)
```

and gate queue delivery with `durable_required` instead of only `require_durable_metadata`.

- [ ] **Step 7: Run A2A durability tests**

Run:

```bash
uv run pytest tests/a2a/test_pipeline_journal.py tests/a2a/test_pipeline_stream.py -q
```

Expected: PASS.

- [ ] **Step 8: Commit Task 3**

Run:

```bash
git add src/iac_code/a2a/pipeline_journal.py src/iac_code/a2a/pipeline_snapshot.py src/iac_code/a2a/pipeline_stream.py tests/a2a/test_pipeline_journal.py tests/a2a/test_pipeline_stream.py
git commit -m "fix: make A2A recovery events durable"
```

## Task 4: A2A Recovery, Active Mismatch, And Handoff Cleanup

**Files:**
- Modify: `/Users/ehzyo/open_repo/iac-code3/src/iac_code/a2a/pipeline_executor.py`
- Modify: `/Users/ehzyo/open_repo/iac-code3/src/iac_code/a2a/executor.py`
- Modify: `/Users/ehzyo/open_repo/iac-code3/scripts/a2a/debugger.py`
- Modify: `/Users/ehzyo/open_repo/iac-code3/scripts/a2a/selling_console.py`
- Test: `/Users/ehzyo/open_repo/iac-code3/tests/a2a/test_pipeline_executor.py`
- Test: `/Users/ehzyo/open_repo/iac-code3/tests/a2a/test_executor_cleanup.py`
- Test: `/Users/ehzyo/open_repo/iac-code3/tests/a2a/test_pipeline_debugger_script.py`
- Test: `/Users/ehzyo/open_repo/iac-code3/tests/a2a/test_selling_console_script.py`

- [ ] **Step 1: Write active mismatch test**

Add to `/Users/ehzyo/open_repo/iac-code3/tests/a2a/test_pipeline_executor.py`:

```python
def test_active_sidecar_mismatch_returns_recoverable_error_without_clearing(tmp_path):
    from iac_code.a2a.pipeline_executor import _active_sidecar_mismatch_error

    error = _active_sidecar_mismatch_error(
        recoverable_task_id="task-owner",
        context_id="ctx-1",
        sidecar_status="running",
    )

    assert error.code == -32602
    assert error.data == {
        "recoverableTaskId": "task-owner",
        "contextId": "ctx-1",
        "sidecarStatus": "running",
    }
```

- [ ] **Step 2: Write cancel handoff atomicity test**

Modify the existing `/Users/ehzyo/open_repo/iac-code3/tests/a2a/test_pipeline_executor.py::test_canceled_pipeline_run_closes_blocked_stream_without_child_task_leak` so it records `append_many()` calls while preserving the existing assertions:

```python
append_many_calls = []
original_append_many = A2APipelineJournal.append_many


def recording_append_many(self, events, durable=False):
    append_many_calls.append(([event["eventType"] for event in events], durable))
    return original_append_many(self, events, durable=durable)

monkeypatch.setattr(A2APipelineJournal, "append_many", recording_append_many)
```

At the end of that test, after the existing journal and snapshot assertions, add:

```python
assert append_many_calls[-1] == (["pipeline_canceled", "pipeline_handoff_ready"], True)
```

- [ ] **Step 3: Write cleanup source-of-truth tests**

Extend `/Users/ehzyo/open_repo/iac-code3/tests/a2a/test_executor_cleanup.py`:

```python
def test_a2a_handoff_does_not_reconstruct_cleanup_prompt_from_public_snapshot(tmp_path):
    snapshot = {
        "cleanup": {
            "resources": [{"provider": "ros", "resourceId": "stack-123", "resourceType": "stack"}],
            "status": "pending",
        }
    }

    cleanup = _cleanup_payload_from_private_ledger_or_unavailable(
        ledger_path=tmp_path / "missing-cleanup.yaml",
        public_snapshot=snapshot,
    )

    assert cleanup["status"] == "unavailable"
    assert "prompt" not in cleanup
    assert "resources" not in cleanup
```

- [ ] **Step 4: Run targeted A2A recovery tests and verify failure**

Run:

```bash
uv run pytest tests/a2a/test_pipeline_executor.py tests/a2a/test_executor_cleanup.py -q
```

Expected: FAIL on the new tests.

- [ ] **Step 5: Implement active mismatch error**

In `/Users/ehzyo/open_repo/iac-code3/src/iac_code/a2a/pipeline_executor.py`, add an invalid-params helper using the repository's existing JSON-RPC error type. Use the same class/import already used by the executor for invalid params. The helper must produce:

```python
{
    "recoverableTaskId": recoverable_task_id,
    "contextId": context_id,
    "sidecarStatus": sidecar_status,
}
```

Replace `_fresh_pipeline_after_sidecar_mismatch()` calls for `running` and `waiting_input` sidecars with returning this error to the A2A request path. Keep terminal/non-resumable cleanup behavior unchanged.

- [ ] **Step 6: Implement cancel handoff durable group**

Replace:

```python
journal.append(envelope)
if handoff_envelope is not None:
    journal.append(handoff_envelope)
snapshot_store.save(reduce_pipeline_events(journal.read_all_repairing_tail()))
```

with:

```python
events_to_append = [envelope]
if handoff_envelope is not None:
    events_to_append.append(handoff_envelope)
journal.append_many(events_to_append, durable=True)
snapshot_store.save(reduce_pipeline_events(journal.read_all_repairing_tail()))
```

- [ ] **Step 7: Implement private-ledger cleanup handoff source of truth**

Add a helper in `/Users/ehzyo/open_repo/iac-code3/src/iac_code/a2a/executor.py`:

```python
def _cleanup_payload_from_private_ledger_or_unavailable(
    *,
    ledger_path: Path,
    public_snapshot: dict[str, Any] | None = None,
) -> dict[str, Any]:
    ledger = CleanupLedger(ledger_path)
    if ledger.load_failed() or not ledger_path.exists():
        return {"status": "unavailable", "statusMessage": _("Cleanup state unavailable. Inspect the session file and cloud resources manually.")}
    prompt = ledger.build_pending_prompt()
    if prompt is None:
        return {"status": "completed", "resourceCount": 0}
    return {
        "status": "pending",
        "resourceCount": len(prompt.resources),
        "statusMessage": prompt.status_message,
        "prompt": prompt.prompt,
        "ledgerPath": str(ledger_path),
    }
```

Use this helper in normal and cancel handoff paths. Keep public snapshot resource summaries for display only.

- [ ] **Step 8: Surface recoverable task ids in scripts**

In `/Users/ehzyo/open_repo/iac-code3/scripts/a2a/debugger.py` and `/Users/ehzyo/open_repo/iac-code3/scripts/a2a/selling_console.py`, when JSON-RPC error data contains `recoverableTaskId`, print or render it next to the error message. Add script tests that parse a response shaped like:

```python
{"error": {"code": -32602, "message": "Pipeline already running.", "data": {"recoverableTaskId": "task-owner", "contextId": "ctx-1", "sidecarStatus": "running"}}}
```

and assert `"task-owner"` is shown.

- [ ] **Step 9: Run A2A recovery tests**

Run:

```bash
uv run pytest tests/a2a/test_pipeline_executor.py tests/a2a/test_executor_cleanup.py tests/a2a/test_pipeline_debugger_script.py tests/a2a/test_selling_console_script.py -q
```

Expected: PASS.

- [ ] **Step 10: Commit Task 4**

Run:

```bash
git add src/iac_code/a2a/pipeline_executor.py src/iac_code/a2a/executor.py scripts/a2a/debugger.py scripts/a2a/selling_console.py tests/a2a/test_pipeline_executor.py tests/a2a/test_executor_cleanup.py tests/a2a/test_pipeline_debugger_script.py tests/a2a/test_selling_console_script.py
git commit -m "fix: preserve A2A recoverable pipeline state"
```

## Task 5: Cleanup Ledger Correctness

**Files:**
- Modify: `/Users/ehzyo/open_repo/iac-code3/src/iac_code/pipeline/engine/cleanup.py`
- Modify: `/Users/ehzyo/open_repo/iac-code3/src/iac_code/pipeline/engine/pipeline_runner.py`
- Modify: `/Users/ehzyo/open_repo/iac-code3/src/iac_code/pipeline/selling/hooks/deploying.py`
- Test: `/Users/ehzyo/open_repo/iac-code3/tests/pipeline/engine/test_cleanup.py`
- Test: `/Users/ehzyo/open_repo/iac-code3/tests/pipeline/engine/test_pipeline_runner_cleanup.py`
- Test: `/Users/ehzyo/open_repo/iac-code3/tests/pipeline/selling/test_deploying_cleanup_hook.py`

- [ ] **Step 1: Write failing cleanup merge tests**

Append to `/Users/ehzyo/open_repo/iac-code3/tests/pipeline/engine/test_cleanup.py`:

```python
def test_mark_cleanup_required_preserves_active_execution_fields(tmp_path) -> None:
    ledger = CleanupLedger(tmp_path / "cleanup.yaml")
    resource = CleanupResource.from_observed(_observed_stack(), reason="rollback requested")
    ledger.mark_cleanup_required([resource], source_step_id="deploying", reason="rollback requested")
    ledger.update_resource(
        provider="ros",
        resource_type="stack",
        resource_id="stack-123",
        region_id="cn-hangzhou",
        cleanup_status="in_progress",
        cleanup_tool_use_id="toolu-delete",
        cleanup_action="DeleteStack",
        progress_status="DELETE_IN_PROGRESS",
        progress_percentage=30,
        last_error="slow",
    )

    ledger.mark_cleanup_required([resource], source_step_id="deploying", reason="rollback requested again")

    [updated] = ledger.cleanup_resources()
    assert updated.cleanup_status == "in_progress"
    assert updated.cleanup_tool_use_id == "toolu-delete"
    assert updated.cleanup_action == "DeleteStack"
    assert updated.progress_status == "DELETE_IN_PROGRESS"
    assert updated.progress_percentage == 30
    assert updated.last_error == "slow"


def test_observer_uses_persisted_tool_mapping_after_restart(tmp_path) -> None:
    ledger = CleanupLedger(tmp_path / "cleanup.yaml")
    resource = CleanupResource.from_observed(_observed_stack(), reason="rollback requested")
    ledger.mark_cleanup_required([resource], source_step_id="deploying", reason="rollback requested")
    CleanupObserver(ledger).observe(
        ToolUseEndEvent(
            tool_use_id="toolu-delete",
            name="ros_stack",
            input={"action": "DeleteStack", "region_id": "cn-hangzhou", "params": {"StackId": "stack-123"}},
        )
    )

    restarted = CleanupObserver(CleanupLedger(tmp_path / "cleanup.yaml"))
    restarted.observe(
        ToolResultEvent(
            tool_use_id="toolu-delete",
            tool_name="ros_stack",
            result=json.dumps({"stack_id": "stack-123", "status": "DELETE_COMPLETE"}),
            is_error=False,
        )
    )

    [updated] = CleanupLedger(tmp_path / "cleanup.yaml").cleanup_resources()
    assert updated.cleanup_status == "completed"
```

- [ ] **Step 2: Write corrupt ledger fail-closed tests**

Append:

```python
def test_corrupt_ledger_records_unavailable_without_overwrite(tmp_path) -> None:
    path = tmp_path / "cleanup.yaml"
    path.write_text("[broken", encoding="utf-8")
    ledger = CleanupLedger(path)

    ledger.mark_cleanup_required([CleanupResource.from_observed(_observed_stack(), reason="rollback")], source_step_id="deploying", reason="rollback")

    assert path.read_text(encoding="utf-8") == "[broken"
    assert ledger.load_failed()
    assert ledger.load_error()
```

- [ ] **Step 3: Run cleanup tests and verify failure**

Run:

```bash
uv run pytest tests/pipeline/engine/test_cleanup.py::test_mark_cleanup_required_preserves_active_execution_fields tests/pipeline/engine/test_cleanup.py::test_observer_uses_persisted_tool_mapping_after_restart tests/pipeline/engine/test_cleanup.py::test_corrupt_ledger_records_unavailable_without_overwrite -q
```

Expected: FAIL on active-field preservation and persisted mapping.

- [ ] **Step 4: Add in-process ledger serialization and state I/O save**

In `CleanupLedger`, use a per-path `threading.RLock` before every load-modify-save path. Add a `_with_write_lock()` helper or wrap `record_observed()`, `mark_cleanup_required()`, `update_resource()`, and `record_prompt_queued()` bodies. Replace `_save()` with:

```python
from iac_code.utils.state_io import atomic_write_text


def _save(self, data: dict[str, Any]) -> None:
    self.path.parent.mkdir(parents=True, exist_ok=True)
    content = yaml.safe_dump(data, allow_unicode=True, sort_keys=False)
    atomic_write_text(self.path, content, durable=True)
```

- [ ] **Step 5: Implement monotonic merge rules**

In `mark_cleanup_required()`, replace the direct `replace(resource, cleanup_required=True, cleanup_reason=resource.cleanup_reason or reason, source_step_id=resource.source_step_id or source_step_id, updated_at=now)` assignment with a merge helper:

```python
def _merge_cleanup_required(existing: CleanupResource | None, incoming: CleanupResource, *, reason: str, source_step_id: str, now: float) -> CleanupResource:
    if existing is None:
        return replace(
            incoming,
            cleanup_required=True,
            cleanup_reason=incoming.cleanup_reason or reason,
            source_step_id=incoming.source_step_id or source_step_id,
            updated_at=now,
        )
    if existing.cleanup_status in _TERMINAL_CLEANUP_STATUSES:
        return existing
    active_status = existing.cleanup_status if existing.cleanup_status in _ACTIVE_CLEANUP_STATUSES or existing.cleanup_status == "failed" else incoming.cleanup_status
    return replace(
        incoming,
        cleanup_required=True,
        cleanup_reason=incoming.cleanup_reason or existing.cleanup_reason or reason,
        source_step_id=incoming.source_step_id or existing.source_step_id or source_step_id,
        cleanup_status=active_status,
        cleanup_tool_use_id=existing.cleanup_tool_use_id,
        cleanup_action=existing.cleanup_action,
        progress_status=existing.progress_status,
        progress_percentage=existing.progress_percentage,
        last_error=existing.last_error,
        observed_at=existing.observed_at or incoming.observed_at,
        updated_at=now,
    )
```

- [ ] **Step 6: Persist tool-use mappings**

Add ledger `tool_uses` data with sanitized input summaries:

```python
def record_tool_use_mapping(self, *, tool_use_id: str, provider: str, resource_type: str, resource_id: str, region_id: str, action: str, tool_name: str, tool_input: dict[str, Any]) -> None:
    data = self._load_for_write()
    if data is None:
        return
    mappings = {str(item.get("tool_use_id")): dict(item) for item in _dict_list(data.get("tool_uses"))}
    mappings[tool_use_id] = {
        "tool_use_id": tool_use_id,
        "provider": provider,
        "resource_type": resource_type,
        "resource_id": resource_id,
        "region_id": region_id,
        "action": action,
        "tool_name": tool_name,
        "input_summary": _safe_history_error(json.dumps(tool_input, ensure_ascii=False, sort_keys=True)),
    }
    data["tool_uses"] = list(mappings.values())
    self._save(data)
```

In `CleanupObserver._observe_tool_use()`, call `record_tool_use_mapping()` for `DeleteStack` and `GetStack`. In `_observe_tool_result()`, if `_tool_inputs` misses the id, load `ledger.tool_use_mapping(tool_use_id)` and use that for matching.

- [ ] **Step 7: Add cleanup unavailable history warning**

When no mapping exists for a cleanup tool result, append a history entry:

```python
{
    "type": "cleanup_tool_result_unmatched",
    "tool_use_id": event.tool_use_id,
    "tool_name": event.tool_name,
    "timestamp": time.time(),
}
```

- [ ] **Step 8: Keep cloud observation window residual but surface write failures**

In `/Users/ehzyo/open_repo/iac-code3/src/iac_code/pipeline/engine/pipeline_runner.py`, when `ledger.record_observed()` raises after Task 1 state I/O changes, log warning and yield or record a recoverable pipeline error. The design accepts the API-success-to-event gap, but not silent ledger write failure.

- [ ] **Step 9: Add deploying hook warning**

In `/Users/ehzyo/open_repo/iac-code3/src/iac_code/pipeline/selling/hooks/deploying.py`, when `from_attempt_id` is falsy in cleanup-required hook code, log:

```python
logger.warning("Skipping deploying cleanup hook because from_attempt_id is missing")
```

- [ ] **Step 10: Run cleanup tests**

Run:

```bash
uv run pytest tests/pipeline/engine/test_cleanup.py tests/pipeline/engine/test_pipeline_runner_cleanup.py tests/pipeline/selling/test_deploying_cleanup_hook.py -q
```

Expected: PASS.

- [ ] **Step 11: Commit Task 5**

Run:

```bash
git add src/iac_code/pipeline/engine/cleanup.py src/iac_code/pipeline/engine/pipeline_runner.py src/iac_code/pipeline/selling/hooks/deploying.py tests/pipeline/engine/test_cleanup.py tests/pipeline/engine/test_pipeline_runner_cleanup.py tests/pipeline/selling/test_deploying_cleanup_hook.py
git commit -m "fix: preserve cleanup ledger state"
```

## Task 6: Pipeline Runner Persistence And REPL Resume Fallback

**Files:**
- Modify: `/Users/ehzyo/open_repo/iac-code3/src/iac_code/pipeline/engine/session.py`
- Modify: `/Users/ehzyo/open_repo/iac-code3/src/iac_code/pipeline/engine/pipeline_runner.py`
- Modify: `/Users/ehzyo/open_repo/iac-code3/src/iac_code/ui/repl.py`
- Test: `/Users/ehzyo/open_repo/iac-code3/tests/pipeline/engine/test_session.py`
- Test: `/Users/ehzyo/open_repo/iac-code3/tests/pipeline/engine/test_pipeline_runner.py`
- Test: `/Users/ehzyo/open_repo/iac-code3/tests/ui/test_repl_pipeline_sidecar_restore.py`

- [ ] **Step 1: Write sidecar YAML durability test**

Add to `/Users/ehzyo/open_repo/iac-code3/tests/pipeline/engine/test_session.py`:

```python
def test_sidecar_yaml_uses_atomic_state_write(monkeypatch, tmp_path) -> None:
    calls = []

    def fake_atomic_write_text(path, content, *, durable=True, replace_attempts=3, encoding="utf-8"):
        calls.append((Path(path).name, durable))
        Path(path).write_text(content, encoding=encoding)

    monkeypatch.setattr("iac_code.pipeline.engine.session.atomic_write_text", fake_atomic_write_text)

    session = PipelineSession(tmp_path / "pipeline")
    session.save_running_sync(
        "step",
        {"current_index": 0, "rollback_count": 0, "step_statuses": {"step": "running"}},
        {},
        {"pipeline_name": "test", "step_ids": ["step"], "sub_pipeline_step_ids": {}, "pipeline_fingerprint": "fp"},
    )

    assert ("context.yaml", True) in calls
    assert ("meta.yaml", True) in calls
```

- [ ] **Step 2: Write runner persistence failure tests**

Add to `/Users/ehzyo/open_repo/iac-code3/tests/pipeline/engine/test_pipeline_runner.py`:

```python
@pytest.mark.asyncio
async def test_sidecar_save_failure_stops_before_next_step(tmp_path):
    runner = _build_two_step_runner(tmp_path)
    runner.session = FailingSavePipelineSession()

    events = []
    async for event in runner.run("start"):
        events.append(event)

    assert any("pipeline state persistence failed" in str(getattr(event, "data", {})).lower() for event in events)
    assert runner.state_machine.current_step.step_id == "a"
    assert runner.session.calls[0][0] == "running_attempted"
```

- [ ] **Step 3: Write damaged `/resume` metadata test**

Add to `/Users/ehzyo/open_repo/iac-code3/tests/ui/test_repl_pipeline_sidecar_restore.py`:

```python
@pytest.mark.asyncio
async def test_confirm_pipeline_resume_handles_corrupt_meta(tmp_path, repl_for_sidecar_restore):
    meta_path = tmp_path / "meta.yaml"
    meta_path.write_text("[broken", encoding="utf-8")

    choice = await repl_for_sidecar_restore._confirm_pipeline_resume(meta_path)

    assert choice == "discard"
    repl_for_sidecar_restore.renderer.print_system_message.assert_called()
```

- [ ] **Step 4: Run targeted tests and verify failure**

Run:

```bash
uv run pytest tests/pipeline/engine/test_session.py::test_sidecar_yaml_uses_atomic_state_write tests/pipeline/engine/test_pipeline_runner.py::test_sidecar_save_failure_stops_before_next_step tests/ui/test_repl_pipeline_sidecar_restore.py::test_confirm_pipeline_resume_handles_corrupt_meta -q
```

Expected: FAIL until implementation lands.

- [ ] **Step 5: Use state I/O helper for sidecar YAML**

In `/Users/ehzyo/open_repo/iac-code3/src/iac_code/pipeline/engine/session.py`, import `atomic_write_text` and change `_atomic_write_yaml()`:

```python
def _atomic_write_yaml(self, path: Path, data: dict) -> None:
    self.session_dir.mkdir(parents=True, exist_ok=True)
    content = yaml.safe_dump(data, allow_unicode=True, sort_keys=False)
    atomic_write_text(path, content, durable=True)
```

Keep the accepted residual risk: `context.yaml` and `meta.yaml` are still separate files.

- [ ] **Step 6: Make sidecar save failures hard errors**

In `/Users/ehzyo/open_repo/iac-code3/src/iac_code/pipeline/engine/pipeline_runner.py`, add:

```python
class PipelineStatePersistenceError(RuntimeError):
    """Raised when recovery-critical pipeline state cannot be persisted."""
```

Change `_try_save_sidecar()` and `_try_save_sidecar_sync()` to raise `PipelineStatePersistenceError("pipeline state persistence failed during {operation}")` after recording observability. Update callers so the async run loop catches this error, yields a failure event shaped like this, and breaks before advancing, handoff, or downstream tools:

```python
PipelineEvent(
    type=PipelineEventType.STEP_FAILED,
    step_id=self.state_machine.current_step.step_id,
    timestamp=time.time(),
    data={
        "error": "Pipeline state persistence failed.",
        "error_summary": "Pipeline state persistence failed.",
        "error_details": {"type": "PipelineStatePersistenceError"},
    },
)
```

- [ ] **Step 7: Add persistence boundary around advance/rollback**

For code paths around `state_machine.advance()`, `state_machine.rollback()`, interrupt rollback, and normal handoff, ensure either:

```python
await self._save_after_advance(step.step_id)
```

or the corresponding rollback save is awaited immediately after the in-memory mutation, and failure stops event emission. Do not emit `STEP_COMPLETED`, `PIPELINE_COMPLETED`, `pipeline_handoff_ready`, or execute the next step when the save raises.

- [ ] **Step 8: Handle damaged `/resume` metadata**

In `/Users/ehzyo/open_repo/iac-code3/src/iac_code/ui/repl.py`, replace raw `_yaml.safe_load(meta_path.read_text(encoding="utf-8"))` with:

```python
try:
    loaded = _yaml.safe_load(meta_path.read_text(encoding="utf-8"))
except (FileNotFoundError, OSError, UnicodeDecodeError, _yaml.YAMLError) as exc:
    self.renderer.print_system_message(
        _("Could not read pipeline state metadata: {reason}").format(reason=str(exc) or type(exc).__name__),
        style="yellow",
    )
    return "discard"
if loaded is None:
    loaded = {}
if not isinstance(loaded, dict):
    self.renderer.print_system_message(_("Pipeline state metadata is invalid; continuing as normal chat."), style="yellow")
    return "discard"
meta = loaded
```

- [ ] **Step 9: Run runner and REPL tests**

Run:

```bash
uv run pytest tests/pipeline/engine/test_session.py tests/pipeline/engine/test_pipeline_runner.py tests/ui/test_repl_pipeline_sidecar_restore.py -q
```

Expected: PASS.

- [ ] **Step 10: Commit Task 6**

Run:

```bash
git add src/iac_code/pipeline/engine/session.py src/iac_code/pipeline/engine/pipeline_runner.py src/iac_code/ui/repl.py tests/pipeline/engine/test_session.py tests/pipeline/engine/test_pipeline_runner.py tests/ui/test_repl_pipeline_sidecar_restore.py
git commit -m "fix: stop on pipeline state persistence failure"
```

## Task 7: Windows Compatibility, i18n, And Minor Code Closures

**Files:**
- Modify: `/Users/ehzyo/open_repo/iac-code3/src/iac_code/tools/base.py`
- Modify: `/Users/ehzyo/open_repo/iac-code3/src/iac_code/tools/read_file.py`
- Modify: `/Users/ehzyo/open_repo/iac-code3/src/iac_code/tools/cloud/base_stack.py`
- Modify: `/Users/ehzyo/open_repo/iac-code3/src/iac_code/a2a/executor.py`
- Modify: `/Users/ehzyo/open_repo/iac-code3/src/iac_code/pipeline/engine/user_input.py`
- Modify: `/Users/ehzyo/open_repo/iac-code3/src/iac_code/pipeline/engine/completion_guard_state.py`
- Modify: `/Users/ehzyo/open_repo/iac-code3/src/iac_code/pipeline/engine/step_executor.py`
- Modify: `/Users/ehzyo/open_repo/iac-code3/src/iac_code/agent/agent_loop.py`
- Modify: `/Users/ehzyo/open_repo/iac-code3/src/iac_code/ui/repl.py`
- Modify: `/Users/ehzyo/open_repo/iac-code3/scripts/a2a/selling_console.py`
- Modify: `/Users/ehzyo/open_repo/iac-code3/scripts/repl/e2e/run_pipeline_scenarios.py`
- Test: existing focused test files under `/Users/ehzyo/open_repo/iac-code3/tests`

- [ ] **Step 1: Write ToolContext compatibility test**

Append to `/Users/ehzyo/open_repo/iac-code3/tests/tools/test_read_file.py` or create `/Users/ehzyo/open_repo/iac-code3/tests/tools/test_tool_context.py`:

```python
from iac_code.tools.base import ToolContext


def test_tool_context_positional_tool_use_id_compatibility() -> None:
    context = ToolContext("/tmp/project", None, "toolu-1")

    assert context.cwd == "/tmp/project"
    assert context.event_queue is None
    assert context.tool_use_id == "toolu-1"
```

- [ ] **Step 2: Write Windows path normalization test**

Add to `/Users/ehzyo/open_repo/iac-code3/tests/tools/test_read_file.py`:

```python
def test_path_is_under_windows_case_insensitive(monkeypatch):
    monkeypatch.setattr("iac_code.tools.path_safety.sys.platform", "win32")
    from iac_code.tools.read_file import _path_is_under

    assert _path_is_under("C:\\Users\\Alice\\project\\file.txt", "c:/users/alice/project")
```

- [ ] **Step 3: Write empty stack id test**

Add to `/Users/ehzyo/open_repo/iac-code3/tests/tools/cloud/test_base_stack.py`:

```python
@pytest.mark.asyncio
async def test_create_stack_does_not_emit_observed_resource_for_empty_stack_id():
    queue = asyncio.Queue()
    tool = FakeStackTool()
    tool.call_action = AsyncMock(return_value="")

    await tool.execute(tool_input={"action": "CreateStack", "params": {}}, context=ToolContext(cwd="/tmp", event_queue=queue, tool_use_id="toolu-1"))

    assert queue.empty()
```

- [ ] **Step 4: Implement ToolContext positional order**

In `/Users/ehzyo/open_repo/iac-code3/src/iac_code/tools/base.py`, reorder fields:

```python
cwd: str = field(default_factory=os.getcwd)
event_queue: asyncio.Queue | None = None
tool_use_id: str | None = None
additional_directories: list[str] = field(default_factory=list)
trusted_read_directories: list[str] = field(default_factory=list)
relative_read_directories: list[str] = field(default_factory=list)
pipeline_mode: bool = False
```

- [ ] **Step 5: Implement read path normalization**

In `/Users/ehzyo/open_repo/iac-code3/src/iac_code/tools/read_file.py`, replace `_path_is_under()` with:

```python
from iac_code.tools.path_safety import _normalize_for_platform


def _path_is_under(path: str, root: str) -> bool:
    try:
        path_real = os.path.realpath(path)
        root_real = os.path.realpath(root)
        common = os.path.commonpath([path_real, root_real])
    except ValueError:
        return False
    return _normalize_for_platform(common) == _normalize_for_platform(root_real)
```

- [ ] **Step 6: Guard empty stack id**

In `/Users/ehzyo/open_repo/iac-code3/src/iac_code/tools/cloud/base_stack.py`, change:

```python
if context.event_queue is not None and action == "CreateStack":
```

to:

```python
if context.event_queue is not None and action == "CreateStack" and stack_id:
```

- [ ] **Step 7: Fix i18n strings**

Update A2A image errors and pipeline image placeholder:

```python
_("Current model {model} does not support image input.").format(model=self._model)
_("[Image input]")
```

Replace Chinese cleanup msgids in source with English msgids and move Chinese translations into `messages.po` through `make translate`. Use `.format()` for interpolated translated strings.

- [ ] **Step 8: Complete minor code closures**

Make these exact edits:

- In `completion_guard_state.py`, log JSON parse failures with `logger.warning("Failed to parse completion guard state", exc_info=True)` and rebuild failures with `logger.warning("Failed to rebuild completion guard state", exc_info=True)`.
- In `step_executor.py`, replace `precompleted_tools_set.update(precompleted_tools)` with `precompleted_tools_set.update(precompleted_tools.keys())`.
- In `agent_loop.py`, remove the duplicate `_pipeline_mode` assignment.
- In `scripts/a2a/selling_console.py`, set `allow_reuse_address = sys.platform != "win32"` on the HTTP server class or use the platform exclusive option if already available.
- In `scripts/a2a/selling_console.py`, add a module docstring describing text-only input support.
- In `scripts/repl/e2e/run_pipeline_scenarios.py`, guard real PTY execution on Windows with `SystemExit("real PTY REPL E2E is POSIX-only")`.
- In `scripts/repl/e2e/run_pipeline_scenarios.py`, use `shlex.split(value, posix=(os.name != "nt"))`.
- In `ui/repl.py`, wrap `loop.add_signal_handler` with an unsupported-platform fallback that keeps existing KeyboardInterrupt handling.

- [ ] **Step 9: Run focused minor tests**

Run:

```bash
uv run pytest tests/tools/test_read_file.py tests/tools/cloud/test_base_stack.py tests/a2a/test_executor.py tests/pipeline/engine/test_completion_guard_state.py tests/pipeline/engine/test_step_executor.py tests/a2a/test_selling_console_script.py tests/repl_e2e/test_run_pipeline_scenarios.py -q
```

Expected: PASS.

- [ ] **Step 10: Update translations**

Run:

```bash
make translate
```

Expected: command completes. Review `.po` changes and keep generated translation changes only if the project workflow updates them.

- [ ] **Step 11: Commit Task 7**

Run:

```bash
git add src/iac_code/tools/base.py src/iac_code/tools/read_file.py src/iac_code/tools/cloud/base_stack.py src/iac_code/a2a/executor.py src/iac_code/pipeline/engine/user_input.py src/iac_code/pipeline/engine/completion_guard_state.py src/iac_code/pipeline/engine/step_executor.py src/iac_code/agent/agent_loop.py src/iac_code/ui/repl.py scripts/a2a/selling_console.py scripts/repl/e2e/run_pipeline_scenarios.py src/iac_code/i18n/locales tests
git commit -m "fix: close Windows i18n and compatibility gaps"
```

## Task 8: Documentation And Closure Summary

**Files:**
- Modify: `/Users/ehzyo/open_repo/iac-code3/docs/batch/20260616-a2a-pipeline-cancel-handoff.md`
- Modify: `/Users/ehzyo/open_repo/iac-code3/docs/batch/20260622-pipeline-image-cherry-pick.md`
- Modify: `/Users/ehzyo/open_repo/iac-code3/docs/batch/20260623-selling-console-cherry-pick.md`
- Modify: `/Users/ehzyo/open_repo/iac-code3/docs/pipeline-image-manual-test-guide.zh-CN.md`
- Modify: `/Users/ehzyo/open_repo/iac-code3/scripts/README.md`
- Modify: `/Users/ehzyo/open_repo/iac-code3/scripts/repl/e2e/README.zh-CN.md`
- Create: `/Users/ehzyo/open_repo/iac-code3/scripts/repl/e2e/README.md`
- Create: `/Users/ehzyo/open_repo/iac-code3/docs/pipeline-schema-reference.md`
- Create: `/Users/ehzyo/open_repo/iac-code3/docs/review-fix-summary.md`

- [ ] **Step 1: Update default-cwd docs**

In `/Users/ehzyo/open_repo/iac-code3/docs/batch/20260616-a2a-pipeline-cancel-handoff.md`, describe:

```markdown
If `--default-cwd` points inside the configured workspace root and the directory does not exist yet, the A2A executor may create it. Requests are rejected only when the resolved path escapes the allowed root, cannot be created, or cannot be used as a directory.
```

- [ ] **Step 2: Update A2A image docs**

In `/Users/ehzyo/open_repo/iac-code3/docs/batch/20260622-pipeline-image-cherry-pick.md` and debugger docs, add:

```markdown
Image input accepts supported image MIME types only. Inline or local payloads are size-limited by the A2A part parser. `file://` inputs must resolve under the request cwd or another allowed read root; local URLs outside those roots are rejected. The A2A debugger sends image parts. The Selling Console web UI currently sends text input only.
```

- [ ] **Step 3: Fix stale paths and REPL E2E docs**

In `/Users/ehzyo/open_repo/iac-code3/docs/pipeline-image-manual-test-guide.zh-CN.md`, replace `.worktrees/pipeline-image` with repository root wording.

In `/Users/ehzyo/open_repo/iac-code3/scripts/repl/e2e/README.zh-CN.md`, replace `/tmp` with “system temporary directory”.

Create `/Users/ehzyo/open_repo/iac-code3/scripts/repl/e2e/README.md` with:

````markdown
# REPL Pipeline E2E Runner

This runner is POSIX-only because it uses a real PTY through `pexpect`.

Run from the repository root:

```bash
uv run python scripts/repl/e2e/run_pipeline_scenarios.py --help
```

By default, run artifacts are written under the system temporary directory, in `iac-code-repl-e2e-runs/<scenario>/<timestamp>-<pid>-<id>/`.

The runner is for manual or smoke validation. It must not require real LLM or cloud credentials in automated unit tests.
````

- [ ] **Step 4: Add schema reference**

Create `/Users/ehzyo/open_repo/iac-code3/docs/pipeline-schema-reference.md` documenting these fields with examples:

- `completion_guards`
- `surface_overrides`
- `parameter_overrides`
- `a2a_artifacts`
- `exit_condition`
- `inject_tools`
- `ui_mode`
- `conclusion_schema`
- `interrupt_judge_failure`
- `hooks_file`
- `enabled_when`

Use examples copied from `/Users/ehzyo/open_repo/iac-code3/src/iac_code/pipeline/selling/pipeline.yaml` where available.

- [ ] **Step 5: Update script and dependency docs**

In `/Users/ehzyo/open_repo/iac-code3/scripts/README.md`, add entries for:

- `scripts/a2a/selling_console.py`
- `scripts/a2a/selling_console_web/`
- `scripts/repl/e2e/run_pipeline_scenarios.py`

Mention `pexpect` as a POSIX-only dev dependency for the real PTY runner. Document the `conftest.py` tiktoken isolation fixture in the nearest test README or `scripts/README.md`.

- [ ] **Step 6: Create closure summary**

Create `/Users/ehzyo/open_repo/iac-code3/docs/review-fix-summary.md` with a table:

```markdown
# Review Fix Summary

| Review item | Resolution | Tests | Residual risk |
| --- | --- | --- | --- |
| Critical 1 A2A delivered-but-not-recoverable event | Fixed by durable event classifier and durable journal/snapshot gate. | `tests/a2a/test_pipeline_stream.py`, `tests/a2a/test_pipeline_journal.py` | None |
| Critical 2 Windows read_file path check | Fixed by cross-platform normalization. | `tests/tools/test_read_file.py` | None |
| Historical 1 sidecar two-file consistency | Accepted residual risk. Single-file writes are atomic, but `context.yaml` and `meta.yaml` are not linked by generation/checksum in this batch. | `tests/pipeline/engine/test_session.py` | Crash between the two sidecar writes can leave the pair out of sync. |
```

Fill every Critical, Major, Minor, and historical item from `/Users/ehzyo/open_repo/iac-code3/docs/review.md` with one row.

- [ ] **Step 7: Commit Task 8**

Run:

```bash
git add -f docs/batch/20260616-a2a-pipeline-cancel-handoff.md docs/batch/20260622-pipeline-image-cherry-pick.md docs/batch/20260623-selling-console-cherry-pick.md docs/pipeline-image-manual-test-guide.zh-CN.md docs/pipeline-schema-reference.md docs/review-fix-summary.md scripts/README.md scripts/repl/e2e/README.md scripts/repl/e2e/README.zh-CN.md
git commit -m "docs: document review fix closure"
```

## Task 9: Full Verification And Review Loop

**Files:**
- Modify only if verification or review finds issues.
- Review output: `/Users/ehzyo/open_repo/iac-code3/docs/review-codex.md`, `/Users/ehzyo/open_repo/iac-code3/docs/review.md` if another merge is needed.

- [ ] **Step 1: Run full test suite**

Run:

```bash
make test
```

Expected: PASS. If a test fails, use `superpowers:systematic-debugging`, fix the root cause, and commit the fix.

- [ ] **Step 2: Run lint**

Run:

```bash
make lint
```

Expected: PASS. Fix lint or type issues and commit.

- [ ] **Step 3: Search for review regressions**

Run:

```bash
rg -n "require_durable_metadata=False|journal\\.append\\(|Path\\.replace\\(|shutil\\.move\\(|CLEANUP_PROMPT_METADATA_TYPE|检测到 pipeline rollback|\\[Image input\\]|set\\.update\\([^)]*precompleted_tools|allow_reuse_address|shlex\\.split\\(" src scripts tests docs
```

Expected:

- Remaining `journal.append(` calls are either best-effort display events or pass `durable=True` through classifier/group behavior.
- No raw `Path.replace()` remains for review-scoped A2A snapshot paths.
- No raw `shutil.move()` remains in SessionStorage migration.
- `CLEANUP_PROMPT_METADATA_TYPE` is defined in one low-dependency module and imported elsewhere.
- No Chinese msgids remain in source for cleanup UI text.
- `[Image input]` is translated.
- `set.update(precompleted_tools)` is gone.
- Selling Console Windows socket behavior is explicit.
- REPL E2E `shlex.split()` is guarded.

- [ ] **Step 4: Request code review**

Use `superpowers:requesting-code-review` and dispatch review agents focused on:

- A2A durability and handoff recovery.
- cleanup ledger state preservation and corruption behavior.
- pipeline runner persistence boundaries.
- Windows/i18n/docs/minor closure.

Each reviewer must read code, not only docs, and must review only branch changes from `02f0a57b` onward.

- [ ] **Step 5: Merge review findings**

If reviewers produce findings, merge them into `/Users/ehzyo/open_repo/iac-code3/docs/review.md`, preserving severity and source. Then repeat the repair loop from the relevant task above.

- [ ] **Step 6: Final completion audit**

Before marking the goal complete, verify:

- Every row in `/Users/ehzyo/open_repo/iac-code3/docs/review.md` has a corresponding row in `/Users/ehzyo/open_repo/iac-code3/docs/review-fix-summary.md`.
- `make test` passed after the last code change.
- `make lint` passed after the last code change.
- Latest review round has no actionable findings.

- [ ] **Step 7: Commit final review artifacts**

Run:

```bash
git add -f docs/review.md docs/review-fix-summary.md docs/review-codex.md
git commit -m "docs: record final review closure"
```

## Self-Review Notes

- Spec coverage: the tasks cover state I/O, A2A durable events, active mismatch, cancel handoff atomicity, cleanup source of truth, cleanup ledger merge/mapping/corruption behavior, pipeline sidecar persistence, `/resume` damaged metadata, Windows compatibility, i18n, docs, closure summary, and final review loop.
- Historical hardening coverage: sidecar two-file consistency is explicitly retained as accepted residual risk; journal fsync, snapshot replace retry, SessionStorage save, SessionStorage move, Windows signal fallback, image privacy, and JSONL append serialization are covered by Tasks 1, 2, 3, 7, and 8.
- Placeholder scan: this plan avoids deferred-work markers, generic “handle edge cases” instructions, and abbreviated code.
- Type consistency: new helper names are `atomic_write_text`, `atomic_write_json`, `append_jsonl_locked`, `append_many`, `PipelineStatePersistenceError`, and `CLEANUP_PROMPT_METADATA_TYPE`.
