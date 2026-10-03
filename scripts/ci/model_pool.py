"""Model assignments and rolling scheduling for isolated live E2E processes."""

from __future__ import annotations

from collections import Counter
from concurrent.futures import FIRST_COMPLETED, Future, ThreadPoolExecutor, wait
from dataclasses import dataclass
from typing import Any, Callable, Iterator

TEXT_MODELS = (
    "deepseek-v4-flash-0731", "glm-5.2-fast-preview", "glm-5.3-prime", "deepseek-v4.1-flash",
)
MULTIMODAL_MODELS = ("qwen3.8-max", "qwen3.8-max-0902", "qwen3.8-flash", "qwen3.8-omni-flash")


@dataclass(frozen=True)
class ModelAssignment:
    model: str
    multimodal: bool
    effort: str = "low"

    @property
    def thinking_budget(self) -> int | None:
        # Flash controls thinking with a token budget instead of reasoning_effort.
        return 2048 if self.model == "qwen3.8-flash" else None

    def report(self) -> dict[str, Any]:
        return {
            "model": self.model,
            "modelKind": "multimodal" if self.multimodal else "text",
            "reasoningEffort": None if self.thinking_budget else self.effort,
            "thinkingBudget": self.thinking_budget,
        }


def scheduled_cases(
    cases: list[Any], jobs: int, execute: Callable[..., Any], *,
    enabled: bool = True, text_models: tuple[str, ...] = TEXT_MODELS,
    multimodal_models: tuple[str, ...] = MULTIMODAL_MODELS,
    text_jobs: int = 2, multimodal_jobs: int = 1, case_models: dict[str, str] | None = None,
) -> Iterator[tuple[Any, ModelAssignment | None, Future]]:
    """Reserve model/resource capacity before occupying a worker, then refill on completion.

    Limits bound concurrent *cases*, not every internal LLM request. A diagnosis
    slot is reserved alongside at most two GLM Prime cases, even with text_jobs=3.
    """
    if not cases:
        return
    case_models = case_models or {}
    for case in cases:
        pinned = case_models.get(case.name)
        models = multimodal_models if case.multimodal else text_models
        if pinned and (not enabled or case.suite != "live" or pinned not in models):
            raise ValueError("pinned model must belong to the matching live model pool")
    pending = list(cases)
    busy_resources: set[str] = set()
    active: Counter[str] = Counter()
    dispatched: Counter[str] = Counter()
    with ThreadPoolExecutor(max_workers=min(jobs, len(cases))) as pool:
        running: dict[Future, tuple[Any, ModelAssignment | None]] = {}
        while pending or running:
            for case in pending[:]:
                if len(running) >= jobs:
                    break
                if case.resource_lock and case.resource_lock in busy_resources:
                    continue
                assignment = None
                if enabled and case.suite == "live":
                    models = multimodal_models if case.multimodal else text_models
                    if case.name in case_models:
                        models = (case_models[case.name],)
                    capacity = multimodal_jobs if case.multimodal else text_jobs
                    available = [
                        model for model in models
                        if active[model] < (min(capacity, 2) if model == "glm-5.3-prime" else capacity)
                    ]
                    if not available:
                        continue
                    model = min(available, key=lambda name: (active[name], dispatched[name]))
                    assignment = ModelAssignment(model, case.multimodal)
                    active[model] += 1
                    dispatched[model] += 1
                if case.resource_lock:
                    busy_resources.add(case.resource_lock)
                pending.remove(case)
                running[pool.submit(execute, case, assignment)] = (case, assignment)
            if not running:
                raise ValueError("no model capacity for pending E2E cases")
            done, _ = wait(running, return_when=FIRST_COMPLETED)
            for future in done:
                case, assignment = running.pop(future)
                if case.resource_lock:
                    busy_resources.remove(case.resource_lock)
                if assignment is not None:
                    active[assignment.model] -= 1
                yield case, assignment, future
