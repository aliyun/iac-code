from __future__ import annotations

import base64
import os
import shutil
import time
from collections import OrderedDict
from pathlib import Path

from iac_code.config import get_config_dir
from iac_code.utils.image.pasted_content import PastedContent

IMAGE_STORE_DIR_NAME = "image-cache"
MAX_STORED_IMAGE_PATHS = 200
# Concurrent REPL sessions each schedule background cleanup. To avoid
# wiping a sibling session's still-in-use cache, only delete dirs whose
# mtime is older than this threshold. Storing an image refreshes the
# session-dir mtime, so any session active in the last 24h is preserved.
CLEANUP_MAX_AGE_SECONDS: float = 24 * 60 * 60


def _get_base_dir() -> Path:
    return get_config_dir() / IMAGE_STORE_DIR_NAME


def _validate_session_id(session_id: str) -> None:
    if not session_id or "/" in session_id or "\\" in session_id or session_id in (".", ".."):
        raise ValueError(f"invalid session_id: {session_id!r}")


class ImageStore:
    def __init__(self, session_id: str) -> None:
        _validate_session_id(session_id)
        self._session_id = session_id
        self._paths: OrderedDict[int, str] = OrderedDict()

    def _session_dir(self) -> Path:
        return _get_base_dir() / self._session_id

    def store(self, pc: PastedContent) -> str | None:
        if not pc.is_valid_image():
            return None
        d = self._session_dir()
        d.mkdir(parents=True, exist_ok=True)
        ext = (pc.media_type or "image/png").split("/")[-1]
        path = d / f"{pc.id}.{ext}"
        try:
            data = base64.b64decode(pc.content)
            fd = os.open(str(path), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            try:
                os.write(fd, data)
            finally:
                os.close(fd)
        except Exception:
            return None
        self.cache_path(pc.id, str(path))
        return str(path)

    def cache_path(self, image_id: int, path: str) -> None:
        if image_id in self._paths:
            self._paths.move_to_end(image_id)
        self._paths[image_id] = path
        while len(self._paths) > MAX_STORED_IMAGE_PATHS:
            self._paths.popitem(last=False)

    def get_path(self, image_id: int) -> str | None:
        return self._paths.get(image_id)

    def clear(self) -> None:
        self._paths.clear()


def cleanup_old_image_caches(
    *,
    current_session_id: str,
    max_age_seconds: float = CLEANUP_MAX_AGE_SECONDS,
) -> None:
    _validate_session_id(current_session_id)
    base = _get_base_dir()
    if not base.exists():
        return
    now = time.time()
    for entry in base.iterdir():
        if not entry.is_dir() or entry.name == current_session_id:
            continue
        try:
            age = now - entry.stat().st_mtime
        except OSError:
            continue
        if age < max_age_seconds:
            continue
        shutil.rmtree(entry, ignore_errors=True)
