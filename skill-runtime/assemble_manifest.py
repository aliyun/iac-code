#!/usr/bin/env python3
"""Assemble one immutable Skill Runtime manifest from native build entries."""

from __future__ import annotations

import argparse
import hashlib
import json
import re
from pathlib import Path

EXPECTED_TARGETS = {
    "darwin-arm64-macos-cp312",
    "linux-x86_64-gnu-cp312",
    "windows-x86_64-msvc-cp312",
}
IDENTITY_FIELDS = {
    "releaseKind",
    "runtimeTag",
    "candidateId",
    "iacCodeVersion",
    "runtimePython",
    "sourceCommit",
    "publisherCommit",
    "publishedAt",
}
PUBLIC_ROOT = "https://ros-public-tools.oss-cn-beijing.aliyuncs.com/github-releases/aliyun/iac-code"
_SEMVER_PATTERN = re.compile(r"(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)")
_TAG_PATTERN = re.compile(r"v(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)")
_CANDIDATE_PATTERN = re.compile(r"candidate-[0-9]{8}T[0-9]{6}Z-([0-9a-f]{12})")
_COMMIT_PATTERN = re.compile(r"[0-9a-f]{40}")
_RFC3339_PATTERN = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}Z")


def _identity(document: dict[str, object]) -> dict[str, object]:
    return {name: document[name] for name in IDENTITY_FIELDS if name in document}


def _validate_identity(identity: dict[str, object]) -> None:
    release_kind = identity.get("releaseKind")
    version = str(identity.get("iacCodeVersion") or "")
    source_commit = str(identity.get("sourceCommit") or "")
    if release_kind == "release":
        runtime_tag = str(identity.get("runtimeTag") or "")
        if (
            "candidateId" in identity
            or _TAG_PATTERN.fullmatch(runtime_tag) is None
            or runtime_tag != "v" + version
        ):
            raise SystemExit("runtime release entries contain an invalid identity")
    elif release_kind == "candidate":
        candidate = _CANDIDATE_PATTERN.fullmatch(str(identity.get("candidateId") or ""))
        if "runtimeTag" in identity or candidate is None or candidate.group(1) != source_commit[:12]:
            raise SystemExit("runtime candidate entries contain an invalid identity")
    else:
        raise SystemExit("runtime entries contain an invalid release kind")
    required = {"iacCodeVersion", "runtimePython", "sourceCommit", "publisherCommit", "publishedAt"}
    if (
        not required.issubset(identity)
        or identity.get("runtimePython") != "cp312"
        or _SEMVER_PATTERN.fullmatch(version) is None
        or _COMMIT_PATTERN.fullmatch(source_commit) is None
        or _COMMIT_PATTERN.fullmatch(str(identity.get("publisherCommit") or "")) is None
        or _RFC3339_PATTERN.fullmatch(str(identity.get("publishedAt") or "")) is None
    ):
        raise SystemExit("runtime entries contain an incomplete identity")


def _validate_artifact(artifact: dict[str, object], identity: dict[str, object]) -> None:
    target = str(artifact.get("target") or "")
    parts = target.split("-")
    expected_values = {
        "darwin-arm64-macos-cp312": ("darwin", "arm64", "macos", "tar.gz", "iac-code"),
        "linux-x86_64-gnu-cp312": ("linux", "x86_64", "gnu", "tar.gz", "iac-code"),
        "windows-x86_64-msvc-cp312": ("windows", "x86_64", "msvc", "zip", "iac-code.exe"),
    }
    if target not in expected_values or len(parts) < 4:
        raise SystemExit("native entry contains an unsupported target")
    os_name, arch, abi, archive_type, executable = expected_values[target]
    if (
        artifact.get("os") != os_name
        or artifact.get("arch") != arch
        or artifact.get("nativeAbi") != abi
        or artifact.get("runtimePython") != "cp312"
        or artifact.get("archive") != archive_type
        or artifact.get("executable") != "iac-code-runtime/" + executable
        or not isinstance(artifact.get("compatibility"), dict)
        or not isinstance(artifact.get("size"), int)
        or int(artifact["size"]) <= 0
        or re.fullmatch(r"[0-9a-f]{64}", str(artifact.get("sha256"))) is None
    ):
        raise SystemExit("native entry contains an invalid artifact contract")
    suffix = ".zip" if archive_type == "zip" else ".tar.gz"
    directory = (
        "skill-runtime/releases/{}".format(identity["runtimeTag"])
        if identity["releaseKind"] == "release"
        else "skill-runtime/candidates/{}".format(identity["candidateId"])
    )
    expected_url = "/".join((PUBLIC_ROOT, directory, target + suffix))
    if artifact.get("url") != expected_url:
        raise SystemExit("native entry artifact URL does not match the fixed release identity")
    compatibility = artifact["compatibility"]
    if target == "linux-x86_64-gnu-cp312":
        libc = compatibility.get("libc")
        if (
            not isinstance(libc, dict)
            or libc.get("name") != "glibc"
            or not isinstance(libc.get("minVersion"), str)
            or not libc["minVersion"]
        ):
            raise SystemExit("native entry has an invalid Linux compatibility baseline")
    elif (
        not isinstance(compatibility.get("minOsVersion"), str)
        or not compatibility["minOsVersion"]
    ):
        raise SystemExit("native entry has an invalid OS compatibility baseline")


def assemble(entries: list[Path]) -> dict[str, object]:
    documents = [json.loads(path.read_text(encoding="utf-8")) for path in entries]
    if not documents or any(
        not isinstance(document, dict)
        or document.get("schemaVersion") != 1
        or document.get("kind") != "iac-code-skill-runtime-entry"
        for document in documents
    ):
        raise SystemExit("native runtime entries are missing or invalid")
    identities = [_identity(document) for document in documents]
    if any(identity != identities[0] for identity in identities[1:]):
        raise SystemExit("native entries do not name the same immutable runtime identity")
    identity = identities[0]
    _validate_identity(identity)
    artifacts = [document.get("artifact") for document in documents]
    if any(not isinstance(artifact, dict) for artifact in artifacts):
        raise SystemExit("native entries contain invalid artifacts")
    typed_artifacts = [artifact for artifact in artifacts if isinstance(artifact, dict)]
    for artifact in typed_artifacts:
        _validate_artifact(artifact, identity)
    targets = {artifact.get("target") for artifact in typed_artifacts}
    if targets != EXPECTED_TARGETS or len(typed_artifacts) != len(EXPECTED_TARGETS):
        raise SystemExit("native entries do not contain the exact supported target matrix")
    manifest_kind = (
        "iac-code-skill-runtime-release"
        if identity["releaseKind"] == "release"
        else "iac-code-skill-runtime-candidate"
    )
    manifest = {
        "schemaVersion": 1,
        "kind": manifest_kind,
        **{key: value for key, value in identity.items() if key != "releaseKind"},
        "artifacts": sorted(typed_artifacts, key=lambda item: str(item["target"])),
    }
    return manifest


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--entry", nargs="+", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    manifest = assemble(args.entry)
    encoded = (json.dumps(manifest, indent=2, sort_keys=True) + "\n").encode("utf-8")
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_bytes(encoded)
    print(json.dumps({"manifest": str(args.output), "sha256": hashlib.sha256(encoded).hexdigest()}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
