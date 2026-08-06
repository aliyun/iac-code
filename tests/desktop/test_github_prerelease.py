from __future__ import annotations

import hashlib
import importlib.util
import json
import sys
from base64 import b64encode
from pathlib import Path

import pytest

ROOT = Path(__file__).parents[2]


def _load_script():
    script = ROOT / "desktop/scripts/prepare_github_release.py"
    spec = importlib.util.spec_from_file_location("iac_code_desktop_prepare_github_release", script)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _fixture_artifacts(root: Path) -> dict[str, Path]:
    files = {
        "macos_installer": root / "desktop-pre-macos-aarch64/dmg/iac-code_0.11.1_aarch64.dmg",
        "macos_updater": root / "desktop-pre-macos-aarch64/macos/iac-code.app.tar.gz",
        "macos_signature": root / "desktop-pre-macos-aarch64/macos/iac-code.app.tar.gz.sig",
        "windows_installer": root / "desktop-pre-windows-x64/nsis/iac-code_0.11.1_x64-setup.exe",
        "windows_updater": root / "desktop-pre-windows-x64/nsis/iac-code_0.11.1_x64-setup.nsis.zip",
        "windows_signature": root
        / "desktop-pre-windows-x64/nsis/iac-code_0.11.1_x64-setup.nsis.zip.sig",
        "linux_installer": root / "desktop-pre-linux-x64/appimage/iac-code_0.11.1_amd64.AppImage",
        "linux_signature": root / "desktop-pre-linux-x64/appimage/iac-code_0.11.1_amd64.AppImage.sig",
        "linux_deb": root / "desktop-pre-linux-x64/deb/iac-code_0.11.1_amd64.deb",
        "sbom": root / "desktop-pre-linux-x64/release-metadata/desktop-sbom.cdx.json",
        "notices": root / "desktop-pre-linux-x64/release-metadata/THIRD_PARTY_NOTICES.txt",
    }
    for name, path in files.items():
        path.parent.mkdir(parents=True, exist_ok=True)
        content = "signature-{}\n".format(name) if name.endswith("signature") else "artifact-{}\n".format(name)
        path.write_text(content, encoding="utf-8")
    return files


def test_release_tag_requires_all_desktop_versions_to_match() -> None:
    module = _load_script()

    assert module.validate_release_tag("v0.11.1", ROOT) == "0.11.1"
    with pytest.raises(RuntimeError, match="must be v0.11.1"):
        module.validate_release_tag("desktop-v0.11.1", ROOT)


def test_create_pre_manifest_records_exact_provenance_and_checksums(tmp_path: Path) -> None:
    module = _load_script()
    source = tmp_path / "input"
    files = _fixture_artifacts(source)
    output = tmp_path / "manifest"

    manifest = module.create_pre_manifest(
        source,
        output,
        repository="aliyun/iac-code",
        tag="v0.11.1",
        version="0.11.1",
        commit="a" * 40,
        workflow_run_id="12345",
        published_at="2026-08-04T12:00:00Z",
    )

    assert manifest["kind"] == "iac-code-desktop-pre"
    assert manifest["commit"] == "a" * 40
    assert manifest["workflowRunId"] == "12345"
    assert manifest["publishedAt"] == "2026-08-04T12:00:00Z"
    assets = {asset["logicalName"]: asset for asset in manifest["assets"]}
    assert set(assets) == set(files)
    assert assets["macos_installer"]["platform"] == "darwin-aarch64"
    assert assets["windows_updater"]["kind"] == "updater"
    assert assets["linux_deb"]["sha256"] == hashlib.sha256(files["linux_deb"].read_bytes()).hexdigest()
    assert json.loads((output / "desktop-pre-manifest.json").read_text(encoding="utf-8")) == manifest
    checksum_lines = (output / "SHA256SUMS").read_text(encoding="utf-8").splitlines()
    assert len(checksum_lines) == len(files)
    assert "private release pipeline" in (output / "DESKTOP_PRE_NOTICE.md").read_text(encoding="utf-8")


def test_create_pre_manifest_rejects_ambiguous_or_invalid_identity(tmp_path: Path) -> None:
    module = _load_script()
    source = tmp_path / "input"
    _fixture_artifacts(source)

    with pytest.raises(RuntimeError, match="full lowercase Git SHA"):
        module.create_pre_manifest(
            source,
            tmp_path / "bad",
            repository="aliyun/iac-code",
            tag="v0.11.1",
            version="0.11.1",
            commit="main",
            workflow_run_id="123",
            published_at="2026-08-04T12:00:00Z",
        )


def test_updater_payload_signatures_are_cryptographically_checked(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    module = _load_script()
    artifacts = _fixture_artifacts(tmp_path)
    commands: list[list[str]] = []
    monkeypatch.setattr(module.shutil, "which", lambda name: "/usr/bin/minisign" if name == "minisign" else None)
    monkeypatch.setattr(module.subprocess, "run", lambda command, check: commands.append(command))

    tauri_public_key = b64encode(b"untrusted comment: minisign public key\nRW-public-key\n").decode("ascii")
    module.verify_updater_signatures(artifacts, tauri_public_key)

    assert len(commands) == 3
    assert all(command[:2] == ["/usr/bin/minisign", "-Vm"] for command in commands)
    assert all(command[-2:] == ["-P", "RW-public-key"] for command in commands)


def test_github_workflows_only_prebuild_and_rebundle_verified_signed_components() -> None:
    pre_workflow = (ROOT / ".github/workflows/desktop-release.yml").read_text(encoding="utf-8")
    signed_workflow = (ROOT / ".github/workflows/desktop-signed-package.yml").read_text(encoding="utf-8")
    allowlist = (ROOT / "desktop/scope-allowlist.txt").read_text(encoding="utf-8")

    assert "types: [published]" in pre_workflow
    assert "desktop-pre-macos-aarch64" in pre_workflow
    assert "desktop-pre-windows-x64" in pre_workflow
    assert "desktop-pre-linux-x64" in pre_workflow
    assert "desktop-pre-manifest" in pre_workflow
    assert "desktop-signing-input-${{ matrix.signing-platform }}" in pre_workflow
    assert "create-pre-manifest" in pre_workflow
    assert "--verify-signatures" in pre_workflow
    assert "secrets.TAURI_SIGNING_PRIVATE_KEY" in pre_workflow
    assert "gh release upload" not in pre_workflow
    assert "publish_release_to_oss.py" not in pre_workflow
    assert "RELEASE_BROKER" not in pre_workflow
    assert "id-token: write" not in pre_workflow

    assert "workflow_dispatch:" in signed_workflow
    assert "desktop/staging/" in signed_workflow
    assert "handoff_sha256" in signed_workflow
    assert "signing_handoff.py consume" in signed_workflow
    assert "signing_handoff.py bundle" in signed_workflow
    assert "ditto -c -k --sequesterRsrc --keepParent" in signed_workflow
    assert "*.app.zip" in signed_workflow
    assert "--no-sign" in (ROOT / "desktop/scripts/signing_handoff.py").read_text(encoding="utf-8")
    assert "gh release upload" not in signed_workflow
    assert ".github/workflows/desktop-release.yml" in allowlist
