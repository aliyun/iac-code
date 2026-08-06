from __future__ import annotations

import socket

import pytest

from iac_code.desktop.ports import DesktopPortError, bind_loopback_listener


def test_loopback_listener_uses_an_os_port_and_is_not_inheritable() -> None:
    bound = bind_loopback_listener(0)
    try:
        assert bound.requested_port == 0
        assert bound.port > 0
        assert bound.socket.getsockname()[0] == "127.0.0.1"
        assert bound.socket.get_inheritable() is False
    finally:
        bound.socket.close()


def test_loopback_listener_reports_a_real_conflict() -> None:
    occupied = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    occupied.bind(("127.0.0.1", 0))
    occupied.listen()
    try:
        with pytest.raises(DesktopPortError) as raised:
            bind_loopback_listener(occupied.getsockname()[1])
        assert raised.value.code == "port_in_use"
    finally:
        occupied.close()
