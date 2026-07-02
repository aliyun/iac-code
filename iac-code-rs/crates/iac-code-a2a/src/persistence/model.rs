use std::collections::BTreeMap;
use std::time::{SystemTime, UNIX_EPOCH};

use iac_code_protocol::json::{self, JsonValue};

#[derive(Clone, Debug, PartialEq)]
pub struct A2ATaskSnapshot {
    pub task_id: String,
    pub context_id: String,
    pub state: String,
    pub output_text: Vec<String>,
    pub status_message: String,
    pub updated_at: f64,
}

#[derive(Clone, Debug, PartialEq)]
pub struct A2AContextSnapshot {
    pub context_id: String,
    pub session_id: String,
    pub cwd: String,
    pub active_task_id: Option<String>,
    pub updated_at: f64,
}

#[derive(Clone, Debug, PartialEq, Eq)]
pub struct A2ARouteSnapshot {
    pub name: String,
    pub url: String,
    pub skills: Vec<String>,
    pub tags: Vec<String>,
}

pub(super) fn task_to_json(snapshot: &A2ATaskSnapshot) -> JsonValue {
    json::object([
        ("context_id", json::string(&snapshot.context_id)),
        ("output_text", string_array(&snapshot.output_text)),
        ("state", json::string(&snapshot.state)),
        ("status_message", json::string(&snapshot.status_message)),
        ("task_id", json::string(&snapshot.task_id)),
        ("updated_at", json::float(snapshot.updated_at)),
    ])
}

pub(super) fn task_from_json(value: &JsonValue) -> Option<A2ATaskSnapshot> {
    let JsonValue::Object(data) = value else {
        return None;
    };
    let task_id = string_field(data, "task_id")?;
    let context_id = string_field(data, "context_id")?;
    let state = string_field(data, "state")?;
    Some(A2ATaskSnapshot {
        task_id,
        context_id,
        state,
        output_text: string_list_field(data, "output_text"),
        status_message: string_field(data, "status_message").unwrap_or_default(),
        updated_at: number_field(data, "updated_at").unwrap_or_else(current_time_seconds),
    })
}

pub(super) fn context_to_json(snapshot: &A2AContextSnapshot) -> JsonValue {
    json::object([
        (
            "active_task_id",
            snapshot
                .active_task_id
                .as_ref()
                .map(json::string)
                .unwrap_or(JsonValue::Null),
        ),
        ("context_id", json::string(&snapshot.context_id)),
        ("cwd", json::string(&snapshot.cwd)),
        ("session_id", json::string(&snapshot.session_id)),
        ("updated_at", json::float(snapshot.updated_at)),
    ])
}

pub(super) fn context_from_json(value: &JsonValue) -> Option<A2AContextSnapshot> {
    let JsonValue::Object(data) = value else {
        return None;
    };
    let context_id = string_field(data, "context_id")?;
    let session_id = string_field(data, "session_id")?;
    let cwd = string_field(data, "cwd")?;
    Some(A2AContextSnapshot {
        context_id,
        session_id,
        cwd,
        active_task_id: string_field(data, "active_task_id"),
        updated_at: number_field(data, "updated_at").unwrap_or_else(current_time_seconds),
    })
}

pub(super) fn routes_to_json(routes: &[A2ARouteSnapshot]) -> JsonValue {
    let route_values = routes.iter().map(route_to_json).collect::<Vec<_>>();
    json::object([("routes", JsonValue::Array(route_values))])
}

pub(super) fn routes_from_json(value: &JsonValue) -> Vec<A2ARouteSnapshot> {
    let JsonValue::Object(data) = value else {
        return Vec::new();
    };
    let Some(JsonValue::Array(routes)) = data.get("routes") else {
        return Vec::new();
    };
    routes.iter().filter_map(route_from_json).collect()
}

fn route_to_json(snapshot: &A2ARouteSnapshot) -> JsonValue {
    json::object([
        ("name", json::string(&snapshot.name)),
        ("skills", string_array(&snapshot.skills)),
        ("tags", string_array(&snapshot.tags)),
        ("url", json::string(&snapshot.url)),
    ])
}

fn route_from_json(value: &JsonValue) -> Option<A2ARouteSnapshot> {
    let JsonValue::Object(data) = value else {
        return None;
    };
    Some(A2ARouteSnapshot {
        name: string_field(data, "name")?,
        url: string_field(data, "url")?,
        skills: string_list_field(data, "skills"),
        tags: string_list_field(data, "tags"),
    })
}

fn string_array(values: &[String]) -> JsonValue {
    JsonValue::Array(values.iter().map(json::string).collect())
}

fn string_field(data: &BTreeMap<String, JsonValue>, key: &str) -> Option<String> {
    match data.get(key) {
        Some(JsonValue::String(value)) => Some(value.clone()),
        _ => None,
    }
}

fn string_list_field(data: &BTreeMap<String, JsonValue>, key: &str) -> Vec<String> {
    let Some(JsonValue::Array(values)) = data.get(key) else {
        return Vec::new();
    };
    values
        .iter()
        .filter_map(|value| match value {
            JsonValue::String(value) => Some(value.clone()),
            _ => None,
        })
        .collect()
}

fn number_field(data: &BTreeMap<String, JsonValue>, key: &str) -> Option<f64> {
    match data.get(key) {
        Some(JsonValue::Number(value)) => value.parse::<f64>().ok(),
        _ => None,
    }
}

pub(super) fn current_time_seconds() -> f64 {
    SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .map(|duration| duration.as_secs_f64())
        .unwrap_or_default()
}
