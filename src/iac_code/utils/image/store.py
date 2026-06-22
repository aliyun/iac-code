from __future__ import annotations

import base64
import hashlib
import os
import shutil
import time
from collections import OrderedDict
from pathlib import Path

from iac_code.config import get_config_dir
from iac_code.utils.file_security import ensure_private_dir
from iac_code.utils.image.pasted_content import PastedContent

IMAGE_STORE_DIR_NAME = "image-cache"
MAX_STORED_IMAGE_PATHS = 200
KNOWN_IMAGE_SUFFIXES = (".png", ".jpeg", ".jpg", ".gif", ".webp")
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
        ensure_private_dir(_get_base_dir())
        d = ensure_private_dir(self._session_dir())
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

    def store_block(self, block: object) -> str | None:
        data = getattr(block, "data", "")
        if not data:
            return None
        ensure_private_dir(_get_base_dir())
        d = ensure_private_dir(self._session_dir())
        media_type = getattr(block, "media_type", None) or "image/png"
        ext = media_type.split("/")[-1]
        digest = hashlib.sha256(str(data).encode()).hexdigest()[:32]
        path = d / f"block-{digest}.{ext}"
        if path.is_file():
            return str(path)
        try:
            decoded = base64.b64decode(data)
            fd = os.open(str(path), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            try:
                os.write(fd, decoded)
            finally:
                os.close(fd)
        except FileExistsError:
            return str(path)
        except Exception:
            return None
        return str(path)

    def cache_path(self, image_id: int, path: str) -> None:
        if image_id in self._paths:
            self._paths.move_to_end(image_id)
        self._paths[image_id] = path
        while len(self._paths) > MAX_STORED_IMAGE_PATHS:
            self._paths.popitem(last=False)

    def get_path(self, image_id: int) -> str | None:
        cached = self._paths.get(image_id)
        if cached:
            return cached
        discovered = self._discover_cached_path(image_id)
        if discovered:
            self.cache_path(image_id, discovered)
        return discovered

    def _discover_cached_path(self, image_id: int) -> str | None:
        session_dir = self._session_dir()
        if not session_dir.exists():
            return None
        for suffix in KNOWN_IMAGE_SUFFIXES:
            path = session_dir / f"{image_id}{suffix}"
            if path.is_file():
                return str(path)
        for path in sorted(session_dir.glob(f"{image_id}.*")):
            if path.is_file():
                return str(path)
        return None

    def next_image_id(self) -> int:
        image_ids = [image_id for image_id in self._paths if image_id > 0]
        session_dir = self._session_dir()
        if session_dir.exists():
            for path in session_dir.iterdir():
                if not path.is_file():
                    continue
                try:
                    image_ids.append(int(path.stem))
                except ValueError:
                    continue
        return max(image_ids, default=0) + 1

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
    ensure_private_dir(base)
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
