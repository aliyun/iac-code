use std::collections::BTreeSet;
use std::fmt;

#[derive(Clone, Copy, Debug, PartialEq, Eq, PartialOrd, Ord)]
pub enum A2AExposureType {
    RawThinking,
    ToolTrace,
}

impl A2AExposureType {
    pub fn as_str(self) -> &'static str {
        match self {
            Self::RawThinking => "raw_thinking",
            Self::ToolTrace => "tool_trace",
        }
    }
}

#[derive(Clone, Debug, PartialEq, Eq)]
pub struct A2AExposureError {
    message: String,
}

impl A2AExposureError {
    fn new(message: impl Into<String>) -> Self {
        Self {
            message: message.into(),
        }
    }
}

impl fmt::Display for A2AExposureError {
    fn fmt(&self, formatter: &mut fmt::Formatter<'_>) -> fmt::Result {
        formatter.write_str(&self.message)
    }
}

impl std::error::Error for A2AExposureError {}

pub fn normalize_a2a_exposure_types(
    value: Option<&str>,
) -> Result<BTreeSet<A2AExposureType>, A2AExposureError> {
    match value {
        None => Ok(BTreeSet::from([A2AExposureType::ToolTrace])),
        Some(value) => normalize_a2a_exposure_tokens(split_tokens(value)),
    }
}

pub fn normalize_a2a_exposure_tokens<I, S>(
    values: I,
) -> Result<BTreeSet<A2AExposureType>, A2AExposureError>
where
    I: IntoIterator<Item = S>,
    S: AsRef<str>,
{
    let tokens = values
        .into_iter()
        .flat_map(|value| split_tokens(value.as_ref()))
        .collect::<Vec<_>>();
    if tokens.is_empty() {
        return Ok(BTreeSet::new());
    }
    if tokens
        .iter()
        .any(|token| matches!(token.as_str(), "all" | "*"))
    {
        return Ok(exposure_type_order().into_iter().collect());
    }

    let mut result = BTreeSet::new();
    for token in tokens {
        if matches!(token.as_str(), "" | "none" | "off" | "false" | "0") {
            continue;
        }
        result.insert(exposure_type_from_token(&token)?);
    }
    Ok(result)
}

pub fn format_a2a_exposure_types(values: &BTreeSet<A2AExposureType>) -> Vec<&'static str> {
    exposure_type_order()
        .into_iter()
        .filter(|value| values.contains(value))
        .map(A2AExposureType::as_str)
        .collect()
}

pub fn format_a2a_exposure_slice(values: &[A2AExposureType]) -> Vec<&'static str> {
    let values = values.iter().copied().collect::<BTreeSet<_>>();
    format_a2a_exposure_types(&values)
}

fn split_tokens(value: &str) -> Vec<String> {
    value
        .replace(';', ",")
        .split(',')
        .map(|item| item.trim().to_ascii_lowercase().replace('-', "_"))
        .filter(|item| !item.is_empty())
        .collect()
}

fn exposure_type_from_token(token: &str) -> Result<A2AExposureType, A2AExposureError> {
    match token {
        "raw_thinking" => Ok(A2AExposureType::RawThinking),
        "tool_trace" => Ok(A2AExposureType::ToolTrace),
        _ => Err(A2AExposureError::new(format!(
            "Unsupported A2A thinking exposure type {token:?}. Supported values: raw-thinking, tool-trace."
        ))),
    }
}

fn exposure_type_order() -> [A2AExposureType; 2] {
    [A2AExposureType::RawThinking, A2AExposureType::ToolTrace]
}
