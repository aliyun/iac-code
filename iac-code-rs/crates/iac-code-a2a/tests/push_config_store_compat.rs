use iac_code_a2a::push::{
    A2APushAuthentication, A2APushConfigStore, InvalidPushNotificationConfigError,
    TaskPushNotificationConfig,
};

#[test]
fn push_config_store_persists_configs_by_owner_and_dispatch_lists_all_owners() {
    let root = temp_root("owners");
    let mut store = A2APushConfigStore::new(&root);

    store
        .set_info(
            "alice",
            "task-1",
            TaskPushNotificationConfig::new("cfg-1", "https://callback.example/a2a"),
        )
        .expect("set alice");

    assert_eq!(
        config_ids(&store.get_info("alice", "task-1").expect("alice configs")),
        vec!["cfg-1"]
    );
    assert!(store
        .get_info("bob", "task-1")
        .expect("bob configs")
        .is_empty());
    assert_eq!(
        config_ids(
            &store
                .get_info_for_dispatch("task-1")
                .expect("dispatch configs")
        ),
        vec!["cfg-1"]
    );
}

#[test]
fn push_config_store_defaults_empty_config_id_to_task_id() {
    let root = temp_root("default-id");
    let mut store = A2APushConfigStore::new(&root);

    store
        .set_info(
            "",
            "task-1",
            TaskPushNotificationConfig::new("", "https://callback.example/a2a"),
        )
        .expect("set");

    let configs = store.get_info("", "task-1").expect("configs");
    assert_eq!(configs[0].id, "task-1");
    assert_eq!(configs[0].task_id, "task-1");
}

#[test]
fn push_config_store_delete_specific_or_all_configs() {
    let root = temp_root("delete");
    let mut store = A2APushConfigStore::new(&root);
    store
        .set_info(
            "owner",
            "task-1",
            TaskPushNotificationConfig::new("cfg-1", "https://one.example/a2a"),
        )
        .expect("set cfg1");
    store
        .set_info(
            "owner",
            "task-1",
            TaskPushNotificationConfig::new("cfg-2", "https://two.example/a2a"),
        )
        .expect("set cfg2");

    store
        .delete_info("owner", "task-1", Some("cfg-1"))
        .expect("delete one");
    assert_eq!(
        config_ids(&store.get_info("owner", "task-1").expect("configs")),
        vec!["cfg-2"]
    );

    store
        .delete_info("owner", "task-1", None)
        .expect("delete all");
    assert!(store
        .get_info("owner", "task-1")
        .expect("configs")
        .is_empty());
}

#[test]
fn push_config_store_rejects_invalid_urls_and_ids_without_persisting() {
    let root = temp_root("invalid");
    let mut store = A2APushConfigStore::new(&root);

    assert_eq!(
        store.set_info(
            "owner",
            "task-1",
            TaskPushNotificationConfig::new("cfg-1", "http://127.0.0.1/a2a"),
        ),
        Err(InvalidPushNotificationConfigError)
    );
    assert!(store
        .get_info("owner", "task-1")
        .expect("configs")
        .is_empty());

    assert_eq!(
        store.set_info(
            "owner",
            "bad/task",
            TaskPushNotificationConfig::new("cfg-1", "https://callback.example/a2a"),
        ),
        Err(InvalidPushNotificationConfigError)
    );
}

#[test]
fn push_config_store_ignores_empty_url_like_python() {
    let root = temp_root("empty-url");
    let mut store = A2APushConfigStore::new(&root);

    store
        .set_info(
            "owner",
            "task-1",
            TaskPushNotificationConfig::new("cfg-1", ""),
        )
        .expect("empty URL no-op");

    assert!(store
        .get_info("owner", "task-1")
        .expect("configs")
        .is_empty());
}

#[cfg(unix)]
#[test]
fn push_config_store_writes_private_files_like_python() {
    use std::os::unix::fs::PermissionsExt;

    let root = temp_root("private-perms");
    let mut store = A2APushConfigStore::new(&root);

    assert_eq!(
        std::fs::metadata(root.join("push_configs"))
            .expect("push config root metadata")
            .permissions()
            .mode()
            & 0o777,
        0o700
    );

    store
        .set_info(
            "owner",
            "task-1",
            TaskPushNotificationConfig::new("cfg-1", "https://callback.example/a2a"),
        )
        .expect("set config");

    let path = config_path(&root, "cfg-1");
    assert_eq!(
        std::fs::metadata(path.parent().expect("task dir"))
            .expect("task dir metadata")
            .permissions()
            .mode()
            & 0o777,
        0o700
    );
    assert_eq!(
        std::fs::metadata(&path)
            .expect("config file metadata")
            .permissions()
            .mode()
            & 0o777,
        0o600
    );
}

#[test]
fn push_config_store_encrypts_token_and_auth_credentials_at_rest() {
    let root = temp_root("encrypted-config");
    let mut store = A2APushConfigStore::new(&root);
    let mut config = TaskPushNotificationConfig::new("cfg-1", "https://callback.example/a2a");
    config.token = "token-1".to_owned();
    config.authentication = Some(A2APushAuthentication::new("bearer", "secret-1"));

    store
        .set_info("owner", "task-1", config)
        .expect("set encrypted config");

    let raw = std::fs::read_to_string(config_path(&root, "cfg-1")).expect("raw config");
    assert!(!raw.contains("token-1"));
    assert!(!raw.contains("secret-1"));
    assert!(raw.contains("iacCodeEncryptedFields"));

    let loaded = store.get_info("owner", "task-1").expect("configs");
    assert_eq!(loaded[0].token, "token-1");
    assert_eq!(
        loaded[0].authentication.as_ref().unwrap().credentials,
        "secret-1"
    );
    assert_eq!(
        store
            .resolve_headers_for_dispatch("task-1", "cfg-1")
            .expect("headers"),
        [
            ("Authorization".to_owned(), "Bearer secret-1".to_owned(),),
            ("X-A2A-Notification-Token".to_owned(), "token-1".to_owned(),),
        ]
        .into_iter()
        .collect()
    );
}

#[test]
fn push_config_store_reloads_encrypted_configs_after_rebuild() {
    let root = temp_root("encrypted-reload");
    let mut store = A2APushConfigStore::new(&root);
    let mut config = TaskPushNotificationConfig::new("cfg-1", "https://callback.example/a2a");
    config.token = "token-1".to_owned();
    config.authentication = Some(A2APushAuthentication::new("bearer", "secret-1"));
    store
        .set_info("owner", "task-1", config)
        .expect("set encrypted config");
    drop(store);

    let reloaded = A2APushConfigStore::new(&root);
    let configs = reloaded.get_info("owner", "task-1").expect("configs");

    assert_eq!(configs[0].token, "token-1");
    assert_eq!(
        configs[0].authentication.as_ref().unwrap().credentials,
        "secret-1"
    );
    assert_eq!(
        reloaded
            .resolve_headers_for_dispatch("task-1", "cfg-1")
            .expect("headers"),
        [
            ("Authorization".to_owned(), "Bearer secret-1".to_owned(),),
            ("X-A2A-Notification-Token".to_owned(), "token-1".to_owned(),),
        ]
        .into_iter()
        .collect()
    );
}

#[test]
fn push_config_store_resolves_basic_and_custom_auth_headers_for_dispatch() {
    let root = temp_root("dispatch-auth-headers");
    let mut store = A2APushConfigStore::new(&root);
    let mut basic = TaskPushNotificationConfig::new("cfg-basic", "https://basic.example/a2a");
    basic.authentication = Some(A2APushAuthentication::new("basic", "user:pass"));
    let mut custom = TaskPushNotificationConfig::new("cfg-custom", "https://custom.example/a2a");
    custom.authentication = Some(A2APushAuthentication::new("ApiKey", "secret-key"));

    store.set_info("owner", "task-1", basic).expect("set basic");
    store
        .set_info("owner", "task-1", custom)
        .expect("set custom");

    assert_eq!(
        store
            .resolve_headers_for_dispatch("task-1", "cfg-basic")
            .expect("basic headers")
            .get("Authorization")
            .map(String::as_str),
        Some("Basic dXNlcjpwYXNz")
    );
    assert_eq!(
        store
            .resolve_headers_for_dispatch("task-1", "cfg-custom")
            .expect("custom headers")
            .get("Authorization")
            .map(String::as_str),
        Some("ApiKey secret-key")
    );
}

#[test]
fn push_config_store_delete_removes_dispatch_headers() {
    let root = temp_root("delete-headers");
    let mut store = A2APushConfigStore::new(&root);
    let mut config = TaskPushNotificationConfig::new("cfg-1", "https://callback.example/a2a");
    config.token = "token-1".to_owned();
    config.authentication = Some(A2APushAuthentication::new("bearer", "secret-1"));
    store
        .set_info("owner", "task-1", config)
        .expect("set config");

    assert!(!store
        .resolve_headers_for_dispatch("task-1", "cfg-1")
        .expect("headers before delete")
        .is_empty());

    store
        .delete_info("owner", "task-1", Some("cfg-1"))
        .expect("delete config");

    assert!(store
        .resolve_headers_for_dispatch("task-1", "cfg-1")
        .expect("headers after delete")
        .is_empty());
}

#[test]
fn push_config_store_dispatch_lists_all_owners_in_stable_order() {
    let root = temp_root("dispatch-owner-order");
    let mut store = A2APushConfigStore::new(&root);
    let mut expected = Vec::new();
    for (owner, config_id) in [
        ("charlie", "cfg-charlie"),
        ("alice", "cfg-alice"),
        ("bob", "cfg-bob"),
    ] {
        expected.push((owner_hash(owner), config_id));
        store
            .set_info(
                owner,
                "task-1",
                TaskPushNotificationConfig::new(config_id, "https://callback.example/a2a"),
            )
            .expect("set config");
    }
    expected.sort_by(|left, right| left.0.cmp(&right.0).then_with(|| left.1.cmp(right.1)));

    assert_eq!(
        config_ids(
            &store
                .get_info_for_dispatch("task-1")
                .expect("dispatch configs")
        ),
        expected
            .iter()
            .map(|(_, config_id)| *config_id)
            .collect::<Vec<_>>()
    );
}

#[test]
fn push_config_store_key_rotation_keeps_old_configs_readable() {
    let root = temp_root("encrypted-rotation");
    let mut store = A2APushConfigStore::new(&root);
    let mut old_config = TaskPushNotificationConfig::new("cfg-old", "https://callback.example/a2a");
    old_config.token = "old".to_owned();

    store
        .set_info("owner", "task-1", old_config)
        .expect("set old config");
    let old_key_id = store.active_secret_key_id().expect("old key id");

    let new_key_id = store.rotate_secret_key(None).expect("rotate key");
    let mut new_config = TaskPushNotificationConfig::new("cfg-new", "https://callback.example/a2a");
    new_config.token = "new".to_owned();
    store
        .set_info("owner", "task-1", new_config)
        .expect("set new config");

    assert_ne!(new_key_id, old_key_id);
    let configs = store
        .get_info("owner", "task-1")
        .expect("configs")
        .into_iter()
        .map(|config| (config.id, config.token))
        .collect::<std::collections::BTreeMap<_, _>>();
    assert_eq!(
        configs,
        [
            ("cfg-new".to_owned(), "new".to_owned()),
            ("cfg-old".to_owned(), "old".to_owned()),
        ]
        .into_iter()
        .collect()
    );
    assert!(std::fs::read_to_string(config_path(&root, "cfg-old"))
        .unwrap()
        .contains(&old_key_id));
    assert!(std::fs::read_to_string(config_path(&root, "cfg-new"))
        .unwrap()
        .contains(&new_key_id));
}

fn config_ids(configs: &[TaskPushNotificationConfig]) -> Vec<&str> {
    configs.iter().map(|config| config.id.as_str()).collect()
}

fn config_path(root: &std::path::Path, config_id: &str) -> std::path::PathBuf {
    (root.join("push_configs"))
        .read_dir()
        .expect("owner dirs")
        .find_map(|owner| {
            let path = owner
                .ok()?
                .path()
                .join("task-1")
                .join(format!("{config_id}.json"));
            path.exists().then_some(path)
        })
        .expect("config path")
}

fn owner_hash(owner: &str) -> String {
    let digest = ring::digest::digest(&ring::digest::SHA256, owner.as_bytes());
    let mut output = String::with_capacity(digest.as_ref().len() * 2);
    for byte in digest.as_ref() {
        use std::fmt::Write;
        write!(output, "{byte:02x}").expect("writing to String should not fail");
    }
    output
}

fn temp_root(name: &str) -> std::path::PathBuf {
    let root = std::env::temp_dir().join(format!(
        "iac-code-a2a-push-config-{name}-{}",
        std::process::id()
    ));
    let _ = std::fs::remove_dir_all(&root);
    root
}
