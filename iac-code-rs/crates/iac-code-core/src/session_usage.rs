use std::collections::BTreeMap;
use std::fs::{self, OpenOptions};
use std::io::{self, Write};
use std::path::{Path, PathBuf};
use std::time::{SystemTime, UNIX_EPOCH};

use iac_code_protocol::json::{self, JsonValue};
use iac_code_protocol::Usage;

use crate::session::sanitize_path;

pub const USAGE_JSONL_FILENAME: &str = "usage.jsonl";

#[derive(Clone, Debug, Default, PartialEq, Eq)]
pub struct SessionUsageTotals {
    pub input_tokens: u64,
    pub output_tokens: u64,
    pub cache_read_input_tokens: u64,
    pub cache_creation_input_tokens: u64,
    pub recorded_events: u64,
}

impl SessionUsageTotals {
    pub fn total_tokens(&self) -> u64 {
        self.input_tokens + self.output_tokens
    }

    pub fn has_recorded_usage(&self) -> bool {
        self.recorded_events > 0
    }

    pub fn add(&mut self, usage: &Usage) -> bool {
        if usage_is_zero(usage) {
            return false;
        }
        self.input_tokens = self.input_tokens.saturating_add(usage.input_tokens);
        self.output_tokens = self.output_tokens.saturating_add(usage.output_tokens);
        self.cache_read_input_tokens = self
            .cache_read_input_tokens
            .saturating_add(usage.cache_read_input_tokens);
        self.cache_creation_input_tokens = self
            .cache_creation_input_tokens
            .saturating_add(usage.cache_creation_input_tokens);
        self.recorded_events = self.recorded_events.saturating_add(1);
        true
    }
}

#[derive(Clone, Debug)]
pub struct SessionUsageStore {
    projects_dir: PathBuf,
}

impl SessionUsageStore {
    pub fn new(projects_dir: impl AsRef<Path>) -> Self {
        Self {
            projects_dir: projects_dir.as_ref().to_path_buf(),
        }
    }

    pub fn path_for(&self, cwd: &str, session_id: &str) -> PathBuf {
        self.project_dir_for(cwd)
            .join(session_id)
            .join(USAGE_JSONL_FILENAME)
    }

    pub fn legacy_path_for(&self, cwd: &str, session_id: &str) -> PathBuf {
        self.project_dir_for(cwd)
            .join(format!("{session_id}.usage.jsonl"))
    }

    pub fn append(
        &self,
        cwd: &str,
        session_id: &str,
        usage: &Usage,
        provider: Option<&str>,
        model: Option<&str>,
    ) -> io::Result<bool> {
        if usage_is_zero(usage) {
            return Ok(false);
        }
        let path = self.path_for(cwd, session_id);
        ensure_private_dir(path_parent(&path)?)?;
        let row = usage_to_row(usage, provider, model);
        let mut file = OpenOptions::new().append(true).create(true).open(&path)?;
        writeln!(file, "{}", row.to_compact_json())?;
        ensure_private_file(&path)?;
        Ok(true)
    }

    pub fn load(&self, cwd: &str, session_id: &str) -> SessionUsageTotals {
        let mut totals = SessionUsageTotals::default();
        load_usage_path(&self.path_for(cwd, session_id), &mut totals);
        load_usage_path(&self.legacy_path_for(cwd, session_id), &mut totals);
        totals
    }

    fn project_dir_for(&self, cwd: &str) -> PathBuf {
        self.projects_dir.join(sanitize_path(cwd))
    }
}

fn load_usage_path(path: &Path, totals: &mut SessionUsageTotals) {
    let Ok(text) = fs::read_to_string(path) else {
        return;
    };
    for line in text.lines().map(str::trim).filter(|line| !line.is_empty()) {
        let Ok(value) = json::parse(line) else {
            continue;
        };
        let Some(fields) = object_fields(&value) else {
            continue;
        };
        if object_string(fields, "type") != Some("usage") {
            continue;
        }
        totals.add(&usage_from_row(fields));
    }
}

fn usage_to_row(usage: &Usage, provider: Option<&str>, model: Option<&str>) -> JsonValue {
    json::object([
        ("type", json::string("usage")),
        ("version", json::number(1)),
        ("created_at", json::string(utc_now())),
        ("provider", optional_string(provider)),
        ("model", optional_string(model)),
        ("input_tokens", json::number(usage.input_tokens)),
        ("output_tokens", json::number(usage.output_tokens)),
        (
            "cache_read_input_tokens",
            json::number(usage.cache_read_input_tokens),
        ),
        (
            "cache_creation_input_tokens",
            json::number(usage.cache_creation_input_tokens),
        ),
    ])
}

fn usage_from_row(fields: &BTreeMap<String, JsonValue>) -> Usage {
    Usage {
        input_tokens: object_u64(fields, "input_tokens").unwrap_or(0),
        output_tokens: object_u64(fields, "output_tokens").unwrap_or(0),
        cache_read_input_tokens: object_u64(fields, "cache_read_input_tokens").unwrap_or(0),
        cache_creation_input_tokens: object_u64(fields, "cache_creation_input_tokens").unwrap_or(0),
    }
}

fn usage_is_zero(usage: &Usage) -> bool {
    usage.input_tokens == 0
        && usage.output_tokens == 0
        && usage.cache_read_input_tokens == 0
        && usage.cache_creation_input_tokens == 0
}

fn object_fields(value: &JsonValue) -> Option<&BTreeMap<String, JsonValue>> {
    match value {
        JsonValue::Object(fields) => Some(fields),
        _ => None,
    }
}

fn object_string<'a>(fields: &'a BTreeMap<String, JsonValue>, key: &str) -> Option<&'a str> {
    match fields.get(key) {
        Some(JsonValue::String(value)) => Some(value.as_str()),
        _ => None,
    }
}

fn object_u64(fields: &BTreeMap<String, JsonValue>, key: &str) -> Option<u64> {
    fields.get(key).and_then(json_int_like_python)
}

fn json_int_like_python(value: &JsonValue) -> Option<u64> {
    match value {
        JsonValue::Number(value) => json_number_to_u64(value),
        JsonValue::String(value) => python_int_string_to_u64(value),
        _ => None,
    }
}

fn json_number_to_u64(value: &str) -> Option<u64> {
    if let Some(value) = signed_integer_to_u64(value) {
        return Some(value);
    }
    let value = value.parse::<f64>().ok()?;
    if !value.is_finite() {
        return None;
    }
    if value <= 0.0 {
        return Some(0);
    }
    if value >= u64::MAX as f64 {
        return Some(u64::MAX);
    }
    Some(value.trunc() as u64)
}

fn python_int_string_to_u64(value: &str) -> Option<u64> {
    signed_integer_to_u64(value.trim())
}

fn signed_integer_to_u64(value: &str) -> Option<u64> {
    let value = value.parse::<i128>().ok()?;
    if value <= 0 {
        return Some(0);
    }
    Some(u64::try_from(value).unwrap_or(u64::MAX))
}

fn optional_string(value: Option<&str>) -> JsonValue {
    value.map(json::string).unwrap_or(JsonValue::Null)
}

fn path_parent(path: &Path) -> io::Result<&Path> {
    path.parent().ok_or_else(|| {
        io::Error::new(
            io::ErrorKind::InvalidInput,
            format!("path has no parent: {}", path.display()),
        )
    })
}

fn utc_now() -> String {
    let seconds = SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .unwrap_or_default()
        .as_secs() as i64;
    format_unix_utc(seconds)
}

fn format_unix_utc(seconds: i64) -> String {
    let days = seconds.div_euclid(86_400);
    let seconds_of_day = seconds.rem_euclid(86_400);
    let (year, month, day) = civil_from_days(days);
    let hour = seconds_of_day / 3_600;
    let minute = seconds_of_day % 3_600 / 60;
    let second = seconds_of_day % 60;
    format!("{year:04}-{month:02}-{day:02}T{hour:02}:{minute:02}:{second:02}Z")
}

fn civil_from_days(days_since_epoch: i64) -> (i64, i64, i64) {
    let z = days_since_epoch + 719_468;
    let era = if z >= 0 { z } else { z - 146_096 } / 146_097;
    let doe = z - era * 146_097;
    let yoe = (doe - doe / 1_460 + doe / 36_524 - doe / 146_096) / 365;
    let y = yoe + era * 400;
    let doy = doe - (365 * yoe + yoe / 4 - yoe / 100);
    let mp = (5 * doy + 2) / 153;
    let day = doy - (153 * mp + 2) / 5 + 1;
    let month = mp + if mp < 10 { 3 } else { -9 };
    let year = y + if month <= 2 { 1 } else { 0 };
    (year, month, day)
}

fn ensure_private_dir(path: &Path) -> io::Result<()> {
    fs::create_dir_all(path)?;
    restrict_dir_permissions(path)
}

fn ensure_private_file(path: &Path) -> io::Result<()> {
    restrict_file_permissions(path)
}

#[cfg(unix)]
fn restrict_dir_permissions(path: &Path) -> io::Result<()> {
    use std::os::unix::fs::PermissionsExt;

    let mut permissions = fs::metadata(path)?.permissions();
    permissions.set_mode(0o700);
    fs::set_permissions(path, permissions)
}

#[cfg(not(unix))]
fn restrict_dir_permissions(_path: &Path) -> io::Result<()> {
    Ok(())
}

#[cfg(unix)]
fn restrict_file_permissions(path: &Path) -> io::Result<()> {
    use std::os::unix::fs::PermissionsExt;

    let mut permissions = fs::metadata(path)?.permissions();
    permissions.set_mode(0o600);
    fs::set_permissions(path, permissions)
}

#[cfg(not(unix))]
fn restrict_file_permissions(_path: &Path) -> io::Result<()> {
    Ok(())
}
