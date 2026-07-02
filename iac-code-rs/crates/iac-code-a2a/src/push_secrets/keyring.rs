use std::collections::BTreeMap;
use std::path::{Path, PathBuf};

use iac_code_protocol::json::{self, JsonValue};

use super::fernet::{fernet_decrypt, fernet_encrypt, generate_fernet_key, FernetKey};
use super::fs::restrict_permissions;
use super::key_ids::{current_time_seconds, new_key_id};
use super::{json_string, A2APushSecretEnvelope, A2APushSecretError};

#[derive(Clone, Debug)]
pub struct A2APushSecretKeyring {
    path: PathBuf,
    loaded: bool,
    env_managed: bool,
    active_key_id: String,
    keys: BTreeMap<String, String>,
}

impl A2APushSecretKeyring {
    pub fn new(path: impl AsRef<Path>) -> Self {
        Self {
            path: path.as_ref().to_path_buf(),
            loaded: false,
            env_managed: false,
            active_key_id: String::new(),
            keys: BTreeMap::new(),
        }
    }

    pub fn active_key_id(&mut self) -> Result<&str, A2APushSecretError> {
        self.ensure_loaded()?;
        Ok(&self.active_key_id)
    }

    pub fn encrypt(&mut self, value: &str) -> Result<A2APushSecretEnvelope, A2APushSecretError> {
        self.ensure_loaded()?;
        let key_id = self.active_key_id.clone();
        let key = self.keys.get(&key_id).ok_or_else(|| {
            A2APushSecretError::new("A2A push secret keyring does not contain its active key")
        })?;
        let token = fernet_encrypt(key, value.as_bytes())?;
        Ok(A2APushSecretEnvelope::new(key_id, token))
    }

    pub fn decrypt(
        &mut self,
        envelope: &A2APushSecretEnvelope,
    ) -> Result<String, A2APushSecretError> {
        self.ensure_loaded()?;
        let key = self.keys.get(envelope.key_id()).ok_or_else(|| {
            A2APushSecretError::new(format!(
                "A2A push secret encryption key is not available: {}",
                envelope.key_id()
            ))
        })?;
        let plaintext = fernet_decrypt(key, envelope.ciphertext())?;
        String::from_utf8(plaintext).map_err(|_| {
            A2APushSecretError::new("A2A push secret ciphertext could not be decrypted")
        })
    }

    pub fn rotate(&mut self, key_id: Option<&str>) -> Result<String, A2APushSecretError> {
        self.ensure_loaded()?;
        if self.env_managed {
            return Err(A2APushSecretError::new(
                "A2A push secret keyring is environment-managed and cannot be rotated locally",
            ));
        }
        let key_id = key_id.map_or_else(new_key_id, ToOwned::to_owned);
        if self.keys.contains_key(&key_id) {
            return Err(A2APushSecretError::new(format!(
                "A2A push secret encryption key already exists: {key_id}"
            )));
        }
        self.keys.insert(key_id.clone(), generate_fernet_key()?);
        self.active_key_id = key_id.clone();
        self.write()?;
        Ok(key_id)
    }

    fn ensure_loaded(&mut self) -> Result<(), A2APushSecretError> {
        if self.loaded {
            return Ok(());
        }

        if let Ok(value) = std::env::var("IAC_CODE_A2A_PUSH_KEYRING") {
            if !value.is_empty() {
                let value = json::parse(&value)
                    .map_err(|_| A2APushSecretError::new("A2A push secret keyring is malformed"))?;
                self.load_data(&value)?;
                self.env_managed = true;
                self.loaded = true;
                return Ok(());
            }
        }

        if self.path.exists() {
            let value = std::fs::read_to_string(&self.path)?;
            let value = json::parse(&value)
                .map_err(|_| A2APushSecretError::new("A2A push secret keyring is malformed"))?;
            self.load_data(&value)?;
            self.loaded = true;
            return Ok(());
        }

        self.active_key_id = new_key_id();
        self.keys
            .insert(self.active_key_id.clone(), generate_fernet_key()?);
        self.loaded = true;
        self.write()
    }

    fn load_data(&mut self, value: &JsonValue) -> Result<(), A2APushSecretError> {
        let JsonValue::Object(object) = value else {
            return Err(A2APushSecretError::new(
                "A2A push secret keyring is malformed",
            ));
        };
        let Some(JsonValue::Array(keys)) = object.get("keys") else {
            return Err(A2APushSecretError::new(
                "A2A push secret keyring is malformed",
            ));
        };

        self.keys.clear();
        for item in keys {
            let JsonValue::Object(item) = item else {
                continue;
            };
            let Some(key_id) = json_string(item, "id") else {
                continue;
            };
            let Some(fernet_key) = json_string(item, "fernetKey") else {
                continue;
            };
            FernetKey::decode(&fernet_key)?;
            self.keys.insert(key_id, fernet_key);
        }

        self.active_key_id = json_string(object, "activeKeyId").unwrap_or_default();
        if self.active_key_id.is_empty() || !self.keys.contains_key(&self.active_key_id) {
            return Err(A2APushSecretError::new(
                "A2A push secret keyring does not contain its active key",
            ));
        }
        Ok(())
    }

    fn write(&self) -> Result<(), A2APushSecretError> {
        if let Some(parent) = self.path.parent() {
            std::fs::create_dir_all(parent)?;
            restrict_permissions(parent, true)?;
        }
        let created_at = current_time_seconds();
        let keys = self
            .keys
            .iter()
            .map(|(key_id, key)| {
                json::object([
                    ("createdAt", json::number(created_at)),
                    ("fernetKey", json::string(key)),
                    ("id", json::string(key_id)),
                ])
            })
            .collect::<Vec<_>>();
        let value = json::object([
            ("activeKeyId", json::string(&self.active_key_id)),
            ("keys", json::array(keys)),
        ]);
        std::fs::write(&self.path, value.to_compact_json())?;
        restrict_permissions(&self.path, false)
    }
}
