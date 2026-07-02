use std::collections::BTreeMap;

use iac_code_a2a::push_queue::{PushQueueError, RedisPushStore, RedisStreamEntry};

use crate::a2a_redis_parse::{redis_stream_reply_entries, redis_xautoclaim_entries_from_value};

pub(crate) struct RedisConnectionPushStore {
    connection: redis::Connection,
}

impl RedisConnectionPushStore {
    pub(crate) fn from_url(url: &str) -> Result<Self, String> {
        let client = redis::Client::open(url).map_err(|error| error.to_string())?;
        let connection = client.get_connection().map_err(|error| error.to_string())?;
        Ok(Self { connection })
    }
}

impl RedisPushStore for RedisConnectionPushStore {
    fn xgroup_create(
        &mut self,
        stream: &str,
        group: &str,
        id: &str,
        mkstream: bool,
    ) -> Result<(), PushQueueError> {
        let mut command = redis::cmd("XGROUP");
        command.arg("CREATE").arg(stream).arg(group).arg(id);
        if mkstream {
            command.arg("MKSTREAM");
        }
        command
            .query::<()>(&mut self.connection)
            .map_err(redis_push_error)
    }

    fn xadd(
        &mut self,
        stream: &str,
        fields: BTreeMap<String, String>,
    ) -> Result<String, PushQueueError> {
        let mut command = redis::cmd("XADD");
        command.arg(stream).arg("*");
        for (key, value) in fields {
            command.arg(key).arg(value);
        }
        command
            .query::<String>(&mut self.connection)
            .map_err(redis_push_error)
    }

    fn xreadgroup(
        &mut self,
        group: &str,
        consumer: &str,
        stream: &str,
        count: usize,
        block_ms: u64,
    ) -> Result<Vec<RedisStreamEntry>, PushQueueError> {
        let reply = redis::cmd("XREADGROUP")
            .arg("GROUP")
            .arg(group)
            .arg(consumer)
            .arg("COUNT")
            .arg(count)
            .arg("BLOCK")
            .arg(block_ms)
            .arg("STREAMS")
            .arg(stream)
            .arg(">")
            .query::<redis::streams::StreamReadReply>(&mut self.connection)
            .map_err(redis_push_error)?;
        redis_stream_reply_entries(reply).map_err(PushQueueError::Redis)
    }

    fn xautoclaim(
        &mut self,
        stream: &str,
        group: &str,
        consumer: &str,
        min_idle_time_ms: u64,
        start_id: &str,
        count: usize,
    ) -> Result<Vec<RedisStreamEntry>, PushQueueError> {
        let reply = redis::cmd("XAUTOCLAIM")
            .arg(stream)
            .arg(group)
            .arg(consumer)
            .arg(min_idle_time_ms)
            .arg(start_id)
            .arg("COUNT")
            .arg(count)
            .query::<redis::Value>(&mut self.connection)
            .map_err(redis_push_error)?;
        redis_xautoclaim_entries_from_value(reply).map_err(PushQueueError::Redis)
    }

    fn xack(&mut self, stream: &str, group: &str, ids: &[String]) -> Result<usize, PushQueueError> {
        let mut command = redis::cmd("XACK");
        command.arg(stream).arg(group);
        for id in ids {
            command.arg(id);
        }
        command
            .query::<usize>(&mut self.connection)
            .map_err(redis_push_error)
    }

    fn zadd(&mut self, key: &str, member: String, score: f64) -> Result<usize, PushQueueError> {
        redis::cmd("ZADD")
            .arg(key)
            .arg(score)
            .arg(member)
            .query::<usize>(&mut self.connection)
            .map_err(redis_push_error)
    }

    fn zrangebyscore(
        &mut self,
        key: &str,
        min: f64,
        max: f64,
        start: usize,
        count: usize,
    ) -> Result<Vec<String>, PushQueueError> {
        redis::cmd("ZRANGEBYSCORE")
            .arg(key)
            .arg(redis_score_bound(min, "-inf", "+inf"))
            .arg(redis_score_bound(max, "-inf", "+inf"))
            .arg("LIMIT")
            .arg(start)
            .arg(count)
            .query::<Vec<String>>(&mut self.connection)
            .map_err(redis_push_error)
    }

    fn zrem(&mut self, key: &str, members: &[String]) -> Result<usize, PushQueueError> {
        let mut command = redis::cmd("ZREM");
        command.arg(key);
        for member in members {
            command.arg(member);
        }
        command
            .query::<usize>(&mut self.connection)
            .map_err(redis_push_error)
    }
}

fn redis_push_error(error: redis::RedisError) -> PushQueueError {
    PushQueueError::Redis(error.to_string())
}

fn redis_score_bound(value: f64, neg_inf: &str, pos_inf: &str) -> String {
    if value.is_infinite() && value.is_sign_negative() {
        neg_inf.to_owned()
    } else if value.is_infinite() && value.is_sign_positive() {
        pos_inf.to_owned()
    } else {
        value.to_string()
    }
}
