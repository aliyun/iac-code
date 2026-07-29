"""Local Web sessions retain the no-redaction context for unchanged consumers."""

from __future__ import annotations

from starlette.applications import Starlette
from starlette.responses import PlainTextResponse
from starlette.routing import Route
from starlette.testclient import TestClient


def test_create_app_installs_local_redaction_suppression_middleware() -> None:
    from iac_code.web.app import create_app

    for app in (create_app(), create_app(expose_local_paths=True)):
        middleware_names = {middleware.cls.__name__ for middleware in app.user_middleware}
        assert "_SuppressAllRedactionMiddleware" in middleware_names


def test_expose_local_paths_compatibility_flag_does_not_change_local_payloads() -> None:
    from iac_code.web.app import create_app

    default_app = create_app()
    compatibility_app = create_app(expose_local_paths=True)

    assert [item.cls for item in default_app.user_middleware] == [
        item.cls for item in compatibility_app.user_middleware
    ]


def test_web_suppression_keeps_existing_permission_audit_behavior() -> None:
    from iac_code.services.permissions.audit import sanitize_free_text
    from iac_code.web.app import _SuppressAllRedactionMiddleware

    async def audit_text(_request):
        return PlainTextResponse(sanitize_free_text("read /tmp/private/result.json") or "")

    app = Starlette(routes=[Route("/", audit_text)])
    app.add_middleware(_SuppressAllRedactionMiddleware)

    with TestClient(app) as client:
        response = client.get("/")

    assert response.text == "read /tmp/private/result.json"
