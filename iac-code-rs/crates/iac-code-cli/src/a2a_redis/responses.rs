use iac_code_a2a::transports::redis_streams::{parse_redis_entry, redis_reply_stream, RedisFields};
use iac_code_protocol::json::JsonValue;

use crate::a2a_response::{
    a2a_final_jsonrpc_payload, is_a2a_streaming_request, A2AJsonRpcResponse,
};
use crate::a2a_server_args::A2AServerArgs;
use crate::a2a_server_dispatch::dispatch_a2a_jsonrpc_value;
use crate::a2a_server_runtime::A2AServerRuntime;

#[derive(Clone, Debug, PartialEq)]
pub(crate) struct A2ARedisPreparedResponse {
    pub(crate) stream: String,
    pub(crate) correlation_id: String,
    pub(crate) payload: JsonValue,
    pub(crate) final_event: bool,
}

pub(crate) fn prepare_a2a_redis_responses(
    entry_id: &str,
    fields: &RedisFields,
    args: &A2AServerArgs,
    runtime: &mut A2AServerRuntime,
) -> Result<Vec<A2ARedisPreparedResponse>, String> {
    let message =
        parse_redis_entry(entry_id.to_owned(), fields).map_err(|error| error.to_string())?;
    let reply_stream = redis_reply_stream(fields, &args.response_stream);
    let response = dispatch_a2a_jsonrpc_value(&message.payload, runtime);
    Ok(a2a_redis_prepared_responses_from_jsonrpc(
        reply_stream,
        message.correlation_id,
        response,
        &message.payload,
    ))
}

fn a2a_redis_prepared_responses_from_jsonrpc(
    stream: String,
    correlation_id: String,
    response: A2AJsonRpcResponse,
    request_payload: &JsonValue,
) -> Vec<A2ARedisPreparedResponse> {
    let mut prepared = Vec::new();
    match response {
        A2AJsonRpcResponse::Json(body) => {
            prepared.push(A2ARedisPreparedResponse {
                stream,
                correlation_id,
                payload: body,
                final_event: true,
            });
        }
        A2AJsonRpcResponse::Sse(events) => {
            for event in events {
                prepared.push(A2ARedisPreparedResponse {
                    stream: stream.clone(),
                    correlation_id: correlation_id.clone(),
                    payload: event,
                    final_event: false,
                });
            }
            if is_a2a_streaming_request(request_payload) {
                let final_payload = a2a_final_jsonrpc_payload(request_payload);
                prepared.push(A2ARedisPreparedResponse {
                    stream,
                    correlation_id,
                    payload: final_payload,
                    final_event: true,
                });
            }
        }
    }
    prepared
}
