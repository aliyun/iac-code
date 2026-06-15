"""Pipeline — state-machine-driven multi-step workflow engine."""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from iac_code.pipeline.engine.pipeline_runner import PipelineRunner

_PIPELINE_ROOT = Path(__file__).parent


def discover_pipelines() -> dict[str, Path]:
    """Scan for subdirectories containing pipeline.yaml."""
    result: dict[str, Path] = {}
    for child in sorted(_PIPELINE_ROOT.iterdir()):
        if child.is_dir() and (child / "pipeline.yaml").exists():
            result[child.name] = child
    return result


def create_pipeline(
    name: str,
    *,
    provider_manager: Any,
    base_tool_registry: Any,
    session_storage: Any,
    session_id: str,
    cwd: str | None = None,
    permission_context_getter: Callable[[], Any] | None = None,
    memory_content_getter: Callable[[], str] | None = None,
    auto_trigger_skills: list[Any] | None = None,
    resume_from_sidecar: bool = False,
) -> PipelineRunner:
    """Factory: create a pipeline runner by name.

    ``resume_from_sidecar=True`` instructs the runner to synchronously
    rebuild state from ``<session_id>/pipeline/`` during ``__init__`` —
    used by ``/resume`` swap to revive a pipeline persisted in the target
    session (问题 4).
    """
    # Import locally so simply importing iac_code.pipeline.config (a lightweight
    # env-var reader) does not cascade into the ~14-module engine load.
    from iac_code.pipeline.engine.pipeline_runner import PipelineRunner

    pipelines = discover_pipelines()
    if name not in pipelines:
        available = list(pipelines.keys())
        raise ValueError(f"Unknown pipeline: {name!r}. Available: {available}")
    return PipelineRunner(
        pipeline_dir=pipelines[name],
        provider_manager=provider_manager,
        base_tool_registry=base_tool_registry,
        session_storage=session_storage,
        session_id=session_id,
        cwd=cwd,
        permission_context_getter=permission_context_getter,
        memory_content_getter=memory_content_getter,
        auto_trigger_skills=auto_trigger_skills,
        resume_from_sidecar=resume_from_sidecar,
    )


def __getattr__(name: str) -> Any:
    """PEP 562 lazy attribute access.

    Lets ``from iac_code.pipeline import PipelineRunner`` keep working
    without pulling in pipeline.engine at package import time. Callers that
    only need iac_code.pipeline.config (lightweight env-var reader) no
    longer pay the ~14-module engine startup cost.
    """
    if name == "PipelineRunner":
        from iac_code.pipeline.engine.pipeline_runner import PipelineRunner

        return PipelineRunner
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


__all__ = ["PipelineRunner", "create_pipeline", "discover_pipelines"]
