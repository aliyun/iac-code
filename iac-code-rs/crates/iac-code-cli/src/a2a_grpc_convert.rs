use iac_code_protocol::json::JsonValue;

use crate::json_utils::{json_number_i64_field, json_object_field, json_string_field};

pub(super) use crate::a2a_grpc_json::{a2a_json_push_config_params, a2a_json_send_message_params};
pub(super) use crate::a2a_grpc_proto::{
    a2a_proto_agent_card_from_json, a2a_proto_list_push_configs_response_from_result,
    a2a_proto_list_tasks_response_from_result, a2a_proto_push_config_from_json,
    a2a_proto_send_message_response_from_result, a2a_proto_stream_response_from_result,
    a2a_proto_task_from_json, a2a_proto_task_state_name,
};

pub(super) type BoxedStatusResult<T> = Result<T, Box<tonic::Status>>;

pub(super) fn box_status(status: tonic::Status) -> Box<tonic::Status> {
    Box::new(status)
}

pub(super) fn a2a_jsonrpc_result_from_response(
    response: JsonValue,
) -> BoxedStatusResult<JsonValue> {
    if let Some(error) = json_object_field(&response, "error") {
        let message = json_string_field(error, "message").unwrap_or("A2A gRPC request failed");
        let status = match json_number_i64_field(error, "code") {
            Some(-32601) => tonic::Status::unimplemented(message.to_owned()),
            Some(-32602) => tonic::Status::invalid_argument(message.to_owned()),
            Some(-32000) => tonic::Status::not_found(message.to_owned()),
            _ => tonic::Status::internal(message.to_owned()),
        };
        return Err(box_status(status));
    }
    json_object_field(&response, "result")
        .cloned()
        .ok_or_else(|| {
            box_status(tonic::Status::internal(
                "A2A JSON-RPC response is missing result",
            ))
        })
}
