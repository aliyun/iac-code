#!/usr/bin/env python3
"""Build one platform/channel-specific Desktop bundle."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import platform
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[2]
DESKTOP = ROOT / "desktop"
TAURI = DESKTOP / "src-tauri"
HELPERS = DESKTOP / "helpers"
FLAVORS = DESKTOP / "flavors"
WINDOWS_UPDATER_MANIFEST = "iac-code-desktop-updater.manifest.json"
WINDOWS_AUTHENTICODE_EVIDENCE_SCRIPT = """
$signature = Get-AuthenticodeSignature -LiteralPath $env:IAC_CODE_VERIFY_HELPER_PATH
$publisher = if ($signature.SignerCertificate) {
  $signature.SignerCertificate.GetNameInfo(
    [System.Security.Cryptography.X509Certificates.X509NameType]::SimpleName,
    $false
  )
} else { $null }
@{ status = [string]$signature.Status; publisher = $publisher } | ConvertTo-Json -Compress
""".strip()


def _default_channel() -> str:
    return {"Darwin": "macos", "Windows": "windows"}.get(platform.system(), "appimage")


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--channel", choices=("macos", "windows", "appimage", "deb"), default=_default_channel())
    parser.add_argument("--skip-sidecar", action="store_true")
    return parser.parse_args()


def _run(command: list[str], *, cwd: Path, environment: dict[str, str]) -> None:
    subprocess.run(command, cwd=cwd, env=environment, check=True)


def npm_executable(system: str | None = None) -> str:
    """Return the directly executable npm launcher for the host platform."""
    return "npm.cmd" if (system or platform.system()) == "Windows" else "npm"


def compose_overlay(
    channel: str,
    *,
    resources: dict[str, str],
    updater_endpoint: str | None = None,
    updater_public_key: str | None = None,
    create_updater_artifacts: bool = True,
) -> dict[str, Any]:
    # Keep flavor overlays outside src-tauri so Tauri cannot auto-load a
    # platform-named config in addition to the composed temporary overlay.
    flavor_path = FLAVORS / "{}.json".format(channel)
    overlay_config = json.loads(flavor_path.read_text(encoding="utf-8"))
    overlay_config["bundle"]["resources"] = resources
    updater_configured = bool(updater_endpoint and updater_public_key) and channel != "deb"
    overlay_config["app"]["security"]["capabilities"] = [
        "desktop-local",
        "desktop-loopback-updater" if updater_configured else "desktop-loopback-external",
    ]
    if updater_configured:
        overlay_config["bundle"]["createUpdaterArtifacts"] = create_updater_artifacts
        overlay_config["plugins"] = {
            "updater": {"endpoints": [updater_endpoint], "pubkey": updater_public_key}
        }
    return overlay_config


def clear_stale_host_helpers() -> None:
    """Keep Tauri from discovering helper artifacts left by earlier Cargo jobs."""
    release_dir = TAURI / "target/release"
    for name in ("iac-code-desktop-exec", "iac-code-desktop-updater"):
        for suffix in ("", ".exe", ".d"):
            (release_dir / "{}{}".format(name, suffix)).unlink(missing_ok=True)


def configure_updater_signing_environment(environment: dict[str, str]) -> None:
    """Resolve a signing-key path without placing key material in argv or config."""
    if environment.get("TAURI_SIGNING_PRIVATE_KEY"):
        return
    key_path = environment.get("TAURI_SIGNING_PRIVATE_KEY_PATH")
    if not key_path:
        return
    key = Path(key_path).expanduser().read_text(encoding="utf-8").strip()
    if not key:
        raise SystemExit("Desktop updater signing private key file is empty")
    environment["TAURI_SIGNING_PRIVATE_KEY"] = key


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(128 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def create_windows_updater_manifest(
    helper: Path,
    *,
    release: bool,
    expected_publisher: str | None,
) -> dict[str, Any]:
    publisher = expected_publisher.strip() if expected_publisher else None
    if release and not publisher:
        raise SystemExit("Windows release builds require IAC_CODE_WINDOWS_SIGNING_PUBLISHER")
    if not release and publisher:
        raise SystemExit("Windows signing publisher is only valid for a release helper")
    return {
        "schemaVersion": 1,
        "fileName": "iac-code-desktop-updater.exe",
        "sha256": _sha256(helper),
        "authenticodeRequired": release,
        "expectedPublisher": publisher,
    }


def verify_windows_updater_manifest(helper: Path, manifest: dict[str, Any]) -> None:
    if manifest.get("schemaVersion") != 1:
        raise SystemExit("Windows updater helper manifest schema is invalid")
    if manifest.get("fileName") != "iac-code-desktop-updater.exe":
        raise SystemExit("Windows updater helper manifest file name is invalid")
    if manifest.get("sha256") != _sha256(helper):
        raise SystemExit("Windows updater helper does not match its build manifest")


def verify_windows_release_helper(helper: Path, expected_publisher: str) -> None:
    """Fail closed unless PowerShell validates the staged release helper signer."""
    environment = dict(os.environ)
    environment["IAC_CODE_VERIFY_HELPER_PATH"] = str(helper)
    result = subprocess.run(
        [
            "powershell.exe",
            "-NoProfile",
            "-NonInteractive",
            "-Command",
            WINDOWS_AUTHENTICODE_EVIDENCE_SCRIPT,
        ],
        check=True,
        capture_output=True,
        text=True,
        encoding="utf-8",
        env=environment,
    )
    evidence = json.loads(result.stdout)
    if evidence.get("status") != "Valid":
        raise SystemExit("signed Windows updater helper failed Authenticode validation")
    if evidence.get("publisher") != expected_publisher:
        raise SystemExit("signed Windows updater helper publisher does not match release configuration")


def main() -> int:
    args = _parse_args()
    expected_system = {"macos": "Darwin", "windows": "Windows", "appimage": "Linux", "deb": "Linux"}[
        args.channel
    ]
    if platform.system() != expected_system:
        raise SystemExit("Desktop bundles are native builds; {} requires {}".format(args.channel, expected_system))
    if sys.version_info[:2] != (3, 12):
        raise SystemExit("release sidecar builds require CPython 3.12.x")
    endpoint = os.environ.get("IAC_CODE_DESKTOP_UPDATER_ENDPOINT")
    public_key = os.environ.get("IAC_CODE_DESKTOP_UPDATER_PUBKEY")
    if bool(endpoint) != bool(public_key):
        raise SystemExit("Desktop updater endpoint and public key must be configured together")
    updater_configured = bool(endpoint and public_key) and args.channel != "deb"
    if os.environ.get("IAC_CODE_DESKTOP_RELEASE") == "1" and args.channel != "deb" and not updater_configured:
        raise SystemExit("release updater builds require IAC_CODE_DESKTOP_UPDATER_ENDPOINT and PUBKEY")
    environment = dict(os.environ)
    environment["IAC_CODE_DESKTOP_CHANNEL"] = args.channel
    environment["IAC_CODE_DESKTOP_UPDATER_CONFIGURED"] = "1" if updater_configured else "0"
    if updater_configured:
        configure_updater_signing_environment(environment)
    if os.environ.get("IAC_CODE_DESKTOP_RELEASE") == "1" and updater_configured and not environment.get(
        "TAURI_SIGNING_PRIVATE_KEY"
    ):
        raise SystemExit("release updater builds require a Tauri updater signing private key")
    if not args.skip_sidecar:
        _run([sys.executable, str(DESKTOP / "scripts/build_sidecar.py")], cwd=ROOT, environment=environment)

    helper = "iac-code-desktop-updater" if args.channel == "windows" else "iac-code-desktop-exec"
    host_features = ["updater"] if updater_configured else []
    helper_target = DESKTOP / "dist/native-helpers" / args.channel
    helper_environment = {**environment, "CARGO_TARGET_DIR": str(helper_target)}
    _run(
        ["cargo", "build", "--release", "--bin", helper],
        cwd=HELPERS,
        environment=helper_environment,
    )
    helper_suffix = ".exe" if args.channel == "windows" else ""
    helper_binary = helper_target / "release" / "{}{}".format(helper, helper_suffix)
    if args.channel == "windows":
        signed_stable_release = os.environ.get("IAC_CODE_DESKTOP_STABLE_SIGNED_RELEASE") == "1"
        if signed_stable_release and os.environ.get("IAC_CODE_DESKTOP_RELEASE") != "1":
            raise SystemExit("a stable signed Windows build must also set IAC_CODE_DESKTOP_RELEASE=1")
        expected_publisher = os.environ.get("IAC_CODE_WINDOWS_SIGNING_PUBLISHER")
        signed_helper = os.environ.get("IAC_CODE_DESKTOP_SIGNED_UPDATER_HELPER")
        if signed_stable_release:
            if not signed_helper:
                raise SystemExit("stable signed Windows builds require IAC_CODE_DESKTOP_SIGNED_UPDATER_HELPER")
            if not expected_publisher or not expected_publisher.strip():
                raise SystemExit("stable signed Windows builds require IAC_CODE_WINDOWS_SIGNING_PUBLISHER")
            signed_helper_path = Path(signed_helper).expanduser().resolve(strict=True)
            if signed_helper_path != helper_binary.resolve():
                shutil.copy2(signed_helper_path, helper_binary)
            verify_windows_release_helper(helper_binary, expected_publisher.strip())
        elif signed_helper:
            raise SystemExit("a pre-signed updater helper is only accepted for release builds")
        manifest = create_windows_updater_manifest(
            helper_binary,
            release=signed_stable_release,
            expected_publisher=expected_publisher,
        )
        verify_windows_updater_manifest(helper_binary, manifest)
        manifest_path = helper_target / "release" / WINDOWS_UPDATER_MANIFEST
        manifest_path.write_text(json.dumps(manifest, sort_keys=True) + "\n", encoding="utf-8")
        environment["IAC_CODE_DESKTOP_UPDATER_HELPER_SHA256"] = manifest["sha256"]
        environment["IAC_CODE_DESKTOP_UPDATER_HELPER_AUTHENTICODE_REQUIRED"] = (
            "1" if manifest["authenticodeRequired"] else "0"
        )
        environment["IAC_CODE_DESKTOP_UPDATER_HELPER_PUBLISHER"] = manifest["expectedPublisher"] or ""
    clear_stale_host_helpers()
    resources = {
        "../dist/sidecar/iac-code-sidecar": "sidecar/iac-code-sidecar",
        "../dist/native-helpers/{}/release/{}{}".format(
            args.channel,
            helper,
            helper_suffix,
        ): "bin/{}{}".format(helper, helper_suffix),
    }
    if args.channel == "windows":
        resources["icons/icon.ico"] = "icons/iac-code-logo-v3.ico"
        resources[
            "../dist/native-helpers/windows/release/{}".format(WINDOWS_UPDATER_MANIFEST)
        ] = "bin/{}".format(WINDOWS_UPDATER_MANIFEST)
    overlay_config = compose_overlay(
        args.channel,
        resources=resources,
        updater_endpoint=endpoint,
        updater_public_key=public_key,
    )
    with tempfile.NamedTemporaryFile(
        mode="w",
        encoding="utf-8",
        suffix=".json",
        prefix="tauri.build.",
        dir=TAURI,
    ) as overlay:
        json.dump(overlay_config, overlay)
        overlay.flush()
        feature_arguments = ["--features", ",".join(host_features)] if host_features else []
        _run(
            [npm_executable(), "run", "tauri", "--", "build", "--config", overlay.name, *feature_arguments],
            cwd=DESKTOP,
            environment=environment,
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
