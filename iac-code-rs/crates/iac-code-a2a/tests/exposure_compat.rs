use std::collections::BTreeSet;

use iac_code_a2a::exposure::{
    format_a2a_exposure_types, normalize_a2a_exposure_tokens, normalize_a2a_exposure_types,
    A2AExposureType,
};

#[test]
fn normalize_a2a_exposure_types_defaults_to_tool_trace() {
    assert_eq!(
        normalize_a2a_exposure_types(None).unwrap(),
        BTreeSet::from([A2AExposureType::ToolTrace])
    );
}

#[test]
fn normalize_a2a_exposure_types_accepts_multi_select_lists_and_aliases() {
    assert_eq!(
        normalize_a2a_exposure_tokens(["raw-thinking", "tool_trace"]).unwrap(),
        BTreeSet::from([A2AExposureType::RawThinking, A2AExposureType::ToolTrace])
    );
}

#[test]
fn normalize_a2a_exposure_types_accepts_comma_separated_string_all_and_disabled() {
    assert_eq!(
        normalize_a2a_exposure_types(Some("raw-thinking, tool-trace")).unwrap(),
        BTreeSet::from([A2AExposureType::RawThinking, A2AExposureType::ToolTrace])
    );
    assert_eq!(
        normalize_a2a_exposure_types(Some("all")).unwrap(),
        BTreeSet::from([A2AExposureType::RawThinking, A2AExposureType::ToolTrace])
    );
    assert!(normalize_a2a_exposure_types(Some("off"))
        .unwrap()
        .is_empty());
}

#[test]
fn format_a2a_exposure_types_uses_python_order() {
    assert_eq!(
        format_a2a_exposure_types(&BTreeSet::from([
            A2AExposureType::ToolTrace,
            A2AExposureType::RawThinking,
        ])),
        vec!["raw_thinking", "tool_trace"]
    );
}

#[test]
fn normalize_a2a_exposure_types_rejects_unsupported_types() {
    let error = normalize_a2a_exposure_tokens(["thought-summary"])
        .unwrap_err()
        .to_string();

    assert!(error.contains("Unsupported A2A thinking exposure type"));
}
