use std::io::{Read, Write};
use std::net::TcpListener;
use std::thread;
use std::time::{Duration, Instant};

use iac_code_config::cloud_credentials::AliyunCredential;
use iac_code_protocol::json;
use iac_code_tools::{RosStackTool, Tool, ToolContext};

#[test]
fn ros_stack_create_polls_until_create_complete_like_python() {
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
            create_request.contains("Action=CreateStack"),
            "{create_request}"
        );
        assert!(
            create_request.contains("StackName=demo"),
            "{create_request}"
        );
        assert!(create_request.contains("TemplateBody="), "{create_request}");
        write_http_response(&mut create_stream, r#"{"StackId":"stack-123"}"#);

        let (mut status_stream, _) =
            accept_with_timeout(listener.try_clone().expect("clone listener"));
        configure_test_stream(&status_stream);
        let status_request = read_http_request_with_body(&mut status_stream);
        assert!(
            status_request.contains("Action=GetStack"),
            "{status_request}"
        );
        assert!(
            status_request.contains("StackId=stack-123"),
            "{status_request}"
        );
        write_http_response(
            &mut status_stream,
            r#"{"StackId":"stack-123","StackName":"demo","Status":"CREATE_COMPLETE","StatusReason":"","ProgressPercentage":100}"#,
        );

        let (mut resources_stream, _) = accept_with_timeout(listener);
        configure_test_stream(&resources_stream);
        let resources_request = read_http_request_with_body(&mut resources_stream);
        assert!(
            resources_request.contains("Action=ListStackResources"),
            "{resources_request}"
        );
        assert!(
            resources_request.contains("StackId=stack-123"),
            "{resources_request}"
        );
        write_http_response(
            &mut resources_stream,
            r#"{"Resources":[{"LogicalResourceId":"Vpc","ResourceType":"ALIYUN::ECS::VPC","Status":"CREATE_COMPLETE","StatusReason":""}]}"#,
        );
    });

    let credential = AliyunCredential {
        mode: "AK".into(),
        access_key_id: "test-ak".into(),
        access_key_secret: "test-secret".into(),
        region_id: "cn-hangzhou".into(),
        ..AliyunCredential::default()
    };
    let tool = RosStackTool::new(Some(credential))
        .with_endpoint_override("ros", format!("http://{addr}/"))
        .with_poll_interval(Duration::from_millis(0));
    let result = tool.execute(
        &json::object([
            ("action", json::string("CreateStack")),
            (
                "params",
                json::object([
                    ("StackName", json::string("demo")),
                    (
                        "TemplateBody",
                        json::string(r#"{"ROSTemplateFormatVersion":"2015-09-01"}"#),
                    ),
                ]),
            ),
            ("region_id", json::string("cn-hangzhou")),
        ]),
        &ToolContext::default(),
    );

    server.join().expect("server thread");

    assert!(!result.is_error, "{result:?}");
    assert!(
        result.content.contains("\"stack_id\": \"stack-123\""),
        "{result:?}"
    );
    assert!(
        result.content.contains("\"stack_name\": \"demo\""),
        "{result:?}"
    );
    assert!(
        result.content.contains("\"status\": \"CREATE_COMPLETE\""),
        "{result:?}"
    );
    assert!(
        result.content.contains("\"is_success\": true"),
        "{result:?}"
    );
}

#[test]
fn ros_stack_rejects_unknown_actions() {
    let tool = RosStackTool::new(None);

    let result = tool.execute(
        &json::object([("action", json::string("ListStacks"))]),
        &ToolContext::default(),
    );

    assert!(result.is_error, "{result:?}");
    assert!(
        result.content.contains("Invalid action 'ListStacks'"),
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
