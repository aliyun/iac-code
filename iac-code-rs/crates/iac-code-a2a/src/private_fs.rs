use std::path::Path;

pub(crate) fn ensure_private_dir(path: &Path) -> std::io::Result<()> {
    std::fs::create_dir_all(path)?;
    restrict_dir_permissions(path)
}

pub(crate) fn write_private_file(path: &Path, content: impl AsRef<[u8]>) -> std::io::Result<()> {
    if let Some(parent) = path.parent() {
        ensure_private_dir(parent)?;
    }
    std::fs::write(path, content)?;
    restrict_file_permissions(path)
}

#[cfg(unix)]
fn restrict_dir_permissions(path: &Path) -> std::io::Result<()> {
    use std::os::unix::fs::PermissionsExt;

    let mut permissions = std::fs::metadata(path)?.permissions();
    permissions.set_mode(0o700);
    std::fs::set_permissions(path, permissions)
}

#[cfg(not(unix))]
fn restrict_dir_permissions(_path: &Path) -> std::io::Result<()> {
    Ok(())
}

#[cfg(unix)]
fn restrict_file_permissions(path: &Path) -> std::io::Result<()> {
    use std::os::unix::fs::PermissionsExt;

    let mut permissions = std::fs::metadata(path)?.permissions();
    permissions.set_mode(0o600);
    std::fs::set_permissions(path, permissions)
}

#[cfg(not(unix))]
fn restrict_file_permissions(_path: &Path) -> std::io::Result<()> {
    Ok(())
}
