use std::fs;
use std::io::{Read, Write};
use std::net::TcpListener;
use std::thread;
use std::time::{Duration, Instant, SystemTime, UNIX_EPOCH};

use iac_code_config::cloud_credentials::AliyunCredential;
use iac_code_protocol::json;
use iac_code_tools::{AliyunApiTool, Tool, ToolContext};

#[test]
fn aliyun_api_schema_versions_and_readonly_match_python_basics() {
    let tool = AliyunApiTool::new(None);

    assert_eq!(tool.name(), "aliyun_api");
    assert!(tool.description().contains("Alibaba Cloud product API"));
    assert!(tool
        .input_schema()
        .to_compact_json()
        .contains("\"product\""));
    assert_eq!(
        tool.resolve_version("ecs", None).expect("ecs version"),
        "2014-05-26"
    );
    assert_eq!(
        tool.resolve_version("ROS", None).expect("ros version"),
        "2019-09-10"
    );
    assert_eq!(
        tool.resolve_version("custom-svc", Some("2023-01-01"))
            .expect("explicit version"),
        "2023-01-01"
    );
    assert!(tool.resolve_version("unknown-product", None).is_err());
    assert!(tool.is_read_only(&json::object([(
        "action",
        json::string("DescribeInstances")
    )])));
    assert!(tool.is_read_only(&json::object([
        ("product", json::string("ros")),
        ("action", json::string("PreviewStack")),
    ])));
    assert!(!tool.is_read_only(&json::object([("action", json::string("CreateInstance"))])));
}

#[test]
fn aliyun_api_rpc_call_signs_serializes_params_and_formats_response() {
    let listener = TcpListener::bind("127.0.0.1:0").expect("bind test server");
    listener
        .set_nonblocking(true)
        .expect("test server should be nonblocking");
    let addr = listener.local_addr().expect("server addr");
    let server = thread::spawn(move || {
        let (mut stream, _) = accept_with_timeout(listener);
        configure_test_stream(&stream);
        let request = read_http_request_with_body(&mut stream);
        assert!(request.starts_with("POST / HTTP/1.1"), "{request}");
        assert!(request.contains("Action=DescribeInstances"), "{request}");
        assert!(request.contains("Version=2014-05-26"), "{request}");
        assert!(request.contains("AccessKeyId=test-ak"), "{request}");
        assert!(request.contains("RegionId=cn-hangzhou"), "{request}");
        assert!(request.contains("PageSize=10"), "{request}");
        assert!(request.contains("DryRun=true"), "{request}");
        assert!(request.contains("Signature="), "{request}");
        assert!(!request.contains("test-secret"), "{request}");

        write_http_response(&mut stream, r#"{"RequestId":"REQ-1","Instances":[]}"#);
    });

    let credential = AliyunCredential {
        mode: "AK".into(),
        access_key_id: "test-ak".into(),
        access_key_secret: "test-secret".into(),
        region_id: "cn-hangzhou".into(),
        ..AliyunCredential::default()
    };
    let tool = AliyunApiTool::new(Some(credential))
        .with_endpoint_override("ecs", format!("http://{addr}/"));
    let result = tool.execute(
        &json::object([
            ("product", json::string("ecs")),
            ("action", json::string("DescribeInstances")),
            (
                "params",
                json::object([
                    ("PageSize", json::number(10)),
                    ("DryRun", json::bool_value(true)),
                ]),
            ),
            ("region_id", json::string("cn-hangzhou")),
        ]),
        &ToolContext::default(),
    );

    server.join().expect("server thread");

    assert!(!result.is_error, "{result:?}");
    assert!(
        result.content.contains("\"RequestId\": \"REQ-1\""),
        "{result:?}"
    );
    assert!(result.content.contains("\"Instances\": []"), "{result:?}");
}

#[test]
fn aliyun_api_refreshes_expired_oauth_sts_before_rpc_call_like_python() {
    let listener = TcpListener::bind("127.0.0.1:0").expect("bind test server");
    listener
        .set_nonblocking(true)
        .expect("test server should be nonblocking");
    let addr = listener.local_addr().expect("server addr");
    let server = thread::spawn(move || {
        let (mut oauth_stream, _) = accept_one_with_timeout(&listener);
        configure_test_stream(&oauth_stream);
        let oauth_request = read_http_request_with_body(&mut oauth_stream);
        assert!(
            oauth_request.starts_with("POST /v1/exchange HTTP/1.1"),
            "{oauth_request}"
        );
        assert!(
            oauth_request
                .to_ascii_lowercase()
                .contains("authorization: bearer old-access"),
            "{oauth_request}"
        );
        assert!(oauth_request.contains("\r\n\r\n{}"), "{oauth_request}");
        write_http_response(
            &mut oauth_stream,
            r#"{"accessKeyId":"new-ak","accessKeySecret":"new-secret","securityToken":"new-sts","expiration":2500}"#,
        );

        let (mut api_stream, _) = accept_one_with_timeout(&listener);
        configure_test_stream(&api_stream);
        let api_request = read_http_request_with_body(&mut api_stream);
        assert!(api_request.starts_with("POST / HTTP/1.1"), "{api_request}");
        assert!(
            api_request.contains("Action=DescribeInstances"),
            "{api_request}"
        );
        assert!(api_request.contains("AccessKeyId=new-ak"), "{api_request}");
        assert!(
            api_request.contains("SecurityToken=new-sts"),
            "{api_request}"
        );
        assert!(!api_request.contains("old-ak"), "{api_request}");
        assert!(!api_request.contains("old-sts"), "{api_request}");
        assert!(!api_request.contains("new-secret"), "{api_request}");
        write_http_response(&mut api_stream, r#"{"RequestId":"REQ-OAUTH"}"#);
    });

    let root = unique_temp_dir("iac-code-rs-oauth-refresh");
    fs::create_dir_all(&root).expect("temp dir should be created");
    let cloud_credentials_path = root.join(".cloud-credentials.yml");
    let credential = AliyunCredential {
        mode: "OAuth".into(),
        access_key_id: "old-ak".into(),
        access_key_secret: "old-secret".into(),
        region_id: "cn-hangzhou".into(),
        sts_token: "old-sts".into(),
        sts_expiration: 900,
        oauth_site_type: "CN".into(),
        oauth_access_token: "old-access".into(),
        oauth_refresh_token: "old-refresh".into(),
        oauth_access_token_expire: 2000,
        ..AliyunCredential::default()
    };
    let tool = AliyunApiTool::new(Some(credential))
        .with_endpoint_override("ecs", format!("http://{addr}/"))
        .with_oauth_base_url(format!("http://{addr}"))
        .with_cloud_credentials_path(cloud_credentials_path.clone())
        .with_now_epoch_seconds(1000);

    let result = tool.execute(
        &json::object([
            ("product", json::string("ecs")),
            ("action", json::string("DescribeInstances")),
            ("region_id", json::string("cn-hangzhou")),
        ]),
        &ToolContext::default(),
    );

    server.join().expect("server thread");

    assert!(!result.is_error, "{result:?}");
    assert!(
        result.content.contains("\"RequestId\": \"REQ-OAUTH\""),
        "{result:?}"
    );
    let saved = fs::read_to_string(&cloud_credentials_path).expect("refreshed credentials saved");
    assert!(saved.contains("mode: OAuth"), "{saved}");
    assert!(saved.contains("region_id: cn-hangzhou"), "{saved}");
    assert!(saved.contains("oauth_access_token: old-access"), "{saved}");
    assert!(
        saved.contains("oauth_refresh_token: old-refresh"),
        "{saved}"
    );
    assert!(saved.contains("access_key_id: new-ak"), "{saved}");
    assert!(saved.contains("access_key_secret: new-secret"), "{saved}");
    assert!(saved.contains("sts_token: new-sts"), "{saved}");
    assert!(saved.contains("sts_expiration: 2500"), "{saved}");

    fs::remove_dir_all(&root).ok();
}

#[test]
fn aliyun_api_accepts_oauth_sts_iso8601_expiration_like_python() {
    let listener = TcpListener::bind("127.0.0.1:0").expect("bind test server");
    listener
        .set_nonblocking(true)
        .expect("test server should be nonblocking");
    let addr = listener.local_addr().expect("server addr");
    let server = thread::spawn(move || {
        let (mut oauth_stream, _) = accept_one_with_timeout(&listener);
        configure_test_stream(&oauth_stream);
        let oauth_request = read_http_request_with_body(&mut oauth_stream);
        assert!(
            oauth_request.starts_with("POST /v1/exchange HTTP/1.1"),
            "{oauth_request}"
        );
        write_http_response(
            &mut oauth_stream,
            r#"{"accessKeyId":"new-ak","accessKeySecret":"new-secret","securityToken":"new-sts","expiration":"1970-01-01T00:41:40Z"}"#,
        );

        let (mut api_stream, _) = accept_one_with_timeout(&listener);
        configure_test_stream(&api_stream);
        let api_request = read_http_request_with_body(&mut api_stream);
        assert!(api_request.starts_with("POST / HTTP/1.1"), "{api_request}");
        assert!(api_request.contains("AccessKeyId=new-ak"), "{api_request}");
        assert!(
            api_request.contains("SecurityToken=new-sts"),
            "{api_request}"
        );
        write_http_response(&mut api_stream, r#"{"RequestId":"REQ-OAUTH-ISO"}"#);
    });

    let root = unique_temp_dir("iac-code-rs-oauth-iso-expiration");
    fs::create_dir_all(&root).expect("temp dir should be created");
    let cloud_credentials_path = root.join(".cloud-credentials.yml");
    let credential = AliyunCredential {
        mode: "OAuth".into(),
        access_key_id: "old-ak".into(),
        access_key_secret: "old-secret".into(),
        region_id: "cn-hangzhou".into(),
        sts_token: "old-sts".into(),
        sts_expiration: 900,
        oauth_site_type: "CN".into(),
        oauth_access_token: "old-access".into(),
        oauth_refresh_token: "old-refresh".into(),
        oauth_access_token_expire: 2000,
        ..AliyunCredential::default()
    };
    let tool = AliyunApiTool::new(Some(credential))
        .with_endpoint_override("ecs", format!("http://{addr}/"))
        .with_oauth_base_url(format!("http://{addr}"))
        .with_cloud_credentials_path(cloud_credentials_path.clone())
        .with_now_epoch_seconds(1000);

    let result = tool.execute(
        &json::object([
            ("product", json::string("ecs")),
            ("action", json::string("DescribeInstances")),
            ("region_id", json::string("cn-hangzhou")),
        ]),
        &ToolContext::default(),
    );

    server.join().expect("server thread");

    assert!(!result.is_error, "{result:?}");
    assert!(
        result.content.contains("\"RequestId\": \"REQ-OAUTH-ISO\""),
        "{result:?}"
    );
    let saved = fs::read_to_string(&cloud_credentials_path).expect("refreshed credentials saved");
    assert!(saved.contains("sts_expiration: 2500"), "{saved}");

    fs::remove_dir_all(&root).ok();
}

#[test]
fn aliyun_api_oauth_permanent_exchange_error_prompts_relogin_and_redacts_token_like_python() {
    let listener = TcpListener::bind("127.0.0.1:0").expect("bind test server");
    listener
        .set_nonblocking(true)
        .expect("test server should be nonblocking");
    let addr = listener.local_addr().expect("server addr");
    let server = thread::spawn(move || {
        let (mut oauth_stream, _) = accept_one_with_timeout(&listener);
        configure_test_stream(&oauth_stream);
        let oauth_request = read_http_request_with_body(&mut oauth_stream);
        assert!(
            oauth_request.starts_with("POST /v1/exchange HTTP/1.1"),
            "{oauth_request}"
        );
        write_http_error_response(
            &mut oauth_stream,
            400,
            r#"{"error":"invalid_grant","error_description":"token old-access expired"}"#,
        );
    });

    let credential = AliyunCredential {
        mode: "OAuth".into(),
        access_key_id: "old-ak".into(),
        access_key_secret: "old-secret".into(),
        region_id: "cn-hangzhou".into(),
        sts_token: "old-sts".into(),
        sts_expiration: 900,
        oauth_site_type: "CN".into(),
        oauth_access_token: "old-access".into(),
        oauth_refresh_token: "old-refresh".into(),
        oauth_access_token_expire: 2000,
        ..AliyunCredential::default()
    };
    let tool = AliyunApiTool::new(Some(credential))
        .with_endpoint_override("ecs", format!("http://{addr}/"))
        .with_oauth_base_url(format!("http://{addr}"))
        .with_now_epoch_seconds(1000);

    let result = tool.execute(
        &json::object([
            ("product", json::string("ecs")),
            ("action", json::string("DescribeInstances")),
            ("region_id", json::string("cn-hangzhou")),
        ]),
        &ToolContext::default(),
    );

    server.join().expect("server thread");

    assert!(result.is_error, "{result:?}");
    assert!(result.content.contains("invalid_grant"), "{result:?}");
    assert!(result.content.contains("/auth"), "{result:?}");
    assert!(result.content.contains("[REDACTED]"), "{result:?}");
    assert!(!result.content.contains("old-access"), "{result:?}");
}

#[test]
fn aliyun_api_refreshes_expired_oauth_access_token_before_sts_exchange_like_python() {
    let listener = TcpListener::bind("127.0.0.1:0").expect("bind test server");
    listener
        .set_nonblocking(true)
        .expect("test server should be nonblocking");
    let addr = listener.local_addr().expect("server addr");
    let server = thread::spawn(move || {
        let (mut token_stream, _) = accept_one_with_timeout(&listener);
        configure_test_stream(&token_stream);
        let token_request = read_http_request_with_body(&mut token_stream);
        assert!(
            token_request.starts_with("POST /v1/token HTTP/1.1"),
            "{token_request}"
        );
        assert!(
            token_request.contains("grant_type=refresh_token"),
            "{token_request}"
        );
        assert!(
            token_request.contains("refresh_token=old-refresh"),
            "{token_request}"
        );
        assert!(
            token_request.contains("client_id=4038181954557748008"),
            "{token_request}"
        );
        write_http_response(
            &mut token_stream,
            r#"{"access_token":"new-access","refresh_token":"new-refresh","expires_in":3600,"refresh_expires_in":7200}"#,
        );

        let (mut oauth_stream, _) = accept_one_with_timeout(&listener);
        configure_test_stream(&oauth_stream);
        let oauth_request = read_http_request_with_body(&mut oauth_stream);
        assert!(
            oauth_request.starts_with("POST /v1/exchange HTTP/1.1"),
            "{oauth_request}"
        );
        assert!(
            oauth_request
                .to_ascii_lowercase()
                .contains("authorization: bearer new-access"),
            "{oauth_request}"
        );
        write_http_response(
            &mut oauth_stream,
            r#"{"accessKeyId":"new-ak","accessKeySecret":"new-secret","securityToken":"new-sts","expiration":2500}"#,
        );

        let (mut api_stream, _) = accept_one_with_timeout(&listener);
        configure_test_stream(&api_stream);
        let api_request = read_http_request_with_body(&mut api_stream);
        assert!(api_request.starts_with("POST / HTTP/1.1"), "{api_request}");
        assert!(api_request.contains("AccessKeyId=new-ak"), "{api_request}");
        assert!(
            api_request.contains("SecurityToken=new-sts"),
            "{api_request}"
        );
        assert!(!api_request.contains("old-access"), "{api_request}");
        assert!(!api_request.contains("old-refresh"), "{api_request}");
        assert!(!api_request.contains("new-secret"), "{api_request}");
        write_http_response(&mut api_stream, r#"{"RequestId":"REQ-OAUTH-REFRESH"}"#);
    });

    let root = unique_temp_dir("iac-code-rs-oauth-token-refresh");
    fs::create_dir_all(&root).expect("temp dir should be created");
    let cloud_credentials_path = root.join(".cloud-credentials.yml");
    let credential = AliyunCredential {
        mode: "OAuth".into(),
        access_key_id: "old-ak".into(),
        access_key_secret: "old-secret".into(),
        region_id: "cn-hangzhou".into(),
        sts_token: "old-sts".into(),
        sts_expiration: 800,
        oauth_site_type: "CN".into(),
        oauth_access_token: "old-access".into(),
        oauth_refresh_token: "old-refresh".into(),
        oauth_access_token_expire: 900,
        ..AliyunCredential::default()
    };
    let tool = AliyunApiTool::new(Some(credential))
        .with_endpoint_override("ecs", format!("http://{addr}/"))
        .with_oauth_base_url(format!("http://{addr}"))
        .with_cloud_credentials_path(cloud_credentials_path.clone())
        .with_now_epoch_seconds(1000);

    let result = tool.execute(
        &json::object([
            ("product", json::string("ecs")),
            ("action", json::string("DescribeInstances")),
            ("region_id", json::string("cn-hangzhou")),
        ]),
        &ToolContext::default(),
    );

    server.join().expect("server thread");

    assert!(!result.is_error, "{result:?}");
    assert!(
        result
            .content
            .contains("\"RequestId\": \"REQ-OAUTH-REFRESH\""),
        "{result:?}"
    );
    let saved = fs::read_to_string(&cloud_credentials_path).expect("refreshed credentials saved");
    assert!(saved.contains("oauth_access_token: new-access"), "{saved}");
    assert!(
        saved.contains("oauth_refresh_token: new-refresh"),
        "{saved}"
    );
    assert!(saved.contains("oauth_access_token_expire: 4600"), "{saved}");
    assert!(
        saved.contains("oauth_refresh_token_expire: 8200"),
        "{saved}"
    );
    assert!(saved.contains("access_key_id: new-ak"), "{saved}");
    assert!(saved.contains("sts_token: new-sts"), "{saved}");

    fs::remove_dir_all(&root).ok();
}

#[test]
fn aliyun_api_roa_call_sends_query_body_and_acs3_headers_like_python_sdk() {
    let listener = TcpListener::bind("127.0.0.1:0").expect("bind test server");
    listener
        .set_nonblocking(true)
        .expect("test server should be nonblocking");
    let addr = listener.local_addr().expect("server addr");
    let server = thread::spawn(move || {
        let (mut stream, _) = accept_with_timeout(listener);
        configure_test_stream(&stream);
        let request = read_http_request_with_body(&mut stream);
        assert!(request.starts_with("PUT /clusters/cluster-1?"), "{request}");
        assert!(request.contains("PageSize=10"), "{request}");
        assert!(request.contains("with_detail=true"), "{request}");
        assert!(request.contains("x-acs-version: 2015-12-15"), "{request}");
        assert!(
            request.contains("x-acs-action: DescribeCluster"),
            "{request}"
        );
        assert!(request.contains("x-acs-date:"), "{request}");
        assert!(request.contains("x-acs-signature-nonce:"), "{request}");
        assert!(request.contains("x-acs-content-sha256:"), "{request}");
        assert!(
            request.contains("Authorization: ACS3-HMAC-SHA256 Credential=test-ak")
                || request.contains("authorization: ACS3-HMAC-SHA256 Credential=test-ak"),
            "{request}"
        );
        assert!(request.contains(r#"{"name":"demo"}"#), "{request}");
        assert!(!request.contains("Action=DescribeCluster"), "{request}");
        assert!(!request.contains("test-secret"), "{request}");

        write_http_response(&mut stream, r#"{"RequestId":"REQ-ROA"}"#);
    });

    let credential = AliyunCredential {
        mode: "AK".into(),
        access_key_id: "test-ak".into(),
        access_key_secret: "test-secret".into(),
        region_id: "cn-hangzhou".into(),
        ..AliyunCredential::default()
    };
    let tool = AliyunApiTool::new(Some(credential))
        .with_endpoint_override("cs", format!("http://{addr}/"));
    let result = tool.execute(
        &json::object([
            ("product", json::string("cs")),
            ("action", json::string("DescribeCluster")),
            ("version", json::string("2015-12-15")),
            ("style", json::string("ROA")),
            ("method", json::string("PUT")),
            ("pathname", json::string("/clusters/cluster-1")),
            (
                "params",
                json::object([
                    ("PageSize", json::number(10)),
                    ("with_detail", json::bool_value(true)),
                ]),
            ),
            ("body", json::object([("name", json::string("demo"))])),
            ("region_id", json::string("cn-hangzhou")),
        ]),
        &ToolContext::default(),
    );

    server.join().expect("server thread");

    assert!(!result.is_error, "{result:?}");
    assert!(
        result.content.contains("\"RequestId\": \"REQ-ROA\""),
        "{result:?}"
    );
}

#[test]
fn aliyun_api_expands_ros_parameters_object_like_python_hook() {
    let listener = TcpListener::bind("127.0.0.1:0").expect("bind test server");
    listener
        .set_nonblocking(true)
        .expect("test server should be nonblocking");
    let addr = listener.local_addr().expect("server addr");
    let server = thread::spawn(move || {
        let (mut stream, _) = accept_with_timeout(listener);
        configure_test_stream(&stream);
        let request = read_http_request_with_body(&mut stream);
        assert!(request.contains("Action=PreviewStack"), "{request}");
        assert!(
            request.contains("Parameters.1.ParameterKey=InstanceType"),
            "{request}"
        );
        assert!(
            request.contains("Parameters.1.ParameterValue=ecs.g7.large"),
            "{request}"
        );
        assert!(
            request.contains("Parameters.2.ParameterKey=SystemDisk"),
            "{request}"
        );
        assert!(
            request.contains("Parameters.2.ParameterValue=%7B%22Size%22%3A40%7D"),
            "{request}"
        );
        assert!(!request.contains("Parameters="), "{request}");

        write_http_response(&mut stream, r#"{"RequestId":"REQ-PARAMS"}"#);
    });

    let credential = AliyunCredential {
        mode: "AK".into(),
        access_key_id: "test-ak".into(),
        access_key_secret: "test-secret".into(),
        region_id: "cn-hangzhou".into(),
        ..AliyunCredential::default()
    };
    let tool = AliyunApiTool::new(Some(credential))
        .with_endpoint_override("ros", format!("http://{addr}/"));
    let result = tool.execute(
        &json::object([
            ("product", json::string("ros")),
            ("action", json::string("PreviewStack")),
            (
                "params",
                json::object([
                    (
                        "Parameters",
                        json::object([
                            ("InstanceType", json::string("ecs.g7.large")),
                            ("SystemDisk", json::object([("Size", json::number(40))])),
                        ]),
                    ),
                    (
                        "TemplateBody",
                        json::string(
                            r#"{"ROSTemplateFormatVersion":"2015-09-01","Resources":{"Vpc":{"Type":"ALIYUN::ECS::VPC"}}}"#,
                        ),
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
        result.content.contains("\"RequestId\": \"REQ-PARAMS\""),
        "{result:?}"
    );
}

#[test]
fn aliyun_api_expands_ros_parameters_list_like_python_hook() {
    let listener = TcpListener::bind("127.0.0.1:0").expect("bind test server");
    listener
        .set_nonblocking(true)
        .expect("test server should be nonblocking");
    let addr = listener.local_addr().expect("server addr");
    let server = thread::spawn(move || {
        let (mut stream, _) = accept_with_timeout(listener);
        configure_test_stream(&stream);
        let request = read_http_request_with_body(&mut stream);
        assert!(request.contains("Action=UpdateStack"), "{request}");
        assert!(
            request.contains("Parameters.1.ParameterKey=Enable"),
            "{request}"
        );
        assert!(
            request.contains("Parameters.1.ParameterValue=true"),
            "{request}"
        );
        assert!(
            request.contains("Parameters.2.ParameterKey=Tags"),
            "{request}"
        );
        assert!(
            request.contains("Parameters.2.ParameterValue=%5B%22a%22%2C%22b%22%5D"),
            "{request}"
        );
        assert!(!request.contains("Parameters="), "{request}");

        write_http_response(&mut stream, r#"{"RequestId":"REQ-PARAMS-LIST"}"#);
    });

    let credential = AliyunCredential {
        mode: "AK".into(),
        access_key_id: "test-ak".into(),
        access_key_secret: "test-secret".into(),
        region_id: "cn-hangzhou".into(),
        ..AliyunCredential::default()
    };
    let tool = AliyunApiTool::new(Some(credential))
        .with_endpoint_override("ros", format!("http://{addr}/"));
    let result = tool.execute(
        &json::object([
            ("product", json::string("ros")),
            ("action", json::string("UpdateStack")),
            (
                "params",
                json::object([
                    (
                        "Parameters",
                        json::array([
                            json::object([
                                ("ParameterKey", json::string("Enable")),
                                ("ParameterValue", json::bool_value(true)),
                            ]),
                            json::object([
                                ("ParameterKey", json::string("Tags")),
                                (
                                    "ParameterValue",
                                    json::array([json::string("a"), json::string("b")]),
                                ),
                            ]),
                        ]),
                    ),
                    ("StackId", json::string("stack-123")),
                ]),
            ),
            ("region_id", json::string("cn-hangzhou")),
        ]),
        &ToolContext::default(),
    );

    server.join().expect("server thread");

    assert!(!result.is_error, "{result:?}");
    assert!(
        result
            .content
            .contains("\"RequestId\": \"REQ-PARAMS-LIST\""),
        "{result:?}"
    );
}

#[test]
fn aliyun_api_blocks_invalid_ros_template_before_credentials_like_python_hook() {
    let tool = AliyunApiTool::new(None);

    let result = tool.execute(
        &json::object([
            ("product", json::string("ros")),
            ("action", json::string("ValidateTemplate")),
            (
                "params",
                json::object([(
                    "TemplateBody",
                    json::string(
                        r#"{"ROSTemplateFormatVersion":"2015-09-01","Resources":{"Vpc":{"Type":"ALIYUN::VPC::VPC","Properties":{}}}}"#,
                    ),
                )]),
            ),
            ("region_id", json::string("cn-hangzhou")),
        ]),
        &ToolContext::default(),
    );

    assert!(result.is_error, "{result:?}");
    assert!(result.content.contains("ALIYUN::ECS::VPC"), "{result:?}");
    assert!(
        !result
            .content
            .contains("Alibaba Cloud credentials not configured"),
        "{result:?}"
    );
}

#[test]
fn aliyun_api_blocks_invalid_ros_yaml_template_before_credentials_like_python_hook() {
    let tool = AliyunApiTool::new(None);

    let result = tool.execute(
        &json::object([
            ("product", json::string("ros")),
            ("action", json::string("ValidateTemplate")),
            (
                "params",
                json::object([(
                    "TemplateBody",
                    json::string(
                        r#"ROSTemplateFormatVersion: '2015-09-01'
Resources:
  Vpc:
    Type: ALIYUN::VPC::VPC
    Properties:
      VpcName: !Ref Name
"#,
                    ),
                )]),
            ),
            ("region_id", json::string("cn-hangzhou")),
        ]),
        &ToolContext::default(),
    );

    assert!(result.is_error, "{result:?}");
    assert!(result.content.contains("ALIYUN::ECS::VPC"), "{result:?}");
    assert!(
        !result
            .content
            .contains("Alibaba Cloud credentials not configured"),
        "{result:?}"
    );
}

#[test]
fn aliyun_api_reports_missing_credentials_after_version_resolution() {
    let tool = AliyunApiTool::new(None);

    let unknown = tool.execute(
        &json::object([
            ("product", json::string("unknown-svc")),
            ("action", json::string("DoSomething")),
        ]),
        &ToolContext::default(),
    );
    assert!(unknown.is_error, "{unknown:?}");
    assert!(unknown.content.contains("unknown-svc"), "{unknown:?}");

    let no_credentials = tool.execute(
        &json::object([
            ("product", json::string("ecs")),
            ("action", json::string("DescribeInstances")),
        ]),
        &ToolContext::default(),
    );
    assert!(no_credentials.is_error, "{no_credentials:?}");
    assert!(
        no_credentials
            .content
            .contains("Alibaba Cloud credentials not configured"),
        "{no_credentials:?}"
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
    accept_one_with_timeout(&listener)
}

fn accept_one_with_timeout(listener: &TcpListener) -> (std::net::TcpStream, std::net::SocketAddr) {
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

fn unique_temp_dir(name: &str) -> std::path::PathBuf {
    let suffix = SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .expect("time should be monotonic enough")
        .as_nanos();
    std::env::temp_dir().join(format!("{name}-{suffix}"))
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

fn write_http_error_response(stream: &mut impl Write, status: u16, body: &str) {
    write!(
        stream,
        "HTTP/1.1 {status} Error\r\ncontent-type: application/json\r\ncontent-length: {}\r\n\r\n{}",
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
