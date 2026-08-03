from __future__ import annotations

import base64
import json
import os
from pathlib import Path

import pytest
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.ciphers.aead import ChaCha20Poly1305
from cryptography.hazmat.primitives.kdf.hkdf import HKDF
from starlette.applications import Starlette
from starlette.responses import JSONResponse, PlainTextResponse, StreamingResponse
from starlette.routing import Route
from starlette.testclient import TestClient

from iac_code.web.token_transport import HKDF_INFO, TokenTransport, load_access_token


def _encode(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).decode("ascii").rstrip("=")


def _decode(value: str) -> bytes:
    return base64.urlsafe_b64decode(value + "=" * ((4 - len(value) % 4) % 4))


class BrowserProtocol:
    def __init__(self, client: TestClient, token: str) -> None:
        self.client = client
        challenge_response = client.post("/api/token/challenge", json={})
        assert challenge_response.status_code == 200
        challenge = challenge_response.json()
        self.session_id = challenge["sessionId"]
        self.request_prefix = _decode(challenge["requestNoncePrefix"])
        self.response_prefix = _decode(challenge["responseNoncePrefix"])
        keys = HKDF(algorithm=hashes.SHA256(), length=64, salt=_decode(challenge["salt"]), info=HKDF_INFO).derive(
            token.encode("ascii")
        )
        self.request_key = keys[:32]
        self.response_key = keys[32:]

    def encrypt(self, message_type: str, sequence: int, plaintext: bytes) -> dict[str, object]:
        nonce = self.request_prefix + sequence.to_bytes(8, "big")
        aad = "\n".join(("v1", self.session_id, "request", message_type, str(sequence))).encode("ascii")
        return {
            "sessionId": self.session_id,
            "sequence": sequence,
            "type": message_type,
            "ciphertext": _encode(ChaCha20Poly1305(self.request_key).encrypt(nonce, plaintext, aad)),
        }

    def decrypt(self, envelope: dict[str, object], message_type: str) -> bytes:
        sequence = envelope["sequence"]
        assert isinstance(sequence, int)
        nonce = self.response_prefix + sequence.to_bytes(8, "big")
        aad = "\n".join(("v1", self.session_id, "response", message_type, str(sequence))).encode("ascii")
        return ChaCha20Poly1305(self.response_key).decrypt(
            nonce,
            _decode(str(envelope["ciphertext"])),
            aad,
        )

    def request_envelope(self, sequence: int, path: str, *, stream: bool = False) -> dict[str, object]:
        plaintext = json.dumps(
            {
                "method": "GET",
                "path": path,
                "headers": {"accept": "application/json"},
                "body": "",
            },
            separators=(",", ":"),
        ).encode()
        return self.encrypt("stream" if stream else "request", sequence, plaintext)


@pytest.fixture
def token() -> str:
    return _encode(bytes(range(32)))


@pytest.fixture
def client(token: str):
    async def home(_request):
        return PlainTextResponse("home")

    async def health(_request):
        return JSONResponse({"status": "ok"})

    async def echo(request):
        return JSONResponse({"path": request.url.path, "query": request.url.query})

    async def stream(_request):
        async def chunks():
            yield b'data: {"value":1}\n\n'
            yield b'data: {"value":2}\n\n'

        return StreamingResponse(chunks(), media_type="text/event-stream")

    async def static_asset(request):
        return PlainTextResponse(request.path_params["path"])

    inner = Starlette(
        routes=[
            Route("/", home),
            Route("/health", health),
            Route("/api/echo", echo),
            Route("/api/items/{name}", echo),
            Route("/api/events", stream),
            Route("/api/cloud/aliyun/oauth-login", echo, methods=["POST"]),
            Route("/static/{path:path}", static_asset),
        ]
    )
    with TestClient(TokenTransport(inner, token), base_url="http://203.0.113.10:8766") as test_client:
        yield test_client


def test_token_file_validation_and_permissions(tmp_path: Path, token: str) -> None:
    token_file = tmp_path / "token"
    token_file.write_text(token + "\n", encoding="ascii")
    token_file.chmod(0o600)
    assert load_access_token(token_file) == token

    token_file.write_text("short", encoding="ascii")
    with pytest.raises(ValueError, match="base64url|at least 256 bits"):
        load_access_token(token_file)

    if os.name == "posix":
        token_file.write_text(token, encoding="ascii")
        token_file.chmod(0o644)
        with pytest.raises(ValueError, match="permissions"):
            load_access_token(token_file)


def test_public_route_and_same_origin_boundary(client: TestClient) -> None:
    assert client.get("/").text == "home"
    assert client.get("/health").json() == {"status": "ok"}
    assert client.get("/api/echo").status_code == 404
    assert client.get("/missing").status_code == 404
    assert client.get("/", headers={"host": "attacker.example"}).status_code == 403
    assert client.get("/", headers={"origin": "http://198.51.100.7:8766"}).status_code == 403
    assert client.get("/", headers={"sec-fetch-site": "cross-site"}).status_code == 403


@pytest.mark.parametrize("path", ["/static/js/app.js", "/static/icons/sidebar-new-thread.svg"])
def test_same_origin_module_and_svg_are_public_in_token_mode(client: TestClient, path: str) -> None:
    response = client.get(
        path,
        headers={
            "origin": "http://203.0.113.10:8766",
            "sec-fetch-site": "same-origin",
        },
    )

    assert response.status_code == 200


def test_ping_request_replay_and_internal_denials(client: TestClient, token: str) -> None:
    browser = BrowserProtocol(client, token)
    ping = client.post("/api/token/ping", json=browser.encrypt("ping", 1, b"ping"))
    assert ping.status_code == 200
    assert browser.decrypt(ping.json(), "pong") == b"pong"

    request = browser.request_envelope(3, "/api/echo?mode=token")
    response = client.post("/api/token/request", json=request)
    assert response.status_code == 200
    payload = json.loads(browser.decrypt(response.json(), "response"))
    assert payload["status"] == 200
    assert json.loads(_decode(payload["body"])) == {"path": "/api/echo", "query": "mode=token"}

    encoded_path_response = client.post(
        "/api/token/request",
        json=browser.request_envelope(4, "/api/items/hello%20world"),
    )
    assert encoded_path_response.status_code == 200
    encoded_payload = json.loads(browser.decrypt(encoded_path_response.json(), "response"))
    assert json.loads(_decode(encoded_payload["body"])) == {"path": "/api/items/hello world", "query": ""}

    out_of_order = client.post("/api/token/request", json=browser.request_envelope(2, "/api/echo"))
    assert out_of_order.status_code == 200
    assert client.post("/api/token/request", json=request).status_code == 401
    assert client.post(
        "/api/token/request",
        json=browser.request_envelope(5, "/api/token/challenge"),
    ).status_code == 401
    assert client.post(
        "/api/token/request",
        json=browser.request_envelope(6, "/api/cloud/aliyun/oauth-login"),
    ).status_code == 401


def test_wrong_token_cannot_validate_ping(client: TestClient) -> None:
    browser = BrowserProtocol(client, _encode(bytes(reversed(range(32)))))
    response = client.post("/api/token/ping", json=browser.encrypt("ping", 1, b"ping"))
    assert response.status_code == 401
    assert "token" not in response.text.lower()


def test_stream_metadata_body_and_end_are_encrypted(client: TestClient, token: str) -> None:
    browser = BrowserProtocol(client, token)
    with client.stream(
        "POST",
        "/api/token/stream",
        json=browser.request_envelope(1, "/api/events", stream=True),
    ) as response:
        assert response.status_code == 200
        envelopes = [json.loads(line) for line in response.iter_lines() if line]

    assert [envelope["type"] for envelope in envelopes] == [
        "stream-start",
        "stream-body",
        "stream-body",
        "stream-end",
    ]
    start = json.loads(browser.decrypt(envelopes[0], "stream-start"))
    assert start["status"] == 200
    body = b"".join(
        _decode(json.loads(browser.decrypt(envelope, "stream-body"))["body"])
        for envelope in envelopes[1:-1]
    )
    assert body == b'data: {"value":1}\n\ndata: {"value":2}\n\n'
    assert json.loads(browser.decrypt(envelopes[-1], "stream-end")) == {}


def test_expired_session_requires_a_new_challenge(token: str) -> None:
    now = [1000.0]

    async def home(_request):
        return PlainTextResponse("home")

    app = TokenTransport(Starlette(routes=[Route("/", home)]), token, clock=lambda: now[0])
    with TestClient(app, base_url="http://203.0.113.10:8766") as client:
        browser = BrowserProtocol(client, token)
        now[0] += 301
        response = client.post("/api/token/ping", json=browser.encrypt("ping", 1, b"ping"))
    assert response.status_code == 401
