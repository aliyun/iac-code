#!/usr/bin/env python3
"""Fail-closed verification for Desktop release metadata and signed artifacts."""

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
from typing import Any, Iterable

METADATA_FILES = ("desktop-sbom.cdx.json", "THIRD_PARTY_NOTICES.txt")
PRIVACY_PLACEHOLDER = re.compile(r"\{\{[A-Z0-9_]+\}\}")
PRIVATE_FILE_SUFFIXES = {".key", ".pem", ".p12", ".pfx", ".jks", ".keystore"}
PRIVATE_CONTENT_MARKERS = (
    b"-----BEGIN PRIVATE KEY-----",
    b"-----BEGIN RSA PRIVATE KEY-----",
    b"-----BEGIN EC PRIVATE KEY-----",
    b"TAURI_SIGNING_PRIVATE_KEY=",
    b"TAURI_SIGNING_PRIVATE_KEY_PASSWORD=",
)
BUILD_PATH_PATTERNS = (
    re.compile(rb"/Users/[^/\x00\r\n]+/"),
    re.compile(rb"/home/(?:runner|[^/\x00\r\n]+)/"),
    re.compile(rb"[A-Za-z]:\\Users\\[^\\\x00\r\n]+\\"),
)


def _run(command: list[str], *, environment: dict[str, str] | None = None) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        command,
        check=True,
        capture_output=True,
        text=True,
        encoding="utf-8",
        env=environment,
    )


def verify_metadata(metadata_dir: Path) -> dict[str, Any]:
    for name in METADATA_FILES:
        path = metadata_dir / name
        if not path.is_file() or path.stat().st_size == 0:
            raise RuntimeError("Desktop release metadata is missing or empty: {}".format(path))
    sbom = json.loads((metadata_dir / "desktop-sbom.cdx.json").read_text(encoding="utf-8"))
    if sbom.get("bomFormat") != "CycloneDX" or sbom.get("specVersion") != "1.5":
        raise RuntimeError("Desktop SBOM is not CycloneDX 1.5")
    components = sbom.get("components")
    if not isinstance(components, list) or not components:
        raise RuntimeError("Desktop SBOM has no components")
    purls = [component.get("purl") for component in components]
    if any(not isinstance(purl, str) or not purl.startswith("pkg:") for purl in purls):
        raise RuntimeError("Desktop SBOM contains an invalid component PURL")
    if purls != sorted(purls) or len(purls) != len(set(purls)):
        raise RuntimeError("Desktop SBOM components must be uniquely sorted by PURL")
    for component in components:
        licenses = component.get("licenses")
        if not licenses or not isinstance(licenses, list):
            raise RuntimeError("Desktop SBOM component has no license evidence: {}".format(component.get("purl")))
        license_value = licenses[0].get("license", {})
        if not any(str(license_value.get(key, "")).strip() for key in ("id", "name", "expression")):
            raise RuntimeError("Desktop SBOM component has an empty license: {}".format(component.get("purl")))
    notices = (metadata_dir / "THIRD_PARTY_NOTICES.txt").read_text(encoding="utf-8")
    missing = [purl for purl in purls if purl not in notices]
    if missing:
        raise RuntimeError("Desktop notices omit SBOM components: {}".format(", ".join(missing[:10])))
    return sbom


def verify_privacy_notice(path: Path) -> None:
    if not path.is_file() or path.stat().st_size == 0:
        raise RuntimeError("rendered Desktop privacy notice is missing")
    text = path.read_text(encoding="utf-8")
    unresolved = sorted(set(PRIVACY_PLACEHOLDER.findall(text)))
    if unresolved:
        raise RuntimeError("rendered Desktop privacy notice has unresolved fields: {}".format(", ".join(unresolved)))
    for heading in ("Data stored on the device", "External services", "Telemetry and diagnostics", "Credentials"):
        if heading not in text:
            raise RuntimeError("rendered Desktop privacy notice is missing section: {}".format(heading))


def _iter_files(root: Path) -> Iterable[Path]:
    return sorted((path for path in root.rglob("*") if path.is_file()), key=lambda path: str(path))


def _scan_artifact_file(path: Path) -> tuple[bool, bool]:
    private_material = False
    build_path = False
    overlap = 512
    tail = b""
    with path.open("rb") as stream:
        while True:
            chunk = stream.read(1024 * 1024)
            if not chunk:
                break
            data = tail + chunk
            private_material = private_material or any(marker in data for marker in PRIVATE_CONTENT_MARKERS)
            build_path = build_path or any(pattern.search(data) for pattern in BUILD_PATH_PATTERNS)
            if private_material and build_path:
                break
            tail = data[-overlap:]
    return private_material, build_path


def verify_no_private_material_or_build_paths(artifact_dir: Path) -> None:
    problems: list[str] = []
    for path in _iter_files(artifact_dir):
        if path.suffix.lower() in PRIVATE_FILE_SUFFIXES:
            problems.append("private-key file name: {}".format(path.relative_to(artifact_dir)))
            continue
        try:
            private_material, build_path = _scan_artifact_file(path)
        except OSError as error:
            problems.append("unreadable artifact {}: {}".format(path.relative_to(artifact_dir), error))
            continue
        if private_material:
            problems.append("private-key material: {}".format(path.relative_to(artifact_dir)))
        if build_path:
            problems.append("personal/CI build path: {}".format(path.relative_to(artifact_dir)))
    if problems:
        raise RuntimeError("Desktop release artifact inspection failed:\n- " + "\n- ".join(problems[:30]))


def _require_command(name: str) -> str:
    command = shutil.which(name)
    if not command:
        raise RuntimeError("required Desktop release verifier is unavailable: {}".format(name))
    return command


def verify_macos(artifact_dir: Path) -> None:
    codesign = _require_command("codesign")
    spctl = _require_command("spctl")
    xcrun = _require_command("xcrun")
    apps = sorted(artifact_dir.rglob("*.app"))
    disk_images = sorted(artifact_dir.rglob("*.dmg"))
    if not apps or not disk_images:
        raise RuntimeError("macOS release verification requires both .app and .dmg artifacts")
    expected_team = os.environ.get("IAC_CODE_APPLE_TEAM_ID", "").strip()
    if not expected_team:
        raise RuntimeError("macOS stable verification requires IAC_CODE_APPLE_TEAM_ID")
    for app in apps:
        _run([codesign, "--verify", "--strict", "--verbose=2", str(app)])
        details = _run([codesign, "-d", "--verbose=4", str(app)])
        evidence = details.stdout + details.stderr
        if "TeamIdentifier={}".format(expected_team) not in evidence:
            raise RuntimeError("macOS app TeamIdentifier does not match the release configuration")
        _run([spctl, "--assess", "--type", "execute", "--verbose=4", str(app)])
        _run([xcrun, "stapler", "validate", str(app)])
    for disk_image in disk_images:
        _run([xcrun, "stapler", "validate", str(disk_image)])


WINDOWS_SIGNATURE_SCRIPT = r"""
$result = @()
foreach ($path in $env:IAC_CODE_WINDOWS_VERIFY_FILES -split "\|") {
  $signature = Get-AuthenticodeSignature -LiteralPath $path
  $publisher = if ($signature.SignerCertificate) {
    $signature.SignerCertificate.GetNameInfo(
      [System.Security.Cryptography.X509Certificates.X509NameType]::SimpleName,
      $false
    )
  } else { $null }
  $result += @{ path = $path; status = [string]$signature.Status; publisher = $publisher }
}
$result | ConvertTo-Json -Compress
""".strip()


def verify_windows(artifact_dir: Path) -> None:
    powershell = _require_command("powershell.exe")
    expected_publisher = os.environ.get("IAC_CODE_WINDOWS_SIGNING_PUBLISHER", "").strip()
    if not expected_publisher:
        raise RuntimeError("Windows stable verification requires IAC_CODE_WINDOWS_SIGNING_PUBLISHER")
    candidates = [
        path
        for path in _iter_files(artifact_dir)
        if path.suffix.lower() in {".exe", ".dll"}
        and (path.name.lower().startswith("iac-code") or "nsis" in {part.lower() for part in path.parts})
    ]
    if not candidates:
        raise RuntimeError("Windows release verification found no first-party PE or NSIS installer")
    environment = dict(os.environ)
    environment["IAC_CODE_WINDOWS_VERIFY_FILES"] = "|".join(str(path.resolve()) for path in candidates)
    result = _run(
        [powershell, "-NoProfile", "-NonInteractive", "-Command", WINDOWS_SIGNATURE_SCRIPT],
        environment=environment,
    )
    evidence = json.loads(result.stdout)
    if isinstance(evidence, dict):
        evidence = [evidence]
    failures = [
        item
        for item in evidence
        if item.get("status") != "Valid" or item.get("publisher") != expected_publisher
    ]
    if failures:
        raise RuntimeError("Windows Authenticode/publisher verification failed: {}".format(failures))


def _parse_checksums(path: Path) -> dict[str, str]:
    checksums: dict[str, str] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        digest, separator, name = line.partition("  ")
        if not separator or len(digest) != 64 or not all(value in "0123456789abcdef" for value in digest.lower()):
            raise RuntimeError("invalid SHA256SUMS line: {}".format(line))
        if Path(name).is_absolute() or ".." in Path(name).parts:
            raise RuntimeError("unsafe SHA256SUMS path: {}".format(name))
        checksums[name] = digest.lower()
    if not checksums:
        raise RuntimeError("SHA256SUMS is empty")
    return checksums


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def verify_linux(artifact_dir: Path) -> None:
    checksum_path = artifact_dir / "SHA256SUMS"
    if not checksum_path.is_file():
        raise RuntimeError("Linux stable release requires SHA256SUMS")
    for name, expected in _parse_checksums(checksum_path).items():
        artifact = artifact_dir / name
        if not artifact.is_file():
            raise RuntimeError("SHA256SUMS references a missing artifact: {}".format(name))
        digest = _sha256_file(artifact)
        if digest != expected:
            raise RuntimeError("Linux artifact checksum mismatch: {}".format(name))
    mode = os.environ.get("IAC_CODE_LINUX_SIGNATURE_MODE", "").strip().lower()
    if mode == "gpg":
        signature = artifact_dir / "SHA256SUMS.asc"
        if not signature.is_file():
            raise RuntimeError("GPG release verification requires SHA256SUMS.asc")
        _run([_require_command("gpg"), "--batch", "--verify", str(signature), str(checksum_path)])
    elif mode == "sigstore":
        bundle = artifact_dir / "SHA256SUMS.sigstore.json"
        identity = os.environ.get("IAC_CODE_SIGSTORE_CERTIFICATE_IDENTITY", "").strip()
        issuer = os.environ.get("IAC_CODE_SIGSTORE_OIDC_ISSUER", "").strip()
        if not bundle.is_file() or not identity or not issuer:
            raise RuntimeError("Sigstore release verification requires bundle, certificate identity, and OIDC issuer")
        _run(
            [
                _require_command("cosign"),
                "verify-blob",
                "--bundle",
                str(bundle),
                "--certificate-identity",
                identity,
                "--certificate-oidc-issuer",
                issuer,
                str(checksum_path),
            ]
        )
    else:
        raise RuntimeError("Linux stable verification requires IAC_CODE_LINUX_SIGNATURE_MODE=gpg|sigstore")


def verify_updater_signature(artifact_dir: Path, channel: str) -> None:
    if channel == "deb":
        return
    signatures = [path for path in _iter_files(artifact_dir) if path.name.endswith(".sig")]
    if not signatures:
        raise RuntimeError("updater-enabled Desktop release has no Tauri updater .sig artifact")
    public_key = os.environ.get("IAC_CODE_DESKTOP_UPDATER_PUBKEY", "").strip()
    if not public_key:
        raise RuntimeError("updater signature verification requires IAC_CODE_DESKTOP_UPDATER_PUBKEY")
    public_key = decode_tauri_updater_public_key(public_key)
    minisign = _require_command("minisign")
    for signature in signatures:
        payload = signature.with_name(signature.name.removesuffix(".sig"))
        if not payload.is_file():
            raise RuntimeError("Tauri updater signature has no matching payload: {}".format(signature.name))
        _run([minisign, "-Vm", str(payload), "-x", str(signature), "-P", public_key])


def decode_tauri_updater_public_key(public_key: str) -> str:
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
    raise RuntimeError("updater public key is not a valid Tauri/minisign public key")


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--metadata-dir", type=Path, required=True)
    parser.add_argument("--metadata-only", action="store_true")
    parser.add_argument("--strict", action="store_true")
    parser.add_argument("--artifact-dir", type=Path)
    parser.add_argument("--privacy-notice", type=Path)
    parser.add_argument("--channel", choices=("macos", "windows", "appimage", "deb"))
    return parser.parse_args()


def main() -> int:
    args = _parse_args()
    verify_metadata(args.metadata_dir)
    if args.metadata_only:
        if args.strict:
            raise RuntimeError("--metadata-only cannot satisfy a strict stable release gate")
        return 0
    if not args.strict:
        raise RuntimeError("artifact verification must use --strict")
    if not args.artifact_dir or not args.privacy_notice or not args.channel:
        raise RuntimeError("strict verification requires --artifact-dir, --privacy-notice, and --channel")
    verify_privacy_notice(args.privacy_notice)
    verify_no_private_material_or_build_paths(args.artifact_dir)
    if args.channel == "macos":
        verify_macos(args.artifact_dir)
    elif args.channel == "windows":
        verify_windows(args.artifact_dir)
    else:
        verify_linux(args.artifact_dir)
    verify_updater_signature(args.artifact_dir, args.channel)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
