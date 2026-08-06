// Tauri parses every `capabilities/*.json` at build time to build the ACL
// manifest, regardless of which capabilities are active or which cargo features
// are enabled. `desktop-loopback-updater.json` references the updater
// `allow-*` permissions, so their commands must be registered here in *every*
// build configuration — otherwise a default build (no `--features updater`)
// fails to resolve `allow-check-update`. This only defines the ACL permissions;
// the command handlers stay gated behind `#[cfg(feature = "updater")]` in
// `src/`, and the updater capability is only activated for updater-enabled
// release flavors.
const COMMANDS: &[&str] = &[
    "complete_bootstrap_check",
    "select_project_directory",
    "retry_start_sidecar",
    "quit_app",
    "confirm_secret_reveal",
    "restart_sidecar",
    "open_diagnostics_directory",
    "open_external_url",
    "check_update",
    "dismiss_update",
    "download_update",
    "install_update",
];

fn main() {
    println!("cargo:rerun-if-env-changed=IAC_CODE_DESKTOP_CHANNEL");
    println!("cargo:rerun-if-env-changed=IAC_CODE_DESKTOP_UPDATER_CONFIGURED");
    println!("cargo:rerun-if-env-changed=IAC_CODE_DESKTOP_UPDATER_HELPER_SHA256");
    println!("cargo:rerun-if-env-changed=IAC_CODE_DESKTOP_UPDATER_HELPER_AUTHENTICODE_REQUIRED");
    println!("cargo:rerun-if-env-changed=IAC_CODE_DESKTOP_UPDATER_HELPER_PUBLISHER");
    println!(
        "cargo:rustc-env=IAC_CODE_DESKTOP_CHANNEL={}",
        std::env::var("IAC_CODE_DESKTOP_CHANNEL").unwrap_or_else(|_| "development".to_string())
    );
    println!(
        "cargo:rustc-env=IAC_CODE_DESKTOP_UPDATER_CONFIGURED={}",
        std::env::var("IAC_CODE_DESKTOP_UPDATER_CONFIGURED").unwrap_or_else(|_| "0".to_string())
    );
    println!(
        "cargo:rustc-env=IAC_CODE_DESKTOP_UPDATER_HELPER_SHA256={}",
        std::env::var("IAC_CODE_DESKTOP_UPDATER_HELPER_SHA256").unwrap_or_default()
    );
    println!(
        "cargo:rustc-env=IAC_CODE_DESKTOP_UPDATER_HELPER_AUTHENTICODE_REQUIRED={}",
        std::env::var("IAC_CODE_DESKTOP_UPDATER_HELPER_AUTHENTICODE_REQUIRED")
            .unwrap_or_else(|_| "0".to_string())
    );
    println!(
        "cargo:rustc-env=IAC_CODE_DESKTOP_UPDATER_HELPER_PUBLISHER={}",
        std::env::var("IAC_CODE_DESKTOP_UPDATER_HELPER_PUBLISHER").unwrap_or_default()
    );
    tauri_build::try_build(
        tauri_build::Attributes::new()
            .app_manifest(tauri_build::AppManifest::new().commands(COMMANDS)),
    )
    .expect("failed to build the iac-code Desktop manifest");
}
