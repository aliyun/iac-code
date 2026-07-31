from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import re
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)


class A2ATerminalTemplateCollector:
    """Collect bounded IaC templates for the terminal A2A status event."""

    _FORMAT_BY_SUFFIX = {
        ".json": "json",
        ".tf": "terraform",
        ".yaml": "yaml",
        ".yml": "yaml",
    }
    _YAML_TEMPLATE_MARKERS = re.compile(r"(?m)^\s*(?:ROSTemplateFormatVersion|Resources|Transform)\s*:")
    _TERRAFORM_TEMPLATE_MARKERS = re.compile(r'(?m)^\s*(?:resource|data|module|provider|terraform)\s+(?:"|{)')

    def __init__(
        self,
        *,
        max_files: int = 50,
        max_file_bytes: int = 2 * 1024 * 1024,
        max_total_bytes: int = 2 * 1024 * 1024,
    ) -> None:
        self._max_files = max_files
        self._max_file_bytes = max_file_bytes
        self._max_total_bytes = max_total_bytes

    async def collect(self, cwd: str | Path) -> list[dict[str, Any]]:
        try:
            return await asyncio.to_thread(self._collect_sync, cwd)
        except Exception:
            logger.exception("Failed to collect terminal A2A templates from cwd=%s", cwd)
            return []

    def _collect_sync(self, cwd: str | Path) -> list[dict[str, Any]]:
        root = Path(cwd).expanduser().resolve()
        if not root.is_dir():
            return []

        templates: list[dict[str, Any]] = []
        total_bytes = 0
        for file_path in self._candidate_paths(root):
            if len(templates) >= self._max_files:
                break
            payload = self._read_candidate(root, file_path)
            if payload is None:
                continue
            content, content_bytes, template_format = payload
            if total_bytes + len(content_bytes) > self._max_total_bytes:
                continue
            templates.append(
                {
                    "filePath": file_path.relative_to(root).as_posix(),
                    "content": content,
                    "format": template_format,
                    "contentSha256": hashlib.sha256(content_bytes).hexdigest(),
                }
            )
            total_bytes += len(content_bytes)
        return templates

    def _candidate_paths(self, root: Path) -> list[Path]:
        candidates: list[Path] = []
        for directory, directory_names, file_names in os.walk(root, followlinks=False):
            current = Path(directory)
            directory_names[:] = sorted(name for name in directory_names if not (current / name).is_symlink())
            for file_name in sorted(file_names):
                file_path = current / file_name
                if not file_path.is_symlink() and file_path.suffix.lower() in self._FORMAT_BY_SUFFIX:
                    candidates.append(file_path)
        return sorted(candidates, key=lambda path: path.relative_to(root).as_posix())

    def _read_candidate(
        self,
        root: Path,
        file_path: Path,
    ) -> tuple[str, bytes, str] | None:
        try:
            resolved = file_path.resolve(strict=True)
            resolved.relative_to(root)
            if not resolved.is_file() or resolved.stat().st_size > self._max_file_bytes:
                return None
            content_bytes = resolved.read_bytes()
            if len(content_bytes) > self._max_file_bytes:
                return None
            content = content_bytes.decode("utf-8")
        except (OSError, UnicodeDecodeError, ValueError):
            return None

        template_format = self._FORMAT_BY_SUFFIX[resolved.suffix.lower()]
        if not self._is_template(content, template_format):
            return None
        return content, content_bytes, template_format

    def _is_template(self, content: str, template_format: str) -> bool:
        if template_format == "json":
            try:
                value = json.loads(content)
            except json.JSONDecodeError:
                return False
            return isinstance(value, dict) and any(
                key in value for key in ("ROSTemplateFormatVersion", "Resources", "Transform")
            )
        if template_format == "terraform":
            return bool(self._TERRAFORM_TEMPLATE_MARKERS.search(content))
        return bool(self._YAML_TEMPLATE_MARKERS.search(content))
