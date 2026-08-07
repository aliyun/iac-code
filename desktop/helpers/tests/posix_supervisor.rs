#![cfg(unix)]

use serde_json::Value;
use std::io::{BufRead, BufReader, Read, Write};
use std::os::fd::AsRawFd;
use std::os::unix::net::UnixStream;
use std::os::unix::process::CommandExt;
use std::process::{Command, Stdio};
use std::thread;
use std::time::{Duration, Instant};
use tempfile::tempdir;

fn set_inheritable(stream: &UnixStream) {
    let fd = stream.as_raw_fd();
    let flags = unsafe { libc::fcntl(fd, libc::F_GETFD) };
    assert!(flags >= 0);
    assert!(unsafe { libc::fcntl(fd, libc::F_SETFD, flags & !libc::FD_CLOEXEC) } >= 0);
}

fn spawn_protocol_guardian(
    control: &UnixStream,
    status: &UnixStream,
    command: &[&str],
    already_session_leader: bool,
) -> std::process::Child {
    let mut process = Command::new(env!("CARGO_BIN_EXE_iac-code-desktop-exec"));
    process
        .arg("--child-guardian")
        .arg("--control-fd")
        .arg(control.as_raw_fd().to_string())
        .arg("--status-fd")
        .arg(status.as_raw_fd().to_string())
        .arg("--")
        .args(command)
        .stdout(Stdio::piped())
        .stderr(Stdio::piped());
    if already_session_leader {
        unsafe {
            process.pre_exec(|| {
                if libc::setsid() < 0 {
                    return Err(std::io::Error::last_os_error());
                }
                Ok(())
            });
        }
    }
    process.spawn().unwrap()
}

#[test]
fn protocol_guardian_waits_for_start_and_reports_real_target_status() {
    let (mut control_writer, control_reader) = UnixStream::pair().unwrap();
    let (status_reader, status_writer) = UnixStream::pair().unwrap();
    set_inheritable(&control_reader);
    set_inheritable(&status_writer);
    let mut guardian = spawn_protocol_guardian(
        &control_reader,
        &status_writer,
        &["/bin/sh", "-c", "printf guardian-output; exit 7"],
        true,
    );
    drop(control_reader);
    drop(status_writer);
    status_reader
        .set_read_timeout(Some(Duration::from_millis(150)))
        .unwrap();
    let mut status = BufReader::new(status_reader);
    let mut line = String::new();
    assert!(
        status.read_line(&mut line).is_err(),
        "target started before START"
    );

    control_writer.write_all(b"START\n").unwrap();
    status
        .get_ref()
        .set_read_timeout(Some(Duration::from_secs(5)))
        .unwrap();
    status.read_line(&mut line).unwrap();
    assert!(line.starts_with("STARTED {\"pid\":"), "{line:?}");
    line.clear();
    status.read_line(&mut line).unwrap();
    assert_eq!(line, "EXIT {\"waitStatus\":1792}\n");

    let mut output = String::new();
    guardian
        .stdout
        .take()
        .unwrap()
        .read_to_string(&mut output)
        .unwrap();
    assert_eq!(output, "guardian-output");
    let drain_started = Instant::now();
    control_writer.write_all(b"DRAIN_FORCE\n").unwrap();
    assert!(guardian.wait().unwrap().code().is_none());
    assert!(
        drain_started.elapsed() < Duration::from_millis(500),
        "natural target completion waited for a grace period"
    );
}

#[test]
fn protocol_guardian_eof_before_start_never_creates_target() {
    let temporary = tempdir().unwrap();
    let marker = temporary.path().join("created");
    let (control_writer, control_reader) = UnixStream::pair().unwrap();
    let (status_reader, status_writer) = UnixStream::pair().unwrap();
    set_inheritable(&control_reader);
    set_inheritable(&status_writer);
    let mut guardian = spawn_protocol_guardian(
        &control_reader,
        &status_writer,
        &["/usr/bin/touch", marker.to_str().unwrap()],
        false,
    );
    drop(control_reader);
    drop(status_writer);
    drop(status_reader);
    drop(control_writer);
    let deadline = Instant::now() + Duration::from_secs(5);
    while guardian.try_wait().unwrap().is_none() && Instant::now() < deadline {
        thread::sleep(Duration::from_millis(25));
    }
    assert!(guardian.try_wait().unwrap().is_some());
    assert!(!marker.exists());
}

#[test]
fn protocol_guardian_control_eof_after_start_reaps_the_whole_group() {
    let (mut control_writer, control_reader) = UnixStream::pair().unwrap();
    let (status_reader, status_writer) = UnixStream::pair().unwrap();
    set_inheritable(&control_reader);
    set_inheritable(&status_writer);
    let mut guardian = spawn_protocol_guardian(
        &control_reader,
        &status_writer,
        &["/bin/sh", "-c", "sleep 30 & wait"],
        false,
    );
    drop(control_reader);
    drop(status_writer);
    control_writer.write_all(b"START\n").unwrap();
    let mut line = String::new();
    BufReader::new(status_reader).read_line(&mut line).unwrap();
    let started: Value =
        serde_json::from_str(line.strip_prefix("STARTED ").unwrap().trim()).unwrap();
    let target_pid = started["pid"].as_i64().unwrap() as libc::pid_t;
    drop(control_writer);

    let deadline = Instant::now() + Duration::from_secs(5);
    while guardian.try_wait().unwrap().is_none() && Instant::now() < deadline {
        thread::sleep(Duration::from_millis(25));
    }
    assert!(
        guardian.try_wait().unwrap().is_some(),
        "guardian did not finish bounded EOF cleanup"
    );
    assert_ne!(
        unsafe { libc::kill(target_pid, 0) },
        0,
        "target remained alive after guardian control EOF"
    );
}

#[test]
fn child_guardian_reaps_its_target_when_parent_identity_is_gone() {
    let temporary = tempdir().unwrap();
    let pid_file = temporary.path().join("target.pid");
    let script = format!("echo $$ > '{}'; exec sleep 30", pid_file.display());
    let mut guardian = Command::new(env!("CARGO_BIN_EXE_iac-code-desktop-exec"))
        .arg("--child-guardian")
        .arg("--parent-pid")
        .arg("99999999")
        .arg("--")
        .arg("/bin/sh")
        .arg("-c")
        .arg(script)
        .stdout(Stdio::null())
        .stderr(Stdio::null())
        .spawn()
        .unwrap();
    let deadline = Instant::now() + Duration::from_secs(5);
    while guardian.try_wait().unwrap().is_none() && Instant::now() < deadline {
        thread::sleep(Duration::from_millis(25));
    }
    assert!(guardian.try_wait().unwrap().is_some());
    if let Ok(raw_pid) = std::fs::read_to_string(pid_file) {
        let pid = raw_pid.trim().parse::<libc::pid_t>().unwrap();
        assert_ne!(
            unsafe { libc::kill(pid, 0) },
            0,
            "guarded target remained alive"
        );
    }
}

#[test]
fn supervisor_liveness_eof_terminates_the_target_group() {
    let (liveness_writer, liveness_reader) = UnixStream::pair().unwrap();
    let (status_reader, status_writer) = UnixStream::pair().unwrap();
    set_inheritable(&liveness_reader);
    set_inheritable(&status_writer);
    let mut supervisor = Command::new(env!("CARGO_BIN_EXE_iac-code-desktop-exec"))
        .arg("--sidecar-supervisor")
        .arg("--liveness-fd")
        .arg(liveness_reader.as_raw_fd().to_string())
        .arg("--status-fd")
        .arg(status_writer.as_raw_fd().to_string())
        .arg("--")
        .arg("/bin/sh")
        .arg("-c")
        .arg("sleep 30 & wait")
        .stdout(Stdio::null())
        .stderr(Stdio::null())
        .spawn()
        .unwrap();
    drop(liveness_reader);
    drop(status_writer);

    let mut line = String::new();
    BufReader::new(status_reader).read_line(&mut line).unwrap();
    let identity: Value =
        serde_json::from_str(line.strip_prefix("SIDECAR_STARTED ").unwrap().trim()).unwrap();
    let target_pid = identity["pid"].as_i64().unwrap() as libc::pid_t;
    drop(liveness_writer);

    let deadline = Instant::now() + Duration::from_secs(5);
    while supervisor.try_wait().unwrap().is_none() && Instant::now() < deadline {
        thread::sleep(Duration::from_millis(25));
    }
    assert!(
        supervisor.try_wait().unwrap().is_some(),
        "supervisor did not exit after liveness EOF"
    );
    let process_exists = unsafe { libc::kill(target_pid, 0) } == 0;
    assert!(
        !process_exists,
        "target remained after the supervisor liveness lease closed"
    );
}
