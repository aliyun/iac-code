use std::collections::BTreeMap;
use std::sync::{Arc, Mutex};

use iac_code_a2a::proto::a2a as a2a_proto;
use iac_code_protocol::{json, json::JsonValue};

use crate::a2a_grpc_convert::{
    a2a_json_push_config_params, a2a_json_send_message_params, a2a_jsonrpc_result_from_response,
    a2a_proto_agent_card_from_json, a2a_proto_list_push_configs_response_from_result,
    a2a_proto_list_tasks_response_from_result, a2a_proto_push_config_from_json,
    a2a_proto_send_message_response_from_result, a2a_proto_stream_response_from_result,
    a2a_proto_task_from_json, a2a_proto_task_state_name, box_status, BoxedStatusResult,
};
use crate::a2a_response::A2AJsonRpcResponse;
use crate::a2a_server_args::A2AServerArgs;
use crate::a2a_server_dispatch::dispatch_a2a_jsonrpc_value;
use crate::a2a_server_grpc::{a2a_grpc_socket_addr, a2a_tokio_runtime};
use crate::a2a_server_runtime::{build_a2a_server_runtime, A2AServerRuntime};

pub(super) fn run_a2a_grpc_server(args: A2AServerArgs) -> Result<(), String> {
    let address = a2a_grpc_socket_addr(&args)?;
    let runtime = Arc::new(Mutex::new(build_a2a_server_runtime(&args, "grpc")?));
    let service = OfficialGrpcA2AService { runtime };
    a2a_tokio_runtime()?.block_on(async move {
        tonic::transport::Server::builder()
            .add_service(a2a_proto::a2a_service_server::A2aServiceServer::new(
                service,
            ))
            .serve(address)
            .await
            .map_err(|error| error.to_string())
    })
}

#[derive(Clone)]
struct OfficialGrpcA2AService {
    runtime: Arc<Mutex<A2AServerRuntime>>,
}

#[tonic::async_trait]
impl a2a_proto::a2a_service_server::A2aService for OfficialGrpcA2AService {
    async fn send_message(
        &self,
        request: tonic::Request<a2a_proto::SendMessageRequest>,
    ) -> Result<tonic::Response<a2a_proto::SendMessageResponse>, tonic::Status> {
        let result = self
            .dispatch_jsonrpc_result(
                "SendMessage",
                a2a_json_send_message_params(request.into_inner()),
            )
            .map_err(|status| *status)?;
        Ok(tonic::Response::new(
            a2a_proto_send_message_response_from_result(&result).map_err(|status| *status)?,
        ))
    }

    type SendStreamingMessageStream = std::pin::Pin<
        Box<
            dyn tokio_stream::Stream<Item = Result<a2a_proto::StreamResponse, tonic::Status>>
                + Send,
        >,
    >;

    async fn send_streaming_message(
        &self,
        request: tonic::Request<a2a_proto::SendMessageRequest>,
    ) -> Result<tonic::Response<Self::SendStreamingMessageStream>, tonic::Status> {
        let events = self
            .dispatch_jsonrpc_events(
                "SendStreamingMessage",
                a2a_json_send_message_params(request.into_inner()),
            )
            .map_err(|status| *status)?;
        let responses = events
            .iter()
            .map(a2a_proto_stream_response_from_result)
            .collect::<BoxedStatusResult<Vec<_>>>()
            .map_err(|status| *status)?
            .into_iter()
            .map(Ok);
        Ok(tonic::Response::new(Box::pin(tokio_stream::iter(
            responses,
        ))))
    }

    async fn get_task(
        &self,
        request: tonic::Request<a2a_proto::GetTaskRequest>,
    ) -> Result<tonic::Response<a2a_proto::Task>, tonic::Status> {
        let request = request.into_inner();
        let mut params = BTreeMap::from([("id".to_owned(), json::string(request.id))]);
        if let Some(history_length) = request.history_length {
            params.insert("historyLength".to_owned(), json::number(history_length));
        }
        let result = self
            .dispatch_jsonrpc_result("GetTask", JsonValue::Object(params))
            .map_err(|status| *status)?;
        Ok(tonic::Response::new(a2a_proto_task_from_json(&result)))
    }

    async fn list_tasks(
        &self,
        request: tonic::Request<a2a_proto::ListTasksRequest>,
    ) -> Result<tonic::Response<a2a_proto::ListTasksResponse>, tonic::Status> {
        let request = request.into_inner();
        let mut params = BTreeMap::new();
        if !request.context_id.is_empty() {
            params.insert("contextId".to_owned(), json::string(request.context_id));
        }
        if let Some(status) = a2a_proto_task_state_name(request.status) {
            if status != "TASK_STATE_UNSPECIFIED" {
                params.insert("status".to_owned(), json::string(status));
            }
        }
        if let Some(page_size) = request.page_size {
            params.insert("pageSize".to_owned(), json::number(page_size));
        }
        if !request.page_token.is_empty() {
            params.insert("pageToken".to_owned(), json::string(request.page_token));
        }
        if let Some(include_artifacts) = request.include_artifacts {
            params.insert(
                "includeArtifacts".to_owned(),
                json::bool_value(include_artifacts),
            );
        }
        let result = self
            .dispatch_jsonrpc_result("ListTasks", JsonValue::Object(params))
            .map_err(|status| *status)?;
        Ok(tonic::Response::new(
            a2a_proto_list_tasks_response_from_result(&result),
        ))
    }

    async fn cancel_task(
        &self,
        request: tonic::Request<a2a_proto::CancelTaskRequest>,
    ) -> Result<tonic::Response<a2a_proto::Task>, tonic::Status> {
        let request = request.into_inner();
        let result = self
            .dispatch_jsonrpc_result(
                "CancelTask",
                json::object([("id", json::string(request.id))]),
            )
            .map_err(|status| *status)?;
        Ok(tonic::Response::new(a2a_proto_task_from_json(&result)))
    }

    type SubscribeToTaskStream = std::pin::Pin<
        Box<
            dyn tokio_stream::Stream<Item = Result<a2a_proto::StreamResponse, tonic::Status>>
                + Send,
        >,
    >;

    async fn subscribe_to_task(
        &self,
        request: tonic::Request<a2a_proto::SubscribeToTaskRequest>,
    ) -> Result<tonic::Response<Self::SubscribeToTaskStream>, tonic::Status> {
        let request = request.into_inner();
        let events = self
            .dispatch_jsonrpc_events(
                "SubscribeToTask",
                json::object([("id", json::string(request.id))]),
            )
            .map_err(|status| *status)?;
        let responses = events
            .iter()
            .map(a2a_proto_stream_response_from_result)
            .collect::<BoxedStatusResult<Vec<_>>>()
            .map_err(|status| *status)?
            .into_iter()
            .map(Ok);
        Ok(tonic::Response::new(Box::pin(tokio_stream::iter(
            responses,
        ))))
    }

    async fn create_task_push_notification_config(
        &self,
        request: tonic::Request<a2a_proto::TaskPushNotificationConfig>,
    ) -> Result<tonic::Response<a2a_proto::TaskPushNotificationConfig>, tonic::Status> {
        let result = self
            .dispatch_jsonrpc_result(
                "CreateTaskPushNotificationConfig",
                a2a_json_push_config_params(request.into_inner()),
            )
            .map_err(|status| *status)?;
        Ok(tonic::Response::new(a2a_proto_push_config_from_json(
            &result,
        )))
    }

    async fn get_task_push_notification_config(
        &self,
        request: tonic::Request<a2a_proto::GetTaskPushNotificationConfigRequest>,
    ) -> Result<tonic::Response<a2a_proto::TaskPushNotificationConfig>, tonic::Status> {
        let request = request.into_inner();
        let result = self
            .dispatch_jsonrpc_result(
                "GetTaskPushNotificationConfig",
                json::object([
                    ("taskId", json::string(request.task_id)),
                    ("id", json::string(request.id)),
                ]),
            )
            .map_err(|status| *status)?;
        Ok(tonic::Response::new(a2a_proto_push_config_from_json(
            &result,
        )))
    }

    async fn list_task_push_notification_configs(
        &self,
        request: tonic::Request<a2a_proto::ListTaskPushNotificationConfigsRequest>,
    ) -> Result<tonic::Response<a2a_proto::ListTaskPushNotificationConfigsResponse>, tonic::Status>
    {
        let request = request.into_inner();
        let mut params = BTreeMap::from([("taskId".to_owned(), json::string(request.task_id))]);
        if request.page_size > 0 {
            params.insert("pageSize".to_owned(), json::number(request.page_size));
        }
        if !request.page_token.is_empty() {
            params.insert("pageToken".to_owned(), json::string(request.page_token));
        }
        let result = self
            .dispatch_jsonrpc_result("ListTaskPushNotificationConfigs", JsonValue::Object(params))
            .map_err(|status| *status)?;
        Ok(tonic::Response::new(
            a2a_proto_list_push_configs_response_from_result(&result),
        ))
    }

    async fn get_extended_agent_card(
        &self,
        _request: tonic::Request<a2a_proto::GetExtendedAgentCardRequest>,
    ) -> Result<tonic::Response<a2a_proto::AgentCard>, tonic::Status> {
        let runtime = self
            .runtime
            .lock()
            .map_err(|_| tonic::Status::internal("A2A runtime lock poisoned"))?;
        Ok(tonic::Response::new(a2a_proto_agent_card_from_json(
            runtime.card(),
        )))
    }

    async fn delete_task_push_notification_config(
        &self,
        request: tonic::Request<a2a_proto::DeleteTaskPushNotificationConfigRequest>,
    ) -> Result<tonic::Response<()>, tonic::Status> {
        let request = request.into_inner();
        self.dispatch_jsonrpc_result(
            "DeleteTaskPushNotificationConfig",
            json::object([
                ("taskId", json::string(request.task_id)),
                ("id", json::string(request.id)),
            ]),
        )
        .map_err(|status| *status)?;
        Ok(tonic::Response::new(()))
    }
}

impl OfficialGrpcA2AService {
    fn dispatch_jsonrpc_result(
        &self,
        method: &str,
        params: JsonValue,
    ) -> BoxedStatusResult<JsonValue> {
        let response = self.dispatch_jsonrpc_response(method, params)?;
        match response {
            A2AJsonRpcResponse::Json(response) => a2a_jsonrpc_result_from_response(response),
            A2AJsonRpcResponse::Sse(mut events) => events
                .drain(..)
                .next()
                .ok_or_else(|| box_status(tonic::Status::internal("A2A stream returned no events")))
                .and_then(a2a_jsonrpc_result_from_response),
        }
    }

    fn dispatch_jsonrpc_events(
        &self,
        method: &str,
        params: JsonValue,
    ) -> BoxedStatusResult<Vec<JsonValue>> {
        let response = self.dispatch_jsonrpc_response(method, params)?;
        match response {
            A2AJsonRpcResponse::Json(response) => {
                Ok(vec![a2a_jsonrpc_result_from_response(response)?])
            }
            A2AJsonRpcResponse::Sse(events) => events
                .into_iter()
                .map(a2a_jsonrpc_result_from_response)
                .collect(),
        }
    }

    fn dispatch_jsonrpc_response(
        &self,
        method: &str,
        params: JsonValue,
    ) -> BoxedStatusResult<A2AJsonRpcResponse> {
        let payload = json::object([
            ("jsonrpc", json::string("2.0")),
            ("id", json::string(format!("grpc-{method}"))),
            ("method", json::string(method)),
            ("params", params),
        ]);
        let mut runtime = self
            .runtime
            .lock()
            .map_err(|_| box_status(tonic::Status::internal("A2A runtime lock poisoned")))?;
        Ok(dispatch_a2a_jsonrpc_value(&payload, &mut runtime))
    }
}
