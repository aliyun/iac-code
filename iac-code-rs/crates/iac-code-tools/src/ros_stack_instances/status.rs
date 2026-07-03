use iac_code_protocol::json::{self, JsonValue};

pub(crate) const TERMINAL_STATUSES: &[&str] = &["SUCCEEDED", "FAILED", "STOPPED"];
const DONE_STATUSES: &[&str] = &["SUCCEEDED", "CURRENT", "FAILED", "STOPPED"];

#[derive(Clone, Debug)]
pub(crate) struct StackInstanceStatus {
    account_id: String,
    region_id: String,
    status: String,
    status_reason: String,
    elapsed_seconds: u64,
}

impl StackInstanceStatus {
    pub(crate) fn from_value(value: &serde_json::Value) -> Self {
        Self {
            account_id: json_string(value, "AccountId").unwrap_or_default(),
            region_id: json_string(value, "RegionId").unwrap_or_default(),
            status: json_string(value, "Status").unwrap_or_default(),
            status_reason: json_string(value, "StatusReason").unwrap_or_default(),
            elapsed_seconds: json_u64(value, "ElapsedSeconds").unwrap_or(0),
        }
    }

    pub(crate) fn to_json_value(&self) -> JsonValue {
        json::object([
            ("account_id", json::string(&self.account_id)),
            ("region_id", json::string(&self.region_id)),
            ("status", json::string(&self.status)),
            ("status_reason", json::string(&self.status_reason)),
            ("elapsed_seconds", json::number(self.elapsed_seconds)),
        ])
    }
}

pub(crate) fn progress_percentage(instances: &[StackInstanceStatus]) -> u64 {
    if instances.is_empty() {
        return 0;
    }
    let done_count = instances
        .iter()
        .filter(|instance| DONE_STATUSES.contains(&instance.status.as_str()))
        .count();
    (done_count * 100 / instances.len()) as u64
}

pub(crate) fn json_string(value: &serde_json::Value, field: &str) -> Option<String> {
    value
        .get(field)
        .and_then(serde_json::Value::as_str)
        .map(str::to_owned)
}

fn json_u64(value: &serde_json::Value, field: &str) -> Option<u64> {
    value.get(field).and_then(|value| {
        value
            .as_u64()
            .or_else(|| value.as_str().and_then(|text| text.parse::<u64>().ok()))
    })
}
