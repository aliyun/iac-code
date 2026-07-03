use std::cell::RefCell;
use std::collections::BTreeMap;
use std::path::{Path, PathBuf};

use crate::private_fs::{ensure_private_dir, write_private_file};
use crate::push_config::TaskPushNotificationConfig;
use crate::push_endpoint::{validate_push_callback_url, InvalidPushNotificationConfigError};
use crate::push_secrets::A2APushSecretKeyring;
use crate::types::validate_protocol_id;

mod file_io;
mod model;

use file_io::{
    is_json_file, owner_hash, read_json_file, remove_file_if_exists, sorted_json_files,
    sorted_owner_dirs,
};
use model::notification_headers;

#[derive(Clone, Debug)]
pub struct A2APushConfigStore {
    root: PathBuf,
    secret_keyring: RefCell<A2APushSecretKeyring>,
}

impl A2APushConfigStore {
    pub fn new(persistence_root: impl AsRef<Path>) -> Self {
        let persistence_root = persistence_root.as_ref();
        let root = persistence_root.join("push_configs");
        ensure_private_dir(&root).expect("creating A2A push config directory should not fail");
        Self {
            root,
            secret_keyring: RefCell::new(A2APushSecretKeyring::new(
                persistence_root.join("push_keys.json"),
            )),
        }
    }

    pub fn with_secret_keyring(
        persistence_root: impl AsRef<Path>,
        secret_keyring: A2APushSecretKeyring,
    ) -> Self {
        let root = persistence_root.as_ref().join("push_configs");
        ensure_private_dir(&root).expect("creating A2A push config directory should not fail");
        Self {
            root,
            secret_keyring: RefCell::new(secret_keyring),
        }
    }

    pub fn active_secret_key_id(&self) -> Result<String, InvalidPushNotificationConfigError> {
        self.secret_keyring
            .borrow_mut()
            .active_key_id()
            .map(ToOwned::to_owned)
            .map_err(|_| InvalidPushNotificationConfigError)
    }

    pub fn rotate_secret_key(
        &self,
        key_id: Option<&str>,
    ) -> Result<String, InvalidPushNotificationConfigError> {
        self.secret_keyring
            .borrow_mut()
            .rotate(key_id)
            .map_err(|_| InvalidPushNotificationConfigError)
    }

    pub fn set_info(
        &mut self,
        owner: &str,
        task_id: &str,
        mut config: TaskPushNotificationConfig,
    ) -> Result<(), InvalidPushNotificationConfigError> {
        let task_id =
            validate_protocol_id(task_id).map_err(|_| InvalidPushNotificationConfigError)?;
        if config.url.is_empty() {
            return Ok(());
        }
        config.url = validate_push_callback_url(&config.url)?;
        if config.id.is_empty() {
            config.id = task_id.clone();
        }
        config.id =
            validate_protocol_id(&config.id).map_err(|_| InvalidPushNotificationConfigError)?;
        config.task_id = task_id;

        let path = self.config_path(owner, &config.task_id, &config.id);
        if let Some(parent) = path.parent() {
            ensure_private_dir(parent).map_err(|_| InvalidPushNotificationConfigError)?;
        }
        write_private_file(
            &path,
            config
                .to_storage_json(&self.secret_keyring)?
                .to_compact_json(),
        )
        .map_err(|_| InvalidPushNotificationConfigError)
    }

    pub fn get_info(
        &self,
        owner: &str,
        task_id: &str,
    ) -> Result<Vec<TaskPushNotificationConfig>, InvalidPushNotificationConfigError> {
        let task_id =
            validate_protocol_id(task_id).map_err(|_| InvalidPushNotificationConfigError)?;
        Ok(self.load_configs_for_owner(&owner_hash(owner), &task_id))
    }

    pub fn get_info_for_dispatch(
        &self,
        task_id: &str,
    ) -> Result<Vec<TaskPushNotificationConfig>, InvalidPushNotificationConfigError> {
        let task_id =
            validate_protocol_id(task_id).map_err(|_| InvalidPushNotificationConfigError)?;
        let mut configs = Vec::new();
        for owner_dir in sorted_owner_dirs(&self.root) {
            let Some(owner_hash) = owner_dir.file_name().and_then(|name| name.to_str()) else {
                continue;
            };
            configs.extend(self.load_configs_for_owner(owner_hash, &task_id));
        }
        Ok(configs)
    }

    pub fn resolve_headers_for_dispatch(
        &self,
        task_id: &str,
        config_id: &str,
    ) -> Result<BTreeMap<String, String>, InvalidPushNotificationConfigError> {
        let task_id =
            validate_protocol_id(task_id).map_err(|_| InvalidPushNotificationConfigError)?;
        let config_id =
            validate_protocol_id(config_id).map_err(|_| InvalidPushNotificationConfigError)?;
        for config in self.get_info_for_dispatch(&task_id)? {
            if config.id == config_id {
                return Ok(notification_headers(&config));
            }
        }
        Ok(BTreeMap::new())
    }

    pub fn delete_info(
        &mut self,
        owner: &str,
        task_id: &str,
        config_id: Option<&str>,
    ) -> Result<(), InvalidPushNotificationConfigError> {
        let task_id =
            validate_protocol_id(task_id).map_err(|_| InvalidPushNotificationConfigError)?;
        if let Some(config_id) = config_id {
            let config_id =
                validate_protocol_id(config_id).map_err(|_| InvalidPushNotificationConfigError)?;
            let path = self.config_path(owner, &task_id, &config_id);
            return remove_file_if_exists(&path).map_err(|_| InvalidPushNotificationConfigError);
        }

        let task_dir = self.owner_dir(owner).join(task_id);
        let Ok(paths) = std::fs::read_dir(task_dir) else {
            return Ok(());
        };
        for path in paths.filter_map(|entry| entry.ok().map(|entry| entry.path())) {
            if is_json_file(&path) {
                remove_file_if_exists(&path).map_err(|_| InvalidPushNotificationConfigError)?;
            }
        }
        Ok(())
    }

    fn config_path(&self, owner: &str, task_id: &str, config_id: &str) -> PathBuf {
        self.owner_dir(owner)
            .join(task_id)
            .join(format!("{config_id}.json"))
    }

    fn owner_dir(&self, owner: &str) -> PathBuf {
        self.root.join(owner_hash(owner))
    }

    fn load_configs_for_owner(
        &self,
        owner_hash: &str,
        task_id: &str,
    ) -> Vec<TaskPushNotificationConfig> {
        let task_dir = self.root.join(owner_hash).join(task_id);
        sorted_json_files(&task_dir)
            .into_iter()
            .filter_map(|path| {
                let value = read_json_file(&path)?;
                TaskPushNotificationConfig::from_storage_json(&value, &self.secret_keyring)
            })
            .collect()
    }
}
