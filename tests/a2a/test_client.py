import base64

import pytest
from a2a.utils.signing import ProtectedHeader, create_agent_card_signer
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa

from iac_code.a2a.client import A2ACardVerificationError, A2AClient, A2AClientResponse, _BoundHttpA2AClient
from iac_code.a2a.signing import (
    ASYMMETRIC_SIGNATURE_ALGORITHM,
    _agent_card_from_dict,
    _agent_card_to_dict,
    sign_agent_card_dict,
)
from iac_code.a2a.transport import A2AAuthConfig


class FakeHTTPResponse:
    def __init__(self, payload: dict[str, object], status_code: int = 200) -> None:
        self._payload = payload
        self.status_code = status_code

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")

    def json(self) -> dict[str, object]:
        return self._payload


class FakeHTTPClient:
    def __init__(self) -> None:
        self.requests: list[tuple[str, str, dict[str, object] | None, dict[str, str] | None]] = []
        self.closed = False

    async def get(self, url: str, headers: dict[str, str] | None = None) -> FakeHTTPResponse:
        self.requests.append(("GET", url, None, headers))
        return FakeHTTPResponse({"name": "remote", "url": "http://remote/", "preferredTransport": "JSONRPC"})

    async def post(self, url: str, json: dict[str, object], headers: dict[str, str] | None = None) -> FakeHTTPResponse:
        self.requests.append(("POST", url, json, headers))
        return FakeHTTPResponse({"result": {"status": {"state": "input-required"}, "text": "done"}})

    def stream(self, method: str, url: str, json: dict[str, object], headers: dict[str, str] | None = None):
        self.requests.append((method, url, json, headers))

        class StreamResponse:
            def raise_for_status(self) -> None:
                return None

            async def aiter_lines(self):
                yield ""
                yield 'data: {"result": {"status": {"state": "working"}}}'
                yield "event: ignored"
                yield 'data: {"result": {"status": {"state": "input-required"}}}'

            async def __aenter__(self):
                return self

            async def __aexit__(self, exc_type, exc, tb):
                return None

        return StreamResponse()

    async def aclose(self) -> None:
        self.closed = True


def _base64url_uint(value: int) -> str:
    raw = value.to_bytes((value.bit_length() + 7) // 8, "big")
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def _rsa_private_key():
    return rsa.generate_private_key(public_exponent=65537, key_size=2048)


def _rsa_public_jwk(private_key, kid: str) -> dict[str, str]:
    numbers = private_key.public_key().public_numbers()
    return {
        "kty": "RSA",
        "kid": kid,
        "alg": ASYMMETRIC_SIGNATURE_ALGORITHM,
        "use": "sig",
        "n": _base64url_uint(numbers.n),
        "e": _base64url_uint(numbers.e),
    }


def _sign_with_rsa(card: dict[str, object], private_key, *, kid: str, jku: str) -> dict[str, object]:
    private_pem = private_key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    )
    protected_header: ProtectedHeader = {
        "alg": ASYMMETRIC_SIGNATURE_ALGORITHM,
        "typ": "JOSE",
        "kid": kid,
        "jku": jku,
    }
    signer = create_agent_card_signer(signing_key=private_pem, protected_header=protected_header)
    return _agent_card_to_dict(signer(_agent_card_from_dict(card)))


@pytest.mark.asyncio
async def test_discover_fetches_agent_card_with_auth_headers() -> None:
    http = FakeHTTPClient()
    client = A2AClient(http_client=http, auth=A2AAuthConfig(bearer_token="secret"))

    card = await client.discover("http://remote")

    assert card["name"] == "remote"
    assert http.requests[0] == (
        "GET",
        "http://remote/.well-known/agent-card.json",
        None,
        {"Authorization": "Bearer secret"},
    )


@pytest.mark.asyncio
async def test_send_message_posts_a2a_1_jsonrpc_request() -> None:
    http = FakeHTTPClient()
    client = A2AClient(http_client=http)

    response = await client.send_message("http://remote/", "hello", cwd="/tmp/work")

    assert isinstance(response, A2AClientResponse)
    assert response.text == "done"
    method, url, payload, headers = http.requests[-1]
    assert method == "POST"
    assert url == "http://remote/"
    assert payload is not None
    assert payload["method"] == "SendMessage"
    assert payload["params"]["message"]["parts"][0]["text"] == "hello"
    assert payload["params"]["message"]["metadata"]["iac_code"]["cwd"] == "/tmp/work"
    assert headers == {"A2A-Version": "1.0"}


@pytest.mark.asyncio
async def test_message_payload_includes_metadata_iac_code_model() -> None:
    http = FakeHTTPClient()
    client = A2AClient(http_client=http)

    await client.send_message("http://remote/", "hello", cwd="/tmp/work", model="metadata-model")
    events = [
        event async for event in client.stream_message("http://remote/", "hello", cwd="/tmp/work", model="stream-model")
    ]

    assert events
    send_payload = http.requests[-2][2]
    stream_payload = http.requests[-1][2]
    assert send_payload["params"]["message"]["metadata"]["iac_code"] == {
        "cwd": "/tmp/work",
        "iac_code_model": "metadata-model",
    }
    assert stream_payload["params"]["message"]["metadata"]["iac_code"] == {
        "cwd": "/tmp/work",
        "iac_code_model": "stream-model",
    }


def test_response_text_extracts_from_task_history_agent_message() -> None:
    response = A2AClientResponse(
        payload={
            "result": {
                "task": {
                    "history": [
                        {"role": "ROLE_USER", "parts": [{"text": "hello"}]},
                        {
                            "role": "ROLE_AGENT",
                            "parts": [{"text": "first "}, {"text": "answer"}],
                        },
                        {"role": "ROLE_USER", "parts": [{"text": "follow up"}]},
                        {"role": "ROLE_AGENT", "parts": [{"text": "final answer"}]},
                    ]
                }
            }
        }
    )

    assert response.text == "final answer"


def test_response_text_extracts_from_task_status_message_parts() -> None:
    response = A2AClientResponse(
        payload={
            "result": {
                "task": {
                    "status": {
                        "message": {
                            "role": "ROLE_AGENT",
                            "parts": [{"text": "status "}, {"text": "text"}],
                        }
                    },
                    "history": [{"role": "ROLE_USER", "parts": [{"text": "hi"}]}],
                }
            }
        }
    )

    assert response.text == "status text"


def test_response_text_returns_empty_for_malformed_payload() -> None:
    assert A2AClientResponse(payload={}).text == ""
    assert A2AClientResponse(payload={"result": "nope"}).text == ""
    assert (
        A2AClientResponse(payload={"result": {"task": {"history": [{"role": "ROLE_AGENT", "parts": "broken"}]}}}).text
        == ""
    )


@pytest.mark.asyncio
async def test_stream_message_posts_stream_request_and_yields_events() -> None:
    http = FakeHTTPClient()
    client = A2AClient(http_client=http)

    events = [event async for event in client.stream_message("http://remote/", "hello", cwd="/tmp/work")]

    assert events[0]["result"]["status"]["state"] == "working"
    assert events[1]["result"]["status"]["state"] == "input-required"
    assert http.requests[-1][2]["method"] == "SendStreamingMessage"
    assert http.requests[-1][3] == {"A2A-Version": "1.0"}


@pytest.mark.asyncio
async def test_task_management_methods_post_a2a_requests() -> None:
    http = FakeHTTPClient()
    client = A2AClient(http_client=http)

    await client.get_task("http://remote/", "task-1", history_length=2)
    await client.list_tasks("http://remote/", context_id="ctx-1", status="TASK_STATE_WORKING", page_size=10)
    await client.cancel_task("http://remote/", "task-1")

    assert http.requests[-3][2]["method"] == "GetTask"
    assert http.requests[-3][2]["params"] == {"id": "task-1", "historyLength": 2}
    assert http.requests[-2][2]["method"] == "ListTasks"
    assert http.requests[-2][2]["params"]["contextId"] == "ctx-1"
    assert http.requests[-2][2]["params"]["status"] == "TASK_STATE_WORKING"
    assert http.requests[-1][2]["method"] == "CancelTask"
    assert http.requests[-1][2]["params"] == {"id": "task-1"}


@pytest.mark.asyncio
async def test_push_config_and_extended_card_methods_post_a2a_requests() -> None:
    http = FakeHTTPClient()
    client = A2AClient(http_client=http)

    await client.create_push_notification_config(
        "http://remote/",
        task_id="task-1",
        config_id="cfg-1",
        url="https://callback.example/a2a",
        token="token-1",
        authentication={"scheme": "bearer", "credentials": "secret"},
    )
    await client.get_push_notification_config("http://remote/", task_id="task-1", config_id="cfg-1")
    await client.list_push_notification_configs("http://remote/", task_id="task-1", page_size=1)
    await client.delete_push_notification_config("http://remote/", task_id="task-1", config_id="cfg-1")
    await client.get_extended_agent_card("http://remote/")

    methods = [request[2]["method"] for request in http.requests[-5:]]
    assert methods == [
        "CreateTaskPushNotificationConfig",
        "GetTaskPushNotificationConfig",
        "ListTaskPushNotificationConfigs",
        "DeleteTaskPushNotificationConfig",
        "GetExtendedAgentCard",
    ]
    assert http.requests[-5][2]["params"]["authentication"]["scheme"] == "bearer"


@pytest.mark.asyncio
async def test_subscribe_task_posts_stream_request() -> None:
    http = FakeHTTPClient()
    client = A2AClient(http_client=http)

    events = [event async for event in client.subscribe_task("http://remote/", "task-1")]

    assert events[0]["result"]["status"]["state"] == "working"
    assert http.requests[-1][2]["method"] == "SubscribeToTask"
    assert http.requests[-1][2]["params"] == {"id": "task-1"}


@pytest.mark.asyncio
async def test_send_message_includes_context_id_and_auth_headers() -> None:
    http = FakeHTTPClient()
    client = A2AClient(http_client=http, auth=A2AAuthConfig(api_key="key-1", api_key_header="X-IAC-Code-Key"))

    await client.send_message("http://remote/", "hello", cwd="/tmp/work", context_id="ctx-1")

    payload = http.requests[-1][2]
    headers = http.requests[-1][3]
    assert payload is not None
    assert payload["params"]["message"]["contextId"] == "ctx-1"
    assert headers == {"A2A-Version": "1.0", "X-IAC-Code-Key": "key-1"}


def test_select_endpoint_url_prefers_first_supported_interface() -> None:
    card = {
        "url": "http://fallback.example/rpc",
        "supportedInterfaces": [
            {
                "url": "http://card.example/a2a",
                "protocolBinding": "JSONRPC",
                "protocolVersion": "1.0",
            }
        ],
    }

    assert A2AClient.select_endpoint_url(card, fallback_url="http://input.example/") == "http://card.example/a2a"


def test_select_endpoint_url_falls_back_when_card_has_no_interface_url() -> None:
    assert A2AClient.select_endpoint_url({"name": "remote"}, fallback_url="http://input.example/") == (
        "http://input.example/"
    )


@pytest.mark.asyncio
async def test_discover_verifies_signed_card_when_configured() -> None:
    http = FakeHTTPClient()
    signed = sign_agent_card_dict({"name": "remote", "url": "http://remote/"}, secret="s" * 32, key_id="local")

    async def fake_get(url: str, headers: dict[str, str] | None = None) -> FakeHTTPResponse:
        return FakeHTTPResponse(signed)

    http.get = fake_get
    client = A2AClient(http_client=http, verification_secret="s" * 32, require_card_signature=True)

    assert (await client.discover("http://remote"))["name"] == "remote"


@pytest.mark.asyncio
async def test_discover_rejects_bad_signature_when_strict() -> None:
    http = FakeHTTPClient()
    signed = sign_agent_card_dict({"name": "remote", "url": "http://remote/"}, secret="s" * 32, key_id="local")

    async def fake_get(url: str, headers: dict[str, str] | None = None) -> FakeHTTPResponse:
        return FakeHTTPResponse(signed)

    http.get = fake_get
    client = A2AClient(http_client=http, verification_secret="w" * 32, require_card_signature=True)

    with pytest.raises(A2ACardVerificationError, match="signature-mismatch"):
        await client.discover("http://remote")


@pytest.mark.asyncio
async def test_discover_fetches_remote_jwks_from_protected_header_jku() -> None:
    private_key = _rsa_private_key()
    signed = _sign_with_rsa(
        {"name": "remote", "url": "http://remote/"},
        private_key,
        kid="rsa-current",
        jku="http://remote/.well-known/jwks.json",
    )
    jwks = {"keys": [_rsa_public_jwk(private_key, "rsa-current")]}

    class RemoteJwksHTTPClient(FakeHTTPClient):
        async def get(self, url: str, headers: dict[str, str] | None = None) -> FakeHTTPResponse:
            self.requests.append(("GET", url, None, headers))
            if url == "http://remote/.well-known/agent-card.json":
                return FakeHTTPResponse(signed)
            if url == "http://remote/.well-known/jwks.json":
                return FakeHTTPResponse(jwks)
            raise AssertionError(f"unexpected URL {url}")

    http = RemoteJwksHTTPClient()
    client = A2AClient(http_client=http, require_card_signature=True)

    assert (await client.discover("http://remote"))["name"] == "remote"
    assert http.requests[1] == ("GET", "http://remote/.well-known/jwks.json", None, None)


@pytest.mark.asyncio
async def test_discover_refreshes_remote_jwks_when_key_rotates() -> None:
    old_private_key = _rsa_private_key()
    new_private_key = _rsa_private_key()
    jku = "http://remote/.well-known/jwks.json"
    old_signed = _sign_with_rsa({"name": "remote", "url": "http://remote/"}, old_private_key, kid="rsa-old", jku=jku)
    new_signed = _sign_with_rsa({"name": "remote", "url": "http://remote/"}, new_private_key, kid="rsa-new", jku=jku)
    old_jwks = {"keys": [_rsa_public_jwk(old_private_key, "rsa-old")]}
    new_jwks = {"keys": [_rsa_public_jwk(new_private_key, "rsa-new")]}

    class RotatingJwksHTTPClient(FakeHTTPClient):
        def __init__(self) -> None:
            super().__init__()
            self.card_responses = [old_signed, new_signed]
            self.jwks_responses = [old_jwks, new_jwks]

        async def get(self, url: str, headers: dict[str, str] | None = None) -> FakeHTTPResponse:
            self.requests.append(("GET", url, None, headers))
            if url == "http://remote/.well-known/agent-card.json":
                return FakeHTTPResponse(self.card_responses.pop(0))
            if url == jku:
                return FakeHTTPResponse(self.jwks_responses.pop(0))
            raise AssertionError(f"unexpected URL {url}")

    http = RotatingJwksHTTPClient()
    client = A2AClient(http_client=http, require_card_signature=True)

    assert (await client.discover("http://remote"))["name"] == "remote"
    assert (await client.discover("http://remote"))["name"] == "remote"
    assert [request[1] for request in http.requests].count(jku) == 2


@pytest.mark.asyncio
async def test_remote_jwks_cache_expires_after_ttl() -> None:
    jku = "http://remote/.well-known/jwks.json"

    class CountingJwksHTTPClient(FakeHTTPClient):
        async def get(self, url: str, headers: dict[str, str] | None = None) -> FakeHTTPResponse:
            self.requests.append(("GET", url, None, headers))
            if url == jku:
                return FakeHTTPResponse({"keys": [{"kid": f"key-{len(self.requests)}"}]})
            return await super().get(url, headers=headers)

    now = 100.0
    http = CountingJwksHTTPClient()
    client = A2AClient(http_client=http, jwks_cache_ttl_seconds=10.0, clock=lambda: now)

    first = await client._remote_jwks(jku, force_refresh=False)
    second = await client._remote_jwks(jku, force_refresh=False)
    now = 111.0
    third = await client._remote_jwks(jku, force_refresh=False)

    assert first is second
    assert third is not first
    assert [request[1] for request in http.requests].count(jku) == 2


@pytest.mark.asyncio
async def test_aclose_does_not_close_injected_http_client() -> None:
    http = FakeHTTPClient()
    client = A2AClient(http_client=http)

    await client.aclose()

    assert http.closed is False


@pytest.mark.asyncio
async def test_aclose_closes_owned_http_client(monkeypatch: pytest.MonkeyPatch) -> None:
    http = FakeHTTPClient()
    monkeypatch.setattr("iac_code.a2a.client.httpx.AsyncClient", lambda: http)
    client = A2AClient()

    await client.aclose()

    assert http.closed is True


@pytest.mark.asyncio
async def test_bound_http_client_aclose_does_not_close_shared_transport() -> None:
    class SharedTransport:
        def __init__(self) -> None:
            self.closed = False

        async def aclose(self) -> None:
            self.closed = True

    transport = SharedTransport()
    bound = _BoundHttpA2AClient(transport, "http://remote/")

    await bound.aclose()

    assert transport.closed is False
