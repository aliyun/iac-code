use std::fmt;

#[derive(Clone, Debug, PartialEq, Eq)]
pub struct A2APushSecretError {
    message: String,
}

impl A2APushSecretError {
    pub(in crate::push_secrets) fn new(message: impl Into<String>) -> Self {
        Self {
            message: message.into(),
        }
    }
}

impl fmt::Display for A2APushSecretError {
    fn fmt(&self, formatter: &mut fmt::Formatter<'_>) -> fmt::Result {
        formatter.write_str(&self.message)
    }
}

impl std::error::Error for A2APushSecretError {}

impl From<std::io::Error> for A2APushSecretError {
    fn from(error: std::io::Error) -> Self {
        Self::new(error.to_string())
    }
}
