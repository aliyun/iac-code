use crate::control::{read_message, write_message, ChildGroupTracker};
use crate::host_state::{deterministic_port_candidates, PortSource};
use crate::lifecycle::LifecycleState;
use crate::AppState;
use anyhow::{bail, Context, Result};
use parking_lot::Mutex;
use serde::Deserialize;
use serde_json::{json, Value};
use std::collections::{BTreeMap, VecDeque};
use std::ffi::{OsStr, OsString};
use std::fs::{File, OpenOptions};
#[cfg(unix)]
use std::io::{BufRead, BufReader};
use std::io::{Read, Write};
use std::net::TcpStream;
use std::path::{Path, PathBuf};
#[cfg(unix)]
use std::process::{Child, Command, Stdio};
use std::sync::atomic::{AtomicBool, Ordering};
use std::sync::mpsc::{self, Receiver};
use std::sync::Arc;
use std::thread;
use std::time::{Duration, Instant};
use tauri::{AppHandle, Manager};
use tauri_plugin_dialog::{DialogExt, MessageDialogButtons, MessageDialogResult};

const STARTUP_TIMEOUT: Duration = Duration::from_secs(20);
const STARTUP_RECOVERY_MAX_TIMEOUT: Duration = Duration::from_secs(360);
const STARTUP_RECOVERY_PROTOCOL_GRACE: Duration = Duration::from_secs(10);
const CONTROL_RESPONSE_TIMEOUT: Duration = Duration::from_secs(10);
const DESKTOP_PROTOCOL_VERSION: u64 = 1;
const HOST_CAPTURE_MAX_BYTES: u64 = 5 * 1024 * 1024;
const HOST_CAPTURE_BACKUPS: usize = 3;
#[cfg(unix)]
const SUPERVISOR_GRACEFUL_REAP_TIMEOUT: Duration = Duration::from_secs(3);
const SUPERVISOR_FORCE_REAP_TIMEOUT: Duration = Duration::from_secs(1);
#[cfg(unix)]
const LOGIN_SHELL_PATH_TIMEOUT: Duration = Duration::from_secs(2);

#[derive(Debug, thiserror::Error)]
#[error("Desktop sidecar startup failed ({code}): {message}")]
struct StartupFailure {
    code: String,
    message: String,
}

struct StartTransaction<'a> {
    lifecycle: &'a Mutex<crate::lifecycle::LifecycleCoordinator>,
    committed: bool,
}

impl<'a> StartTransaction<'a> {
    fn new(lifecycle: &'a Mutex<crate::lifecycle::LifecycleCoordinator>) -> Self {
        Self {
            lifecycle,
            committed: false,
        }
    }

    fn commit(&mut self) {
        self.committed = true;
    }
}

impl Drop for StartTransaction<'_> {
    fn drop(&mut self) {
        if !self.committed {
            let mut lifecycle = self.lifecycle.lock();
            lifecycle.healthy_origin = None;
            lifecycle.begin(LifecycleState::Recovering);
        }
    }
}

pub struct SidecarHandle {
    pub generation: u64,
    pub port: u16,
    control_writer: Arc<Mutex<ControlWriter>>,
    control_messages: Mutex<Receiver<Value>>,
    pending_messages: Mutex<VecDeque<Value>>,
    request_lock: Mutex<()>,
    supervisor: Mutex<Supervisor>,
    exited: Arc<AtomicBool>,
    #[cfg(unix)]
    liveness_writer: Option<LivenessWriter>,
}

#[cfg(unix)]
type ControlWriter = std::os::unix::net::UnixStream;
#[cfg(unix)]
type LivenessWriter = std::os::unix::net::UnixStream;

#[cfg(unix)]
struct Supervisor {
    child: Option<Child>,
}

#[cfg(unix)]
impl Supervisor {
    fn new(child: Child) -> Self {
        Self { child: Some(child) }
    }
}

#[cfg(unix)]
impl Drop for Supervisor {
    fn drop(&mut self) {
        terminate_supervisor(self);
    }
}

#[cfg(windows)]
type ControlWriter = File;

#[cfg(windows)]
struct Supervisor {
    process: Option<windows::Win32::Foundation::HANDLE>,
    job: Option<windows::Win32::Foundation::HANDLE>,
}

#[cfg(windows)]
unsafe impl Send for Supervisor {}

#[cfg(windows)]
impl Drop for Supervisor {
    fn drop(&mut self) {
        use windows::Win32::Foundation::CloseHandle;
        terminate_supervisor(self);
        if let Some(process) = self.process.take() {
            let _ = unsafe { CloseHandle(process) };
        }
        if let Some(job) = self.job.take() {
            let _ = unsafe { CloseHandle(job) };
        }
    }
}

impl SidecarHandle {
    pub fn request(&self, request: Value, response_type: &str) -> Result<Value> {
        let _request = self.request_lock.lock();
        write_message(&mut *self.control_writer.lock(), &request)?;
        let deadline = Instant::now() + CONTROL_RESPONSE_TIMEOUT;
        loop {
            let pending_response = {
                let mut pending = self.pending_messages.lock();
                pending
                    .iter()
                    .position(|message| response_matches_request(&request, message, response_type))
                    .and_then(|index| pending.remove(index))
            };
            if let Some(response) = pending_response {
                return Ok(response);
            }
            let remaining = deadline.saturating_duration_since(Instant::now());
            let response = self
                .control_messages
                .lock()
                .recv_timeout(remaining)
                .context("wait for Desktop sidecar control response")?;
            if response_matches_request(&request, &response, response_type) {
                return Ok(response);
            }
            if response.get("type").and_then(Value::as_str) == Some("eof") {
                bail!("Desktop sidecar control channel closed");
            }
            self.pending_messages.lock().push_back(response);
        }
    }

    pub fn wait(&self) -> Result<()> {
        wait_supervisor(&mut self.supervisor.lock())
    }

    fn abort(mut self) {
        self.terminate_container();
    }

    fn terminate_container(&mut self) {
        #[cfg(unix)]
        self.liveness_writer.take();
        terminate_supervisor(&mut self.supervisor.lock());
    }
}

impl Drop for SidecarHandle {
    fn drop(&mut self) {
        self.terminate_container();
    }
}

fn response_matches_request(request: &Value, response: &Value, response_type: &str) -> bool {
    if response.get("type").and_then(Value::as_str) != Some(response_type) {
        return false;
    }
    ["pickerOperationId", "sourceGeneration", "sidecarGeneration"]
        .into_iter()
        .all(|key| request.get(key).is_none() || request.get(key) == response.get(key))
}

fn start_control_dispatcher(
    mut reader: impl Read + Send + 'static,
    writer: Arc<Mutex<ControlWriter>>,
    generation: u64,
    message_sender: mpsc::Sender<Value>,
    exited: Arc<AtomicBool>,
) {
    thread::spawn(move || {
        let tracker = Arc::new(Mutex::new(ChildGroupTracker::new(generation)));
        while let Ok(Some(message)) = read_message(&mut reader) {
            if let Some(response) = tracker.lock().dispatch(&message) {
                if write_message(&mut *writer.lock(), &response).is_err() {
                    break;
                }
            } else if message_sender.send(message).is_err() {
                break;
            }
        }
        // Guardian control writers belong exclusively to the sidecar. Once its
        // carrier closes, each guardian observes EOF and owns bounded teardown;
        // the Host only forgets generation-scoped records.
        tracker.lock().clear_on_sidecar_exit();
        exited.store(true, Ordering::Release);
        let _ = message_sender.send(json!({"type": "eof"}));
    });
}

fn wait_for_startup_message(receiver: &Receiver<Value>, generation: u64) -> Result<Value> {
    let mut deadline = Instant::now() + STARTUP_TIMEOUT;
    let mut recovery_announced = false;
    loop {
        let remaining = deadline.saturating_duration_since(Instant::now());
        let message = receiver
            .recv_timeout(remaining)
            .context("wait for Desktop sidecar readiness")?;
        if message.get("type").and_then(Value::as_str) != Some("startup-recovery-begin") {
            return Ok(message);
        }
        if recovery_announced {
            bail!("Desktop startup recovery was announced more than once");
        }
        recovery_announced = true;
        if message.get("sidecarGeneration").and_then(Value::as_u64) != Some(generation) {
            bail!("Desktop startup recovery generation mismatch");
        }
        let timeout = message
            .get("timeoutSeconds")
            .and_then(Value::as_f64)
            .filter(|value| value.is_finite() && *value > 0.0)
            .map(Duration::from_secs_f64)
            .context("Desktop startup recovery timeout is invalid")?;
        if timeout > STARTUP_RECOVERY_MAX_TIMEOUT {
            bail!("Desktop startup recovery timeout exceeds the Host limit");
        }
        let recovery_deadline = Instant::now() + timeout + STARTUP_RECOVERY_PROTOCOL_GRACE;
        if recovery_deadline > deadline {
            deadline = recovery_deadline;
        }
    }
}

#[cfg(unix)]
fn wait_supervisor(supervisor: &mut Supervisor) -> Result<()> {
    let Some(child) = supervisor.child.take() else {
        return Ok(());
    };
    reap_supervisor(child, CONTROL_RESPONSE_TIMEOUT)
}

#[cfg(unix)]
fn terminate_supervisor(supervisor: &mut Supervisor) {
    let Some(child) = supervisor.child.take() else {
        return;
    };
    let _ = reap_supervisor(child, SUPERVISOR_GRACEFUL_REAP_TIMEOUT);
}

#[cfg(unix)]
fn wait_for_child_exit(child: &mut Child, timeout: Duration) -> Result<bool> {
    let deadline = Instant::now() + timeout;
    loop {
        if child
            .try_wait()
            .context("poll Desktop sidecar supervisor")?
            .is_some()
        {
            return Ok(true);
        }
        if Instant::now() >= deadline {
            return Ok(false);
        }
        thread::sleep(Duration::from_millis(20));
    }
}

#[cfg(unix)]
fn reap_supervisor(mut child: Child, graceful_timeout: Duration) -> Result<()> {
    if matches!(wait_for_child_exit(&mut child, graceful_timeout), Ok(true)) {
        return Ok(());
    }

    // The native supervisor is the generation process-group leader. It is
    // still an unreaped child here, so its PID cannot have been reused. Kill
    // the owned group rather than only the supervisor, which could orphan the
    // Python target during an early-start/control failure.
    let process_group = child.id() as libc::pid_t;
    if unsafe { libc::kill(-process_group, libc::SIGKILL) } != 0 {
        let _ = child.kill();
    }
    if matches!(
        wait_for_child_exit(&mut child, SUPERVISOR_FORCE_REAP_TIMEOUT),
        Ok(true)
    ) {
        return Ok(());
    }

    // Do not block the lifecycle coordinator indefinitely on a pathological
    // kernel/filesystem state. Ownership moves to a dedicated reaper so a
    // later exit can never become a zombie.
    thread::spawn(move || {
        let _ = child.wait();
    });
    bail!("Desktop sidecar supervisor cleanup exceeded its bounded reap deadline")
}

#[cfg(windows)]
fn wait_supervisor(supervisor: &mut Supervisor) -> Result<()> {
    wait_windows_supervisor(supervisor, CONTROL_RESPONSE_TIMEOUT)
}

#[cfg(windows)]
fn wait_windows_supervisor(supervisor: &Supervisor, timeout: Duration) -> Result<()> {
    use windows::Win32::Foundation::{WAIT_OBJECT_0, WAIT_TIMEOUT};
    use windows::Win32::System::Threading::WaitForSingleObject;
    let process = supervisor
        .process
        .context("Desktop sidecar process handle is closed")?;
    let result = unsafe { WaitForSingleObject(process, timeout.as_millis() as u32) };
    if result == WAIT_TIMEOUT {
        bail!("Desktop sidecar process exceeded its bounded reap deadline");
    }
    (result == WAIT_OBJECT_0).then_some(()).context(format!(
        "wait for Desktop sidecar process failed: {result:?}"
    ))
}

#[cfg(windows)]
fn terminate_supervisor(supervisor: &mut Supervisor) {
    use windows::Win32::System::JobObjects::TerminateJobObject;
    if let Some(job) = supervisor.job {
        let _ = unsafe { TerminateJobObject(job, 1) };
    }
    let _ = wait_windows_supervisor(supervisor, SUPERVISOR_FORCE_REAP_TIMEOUT);
}

#[cfg(unix)]
fn set_inheritable(stream: &std::os::unix::net::UnixStream) -> Result<()> {
    use std::os::fd::AsRawFd;
    let fd = stream.as_raw_fd();
    let flags = unsafe { libc::fcntl(fd, libc::F_GETFD) };
    if flags < 0 || unsafe { libc::fcntl(fd, libc::F_SETFD, flags & !libc::FD_CLOEXEC) } < 0 {
        return Err(std::io::Error::last_os_error()).context("make Desktop carrier inheritable");
    }
    Ok(())
}

#[cfg(unix)]
struct RotatingLog {
    path: PathBuf,
    file: File,
    written: u64,
}

#[cfg(unix)]
impl RotatingLog {
    fn open(path: PathBuf) -> Result<Self> {
        let file = OpenOptions::new().create(true).append(true).open(&path)?;
        let written = file.metadata()?.len();
        Ok(Self {
            path,
            file,
            written,
        })
    }

    fn write(&mut self, buffer: &[u8]) -> std::io::Result<()> {
        if self.written.saturating_add(buffer.len() as u64) > HOST_CAPTURE_MAX_BYTES {
            self.file.flush()?;
            for index in (1..=HOST_CAPTURE_BACKUPS).rev() {
                let source = if index == 1 {
                    self.path.clone()
                } else {
                    PathBuf::from(format!("{}.{}", self.path.display(), index - 1))
                };
                let destination = PathBuf::from(format!("{}.{}", self.path.display(), index));
                if source.exists() {
                    let _ = std::fs::remove_file(&destination);
                    std::fs::rename(source, destination)?;
                }
            }
            self.file = OpenOptions::new()
                .create(true)
                .write(true)
                .truncate(true)
                .open(&self.path)?;
            self.written = 0;
        }
        self.file.write_all(buffer)?;
        self.file.flush()?;
        self.written = self.written.saturating_add(buffer.len() as u64);
        Ok(())
    }
}

#[cfg(unix)]
fn drain_to_log(mut reader: impl Read + Send + 'static, log: Arc<Mutex<RotatingLog>>) {
    thread::spawn(move || {
        let mut buffer = [0_u8; 8192];
        loop {
            match reader.read(&mut buffer) {
                Ok(0) => return,
                Ok(size) => {
                    let mut file = log.lock();
                    let _ = file.write(&buffer[..size]);
                }
                Err(_) => return,
            }
        }
    });
}

#[derive(Default, Deserialize)]
#[serde(rename_all = "camelCase")]
struct DesktopPathSettings {
    #[serde(default)]
    tool_paths: BTreeMap<String, PathBuf>,
    #[serde(default)]
    search_paths: Vec<PathBuf>,
}

#[derive(Default, Deserialize)]
struct DesktopSettingsFile {
    #[serde(default)]
    desktop: DesktopPathSettings,
}

fn push_unique(entries: &mut Vec<PathBuf>, path: PathBuf) {
    if !path.as_os_str().is_empty() && !entries.contains(&path) {
        entries.push(path);
    }
}

fn configured_tool_path_matches(key: &str, executable: &Path) -> bool {
    const CONFIGURABLE_TOOLS: [&str; 6] = ["git", "terraform", "node", "npm", "npx", "infraguard"];
    if !CONFIGURABLE_TOOLS.contains(&key) || !executable.is_absolute() || !executable.is_file() {
        return false;
    }
    let Some(file_name) = executable.file_name().and_then(OsStr::to_str) else {
        return false;
    };
    [key.to_string(), format!("{key}.exe")]
        .iter()
        .any(|expected| file_name.eq_ignore_ascii_case(expected))
}

fn explicit_tool_directories(config_directory: &Path) -> Vec<PathBuf> {
    let settings = std::fs::read(config_directory.join("settings.yml"))
        .ok()
        .and_then(|raw| serde_yaml::from_slice::<DesktopSettingsFile>(&raw).ok())
        .unwrap_or_default()
        .desktop;
    let mut entries = Vec::new();
    for (key, executable) in settings.tool_paths {
        if !configured_tool_path_matches(&key, &executable) {
            continue;
        }
        if let Some(parent) = executable.parent() {
            push_unique(&mut entries, parent.to_path_buf());
        }
    }
    for directory in settings.search_paths {
        if directory.is_absolute() && directory.is_dir() {
            push_unique(&mut entries, directory);
        }
    }
    entries
}

#[cfg(unix)]
fn controlled_login_shell_path() -> Vec<PathBuf> {
    use std::os::unix::process::CommandExt;

    let shell = std::env::var_os("SHELL")
        .map(PathBuf::from)
        .unwrap_or_else(|| {
            if cfg!(target_os = "macos") {
                PathBuf::from("/bin/zsh")
            } else {
                PathBuf::from("/bin/sh")
            }
        });
    let shell_name = shell.file_name().and_then(OsStr::to_str);
    let allowed_shell = shell.is_absolute()
        && shell_name.is_some_and(|name| matches!(name, "sh" | "bash" | "zsh" | "ksh" | "fish"));
    if !allowed_shell || !shell.is_file() {
        return Vec::new();
    }
    let print_path = if shell_name == Some("fish") {
        "string join : $PATH"
    } else {
        "printf '%s' \"$PATH\""
    };

    let mut child = match Command::new(shell)
        .args(["-l", "-c", print_path])
        .process_group(0)
        .stdin(Stdio::null())
        .stdout(Stdio::piped())
        .stderr(Stdio::null())
        .spawn()
    {
        Ok(child) => child,
        Err(_) => return Vec::new(),
    };
    let deadline = Instant::now() + LOGIN_SHELL_PATH_TIMEOUT;
    loop {
        match child.try_wait() {
            Ok(Some(status)) if status.success() => {
                let mut output = Vec::new();
                if child
                    .stdout
                    .take()
                    .is_some_and(|stdout| stdout.take(64 * 1024).read_to_end(&mut output).is_ok())
                {
                    return std::env::split_paths(&OsString::from(
                        String::from_utf8_lossy(&output).trim(),
                    ))
                    .collect();
                }
                return Vec::new();
            }
            Ok(Some(_)) | Err(_) => return Vec::new(),
            Ok(None) if Instant::now() < deadline => {
                thread::sleep(Duration::from_millis(20));
            }
            Ok(None) => {
                let _ = unsafe { libc::kill(-(child.id() as libc::pid_t), libc::SIGKILL) };
                let _ = child.wait();
                return Vec::new();
            }
        }
    }
}

fn platform_default_path_entries() -> Vec<PathBuf> {
    #[cfg(windows)]
    {
        let windows = std::env::var_os("SystemRoot")
            .map(PathBuf::from)
            .unwrap_or_else(|| PathBuf::from(r"C:\Windows"));
        vec![windows.join("System32"), windows]
    }
    #[cfg(not(windows))]
    {
        [
            "/opt/homebrew/bin",
            "/usr/local/bin",
            "/usr/bin",
            "/bin",
            "/usr/sbin",
            "/sbin",
        ]
        .into_iter()
        .map(PathBuf::from)
        .collect()
    }
}

fn gui_path_entries(
    config_directory: &Path,
    sidecar_target: &Path,
    inherited: Option<&OsStr>,
    login_shell: Vec<PathBuf>,
    defaults: Vec<PathBuf>,
) -> Vec<PathBuf> {
    let mut entries = explicit_tool_directories(config_directory);
    if let Some(inherited) = inherited {
        for entry in std::env::split_paths(inherited) {
            push_unique(&mut entries, entry);
        }
    }
    for entry in login_shell {
        push_unique(&mut entries, entry);
    }
    // Bundled helpers are a platform fallback, not a user-configured tool
    // override, so they cannot precede the inherited or login-shell PATH.
    if let Some(bundled_tools) = sidecar_target.parent() {
        push_unique(&mut entries, bundled_tools.to_path_buf());
    }
    for entry in defaults {
        push_unique(&mut entries, entry);
    }
    entries
}

fn gui_path(app: &AppHandle, sidecar_target: &Path) -> Result<OsString> {
    let config_directory = config_dir(app)?;
    #[cfg(unix)]
    let login_shell = controlled_login_shell_path();
    #[cfg(windows)]
    let login_shell = Vec::new();
    let inherited = std::env::var_os("PATH");
    let entries = gui_path_entries(
        &config_directory,
        sidecar_target,
        inherited.as_deref(),
        login_shell,
        platform_default_path_entries(),
    );
    std::env::join_paths(&entries).context("assemble Desktop GUI PATH")
}

fn expand_dollar_variables(raw: &str) -> String {
    let mut expanded = String::with_capacity(raw.len());
    let mut cursor = 0;
    while let Some(offset) = raw[cursor..].find('$') {
        let dollar = cursor + offset;
        expanded.push_str(&raw[cursor..dollar]);
        let after = dollar + 1;
        let (name, end) = if raw[after..].starts_with('{') {
            let name_start = after + 1;
            match raw[name_start..].find('}') {
                Some(close) => (&raw[name_start..name_start + close], name_start + close + 1),
                None => {
                    expanded.push('$');
                    cursor = after;
                    continue;
                }
            }
        } else {
            let length = raw[after..]
                .bytes()
                .take_while(|byte| byte.is_ascii_alphanumeric() || *byte == b'_')
                .count();
            if length == 0 {
                expanded.push('$');
                cursor = after;
                continue;
            }
            (&raw[after..after + length], after + length)
        };
        if name.is_empty() {
            expanded.push_str(&raw[dollar..end]);
        } else if let Some(value) = std::env::var_os(name) {
            expanded.push_str(&value.to_string_lossy());
        } else {
            expanded.push_str(&raw[dollar..end]);
        }
        cursor = end;
    }
    expanded.push_str(&raw[cursor..]);
    expanded
}

fn normalize_absolute_path(path: PathBuf, relative_to: &Path) -> PathBuf {
    use std::path::Component;

    let absolute = if path.is_absolute() {
        path
    } else {
        relative_to.join(path)
    };
    if let Ok(canonical) = absolute.canonicalize() {
        return canonical;
    }
    let mut normalized = PathBuf::new();
    for component in absolute.components() {
        match component {
            Component::CurDir => {}
            Component::ParentDir => {
                normalized.pop();
            }
            Component::Prefix(_) | Component::RootDir | Component::Normal(_) => {
                normalized.push(component.as_os_str());
            }
        }
    }
    normalized
}

pub(crate) fn expand_config_path(raw: &OsStr, home: &Path, relative_to: &Path) -> Option<PathBuf> {
    let Some(raw) = raw.to_str() else {
        return Some(normalize_absolute_path(PathBuf::from(raw), relative_to));
    };
    let raw = raw.trim();
    if raw.is_empty() {
        return None;
    }
    // Match Python's expandvars(expanduser(raw)) order. In particular, an
    // environment variable whose value happens to begin with '~' is not
    // expanded a second time.
    let user_expanded = if raw == "~" {
        home.to_string_lossy().into_owned()
    } else if raw.starts_with("~/") || raw.starts_with("~\\") {
        home.join(&raw[2..]).to_string_lossy().into_owned()
    } else {
        raw.to_string()
    };
    Some(normalize_absolute_path(
        PathBuf::from(expand_dollar_variables(&user_expanded)),
        relative_to,
    ))
}

pub(crate) fn config_dir(app: &AppHandle) -> Result<PathBuf> {
    let home = app.path().home_dir()?;
    if let Some(configured) = std::env::var_os("IAC_CODE_CONFIG_DIR") {
        let runtime_dir = app.path().app_local_data_dir()?.join("runtime");
        if let Some(expanded) = expand_config_path(&configured, &home, &runtime_dir) {
            return Ok(expanded);
        }
    }
    Ok(home.join(".iac-code"))
}

fn check_health(port: u16) -> Result<()> {
    let mut stream = TcpStream::connect_timeout(
        &format!("127.0.0.1:{port}").parse()?,
        Duration::from_secs(1),
    )?;
    stream.set_read_timeout(Some(Duration::from_secs(2)))?;
    stream.write_all(b"GET /health HTTP/1.1\r\nHost: 127.0.0.1\r\nConnection: close\r\n\r\n")?;
    let mut response = Vec::new();
    stream.take(16 * 1024).read_to_end(&mut response)?;
    let response = String::from_utf8_lossy(&response);
    if !response.starts_with("HTTP/1.1 200") && !response.starts_with("HTTP/1.0 200") {
        bail!("Desktop sidecar health check did not return HTTP 200");
    }
    Ok(())
}

#[cfg(unix)]
fn helper_path(app: &AppHandle) -> Result<PathBuf> {
    if let Some(override_path) = std::env::var_os("IAC_CODE_DESKTOP_EXEC") {
        return Ok(PathBuf::from(override_path));
    }
    if cfg!(debug_assertions) {
        let current = std::env::current_exe()?;
        return Ok(current.with_file_name("iac-code-desktop-exec"));
    }
    Ok(app.path().resource_dir()?.join("bin/iac-code-desktop-exec"))
}

#[cfg(unix)]
fn sidecar_command(app: &AppHandle) -> Result<(PathBuf, Vec<String>)> {
    if let Some(override_path) = std::env::var_os("IAC_CODE_DESKTOP_SIDECAR") {
        return Ok((PathBuf::from(override_path), Vec::new()));
    }
    if cfg!(debug_assertions) {
        let root = PathBuf::from(env!("CARGO_MANIFEST_DIR")).join("../..");
        let python = root.join(".venv/bin/python");
        return Ok((
            python,
            vec!["-m".to_string(), "iac_code.desktop".to_string()],
        ));
    }
    Ok((
        app.path()
            .resource_dir()?
            .join("sidecar/iac-code-sidecar")
            .join("iac-code-sidecar"),
        Vec::new(),
    ))
}

#[cfg(windows)]
fn sidecar_command(app: &AppHandle) -> Result<(PathBuf, Vec<OsString>)> {
    if let Some(override_path) = std::env::var_os("IAC_CODE_DESKTOP_SIDECAR") {
        return Ok((PathBuf::from(override_path), Vec::new()));
    }
    if cfg!(debug_assertions) {
        let root = PathBuf::from(env!("CARGO_MANIFEST_DIR")).join("../..");
        return Ok((
            root.join(".venv/Scripts/python.exe"),
            vec![OsString::from("-m"), OsString::from("iac_code.desktop")],
        ));
    }
    Ok((
        app.path()
            .resource_dir()?
            .join("sidecar/iac-code-sidecar/iac-code-sidecar.exe"),
        Vec::new(),
    ))
}

pub(crate) fn distribution_channel() -> &'static str {
    env!("IAC_CODE_DESKTOP_CHANNEL")
}

fn update_mode() -> &'static str {
    if cfg!(feature = "updater") && env!("IAC_CODE_DESKTOP_UPDATER_CONFIGURED") == "1" {
        "tauri"
    } else {
        "external"
    }
}

pub(crate) fn install_id_for_identifier(identifier: &str) -> String {
    format!("{}-{}", identifier, distribution_channel())
        .to_ascii_lowercase()
        .chars()
        .map(|character| {
            if character.is_ascii_alphanumeric() {
                character
            } else {
                '-'
            }
        })
        .collect()
}

fn install_id(app: &AppHandle) -> String {
    install_id_for_identifier(&app.config().identifier)
}

#[cfg(unix)]
#[allow(clippy::too_many_arguments)]
fn append_posix_sidecar_arguments(
    arguments: &mut Vec<String>,
    requested_port: u16,
    desktop_install_id: String,
    host_state_dir: &Path,
    install_lock_dir: &Path,
    runtime_dir: &Path,
    project: &Path,
    generation: u64,
    control_fd: i32,
    capture_path: &Path,
) {
    arguments.extend([
        "--requested-port".to_string(),
        requested_port.to_string(),
        "--desktop-install-id".to_string(),
        desktop_install_id,
        "--host-state-dir".to_string(),
        host_state_dir.to_string_lossy().into_owned(),
        "--desktop-install-lock-dir".to_string(),
        install_lock_dir.to_string_lossy().into_owned(),
        "--runtime-dir".to_string(),
        runtime_dir.to_string_lossy().into_owned(),
        "--default-project-cwd".to_string(),
        project.to_string_lossy().into_owned(),
        "--distribution-channel".to_string(),
        distribution_channel().to_string(),
        "--update-mode".to_string(),
        update_mode().to_string(),
        "--sidecar-generation".to_string(),
        generation.to_string(),
        "--control-fd".to_string(),
        control_fd.to_string(),
        "--host-capture-path".to_string(),
        capture_path.to_string_lossy().into_owned(),
    ]);
}

#[cfg(unix)]
fn launch_once(
    app: &AppHandle,
    project: &Path,
    requested_port: u16,
    generation: u64,
    gui_environment_path: &OsStr,
) -> Result<(SidecarHandle, Value)> {
    use std::os::fd::AsRawFd;
    use std::os::unix::net::UnixStream;

    let state = app.state::<AppState>();
    let (host_control, child_control) = UnixStream::pair()?;
    let (liveness_writer, liveness_reader) = UnixStream::pair()?;
    let (status_reader, status_writer) = UnixStream::pair()?;
    set_inheritable(&child_control)?;
    set_inheritable(&liveness_reader)?;
    set_inheritable(&status_writer)?;

    let helper = helper_path(app)?;
    let (target, mut target_prefix) = sidecar_command(app)?;
    let capture_path = state
        .paths
        .log_dir
        .join(format!("host-sidecar-{generation}.log"));
    let capture = Arc::new(Mutex::new(RotatingLog::open(capture_path.clone())?));
    let python_log = state
        .paths
        .log_dir
        .join(format!("desktop-{generation}.log"));
    append_posix_sidecar_arguments(
        &mut target_prefix,
        requested_port,
        install_id(app),
        &state.paths.host_state_dir,
        &state.paths.install_lock_dir,
        &state.paths.runtime_dir,
        project,
        generation,
        child_control.as_raw_fd(),
        &capture_path,
    );
    let mut supervisor_command = Command::new(&helper);
    supervisor_command
        .arg("--sidecar-supervisor")
        .arg("--liveness-fd")
        .arg(liveness_reader.as_raw_fd().to_string())
        .arg("--status-fd")
        .arg(status_writer.as_raw_fd().to_string())
        .arg("--")
        .arg(&target)
        .args(target_prefix)
        .current_dir(&state.paths.runtime_dir)
        .env("IAC_CODE_LOG_DIR", &state.paths.log_dir)
        .env("IAC_CODE_CONFIG_DIR", config_dir(app)?)
        .env("IAC_CODE_DESKTOP_EXEC", &helper)
        .env("PATH", gui_environment_path)
        .env("PYTHONUTF8", "1");
    if !cfg!(debug_assertions) {
        supervisor_command.env(
            "TIKTOKEN_CACHE_DIR",
            app.path()
                .resource_dir()?
                .join("sidecar/iac-code-sidecar/_internal/iac_code/tokenizer_cache"),
        );
    }
    let supervisor = supervisor_command
        .stdout(Stdio::piped())
        .stderr(Stdio::piped())
        .spawn()
        .context("spawn Desktop sidecar supervisor")?;
    struct LaunchGuard {
        supervisor: Supervisor,
        liveness_writer: Option<LivenessWriter>,
    }
    impl Drop for LaunchGuard {
        fn drop(&mut self) {
            self.liveness_writer.take();
            terminate_supervisor(&mut self.supervisor);
        }
    }
    let mut launch_guard = LaunchGuard {
        supervisor: Supervisor::new(supervisor),
        liveness_writer: Some(liveness_writer),
    };
    drop(child_control);
    drop(liveness_reader);
    drop(status_writer);

    let supervisor = launch_guard
        .supervisor
        .child
        .as_mut()
        .context("Desktop sidecar supervisor is unavailable")?;
    if let Some(stdout) = supervisor.stdout.take() {
        drain_to_log(stdout, capture.clone());
    }
    if let Some(stderr) = supervisor.stderr.take() {
        drain_to_log(stderr, capture);
    }

    let mut status_line = String::new();
    status_reader.set_read_timeout(Some(STARTUP_TIMEOUT))?;
    BufReader::new(status_reader).read_line(&mut status_line)?;
    let identity: Value = serde_json::from_str(
        status_line
            .strip_prefix("SIDECAR_STARTED ")
            .context("invalid Desktop supervisor status")?
            .trim(),
    )?;
    let target_pid = identity
        .get("pid")
        .and_then(Value::as_u64)
        .context("missing sidecar target pid")?;

    let reader = host_control.try_clone()?;
    let writer = Arc::new(Mutex::new(host_control));
    let exited = Arc::new(AtomicBool::new(false));
    let (message_sender, message_receiver) = mpsc::channel();
    start_control_dispatcher(
        reader,
        writer.clone(),
        generation,
        message_sender,
        exited.clone(),
    );
    let first = wait_for_startup_message(&message_receiver, generation)?;
    if first.get("type").and_then(Value::as_str) == Some("ready") {
        if first.get("sidecarGeneration").and_then(Value::as_u64) != Some(generation)
            || first.get("pid").and_then(Value::as_u64) != Some(target_pid)
            || first.get("protocolVersion").and_then(Value::as_u64)
                != Some(DESKTOP_PROTOCOL_VERSION)
        {
            bail!("Desktop sidecar readiness identity mismatch");
        }
        let port = first
            .get("port")
            .and_then(Value::as_u64)
            .and_then(|value| u16::try_from(value).ok())
            .context("Desktop sidecar readiness port is invalid")?;
        if requested_port != 0 && port != requested_port {
            bail!("Desktop sidecar readiness port does not match the requested port");
        }
        let handle = SidecarHandle {
            generation,
            port,
            control_writer: writer,
            control_messages: Mutex::new(message_receiver),
            pending_messages: Mutex::new(VecDeque::new()),
            request_lock: Mutex::new(()),
            supervisor: Mutex::new(Supervisor {
                child: launch_guard.supervisor.child.take(),
            }),
            exited,
            liveness_writer: launch_guard.liveness_writer.take(),
        };
        let _ = python_log;
        return Ok((handle, first));
    }
    let code = first
        .get("code")
        .and_then(Value::as_str)
        .unwrap_or("protocol_error")
        .to_string();
    let message = first
        .get("message")
        .and_then(Value::as_str)
        .unwrap_or("unexpected first control message")
        .to_string();
    Err(StartupFailure { code, message }.into())
}

#[cfg(windows)]
mod windows_launcher {
    use super::*;
    use std::ffi::{OsStr, OsString};
    use std::mem::{size_of, zeroed};
    use std::os::windows::ffi::OsStrExt;
    use std::os::windows::io::{AsRawHandle, FromRawHandle, RawHandle};
    use windows::core::{BOOL, PCWSTR, PWSTR};
    use windows::Win32::Foundation::{
        CloseHandle, GetLastError, SetHandleInformation, ERROR_BROKEN_PIPE, ERROR_NO_DATA,
        ERROR_PIPE_CONNECTED, ERROR_PIPE_NOT_CONNECTED, HANDLE, HANDLE_FLAG_INHERIT,
        INVALID_HANDLE_VALUE,
    };
    use windows::Win32::Storage::FileSystem::PIPE_ACCESS_DUPLEX;
    use windows::Win32::System::JobObjects::{
        CreateJobObjectW, IsProcessInJob, JobObjectExtendedLimitInformation,
        SetInformationJobObject, JOBOBJECT_EXTENDED_LIMIT_INFORMATION,
        JOB_OBJECT_LIMIT_BREAKAWAY_OK, JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE,
    };
    use windows::Win32::System::Pipes::{
        ConnectNamedPipe, CreateNamedPipeW, GetNamedPipeClientProcessId, PeekNamedPipe,
        SetNamedPipeHandleState, PIPE_NOWAIT, PIPE_READMODE_BYTE, PIPE_REJECT_REMOTE_CLIENTS,
        PIPE_TYPE_BYTE, PIPE_WAIT,
    };
    use windows::Win32::System::Threading::{
        CreateProcessW, DeleteProcThreadAttributeList, InitializeProcThreadAttributeList,
        OpenProcess, UpdateProcThreadAttribute, CREATE_NO_WINDOW, EXTENDED_STARTUPINFO_PRESENT,
        LPPROC_THREAD_ATTRIBUTE_LIST, PROCESS_INFORMATION, PROCESS_QUERY_LIMITED_INFORMATION,
        PROC_THREAD_ATTRIBUTE_HANDLE_LIST, PROC_THREAD_ATTRIBUTE_JOB_LIST, STARTF_USESTDHANDLES,
        STARTUPINFOEXW,
    };

    struct AttributeList {
        storage: Vec<usize>,
        list: LPPROC_THREAD_ATTRIBUTE_LIST,
        job_handles: Vec<HANDLE>,
        inherited_handles: Vec<HANDLE>,
    }

    struct OwnedKernelHandle(Option<HANDLE>);

    struct PollingControlReader(File);

    impl Read for PollingControlReader {
        fn read(&mut self, buffer: &mut [u8]) -> std::io::Result<usize> {
            if buffer.is_empty() {
                return Ok(0);
            }
            loop {
                let mut available = 0_u32;
                if unsafe {
                    PeekNamedPipe(
                        HANDLE(self.0.as_raw_handle()),
                        None,
                        0,
                        None,
                        Some(&mut available),
                        None,
                    )
                }
                .is_err()
                {
                    let error = std::io::Error::last_os_error();
                    if error.raw_os_error() == Some(ERROR_NO_DATA.0 as i32) {
                        thread::sleep(Duration::from_millis(5));
                        continue;
                    }
                    if matches!(
                        error.raw_os_error(),
                        Some(code)
                            if code == ERROR_BROKEN_PIPE.0 as i32
                                || code == ERROR_PIPE_NOT_CONNECTED.0 as i32
                    ) {
                        return Ok(0);
                    }
                    return Err(error);
                }
                if available > 0 {
                    let readable = buffer.len().min(available as usize);
                    return self.0.read(&mut buffer[..readable]);
                }
                thread::sleep(Duration::from_millis(5));
            }
        }
    }

    impl OwnedKernelHandle {
        fn raw(&self) -> HANDLE {
            self.0.expect("owned Windows kernel handle is missing")
        }

        fn take(&mut self) -> HANDLE {
            self.0
                .take()
                .expect("owned Windows kernel handle is missing")
        }
    }

    impl Drop for OwnedKernelHandle {
        fn drop(&mut self) {
            if let Some(handle) = self.0.take() {
                let _ = unsafe { CloseHandle(handle) };
            }
        }
    }

    impl AttributeList {
        fn for_job_and_stdio(job: HANDLE, inherited_handles: Vec<HANDLE>) -> Result<Self> {
            // PROC_THREAD_ATTRIBUTE_JOB_LIST keeps the supplied handle-array
            // storage alive until CreateProcessW consumes the attribute list.
            // Do not point it at the stack-local `job` argument.
            let job_handles = vec![job];
            let mut bytes = 0_usize;
            let _ = unsafe { InitializeProcThreadAttributeList(None, 2, None, &mut bytes) };
            if bytes == 0 {
                bail!("Windows did not report a process attribute-list size");
            }
            let words = bytes.div_ceil(size_of::<usize>());
            let mut storage = vec![0_usize; words];
            let list = LPPROC_THREAD_ATTRIBUTE_LIST(storage.as_mut_ptr().cast());
            unsafe { InitializeProcThreadAttributeList(Some(list), 2, None, &mut bytes) }
                .context("initialize Windows process attribute list")?;
            if let Err(error) = unsafe {
                UpdateProcThreadAttribute(
                    list,
                    0,
                    PROC_THREAD_ATTRIBUTE_JOB_LIST as usize,
                    Some(job_handles.as_ptr().cast()),
                    job_handles.len() * size_of::<HANDLE>(),
                    None,
                    None,
                )
            } {
                unsafe { DeleteProcThreadAttributeList(list) };
                return Err(error).context("attach Windows Job list process attribute");
            }
            if let Err(error) = unsafe {
                UpdateProcThreadAttribute(
                    list,
                    0,
                    PROC_THREAD_ATTRIBUTE_HANDLE_LIST as usize,
                    Some(inherited_handles.as_ptr().cast()),
                    inherited_handles.len() * size_of::<HANDLE>(),
                    None,
                    None,
                )
            } {
                unsafe { DeleteProcThreadAttributeList(list) };
                return Err(error).context("restrict inherited Windows sidecar handles");
            }
            Ok(Self {
                storage,
                list,
                job_handles,
                inherited_handles,
            })
        }
    }

    impl Drop for AttributeList {
        fn drop(&mut self) {
            unsafe { DeleteProcThreadAttributeList(self.list) };
            self.storage.clear();
            self.job_handles.clear();
            self.inherited_handles.clear();
        }
    }

    fn wide(value: &OsStr) -> Vec<u16> {
        value.encode_wide().chain(std::iter::once(0)).collect()
    }

    fn child_process_path(path: &Path) -> PathBuf {
        use std::os::windows::ffi::OsStringExt;

        let units: Vec<u16> = path.as_os_str().encode_wide().collect();
        let verbatim = [b'\\' as u16, b'\\' as u16, b'?' as u16, b'\\' as u16];
        if !units.starts_with(&verbatim) {
            return path.to_path_buf();
        }
        let unc = [b'U' as u16, b'N' as u16, b'C' as u16, b'\\' as u16];
        if units.get(4..8) == Some(unc.as_slice()) {
            let mut simplified = vec![b'\\' as u16, b'\\' as u16];
            simplified.extend_from_slice(&units[8..]);
            return PathBuf::from(OsString::from_wide(&simplified));
        }
        if units.get(5) == Some(&(b':' as u16)) && units.get(6) == Some(&(b'\\' as u16)) {
            return PathBuf::from(OsString::from_wide(&units[4..]));
        }
        path.to_path_buf()
    }

    fn append_quoted(command_line: &mut Vec<u16>, argument: &OsStr) {
        let units: Vec<u16> = argument.encode_wide().collect();
        let needs_quotes = units.is_empty()
            || units
                .iter()
                .any(|unit| *unit == b' ' as u16 || *unit == b'\t' as u16 || *unit == b'"' as u16);
        if !needs_quotes {
            command_line.extend(units);
            return;
        }
        command_line.push(b'"' as u16);
        let mut slashes = 0_usize;
        for unit in units {
            if unit == b'\\' as u16 {
                slashes += 1;
            } else if unit == b'"' as u16 {
                command_line.extend(std::iter::repeat(b'\\' as u16).take(slashes * 2 + 1));
                command_line.push(unit);
                slashes = 0;
            } else {
                command_line.extend(std::iter::repeat(b'\\' as u16).take(slashes));
                command_line.push(unit);
                slashes = 0;
            }
        }
        command_line.extend(std::iter::repeat(b'\\' as u16).take(slashes * 2));
        command_line.push(b'"' as u16);
    }

    fn command_line(target: &Path, arguments: &[OsString]) -> Vec<u16> {
        let mut result = Vec::new();
        append_quoted(&mut result, target.as_os_str());
        for argument in arguments {
            result.push(b' ' as u16);
            append_quoted(&mut result, argument);
        }
        result.push(0);
        result
    }

    fn push_argument(arguments: &mut Vec<OsString>, name: &str, value: impl Into<OsString>) {
        arguments.push(OsString::from(name));
        arguments.push(value.into());
    }

    fn create_job() -> Result<OwnedKernelHandle> {
        let job = unsafe { CreateJobObjectW(None, PCWSTR::null()) }
            .context("create Desktop Windows Job Object")?;
        let mut limits = JOBOBJECT_EXTENDED_LIMIT_INFORMATION::default();
        limits.BasicLimitInformation.LimitFlags =
            JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE | JOB_OBJECT_LIMIT_BREAKAWAY_OK;
        if let Err(error) = unsafe {
            SetInformationJobObject(
                job,
                JobObjectExtendedLimitInformation,
                (&limits as *const JOBOBJECT_EXTENDED_LIMIT_INFORMATION).cast(),
                size_of::<JOBOBJECT_EXTENDED_LIMIT_INFORMATION>() as u32,
            )
        } {
            let _ = unsafe { CloseHandle(job) };
            return Err(error).context("configure Desktop Windows Job Object");
        }
        Ok(OwnedKernelHandle(Some(job)))
    }

    fn create_control_pipe(name: &str) -> Result<OwnedKernelHandle> {
        let name = wide(OsStr::new(name));
        let handle = unsafe {
            CreateNamedPipeW(
                PCWSTR(name.as_ptr()),
                PIPE_ACCESS_DUPLEX,
                PIPE_TYPE_BYTE | PIPE_READMODE_BYTE | PIPE_WAIT | PIPE_REJECT_REMOTE_CLIENTS,
                1,
                1024 * 1024,
                1024 * 1024,
                STARTUP_TIMEOUT.as_millis() as u32,
                None,
            )
        };
        if handle == INVALID_HANDLE_VALUE {
            return Err(std::io::Error::last_os_error()).context("create Desktop control pipe");
        }
        Ok(OwnedKernelHandle(Some(handle)))
    }

    fn rotate_capture(path: &Path) -> Result<()> {
        if path.metadata().map_or(0, |metadata| metadata.len()) < HOST_CAPTURE_MAX_BYTES {
            return Ok(());
        }
        for index in (1..=HOST_CAPTURE_BACKUPS).rev() {
            let source = if index == 1 {
                path.to_path_buf()
            } else {
                PathBuf::from(format!("{}.{}", path.display(), index - 1))
            };
            let destination = PathBuf::from(format!("{}.{}", path.display(), index));
            if source.exists() {
                let _ = std::fs::remove_file(&destination);
                std::fs::rename(source, destination)?;
            }
        }
        Ok(())
    }

    fn inheritable_file(file: &File) -> Result<HANDLE> {
        let handle = HANDLE(file.as_raw_handle());
        unsafe { SetHandleInformation(handle, HANDLE_FLAG_INHERIT.0, HANDLE_FLAG_INHERIT) }
            .context("make Windows sidecar capture handle inheritable")?;
        Ok(handle)
    }

    fn control_pipe_client_is_expected(
        client_pid: u32,
        expected_pid: u32,
        job: HANDLE,
    ) -> Result<bool> {
        if client_pid == expected_pid {
            return Ok(true);
        }
        // A PyInstaller one-file executable connects from its extracted child
        // process rather than the bootstrap PID returned by CreateProcessW.
        // The child is still atomically contained by this launch's private Job.
        let client = unsafe { OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, false, client_pid) }
            .context("open Desktop control-pipe client process")?;
        let client = OwnedKernelHandle(Some(client));
        let mut is_in_job = BOOL(0);
        unsafe { IsProcessInJob(client.raw(), Some(job), &mut is_in_job) }
            .context("verify Desktop control-pipe client Job identity")?;
        Ok(is_in_job.as_bool())
    }

    fn connect_control_pipe(
        pipe: HANDLE,
        pipe_name: &str,
        expected_pid: u32,
        job: HANDLE,
    ) -> Result<File> {
        let wake_name = pipe_name.to_string();
        thread::spawn(move || {
            thread::sleep(STARTUP_TIMEOUT);
            let _ = OpenOptions::new().read(true).write(true).open(wake_name);
        });
        if unsafe { ConnectNamedPipe(pipe, None) }.is_err()
            && unsafe { GetLastError() } != ERROR_PIPE_CONNECTED
        {
            let error = std::io::Error::last_os_error();
            let _ = unsafe { CloseHandle(pipe) };
            return Err(error).context("connect Desktop control pipe");
        }
        let mut client_pid = 0_u32;
        if let Err(error) = unsafe { GetNamedPipeClientProcessId(pipe, &mut client_pid) } {
            let _ = unsafe { CloseHandle(pipe) };
            return Err(error).context("identify Desktop control-pipe client");
        }
        if client_pid == std::process::id() {
            let _ = unsafe { CloseHandle(pipe) };
            bail!("Desktop sidecar control-pipe connection timed out");
        }
        if !control_pipe_client_is_expected(client_pid, expected_pid, job)? {
            let _ = unsafe { CloseHandle(pipe) };
            bail!(
                "Desktop control-pipe client identity mismatch (expected process {expected_pid} or its Job, got {client_pid})"
            );
        }
        let mode = PIPE_READMODE_BYTE | PIPE_NOWAIT;
        if let Err(error) = unsafe { SetNamedPipeHandleState(pipe, Some(&mode), None, None) } {
            let _ = unsafe { CloseHandle(pipe) };
            return Err(error).context("make Desktop control pipe nonblocking");
        }
        let file = unsafe { File::from_raw_handle(pipe.0 as RawHandle) };
        Ok(file)
    }

    pub(super) fn launch_once(
        app: &AppHandle,
        project: &Path,
        requested_port: u16,
        generation: u64,
        gui_environment_path: &OsStr,
    ) -> Result<(SidecarHandle, Value)> {
        let state = app.state::<AppState>();
        let pipe_name = format!(
            r"\\.\pipe\iac-code-desktop-{}-{}-{}",
            std::process::id(),
            generation,
            uuid::Uuid::new_v4()
        );
        let mut pipe = create_control_pipe(&pipe_name)?;
        let mut job = create_job()?;
        let (target, mut arguments) = sidecar_command(app)?;
        // Tauri canonicalizes packaged resources with a `\\?\` prefix. Passing
        // that spelling to PyInstaller makes sys._MEIPASS verbatim too; Windows
        // then refuses to resolve the `..` components used by PyCryptodome's
        // native-module loader. CreateProcessW accepts the equivalent regular
        // drive/UNC spelling and the installed target is comfortably bounded by
        // the NSIS per-user installation path.
        let target = child_process_path(&target);
        let capture_path = state
            .paths
            .log_dir
            .join(format!("host-sidecar-{generation}.log"));
        rotate_capture(&capture_path)?;
        let capture = OpenOptions::new()
            .create(true)
            .append(true)
            .open(&capture_path)
            .context("open Windows sidecar Host capture")?;
        let null_input = OpenOptions::new()
            .read(true)
            .open("NUL")
            .context("open Windows null input")?;
        let capture_handle = inheritable_file(&capture)?;
        let input_handle = inheritable_file(&null_input)?;
        let attributes =
            AttributeList::for_job_and_stdio(job.raw(), vec![capture_handle, input_handle])?;
        push_argument(
            &mut arguments,
            "--requested-port",
            requested_port.to_string(),
        );
        push_argument(&mut arguments, "--desktop-install-id", install_id(app));
        push_argument(
            &mut arguments,
            "--host-state-dir",
            state.paths.host_state_dir.as_os_str(),
        );
        push_argument(
            &mut arguments,
            "--desktop-install-lock-dir",
            state.paths.install_lock_dir.as_os_str(),
        );
        push_argument(
            &mut arguments,
            "--runtime-dir",
            state.paths.runtime_dir.as_os_str(),
        );
        push_argument(&mut arguments, "--default-project-cwd", project.as_os_str());
        push_argument(
            &mut arguments,
            "--distribution-channel",
            distribution_channel(),
        );
        push_argument(&mut arguments, "--update-mode", update_mode());
        push_argument(
            &mut arguments,
            "--sidecar-generation",
            generation.to_string(),
        );
        push_argument(&mut arguments, "--control-pipe", pipe_name.clone());
        push_argument(
            &mut arguments,
            "--host-capture-path",
            capture_path.as_os_str(),
        );
        push_argument(&mut arguments, "--config-dir", config_dir(app)?.as_os_str());
        push_argument(&mut arguments, "--log-dir", state.paths.log_dir.as_os_str());
        push_argument(&mut arguments, "--gui-path", gui_environment_path);
        if !cfg!(debug_assertions) {
            push_argument(
                &mut arguments,
                "--tiktoken-cache-dir",
                app.path()
                    .resource_dir()?
                    .join("sidecar/iac-code-sidecar/_internal/iac_code/tokenizer_cache")
                    .into_os_string(),
            );
        }

        let application = wide(target.as_os_str());
        let mut command_line = command_line(&target, &arguments);
        let current_dir = wide(state.paths.runtime_dir.as_os_str());
        let mut startup = STARTUPINFOEXW::default();
        startup.StartupInfo.cb = size_of::<STARTUPINFOEXW>() as u32;
        startup.lpAttributeList = attributes.list;
        startup.StartupInfo.dwFlags = STARTF_USESTDHANDLES;
        startup.StartupInfo.hStdInput = input_handle;
        startup.StartupInfo.hStdOutput = capture_handle;
        startup.StartupInfo.hStdError = capture_handle;
        let mut process: PROCESS_INFORMATION = unsafe { zeroed() };
        let create_result = unsafe {
            CreateProcessW(
                PCWSTR(application.as_ptr()),
                Some(PWSTR(command_line.as_mut_ptr())),
                None,
                None,
                true,
                CREATE_NO_WINDOW | EXTENDED_STARTUPINFO_PRESENT,
                None,
                PCWSTR(current_dir.as_ptr()),
                &startup.StartupInfo as *const _,
                &mut process,
            )
        };
        drop(attributes);
        if let Err(error) = create_result {
            return Err(error).context("create Desktop sidecar atomically in Windows Job");
        }
        let _ = unsafe { CloseHandle(process.hThread) };
        let job_handle = job.raw();
        let mut supervisor = Supervisor {
            process: Some(process.hProcess),
            job: Some(job.take()),
        };
        let host_control =
            match connect_control_pipe(pipe.take(), &pipe_name, process.dwProcessId, job_handle) {
                Ok(control) => control,
                Err(error) => {
                    terminate_supervisor(&mut supervisor);
                    return Err(error);
                }
            };
        // A pending synchronous ReadFile on a duplicated named-pipe handle can
        // serialize a concurrent Host write. Polling before each bounded read
        // keeps request/response traffic full-duplex without an overlapped-I/O
        // runtime or a second control pipe.
        let reader = PollingControlReader(host_control.try_clone()?);
        let writer = Arc::new(Mutex::new(host_control));
        let exited = Arc::new(AtomicBool::new(false));
        let (message_sender, message_receiver) = mpsc::channel();
        start_control_dispatcher(
            reader,
            writer.clone(),
            generation,
            message_sender,
            exited.clone(),
        );
        let first = wait_for_startup_message(&message_receiver, generation)?;
        if first.get("type").and_then(Value::as_str) != Some("ready") {
            let code = first
                .get("code")
                .and_then(Value::as_str)
                .unwrap_or("protocol_error")
                .to_string();
            let message = first
                .get("message")
                .and_then(Value::as_str)
                .unwrap_or("unexpected first control message")
                .to_string();
            terminate_supervisor(&mut supervisor);
            return Err(StartupFailure { code, message }.into());
        }
        if first.get("sidecarGeneration").and_then(Value::as_u64) != Some(generation)
            || first.get("pid").and_then(Value::as_u64) != Some(process.dwProcessId as u64)
            || first.get("protocolVersion").and_then(Value::as_u64)
                != Some(DESKTOP_PROTOCOL_VERSION)
        {
            terminate_supervisor(&mut supervisor);
            bail!("Desktop sidecar readiness identity mismatch");
        }
        let port = first
            .get("port")
            .and_then(Value::as_u64)
            .and_then(|value| u16::try_from(value).ok())
            .context("Desktop sidecar readiness port is invalid")?;
        if requested_port != 0 && port != requested_port {
            terminate_supervisor(&mut supervisor);
            bail!("Desktop sidecar readiness port does not match the requested port");
        }
        Ok((
            SidecarHandle {
                generation,
                port,
                control_writer: writer,
                control_messages: Mutex::new(message_receiver),
                pending_messages: Mutex::new(VecDeque::new()),
                request_lock: Mutex::new(()),
                supervisor: Mutex::new(supervisor),
                exited,
            },
            first,
        ))
    }

    #[cfg(test)]
    mod tests {
        use super::*;

        #[test]
        fn child_process_path_simplifies_verbatim_drive_and_unc_paths() {
            assert_eq!(
                child_process_path(Path::new(r"\\?\C:\Users\tester\iac-code.exe")),
                PathBuf::from(r"C:\Users\tester\iac-code.exe")
            );
            assert_eq!(
                child_process_path(Path::new(r"\\?\UNC\server\share\iac-code.exe")),
                PathBuf::from(r"\\server\share\iac-code.exe")
            );
            assert_eq!(
                child_process_path(Path::new(r"C:\iac-code.exe")),
                PathBuf::from(r"C:\iac-code.exe")
            );
        }
    }
}

#[cfg(windows)]
fn launch_once(
    app: &AppHandle,
    project: &Path,
    requested_port: u16,
    generation: u64,
    gui_environment_path: &OsStr,
) -> Result<(SidecarHandle, Value)> {
    windows_launcher::launch_once(
        app,
        project,
        requested_port,
        generation,
        gui_environment_path,
    )
}

fn show_bundled_page(app: &AppHandle, mode: &str, error: Option<&str>) {
    let Some(window) = app.get_webview_window("main") else {
        return;
    };
    let mut query = url::form_urlencoded::Serializer::new(String::new());
    query.append_pair("mode", mode);
    let bootstrap_operation_id = {
        let state = app.state::<AppState>();
        let operation_id = state.lifecycle.lock().begin_bootstrap();
        operation_id
    };
    let bootstrap_operation_id = bootstrap_operation_id.to_string();
    query.append_pair("bootstrapOperationId", &bootstrap_operation_id);
    query.append_pair("theme", &crate::configured_theme(app));
    if let Some(error) = error {
        query.append_pair("error", error);
    }
    #[cfg(windows)]
    let bundled_base = "http://tauri.localhost/index.html";
    #[cfg(not(windows))]
    let bundled_base = "tauri://localhost/index.html";
    let target = format!("{bundled_base}?{}", query.finish());
    if let Ok(url) = target.parse() {
        let _ = window.navigate(url);
    }
}

pub fn show_recovery_page(app: &AppHandle, error: &str) {
    show_bundled_page(app, "recovery", Some(error));
}

pub fn show_localized_recovery_page(app: &AppHandle, key: &str) {
    let language = app.state::<AppState>().language.clone();
    show_recovery_page(app, crate::desktop_text(&language, key));
}

fn exit_requires_recovery(
    lifecycle: LifecycleState,
    current_generation: Option<u64>,
    exited_generation: u64,
) -> bool {
    matches!(
        lifecycle,
        LifecycleState::Running | LifecycleState::Quiescing
    ) && current_generation == Some(exited_generation)
}

fn can_complete_resume(
    lifecycle: LifecycleState,
    current_generation: Option<u64>,
    expected_generation: u64,
    exited: bool,
) -> bool {
    lifecycle == LifecycleState::Quiescing
        && current_generation == Some(expected_generation)
        && !exited
}

#[cfg(feature = "updater")]
pub fn navigate_to_port(app: &AppHandle, port: u16) -> Result<()> {
    let window = app
        .get_webview_window("main")
        .context("Desktop main window is unavailable")?;
    window
        .navigate(format!("http://127.0.0.1:{port}/").parse()?)
        .context("navigate to healthy Desktop sidecar")
}

fn monitor_exit(app: AppHandle, generation: u64, exited: Arc<AtomicBool>) {
    thread::spawn(move || {
        while !exited.load(Ordering::Acquire) {
            thread::sleep(Duration::from_millis(50));
        }
        let state = app.state::<AppState>();
        let handle_to_reap = {
            let mut lifecycle = state.lifecycle.lock();
            let mut current = state.sidecar.lock();
            let current_generation = current.as_ref().map(|handle| handle.generation);
            if exit_requires_recovery(lifecycle.state, current_generation, generation) {
                #[cfg(feature = "updater")]
                crate::updater::invalidate_for_lifecycle(&app);
                let handle = current.take();
                lifecycle.healthy_origin = None;
                lifecycle.begin(LifecycleState::Recovering);
                handle
            } else {
                None
            }
        };
        if let Some(mut handle) = handle_to_reap {
            handle.terminate_container();
            show_localized_recovery_page(&app, "runtime_stopped");
        }
    });
}

pub fn start(app: &AppHandle, project: &Path) -> Result<u16> {
    start_with_options(app, project, false)
}

pub fn start_with_options(
    app: &AppHandle,
    project: &Path,
    replace_occupied_port: bool,
) -> Result<u16> {
    let state = app.state::<AppState>();
    {
        let mut lifecycle = state.lifecycle.lock();
        if !matches!(
            lifecycle.state,
            LifecycleState::Stopped
                | LifecycleState::Restarting
                | LifecycleState::Updating
                | LifecycleState::Recovering
        ) {
            bail!("Desktop sidecar lifecycle is busy");
        }
        #[cfg(feature = "updater")]
        crate::updater::invalidate_for_lifecycle(app);
        lifecycle.begin(LifecycleState::Starting);
    }
    start_claimed(app, project, replace_occupied_port)
}

pub fn start_from_bootstrap(
    app: &AppHandle,
    project: &Path,
    bootstrap_operation_id: uuid::Uuid,
) -> Result<u16> {
    let state = app.state::<AppState>();
    {
        let mut lifecycle = state.lifecycle.lock();
        lifecycle
            .finish_local_picker(bootstrap_operation_id)
            .map_err(anyhow::Error::msg)?;
        if !matches!(
            lifecycle.state,
            LifecycleState::Stopped | LifecycleState::Recovering
        ) {
            bail!("Desktop sidecar lifecycle is busy");
        }
        #[cfg(feature = "updater")]
        crate::updater::invalidate_for_lifecycle(app);
        lifecycle.begin(LifecycleState::Starting);
    }
    start_claimed(app, project, false)
}

fn start_claimed(app: &AppHandle, project: &Path, replace_occupied_port: bool) -> Result<u16> {
    let state = app.state::<AppState>();
    let mut transaction = StartTransaction::new(&state.lifecycle);
    let sidecar_target = sidecar_command(app)?.0;
    let gui_environment_path = gui_path(app, &sidecar_target)?;
    let install_id = install_id(app);
    let candidates = {
        let host_state = state.host_state.lock();
        match (
            host_state.state().preferred_loopback_port,
            host_state.state().preferred_loopback_port_source,
        ) {
            (Some(port), Some(PortSource::Deterministic)) if !replace_occupied_port => vec![port],
            (Some(port), Some(PortSource::Deterministic)) => {
                let mut candidates = deterministic_port_candidates(&install_id);
                candidates.retain(|candidate| *candidate != port);
                candidates
            }
            _ => deterministic_port_candidates(&install_id),
        }
    };
    let mut last_error = None;
    let allow_os_fallback = replace_occupied_port
        || state
            .host_state
            .lock()
            .state()
            .preferred_loopback_port_source
            != Some(PortSource::Deterministic);
    for requested_port in candidates.into_iter().chain(allow_os_fallback.then_some(0)) {
        let generation = state.host_state.lock().claim_sidecar_generation()?;
        match launch_once(
            app,
            project,
            requested_port,
            generation,
            &gui_environment_path,
        ) {
            Ok((handle, _ready)) => {
                if let Err(error) = check_health(handle.port) {
                    handle.abort();
                    return Err(error).context("Desktop sidecar failed its health check");
                }
                let source = if requested_port == 0 {
                    PortSource::OsFallback
                } else {
                    PortSource::Deterministic
                };
                if let Err(error) = state
                    .host_state
                    .lock()
                    .save_preferred_port(handle.port, source)
                {
                    handle.abort();
                    return Err(error).context("persist the healthy Desktop loopback port");
                }
                let port = handle.port;
                let healthy_origin = match format!("http://127.0.0.1:{port}/").parse() {
                    Ok(origin) => origin,
                    Err(error) => {
                        handle.abort();
                        return Err(error).context("build the healthy Desktop loopback origin");
                    }
                };
                let exited = handle.exited.clone();
                *state.sidecar.lock() = Some(handle);
                let mut lifecycle = state.lifecycle.lock();
                lifecycle.state = LifecycleState::Running;
                lifecycle.healthy_origin = Some(healthy_origin);
                drop(lifecycle);
                monitor_exit(app.clone(), generation, exited);
                transaction.commit();
                return Ok(port);
            }
            Err(error) => {
                let retryable_port_failure =
                    error
                        .downcast_ref::<StartupFailure>()
                        .is_some_and(|failure| {
                            matches!(failure.code.as_str(), "port_in_use" | "port_draining")
                        });
                last_error = Some(error);
                if !retryable_port_failure {
                    break;
                }
            }
        }
    }
    Err(last_error.context("no Desktop loopback port candidate was usable")?)
}

fn active_work_count(close_state: &Value) -> u64 {
    close_state
        .get("activeWorkCount")
        .and_then(Value::as_u64)
        .unwrap_or(0)
}

pub fn prepare_close(app: &AppHandle, reason: &str) -> Result<u64> {
    let state = app.state::<AppState>();
    {
        let mut lifecycle = state.lifecycle.lock();
        if lifecycle.state != LifecycleState::Running {
            bail!("Desktop sidecar lifecycle is busy");
        }
        #[cfg(feature = "updater")]
        crate::updater::invalidate_for_lifecycle(app);
        lifecycle.begin(LifecycleState::Quiescing);
    }
    let close_result = {
        let sidecar = state.sidecar.lock();
        match sidecar.as_ref() {
            Some(handle) => handle.request(
                json!({"type": "prepare-close", "reason": reason}),
                "close-state",
            ),
            None => Err(anyhow::anyhow!("Desktop sidecar is not running")),
        }
    };
    let close_state = match close_result {
        Ok(close_state) => close_state,
        Err(error) => {
            for _ in 0..10 {
                let exited = state
                    .sidecar
                    .lock()
                    .as_ref()
                    .map_or(true, |handle| handle.exited.load(Ordering::Acquire));
                if exited {
                    break;
                }
                thread::sleep(Duration::from_millis(20));
            }
            let exited = state
                .sidecar
                .lock()
                .as_ref()
                .map_or(true, |handle| handle.exited.load(Ordering::Acquire));
            if exited {
                let handle = state.sidecar.lock().take();
                let mut lifecycle = state.lifecycle.lock();
                lifecycle.healthy_origin = None;
                lifecycle.begin(LifecycleState::Recovering);
                drop(lifecycle);
                if let Some(mut handle) = handle {
                    handle.terminate_container();
                }
                show_localized_recovery_page(app, "runtime_stopped");
            } else {
                state.lifecycle.lock().state = LifecycleState::Running;
            }
            return Err(error);
        }
    };
    Ok(active_work_count(&close_state))
}

pub fn close_status(app: &AppHandle) -> Result<u64> {
    let state = app.state::<AppState>();
    if state.lifecycle.lock().state != LifecycleState::Quiescing {
        bail!("Desktop sidecar is not waiting to close");
    }
    let sidecar = state.sidecar.lock();
    let handle = sidecar.as_ref().context("Desktop sidecar is not running")?;
    Ok(active_work_count(
        &handle.request(json!({"type": "close-status"}), "close-state")?,
    ))
}

pub fn resume_close(app: &AppHandle) -> Result<()> {
    let state = app.state::<AppState>();
    if state.lifecycle.lock().state != LifecycleState::Quiescing {
        bail!("Desktop sidecar is not waiting to close");
    }
    let generation = {
        let sidecar = state.sidecar.lock();
        let handle = sidecar.as_ref().context("Desktop sidecar is not running")?;
        let generation = handle.generation;
        handle.request(json!({"type": "resume"}), "resumed")?;
        generation
    };
    let handle_to_reap = {
        let mut lifecycle = state.lifecycle.lock();
        let mut sidecar = state.sidecar.lock();
        let current_generation = sidecar.as_ref().map(|handle| handle.generation);
        let exited = sidecar
            .as_ref()
            .map_or(true, |handle| handle.exited.load(Ordering::Acquire));
        if can_complete_resume(lifecycle.state, current_generation, generation, exited) {
            lifecycle.state = LifecycleState::Running;
            None
        } else if current_generation == Some(generation) && exited {
            let handle = sidecar.take();
            lifecycle.healthy_origin = None;
            lifecycle.begin(LifecycleState::Recovering);
            handle
        } else {
            bail!("Desktop sidecar changed while close was being resumed");
        }
    };
    if let Some(mut handle) = handle_to_reap {
        handle.terminate_container();
        show_localized_recovery_page(app, "runtime_stopped");
        bail!("Desktop sidecar stopped while close was being resumed");
    }
    Ok(())
}

pub fn commit_stop(app: &AppHandle, force: bool) -> Result<()> {
    let state = app.state::<AppState>();
    if state.lifecycle.lock().state != LifecycleState::Quiescing {
        bail!("Desktop sidecar is not ready to stop");
    }
    show_bundled_page(app, "stopping", None);
    let handle = state
        .sidecar
        .lock()
        .take()
        .context("Desktop sidecar is not running")?;
    state.lifecycle.lock().state = LifecycleState::Stopping;
    if let Err(error) = handle.request(json!({"type": "shutdown", "force": force}), "stopped") {
        let mut handle = handle;
        handle.terminate_container();
        state.lifecycle.lock().healthy_origin = None;
        state.lifecycle.lock().state = LifecycleState::Stopped;
        return Err(error);
    }
    let wait_result = handle.wait();
    state.lifecycle.lock().healthy_origin = None;
    state.lifecycle.lock().state = LifecycleState::Stopped;
    wait_result
}

pub fn stop(app: &AppHandle, force: bool) -> Result<()> {
    if force {
        return force_stop_container(app);
    }
    let active = prepare_close(app, "restart")?;
    if active > 0 {
        let _ = resume_close(app);
        bail!("Desktop sidecar still has active work");
    }
    commit_stop(app, false)
}

pub fn force_stop_container(app: &AppHandle) -> Result<()> {
    let state = app.state::<AppState>();
    show_bundled_page(app, "stopping", None);
    state.lifecycle.lock().begin(LifecycleState::Stopping);
    let Some(mut handle) = state.sidecar.lock().take() else {
        state.lifecycle.lock().healthy_origin = None;
        state.lifecycle.lock().state = LifecycleState::Stopped;
        return Ok(());
    };
    handle.terminate_container();
    let mut lifecycle = state.lifecycle.lock();
    lifecycle.healthy_origin = None;
    lifecycle.state = LifecycleState::Stopped;
    Ok(())
}

pub fn stop_with_dialog(app: &AppHandle, reason: &str) -> Result<()> {
    let language = app.state::<AppState>().language.clone();
    let active = match prepare_close(app, reason) {
        Ok(active) => active,
        Err(error) => {
            if app.state::<AppState>().sidecar.lock().is_none() {
                bail!(
                    "Desktop lifecycle cannot stop while another operation is in progress: {error}"
                );
            }
            let force = app
                .dialog()
                .message(format!(
                    "{}\n\n{error}",
                    crate::desktop_text(&language, "close_failed")
                ))
                .title(crate::desktop_text(&language, "quit_title"))
                .buttons(MessageDialogButtons::OkCancelCustom(
                    crate::desktop_text(&language, "force_quit").to_string(),
                    crate::desktop_text(&language, "return_app").to_string(),
                ))
                .blocking_show();
            if force {
                return stop(app, true);
            }
            bail!("Desktop close was cancelled");
        }
    };
    if active == 0 {
        return commit_stop(app, false);
    }
    let wait_label = crate::desktop_text(&language, "wait");
    let force_label = crate::desktop_text(&language, "force_quit");
    loop {
        let choice = app
            .dialog()
            .message(crate::desktop_text(&language, "active_work"))
            .title(crate::desktop_text(&language, "quit_title"))
            .buttons(MessageDialogButtons::YesNoCancelCustom(
                wait_label.to_string(),
                force_label.to_string(),
                crate::desktop_text(&language, "return_app").to_string(),
            ))
            .blocking_show_with_result();
        let wait = choice == MessageDialogResult::Yes
            || matches!(&choice, MessageDialogResult::Custom(label) if label == wait_label);
        let force = choice == MessageDialogResult::No
            || matches!(&choice, MessageDialogResult::Custom(label) if label == force_label);
        if force {
            return force_stop_container(app);
        }
        if !wait {
            resume_close(app)?;
            bail!("Desktop close was cancelled");
        }
        for _ in 0..32 {
            match close_status(app) {
                Ok(0) => return commit_stop(app, false),
                Ok(_) => thread::sleep(Duration::from_millis(250)),
                Err(error) => {
                    // The user already chose to close. Do not strand the Host in
                    // quiescing when only the control channel has failed.
                    return force_stop_container(app).context(error);
                }
            }
        }
    }
}

pub fn restart(app: &AppHandle) -> Result<u16> {
    let project = app
        .state::<AppState>()
        .host_state
        .lock()
        .state()
        .recent_project
        .clone()
        .context("no Desktop project is selected")?;
    stop_with_dialog(app, "restart")?;
    app.state::<AppState>()
        .lifecycle
        .lock()
        .begin(LifecycleState::Restarting);
    match start(app, &project) {
        Ok(port) => Ok(port),
        Err(error) => {
            show_localized_recovery_page(app, "runtime_restart_failed");
            Err(error)
        }
    }
}

pub fn current_generation(app: &AppHandle) -> Result<u64> {
    app.state::<AppState>()
        .sidecar
        .lock()
        .as_ref()
        .map(|handle| handle.generation)
        .context("Desktop sidecar is not running")
}

pub fn set_default_project(
    app: &AppHandle,
    project: &Path,
    picker_operation_id: uuid::Uuid,
    source_generation: u64,
) -> Result<()> {
    let state = app.state::<AppState>();
    let sidecar = state.sidecar.lock();
    let handle = sidecar.as_ref().context("Desktop sidecar is not running")?;
    if handle.generation != source_generation {
        bail!("Desktop project picker belongs to a stale sidecar generation");
    }
    let path = project.to_string_lossy().into_owned();
    let response = handle.request(
        json!({
            "type": "set-default-project",
            "path": path,
            "pickerOperationId": picker_operation_id,
            "sourceGeneration": source_generation,
        }),
        "default-project-set",
    )?;
    if response.get("pickerOperationId").and_then(Value::as_str)
        != Some(picker_operation_id.to_string().as_str())
        || response.get("sourceGeneration").and_then(Value::as_u64) != Some(source_generation)
        || response.get("path").and_then(Value::as_str) != Some(path.as_str())
    {
        bail!("Desktop project picker acknowledgement does not match the active operation");
    }
    if let Some(error) = response.get("error").and_then(Value::as_str) {
        bail!("Desktop sidecar rejected the selected project: {error}");
    }
    Ok(())
}

#[cfg(all(test, unix))]
mod tests {
    use super::*;
    use std::os::unix::fs::PermissionsExt;
    use std::os::unix::net::UnixStream;
    use std::os::unix::process::CommandExt;

    #[test]
    fn bounded_supervisor_cleanup_kills_and_reaps_owned_group() {
        let child = Command::new("/bin/sh")
            .args(["-c", "trap '' TERM; sleep 30 & wait"])
            .process_group(0)
            .spawn()
            .unwrap();
        let process_group = child.id() as libc::pid_t;
        let started = Instant::now();
        reap_supervisor(child, Duration::from_millis(10)).unwrap();
        assert!(started.elapsed() < Duration::from_secs(2));
        assert_eq!(unsafe { libc::kill(-process_group, 0) }, -1);
        assert_eq!(
            std::io::Error::last_os_error().raw_os_error(),
            Some(libc::ESRCH)
        );
    }

    #[test]
    fn posix_sidecar_arguments_have_one_value_for_every_required_flag() {
        let mut arguments = vec!["-m".to_string(), "iac_code.desktop".to_string()];
        append_posix_sidecar_arguments(
            &mut arguments,
            8766,
            "desktop-install".to_string(),
            Path::new("/host"),
            Path::new("/locks"),
            Path::new("/runtime"),
            Path::new("/project"),
            4,
            17,
            Path::new("/logs/capture.log"),
        );
        for flag in [
            "--requested-port",
            "--desktop-install-id",
            "--host-state-dir",
            "--desktop-install-lock-dir",
            "--runtime-dir",
            "--default-project-cwd",
            "--distribution-channel",
            "--update-mode",
            "--sidecar-generation",
            "--control-fd",
            "--host-capture-path",
        ] {
            let positions: Vec<_> = arguments
                .iter()
                .enumerate()
                .filter_map(|(index, argument)| (argument == flag).then_some(index))
                .collect();
            assert_eq!(positions.len(), 1, "{flag} must be emitted exactly once");
            let value = arguments.get(positions[0] + 1).unwrap();
            assert!(!value.starts_with("--"), "{flag} is missing its value");
        }
        let install_lock_flag = arguments
            .iter()
            .position(|argument| argument == "--desktop-install-lock-dir")
            .unwrap();
        assert_eq!(arguments[install_lock_flag + 1], "/locks");
    }

    #[test]
    fn failed_start_transaction_always_enters_recovery() {
        let lifecycle = Mutex::new(crate::lifecycle::LifecycleCoordinator {
            state: LifecycleState::Starting,
            healthy_origin: Some("http://127.0.0.1:8766/".parse().unwrap()),
            ..crate::lifecycle::LifecycleCoordinator::default()
        });
        let operation_before = lifecycle.lock().operation_id;
        drop(StartTransaction::new(&lifecycle));
        let lifecycle = lifecycle.lock();
        assert_eq!(lifecycle.state, LifecycleState::Recovering);
        assert!(lifecycle.healthy_origin.is_none());
        assert_ne!(lifecycle.operation_id, operation_before);
    }

    #[test]
    fn committed_start_transaction_preserves_running_state() {
        let lifecycle = Mutex::new(crate::lifecycle::LifecycleCoordinator {
            state: LifecycleState::Running,
            healthy_origin: Some("http://127.0.0.1:8766/".parse().unwrap()),
            ..crate::lifecycle::LifecycleCoordinator::default()
        });
        let mut transaction = StartTransaction::new(&lifecycle);
        transaction.commit();
        drop(transaction);
        assert_eq!(lifecycle.lock().state, LifecycleState::Running);
    }

    #[test]
    fn quiescing_exit_and_resume_ack_race_cannot_restore_dead_generation() {
        assert!(exit_requires_recovery(
            LifecycleState::Quiescing,
            Some(9),
            9
        ));
        assert!(!exit_requires_recovery(
            LifecycleState::Stopping,
            Some(9),
            9
        ));
        assert!(!can_complete_resume(
            LifecycleState::Quiescing,
            Some(9),
            9,
            true,
        ));
        assert!(!can_complete_resume(
            LifecycleState::Recovering,
            None,
            9,
            true,
        ));
        assert!(can_complete_resume(
            LifecycleState::Quiescing,
            Some(9),
            9,
            false,
        ));
    }

    #[test]
    fn config_override_expands_home_and_environment_like_python() {
        let home = Path::new("/tmp/iac-code-home");
        let variable = format!("IAC_CODE_TEST_CONFIG_{}", uuid::Uuid::new_v4().simple());
        std::env::set_var(&variable, "nested");
        let raw = OsString::from(format!("~/${{{variable}}}/../settings"));
        let expanded = expand_config_path(&raw, home, Path::new("/runtime")).unwrap();
        std::env::remove_var(variable);
        assert_eq!(expanded, home.join("settings"));
        assert!(expand_config_path(OsStr::new("  "), home, Path::new("/runtime")).is_none());
        assert_eq!(
            expand_config_path(OsStr::new("relative"), home, Path::new("/runtime")).unwrap(),
            PathBuf::from("/runtime/relative")
        );
    }

    #[test]
    fn explicit_desktop_tool_paths_are_absolute_and_precede_fallbacks() {
        let directory = tempfile::tempdir().unwrap();
        let explicit_bin = directory.path().join("explicit/bin");
        let explicit_search = directory.path().join("explicit/search");
        std::fs::create_dir_all(&explicit_bin).unwrap();
        std::fs::create_dir_all(&explicit_search).unwrap();
        let explicit_git = explicit_bin.join("git");
        let mismatched_git = explicit_bin.join("not-git");
        std::fs::write(&explicit_git, "").unwrap();
        std::fs::write(&mismatched_git, "").unwrap();
        std::fs::set_permissions(&explicit_git, std::fs::Permissions::from_mode(0o755)).unwrap();
        std::fs::set_permissions(&mismatched_git, std::fs::Permissions::from_mode(0o755)).unwrap();
        std::fs::write(
            directory.path().join("settings.yml"),
            format!(
                "desktop:\n  toolPaths:\n    git: {}\n    terraform: {}\n    unknown: {}\n  searchPaths:\n    - {}\n    - relative/bin\n",
                explicit_git.display(),
                mismatched_git.display(),
                explicit_git.display(),
                explicit_search.display(),
            ),
        )
        .unwrap();
        assert_eq!(
            explicit_tool_directories(directory.path()),
            vec![explicit_bin.clone(), explicit_search.clone()]
        );

        let inherited = std::env::join_paths([directory.path().join("inherited")]).unwrap();
        let entries = gui_path_entries(
            directory.path(),
            &directory.path().join("bundle/iac-code-sidecar"),
            Some(&inherited),
            vec![directory.path().join("login")],
            vec![directory.path().join("default")],
        );
        assert_eq!(
            entries,
            vec![
                explicit_bin,
                explicit_search,
                directory.path().join("inherited"),
                directory.path().join("login"),
                directory.path().join("bundle"),
                directory.path().join("default"),
            ]
        );
    }

    #[test]
    fn control_dispatcher_handles_child_registration_before_readiness() {
        let (sidecar, mut python) = UnixStream::pair().unwrap();
        let reader = sidecar.try_clone().unwrap();
        let writer = Arc::new(Mutex::new(sidecar));
        let exited = Arc::new(AtomicBool::new(false));
        let (sender, receiver) = mpsc::channel();
        start_control_dispatcher(reader, writer, 17, sender, exited.clone());

        write_message(
            &mut python,
            &json!({
                "type": "register-child-group",
                "sidecarGeneration": 17,
                "registrationId": 1,
                "pgid": 8123,
                "kind": "prerequisite",
            }),
        )
        .unwrap();
        let acknowledgement = read_message(&mut python).unwrap().unwrap();
        assert_eq!(acknowledgement["type"], "child-group-registered");
        assert_eq!(acknowledgement["registrationId"], 1);

        write_message(
            &mut python,
            &json!({
                "type": "ready",
                "sidecarGeneration": 17,
                "port": 8766,
            }),
        )
        .unwrap();
        assert_eq!(
            receiver.recv_timeout(Duration::from_secs(1)).unwrap()["type"],
            "ready"
        );
        drop(python);
        let deadline = Instant::now() + Duration::from_secs(1);
        while !exited.load(Ordering::Acquire) && Instant::now() < deadline {
            thread::sleep(Duration::from_millis(5));
        }
        assert!(exited.load(Ordering::Acquire));
    }

    #[test]
    fn startup_wait_accepts_bounded_recovery_notice_before_ready() {
        let (sender, receiver) = mpsc::channel();
        sender
            .send(json!({
                "type": "startup-recovery-begin",
                "sidecarGeneration": 23,
                "timeoutSeconds": 360.0,
            }))
            .unwrap();
        sender
            .send(json!({
                "type": "ready",
                "sidecarGeneration": 23,
                "port": 8766,
            }))
            .unwrap();
        assert_eq!(
            wait_for_startup_message(&receiver, 23).unwrap()["type"],
            "ready"
        );
    }

    #[test]
    fn startup_wait_rejects_stale_or_unbounded_recovery_notice() {
        for notice in [
            json!({
                "type": "startup-recovery-begin",
                "sidecarGeneration": 22,
                "timeoutSeconds": 360.0,
            }),
            json!({
                "type": "startup-recovery-begin",
                "sidecarGeneration": 23,
                "timeoutSeconds": 361.0,
            }),
        ] {
            let (sender, receiver) = mpsc::channel();
            sender.send(notice).unwrap();
            assert!(wait_for_startup_message(&receiver, 23).is_err());
        }
    }

    #[test]
    fn request_matching_keeps_stale_correlated_frames_for_their_owner() {
        let request = json!({
            "type": "set-default-project",
            "pickerOperationId": "current",
            "sourceGeneration": 4,
        });
        assert!(!response_matches_request(
            &request,
            &json!({
                "type": "default-project-set",
                "pickerOperationId": "stale",
                "sourceGeneration": 4,
            }),
            "default-project-set",
        ));
        assert!(response_matches_request(
            &request,
            &json!({
                "type": "default-project-set",
                "pickerOperationId": "current",
                "sourceGeneration": 4,
            }),
            "default-project-set",
        ));
    }
}
