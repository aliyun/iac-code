use std::path::{Path, PathBuf};

use crate::transport::A2ATransportConfigError;

pub fn validate_socket_path(
    socket_path: impl AsRef<Path>,
) -> Result<PathBuf, A2ATransportConfigError> {
    let path = socket_path.as_ref();
    let parent = path
        .parent()
        .filter(|parent| !parent.as_os_str().is_empty())
        .unwrap_or_else(|| Path::new("."));
    if !parent.exists() {
        return Err(A2ATransportConfigError::new(format!(
            "Unix socket parent does not exist: {}",
            parent.display()
        )));
    }
    Ok(path.to_path_buf())
}
