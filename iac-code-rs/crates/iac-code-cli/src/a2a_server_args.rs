mod config;
mod model;
mod parser;
mod validation;

pub(super) use model::A2AServerArgs;
pub(super) use parser::parse_a2a_server_args;
pub(super) use validation::validate_a2a_server_startup_options_for_cli;

#[cfg(test)]
mod tests {
    use std::fs;

    use super::*;
    use crate::test_support::unique_temp_dir;

    #[test]
    fn a2a_server_config_reads_artifact_dir_like_python() {
        let root = unique_temp_dir("iac-code-rs-a2a-artifact-dir-config");
        fs::create_dir_all(&root).expect("root dir should be created");
        let config_path = root.join("a2a.yml");
        let artifact_dir = root.join("artifacts");
        fs::write(
            &config_path,
            format!(
                "artifact-dir: {}\npersistence-dir: {}\n",
                artifact_dir.display(),
                root.display()
            ),
        )
        .expect("config should be written");

        let args = parse_a2a_server_args(&[
            "--config".to_owned(),
            config_path.to_string_lossy().to_string(),
        ])
        .expect("server args should parse");

        assert_eq!(args.artifact_dir, artifact_dir.to_string_lossy());

        fs::remove_dir_all(root).ok();
    }

    #[test]
    fn a2a_server_config_reads_redis_push_queue_options_like_python() {
        let root = unique_temp_dir("iac-code-rs-a2a-redis-push-config");
        fs::create_dir_all(&root).expect("root dir should be created");
        let config_path = root.join("a2a.yml");
        fs::write(
            &config_path,
            "push-notifications: true\n\
             push-queue: redis-streams\n\
             push-redis-url: redis://localhost:6379/0\n\
             push-stream: custom:push\n\
             push-retry-key: custom:push:retry\n\
             push-dead-stream: custom:push:dead\n\
             push-consumer-group: custom-workers\n\
             push-consumer-name: worker-a\n\
             push-lease-timeout-ms: 120000\n",
        )
        .expect("config should be written");

        let args = parse_a2a_server_args(&[
            "--config".to_owned(),
            config_path.to_string_lossy().to_string(),
        ])
        .expect("server args should parse");

        assert!(args.push_notifications);
        assert_eq!(args.push_queue, "redis-streams");
        assert_eq!(args.push_redis_url, "redis://localhost:6379/0");
        assert_eq!(args.push_stream, "custom:push");
        assert_eq!(args.push_retry_key, "custom:push:retry");
        assert_eq!(args.push_dead_stream, "custom:push:dead");
        assert_eq!(args.push_consumer_group, "custom-workers");
        assert_eq!(args.push_consumer_name, "worker-a");
        assert_eq!(args.push_lease_timeout_ms, 120_000);

        fs::remove_dir_all(root).ok();
    }

    #[test]
    fn a2a_server_log_to_stdout_matches_python_cli_and_config_rules() {
        let root = unique_temp_dir("iac-code-rs-a2a-log-stdout-config");
        fs::create_dir_all(&root).expect("root dir should be created");
        let config_path = root.join("a2a.yml");
        fs::write(&config_path, "transport: http\nlog-to-stdout: true\n")
            .expect("config should be written");

        let args = parse_a2a_server_args(&[
            "--config".to_owned(),
            config_path.to_string_lossy().to_string(),
        ])
        .expect("server args should parse");
        assert!(args.log_to_stdout);

        let args = parse_a2a_server_args(&[
            "--config".to_owned(),
            config_path.to_string_lossy().to_string(),
            "--no-log-to-stdout".to_owned(),
        ])
        .expect("CLI negative log flag should override config");
        assert!(!args.log_to_stdout);

        fs::write(&config_path, "transport: http\nlog-to-stdout: false\n")
            .expect("config should be rewritten");
        let args = parse_a2a_server_args(&[
            "--config".to_owned(),
            config_path.to_string_lossy().to_string(),
            "--log-to-stdout".to_owned(),
        ])
        .expect("CLI positive log flag should override config");
        assert!(args.log_to_stdout);

        let args = parse_a2a_server_args(&["--log-to-stdout".to_owned()])
            .expect("CLI log flag should parse");
        assert!(args.log_to_stdout);

        let args = parse_a2a_server_args(&[
            "--transport".to_owned(),
            "stdio".to_owned(),
            "--log-to-stdout".to_owned(),
        ])
        .expect("stdio args should parse before startup validation");
        let error = validate_a2a_server_startup_options_for_cli(&args)
            .expect_err("stdio must reject stdout logging");
        assert_eq!(
            error,
            "--log-to-stdout cannot be used with --transport stdio because stdout carries A2A frames."
        );

        fs::remove_dir_all(root).ok();
    }
}
