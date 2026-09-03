from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import re
import subprocess
import sys
import tarfile
import zipfile
from pathlib import Path

import pytest

from iac_code import __version__

ROOT = Path(__file__).resolve().parents[2]
BUILD_SCRIPT = ROOT / "skill-runtime/build_runtime.py"
ASSEMBLE_SCRIPT = ROOT / "skill-runtime/assemble_manifest.py"
PACKAGE_SCRIPT = ROOT / "skill-runtime/package_skill.py"
PROFILE_SCRIPT = ROOT / "skill-runtime/skill_profiles.py"
SOURCE_COMMIT = "a" * 40
PUBLISHER_COMMIT = "b" * 40
PUBLISHED_AT = "2026-08-15T10:30:00Z"
NON_ENGLISH_SOURCE_PATTERN = re.compile(r"[\u3040-\u30ff\u3400-\u4dbf\u4e00-\u9fff\uac00-\ud7af]")


def _load_module(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _entry(target: str, os_name: str, native_abi: str, compatibility: dict[str, object]) -> dict[str, object]:
    archive = "zip" if os_name == "windows" else "tar.gz"
    executable = "iac-code.exe" if os_name == "windows" else "iac-code"
    archive_name = target + (".zip" if archive == "zip" else ".tar.gz")
    return {
        "schemaVersion": 1,
        "kind": "iac-code-skill-runtime-entry",
        "releaseKind": "release",
        "runtimeTag": "v0.12.0",
        "iacCodeVersion": "0.12.0",
        "runtimePython": "cp312",
        "sourceCommit": SOURCE_COMMIT,
        "publisherCommit": PUBLISHER_COMMIT,
        "publishedAt": PUBLISHED_AT,
        "artifact": {
            "target": target,
            "os": os_name,
            "arch": "arm64" if os_name == "darwin" else "x86_64",
            "nativeAbi": native_abi,
            "runtimePython": "cp312",
            "compatibility": compatibility,
            "url": (
                "https://ros-public-tools.oss-cn-beijing.aliyuncs.com/github-releases/aliyun/iac-code/"
                "skill-runtime/releases/v0.12.0/" + archive_name
            ),
            "sha256": "c" * 64,
            "size": 100,
            "archive": archive,
            "executable": "iac-code-runtime/" + executable,
        },
    }


def test_build_script_uses_tag_identity_and_fixed_public_path() -> None:
    module = _load_module("skill_runtime_build", BUILD_SCRIPT)
    assert module.ROOT == ROOT
    assert module.iac_code_version() == __version__
    args = argparse.Namespace(
        runtime_tag="v0.12.0",
        candidate_id=None,
        source_commit=SOURCE_COMMIT,
        publisher_commit=PUBLISHER_COMMIT,
        published_at=PUBLISHED_AT,
        release_date="2026-08-15",
    )
    identity = module._release_identity(args, "0.12.0")
    assert identity["runtimeTag"] == "v0.12.0"
    assert module.runtime_public_root(identity).endswith("/skill-runtime/releases/v0.12.0")


def test_runtime_archive_and_version_marker_are_rooted_consistently(tmp_path: Path) -> None:
    module = _load_module("skill_runtime_archive", BUILD_SCRIPT)
    bundle = tmp_path / "bundle"
    bundle.mkdir()
    (bundle / "iac-code").write_text("runtime", encoding="utf-8")
    identity = {
        "releaseKind": "release",
        "runtimeTag": "v0.12.0",
        "sourceCommit": SOURCE_COMMIT,
        "publisherCommit": PUBLISHER_COMMIT,
        "publishedAt": PUBLISHED_AT,
    }
    module.write_runtime_version(bundle, version="0.12.0", identity=identity)
    output = tmp_path / "runtime.tar.gz"
    module.archive_bundle(bundle, output, "tar.gz")
    with tarfile.open(output, "r:gz") as archive:
        names = set(archive.getnames())
        marker = json.load(archive.extractfile("iac-code-runtime/runtime-version.json"))
    assert "iac-code-runtime/iac-code" in names
    assert marker["runtimeTag"] == "v0.12.0"
    assert "artifactRevision" not in marker


def test_runtime_a2a_smoke_checks_health_and_agent_card(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    module = _load_module("skill_runtime_smoke", BUILD_SCRIPT)
    server = tmp_path / "server.py"
    server.write_text(
        """import json,sys
from http.server import BaseHTTPRequestHandler,HTTPServer
class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        value = {"status":"healthy","version":"0.12.0"} if self.path == "/health" else {"name":"iac-code"}
        body = json.dumps(value).encode()
        self.send_response(200)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)
    def log_message(self, *_args): pass
HTTPServer(("127.0.0.1", int(sys.argv[1])), Handler).serve_forever()
""",
        encoding="utf-8",
    )
    real_popen = subprocess.Popen

    def fake_popen(command, **kwargs):
        return real_popen([sys.executable, str(server), command[-1]], **kwargs)

    monkeypatch.setattr(module.subprocess, "Popen", fake_popen)

    assert module.smoke_test_a2a(tmp_path / "iac-code", "0.12.0") == {
        "health": "healthy",
        "agentCard": True,
    }


def test_manifest_assembly_is_deterministic_and_bridge_valid(tmp_path: Path) -> None:
    documents = [
        _entry("windows-x86_64-msvc-cp312", "windows", "msvc", {"minOsVersion": "10.0.17763"}),
        _entry("darwin-arm64-macos-cp312", "darwin", "macos", {"minOsVersion": "12.0"}),
        _entry(
            "linux-x86_64-gnu-cp312",
            "linux",
            "gnu",
            {"libc": {"name": "glibc", "minVersion": "2.35"}},
        ),
    ]
    entries = []
    for index, document in enumerate(documents):
        path = tmp_path / "entry-{}.json".format(index)
        path.write_text(json.dumps(document), encoding="utf-8")
        entries.append(path)
    output = tmp_path / "runtime-manifest.json"
    result = subprocess.run(
        [sys.executable, str(ASSEMBLE_SCRIPT), "--entry", *(str(path) for path in entries), "--output", str(output)],
        cwd=ROOT,
        check=True,
        capture_output=True,
        text=True,
    )
    report = json.loads(result.stdout)
    encoded = output.read_bytes()
    assert report["sha256"] == hashlib.sha256(encoded).hexdigest()
    manifest = json.loads(encoded)
    assert manifest["kind"] == "iac-code-skill-runtime-release"
    assert manifest["runtimeTag"] == "v0.12.0"
    assert "releaseKind" not in manifest
    assert [item["target"] for item in manifest["artifacts"]] == sorted(
        document["artifact"]["target"] for document in documents
    )

    bridge = _load_module("release_validation_bridge", ROOT / "skills/iac-code/scripts/iac_code.py")
    assert bridge.validate_manifest(manifest) == manifest


def _package_command(output: Path, manifest_output: Path) -> list[str]:
    return [
        sys.executable,
        str(PACKAGE_SCRIPT),
        "--skill-version",
        "0.1.0",
        "--runtime-tag",
        "v0.12.0",
        "--iac-code-version",
        "0.12.0",
        "--manifest-sha256",
        "d" * 64,
        "--manifest-size",
        "1234",
        "--source-commit",
        SOURCE_COMMIT,
        "--publisher-commit",
        PUBLISHER_COMMIT,
        "--published-at",
        PUBLISHED_AT,
        "--output",
        str(output),
        "--manifest-output",
        str(manifest_output),
    ]


def test_skill_package_is_deterministic_whitelisted_and_pinned(tmp_path: Path) -> None:
    first = tmp_path / "first/iac-code-skill-0.1.0.zip"
    second = tmp_path / "second/iac-code-skill-0.1.0.zip"
    first_manifest = tmp_path / "first/skill-release-manifest.json"
    second_manifest = tmp_path / "second/skill-release-manifest.json"
    subprocess.run(_package_command(first, first_manifest), cwd=ROOT, check=True)
    subprocess.run(_package_command(second, second_manifest), cwd=ROOT, check=True)
    assert first.read_bytes() == second.read_bytes()
    assert first_manifest.read_bytes() == second_manifest.read_bytes()
    with zipfile.ZipFile(first) as archive:
        assert archive.namelist() == [
            "iac-code/SKILL.md",
            "iac-code/agents/openai.yaml",
            "iac-code/scripts/iac_code.py",
        ]
        bridge = archive.read("iac-code/scripts/iac_code.py").decode("utf-8")
    assert 'SKILL_VERSION = "0.1.0"' in bridge
    assert 'RUNTIME_TAG = "v0.12.0"' in bridge
    assert "skill-runtime/releases/v0.12.0/runtime-manifest.json" in bridge
    assert "d" * 64 in bridge
    manifest = json.loads(first_manifest.read_text(encoding="utf-8"))
    assert manifest["skillVersion"] == "0.1.0"
    assert manifest["runtimeTag"] == "v0.12.0"
    assert manifest["skill"]["sha256"] == hashlib.sha256(first.read_bytes()).hexdigest()


def test_skill_profiles_render_three_strict_product_shapes(tmp_path: Path) -> None:
    subprocess.run([sys.executable, str(PROFILE_SCRIPT), "--check-defaults"], cwd=ROOT, check=True)
    expected = {
        "iac-code": ["SKILL.md", "agents/openai.yaml", "scripts/iac_code.py"],
        "alibabacloud-iac-code": [
            "SKILL.md",
            "references/ram-policies.md",
            "scripts/iac_code.py",
        ],
        "alibabacloud-ros-agent": [
            "SKILL.md",
            "references/ram-policies.md",
            "scripts/_ros_agent_core.py",
            "scripts/_ros_agent_projection.py",
            "scripts/_ros_agent_runtime.py",
            "scripts/requirements.txt",
            "scripts/ros_agent.py",
        ],
    }
    for name, files in expected.items():
        output = tmp_path / name
        subprocess.run(
            [sys.executable, str(PROFILE_SCRIPT), "--profile", name, "--output", str(output)],
            cwd=ROOT,
            check=True,
        )
        actual = sorted(path.relative_to(output).as_posix() for path in output.rglob("*") if path.is_file())
        assert actual == files
        skill = (output / "SKILL.md").read_text(encoding="utf-8")
        assert "name: {}".format(name) in skill
        bridge_name = "ros_agent.py" if name == "alibabacloud-ros-agent" else "iac_code.py"
        bridge = next(output.rglob(bridge_name)).read_text(encoding="utf-8")
        if name.startswith("alibabacloud-"):
            assert "## Observability" in skill
            assert "references/ram-policies.md" in skill
            assert "32-character lowercase hexadecimal string" in skill
            assert 'SKILL_DISTRIBUTION = "agenthub"' in bridge
            assert "AlibabaCloud-Agent-Skills/{}/{{session-id}}".format(name) in bridge
            if name == "alibabacloud-ros-agent":
                assert "`scripts/requirements.txt`" in skill
                assert "requirements-code.txt" not in skill
                assert 'REQUIREMENTS_FILE = "scripts/requirements.txt"' in bridge
                ros_sources = "\n".join(
                    path.read_text(encoding="utf-8") for path in sorted((output / "scripts").glob("*.py"))
                )
                assert NON_ENGLISH_SOURCE_PATTERN.search(ros_sources) is None
                assert all(path.stat().st_size <= 128 * 1024 for path in (output / "scripts").glob("*.py"))
                subprocess.run([sys.executable, str(output / "scripts/ros_agent.py"), "--help"], check=True)
        else:
            assert "## Observability" not in skill
            assert 'SKILL_DISTRIBUTION = "public"' in bridge


def test_agenthub_profiles_package_as_independent_products(tmp_path: Path) -> None:
    iac_archive = tmp_path / "alibabacloud-iac-code-skill-0.1.0.zip"
    iac_manifest = tmp_path / "iac-manifest.json"
    iac_command = _package_command(iac_archive, iac_manifest)
    iac_command[2:2] = ["--profile", "alibabacloud-iac-code"]
    subprocess.run(iac_command, cwd=ROOT, check=True)
    with zipfile.ZipFile(iac_archive) as archive:
        assert archive.namelist() == [
            "alibabacloud-iac-code/SKILL.md",
            "alibabacloud-iac-code/references/ram-policies.md",
            "alibabacloud-iac-code/scripts/iac_code.py",
        ]
    assert json.loads(iac_manifest.read_text(encoding="utf-8"))["skillName"] == "alibabacloud-iac-code"

    ros_archive = tmp_path / "alibabacloud-ros-agent-skill-0.1.0.zip"
    ros_manifest = tmp_path / "ros-manifest.json"
    subprocess.run(
        [
            sys.executable,
            str(PACKAGE_SCRIPT),
            "--profile",
            "alibabacloud-ros-agent",
            "--skill-version",
            "0.1.0",
            "--source-commit",
            SOURCE_COMMIT,
            "--publisher-commit",
            PUBLISHER_COMMIT,
            "--published-at",
            PUBLISHED_AT,
            "--output",
            str(ros_archive),
            "--manifest-output",
            str(ros_manifest),
        ],
        cwd=ROOT,
        check=True,
    )
    with zipfile.ZipFile(ros_archive) as archive:
        assert archive.namelist() == [
            "alibabacloud-ros-agent/SKILL.md",
            "alibabacloud-ros-agent/references/ram-policies.md",
            "alibabacloud-ros-agent/scripts/_ros_agent_core.py",
            "alibabacloud-ros-agent/scripts/_ros_agent_projection.py",
            "alibabacloud-ros-agent/scripts/_ros_agent_runtime.py",
            "alibabacloud-ros-agent/scripts/requirements.txt",
            "alibabacloud-ros-agent/scripts/ros_agent.py",
        ]
    assert json.loads(ros_manifest.read_text(encoding="utf-8"))["skillName"] == "alibabacloud-ros-agent"


def test_formal_skill_package_rejects_runtime_candidate(tmp_path: Path) -> None:
    command = _package_command(tmp_path / "skill.zip", tmp_path / "manifest.json")
    tag_index = command.index("--runtime-tag")
    command[tag_index : tag_index + 2] = [
        "--runtime-candidate-id",
        "candidate-20260815T103000Z-{}".format(SOURCE_COMMIT[:12]),
    ]
    result = subprocess.run(command, cwd=ROOT, check=False, capture_output=True, text=True)
    assert result.returncode != 0
    assert "formal Skill release" in result.stderr


def test_publisher_contracts_are_explicit() -> None:
    runtime_contract = json.loads((ROOT / "skill-runtime/publisher-contract.json").read_text(encoding="utf-8"))
    skill_contract = json.loads((ROOT / "skill-runtime/skill-package-contract.json").read_text(encoding="utf-8"))
    assert runtime_contract["runtimePython"] == "cp312"
    assert len(runtime_contract["targets"]) == 3
    assert skill_contract["files"] == ["SKILL.md", "agents/openai.yaml", "scripts/iac_code.py"]
    assert skill_contract["profileScript"] == "skill-runtime/skill_profiles.py"
    assert sorted(skill_contract["profiles"]) == [
        "alibabacloud-iac-code",
        "alibabacloud-ros-agent",
        "iac-code",
    ]
    assert skill_contract["profiles"]["alibabacloud-iac-code"]["files"] == [
        "SKILL.md",
        "references/ram-policies.md",
        "scripts/iac_code.py",
    ]
    assert skill_contract["profiles"]["alibabacloud-ros-agent"]["files"] == [
        "SKILL.md",
        "references/ram-policies.md",
        "scripts/_ros_agent_core.py",
        "scripts/_ros_agent_projection.py",
        "scripts/_ros_agent_runtime.py",
        "scripts/requirements.txt",
        "scripts/ros_agent.py",
    ]


def test_manifest_assembly_rejects_incomplete_target_matrix(tmp_path: Path) -> None:
    path = tmp_path / "entry.json"
    path.write_text(
        json.dumps(_entry("darwin-arm64-macos-cp312", "darwin", "macos", {"minOsVersion": "12.0"})),
        encoding="utf-8",
    )
    result = subprocess.run(
        [sys.executable, str(ASSEMBLE_SCRIPT), "--entry", str(path), "--output", str(tmp_path / "manifest.json")],
        cwd=ROOT,
        check=False,
        capture_output=True,
        text=True,
    )
    assert result.returncode != 0
    assert "exact supported target matrix" in result.stderr


@pytest.mark.parametrize("name", ["runtime_tag", "candidate_id"])
def test_runtime_identity_requires_exactly_one_kind(name: str) -> None:
    parser_source = BUILD_SCRIPT.read_text(encoding="utf-8")
    assert "add_mutually_exclusive_group(required=True)" in parser_source
    assert name in parser_source
