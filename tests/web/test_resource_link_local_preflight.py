from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest


def test_local_preflight_offline_contracts() -> None:
    node = shutil.which("node")
    if not node:
        pytest.skip("Node.js is required for browser helper tests")
    result = subprocess.run(
        [node, "--test", str(Path(__file__).with_name("resource_link_local_preflight.test.mjs"))],
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=30,
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr
