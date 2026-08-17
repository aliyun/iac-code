#!/usr/bin/env python3
"""Build one native CPython 3.12 runtime for the external iac-code Skill."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import platform
import re
import shutil
import socket
import subprocess
import sys
import tarfile
import tempfile
import time
import urllib.error
import urllib.request
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from desktop.scripts.build_sidecar import (  # noqa: E402, I001
    prepare_staging,
    sanitize_frozen_metadata,
    validate_frozen_bundle,
)

SPEC = ROOT / "skill-runtime/iac-code-runtime.spec"
VERSION_FILE = ROOT / "src/iac_code/__init__.py"
PUBLIC_ORIGIN = "https://ros-public-tools.oss-cn-beijing.aliyuncs.com"
PRODUCT_PREFIX = "github-releases/aliyun/iac-code"
RUNTIME_PYTHON = "cp312"
_TAG_PATTERN = re.compile(r"v(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)")
_CANDIDATE_PATTERN = re.compile(r"candidate-[0-9]{8}T[0-9]{6}Z-([0-9a-f]{12})")
_COMMIT_PATTERN = re.compile(r"[0-9a-f]{40}")
_RFC3339_PATTERN = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}Z")


def iac_code_version() -> str:
    for line in VERSION_FILE.read_text(encoding="utf-8").splitlines():
        if line.startswith("__version__"):
            return line.split("=", 1)[1].strip().strip('"')
    raise RuntimeError("iac-code version is missing")


def normalized_host_target() -> tuple[str, str, str]:
    system = platform.system().lower()
    machine = platform.machine().lower()
    os_name = {"darwin": "darwin", "linux": "linux", "windows": "windows"}.get(system)
    arch = {"arm64": "arm64", "aarch64": "arm64", "amd64": "x86_64", "x86_64": "x86_64"}.get(machine)
    abi = {"darwin": "macos", "linux": "gnu", "windows": "msvc"}.get(system)
    if not os_name or not arch or not abi:
        raise RuntimeError("unsupported native Skill runtime build host")
    return os_name, arch, abi


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _release_identity(args: argparse.Namespace, version: str) -> dict[str, str]:
    if _COMMIT_PATTERN.fullmatch(args.source_commit) is None:
        raise SystemExit("--source-commit must be a lowercase 40-character Git commit")
    if _COMMIT_PATTERN.fullmatch(args.publisher_commit) is None:
        raise SystemExit("--publisher-commit must be a lowercase 40-character Git commit")
    if _RFC3339_PATTERN.fullmatch(args.published_at) is None:
        raise SystemExit("--published-at must use UTC RFC3339 form YYYY-MM-DDTHH:MM:SSZ")
    if args.release_date != args.published_at[:10]:
        raise SystemExit("--release-date must match the date in --published-at")
    if args.runtime_tag:
        match = _TAG_PATTERN.fullmatch(args.runtime_tag)
        if match is None or args.runtime_tag != "v{}".format(version):
            raise SystemExit("--runtime-tag must be the canonical tag for the source iac-code version")
        return {
            "releaseKind": "release",
            "runtimeTag": args.runtime_tag,
            "sourceCommit": args.source_commit,
            "publisherCommit": args.publisher_commit,
            "publishedAt": args.published_at,
        }
    match = _CANDIDATE_PATTERN.fullmatch(args.candidate_id or "")
    if match is None or match.group(1) != args.source_commit[:12]:
        raise SystemExit("--candidate-id must use candidate-YYYYMMDDTHHMMSSZ-<source commit prefix>")
    return {
        "releaseKind": "candidate",
        "candidateId": str(args.candidate_id),
        "sourceCommit": args.source_commit,
        "publisherCommit": args.publisher_commit,
        "publishedAt": args.published_at,
    }


def runtime_public_root(identity: dict[str, str]) -> str:
    if identity["releaseKind"] == "release":
        suffix = "skill-runtime/releases/{}".format(identity["runtimeTag"])
    else:
        suffix = "skill-runtime/candidates/{}".format(identity["candidateId"])
    return "/".join((PUBLIC_ORIGIN, PRODUCT_PREFIX, suffix))


def write_runtime_version(bundle: Path, *, version: str, identity: dict[str, str]) -> None:
    value = {
        "schemaVersion": 1,
        "iacCodeVersion": version,
        "runtimePython": RUNTIME_PYTHON,
        "sourceCommit": identity["sourceCommit"],
        "publisherCommit": identity["publisherCommit"],
    }
    if identity["releaseKind"] == "release":
        value["runtimeTag"] = identity["runtimeTag"]
    else:
        value["candidateId"] = identity["candidateId"]
    (bundle / "runtime-version.json").write_text(
        json.dumps(value, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )


def archive_bundle(bundle: Path, output: Path, archive_type: str) -> None:
    output.parent.mkdir(parents=True, exist_ok=True)
    if archive_type == "zip":
        with zipfile.ZipFile(output, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=9) as archive:
            for source in sorted(bundle.rglob("*")):
                if source.is_file():
                    archive.write(source, Path("iac-code-runtime") / source.relative_to(bundle))
        return
    with tarfile.open(output, "w:gz") as archive:
        archive.add(bundle, arcname="iac-code-runtime", recursive=True)


def smoke_test_a2a(executable: Path, expected_version: str) -> dict[str, object]:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
        listener.bind(("127.0.0.1", 0))
        port = int(listener.getsockname()[1])
    with tempfile.TemporaryDirectory(prefix="iac-code-skill-smoke-") as temporary:
        environment = {
            **os.environ,
            "IAC_CODE_CONFIG_DIR": temporary,
            "IAC_CODE_TELEMETRY_ENABLED": "false",
        }
        process = subprocess.Popen(
            [
                str(executable),
                "a2a",
                "--transport",
                "http",
                "--host",
                "127.0.0.1",
                "--port",
                str(port),
            ],
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            encoding="utf-8",
            errors="replace",
            env=environment,
        )
        deadline = time.monotonic() + 30
        health: dict[str, object] | None = None
        card: dict[str, object] | None = None
        try:
            while time.monotonic() < deadline:
                if process.poll() is not None:
                    output = process.communicate()[0][-4000:]
                    raise RuntimeError("frozen Skill runtime A2A server exited during smoke test: " + output)
                try:
                    with urllib.request.urlopen("http://127.0.0.1:{}/health".format(port), timeout=1) as response:
                        health_value = json.loads(response.read().decode("utf-8"))
                    with urllib.request.urlopen(
                        "http://127.0.0.1:{}/.well-known/agent-card.json".format(port), timeout=1
                    ) as response:
                        card_value = json.loads(response.read().decode("utf-8"))
                    if isinstance(health_value, dict) and isinstance(card_value, dict):
                        health = health_value
                        card = card_value
                        break
                except (OSError, urllib.error.URLError, json.JSONDecodeError):
                    time.sleep(0.25)
            if (
                health is None
                or health.get("status") != "healthy"
                or health.get("version") != expected_version
                or not card
            ):
                raise RuntimeError("frozen Skill runtime A2A health/Agent Card smoke test timed out")
            return {"health": "healthy", "agentCard": True}
        finally:
            if process.poll() is None:
                process.terminate()
                try:
                    process.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=5)
            if process.stdout is not None:
                process.stdout.close()


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, default=ROOT / "skill-runtime/dist")
    parser.add_argument("--staging", type=Path)
    identity = parser.add_mutually_exclusive_group(required=True)
    identity.add_argument("--runtime-tag")
    identity.add_argument("--candidate-id")
    parser.add_argument("--source-commit", required=True)
    parser.add_argument("--publisher-commit", required=True)
    parser.add_argument("--published-at", required=True)
    parser.add_argument("--release-date", required=True)
    parser.add_argument("--min-os-version")
    parser.add_argument("--glibc-min-version")
    parser.add_argument("--skip-tokenizers", action="store_true")
    parser.add_argument("--prepare-only", action="store_true")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    if sys.version_info[:2] != (3, 12):
        raise SystemExit("Skill runtime builds require CPython 3.12")
    os_name, arch, native_abi = normalized_host_target()
    if os_name == "linux" and not args.glibc_min_version:
        raise SystemExit("Linux builds require --glibc-min-version from the native build baseline")
    if os_name != "linux" and not args.min_os_version:
        raise SystemExit("macOS and Windows builds require --min-os-version from the native build baseline")
    version = iac_code_version()
    identity = _release_identity(args, version)
    target = "{}-{}-{}-{}".format(os_name, arch, native_abi, RUNTIME_PYTHON)
    archive_type = "zip" if os_name == "windows" else "tar.gz"
    archive_name = target + (".zip" if archive_type == "zip" else ".tar.gz")
    output = args.output.resolve()
    output.mkdir(parents=True, exist_ok=True)
    temporary = None
    if args.staging is None:
        temporary = tempfile.TemporaryDirectory(prefix="iac-code-skill-runtime-")
        staging = Path(temporary.name)
    else:
        staging = args.staging.resolve()
        if staging.exists():
            shutil.rmtree(staging)
        staging.mkdir(parents=True)
    prepare_staging(staging, warm_tokenizers=not args.skip_tokenizers, release_date=args.release_date)
    if args.prepare_only:
        print(staging)
        return 0
    environment = {
        **os.environ,
        "IAC_CODE_SKILL_RUNTIME_ROOT": str(ROOT),
        "IAC_CODE_SKILL_RUNTIME_STAGING": str(staging),
    }
    command = [
        sys.executable,
        "-m",
        "PyInstaller",
        "--noconfirm",
        "--clean",
        "--distpath",
        str(output / "bundle"),
        "--workpath",
        str(output / ".work"),
        str(SPEC),
    ]
    subprocess.run(command, cwd=ROOT, env=environment, check=True)
    bundle = output / "bundle/iac-code-runtime"
    sanitize_frozen_metadata(bundle)
    validate_frozen_bundle(bundle)
    write_runtime_version(bundle, version=version, identity=identity)
    executable = bundle / ("iac-code.exe" if os_name == "windows" else "iac-code")
    check = subprocess.run(
        [str(executable), "--version"],
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        encoding="utf-8",
        timeout=30,
        check=False,
    )
    if check.returncode != 0 or version not in check.stdout:
        raise RuntimeError("frozen Skill runtime self-check failed")
    checks = {
        "version": True,
        "a2a": smoke_test_a2a(executable, version),
    }
    archive_path = output / archive_name
    archive_bundle(bundle, archive_path, archive_type)
    compatibility = (
        {"libc": {"name": "glibc", "minVersion": args.glibc_min_version}}
        if os_name == "linux"
        else {"minOsVersion": args.min_os_version}
    )
    entry = {
        "schemaVersion": 1,
        "kind": "iac-code-skill-runtime-entry",
        **identity,
        "iacCodeVersion": version,
        "runtimePython": RUNTIME_PYTHON,
        "checks": checks,
        "artifact": {
            "target": target,
            "os": os_name,
            "arch": arch,
            "nativeAbi": native_abi,
            "runtimePython": RUNTIME_PYTHON,
            "compatibility": compatibility,
            "url": runtime_public_root(identity) + "/" + archive_name,
            "sha256": sha256(archive_path),
            "size": archive_path.stat().st_size,
            "archive": archive_type,
            "executable": "iac-code-runtime/" + executable.name,
        },
    }
    entry_path = output / (target + ".json")
    entry_path.write_text(json.dumps(entry, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(json.dumps({"archive": str(archive_path), "entry": str(entry_path), "target": target}))
    if temporary is not None:
        temporary.cleanup()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
