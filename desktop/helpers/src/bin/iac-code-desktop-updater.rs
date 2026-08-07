#![cfg_attr(windows, windows_subsystem = "windows")]

#[cfg(windows)]
mod windows_helper {
    use anyhow::{bail, Context, Result};
    use iac_code_desktop_helpers::windows_update::{
        current_process_creation_time, load_marker, open_source_process, save_marker,
        verify_helper_integrity, UpdateAttempt, UpdateAttemptPhase,
    };
    use std::ffi::OsStr;
    use std::fs::{File, OpenOptions};
    use std::io::Write;
    use std::os::windows::ffi::OsStrExt;
    use std::os::windows::process::CommandExt;
    use std::path::{Path, PathBuf};
    use std::process::{Child, Command};
    use std::time::{Duration, Instant};
    use windows::core::PCWSTR;
    use windows::Win32::Foundation::{CloseHandle, HANDLE, WAIT_OBJECT_0};
    use windows::Win32::System::Threading::{GetExitCodeProcess, WaitForSingleObject, INFINITE};
    use windows::Win32::UI::Shell::{ShellExecuteExW, SEE_MASK_NOCLOSEPROCESS, SHELLEXECUTEINFOW};
    use windows::Win32::UI::WindowsAndMessaging::SW_HIDE;
    use zip::ZipArchive;

    const CREATE_NO_WINDOW: u32 = 0x0800_0000;

    fn parse_marker_path() -> Result<PathBuf> {
        let mut arguments = std::env::args_os().skip(1);
        while let Some(argument) = arguments.next() {
            if argument == "--marker" {
                return arguments
                    .next()
                    .map(PathBuf::from)
                    .context("--marker requires a path");
            }
        }
        bail!("missing --marker")
    }

    fn verify_staged_inputs(attempt: &UpdateAttempt) -> Result<()> {
        let current = std::env::current_exe()?.canonicalize()?;
        if current != attempt.helper_executable_path.canonicalize()? {
            bail!("updater helper is not running from the attempt staging directory");
        }
        verify_helper_integrity(
            &current,
            &attempt.helper_bundle_sha256,
            attempt.helper_authenticode_required,
            attempt.helper_expected_publisher.as_deref(),
        )?;
        if iac_code_desktop_helpers::windows_update::sha256_file(&attempt.verified_artifact_path)?
            != attempt.verified_artifact_sha256
        {
            bail!("verified updater artifact hash changed after download");
        }
        Ok(())
    }

    fn installer_from_archive(attempt: &UpdateAttempt) -> Result<PathBuf> {
        let file = File::open(&attempt.verified_artifact_path)?;
        let mut archive = ZipArchive::new(file).context("open NSIS updater archive")?;
        let mut installer_index = None;
        let mut installer_name = None;
        for index in 0..archive.len() {
            let entry = archive.by_index(index)?;
            if entry.is_dir() {
                continue;
            }
            let enclosed = entry
                .enclosed_name()
                .context("updater archive contains an unsafe path")?;
            if enclosed.components().count() != 1
                || enclosed.extension().and_then(OsStr::to_str) != Some("exe")
            {
                bail!("NSIS updater archive must contain only one root executable");
            }
            if entry
                .unix_mode()
                .is_some_and(|mode| mode & 0o170_000 == 0o120_000)
            {
                bail!("NSIS updater archive cannot contain links");
            }
            if installer_index.replace(index).is_some() {
                bail!("NSIS updater archive contains multiple installers");
            }
            installer_name = Some(enclosed.to_path_buf());
        }
        let index = installer_index.context("NSIS updater archive contains no installer")?;
        let name = installer_name.context("NSIS updater installer name is missing")?;
        let attempt_dir = attempt
            .helper_executable_path
            .parent()
            .context("updater helper has no staging directory")?;
        let destination = attempt_dir.join(name);
        let mut source = archive.by_index(index)?;
        let mut output = OpenOptions::new()
            .create_new(true)
            .write(true)
            .open(&destination)
            .context("create staged NSIS installer")?;
        std::io::copy(&mut source, &mut output)?;
        output.flush()?;
        output.sync_all()?;
        Ok(destination.canonicalize()?)
    }

    fn wide(value: &OsStr) -> Vec<u16> {
        value.encode_wide().chain(std::iter::once(0)).collect()
    }

    struct InstallerHandle(HANDLE);

    impl Drop for InstallerHandle {
        fn drop(&mut self) {
            let _ = unsafe { CloseHandle(self.0) };
        }
    }

    fn launch_installer(path: &Path) -> Result<InstallerHandle> {
        let file = wide(path.as_os_str());
        let parameters = wide(OsStr::new("/P /UPDATE"));
        let directory = path.parent().map(|value| wide(value.as_os_str()));
        let mut execution = SHELLEXECUTEINFOW {
            cbSize: std::mem::size_of::<SHELLEXECUTEINFOW>() as u32,
            fMask: SEE_MASK_NOCLOSEPROCESS,
            lpFile: PCWSTR(file.as_ptr()),
            lpParameters: PCWSTR(parameters.as_ptr()),
            lpDirectory: directory
                .as_ref()
                .map_or(PCWSTR::null(), |value| PCWSTR(value.as_ptr())),
            nShow: SW_HIDE.0,
            ..Default::default()
        };
        unsafe { ShellExecuteExW(&mut execution) }.context("launch NSIS updater")?;
        if execution.hProcess.is_invalid() {
            bail!("NSIS updater did not return a process handle");
        }
        Ok(InstallerHandle(execution.hProcess))
    }

    fn wait_for_process(process: HANDLE) -> Result<u32> {
        if unsafe { WaitForSingleObject(process, INFINITE) } != WAIT_OBJECT_0 {
            bail!("wait for Windows updater process failed");
        }
        let mut exit_code = 0_u32;
        unsafe { GetExitCodeProcess(process, &mut exit_code) }
            .context("read Windows updater exit code")?;
        Ok(exit_code)
    }

    fn relaunch_host(attempt: &UpdateAttempt, recovery: bool) -> Result<Child> {
        let mut command = Command::new(&attempt.current_executable_path);
        command
            .args(&attempt.relaunch_args)
            .creation_flags(CREATE_NO_WINDOW);
        if recovery {
            command
                .arg("--desktop-update-recovery")
                .arg(&attempt.attempt_id);
        } else {
            command
                .arg("--desktop-update-attempt")
                .arg(&attempt.attempt_id);
        }
        command.spawn().context("relaunch Desktop Host")
    }

    fn fail_after_handoff(marker: &Path, attempt: &mut UpdateAttempt, error: anyhow::Error) {
        attempt.phase = UpdateAttemptPhase::Failed;
        attempt.error = Some(format!("{error:#}"));
        let should_relaunch = !attempt.recovery_relaunch_started;
        attempt.recovery_relaunch_started = true;
        let _ = save_marker(marker, attempt);
        if should_relaunch {
            let _ = relaunch_host(attempt, true);
        }
    }

    fn continue_after_host_exit(marker: &Path, attempt: &mut UpdateAttempt) -> Result<()> {
        let installer = installer_from_archive(attempt)?;
        attempt.installer_executable_path = Some(installer.clone());
        attempt.phase = UpdateAttemptPhase::InstallerRunning;
        save_marker(marker, attempt)?;
        let installer_process = launch_installer(&installer)?;
        let exit_code = wait_for_process(installer_process.0)?;
        if exit_code != 0 {
            bail!("NSIS updater exited with code {exit_code}");
        }
        attempt.phase = UpdateAttemptPhase::HandoffPending;
        save_marker(marker, attempt)?;
        let mut target_host = relaunch_host(attempt, false)?;
        let deadline = Instant::now() + Duration::from_secs(60);
        while Instant::now() < deadline {
            if let Ok(current) = load_marker(marker) {
                if current.attempt_id != attempt.attempt_id {
                    bail!("Windows update marker was replaced during target handoff");
                }
                if matches!(current.phase, UpdateAttemptPhase::Complete) {
                    return Ok(());
                }
                if matches!(current.phase, UpdateAttemptPhase::Failed) {
                    return Ok(());
                }
            }
            std::thread::sleep(Duration::from_millis(100));
        }
        if target_host.try_wait()?.is_none() {
            target_host
                .kill()
                .context("terminate unresponsive updated Desktop Host")?;
            target_host
                .wait()
                .context("wait for unresponsive updated Desktop Host")?;
        }
        attempt.phase = UpdateAttemptPhase::Failed;
        attempt.error = Some("updated Desktop Host did not complete the handoff".to_string());
        attempt.recovery_relaunch_started = true;
        save_marker(marker, attempt)?;
        let _recovery_host =
            relaunch_host(attempt, true).context("launch Desktop update recovery Host")?;
        Ok(())
    }

    pub fn run() -> Result<()> {
        let marker = parse_marker_path()?.canonicalize()?;
        let mut attempt = load_marker(&marker)?;
        verify_staged_inputs(&attempt)?;
        let source =
            open_source_process(attempt.source_host_pid, attempt.source_host_creation_time)?;
        attempt.helper_pid = Some(std::process::id());
        attempt.helper_creation_time = Some(current_process_creation_time()?);
        attempt.phase = UpdateAttemptPhase::HelperReady;
        save_marker(&marker, &mut attempt)?;
        wait_for_process(source.0)?;
        if let Err(error) = continue_after_host_exit(&marker, &mut attempt) {
            fail_after_handoff(&marker, &mut attempt, error);
        }
        Ok(())
    }

    pub fn record_startup_failure(error: &anyhow::Error) {
        let Ok(marker) = parse_marker_path() else {
            return;
        };
        let Ok(mut attempt) = load_marker(&marker) else {
            return;
        };
        attempt.phase = UpdateAttemptPhase::Failed;
        attempt.error = Some(format!("{error:#}"));
        let _ = save_marker(&marker, &mut attempt);
    }
}

#[cfg(windows)]
fn main() {
    if let Err(error) = windows_helper::run() {
        windows_helper::record_startup_failure(&error);
        std::process::exit(1);
    }
}

#[cfg(not(windows))]
fn main() {
    eprintln!("iac-code-desktop-updater is only shipped on Windows");
    std::process::exit(2);
}
