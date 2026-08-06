from __future__ import annotations

import hashlib
import importlib.util
import json
import plistlib
import re
from pathlib import Path

import pytest

SRC_TAURI = Path(__file__).parents[2] / "desktop/src-tauri"
REPO_ROOT = Path(__file__).parents[2]


def test_desktop_package_lock_uses_public_registry() -> None:
    lock = json.loads((REPO_ROOT / "desktop/package-lock.json").read_text(encoding="utf-8"))
    resolved_urls = [
        package["resolved"]
        for package in lock["packages"].values()
        if isinstance(package, dict) and "resolved" in package
    ]

    assert resolved_urls
    assert all(url.startswith("https://registry.npmjs.org/") for url in resolved_urls)


def test_desktop_signing_material_is_gitignored() -> None:
    gitignore = (REPO_ROOT / ".gitignore").read_text(encoding="utf-8").splitlines()

    assert {
        "desktop/**/*.key",
        "desktop/**/*.pem",
        "desktop/**/*.p12",
        "desktop/**/*.pfx",
        "desktop/**/*.jks",
        "desktop/**/*.keystore",
    } <= set(gitignore)


def _command_from_permission(identifier: str) -> str | None:
    for prefix in ("allow-", "deny-"):
        if identifier.startswith(prefix):
            return identifier[len(prefix) :].replace("-", "_")
    return None


def _capability_commands() -> set[str]:
    commands: set[str] = set()
    for path in sorted((SRC_TAURI / "capabilities").glob("*.json")):
        data = json.loads(path.read_text(encoding="utf-8"))
        for permission in data.get("permissions", []):
            if isinstance(permission, str):
                command = _command_from_permission(permission)
                if command is not None:
                    commands.add(command)
    return commands


def test_build_rs_registers_capability_commands_without_updater_feature() -> None:
    """Tauri parses every ``capabilities/*.json`` at build time, independent of
    which capabilities are active or which cargo features are enabled.
    ``build.rs`` must therefore register the updater command ACL permissions
    unconditionally: a default build (no ``--features updater``) otherwise fails
    to resolve ``allow-check-update`` referenced by
    ``desktop-loopback-updater.json``. The command *handlers* stay gated behind
    ``#[cfg(feature = "updater")]`` and the updater capability is only activated
    for updater-enabled release flavors."""
    build_rs = (SRC_TAURI / "build.rs").read_text(encoding="utf-8")

    # Command ACL registration must not branch on the updater cargo feature.
    assert "CARGO_FEATURE_UPDATER" not in build_rs

    handoff = re.search(r"\.commands\((\w+)\)", build_rs)
    assert handoff is not None, "build.rs must hand a command list to AppManifest::commands"
    array = re.search(
        rf"const {handoff.group(1)}:\s*&\[&str\]\s*=\s*&\[(.*?)\];",
        build_rs,
        re.S,
    )
    assert array is not None, "the registered command list must be a single const array"
    registered = set(re.findall(r'"([^"]+)"', array.group(1)))

    missing = _capability_commands() - registered
    assert not missing, f"capabilities reference commands not registered in build.rs: {sorted(missing)}"
    assert {"check_update", "dismiss_update", "download_update", "install_update"} <= registered

    updater_capability = json.loads(
        (SRC_TAURI / "capabilities/desktop-loopback-updater.json").read_text(encoding="utf-8")
    )
    assert "allow-dismiss-update" in updater_capability["permissions"]


def test_desktop_stack_links_use_allowlisted_native_external_opener() -> None:
    commands = (SRC_TAURI / "src/commands.rs").read_text(encoding="utf-8")
    command_body = commands.split("pub fn open_external_url(", 1)[1].split("\n}", 1)[0]
    assert "require_remote(&window, &state)?" in command_body
    assert 'matches!(parsed.scheme(), "http" | "https")' in command_body
    assert "parsed.host_str().is_none()" in command_body
    assert ".open_url(parsed.as_str()" in command_body

    for capability_name in ("desktop-loopback-external.json", "desktop-loopback-updater.json"):
        capability = json.loads((SRC_TAURI / "capabilities" / capability_name).read_text(encoding="utf-8"))
        assert "allow-open-external-url" in capability["permissions"]


def test_windows_release_host_uses_gui_subsystem() -> None:
    main_rs = (SRC_TAURI / "src/main.rs").read_text(encoding="utf-8")

    assert '#![cfg_attr(not(debug_assertions), windows_subsystem = "windows")]' in main_rs


def test_linux_application_description_is_language_neutral() -> None:
    cargo = (SRC_TAURI / "Cargo.toml").read_text(encoding="utf-8")

    assert 'description = "iac-code"' in cargo
    assert "Desktop host for iac-code" not in cargo


def test_native_menu_waits_for_setup_path_resolver_and_configured_language() -> None:
    lib_rs = (SRC_TAURI / "src/lib.rs").read_text(encoding="utf-8")

    assert ".menu(localized_menu)" not in lib_rs
    setup = lib_rs.split("let builder = builder.setup(|app| {", 1)[1].split("build_main_window(", 1)[0]
    language = setup.index("let language = configured_language(app.handle());")
    menu = setup.index("app.set_menu(localized_menu(app.handle(), &language)?)?;")
    assert language < menu


def test_macos_language_falls_back_to_native_preferred_languages() -> None:
    lib_rs = (SRC_TAURI / "src/lib.rs").read_text(encoding="utf-8")

    assert "fn macos_user_language()" in lib_rs
    assert "NSLocale::preferredLanguages()" in lib_rs
    assert "if let Some(language) = macos_user_language()" in lib_rs


def test_macos_bundle_declares_every_native_menu_localization() -> None:
    info = plistlib.loads((SRC_TAURI / "Info.plist").read_bytes())

    assert info["CFBundleDevelopmentRegion"] == "en"
    assert set(info["CFBundleLocalizations"]) == {"en", "zh-Hans", "es", "fr", "de", "ja", "pt"}


def test_windows_installer_refreshes_existing_desktop_shortcut_icon() -> None:
    flavor = json.loads((SRC_TAURI.parent / "flavors/windows.json").read_text(encoding="utf-8"))
    hooks_path = SRC_TAURI / flavor["bundle"]["windows"]["nsis"]["installerHooks"]
    hooks = hooks_path.read_text(encoding="utf-8")

    assert hooks_path.is_file()
    assert "NSIS_HOOK_POSTINSTALL" in hooks
    assert 'IfFileExists "$DESKTOP\\${PRODUCTNAME}.lnk"' in hooks
    assert 'CreateShortCut "$DESKTOP\\${PRODUCTNAME}.lnk"' in hooks
    assert '"$INSTDIR\\icons\\iac-code-logo-v3.ico" 0' in hooks
    assert "SHChangeNotify" in hooks

    build_script = (SRC_TAURI.parent / "scripts/build_desktop.py").read_text(encoding="utf-8")
    assert 'resources["icons/icon.ico"] = "icons/iac-code-logo-v3.ico"' in build_script


def test_deb_installer_manages_user_desktop_shortcuts() -> None:
    flavor = json.loads((SRC_TAURI.parent / "flavors/deb.json").read_text(encoding="utf-8"))
    deb = flavor["bundle"]["linux"]["deb"]
    postinst = SRC_TAURI / deb["postInstallScript"]
    postrm = SRC_TAURI / deb["postRemoveScript"]

    assert flavor["bundle"]["category"] == "DeveloperTool"
    assert deb["recommends"] == ["fonts-noto-cjk", "fonts-noto-color-emoji"]
    assert postinst.is_file()
    assert postrm.is_file()

    install_script = postinst.read_text(encoding="utf-8")
    remove_script = postrm.read_text(encoding="utf-8")
    assert "/usr/share/applications/iac-code.desktop" in install_script
    assert "xdg-user-dir DESKTOP" in install_script
    assert 'install -m 0755' in install_script
    assert 'shortcut="$desktop_dir/iac-code.desktop"' in install_script
    assert "metadata::trusted true" in install_script
    assert "remove|purge" in remove_script
    assert 'rm -f "$desktop_dir/iac-code.desktop"' in remove_script


def _build_module():
    script = Path(__file__).parents[2] / "desktop/scripts/build_desktop.py"
    spec = importlib.util.spec_from_file_location("iac_code_desktop_build", script)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_build_uses_directly_executable_npm_launcher_for_each_platform() -> None:
    module = _build_module()

    assert module.npm_executable("Windows") == "npm.cmd"
    assert module.npm_executable("Linux") == "npm"
    assert module.npm_executable("Darwin") == "npm"


def test_linux_flavors_select_one_target_and_distinct_capabilities() -> None:
    module = _build_module()
    assert module.FLAVORS.parent == module.DESKTOP
    assert not list(module.TAURI.glob("tauri.*.conf.json"))
    resources = {"sidecar": "sidecar"}

    appimage = module.compose_overlay(
        "appimage",
        resources=resources,
        updater_endpoint="https://updates.example/appimage.json",
        updater_public_key="fake-public-key",
    )
    deb = module.compose_overlay(
        "deb",
        resources=resources,
        updater_endpoint="https://updates.example/deb.json",
        updater_public_key="fake-public-key",
    )

    assert appimage["bundle"]["targets"] == ["appimage"]
    assert appimage["app"]["security"]["capabilities"][-1] == "desktop-loopback-updater"
    assert appimage["bundle"]["createUpdaterArtifacts"] is True
    assert appimage["plugins"]["updater"]["endpoints"] == ["https://updates.example/appimage.json"]
    assert deb["bundle"]["targets"] == ["deb"]
    assert deb["app"]["security"]["capabilities"][-1] == "desktop-loopback-external"
    assert "plugins" not in deb


def test_development_build_without_updater_does_not_expose_native_updater() -> None:
    module = _build_module()

    overlay = module.compose_overlay("macos", resources={"sidecar": "sidecar"})

    assert overlay["app"]["security"]["capabilities"][-1] == "desktop-loopback-external"
    assert "plugins" not in overlay


def test_second_stage_bundle_embeds_updater_without_generating_unsigned_payloads() -> None:
    module = _build_module()

    overlay = module.compose_overlay(
        "macos",
        resources={"sidecar": "sidecar"},
        updater_endpoint="https://updates.example/latest.json",
        updater_public_key="fake-public-key",
        create_updater_artifacts=False,
    )

    assert overlay["app"]["security"]["capabilities"][-1] == "desktop-loopback-updater"
    assert overlay["bundle"]["createUpdaterArtifacts"] is False
    assert overlay["plugins"]["updater"]["endpoints"] == ["https://updates.example/latest.json"]


def test_stale_platform_helpers_are_removed_from_tauri_target(monkeypatch, tmp_path: Path) -> None:
    module = _build_module()
    tauri = tmp_path / "src-tauri"
    release = tauri / "target/release"
    release.mkdir(parents=True)
    kept = release / "iac-code-desktop"
    kept.write_text("host", encoding="utf-8")
    stale = [
        release / "iac-code-desktop-exec",
        release / "iac-code-desktop-exec.d",
        release / "iac-code-desktop-updater.exe",
    ]
    for path in stale:
        path.write_text("stale", encoding="utf-8")
    monkeypatch.setattr(module, "TAURI", tauri)

    module.clear_stale_host_helpers()

    assert kept.exists()
    assert all(not path.exists() for path in stale)


def test_updater_signing_key_path_is_resolved_only_in_child_environment(tmp_path: Path) -> None:
    module = _build_module()
    key_path = tmp_path / "updater.key"
    key_path.write_text("private-key\n", encoding="utf-8")
    environment = {"TAURI_SIGNING_PRIVATE_KEY_PATH": str(key_path)}

    module.configure_updater_signing_environment(environment)

    assert environment["TAURI_SIGNING_PRIVATE_KEY"] == "private-key"


def test_windows_updater_manifest_pins_build_helper_bytes(tmp_path: Path) -> None:
    module = _build_module()
    helper = tmp_path / "iac-code-desktop-updater.exe"
    helper.write_bytes(b"signed-helper-v1")

    manifest = module.create_windows_updater_manifest(
        helper,
        release=True,
        expected_publisher="iac-code Release",
    )

    assert manifest == {
        "schemaVersion": 1,
        "fileName": "iac-code-desktop-updater.exe",
        "sha256": hashlib.sha256(b"signed-helper-v1").hexdigest(),
        "authenticodeRequired": True,
        "expectedPublisher": "iac-code Release",
    }
    module.verify_windows_updater_manifest(helper, manifest)
    helper.write_bytes(b"tampered-helper")
    with pytest.raises(SystemExit, match="does not match"):
        module.verify_windows_updater_manifest(helper, manifest)


def test_windows_release_helper_manifest_fails_closed_without_publisher(tmp_path: Path) -> None:
    module = _build_module()
    helper = tmp_path / "iac-code-desktop-updater.exe"
    helper.write_bytes(b"helper")

    with pytest.raises(SystemExit, match="SIGNING_PUBLISHER"):
        module.create_windows_updater_manifest(helper, release=True, expected_publisher=None)


def test_windows_development_manifest_cannot_claim_release_publisher(tmp_path: Path) -> None:
    module = _build_module()
    helper = tmp_path / "iac-code-desktop-updater.exe"
    helper.write_bytes(b"helper")

    with pytest.raises(SystemExit, match="only valid for a release helper"):
        module.create_windows_updater_manifest(
            helper,
            release=False,
            expected_publisher="unexpected publisher",
        )


def test_windows_build_and_runtime_use_simple_authenticode_publisher_identity() -> None:
    module = _build_module()

    assert "X509NameType]::SimpleName" in module.WINDOWS_AUTHENTICODE_EVIDENCE_SCRIPT
    assert "SignerCertificate.Subject" not in module.WINDOWS_AUTHENTICODE_EVIDENCE_SCRIPT
    runtime = (module.HELPERS / "src/windows_update.rs").read_text(encoding="utf-8")
    assert "CERT_NAME_SIMPLE_DISPLAY_TYPE" in runtime
