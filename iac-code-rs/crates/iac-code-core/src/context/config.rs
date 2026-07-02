#[derive(Clone, Copy, Debug, PartialEq)]
pub struct ContextWindowConfig {
    pub context_window: u64,
    pub max_output_tokens: u64,
    pub compact_buffer: u64,
    pub compact_threshold: f64,
    pub preserve_recent_turns: usize,
}

pub fn context_window_config(model: &str) -> ContextWindowConfig {
    let model = model.to_ascii_lowercase();
    for (prefix, config) in [
        ("claude", ContextWindowConfig::new(200_000, 8_192, 20_000)),
        ("gpt-5", ContextWindowConfig::new(200_000, 8_192, 20_000)),
        ("gpt-4", ContextWindowConfig::new(128_000, 8_192, 15_000)),
        ("qwen", ContextWindowConfig::new(131_072, 8_192, 15_000)),
        ("qwq", ContextWindowConfig::new(131_072, 8_192, 15_000)),
        ("o3", ContextWindowConfig::new(200_000, 8_192, 20_000)),
        ("o4", ContextWindowConfig::new(200_000, 8_192, 20_000)),
    ] {
        if model.starts_with(prefix) {
            return config;
        }
    }
    ContextWindowConfig::new(128_000, 8_192, 15_000)
}

impl ContextWindowConfig {
    const fn new(context_window: u64, max_output_tokens: u64, compact_buffer: u64) -> Self {
        Self {
            context_window,
            max_output_tokens,
            compact_buffer,
            compact_threshold: 0.93,
            preserve_recent_turns: 3,
        }
    }
}
