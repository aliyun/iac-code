"""Pipeline configuration — run mode and environment settings."""

from __future__ import annotations

import os
from enum import Enum

from loguru import logger

from iac_code.config import _load_yaml, _save_yaml, get_settings_path

# settings.yml 里流水线相关的持久化用户设置(供 web「设置/常规」与流水线执行共读),
# 放在中立的 pipeline 层:web 依赖 a2a/pipeline,反向导入会成环,故读取侧落此处。
_PIPELINE_SETTINGS_KEY = "pipeline"
# 售卖流水线「审查步骤」(enable_reviewing 特性开关)的用户开关键;默认关闭,与 pipeline.yaml 默认一致。
_SELLING_REVIEW_STEP_KEY = "sellingReviewStep"


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


def is_selling_review_step_enabled() -> bool:
    """Return whether the user enabled the selling pipeline's review step (default ``False``).

    Persisted from the web「设置/常规」toggle. Read at fresh pipeline construction to
    override the YAML default of ``enable_reviewing``; an explicit env var still wins.
    """
    settings = _load_yaml(get_settings_path())
    section = settings.get(_PIPELINE_SETTINGS_KEY)
    if not isinstance(section, dict):
        return False
    return bool(section.get(_SELLING_REVIEW_STEP_KEY, False))


def save_selling_review_step_enabled(enabled: bool) -> bool:
    """Persist the selling pipeline review-step toggle, preserving other section keys."""
    settings_path = get_settings_path()
    settings = _load_yaml(settings_path)
    section = settings.get(_PIPELINE_SETTINGS_KEY)
    if not isinstance(section, dict):
        section = {}
    section[_SELLING_REVIEW_STEP_KEY] = bool(enabled)
    settings[_PIPELINE_SETTINGS_KEY] = section
    _save_yaml(settings_path, settings)
    return bool(enabled)
