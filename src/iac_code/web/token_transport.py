"""Encrypted transport for explicitly public, single-user Web deployments."""

from __future__ import annotations

import asyncio
import base64
import binascii
import ipaddress
import json
import os
import stat
import threading
import time
from collections import defaultdict, deque
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import unquote, urlsplit

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.ciphers.aead import ChaCha20Poly1305
from cryptography.hazmat.primitives.kdf.hkdf import HKDF

PROTOCOL_VERSION = "v1"
HKDF_INFO = b"iac-code-web-token-v1"
SESSION_TTL_SECONDS = 300
REPLAY_WINDOW_SIZE = 1024
MAX_SEQUENCE = (1 << 64) - 1
TOKEN_MIN_BYTES = 32
TOKEN_ENDPOINTS = {
    "/api/token/challenge",
    "/api/token/ping",
    "/api/token/request",
    "/api/token/stream",
}
DENIED_INTERNAL_PATHS = {"/api/cloud/aliyun/oauth-login"}
_PUBLIC_GET_PREFIXES = ("/static/",)
_PUBLIC_GET_PATHS = {"/", "/health"}


def _b64url_encode(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).decode("ascii").rstrip("=")


def _b64url_decode(value: str) -> bytes:
    alphabet = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_"
    if not value or any(char not in alphabet for char in value):
        raise ValueError("invalid base64url")
    padding = "=" * ((4 - len(value) % 4) % 4)
    try:
        return base64.b64decode(value + padding, altchars=b"-_", validate=True)
    except (binascii.Error, ValueError) as exc:
        raise ValueError("invalid base64url") from exc


def load_access_token(path: str | os.PathLike[str]) -> str:
    """Load and validate a base64url access token without exposing its value."""
    token_path = Path(path).expanduser()
    try:
        metadata = token_path.stat()
        raw = token_path.read_text(encoding="ascii")
    except (OSError, UnicodeError) as exc:
        raise ValueError("access token file cannot be read") from exc
    if not stat.S_ISREG(metadata.st_mode):
        raise ValueError("access token file must be a regular file")
    if os.name == "posix" and stat.S_IMODE(metadata.st_mode) != 0o600:
        raise ValueError("access token file permissions must be 0600")
    token = raw.strip()
    if raw != token and raw not in {token + "\n", token + "\r\n"}:
        raise ValueError("access token contains invalid whitespace")
    try:
        decoded = _b64url_decode(token)
    except ValueError as exc:
        raise ValueError("access token must be unpadded base64url") from exc
    if len(decoded) < TOKEN_MIN_BYTES or _b64url_encode(decoded) != token:
        raise ValueError("access token must contain at least 256 bits")
    return token


def _nonce(prefix: bytes, sequence: int) -> bytes:
    if len(prefix) != 4 or not 0 < sequence <= MAX_SEQUENCE:
        raise ValueError("invalid sequence")
    return prefix + sequence.to_bytes(8, "big")


def _aad(session_id: str, direction: str, message_type: str, sequence: int) -> bytes:
    return "\n".join((PROTOCOL_VERSION, session_id, direction, message_type, str(sequence))).encode("ascii")


@dataclass
class _ReplayWindow:
    maximum: int = 0
    seen: set[int] = field(default_factory=set)

    def accept(self, sequence: int) -> bool:
        if not 0 < sequence <= MAX_SEQUENCE:
            return False
        floor = max(0, self.maximum - REPLAY_WINDOW_SIZE + 1)
        if sequence < floor or sequence in self.seen:
            return False
        self.seen.add(sequence)
        if sequence > self.maximum:
            self.maximum = sequence
            floor = max(0, self.maximum - REPLAY_WINDOW_SIZE + 1)
            self.seen = {value for value in self.seen if value >= floor}
        return True


@dataclass
class _TokenSession:
    session_id: str
    request_key: bytes
    response_key: bytes
    request_nonce_prefix: bytes
    response_nonce_prefix: bytes
    expires_at: float
    request_sequences: _ReplayWindow = field(default_factory=_ReplayWindow)
    response_sequence: int = 0
    lock: threading.Lock = field(default_factory=threading.Lock)

    def decrypt(self, message_type: str, sequence: int, ciphertext: bytes) -> bytes:
        plaintext = ChaCha20Poly1305(self.request_key).decrypt(
            _nonce(self.request_nonce_prefix, sequence),
            ciphertext,
            _aad(self.session_id, "request", message_type, sequence),
        )
        with self.lock:
            if not self.request_sequences.accept(sequence):
                raise ValueError("replayed sequence")
        return plaintext

    def encrypt(self, message_type: str, plaintext: bytes) -> dict[str, Any]:
        with self.lock:
            self.response_sequence += 1
            sequence = self.response_sequence
        if sequence > MAX_SEQUENCE:
            raise ValueError("sequence exhausted")
        ciphertext = ChaCha20Poly1305(self.response_key).encrypt(
            _nonce(self.response_nonce_prefix, sequence),
            plaintext,
            _aad(self.session_id, "response", message_type, sequence),
        )
        return {
            "sessionId": self.session_id,
            "sequence": sequence,
            "type": message_type,
            "ciphertext": _b64url_encode(ciphertext),
        }


class _RateLimiter:
    def __init__(self, *, limit: int, interval_seconds: float) -> None:
        self.limit = limit
        self.interval_seconds = interval_seconds
        self._events: dict[str, deque[float]] = defaultdict(deque)
        self._lock = threading.Lock()

    def allow(self, key: str, now: float) -> bool:
        with self._lock:
            events = self._events[key]
            floor = now - self.interval_seconds
            while events and events[0] <= floor:
                events.popleft()
            if len(events) >= self.limit:
                return False
            events.append(now)
            return True


class TokenTransport:
    """Expose only static assets and encrypted request/stream tunnels."""

    def __init__(
        self,
        app: Any,
        access_token: str,
        *,
        clock: Any = time.time,
        random_bytes: Any = os.urandom,
    ) -> None:
        self.app = app
        self._token = access_token.encode("ascii")
        self._clock = clock
        self._random_bytes = random_bytes
        self._sessions: dict[str, _TokenSession] = {}
        self._sessions_lock = threading.Lock()
        self._challenge_limiter = _RateLimiter(limit=20, interval_seconds=60)
        self._failed_ping_limiter = _RateLimiter(limit=20, interval_seconds=60)

    async def __call__(self, scope: dict[str, Any], receive: Any, send: Any) -> None:
        if scope.get("type") == "lifespan":
            await self.app(scope, receive, send)
            return
        if scope.get("type") == "websocket":
            await send({"type": "websocket.close", "code": 1008, "reason": "Forbidden"})
            return
        if scope.get("type") != "http":
            await self._plain_response(send, 404, b"Not found")
            return
        if not self._valid_public_request(scope):
            await self._plain_response(send, 403, b"Forbidden")
            return

        path = str(scope.get("path") or "")
        method = str(scope.get("method") or "GET").upper()
        if method == "GET" and (path in _PUBLIC_GET_PATHS or path.startswith(_PUBLIC_GET_PREFIXES)):
            await self.app(scope, receive, send)
            return
        if path not in TOKEN_ENDPOINTS:
            await self._plain_response(send, 404, b"Not found")
            return
        if path == "/api/token/challenge" and method == "POST":
            await self._challenge(scope, receive, send)
            return
        if path == "/api/token/ping" and method == "POST":
            await self._ping(scope, receive, send)
            return
        if path == "/api/token/request" and method == "POST":
            await self._request(receive, send)
            return
        if path == "/api/token/stream" and method == "POST":
            await self._stream(receive, send)
            return
        await self._plain_response(send, 404, b"Not found")

    def _valid_public_request(self, scope: dict[str, Any]) -> bool:
        headers = _header_map(scope)
        hosts = headers.get("host", [])
        origins = headers.get("origin", [])
        fetch_sites = headers.get("sec-fetch-site", [])
        if len(hosts) != 1 or _ip_hostname(hosts[0]) is None:
            return False
        if len(origins) > 1 or (
            origins and not _origin_matches(origins[0], hosts[0], str(scope.get("scheme") or "http"))
        ):
            return False
        return len(fetch_sites) <= 1 and (
            not fetch_sites or fetch_sites[0].lower() in {"same-origin", "none"}
        )

    async def _challenge(self, scope: dict[str, Any], receive: Any, send: Any) -> None:
        await _read_body(receive)
        client = scope.get("client") or ("unknown", 0)
        client_ip = str(client[0])
        now = float(self._clock())
        if not self._challenge_limiter.allow(client_ip, now):
            await self._json_response(send, 429, {"error": "rate_limited"})
            return
        session_id = _b64url_encode(self._random_bytes(18))
        salt = self._random_bytes(32)
        request_prefix = self._random_bytes(4)
        response_prefix = self._random_bytes(4)
        key_material = HKDF(
            algorithm=hashes.SHA256(),
            length=64,
            salt=salt,
            info=HKDF_INFO,
        ).derive(self._token)
        expires_at = now + SESSION_TTL_SECONDS
        session = _TokenSession(
            session_id=session_id,
            request_key=key_material[:32],
            response_key=key_material[32:],
            request_nonce_prefix=request_prefix,
            response_nonce_prefix=response_prefix,
            expires_at=expires_at,
        )
        with self._sessions_lock:
            self._remove_expired_sessions(now)
            self._sessions[session_id] = session
        await self._json_response(
            send,
            200,
            {
                "version": PROTOCOL_VERSION,
                "sessionId": session_id,
                "salt": _b64url_encode(salt),
                "requestNoncePrefix": _b64url_encode(request_prefix),
                "responseNoncePrefix": _b64url_encode(response_prefix),
                "expiresAt": int(expires_at),
            },
        )

    async def _ping(self, scope: dict[str, Any], receive: Any, send: Any) -> None:
        body = await self._read_json(receive)
        try:
            session, sequence, ciphertext = self._encrypted_input(body, "ping")
            plaintext = session.decrypt("ping", sequence, ciphertext)
            if plaintext != b"ping":
                raise ValueError("invalid ping")
        except (InvalidTag, TypeError, ValueError):
            client = scope.get("client") or ("unknown", 0)
            if not self._failed_ping_limiter.allow(str(client[0]), float(self._clock())):
                await self._json_response(send, 429, {"error": "rate_limited"})
                return
            await self._json_response(send, 401, {"error": "authentication_failed"})
            return
        await self._json_response(send, 200, session.encrypt("pong", b"pong"))

    async def _request(self, receive: Any, send: Any) -> None:
        body = await self._read_json(receive)
        try:
            session, sequence, ciphertext = self._encrypted_input(body, "request")
            request_data = _decode_request(session.decrypt("request", sequence, ciphertext))
            inner_scope, inner_body = _inner_scope(request_data)
        except (InvalidTag, TypeError, ValueError):
            await self._json_response(send, 401, {"error": "invalid_envelope"})
            return

        status = 500
        headers: list[tuple[bytes, bytes]] = []
        chunks: list[bytes] = []
        request_delivered = False

        async def inner_receive() -> dict[str, Any]:
            nonlocal inner_body, request_delivered
            if request_delivered:
                return {"type": "http.disconnect"}
            request_delivered = True
            value, inner_body = inner_body, b""
            return {"type": "http.request", "body": value, "more_body": False}

        async def inner_send(message: dict[str, Any]) -> None:
            nonlocal status, headers
            if message["type"] == "http.response.start":
                status = int(message["status"])
                headers = list(message.get("headers", []))
            elif message["type"] == "http.response.body":
                chunks.append(bytes(message.get("body", b"")))

        await self.app(inner_scope, inner_receive, inner_send)
        response_plaintext = json.dumps(
            {
                "status": status,
                "headers": _serialize_headers(headers),
                "body": _b64url_encode(b"".join(chunks)),
            },
            separators=(",", ":"),
        ).encode("utf-8")
        await self._json_response(send, 200, session.encrypt("response", response_plaintext))

    async def _stream(self, receive: Any, send: Any) -> None:
        body = await self._read_json(receive)
        try:
            session, sequence, ciphertext = self._encrypted_input(body, "stream")
            request_data = _decode_request(session.decrypt("stream", sequence, ciphertext))
            inner_scope, inner_body = _inner_scope(request_data)
        except (InvalidTag, TypeError, ValueError):
            await self._json_response(send, 401, {"error": "invalid_envelope"})
            return

        started = False
        finished = False
        disconnected = asyncio.Event()

        async def watch_disconnect() -> None:
            while True:
                message = await receive()
                if message.get("type") == "http.disconnect":
                    disconnected.set()
                    return

        watcher = asyncio.create_task(watch_disconnect())

        async def inner_receive() -> dict[str, Any]:
            nonlocal inner_body
            if inner_body is not None:
                value, inner_body = inner_body, None
                return {"type": "http.request", "body": value, "more_body": False}
            await disconnected.wait()
            return {"type": "http.disconnect"}

        async def send_frame(message_type: str, payload: dict[str, Any]) -> None:
            frame = session.encrypt(
                message_type,
                json.dumps(payload, separators=(",", ":")).encode("utf-8"),
            )
            await send(
                {
                    "type": "http.response.body",
                    "body": json.dumps(frame, separators=(",", ":")).encode("utf-8") + b"\n",
                    "more_body": True,
                }
            )

        async def inner_send(message: dict[str, Any]) -> None:
            nonlocal started, finished
            if message["type"] == "http.response.start":
                if not started:
                    await send(
                        {
                            "type": "http.response.start",
                            "status": 200,
                            "headers": [
                                (b"content-type", b"application/x-ndjson"),
                                (b"cache-control", b"no-store"),
                            ],
                        }
                    )
                    started = True
                await send_frame(
                    "stream-start",
                    {
                        "status": int(message["status"]),
                        "headers": _serialize_headers(list(message.get("headers", []))),
                    },
                )
            elif message["type"] == "http.response.body":
                chunk = bytes(message.get("body", b""))
                if chunk:
                    await send_frame("stream-body", {"body": _b64url_encode(chunk)})
                if not message.get("more_body", False):
                    await send_frame("stream-end", {})
                    await send({"type": "http.response.body", "body": b"", "more_body": False})
                    finished = True

        try:
            await self.app(inner_scope, inner_receive, inner_send)
            if started and not finished:
                await send_frame("stream-end", {})
                await send({"type": "http.response.body", "body": b"", "more_body": False})
        finally:
            watcher.cancel()
            try:
                await watcher
            except asyncio.CancelledError:
                pass

    def _encrypted_input(self, body: dict[str, Any], message_type: str) -> tuple[_TokenSession, int, bytes]:
        if body.get("type") != message_type:
            raise ValueError("invalid type")
        session_id = body.get("sessionId")
        sequence = body.get("sequence")
        ciphertext_value = body.get("ciphertext")
        if not isinstance(session_id, str) or not isinstance(sequence, int) or not isinstance(ciphertext_value, str):
            raise ValueError("invalid envelope")
        now = float(self._clock())
        with self._sessions_lock:
            session = self._sessions.get(session_id)
            if session is None or session.expires_at <= now:
                self._sessions.pop(session_id, None)
                raise ValueError("expired session")
        return session, sequence, _b64url_decode(ciphertext_value)

    def _remove_expired_sessions(self, now: float) -> None:
        for session_id in [key for key, value in self._sessions.items() if value.expires_at <= now]:
            self._sessions.pop(session_id, None)

    async def _read_json(self, receive: Any) -> dict[str, Any]:
        try:
            value = json.loads((await _read_body(receive)).decode("utf-8"))
        except (UnicodeError, json.JSONDecodeError):
            return {}
        return value if isinstance(value, dict) else {}

    async def _json_response(self, send: Any, status: int, payload: dict[str, Any]) -> None:
        body = json.dumps(payload, separators=(",", ":")).encode("utf-8")
        await send(
            {
                "type": "http.response.start",
                "status": status,
                "headers": [
                    (b"content-type", b"application/json"),
                    (b"content-length", str(len(body)).encode("ascii")),
                    (b"cache-control", b"no-store"),
                ],
            }
        )
        await send({"type": "http.response.body", "body": body})

    async def _plain_response(self, send: Any, status: int, body: bytes) -> None:
        await send(
            {
                "type": "http.response.start",
                "status": status,
                "headers": [
                    (b"content-type", b"text/plain; charset=utf-8"),
                    (b"content-length", str(len(body)).encode("ascii")),
                    (b"cache-control", b"no-store"),
                ],
            }
        )
        await send({"type": "http.response.body", "body": body})


def _header_map(scope: dict[str, Any]) -> dict[str, list[str]]:
    result: dict[str, list[str]] = defaultdict(list)
    for raw_name, raw_value in scope.get("headers", []):
        result[raw_name.decode("latin-1").lower()].append(raw_value.decode("latin-1").strip())
    return result


def _ip_hostname(authority: str) -> str | None:
    if not authority or "@" in authority:
        return None
    try:
        parsed = urlsplit("//{}".format(authority))
        _ = parsed.port
        hostname = parsed.hostname
        if hostname is None:
            return None
        return str(ipaddress.ip_address(hostname))
    except ValueError:
        return None


def _origin_matches(origin: str, authority: str, scope_scheme: str) -> bool:
    try:
        parsed_origin = urlsplit(origin)
        parsed_host = urlsplit("//{}".format(authority))
        origin_host = ipaddress.ip_address(parsed_origin.hostname or "")
        request_host = ipaddress.ip_address(parsed_host.hostname or "")
        origin_port = parsed_origin.port
        request_port = parsed_host.port
    except ValueError:
        return False
    expected_scheme = {"ws": "http", "wss": "https"}.get(scope_scheme, scope_scheme)
    if (
        parsed_origin.scheme != expected_scheme
        or parsed_origin.username is not None
        or parsed_origin.password is not None
    ):
        return False
    default_port = 443 if expected_scheme == "https" else 80
    return origin_host == request_host and (origin_port or default_port) == (request_port or default_port)


async def _read_body(receive: Any) -> bytes:
    chunks: list[bytes] = []
    while True:
        message = await receive()
        if message.get("type") == "http.disconnect":
            break
        chunks.append(bytes(message.get("body", b"")))
        if not message.get("more_body", False):
            break
    return b"".join(chunks)


def _decode_request(plaintext: bytes) -> dict[str, Any]:
    try:
        value = json.loads(plaintext.decode("utf-8"))
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise ValueError("invalid request") from exc
    if not isinstance(value, dict):
        raise ValueError("invalid request")
    return value


def _inner_scope(request_data: dict[str, Any]) -> tuple[dict[str, Any], bytes]:
    method = request_data.get("method")
    target = request_data.get("path")
    raw_headers = request_data.get("headers", {})
    raw_body = request_data.get("body", "")
    if not isinstance(method, str) or method.upper() not in {"GET", "POST", "PUT", "PATCH", "DELETE"}:
        raise ValueError("invalid method")
    if not isinstance(target, str) or not isinstance(raw_headers, dict) or not isinstance(raw_body, str):
        raise ValueError("invalid request")
    parsed = urlsplit(target)
    try:
        path = unquote(parsed.path, encoding="utf-8", errors="strict")
        raw_path = parsed.path.encode("ascii")
    except (UnicodeDecodeError, UnicodeEncodeError) as exc:
        raise ValueError("invalid path") from exc
    if parsed.scheme or parsed.netloc or parsed.fragment or not path.startswith("/api/"):
        raise ValueError("invalid path")
    if path in TOKEN_ENDPOINTS or path in DENIED_INTERNAL_PATHS:
        raise ValueError("denied path")
    headers: list[tuple[bytes, bytes]] = []
    for name, value in raw_headers.items():
        normalized = str(name).strip().lower()
        if normalized not in {"accept", "content-type", "last-event-id"} or not isinstance(value, str):
            continue
        headers.append((normalized.encode("ascii"), value.encode("latin-1")))
    headers.append((b"host", b"127.0.0.1"))
    body = _b64url_decode(raw_body) if raw_body else b""
    scope = {
        "type": "http",
        "asgi": {"version": "3.0", "spec_version": "2.3"},
        "http_version": "1.1",
        "method": method.upper(),
        "scheme": "http",
        "path": path,
        "raw_path": raw_path,
        "query_string": parsed.query.encode("ascii"),
        "root_path": "",
        "headers": headers,
        "client": ("127.0.0.1", 0),
        "server": ("127.0.0.1", 8766),
    }
    return scope, body


def _serialize_headers(headers: list[tuple[bytes, bytes]]) -> list[list[str]]:
    return [[name.decode("latin-1"), value.decode("latin-1")] for name, value in headers]
