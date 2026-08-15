import json
from pathlib import Path

ROOT = Path(__file__).parents[2]


def test_public_repository_has_no_release_packaging_actions() -> None:
    workflows = ROOT / ".github/workflows"
    retired_workflows = ("desktop-release.yml", "desktop-signed-package.yml")

    for name in retired_workflows:
        assert not (workflows / name).exists()
    desktop_ci = (workflows / "desktop.yml").read_text(encoding="utf-8")
    scope_audit = (ROOT / "desktop/scripts/scope_audit.py").read_text(encoding="utf-8")
    for name in retired_workflows:
        assert name in desktop_ci
        assert name in scope_audit
    assert "TAURI_SIGNING_PRIVATE_KEY_PATH=$key_dir/updater.key" in desktop_ci
    assert "secrets.TAURI_SIGNING_PRIVATE_KEY" not in desktop_ci
    assert "gh release upload" not in desktop_ci
    assert "actions/upload-artifact" not in desktop_ci


def test_public_build_supports_private_local_updater_finalization() -> None:
    script = (ROOT / "desktop/scripts/build_desktop.py").read_text(encoding="utf-8")

    assert '"--skip-updater-artifacts"' in script
    assert "create_updater_artifacts = not args.skip_updater_artifacts" in script


def test_public_release_contract_tracks_private_publisher_interfaces() -> None:
    contract = json.loads((ROOT / "desktop/release/publisher-contract.json").read_text(encoding="utf-8"))

    assert contract == {
        "schemaVersion": 1,
        "minimumPublisherContract": 1,
        "capabilities": [
            "privacy-notice-rendering",
            "release-date-sidecar",
            "skip-updater-artifacts",
            "windows-signed-updater-helper",
        ],
    }
