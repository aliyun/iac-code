"""Small length-prefixed JSON control protocol used by the Desktop sidecar."""

from __future__ import annotations

import asyncio
import json
import os
import queue
import struct
import threading
import time
from collections.abc import Mapping
from typing import Any, BinaryIO, cast

_HEADER = struct.Struct(">I")
MAX_CONTROL_MESSAGE_BYTES = 1024 * 1024


class _WindowsNamedPipeReader:
    """Poll a synchronous pipe before reading so sidecar writes cannot deadlock."""

    def __init__(self, stream: BinaryIO) -> None:
        import ctypes
        import msvcrt

        self._ctypes = ctypes
        self._stream = stream
        get_osfhandle = getattr(msvcrt, "get_osfhandle")
        self._handle = ctypes.c_void_p(get_osfhandle(stream.fileno()))
        kernel32 = getattr(ctypes, "WinDLL")("kernel32", use_last_error=True)
        set_state = kernel32.SetNamedPipeHandleState
        set_state.argtypes = [
            ctypes.c_void_p,
            ctypes.POINTER(ctypes.c_uint32),
            ctypes.c_void_p,
            ctypes.c_void_p,
        ]
        set_state.restype = ctypes.c_int
        mode = ctypes.c_uint32(1)  # PIPE_NOWAIT with byte read mode.
        if not set_state(self._handle, ctypes.byref(mode), None, None):
            raise getattr(ctypes, "WinError")(getattr(ctypes, "get_last_error")())
        self._peek = kernel32.PeekNamedPipe
        self._get_last_error = getattr(ctypes, "get_last_error")
        self._win_error = getattr(ctypes, "WinError")
        self._peek.argtypes = [
            ctypes.c_void_p,
            ctypes.c_void_p,
            ctypes.c_uint32,
            ctypes.c_void_p,
            ctypes.POINTER(ctypes.c_uint32),
            ctypes.c_void_p,
        ]
        self._peek.restype = ctypes.c_int

    def read(self, size: int = -1) -> bytes:
        if size < 0:
            raise ValueError("Windows control-pipe reads must be bounded")
        if size == 0:
            return b""
        while True:
            available = self._ctypes.c_uint32()
            if self._peek(self._handle, None, 0, None, self._ctypes.byref(available), None):
                if available.value:
                    return self._stream.read(min(size, available.value))
            else:
                error = self._get_last_error()
                if error == 232:  # ERROR_NO_DATA: PIPE_NOWAIT has no bytes yet.
                    time.sleep(0.005)
                    continue
                if error in (109, 233):  # ERROR_BROKEN_PIPE / ERROR_PIPE_NOT_CONNECTED
                    return b""
                raise self._win_error(error)
            time.sleep(0.005)

    def close(self) -> None:
        self._stream.close()


class ControlProtocolError(RuntimeError):
    """Raised when the native host sends a malformed control frame."""


class ParentControlGoneError(RuntimeError):
    """Raised when the Desktop Host carrier closes during a child-group ACK."""


def encode_control_message(message: Mapping[str, Any]) -> bytes:
    payload = json.dumps(dict(message), ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    if not payload or len(payload) > MAX_CONTROL_MESSAGE_BYTES:
        raise ControlProtocolError("control message size is invalid")
    return _HEADER.pack(len(payload)) + payload


def _read_exact(stream: BinaryIO, size: int) -> bytes | None:
    chunks: list[bytes] = []
    remaining = size
    while remaining:
        chunk = stream.read(remaining)
        if not chunk:
            if remaining == size:
                return None
            raise ControlProtocolError("control frame ended early")
        chunks.append(chunk)
        remaining -= len(chunk)
    return b"".join(chunks)


class FramedControl:
    """Thread-safe writer and single-reader wrapper around a duplex byte stream."""

    def __init__(self, reader: BinaryIO, writer: BinaryIO | None = None) -> None:
        self._reader = reader
        self._writer = reader if writer is None else writer
        self._write_lock = threading.Lock()

    @classmethod
    def from_fd(cls, fd: int) -> FramedControl:
        if fd < 0:
            raise ValueError("control fd must be non-negative")
        return cls(os.fdopen(fd, "r+b", buffering=0, closefd=True))

    @classmethod
    def from_named_pipe(cls, path: str) -> FramedControl:
        if os.name != "nt":
            raise ValueError("named-pipe control is only available on Windows")
        if not path.startswith("\\\\.\\pipe\\iac-code-desktop-"):
            raise ValueError("control pipe name is invalid")
        writer = open(path, "r+b", buffering=0)
        try:
            reader_fd = os.dup(writer.fileno())
            try:
                reader = os.fdopen(reader_fd, "rb", buffering=0)
            except BaseException:
                os.close(reader_fd)
                raise
        except BaseException:
            writer.close()
            raise
        # A blocking read and a write on the same Windows FileIO object can
        # serialize behind the object's synchronous I/O state. Separate CRT
        # descriptors let the dispatcher receive Host commands while startup
        # and child-registration frames are written concurrently.
        return cls(cast(BinaryIO, _WindowsNamedPipeReader(reader)), writer)

    def read(self) -> dict[str, Any] | None:
        header = _read_exact(self._reader, _HEADER.size)
        if header is None:
            return None
        (size,) = _HEADER.unpack(header)
        if size == 0 or size > MAX_CONTROL_MESSAGE_BYTES:
            raise ControlProtocolError("control message size is invalid")
        raw = _read_exact(self._reader, size)
        if raw is None:
            raise ControlProtocolError("control message body is missing")
        try:
            message = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ControlProtocolError("control message is not valid UTF-8 JSON") from exc
        if not isinstance(message, dict) or not isinstance(message.get("type"), str):
            raise ControlProtocolError("control message must be an object with a string type")
        return message

    def write(self, message: Mapping[str, Any]) -> None:
        frame = encode_control_message(message)
        with self._write_lock:
            self._writer.write(frame)
            self._writer.flush()

    def close(self) -> None:
        try:
            self._reader.close()
        finally:
            if self._writer is not self._reader:
                self._writer.close()


class DesktopControlDispatcher:
    """Continuously demultiplex Host commands and child-group acknowledgements."""

    _ACK_TYPES = frozenset({"child-group-registered", "child-group-complete"})

    def __init__(self, control: FramedControl, loop: asyncio.AbstractEventLoop, sidecar_generation: int) -> None:
        if sidecar_generation <= 0:
            raise ValueError("sidecar generation must be positive")
        self.control = control
        self.loop = loop
        self.sidecar_generation = sidecar_generation
        self.queue: asyncio.Queue[dict[str, Any] | None] = asyncio.Queue()
        self._registration_lock = threading.Lock()
        self._next_registration_id = 1
        self._pending_lock = threading.Lock()
        self._pending: dict[tuple[str, int], queue.Queue[dict[str, Any] | None]] = {}
        self._guardian_writers: dict[int, BinaryIO] = {}
        self._parent_gone = threading.Event()
        self.thread = threading.Thread(target=self._read, name="iac-code-desktop-control", daemon=True)

    @property
    def parent_gone(self) -> bool:
        return self._parent_gone.is_set()

    def start(self) -> None:
        self.thread.start()

    def _read(self) -> None:
        try:
            while True:
                message = self.control.read()
                if message is None:
                    break
                message_type = message["type"]
                registration_id = message.get("registrationId")
                if message_type in self._ACK_TYPES and isinstance(registration_id, int):
                    with self._pending_lock:
                        waiter = self._pending.get((message_type, registration_id))
                    if waiter is not None:
                        try:
                            waiter.put_nowait(message)
                        except queue.Full:
                            pass
                        continue
                try:
                    self.loop.call_soon_threadsafe(self.queue.put_nowait, message)
                except RuntimeError:
                    return
        except (OSError, ValueError, ControlProtocolError):
            pass
        finally:
            self._mark_parent_gone()

    def _mark_parent_gone(self) -> None:
        if self._parent_gone.is_set():
            return
        self._parent_gone.set()
        with self._pending_lock:
            waiters = tuple(self._pending.values())
            guardian_writers = tuple(self._guardian_writers.values())
            self._guardian_writers.clear()
        for writer in guardian_writers:
            try:
                writer.close()
            except OSError:
                pass
        for waiter in waiters:
            try:
                waiter.put_nowait(None)
            except queue.Full:
                pass
        try:
            self.loop.call_soon_threadsafe(self.queue.put_nowait, None)
        except RuntimeError:
            pass

    def allocate_registration_id(self) -> int:
        with self._registration_lock:
            registration_id = self._next_registration_id
            self._next_registration_id += 1
        return registration_id

    def attach_guardian_writer(self, registration_id: int, writer: BinaryIO) -> None:
        with self._pending_lock:
            if self._parent_gone.is_set():
                writer.close()
                raise ParentControlGoneError("Desktop Host control carrier is closed")
            self._guardian_writers[registration_id] = writer

    def detach_guardian_writer(self, registration_id: int) -> None:
        with self._pending_lock:
            self._guardian_writers.pop(registration_id, None)

    def _request_ack(
        self,
        message: Mapping[str, Any],
        response_type: str,
        registration_id: int,
        timeout: float,
    ) -> dict[str, Any]:
        if self._parent_gone.is_set():
            raise ParentControlGoneError("Desktop Host control carrier is closed")
        waiter: queue.Queue[dict[str, Any] | None] = queue.Queue(maxsize=1)
        key = (response_type, registration_id)
        with self._pending_lock:
            if self._parent_gone.is_set():
                raise ParentControlGoneError("Desktop Host control carrier is closed")
            self._pending[key] = waiter
        try:
            self.control.write(message)
            try:
                response = waiter.get(timeout=timeout)
            except queue.Empty as exc:
                raise TimeoutError("Desktop Host child-group acknowledgement timed out") from exc
            if response is None:
                raise ParentControlGoneError("Desktop Host control carrier is closed")
            if response.get("sidecarGeneration") != self.sidecar_generation:
                raise ControlProtocolError("child-group acknowledgement has a stale generation")
            if response.get("registrationId") != registration_id:
                raise ControlProtocolError("child-group acknowledgement has a mismatched registration")
            if error := response.get("error"):
                raise ControlProtocolError(str(error))
            return response
        finally:
            with self._pending_lock:
                self._pending.pop(key, None)

    def register_child_group(
        self,
        pgid: int,
        kind: str,
        *,
        timeout: float = 10.0,
        registration_id: int | None = None,
    ) -> int:
        owns_registration_id = registration_id is None
        if registration_id is None:
            registration_id = self.allocate_registration_id()
        try:
            self._request_ack(
                {
                    "type": "register-child-group",
                    "sidecarGeneration": self.sidecar_generation,
                    "registrationId": registration_id,
                    "pgid": pgid,
                    "kind": kind,
                },
                "child-group-registered",
                registration_id,
                timeout,
            )
        except BaseException:
            # The Host may have committed registration even when its ACK was
            # lost. Standalone callers compensate here; guardian activation
            # preallocates the id and compensates only after process cleanup.
            if owns_registration_id:
                try:
                    self.complete_child_group(registration_id, pgid, timeout=min(timeout, 1.0))
                except BaseException:
                    pass
            raise
        return registration_id

    def complete_child_group(
        self,
        registration_id: int,
        pgid: int,
        *,
        timeout: float = 10.0,
    ) -> None:
        if self._parent_gone.is_set():
            return
        self._request_ack(
            {
                "type": "complete-child-group",
                "sidecarGeneration": self.sidecar_generation,
                "registrationId": registration_id,
                "pgid": pgid,
            },
            "child-group-complete",
            registration_id,
            timeout,
        )


_DISPATCHER_LOCK = threading.Lock()
_DISPATCHER: DesktopControlDispatcher | None = None


def install_control_dispatcher(dispatcher: DesktopControlDispatcher | None) -> None:
    global _DISPATCHER
    with _DISPATCHER_LOCK:
        _DISPATCHER = dispatcher


def get_control_dispatcher() -> DesktopControlDispatcher | None:
    with _DISPATCHER_LOCK:
        return _DISPATCHER
