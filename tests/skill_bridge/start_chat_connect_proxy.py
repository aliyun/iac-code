"""Restricted CONNECT proxy for routing the real aliyun CLI to the local relay."""

from __future__ import annotations

import argparse
import json
import select
import signal
import socket
import socketserver
import threading
import time
from typing import Any

_MAX_HEADER_BYTES = 64 * 1024


class _ProxyServer(socketserver.ThreadingTCPServer):
    allow_reuse_address = True
    daemon_threads = True

    def __init__(
        self,
        server_address: tuple[str, int],
        *,
        allowed_authority: str,
        target_host: str,
        target_port: int,
    ) -> None:
        super().__init__(server_address, _ProxyHandler)
        self.allowed_authority = allowed_authority
        self.target_host = target_host
        self.target_port = target_port
        self.metrics_lock = threading.Lock()
        self.metrics: dict[str, Any] = {
            "connections": 0,
            "rejected": 0,
            "clientToRelayBytes": 0,
            "relayToClientBytes": 0,
        }

    def add_metric(self, key: str, value: int = 1) -> None:
        with self.metrics_lock:
            self.metrics[key] += value


class _ProxyHandler(socketserver.BaseRequestHandler):
    server: _ProxyServer

    def handle(self) -> None:
        header = self._read_header()
        if header is None:
            self.server.add_metric("rejected")
            return
        first_line = header.split(b"\r\n", 1)[0].decode("ascii", "replace")
        pieces = first_line.split()
        if len(pieces) != 3 or pieces[0] != "CONNECT" or pieces[1] != self.server.allowed_authority:
            self.request.sendall(b"HTTP/1.1 403 Forbidden\r\nContent-Length: 0\r\n\r\n")
            self.server.add_metric("rejected")
            return
        try:
            upstream = socket.create_connection((self.server.target_host, self.server.target_port), timeout=10)
        except OSError:
            self.request.sendall(b"HTTP/1.1 502 Bad Gateway\r\nContent-Length: 0\r\n\r\n")
            return
        self.server.add_metric("connections")
        self.request.sendall(b"HTTP/1.1 200 Connection Established\r\n\r\n")
        try:
            self._tunnel(upstream)
        finally:
            upstream.close()

    def _read_header(self) -> bytes | None:
        data = bytearray()
        while b"\r\n\r\n" not in data and len(data) < _MAX_HEADER_BYTES:
            chunk = self.request.recv(4096)
            if not chunk:
                return None
            data.extend(chunk)
        return bytes(data) if b"\r\n\r\n" in data else None

    def _tunnel(self, upstream: socket.socket) -> None:
        sockets = (self.request, upstream)
        while True:
            readable, _, _ = select.select(sockets, (), (), 30)
            if not readable:
                continue
            for source in readable:
                data = source.recv(64 * 1024)
                if not data:
                    return
                if source is self.request:
                    upstream.sendall(data)
                    self.server.add_metric("clientToRelayBytes", len(data))
                else:
                    self.request.sendall(data)
                    self.server.add_metric("relayToClientBytes", len(data))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Route one HTTPS authority to a local StartChat relay.")
    parser.add_argument("--allowed-authority", default="ros.aliyuncs.com:443")
    parser.add_argument("--target-host", default="127.0.0.1")
    parser.add_argument("--target-port", type=int, required=True)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=0)
    parser.add_argument("--metrics-file")
    args = parser.parse_args(argv)
    server = _ProxyServer(
        (args.host, args.port),
        allowed_authority=args.allowed_authority,
        target_host=args.target_host,
        target_port=args.target_port,
    )

    def stop(_signum: int, _frame: object) -> None:
        threading.Thread(target=server.shutdown, daemon=True).start()

    signal.signal(signal.SIGINT, stop)
    signal.signal(signal.SIGTERM, stop)
    print(
        json.dumps(
            {"host": args.host, "port": server.server_address[1], "protocol": "http-connect"},
            separators=(",", ":"),
        ),
        flush=True,
    )
    try:
        server.serve_forever()
    finally:
        if args.metrics_file:
            with server.metrics_lock:
                payload = {"schemaVersion": 1, "finishedAtUnixMs": int(time.time() * 1000), **server.metrics}
            with open(args.metrics_file, "w", encoding="utf-8") as stream:
                json.dump(payload, stream, ensure_ascii=False, indent=2, sort_keys=True)
                stream.write("\n")
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
