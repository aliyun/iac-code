use std::fs;
use std::io;
use std::path::Path;

use iac_code_protocol::json::{self, JsonValue};

use super::json_value::{object_fields, object_string};
use super::{
    ensure_private_dir, ensure_private_file, SESSION_METADATA_FILENAME,
    SESSION_METADATA_SCHEMA_VERSION,
};

#[derive(Clone, Debug, PartialEq, Eq)]
pub struct SessionMetadata {
    pub session_id: String,
    pub name: Option<String>,
    pub cwd: Option<String>,
    pub git_branch: Option<String>,
    pub created_at: Option<String>,
    pub updated_at: Option<String>,
    pub schema_version: u64,
}

impl SessionMetadata {
    pub fn new(session_id: impl Into<String>) -> Self {
        Self {
            session_id: session_id.into(),
            name: None,
            cwd: None,
            git_branch: None,
            created_at: None,
            updated_at: None,
            schema_version: SESSION_METADATA_SCHEMA_VERSION,
        }
    }

    pub fn from_json_value(value: &JsonValue) -> Option<Self> {
        let fields = object_fields(value)?;
        let session_id = object_string(fields, "session_id")?;
        if session_id.is_empty() {
            return None;
        }
        let schema_version = match fields.get("schema_version") {
            Some(JsonValue::Number(value)) => value
                .parse::<u64>()
                .unwrap_or(SESSION_METADATA_SCHEMA_VERSION),
            _ => SESSION_METADATA_SCHEMA_VERSION,
        };
        Some(Self {
            session_id: session_id.to_owned(),
            name: object_non_empty_string(fields, "name").map(str::to_owned),
            cwd: object_string(fields, "cwd").map(str::to_owned),
            git_branch: object_string(fields, "git_branch").map(str::to_owned),
            created_at: object_string(fields, "created_at").map(str::to_owned),
            updated_at: object_string(fields, "updated_at").map(str::to_owned),
            schema_version,
        })
    }

    pub fn to_json_value(&self) -> JsonValue {
        json::object([
            ("session_id", json::string(self.session_id.clone())),
            ("name", optional_string(&self.name)),
            ("cwd", optional_string(&self.cwd)),
            ("git_branch", optional_string(&self.git_branch)),
            ("created_at", optional_string(&self.created_at)),
            ("updated_at", optional_string(&self.updated_at)),
            ("schema_version", json::number(self.schema_version)),
        ])
    }
}

pub fn read_session_metadata(session_dir: &Path) -> Option<SessionMetadata> {
    let text = fs::read_to_string(session_dir.join(SESSION_METADATA_FILENAME)).ok()?;
    let value = json::parse(&text).ok()?;
    SessionMetadata::from_json_value(&value)
}

pub fn write_session_metadata(session_dir: &Path, metadata: &SessionMetadata) -> io::Result<()> {
    ensure_private_dir(session_dir)?;
    let path = session_dir.join(SESSION_METADATA_FILENAME);
    fs::write(
        &path,
        format!("{}\n", metadata.to_json_value().to_compact_json()),
    )?;
    ensure_private_file(&path)
}

fn optional_string(value: &Option<String>) -> JsonValue {
    value
        .as_ref()
        .map(|value| json::string(value.clone()))
        .unwrap_or(JsonValue::Null)
}

fn object_non_empty_string<'a>(
    fields: &'a std::collections::BTreeMap<String, JsonValue>,
    key: &str,
) -> Option<&'a str> {
    object_string(fields, key).filter(|value| !value.is_empty())
}
