"""Bounded parse cache for packaged public data, with independent caller copies."""

from __future__ import annotations

import copy
import json
from functools import lru_cache
from pathlib import Path
from typing import Any

import yaml


@lru_cache(maxsize=16)
def _parse(path: Path, signature: tuple[int, int, int, int], format: str) -> Any:
    text = path.read_text(encoding="utf-8")
    return json.loads(text) if format == "json" else yaml.safe_load(text)


def load_bundled_data(path: Path, *, format: str) -> Any:
    """Invalidate replacements/edits; never share mutable policy dictionaries."""
    path = path.resolve()
    info = path.stat()
    signature = (info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns)
    return copy.deepcopy(_parse(path, signature, format))
