from __future__ import annotations

import importlib.util
import sys

import pytest


def _fixture_runner():
    spec = importlib.util.spec_from_file_location(
        "sub_pipeline_permission_timeout_runner",
        "scripts/a2a/e2e/permission_wait/run_sub_pipeline_permission_timeout.py",
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


@pytest.mark.asyncio
async def test_real_agent_loop_candidates_continue_through_sub_pipeline_hard_timeout(tmp_path) -> None:
    runner = _fixture_runner()

    result = await runner.run_scenario(run_dir=tmp_path / "run", timeout_seconds=0.02)

    assert result["passed"] is True
    assert all(result["checks"].values())
