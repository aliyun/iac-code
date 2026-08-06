from __future__ import annotations

import importlib.util
from pathlib import Path


def _smoke_module():
    script = Path(__file__).parents[2] / "desktop/scripts/smoke_sidecar.py"
    spec = importlib.util.spec_from_file_location("iac_code_desktop_smoke", script)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_windows_smoke_pipe_uses_sidecar_control_prefix() -> None:
    pipe_name = _smoke_module().windows_control_pipe_name(123, "unique")

    assert pipe_name == r"\\.\pipe\iac-code-desktop-smoke-123-unique"
