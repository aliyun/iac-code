from starlette.applications import Starlette
from starlette.responses import PlainTextResponse
from starlette.routing import Route
from starlette.testclient import TestClient


def _guarded_client():
    from iac_code.web.server import protect_loopback_app

    async def home(_request):
        return PlainTextResponse("ok")

    return TestClient(
        protect_loopback_app(Starlette(routes=[Route("/", home)])),
        base_url="http://127.0.0.1:8766",
    )


def test_loopback_guard_accepts_loopback_and_test_clients() -> None:
    with _guarded_client() as client:
        assert client.get("/").status_code == 200
        assert (
            client.get(
                "/",
                headers={
                    "host": "localhost:8766",
                    "origin": "http://localhost:8766",
                    "sec-fetch-site": "same-origin",
                },
            ).status_code
            == 200
        )
        assert (
            client.get(
                "/",
                headers={"host": "[::1]:8766", "origin": "http://[::1]:8766", "sec-fetch-site": "none"},
            ).status_code
            == 200
        )


def test_loopback_guard_fails_closed_for_rebinding_and_cross_site_requests() -> None:
    with _guarded_client() as client:
        assert client.get("/", headers={"host": "attacker.example"}).status_code == 403
        assert (
            client.get(
                "/",
                headers={"host": "localhost:8766", "origin": "https://attacker.example"},
            ).status_code
            == 403
        )
        assert (
            client.get(
                "/",
                headers={"host": "localhost:8766", "origin": "http://127.0.0.1:8766"},
            ).status_code
            == 403
        )
        assert (
            client.get(
                "/",
                headers={"host": "localhost:8766", "origin": "http://localhost:9000"},
            ).status_code
            == 403
        )
        assert (
            client.get(
                "/",
                headers={"host": "localhost:8766", "sec-fetch-site": "cross-site"},
            ).status_code
            == 403
        )
