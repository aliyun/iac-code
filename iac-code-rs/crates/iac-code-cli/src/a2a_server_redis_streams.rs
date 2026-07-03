use super::a2a_payload::new_a2a_server_id;
use super::a2a_redis::{
    ack_a2a_redis_entry, ensure_a2a_redis_consumer_group, prepare_a2a_redis_responses,
    read_a2a_redis_stream_entry, redis_stream_fields, write_a2a_redis_response_payload,
};
use super::a2a_server_args::A2AServerArgs;
use super::a2a_server_runtime::build_a2a_server_runtime;

pub(super) fn run_a2a_redis_streams_server(args: A2AServerArgs) -> Result<(), String> {
    let client = redis::Client::open(args.redis_url.as_str()).map_err(|error| error.to_string())?;
    let mut connection = client.get_connection().map_err(|error| error.to_string())?;
    ensure_a2a_redis_consumer_group(&mut connection, &args)?;
    let mut runtime = build_a2a_server_runtime(&args, "redis-streams")?;
    let consumer_name = format!("consumer-{}", new_a2a_server_id("redis"));

    loop {
        let reply = read_a2a_redis_stream_entry(&mut connection, &args, &consumer_name)?;
        for stream in reply.keys {
            for entry in stream.ids {
                let fields = redis_stream_fields(entry.map)?;
                let responses =
                    prepare_a2a_redis_responses(&entry.id, &fields, &args, &mut runtime)?;
                for response in responses {
                    write_a2a_redis_response_payload(
                        &mut connection,
                        &response.stream,
                        &response.correlation_id,
                        &response.payload,
                        response.final_event,
                    )?;
                }
                ack_a2a_redis_entry(&mut connection, &args, &entry.id)?;
            }
        }
    }
}
