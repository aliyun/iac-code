"""ACP test fixtures.

Provides an autouse fixture that permits common test CWD values (like "/tmp")
through the cwd validation introduced for security hardening.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest


def _permissive_roots() -> list[Path]:
    """Return root paths that accept any absolute path on the host platform.

    On POSIX ``Path("/")`` covers every absolute path. On Windows
    ``Path("/")`` has no drive letter, so ``Path("C:\\tmp").relative_to``
    raises ValueError — include each fixed drive root explicitly so that
    POSIX-style placeholders like ``"/tmp"`` (which ``Path.resolve()``
    rewrites to ``C:\\tmp``) still pass the containment check.
    """
    if sys.platform != "win32":
        return [Path("/")]
    import string

    return [Path(f"{letter}:\\") for letter in string.ascii_uppercase]


@pytest.fixture(autouse=True)
def _allow_test_cwds(monkeypatch: pytest.MonkeyPatch) -> None:
    """Monkeypatch allowed_cwd_roots so test-supplied CWD values are accepted.

    Many ACP tests pass synthetic paths such as "/tmp" or "/source project;unsafe"
    as cwd. In production these are validated against a configurable allowlist;
    in tests we permit the filesystem root so all absolute paths pass.
    """
    monkeypatch.setattr(
        "iac_code.acp.server.allowed_cwd_roots",
        _permissive_roots,
    )
