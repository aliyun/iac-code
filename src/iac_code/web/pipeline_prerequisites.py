"""Web-surface detection and streaming installation for pipeline prerequisites.

The single source of truth for prerequisite resolution and installation stays in
:mod:`iac_code.pipeline.engine.prerequisites`. This module only adapts the
*blocking* installer (`prepare_prerequisites`, which downloads via urllib and
spawns subprocesses) into an async, NDJSON-friendly event stream for the web UI,
and exposes a read-only detection helper for the settings card.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Mapping
from typing import Any

import yaml

from iac_code.pipeline import discover_pipelines
from iac_code.pipeline.config import get_pipeline_name
from iac_code.pipeline.engine.loader import _resolve_feature_flags
from iac_code.pipeline.engine.prerequisites import (
    InstallerSpec,
    PrerequisiteProgress,
    inspect_prerequisites,
    prepare_prerequisites,
)

_REVIEW_PREREQUISITE_NAME = "infraguard"
_REVIEW_FEATURE_FLAG = "enable_reviewing"

# Only one install may run at a time (downloads to ~/bin, runs post-install).
_install_lock = asyncio.Lock()


def _load_pipeline_raw_config(pipeline_name: str) -> dict[str, Any]:
    pipeline_dir = discover_pipelines().get(pipeline_name)
    if pipeline_dir is None:
        return {}
    raw = yaml.safe_load((pipeline_dir / "pipeline.yaml").read_text(encoding="utf-8")) or {}
    return raw if isinstance(raw, dict) else {}


def _review_config() -> tuple[dict[str, Any], dict[str, bool]]:
    """Return (raw_prerequisites, feature_flags) for the current pipeline.

    ``enable_reviewing`` is force-enabled so the infraguard prerequisite (gated by
    that flag) is always evaluated, regardless of the user's current toggle state.
    """
    raw = _load_pipeline_raw_config(get_pipeline_name())
    prerequisites = raw.get("prerequisites") or {}
    if not isinstance(prerequisites, dict):
        prerequisites = {}
    flags = dict(_resolve_feature_flags(raw.get("feature_flags")))
    flags[_REVIEW_FEATURE_FLAG] = True
    return prerequisites, flags


def _installable_under_web(raw_prerequisite: Mapping[str, Any]) -> bool:
    on_missing = raw_prerequisite.get("on_missing") or {}
    installers = raw_prerequisite.get("installers") or []
    return bool(isinstance(on_missing, Mapping) and on_missing.get("web") == "prompt_install" and installers)


def _detection_payload(prerequisites: Mapping[str, Any], flags: Mapping[str, bool]) -> dict[str, Any]:
    raw_prereq = prerequisites.get(_REVIEW_PREREQUISITE_NAME)
    if not isinstance(raw_prereq, Mapping):
        return {
            "name": _REVIEW_PREREQUISITE_NAME,
            "satisfied": True,
            "status": "skipped",
            "installable": False,
        }
    resolution = inspect_prerequisites(prerequisites, feature_flags=flags)
    decision = resolution.decisions.get(_REVIEW_PREREQUISITE_NAME)
    status = decision.status if decision is not None else "unknown"
    satisfied = status == "available"
    return {
        "name": _REVIEW_PREREQUISITE_NAME,
        "satisfied": satisfied,
        "status": status,
        "installable": (not satisfied) and _installable_under_web(raw_prereq),
    }


def _inspect_review_step_prerequisite() -> dict[str, Any]:
    prerequisites, flags = _review_config()
    return _detection_payload(prerequisites, flags)


async def inspect_review_step_prerequisite() -> dict[str, Any]:
    """Read-only detection: is infraguard ready, and can the web install it?"""
    return await asyncio.to_thread(_inspect_review_step_prerequisite)


def install_in_progress() -> bool:
    return _install_lock.locked()


def _progress_to_dict(progress: PrerequisiteProgress) -> dict[str, Any]:
    # Structured fields only — the frontend renders Chinese labels from `phase`
    # so backend strings stay translation-free. `message` is a fallback text.
    return {
        "name": progress.name,
        "phase": progress.phase,
        "status": progress.status,
        "message": progress.message,
        "installer_display_name": progress.installer_display_name,
        "downloaded_bytes": progress.downloaded_bytes,
        "total_bytes": progress.total_bytes,
        "command": list(progress.command) if progress.command else [],
    }


def _choose_first_installer(_name: str, installers: list[InstallerSpec]) -> str | None:
    return installers[0].id if installers else None


async def stream_install_review_step_prerequisite() -> AsyncIterator[dict[str, Any]]:
    """Run the blocking installer in a worker thread, streaming progress events.

    Yields structured progress dicts as they arrive, then a terminal
    ``{"phase": "result", "status": "ok"|"error", "satisfied": bool, ...}`` event.
    """
    async with _install_lock:
        loop = asyncio.get_running_loop()
        queue: asyncio.Queue[Any] = asyncio.Queue()
        sentinel = object()

        def progress_handler(progress: PrerequisiteProgress) -> None:
            loop.call_soon_threadsafe(queue.put_nowait, _progress_to_dict(progress))

        def run_prepare() -> dict[str, Any]:
            prerequisites, flags = _review_config()
            resolution = prepare_prerequisites(
                prerequisites,
                feature_flags=flags,
                surface="web",
                choose_installer=_choose_first_installer,
                progress_handler=progress_handler,
            )
            decision = resolution.decisions.get(_REVIEW_PREREQUISITE_NAME)
            status = decision.status if decision is not None else "unknown"
            satisfied = status == "available"
            return {
                "phase": "result",
                "status": "ok" if satisfied else "error",
                "satisfied": satisfied,
                "prerequisite_status": status,
                "message": (decision.message if decision is not None else ""),
            }

        future = loop.run_in_executor(None, run_prepare)
        future.add_done_callback(lambda _f: loop.call_soon_threadsafe(queue.put_nowait, sentinel))

        while True:
            item = await queue.get()
            if item is sentinel:
                break
            yield item

        try:
            yield future.result()
        except Exception as exc:  # defensive: surface unexpected installer crashes
            yield {
                "phase": "result",
                "status": "error",
                "satisfied": False,
                "message": str(exc),
            }
