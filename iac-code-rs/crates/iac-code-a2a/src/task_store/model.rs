#[derive(Clone, Debug, PartialEq, Eq)]
pub struct Artifact {
    pub artifact_id: String,
    pub filename: String,
    pub media_type: String,
    pub byte_size: usize,
    pub sha256: String,
    pub uri: String,
}

#[derive(Clone, Debug, PartialEq, Eq)]
pub struct SdkTask {
    pub id: String,
    pub context_id: String,
    pub status: String,
    pub status_message: String,
    pub status_timestamp: Option<i64>,
    pub artifacts: Vec<Artifact>,
}

impl SdkTask {
    pub fn new(
        task_id: impl Into<String>,
        context_id: impl Into<String>,
        status: impl Into<String>,
        updated_at: i64,
    ) -> Self {
        Self {
            id: task_id.into(),
            context_id: context_id.into(),
            status: status.into(),
            status_message: String::new(),
            status_timestamp: Some(updated_at),
            artifacts: Vec::new(),
        }
    }

    pub fn with_status_message(mut self, message: impl Into<String>) -> Self {
        self.status_message = message.into();
        self
    }
}

#[derive(Clone, Debug, Default, PartialEq, Eq)]
pub struct ListTasksRequest {
    pub context_id: Option<String>,
    pub status: Option<String>,
    pub page_size: Option<usize>,
    pub page_token: Option<String>,
    pub include_artifacts: bool,
}

#[derive(Clone, Debug, PartialEq, Eq)]
pub struct ListTasksResponse {
    pub tasks: Vec<SdkTask>,
    pub next_page_token: String,
    pub page_size: usize,
    pub total_size: usize,
}
