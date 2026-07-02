use std::fs;
use std::io::ErrorKind;

use crate::file_security::write_private_file;
use crate::paths::ConfigPaths;
use crate::{ConfigError, ConfigResult};

pub(super) fn read_settings_content(paths: &ConfigPaths) -> ConfigResult<String> {
    match fs::read_to_string(&paths.settings_path) {
        Ok(content) => Ok(content),
        Err(error) if error.kind() == ErrorKind::NotFound => Ok(String::new()),
        Err(error) => Err(ConfigError::from(error)),
    }
}

pub(super) fn write_settings_content(paths: &ConfigPaths, content: String) -> ConfigResult<()> {
    write_private_file(&paths.settings_path, content).map_err(ConfigError::from)
}
