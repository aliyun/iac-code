#!/usr/bin/env python3
"""Real-provider A2A server backed by the small live selector test pipelines."""

from __future__ import annotations

import argparse
import os
from pathlib import Path
from typing import Any


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, required=True)
    parser.add_argument("--config-dir", type=Path, required=True)
    parser.add_argument("--persistence-dir", type=Path, required=True)
    parser.add_argument("--artifact-dir", type=Path, required=True)
    parser.add_argument("--workspace", type=Path, required=True)
    parser.add_argument("--pipeline-root", type=Path, required=True)
    parser.add_argument("--pipeline-name", required=True)
    parser.add_argument("--model", default="")
    return parser.parse_args()


def main() -> int:
    args = _parse_args()
    config_dir = args.config_dir.expanduser().resolve()
    persistence_dir = args.persistence_dir.expanduser().resolve()
    artifact_dir = args.artifact_dir.expanduser().resolve()
    workspace = args.workspace.expanduser().resolve()
    pipeline_root = args.pipeline_root.expanduser().resolve()
    pipeline_dir = pipeline_root / args.pipeline_name
    if not (pipeline_dir / "pipeline.yaml").is_file():
        raise ValueError("live resource-selector pipeline is missing: {}".format(pipeline_dir))
    for path in (config_dir, persistence_dir, artifact_dir, workspace):
        path.mkdir(parents=True, exist_ok=True)

    os.environ["IAC_CODE_CONFIG_DIR"] = str(config_dir)
    os.environ["IAC_CODE_MODE"] = "pipeline"
    os.environ["IAC_CODE_PIPELINE_NAME"] = args.pipeline_name
    os.environ["IACCODE_A2A_ALLOWED_CWDS"] = str(workspace)

    import uvicorn

    from iac_code.a2a import pipeline_executor as pipeline_executor_module
    from iac_code.a2a.app import create_app
    from iac_code.config import DEFAULT_MODEL, load_saved_model
    from iac_code.pipeline.engine.pipeline_runner import PipelineRunner

    def discover_live_pipelines() -> dict[str, Path]:
        return {
            path.name: path
            for path in pipeline_root.iterdir()
            if path.is_dir() and (path / "pipeline.yaml").is_file()
        }

    def create_live_pipeline(name: str, *unused_args: Any, **kwargs: Any) -> PipelineRunner:
        del unused_args
        selected = discover_live_pipelines().get(name)
        if selected is None:
            raise ValueError("unknown live resource-selector pipeline: {}".format(name))
        return PipelineRunner(pipeline_dir=selected, **kwargs)

    # Patch only pipeline discovery/creation. Agent runtime, provider, tools,
    # credential resolution, checkpointing and transport remain production code.
    pipeline_executor_module.discover_pipelines = discover_live_pipelines
    pipeline_executor_module.create_pipeline = create_live_pipeline

    app = create_app(
        host=args.host,
        port=args.port,
        token=None,
        model=args.model or load_saved_model() or DEFAULT_MODEL,
        persistence_dir=persistence_dir,
        artifact_dir=artifact_dir,
        auto_approve_permissions=False,
    )
    uvicorn.run(app, host=args.host, port=args.port, log_level="warning")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
