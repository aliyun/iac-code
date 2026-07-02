use std::fs;
use std::path::PathBuf;
use std::time::{SystemTime, UNIX_EPOCH};

use iac_code_a2a::transports::unix::validate_socket_path;

#[test]
fn unix_socket_path_accepts_existing_parent_like_python() {
    let temp_dir = unique_temp_dir("iac-code-a2a-unix-ok");
    fs::create_dir_all(&temp_dir).expect("temp dir");
    let socket_path = temp_dir.join("agent.sock");

    assert_eq!(
        validate_socket_path(&socket_path).expect("valid socket path"),
        socket_path
    );

    fs::remove_dir_all(&temp_dir).expect("cleanup");
}

#[test]
fn unix_socket_path_rejects_missing_parent_with_python_message() {
    let temp_dir = unique_temp_dir("iac-code-a2a-unix-missing");
    let socket_path = temp_dir.join("missing").join("agent.sock");
    let missing_parent = socket_path.parent().expect("parent").to_path_buf();

    let error = validate_socket_path(&socket_path)
        .expect_err("missing parent")
        .to_string();

    assert_eq!(
        error,
        format!(
            "Unix socket parent does not exist: {}",
            missing_parent.display()
        )
    );
}

#[test]
fn unix_socket_path_accepts_relative_parent_dot_like_python_pathlib() {
    assert_eq!(
        validate_socket_path("agent.sock").expect("relative path"),
        PathBuf::from("agent.sock")
    );
}

fn unique_temp_dir(prefix: &str) -> PathBuf {
    let now = SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .expect("system time")
        .as_nanos();
    std::env::temp_dir().join(format!("{prefix}-{}-{now}", std::process::id()))
}
