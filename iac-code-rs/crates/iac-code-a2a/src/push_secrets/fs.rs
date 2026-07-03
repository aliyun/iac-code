use std::path::Path;

use super::A2APushSecretError;

#[cfg(unix)]
pub(super) fn restrict_permissions(path: &Path, directory: bool) -> Result<(), A2APushSecretError> {
    use std::os::unix::fs::PermissionsExt;

    let mode = if directory { 0o700 } else { 0o600 };
    let mut permissions = std::fs::metadata(path)?.permissions();
    permissions.set_mode(mode);
    std::fs::set_permissions(path, permissions)?;
    Ok(())
}

#[cfg(not(unix))]
pub(super) fn restrict_permissions(
    _path: &Path,
    _directory: bool,
) -> Result<(), A2APushSecretError> {
    Ok(())
}
