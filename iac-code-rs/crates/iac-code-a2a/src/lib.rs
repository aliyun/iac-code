pub mod agent_card;
pub mod app;
pub mod artifacts;
pub mod client;
pub mod dispatcher;
pub mod events;
pub mod exposure;
pub mod metrics;
pub mod parts;
pub mod persistence;
mod private_fs;
pub mod proto {
    pub mod a2a {
        tonic::include_proto!("lf.a2a.v1");
    }

    pub mod grpc_jsonrpc {
        tonic::include_proto!("iac_code.a2a.transports.proto");
    }
}
pub mod push;
mod push_config;
mod push_config_store;
mod push_endpoint;
pub mod push_queue;
mod push_queue_job;
mod push_queue_local;
mod push_queue_redis;
pub mod push_secrets;
mod push_sender;
pub mod push_worker;
pub mod router;
pub mod server;
pub mod signing;
pub mod task_store;
pub mod transport;
pub mod transports;
pub mod types;

pub const CRATE_NAME: &str = "iac-code-a2a";
