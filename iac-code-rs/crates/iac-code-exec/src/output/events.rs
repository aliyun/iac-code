#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum OutputFormat {
    Text,
    Json,
    StreamJson,
}

impl OutputFormat {
    pub fn parse(value: &str) -> Option<Self> {
        match value {
            "text" => Some(Self::Text),
            "json" => Some(Self::Json),
            "stream-json" => Some(Self::StreamJson),
            _ => None,
        }
    }
}

#[derive(Clone, Debug, Default, PartialEq, Eq)]
pub struct OutputCapture {
    pub stdout: String,
    pub stderr: String,
}
