"""Tests for the loopback Web workbench all-redaction suppression middleware."""

from __future__ import annotations

import asyncio

from iac_code.utils.public_errors import all_redaction_suppressed


def test_middleware_suppresses_all_redaction_during_request_and_resets() -> None:
    from iac_code.web.app import _SuppressAllRedactionMiddleware

    seen: dict[str, bool] = {}

    async def downstream(scope, receive, send) -> None:
        seen["during"] = all_redaction_suppressed()
        # A background task created inside the request copies the current context,
        # so it must keep redaction suppressed even after the request returns.
        seen["task"] = None

        async def _bg() -> None:
            seen["task"] = all_redaction_suppressed()

        task = asyncio.create_task(_bg())
        await task

    middleware = _SuppressAllRedactionMiddleware(downstream)

    assert all_redaction_suppressed() is False
    asyncio.run(middleware({"type": "http"}, None, None))
    assert seen["during"] is True
    assert seen["task"] is True
    # ContextVar must reset once the request scope exits.
    assert all_redaction_suppressed() is False


def test_create_app_wires_middleware_only_when_exposing_local_paths() -> None:
    from iac_code.web.app import _SuppressAllRedactionMiddleware, create_app

    def _has_suppress_mw(app) -> bool:
        return any(mw.cls is _SuppressAllRedactionMiddleware for mw in app.user_middleware)

    assert _has_suppress_mw(create_app(expose_local_paths=True)) is True
    assert _has_suppress_mw(create_app()) is False
