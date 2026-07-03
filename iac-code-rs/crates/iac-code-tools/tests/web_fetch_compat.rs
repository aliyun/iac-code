use std::io::{Read, Write};
use std::net::TcpListener;
use std::thread;
use std::time::{Duration, Instant};

use iac_code_protocol::json;
use iac_code_tools::{
    register_file_tools, RegistryToolExecutor, ToolCallRequest, ToolContext, ToolExecutor,
    ToolRegistry,
};

#[test]
fn default_tools_fetch_html_pages_as_plain_text_like_python() {
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
            request.starts_with("GET /doc HTTP/1.1"),
            "unexpected request line: {request}"
        );
        let body = r#"
            <html>
              <head><style>.hidden { display: none; }</style></head>
              <body>
                <h1>Hello &amp; world</h1>
                <script>throw new Error("ignore");</script>
                <p>ROS template docs</p>
              </body>
            </html>
        "#;
        write!(
            stream,
            "HTTP/1.1 200 OK\r\ncontent-type: text/html; charset=utf-8\r\ncontent-length: {}\r\n\r\n{}",
            body.len(),
            body
        )
        .expect("write response");
    });

    let mut registry = ToolRegistry::new();
    register_file_tools(&mut registry);
    let executor =
        RegistryToolExecutor::new(registry).with_context(ToolContext { cwd: ".".into() });

    let result = executor.execute(ToolCallRequest {
        tool_use_id: "toolu_web".into(),
        tool_name: "web_fetch".into(),
        input: json::object([
            ("url", json::string(format!("http://{addr}/doc"))),
            ("max_length", json::number(200)),
        ]),
    });

    server.join().expect("server thread");

    assert!(!result.is_error, "{result:?}");
    assert!(
        result.content.contains("Hello & world"),
        "missing decoded text: {result:?}"
    );
    assert!(
        result.content.contains("ROS template docs"),
        "missing body text: {result:?}"
    );
    assert!(
        !result.content.contains("throw new Error") && !result.content.contains(".hidden"),
        "script/style content should be removed: {result:?}"
    );
    assert!(
        !result.content.contains("<h1>") && !result.content.contains("</p>"),
        "html tags should be stripped: {result:?}"
    );
}

#[test]
fn default_tools_decode_response_charset_like_python_httpx() {
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
            request.starts_with("GET /latin1 HTTP/1.1"),
            "unexpected request line: {request}"
        );
        let body = b"caf\xe9";
        write!(
            stream,
            "HTTP/1.1 200 OK\r\ncontent-type: text/plain; charset=iso-8859-1\r\ncontent-length: {}\r\n\r\n",
            body.len()
        )
        .expect("write headers");
        stream.write_all(body).expect("write body");
    });

    let mut registry = ToolRegistry::new();
    register_file_tools(&mut registry);
    let executor =
        RegistryToolExecutor::new(registry).with_context(ToolContext { cwd: ".".into() });

    let result = executor.execute(ToolCallRequest {
        tool_use_id: "toolu_web_charset".into(),
        tool_name: "web_fetch".into(),
        input: json::object([("url", json::string(format!("http://{addr}/latin1")))]),
    });

    server.join().expect("server thread");

    assert_eq!(result.content, "café");
    assert!(!result.is_error, "{result:?}");
}

#[test]
fn default_tools_treat_non_positive_max_length_like_python() {
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
            request.starts_with("GET /empty HTTP/1.1"),
            "unexpected request line: {request}"
        );
        let body = "abcdef";
        write!(
            stream,
            "HTTP/1.1 200 OK\r\ncontent-type: text/plain\r\ncontent-length: {}\r\n\r\n{}",
            body.len(),
            body
        )
        .expect("write response");
    });

    let mut registry = ToolRegistry::new();
    register_file_tools(&mut registry);
    let executor =
        RegistryToolExecutor::new(registry).with_context(ToolContext { cwd: ".".into() });

    let result = executor.execute(ToolCallRequest {
        tool_use_id: "toolu_web_max_length".into(),
        tool_name: "web_fetch".into(),
        input: json::object([
            ("url", json::string(format!("http://{addr}/empty"))),
            ("max_length", json::number(-1)),
        ]),
    });

    server.join().expect("server thread");

    assert!(!result.is_error, "{result:?}");
    assert_eq!(result.content, "");
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
