use std::fs;

use iac_code_a2a::artifacts::A2AArtifactStore;
use iac_code_config::cloud_credentials::DEFAULT_REGION;
use iac_code_protocol::{json, StreamEvent, ToolResultEvent};

use crate::a2a_artifacts::a2a_artifacts_from_events;
use crate::a2a_messages::{a2a_message_aliyun_credential, a2a_message_iac_metadata_string};
use crate::a2a_server::{build_a2a_server_runtime, run_a2a_server, write_a2a_server_log_to_stdout};
use crate::a2a_server_args::A2AServerArgs;
use crate::test_support::unique_temp_dir;

#[test]
fn a2a_server_log_to_stdout_reaches_runtime_logging() {
    let root = unique_temp_dir("iac-code-rs-a2a-log-stdout-runtime");
    let persistence_dir = root.join("a2a");
    fs::create_dir_all(&persistence_dir).expect("persistence dir should be created");
    let mut args = A2AServerArgs {
        log_to_stdout: true,
        persistence_dir: persistence_dir.to_string_lossy().to_string(),
        ..A2AServerArgs::default()
    };
    args.transport = "http".to_owned();

    let runtime = build_a2a_server_runtime(&args, "http").expect("runtime should build from args");
    assert!(runtime.logs_to_stdout());

    let mut stdout = Vec::new();
    write_a2a_server_log_to_stdout(true, "server started", &mut stdout)
        .expect("stdout logging should write");
    assert_eq!(String::from_utf8(stdout).expect("utf8"), "server started\n");

    let mut stdout = Vec::new();
    write_a2a_server_log_to_stdout(false, "server started", &mut stdout)
        .expect("disabled stdout logging should not fail");
    assert!(stdout.is_empty());

    fs::remove_dir_all(root).ok();
}

#[test]
fn a2a_server_optional_runtime_transports_reach_runtime_layer() {
    let cases = [
        vec![
            "--transport",
            "grpc",
            "--host",
            "not-a-valid-host.invalid",
            "--port",
            "41242",
        ],
        vec![
            "--transport",
            "grpc-jsonrpc",
            "--host",
            "not-a-valid-host.invalid",
            "--port",
            "41242",
        ],
        vec![
            "--transport",
            "redis-streams",
            "--redis-url",
            "not-a-redis-url",
        ],
    ];

    for args in cases {
        let args = args.into_iter().map(str::to_owned).collect::<Vec<_>>();
        let error = run_a2a_server(&args).expect_err("invalid runtime config should fail");

        assert!(
            !error.contains("not implemented"),
            "transport stopped at dispatcher instead of runtime layer: {error}"
        );
    }
}

#[test]
fn a2a_server_extracts_model_override_from_message_metadata_like_python() {
    let message = json::object([(
        "metadata",
        json::object([(
            "iac_code",
            json::object([("iac_code_model", json::string(" qwen3.7-plus "))]),
        )]),
    )]);

    assert_eq!(
        a2a_message_iac_metadata_string(&message, "iac_code_model"),
        Some("qwen3.7-plus")
    );
}

#[test]
fn a2a_server_extracts_aliyun_credential_from_message_metadata_like_python() {
    let message = json::object([(
        "metadata",
        json::object([(
            "iac_code",
            json::object([
                ("alibaba_cloud_access_key_id", json::string(" client-ak ")),
                (
                    "alibaba_cloud_access_key_secret",
                    json::string(" client-secret "),
                ),
                ("alibaba_cloud_security_token", json::string(" client-sts ")),
                ("alibaba_cloud_region_id", json::string(" cn-beijing ")),
            ]),
        )]),
    )]);

    let credential = a2a_message_aliyun_credential(&message).expect("metadata credential");

    assert_eq!(credential.mode, "StsToken");
    assert_eq!(credential.access_key_id, "client-ak");
    assert_eq!(credential.access_key_secret, "client-secret");
    assert_eq!(credential.sts_token, "client-sts");
    assert_eq!(credential.region_id, "cn-beijing");

    let ak_message = json::object([(
        "metadata",
        json::object([(
            "iac_code",
            json::object([
                ("alibaba_cloud_access_key_id", json::string("client-ak")),
                (
                    "alibaba_cloud_access_key_secret",
                    json::string("client-secret"),
                ),
            ]),
        )]),
    )]);
    let credential = a2a_message_aliyun_credential(&ak_message).expect("metadata credential");

    assert_eq!(credential.mode, "AK");
    assert_eq!(credential.sts_token, "");
    assert_eq!(credential.region_id, DEFAULT_REGION);

    let partial_message = json::object([(
        "metadata",
        json::object([(
            "iac_code",
            json::object([("alibaba_cloud_access_key_id", json::string("client-ak"))]),
        )]),
    )]);

    assert!(a2a_message_aliyun_credential(&partial_message).is_none());
}

#[test]
fn a2a_tool_result_artifacts_are_saved_from_json_payloads_like_python() {
    let root = unique_temp_dir("iac-code-rs-a2a-tool-result-artifacts");
    let artifact_store = A2AArtifactStore::new(root.join("artifacts"));
    fs::create_dir_all(&root).expect("root dir should be created");
    let source_path = root.join("source.txt");
    fs::write(&source_path, "from source path").expect("source file should be written");

    let events = vec![
        StreamEvent::ToolResult(ToolResultEvent {
            tool_use_id: "tool-text".into(),
            tool_name: "render".into(),
            result: json::object([(
                "artifact",
                json::object([
                    ("filename", json::string("report.txt")),
                    ("mediaType", json::string("text/plain")),
                    ("content", json::string("hello artifact")),
                ]),
            )])
            .to_compact_json(),
            is_error: false,
        }),
        StreamEvent::ToolResult(ToolResultEvent {
            tool_use_id: "tool-base64".into(),
            tool_name: "render".into(),
            result: json::object([(
                "artifact",
                json::object([
                    ("filename", json::string("image.bin")),
                    ("media_type", json::string("application/octet-stream")),
                    ("base64", json::string("AAFiYXNlNjQ=")),
                ]),
            )])
            .to_compact_json(),
            is_error: false,
        }),
        StreamEvent::ToolResult(ToolResultEvent {
            tool_use_id: "tool-path".into(),
            tool_name: "render".into(),
            result: json::object([(
                "artifact",
                json::object([
                    ("filename", json::string("source-copy.txt")),
                    ("mediaType", json::string("text/plain")),
                    ("path", json::string(source_path.to_string_lossy())),
                ]),
            )])
            .to_compact_json(),
            is_error: false,
        }),
        StreamEvent::ToolResult(ToolResultEvent {
            tool_use_id: "tool-plain".into(),
            tool_name: "read_file".into(),
            result: "plain text result".into(),
            is_error: false,
        }),
    ];

    let artifacts = a2a_artifacts_from_events(&events, &artifact_store);

    assert_eq!(artifacts.len(), 3);
    assert_eq!(artifacts[0].filename, "report.txt");
    assert_eq!(artifacts[0].media_type, "text/plain");
    assert_eq!(artifacts[0].byte_size, "hello artifact".len());
    assert_eq!(artifacts[1].filename, "image.bin");
    assert_eq!(artifacts[1].byte_size, b"\x00\x01base64".len());
    assert_eq!(artifacts[2].filename, "source-copy.txt");
    assert_eq!(
        fs::read_to_string(
            artifact_store
                .path_for(&artifacts[0].artifact_id)
                .expect("text path")
        )
        .expect("text artifact should be readable"),
        "hello artifact"
    );
    assert_eq!(
        fs::read(
            artifact_store
                .path_for(&artifacts[1].artifact_id)
                .expect("base64 path")
        )
        .expect("base64 artifact should be readable"),
        b"\x00\x01base64"
    );
    assert_eq!(
        fs::read_to_string(
            artifact_store
                .path_for(&artifacts[2].artifact_id)
                .expect("path artifact")
        )
        .expect("path artifact should be readable"),
        "from source path"
    );

    fs::remove_dir_all(root).ok();
}
