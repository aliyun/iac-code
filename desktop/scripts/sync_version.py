#!/usr/bin/env python3
"""Synchronize every Desktop package version from the Python package version."""

from __future__ import annotations

import argparse
import json
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
PYTHON_VERSION_PATTERN = re.compile(r'^__version__\s*=\s*"([^"]+)"', re.MULTILINE)


def python_version(root: Path = ROOT) -> str:
    source = (root / "src/iac_code/__init__.py").read_text(encoding="utf-8")
    match = PYTHON_VERSION_PATTERN.search(source)
    if match is None:
        raise RuntimeError("could not read the Python package version")
    return match.group(1)


def _replace_package_version(path: Path, version: str) -> str:
    source = path.read_text(encoding="utf-8")
    updated, count = re.subn(
        r'(?ms)(\[package\]\s+name\s*=\s*"[^"]+"\s+version\s*=\s*")[^"]+("\s*)',
        r"\g<1>{}\g<2>".format(version),
        source,
        count=1,
    )
    if count != 1:
        raise RuntimeError("could not update package version in {}".format(path))
    return updated


def _replace_lock_package_version(
    path: Path,
    package_name: str,
    version: str,
    *,
    source: str | None = None,
) -> str:
    current = path.read_text(encoding="utf-8") if source is None else source
    pattern = re.compile(
        r'(?ms)(\[\[package\]\]\s+name\s*=\s*"{}"\s+version\s*=\s*")[^"]+("\s*)'.format(
            re.escape(package_name)
        )
    )
    updated, count = pattern.subn(r"\g<1>{}\g<2>".format(version), current)
    if count != 1:
        raise RuntimeError("could not update {} in {}".format(package_name, path))
    return updated


def rendered_version_files(root: Path = ROOT) -> dict[Path, str]:
    version = python_version(root)
    rendered: dict[Path, str] = {}

    package_json = root / "desktop/package.json"
    package_payload = json.loads(package_json.read_text(encoding="utf-8"))
    package_payload["version"] = version
    rendered[package_json] = json.dumps(package_payload, ensure_ascii=False, indent=2) + "\n"

    tauri_config = root / "desktop/src-tauri/tauri.conf.json"
    tauri_source = tauri_config.read_text(encoding="utf-8")
    tauri_rendered, count = re.subn(
        r'(?m)^(\s*"version"\s*:\s*")[^"]+("\s*,?\s*)$',
        r"\g<1>{}\g<2>".format(version),
        tauri_source,
        count=1,
    )
    if count != 1:
        raise RuntimeError("could not update Desktop version in {}".format(tauri_config))
    rendered[tauri_config] = tauri_rendered

    package_lock = root / "desktop/package-lock.json"
    lock_payload = json.loads(package_lock.read_text(encoding="utf-8"))
    lock_payload["version"] = version
    lock_payload["packages"][""]["version"] = version
    rendered[package_lock] = json.dumps(lock_payload, ensure_ascii=False, indent=2) + "\n"

    main_cargo = root / "desktop/src-tauri/Cargo.toml"
    helper_cargo = root / "desktop/helpers/Cargo.toml"
    rendered[main_cargo] = _replace_package_version(main_cargo, version)
    rendered[helper_cargo] = _replace_package_version(helper_cargo, version)

    main_lock = root / "desktop/src-tauri/Cargo.lock"
    helper_lock = root / "desktop/helpers/Cargo.lock"
    main_lock_source = _replace_lock_package_version(main_lock, "iac-code-desktop", version)
    rendered[main_lock] = _replace_lock_package_version(
        main_lock,
        "iac-code-desktop-helpers",
        version,
        source=main_lock_source,
    )
    rendered[helper_lock] = _replace_lock_package_version(helper_lock, "iac-code-desktop-helpers", version)
    return rendered


def sync_desktop_versions(root: Path = ROOT, *, check: bool = False) -> list[Path]:
    rendered = rendered_version_files(root)
    changed = [
        path for path, content in rendered.items() if path.read_text(encoding="utf-8") != content
    ]
    if check:
        return changed
    for path in changed:
        path.write_text(rendered[path], encoding="utf-8")
    return changed


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--check", action="store_true", help="fail instead of updating files when versions drift")
    args = parser.parse_args()
    changed = sync_desktop_versions(check=args.check)
    if args.check and changed:
        print("Desktop versions do not match src/iac_code/__init__.py:")
        for path in changed:
            print("- {}".format(path.relative_to(ROOT)))
        return 1
    if changed:
        print("Synchronized Desktop version {} in {} file(s).".format(python_version(), len(changed)))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
