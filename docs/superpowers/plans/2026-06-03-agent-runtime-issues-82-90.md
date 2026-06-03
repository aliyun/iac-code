# Agent Runtime Issues #82 and #90 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Fix context compaction so it never splits tool round-trips, and fix `AgentTool` runtime state so progress queues are per-call and background tasks can be cancelled.

**Architecture:** Keep each fix inside its existing owner. `ContextManager` computes safe compaction boundaries, `ToolExecutor` passes per-call queues through `ToolContext`, `AgentTool` consumes that context queue and registers background task handles, and `TaskManager` owns task cancellation state.

**Tech Stack:** Python 3.10+, asyncio, pytest, unittest.mock, existing `uv`/Makefile commands.

---

### Task 1: Make Context Compaction Tool-Boundary Safe

**Files:**
- Modify: `src/iac_code/services/context_manager.py:10-178`
- Test: `tests/services/test_context_manager.py`

- [ ] **Step 1: Write failing tests for tool round-trip preservation**

Add `ToolUseBlock` to the import and append these tests to `TestSegmentedCompaction`:

```python
from iac_code.agent.message import TextBlock, ToolResultBlock, ToolUseBlock


def test_apply_compaction_does_not_split_tool_round_trip(self):
    cm = ContextManager(system_prompt="sys", model="qwen")
    cm.add_user_message("User message 0")
    cm.add_assistant_message([TextBlock(text="Assistant response 0")])
    cm.add_user_message("Please read a file")
    cm.add_assistant_message(
        [ToolUseBlock(id="toolu_read", name="read_file", input={"path": "a.txt"})]
    )
    cm.add_tool_results([ToolResultBlock(tool_use_id="toolu_read", content="file contents")])
    cm.add_assistant_message([TextBlock(text="Read complete")])
    cm.add_user_message("User message 2")
    cm.add_assistant_message([TextBlock(text="Assistant response 2")])
    cm.add_user_message("User message 3")
    cm.add_assistant_message([TextBlock(text="Assistant response 3")])

    cm.apply_compaction("Summary of old conversation")

    messages = cm.get_messages()
    assert "Summary" in messages[0].get_text()
    assert messages[1].role == "assistant"
    assert messages[1].get_tool_use_blocks()[0].id == "toolu_read"
    assert messages[2].role == "user"
    assert isinstance(messages[2].content, list)
    assert messages[2].content[0].tool_use_id == "toolu_read"


def test_compaction_keeps_unfinished_tool_use_in_recent_messages(self):
    cm = ContextManager(system_prompt="sys", model="qwen")
    cm.add_user_message("User message 0")
    cm.add_assistant_message([TextBlock(text="Assistant response 0")])
    cm.add_user_message("Start a tool")
    cm.add_assistant_message(
        [ToolUseBlock(id="toolu_pending", name="bash", input={"command": "sleep 1"})]
    )
    cm.add_user_message("Follow-up after interrupted tool use")
    cm.add_assistant_message([TextBlock(text="Assistant response after interruption")])
    cm.add_user_message("User message 3")
    cm.add_assistant_message([TextBlock(text="Assistant response 3")])

    cm.apply_compaction("Summary of old conversation")

    messages = cm.get_messages()
    assert "Summary" in messages[0].get_text()
    assert any(
        msg.get_tool_use_blocks()
        and msg.get_tool_use_blocks()[0].id == "toolu_pending"
        for msg in messages[1:]
    )
```

- [ ] **Step 2: Run the new tests and verify they fail**

Run:

```bash
PATH=/Users/ehzyo/.local/bin:$PATH uv run pytest tests/services/test_context_manager.py::TestSegmentedCompaction -v
```

Expected: the new tool-boundary tests fail because `_split_messages_for_compaction()` still uses `preserve_recent_turns * 2` and leaves the `tool_use` in old messages.

- [ ] **Step 3: Implement safe split helpers in `ContextManager`**

Modify the import and add helper methods before `_split_messages_for_compaction()`:

```python
from iac_code.agent.message import ContentBlock, Conversation, Message, ToolResultBlock, ToolUseBlock
```

```python
    @staticmethod
    def _tool_use_ids(message: Message) -> set[str]:
        if isinstance(message.content, str):
            return set()
        return {block.id for block in message.content if isinstance(block, ToolUseBlock)}

    @staticmethod
    def _tool_result_ids(message: Message) -> set[str]:
        if isinstance(message.content, str):
            return set()
        return {block.tool_use_id for block in message.content if isinstance(block, ToolResultBlock)}

    @classmethod
    def _find_safe_compaction_split(cls, messages: list[Message], split_point: int) -> int:
        split_point = max(0, min(split_point, len(messages)))
        while split_point > 0:
            old_messages = messages[:split_point]
            old_tool_uses: dict[str, int] = {}
            old_tool_results: set[str] = set()

            for index, message in enumerate(old_messages):
                for tool_use_id in cls._tool_use_ids(message):
                    old_tool_uses.setdefault(tool_use_id, index)
                old_tool_results.update(cls._tool_result_ids(message))

            unpaired_tool_uses = set(old_tool_uses) - old_tool_results
            if not unpaired_tool_uses:
                return split_point

            split_point = min(old_tool_uses[tool_use_id] for tool_use_id in unpaired_tool_uses)

        return split_point
```

Then change `_split_messages_for_compaction()`:

```python
        split_point = len(messages) - preserve_count
        split_point = self._find_safe_compaction_split(messages, split_point)
        return messages[:split_point], messages[split_point:]
```

- [ ] **Step 4: Run context manager tests**

Run:

```bash
PATH=/Users/ehzyo/.local/bin:$PATH uv run pytest tests/services/test_context_manager.py -v
```

Expected: all tests in `tests/services/test_context_manager.py` pass, including existing text-only compaction tests.

- [ ] **Step 5: Commit context compaction fix**

Run:

```bash
git add src/iac_code/services/context_manager.py tests/services/test_context_manager.py
git commit -m "fix: preserve tool round trips during compaction"
```

### Task 2: Move Agent Progress Queues To Per-Call ToolContext

**Files:**
- Modify: `src/iac_code/tools/tool_executor.py:1-75`
- Modify: `src/iac_code/agent/agent_tool.py:113-207`
- Test: `tests/tools/test_tool_executor.py`
- Test: `tests/agent/test_agent_tool.py`

- [ ] **Step 1: Add failing executor test for context queue isolation**

Append this test to `TestToolExecutor`:

```python
    async def test_event_queue_is_passed_only_through_context(self):
        class QueueAwareTool(FakeReadTool):
            def __init__(self):
                self._event_queue = None
                self.seen_context_queues = {}

            async def execute(self, *, tool_input, context):
                self.seen_context_queues[tool_input["name"]] = context.event_queue
                return ToolResult.success(tool_input["name"])

        tool = QueueAwareTool()
        registry = MagicMock()
        registry.get = lambda name: tool
        executor = ToolExecutor(registry=registry)
        first_queue = asyncio.Queue()
        second_queue = asyncio.Queue()
        calls = [
            ToolCallRequest(id="a", name="read", input={"name": "first"}, event_queue=first_queue),
            ToolCallRequest(id="b", name="read", input={"name": "second"}, event_queue=second_queue),
        ]

        results = await executor.execute_batch(calls, ToolContext())

        assert [result.content for result in results] == ["first", "second"]
        assert tool.seen_context_queues == {"first": first_queue, "second": second_queue}
        assert tool._event_queue is None
```

- [ ] **Step 2: Add failing `AgentTool` tests for `ToolContext.event_queue`**

Replace the existing queue tests in `TestAgentToolExecution` with these tests:

```python
    async def test_execute_uses_context_event_queue_on_success(self):
        tool = AgentTool()
        queue = asyncio.Queue()

        with patch(
            "iac_code.agent.agent_tool.run_sub_agent",
            new_callable=AsyncMock,
            return_value=("Done", AgentProgress(tool_use_count=2, token_count=99)),
        ) as run_sub_agent:
            result = await tool.execute(
                tool_input={"prompt": "Find files", "description": "Find"},
                context=ToolContext(event_queue=queue),
            )

        assert result.is_error is False
        assert run_sub_agent.await_args.kwargs["event_queue"] is queue
        assert await queue.get() is None

    async def test_execute_closes_context_event_queue_on_failure(self):
        tool = AgentTool()
        queue = asyncio.Queue()

        with patch("iac_code.agent.agent_tool.run_sub_agent", new_callable=AsyncMock, side_effect=RuntimeError("boom")):
            result = await tool.execute(
                tool_input={"prompt": "Find files", "description": "Find"},
                context=ToolContext(event_queue=queue),
            )

        assert result.is_error is True
        assert "boom" in result.content
        assert await queue.get() is None
```

- [ ] **Step 3: Run the queue tests and verify they fail**

Run:

```bash
PATH=/Users/ehzyo/.local/bin:$PATH uv run pytest tests/tools/test_tool_executor.py::TestToolExecutor::test_event_queue_is_passed_only_through_context tests/agent/test_agent_tool.py::TestAgentToolExecution::test_execute_uses_context_event_queue_on_success tests/agent/test_agent_tool.py::TestAgentToolExecution::test_execute_closes_context_event_queue_on_failure -v
```

Expected: executor test fails because `_event_queue` is mutated; `AgentTool` tests fail because `execute()` reads `self._event_queue` instead of `context.event_queue`.

- [ ] **Step 4: Remove shared queue mutation from `ToolExecutor`**

Delete `Protocol`, `cast`, `TYPE_CHECKING` remains, and the `_ToolWithEventQueue` protocol. Keep `ToolContext` wrapping only:

```python
from typing import TYPE_CHECKING
```

```python
        # Pass event_queue from call to context for tools that emit progress events.
        if call.event_queue is not None:
            context = ToolContext(cwd=context.cwd, event_queue=call.event_queue)
```

- [ ] **Step 5: Make `AgentTool` use the context queue**

Remove `self._event_queue` from `__init__()`. In `execute()`, read the queue once:

```python
        event_queue = context.event_queue
```

Pass and close that queue:

```python
                event_queue=event_queue,
```

```python
            if event_queue is not None:
                await event_queue.put(None)
```

Use the same `event_queue is not None` close path in the `except Exception as e:` block.

- [ ] **Step 6: Run focused queue tests**

Run:

```bash
PATH=/Users/ehzyo/.local/bin:$PATH uv run pytest tests/tools/test_tool_executor.py tests/agent/test_agent_tool.py -v
```

Expected: both test files pass. `AgentTool.is_concurrency_safe({})` remains `True`.

- [ ] **Step 7: Commit event queue fix**

Run:

```bash
git add src/iac_code/tools/tool_executor.py src/iac_code/agent/agent_tool.py tests/tools/test_tool_executor.py tests/agent/test_agent_tool.py
git commit -m "fix: isolate agent progress event queues"
```

### Task 3: Track And Cancel Background Agent Tasks

**Files:**
- Modify: `src/iac_code/tasks/task_state.py:1-66`
- Modify: `src/iac_code/agent/agent_tool.py:1-243`
- Test: `tests/tasks/test_task_state.py`
- Test: `tests/agent/test_agent_tool.py`

- [ ] **Step 1: Add failing `TaskManager` cancellation tests**

Update imports in `tests/tasks/test_task_state.py`:

```python
import asyncio
import contextlib

import pytest

from iac_code.tasks.task_state import TaskManager, TaskStatus
```

Append these tests:

```python
    @pytest.mark.asyncio
    async def test_stop_cancels_registered_asyncio_task(self):
        mgr = TaskManager()
        tid = mgr.register(description="test")

        async def wait_forever():
            await asyncio.Event().wait()

        background_task = asyncio.create_task(wait_forever())
        try:
            mgr.attach_task(tid, background_task)
            mgr.stop(tid)
            await asyncio.sleep(0)

            assert mgr.get(tid).status == TaskStatus.STOPPED
            assert background_task.cancelled()
        finally:
            background_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await background_task

    def test_complete_and_fail_do_not_override_stopped_task(self):
        mgr = TaskManager()
        tid = mgr.register(description="test")

        mgr.stop(tid)
        mgr.complete(tid, result="done")
        mgr.fail(tid, error="boom")

        task = mgr.get(tid)
        assert task.status == TaskStatus.STOPPED
        assert task.result is None
        assert task.error is None
```

- [ ] **Step 2: Add failing `AgentTool` cancellation tests**

Update imports in `tests/agent/test_agent_tool.py`:

```python
from iac_code.tasks.task_state import TaskManager, TaskStatus
```

Append these tests to `TestAgentToolExecution`:

```python
    async def test_background_execution_attaches_task_handle(self):
        tm = TaskManager()
        tool = AgentTool(task_manager=tm)
        release = asyncio.Event()

        async def fake_run_sub_agent(**kwargs):
            await release.wait()
            return "bg done", AgentProgress()

        with patch("iac_code.agent.agent_tool.run_sub_agent", side_effect=fake_run_sub_agent):
            result = await tool.execute(
                tool_input={"prompt": "task", "description": "bg", "run_in_background": True},
                context=ToolContext(),
            )

        task_info = tm.list_all()[0]
        assert "task_id" in result.content
        assert task_info.background_task is not None
        assert not task_info.background_task.done()

        tm.stop(task_info.id)
        release.set()
        with contextlib.suppress(asyncio.CancelledError):
            await task_info.background_task

    async def test_run_background_cancellation_marks_task_stopped(self):
        tm = TaskManager()
        tid = tm.register(description="bg")
        tool = AgentTool(task_manager=tm)

        with patch("iac_code.agent.agent_tool.run_sub_agent", new_callable=AsyncMock, side_effect=asyncio.CancelledError):
            with pytest.raises(asyncio.CancelledError):
                await tool._run_background(tid, "prompt", "general-purpose", ToolContext(cwd="/tmp"))

        assert tm.get(tid).status == TaskStatus.STOPPED
```

If `contextlib` is not already imported in this file, add:

```python
import contextlib
```

- [ ] **Step 3: Run the cancellation tests and verify they fail**

Run:

```bash
PATH=/Users/ehzyo/.local/bin:$PATH uv run pytest tests/tasks/test_task_state.py::TestTaskManager::test_stop_cancels_registered_asyncio_task tests/tasks/test_task_state.py::TestTaskManager::test_complete_and_fail_do_not_override_stopped_task tests/agent/test_agent_tool.py::TestAgentToolExecution::test_background_execution_attaches_task_handle tests/agent/test_agent_tool.py::TestAgentToolExecution::test_run_background_cancellation_marks_task_stopped -v
```

Expected: tests fail because `TaskManager` has no `attach_task()` or `background_task`, and `_run_background()` does not handle `CancelledError`.

- [ ] **Step 4: Extend `TaskManager` with task handles**

Modify `src/iac_code/tasks/task_state.py`:

```python
import asyncio
import uuid
from collections import OrderedDict
from dataclasses import dataclass, field
from enum import Enum
from typing import Any
```

```python
    background_task: asyncio.Task[Any] | None = field(default=None, repr=False, compare=False)
```

Add this method:

```python
    def attach_task(self, task_id: str, background_task: asyncio.Task[Any]) -> None:
        task = self._tasks.get(task_id)
        if task:
            task.background_task = background_task
```

Update state transitions:

```python
    def complete(self, task_id: str, result: str) -> None:
        task = self._tasks.get(task_id)
        if task and task.status != TaskStatus.STOPPED:
            task.status = TaskStatus.COMPLETED
            task.result = result

    def fail(self, task_id: str, error: str) -> None:
        task = self._tasks.get(task_id)
        if task and task.status != TaskStatus.STOPPED:
            task.status = TaskStatus.FAILED
            task.error = error

    def stop(self, task_id: str) -> None:
        task = self._tasks.get(task_id)
        if task:
            task.status = TaskStatus.STOPPED
            if task.background_task is not None and not task.background_task.done():
                task.background_task.cancel()
```

- [ ] **Step 5: Register and consume background tasks in `AgentTool`**

Modify `src/iac_code/agent/agent_tool.py` imports:

```python
import asyncio
from contextlib import suppress
```

In `execute()` background path:

```python
            background_task = asyncio.create_task(self._run_background(task_id, prompt, agent_type, context))
            if hasattr(self._task_manager, "attach_task"):
                self._task_manager.attach_task(task_id, background_task)
            background_task.add_done_callback(self._consume_background_task_exception)
            return ToolResult.success(f"Background agent launched (task_id: {task_id}, type: {agent_type})")
```

Add a callback method to `AgentTool`:

```python
    @staticmethod
    def _consume_background_task_exception(task: asyncio.Task) -> None:
        if task.cancelled():
            return
        with suppress(asyncio.CancelledError):
            task.exception()
```

Add the cancellation branch before `except Exception as e:` in `_run_background()`:

```python
        except asyncio.CancelledError:
            self._task_manager.stop(task_id)
            if self._notification_queue:
                self._notification_queue.enqueue(
                    task_id=task_id,
                    message="Agent stopped",
                )
            raise
```

- [ ] **Step 6: Run task and agent tests**

Run:

```bash
PATH=/Users/ehzyo/.local/bin:$PATH uv run pytest tests/tasks/test_task_state.py tests/tasks/test_task_tools.py tests/commands/test_tasks.py tests/agent/test_agent_tool.py -v
```

Expected: task lifecycle tests pass, and existing `/tasks` command/tool behavior remains compatible.

- [ ] **Step 7: Commit background task lifecycle fix**

Run:

```bash
git add src/iac_code/tasks/task_state.py src/iac_code/agent/agent_tool.py tests/tasks/test_task_state.py tests/agent/test_agent_tool.py
git commit -m "fix: cancel background agent tasks"
```

### Task 4: Final Regression And Known Baseline

**Files:**
- Review: `src/iac_code/services/context_manager.py`
- Review: `src/iac_code/tools/tool_executor.py`
- Review: `src/iac_code/agent/agent_tool.py`
- Review: `src/iac_code/tasks/task_state.py`

- [ ] **Step 1: Run focused regression suite**

Run:

```bash
PATH=/Users/ehzyo/.local/bin:$PATH uv run pytest tests/services/test_context_manager.py tests/tools/test_tool_executor.py tests/agent/test_agent_tool.py tests/tasks/test_task_state.py tests/tasks/test_task_tools.py tests/commands/test_tasks.py -v
```

Expected: all focused tests pass.

- [ ] **Step 2: Run lint**

Run:

```bash
PATH=/Users/ehzyo/.local/bin:$PATH make lint
```

Expected: lint and type checks pass, or failures are unrelated to the touched files and are documented before final handoff.

- [ ] **Step 3: Run full test suite**

Run:

```bash
PATH=/Users/ehzyo/.local/bin:$PATH make test
```

Expected: issue-specific regressions pass. If the full suite still fails only on `tests/test_i18n.py` because `src/iac_code/i18n/messages.pot` is missing, record the exact summary and note that this was the pre-existing baseline failure observed before implementation.

- [ ] **Step 4: Inspect final diff**

Run:

```bash
git status --short
git diff --stat
git diff --check
```

Expected: only files listed in this plan are modified, and `git diff --check` reports no whitespace errors.

- [ ] **Step 5: Commit final regression adjustments**

If Task 4 produced only verification notes and no file changes, do not create a commit. If small fixes were required during final regression, commit them:

```bash
git add src/iac_code/services/context_manager.py src/iac_code/tools/tool_executor.py src/iac_code/agent/agent_tool.py src/iac_code/tasks/task_state.py tests/services/test_context_manager.py tests/tools/test_tool_executor.py tests/agent/test_agent_tool.py tests/tasks/test_task_state.py tests/tasks/test_task_tools.py tests/commands/test_tasks.py
git commit -m "test: cover agent runtime regressions"
```
