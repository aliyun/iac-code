#!/usr/bin/env python3
"""Build the deterministic external iac-code Skill ZIP in an isolated staging directory."""

from __future__ import annotations

import argparse
import ast
import hashlib
import json
import re
import tempfile
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SKILL_SOURCE = ROOT / "skills/iac-code"
PUBLIC_ORIGIN = "https://ros-public-tools.oss-cn-beijing.aliyuncs.com"
PRODUCT_PREFIX = "github-releases/aliyun/iac-code"
RUNTIME_PYTHON = "cp312"
SKILL_FILES = ("SKILL.md", "agents/openai.yaml", "scripts/iac_code.py")
_SEMVER_PATTERN = re.compile(r"(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)")
_TAG_PATTERN = re.compile(r"v(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)")
_CANDIDATE_PATTERN = re.compile(r"candidate-[0-9]{8}T[0-9]{6}Z-([0-9a-f]{12})")
_COMMIT_PATTERN = re.compile(r"[0-9a-f]{40}")
_DIGEST_PATTERN = re.compile(r"[0-9a-f]{64}")
_RFC3339_PATTERN = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}Z")


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _candidate(value: str | None, source_commit: str, name: str) -> str | None:
    if value is None:
        return None
    match = _CANDIDATE_PATTERN.fullmatch(value)
    if match is None or match.group(1) != source_commit[:12]:
        raise SystemExit("{} must use candidate-YYYYMMDDTHHMMSSZ-<source commit prefix>".format(name))
    return value


def _validate_args(args: argparse.Namespace) -> None:
    for name in ("source_commit", "publisher_commit"):
        if _COMMIT_PATTERN.fullmatch(getattr(args, name)) is None:
            raise SystemExit("--{} must be a lowercase 40-character Git commit".format(name.replace("_", "-")))
    if _RFC3339_PATTERN.fullmatch(args.published_at) is None:
        raise SystemExit("--published-at must use UTC RFC3339 form YYYY-MM-DDTHH:MM:SSZ")
    if _DIGEST_PATTERN.fullmatch(args.manifest_sha256) is None or set(args.manifest_sha256) == {"0"}:
        raise SystemExit("--manifest-sha256 must be a non-placeholder lowercase SHA-256")
    if args.manifest_size <= 0:
        raise SystemExit("--manifest-size must be positive")
    if args.runtime_python != RUNTIME_PYTHON:
        raise SystemExit("the initial Skill contract requires runtime Python cp312")
    if args.skill_version and _SEMVER_PATTERN.fullmatch(args.skill_version) is None:
        raise SystemExit("--skill-version must use canonical X.Y.Z")
    if args.runtime_tag:
        match = _TAG_PATTERN.fullmatch(args.runtime_tag)
        if match is None or args.runtime_tag != "v{}".format(args.iac_code_version):
            raise SystemExit("--runtime-tag must be the canonical tag for --iac-code-version")
    if _SEMVER_PATTERN.fullmatch(args.iac_code_version) is None:
        raise SystemExit("--iac-code-version must use canonical X.Y.Z")
    _candidate(args.skill_candidate_id, args.source_commit, "--skill-candidate-id")
    runtime_source = args.runtime_source_commit or args.source_commit
    if _COMMIT_PATTERN.fullmatch(runtime_source) is None:
        raise SystemExit("--runtime-source-commit must be a lowercase 40-character Git commit")
    _candidate(args.runtime_candidate_id, runtime_source, "--runtime-candidate-id")
    if args.skill_version and args.runtime_candidate_id:
        raise SystemExit("a formal Skill release cannot reference a Runtime Candidate")


def runtime_manifest_url(args: argparse.Namespace) -> str:
    if args.runtime_tag:
        suffix = "skill-runtime/releases/{}/runtime-manifest.json".format(args.runtime_tag)
    else:
        suffix = "skill-runtime/candidates/{}/runtime-manifest.json".format(args.runtime_candidate_id)
    return "/".join((PUBLIC_ORIGIN, PRODUCT_PREFIX, suffix))


def skill_public_url(args: argparse.Namespace) -> str:
    if args.skill_version:
        suffix = "skill/releases/{}/iac-code-skill-{}.zip".format(args.skill_version, args.skill_version)
    else:
        suffix = "skill/candidates/{}/iac-code-skill.zip".format(args.skill_candidate_id)
    return "/".join((PUBLIC_ORIGIN, PRODUCT_PREFIX, suffix))


def _replace_constant(source: str, name: str, value: str) -> str:
    pattern = re.compile(r'^{} = "[^"]*"$'.format(re.escape(name)), re.MULTILINE)
    replacement = "{} = {}".format(name, json.dumps(value))
    updated, count = pattern.subn(replacement, source)
    if count != 1:
        raise SystemExit("Skill bridge must contain exactly one {} constant".format(name))
    return updated


def _stage(args: argparse.Namespace, root: Path) -> Path:
    skill_root = root / "iac-code"
    for relative in SKILL_FILES:
        source = SKILL_SOURCE / relative
        if not source.is_file():
            raise SystemExit("Skill source is missing {}".format(relative))
        destination = skill_root / relative
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(source.read_bytes())
    bridge_path = skill_root / "scripts/iac_code.py"
    bridge = bridge_path.read_text(encoding="utf-8")
    runtime_identity = args.runtime_tag or args.runtime_candidate_id
    skill_identity = args.skill_version or args.skill_candidate_id
    replacements = {
        "SKILL_VERSION": str(skill_identity),
        "RUNTIME_TAG": str(runtime_identity),
        "IAC_CODE_VERSION": args.iac_code_version,
        "RUNTIME_PYTHON": args.runtime_python,
        "MANIFEST_URL": runtime_manifest_url(args),
        "MANIFEST_SHA256": args.manifest_sha256,
    }
    for name, value in replacements.items():
        bridge = _replace_constant(bridge, name, value)
    ast.parse(bridge, filename=str(bridge_path), feature_version=(3, 8))
    bridge_path.write_text(bridge, encoding="utf-8", newline="\n")
    actual = sorted(path.relative_to(skill_root).as_posix() for path in skill_root.rglob("*") if path.is_file())
    if actual != sorted(SKILL_FILES):
        raise SystemExit("Skill staging contains files outside the package whitelist")
    return skill_root


def deterministic_zip(skill_root: Path, output: Path) -> None:
    output.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(output, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=9) as archive:
        for relative in SKILL_FILES:
            source = skill_root / relative
            info = zipfile.ZipInfo("iac-code/" + relative, date_time=(1980, 1, 1, 0, 0, 0))
            info.create_system = 3
            mode = 0o755 if relative == "scripts/iac_code.py" else 0o644
            info.external_attr = (0o100000 | mode) << 16
            info.compress_type = zipfile.ZIP_DEFLATED
            archive.writestr(info, source.read_bytes(), compress_type=zipfile.ZIP_DEFLATED, compresslevel=9)


def release_manifest(args: argparse.Namespace, archive: Path) -> dict[str, object]:
    runtime_identity_name = "runtimeTag" if args.runtime_tag else "runtimeCandidateId"
    runtime_identity = args.runtime_tag or args.runtime_candidate_id
    value: dict[str, object] = {
        "schemaVersion": 1,
        "kind": "iac-code-skill-release" if args.skill_version else "iac-code-skill-candidate",
        "skillSourceCommit": args.source_commit,
        "publisherCommit": args.publisher_commit,
        "publishedAt": args.published_at,
        runtime_identity_name: runtime_identity,
        "runtimeManifest": {
            "url": runtime_manifest_url(args),
            "size": args.manifest_size,
            "sha256": args.manifest_sha256,
        },
        "skill": {
            "name": archive.name,
            "url": skill_public_url(args),
            "size": archive.stat().st_size,
            "sha256": sha256(archive),
        },
    }
    if args.skill_version:
        value["skillVersion"] = args.skill_version
    else:
        value["candidateId"] = args.skill_candidate_id
    return value


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    skill = parser.add_mutually_exclusive_group(required=True)
    skill.add_argument("--skill-version")
    skill.add_argument("--skill-candidate-id")
    runtime = parser.add_mutually_exclusive_group(required=True)
    runtime.add_argument("--runtime-tag")
    runtime.add_argument("--runtime-candidate-id")
    parser.add_argument("--iac-code-version", required=True)
    parser.add_argument("--runtime-python", default=RUNTIME_PYTHON)
    parser.add_argument("--manifest-sha256", required=True)
    parser.add_argument("--manifest-size", type=int, required=True)
    parser.add_argument("--source-commit", required=True)
    parser.add_argument("--runtime-source-commit")
    parser.add_argument("--publisher-commit", required=True)
    parser.add_argument("--published-at", required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--manifest-output", type=Path, required=True)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    _validate_args(args)
    with tempfile.TemporaryDirectory(prefix="iac-code-skill-package-") as temporary:
        skill_root = _stage(args, Path(temporary))
        deterministic_zip(skill_root, args.output)
    manifest = release_manifest(args, args.output)
    args.manifest_output.parent.mkdir(parents=True, exist_ok=True)
    encoded = json.dumps(manifest, indent=2, sort_keys=True) + "\n"
    args.manifest_output.write_text(encoded, encoding="utf-8", newline="\n")
    print(
        json.dumps(
            {
                "archive": str(args.output),
                "manifest": str(args.manifest_output),
                "sha256": sha256(args.output),
            },
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
