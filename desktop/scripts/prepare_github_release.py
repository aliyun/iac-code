#!/usr/bin/env python3
"""Validate and describe Desktop pre-build assets for the internal publisher."""

from __future__ import annotations

import argparse
import base64
import hashlib
import json
import os
import re
import shutil
import subprocess
from pathlib import Path
from typing import Callable

ROOT = Path(__file__).resolve().parents[2]
REPOSITORY_PATTERN = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")
PYTHON_VERSION_PATTERN = re.compile(r'^__version__\s*=\s*"([^"]+)"', re.MULTILINE)
CARGO_VERSION_PATTERN = re.compile(r'^version\s*=\s*"([^"]+)"', re.MULTILINE)


def desktop_versions(root: Path = ROOT) -> dict[str, str]:
    package = json.loads((root / "desktop/package.json").read_text(encoding="utf-8"))
    tauri = json.loads((root / "desktop/src-tauri/tauri.conf.json").read_text(encoding="utf-8"))
    python_match = PYTHON_VERSION_PATTERN.search((root / "src/iac_code/__init__.py").read_text(encoding="utf-8"))
    cargo_match = CARGO_VERSION_PATTERN.search((root / "desktop/src-tauri/Cargo.toml").read_text(encoding="utf-8"))
    if python_match is None or cargo_match is None:
        raise RuntimeError("could not read every Desktop version source")
    return {
        "python": python_match.group(1),
        "desktop/package.json": str(package["version"]),
        "tauri.conf.json": str(tauri["version"]),
        "Cargo.toml": cargo_match.group(1),
    }


def validate_release_tag(tag: str, root: Path = ROOT) -> str:
    versions = desktop_versions(root)
    unique_versions = set(versions.values())
    if len(unique_versions) != 1:
        details = ", ".join("{}={}".format(name, value) for name, value in sorted(versions.items()))
        raise RuntimeError("Desktop version sources disagree: {}".format(details))
    version = unique_versions.pop()
    expected_tag = "v{}".format(version)
    if tag != expected_tag:
        raise RuntimeError("Desktop release tag must be {}, got {}".format(expected_tag, tag))
    return version


def _find_exactly_one(root: Path, label: str, predicate: Callable[[Path], bool]) -> Path:
    matches = sorted(
        (path for path in root.rglob("*") if path.is_file() and predicate(path)),
        key=lambda path: str(path),
    )
    if len(matches) != 1:
        rendered = ", ".join(str(path.relative_to(root)) for path in matches) or "none"
        raise RuntimeError("expected exactly one {} artifact, found {}: {}".format(label, len(matches), rendered))
    return matches[0]


def discover_release_artifacts(input_dir: Path) -> dict[str, Path]:
    artifacts = {
        "macos_installer": _find_exactly_one(
            input_dir,
            "macOS DMG installer",
            lambda path: path.name.endswith(".dmg") and not path.name.startswith("rw."),
        ),
        "macos_updater": _find_exactly_one(
            input_dir, "macOS updater", lambda path: path.name.endswith(".app.tar.gz")
        ),
        "macos_signature": _find_exactly_one(
            input_dir, "macOS updater signature", lambda path: path.name.endswith(".app.tar.gz.sig")
        ),
        "windows_installer": _find_exactly_one(
            input_dir,
            "Windows NSIS installer",
            lambda path: path.name.lower().endswith(".exe") and "setup" in path.name.lower(),
        ),
        "windows_updater": _find_exactly_one(
            input_dir, "Windows updater", lambda path: path.name.endswith(".nsis.zip")
        ),
        "windows_signature": _find_exactly_one(
            input_dir, "Windows updater signature", lambda path: path.name.endswith(".nsis.zip.sig")
        ),
        "linux_installer": _find_exactly_one(
            input_dir, "Linux AppImage", lambda path: path.name.endswith(".AppImage")
        ),
        "linux_signature": _find_exactly_one(
            input_dir, "Linux updater signature", lambda path: path.name.endswith(".AppImage.sig")
        ),
        "linux_deb": _find_exactly_one(input_dir, "Linux deb", lambda path: path.name.endswith(".deb")),
        "sbom": _find_exactly_one(
            input_dir, "Desktop SBOM", lambda path: path.name == "desktop-sbom.cdx.json"
        ),
        "notices": _find_exactly_one(
            input_dir, "third-party notices", lambda path: path.name == "THIRD_PARTY_NOTICES.txt"
        ),
    }
    for payload, signature in (
        (artifacts["macos_updater"], artifacts["macos_signature"]),
        (artifacts["windows_updater"], artifacts["windows_signature"]),
        (artifacts["linux_installer"], artifacts["linux_signature"]),
    ):
        if signature != payload.with_name(payload.name + ".sig"):
            raise RuntimeError("updater signature is not adjacent to its payload: {}".format(signature))
    return artifacts


def decode_tauri_updater_public_key(public_key: str) -> str:
    """Return the minisign RW... key embedded in a Tauri updater public-key value."""
    value = public_key.strip()
    if value.startswith("RW") and "\n" not in value:
        return value
    decoded = value
    try:
        decoded = base64.b64decode(value, validate=True).decode("utf-8")
    except (ValueError, UnicodeDecodeError):
        pass
    for line in decoded.splitlines():
        candidate = line.strip()
        if candidate.startswith("RW"):
            return candidate
    raise RuntimeError("IAC_CODE_DESKTOP_UPDATER_PUBKEY is not a valid Tauri/minisign public key")


def verify_updater_signatures(artifacts: dict[str, Path], public_key: str) -> None:
    if not public_key.strip():
        raise RuntimeError("IAC_CODE_DESKTOP_UPDATER_PUBKEY is required")
    minisign = shutil.which("minisign")
    if minisign is None:
        raise RuntimeError("minisign is required to verify Desktop updater artifacts")
    minisign_public_key = decode_tauri_updater_public_key(public_key)
    for payload, signature in (
        (artifacts["macos_updater"], artifacts["macos_signature"]),
        (artifacts["windows_updater"], artifacts["windows_signature"]),
        (artifacts["linux_installer"], artifacts["linux_signature"]),
    ):
        subprocess.run(
            [minisign, "-Vm", str(payload), "-x", str(signature), "-P", minisign_public_key],
            check=True,
        )


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def render_pre_notice(version: str) -> str:
    return """# iac-code Desktop {version} pre-build notice

These artifacts are unsigned pre-build inputs for the private release pipeline. They are not final stable assets.
The complete pre installers remain usable while commercial platform certificates are unavailable. Their updater
payloads are signed with the project's persistent Tauri updater key.

- Windows pre installer: no Authenticode publisher signature.
- macOS pre installer: ad-hoc signed, without Developer ID notarization.
- Linux pre packages: no platform publisher signature.

这是供内网发布流水线使用的 Desktop 预构建产物，不是正式稳定发布资产。完整 pre 安装包在商业签名证书
尚不可用时仍可使用；Windows 可能显示“未知发布者”，macOS 可能要求在“隐私与安全性”中放行。
""".format(version=version)


ASSET_METADATA = {
    "macos_installer": ("darwin-aarch64", "installer"),
    "macos_updater": ("darwin-aarch64", "updater"),
    "macos_signature": ("darwin-aarch64", "updater-signature"),
    "windows_installer": ("windows-x86_64", "installer"),
    "windows_updater": ("windows-x86_64", "updater"),
    "windows_signature": ("windows-x86_64", "updater-signature"),
    "linux_installer": ("linux-x86_64", "appimage"),
    "linux_signature": ("linux-x86_64", "updater-signature"),
    "linux_deb": ("linux-x86_64", "deb"),
    "sbom": ("all", "sbom"),
    "notices": ("all", "third-party-notices"),
}


def create_pre_manifest(
    input_dir: Path,
    output_dir: Path,
    *,
    repository: str,
    tag: str,
    version: str,
    commit: str,
    workflow_run_id: str,
    published_at: str,
) -> dict[str, object]:
    if not REPOSITORY_PATTERN.fullmatch(repository):
        raise RuntimeError("GitHub repository must use owner/name format")
    if tag != "v{}".format(version):
        raise RuntimeError("release tag and Desktop version do not match")
    if not re.fullmatch(r"[0-9a-f]{40}", commit):
        raise RuntimeError("Desktop pre-build commit must be a full lowercase Git SHA")
    if not str(workflow_run_id).isdigit():
        raise RuntimeError("Desktop pre-build workflow run ID must be numeric")
    if output_dir.exists() and any(output_dir.iterdir()):
        raise RuntimeError("pre manifest output directory must be empty: {}".format(output_dir))
    output_dir.mkdir(parents=True, exist_ok=True)
    artifacts = discover_release_artifacts(input_dir)
    assets = []
    for logical_name, source in sorted(artifacts.items()):
        platform_name, kind = ASSET_METADATA[logical_name]
        assets.append(
            {
                "logicalName": logical_name,
                "platform": platform_name,
                "kind": kind,
                "fileName": source.name,
                "relativePath": source.relative_to(input_dir).as_posix(),
                "size": source.stat().st_size,
                "sha256": _sha256(source),
            }
        )
    manifest: dict[str, object] = {
        "schemaVersion": 1,
        "kind": "iac-code-desktop-pre",
        "repository": repository,
        "tag": tag,
        "version": version,
        "commit": commit,
        "workflowRunId": str(workflow_run_id),
        "publishedAt": published_at,
        "assets": assets,
    }
    (output_dir / "desktop-pre-manifest.json").write_text(
        json.dumps(manifest, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    (output_dir / "DESKTOP_PRE_NOTICE.md").write_text(render_pre_notice(version), encoding="utf-8")
    (output_dir / "SHA256SUMS").write_text(
        "".join("{}  {}\n".format(asset["sha256"], asset["relativePath"]) for asset in assets),
        encoding="utf-8",
    )
    return manifest


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    subparsers = parser.add_subparsers(dest="command", required=True)
    validate = subparsers.add_parser("validate-tag")
    validate.add_argument("--tag", required=True)
    manifest = subparsers.add_parser("create-pre-manifest")
    manifest.add_argument("--input-dir", type=Path, required=True)
    manifest.add_argument("--output-dir", type=Path, required=True)
    manifest.add_argument("--repository", required=True)
    manifest.add_argument("--tag", required=True)
    manifest.add_argument("--version", required=True)
    manifest.add_argument("--commit", required=True)
    manifest.add_argument("--workflow-run-id", required=True)
    manifest.add_argument("--published-at", required=True)
    manifest.add_argument("--verify-signatures", action="store_true")
    return parser.parse_args()


def main() -> int:
    args = _parse_args()
    if args.command == "validate-tag":
        print(validate_release_tag(args.tag))
        return 0
    artifacts = discover_release_artifacts(args.input_dir)
    if args.verify_signatures:
        verify_updater_signatures(artifacts, os.environ.get("IAC_CODE_DESKTOP_UPDATER_PUBKEY", ""))
    create_pre_manifest(
        args.input_dir,
        args.output_dir,
        repository=args.repository,
        tag=args.tag,
        version=args.version,
        commit=args.commit,
        workflow_run_id=args.workflow_run_id,
        published_at=args.published_at,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
