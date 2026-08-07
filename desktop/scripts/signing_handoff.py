#!/usr/bin/env python3
"""Create and consume the signed-component handoff used between GitHub build stages."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import stat
import subprocess
import tempfile
import zipfile
from pathlib import Path, PurePosixPath
from typing import Any

from build_desktop import compose_overlay, npm_executable

ROOT = Path(__file__).resolve().parents[2]
TAURI = ROOT / "desktop/src-tauri"
TAG_PATTERN = re.compile(r"^v(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)$")
PLATFORMS = {
    "macos-aarch64": {
        "channel": "macos",
        "host": "desktop/src-tauri/target/release/iac-code-desktop",
        "helper": "desktop/dist/native-helpers/macos/release/iac-code-desktop-exec",
        "bundle": "app",
    },
    "windows-x64": {
        "channel": "windows",
        "host": "desktop/src-tauri/target/release/iac-code-desktop.exe",
        "helper": "desktop/dist/native-helpers/windows/release/iac-code-desktop-updater.exe",
        "bundle": "nsis",
    },
}


def _safe_relative_path(value: str) -> PurePosixPath:
    path = PurePosixPath(value)
    if path.is_absolute() or not path.parts or ".." in path.parts or "." in path.parts:
        raise RuntimeError("unsafe signing handoff path: {}".format(value))
    if path.parts[0] != "desktop":
        raise RuntimeError("signing handoff path is outside Desktop: {}".format(value))
    return path


def _selected_files(root: Path, platform_name: str) -> list[Path]:
    config = PLATFORMS[platform_name]
    required = [root / str(config["host"]), root / str(config["helper"])]
    if platform_name == "windows-x64":
        required.append(
            root / "desktop/dist/native-helpers/windows/release/iac-code-desktop-updater.manifest.json"
        )
    sidecar = root / "desktop/dist/sidecar/iac-code-sidecar"
    if not sidecar.is_dir():
        raise RuntimeError("Desktop signing input is missing the frozen sidecar")
    for path in required:
        if not path.is_file():
            raise RuntimeError("Desktop signing input is missing {}".format(path.relative_to(root)))
    return sorted([*required, *(path for path in sidecar.rglob("*") if path.is_file() or path.is_symlink())])


def _sign_targets(root: Path, platform_name: str, files: list[Path]) -> list[str]:
    names = {
        "iac-code-desktop",
        "iac-code-desktop.exe",
        "iac-code-desktop-exec",
        "iac-code-desktop-updater.exe",
        "iac-code-sidecar",
        "iac-code-sidecar.exe",
        "iac-code-tf2ros",
        "iac-code-tf2ros.exe",
    }
    return [path.relative_to(root).as_posix() for path in files if path.name in names]


def build_manifest(
    source_root: Path,
    *,
    repository: str,
    tag: str,
    version: str,
    commit: str,
    platform_name: str,
    stage: str,
    publisher: str = "",
) -> dict[str, Any]:
    if platform_name not in PLATFORMS:
        raise RuntimeError("unsupported signing handoff platform: {}".format(platform_name))
    if not TAG_PATTERN.fullmatch(tag) or version != tag[1:]:
        raise RuntimeError("signing handoff requires an exact vX.Y.Z tag")
    if not re.fullmatch(r"[0-9a-f]{40}", commit):
        raise RuntimeError("signing handoff requires a full lowercase Git SHA")
    if stage not in {"unsigned-components", "signed-components"}:
        raise RuntimeError("invalid signing handoff stage")
    if stage == "signed-components" and not publisher.strip():
        raise RuntimeError("signed component handoff requires publisher evidence")
    files = _selected_files(source_root, platform_name)
    return {
        "schemaVersion": 1,
        "kind": "iac-code-desktop-signing-handoff",
        "repository": repository,
        "tag": tag,
        "version": version,
        "commit": commit,
        "platform": platform_name,
        "stage": stage,
        "publisher": publisher.strip() or None,
        "signTargets": _sign_targets(source_root, platform_name, files),
        "files": [_file_record(source_root, path) for path in files],
    }


def _file_bytes(path: Path) -> bytes:
    return os.readlink(path).encode("utf-8") if path.is_symlink() else path.read_bytes()


def _file_record(root: Path, path: Path) -> dict[str, Any]:
    content = _file_bytes(path)
    return {
        "path": path.relative_to(root).as_posix(),
        "type": "symlink" if path.is_symlink() else "file",
        "size": len(content),
        "sha256": hashlib.sha256(content).hexdigest(),
        "mode": stat.S_IMODE(path.lstat().st_mode),
    }


def create_archive(source_root: Path, output: Path, manifest: dict[str, Any]) -> None:
    output.parent.mkdir(parents=True, exist_ok=True)
    if output.exists():
        output.unlink()
    entries = [("handoff-manifest.json", (json.dumps(manifest, indent=2, sort_keys=True) + "\n").encode())]
    entries.extend((item["path"], _file_bytes(source_root / item["path"])) for item in manifest["files"])
    records = {item["path"]: item for item in manifest["files"]}
    with zipfile.ZipFile(output, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=9) as archive:
        for name, content in entries:
            info = zipfile.ZipInfo(name, date_time=(1980, 1, 1, 0, 0, 0))
            info.compress_type = zipfile.ZIP_DEFLATED
            info.create_system = 3
            record = records.get(name, {"mode": 0o644, "type": "file"})
            kind = stat.S_IFLNK if record["type"] == "symlink" else stat.S_IFREG
            info.external_attr = ((int(record["mode"]) | kind) & 0xFFFF) << 16
            archive.writestr(info, content)


def read_archive_manifest(archive_path: Path) -> dict[str, Any]:
    with zipfile.ZipFile(archive_path) as archive:
        names = archive.namelist()
        if names.count("handoff-manifest.json") != 1 or len(names) != len(set(names)):
            raise RuntimeError("signing handoff archive has invalid or duplicate entries")
        manifest = json.loads(archive.read("handoff-manifest.json"))
    if not isinstance(manifest, dict) or manifest.get("schemaVersion") != 1:
        raise RuntimeError("signing handoff manifest schema is invalid")
    if manifest.get("kind") != "iac-code-desktop-signing-handoff":
        raise RuntimeError("signing handoff manifest kind is invalid")
    return manifest


def verify_archive(
    archive_path: Path,
    *,
    repository: str,
    tag: str,
    version: str,
    commit: str,
    platform_name: str,
    required_stage: str,
) -> dict[str, Any]:
    manifest = read_archive_manifest(archive_path)
    expected = {
        "repository": repository,
        "tag": tag,
        "version": version,
        "commit": commit,
        "platform": platform_name,
        "stage": required_stage,
    }
    mismatches = [name for name, value in expected.items() if manifest.get(name) != value]
    if mismatches:
        raise RuntimeError("signing handoff identity mismatch: {}".format(", ".join(mismatches)))
    files = manifest.get("files")
    if not isinstance(files, list) or not files:
        raise RuntimeError("signing handoff contains no files")
    manifest_paths = []
    with zipfile.ZipFile(archive_path) as archive:
        archive_names = set(archive.namelist())
        for item in files:
            if not isinstance(item, dict):
                raise RuntimeError("signing handoff file entry is invalid")
            relative = _safe_relative_path(str(item.get("path", "")))
            name = relative.as_posix()
            manifest_paths.append(name)
            if name not in archive_names:
                raise RuntimeError("signing handoff is missing {}".format(name))
            data = archive.read(name)
            if item.get("type") not in {"file", "symlink"}:
                raise RuntimeError("signing handoff file type is invalid: {}".format(name))
            if not isinstance(item.get("mode"), int) or not 0 <= item["mode"] <= 0o7777:
                raise RuntimeError("signing handoff file mode is invalid: {}".format(name))
            if len(data) != item.get("size") or hashlib.sha256(data).hexdigest() != item.get("sha256"):
                raise RuntimeError("signing handoff checksum mismatch: {}".format(name))
        if archive_names != {"handoff-manifest.json", *manifest_paths}:
            raise RuntimeError("signing handoff contains unmanifested files")
    if len(manifest_paths) != len(set(manifest_paths)):
        raise RuntimeError("signing handoff manifest contains duplicate file paths")
    sign_targets = manifest.get("signTargets")
    if not isinstance(sign_targets, list) or not set(sign_targets).issubset(manifest_paths):
        raise RuntimeError("signing handoff sign-target list is invalid")
    return manifest


def extract_archive(archive_path: Path, destination: Path, manifest: dict[str, Any]) -> None:
    destination.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(archive_path) as archive:
        for item in manifest["files"]:
            relative = _safe_relative_path(item["path"])
            target = destination.joinpath(*relative.parts)
            target.parent.mkdir(parents=True, exist_ok=True)
            content = archive.read(relative.as_posix())
            if item["type"] == "symlink":
                link_target = content.decode("utf-8")
                if Path(link_target).is_absolute() or ".." in Path(link_target).parts:
                    raise RuntimeError("unsafe signing handoff symlink: {}".format(relative))
                target.symlink_to(link_target)
            else:
                target.write_bytes(content)
                target.chmod(int(item["mode"]))


def bundle_signed_components(platform_name: str) -> None:
    config = PLATFORMS[platform_name]
    endpoint = os.environ.get("IAC_CODE_DESKTOP_UPDATER_ENDPOINT", "").strip()
    public_key = os.environ.get("IAC_CODE_DESKTOP_UPDATER_PUBKEY", "").strip()
    if not endpoint or not public_key:
        raise RuntimeError("signed packaging requires the Desktop updater endpoint and public key")
    channel = str(config["channel"])
    helper = "iac-code-desktop-updater" if channel == "windows" else "iac-code-desktop-exec"
    helper_suffix = ".exe" if channel == "windows" else ""
    resources = {
        "../dist/sidecar/iac-code-sidecar": "sidecar/iac-code-sidecar",
        "../dist/native-helpers/{}/release/{}{}".format(channel, helper, helper_suffix): "bin/{}{}".format(
            helper, helper_suffix
        ),
    }
    if channel == "windows":
        resources["icons/icon.ico"] = "icons/iac-code-logo-v3.ico"
        resources[
            "../dist/native-helpers/windows/release/iac-code-desktop-updater.manifest.json"
        ] = "bin/iac-code-desktop-updater.manifest.json"
    overlay_config = compose_overlay(
        channel,
        resources=resources,
        updater_endpoint=endpoint,
        updater_public_key=public_key,
        create_updater_artifacts=False,
    )
    bundle_dir = TAURI / "target/release/bundle"
    if bundle_dir.exists():
        shutil.rmtree(bundle_dir)
    with tempfile.NamedTemporaryFile(
        mode="w", encoding="utf-8", suffix=".json", prefix="tauri.signed-bundle.", dir=TAURI
    ) as overlay:
        json.dump(overlay_config, overlay)
        overlay.flush()
        command = [
            npm_executable(),
            "run",
            "tauri",
            "--",
            "bundle",
            "--config",
            overlay.name,
            "--features",
            "updater",
            "--no-sign",
        ]
        if config["bundle"] == "app":
            command.extend(["--bundles", "app"])
        subprocess.run(
            command,
            cwd=ROOT / "desktop",
            env={**os.environ, "IAC_CODE_DESKTOP_CHANNEL": channel},
            check=True,
        )


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    subparsers = parser.add_subparsers(dest="command", required=True)
    export = subparsers.add_parser("export")
    export.add_argument("--output", type=Path, required=True)
    export.add_argument("--repository", required=True)
    export.add_argument("--tag", required=True)
    export.add_argument("--version", required=True)
    export.add_argument("--commit", required=True)
    export.add_argument("--platform", choices=sorted(PLATFORMS), required=True)
    consume = subparsers.add_parser("consume")
    consume.add_argument("--archive", type=Path, required=True)
    consume.add_argument("--repository", required=True)
    consume.add_argument("--tag", required=True)
    consume.add_argument("--version", required=True)
    consume.add_argument("--commit", required=True)
    consume.add_argument("--platform", choices=sorted(PLATFORMS), required=True)
    bundle = subparsers.add_parser("bundle")
    bundle.add_argument("--platform", choices=sorted(PLATFORMS), required=True)
    return parser.parse_args()


def main() -> int:
    args = _parse_args()
    if args.command == "bundle":
        bundle_signed_components(args.platform)
        return 0
    if args.command == "export":
        manifest = build_manifest(
            ROOT,
            repository=args.repository,
            tag=args.tag,
            version=args.version,
            commit=args.commit,
            platform_name=args.platform,
            stage="unsigned-components",
        )
        create_archive(ROOT, args.output, manifest)
        return 0
    manifest = verify_archive(
        args.archive,
        repository=args.repository,
        tag=args.tag,
        version=args.version,
        commit=args.commit,
        platform_name=args.platform,
        required_stage="signed-components",
    )
    extract_archive(args.archive, ROOT, manifest)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
