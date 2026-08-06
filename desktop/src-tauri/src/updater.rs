use crate::lifecycle::LifecycleState;
use crate::{sidecar, AppState};
use anyhow::{bail, Context, Result};
use parking_lot::Mutex;
#[cfg(windows)]
use serde::Deserialize;
use serde::Serialize;
use tauri::{AppHandle, Manager};
use tauri_plugin_updater::{Update, UpdaterExt};
use uuid::Uuid;

#[cfg(windows)]
use iac_code_desktop_helpers::windows_update::{
    current_process_creation_time, load_marker, open_source_process, save_marker, sha256_file,
    verify_helper_integrity, UpdateAttempt, UpdateAttemptPhase, MARKER_NAME,
};
#[cfg(windows)]
use std::fs::{self, File, OpenOptions};
#[cfg(windows)]
use std::io::Write;
#[cfg(windows)]
use std::os::windows::process::CommandExt;
#[cfg(windows)]
use std::path::Path;
#[cfg(windows)]
use std::process::{Child, Command};
#[cfg(windows)]
use std::time::{Duration, Instant};

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
enum UpdatePhase {
    Checking,
    Available,
    Downloading,
    Downloaded,
    Installing,
}

struct UpdateOperation {
    operation_id: Uuid,
    source_generation: u64,
    phase: UpdatePhase,
    update: Option<Update>,
    bytes: Option<Vec<u8>>,
}

#[derive(Default)]
pub struct UpdaterCoordinator {
    operation: Mutex<Option<UpdateOperation>>,
}

impl UpdaterCoordinator {
    fn invalidate_for_lifecycle(&self) {
        let mut operation = self.operation.lock();
        if operation
            .as_ref()
            .is_some_and(|operation| operation.phase != UpdatePhase::Installing)
        {
            operation.take();
        }
    }

    fn invalidate_all(&self) {
        self.operation.lock().take();
    }

    fn invalidate_if_operation(&self, operation_id: Uuid) {
        let mut operation = self.operation.lock();
        if operation
            .as_ref()
            .is_some_and(|operation| operation.operation_id == operation_id)
        {
            operation.take();
        }
    }
}

pub fn invalidate_for_lifecycle(app: &AppHandle) {
    app.state::<AppState>().updater.invalidate_for_lifecycle();
}

pub fn invalidate_all(app: &AppHandle) {
    app.state::<AppState>().updater.invalidate_all();
}

fn operation_identity_is_current(
    operation: &UpdateOperation,
    operation_id: Uuid,
    source_generation: u64,
    lifecycle_is_running: bool,
    current_generation: Option<u64>,
) -> bool {
    lifecycle_is_running
        && current_generation == Some(source_generation)
        && operation.operation_id == operation_id
        && operation.source_generation == source_generation
}

fn operation_context_is_current(
    app: &AppHandle,
    operation_id: Uuid,
    source_generation: u64,
) -> bool {
    let lifecycle_is_running =
        app.state::<AppState>().lifecycle.lock().state == LifecycleState::Running;
    let current_generation = sidecar::current_generation(app).ok();
    app.state::<AppState>()
        .updater
        .operation
        .lock()
        .as_ref()
        .is_some_and(|operation| {
            operation_identity_is_current(
                operation,
                operation_id,
                source_generation,
                lifecycle_is_running,
                current_generation,
            )
        })
}

fn update_is_dismissed(dismissed_version: Option<&str>, latest_version: &str) -> bool {
    dismissed_version == Some(latest_version)
}

#[derive(Clone, Debug, Serialize)]
#[serde(rename_all = "camelCase")]
pub struct UpdateStatus {
    pub available: bool,
    pub current_version: String,
    pub latest_version: Option<String>,
    pub notes: Option<String>,
    pub phase: String,
}

impl UpdateStatus {
    fn none(app: &AppHandle) -> Self {
        Self {
            available: false,
            current_version: app.package_info().version.to_string(),
            latest_version: None,
            notes: None,
            phase: "idle".to_string(),
        }
    }

    fn from_operation(app: &AppHandle, operation: &UpdateOperation) -> Self {
        let update = operation.update.as_ref();
        Self {
            available: update.is_some(),
            current_version: app.package_info().version.to_string(),
            latest_version: update.map(|value| value.version.clone()),
            notes: update.and_then(|value| value.body.clone()),
            phase: match operation.phase {
                UpdatePhase::Checking => "checking",
                UpdatePhase::Available => "available",
                UpdatePhase::Downloading => "downloading",
                UpdatePhase::Downloaded => "downloaded",
                UpdatePhase::Installing => "installing",
            }
            .to_string(),
        }
    }
}

pub async fn check(app: &AppHandle) -> Result<UpdateStatus> {
    let coordinator = &app.state::<AppState>().updater;
    if app.state::<AppState>().lifecycle.lock().state != LifecycleState::Running {
        bail!("Desktop updater is only available while the local runtime is running");
    }
    let source_generation = sidecar::current_generation(app)?;
    let dismissed_version = app
        .state::<AppState>()
        .host_state
        .lock()
        .state()
        .dismissed_update_version
        .clone();
    let operation_id = {
        let mut guard = coordinator.operation.lock();
        if let Some(operation) = guard.as_ref() {
            if operation.source_generation == source_generation {
                if operation.update.as_ref().is_some_and(|candidate| {
                    update_is_dismissed(dismissed_version.as_deref(), &candidate.version)
                }) {
                    guard.take();
                    return Ok(UpdateStatus::none(app));
                }
                return Ok(UpdateStatus::from_operation(app, operation));
            }
            guard.take();
        }
        let operation_id = Uuid::new_v4();
        *guard = Some(UpdateOperation {
            operation_id,
            source_generation,
            phase: UpdatePhase::Checking,
            update: None,
            bytes: None,
        });
        operation_id
    };

    let update = match app.updater()?.check().await {
        Ok(update) => update,
        Err(error) => {
            let mut guard = coordinator.operation.lock();
            if guard
                .as_ref()
                .is_some_and(|operation| operation.operation_id == operation_id)
            {
                guard.take();
            }
            return Err(error.into());
        }
    };
    if !operation_context_is_current(app, operation_id, source_generation) {
        coordinator.invalidate_if_operation(operation_id);
        bail!("Desktop updater check belongs to a stale lifecycle operation");
    }
    let dismissed_version = app
        .state::<AppState>()
        .host_state
        .lock()
        .state()
        .dismissed_update_version
        .clone();
    if update.as_ref().is_some_and(|candidate| {
        update_is_dismissed(dismissed_version.as_deref(), &candidate.version)
    }) {
        coordinator.invalidate_if_operation(operation_id);
        return Ok(UpdateStatus::none(app));
    }
    let mut guard = coordinator.operation.lock();
    let operation = guard
        .as_mut()
        .filter(|operation| {
            operation.operation_id == operation_id
                && operation.source_generation == source_generation
        })
        .context("Desktop updater check belongs to a stale operation")?;
    let Some(update) = update else {
        *guard = None;
        return Ok(UpdateStatus::none(app));
    };
    operation.phase = UpdatePhase::Available;
    operation.update = Some(update);
    Ok(UpdateStatus::from_operation(app, operation))
}

pub async fn download(app: &AppHandle) -> Result<UpdateStatus> {
    let coordinator = &app.state::<AppState>().updater;
    if app.state::<AppState>().lifecycle.lock().state != LifecycleState::Running {
        bail!("Desktop updater is only available while the local runtime is running");
    }
    let source_generation = sidecar::current_generation(app)?;
    let (operation_id, update) = {
        let mut guard = coordinator.operation.lock();
        let operation = guard.as_mut().context("no Desktop update is available")?;
        if matches!(
            operation.phase,
            UpdatePhase::Downloading | UpdatePhase::Downloaded
        ) {
            return Ok(UpdateStatus::from_operation(app, operation));
        }
        if operation.source_generation != source_generation
            || operation.phase != UpdatePhase::Available
        {
            bail!("Desktop updater is busy or belongs to a stale sidecar generation");
        }
        operation.phase = UpdatePhase::Downloading;
        (
            operation.operation_id,
            operation
                .update
                .clone()
                .context("Desktop update metadata is missing")?,
        )
    };
    let bytes = match update.download(|_, _| {}, || {}).await {
        Ok(bytes) => bytes,
        Err(error) => {
            let is_current = operation_context_is_current(app, operation_id, source_generation);
            let mut guard = coordinator.operation.lock();
            if let Some(operation) = guard
                .as_mut()
                .filter(|operation| operation.operation_id == operation_id)
            {
                if is_current {
                    operation.phase = UpdatePhase::Available;
                } else {
                    guard.take();
                }
            }
            return Err(error.into());
        }
    };
    if !operation_context_is_current(app, operation_id, source_generation) {
        coordinator.invalidate_if_operation(operation_id);
        bail!("Desktop updater download belongs to a stale lifecycle operation");
    }
    let mut guard = coordinator.operation.lock();
    let operation = guard
        .as_mut()
        .filter(|operation| {
            operation.operation_id == operation_id
                && operation.source_generation == source_generation
        })
        .context("Desktop updater download belongs to a stale operation")?;
    operation.phase = UpdatePhase::Downloaded;
    operation.bytes = Some(bytes);
    Ok(UpdateStatus::from_operation(app, operation))
}

pub async fn install(app: &AppHandle) -> Result<()> {
    let coordinator = &app.state::<AppState>().updater;
    if app.state::<AppState>().lifecycle.lock().state != LifecycleState::Running {
        bail!("Desktop updater is only available while the local runtime is running");
    }
    let source_generation = sidecar::current_generation(app)?;
    let (operation_id, update, bytes) = {
        let mut guard = coordinator.operation.lock();
        let operation = guard
            .as_mut()
            .context("no Desktop update has been downloaded")?;
        if operation.source_generation != source_generation
            || operation.phase != UpdatePhase::Downloaded
        {
            bail!("Desktop updater is busy or belongs to a stale sidecar generation");
        }
        operation.phase = UpdatePhase::Installing;
        (
            operation.operation_id,
            operation
                .update
                .clone()
                .context("Desktop update metadata is missing")?,
            operation
                .bytes
                .clone()
                .context("Desktop update artifact is missing")?,
        )
    };

    let stop_app = app.clone();
    let stop_result = match tauri::async_runtime::spawn_blocking(move || {
        sidecar::stop_with_dialog(&stop_app, "update")
    })
    .await
    {
        Ok(result) => result,
        Err(error) => {
            Err(anyhow::Error::from(error).context("join Desktop sidecar stop operation"))
        }
    };
    if let Err(error) = stop_result {
        let can_restore = operation_context_is_current(app, operation_id, source_generation);
        let mut guard = coordinator.operation.lock();
        if let Some(operation) = guard.as_mut().filter(|operation| {
            operation.operation_id == operation_id
                && operation.source_generation == source_generation
        }) {
            if can_restore {
                operation.phase = UpdatePhase::Downloaded;
            } else {
                guard.take();
            }
        }
        return Err(error).context("Desktop update is waiting for active work to finish");
    }
    app.state::<AppState>()
        .lifecycle
        .lock()
        .begin(LifecycleState::Updating);

    #[cfg(not(windows))]
    let install_result: Result<()> =
        match tauri::async_runtime::spawn_blocking(move || update.install(&bytes)).await {
            Ok(result) => result.map_err(anyhow::Error::from),
            Err(error) => {
                Err(anyhow::Error::from(error).context("join Desktop update install operation"))
            }
        };
    #[cfg(windows)]
    let install_result = {
        let target_version = update.version.clone();
        let install_app = app.clone();
        match tauri::async_runtime::spawn_blocking(move || {
            stage_windows_update(&install_app, &target_version, &bytes)
        })
        .await
        {
            Ok(result) => result,
            Err(error) => {
                Err(anyhow::Error::from(error).context("join staged Windows update handoff"))
            }
        }
    };
    if let Err(error) = install_result {
        let project = app
            .state::<AppState>()
            .host_state
            .lock()
            .state()
            .recent_project
            .clone();
        let recovery_result = if let Some(project) = project {
            let recovery_app = app.clone();
            match tauri::async_runtime::spawn_blocking(move || {
                sidecar::start(&recovery_app, &project)
            })
            .await
            {
                Ok(result) => result,
                Err(error) => {
                    Err(anyhow::Error::from(error).context("join Desktop sidecar update recovery"))
                }
            }
        } else {
            Err(anyhow::anyhow!("no Desktop project is selected"))
        };
        invalidate_all(app);
        match recovery_result {
            Ok(port) => {
                sidecar::navigate_to_port(app, port)?;
                #[cfg(windows)]
                cleanup_failed_windows_handoff(app);
            }
            Err(recovery_error) => {
                sidecar::show_localized_recovery_page(app, "update_recovery_failed");
                return Err(error).context(format!(
                    "install Desktop update; sidecar recovery also failed: {recovery_error:#}"
                ));
            }
        }
        return Err(error).context("install Desktop update");
    }
    #[cfg(windows)]
    {
        app.exit(0);
        return Ok(());
    }
    #[cfg(not(windows))]
    app.restart();
}

#[cfg(windows)]
fn cleanup_failed_windows_handoff(app: &AppHandle) {
    let marker = app
        .state::<AppState>()
        .paths
        .host_state_dir
        .join(MARKER_NAME);
    let staging = load_marker(&marker).ok().and_then(|attempt| {
        if matches!(attempt.phase, UpdateAttemptPhase::Failed) {
            attempt
                .helper_executable_path
                .parent()
                .map(Path::to_path_buf)
        } else {
            None
        }
    });
    if staging.is_none() {
        return;
    }
    if let Some(staging) = staging {
        let mut removed = false;
        for _ in 0..20 {
            match fs::remove_dir_all(&staging) {
                Ok(()) => {
                    removed = true;
                    break;
                }
                Err(error) if error.kind() == std::io::ErrorKind::NotFound => {
                    removed = true;
                    break;
                }
                Err(_) => std::thread::sleep(Duration::from_millis(25)),
            }
        }
        if !removed {
            return;
        }
    }
    let _ = fs::remove_file(&marker);
}

#[cfg(windows)]
#[derive(Debug, Deserialize)]
#[serde(rename_all = "camelCase")]
struct WindowsUpdaterHelperManifest {
    schema_version: u32,
    file_name: String,
    sha256: String,
    authenticode_required: bool,
    expected_publisher: Option<String>,
}

#[cfg(windows)]
fn load_windows_updater_helper_manifest(app: &AppHandle) -> Result<WindowsUpdaterHelperManifest> {
    let path = app
        .path()
        .resource_dir()?
        .join("bin/iac-code-desktop-updater.manifest.json");
    let manifest: WindowsUpdaterHelperManifest =
        serde_json::from_slice(&fs::read(&path).context("read bundled updater helper manifest")?)
            .context("parse bundled updater helper manifest")?;
    if manifest.schema_version != 1 || manifest.file_name != "iac-code-desktop-updater.exe" {
        bail!("bundled updater helper manifest is incompatible");
    }
    let compiled_publisher = match env!("IAC_CODE_DESKTOP_UPDATER_HELPER_PUBLISHER") {
        "" => None,
        value => Some(value),
    };
    if manifest.sha256 != env!("IAC_CODE_DESKTOP_UPDATER_HELPER_SHA256")
        || manifest.authenticode_required
            != (env!("IAC_CODE_DESKTOP_UPDATER_HELPER_AUTHENTICODE_REQUIRED") == "1")
        || manifest.expected_publisher.as_deref() != compiled_publisher
    {
        bail!("bundled updater helper manifest does not match the Host build identity");
    }
    if manifest.sha256.len() != 64
        || !manifest
            .sha256
            .bytes()
            .all(|value| value.is_ascii_hexdigit())
    {
        bail!("bundled updater helper manifest contains an invalid SHA-256");
    }
    Ok(manifest)
}

#[cfg(windows)]
fn write_synced(path: &Path, bytes: &[u8]) -> Result<()> {
    let mut file = OpenOptions::new().create_new(true).write(true).open(path)?;
    file.write_all(bytes)?;
    file.sync_all()?;
    Ok(())
}

#[cfg(windows)]
struct AttemptStagingGuard {
    path: std::path::PathBuf,
    preserve: bool,
}

#[cfg(windows)]
impl AttemptStagingGuard {
    fn new(path: std::path::PathBuf) -> Self {
        Self {
            path,
            preserve: false,
        }
    }

    fn preserve_for_recovery(&mut self) {
        self.preserve = true;
    }
}

#[cfg(windows)]
impl Drop for AttemptStagingGuard {
    fn drop(&mut self) {
        if !self.preserve {
            let _ = fs::remove_dir_all(&self.path);
        }
    }
}

#[cfg(windows)]
fn relaunch_args() -> Vec<String> {
    let mut result = Vec::new();
    let mut skip_next = false;
    for argument in std::env::args().skip(1) {
        if skip_next {
            skip_next = false;
            continue;
        }
        if matches!(
            argument.as_str(),
            "--desktop-update-attempt" | "--desktop-update-recovery"
        ) {
            skip_next = true;
            continue;
        }
        result.push(argument);
    }
    result
}

#[cfg(windows)]
fn terminate_and_wait_helper(helper: &mut Child) {
    if helper.try_wait().ok().flatten().is_none() {
        let _ = helper.kill();
    }
    let _ = helper.wait();
}

#[cfg(windows)]
fn helper_acknowledgement_ready(
    current: &UpdateAttempt,
    expected_attempt_id: &str,
    expected_helper_pid: u32,
) -> Result<bool> {
    if current.attempt_id != expected_attempt_id {
        bail!("Windows update marker was replaced by another operation");
    }
    match current.phase {
        UpdateAttemptPhase::HelperReady
            if current.helper_pid == Some(expected_helper_pid)
                && current.helper_creation_time.is_some() =>
        {
            Ok(true)
        }
        UpdateAttemptPhase::HelperReady => {
            bail!("Windows updater helper acknowledgement has the wrong process identity")
        }
        UpdateAttemptPhase::Failed => {
            bail!(
                "Windows updater helper failed before handoff: {}",
                current.error.as_deref().unwrap_or("unknown error")
            )
        }
        _ => Ok(false),
    }
}

#[cfg(windows)]
fn wait_for_windows_helper_ready(
    marker: &Path,
    attempt: &UpdateAttempt,
    helper: &mut Child,
) -> Result<()> {
    let deadline = Instant::now() + Duration::from_secs(10);
    while Instant::now() < deadline {
        if let Some(status) = helper
            .try_wait()
            .context("poll staged Windows updater helper")?
        {
            bail!("Windows updater helper exited before handoff with {status}");
        }
        let current = load_marker(marker)?;
        if helper_acknowledgement_ready(&current, &attempt.attempt_id, helper.id())? {
            let identity = open_source_process(
                helper.id(),
                current
                    .helper_creation_time
                    .context("helper creation time is missing")?,
            )?;
            drop(identity);
            if helper.try_wait()?.is_some() {
                bail!("Windows updater helper exited while acknowledging handoff");
            }
            return Ok(());
        }
        std::thread::sleep(Duration::from_millis(50));
    }
    bail!("Windows updater helper did not acknowledge handoff")
}

#[cfg(windows)]
fn stage_windows_update(app: &AppHandle, target_version: &str, bytes: &[u8]) -> Result<()> {
    const CREATE_NO_WINDOW: u32 = 0x0800_0000;
    let state = app.state::<AppState>();
    let attempt_id = Uuid::new_v4().to_string();
    let attempt_dir = state.paths.host_state_dir.join("updates").join(&attempt_id);
    fs::create_dir_all(&attempt_dir).context("create Windows update staging directory")?;
    let mut staging_guard = AttemptStagingGuard::new(attempt_dir.clone());
    let artifact_path = attempt_dir.join("update.nsis.zip");
    write_synced(&artifact_path, bytes).context("stage verified Windows update artifact")?;
    let helper_source = app
        .path()
        .resource_dir()?
        .join("bin/iac-code-desktop-updater.exe")
        .canonicalize()
        .context("resolve bundled Windows updater helper")?;
    let helper_manifest = load_windows_updater_helper_manifest(app)?;
    verify_helper_integrity(
        &helper_source,
        &helper_manifest.sha256,
        helper_manifest.authenticode_required,
        helper_manifest.expected_publisher.as_deref(),
    )
    .context("verify bundled Windows updater helper")?;
    let helper_path = attempt_dir.join("iac-code-desktop-updater.exe");
    fs::copy(&helper_source, &helper_path).context("stage Windows updater helper")?;
    File::open(&helper_path)?.sync_all()?;
    let helper_path = helper_path.canonicalize()?;
    verify_helper_integrity(
        &helper_path,
        &helper_manifest.sha256,
        helper_manifest.authenticode_required,
        helper_manifest.expected_publisher.as_deref(),
    )
    .context("verify staged Windows updater helper")?;
    let artifact_path = artifact_path.canonicalize()?;
    let current_executable_path = std::env::current_exe()?.canonicalize()?;
    let marker = state.paths.host_state_dir.join(MARKER_NAME);
    let mut attempt = UpdateAttempt {
        attempt_id,
        source_version: app.package_info().version.to_string(),
        target_version: target_version.to_string(),
        source_host_pid: std::process::id(),
        source_host_creation_time: current_process_creation_time()?,
        verified_artifact_sha256: sha256_file(&artifact_path)?,
        verified_artifact_path: artifact_path,
        current_executable_path,
        relaunch_args: relaunch_args(),
        helper_bundle_sha256: helper_manifest.sha256,
        helper_authenticode_required: helper_manifest.authenticode_required,
        helper_expected_publisher: helper_manifest.expected_publisher,
        helper_executable_path: helper_path.clone(),
        installer_executable_path: None,
        helper_pid: None,
        helper_creation_time: None,
        recovery_relaunch_started: false,
        phase: UpdateAttemptPhase::Prepared,
        error: None,
        updated_at_unix_ms: 0,
    };
    save_marker(&marker, &mut attempt)?;
    staging_guard.preserve_for_recovery();
    let helper_result = Command::new(&helper_path)
        .arg("--marker")
        .arg(&marker)
        .current_dir(&attempt_dir)
        .creation_flags(CREATE_NO_WINDOW)
        .spawn();
    let mut helper = match helper_result {
        Ok(helper) => helper,
        Err(error) => {
            attempt.phase = UpdateAttemptPhase::Failed;
            attempt.error = Some(format!("start staged Windows updater helper: {error}"));
            let _ = save_marker(&marker, &mut attempt);
            return Err(error).context("start staged Windows updater helper");
        }
    };
    if let Err(error) = wait_for_windows_helper_ready(&marker, &attempt, &mut helper) {
        terminate_and_wait_helper(&mut helper);
        let mut failed = load_marker(&marker).unwrap_or(attempt);
        failed.phase = UpdateAttemptPhase::Failed;
        failed.error = Some(format!("{error:#}"));
        let _ = save_marker(&marker, &mut failed);
        return Err(error);
    }
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::*;

    fn coordinator_with_phase(phase: UpdatePhase) -> (UpdaterCoordinator, Uuid) {
        let operation_id = Uuid::new_v4();
        let coordinator = UpdaterCoordinator::default();
        *coordinator.operation.lock() = Some(UpdateOperation {
            operation_id,
            source_generation: 41,
            phase,
            update: None,
            bytes: None,
        });
        (coordinator, operation_id)
    }

    #[test]
    fn lifecycle_invalidation_clears_network_operation_but_preserves_install_handoff() {
        let (network, _) = coordinator_with_phase(UpdatePhase::Downloading);
        network.invalidate_for_lifecycle();
        assert!(network.operation.lock().is_none());

        let (installing, _) = coordinator_with_phase(UpdatePhase::Installing);
        installing.invalidate_for_lifecycle();
        assert_eq!(
            installing
                .operation
                .lock()
                .as_ref()
                .map(|value| value.phase),
            Some(UpdatePhase::Installing)
        );
    }

    #[test]
    fn stale_completion_cannot_match_new_generation_or_non_running_lifecycle() {
        let (coordinator, operation_id) = coordinator_with_phase(UpdatePhase::Downloading);
        let guard = coordinator.operation.lock();
        let operation = guard.as_ref().unwrap();
        assert!(operation_identity_is_current(
            operation,
            operation_id,
            41,
            true,
            Some(41)
        ));
        assert!(!operation_identity_is_current(
            operation,
            operation_id,
            41,
            true,
            Some(42)
        ));
        assert!(!operation_identity_is_current(
            operation,
            operation_id,
            41,
            false,
            Some(41)
        ));
    }

    #[test]
    fn stale_completion_cannot_clear_a_newer_updater_operation() {
        let (coordinator, stale_id) = coordinator_with_phase(UpdatePhase::Checking);
        let replacement_id = Uuid::new_v4();
        *coordinator.operation.lock() = Some(UpdateOperation {
            operation_id: replacement_id,
            source_generation: 42,
            phase: UpdatePhase::Checking,
            update: None,
            bytes: None,
        });

        coordinator.invalidate_if_operation(stale_id);

        assert_eq!(
            coordinator
                .operation
                .lock()
                .as_ref()
                .map(|operation| operation.operation_id),
            Some(replacement_id)
        );
    }

    #[test]
    fn only_the_exact_dismissed_update_version_is_suppressed() {
        assert!(update_is_dismissed(Some("0.12.0"), "0.12.0"));
        assert!(!update_is_dismissed(Some("0.12.0"), "0.12.1"));
        assert!(!update_is_dismissed(None, "0.12.0"));
    }

    #[cfg(windows)]
    fn windows_attempt(phase: UpdateAttemptPhase) -> UpdateAttempt {
        UpdateAttempt {
            attempt_id: "attempt-1".to_string(),
            source_version: "1.0.0".to_string(),
            target_version: "1.1.0".to_string(),
            source_host_pid: 1,
            source_host_creation_time: 2,
            verified_artifact_path: "artifact.zip".into(),
            verified_artifact_sha256: "a".repeat(64),
            current_executable_path: "host.exe".into(),
            relaunch_args: Vec::new(),
            helper_executable_path: "helper.exe".into(),
            helper_bundle_sha256: "b".repeat(64),
            helper_authenticode_required: false,
            helper_expected_publisher: None,
            installer_executable_path: None,
            helper_pid: None,
            helper_creation_time: None,
            recovery_relaunch_started: false,
            phase,
            error: None,
            updated_at_unix_ms: 0,
        }
    }

    #[cfg(windows)]
    #[test]
    fn helper_ack_waits_for_delayed_identity_and_rejects_wrong_process() {
        let prepared = windows_attempt(UpdateAttemptPhase::Prepared);
        assert!(!helper_acknowledgement_ready(&prepared, "attempt-1", 91).unwrap());

        let mut ready = windows_attempt(UpdateAttemptPhase::HelperReady);
        ready.helper_pid = Some(91);
        ready.helper_creation_time = Some(101);
        assert!(helper_acknowledgement_ready(&ready, "attempt-1", 91).unwrap());
        assert!(helper_acknowledgement_ready(&ready, "attempt-1", 92).is_err());
    }
}
