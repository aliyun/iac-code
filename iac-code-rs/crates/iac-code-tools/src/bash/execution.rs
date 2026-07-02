use std::io::{self, Read};
use std::process::{Child, Command, ExitStatus, Stdio};
use std::thread;
use std::time::{Duration, Instant};

#[cfg(unix)]
use std::os::unix::process::{CommandExt, ExitStatusExt};

use crate::ToolResult;

#[cfg(unix)]
const SIGKILL: i32 = 9;

pub(super) struct ShellOutput {
    stdout: Vec<u8>,
    stderr: Vec<u8>,
    status: ExitStatus,
}

impl ShellOutput {
    pub(super) fn into_tool_result(self) -> ToolResult {
        let stdout = String::from_utf8_lossy(&self.stdout);
        let stderr = String::from_utf8_lossy(&self.stderr);
        let exit_code = shell_exit_code(&self.status);

        let mut parts = Vec::new();
        if !stdout.is_empty() {
            parts.push(format!("STDOUT:\n{stdout}"));
        }
        if !stderr.is_empty() {
            parts.push(format!("STDERR:\n{stderr}"));
        }
        parts.push(format!("Exit code: {exit_code}"));

        let output = parts.join("\n");
        if self.status.success() {
            ToolResult::success(output)
        } else {
            ToolResult::error(output)
        }
    }
}

pub(super) enum ShellExecution {
    Completed(ShellOutput),
    TimedOut,
}

pub(super) fn execute_shell_command(
    command: &str,
    timeout_seconds: u64,
    cwd: &str,
) -> Result<ShellExecution, String> {
    let mut command_builder = shell_command(command);
    command_builder
        .stdout(Stdio::piped())
        .stderr(Stdio::piped())
        .current_dir(cwd);

    #[cfg(unix)]
    {
        command_builder.process_group(0);
    }

    let mut child = command_builder.spawn().map_err(|error| error.to_string())?;
    let stdout = child
        .stdout
        .take()
        .ok_or_else(|| "failed to capture stdout".to_owned())?;
    let stderr = child
        .stderr
        .take()
        .ok_or_else(|| "failed to capture stderr".to_owned())?;
    let stdout_reader = spawn_reader(stdout);
    let stderr_reader = spawn_reader(stderr);

    let Some(status) = wait_for_child(&mut child, Duration::from_secs(timeout_seconds))
        .map_err(|error| error.to_string())?
    else {
        kill_child_tree(&mut child);
        let _ = child.wait();
        let _ = collect_reader_output(stdout_reader);
        let _ = collect_reader_output(stderr_reader);
        return Ok(ShellExecution::TimedOut);
    };

    Ok(ShellExecution::Completed(ShellOutput {
        stdout: collect_reader_output(stdout_reader)?,
        stderr: collect_reader_output(stderr_reader)?,
        status,
    }))
}

fn shell_command(command: &str) -> Command {
    #[cfg(windows)]
    {
        let mut shell = Command::new("bash");
        shell.arg("-lc").arg(command);
        shell
    }
    #[cfg(not(windows))]
    {
        let mut shell = Command::new("/bin/sh");
        shell.arg("-c").arg(command);
        shell
    }
}

fn spawn_reader<R>(mut reader: R) -> thread::JoinHandle<io::Result<Vec<u8>>>
where
    R: Read + Send + 'static,
{
    thread::spawn(move || {
        let mut output = Vec::new();
        reader.read_to_end(&mut output)?;
        Ok(output)
    })
}

fn collect_reader_output(
    reader: thread::JoinHandle<io::Result<Vec<u8>>>,
) -> Result<Vec<u8>, String> {
    reader
        .join()
        .map_err(|_| "reader thread panicked".to_owned())?
        .map_err(|error| error.to_string())
}

fn wait_for_child(child: &mut Child, timeout: Duration) -> io::Result<Option<ExitStatus>> {
    let started = Instant::now();
    loop {
        if let Some(status) = child.try_wait()? {
            return Ok(Some(status));
        }
        if started.elapsed() >= timeout {
            return Ok(None);
        }
        let remaining = timeout.saturating_sub(started.elapsed());
        thread::sleep(remaining.min(Duration::from_millis(10)));
    }
}

#[cfg(unix)]
fn kill_child_tree(child: &mut Child) {
    let process_group = -(child.id() as i32);
    unsafe {
        kill(process_group, SIGKILL);
    }
    let _ = child.kill();
}

#[cfg(not(unix))]
fn kill_child_tree(child: &mut Child) {
    let _ = child.kill();
}

fn shell_exit_code(status: &ExitStatus) -> i32 {
    if let Some(code) = status.code() {
        return code;
    }

    #[cfg(unix)]
    {
        status.signal().map_or(-1, |signal| -signal)
    }
    #[cfg(not(unix))]
    {
        -1
    }
}

#[cfg(unix)]
unsafe extern "C" {
    fn kill(pid: i32, sig: i32) -> i32;
}
