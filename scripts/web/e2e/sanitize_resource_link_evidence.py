#!/usr/bin/env python3
"""Redact sensitive URL parameters from a pulled resource-link evidence directory."""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import shutil
from pathlib import Path


TEXT_SUFFIXES = {".json", ".jsonl", ".log", ".md", ".txt"}
SENSITIVE_NAME = (
    r"(?:access_token|auth_token|token|authorization|signature|sig|secret|secret_key|access_key_secret|code)"
)
PATTERNS = (
    re.compile(rf"([?&]{SENSITIVE_NAME}=)[^&#\s\"'<>]+", re.IGNORECASE),
    re.compile(rf"((?:^|[\s\"'(]){SENSITIVE_NAME}=)[^&\s\"'<>]+", re.IGNORECASE),
    re.compile(rf'(\"{SENSITIVE_NAME}\"\s*:\s*\")[^\"]+', re.IGNORECASE),
    re.compile(rf"(%3F|%26)({SENSITIVE_NAME})(%3D)[^%\s\"'<>]+", re.IGNORECASE),
)


def digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def redact(text: str) -> tuple[str, int]:
    count = 0
    for pattern in PATTERNS:
        text, replacements = pattern.subn(lambda match: f"{match.group(1)}[REDACTED]", text)
        count += replacements
    return text, count


def _referenced_relative_path(value: str, candidates: set[str]) -> str | None:
    normalized = value.replace("\\", "/")
    for candidate in sorted(candidates, key=len, reverse=True):
        if normalized == candidate or normalized.endswith(f"/{candidate}"):
            return candidate
    return None


def _rewrite_digest_references(value: object, changed_digests: dict[str, tuple[str, str]]) -> int:
    updates = 0
    if isinstance(value, list):
        for item in value:
            updates += _rewrite_digest_references(item, changed_digests)
        return updates
    if not isinstance(value, dict):
        return updates

    for path_key, digest_key in (("path", "digest"), ("artifactPath", "artifactDigest")):
        path_value = value.get(path_key)
        digest_value = value.get(digest_key)
        if not isinstance(path_value, str) or not isinstance(digest_value, str):
            continue
        relative = _referenced_relative_path(path_value, set(changed_digests))
        if relative is None:
            continue
        original_digest, sanitized_digest = changed_digests[relative]
        if digest_value == original_digest:
            value[digest_key] = sanitized_digest
            updates += 1

    for item in value.values():
        updates += _rewrite_digest_references(item, changed_digests)
    return updates


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("directory", type=Path)
    args = parser.parse_args()
    root = args.directory.resolve()
    manifest = root / "manifest.json"
    remote_manifest = root / "manifest.remote.json"
    audit_path = root / "redaction-audit.json"
    if not manifest.is_file():
        raise SystemExit(f"manifest is missing: {manifest}")
    if not remote_manifest.exists():
        shutil.copyfile(manifest, remote_manifest)

    current_manifest_value = json.loads(manifest.read_text(encoding="utf-8"))
    remote_manifest_value = json.loads(remote_manifest.read_text(encoding="utf-8"))
    current_manifest_digests = {
        item["path"]: item["sha256"] for item in current_manifest_value.get("files", [])
    }
    remote_manifest_digests = {
        item["path"]: item["sha256"] for item in remote_manifest_value.get("files", [])
    }

    original_digests: dict[str, str] = {}
    replacements_by_path: dict[str, int] = {}
    for path in sorted(root.rglob("*")):
        if not path.is_file() or path in {manifest, remote_manifest, audit_path} or path.suffix not in TEXT_SUFFIXES:
            continue
        relative = path.relative_to(root).as_posix()
        original_digest = digest(path)
        original_digests[relative] = original_digest
        original = path.read_text(encoding="utf-8")
        sanitized, replacements = redact(original)
        if not replacements:
            continue
        path.write_text(sanitized, encoding="utf-8")
        replacements_by_path[relative] = replacements

    changed_digests = {
        relative: (original_digests[relative], digest(root / relative))
        for relative in replacements_by_path
    }
    for relative, original_digest in remote_manifest_digests.items():
        current_digest = current_manifest_digests.get(relative)
        candidate = root / relative
        if current_digest is None or current_digest == original_digest or not candidate.is_file():
            continue
        actual_digest = digest(candidate)
        if actual_digest == current_digest:
            changed_digests.setdefault(relative, (original_digest, actual_digest))
    reference_updates_by_path: dict[str, int] = {}
    for path in sorted(root.rglob("*.json")):
        if path in {manifest, remote_manifest, audit_path}:
            continue
        relative = path.relative_to(root).as_posix()
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            continue
        updates = _rewrite_digest_references(value, changed_digests)
        if not updates:
            continue
        if relative not in original_digests:
            original_digests[relative] = digest(path)
        path.write_text(f"{json.dumps(value, indent=2, ensure_ascii=False)}\n", encoding="utf-8")
        reference_updates_by_path[relative] = updates

    changed = []
    for relative in sorted(set(replacements_by_path) | set(reference_updates_by_path)):
        changed.append(
            {
                "path": relative,
                "replacements": replacements_by_path.get(relative, 0),
                "digestReferencesUpdated": reference_updates_by_path.get(relative, 0),
                "originalSha256": original_digests[relative],
                "sanitizedSha256": digest(root / relative),
            }
        )

    audit = {
        "schemaVersion": 1,
        "changedFiles": changed,
        "totalReplacements": sum(i["replacements"] for i in changed),
        "totalDigestReferencesUpdated": sum(i["digestReferencesUpdated"] for i in changed),
    }
    audit_path.write_text(f"{json.dumps(audit, indent=2, ensure_ascii=False)}\n", encoding="utf-8")

    files = []
    for path in sorted(root.rglob("*")):
        if not path.is_file() or path == manifest:
            continue
        files.append(
            {
                "path": path.relative_to(root).as_posix(),
                "bytes": path.stat().st_size,
                "sha256": digest(path),
            }
        )
    sanitized_manifest = {
        "schemaVersion": 1,
        "sanitizedFrom": "manifest.remote.json",
        "files": files,
    }
    manifest.write_text(f"{json.dumps(sanitized_manifest, indent=2, ensure_ascii=False)}\n", encoding="utf-8")


if __name__ == "__main__":
    main()
