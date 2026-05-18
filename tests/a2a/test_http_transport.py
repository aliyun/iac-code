import pytest

from iac_code.a2a.transports.http import HttpA2AClient

from .test_client import FakeHTTPClient


@pytest.mark.asyncio
async def test_http_transport_does_not_close_injected_http_client() -> None:
    http = FakeHTTPClient()
    client = HttpA2AClient(http_client=http)

    await client.aclose()

    assert http.closed is False


@pytest.mark.asyncio
async def test_http_transport_closes_owned_http_client(monkeypatch: pytest.MonkeyPatch) -> None:
    http = FakeHTTPClient()
    monkeypatch.setattr("iac_code.a2a.transports.http.httpx.AsyncClient", lambda: http)
    client = HttpA2AClient()

    await client.aclose()

    assert http.closed is True
