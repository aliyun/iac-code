#![cfg(target_os = "macos")]

use std::env;
use std::fs;
use std::path::{Path, PathBuf};
use std::sync::Mutex;
use std::time::{SystemTime, UNIX_EPOCH};

use iac_code_providers::load_from_qwenpaw;

static ENV_LOCK: Mutex<()> = Mutex::new(());

#[test]
fn qwenpaw_invalid_env_secret_dir_does_not_fall_back_like_python() {
    let _guard = ENV_LOCK.lock().expect("env lock");
    let workspace = TestWorkspace::new("qwenpaw-invalid-env-dir");
    let home_secret = workspace.path().join("home").join(".qwenpaw.secret");
    fs::create_dir_all(home_secret.join("providers")).expect("create home qwenpaw dirs");
    fs::write(
        home_secret.join("providers").join("active_model.json"),
        r#"{"model":"home-model","provider_id":"openai"}"#,
    )
    .expect("write home active model");

    let _env = EnvGuard::set_vars([
        (
            "QWENPAW_SECRET_DIR",
            Some(workspace.path().join("missing-secret").into_os_string()),
        ),
        ("COPAW_SECRET_DIR", None),
        ("HOME", Some(workspace.path().join("home").into_os_string())),
    ]);

    let config = load_from_qwenpaw().expect("qwenpaw load should not error");

    assert!(
        config.is_none(),
        "invalid QWENPAW_SECRET_DIR should not fall back to HOME secrets"
    );
}

#[test]
fn qwenpaw_master_key_prefers_keychain_over_master_key_file_like_python() {
    let _guard = ENV_LOCK.lock().expect("env lock");
    let workspace = TestWorkspace::new("qwenpaw-keychain-priority");
    let secret_dir = workspace.path().join("secret");
    let fake_bin = workspace.path().join("bin");
    fs::create_dir_all(secret_dir.join("providers").join("custom")).expect("create qwenpaw dirs");
    fs::create_dir_all(&fake_bin).expect("create fake bin dir");

    fs::write(
        secret_dir.join("providers").join("active_model.json"),
        r#"{"model":"fixture-qwenpaw-encrypted-model","provider_id":"openai"}"#,
    )
    .expect("write active model");
    fs::write(
        secret_dir
            .join("providers")
            .join("custom")
            .join("openai.json"),
        r#"{"api_key":"ENC:gAAAAABlU_EAEBESExQVFhcYGRobHB0eH-PL8hlOsFk83vaJHIwd73emw-xQHoM-bLNpYv_5oKQU2zutDFYIUMNJZVhc2tZN-w=="}"#,
    )
    .expect("write encrypted provider config");
    fs::write(
        secret_dir.join(".master_key"),
        "ffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffff\n",
    )
    .expect("write stale master key file");
    let security_called = workspace.path().join("security-called");
    write_fake_security(
        &fake_bin.join("security"),
        &security_called,
        "000102030405060708090a0b0c0d0e0f101112131415161718191a1b1c1d1e1f",
    );

    let _env = EnvGuard::set_vars([
        (
            "QWENPAW_SECRET_DIR",
            Some(secret_dir.as_os_str().to_owned()),
        ),
        ("COPAW_SECRET_DIR", None),
        (
            "PATH",
            Some(
                format!(
                    "{}:{}",
                    fake_bin.display(),
                    env::var("PATH").unwrap_or_default()
                )
                .into(),
            ),
        ),
    ]);

    let config = load_from_qwenpaw()
        .expect("qwenpaw load should not error")
        .expect("qwenpaw config should be present");

    assert_eq!(config.provider_key, "openai");
    assert!(security_called.exists(), "fake security should be called");
    assert_eq!(config.api_key.as_deref(), Some("hello fernet"));
}

fn write_fake_security(path: &Path, marker: &Path, output: &str) {
    use std::os::unix::fs::PermissionsExt;

    fs::write(
        path,
        format!(
            "#!/bin/sh\nprintf '%s' called > {marker:?}\nprintf '%s' {output:?}\n",
            marker = marker.display().to_string()
        ),
    )
    .expect("write fake security");
    let mut permissions = fs::metadata(path)
        .expect("fake security metadata")
        .permissions();
    permissions.set_mode(0o700);
    fs::set_permissions(path, permissions).expect("chmod fake security");
}

struct EnvGuard {
    previous: Vec<(String, Option<std::ffi::OsString>)>,
}

impl EnvGuard {
    fn set_vars<const N: usize>(vars: [(&str, Option<std::ffi::OsString>); N]) -> Self {
        let mut previous = Vec::with_capacity(N);
        for (key, value) in vars {
            previous.push((key.to_owned(), env::var_os(key)));
            match value {
                Some(value) => env::set_var(key, value),
                None => env::remove_var(key),
            }
        }
        Self { previous }
    }
}

impl Drop for EnvGuard {
    fn drop(&mut self) {
        for (key, value) in self.previous.drain(..).rev() {
            match value {
                Some(value) => env::set_var(key, value),
                None => env::remove_var(key),
            }
        }
    }
}

struct TestWorkspace {
    path: PathBuf,
}

impl TestWorkspace {
    fn new(name: &str) -> Self {
        let unique = SystemTime::now()
            .duration_since(UNIX_EPOCH)
            .expect("system time")
            .as_nanos();
        let path = env::temp_dir().join(format!("iac-code-rs-providers-{name}-{unique}"));
        fs::create_dir_all(&path).expect("create workspace");
        Self { path }
    }

    fn path(&self) -> &Path {
        &self.path
    }
}

impl Drop for TestWorkspace {
    fn drop(&mut self) {
        fs::remove_dir_all(&self.path).ok();
    }
}
