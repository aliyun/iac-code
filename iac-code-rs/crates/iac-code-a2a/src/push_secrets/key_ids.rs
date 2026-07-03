use std::time::{SystemTime, UNIX_EPOCH};

use ring::rand;
use ring::rand::SecureRandom;

pub(in crate::push_secrets) fn new_key_id() -> String {
    let mut random = [0_u8; 6];
    let _ = rand::SystemRandom::new().fill(&mut random);
    format!("push-{}-{}", current_time_seconds(), hex_lower(&random))
}

pub(in crate::push_secrets) fn current_time_seconds() -> u64 {
    SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .map_or(0, |duration| duration.as_secs())
}

fn hex_lower(bytes: &[u8]) -> String {
    let mut output = String::with_capacity(bytes.len() * 2);
    for byte in bytes {
        use std::fmt::Write;
        write!(output, "{byte:02x}").expect("writing to String should not fail");
    }
    output
}
