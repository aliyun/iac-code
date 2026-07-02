use std::io::{Read, Write};
use std::net::TcpListener;
use std::thread;
use std::time::{Duration, Instant};

use iac_code_config::cloud_credentials::AliyunCredential;
use iac_code_protocol::json;
use iac_code_tools::{RosStackInstancesTool, Tool, ToolContext};

#[test]
fn ros_stack_instances_create_polls_operation_until_success_like_python() {
    let listener = TcpListener::bind("127.0.0.1:0").expect("bind test server");
    listener
        .set_nonblocking(true)
        .expect("test server should be nonblocking");
    let addr = listener.local_addr().expect("server addr");
    let server = thread::spawn(move || {
        let (mut create_stream, _) =
            accept_with_timeout(listener.try_clone().expect("clone listener"));
        configure_test_stream(&create_stream);
        let create_request = read_http_request_with_body(&mut create_stream);
        assert!(
            create_request.contains("Action=CreateStackInstances"),
            "{create_request}"
        );
        assert!(
            create_request.contains("StackGroupName=test-group"),
            "{create_request}"
        );
        write_http_response(&mut create_stream, r#"{"OperationId":"op-123"}"#);

        let (mut operation_stream, _) =
            accept_with_timeout(listener.try_clone().expect("clone listener"));
        configure_test_stream(&operation_stream);
        let operation_request = read_http_request_with_body(&mut operation_stream);
        assert!(
            operation_request.contains("Action=GetStackGroupOperation"),
            "{operation_request}"
        );
        assert!(
            operation_request.contains("OperationId=op-123"),
            "{operation_request}"
        );
        write_http_response(&mut operation_stream, r#"{"Status":"SUCCEEDED"}"#);

        let (mut instances_stream, _) = accept_with_timeout(listener);
        configure_test_stream(&instances_stream);
        let instances_request = read_http_request_with_body(&mut instances_stream);
        assert!(
            instances_request.contains("Action=ListStackInstances"),
            "{instances_request}"
        );
        assert!(
            instances_request.contains("StackGroupName=test-group"),
            "{instances_request}"
        );
        write_http_response(
            &mut instances_stream,
            r#"{"StackInstances":[{"AccountId":"123456789","RegionId":"cn-hangzhou","Status":"SUCCEEDED","StatusReason":"","ElapsedSeconds":3}]}"#,
        );
    });

    let credential = AliyunCredential {
        mode: "AK".into(),
        access_key_id: "test-ak".into(),
        access_key_secret: "test-secret".into(),
        region_id: "cn-hangzhou".into(),
        ..AliyunCredential::default()
    };
    let tool = RosStackInstancesTool::new(Some(credential))
        .with_endpoint_override("ros", format!("http://{addr}/"))
        .with_poll_interval(Duration::from_millis(0));
    let result = tool.execute(
        &json::object([
            ("action", json::string("CreateStackInstances")),
            (
                "params",
                json::object([
                    ("StackGroupName", json::string("test-group")),
                    ("AccountIds", json::array([json::string("123456789")])),
                    ("RegionIds", json::array([json::string("cn-hangzhou")])),
                ]),
            ),
            ("region_id", json::string("cn-hangzhou")),
        ]),
        &ToolContext::default(),
    );

    server.join().expect("server thread");

    assert!(!result.is_error, "{result:?}");
    assert!(
        result.content.contains("\"operation_id\": \"op-123\""),
        "{result:?}"
    );
    assert!(
        result
            .content
            .contains("\"stack_group_name\": \"test-group\""),
        "{result:?}"
    );
    assert!(
        result.content.contains("\"status\": \"SUCCEEDED\""),
        "{result:?}"
    );
    assert!(
        result.content.contains("\"progress_percentage\": 100"),
        "{result:?}"
    );
    assert!(
        result.content.contains("\"is_success\": true"),
        "{result:?}"
    );
}

#[test]
fn ros_stack_instances_rejects_unknown_actions() {
    let tool = RosStackInstancesTool::new(None);

    let result = tool.execute(
        &json::object([("action", json::string("ListStackInstances"))]),
        &ToolContext::default(),
    );

    assert!(result.is_error, "{result:?}");
    assert!(
        result
            .content
            .contains("Invalid action 'ListStackInstances'"),
        "{result:?}"
    );
}

fn configure_test_stream(stream: &std::net::TcpStream) {
    stream
        .set_nonblocking(false)
        .expect("accepted stream should be blocking");
    stream
        .set_read_timeout(Some(Duration::from_secs(5)))
        .expect("accepted stream should have read timeout");
}

fn accept_with_timeout(listener: TcpListener) -> (std::net::TcpStream, std::net::SocketAddr) {
    let deadline = Instant::now() + Duration::from_secs(5);
    loop {
        match listener.accept() {
            Ok(value) => return value,
            Err(error) if error.kind() == std::io::ErrorKind::WouldBlock => {
                if Instant::now() >= deadline {
                    panic!("timed out waiting for test server request");
                }
                thread::sleep(Duration::from_millis(10));
            }
            Err(error) => panic!("failed to accept test server request: {error}"),
        }
    }
}

fn write_http_response(stream: &mut impl Write, body: &str) {
    write!(
        stream,
        "HTTP/1.1 200 OK\r\ncontent-type: application/json\r\ncontent-length: {}\r\n\r\n{}",
        body.len(),
        body
    )
    .expect("write response");
}

fn read_http_request_with_body(stream: &mut impl Read) -> String {
    let mut buffer = [0_u8; 4096];
    let mut request = String::new();
    loop {
        let bytes_read = stream.read(&mut buffer).expect("read request");
        if bytes_read == 0 {
            break;
        }
        request.push_str(&String::from_utf8_lossy(&buffer[..bytes_read]));
        if let Some(expected_length) = content_length(&request) {
            if let Some(header_end) = request.find("\r\n\r\n") {
                let body_start = header_end + 4;
                if request.len().saturating_sub(body_start) >= expected_length {
                    break;
                }
            }
        }
    }
    request
}

fn content_length(request: &str) -> Option<usize> {
    request.lines().find_map(|line| {
        let (name, value) = line.split_once(':')?;
        name.eq_ignore_ascii_case("content-length")
            .then(|| value.trim().parse::<usize>().ok())
            .flatten()
    })
}
