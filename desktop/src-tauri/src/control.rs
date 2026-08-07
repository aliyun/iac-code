use anyhow::{bail, Context, Result};
use serde_json::Value;
use std::collections::{HashMap, HashSet};
use std::io::{Read, Write};

const MAX_CONTROL_MESSAGE_BYTES: usize = 1024 * 1024;

const CHILD_GROUP_KINDS: &[&str] = &[
    "bash",
    "renderer",
    "prerequisite",
    "prerequisite-probe",
    "desktop-diagnostics",
    "infraguard",
    "mcp",
];

#[derive(Clone, Debug, Eq, PartialEq)]
struct ChildGroup {
    pgid: i64,
    kind: String,
}

/// Generation-scoped bookkeeping for POSIX Desktop child guardians.
///
/// The Host deliberately never signals a recorded PGID. The guardian owns
/// liveness and group teardown; this set only closes the register/complete ACK
/// protocol and is dropped when the sidecar carrier reaches EOF.
#[derive(Debug)]
pub struct ChildGroupTracker {
    generation: u64,
    active: HashMap<u64, ChildGroup>,
    completed: HashSet<(u64, i64)>,
}

impl ChildGroupTracker {
    pub fn new(generation: u64) -> Self {
        Self {
            generation,
            active: HashMap::new(),
            completed: HashSet::new(),
        }
    }

    pub fn dispatch(&mut self, message: &Value) -> Option<Value> {
        match message.get("type").and_then(Value::as_str) {
            Some("register-child-group") => Some(self.register(message)),
            Some("complete-child-group") => Some(self.complete(message)),
            _ => None,
        }
    }

    pub fn clear_on_sidecar_exit(&mut self) {
        // The sidecar-owned guardian control writers close with the sidecar.
        // Guardians then reap their own groups, so signalling the stored PGIDs
        // here would introduce a PID-reuse race.
        self.active.clear();
    }

    #[cfg(test)]
    fn active_len(&self) -> usize {
        self.active.len()
    }

    fn identity(&self, message: &Value) -> std::result::Result<(u64, i64), &'static str> {
        if message.get("sidecarGeneration").and_then(Value::as_u64) != Some(self.generation) {
            return Err("stale sidecar generation");
        }
        let registration_id = message
            .get("registrationId")
            .and_then(Value::as_u64)
            .filter(|value| *value > 0)
            .ok_or("invalid registration id")?;
        let pgid = message
            .get("pgid")
            .and_then(Value::as_i64)
            .filter(|value| *value > 0)
            .ok_or("invalid process group")?;
        Ok((registration_id, pgid))
    }

    fn register(&mut self, message: &Value) -> Value {
        let response = |registration_id: Option<u64>, error: Option<&str>| {
            let mut response = serde_json::json!({
                "type": "child-group-registered",
                "registrationId": registration_id,
                "sidecarGeneration": self.generation,
            });
            if let Some(error) = error {
                response["error"] = Value::String(error.to_string());
            }
            response
        };
        let registration_id = message.get("registrationId").and_then(Value::as_u64);
        let (registration_id, pgid) = match self.identity(message) {
            Ok(identity) => identity,
            Err(error) => return response(registration_id, Some(error)),
        };
        let Some(kind) = message.get("kind").and_then(Value::as_str) else {
            return response(Some(registration_id), Some("missing child-group kind"));
        };
        if !CHILD_GROUP_KINDS.contains(&kind) {
            return response(Some(registration_id), Some("unsupported child-group kind"));
        }
        let child_group = ChildGroup {
            pgid,
            kind: kind.to_string(),
        };
        if self.completed.contains(&(registration_id, pgid)) {
            return response(
                Some(registration_id),
                Some("child group is already complete"),
            );
        }
        if let Some(existing) = self.active.get(&registration_id) {
            if existing != &child_group {
                return response(Some(registration_id), Some("registration id was reused"));
            }
        } else {
            self.active.insert(registration_id, child_group);
        }
        response(Some(registration_id), None)
    }

    fn complete(&mut self, message: &Value) -> Value {
        let response = |registration_id: Option<u64>, error: Option<&str>| {
            let mut response = serde_json::json!({
                "type": "child-group-complete",
                "registrationId": registration_id,
                "sidecarGeneration": self.generation,
            });
            if let Some(error) = error {
                response["error"] = Value::String(error.to_string());
            }
            response
        };
        let registration_id = message.get("registrationId").and_then(Value::as_u64);
        let (registration_id, pgid) = match self.identity(message) {
            Ok(identity) => identity,
            Err(error) => return response(registration_id, Some(error)),
        };
        if self.completed.contains(&(registration_id, pgid)) {
            return response(Some(registration_id), None);
        }
        match self.active.get(&registration_id) {
            Some(group) if group.pgid == pgid => {
                self.active.remove(&registration_id);
                self.completed.insert((registration_id, pgid));
                response(Some(registration_id), None)
            }
            Some(_) => response(
                Some(registration_id),
                Some("process group does not match registration"),
            ),
            None => response(Some(registration_id), Some("child group is not registered")),
        }
    }
}

pub fn read_message(reader: &mut impl Read) -> Result<Option<Value>> {
    let mut header = [0_u8; 4];
    match reader.read_exact(&mut header) {
        Ok(()) => {}
        Err(error) if error.kind() == std::io::ErrorKind::UnexpectedEof => return Ok(None),
        Err(error) => return Err(error).context("read Desktop control header"),
    }
    let size = u32::from_be_bytes(header) as usize;
    if size == 0 || size > MAX_CONTROL_MESSAGE_BYTES {
        bail!("invalid Desktop control message size");
    }
    let mut payload = vec![0_u8; size];
    reader
        .read_exact(&mut payload)
        .context("read Desktop control payload")?;
    let value: Value = serde_json::from_slice(&payload).context("parse Desktop control JSON")?;
    if !value.is_object() || value.get("type").and_then(Value::as_str).is_none() {
        bail!("Desktop control message must have a string type");
    }
    Ok(Some(value))
}

pub fn write_message(writer: &mut impl Write, message: &Value) -> Result<()> {
    let payload = serde_json::to_vec(message)?;
    if payload.is_empty() || payload.len() > MAX_CONTROL_MESSAGE_BYTES {
        bail!("invalid Desktop control message size");
    }
    writer
        .write_all(&(payload.len() as u32).to_be_bytes())
        .context("write Desktop control header")?;
    writer
        .write_all(&payload)
        .context("write Desktop control payload")?;
    writer.flush().context("flush Desktop control message")?;
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::*;
    use serde_json::json;

    #[test]
    fn frame_round_trip_matches_python_protocol() {
        let message = json!({"type": "ready", "port": 8766});
        let mut bytes = Vec::new();
        write_message(&mut bytes, &message).unwrap();
        assert_eq!(read_message(&mut bytes.as_slice()).unwrap(), Some(message));
    }

    #[test]
    fn child_group_tracking_is_generation_scoped_and_idempotent() {
        let mut tracker = ChildGroupTracker::new(9);
        let register = json!({
            "type": "register-child-group",
            "sidecarGeneration": 9,
            "registrationId": 1,
            "pgid": 123,
            "kind": "bash",
        });
        assert_eq!(
            tracker.dispatch(&register).unwrap()["type"],
            "child-group-registered"
        );
        assert_eq!(tracker.active_len(), 1);
        assert!(tracker.dispatch(&register).unwrap().get("error").is_none());
        let complete = json!({
            "type": "complete-child-group",
            "sidecarGeneration": 9,
            "registrationId": 1,
            "pgid": 123,
        });
        assert!(tracker.dispatch(&complete).unwrap().get("error").is_none());
        assert!(tracker.dispatch(&complete).unwrap().get("error").is_none());
        assert_eq!(tracker.active_len(), 0);
    }

    #[test]
    fn child_group_tracking_rejects_stale_or_unfixed_registration() {
        let mut tracker = ChildGroupTracker::new(10);
        for message in [
            json!({
                "type": "register-child-group",
                "sidecarGeneration": 9,
                "registrationId": 1,
                "pgid": 123,
                "kind": "bash",
            }),
            json!({
                "type": "register-child-group",
                "sidecarGeneration": 10,
                "registrationId": 2,
                "pgid": 124,
                "kind": "arbitrary-spawn-service",
            }),
        ] {
            assert!(tracker.dispatch(&message).unwrap().get("error").is_some());
        }
        assert_eq!(tracker.active_len(), 0);
    }
}
