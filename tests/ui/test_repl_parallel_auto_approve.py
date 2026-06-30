"""Parallel sub-pipeline permission requests are prompted serially."""

from __future__ import annotations

import asyncio
import time
from io import StringIO
from unittest.mock import AsyncMock, MagicMock

import pytest
from rich.console import Console


class _FakeLive:
    instances = []

    def __init__(self, *args, **kwargs):
        self.start_count = 0
        self.stop_count = 0
        self.update_count = 0
        _FakeLive.instances.append(self)

    def start(self):
        self.start_count += 1

    def stop(self):
        self.stop_count += 1

    def update(self, *args, **kwargs):
        self.update_count += 1


async def _stream_permission_request(response_future: asyncio.Future):
    from iac_code.pipeline.engine.events import PipelineEvent, PipelineEventType
    from iac_code.types.stream_events import PermissionRequestEvent, SubPipelineStreamEvent

    sub_id = "sub_test_abcd1234"
    yield PipelineEvent(
        type=PipelineEventType.SUB_PIPELINE_STARTED,
        step_id=None,
        timestamp=time.time(),
        data={
            "sub_pipeline_id": sub_id,
            "candidate_index": 0,
            "candidate_name": "方案1",
            "total_steps": 1,
            "sub_pipeline_name": "test",
        },
    )
    yield SubPipelineStreamEvent(
        sub_pipeline_id=sub_id,
        candidate_index=0,
        inner=PermissionRequestEvent(
            tool_name="bash",
            tool_input={},
            tool_use_id="t0",
            response_future=response_future,
        ),
    )
    yield PipelineEvent(
        type=PipelineEventType.SUB_PIPELINE_COMPLETED,
        step_id=None,
        timestamp=time.time(),
        data={
            "sub_pipeline_id": sub_id,
            "candidate_index": 0,
            "failed": False,
        },
    )
    yield PipelineEvent(
        type=PipelineEventType.STEP_COMPLETED,
        step_id=None,
        timestamp=time.time(),
        data={},
    )


async def _stream_nested_permission_request(response_future: asyncio.Future):
    from iac_code.pipeline.engine.events import PipelineEvent, PipelineEventType
    from iac_code.types.stream_events import PermissionRequestEvent, SubPipelineStreamEvent

    sub_id = "sub_nested_inner"
    yield PipelineEvent(
        type=PipelineEventType.SUB_PIPELINE_STARTED,
        step_id=None,
        timestamp=time.time(),
        data={
            "sub_pipeline_id": sub_id,
            "candidate_index": 0,
            "candidate_name": "方案1",
            "total_steps": 1,
            "sub_pipeline_name": "test",
        },
    )
    yield SubPipelineStreamEvent(
        sub_pipeline_id="sub_nested_outer",
        candidate_index=1,
        inner=SubPipelineStreamEvent(
            sub_pipeline_id=sub_id,
            candidate_index=0,
            inner=PermissionRequestEvent(
                tool_name="bash",
                tool_input={},
                tool_use_id="t_nested",
                response_future=response_future,
            ),
        ),
    )
    yield PipelineEvent(
        type=PipelineEventType.STEP_COMPLETED,
        step_id=None,
        timestamp=time.time(),
        data={},
    )


def _make_repl(prompt_result: bool):
    from iac_code.ui.repl import InlineREPL

    repl = InlineREPL.__new__(InlineREPL)
    repl.renderer = MagicMock()
    repl.renderer.console = Console(file=StringIO(), width=120, force_terminal=True, record=True)
    repl.renderer.prompt_permission = AsyncMock(return_value=prompt_result)
    repl._pipeline = MagicMock()
    return repl


def _console_text(repl) -> str:
    return repl.renderer.console.export_text()


@pytest.fixture
def fake_live(monkeypatch):
    from iac_code.ui import repl as repl_module

    _FakeLive.instances = []
    monkeypatch.setattr(repl_module.InlineREPL, "_create_parallel_live", lambda self: _FakeLive(), raising=False)
    return _FakeLive


@pytest.fixture
def key_reader_tasks(monkeypatch):
    from iac_code.ui import repl as repl_module

    original_create_task = asyncio.create_task
    created = []

    def create_task(coro, *args, **kwargs):
        if getattr(getattr(coro, "cr_code", None), "co_name", None) == "key_reader":
            created.append(coro)
        return original_create_task(coro, *args, **kwargs)

    monkeypatch.setattr(repl_module.asyncio, "create_task", create_task)
    return created


@pytest.mark.asyncio
async def test_parallel_permission_request_prompts_and_allows():
    repl = _make_repl(prompt_result=True)
    future = asyncio.get_running_loop().create_future()

    interrupted = await repl._render_parallel_tabs(_stream_permission_request(future))

    assert interrupted is False
    repl.renderer.prompt_permission.assert_awaited_once()
    assert future.done()
    assert future.result() is True
    assert "auto-approved" not in _console_text(repl)


@pytest.mark.asyncio
async def test_parallel_nested_permission_request_prompts_and_allows():
    repl = _make_repl(prompt_result=True)
    future = asyncio.get_running_loop().create_future()

    interrupted = await repl._render_parallel_tabs(_stream_nested_permission_request(future))

    assert interrupted is False
    repl.renderer.prompt_permission.assert_awaited_once()
    assert future.done()
    assert future.result() is True
    assert "auto-approved" not in _console_text(repl)


@pytest.mark.asyncio
async def test_parallel_permission_request_prompts_and_denies():
    repl = _make_repl(prompt_result=False)
    future = asyncio.get_running_loop().create_future()

    interrupted = await repl._render_parallel_tabs(_stream_permission_request(future))

    assert interrupted is False
    repl.renderer.prompt_permission.assert_awaited_once()
    assert future.done()
    assert future.result() is False
    assert "auto-approved" not in _console_text(repl)


@pytest.mark.asyncio
async def test_parallel_permission_done_future_is_not_prompted():
    repl = _make_repl(prompt_result=True)
    future = asyncio.get_running_loop().create_future()
    future.set_result(False)

    interrupted = await repl._render_parallel_tabs(_stream_permission_request(future))

    assert interrupted is False
    repl.renderer.prompt_permission.assert_not_awaited()
    assert future.result() is False
    assert "auto-approved" not in _console_text(repl)


@pytest.mark.asyncio
async def test_parallel_permission_prompt_cancellation_denies_without_restarting_ui(fake_live, key_reader_tasks):
    repl = _make_repl(prompt_result=True)
    repl.renderer.prompt_permission = AsyncMock(side_effect=asyncio.CancelledError)
    future = asyncio.get_running_loop().create_future()

    with pytest.raises(asyncio.CancelledError):
        await repl._render_parallel_tabs(_stream_permission_request(future))

    repl.renderer.prompt_permission.assert_awaited_once()
    assert future.done()
    assert future.result() is False
    assert sum(live.start_count for live in fake_live.instances) == 1
    assert len(key_reader_tasks) == 1


@pytest.mark.asyncio
async def test_parallel_parent_cancellation_during_key_reader_shutdown_propagates(monkeypatch, fake_live):
    from iac_code.pipeline.engine.events import PipelineEvent, PipelineEventType
    from iac_code.types.stream_events import PermissionRequestEvent, SubPipelineStreamEvent
    from iac_code.ui import repl as repl_module

    repl = _make_repl(prompt_result=True)
    future = asyncio.get_running_loop().create_future()
    sub_id = "sub_parent_cancel"
    key_reader_started = asyncio.Event()
    key_reader_cancelled = asyncio.Event()
    permission_can_arrive = asyncio.Event()
    release_key_reader = asyncio.Event()
    original_create_task = asyncio.create_task

    async def stream():
        yield PipelineEvent(
            type=PipelineEventType.SUB_PIPELINE_STARTED,
            step_id=None,
            timestamp=time.time(),
            data={
                "sub_pipeline_id": sub_id,
                "candidate_index": 0,
                "candidate_name": "方案1",
                "total_steps": 1,
                "sub_pipeline_name": "test",
            },
        )
        await permission_can_arrive.wait()
        yield SubPipelineStreamEvent(
            sub_pipeline_id=sub_id,
            candidate_index=0,
            inner=PermissionRequestEvent(
                tool_name="bash",
                tool_input={},
                tool_use_id="t_parent_cancel",
                response_future=future,
            ),
        )

    async def fake_key_reader():
        key_reader_started.set()
        try:
            await asyncio.Future()
        except asyncio.CancelledError:
            key_reader_cancelled.set()
            await release_key_reader.wait()

    def create_task(coro, *args, **kwargs):
        if getattr(getattr(coro, "cr_code", None), "co_name", None) == "key_reader":
            coro.close()
            return original_create_task(fake_key_reader())
        return original_create_task(coro, *args, **kwargs)

    monkeypatch.setattr(repl_module.asyncio, "create_task", create_task)

    render_task = original_create_task(repl._render_parallel_tabs(stream()))
    await asyncio.wait_for(key_reader_started.wait(), timeout=1)
    permission_can_arrive.set()
    await asyncio.wait_for(key_reader_cancelled.wait(), timeout=1)

    render_task.cancel()
    release_key_reader.set()

    with pytest.raises(asyncio.CancelledError):
        await render_task
    repl.renderer.prompt_permission.assert_not_awaited()
    assert future.done()
    assert future.result() is False
    assert sum(live.start_count for live in fake_live.instances) == 1


@pytest.mark.asyncio
async def test_parallel_tabs_ctrl_c_key_cancels_parent(monkeypatch, fake_live):
    from iac_code.pipeline.engine.events import PipelineEvent, PipelineEventType
    from iac_code.ui.core.key_event import KeyEvent

    repl = _make_repl(prompt_result=True)
    repl._pipeline.pause_agent_loops = MagicMock()

    class FakeCapture:
        def __init__(self, *args, **kwargs):
            self._sent = False

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def read_key(self, timeout):
            if self._sent:
                if timeout:
                    time.sleep(min(timeout, 0.05))
                return None
            self._sent = True
            return KeyEvent(key="c", char="\x03", ctrl=True)

    monkeypatch.setattr("iac_code.ui.core.raw_input.RawInputCapture", FakeCapture)

    async def stream():
        yield PipelineEvent(
            type=PipelineEventType.SUB_PIPELINE_STARTED,
            step_id=None,
            timestamp=time.time(),
            data={
                "sub_pipeline_id": "sub_test_ctrl_c",
                "candidate_index": 0,
                "candidate_name": "方案1",
                "total_steps": 1,
                "sub_pipeline_name": "test",
            },
        )
        await asyncio.Future()

    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(repl._render_parallel_tabs(stream()), timeout=5.0)


@pytest.mark.asyncio
async def test_parallel_tabs_escape_interrupt_forwards_pipeline_user_input(monkeypatch, fake_live):
    from iac_code.agent.message import ImageBlock, TextBlock
    from iac_code.pipeline.engine.events import PipelineEvent, PipelineEventType
    from iac_code.pipeline.engine.user_input import PipelineUserInput
    from iac_code.ui.core.key_event import KeyEvent

    image_input = PipelineUserInput(
        content=[
            TextBlock(text="change"),
            ImageBlock(media_type="image/png", data="aGVsbG8="),
        ],
        display_text="change [Image #1]",
        has_images=True,
    )

    repl = _make_repl(prompt_result=True)
    repl._pipeline_waiting_input = False
    repl._read_pipeline_interrupt_input = AsyncMock(return_value=image_input)
    repl._handle_mid_pipeline_message = AsyncMock(return_value=(False, "feedback"))

    pause_called = asyncio.Event()
    repl._pipeline.pause_agent_loops = MagicMock(side_effect=pause_called.set)
    repl._pipeline.resume_agent_loops = MagicMock()

    class FakeCapture:
        def __init__(self, *args, **kwargs):
            self._sent = False

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def read_key(self, timeout):
            if self._sent:
                if timeout:
                    time.sleep(min(timeout, 0.05))
                return None
            self._sent = True
            return KeyEvent(key="escape", char="\x1b")

    monkeypatch.setattr("iac_code.ui.core.raw_input.RawInputCapture", FakeCapture)

    async def stream():
        yield PipelineEvent(
            type=PipelineEventType.SUB_PIPELINE_STARTED,
            step_id=None,
            timestamp=time.time(),
            data={
                "sub_pipeline_id": "sub_test_escape",
                "candidate_index": 0,
                "candidate_name": "方案1",
                "total_steps": 1,
                "sub_pipeline_name": "test",
            },
        )
        await asyncio.wait_for(pause_called.wait(), timeout=1.0)
        yield PipelineEvent(
            type=PipelineEventType.STEP_COMPLETED,
            step_id=None,
            timestamp=time.time(),
            data={},
        )

    interrupted = await asyncio.wait_for(repl._render_parallel_tabs(stream()), timeout=5.0)

    assert interrupted is False
    repl._read_pipeline_interrupt_input.assert_awaited_once()
    repl._handle_mid_pipeline_message.assert_awaited_once_with(image_input, suppress_render=True)


@pytest.mark.asyncio
async def test_parallel_permission_prompt_exception_denies_and_resumes_ui(fake_live, key_reader_tasks):
    repl = _make_repl(prompt_result=True)
    repl.renderer.prompt_permission = AsyncMock(side_effect=RuntimeError("prompt failed"))
    future = asyncio.get_running_loop().create_future()

    interrupted = await repl._render_parallel_tabs(_stream_permission_request(future))

    assert interrupted is False
    repl.renderer.prompt_permission.assert_awaited_once()
    assert future.done()
    assert future.result() is False
    assert sum(live.start_count for live in fake_live.instances) == 2
    assert len(key_reader_tasks) == 2


@pytest.mark.asyncio
async def test_parallel_permission_cancelled_future_is_not_prompted():
    repl = _make_repl(prompt_result=True)
    future = asyncio.get_running_loop().create_future()
    future.cancel()

    interrupted = await repl._render_parallel_tabs(_stream_permission_request(future))

    assert interrupted is False
    repl.renderer.prompt_permission.assert_not_awaited()
    assert future.cancelled()
    assert "auto-approved" not in _console_text(repl)
