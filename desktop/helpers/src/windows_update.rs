#![cfg(windows)]

use anyhow::{bail, Context, Result};
use serde::{Deserialize, Serialize};
use sha2::{Digest, Sha256};
use std::fs::{self, File, OpenOptions};
use std::io::{Read, Write};
use std::os::windows::ffi::OsStrExt;
use std::path::{Path, PathBuf};
use std::time::{Duration, SystemTime, UNIX_EPOCH};
use windows::core::{HRESULT, PCWSTR};
use windows::Win32::Foundation::{
    CloseHandle, ERROR_INVALID_PARAMETER, FILETIME, HANDLE, HWND, WAIT_OBJECT_0,
};
use windows::Win32::Security::Cryptography::{
    CertCloseStore, CertFindCertificateInStore, CertFreeCertificateContext, CertGetNameStringW,
    CryptMsgClose, CryptMsgGetParam, CryptQueryObject, CERT_FIND_SUBJECT_CERT, CERT_INFO,
    CERT_NAME_SIMPLE_DISPLAY_TYPE, CERT_QUERY_CONTENT_FLAG_PKCS7_SIGNED_EMBED,
    CERT_QUERY_FORMAT_FLAG_BINARY, CERT_QUERY_OBJECT_FILE, CMSG_SIGNER_INFO,
    CMSG_SIGNER_INFO_PARAM, HCERTSTORE, PKCS_7_ASN_ENCODING, X509_ASN_ENCODING,
};
use windows::Win32::Security::WinTrust::{
    WinVerifyTrust, WINTRUST_ACTION_GENERIC_VERIFY_V2, WINTRUST_DATA, WINTRUST_DATA_0,
    WINTRUST_FILE_INFO, WTD_CHOICE_FILE, WTD_REVOKE_NONE, WTD_STATEACTION_CLOSE,
    WTD_STATEACTION_VERIFY, WTD_UI_NONE,
};
use windows::Win32::Storage::FileSystem::{
    MoveFileExW, MOVEFILE_REPLACE_EXISTING, MOVEFILE_WRITE_THROUGH,
};
use windows::Win32::System::Threading::{
    GetCurrentProcess, GetProcessTimes, OpenProcess, WaitForSingleObject,
    PROCESS_QUERY_LIMITED_INFORMATION, PROCESS_SYNCHRONIZE,
};

pub const MARKER_NAME: &str = "windows-update-attempt.json";

#[derive(Clone, Debug, Deserialize, Eq, PartialEq, Serialize)]
#[serde(rename_all = "snake_case")]
pub enum UpdateAttemptPhase {
    Prepared,
    HelperReady,
    InstallerRunning,
    Failed,
    HandoffPending,
    Complete,
}

#[derive(Clone, Debug, Deserialize, Serialize)]
#[serde(rename_all = "camelCase")]
pub struct UpdateAttempt {
    pub attempt_id: String,
    pub source_version: String,
    pub target_version: String,
    pub source_host_pid: u32,
    pub source_host_creation_time: u64,
    pub verified_artifact_path: PathBuf,
    pub verified_artifact_sha256: String,
    pub current_executable_path: PathBuf,
    pub relaunch_args: Vec<String>,
    pub helper_executable_path: PathBuf,
    pub helper_bundle_sha256: String,
    pub helper_authenticode_required: bool,
    pub helper_expected_publisher: Option<String>,
    pub installer_executable_path: Option<PathBuf>,
    pub helper_pid: Option<u32>,
    pub helper_creation_time: Option<u64>,
    #[serde(default)]
    pub recovery_relaunch_started: bool,
    pub phase: UpdateAttemptPhase,
    pub error: Option<String>,
    pub updated_at_unix_ms: u128,
}

impl UpdateAttempt {
    pub fn touch(&mut self) {
        self.updated_at_unix_ms = SystemTime::now()
            .duration_since(UNIX_EPOCH)
            .unwrap_or_default()
            .as_millis();
    }
}

fn wide(value: &Path) -> Vec<u16> {
    value
        .as_os_str()
        .encode_wide()
        .chain(std::iter::once(0))
        .collect()
}

pub fn sha256_file(path: &Path) -> Result<String> {
    let mut file = File::open(path)?;
    let mut digest = Sha256::new();
    let mut buffer = [0_u8; 128 * 1024];
    loop {
        let size = file.read(&mut buffer)?;
        if size == 0 {
            break;
        }
        digest.update(&buffer[..size]);
    }
    Ok(hex::encode(digest.finalize()))
}

fn authenticode_publisher(path: &Path) -> Result<String> {
    let path_wide = wide(path);
    let mut store = HCERTSTORE::default();
    let mut message = std::ptr::null_mut();
    unsafe {
        CryptQueryObject(
            CERT_QUERY_OBJECT_FILE,
            path_wide.as_ptr().cast(),
            CERT_QUERY_CONTENT_FLAG_PKCS7_SIGNED_EMBED,
            CERT_QUERY_FORMAT_FLAG_BINARY,
            0,
            None,
            None,
            None,
            Some(&mut store),
            Some(&mut message),
            None,
        )
    }
    .context("read updater helper Authenticode certificate")?;

    let result = (|| -> Result<String> {
        let mut signer_size = 0_u32;
        unsafe { CryptMsgGetParam(message, CMSG_SIGNER_INFO_PARAM, 0, None, &mut signer_size) }
            .context("measure updater helper signer information")?;
        let mut signer_bytes = vec![0_u8; signer_size as usize];
        unsafe {
            CryptMsgGetParam(
                message,
                CMSG_SIGNER_INFO_PARAM,
                0,
                Some(signer_bytes.as_mut_ptr().cast()),
                &mut signer_size,
            )
        }
        .context("read updater helper signer information")?;
        let signer = unsafe { &*(signer_bytes.as_ptr().cast::<CMSG_SIGNER_INFO>()) };
        let certificate_identity = CERT_INFO {
            Issuer: signer.Issuer,
            SerialNumber: signer.SerialNumber,
            ..Default::default()
        };
        let encoding = X509_ASN_ENCODING | PKCS_7_ASN_ENCODING;
        let certificate = unsafe {
            CertFindCertificateInStore(
                store,
                encoding,
                0,
                CERT_FIND_SUBJECT_CERT,
                Some(std::ptr::addr_of!(certificate_identity).cast()),
                None,
            )
        };
        if certificate.is_null() {
            bail!("updater helper signer certificate is missing");
        }
        let name_size = unsafe {
            CertGetNameStringW(certificate, CERT_NAME_SIMPLE_DISPLAY_TYPE, 0, None, None)
        };
        if name_size <= 1 {
            let _ = unsafe { CertFreeCertificateContext(Some(certificate)) };
            bail!("updater helper signer publisher is missing");
        }
        let mut name = vec![0_u16; name_size as usize];
        let written = unsafe {
            CertGetNameStringW(
                certificate,
                CERT_NAME_SIMPLE_DISPLAY_TYPE,
                0,
                None,
                Some(&mut name),
            )
        };
        let _ = unsafe { CertFreeCertificateContext(Some(certificate)) };
        if written != name_size {
            bail!("updater helper signer publisher could not be read");
        }
        name.truncate(name.len().saturating_sub(1));
        String::from_utf16(&name).context("decode updater helper signer publisher")
    })();
    let _ = unsafe { CryptMsgClose(Some(message)) };
    let _ = unsafe { CertCloseStore(Some(store), 0) };
    result
}

pub fn verify_helper_integrity(
    path: &Path,
    expected_sha256: &str,
    authenticode_required: bool,
    expected_publisher: Option<&str>,
) -> Result<()> {
    if sha256_file(path)? != expected_sha256 {
        bail!("updater helper does not match the build-time bundle manifest");
    }
    if !authenticode_required {
        if expected_publisher.is_some() {
            bail!("development updater helper cannot claim a signed publisher");
        }
        return Ok(());
    }
    let expected_publisher = expected_publisher
        .filter(|value| !value.trim().is_empty())
        .context("release updater helper publisher is not configured")?;
    // Keep the UTF-16 path alive for the complete WinVerifyTrust transaction.
    let path_wide = wide(path);
    let mut file_info = WINTRUST_FILE_INFO {
        cbStruct: std::mem::size_of::<WINTRUST_FILE_INFO>() as u32,
        pcwszFilePath: PCWSTR(path_wide.as_ptr()),
        ..Default::default()
    };
    let mut trust_data = WINTRUST_DATA {
        cbStruct: std::mem::size_of::<WINTRUST_DATA>() as u32,
        dwUIChoice: WTD_UI_NONE,
        fdwRevocationChecks: WTD_REVOKE_NONE,
        dwUnionChoice: WTD_CHOICE_FILE,
        Anonymous: WINTRUST_DATA_0 {
            pFile: &mut file_info,
        },
        dwStateAction: WTD_STATEACTION_VERIFY,
        ..Default::default()
    };
    let mut action = WINTRUST_ACTION_GENERIC_VERIFY_V2;
    let status = unsafe {
        WinVerifyTrust(
            HWND::default(),
            &mut action,
            std::ptr::addr_of_mut!(trust_data).cast(),
        )
    };
    trust_data.dwStateAction = WTD_STATEACTION_CLOSE;
    let _ = unsafe {
        WinVerifyTrust(
            HWND::default(),
            &mut action,
            std::ptr::addr_of_mut!(trust_data).cast(),
        )
    };
    if status != 0 {
        bail!("updater helper Authenticode verification failed with status {status:#x}");
    }
    let publisher = authenticode_publisher(path)?;
    if publisher != expected_publisher {
        bail!(
            "updater helper publisher mismatch: expected {expected_publisher:?}, got {publisher:?}"
        );
    }
    Ok(())
}

pub fn save_marker(path: &Path, attempt: &mut UpdateAttempt) -> Result<()> {
    attempt.touch();
    let parent = path
        .parent()
        .context("Windows update marker has no parent")?;
    fs::create_dir_all(parent)?;
    let temporary = parent.join(format!(".{MARKER_NAME}.{}.tmp", std::process::id()));
    let mut file = OpenOptions::new()
        .create_new(true)
        .write(true)
        .open(&temporary)
        .context("create Windows update marker temporary file")?;
    serde_json::to_writer_pretty(&mut file, attempt)?;
    file.write_all(b"\n")?;
    file.sync_all()?;
    drop(file);
    let source = wide(&temporary);
    let destination = wide(path);
    if let Err(error) = unsafe {
        MoveFileExW(
            PCWSTR(source.as_ptr()),
            PCWSTR(destination.as_ptr()),
            MOVEFILE_REPLACE_EXISTING | MOVEFILE_WRITE_THROUGH,
        )
    } {
        let _ = fs::remove_file(&temporary);
        return Err(error).context("atomically replace Windows update marker");
    }
    if let Ok(directory) = File::open(parent) {
        let _ = directory.sync_all();
    }
    Ok(())
}

pub fn load_marker(path: &Path) -> Result<UpdateAttempt> {
    let bytes = fs::read(path).context("read Windows update marker")?;
    serde_json::from_slice(&bytes).context("parse Windows update marker")
}

pub fn process_creation_time(process: HANDLE) -> Result<u64> {
    let mut creation = FILETIME::default();
    let mut exit = FILETIME::default();
    let mut kernel = FILETIME::default();
    let mut user = FILETIME::default();
    unsafe { GetProcessTimes(process, &mut creation, &mut exit, &mut kernel, &mut user) }
        .context("read Windows process creation time")?;
    Ok(((creation.dwHighDateTime as u64) << 32) | creation.dwLowDateTime as u64)
}

pub fn current_process_creation_time() -> Result<u64> {
    process_creation_time(unsafe { GetCurrentProcess() })
}

pub struct ProcessHandle(pub HANDLE);

impl Drop for ProcessHandle {
    fn drop(&mut self) {
        let _ = unsafe { CloseHandle(self.0) };
    }
}

pub fn open_source_process(pid: u32, expected_creation_time: u64) -> Result<ProcessHandle> {
    let process = unsafe {
        OpenProcess(
            PROCESS_QUERY_LIMITED_INFORMATION | PROCESS_SYNCHRONIZE,
            false,
            pid,
        )
    }
    .context("open source Desktop Host process")?;
    let handle = ProcessHandle(process);
    if process_creation_time(handle.0)? != expected_creation_time {
        bail!("source Desktop Host process identity changed");
    }
    Ok(handle)
}

pub fn wait_for_process_identity_exit(
    pid: u32,
    expected_creation_time: u64,
    timeout: Duration,
) -> Result<bool> {
    let process = match unsafe {
        OpenProcess(
            PROCESS_QUERY_LIMITED_INFORMATION | PROCESS_SYNCHRONIZE,
            false,
            pid,
        )
    } {
        Ok(process) => process,
        Err(error) if error.code() == HRESULT::from_win32(ERROR_INVALID_PARAMETER.0) => {
            return Ok(true)
        }
        Err(error) => return Err(error).context("open updater helper process for cleanup"),
    };
    let handle = ProcessHandle(process);
    if process_creation_time(handle.0)? != expected_creation_time {
        return Ok(true);
    }
    let timeout_ms = timeout.as_millis().min(u32::MAX as u128) as u32;
    Ok(unsafe { WaitForSingleObject(handle.0, timeout_ms) } == WAIT_OBJECT_0)
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::process::Command;

    #[test]
    fn bundle_manifest_hash_rejects_tampered_helper() {
        let directory = tempfile::tempdir().unwrap();
        let helper = directory.path().join("iac-code-desktop-updater.exe");
        fs::write(&helper, b"expected helper bytes").unwrap();
        let expected = sha256_file(&helper).unwrap();
        verify_helper_integrity(&helper, &expected, false, None).unwrap();

        fs::write(&helper, b"tampered helper bytes").unwrap();
        let error = verify_helper_integrity(&helper, &expected, false, None).unwrap_err();
        assert!(error.to_string().contains("build-time bundle manifest"));
    }

    #[test]
    fn unsigned_helper_cannot_claim_a_release_publisher() {
        let directory = tempfile::tempdir().unwrap();
        let helper = directory.path().join("iac-code-desktop-updater.exe");
        fs::write(&helper, b"development helper").unwrap();
        let expected = sha256_file(&helper).unwrap();
        assert!(verify_helper_integrity(&helper, &expected, false, Some("publisher")).is_err());
    }

    #[test]
    fn cleanup_wait_reports_a_hung_owned_process_without_claiming_it_exited() {
        let mut child = Command::new("cmd.exe")
            .args(["/D", "/S", "/C", "ping -n 6 127.0.0.1 >NUL"])
            .spawn()
            .unwrap();
        let process = unsafe {
            OpenProcess(
                PROCESS_QUERY_LIMITED_INFORMATION | PROCESS_SYNCHRONIZE,
                false,
                child.id(),
            )
        }
        .unwrap();
        let handle = ProcessHandle(process);
        let creation_time = process_creation_time(handle.0).unwrap();
        drop(handle);

        assert!(!wait_for_process_identity_exit(
            child.id(),
            creation_time,
            Duration::from_millis(1)
        )
        .unwrap());
        child.kill().unwrap();
        child.wait().unwrap();
        assert!(wait_for_process_identity_exit(
            child.id(),
            creation_time,
            Duration::from_millis(1)
        )
        .unwrap());
    }
}
