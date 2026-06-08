"""ACP test fixtures.

Provides an autouse fixture that permits common test CWD values (like "/tmp")
through the cwd validation introduced for security hardening.
"""

from __future__ import annotations

from pathlib import Path

import pytest


@pytest.fixture(autouse=True)
def _allow_test_cwds(monkeypatch: pytest.MonkeyPatch) -> None:
    """Monkeypatch allowed_cwd_roots so test-supplied CWD values are accepted.

    Many ACP tests pass synthetic paths such as "/tmp" or "/source project;unsafe"
    as cwd. In production these are validated against a configurable allowlist;
    in tests we permit the filesystem root so all absolute paths pass.
    """
    monkeypatch.setattr(
        "iac_code.acp.server.allowed_cwd_roots",
        lambda: [Path("/")],
    )
