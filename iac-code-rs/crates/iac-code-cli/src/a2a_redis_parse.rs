use std::collections::{BTreeMap, HashMap};

use iac_code_a2a::push_queue::RedisStreamEntry;

pub(super) fn redis_stream_reply_entries(
    reply: redis::streams::StreamReadReply,
) -> Result<Vec<RedisStreamEntry>, String> {
    let mut entries = Vec::new();
    for stream in reply.keys {
        for entry in stream.ids {
            entries.push(RedisStreamEntry {
                entry_id: entry.id,
                fields: redis_hash_stream_fields(entry.map)?,
            });
        }
    }
    Ok(entries)
}

fn redis_hash_stream_fields(
    map: HashMap<String, redis::Value>,
) -> Result<BTreeMap<String, String>, String> {
    map.into_iter()
        .map(|(key, value)| Ok((key, redis_value_string(&value)?)))
        .collect()
}

pub(super) fn redis_xautoclaim_entries_from_value(
    value: redis::Value,
) -> Result<Vec<RedisStreamEntry>, String> {
    let redis::Value::Array(items) = value else {
        return Err("Redis XAUTOCLAIM reply must be an array.".to_owned());
    };
    let Some(entries_value) = items.get(1) else {
        return Ok(Vec::new());
    };
    let redis::Value::Array(entries) = entries_value else {
        return Ok(Vec::new());
    };
    entries.iter().map(redis_stream_entry_from_value).collect()
}

fn redis_stream_entry_from_value(value: &redis::Value) -> Result<RedisStreamEntry, String> {
    let redis::Value::Array(parts) = value else {
        return Err("Redis stream entry must be an array.".to_owned());
    };
    let [entry_id, fields] = parts.as_slice() else {
        return Err("Redis stream entry must contain id and fields.".to_owned());
    };
    Ok(RedisStreamEntry {
        entry_id: redis_value_string(entry_id)?,
        fields: redis_stream_fields_from_value(fields)?,
    })
}

fn redis_stream_fields_from_value(
    value: &redis::Value,
) -> Result<BTreeMap<String, String>, String> {
    match value {
        redis::Value::Array(items) => {
            if items.len() % 2 != 0 {
                return Err("Redis stream entry fields must contain key-value pairs.".to_owned());
            }
            let mut fields = BTreeMap::new();
            for pair in items.chunks_exact(2) {
                fields.insert(redis_value_string(&pair[0])?, redis_value_string(&pair[1])?);
            }
            Ok(fields)
        }
        redis::Value::Map(items) => items
            .iter()
            .map(|(key, value)| Ok((redis_value_string(key)?, redis_value_string(value)?)))
            .collect(),
        _ => Err("Redis stream entry fields must be an array or map.".to_owned()),
    }
}

fn redis_value_string(value: &redis::Value) -> Result<String, String> {
    match value {
        redis::Value::BulkString(bytes) => {
            String::from_utf8(bytes.clone()).map_err(|error| error.to_string())
        }
        redis::Value::SimpleString(value) => Ok(value.clone()),
        redis::Value::Okay => Ok("OK".to_owned()),
        redis::Value::Int(value) => Ok(value.to_string()),
        _ => Err(format!("Redis value is not a string: {value:?}")),
    }
}
