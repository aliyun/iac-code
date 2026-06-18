"""Regression tests for N-I2: pipeline modules must not load in normal mode."""

from __future__ import annotations

import sys


def _preserve_parent_module_attr(monkeypatch, module_name: str) -> None:
    parent_name, _, attr = module_name.rpartition(".")
    if not parent_name:
        return
    parent_module = sys.modules.get(parent_name)
    if parent_module is None or not hasattr(parent_module, attr):
        return
    monkeypatch.setattr(parent_module, attr, getattr(parent_module, attr), raising=False)


def test_pipeline_engine_not_imported_in_normal_mode(monkeypatch):
    """Normal mode (IAC_CODE_MODE unset) must not trigger pipeline.engine load.

    Background: iac_code.pipeline.__init__.py top-level imports PipelineRunner
    from pipeline.engine.pipeline_runner, so any top-level
    `from iac_code.pipeline import ...` pulls in all 14 engine modules.
    Users who never use pipeline mode pay that startup cost.
    """
    monkeypatch.delenv("IAC_CODE_MODE", raising=False)

    # Clear engine + repl/server modules so we observe fresh import behavior.
    for mod_name in list(sys.modules):
        if (
            mod_name.startswith("iac_code.pipeline.engine")
            or mod_name == "iac_code.pipeline"
            or mod_name == "iac_code.ui.repl"
            or mod_name == "iac_code.acp.server"
        ):
            _preserve_parent_module_attr(monkeypatch, mod_name)
            monkeypatch.delitem(sys.modules, mod_name)

    # Importing the REPL module must NOT cascade into pipeline.engine.
    import iac_code.ui.repl  # noqa: F401

    assert "iac_code.pipeline.engine.pipeline_runner" not in sys.modules, (
        "repl.py is still triggering pipeline.engine load at import time"
    )
    assert "iac_code.pipeline.engine.sub_pipeline_executor" not in sys.modules

    # ACP server module also must not cascade.
    import iac_code.acp.server  # noqa: F401

    assert "iac_code.pipeline.engine.pipeline_runner" not in sys.modules
