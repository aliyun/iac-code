use iac_code_protocol::message::{AgentContentBlock, AgentMessage, AgentMessageContent};
use iac_code_protocol::provider::ToolDefinition;

#[derive(Clone, Copy, Debug, PartialEq)]
struct TokenEstimateProfile {
    chars_per_token: f64,
    cjk_chars_per_token: f64,
}

#[derive(Clone, Debug, PartialEq, Eq)]
pub struct TokenBudget {
    total: Option<u64>,
    used: u64,
}

impl TokenBudget {
    pub fn new(total: Option<u64>) -> Self {
        Self { total, used: 0 }
    }

    pub fn unlimited() -> Self {
        Self::new(None)
    }

    pub fn from_shorthand(text: &str) -> Result<Self, String> {
        Ok(Self::new(Some(Self::parse_shorthand(text)?)))
    }

    pub fn parse_shorthand(text: &str) -> Result<u64, String> {
        let cleaned = text.trim().trim_start_matches('+');
        if cleaned.is_empty() {
            return Err(format!("Invalid token shorthand: '{text}'"));
        }

        let mut number = cleaned;
        let mut multiplier = 1.0;
        if let Some(suffix) = cleaned.chars().last() {
            match suffix {
                'k' | 'K' => {
                    number = cleaned[..cleaned.len() - suffix.len_utf8()].trim_end();
                    multiplier = 1_000.0;
                }
                'm' | 'M' => {
                    number = cleaned[..cleaned.len() - suffix.len_utf8()].trim_end();
                    multiplier = 1_000_000.0;
                }
                _ => {}
            }
        }

        if !valid_python_shorthand_number(number) {
            return Err(format!("Invalid token shorthand: '{text}'"));
        }
        let value = number
            .parse::<f64>()
            .map_err(|_| format!("Invalid token shorthand: '{text}'"))?;
        Ok((value * multiplier) as u64)
    }

    pub fn consume(&mut self, tokens: u64) {
        self.used = self.used.saturating_add(tokens);
    }

    pub fn used(&self) -> u64 {
        self.used
    }

    pub fn remaining(&self) -> Option<u64> {
        self.total.map(|total| total.saturating_sub(self.used))
    }

    pub fn is_exhausted(&self) -> bool {
        self.total.is_some_and(|total| self.used >= total)
    }

    pub fn usage_percent(&self) -> f64 {
        match self.total {
            Some(0) | None => 0.0,
            Some(total) => (self.used as f64 / total as f64) * 100.0,
        }
    }
}

fn valid_python_shorthand_number(number: &str) -> bool {
    let mut parts = number.split('.');
    let Some(integer) = parts.next() else {
        return false;
    };
    if integer.is_empty() || !integer.chars().all(|ch| ch.is_ascii_digit()) {
        return false;
    }
    match (parts.next(), parts.next()) {
        (None, None) => true,
        (Some(fraction), None) => {
            !fraction.is_empty() && fraction.chars().all(|ch| ch.is_ascii_digit())
        }
        _ => false,
    }
}

#[derive(Clone, Debug)]
pub struct TokenCounter {
    profile: TokenEstimateProfile,
}

impl TokenCounter {
    pub fn new(model: &str) -> Self {
        Self {
            profile: estimate_profile(model),
        }
    }

    pub fn count_text(&self, text: &str) -> u64 {
        if text.is_empty() {
            return 0;
        }

        let mut cjk_chars = 0_u64;
        let mut other_chars = 0_u64;
        for ch in text.chars().filter(|ch| !ch.is_whitespace()) {
            if is_cjk(ch) {
                cjk_chars += 1;
            } else {
                other_chars += 1;
            }
        }
        let estimated = (cjk_chars as f64 / self.profile.cjk_chars_per_token)
            + (other_chars as f64 / self.profile.chars_per_token);
        estimated.ceil().max(1.0) as u64
    }

    pub fn count_message(&self, message: &AgentMessage) -> u64 {
        let mut count = 4_u64;
        match &message.content {
            AgentMessageContent::Text(text) => count += self.count_text(text),
            AgentMessageContent::Blocks(blocks) => {
                for block in blocks {
                    match block {
                        AgentContentBlock::Text(text) => {
                            count += self.count_text(&text.text);
                        }
                        AgentContentBlock::ToolUse(tool_use) => {
                            count += 10;
                            count += self.count_text(&tool_use.name);
                            count += self.count_text(&tool_use.input.to_compact_json());
                        }
                        AgentContentBlock::ToolResult(tool_result) => {
                            count += 10;
                            count += self.count_text(&tool_result.content);
                        }
                        AgentContentBlock::Thinking(_) | AgentContentBlock::Image(_) => {}
                    }
                }
            }
        }
        count
    }

    pub fn count_tool_definition(&self, tool: &ToolDefinition) -> u64 {
        12 + self.count_text(&tool.name)
            + self.count_text(&tool.description)
            + self.count_text(&tool.input_schema.to_compact_json())
    }

    pub fn count_tool_definitions(&self, tools: &[ToolDefinition]) -> u64 {
        tools
            .iter()
            .map(|tool| self.count_tool_definition(tool))
            .sum()
    }
}

fn estimate_profile(model: &str) -> TokenEstimateProfile {
    let model = model.to_ascii_lowercase();
    for (prefix, profile) in [
        ("qwen", TokenEstimateProfile::new(3.5, 1.0)),
        ("qwq", TokenEstimateProfile::new(3.5, 1.0)),
        ("kimi", TokenEstimateProfile::new(3.5, 1.1)),
        ("glm", TokenEstimateProfile::new(3.5, 1.1)),
        ("doubao", TokenEstimateProfile::new(3.5, 1.1)),
        ("minimax", TokenEstimateProfile::new(3.5, 1.1)),
        ("gemini", TokenEstimateProfile::new(4.0, 1.2)),
    ] {
        if model.starts_with(prefix) {
            return profile;
        }
    }
    TokenEstimateProfile::new(4.0, 1.6)
}

impl TokenEstimateProfile {
    const fn new(chars_per_token: f64, cjk_chars_per_token: f64) -> Self {
        Self {
            chars_per_token,
            cjk_chars_per_token,
        }
    }
}

fn is_cjk(ch: char) -> bool {
    matches!(
        ch as u32,
        0x3400..=0x4DBF | 0x4E00..=0x9FFF | 0xF900..=0xFAFF | 0x3040..=0x30FF | 0xAC00..=0xD7AF
    )
}
