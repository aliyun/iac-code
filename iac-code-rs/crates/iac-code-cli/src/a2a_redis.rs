mod responses;
mod server_io;
mod store;

pub(super) use responses::prepare_a2a_redis_responses;
#[allow(unused_imports)]
pub(super) use responses::A2ARedisPreparedResponse;
pub(super) use server_io::{
    ack_a2a_redis_entry, ensure_a2a_redis_consumer_group, read_a2a_redis_stream_entry,
    redis_stream_fields, write_a2a_redis_response_payload,
};
pub(super) use store::RedisConnectionPushStore;

#[cfg(test)]
mod tests {
    use super::*;
    use crate::a2a_redis_parse::redis_xautoclaim_entries_from_value;
    use crate::a2a_server_args::A2AServerArgs;
    use crate::a2a_server_runtime::build_a2a_server_runtime;
    use crate::json_utils::json_string_field;
    use iac_code_protocol::{json, json::JsonValue};

    #[test]
    fn redis_xautoclaim_entries_from_value_accepts_stream_entry_shape_like_python() {
        let reply = redis::Value::Array(vec![
            redis::Value::BulkString(b"0-0".to_vec()),
            redis::Value::Array(vec![redis::Value::Array(vec![
                redis::Value::BulkString(b"1-0".to_vec()),
                redis::Value::Array(vec![
                    redis::Value::BulkString(b"job".to_vec()),
                    redis::Value::BulkString(br#"{"jobId":"job-1"}"#.to_vec()),
                ]),
            ])]),
            redis::Value::Array(Vec::new()),
        ]);

        let entries = redis_xautoclaim_entries_from_value(reply).expect("entries parse");

        assert_eq!(entries.len(), 1);
        assert_eq!(entries[0].entry_id, "1-0");
        assert_eq!(
            entries[0].fields.get("job").map(String::as_str),
            Some(r#"{"jobId":"job-1"}"#)
        );
    }

    #[test]
    fn a2a_redis_streams_prepares_response_payloads_without_live_redis() {
        let args = A2AServerArgs {
            transport: "redis-streams".to_owned(),
            redis_url: "redis://127.0.0.1:6379".to_owned(),
            ..A2AServerArgs::default()
        };
        let mut runtime =
            build_a2a_server_runtime(&args, "redis-streams").expect("runtime should build");
        let request = json::object([
            ("jsonrpc", json::string("2.0")),
            ("id", json::string("card-redis")),
            ("method", json::string("GetExtendedAgentCard")),
            ("params", json::object(Vec::<(&str, JsonValue)>::new())),
        ]);
        let fields = iac_code_a2a::transports::redis_streams::redis_request_fields(
            "corr-redis-1",
            "custom:responses",
            &request,
        );

        let responses = prepare_a2a_redis_responses("1-0", &fields, &args, &mut runtime)
            .expect("redis responses should be prepared");

        assert_eq!(responses.len(), 1);
        assert_eq!(responses[0].stream, "custom:responses");
        assert_eq!(responses[0].correlation_id, "corr-redis-1");
        assert!(responses[0].final_event);
        assert_eq!(
            json_string_field(&responses[0].payload, "id"),
            Some("card-redis")
        );
    }

    #[test]
    fn a2a_redis_streams_marks_send_streaming_message_final_event() {
        let args = A2AServerArgs {
            transport: "redis-streams".to_owned(),
            redis_url: "redis://127.0.0.1:6379".to_owned(),
            ..A2AServerArgs::default()
        };
        let mut runtime =
            build_a2a_server_runtime(&args, "redis-streams").expect("runtime should build");
        let request = json::object([
            ("jsonrpc", json::string("2.0")),
            ("id", json::string("stream-redis")),
            ("method", json::string("SendStreamingMessage")),
            (
                "params",
                json::object([
                    (
                        "message",
                        json::object([
                            ("messageId", json::string("msg-stream-redis")),
                            ("taskId", json::string("task-stream-redis")),
                            ("contextId", json::string("ctx-stream-redis")),
                            ("role", json::string("ROLE_USER")),
                            (
                                "parts",
                                json::array([json::object([(
                                    "text",
                                    json::string("hello redis stream"),
                                )])]),
                            ),
                            (
                                "metadata",
                                json::object([(
                                    "iac_code",
                                    json::object([(
                                        "cwd",
                                        json::string(env!("CARGO_MANIFEST_DIR")),
                                    )]),
                                )]),
                            ),
                        ]),
                    ),
                    (
                        "configuration",
                        json::object([("returnImmediately", json::bool_value(true))]),
                    ),
                ]),
            ),
        ]);
        let fields = iac_code_a2a::transports::redis_streams::redis_request_fields(
            "corr-stream-redis",
            "stream:responses",
            &request,
        );

        let responses = prepare_a2a_redis_responses("1-0", &fields, &args, &mut runtime)
            .expect("redis responses should be prepared");

        assert_eq!(responses.len(), 2);
        assert!(!responses[0].final_event);
        assert!(responses[1].final_event);
        assert_eq!(
            json_string_field(&responses[1].payload, "id"),
            Some("stream-redis")
        );
    }
}
