use std::fmt;
use std::io;

pub mod cloud_credentials;
pub mod credentials;
mod file_security;
pub mod i18n;
pub mod paths;
pub mod settings;
mod simple_yaml;

pub const CRATE_NAME: &str = "iac-code-config";

pub type ConfigResult<T> = Result<T, ConfigError>;

#[derive(Debug)]
pub enum ConfigError {
    Io(io::Error),
    InvalidProvider(String),
    InvalidValue(String),
}

impl fmt::Display for ConfigError {
    fn fmt(&self, formatter: &mut fmt::Formatter<'_>) -> fmt::Result {
        match self {
            ConfigError::Io(error) => write!(formatter, "{error}"),
            ConfigError::InvalidProvider(value) => write!(formatter, "invalid provider: {value}"),
            ConfigError::InvalidValue(value) => write!(formatter, "{value}"),
        }
    }
}

impl std::error::Error for ConfigError {}

impl From<io::Error> for ConfigError {
    fn from(value: io::Error) -> Self {
        ConfigError::Io(value)
    }
}
