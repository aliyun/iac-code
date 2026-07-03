use iac_code_tui::{
    EffortLevel, ModelDefinition, ModelPickerEntry, ModelPickerState, ModelProviderGroup,
    ModelThinkingSpec,
};

#[test]
fn model_picker_builds_headers_models_and_initial_focus_like_python() {
    let picker = ModelPickerState::new("gpt-5.5", make_groups());

    assert_eq!(
        entry_labels(picker.items()),
        vec![
            "header:Alibaba Cloud Bailian",
            "model:dashscope:qwen3.6-plus",
            "model:dashscope:qwen3.5-plus",
            "header:OpenAI",
            "model:openai:gpt-5.5",
            "model:openai:gpt-5.4",
            "header:DeepSeek",
            "model:deepseek:deepseek-v4-pro",
        ]
    );
    assert_eq!(picker.focused_index(), 4);
    assert_eq!(picker.focused_pair(), Some(("openai", "gpt-5.5")));
}

#[test]
fn model_picker_falls_back_to_first_selectable_model() {
    let picker = ModelPickerState::new("missing-model", make_groups());

    assert_eq!(picker.focused_index(), 1);
    assert_eq!(picker.focused_pair(), Some(("dashscope", "qwen3.6-plus")));
}

#[test]
fn model_picker_navigation_skips_headers_and_clamps_at_edges() {
    let mut picker = ModelPickerState::new("qwen3.5-plus", make_groups());

    assert_eq!(picker.focused_pair(), Some(("dashscope", "qwen3.5-plus")));
    picker.move_focus(1);
    assert_eq!(picker.focused_pair(), Some(("openai", "gpt-5.5")));

    picker.move_focus(-1);
    assert_eq!(picker.focused_pair(), Some(("dashscope", "qwen3.5-plus")));

    picker.move_focus(-1);
    assert_eq!(picker.focused_pair(), Some(("dashscope", "qwen3.6-plus")));
    picker.move_focus(-1);
    assert_eq!(picker.focused_pair(), Some(("dashscope", "qwen3.6-plus")));

    picker.move_focus(1);
    assert_eq!(picker.focused_pair(), Some(("dashscope", "qwen3.5-plus")));
    picker.move_focus(1);
    assert_eq!(picker.focused_pair(), Some(("openai", "gpt-5.5")));
    picker.move_focus(1);
    assert_eq!(picker.focused_pair(), Some(("openai", "gpt-5.4")));
    picker.move_focus(1);
    assert_eq!(picker.focused_pair(), Some(("deepseek", "deepseek-v4-pro")));
    picker.move_focus(1);
    assert_eq!(picker.focused_pair(), Some(("deepseek", "deepseek-v4-pro")));
}

#[test]
fn model_picker_initializes_and_cycles_effort_with_allowed_ranges() {
    let mut picker = ModelPickerState::new("gpt-5.5", make_groups());

    assert_eq!(
        picker.effort_for("openai", "gpt-5.5"),
        Some(EffortLevel::High)
    );
    assert_eq!(
        picker.effort_for("deepseek", "deepseek-v4-pro"),
        Some(EffortLevel::High)
    );
    assert_eq!(picker.effort_for("dashscope", "qwen3.6-plus"), None);

    picker.cycle_effort(("openai", "gpt-5.5"), 1);
    assert_eq!(
        picker.effort_for("openai", "gpt-5.5"),
        Some(EffortLevel::XHigh)
    );
    picker.cycle_effort(("openai", "gpt-5.5"), 1);
    assert_eq!(
        picker.effort_for("openai", "gpt-5.5"),
        Some(EffortLevel::XHigh)
    );
    picker.cycle_effort(("openai", "gpt-5.5"), -2);
    assert_eq!(
        picker.effort_for("openai", "gpt-5.5"),
        Some(EffortLevel::Medium)
    );

    picker.cycle_effort(("deepseek", "deepseek-v4-pro"), 1);
    assert_eq!(
        picker.effort_for("deepseek", "deepseek-v4-pro"),
        Some(EffortLevel::Max)
    );
}

#[test]
fn model_picker_select_and_cancel_match_python_done_state() {
    let mut unchanged = ModelPickerState::new("gpt-5.5", make_groups());
    let unchanged_selection = unchanged
        .select_focused()
        .expect("focused model should select");

    assert_eq!(unchanged_selection.provider_key, "openai");
    assert_eq!(unchanged_selection.model, "gpt-5.5");
    assert_eq!(unchanged_selection.effort, None);

    let mut picker = ModelPickerState::new("gpt-5.5", make_groups());

    picker.cycle_effort(("openai", "gpt-5.5"), 1);
    let selection = picker
        .select_focused()
        .expect("focused model should select");

    assert!(picker.is_done());
    assert_eq!(selection.provider_key, "openai");
    assert_eq!(selection.model, "gpt-5.5");
    assert_eq!(selection.effort, Some(EffortLevel::XHigh));
    assert_eq!(
        picker.result().map(|result| result.model.as_str()),
        Some("gpt-5.5")
    );

    let mut cancelled = ModelPickerState::new("qwen3.6-plus", make_groups());
    cancelled.cancel();
    assert!(cancelled.is_done());
    assert!(cancelled.result().is_none());
}

fn make_groups() -> Vec<ModelProviderGroup> {
    vec![
        ModelProviderGroup::new(
            "dashscope",
            "Alibaba Cloud Bailian",
            vec![
                ModelDefinition::new("qwen3.6-plus", ModelThinkingSpec::none()),
                ModelDefinition::new("qwen3.5-plus", ModelThinkingSpec::none()),
            ],
        ),
        ModelProviderGroup::new(
            "openai",
            "OpenAI",
            vec![
                ModelDefinition::new(
                    "gpt-5.5",
                    ModelThinkingSpec::new(
                        vec![
                            EffortLevel::Low,
                            EffortLevel::Medium,
                            EffortLevel::High,
                            EffortLevel::XHigh,
                        ],
                        Some(EffortLevel::High),
                    ),
                ),
                ModelDefinition::new(
                    "gpt-5.4",
                    ModelThinkingSpec::new(
                        vec![
                            EffortLevel::Low,
                            EffortLevel::Medium,
                            EffortLevel::High,
                            EffortLevel::XHigh,
                        ],
                        Some(EffortLevel::High),
                    ),
                ),
            ],
        ),
        ModelProviderGroup::new(
            "deepseek",
            "DeepSeek",
            vec![ModelDefinition::new(
                "deepseek-v4-pro",
                ModelThinkingSpec::new(
                    vec![EffortLevel::High, EffortLevel::Max],
                    Some(EffortLevel::High),
                ),
            )],
        ),
    ]
}

fn entry_labels(items: &[ModelPickerEntry]) -> Vec<String> {
    items
        .iter()
        .map(|item| match item {
            ModelPickerEntry::Header { display_name } => format!("header:{display_name}"),
            ModelPickerEntry::Model {
                provider_key,
                model,
            } => format!("model:{provider_key}:{model}"),
        })
        .collect()
}
