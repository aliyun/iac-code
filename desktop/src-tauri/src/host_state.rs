use anyhow::{Context, Result};
use serde::{Deserialize, Serialize};
use sha2::{Digest, Sha256};
use std::fs::{self, File};
use std::io::Write;
use std::path::{Path, PathBuf};

pub const DEFAULT_LOOPBACK_PORT: u16 = 8766;
pub const DEFAULT_WINDOW_WIDTH: f64 = 1180.0;
pub const DEFAULT_WINDOW_HEIGHT: f64 = 780.0;
pub const MIN_WINDOW_WIDTH: f64 = 860.0;
pub const MIN_WINDOW_HEIGHT: f64 = 620.0;
const DETERMINISTIC_PORT_START: u16 = 12_000;
const DETERMINISTIC_PORT_COUNT: u16 = 18_000;
const MAX_DETERMINISTIC_CANDIDATES: usize = 32;
const MIN_VISIBLE_WINDOW_WIDTH: i64 = 64;
const MIN_VISIBLE_WINDOW_HEIGHT: i64 = 48;

#[derive(Clone, Debug, Default, Deserialize, PartialEq, Serialize)]
#[serde(default, rename_all = "camelCase")]
pub struct HostState {
    #[serde(default)]
    pub desktop_install_id: String,
    #[serde(default)]
    pub window_state: Option<WindowState>,
    pub recent_project: Option<PathBuf>,
    pub preferred_loopback_port: Option<u16>,
    pub preferred_loopback_port_source: Option<PortSource>,
    pub dismissed_update_version: Option<String>,
    pub next_sidecar_generation: u64,
}

#[derive(Clone, Debug, Default, Deserialize, PartialEq, Serialize)]
#[serde(default, rename_all = "camelCase")]
pub struct WindowState {
    pub physical_position: Option<PhysicalWindowPosition>,
    pub logical_size: Option<LogicalWindowSize>,
    pub physical_size: Option<PhysicalWindowSize>,
    pub scale_factor: Option<f64>,
    #[serde(default)]
    pub maximized: bool,
}

#[derive(Clone, Copy, Debug, Deserialize, PartialEq, Serialize)]
#[serde(rename_all = "camelCase")]
pub struct PhysicalWindowPosition {
    pub x: i32,
    pub y: i32,
}

#[derive(Clone, Copy, Debug, Deserialize, PartialEq, Serialize)]
#[serde(rename_all = "camelCase")]
pub struct LogicalWindowSize {
    pub width: f64,
    pub height: f64,
}

#[derive(Clone, Copy, Debug, Deserialize, Eq, PartialEq, Serialize)]
#[serde(rename_all = "camelCase")]
pub struct PhysicalWindowSize {
    pub width: u32,
    pub height: u32,
}

#[derive(Clone, Copy, Debug, PartialEq)]
pub struct MonitorWorkArea {
    pub x: i32,
    pub y: i32,
    pub width: u32,
    pub height: u32,
    pub scale_factor: f64,
}

#[derive(Clone, Copy, Debug, PartialEq)]
pub struct RestoredWindowPlacement {
    pub logical_position: Option<(f64, f64)>,
    pub logical_size: LogicalWindowSize,
    pub maximized: bool,
}

#[derive(Clone, Copy, Debug, Deserialize, Eq, PartialEq, Serialize)]
#[serde(rename_all = "snake_case")]
pub enum PortSource {
    Deterministic,
    OsFallback,
}

pub struct HostStateStore {
    path: PathBuf,
    state: HostState,
}

impl HostStateStore {
    pub fn open(root: &Path, expected_install_id: &str) -> Result<Self> {
        Self::open_with_legacy(root, None, expected_install_id)
    }

    pub fn open_with_legacy(
        root: &Path,
        legacy_root: Option<&Path>,
        expected_install_id: &str,
    ) -> Result<Self> {
        if expected_install_id.is_empty() {
            anyhow::bail!("Desktop install id cannot be empty");
        }
        fs::create_dir_all(root)
            .with_context(|| format!("create host-state directory {}", root.display()))?;
        let path = root.join("host-state.json");
        let (mut state, migrated_from_legacy) = match fs::read(&path) {
            Ok(raw) => (
                serde_json::from_slice(&raw).context("parse Desktop host state")?,
                false,
            ),
            Err(error) if error.kind() == std::io::ErrorKind::NotFound => {
                match legacy_root.filter(|legacy| *legacy != root) {
                    Some(legacy) => match fs::read(legacy.join("host-state.json")) {
                        Ok(raw) => {
                            let legacy_state: HostState = serde_json::from_slice(&raw)
                                .context("parse legacy Desktop host state")?;
                            if !legacy_state.desktop_install_id.is_empty()
                                && legacy_state.desktop_install_id != expected_install_id
                            {
                                (HostState::default(), false)
                            } else {
                                (legacy_state, true)
                            }
                        }
                        Err(error) if error.kind() == std::io::ErrorKind::NotFound => {
                            (HostState::default(), false)
                        }
                        Err(error) => return Err(error).context("read legacy Desktop host state"),
                    },
                    None => (HostState::default(), false),
                }
            }
            Err(error) => return Err(error).context("read Desktop host state"),
        };
        if !state.desktop_install_id.is_empty() && state.desktop_install_id != expected_install_id {
            anyhow::bail!(
                "Desktop host state belongs to install id {}, expected {}",
                state.desktop_install_id,
                expected_install_id
            );
        }
        let install_id_was_missing = state.desktop_install_id.is_empty();
        state.desktop_install_id = expected_install_id.to_string();
        let migrate_legacy_dynamic_port = state.preferred_loopback_port_source.is_none()
            && state
                .preferred_loopback_port
                .is_some_and(is_platform_dynamic_client_port);
        if migrate_legacy_dynamic_port {
            state.preferred_loopback_port = None;
        }
        let store = Self { path, state };
        if migrated_from_legacy || install_id_was_missing || migrate_legacy_dynamic_port {
            store.persist()?;
        }
        Ok(store)
    }

    pub fn state(&self) -> &HostState {
        &self.state
    }

    pub fn save_project(&mut self, project: PathBuf) -> Result<()> {
        let previous = self.state.recent_project.clone();
        self.state.recent_project = Some(project);
        if let Err(error) = self.persist() {
            self.state.recent_project = previous;
            return Err(error);
        }
        Ok(())
    }

    pub fn save_window_state(&mut self, window_state: WindowState) -> Result<()> {
        let previous = self.state.window_state.replace(window_state);
        if let Err(error) = self.persist() {
            self.state.window_state = previous;
            return Err(error);
        }
        Ok(())
    }

    pub fn claim_sidecar_generation(&mut self) -> Result<u64> {
        let generation = self
            .state
            .next_sidecar_generation
            .checked_add(1)
            .context("sidecar generation overflow")?;
        self.state.next_sidecar_generation = generation;
        self.persist()?;
        Ok(generation)
    }

    pub fn save_preferred_port(&mut self, port: u16, source: PortSource) -> Result<()> {
        self.state.preferred_loopback_port = Some(port);
        self.state.preferred_loopback_port_source = Some(source);
        self.persist()
    }

    pub fn save_dismissed_update_version(&mut self, version: String) -> Result<()> {
        let previous = self.state.dismissed_update_version.replace(version);
        if let Err(error) = self.persist() {
            self.state.dismissed_update_version = previous;
            return Err(error);
        }
        Ok(())
    }

    fn persist(&self) -> Result<()> {
        let parent = self
            .path
            .parent()
            .context("host-state path has no parent")?;
        let temporary = parent.join("host-state.json.tmp");
        let payload = serde_json::to_vec_pretty(&self.state)?;
        let mut file = File::create(&temporary).context("create temporary Desktop host state")?;
        #[cfg(unix)]
        {
            use std::os::unix::fs::PermissionsExt;
            file.set_permissions(fs::Permissions::from_mode(0o600))?;
        }
        file.write_all(&payload)
            .context("write Desktop host state")?;
        file.write_all(b"\n").context("finish Desktop host state")?;
        file.sync_all().context("sync Desktop host state")?;
        replace_file(&temporary, &self.path).context("replace Desktop host state")?;
        sync_directory(parent)?;
        Ok(())
    }
}

pub fn restored_window_placement(
    state: Option<&WindowState>,
    monitors: &[MonitorWorkArea],
) -> RestoredWindowPlacement {
    let default_size = LogicalWindowSize {
        width: DEFAULT_WINDOW_WIDTH,
        height: DEFAULT_WINDOW_HEIGHT,
    };
    let Some(state) = state else {
        return RestoredWindowPlacement {
            logical_position: None,
            logical_size: default_size,
            maximized: false,
        };
    };

    let visible_monitor = state.physical_position.and_then(|position| {
        monitors.iter().find(|monitor| {
            let scale = valid_scale(monitor.scale_factor).unwrap_or(1.0);
            let physical_size = valid_physical_size(state.physical_size).unwrap_or_else(|| {
                let logical_size = valid_logical_size(state.logical_size).unwrap_or(default_size);
                PhysicalWindowSize {
                    width: (logical_size.width * scale).round().max(1.0) as u32,
                    height: (logical_size.height * scale).round().max(1.0) as u32,
                }
            });
            has_visible_intersection(position, physical_size, monitor)
        })
    });
    let fallback_scale = visible_monitor
        .and_then(|monitor| valid_scale(monitor.scale_factor))
        .or_else(|| state.scale_factor.and_then(valid_scale))
        .unwrap_or(1.0);
    let logical_size = valid_logical_size(state.logical_size)
        .or_else(|| {
            valid_physical_size(state.physical_size).and_then(|physical| {
                valid_logical_size(Some(LogicalWindowSize {
                    width: f64::from(physical.width) / fallback_scale,
                    height: f64::from(physical.height) / fallback_scale,
                }))
            })
        })
        .unwrap_or(default_size);
    let logical_position = state.physical_position.and_then(|position| {
        visible_monitor.map(|monitor| {
            let scale = valid_scale(monitor.scale_factor).unwrap_or(1.0);
            (f64::from(position.x) / scale, f64::from(position.y) / scale)
        })
    });
    RestoredWindowPlacement {
        logical_position,
        logical_size,
        maximized: state.maximized,
    }
}

fn valid_scale(scale: f64) -> Option<f64> {
    (scale.is_finite() && (0.25..=8.0).contains(&scale)).then_some(scale)
}

fn valid_logical_size(size: Option<LogicalWindowSize>) -> Option<LogicalWindowSize> {
    size.filter(|size| {
        size.width.is_finite()
            && size.height.is_finite()
            && (MIN_WINDOW_WIDTH..=32_768.0).contains(&size.width)
            && (MIN_WINDOW_HEIGHT..=32_768.0).contains(&size.height)
    })
}

fn valid_physical_size(size: Option<PhysicalWindowSize>) -> Option<PhysicalWindowSize> {
    size.filter(|size| size.width > 0 && size.height > 0)
}

fn has_visible_intersection(
    position: PhysicalWindowPosition,
    size: PhysicalWindowSize,
    monitor: &MonitorWorkArea,
) -> bool {
    let window_left = i64::from(position.x);
    let window_top = i64::from(position.y);
    let window_right = window_left.saturating_add(i64::from(size.width));
    let window_bottom = window_top.saturating_add(i64::from(size.height));
    let monitor_left = i64::from(monitor.x);
    let monitor_top = i64::from(monitor.y);
    let monitor_right = monitor_left.saturating_add(i64::from(monitor.width));
    let monitor_bottom = monitor_top.saturating_add(i64::from(monitor.height));
    let visible_width = window_right.min(monitor_right) - window_left.max(monitor_left);
    let visible_height = window_bottom.min(monitor_bottom) - window_top.max(monitor_top);
    visible_width >= MIN_VISIBLE_WINDOW_WIDTH && visible_height >= MIN_VISIBLE_WINDOW_HEIGHT
}

#[cfg(not(windows))]
fn replace_file(source: &Path, destination: &Path) -> std::io::Result<()> {
    fs::rename(source, destination)
}

#[cfg(windows)]
fn replace_file(source: &Path, destination: &Path) -> std::io::Result<()> {
    use std::os::windows::ffi::OsStrExt;
    use windows::core::PCWSTR;
    use windows::Win32::Storage::FileSystem::{
        MoveFileExW, MOVEFILE_REPLACE_EXISTING, MOVEFILE_WRITE_THROUGH,
    };

    let source: Vec<u16> = source
        .as_os_str()
        .encode_wide()
        .chain(std::iter::once(0))
        .collect();
    let destination: Vec<u16> = destination
        .as_os_str()
        .encode_wide()
        .chain(std::iter::once(0))
        .collect();
    unsafe {
        MoveFileExW(
            PCWSTR(source.as_ptr()),
            PCWSTR(destination.as_ptr()),
            MOVEFILE_REPLACE_EXISTING | MOVEFILE_WRITE_THROUGH,
        )
    }
    .map_err(std::io::Error::other)
}

fn is_platform_dynamic_client_port(port: u16) -> bool {
    if cfg!(target_os = "linux") {
        (32_768..=60_999).contains(&port)
    } else {
        (49_152..=65_535).contains(&port)
    }
}

#[cfg(unix)]
fn sync_directory(path: &Path) -> Result<()> {
    File::open(path)?.sync_all()?;
    Ok(())
}

#[cfg(not(unix))]
fn sync_directory(_path: &Path) -> Result<()> {
    Ok(())
}

pub fn deterministic_port_candidates(install_id: &str) -> Vec<u16> {
    let digest = Sha256::digest(install_id.as_bytes());
    let offset = u64::from_be_bytes(digest[..8].try_into().expect("SHA-256 prefix length"))
        % u64::from(DETERMINISTIC_PORT_COUNT);
    let first = DETERMINISTIC_PORT_START + offset as u16;
    std::iter::once(DEFAULT_LOOPBACK_PORT)
        .chain((0..MAX_DETERMINISTIC_CANDIDATES).map(|index| {
            DETERMINISTIC_PORT_START
                + ((first - DETERMINISTIC_PORT_START + index as u16) % DETERMINISTIC_PORT_COUNT)
        }))
        .collect()
}

#[cfg(test)]
mod tests {
    use super::*;

    const INSTALL_ID: &str = "com-alibaba-cloud-iaccode-desktop-appimage";

    #[test]
    fn port_candidates_are_stable_and_outside_default_dynamic_ranges() {
        let first = deterministic_port_candidates("com-alibaba-cloud-iaccode-stable");
        let second = deterministic_port_candidates("com-alibaba-cloud-iaccode-stable");
        assert_eq!(first, second);
        assert_eq!(first[0], DEFAULT_LOOPBACK_PORT);
        assert_eq!(first.len(), 33);
        assert!(first[1..]
            .iter()
            .all(|port| (12_000..30_000).contains(port)));
    }

    #[test]
    fn generation_is_persisted_before_it_is_returned() {
        let root =
            std::env::temp_dir().join(format!("iac-code-host-state-{}", uuid::Uuid::new_v4()));
        let mut store = HostStateStore::open(&root, INSTALL_ID).unwrap();
        assert_eq!(store.claim_sidecar_generation().unwrap(), 1);
        drop(store);
        let mut reopened = HostStateStore::open(&root, INSTALL_ID).unwrap();
        assert_eq!(reopened.claim_sidecar_generation().unwrap(), 2);
        fs::remove_dir_all(root).unwrap();
    }

    #[test]
    fn legacy_dynamic_port_is_not_kept_as_a_preferred_origin() {
        let root = std::env::temp_dir().join(format!(
            "iac-code-host-state-migration-{}",
            uuid::Uuid::new_v4()
        ));
        fs::create_dir_all(&root).unwrap();
        let dynamic_port = if cfg!(target_os = "linux") {
            40_000
        } else {
            55_000
        };
        fs::write(
            root.join("host-state.json"),
            format!("{{\"preferredLoopbackPort\":{dynamic_port},\"nextSidecarGeneration\":0}}"),
        )
        .unwrap();
        let store = HostStateStore::open(&root, INSTALL_ID).unwrap();
        assert_eq!(store.state().preferred_loopback_port, None);
        assert_eq!(store.state().desktop_install_id, INSTALL_ID);
        let persisted: HostState =
            serde_json::from_slice(&fs::read(root.join("host-state.json")).unwrap()).unwrap();
        assert_eq!(persisted.preferred_loopback_port, None);
        fs::remove_dir_all(root).unwrap();
    }

    #[test]
    fn legacy_state_is_copied_into_channel_state_without_removing_source() {
        let family_root = std::env::temp_dir().join(format!(
            "iac-code-host-state-family-migration-{}",
            uuid::Uuid::new_v4()
        ));
        let channel_root = family_root.join("appimage");
        fs::create_dir_all(&family_root).unwrap();
        let legacy = br#"{"recentProject":"/tmp/project","preferredLoopbackPort":8766,"preferredLoopbackPortSource":"deterministic","nextSidecarGeneration":7}"#;
        fs::write(family_root.join("host-state.json"), legacy).unwrap();

        let store = HostStateStore::open_with_legacy(&channel_root, Some(&family_root), INSTALL_ID)
            .unwrap();

        assert_eq!(store.state().next_sidecar_generation, 7);
        assert_eq!(store.state().desktop_install_id, INSTALL_ID);
        assert_eq!(
            fs::read(family_root.join("host-state.json")).unwrap(),
            legacy
        );
        assert!(channel_root.join("host-state.json").is_file());
        fs::remove_dir_all(family_root).unwrap();
    }

    #[test]
    fn frozen_install_id_rejects_a_mismatched_channel_state() {
        let root = std::env::temp_dir().join(format!(
            "iac-code-host-state-install-id-{}",
            uuid::Uuid::new_v4()
        ));
        fs::create_dir_all(&root).unwrap();
        fs::write(
            root.join("host-state.json"),
            r#"{"desktopInstallId":"other-channel","nextSidecarGeneration":0}"#,
        )
        .unwrap();
        let error = HostStateStore::open(&root, INSTALL_ID)
            .err()
            .expect("mismatched install id should fail");
        assert!(error.to_string().contains("belongs to install id"));
        fs::remove_dir_all(root).unwrap();
    }

    #[test]
    fn window_geometry_and_maximized_state_are_persisted() {
        let root = std::env::temp_dir().join(format!(
            "iac-code-host-state-window-{}",
            uuid::Uuid::new_v4()
        ));
        let window_state = WindowState {
            physical_position: Some(PhysicalWindowPosition { x: 40, y: 80 }),
            logical_size: Some(LogicalWindowSize {
                width: 1200.0,
                height: 800.0,
            }),
            physical_size: Some(PhysicalWindowSize {
                width: 2400,
                height: 1600,
            }),
            scale_factor: Some(2.0),
            maximized: true,
        };
        let mut store = HostStateStore::open(&root, INSTALL_ID).unwrap();
        store.save_window_state(window_state.clone()).unwrap();
        drop(store);

        let reopened = HostStateStore::open(&root, INSTALL_ID).unwrap();

        assert_eq!(reopened.state().window_state.as_ref(), Some(&window_state));
        fs::remove_dir_all(root).unwrap();
    }

    #[test]
    fn dismissed_update_version_is_persisted_in_channel_state() {
        let root = std::env::temp_dir().join(format!(
            "iac-code-host-state-dismissed-update-{}",
            uuid::Uuid::new_v4()
        ));
        let mut store = HostStateStore::open(&root, INSTALL_ID).unwrap();
        store
            .save_dismissed_update_version("0.12.0".to_string())
            .unwrap();
        drop(store);

        let reopened = HostStateStore::open(&root, INSTALL_ID).unwrap();

        assert_eq!(
            reopened.state().dismissed_update_version.as_deref(),
            Some("0.12.0")
        );
        fs::remove_dir_all(root).unwrap();
    }

    #[test]
    fn old_host_state_without_window_fields_remains_compatible() {
        let state: HostState = serde_json::from_str(
            r#"{"preferredLoopbackPort":8766,"preferredLoopbackPortSource":"deterministic"}"#,
        )
        .unwrap();
        assert!(state.desktop_install_id.is_empty());
        assert_eq!(state.window_state, None);
        assert_eq!(state.dismissed_update_version, None);
        assert_eq!(state.next_sidecar_generation, 0);
    }

    #[test]
    fn invalid_or_offscreen_window_geometry_falls_back_safely() {
        let monitors = [MonitorWorkArea {
            x: 0,
            y: 0,
            width: 1920,
            height: 1080,
            scale_factor: 1.0,
        }];
        let state = WindowState {
            physical_position: Some(PhysicalWindowPosition {
                x: 10_000,
                y: 10_000,
            }),
            logical_size: Some(LogicalWindowSize {
                width: MIN_WINDOW_WIDTH - 1.0,
                height: MIN_WINDOW_HEIGHT - 1.0,
            }),
            physical_size: Some(PhysicalWindowSize {
                width: 100,
                height: 100,
            }),
            scale_factor: Some(1.0),
            maximized: false,
        };

        let placement = restored_window_placement(Some(&state), &monitors);

        assert_eq!(placement.logical_position, None);
        assert_eq!(placement.logical_size.width, DEFAULT_WINDOW_WIDTH);
        assert_eq!(placement.logical_size.height, DEFAULT_WINDOW_HEIGHT);
    }

    #[test]
    fn visible_physical_geometry_restores_with_current_monitor_scale() {
        let monitors = [MonitorWorkArea {
            x: 2000,
            y: 0,
            width: 2560,
            height: 1600,
            scale_factor: 2.0,
        }];
        let state = WindowState {
            physical_position: Some(PhysicalWindowPosition { x: 2200, y: 200 }),
            logical_size: None,
            physical_size: Some(PhysicalWindowSize {
                width: 2360,
                height: 1560,
            }),
            scale_factor: Some(1.0),
            maximized: true,
        };

        let placement = restored_window_placement(Some(&state), &monitors);

        assert_eq!(placement.logical_position, Some((1100.0, 100.0)));
        assert_eq!(placement.logical_size.width, DEFAULT_WINDOW_WIDTH);
        assert_eq!(placement.logical_size.height, DEFAULT_WINDOW_HEIGHT);
        assert!(placement.maximized);
    }
}
