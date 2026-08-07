from __future__ import annotations

import asyncio
import io
import os
import socket
import struct
import time
from types import SimpleNamespace

import pytest

from iac_code.desktop.control import (
    ControlProtocolError,
    DesktopControlDispatcher,
    FramedControl,
    encode_control_message,
)
from iac_code.desktop.controller import DesktopRuntimeController
from iac_code.web.session_manager import WebSessionManager


def test_control_frame_round_trip() -> None:
    message = {"type": "ready", "port": 8766, "sidecarGeneration": 4}
    control = FramedControl(io.BytesIO(encode_control_message(message)))

    assert control.read() == message
    assert control.read() is None


def test_control_writer_uses_big_endian_length_prefix() -> None:
    stream = io.BytesIO()
    control = FramedControl(stream)

    control.write({"type": "close-status"})

    frame = stream.getvalue()
    assert struct.unpack(">I", frame[:4])[0] == len(frame) - 4


def test_control_can_use_separate_reader_and_writer_streams() -> None:
    message = {"type": "ready", "port": 8766}
    reader = io.BytesIO(encode_control_message(message))
    writer = io.BytesIO()
    control = FramedControl(reader, writer)

    assert control.read() == message
    control.write({"type": "close-status"})

    frame = writer.getvalue()
    assert struct.unpack(">I", frame[:4])[0] == len(frame) - 4


@pytest.mark.parametrize(
    "frame",
    [
        struct.pack(">I", 0),
        struct.pack(">I", 3) + b"{}\n",
        struct.pack(">I", 2) + b"[]",
    ],
)
def test_control_rejects_invalid_frames(frame: bytes) -> None:
    with pytest.raises(ControlProtocolError):
        FramedControl(io.BytesIO(frame)).read()


@pytest.mark.skipif(os.name == "nt", reason="the named pipe is exercised by Windows integration tests")
def test_named_pipe_control_is_rejected_off_windows() -> None:
    with pytest.raises(ValueError, match="only available on Windows"):
        FramedControl.from_named_pipe(r"\\.\pipe\iac-code-desktop-test")


@pytest.mark.skipif(os.name == "nt", reason="uses a POSIX socketpair")
def test_dispatcher_routes_child_ack_without_losing_host_command() -> None:
    async def exercise() -> None:
        sidecar_socket, host_socket = socket.socketpair()
        sidecar = FramedControl(sidecar_socket.makefile("rwb", buffering=0))
        host = FramedControl(host_socket.makefile("rwb", buffering=0))
        dispatcher = DesktopControlDispatcher(sidecar, asyncio.get_running_loop(), 41)
        dispatcher.start()
        try:
            registration = asyncio.create_task(asyncio.to_thread(dispatcher.register_child_group, 8123, "bash"))
            request = await asyncio.to_thread(host.read)
            assert request == {
                "type": "register-child-group",
                "sidecarGeneration": 41,
                "registrationId": 1,
                "pgid": 8123,
                "kind": "bash",
            }
            host.write(
                {
                    "type": "child-group-registered",
                    "sidecarGeneration": 41,
                    "registrationId": 1,
                }
            )
            assert await registration == 1

            host.write({"type": "prepare-close", "reason": "test"})
            assert await asyncio.wait_for(dispatcher.queue.get(), 1) == {
                "type": "prepare-close",
                "reason": "test",
            }
        finally:
            host.close()
            sidecar_socket.close()
            host_socket.close()

    asyncio.run(exercise())


@pytest.mark.skipif(os.name == "nt", reason="uses POSIX file descriptors")
def test_dispatcher_host_eof_closes_guardian_liveness_writers() -> None:
    async def exercise() -> None:
        sidecar_socket, host_socket = socket.socketpair()
        sidecar = FramedControl(sidecar_socket.makefile("rwb", buffering=0))
        host_stream = host_socket.makefile("rwb", buffering=0)
        dispatcher = DesktopControlDispatcher(sidecar, asyncio.get_running_loop(), 42)
        dispatcher.start()
        reader_fd, writer_fd = os.pipe()
        reader = os.fdopen(reader_fd, "rb", buffering=0)
        writer = os.fdopen(writer_fd, "wb", buffering=0)
        dispatcher.attach_guardian_writer(1, writer)
        host_stream.close()
        host_socket.close()
        deadline = time.monotonic() + 1
        while not dispatcher.parent_gone and time.monotonic() < deadline:
            await asyncio.sleep(0.01)
        assert dispatcher.parent_gone
        assert await asyncio.to_thread(reader.read, 1) == b""
        reader.close()
        sidecar_socket.close()

    asyncio.run(exercise())


def test_register_ack_failure_uses_same_request_id_for_compensating_complete(monkeypatch) -> None:
    loop = asyncio.new_event_loop()
    dispatcher = DesktopControlDispatcher(FramedControl(io.BytesIO()), loop, 43)
    messages: list[dict[str, object]] = []

    def request_ack(message, response_type, registration_id, timeout):
        messages.append(dict(message))
        if message["type"] == "register-child-group":
            raise TimeoutError("registered ACK was lost")
        return {"type": response_type, "registrationId": registration_id}

    monkeypatch.setattr(dispatcher, "_request_ack", request_ack)
    try:
        with pytest.raises(TimeoutError, match="ACK was lost"):
            dispatcher.register_child_group(8123, "bash")
    finally:
        loop.close()

    assert [message["type"] for message in messages] == ["register-child-group", "complete-child-group"]
    assert messages[0]["registrationId"] == messages[1]["registrationId"] == 1
    assert messages[0]["pgid"] == messages[1]["pgid"] == 8123


@pytest.mark.asyncio
async def test_committed_shutdown_publishes_desktop_closing_but_prepare_close_does_not(
    monkeypatch,
    tmp_path,
) -> None:
    from iac_code.desktop import __main__ as desktop_main

    project = tmp_path / "project"
    project.mkdir()
    manager = WebSessionManager(projects_dir=tmp_path / "sessions", cwd=project)
    session = manager.create_session(cwd=str(project))
    controller = DesktopRuntimeController(manager, project)
    messages: asyncio.Queue[dict[str, object] | None] = asyncio.Queue()
    control_messages: list[dict[str, object]] = []
    dispatcher = SimpleNamespace(
        queue=messages,
        control=SimpleNamespace(write=lambda message: control_messages.append(message)),
    )
    app = SimpleNamespace(state=SimpleNamespace(desktop_controller=controller))
    server = SimpleNamespace(force_exit=False, should_exit=False)
    monkeypatch.setattr(desktop_main, "_DESKTOP_CLOSING_FLUSH_SECONDS", 0)
    task = asyncio.create_task(desktop_main._dispatch_control(dispatcher, app, server))

    messages.put_nowait({"type": "prepare-close"})
    for _ in range(20):
        if control_messages:
            break
        await asyncio.sleep(0)
    assert control_messages and control_messages[0]["type"] == "close-state"
    assert not any(event["type"] == "desktop-closing" for event in session.events.replay_after(0))

    messages.put_nowait({"type": "shutdown", "force": False})
    await asyncio.wait_for(task, timeout=1)

    closing = [event for event in session.events.replay_after(0) if event["type"] == "desktop-closing"]
    assert len(closing) == 1
    assert closing[0]["payload"] == {"force": False}
    assert server.force_exit is False
    assert server.should_exit is True


@pytest.mark.asyncio
async def test_desktop_closing_notification_has_a_hard_deadline(monkeypatch) -> None:
    from iac_code.desktop import __main__ as desktop_main

    class Events:
        async def publish(self, event_type, payload):
            await asyncio.Event().wait()

    controller = SimpleNamespace(manager=SimpleNamespace(loaded_sessions=lambda: (SimpleNamespace(events=Events()),)))
    monkeypatch.setattr(desktop_main, "_DESKTOP_CLOSING_PUBLISH_SECONDS", 0.01)
    monkeypatch.setattr(desktop_main, "_DESKTOP_CLOSING_FLUSH_SECONDS", 0)

    await asyncio.wait_for(desktop_main._publish_desktop_closing(controller, force=True), timeout=0.2)
