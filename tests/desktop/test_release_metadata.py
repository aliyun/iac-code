from __future__ import annotations

import hashlib
import importlib.util
import json
import sys
from base64 import b64encode
from pathlib import Path

import pytest


def _load_script(name: str):
    script = Path(__file__).parents[2] / "desktop/scripts" / name
    spec = importlib.util.spec_from_file_location("iac_code_desktop_{}".format(script.stem), script)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _components(module):
    return [
        module.Component(
            ecosystem="pypi",
            name="example-python",
            version="1.2.3",
            license_expression="MIT",
            role="runtime",
            license_texts={"LICENSE": "MIT fixture\n"},
        ),
        module.Component(
            ecosystem="cargo",
            name="example-rust",
            version="2.0.0",
            license_expression="Apache-2.0",
            role="runtime",
            license_texts={"LICENSE-APACHE": "Apache fixture\n"},
        ),
    ]


def test_release_sbom_and_notices_are_deterministic_and_cross_referenced(tmp_path: Path) -> None:
    generator = _load_script("generate_release_metadata.py")
    verifier = _load_script("verify_release.py")
    components = list(reversed(_components(generator)))
    sbom = generator.build_sbom(components, "0.11.1")
    notices = generator.render_notices(components)
    metadata_dir = tmp_path / "metadata"
    metadata_dir.mkdir()
    (metadata_dir / "desktop-sbom.cdx.json").write_text(
        json.dumps(sbom, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    (metadata_dir / "THIRD_PARTY_NOTICES.txt").write_text(notices, encoding="utf-8")

    verified = verifier.verify_metadata(metadata_dir)

    assert [component["purl"] for component in verified["components"]] == sorted(
        component.purl for component in components
    )
    assert generator.render_notices(components) == generator.render_notices(reversed(components))


def test_release_metadata_verifier_rejects_notice_without_component(tmp_path: Path) -> None:
    generator = _load_script("generate_release_metadata.py")
    verifier = _load_script("verify_release.py")
    metadata_dir = tmp_path / "metadata"
    metadata_dir.mkdir()
    sbom = generator.build_sbom(_components(generator), "0.11.1")
    (metadata_dir / "desktop-sbom.cdx.json").write_text(json.dumps(sbom), encoding="utf-8")
    (metadata_dir / "THIRD_PARTY_NOTICES.txt").write_text("incomplete\n", encoding="utf-8")

    with pytest.raises(RuntimeError, match="omit SBOM components"):
        verifier.verify_metadata(metadata_dir)


def test_privacy_notice_requires_release_owner_contact_retention_and_date(tmp_path: Path) -> None:
    generator = _load_script("generate_release_metadata.py")
    verifier = _load_script("verify_release.py")
    template = generator.PRIVACY_TEMPLATE.read_text(encoding="utf-8")

    with pytest.raises(RuntimeError, match="TELEMETRY_RETENTION"):
        generator.render_privacy_notice(
            template,
            {
                "LEGAL_ENTITY": "Example Operator",
                "PRIVACY_CONTACT": "privacy@example.invalid",
                "TELEMETRY_RETENTION": "",
                "EFFECTIVE_DATE": "2026-08-02",
            },
        )

    rendered = generator.render_privacy_notice(
        template,
        {
            "LEGAL_ENTITY": "Example Operator",
            "PRIVACY_CONTACT": "privacy@example.invalid",
            "TELEMETRY_RETENTION": "30 days",
            "EFFECTIVE_DATE": "2026-08-02",
        },
    )
    notice = tmp_path / "PRIVACY_NOTICE.md"
    notice.write_text(rendered, encoding="utf-8")
    verifier.verify_privacy_notice(notice)
    assert "{{" not in rendered


def test_artifact_inspection_rejects_private_key_and_personal_build_path(tmp_path: Path) -> None:
    verifier = _load_script("verify_release.py")
    artifact = tmp_path / "bundle"
    artifact.mkdir()
    (artifact / "host.bin").write_bytes(b"compiled at /Users/release-user/project\x00")
    (artifact / "updater.key").write_text("-----BEGIN PRIVATE KEY-----\n", encoding="utf-8")

    with pytest.raises(RuntimeError, match="private-key file name"):
        verifier.verify_no_private_material_or_build_paths(artifact)


def test_linux_checksum_parser_and_hash_gate(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    verifier = _load_script("verify_release.py")
    artifact = tmp_path / "bundle"
    artifact.mkdir()
    payload = artifact / "iac-code.AppImage"
    payload.write_bytes(b"appimage")
    digest = hashlib.sha256(payload.read_bytes()).hexdigest()
    checksums = artifact / "SHA256SUMS"
    checksums.write_text("{}  {}\n".format(digest, payload.name), encoding="utf-8")

    assert verifier._parse_checksums(checksums) == {payload.name: digest}
    payload.write_bytes(b"tampered")
    monkeypatch.setenv("IAC_CODE_LINUX_SIGNATURE_MODE", "unsupported")
    with pytest.raises(RuntimeError, match="checksum mismatch"):
        verifier.verify_linux(artifact)


def test_updater_signature_gate_requires_public_key_and_verifies_payload(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    verifier = _load_script("verify_release.py")
    artifact = tmp_path / "bundle"
    artifact.mkdir()
    payload = artifact / "iac-code.nsis.zip"
    signature = artifact / "iac-code.nsis.zip.sig"
    payload.write_bytes(b"payload")
    signature.write_text("signature", encoding="utf-8")

    monkeypatch.delenv("IAC_CODE_DESKTOP_UPDATER_PUBKEY", raising=False)
    with pytest.raises(RuntimeError, match="UPDATER_PUBKEY"):
        verifier.verify_updater_signature(artifact, "windows")

    commands: list[list[str]] = []
    public_key = b64encode(b"untrusted comment: minisign public key\nRW-public-key\n").decode("ascii")
    monkeypatch.setenv("IAC_CODE_DESKTOP_UPDATER_PUBKEY", public_key)
    monkeypatch.setattr(verifier, "_require_command", lambda name: name)
    monkeypatch.setattr(verifier, "_run", lambda command: commands.append(command))
    verifier.verify_updater_signature(artifact, "windows")

    assert commands == [["minisign", "-Vm", str(payload), "-x", str(signature), "-P", "RW-public-key"]]
