#[cfg(unix)]
mod unix {
    use std::env;
    use std::fs::File;
    use std::io::{Read, Write};
    use std::os::fd::{FromRawFd, RawFd};
    use std::os::unix::process::CommandExt;
    use std::os::unix::process::ExitStatusExt;
    use std::process::{Child, Command, ExitStatus};
    use std::sync::atomic::{AtomicBool, Ordering};
    use std::thread;
    use std::time::{Duration, Instant};

    const TERM_GRACE: Duration = Duration::from_secs(2);
    const DESCENDANT_DRAIN_GRACE: Duration = Duration::from_millis(100);
    static TERMINATE_REQUESTED: AtomicBool = AtomicBool::new(false);

    extern "C" fn request_termination(_signal: libc::c_int) {
        TERMINATE_REQUESTED.store(true, Ordering::Release);
    }

    struct Arguments {
        liveness_fd: RawFd,
        status_fd: RawFd,
        command: Vec<String>,
    }

    fn parse_fd(value: Option<String>, name: &str) -> Result<RawFd, String> {
        value
            .ok_or_else(|| format!("missing {name}"))?
            .parse::<RawFd>()
            .map_err(|_| format!("invalid {name}"))
    }

    fn parse_arguments() -> Result<Arguments, String> {
        let mut values = env::args().skip(1);
        if values.next().as_deref() != Some("--sidecar-supervisor") {
            return Err("expected --sidecar-supervisor".to_string());
        }
        if values.next().as_deref() != Some("--liveness-fd") {
            return Err("expected --liveness-fd".to_string());
        }
        let liveness_fd = parse_fd(values.next(), "liveness fd")?;
        if values.next().as_deref() != Some("--status-fd") {
            return Err("expected --status-fd".to_string());
        }
        let status_fd = parse_fd(values.next(), "status fd")?;
        if values.next().as_deref() != Some("--") {
            return Err("expected -- before the target command".to_string());
        }
        let command: Vec<String> = values.collect();
        if command.is_empty() {
            return Err("target command is missing".to_string());
        }
        Ok(Arguments {
            liveness_fd,
            status_fd,
            command,
        })
    }

    fn set_close_on_exec(fd: RawFd) -> Result<(), String> {
        let flags = unsafe { libc::fcntl(fd, libc::F_GETFD) };
        if flags < 0 || unsafe { libc::fcntl(fd, libc::F_SETFD, flags | libc::FD_CLOEXEC) } < 0 {
            return Err(std::io::Error::last_os_error().to_string());
        }
        Ok(())
    }

    fn set_nonblocking(fd: RawFd) -> Result<(), String> {
        let flags = unsafe { libc::fcntl(fd, libc::F_GETFL) };
        if flags < 0 || unsafe { libc::fcntl(fd, libc::F_SETFL, flags | libc::O_NONBLOCK) } < 0 {
            return Err(std::io::Error::last_os_error().to_string());
        }
        Ok(())
    }

    fn liveness_closed(file: &mut File) -> Result<bool, String> {
        let mut poll_fd = libc::pollfd {
            fd: std::os::fd::AsRawFd::as_raw_fd(file),
            events: libc::POLLIN | libc::POLLHUP | libc::POLLERR,
            revents: 0,
        };
        let result = unsafe { libc::poll(&mut poll_fd, 1, 50) };
        if result < 0 {
            let error = std::io::Error::last_os_error();
            if error.kind() == std::io::ErrorKind::Interrupted {
                return Ok(false);
            }
            return Err(error.to_string());
        }
        if result == 0 {
            return Ok(false);
        }
        let mut byte = [0_u8; 1];
        match file.read(&mut byte) {
            Ok(0) => Ok(true),
            Ok(_) => Ok(false),
            Err(error) if error.kind() == std::io::ErrorKind::WouldBlock => Ok(false),
            Err(error) => Err(error.to_string()),
        }
    }

    fn signal_group(group: libc::pid_t, signal: libc::c_int) {
        if group > 0 {
            unsafe {
                libc::kill(-group, signal);
            }
        }
    }

    fn terminate_group(child: &mut Child, group: libc::pid_t) -> ! {
        signal_group(group, libc::SIGTERM);
        let deadline = Instant::now() + TERM_GRACE;
        while Instant::now() < deadline {
            if child.try_wait().ok().flatten().is_some() {
                // The direct target may exit before descendants that inherited its
                // process group. Parent-death cleanup owns the whole group.
                signal_group(group, libc::SIGKILL);
                std::process::exit(0);
            }
            thread::sleep(Duration::from_millis(25));
        }
        signal_group(group, libc::SIGKILL);
        let _ = child.wait();
        std::process::exit(1);
    }

    fn cleanup_group_after_target_exit(group: libc::pid_t) {
        // If descendants still exist they retain the target PGID. Signal them
        // immediately; do not sleep and send a later signal after the PGID can
        // be recycled.
        signal_group(group, libc::SIGTERM);
    }

    fn exit_code(status: ExitStatus) -> i32 {
        status
            .code()
            .unwrap_or_else(|| 128 + status.signal().unwrap_or(1))
    }

    fn spawn_target(
        command: &[String],
        create_process_group: bool,
    ) -> Result<(Child, libc::pid_t), String> {
        let mut target = Command::new(&command[0]);
        target.args(&command[1..]);
        unsafe {
            target.pre_exec(move || {
                libc::signal(libc::SIGTERM, libc::SIG_DFL);
                libc::signal(libc::SIGINT, libc::SIG_DFL);
                if create_process_group && libc::setpgid(0, 0) < 0 {
                    return Err(std::io::Error::last_os_error());
                }
                Ok(())
            });
        }
        let child = target
            .spawn()
            .map_err(|error| format!("spawn target: {error}"))?;
        let target_group = if create_process_group {
            child.id() as libc::pid_t
        } else {
            unsafe { libc::getpgrp() }
        };
        Ok((child, target_group))
    }

    fn establish_guardian_session() -> Result<libc::pid_t, String> {
        if unsafe { libc::setsid() } < 0 {
            let error = std::io::Error::last_os_error();
            let already_session_leader = unsafe { libc::getpgrp() == libc::getpid() };
            if error.raw_os_error() != Some(libc::EPERM) || !already_session_leader {
                return Err(format!("setsid failed: {error}"));
            }
        }
        Ok(unsafe { libc::getpgrp() })
    }

    fn run_child_guardian(values: &[String]) -> Result<i32, String> {
        if values.len() < 5 || values[1] != "--parent-pid" || values[3] != "--" {
            return Err("invalid --child-guardian arguments".to_string());
        }
        let parent_pid = values[2]
            .parse::<libc::pid_t>()
            .map_err(|_| "invalid parent pid".to_string())?;
        let command = &values[4..];
        establish_guardian_session()?;
        TERMINATE_REQUESTED.store(false, Ordering::Release);
        unsafe {
            libc::signal(
                libc::SIGTERM,
                request_termination as *const () as libc::sighandler_t,
            );
            libc::signal(
                libc::SIGINT,
                request_termination as *const () as libc::sighandler_t,
            );
        }
        let (mut child, target_group) = spawn_target(command, true)?;
        loop {
            if let Some(exit) = child.try_wait().map_err(|error| error.to_string())? {
                cleanup_group_after_target_exit(target_group);
                return Ok(exit_code(exit));
            }
            if TERMINATE_REQUESTED.load(Ordering::Acquire)
                || unsafe { libc::getppid() } != parent_pid
            {
                terminate_group(&mut child, target_group);
            }
            thread::sleep(Duration::from_millis(25));
        }
    }

    struct GuardianArguments {
        control_fd: RawFd,
        status_fd: RawFd,
        command: Vec<String>,
    }

    fn parse_guardian_arguments(values: &[String]) -> Result<GuardianArguments, String> {
        if values.len() < 7
            || values[1] != "--control-fd"
            || values[3] != "--status-fd"
            || values[5] != "--"
        {
            return Err("invalid --child-guardian arguments".to_string());
        }
        let control_fd = values[2]
            .parse::<RawFd>()
            .map_err(|_| "invalid guardian control fd".to_string())?;
        let status_fd = values[4]
            .parse::<RawFd>()
            .map_err(|_| "invalid guardian status fd".to_string())?;
        let command = values[6..].to_vec();
        if command.is_empty() {
            return Err("guardian target command is missing".to_string());
        }
        Ok(GuardianArguments {
            control_fd,
            status_fd,
            command,
        })
    }

    #[derive(Clone, Copy, Debug, Eq, PartialEq)]
    enum GuardianCommand {
        Start,
        DrainGrace,
        DrainForce,
    }

    fn read_guardian_command(
        control: &mut File,
        pending: &mut Vec<u8>,
    ) -> Result<Option<Option<GuardianCommand>>, String> {
        let mut poll_fd = libc::pollfd {
            fd: std::os::fd::AsRawFd::as_raw_fd(control),
            events: libc::POLLIN | libc::POLLHUP | libc::POLLERR,
            revents: 0,
        };
        let result = unsafe { libc::poll(&mut poll_fd, 1, 25) };
        if result < 0 {
            let error = std::io::Error::last_os_error();
            if error.kind() == std::io::ErrorKind::Interrupted {
                return Ok(None);
            }
            return Err(error.to_string());
        }
        if result == 0 {
            return Ok(None);
        }
        let mut chunk = [0_u8; 128];
        match control.read(&mut chunk) {
            Ok(0) => return Ok(Some(None)),
            Ok(size) => pending.extend_from_slice(&chunk[..size]),
            Err(error) if error.kind() == std::io::ErrorKind::WouldBlock => return Ok(None),
            Err(error) => return Err(error.to_string()),
        }
        let Some(newline) = pending.iter().position(|byte| *byte == b'\n') else {
            if pending.len() > 128 {
                return Err("guardian control command is too long".to_string());
            }
            return Ok(None);
        };
        let line = pending.drain(..=newline).collect::<Vec<_>>();
        let line = std::str::from_utf8(&line[..line.len() - 1])
            .map_err(|_| "guardian control command is not UTF-8".to_string())?;
        let command = match line {
            "START" => GuardianCommand::Start,
            "DRAIN_GRACE" => GuardianCommand::DrainGrace,
            "DRAIN_FORCE" => GuardianCommand::DrainForce,
            _ => return Err("unknown guardian control command".to_string()),
        };
        Ok(Some(Some(command)))
    }

    fn close_standard_streams() {
        unsafe {
            libc::close(libc::STDIN_FILENO);
            libc::close(libc::STDOUT_FILENO);
            libc::close(libc::STDERR_FILENO);
        }
    }

    fn report_exit(status: &mut File, exit: ExitStatus) {
        let _ = writeln!(status, "EXIT {{\"waitStatus\":{}}}", exit.into_raw());
        let _ = status.flush();
    }

    fn drain_group(
        mut child: Option<&mut Child>,
        status: &mut File,
        group: libc::pid_t,
        force: bool,
    ) -> ! {
        if force {
            signal_group(group, libc::SIGKILL);
            unsafe { libc::_exit(137) }
        }
        signal_group(group, libc::SIGTERM);
        let deadline = Instant::now()
            + if child.is_some() {
                TERM_GRACE
            } else {
                DESCENDANT_DRAIN_GRACE
            };
        while Instant::now() < deadline {
            if let Some(target) = child.as_mut() {
                if let Ok(Some(exit)) = target.try_wait() {
                    report_exit(status, exit);
                    child = None;
                }
            }
            thread::sleep(Duration::from_millis(25));
        }
        signal_group(group, libc::SIGKILL);
        unsafe { libc::_exit(143) }
    }

    fn run_protocol_child_guardian(values: &[String]) -> Result<i32, String> {
        let arguments = parse_guardian_arguments(values)?;
        let group = establish_guardian_session()?;
        set_close_on_exec(arguments.control_fd)?;
        set_close_on_exec(arguments.status_fd)?;
        set_nonblocking(arguments.control_fd)?;
        unsafe {
            libc::signal(libc::SIGTERM, libc::SIG_IGN);
            libc::signal(libc::SIGINT, libc::SIG_IGN);
        }
        let mut control = unsafe { File::from_raw_fd(arguments.control_fd) };
        let mut status = unsafe { File::from_raw_fd(arguments.status_fd) };
        let mut pending = Vec::new();

        loop {
            match read_guardian_command(&mut control, &mut pending)? {
                Some(Some(GuardianCommand::Start)) => break,
                Some(Some(GuardianCommand::DrainGrace | GuardianCommand::DrainForce))
                | Some(None) => return Ok(0),
                None => {}
            }
        }

        let (mut child, target_group) = spawn_target(&arguments.command, false)?;
        if target_group != group {
            return Err("guardian target escaped its process group".to_string());
        }
        writeln!(status, "STARTED {{\"pid\":{}}}", child.id())
            .map_err(|error| format!("write guardian STARTED status: {error}"))?;
        status.flush().map_err(|error| error.to_string())?;
        close_standard_streams();

        let mut exit_reported = false;
        loop {
            if !exit_reported {
                if let Some(exit) = child.try_wait().map_err(|error| error.to_string())? {
                    writeln!(status, "EXIT {{\"waitStatus\":{}}}", exit.into_raw())
                        .map_err(|error| format!("write guardian EXIT status: {error}"))?;
                    status.flush().map_err(|error| error.to_string())?;
                    exit_reported = true;
                }
            }
            match read_guardian_command(&mut control, &mut pending)? {
                Some(Some(GuardianCommand::DrainGrace)) => drain_group(
                    (!exit_reported).then_some(&mut child),
                    &mut status,
                    group,
                    false,
                ),
                Some(Some(GuardianCommand::DrainForce)) => drain_group(
                    (!exit_reported).then_some(&mut child),
                    &mut status,
                    group,
                    true,
                ),
                Some(Some(GuardianCommand::Start)) => {}
                Some(None) => drain_group(
                    (!exit_reported).then_some(&mut child),
                    &mut status,
                    group,
                    false,
                ),
                None => {}
            }
        }
    }

    fn run_sidecar_supervisor() -> Result<i32, String> {
        let arguments = parse_arguments()?;
        if unsafe { libc::setsid() } < 0 {
            return Err(format!(
                "setsid failed: {}",
                std::io::Error::last_os_error()
            ));
        }
        set_close_on_exec(arguments.liveness_fd)?;
        set_close_on_exec(arguments.status_fd)?;
        let (mut child, target_group) = spawn_target(&arguments.command, true)?;
        let mut status = unsafe { File::from_raw_fd(arguments.status_fd) };
        writeln!(
            status,
            "SIDECAR_STARTED {{\"pid\":{},\"pgid\":{}}}",
            child.id(),
            target_group
        )
        .map_err(|error| format!("write supervisor status: {error}"))?;
        status.flush().map_err(|error| error.to_string())?;
        drop(status);
        let mut liveness = unsafe { File::from_raw_fd(arguments.liveness_fd) };

        unsafe {
            libc::close(libc::STDIN_FILENO);
            libc::close(libc::STDOUT_FILENO);
            libc::close(libc::STDERR_FILENO);
        }
        loop {
            if let Some(exit) = child.try_wait().map_err(|error| error.to_string())? {
                cleanup_group_after_target_exit(target_group);
                return Ok(exit_code(exit));
            }
            if liveness_closed(&mut liveness)? {
                terminate_group(&mut child, target_group);
            }
        }
    }

    pub fn run() -> Result<i32, String> {
        let values: Vec<String> = env::args().skip(1).collect();
        if values.first().map(String::as_str) == Some("--child-guardian") {
            if values.get(1).map(String::as_str) == Some("--control-fd") {
                run_protocol_child_guardian(&values)
            } else {
                run_child_guardian(&values)
            }
        } else {
            run_sidecar_supervisor()
        }
    }
}

#[cfg(unix)]
fn main() {
    match unix::run() {
        Ok(code) => std::process::exit(code),
        Err(error) => {
            eprintln!("{error}");
            std::process::exit(2);
        }
    }
}

#[cfg(not(unix))]
fn main() {
    eprintln!("iac-code-desktop-exec is only shipped on macOS and Linux");
    std::process::exit(2);
}
