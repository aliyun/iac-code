use std::collections::HashMap;

use iac_code_a2a::transports::redis_streams::{redis_response_fields, RedisFields};
use iac_code_protocol::json::JsonValue;

use crate::a2a_server_args::A2AServerArgs;

pub(crate) fn ensure_a2a_redis_consumer_group(
    connection: &mut redis::Connection,
    args: &A2AServerArgs,
) -> Result<(), String> {
    let result = redis::cmd("XGROUP")
        .arg("CREATE")
        .arg(&args.request_stream)
        .arg(&args.consumer_group)
        .arg("0-0")
        .arg("MKSTREAM")
        .query::<()>(connection);
    match result {
        Ok(()) => Ok(()),
        Err(error) if error.to_string().contains("BUSYGROUP") => Ok(()),
        Err(error) => Err(error.to_string()),
    }
}

pub(crate) fn read_a2a_redis_stream_entry(
    connection: &mut redis::Connection,
    args: &A2AServerArgs,
    consumer_name: &str,
) -> Result<redis::streams::StreamReadReply, String> {
    redis::cmd("XREADGROUP")
        .arg("GROUP")
        .arg(&args.consumer_group)
        .arg(consumer_name)
        .arg("COUNT")
        .arg(1)
        .arg("BLOCK")
        .arg(100)
        .arg("STREAMS")
        .arg(&args.request_stream)
        .arg(">")
        .query::<redis::streams::StreamReadReply>(connection)
        .map_err(|error| error.to_string())
}

pub(crate) fn redis_stream_fields(
    map: HashMap<String, redis::Value>,
) -> Result<RedisFields, String> {
    map.into_iter()
        .map(|(key, value)| {
            let value =
                redis::from_redis_value::<String>(&value).map_err(|error| error.to_string())?;
            Ok((key.into_bytes(), value.into_bytes()))
        })
        .collect()
}

pub(crate) fn write_a2a_redis_response_payload(
    connection: &mut redis::Connection,
    stream: &str,
    correlation_id: &str,
    payload: &JsonValue,
    final_event: bool,
) -> Result<(), String> {
    let fields = redis_response_fields(correlation_id, payload, final_event);
    let mut command = redis::cmd("XADD");
    command.arg(stream).arg("*");
    for (key, value) in fields {
        command.arg(key).arg(value);
    }
    command
        .query::<String>(connection)
        .map(|_| ())
        .map_err(|error| error.to_string())
}

pub(crate) fn ack_a2a_redis_entry(
    connection: &mut redis::Connection,
    args: &A2AServerArgs,
    entry_id: &str,
) -> Result<(), String> {
    redis::cmd("XACK")
        .arg(&args.request_stream)
        .arg(&args.consumer_group)
        .arg(entry_id)
        .query::<i64>(connection)
        .map(|_| ())
        .map_err(|error| error.to_string())
}
