"""Web 独有的第 4 步架构图优化结果缓存(纯 IO)。

仿 image-cache/ 约定,文件落在 get_config_dir()/diagram-cache/,绝不写共享 a2a journal。
读未命中(缺失 / 损坏 / 模板变更 / context_id 非法)一律返回 None,调用方回退确定性草图。
"""

from __future__ import annotations

import hashlib
import json
import logging
from pathlib import Path

from iac_code.config import get_config_dir
from iac_code.utils.file_security import atomic_write_text, ensure_private_dir

DIAGRAM_CACHE_DIR_NAME = "diagram-cache"

logger = logging.getLogger(__name__)


def _safe_context_id(context_id: str | None) -> str | None:
    if not context_id:
        return None
    text = str(context_id)
    if "/" in text or "\\" in text or ".." in text:
        return None
    return text


def template_hash(template_content: str) -> str:
    return hashlib.sha256(template_content.encode("utf-8")).hexdigest()[:16]


def cache_path(context_id: str, candidate_index: int, thash: str) -> Path:
    return get_config_dir() / DIAGRAM_CACHE_DIR_NAME / context_id / "{}-{}.json".format(candidate_index, thash)


def read_cached(context_id: str | None, candidate_index: int, template_content: str) -> list[dict] | None:
    safe = _safe_context_id(context_id)
    if safe is None:
        return None
    path = cache_path(safe, candidate_index, template_hash(template_content))
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict):
        return None
    raw_views = data.get("views")
    if isinstance(raw_views, list):
        views = [
            {
                "id": str(v.get("id") or ""),
                "title": str(v.get("title") or ""),
                "mermaidSource": v["mermaidSource"],
            }
            for v in raw_views
            if isinstance(v, dict) and isinstance(v.get("mermaidSource"), str) and v.get("mermaidSource")
        ]
        return views or None
    legacy = data.get("mermaidSource")
    if isinstance(legacy, str) and legacy:
        return [{"id": "overview", "title": "", "mermaidSource": legacy}]
    return None


def write_cached(
    context_id: str | None,
    candidate_index: int,
    template_content: str,
    views: list[dict],
    model: str | None,
) -> None:
    safe = _safe_context_id(context_id)
    if safe is None:
        return
    thash = template_hash(template_content)
    path = cache_path(safe, candidate_index, thash)
    payload = {
        "candidateIndex": candidate_index,
        "templateHash": thash,
        "views": views,
        "model": model,
    }
    try:
        ensure_private_dir(path.parent)
        atomic_write_text(path, json.dumps(payload, ensure_ascii=False))
    except (OSError, ValueError):
        logger.exception("Failed to write diagram cache for candidate %s", candidate_index)
