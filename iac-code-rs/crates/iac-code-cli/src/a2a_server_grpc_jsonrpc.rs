use std::sync::{Arc, Mutex};

use iac_code_a2a::proto::grpc_jsonrpc as grpc_jsonrpc_proto;
use iac_code_protocol::json::JsonValue;

use crate::a2a_grpc_convert::{box_status, BoxedStatusResult};
use crate::a2a_response::A2AJsonRpcResponse;
use crate::a2a_server_args::A2AServerArgs;
use crate::a2a_server_dispatch::dispatch_a2a_jsonrpc_body;
use crate::a2a_server_grpc::{a2a_grpc_socket_addr, a2a_tokio_runtime};
use crate::a2a_server_runtime::{build_a2a_server_runtime, A2AServerRuntime};
use crate::jsonrpc_payload::jsonrpc_error;

pub(super) fn run_a2a_grpc_jsonrpc_server(args: A2AServerArgs) -> Result<(), String> {
    let address = a2a_grpc_socket_addr(&args)?;
    let runtime = Arc::new(Mutex::new(build_a2a_server_runtime(&args, "grpc-jsonrpc")?));
    let service = GrpcJsonRpcA2AService { runtime };
    a2a_tokio_runtime()?.block_on(async move {
        tonic::transport::Server::builder()
            .add_service(grpc_jsonrpc_proto::a2a_json_rpc_server::A2aJsonRpcServer::new(service))
            .serve(address)
            .await
            .map_err(|error| error.to_string())
    })
}

#[derive(Clone)]
struct GrpcJsonRpcA2AService {
    runtime: Arc<Mutex<A2AServerRuntime>>,
}

#[tonic::async_trait]
impl grpc_jsonrpc_proto::a2a_json_rpc_server::A2aJsonRpc for GrpcJsonRpcA2AService {
    async fn send(
        &self,
        request: tonic::Request<grpc_jsonrpc_proto::JsonRpcEnvelope>,
    ) -> Result<tonic::Response<grpc_jsonrpc_proto::JsonRpcEnvelope>, tonic::Status> {
        let response = self
            .dispatch_envelope(request.into_inner())
            .map_err(|status| *status)?;
        let body = match response {
            A2AJsonRpcResponse::Json(body) => body,
            A2AJsonRpcResponse::Sse(mut events) => events
                .drain(..)
                .next()
                .unwrap_or_else(|| jsonrpc_error(JsonValue::Null, -32603, "Empty stream")),
        };
        Ok(tonic::Response::new(grpc_jsonrpc_proto::JsonRpcEnvelope {
            payload: body.to_compact_json().into_bytes(),
            r#final: false,
        }))
    }

    type StreamStream = std::pin::Pin<
        Box<
            dyn tokio_stream::Stream<
                    Item = Result<grpc_jsonrpc_proto::JsonRpcEnvelope, tonic::Status>,
                > + Send,
        >,
    >;

    async fn stream(
        &self,
        request: tonic::Request<grpc_jsonrpc_proto::JsonRpcEnvelope>,
    ) -> Result<tonic::Response<Self::StreamStream>, tonic::Status> {
        let response = self
            .dispatch_envelope(request.into_inner())
            .map_err(|status| *status)?;
        let mut envelopes = Vec::new();
        match response {
            A2AJsonRpcResponse::Json(body) => {
                envelopes.push(Ok(grpc_jsonrpc_proto::JsonRpcEnvelope {
                    payload: body.to_compact_json().into_bytes(),
                    r#final: false,
                }))
            }
            A2AJsonRpcResponse::Sse(events) => {
                for event in events {
                    envelopes.push(Ok(grpc_jsonrpc_proto::JsonRpcEnvelope {
                        payload: event.to_compact_json().into_bytes(),
                        r#final: false,
                    }));
                }
            }
        }
        Ok(tonic::Response::new(Box::pin(tokio_stream::iter(
            envelopes,
        ))))
    }
}

impl GrpcJsonRpcA2AService {
    fn dispatch_envelope(
        &self,
        envelope: grpc_jsonrpc_proto::JsonRpcEnvelope,
    ) -> BoxedStatusResult<A2AJsonRpcResponse> {
        let body = std::str::from_utf8(&envelope.payload)
            .map_err(|error| box_status(tonic::Status::invalid_argument(error.to_string())))?;
        let mut runtime = self
            .runtime
            .lock()
            .map_err(|_| box_status(tonic::Status::internal("A2A runtime lock poisoned")))?;
        Ok(dispatch_a2a_jsonrpc_body(body, &mut runtime))
    }
}
