use std::fs;
use std::path::{Path, PathBuf};
use std::sync::Mutex;
use std::time::{SystemTime, UNIX_EPOCH};

use iac_code_config::cloud_credentials::{
    has_aliyun_provider, load_aliyun_credentials, AliyunCredential,
};
use iac_code_config::paths::ConfigPaths;

static ENV_LOCK: Mutex<()> = Mutex::new(());

#[test]
fn aliyun_credentials_load_from_env_iac_config_and_cli_like_python() {
    let _guard = ENV_LOCK.lock().expect("env lock should not be poisoned");
    clear_aliyun_env();

    let root = unique_temp_dir("iac-code-rs-cloud-credentials");
    let config_dir = root.join("config");
    fs::create_dir_all(&config_dir).expect("config dir should be created");
    let paths = paths_for(&config_dir);

    fs::write(
        &paths.cloud_credentials_path,
        "aliyun:\n  mode: AK\n  access_key_id: config-ak\n  access_key_secret: config-secret\n  region_id: cn-shanghai\n",
    )
    .expect("cloud credentials should be written");

    assert_eq!(
        load_aliyun_credentials(&paths, None).expect("config credentials should load"),
        Some(AliyunCredential {
            mode: "AK".into(),
            access_key_id: "config-ak".into(),
            access_key_secret: "config-secret".into(),
            region_id: "cn-shanghai".into(),
            ..AliyunCredential::default()
        })
    );
    assert!(has_aliyun_provider(&paths, None).expect("provider check should load"));

    std::env::set_var("ALIBABA_CLOUD_ACCESS_KEY_ID", "env-ak");
    std::env::set_var("ALIBABA_CLOUD_ACCESS_KEY_SECRET", "env-secret");
    let env_credential =
        load_aliyun_credentials(&paths, None).expect("env credentials should load");
    assert_eq!(
        env_credential.expect("env credential").access_key_id,
        "env-ak"
    );
    assert_eq!(
        load_aliyun_credentials(&paths, None)
            .expect("env credentials should load")
            .expect("env credential")
            .region_id,
        "cn-shanghai"
    );
    clear_aliyun_env();

    fs::write(&paths.cloud_credentials_path, "").expect("cloud credentials should be cleared");
    let cli_path = root.join("aliyun").join("config.json");
    fs::create_dir_all(cli_path.parent().expect("cli parent")).expect("cli dir should be created");
    fs::write(
        &cli_path,
        r#"{"current":"default","profiles":[{"name":"default","mode":"AK","access_key_id":"cli-ak","access_key_secret":"cli-secret","region_id":"cn-hangzhou"}]}"#,
    )
    .expect("aliyun cli config should be written");

    assert_eq!(
        load_aliyun_credentials(&paths, Some(&cli_path))
            .expect("cli credentials should load")
            .expect("cli credential")
            .access_key_id,
        "cli-ak"
    );

    fs::remove_dir_all(&root).ok();
    clear_aliyun_env();
}

#[test]
fn aliyun_cli_credentials_reject_invalid_expiration_like_python() {
    let _guard = ENV_LOCK.lock().expect("env lock should not be poisoned");
    clear_aliyun_env();

    let root = unique_temp_dir("iac-code-rs-cloud-credentials-invalid-cli");
    let config_dir = root.join("config");
    fs::create_dir_all(&config_dir).expect("config dir should be created");
    let paths = paths_for(&config_dir);
    fs::write(&paths.cloud_credentials_path, "").expect("cloud credentials should be cleared");

    let cli_path = root.join("aliyun").join("config.json");
    fs::create_dir_all(cli_path.parent().expect("cli parent")).expect("cli dir should be created");
    fs::write(
        &cli_path,
        r#"{"current":"default","profiles":[{"name":"default","mode":"StsToken","access_key_id":"cli-ak","access_key_secret":"cli-secret","region_id":"cn-hangzhou","sts_expiration":"not-an-int"}]}"#,
    )
    .expect("aliyun cli config should be written");

    assert_eq!(
        load_aliyun_credentials(&paths, Some(&cli_path)).expect("cli credentials should load"),
        None
    );

    fs::remove_dir_all(&root).ok();
    clear_aliyun_env();
}

#[test]
fn aliyun_iac_code_credentials_reject_invalid_expiration_like_python() {
    let _guard = ENV_LOCK.lock().expect("env lock should not be poisoned");
    clear_aliyun_env();

    let root = unique_temp_dir("iac-code-rs-cloud-credentials-invalid-yaml");
    let config_dir = root.join("config");
    fs::create_dir_all(&config_dir).expect("config dir should be created");
    let paths = paths_for(&config_dir);

    fs::write(
        &paths.cloud_credentials_path,
        "aliyun:\n  mode: OAuth\n  oauth_site_type: CN\n  oauth_access_token_expire: not-an-int\n",
    )
    .expect("cloud credentials should be written");

    let error = load_aliyun_credentials(&paths, None)
        .expect_err("invalid iac-code credential integer should fail like Python");
    assert!(
        error.to_string().contains("oauth_access_token_expire"),
        "{error}"
    );

    fs::remove_dir_all(&root).ok();
    clear_aliyun_env();
}

fn paths_for(config_dir: &Path) -> ConfigPaths {
    ConfigPaths {
        config_dir: config_dir.to_path_buf(),
        credentials_path: config_dir.join(".credentials.yml"),
        settings_path: config_dir.join("settings.yml"),
        cloud_credentials_path: config_dir.join(".cloud-credentials.yml"),
        history_path: config_dir.join(".input_history"),
    }
}

fn unique_temp_dir(name: &str) -> PathBuf {
    let suffix = SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .expect("time should be monotonic enough")
        .as_nanos();
    std::env::temp_dir().join(format!("{name}-{suffix}"))
}

fn clear_aliyun_env() {
    for key in [
        "ALIBABA_CLOUD_ACCESS_KEY_ID",
        "ALIBABA_CLOUD_ACCESS_KEY_SECRET",
        "ALIBABA_CLOUD_REGION_ID",
        "ALIBABA_CLOUD_SECURITY_TOKEN",
    ] {
        std::env::remove_var(key);
    }
}
