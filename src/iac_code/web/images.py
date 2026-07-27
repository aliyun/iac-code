"""Minimal image cache interface used by the Web runtime."""

from __future__ import annotations

import base64
import hashlib
import json
import os
import re
from collections import OrderedDict
from dataclasses import dataclass
from pathlib import Path, PureWindowsPath

from iac_code.config import get_config_dir
from iac_code.i18n import _
from iac_code.utils.file_security import atomic_write_text, ensure_private_dir, ensure_private_file

IMAGE_CACHE_DIR_NAME = "image-cache"
IMAGE_ID_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$")
MAX_IMAGE_BYTES = 20 * 1024 * 1024
MAX_IN_MEMORY_FALLBACK_IMAGES = 32
MAX_IN_MEMORY_FALLBACK_BYTES = 32 * 1024 * 1024
SUPPORTED_IMAGE_MEDIA_TYPES = frozenset({"image/png", "image/jpeg", "image/gif", "image/webp"})


@dataclass(frozen=True)
class CachedWebImage:
    image_id: str
    media_type: str
    data: bytes
    persisted: bool = True
    recovery_available: bool = True
    warning: str | None = None

    @property
    def base64_data(self) -> str:
        return base64.b64encode(self.data).decode("ascii")


_IMAGE_WRITE_FALLBACK_WARNING = (
    "Image was kept in memory because persistent cache write failed; cross-session recovery is unavailable."
)
_IN_MEMORY_IMAGE_CACHE: OrderedDict[tuple[str, str, str], CachedWebImage] = OrderedDict()


def _validate_image_id(image_id: str) -> str:
    if (
        not isinstance(image_id, str)
        or Path(image_id).is_absolute()
        or PureWindowsPath(image_id).is_absolute()
        or "/" in image_id
        or "\\" in image_id
        or ".." in image_id
        or not IMAGE_ID_PATTERN.fullmatch(image_id)
    ):
        raise ValueError(_("image id is invalid"))
    return image_id


def _validate_session_id(session_id: str) -> str:
    if (
        not isinstance(session_id, str)
        or Path(session_id).is_absolute()
        or PureWindowsPath(session_id).is_absolute()
        or "/" in session_id
        or "\\" in session_id
        or ".." in session_id
        or not IMAGE_ID_PATTERN.fullmatch(session_id)
    ):
        raise ValueError(_("session id is invalid"))
    return session_id


def _validate_media_type(media_type: str) -> str:
    if not isinstance(media_type, str) or len(media_type) > 128 or media_type not in SUPPORTED_IMAGE_MEDIA_TYPES:
        raise ValueError(_("media type is invalid"))
    return media_type


def _detect_media_type(data: bytes) -> str | None:
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if data.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if data.startswith((b"GIF87a", b"GIF89a")):
        return "image/gif"
    if len(data) >= 12 and data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp"
    return None


def _validate_image_data(data: bytes, media_type: str) -> bytes:
    image_data = bytes(data)
    if not image_data:
        raise ValueError(_("image data is empty"))
    if len(image_data) > MAX_IMAGE_BYTES:
        raise ValueError(_("image data is too large"))
    detected_media_type = _detect_media_type(image_data)
    if detected_media_type is None:
        raise ValueError(_("image data is not a supported image"))
    if detected_media_type != media_type:
        raise ValueError(_("image data does not match media type"))
    return image_data


def _normalize_cwd(cwd: str) -> str:
    if not isinstance(cwd, str) or not cwd:
        raise ValueError(_("cwd is invalid"))
    expanded = os.path.expandvars(os.path.expanduser(cwd))
    return str(Path(expanded).resolve(strict=False))


def _cwd_namespace(cwd: str) -> str:
    return hashlib.sha256(_normalize_cwd(cwd).encode("utf-8")).hexdigest()


def _session_cache_dir(session_id: str, *, cwd: str | None = None) -> Path:
    session_dir = get_config_dir() / IMAGE_CACHE_DIR_NAME / _validate_session_id(session_id)
    if cwd is None:
        return session_dir
    return session_dir / _cwd_namespace(cwd)


def _cache_paths(image_id: str, *, session_id: str, cwd: str) -> tuple[Path, Path]:
    safe_image_id = _validate_image_id(image_id)
    session_dir = _session_cache_dir(session_id, cwd=cwd)
    return session_dir / "{}.bin".format(safe_image_id), session_dir / "{}.json".format(safe_image_id)


def _fallback_key(image_id: str, *, session_id: str, cwd: str) -> tuple[str, str, str]:
    return (_validate_session_id(session_id), _cwd_namespace(cwd), _validate_image_id(image_id))


def _store_in_memory_fallback(
    image_id: str,
    image_data: bytes,
    *,
    media_type: str,
    cwd: str,
    session_id: str,
) -> CachedWebImage:
    key = _fallback_key(image_id, session_id=session_id, cwd=cwd)
    _reserve_in_memory_fallback_space(key, len(image_data))
    cached_image = CachedWebImage(
        image_id=image_id,
        media_type=media_type,
        data=image_data,
        persisted=False,
        recovery_available=False,
        warning=_IMAGE_WRITE_FALLBACK_WARNING,
    )
    _IN_MEMORY_IMAGE_CACHE[key] = cached_image
    return cached_image


def _load_in_memory_fallback(image_id: str, *, session_id: str, cwd: str) -> CachedWebImage | None:
    key = _fallback_key(image_id, session_id=session_id, cwd=cwd)
    cached_image = _IN_MEMORY_IMAGE_CACHE.get(key)
    if cached_image is not None:
        _IN_MEMORY_IMAGE_CACHE.move_to_end(key)
    return cached_image


def _in_memory_fallback_bytes() -> int:
    return sum(len(cached_image.data) for cached_image in _IN_MEMORY_IMAGE_CACHE.values())


def _reserve_in_memory_fallback_space(key: tuple[str, str, str], image_size: int) -> None:
    if image_size > MAX_IN_MEMORY_FALLBACK_BYTES:
        raise OSError(_("image fallback cache limit exceeded"))

    _IN_MEMORY_IMAGE_CACHE.pop(key, None)
    current_bytes = _in_memory_fallback_bytes()

    while _IN_MEMORY_IMAGE_CACHE and (
        current_bytes + image_size > MAX_IN_MEMORY_FALLBACK_BYTES
        or len(_IN_MEMORY_IMAGE_CACHE) >= MAX_IN_MEMORY_FALLBACK_IMAGES
    ):
        _old_key, old_image = _IN_MEMORY_IMAGE_CACHE.popitem(last=False)
        current_bytes -= len(old_image.data)

    if current_bytes + image_size > MAX_IN_MEMORY_FALLBACK_BYTES:
        raise OSError(_("image fallback cache limit exceeded"))


def _legacy_cache_paths(image_id: str, *, session_id: str) -> tuple[Path, Path]:
    safe_image_id = _validate_image_id(image_id)
    session_dir = _session_cache_dir(session_id)
    return session_dir / "{}.bin".format(safe_image_id), session_dir / "{}.json".format(safe_image_id)


def _metadata_matches_cwd(metadata: dict[str, object], cwd: str) -> bool:
    stored_cwd = metadata.get("cwd")
    return isinstance(stored_cwd, str) and _cwd_namespace(stored_cwd) == _cwd_namespace(cwd)


def _load_cached_image_from_paths(image_id: str, *, cwd: str, data_path: Path, metadata_path: Path) -> CachedWebImage:
    if not data_path.is_file() or not metadata_path.is_file():
        raise FileNotFoundError("image is not available: {}".format(image_id))
    try:
        metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise FileNotFoundError("image is not available: {}".format(image_id)) from exc
    if not isinstance(metadata, dict) or not _metadata_matches_cwd(metadata, cwd):
        raise FileNotFoundError("image is not available: {}".format(image_id))
    media_type = _validate_media_type(str(metadata.get("media_type") or ""))
    return CachedWebImage(image_id=image_id, media_type=media_type, data=data_path.read_bytes())


def store_cached_image(
    image_id: str,
    data: bytes,
    *,
    media_type: str,
    cwd: str,
    session_id: str,
) -> CachedWebImage:
    """Persist a web-uploaded image in the per-session temporary cache."""
    data_path, metadata_path = _cache_paths(image_id, session_id=session_id, cwd=cwd)
    safe_media_type = _validate_media_type(media_type)
    image_data = _validate_image_data(data, safe_media_type)
    try:
        ensure_private_dir(data_path.parent)
        fd = os.open(str(data_path), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        try:
            _write_all(fd, image_data)
        finally:
            os.close(fd)
        ensure_private_file(data_path)
        atomic_write_text(
            metadata_path,
            json.dumps(
                {
                    "image_id": image_id,
                    "media_type": safe_media_type,
                    "cwd": cwd,
                },
                ensure_ascii=False,
                sort_keys=True,
            ),
        )
        ensure_private_file(metadata_path)
    except OSError:
        return _store_in_memory_fallback(
            image_id,
            image_data,
            media_type=safe_media_type,
            cwd=cwd,
            session_id=session_id,
        )
    return CachedWebImage(image_id=image_id, media_type=safe_media_type, data=image_data)


def _write_all(fd: int, data: bytes) -> None:
    view = memoryview(data)
    written_total = 0
    while written_total < len(view):
        written = os.write(fd, view[written_total:])
        if written == 0:
            raise OSError(_("failed to write image data"))
        written_total += written


def load_cached_image(image_id: str, *, cwd: str, session_id: str) -> CachedWebImage:
    """Load a cached image by id from the per-session temporary cache."""
    for data_path, metadata_path in (
        _cache_paths(image_id, session_id=session_id, cwd=cwd),
        _legacy_cache_paths(image_id, session_id=session_id),
    ):
        try:
            return _load_cached_image_from_paths(image_id, cwd=cwd, data_path=data_path, metadata_path=metadata_path)
        except (FileNotFoundError, OSError):
            continue
    fallback = _load_in_memory_fallback(image_id, cwd=cwd, session_id=session_id)
    if fallback is not None:
        return fallback
    raise FileNotFoundError("image is not available: {}".format(image_id))
