"""Pipeline configuration — run mode and environment settings."""

from __future__ import annotations

import os
from enum import Enum

from loguru import logger


class RunMode(str, Enum):
    NORMAL = "normal"
    PIPELINE = "pipeline"


def get_run_mode() -> RunMode:
    raw = os.environ.get("IAC_CODE_MODE", "normal").lower()
    try:
        return RunMode(raw)
    except ValueError:
        logger.warning("Unknown IAC_CODE_MODE={!r}, falling back to normal", raw)
        return RunMode.NORMAL


def get_pipeline_name() -> str:
    return os.environ.get("IAC_CODE_PIPELINE_NAME", "selling")


def get_working_directory() -> str | None:
    return os.environ.get("IAC_CODE_CWD") or None
