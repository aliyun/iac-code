use std::io::{Read, Write};
use std::net::TcpListener;
use std::thread;
use std::time::{Duration, Instant};

use iac_code_protocol::json;
use iac_code_tools::{AliyunDocSearchTool, Tool, ToolContext};

#[test]
fn aliyun_doc_search_requests_python_params_and_formats_results() {
    let listener = TcpListener::bind("127.0.0.1:0").expect("bind test server");
    listener
        .set_nonblocking(true)
        .expect("test server should be nonblocking");
    let addr = listener.local_addr().expect("server addr");
    let server = thread::spawn(move || {
        let (mut stream, _) = accept_with_timeout(listener);
        configure_test_stream(&stream);
        let request = read_http_request(&mut stream);
        assert!(
            request.starts_with("GET /help/json/search.json?"),
            "unexpected request path: {request}"
        );
        assert!(
            request.contains("keywords=ROS"),
            "missing keywords: {request}"
        );
        assert!(
            request.contains("topics=DOCUMENT%2CPRODUCT"),
            "missing topics: {request}"
        );
        assert!(
            request.contains("language=zh") && request.contains("website=cn"),
            "missing language or website: {request}"
        );
        assert!(
            request.contains("pageSize=10") && request.contains("pageNum=1"),
            "missing pagination params: {request}"
        );
        assert!(
            request.contains("categoryId=28850"),
            "missing categoryId: {request}"
        );

        write_http_response(
            &mut stream,
            r#"{
                "success": true,
                "data": {
                    "documents": {
                        "totalCount": 50,
                        "data": [
                            {"title": "ROS 概述", "content": "资源编排服务简介", "url": "https://help.aliyun.com/doc1"},
                            {"title": "ROS 模板", "content": "模板语法说明", "url": "https://help.aliyun.com/doc2"}
                        ]
                    }
                }
            }"#,
        );
    });

    let tool =
        AliyunDocSearchTool::new().with_search_url(format!("http://{addr}/help/json/search.json"));
    let result = tool.execute(
        &json::object([
            ("keywords", json::string("ROS")),
            ("category_id", json::number(28850)),
        ]),
        &ToolContext::default(),
    );

    server.join().expect("server thread");

    assert!(!result.is_error, "{result:?}");
    assert!(result.content.contains("1. ROS 概述"), "{result:?}");
    assert!(result.content.contains("资源编排服务简介"), "{result:?}");
    assert!(
        result
            .content
            .contains("Link: https://help.aliyun.com/doc1"),
        "{result:?}"
    );
    assert!(
        result.content.contains("Found 2 documents (total 50)"),
        "{result:?}"
    );
    assert!(
        result
            .content
            .contains("Use web_fetch tool to read full document content if needed."),
        "{result:?}"
    );
}

#[test]
fn aliyun_doc_search_empty_keywords_matches_python_error() {
    let tool = AliyunDocSearchTool::new();

    let result = tool.execute(
        &json::object([("keywords", json::string("  "))]),
        &ToolContext::default(),
    );

    assert!(result.is_error, "{result:?}");
    assert_eq!(result.content, "keywords cannot be empty.");
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

fn read_http_request(stream: &mut impl Read) -> String {
    let mut buffer = [0_u8; 4096];
    let mut request = String::new();
    loop {
        let bytes_read = stream.read(&mut buffer).expect("read request");
        if bytes_read == 0 {
            break;
        }
        request.push_str(&String::from_utf8_lossy(&buffer[..bytes_read]));
        if request.contains("\r\n\r\n") {
            break;
        }
    }
    request
}
