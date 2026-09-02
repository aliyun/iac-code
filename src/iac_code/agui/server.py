"""Uvicorn entry point for the A2A-backed AG-UI adapter."""

from __future__ import annotations

import contextlib
import os
from collections.abc import Iterator
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from iac_code.a2a.client import A2AClient
from iac_code.a2a.transport import A2AAuthConfig
from iac_code.agui.process import LocalA2AProcess
from iac_code.i18n import _


def run_server(
    *,
    host: str = "127.0.0.1",
    port: int = 8000,
    a2a_url: str | None = None,
    a2a_token: str | None = None,
    state_dir: str | Path | None = None,
    debug: bool = False,
    auth_token: str | None = None,
    idle_shutdown: float = 0,
) -> None:
    import uvicorn

    from iac_code.agui.app import create_app

    with _a2a_endpoint(a2a_url=a2a_url, a2a_token=a2a_token) as endpoint:
        client = A2AClient(
            auth=A2AAuthConfig(bearer_token=endpoint[1]) if endpoint[1] else None,
            timeout_seconds=3600,
        )
        server: Any | None = None

        def request_shutdown() -> None:
            assert server is not None
            server.should_exit = True

        app = create_app(
            a2a_url=endpoint[0],
            a2a_client=client,
            state_dir=state_dir,
            auth_token=auth_token,
            idle_shutdown=idle_shutdown,
            request_shutdown=request_shutdown,
        )
        config = uvicorn.Config(
            app,
            host=host,
            port=port,
            log_level="debug" if debug else "info",
            workers=1,
        )
        server = uvicorn.Server(config)
        server.run()


@contextlib.contextmanager
def _a2a_endpoint(*, a2a_url: str | None, a2a_token: str | None) -> Iterator[tuple[str, str | None]]:
    configured_url = a2a_url or os.environ.get("IAC_CODE_AGUI_A2A_URL")
    configured_token = a2a_token or os.environ.get("IAC_CODE_AGUI_A2A_TOKEN")
    if configured_url:
        yield _local_a2a_url(configured_url), configured_token
        return
    process: Any = LocalA2AProcess()
    try:
        process.start()
        yield process.url, process.token
    finally:
        process.close()


def _local_a2a_url(value: str) -> str:
    parsed = urlparse(value)
    if parsed.scheme not in {"http", "https"} or parsed.hostname not in {"127.0.0.1", "localhost", "::1"}:
        raise ValueError(_("The AG-UI adapter may connect only to a loopback A2A HTTP(S) URL."))
    return value.rstrip("/") + "/"
